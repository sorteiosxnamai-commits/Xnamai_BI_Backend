"""Outbox: escrita externa assíncrona, sem POST cego.

Fluxo (documento, 7.2): intenção validada + auditoria + operação na mesma
transação local; o worker chama o Adaptor fora da transação e grava o resultado
em outra. `dispatch_started_at` é gravado ANTES da chamada: se o processo cair
depois disso, a operação vira `unknown` e nunca volta sozinha para a fila.
"""

import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.erp import capabilities
from app.erp.audit import audit
from app.erp.auth import ErpUser
from app.erp.common import as_utc, fingerprint, http_error, utcnow
from app.erp.config import erp_settings
from app.erp.db import session_scope
from app.erp.integrations.mercos_client import MercosAdaptorClient, WriteOutcome
from app.erp.models.commercial import ErpCustomer, ErpSalesOrder
from app.erp.models.core import ErpConflict, ErpOperation
from app.erp.schemas.commands import (
    CustomerCreate,
    CustomerPatch,
    OrderCreate,
    OrderPatch,
)

log = logging.getLogger("uvicorn.error")

STATES = (
    "queued",
    "processing",
    "waiting_rate_limit",
    "succeeded",
    "failed",
    "unknown",
    "conflict",
)

# transições legais: origem -> destinos
TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"processing"}),
    "processing": frozenset(
        {"succeeded", "failed", "unknown", "waiting_rate_limit", "conflict", "queued"}
    ),
    "waiting_rate_limit": frozenset({"queued", "processing"}),
    "conflict": frozenset({"queued", "failed"}),
    "unknown": frozenset({"succeeded", "failed"}),  # somente via reconciliação
    "succeeded": frozenset(),
    "failed": frozenset(),
}

CAPABILITY_BY_KIND = {
    "create_customer": "write.customers",
    "update_customer": "write.customers",
    "create_order": "write.orders",
    "update_order": "write.orders",
    "create_title": "write.titles",
    "update_title": "write.titles",
}
TARGET_BY_KIND = {
    "create_customer": "customers",
    "update_customer": "customers",
    "create_order": "orders",
    "update_order": "orders",
    "create_title": "titles",
    "update_title": "titles",
}

CUSTOMER_API_TO_ATTR = {
    "name": "name",
    "tradeName": "trade_name",
    "personType": "person_type",
    "document": "document",
    "stateRegistration": "state_registration",
    "street": "street",
    "number": "number",
    "complement": "complement",
    "district": "district",
    "zipCode": "zip_code",
    "city": "city",
    "state": "state",
    "email": "email",
    "phone": "phone",
    "mobile": "mobile",
    "segmentId": "segment_external_id",
    "notes": "notes",
}
CUSTOMER_API_TO_MERCOS = {
    "name": "razao_social",
    "tradeName": "nome_fantasia",
    "personType": "tipo",
    "stateRegistration": "inscricao_estadual",
    "street": "rua",
    "number": "numero",
    "complement": "complemento",
    "district": "bairro",
    "zipCode": "cep",
    "city": "cidade",
    "state": "estado",
    "phone": "telefone",
    "mobile": "celular",
    "segmentId": "segmento_id",
    "notes": "observacao",
}
ORDER_API_TO_ATTR = {
    "notes": "notes",
    "paymentConditionId": "payment_condition_external_id",
    "carrierId": "carrier_external_id",
    "expectedDeliveryDate": "expected_delivery_date",
}
ORDER_API_TO_MERCOS = {
    "notes": "observacoes",
    "paymentConditionId": "condicao_pagamento_id",
    "carrierId": "transportadora_id",
    "expectedDeliveryDate": "data_entrega",
}


def _number(value: Decimal) -> float:
    return float(value)


def customer_payload(dto: dict) -> dict:
    payload: dict[str, Any] = {}
    for key, target in CUSTOMER_API_TO_MERCOS.items():
        if key in dto:
            payload[target] = dto[key]
    if "document" in dto and dto["document"]:
        person = dto.get("personType")
        digits = "".join(ch for ch in dto["document"] if ch.isalnum())
        if person is None:
            person = "F" if len(digits) == 11 else "J"
            payload.setdefault("tipo", person)
        payload["cnpj" if person == "J" else "cpf"] = dto["document"]
    if "email" in dto and dto["email"]:
        payload["emails"] = [{"email": dto["email"]}]
    return payload


def _items_payload(items: list[dict]) -> list[dict]:
    result = []
    for item in items:
        row: dict[str, Any] = {
            "produto_id": item["productId"],
            "quantidade": _number(Decimal(str(item["quantity"]))),
        }
        if item.get("unitPrice") is not None:
            row["preco_liquido"] = _number(Decimal(str(item["unitPrice"])))
        if item.get("discount") is not None:
            row["desconto"] = _number(Decimal(str(item["discount"])))
        result.append(row)
    return result


def order_payload(dto: dict, *, create: bool) -> dict:
    payload: dict[str, Any] = {}
    if create:
        payload["cliente_id"] = dto["customerId"]
        for key, target in (
            ("priceTableId", "tabela_preco_id"),
            ("orderTypeId", "tipo_pedido_id"),
            ("sellerId", "criador_id"),
        ):
            if dto.get(key):
                payload[target] = dto[key]
    for key, target in ORDER_API_TO_MERCOS.items():
        if key in dto:
            value = dto[key]
            payload[target] = value.isoformat() if hasattr(value, "isoformat") else value
    if dto.get("items") is not None:
        payload["itens"] = _items_payload(dto["items"])
    return payload


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def transition(op: ErpOperation, new: str) -> None:
    if new not in TRANSITIONS.get(op.status, frozenset()):
        raise http_error(
            409,
            "illegal_transition",
            f"Transição inválida: {op.status} → {new}",
        )
    op.status = new


def _entity_values(entity: Any, attrs: list[str]) -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    for attr in attrs:
        value = getattr(entity, attr, None)
        values[attr] = None if value is None else str(value)
    return values


def submit(
    db: Session,
    *,
    connection_id: str,
    user: ErpUser,
    kind: str,
    body: CustomerCreate | CustomerPatch | OrderCreate | OrderPatch,
    idempotency_key: str | None,
    target_external_id: str | None = None,
    correlation_id: str | None = None,
) -> tuple[ErpOperation, bool]:
    """Valida e enfileira a intenção. Devolve (operação, criada)."""
    if not idempotency_key or not (8 <= len(idempotency_key) <= 120):
        raise http_error(
            422,
            "idempotency_key_required",
            "Header Idempotency-Key obrigatório (8 a 120 caracteres)",
        )
    cap_key = CAPABILITY_BY_KIND[kind]
    enabled, reason = capabilities.capability_enabled(db, connection_id, cap_key)
    if not enabled:
        raise http_error(
            409, "capability_disabled", "Capacidade desabilitada", capability=cap_key, reason=reason
        )

    is_update = kind.startswith("update_")
    dto = _jsonable(body.model_dump(exclude_unset=True, mode="python"))
    expected_version = dto.pop("expectedVersion", None)

    base_fingerprint = None
    base_snapshot = None
    entity = None
    if is_update:
        entity = _load_target(db, connection_id, kind, target_external_id)
        if expected_version != entity.version:
            raise http_error(
                409,
                "version_mismatch",
                "A versão local mudou; recarregue antes de editar",
                currentVersion=entity.version,
            )
        mapping = CUSTOMER_API_TO_ATTR if kind == "update_customer" else ORDER_API_TO_ATTR
        attrs = [mapping[key] for key in dto if key in mapping]
        if not attrs and "items" not in dto:
            raise http_error(422, "empty_patch", "Nenhum campo para alterar")
        base_fingerprint = entity.fingerprint
        base_snapshot = _entity_values(entity, attrs)
        if kind == "update_order" and not entity.items_complete:
            raise http_error(
                409,
                "order_incomplete",
                "Pedido sem itens completos; operação bloqueada até a hidratação",
            )
    elif kind == "create_order":
        customer = db.scalar(
            select(ErpCustomer).where(
                ErpCustomer.connection_id == connection_id,
                ErpCustomer.external_id == dto["customerId"],
            )
        )
        if customer is None:
            raise http_error(422, "unknown_customer", "Cliente não encontrado no espelho local")
        if customer.blocked:
            raise http_error(409, "customer_blocked", "Cliente bloqueado na origem")

    payload_hash = fingerprint({"kind": kind, "target": target_external_id, "dto": dto, "v": expected_version})
    existing = db.scalar(
        select(ErpOperation).where(
            ErpOperation.connection_id == connection_id,
            ErpOperation.operator == user.username,
            ErpOperation.kind == kind,
            ErpOperation.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise http_error(
                409,
                "idempotency_key_reuse",
                "Idempotency-Key já usada com corpo diferente",
            )
        return existing, False

    if kind in ("create_customer", "update_customer"):
        mercos_payload = customer_payload(dto)
    else:
        mercos_payload = order_payload(dto, create=kind == "create_order")
    op = ErpOperation(
        id=str(uuid4()),
        connection_id=connection_id,
        operator=user.username,
        kind=kind,
        target_resource=TARGET_BY_KIND[kind],
        target_external_id=target_external_id,
        status="queued",
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        payload={"dto": dto, "mercos": mercos_payload},
        base_fingerprint=base_fingerprint,
        base_snapshot=base_snapshot,
        expected_version=expected_version,
        correlation_id=correlation_id,
    )
    db.add(op)
    audit(
        db,
        operator=user.username,
        action=f"operation.{kind}",
        connection_id=connection_id,
        resource=TARGET_BY_KIND[kind],
        resource_id=target_external_id or op.id,
        correlation_id=correlation_id,
        result="queued",
        detail={"operationId": op.id, "fields": sorted(dto)},
    )
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(ErpOperation).where(
                ErpOperation.connection_id == connection_id,
                ErpOperation.operator == user.username,
                ErpOperation.kind == kind,
                ErpOperation.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            raise
        return existing, False
    return op, True


def _load_target(db: Session, connection_id: str, kind: str, external_id: str | None):
    model = ErpCustomer if kind == "update_customer" else ErpSalesOrder
    if not external_id:
        raise http_error(422, "missing_target", "Identificador do registro obrigatório")
    entity = db.scalar(
        select(model).where(
            model.connection_id == connection_id, model.external_id == external_id
        )
    )
    if entity is None:
        raise http_error(404, "not_found", "Registro não encontrado no espelho local")
    return entity


def detect_conflict(db: Session, op: ErpOperation) -> dict | None:
    """Campos alterados nos dois lados desde a base bloqueiam o envio."""
    if not op.kind.startswith("update_"):
        return None
    entity = _load_target(db, op.connection_id, op.kind, op.target_external_id)
    if entity.fingerprint == op.base_fingerprint:
        return None
    base = op.base_snapshot or {}
    current = _entity_values(entity, list(base))
    changed = [attr for attr, value in base.items() if current.get(attr) != value]
    if not changed:
        return None  # a origem mudou outros campos; só enviamos os intencionais
    return {
        "fields": changed,
        "base": {a: base[a] for a in changed},
        "external": {a: current[a] for a in changed},
        "local": op.payload.get("dto", {}),
    }


def _claim(db: Session, op_id: str, token: str) -> bool:
    now = utcnow()
    lease = now + timedelta(seconds=erp_settings().erp_job_lease_seconds)
    result = db.execute(
        update(ErpOperation)
        .where(
            ErpOperation.id == op_id,
            ErpOperation.status.in_(("queued", "waiting_rate_limit")),
            ErpOperation.next_attempt_at <= now,
        )
        .values(
            status="processing",
            lease_token=token,
            leased_until=lease,
            attempts=ErpOperation.attempts + 1,
        )
    )
    return result.rowcount == 1


def due_operation_ids(limit: int = 10) -> list[str]:
    with session_scope() as db:
        return list(
            db.scalars(
                select(ErpOperation.id)
                .where(
                    ErpOperation.status.in_(("queued", "waiting_rate_limit")),
                    ErpOperation.next_attempt_at <= utcnow(),
                )
                .order_by(ErpOperation.next_attempt_at, ErpOperation.created_at)
                .limit(limit)
            )
        )


def _prepare_dispatch(op_id: str, token: str) -> dict | None:
    """Fase A: claim, checagens e marca de envio. Devolve o que enviar, ou None."""
    with session_scope() as db:
        if not _claim(db, op_id, token):
            return None
        op = db.get(ErpOperation, op_id)
        db.refresh(op)
        enabled, reason = capabilities.capability_enabled(
            db, op.connection_id, CAPABILITY_BY_KIND[op.kind]
        )
        if not enabled:
            op.status = "failed"
            op.error_code = "capability_disabled"
            op.error = reason
            op.completed_at = utcnow()
            op.lease_token = None
            db.add(op)
            return None
        conflict = detect_conflict(db, op)
        if conflict:
            op.status = "conflict"
            op.lease_token = None
            op.error_code = "conflict"
            op.error = "Campos alterados nos dois lados; resolva o conflito"
            db.add(op)
            db.add(
                ErpConflict(
                    connection_id=op.connection_id,
                    operation_id=op.id,
                    entity_type=op.target_resource,
                    entity_external_id=op.target_external_id,
                    fields=conflict["fields"],
                    base=conflict["base"],
                    local=conflict["local"],
                    external=conflict["external"],
                )
            )
            audit(
                db,
                operator="worker",
                action="operation.conflict",
                connection_id=op.connection_id,
                resource=op.target_resource,
                resource_id=op.target_external_id,
                result="conflict",
                detail={"operationId": op.id, "fields": conflict["fields"]},
            )
            return None
        # Marca ANTES de enviar: a partir daqui o resultado pode ser desconhecido.
        op.dispatch_started_at = utcnow()
        db.add(op)
        return {
            "kind": op.kind,
            "payload": op.payload["mercos"],
            "external_id": op.target_external_id,
        }


def _persist_outcome(op_id: str, token: str, outcome: WriteOutcome) -> None:
    with session_scope() as db:
        op = db.scalar(
            select(ErpOperation).where(
                ErpOperation.id == op_id, ErpOperation.lease_token == token
            )
        )
        if op is None:
            log.warning("Lease perdida ao gravar resultado da operação %s", op_id)
            return
        op.response_status = outcome.status_code
        op.response_body = _jsonable(outcome.body) if outcome.body is not None else None
        op.error_code = outcome.error_code
        op.error = outcome.error
        op.lease_token = None
        op.leased_until = None
        if outcome.kind == "success":
            transition(op, "succeeded")
            op.external_id = outcome.external_id
            op.completed_at = utcnow()
            op.error = None
        elif outcome.kind == "rejected":
            transition(op, "failed")
            op.completed_at = utcnow()
        elif outcome.kind == "rate_limited":
            # Rejeição conhecida: o Mercos não executou. Reagendar é seguro.
            transition(op, "waiting_rate_limit")
            op.dispatch_started_at = None
            op.next_attempt_at = utcnow() + timedelta(seconds=outcome.retry_after or 30.0)
        else:
            transition(op, "unknown")
        db.add(op)
        audit(
            db,
            operator="worker",
            action=f"operation.{op.kind}.result",
            connection_id=op.connection_id,
            resource=op.target_resource,
            resource_id=op.external_id or op.target_external_id,
            correlation_id=op.correlation_id,
            result=op.status,
            reason=outcome.error,
            detail={"operationId": op.id, "httpStatus": outcome.status_code},
        )


async def dispatch_operation(client: MercosAdaptorClient, op_id: str) -> str | None:
    import asyncio

    token = str(uuid4())
    plan = await asyncio.to_thread(_prepare_dispatch, op_id, token)
    if plan is None:
        return None
    outcome = await client.write(plan["kind"], plan["payload"], plan["external_id"])
    for attempt in range(3):
        try:
            await asyncio.to_thread(_persist_outcome, op_id, token, outcome)
            break
        except Exception:  # noqa: BLE001
            log.exception("Falha ao gravar resultado da operação %s (tentativa %s)", op_id, attempt + 1)
            await asyncio.sleep(0.5 * (attempt + 1))
    return outcome.kind


def recover_stale(db: Session) -> int:
    """Lease vencida: sem envio volta à fila; com envio vira `unknown`."""
    now = utcnow()
    stale = db.scalars(
        select(ErpOperation).where(
            ErpOperation.status == "processing", ErpOperation.leased_until < now
        )
    ).all()
    for op in stale:
        op.lease_token = None
        op.leased_until = None
        if op.dispatch_started_at is None:
            op.status = "queued"
        else:
            op.status = "unknown"
            op.error_code = "crash_after_dispatch"
            op.error = (
                "O processo caiu após o envio; o Mercos pode ter aceitado. Reconcilie."
            )
        db.add(op)
    return len(stale)


def reconcile_evidence(db: Session, op: ErpOperation) -> dict:
    """Coleta evidências; nunca decide sozinho nem usa nome/valor como prova."""
    evidence: dict[str, Any] = {"checkedAt": utcnow().isoformat()}
    started = as_utc(op.dispatch_started_at)
    if op.kind.startswith("update_"):
        entity = _load_target(db, op.connection_id, op.kind, op.target_external_id)
        dto = op.payload.get("dto", {})
        mapping = CUSTOMER_API_TO_ATTR if op.kind == "update_customer" else ORDER_API_TO_ATTR
        attrs = {mapping[k]: str(v) for k, v in dto.items() if k in mapping}
        current = _entity_values(entity, list(attrs))
        evidence.update(
            mirrorVersion=entity.version,
            capturedAfterDispatch=bool(started and as_utc(entity.captured_at) >= started),
            fieldsMatchIntent=all(current[a] == v for a, v in attrs.items()) if attrs else None,
        )
    elif op.kind == "create_customer":
        document = (op.payload.get("dto") or {}).get("document")
        rows = []
        if document:
            rows = db.scalars(
                select(ErpCustomer).where(
                    ErpCustomer.connection_id == op.connection_id,
                    ErpCustomer.document == document,
                )
            ).all()
        evidence["candidates"] = [
            {
                "externalId": r.external_id,
                "capturedAt": as_utc(r.captured_at).isoformat(),
                "afterDispatch": bool(started and as_utc(r.captured_at) >= started),
            }
            for r in rows
        ]
        evidence["ambiguous"] = len(rows) != 1
    elif op.kind == "create_order":
        customer_id = (op.payload.get("dto") or {}).get("customerId")
        rows = db.scalars(
            select(ErpSalesOrder).where(
                ErpSalesOrder.connection_id == op.connection_id,
                ErpSalesOrder.customer_external_id == customer_id,
            )
        ).all()
        recent = [
            r for r in rows if started and as_utc(r.captured_at) >= started
        ]
        evidence["candidates"] = [
            {"externalId": r.external_id, "number": r.number} for r in recent
        ]
        evidence["ambiguous"] = len(recent) != 1
    evidence["note"] = (
        "Evidência auxiliar. Igualdade de nome/valor não prova registro único; "
        "a decisão é humana."
    )
    return evidence


def reconcile(
    db: Session,
    op: ErpOperation,
    user: ErpUser,
    *,
    decision: str,
    external_id: str | None,
    note: str | None,
) -> ErpOperation:
    if op.status != "unknown":
        raise http_error(409, "not_unknown", "Somente operações em estado unknown são reconciliadas")
    if decision == "check":
        op.reconcile_evidence = reconcile_evidence(db, op)
    elif decision == "confirm_created":
        if not external_id and not op.target_external_id:
            raise http_error(422, "external_id_required", "Informe o ID externo confirmado")
        transition(op, "succeeded")
        op.external_id = external_id or op.target_external_id
        op.completed_at = utcnow()
        op.error_code = "reconciled_created"
        op.error = None
    else:
        transition(op, "failed")
        op.completed_at = utcnow()
        op.error_code = "confirmed_not_created"
        op.error = "Operador confirmou que o Mercos não registrou a operação"
    db.add(op)
    audit(
        db,
        operator=user.username,
        action="operation.reconcile",
        connection_id=op.connection_id,
        resource=op.target_resource,
        resource_id=op.external_id or op.target_external_id,
        correlation_id=op.correlation_id,
        result=decision,
        reason=note,
        detail={"operationId": op.id},
    )
    return op


def resolve_conflict(
    db: Session, conflict: ErpConflict, user: ErpUser, *, resolution: str, note: str | None
) -> ErpConflict:
    if conflict.status != "open":
        raise http_error(409, "conflict_closed", "Conflito já resolvido")
    op = db.get(ErpOperation, conflict.operation_id) if conflict.operation_id else None
    if op is not None and op.status == "conflict":
        if resolution == "use_local":
            entity = _load_target(db, op.connection_id, op.kind, op.target_external_id)
            mapping = CUSTOMER_API_TO_ATTR if op.kind == "update_customer" else ORDER_API_TO_ATTR
            attrs = list((op.base_snapshot or {}).keys()) or [
                mapping[k] for k in op.payload.get("dto", {}) if k in mapping
            ]
            op.base_fingerprint = entity.fingerprint
            op.base_snapshot = _entity_values(entity, attrs)
            op.error = None
            op.error_code = None
            op.next_attempt_at = utcnow()
            transition(op, "queued")
        else:
            transition(op, "failed")
            op.error_code = "conflict_external_wins"
            op.error = "Operador manteve a versão da origem"
            op.completed_at = utcnow()
        db.add(op)
    conflict.status = "resolved"
    conflict.resolution = resolution
    conflict.resolved_by = user.username
    conflict.resolved_at = utcnow()
    db.add(conflict)
    audit(
        db,
        operator=user.username,
        action="conflict.resolve",
        connection_id=conflict.connection_id,
        resource=conflict.entity_type,
        resource_id=conflict.entity_external_id,
        result=resolution,
        reason=note,
        detail={"conflictId": conflict.id, "operationId": conflict.operation_id},
    )
    return conflict
