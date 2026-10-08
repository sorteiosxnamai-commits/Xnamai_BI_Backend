"""Comando administrativo do ERP: estado e carga da sincronização, sem usar a interface.

    python -m app.erp.cli status
    python -m app.erp.cli run [--resource RECURSO ...] [--timeout SEGUNDOS]

Usa as configurações do ambiente (DATABASE_URL, MERCOS_ADAPTOR_URL e chave do Adaptor).
Reaproveita fila, lease, sincronização e checkpoint do módulo: `run` agenda o que está
vencido e processa a fila por este processo, com claim atômico no banco (pode coexistir
com o scheduler do serviço web sem processar o mesmo trabalho duas vezes). Só lê do Mercos
e só grava nas tabelas `erp_*`. Nada de payload ou segredo é impresso.
"""

import argparse
import asyncio
import sys
import time

from sqlalchemy import func, select

from app.erp import queue
from app.erp.common import as_utc
from app.erp.config import erp_settings
from app.erp.db import session_scope
from app.erp.integrations.mercos_client import MercosAdaptorClient
from app.erp.models.core import ErpJob, ErpQuarantine, ErpSyncCheckpoint, ErpSyncRun
from app.erp.registry import REGISTRY, SYNC_ORDER
from app.erp.workers import drain_once, reset_schedule_clock

TERMINAL_OK = {"success", "partial_quarantine"}
TERMINAL_DENIED = {"forbidden", "unavailable"}


def collect_status(connection_id: str) -> dict:
    """Mesmas fontes que a tela de Integrações: checkpoints, execuções e fila."""
    rows = []
    with session_scope() as db:
        checkpoints = {
            c.resource: c
            for c in db.scalars(
                select(ErpSyncCheckpoint).where(ErpSyncCheckpoint.connection_id == connection_id)
            )
        }
        for resource in SYNC_ORDER:
            definition = REGISTRY[resource]
            cp = checkpoints.get(resource)
            local = int(
                db.scalar(
                    select(func.count(definition.model.id)).where(
                        definition.model.connection_id == connection_id
                    )
                )
                or 0
            )
            run = db.scalar(
                select(ErpSyncRun)
                .where(ErpSyncRun.connection_id == connection_id, ErpSyncRun.resource == resource)
                .order_by(ErpSyncRun.id.desc())
                .limit(1)
            )
            quarantined = int(
                db.scalar(
                    select(func.count(ErpQuarantine.id)).where(
                        ErpQuarantine.connection_id == connection_id,
                        ErpQuarantine.resource == resource,
                        ErpQuarantine.resolved_at.is_(None),
                    )
                )
                or 0
            )
            rows.append(
                {
                    "resource": resource,
                    "state": cp.status if cp else "never",
                    "local": local,
                    "checkpoint": cp.cursor if cp else None,
                    "lastActivity": cp.last_attempt_at if cp else None,
                    "lastSuccess": cp.last_success_at if cp else None,
                    "quarantined": quarantined,
                    "runStatus": run.status if run else None,
                    "pages": run.pages if run else 0,
                    "received": run.received if run else 0,
                    "persisted": run.persisted if run else 0,
                    "unchanged": run.unchanged if run else 0,
                    "runQuarantined": run.quarantined if run else 0,
                    "error": (cp.error or (run.error if run else None)) if (cp or run) else None,
                }
            )
        jobs = dict(
            db.execute(
                select(ErpJob.status, func.count()).where(ErpJob.connection_id == connection_id).group_by(ErpJob.status)
            ).all()
        )
    return {"resources": rows, "jobs": jobs}


def render(snapshot: dict) -> str:
    def when(value):
        return as_utc(value).strftime("%d/%m %H:%M:%S") if value else "—"

    lines = [
        f"{'recurso':<20}{'estado':<20}{'local':>8}{'recebidos':>10}{'gravados':>9}{'inalt.':>8}{'quar.':>6}  "
        f"{'últ. atividade':<15}checkpoint / motivo"
    ]
    for r in snapshot["resources"]:
        detail = (r["error"] or r["checkpoint"] or "—")
        lines.append(
            f"{r['resource']:<20}{r['state']:<20}{r['local']:>8}{r['received']:>10}{r['persisted']:>9}"
            f"{r['unchanged']:>8}{r['quarantined']:>6}  {when(r['lastActivity']):<15}{str(detail)[:70]}"
        )
    lines.append(f"fila: {snapshot['jobs'] or 'vazia'}")
    return "\n".join(lines)


def _pending_work(connection_id: str) -> int:
    """Jobs ativos que ainda vão rodar (inclui os aguardando limite de requisições)."""
    with session_scope() as db:
        return int(
            db.scalar(
                select(func.count(ErpJob.id)).where(
                    ErpJob.connection_id == connection_id, ErpJob.status.in_(queue.ACTIVE)
                )
            )
            or 0
        )


def _enqueue_resources(connection_id: str, resources: list[str]) -> list[str]:
    created = []
    for resource in resources:
        with session_scope() as db:
            _, was_created = queue.enqueue(
                db, kind="sync", connection_id=connection_id, resource=resource,
                mode="incremental", requested_by="cli",
            )
        if was_created:
            created.append(resource)
    return created


async def run(resources: list[str] | None, timeout: float) -> int:
    cfg = erp_settings()
    if not cfg.erp_enabled:
        print("ERP_ENABLED desligado: nada a fazer.", file=sys.stderr)
        return 1
    client = MercosAdaptorClient()
    if not client.configured:
        print("Adaptor não configurado (MERCOS_ADAPTOR_URL / chave).", file=sys.stderr)
        return 1
    connection_id = cfg.erp_connection_id
    if resources:
        unknown = [r for r in resources if r not in REGISTRY]
        if unknown:
            print(f"Recurso(s) desconhecido(s): {', '.join(unknown)}", file=sys.stderr)
            return 1
        print("Enfileirados:", ", ".join(_enqueue_resources(connection_id, resources)) or "nenhum (já ativos)")
    else:
        reset_schedule_clock()  # o ciclo agenda os vencidos (carga inicial + incremental)
    deadline = time.monotonic() + timeout
    last_print = 0.0
    while True:
        await drain_once(client, max_cycles=1)
        pending = await asyncio.to_thread(_pending_work, connection_id)
        now = time.monotonic()
        if now - last_print >= 5:
            snap = await asyncio.to_thread(collect_status, connection_id)
            done = sum(r["received"] for r in snap["resources"])
            print(f"[{time.strftime('%H:%M:%S')}] fila={snap['jobs']} recebidos(última execução)={done}", flush=True)
            last_print = now
        if pending == 0:
            break
        if now >= deadline:
            print(f"Tempo limite de {int(timeout)}s: a fila continua no banco e será retomada.", file=sys.stderr)
            break
        await asyncio.sleep(1.0)
    snapshot = await asyncio.to_thread(collect_status, connection_id)
    print(render(snapshot))
    bad = [
        r["resource"] for r in snapshot["resources"]
        if r["state"] not in TERMINAL_OK | TERMINAL_DENIED
    ]
    if bad:
        print("Pendentes ou com erro:", ", ".join(bad), file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.erp.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Estado atual por recurso e da fila")
    run_parser = sub.add_parser("run", help="Executa ou retoma a carga dos recursos permitidos")
    run_parser.add_argument("--resource", action="append", help="Limita a um recurso (repetível)")
    run_parser.add_argument("--timeout", type=float, default=3600.0, help="Segundos (padrão 3600)")
    args = parser.parse_args(argv)
    if args.command == "status":
        print(render(collect_status(erp_settings().erp_connection_id)))
        return 0
    return asyncio.run(run(args.resource, args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
