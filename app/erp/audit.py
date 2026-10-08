from typing import Any

from sqlalchemy.orm import Session

from app.erp.models.core import ErpAuditEvent

_SENSITIVE = {
    "document",
    "email",
    "phone",
    "mobile",
    "token",
    "payment_token",
    "password",
    "authorization",
    "x-api-key",
    "cnpj",
    "cpf",
}


def scrub(value: Any, depth: int = 0) -> Any:
    """Remove PII e segredos antes de gravar detalhes de auditoria."""
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        return {
            key: ("[omitido]" if str(key).casefold() in _SENSITIVE else scrub(item, depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [scrub(item, depth + 1) for item in value[:50]]
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + "…"
    return value


def audit(
    db: Session,
    *,
    operator: str,
    action: str,
    connection_id: str | None = None,
    resource: str | None = None,
    resource_id: str | None = None,
    correlation_id: str | None = None,
    result: str | None = "ok",
    reason: str | None = None,
    detail: dict | None = None,
) -> ErpAuditEvent:
    event = ErpAuditEvent(
        operator=operator,
        action=action,
        connection_id=connection_id,
        resource=resource,
        resource_id=None if resource_id is None else str(resource_id),
        correlation_id=correlation_id,
        result=result,
        reason=reason,
        detail=scrub(detail) if detail else None,
    )
    db.add(event)
    return event
