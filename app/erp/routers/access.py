from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.erp import capabilities, integrity
from app.erp.auth import ErpUser, erp_user, erp_user_pending_ok
from app.erp.common import as_utc, iso, utcnow
from app.erp.config import erp_settings
from app.erp.db import erp_db
from app.erp.models import commercial as m
from app.erp.models import core
from app.erp.registry import REGISTRY, SYNC_ORDER

CONTRACT_VERSION = "erp-1"

router = APIRouter(tags=["erp-access"])


def connection_id() -> str:
    return erp_settings().erp_connection_id


@router.get("/me", summary="Identidade e permissões do operador ERP")
def me(user: ErpUser = Depends(erp_user_pending_ok)):
    return {
        "username": user.username,
        "roles": user.roles,
        "permissions": sorted(user.permissions),
        "bootstrap": user.bootstrap,
        "authMethod": user.auth_method,
        "mustChangePassword": user.must_change_password,
        "connectionId": connection_id(),
        "contractVersion": CONTRACT_VERSION,
    }


@router.get("/capabilities", summary="Matriz de capacidades")
def get_capabilities(user: ErpUser = Depends(erp_user), db: Session = Depends(erp_db)):
    items = capabilities.build_matrix(db, connection_id())
    db.commit()
    return {"connectionId": connection_id(), "items": items}


MODELS = {
    "customers": m.ErpCustomer,
    "products": m.ErpProduct,
    "orders": m.ErpSalesOrder,
}


@router.get("/overview", summary="Visão operacional (dado indisponível não vira zero)")
def overview(user: ErpUser = Depends(erp_user), db: Session = Depends(erp_db)):
    cid = connection_id()
    checkpoints = {
        row.resource: row
        for row in db.scalars(
            select(core.ErpSyncCheckpoint).where(core.ErpSyncCheckpoint.connection_id == cid)
        )
    }
    resources = []
    for alias in SYNC_ORDER:
        model = REGISTRY[alias].model
        row = checkpoints.get(alias)
        synced = row is not None and row.last_success_at is not None
        count = (
            int(db.scalar(select(func.count(model.id)).where(model.connection_id == cid)) or 0)
            if synced
            else None
        )
        resources.append(
            {
                "resource": alias,
                "label": REGISTRY[alias].label,
                "status": row.status if row else "never",
                "records": count,
                "dataThrough": iso(row.data_through) if row else None,
                "lastSuccessAt": iso(row.last_success_at) if row else None,
                "unresolved": row.unresolved if row else None,
                "error": row.error if row else None,
            }
        )
    op_counts = dict(
        db.execute(
            select(core.ErpOperation.status, func.count())
            .where(core.ErpOperation.connection_id == cid)
            .group_by(core.ErpOperation.status)
        ).all()
    )
    last_success = [as_utc(r.last_success_at) for r in checkpoints.values() if r.last_success_at]
    last = min(last_success) if last_success and len(last_success) == len(SYNC_ORDER) else None
    return {
        "connectionId": cid,
        "generatedAt": utcnow().isoformat(),
        "source": "Mercos (espelho ERP)",
        # só há "dataThrough" global quando todos os 12 recursos já sincronizaram
        "dataThrough": last.isoformat() if last else None,
        "resources": resources,
        "operations": {
            status: int(op_counts.get(status, 0))
            for status in (
                "queued", "processing", "waiting_rate_limit", "succeeded",
                "failed", "unknown", "conflict",
            )
        },
        "openConflicts": int(
            db.scalar(
                select(func.count(core.ErpConflict.id)).where(
                    core.ErpConflict.connection_id == cid, core.ErpConflict.status == "open"
                )
            )
            or 0
        ),
        "pendingReferences": integrity.pending_references(db, cid),
        "enabled": erp_settings().erp_enabled,
    }
