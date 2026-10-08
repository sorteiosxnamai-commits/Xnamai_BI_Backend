"""Sincronização de entrada: carga completa/incremental dos 12 recursos.

Regras (documento, 7.1):
- página buscada fora de transação; upsert + snapshot + checkpoint na mesma;
- `nextCursor` só indica continuação; `pageCursor` é o checkpoint final;
- linha inválida vai para quarentena e impede o avanço do checkpoint;
- cursor repetido com continuação é falha visível (sem somar segundos);
- 429 agenda para depois; 403/404 viram estado legível; sem loop infinito.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.erp import capabilities, queue
from app.erp.common import as_utc, fingerprint, parse_source_instant, utcnow
from app.erp.config import erp_settings
from app.erp.db import session_scope
from app.erp.integrations.mercos_client import AdaptorError, MercosAdaptorClient
from app.erp.models.commercial import ErpSalesOrder
from app.erp.models.core import ErpJob, ErpQuarantine, ErpSyncCheckpoint, ErpSyncRun
from app.erp.registry import REGISTRY, SYNC_ORDER
from app.erp.sync.rows import (
    definition_for,
    process_row,
    quarantine,
    update_field_inventory,
)

log = logging.getLogger("uvicorn.error")

HYDRATE_BATCH_LIMIT = 50


class ResourceBusy(Exception):
    """Outro worker, com lease válido, já sincroniza este recurso."""


@dataclass
class SyncResult:
    resource: str
    status: str
    run_id: int | None = None
    pages: int = 0
    received: int = 0
    persisted: int = 0
    unchanged: int = 0
    quarantined: int = 0
    error: str | None = None
    retry_after: float | None = None
    unmapped: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "resource": self.resource,
            "status": self.status,
            "runId": self.run_id,
            "pages": self.pages,
            "received": self.received,
            "persisted": self.persisted,
            "unchanged": self.unchanged,
            "quarantined": self.quarantined,
            "error": self.error,
            "retryAfterSeconds": self.retry_after,
            "unmappedFields": self.unmapped,
        }


def overlap_cursor(cursor: str | None, seconds: int) -> str | None:
    """Pequena sobreposição reduz perda em bordas; a deduplicação absorve."""
    if not cursor or seconds <= 0:
        return cursor
    try:
        parsed = datetime.fromisoformat(cursor)
    except ValueError:
        return cursor
    return (parsed - timedelta(seconds=seconds)).isoformat()


def _checkpoint(
    db: Session, connection_id: str, resource: str, *, lock: bool = False
) -> ErpSyncCheckpoint:
    query = select(ErpSyncCheckpoint).where(
        ErpSyncCheckpoint.connection_id == connection_id,
        ErpSyncCheckpoint.resource == resource,
        ErpSyncCheckpoint.scope == "",
    )
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None:
        row = ErpSyncCheckpoint(connection_id=connection_id, resource=resource, scope="")
        db.add(row)
        db.flush()
    return row


def _begin_run(
    connection_id: str,
    resource: str,
    mode: str,
    job_id: int | None,
    lease_token: str | None = None,
) -> tuple[int, str | None]:
    with session_scope() as db:
        queue.assert_lease(db, job_id, lease_token)
        # A linha do checkpoint serializa quem começa uma execução do mesmo recurso.
        checkpoint = _checkpoint(db, connection_id, resource, lock=True)
        cursor_before = checkpoint.cursor
        now = utcnow()
        for previous in db.scalars(
            select(ErpSyncRun).where(
                ErpSyncRun.connection_id == connection_id,
                ErpSyncRun.resource == resource,
                ErpSyncRun.status == "running",
            )
        ):
            owner = db.get(ErpJob, previous.job_id) if previous.job_id else None
            if (
                owner is not None
                and owner.id != job_id
                and owner.status == "processing"
                and owner.leased_until is not None
                and as_utc(owner.leased_until) > now
            ):
                # execução viva de outro worker: não pisar nela
                raise ResourceBusy(resource)
            # dono morto (crash/restart/lease vencido): encerra a execução órfã
            previous.status = "interrupted"
            previous.finished_at = now
            previous.error = "Processo reiniciado durante a execução; retomada pelo checkpoint"
            db.add(previous)
        checkpoint.status = "running"
        checkpoint.last_attempt_at = utcnow()
        checkpoint.error = None
        db.add(checkpoint)
        run = ErpSyncRun(
            connection_id=connection_id,
            resource=resource,
            mode=mode,
            status="running",
            job_id=job_id,
            cursor_before=cursor_before,
        )
        db.add(run)
        db.flush()
        return run.id, cursor_before


def _persist_page(
    connection_id: str,
    resource: str,
    run_id: int,
    rows: list[dict],
    *,
    page_cursor: str | None,
    transport_cursor: str | None,
    pages: int,
    received: int,
    job_id: int | None = None,
    lease_token: str | None = None,
) -> dict:
    definition = definition_for(resource)
    persisted = unchanged = quarantined = 0
    with session_scope() as db:
        for row in rows:
            fallback_key = f"sem-id:{fingerprint(row)[:16]}"
            try:
                key = definition.key(row)
            except ValueError as exc:
                quarantine(db, connection_id, resource, fallback_key, str(exc), row, run_id)
                quarantined += 1
                continue
            try:
                with db.begin_nested():
                    outcome = process_row(db, connection_id, definition, row, run_id)
            except Exception as exc:  # noqa: BLE001 - qualquer falha de linha isola a linha
                quarantine(db, connection_id, resource, key, f"{type(exc).__name__}: {exc}", row, run_id)
                quarantined += 1
                continue
            if outcome == "persisted":
                persisted += 1
            else:
                unchanged += 1
        unmapped = update_field_inventory(db, connection_id, definition, rows)
        db.flush()
        unresolved = int(
            db.scalar(
                select(func.count(ErpQuarantine.id)).where(
                    ErpQuarantine.connection_id == connection_id,
                    ErpQuarantine.resource == resource,
                    ErpQuarantine.resolved_at.is_(None),
                )
            )
            or 0
        )
        checkpoint = _checkpoint(db, connection_id, resource)
        checkpoint.transport_cursor = transport_cursor or checkpoint.transport_cursor
        checkpoint.unresolved = unresolved
        # Só avança o cursor de processamento quando nada ficou pendente.
        if unresolved == 0 and page_cursor:
            checkpoint.cursor = page_cursor
            checkpoint.data_through = parse_source_instant(page_cursor) or checkpoint.data_through
        checkpoint.records = (checkpoint.records or 0) + persisted
        db.add(checkpoint)
        run = db.get(ErpSyncRun, run_id)
        run.pages = pages
        run.received = received
        run.persisted = (run.persisted or 0) + persisted
        run.unchanged = (run.unchanged or 0) + unchanged
        run.quarantined = (run.quarantined or 0) + quarantined
        run.cursor_after = checkpoint.cursor
        db.add(run)
        # O lock da linha do job só é tomado AQUI, no fim da página, imediatamente antes do
        # commit: quem perdeu o lease não confirma (rollback da página inteira), e o lock
        # dura milissegundos em vez de toda a gravação (que pode ser lenta).
        queue.assert_lease(db, job_id, lease_token)
    return {
        "persisted": persisted,
        "unchanged": unchanged,
        "quarantined": quarantined,
        "unmapped": unmapped,
    }


def _finish_run(
    connection_id: str,
    resource: str,
    run_id: int,
    *,
    status: str,
    error: str | None,
    retry_after: float | None = None,
    access: str | None = None,
    job_id: int | None = None,
    lease_token: str | None = None,
) -> None:
    with session_scope() as db:
        queue.assert_lease(db, job_id, lease_token)
        run = db.get(ErpSyncRun, run_id)
        run.status = status
        run.finished_at = utcnow()
        run.error = error
        db.add(run)
        checkpoint = _checkpoint(db, connection_id, resource)
        unresolved = checkpoint.unresolved or 0
        checkpoint.status = "partial_quarantine" if status == "success" and unresolved else status
        checkpoint.error = error
        if status == "success" and not unresolved:
            checkpoint.last_success_at = utcnow()
        checkpoint.retry_after = (
            utcnow() + timedelta(seconds=retry_after) if retry_after else None
        )
        db.add(checkpoint)
        if access is not None:
            capabilities.record_access(
                db, connection_id, f"read.{resource}", access, error if access == "denied" else None
            )


async def sync_resource(
    client: MercosAdaptorClient,
    connection_id: str,
    resource: str,
    *,
    mode: str = "incremental",
    job_id: int | None = None,
    lease_token: str | None = None,
    cancel_requested=lambda: False,
) -> SyncResult:
    definition = REGISTRY[resource]
    cfg = erp_settings()
    result = SyncResult(resource=resource, status="running")
    try:
        run_id, cursor_before = await asyncio.to_thread(
            _begin_run, connection_id, resource, mode, job_id, lease_token
        )
    except ResourceBusy:
        result.status = "busy"
        result.error = "Outro worker já sincroniza este recurso"
        result.retry_after = 30.0
        return result
    except queue.LeaseLost:
        result.status = "lease_lost"
        result.error = "Lease do job perdido antes de iniciar"
        return result
    result.run_id = run_id
    cursor = None if mode == "full" else overlap_cursor(cursor_before, cfg.erp_sync_overlap_seconds)

    async def finish(status: str, error: str | None = None, *, retry=None, access=None):
        result.status = status
        result.error = error
        result.retry_after = retry
        try:
            await asyncio.to_thread(
                _finish_run, connection_id, resource, run_id,
                status=status, error=error, retry_after=retry, access=access,
                job_id=job_id, lease_token=lease_token,
            )
        except queue.LeaseLost:
            # Quem perdeu o lease não confirma nada; o novo dono assume pelo checkpoint.
            result.status = "lease_lost"
            result.error = "Lease do job perdido; nenhuma confirmação foi gravada"
        return result

    try:
        for page_number in range(1, cfg.erp_sync_max_pages + 1):
            if not await asyncio.to_thread(queue.lease_alive, job_id, lease_token):
                result.status = "lease_lost"
                result.error = "Lease do job perdido; interrompido antes da próxima página"
                return result
            if await asyncio.to_thread(cancel_requested):
                return await finish("cancelled", "Cancelado pelo operador; nada foi desfeito")
            page = await client.list_page(definition.alias, cursor)
            result.received += len(page.data)
            if not page.data:
                break
            page_cursor = page.page_cursor or page.next_cursor
            stats = await asyncio.to_thread(
                _persist_page,
                connection_id,
                resource,
                run_id,
                page.data,
                page_cursor=page_cursor,
                transport_cursor=page.next_cursor or page_cursor,
                pages=page_number,
                received=result.received,
                job_id=job_id,
                lease_token=lease_token,
            )
            result.pages = page_number
            result.persisted += stats["persisted"]
            result.unchanged += stats["unchanged"]
            result.quarantined += stats["quarantined"]
            for name in stats["unmapped"]:
                if name not in result.unmapped:
                    result.unmapped.append(name)
            if not page.next_cursor:
                break
            if page.next_cursor == cursor:
                return await finish(
                    "failed",
                    "Cursor repetido com indicação de continuação; "
                    "possível página inteira com mesmo timestamp. Intervenção necessária.",
                )
            cursor = page.next_cursor
        else:
            return await finish(
                "partial", f"Limite de {cfg.erp_sync_max_pages} páginas; execute novamente"
            )
    except queue.LeaseLost:
        result.status = "lease_lost"
        result.error = "Lease do job perdido; a página em curso foi descartada (rollback)"
        return result
    except AdaptorError as exc:
        if exc.kind == "rate_limited":
            return await finish("waiting_rate_limit", exc.message, retry=exc.retry_after or 30.0)
        if exc.kind == "forbidden":
            return await finish("forbidden", exc.message, access="denied")
        if exc.kind == "not_found":
            return await finish("unavailable", exc.message, access="denied")
        if exc.kind in ("transport", "unavailable"):
            return await finish("interrupted", exc.message)
        return await finish("failed", exc.message)
    except Exception as exc:  # noqa: BLE001
        log.exception("ERP sync %s falhou", resource)
        return await finish("failed", f"{type(exc).__name__}: {exc}")
    return await finish("success", None, access="allowed")


def _incomplete_order_ids(connection_id: str, limit: int) -> list[str]:
    with session_scope() as db:
        return list(
            db.scalars(
                select(ErpSalesOrder.external_id)
                .where(
                    ErpSalesOrder.connection_id == connection_id,
                    ErpSalesOrder.items_complete.is_(False),
                    ErpSalesOrder.source_deleted.is_(False),
                )
                .order_by(ErpSalesOrder.issued_at.desc(), ErpSalesOrder.id.desc())
                .limit(limit)
            )
        )


def _persist_detail(
    connection_id: str, detail: dict, job_id: int | None = None, lease_token: str | None = None
) -> str:
    definition = definition_for("orders")
    with session_scope() as db:
        queue.assert_lease(db, job_id, lease_token)
        try:
            with db.begin_nested():
                return process_row(db, connection_id, definition, detail, None)
        except Exception as exc:  # noqa: BLE001
            key = str(detail.get("id") or "sem-id")
            quarantine(db, connection_id, "orders", key, f"{type(exc).__name__}: {exc}", detail, None)
            return "quarantined"


async def hydrate_orders(
    client: MercosAdaptorClient,
    connection_id: str,
    *,
    limit: int = HYDRATE_BATCH_LIMIT,
    job_id: int | None = None,
    lease_token: str | None = None,
) -> SyncResult:
    """Completa itens de pedidos cuja listagem v2 não os trouxe.

    Detalhe por ID pode estar bloqueado em produção: nesse caso a hidratação
    para com estado legível e as páginas locais continuam funcionando.
    """
    result = SyncResult(resource="orders", status="success")
    ids = await asyncio.to_thread(_incomplete_order_ids, connection_id, limit)
    for external_id in ids:
        try:
            detail = await client.get_detail("orders", external_id)
        except AdaptorError as exc:
            result.error = exc.message
            if exc.kind == "rate_limited":
                result.status = "waiting_rate_limit"
                result.retry_after = exc.retry_after or 30.0
            elif exc.kind == "forbidden":
                result.status = "forbidden"
                with session_scope() as db:
                    capabilities.record_access(
                        db, connection_id, "read.orders", "allowed",
                        "Listagem liberada; detalhe por ID indisponível (pedidos podem ficar incompletos)",
                    )
            else:
                result.status = "interrupted"
            return result
        if str(detail.get("id") or external_id) != external_id:
            result.error = f"Detalhe do pedido {external_id} retornou id divergente"
            result.status = "failed"
            return result
        try:
            outcome = await asyncio.to_thread(
                _persist_detail, connection_id, {**detail, "id": external_id}, job_id, lease_token
            )
        except queue.LeaseLost:
            result.status = "lease_lost"
            result.error = "Lease do job perdido; hidratação interrompida"
            return result
        result.received += 1
        if outcome == "persisted":
            result.persisted += 1
        elif outcome == "quarantined":
            result.quarantined += 1
        else:
            result.unchanged += 1
    return result


def pending_hydration(connection_id: str) -> int:
    with session_scope() as db:
        return int(
            db.scalar(
                select(func.count(ErpSalesOrder.id)).where(
                    ErpSalesOrder.connection_id == connection_id,
                    ErpSalesOrder.items_complete.is_(False),
                    ErpSalesOrder.source_deleted.is_(False),
                )
            )
            or 0
        )


def due_resources(connection_id: str, interval_seconds: float) -> list[str]:
    """Recursos cujo último sucesso é mais antigo que o intervalo (ordem de dependência)."""
    now = utcnow()
    due: list[str] = []
    with session_scope() as db:
        rows = {
            row.resource: row
            for row in db.scalars(
                select(ErpSyncCheckpoint).where(ErpSyncCheckpoint.connection_id == connection_id)
            )
        }
    for resource in SYNC_ORDER:
        row = rows.get(resource)
        if row is None or row.last_attempt_at is None:
            due.append(resource)
            continue
        if row.retry_after and as_utc(row.retry_after) > now:
            continue
        last = as_utc(row.last_attempt_at)
        # Recurso negado pela conta é reavaliado raramente, não a cada ciclo.
        wait = interval_seconds * (24 if row.status in ("forbidden", "unavailable") else 1)
        if (now - last).total_seconds() >= wait:
            due.append(resource)
    return due
