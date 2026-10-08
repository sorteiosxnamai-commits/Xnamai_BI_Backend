"""Worker ERP: processo separado do web (`python -m app.erp.worker`).

Consome jobs persistidos (claim atômico + lease), a outbox de escrita e o inbox
de webhooks, e agenda sincronização periódica/reconciliação. Não usa o
scheduler nem os cursores/mutex do BI.
"""

import asyncio
import logging
from datetime import timedelta

from app.erp import inbox, outbox, queue
from app.erp.common import utcnow
from app.erp.config import erp_settings
from app.erp.db import session_scope
from app.erp.integrations.mercos_client import MercosAdaptorClient
from app.erp.sync import engine

log = logging.getLogger("uvicorn.error")

SYNC_INTERVAL_SECONDS = 15 * 60
RECONCILE_INTERVAL_SECONDS = 24 * 3600


async def run_sync_job(client: MercosAdaptorClient, job) -> dict:
    """Executa um job `sync`/`hydrate`; devolve resultado e eventual reagendamento."""
    def cancelled() -> bool:
        with session_scope() as db:
            return queue.is_cancel_requested(db, job.id)

    resources = [job.resource] if job.resource and job.resource != "all" else None
    if job.kind == "hydrate":
        result = await engine.hydrate_orders(
            client, job.connection_id, job_id=job.id, lease_token=job.lease_token
        )
        return {"results": [result.as_dict()], "retryAfter": result.retry_after}
    if resources is None:
        resources = list(engine.SYNC_ORDER)
    results = []
    retry_after = None
    for resource in resources:
        outcome = await engine.sync_resource(
            client,
            job.connection_id,
            resource,
            mode=job.mode,
            job_id=job.id,
            lease_token=job.lease_token,
            cancel_requested=cancelled,
        )
        results.append(outcome.as_dict())
        if outcome.status == "lease_lost":
            break  # outro worker é o dono agora; não mexer em mais nada
        if outcome.status == "busy":
            retry_after = outcome.retry_after
            break
        if outcome.status == "waiting_rate_limit":
            # A cota é da conta inteira: parar evita gastar mais requisições.
            retry_after = outcome.retry_after
            break
        if outcome.status in ("cancelled", "interrupted"):
            break
    if retry_after is None and any(r["resource"] == "orders" for r in results):
        pending = await asyncio.to_thread(engine.pending_hydration, job.connection_id)
        if pending:
            with session_scope() as db:
                queue.enqueue(
                    db, kind="hydrate", connection_id=job.connection_id,
                    resource="orders", requested_by="worker",
                )
    return {"results": results, "retryAfter": retry_after}


async def _keep_lease(job_id: int, token: str, every: float) -> None:
    """Renova o lease enquanto o job roda; sem isto uma carga longa seria reenfileirada."""
    while True:
        await asyncio.sleep(every)

        def beat() -> bool:
            with session_scope() as db:
                return queue.heartbeat(db, job_id, token)

        try:
            if not await asyncio.to_thread(beat):
                return
        except Exception:  # noqa: BLE001
            log.exception("Heartbeat do job %s falhou", job_id)


async def process_next_job(client: MercosAdaptorClient) -> bool:
    with session_scope() as db:
        queue.release_expired(db)
        outbox.recover_stale(db)
    with session_scope() as db:
        job = queue.claim_next(db, kinds=("sync", "hydrate"))
    if job is None:
        return False
    beat = asyncio.create_task(
        _keep_lease(job.id, job.lease_token, max(erp_settings().erp_job_lease_seconds / 3, 1.0))
    )
    try:
        outcome = await run_sync_job(client, job)
    except Exception as exc:  # noqa: BLE001
        log.exception("Job ERP %s falhou", job.id)
        with session_scope() as db:
            queue.finish(db, job.id, job.lease_token, status="failed", error=str(exc)[:1000])
        return True
    finally:
        beat.cancel()
    statuses = {r["status"] for r in outcome["results"]}
    if "lease_lost" in statuses:
        log.warning("Job %s perdeu o lease; resultado descartado", job.id)
        return True  # o `finish` com o token antigo não teria efeito mesmo assim
    with session_scope() as db:
        if outcome["retryAfter"]:
            queue.finish(
                db, job.id, job.lease_token, status="queued", result=outcome,
                error="Aguardando limite de requisições do Mercos",
                run_after=utcnow() + timedelta(seconds=outcome["retryAfter"]),
            )
        elif statuses & {"failed", "interrupted", "forbidden", "unavailable"}:
            # Falha visível; o agendamento periódico decide a próxima tentativa.
            queue.finish(db, job.id, job.lease_token, status="failed", result=outcome,
                         error="; ".join(r["error"] for r in outcome["results"] if r["error"])[:1000])
        elif "cancelled" in statuses:
            queue.finish(db, job.id, job.lease_token, status="cancelled", result=outcome)
        else:
            queue.finish(db, job.id, job.lease_token, status="succeeded", result=outcome)
    return True


async def process_outbox(client: MercosAdaptorClient, limit: int = 5) -> int:
    handled = 0
    for op_id in await asyncio.to_thread(outbox.due_operation_ids, limit):
        if await outbox.dispatch_operation(client, op_id) is not None:
            handled += 1
    return handled


async def process_inbox(limit: int = 50) -> int:
    handled = 0
    for inbox_id in await asyncio.to_thread(inbox.pending_ids, limit):
        try:
            await asyncio.to_thread(inbox.process, inbox_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("Webhook %s falhou ao processar", inbox_id)
            await asyncio.to_thread(inbox.record_failure, inbox_id, f"{type(exc).__name__}: {exc}")
        handled += 1
    return handled


def schedule_periodic(connection_id: str) -> list[str]:
    """Enfileira sync incremental dos recursos vencidos (sem tempestade de jobs)."""
    queued = []
    for resource in engine.due_resources(connection_id, SYNC_INTERVAL_SECONDS):
        with session_scope() as db:
            _, created = queue.enqueue(
                db, kind="sync", connection_id=connection_id, resource=resource,
                mode="incremental", requested_by="scheduler",
            )
        if created:
            queued.append(resource)
    return queued


async def worker_loop(
    stop: asyncio.Event,
    client: MercosAdaptorClient | None = None,
    *,
    schedule: bool = True,
) -> None:
    cfg = erp_settings()
    client = client or MercosAdaptorClient()
    next_schedule = 0.0
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        try:
            if schedule and cfg.erp_enabled and client.configured and loop.time() >= next_schedule:
                await asyncio.to_thread(schedule_periodic, cfg.erp_connection_id)
                next_schedule = loop.time() + 60
            busy = await process_inbox()
            busy += int(await process_next_job(client))
            busy += await process_outbox(client)
        except Exception:  # noqa: BLE001
            log.exception("Ciclo do worker ERP falhou")
            busy = 0
        if not busy:
            try:
                await asyncio.wait_for(stop.wait(), timeout=cfg.erp_worker_poll_seconds)
            except asyncio.TimeoutError:
                pass


async def drain_once(client: MercosAdaptorClient | None = None, max_cycles: int = 50) -> int:
    """Esvazia a fila (webhooks, jobs, outbox) e volta; sem laço permanente.

    Usado pelo scheduler do BI no próprio serviço web. Não agenda sincronização
    periódica: só executa o que o operador enfileirou. O claim atômico no banco
    mantém a segurança se um worker separado também existir."""
    cfg = erp_settings()
    if not cfg.erp_enabled or not cfg.erp_queue_in_scheduler:
        return 0
    client = client or MercosAdaptorClient()
    total = 0
    for _ in range(max_cycles):
        busy = await process_inbox()
        busy += int(await process_next_job(client))
        busy += await process_outbox(client)
        total += busy
        if not busy:
            break
    return total
