"""Acesso ao ERP: nega por padrão.

A chave de serviço do BI e o papel `viewer` nunca concedem acesso ao ERP. O
administrador JWT só entra com vínculo explícito (lista de bootstrap ou linha
em `erp_operators`). Vendedor Mercos não vira operador automaticamente.
"""

from dataclasses import dataclass, field

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import AuthUser, _digest_equal, decode_token
from app.config import settings
from app.erp import sessions
from app.erp.common import http_error
from app.erp.config import erp_settings
from app.erp.db import erp_db
from app.erp.models.core import ErpOperator

bearer = HTTPBearer(auto_error=False)

ROLES = ("erp_admin", "comercial", "estoque", "compras", "financeiro", "consulta")

# Permissões por papel. `*` é exclusivo do administrador ERP.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "erp_admin": frozenset({"*"}),
    "comercial": frozenset(
        {
            "read",
            "pii:read",
            "customers:write",
            "orders:write",
            "integration:read",
            "integration:sync",
            "integration:resolve",
            "shipping:read",
            "shipping:write",
            "invoices:read",
            "invoices:write",
            "refunds:read",
            "refunds:request",
        }
    ),
    "estoque": frozenset(
        {
            "read",
            "inventory:read",
            "inventory:adjust",
            "inventory:transfer",
            "integration:read",
        }
    ),
    "compras": frozenset(
        {
            "read",
            "purchases:read",
            "purchases:write",
            "purchases:receive",
            "inventory:read",
            "integration:read",
        }
    ),
    "financeiro": frozenset(
        {
            "read",
            "pii:read",
            "finance:read",
            "finance:write",
            "finance:settle",
            "financial_links:read",
            "purchases:read",
            "integration:read",
            "invoices:read",
            "refunds:read",
            "refunds:request",
            "refunds:approve",
        }
    ),
    "consulta": frozenset({"read", "integration:read", "shipping:read", "invoices:read", "refunds:read"}),
}


@dataclass
class ErpUser:
    username: str
    roles: list[str]
    bootstrap: bool = False
    permissions: set[str] = field(default_factory=set)
    # erp_session = login individual do ERP; bi_bootstrap/bi_link = token do BI
    auth_method: str = "bi_link"
    session_id: str | None = None
    must_change_password: bool = False

    def can(self, permission: str) -> bool:
        return "*" in self.permissions or permission in self.permissions


def _permissions(roles: list[str]) -> set[str]:
    result: set[str] = set()
    for role in roles:
        result |= ROLE_PERMISSIONS.get(role, frozenset())
    return result


def module_enabled() -> None:
    """Flag desligada bloqueia o acesso por URL, não só o menu."""
    if not erp_settings().erp_enabled:
        raise http_error(404, "erp_disabled", "Módulo ERP desativado")


def resolve_operator(db: Session, user: AuthUser) -> ErpUser:
    """Token do BI -> operador ERP. Só o bootstrap explícito entra por padrão;
    contas compartilhadas do BI não identificam pessoas, então o vínculo legado
    por operador exige ERP_ALLOW_BI_OPERATOR_LINK."""
    username = user.username.strip()
    cfg = erp_settings()
    if user.role == "admin" and username.casefold() in cfg.bootstrap_admins:
        return ErpUser(username, ["erp_admin"], bootstrap=True, permissions={"*"},
                       auth_method="bi_bootstrap")
    row = db.scalar(
        select(ErpOperator).where(ErpOperator.username == username.casefold())
    )
    if row is not None and cfg.erp_allow_bi_operator_link:
        if not row.active:
            raise http_error(403, "operator_inactive", "Operador ERP inativo")
        roles = [role for role in (row.roles or []) if role in ROLES]
        return ErpUser(username, roles, permissions=_permissions(roles))
    raise http_error(
        403,
        "no_erp_access",
        "Usuário autenticado sem vínculo de acesso ao ERP",
    )


def _authenticate(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    x_api_key: str | None,
    db: Session,
) -> ErpUser:
    module_enabled()
    expected_key = settings().bi_api_key
    if x_api_key and expected_key and _digest_equal(x_api_key, expected_key):
        raise http_error(
            403, "service_key_not_allowed",
            "A chave de serviço do BI não concede acesso ao ERP",
        )
    if not (credentials and credentials.credentials):
        raise http_error(401, "unauthenticated", "Não autenticado")
    token = credentials.credentials
    valid = sessions.validate_access(db, token)
    if valid is not None:
        operator, session = valid
        roles = [role for role in (operator.roles or []) if role in ROLES]
        user = ErpUser(
            operator.username, roles, permissions=_permissions(roles),
            auth_method="erp_session", session_id=session.id,
            must_change_password=bool(operator.must_change_password),
        )
    else:
        user = resolve_operator(db, decode_token(token, "access"))
    request.state.erp_operator = user.username
    return user


def erp_user_pending_ok(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    x_api_key: str | None = Header(None),
    db: Session = Depends(erp_db),
) -> ErpUser:
    """Autentica sem exigir troca de senha (para /me, trocar senha e sair)."""
    return _authenticate(request, credentials, x_api_key, db)


def erp_user(user: ErpUser = Depends(erp_user_pending_ok)) -> ErpUser:
    if user.must_change_password:
        raise http_error(
            403, "password_change_required",
            "Troque a senha temporária antes de usar o ERP",
        )
    return user


def require(permission: str):
    def dependency(user: ErpUser = Depends(erp_user)) -> ErpUser:
        if not user.can(permission):
            raise http_error(
                403,
                "forbidden",
                "Sem permissão para esta operação",
                permission=permission,
            )
        return user

    return dependency
