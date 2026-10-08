"""Integração: status, jobs de sync, execuções, operações, conflitos e inventário."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.erp import capabilities, outbox, queue
from app.erp import serializers as ser
from app.erp.audit import audit
from app.erp.auth import ErpUser, require
from app.erp.common import http_error, iso, paginate, utcnow
from app.erp.config import erp_settings
from app.erp.db import erp_db
from app.erp.integrations.mercos_client import AdaptorError, MercosAdaptorClient
from app.erp.integrity import pending_references
from app.erp.models import core
from app.erp.registry import REGISTRY, SYNC_ORDER
from app.erp.schemas.commands import ConflictResolveInput, ReconcileInput, SyncRequest

router = APIRouter(prefix="/integration", tags=["erp-integration"])

PAGE = Query(1, ge=1)
PAGE_SIZE = Query(25, ge=1, le=100)
ORDER = Query("desc", pattern="^(asc|desc)$")


def cid() -> str:
    return erp_settings().erp_connection_id


def _checkpoint(row) -> dict:
    return {
        "status": row.status,
        "cursor": row.cursor,
        "transportCursor": row.transport_cursor,
        "dataThrough": iso(row.data_through),
        "lastSuccessAt": iso(row.last_success_at),
        "lastAttemptAt": iso(row.last_attempt_at),
        "records": row.records,
        "unresolved": row.unresolved,
        "retryAfter": iso(row.retry_after),
        "error": row.error,
    }


@router.get("/status", summary="Estado da integração por recurso")
def integration_status(
    user: ErpUser = Depends(require("integration:read")), db: Session = Depends(erp_db)
):
    connection = cid()
    cfg = erp_settings()
    checkpoints = {
        r.resource: r
        for r in db.scalars(
            select(core.ErpSyncCheckpoint).where(core.ErpSyncCheckpoint.connection_id == connection)
        )
    }
    unmapped = dict(
        db.execute(
            select(core.ErpFieldInventory.resource, func.count())
            .where(
                core.ErpFieldInventory.connection_id == connection,
                core.ErpFieldInventory.mapped.is_(False),
            )
            .group_by(core.ErpFieldInventory.resource)
        ).all()
    )
    resources = []
    for alias in SYNC_ORDER:
        definition = REGISTRY[alias]
        row = checkpoints.get(alias)
        local = int(
            db.scalar(
                select(func.count(definition.model.id)).where(
                    definition.model.connection_id == connection
                )
            )
            or 0
        )
        resources.append(
            {
                "resource": alias,
                "label": definition.label,
                "upstream": f"{definition.upstream} ({definition.version})",
                "localRecords": local if row and row.last_success_at else None,
                "unmappedFields": int(unmapped.get(alias, 0)),
                **(_checkpoint(row) if row else {"status": "never"}),
            }
        )
    jobs = dict(
        db.execute(
            select(core.ErpJob.status, func.count())
            .where(core.ErpJob.connection_id == connection)
            .group_by(core.ErpJob.status)
        ).all()
    )
    return {
        "connectionId": connection,
        "adaptorConfigured": bool(cfg.adaptor_url and cfg.read_key),
        "writeKeyConfigured": cfg.has_write_key,
        "webhookConfigured": bool(cfg.erp_webhook_secret_hex.strip()),
        "resources": resources,
        "jobs": {k: int(v) for k, v in jobs.items()},
        "pendingReferences": pending_references(db, connection),
        "capabilities": capabilities.build_matrix(db, connection),
    }


def _job(job: core.ErpJob) -> dict:
    return {
        "jobId": job.id,
        "kind": job.kind,
        "resource": job.resource,
        "mode": job.mode,
        "status": job.status,
        "attempts": job.attempts,
        "runAfter": iso(job.run_after),
        "result": job.result,
        "error": job.error,
        "cancelRequested": job.cancel_requested,
        "createdAt": iso(job.created_at),
        "finishedAt": iso(job.finished_at),
        "statusUrl": f"/api/v1/erp/integration/jobs/{job.id}",
    }


@router.post("/sync", status_code=202, summary="Agenda sincronização (nunca lê catálogo na requisição)")
def request_sync(
    body: SyncRequest,
    user: ErpUser = Depends(require("integration:sync")),
    db: Session = Depends(erp_db),
):
    if body.resource != "all" and body.resource not in REGISTRY:
        raise http_error(404, "unknown_resource", "Recurso inválido", allowed=["all", *SYNC_ORDER])
    job, created = queue.enqueue(
        db, kind="sync", connection_id=cid(), resource=body.resource,
        mode="full" if body.full else "incremental", requested_by=user.username,
    )
    audit(
        db, operator=user.username, action="integration.sync", connection_id=cid(),
        resource=body.resource, resource_id=job.id, result="queued" if created else "already_queued",
        detail={"full": body.full},
    )
    db.commit()
    return JSONResponse(status_code=202, content={**_job(job), "created": created})


@router.post("/discover", summary="Descobre o que o Adaptor oferece (GET /v1/capabilities)")
async def discover_adaptor(
    user: ErpUser = Depends(require("integration:sync")), db: Session = Depends(erp_db)
):
    client = MercosAdaptorClient()
    try:
        payload = await client.get_capabilities()
    except AdaptorError as exc:
        raise http_error(502 if exc.kind != "forbidden" else 403, "adaptor_" + exc.kind, exc.message) from exc
    result = capabilities.apply_discovery(db, cid(), payload)
    audit(db, operator=user.username, action="integration.discover", connection_id=cid(),
          result=result["adaptor"], detail={k: v for k, v in result.items() if k != "scopes"})
    db.commit()
    return result


@router.get("/jobs/{job_id}", summary="Acompanha um job")
def get_job(
    job_id: int, user: ErpUser = Depends(require("integration:read")), db: Session = Depends(erp_db)
):
    job = db.get(core.ErpJob, job_id)
    if job is None or job.connection_id != cid():
        raise http_error(404, "not_found", "Job não encontrado")
    return _job(job)


@router.post("/jobs/{job_id}/cancel", summary="Cancela pendências futuras (não desfaz o aceito)")
def cancel_job(
    job_id: int, user: ErpUser = Depends(require("integration:sync")), db: Session = Depends(erp_db)
):
    job = db.get(core.ErpJob, job_id)
    if job is None or job.connection_id != cid():
        raise http_error(404, "not_found", "Job não encontrado")
    queue.cancel(db, job_id)
    audit(db, operator=user.username, action="integration.cancel", connection_id=cid(),
          resource="job", resource_id=job_id)
    db.commit()
    return _job(job)


def _run(run: core.ErpSyncRun) -> dict:
    return {
        "id": run.id,
        "resource": run.resource,
        "mode": run.mode,
        "status": run.status,
        "jobId": run.job_id,
        "startedAt": iso(run.started_at),
        "finishedAt": iso(run.finished_at),
        "cursorBefore": run.cursor_before,
        "cursorAfter": run.cursor_after,
        "pages": run.pages,
        "received": run.received,
        "persisted": run.persisted,
        "unchanged": run.unchanged,
        "quarantined": run.quarantined,
        "error": run.error,
    }


@router.get("/runs", summary="Execuções de sincronização")
def list_runs(
    resource: str | None = None,
    status: str | None = None,
    page: int = PAGE,
    page_size: int = PAGE_SIZE,
    order: str = ORDER,
    user: ErpUser = Depends(require("integration:read")),
    db: Session = Depends(erp_db),
):
    M = core.ErpSyncRun
    query = select(M).where(M.connection_id == cid())
    if resource:
        query = query.where(M.resource == resource)
    if status:
        query = query.where(M.status == status)
    result, key, direction = paginate(
        db, query, id_column=M.id, sort_columns={"startedAt": M.started_at},
        sort=None, default_sort="startedAt", order=order, page=page, page_size=page_size,
    )
    return result.envelope(_run, sort=key, order=direction,
                           filters={"resource": resource, "status": status})


@router.get("/runs/{run_id}", summary="Detalhe da execução")
def get_run(
    run_id: int, user: ErpUser = Depends(require("integration:read")), db: Session = Depends(erp_db)
):
    run = db.get(core.ErpSyncRun, run_id)
    if run is None or run.connection_id != cid():
        raise http_error(404, "not_found", "Execução não encontrada")
    quarantine = db.scalars(
        select(core.ErpQuarantine).where(
            core.ErpQuarantine.connection_id == cid(), core.ErpQuarantine.run_id == run_id
        ).limit(50)
    ).all()
    return {
        **_run(run),
        "quarantine": [
            {"externalKey": q.external_key, "reason": q.reason, "resolved": q.resolved_at is not None}
            for q in quarantine
        ],
    }


@router.get("/operations", summary="Operações de escrita externa (outbox)")
def list_operations(
    status: str | None = None,
    kind: str | None = None,
    page: int = PAGE,
    page_size: int = PAGE_SIZE,
    order: str = ORDER,
    user: ErpUser = Depends(require("integration:read")),
    db: Session = Depends(erp_db),
):
    M = core.ErpOperation
    query = select(M).where(M.connection_id == cid())
    if status:
        query = query.where(M.status == status)
    if kind:
        query = query.where(M.kind == kind)
    if not user.can("*") and not user.can("integration:sync"):
        query = query.where(M.operator == user.username)
    result, key, direction = paginate(
        db, query, id_column=M.id, sort_columns={"createdAt": M.created_at},
        sort=None, default_sort="createdAt", order=order, page=page, page_size=page_size,
    )
    return result.envelope(lambda op: ser.operation(op, user), sort=key, order=direction,
                           filters={"status": status, "kind": kind})


def _operation(db: Session, user: ErpUser, operation_id: str) -> core.ErpOperation:
    op = db.get(core.ErpOperation, operation_id)
    if op is None or op.connection_id != cid():
        raise http_error(404, "not_found", "Operação não encontrada")
    if not (user.can("*") or user.can("integration:sync") or op.operator == user.username):
        raise http_error(403, "forbidden", "Operação de outro operador")
    return op


@router.get("/operations/{operation_id}", summary="Detalhe e evidências da operação")
def get_operation(
    operation_id: str,
    user: ErpUser = Depends(require("integration:read")),
    db: Session = Depends(erp_db),
):
    op = _operation(db, user, operation_id)
    return ser.operation(op, user, detail=True)


@router.post("/operations/{operation_id}/reconcile", summary="Reconcilia operação em estado unknown")
def reconcile_operation(
    operation_id: str,
    body: ReconcileInput,
    user: ErpUser = Depends(require("integration:resolve")),
    db: Session = Depends(erp_db),
):
    op = _operation(db, user, operation_id)
    outbox.reconcile(
        db, op, user, decision=body.decision, external_id=body.externalId, note=body.note
    )
    db.commit()
    return ser.operation(op, user, detail=True)


# atributos de cliente (nomes das colunas) que são dados pessoais
PII_ATTRS = {
    "document", "email", "phone", "mobile", "street", "number", "complement", "district",
    "zip_code", "state_registration", "notes",
}


def _mask_attrs(values: dict | None, hide: bool) -> dict | None:
    if not hide or values is None:
        return values
    return {k: ("[restrito]" if k in PII_ATTRS else v) for k, v in values.items()}


def _conflict(row: core.ErpConflict, user: ErpUser) -> dict:
    # Conflito de cliente carrega valores de campos pessoais: mesma regra de pii:read.
    hide = row.entity_type == "customers" and not user.can("pii:read")
    mask = ser.strip_pii if hide else (lambda value: value)
    fields = ["[restrito]" if hide and f in PII_ATTRS else f for f in row.fields]
    return {
        "id": row.id,
        "operationId": row.operation_id,
        "entityType": row.entity_type,
        "entityId": row.entity_external_id,
        "fields": fields,
        "base": _mask_attrs(row.base, hide),
        "local": mask(row.local),
        "external": _mask_attrs(row.external, hide),
        "status": row.status,
        "resolution": row.resolution,
        "resolvedBy": row.resolved_by,
        "createdAt": iso(row.created_at),
        "resolvedAt": iso(row.resolved_at),
    }


@router.get("/conflicts", summary="Conflitos base/local/externo")
def list_conflicts(
    status: str | None = None,
    page: int = PAGE,
    page_size: int = PAGE_SIZE,
    order: str = ORDER,
    user: ErpUser = Depends(require("integration:read")),
    db: Session = Depends(erp_db),
):
    M = core.ErpConflict
    query = select(M).where(M.connection_id == cid())
    if status:
        query = query.where(M.status == status)
    result, key, direction = paginate(
        db, query, id_column=M.id, sort_columns={"createdAt": M.created_at},
        sort=None, default_sort="createdAt", order=order, page=page, page_size=page_size,
    )
    return result.envelope(lambda row: _conflict(row, user), sort=key, order=direction, filters={"status": status})


@router.post("/conflicts/{conflict_id}/resolve", summary="Resolve conflito")
def resolve_conflict(
    conflict_id: int,
    body: ConflictResolveInput,
    user: ErpUser = Depends(require("integration:resolve")),
    db: Session = Depends(erp_db),
):
    row = db.get(core.ErpConflict, conflict_id)
    if row is None or row.connection_id != cid():
        raise http_error(404, "not_found", "Conflito não encontrado")
    outbox.resolve_conflict(db, row, user, resolution=body.resolution, note=body.note)
    db.commit()
    return _conflict(row, user)


@router.get("/quarantine", summary="Linhas em quarentena")
def list_quarantine(
    resource: str | None = None,
    resolved: bool = False,
    page: int = PAGE,
    page_size: int = PAGE_SIZE,
    user: ErpUser = Depends(require("integration:read")),
    db: Session = Depends(erp_db),
):
    M = core.ErpQuarantine
    query = select(M).where(M.connection_id == cid())
    query = query.where(M.resolved_at.is_not(None) if resolved else M.resolved_at.is_(None))
    if resource:
        query = query.where(M.resource == resource)
    result, key, direction = paginate(
        db, query, id_column=M.id, sort_columns={"createdAt": M.created_at},
        sort=None, default_sort="createdAt", order="desc", page=page, page_size=page_size,
    )
    return result.envelope(
        lambda q: {
            "id": q.id, "resource": q.resource, "externalKey": q.external_key,
            "reason": q.reason, "attempts": q.attempts, "createdAt": iso(q.created_at),
            "resolvedAt": iso(q.resolved_at),
        },
        sort=key, order=direction, filters={"resource": resource, "resolved": resolved},
    )


@router.get("/fields", summary="Inventário de campos de origem (mapeados ou não)")
def list_fields(
    resource: str | None = None,
    mapped: bool | None = None,
    page: int = PAGE,
    page_size: int = Query(50, ge=1, le=200),
    user: ErpUser = Depends(require("integration:read")),
    db: Session = Depends(erp_db),
):
    M = core.ErpFieldInventory
    query = select(M).where(M.connection_id == cid())
    if resource:
        query = query.where(M.resource == resource)
    if mapped is not None:
        query = query.where(M.mapped == mapped)
    result, key, direction = paginate(
        db, query, id_column=M.id,
        sort_columns={"resource": M.resource, "key": M.source_key, "seen": M.seen_count},
        sort=None, default_sort="resource", order="asc", page=page, page_size=page_size,
    )
    return result.envelope(
        lambda f: {
            "resource": f.resource, "sourceKey": f.source_key, "mapped": f.mapped,
            "seenCount": f.seen_count, "sampleType": f.sample_type,
            "firstSeenAt": iso(f.first_seen_at), "lastSeenAt": iso(f.last_seen_at),
        },
        sort=key, order=direction, filters={"resource": resource, "mapped": mapped},
    )


@router.get("/webhooks", summary="Fila de webhooks recebidos")
def list_webhooks(
    page: int = PAGE,
    page_size: int = PAGE_SIZE,
    user: ErpUser = Depends(require("integration:sync")),
    db: Session = Depends(erp_db),
):
    M = core.ErpWebhookInbox
    query = select(M).where(M.connection_id == cid())
    result, key, direction = paginate(
        db, query, id_column=M.id, sort_columns={"receivedAt": M.received_at},
        sort=None, default_sort="receivedAt", order="desc", page=page, page_size=page_size,
    )
    return result.envelope(
        lambda w: {
            "id": w.id, "event": w.event, "status": w.status, "attempts": w.attempts,
            "receivedAt": iso(w.received_at), "processedAt": iso(w.processed_at),
            "result": w.result, "error": w.error,
        },
        sort=key, order=direction, filters={},
    )


@router.get("/snapshots/{resource}/{external_id}", summary="Snapshot de origem (restrito)")
def get_snapshot(
    resource: str,
    external_id: str,
    user: ErpUser = Depends(require("snapshots:read")),
    db: Session = Depends(erp_db),
):
    rows = db.scalars(
        select(core.ErpSourceSnapshot).where(
            core.ErpSourceSnapshot.connection_id == cid(),
            core.ErpSourceSnapshot.resource == resource,
            core.ErpSourceSnapshot.external_key == external_id,
        ).order_by(core.ErpSourceSnapshot.captured_at.desc()).limit(5)
    ).all()
    if not rows:
        raise http_error(404, "not_found", "Snapshot não encontrado")
    audit(db, operator=user.username, action="snapshot.read", connection_id=cid(),
          resource=resource, resource_id=external_id)
    db.commit()
    return {
        "items": [
            {"fingerprint": r.fingerprint, "capturedAt": iso(r.captured_at),
             "retentionUntil": iso(r.retention_until), "payload": r.payload}
            for r in rows
        ],
        "retrievedAt": utcnow().isoformat(),
    }
