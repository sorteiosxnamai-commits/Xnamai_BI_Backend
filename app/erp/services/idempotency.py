"""Idempotência de operações locais (compras, estoque, financeiro).

Unicidade por conexão/operador/operação/chave, com hash do payload e resposta
registrada. Reusar a chave com corpo diferente devolve 409.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.erp.common import fingerprint, http_error
from app.erp.models.core import ErpIdempotencyKey


def require_key(value: str | None) -> str:
    if not value or not (8 <= len(value) <= 120):
        raise http_error(
            422,
            "idempotency_key_required",
            "Header Idempotency-Key obrigatório (8 a 120 caracteres)",
        )
    return value


def begin(
    db: Session,
    *,
    connection_id: str,
    operator: str,
    operation: str,
    key: str | None,
    payload: Any,
) -> tuple[ErpIdempotencyKey, dict | list | None, int | None]:
    """Devolve (registro, resposta_anterior, status_anterior)."""
    key = require_key(key)
    digest = fingerprint(payload)
    row = db.scalar(
        select(ErpIdempotencyKey).where(
            ErpIdempotencyKey.connection_id == connection_id,
            ErpIdempotencyKey.operator == operator,
            ErpIdempotencyKey.operation == operation,
            ErpIdempotencyKey.key == key,
        )
    )
    if row is not None:
        if row.payload_hash != digest:
            raise http_error(
                409, "idempotency_key_reuse", "Idempotency-Key já usada com corpo diferente"
            )
        return row, row.response_body, row.response_status
    row = ErpIdempotencyKey(
        connection_id=connection_id,
        operator=operator,
        operation=operation,
        key=key,
        payload_hash=digest,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise http_error(
            409, "idempotency_in_progress", "Operação com esta chave está em andamento"
        ) from exc
    return row, None, None


def finish(row: ErpIdempotencyKey, status: int, body: dict | list) -> None:
    row.response_status = status
    row.response_body = body
