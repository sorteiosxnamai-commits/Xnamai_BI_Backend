"""Receptor de webhooks do Mercos (inbox durável).

- assinatura `X-Hub-Signature-256` sobre os bytes originais, segredo hexadecimal
  convertido para bytes, comparação em tempo constante;
- a linha entra no inbox ANTES de qualquer 2xx; banco indisponível => erro;
- o evento não é aplicado como verdade: registra-se e dispara-se consulta
  incremental do recurso afetado (um webhook atrasado não regride entidade);
- eventos fora do catálogo documentado são ignorados com motivo, não inventados.
"""

import hashlib
import hmac
import json
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.erp import queue
from app.erp.common import as_utc, utcnow
from app.erp.config import erp_settings
from app.erp.db import session_scope
from app.erp.models.core import ErpWebhookInbox

log = logging.getLogger("uvicorn.error")

# Catálogo documentado (F14): evento normalizado -> recurso a reconsultar.
EVENT_RESOURCE = {
    "pedido.gerado": "orders",
    "pedido.faturado": "orders",
    "pedido.cancelado": "orders",
    "cliente.cadastrado": "customers",
    "cliente.atualizado": "customers",
    "cliente.bloqueio_atualizado": "customers",
    "cliente.excluido": "customers",
    "pagamento.atualizado": None,  # recurso payments ainda não sincronizado
}
MAX_BATCH = 200


class SignatureError(Exception):
    pass


def verify_signature(raw_body: bytes, header: str | None) -> None:
    secret_hex = erp_settings().erp_webhook_secret_hex.strip()
    if not secret_hex:
        raise SignatureError("not_configured")
    try:
        secret = bytes.fromhex(secret_hex)
    except ValueError as exc:
        raise SignatureError("not_configured") from exc
    if not header:
        raise SignatureError("missing")
    given = header.strip()
    if given.lower().startswith("sha256="):
        given = given[7:]
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, given.lower()):
        raise SignatureError("invalid")


def normalize_event(name: object) -> str:
    return str(name or "").strip().lower().replace(" ", "_").replace("-", "_")


def _events(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload[:MAX_BATCH] if isinstance(item, dict)]
    if isinstance(payload, dict):
        batch = payload.get("eventos") or payload.get("events")
        if isinstance(batch, list):
            return [item for item in batch[:MAX_BATCH] if isinstance(item, dict)]
        return [payload]
    return []


def event_name(item: dict) -> str:
    return normalize_event(item.get("evento") or item.get("event") or item.get("tipo"))


def receive(
    db: Session,
    *,
    connection_id: str,
    raw_body: bytes,
    delivery_id: str | None,
) -> tuple[ErpWebhookInbox | None, str]:
    """Persiste no inbox. Devolve (linha, 'accepted'|'duplicate')."""
    body_hash = hashlib.sha256(raw_body).hexdigest()
    dedupe_key = (delivery_id or body_hash)[:80]
    window = timedelta(seconds=erp_settings().erp_webhook_dedupe_window_seconds)
    conditions = [
        ErpWebhookInbox.connection_id == connection_id,
        ErpWebhookInbox.dedupe_key == dedupe_key,
    ]
    if not delivery_id:
        # Sem ID de entrega, o hash só deduplica dentro de uma janela: dois
        # eventos legítimos iguais, bem separados no tempo, não são descartados.
        conditions.append(ErpWebhookInbox.received_at >= utcnow() - window)
    duplicate = db.scalar(select(ErpWebhookInbox.id).where(*conditions))
    if duplicate is not None:
        return None, "duplicate"
    row = ErpWebhookInbox(
        connection_id=connection_id,
        delivery_id=delivery_id,
        dedupe_key=dedupe_key,
        body_hash=body_hash,
        raw_body=raw_body,
        status="received",
    )
    db.add(row)
    db.flush()
    return row, "accepted"


def pending_ids(limit: int = 50) -> list[int]:
    with session_scope() as db:
        return list(
            db.scalars(
                select(ErpWebhookInbox.id)
                .where(ErpWebhookInbox.status.in_(("received", "retry")))
                .order_by(ErpWebhookInbox.received_at, ErpWebhookInbox.id)
                .limit(limit)
            )
        )


def process(inbox_id: int) -> dict:
    """Processa fora da requisição: registra e agenda consulta incremental."""
    with session_scope() as db:
        row = db.get(ErpWebhookInbox, inbox_id)
        if row is None or row.status not in ("received", "retry"):
            return {"status": "skipped"}
        row.attempts = (row.attempts or 0) + 1
        try:
            payload = json.loads(row.raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            row.status = "failed"
            row.error = "invalid_json"
            row.processed_at = utcnow()
            db.add(row)
            return {"status": "failed", "error": "invalid_json"}
        scheduled: list[str] = []
        ignored: list[dict] = []
        for item in _events(payload):
            name = event_name(item)
            if name not in EVENT_RESOURCE:
                ignored.append({"event": name or None, "reason": "fora do catálogo documentado"})
                continue
            resource = EVENT_RESOURCE[name]
            if resource is None:
                ignored.append({"event": name, "reason": "recurso ainda não sincronizado pelo ERP"})
                continue
            queue.enqueue(
                db,
                kind="sync",
                connection_id=row.connection_id,
                resource=resource,
                mode="incremental",
                requested_by="webhook",
                payload={"inboxId": row.id, "event": name},
            )
            if resource not in scheduled:
                scheduled.append(resource)
        row.event = ",".join(scheduled) or (ignored[0]["event"] if ignored else None)
        row.status = "processed" if scheduled else "ignored"
        row.processed_at = utcnow()
        row.result = {"scheduled": scheduled, "ignored": ignored}
        db.add(row)
        return {"status": row.status, **row.result}


MAX_ATTEMPTS = 5


def record_failure(inbox_id: int, error: str) -> str:
    """Falha ao processar: tenta de novo até o limite e então marca como failed.

    O incremento fica numa transação própria, porque a do processamento foi
    revertida; sem isto uma mensagem venenosa seria reprocessada para sempre."""
    with session_scope() as db:
        row = db.get(ErpWebhookInbox, inbox_id)
        if row is None:
            return "missing"
        row.attempts = (row.attempts or 0) + 1
        row.error = error[:1000]
        row.status = "failed" if row.attempts >= MAX_ATTEMPTS else "retry"
        if row.status == "failed":
            row.processed_at = utcnow()
        db.add(row)
        return row.status


def stale_age_seconds(db: Session) -> float | None:
    oldest = db.scalar(
        select(ErpWebhookInbox.received_at)
        .where(ErpWebhookInbox.status.in_(("received", "retry")))
        .order_by(ErpWebhookInbox.received_at)
        .limit(1)
    )
    if oldest is None:
        return None
    return (utcnow() - as_utc(oldest)).total_seconds()
