"""Autenticação individual do ERP (independente do login do BI)."""

from fastapi import APIRouter, Cookie, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.erp import credentials, sessions
from app.erp.audit import audit
from app.erp.auth import ErpUser, erp_user_pending_ok, module_enabled
from app.erp.common import as_utc, http_error, iso, utcnow
from app.erp.config import erp_settings
from app.erp.db import erp_db
from app.erp.models.core import ErpOperator, ErpSession
from app.erp.schemas.commands import ChangePasswordInput, LoginInput

router = APIRouter(prefix="/auth", tags=["erp-auth"])


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/login", summary="Login individual do operador ERP")
def login(body: LoginInput, request: Request, response: Response, db: Session = Depends(erp_db)):
    module_enabled()
    return sessions.login(
        db, response, username=body.username, password=body.password, ip=_ip(request),
        user_agent=request.headers.get("user-agent"),
        connection_id=erp_settings().erp_connection_id,
    )


@router.post("/refresh", summary="Renova o acesso (refresh rotativo por cookie httpOnly)")
def refresh(
    response: Response,
    erp_refresh: str | None = Cookie(None),
    db: Session = Depends(erp_db),
):
    module_enabled()
    return sessions.refresh(db, response, erp_refresh, connection_id=erp_settings().erp_connection_id)


@router.post("/logout", status_code=204, summary="Encerra a sessão atual")
def logout(response: Response, erp_refresh: str | None = Cookie(None), db: Session = Depends(erp_db)):
    claims = sessions.decode(erp_refresh, "erp_refresh") if erp_refresh else None
    if claims:
        session = db.get(ErpSession, claims.get("sid"))
        if session is not None:
            sessions.revoke(db, session, "logout")
            db.commit()
    sessions.clear_cookie(response)


@router.post("/change-password", summary="Troca a própria senha (encerra as outras sessões)")
def change_password(
    body: ChangePasswordInput,
    user: ErpUser = Depends(erp_user_pending_ok),
    db: Session = Depends(erp_db),
):
    if user.auth_method != "erp_session":
        raise http_error(409, "no_erp_credentials", "Esta sessão não usa credencial própria do ERP")
    operator = db.scalar(select(ErpOperator).where(ErpOperator.username == user.username))
    if operator is None or not credentials.verify_password(body.currentPassword, operator.password_hash):
        audit(db, operator=user.username, action="auth.change_password",
              connection_id=erp_settings().erp_connection_id, result="failed")
        db.commit()
        raise http_error(403, "invalid_credentials", "Senha atual incorreta")
    if body.newPassword == body.currentPassword:
        raise http_error(422, "same_password", "A nova senha deve ser diferente da atual")
    sessions.set_password(
        db, operator, body.newPassword, must_change=False, actor=user.username,
        keep_session=user.session_id, connection_id=erp_settings().erp_connection_id,
    )
    db.commit()
    return {"status": "ok", "otherSessionsRevoked": True}


@router.get("/sessions", summary="Minhas sessões ativas")
def my_sessions(user: ErpUser = Depends(erp_user_pending_ok), db: Session = Depends(erp_db)):
    operator = db.scalar(select(ErpOperator).where(ErpOperator.username == user.username))
    if operator is None:
        return {"items": []}
    rows = db.scalars(
        select(ErpSession).where(
            ErpSession.operator_id == operator.id,
            ErpSession.revoked_at.is_(None),
            ErpSession.expires_at > utcnow(),
        ).order_by(ErpSession.created_at.desc())
    ).all()
    return {
        "items": [
            {"id": s.id, "current": s.id == user.session_id, "createdAt": iso(s.created_at),
             "lastUsedAt": iso(s.last_used_at), "expiresAt": iso(as_utc(s.expires_at)),
             "ip": s.ip, "userAgent": s.user_agent}
            for s in rows
        ]
    }


@router.delete("/sessions/{session_id}", status_code=204, summary="Encerra uma das minhas sessões")
def revoke_my_session(
    session_id: str, user: ErpUser = Depends(erp_user_pending_ok), db: Session = Depends(erp_db)
):
    operator = db.scalar(select(ErpOperator).where(ErpOperator.username == user.username))
    session = db.get(ErpSession, session_id)
    if operator is None or session is None or session.operator_id != operator.id:
        raise http_error(404, "not_found", "Sessão não encontrada")
    sessions.revoke(db, session, "revoked_by_user")
    db.commit()
