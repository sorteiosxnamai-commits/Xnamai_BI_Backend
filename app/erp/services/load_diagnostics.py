"""Diagnóstico somente leitura da carga de Pedidos.

Responde, só com o que está gravado (execuções, checkpoint, jobs e o espelho), a perguntas como:
a carga retoma do último cursor confirmado? quantas páginas são relidas e por quê? o que está
parado atrás dela? por que pedidos ficam sem itens/cliente? Nada aqui chama o Mercos nem altera dados."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.erp import serializers as ser
from app.erp.common import as_utc, sanitize_remote_message, utcnow
from app.erp.integrity import pending_references
from app.erp.models import commercial as m
from app.erp.models.core import ErpJob, ErpSyncCheckpoint, ErpSyncRun
from app.erp.services.order_operations import unresolved_reason
from app.erp.sync.engine import RESUMABLE_FULL_STATUSES

RESTART_MARK = "reiniciado"


def _run(r: ErpSyncRun) -> dict:
    return {
        "runId": r.id,
        "mode": r.mode,
        "status": r.status,
        "startedAt": ser.iso(r.started_at),
        "finishedAt": ser.iso(r.finished_at),
        "pages": r.pages,
        "received": r.received,
        "persisted": r.persisted,
        "unchanged": r.unchanged,
        "cursorBefore": r.cursor_before,
        "cursorAfter": r.cursor_after,
        "error": sanitize_remote_message(r.error),
    }


def _jobs(db: Session, connection_id: str) -> dict:
    active = list(
        db.scalars(
            select(ErpJob).where(
                ErpJob.connection_id == connection_id,
                ErpJob.status.in_(("queued", "processing", "waiting_rate_limit")),
            )
        )
    )
    now = utcnow()
    processing = [j for j in active if j.status == "processing"]
    queued = [j for j in active if j.status == "queued"]
    oldest = min((as_utc(j.created_at) for j in queued if j.created_at), default=None)
    return {
        "processing": [
            {
                "jobId": j.id, "kind": j.kind, "resource": j.resource, "mode": j.mode, "attempts": j.attempts,
                "heartbeatAt": ser.iso(j.heartbeat_at), "createdAt": ser.iso(j.created_at),
                "requestedBy": j.requested_by,
            }
            for j in processing
        ],
        "queued": [
            {"jobId": j.id, "kind": j.kind, "resource": j.resource, "mode": j.mode, "createdAt": ser.iso(j.created_at)}
            for j in sorted(queued, key=lambda j: j.id)
        ],
        "oldestQueuedAgeMinutes": round((now - oldest).total_seconds() / 60, 1) if oldest else None,
        # a fila é drenada um job por vez: enquanto um job longo roda, os demais esperam
        "blockedBehindProcessing": bool(processing and queued),
    }


def orders_load(db: Session, connection_id: str) -> dict:
    M = m.ErpSalesOrder
    cps = {cp.resource: cp for cp in db.scalars(select(ErpSyncCheckpoint).where(ErpSyncCheckpoint.connection_id == connection_id))}
    cp = cps.get("orders")
    runs = list(
        db.scalars(
            select(ErpSyncRun)
            .where(ErpSyncRun.connection_id == connection_id, ErpSyncRun.resource == "orders")
            .order_by(ErpSyncRun.id.desc())
            .limit(60)
        )
    )
    chrono = sorted(runs, key=lambda r: r.id)
    restarts = [r for r in chrono if r.status == "interrupted" and RESTART_MARK in (r.error or "")]
    findings: list[dict] = []
    previous_full = None
    for r in chrono:
        if r.mode != "full":
            continue
        if previous_full is not None and previous_full.status in RESUMABLE_FULL_STATUSES:
            resumed = r.cursor_before is not None
            if not resumed and (previous_full.cursor_after or previous_full.pages):
                findings.append(
                    {
                        "code": "full_run_restarted_from_start",
                        "runId": r.id,
                        "message": "Carga completa recomeçou da primeira página apesar de a anterior ter progresso confirmado",
                        "previousRunId": previous_full.id,
                        "previousCursorAfter": previous_full.cursor_after,
                    }
                )
        previous_full = r
    received = sum(r.received or 0 for r in chrono)
    unchanged = sum(r.unchanged or 0 for r in chrono)
    persisted = sum(r.persisted or 0 for r in chrono)
    if received and unchanged == received:
        findings.append(
            {
                "code": "all_records_reread",
                "message": "Todos os registros recebidos nas últimas execuções já estavam gravados (sem mudança)",
            }
        )
    for job in db.scalars(
        select(ErpJob).where(
            ErpJob.connection_id == connection_id, ErpJob.status == "processing", ErpJob.attempts > 1
        )
    ):
        # mais de uma tomada do mesmo job: o processo morreu ou o lease expirou e a fila o devolveu
        findings.append(
            {
                "code": "job_reclaimed_after_lease_expiry",
                "jobId": job.id,
                "attempts": job.attempts,
                "message": f"O job {job.id} já foi tomado {job.attempts} vezes (reinício do processo ou lease vencido)",
            }
        )
    orders_total = int(db.scalar(select(func.count(M.id)).where(M.connection_id == connection_id)) or 0)
    span = db.execute(
        select(func.min(M.issue_date), func.max(M.issue_date), func.max(M.source_updated_at), func.max(M.captured_at))
        .where(M.connection_id == connection_id)
    ).one()
    # --- pedidos sem itens, sem cliente e referências históricas (separados)
    no_items = [
        (kind, int(total))
        for kind, total in db.execute(
            select(M.kind, func.count()).where(
                M.connection_id == connection_id, M.items_complete.is_(False), M.source_deleted.is_(False)
            ).group_by(M.kind)
        )
    ]
    no_items_span = db.execute(
        select(func.min(M.issue_date), func.max(M.issue_date)).where(
            M.connection_id == connection_id, M.items_complete.is_(False)
        )
    ).one()
    known_customers = select(m.ErpCustomer.external_id).where(m.ErpCustomer.connection_id == connection_id)
    missing_customer = (M.connection_id == connection_id, M.customer_external_id.is_not(None),
                        M.customer_external_id.not_in(known_customers))
    missing_count = int(db.scalar(select(func.count(M.id)).where(*missing_customer)) or 0)
    missing_ids = list(db.scalars(
        select(M.customer_external_id).where(*missing_customer).distinct().order_by(M.customer_external_id).limit(25)
    ))
    missing_distinct = int(db.scalar(select(func.count(func.distinct(M.customer_external_id))).where(*missing_customer)) or 0)
    missing_span = db.execute(select(func.min(M.issue_date), func.max(M.issue_date)).where(*missing_customer)).one()
    null_customer = int(db.scalar(select(func.count(M.id)).where(M.connection_id == connection_id, M.customer_external_id.is_(None))) or 0)
    # total zerado com itens que somam valor: o zero veio do Mercos (mesmo mapeamento do BI), mas não bate com os itens
    item_sums = (
        select(m.ErpSalesOrderItem.order_id.label("order_id"), func.sum(m.ErpSalesOrderItem.total).label("items_total"))
        .where(m.ErpSalesOrderItem.excluded.is_(False))
        .group_by(m.ErpSalesOrderItem.order_id)
        .subquery()
    )
    zero_rows = db.execute(
        select(M.external_id, M.kind, M.issue_date, M.source_updated_at, item_sums.c.items_total)
        .join(item_sums, item_sums.c.order_id == M.id)
        .where(
            M.connection_id == connection_id, M.items_complete.is_(True),
            func.coalesce(M.net_total, 0) == 0, item_sums.c.items_total > 0,
        )
        .order_by(M.source_updated_at.desc())
    ).all()
    zero_by_kind: dict[str, int] = {}
    for row in zero_rows:
        zero_by_kind[row[1] or "unknown"] = zero_by_kind.get(row[1] or "unknown", 0) + 1
    zero_dates = [r[2] for r in zero_rows if r[2]]
    code, reason = unresolved_reason(cps["customers"].status if "customers" in cps else None)
    last_hydrate = db.scalar(
        select(ErpJob).where(ErpJob.connection_id == connection_id, ErpJob.kind == "hydrate").order_by(ErpJob.id.desc()).limit(1)
    )
    return {
        "generatedAt": ser.iso(utcnow()),
        "checkpoint": {
            "status": cp.status if cp else "never",
            "cursor": cp.cursor if cp else None,
            "dataThrough": ser.iso(cp.data_through) if cp else None,
            "lastAttemptAt": ser.iso(cp.last_attempt_at) if cp else None,
            "lastSuccessAt": ser.iso(cp.last_success_at) if cp else None,
            "recordsPersistedTotal": cp.records if cp else None,
        },
        "mirror": {
            "orders": orders_total,
            "orderDate": {"min": span[0].isoformat() if span[0] else None, "max": span[1].isoformat() if span[1] else None},
            "sourceUpdatedAtMax": ser.iso(span[2]),
            "capturedAtMax": ser.iso(span[3]),
        },
        "runs": {
            "recent": [_run(r) for r in runs[:15]],
            "restartsRecorded": len(restarts),
            "restartTimes": [ser.iso(r.finished_at) for r in restarts][-10:],
            "received": received,
            "persisted": persisted,
            "unchanged": unchanged,
            "rereadRatio": round(unchanged / received, 4) if received else None,
            "intentionalOverlapSeconds": 5,
        },
        "findings": findings,
        "jobs": _jobs(db, connection_id),
        "hydration": {
            "pendingOrders": int(sum(total for _, total in no_items)),
            "lastJob": (
                {"jobId": last_hydrate.id, "status": last_hydrate.status, "error": sanitize_remote_message(last_hydrate.error),
                 "createdAt": ser.iso(last_hydrate.created_at), "finishedAt": ser.iso(last_hydrate.finished_at)}
                if last_hydrate else None
            ),
            "note": "A hidratação de itens só é enfileirada quando um job de pedidos termina ou é interrompido",
        },
        "ordersWithoutItems": {
            "total": sum(total for _, total in no_items),
            "byKind": dict(no_items),
            "orderDate": {"min": no_items_span[0].isoformat() if no_items_span[0] else None,
                          "max": no_items_span[1].isoformat() if no_items_span[1] else None},
        },
        "ordersWithoutCustomer": {
            "customerIdMissingInMirror": {
                "orders": missing_count,
                "distinctCustomerIds": missing_distinct,
                "sampleCustomerIds": missing_ids,
                "orderDate": {"min": missing_span[0].isoformat() if missing_span[0] else None,
                              "max": missing_span[1].isoformat() if missing_span[1] else None},
                "reasonCode": code,
                "reason": reason,
            },
            "withoutCustomerIdInSource": null_customer,
        },
        "ordersTotalZeroWithItems": {
            "total": len(zero_rows),
            "byKind": zero_by_kind,
            "orderDate": {
                "min": min(zero_dates).isoformat() if zero_dates else None,
                "max": max(zero_dates).isoformat() if zero_dates else None,
            },
            "sample": [
                {"id": r[0], "kind": r[1], "itemsTotal": ser.money(r[4]), "sourceUpdatedAt": ser.iso(r[3])}
                for r in zero_rows[:15]
            ],
            "note": "Total líquido 0 no Mercos, mas os itens somam valor. O ERP mostra o total recebido; não o recalcula.",
        },
        "unresolvedReferences": [p for p in pending_references(db, connection_id) if p["resource"] == "orders"],
    }
