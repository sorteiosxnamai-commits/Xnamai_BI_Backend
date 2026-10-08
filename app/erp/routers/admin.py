"""Administração ERP: operadores/permissões e auditoria."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.erp import credentials, sessions
from app.erp.audit import audit
from app.erp.auth import ROLES, ErpUser, require
from app.erp.common import as_utc, http_error, iso, paginate, utcnow
from app.erp.config import erp_settings
from app.erp.db import erp_db
from app.erp.models.core import ErpAuditEvent, ErpOperator, ErpSession
from app.erp.schemas.commands import OperatorInput, SetPasswordInput

router = APIRouter(tags=["erp-admin"])


def _operator(o: ErpOperator, active_sessions: int = 0) -> dict:
    locked = bool(o.locked_until and as_utc(o.locked_until) > utcnow())
    return {
        "username": o.username, "displayName": o.display_name, "roles": o.roles,
        "active": o.active, "createdAt": iso(o.created_at), "updatedAt": iso(o.updated_at),
        "hasPassword": bool(o.password_hash), "mustChangePassword": bool(o.must_change_password),
        "locked": locked, "lastLoginAt": iso(o.last_login_at), "activeSessions": active_sessions,
    }


def _session_counts(db: Session) -> dict[int, int]:
    rows = db.execute(
        select(ErpSession.operator_id, func.count()).where(
            ErpSession.revoked_at.is_(None), ErpSession.expires_at > utcnow()
        ).group_by(ErpSession.operator_id)
    ).all()
    return {operator_id: int(count) for operator_id, count in rows}


@router.get("/operators", summary="Operadores ERP")
def list_operators(user: ErpUser = Depends(require("*")), db: Session = Depends(erp_db)):
    rows = db.scalars(select(ErpOperator).order_by(ErpOperator.username)).all()
    counts = _session_counts(db)
    return {"items": [_operator(o, counts.get(o.id, 0)) for o in rows], "roles": list(ROLES)}


@router.put("/operators", summary="Cria ou atualiza operador (vínculo explícito com o login)")
def upsert_operator(
    body: OperatorInput, user: ErpUser = Depends(require("*")), db: Session = Depends(erp_db)
):
    invalid = [role for role in body.roles if role not in ROLES]
    if invalid:
        raise http_error(422, "invalid_role", "Papel inválido", invalid=invalid, allowed=list(ROLES))
    username = body.username.strip().casefold()
    row = db.scalar(select(ErpOperator).where(ErpOperator.username == username))
    if row is None:
        row = ErpOperator(username=username)
    row.display_name = body.displayName or username
    row.roles = body.roles
    row.active = body.active
    db.add(row)
    db.flush()
    if not body.active:
        sessions.revoke_all(db, row.id, "operator_deactivated")
    audit(
        db, operator=user.username, action="operator.upsert",
        connection_id=erp_settings().erp_connection_id, resource="operator", resource_id=username,
        detail={"roles": body.roles, "active": body.active},
    )
    db.commit()
    return _operator(row)


@router.post("/operators/{username}/password", summary="Define senha temporária (exibida uma única vez)")
def set_operator_password(
    username: str,
    body: SetPasswordInput,
    user: ErpUser = Depends(require("*")),
    db: Session = Depends(erp_db),
):
    row = db.scalar(select(ErpOperator).where(ErpOperator.username == username.strip().casefold()))
    if row is None:
        raise http_error(404, "not_found", "Operador não encontrado")
    temporary = body.temporaryPassword or credentials.generate_temporary_password(row.username)
    sessions.set_password(
        db, row, temporary, must_change=True, actor=user.username,
        connection_id=erp_settings().erp_connection_id,
    )
    db.commit()
    return {"username": row.username, "temporaryPassword": temporary, "mustChangePassword": True,
            "note": "Entregue por canal seguro. Não será exibida novamente."}


@router.post("/operators/{username}/revoke-sessions", summary="Encerra todas as sessões do operador")
def revoke_operator_sessions(
    username: str, user: ErpUser = Depends(require("*")), db: Session = Depends(erp_db)
):
    row = db.scalar(select(ErpOperator).where(ErpOperator.username == username.strip().casefold()))
    if row is None:
        raise http_error(404, "not_found", "Operador não encontrado")
    revoked = sessions.revoke_all(db, row.id, "revoked_by_admin")
    audit(db, operator=user.username, action="auth.sessions_revoked",
          connection_id=erp_settings().erp_connection_id, resource="operator",
          resource_id=row.username, detail={"sessionsRevoked": revoked})
    db.commit()
    return {"username": row.username, "sessionsRevoked": revoked}


@router.get("/audit-events", summary="Auditoria (restrita)")
def list_audit_events(
    action: str | None = None,
    operator: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: ErpUser = Depends(require("audit:read")),
    db: Session = Depends(erp_db),
):
    M = ErpAuditEvent
    query = select(M)
    if action:
        query = query.where(M.action == action)
    if operator:
        query = query.where(M.operator == operator)
    result, key, direction = paginate(
        db, query, id_column=M.id, sort_columns={"at": M.at}, sort=None,
        default_sort="at", order="desc", page=page, page_size=page_size,
    )
    return result.envelope(
        lambda e: {
            "id": e.id, "at": iso(e.at), "operator": e.operator, "action": e.action,
            "resource": e.resource, "resourceId": e.resource_id, "result": e.result,
            "reason": e.reason, "correlationId": e.correlation_id, "detail": e.detail,
        },
        sort=key, order=direction, filters={"action": action, "operator": operator},
    )
