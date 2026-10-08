"""Receptor de webhooks do Mercos.

Não usa login de navegador nem cookie ERP: a autenticidade vem da assinatura
HMAC sobre os bytes originais. Persiste no inbox antes do 2xx.
"""

import logging

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.erp import inbox
from app.erp.auth import module_enabled
from app.erp.config import erp_settings
from app.erp.db import new_session

router = APIRouter(tags=["erp-webhooks"])
log = logging.getLogger("uvicorn.error")


def _reply(status: int, code: str, message: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message or code})


@router.post("/webhooks/mercos", summary="Webhook Mercos (autenticado por assinatura)", include_in_schema=True)
async def mercos_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(None),
    x_delivery_id: str | None = Header(None),
):
    module_enabled()
    cfg = erp_settings()
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cfg.erp_webhook_max_bytes:
        return _reply(413, "payload_too_large")
    raw = await request.body()
    if len(raw) > cfg.erp_webhook_max_bytes:
        return _reply(413, "payload_too_large")
    try:
        inbox.verify_signature(raw, x_hub_signature_256)
    except inbox.SignatureError as exc:
        if str(exc) == "not_configured":
            return _reply(503, "webhook_not_configured", "Segredo de webhook não configurado")
        return _reply(401, "invalid_signature", "Assinatura ausente ou inválida")
    db = new_session()
    try:
        row, outcome = inbox.receive(
            db,
            connection_id=cfg.erp_connection_id,
            raw_body=raw,
            delivery_id=(x_delivery_id or None),
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.exception("Inbox de webhook indisponível")
        # Nunca responder sucesso sem ter persistido o evento.
        return _reply(503, "inbox_unavailable", "Falha ao persistir o evento")
    finally:
        db.close()
    return JSONResponse(
        status_code=200,
        content={"status": outcome, "id": row.id if row is not None else None},
    )
