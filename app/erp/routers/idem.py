"""Execução idempotente de comandos locais (mesma transação do efeito)."""

from collections.abc import Callable
from typing import Any

from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.erp.auth import ErpUser
from app.erp.config import erp_settings
from app.erp.services import idempotency


def run(
    db: Session,
    user: ErpUser,
    operation: str,
    key: str | None,
    payload: Any,
    handler: Callable[[], dict],
    *,
    status: int = 201,
) -> JSONResponse:
    record, previous, previous_status = idempotency.begin(
        db,
        connection_id=erp_settings().erp_connection_id,
        operator=user.username,
        operation=operation,
        key=key,
        payload=payload,
    )
    if previous is not None:
        return JSONResponse(
            status_code=previous_status or 200,
            content={**previous, "replayed": True} if isinstance(previous, dict) else previous,
        )
    body = handler()
    idempotency.finish(record, status, body)
    db.commit()
    return JSONResponse(status_code=status, content=body)
