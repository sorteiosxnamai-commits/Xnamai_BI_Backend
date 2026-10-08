"""Sessões individuais do ERP: access curto, refresh rotativo, revogação e bloqueio.

Reuso de um refresh já rotacionado indica token copiado: a sessão inteira é
revogada. Contas bloqueiam após falhas seguidas (contador no banco, vale entre
processos). Mensagens de login nunca revelam se o usuário existe.
"""

import hashlib
from datetime import timedelta
from uuid import uuid4

from fastapi import Response
from jose import JWTError, jwt
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import settings as bi_settings
from app.erp import credentials
from app.erp.audit import audit
from app.erp.common import as_utc, http_error, utcnow
from app.erp.config import erp_settings
from app.erp.models.core import ErpOperator, ErpSession

ALGORITHM = "HS256"
COOKIE = "erp_refresh"
COOKIE_PATH = "/api/v1/erp/auth"


def _secret() -> str:
    cfg = erp_settings()
    if cfg.erp_jwt_secret:
        return cfg.erp_jwt_secret
    # Derivado: um token do BI nunca valida como token ERP, nem o inverso.
    return hashlib.sha256(("erp:" + bi_settings().jwt_secret).encode()).hexdigest()


def _encode(claims: dict, lifetime: timedelta) -> str:
    now = utcnow()
    return jwt.encode({**claims, "iat": now, "exp": now + lifetime}, _secret(), algorithm=ALGORITHM)


def decode(token: str, expected: str) -> dict | None:
    try:
        payload = jwt.decode(token, _secret(), algorithms=[ALGORITHM])
    except JWTError:
        return None
    return payload if payload.get("type") == expected else None


def issue_access(operator: ErpOperator, session_id: str) -> tuple[str, int]:
    seconds = erp_settings().erp_access_minutes * 60
    token = _encode(
        {"sub": operator.username, "sid": session_id, "type": "erp_access"},
        timedelta(seconds=seconds),
    )
    return token, seconds


def _set_cookie(response: Response, session: ErpSession) -> None:
    cfg = bi_settings()
    days = erp_settings().erp_refresh_days
    token = _encode(
        {"sub": str(session.operator_id), "sid": session.id, "jti": session.refresh_jti, "type": "erp_refresh"},
        timedelta(days=days),
    )
    response.set_cookie(
        COOKIE,
        token,
        max_age=days * 86400,
        httponly=True,
        secure=cfg.auth_cookie_secure,
        samesite=cfg.auth_cookie_samesite,
        path=COOKIE_PATH,
    )


def clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path=COOKIE_PATH)


def _tokens(operator: ErpOperator, session: ErpSession, response: Response) -> dict:
    access, seconds = issue_access(operator, session.id)
    _set_cookie(response, session)
    return {
        "accessToken": access,
        "tokenType": "bearer",
        "expiresIn": seconds,
        "user": {
            "username": operator.username,
            "displayName": operator.display_name,
            "roles": operator.roles or [],
            "mustChangePassword": bool(operator.must_change_password),
        },
    }


def login(
    db: Session,
    response: Response,
    *,
    username: str,
    password: str,
    ip: str | None,
    user_agent: str | None,
    connection_id: str,
) -> dict:
    cfg = erp_settings()
    name = (username or "").strip().casefold()
    operator = db.scalar(select(ErpOperator).where(ErpOperator.username == name))
    now = utcnow()
    if operator is not None and operator.locked_until and as_utc(operator.locked_until) > now:
        audit(db, operator=name, action="auth.login", connection_id=connection_id,
              result="locked", detail={"ip": ip})
        db.commit()
        raise http_error(423, "account_locked", "Conta temporariamente bloqueada por tentativas inválidas")
    valid = credentials.verify_password(password or "", operator.password_hash if operator else None)
    if operator is None or not valid or not operator.active:
        if operator is not None and operator.password_hash:
            operator.failed_attempts = (operator.failed_attempts or 0) + 1
            if operator.failed_attempts >= cfg.erp_login_max_failures:
                operator.locked_until = now + timedelta(minutes=cfg.erp_lockout_minutes)
                operator.failed_attempts = 0
            db.add(operator)
        audit(db, operator=name or "?", action="auth.login", connection_id=connection_id,
              result="failed", detail={"ip": ip})
        db.commit()
        raise http_error(401, "invalid_credentials", "Usuário ou senha inválidos")
    operator.failed_attempts = 0
    operator.locked_until = None
    operator.last_login_at = now
    session = ErpSession(
        id=str(uuid4()),
        operator_id=operator.id,
        refresh_jti=uuid4().hex,
        expires_at=now + timedelta(days=cfg.erp_refresh_days),
        ip=ip,
        user_agent=(user_agent or "")[:300] or None,
    )
    db.add_all([operator, session])
    audit(db, operator=operator.username, action="auth.login", connection_id=connection_id,
          result="ok", detail={"ip": ip, "sessionId": session.id})
    db.commit()
    return _tokens(operator, session, response)


def revoke(db: Session, session: ErpSession, reason: str) -> None:
    if session.revoked_at is None:
        session.revoked_at = utcnow()
        session.revoked_reason = reason
        db.add(session)


def revoke_all(db: Session, operator_id: int, reason: str, *, except_id: str | None = None) -> int:
    query = (
        update(ErpSession)
        .where(ErpSession.operator_id == operator_id, ErpSession.revoked_at.is_(None))
        .values(revoked_at=utcnow(), revoked_reason=reason)
    )
    if except_id:
        query = query.where(ErpSession.id != except_id)
    return int(db.execute(query).rowcount or 0)


def refresh(db: Session, response: Response, cookie: str | None, *, connection_id: str) -> dict:
    claims = decode(cookie, "erp_refresh") if cookie else None
    if not claims:
        raise http_error(401, "session_expired", "Sessão expirada")
    session = db.get(ErpSession, claims.get("sid"))
    if session is None or session.revoked_at is not None or as_utc(session.expires_at) <= utcnow():
        raise http_error(401, "session_expired", "Sessão expirada")
    jti = claims.get("jti")
    if jti != session.refresh_jti:
        if jti and jti == session.previous_jti:
            # token antigo reapresentado: alguém copiou o cookie. Derruba a sessão.
            revoke(db, session, "refresh_reuse")
            operator = db.get(ErpOperator, session.operator_id)
            audit(db, operator=operator.username if operator else "?", action="auth.refresh_reuse",
                  connection_id=connection_id, result="revoked", detail={"sessionId": session.id})
            db.commit()
        raise http_error(401, "session_expired", "Sessão expirada")
    operator = db.get(ErpOperator, session.operator_id)
    if operator is None or not operator.active:
        revoke(db, session, "operator_inactive")
        db.commit()
        raise http_error(401, "session_expired", "Sessão expirada")
    session.previous_jti = session.refresh_jti
    session.refresh_jti = uuid4().hex
    session.last_used_at = utcnow()
    db.add(session)
    db.commit()
    return _tokens(operator, session, response)


def validate_access(db: Session, token: str) -> tuple[ErpOperator, ErpSession] | None:
    claims = decode(token, "erp_access")
    if not claims:
        return None
    session = db.get(ErpSession, claims.get("sid"))
    if session is None or session.revoked_at is not None or as_utc(session.expires_at) <= utcnow():
        return None
    operator = db.get(ErpOperator, session.operator_id)
    if operator is None or not operator.active or operator.username != claims.get("sub"):
        return None
    return operator, session


def set_password(
    db: Session,
    operator: ErpOperator,
    password: str,
    *,
    must_change: bool,
    actor: str,
    keep_session: str | None = None,
    connection_id: str,
) -> None:
    credentials.check_policy(password, operator.username, erp_settings().erp_min_password_length)
    operator.password_hash = credentials.hash_password(password)
    operator.must_change_password = must_change
    operator.password_changed_at = utcnow()
    operator.failed_attempts = 0
    operator.locked_until = None
    db.add(operator)
    revoked = revoke_all(db, operator.id, "password_changed", except_id=keep_session)
    audit(db, operator=actor, action="auth.password_set", connection_id=connection_id,
          resource="operator", resource_id=operator.username,
          detail={"mustChange": must_change, "sessionsRevoked": revoked})
