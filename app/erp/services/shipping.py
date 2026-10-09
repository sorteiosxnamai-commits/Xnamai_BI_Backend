"""Frete local do painel operacional.

Regras que este módulo garante:

- cotação automática só existe com conector real; hoje não há, então só há cadastro MANUAL e
  auditado de cotação obtida fora do sistema (nenhum preço é simulado);
- seleção é local: não contrata, não gera etiqueta e não confirma envio;
- cotação é atrelada à versão do pedido; se o pedido mudou depois, a cotação fica DESATUALIZADA e
  a seleção é recusada até nova revisão;
- opção vencida não pode ser selecionada;
- no máximo uma cotação selecionada por pedido (índice parcial único) e seleção com controle
  otimista de versão: duas pessoas selecionando ao mesmo tempo, só uma vence, sem duplicar;
- mudar uma seleção já feita exige motivo.
"""

from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.erp import serializers as ser
from app.erp.audit import audit
from app.erp.auth import ErpUser
from app.erp.common import as_utc, http_error, plain_decimal, utcnow
from app.erp.models import commercial as m
from app.erp.models import operations as op
from app.erp.schemas.operations import ShippingOptionInput, ShippingQuoteCreate, ShippingSelectInput
from app.erp.services import order_operations as oo

PROVIDER = {
    "available": False,
    "reason": "Nenhum conector de cotação de frete está configurado nesta base. "
    "Registre manualmente a cotação obtida fora do sistema; a automática depende da escolha "
    "do provedor, de credenciais de homologação e das regras comerciais.",
}


def _order(db: Session, connection_id: str, external_id: str) -> m.ErpSalesOrder:
    row = db.scalar(
        select(m.ErpSalesOrder).where(
            m.ErpSalesOrder.connection_id == connection_id, m.ErpSalesOrder.external_id == external_id
        )
    )
    if row is None:
        raise http_error(404, "not_found", "Pedido não encontrado")
    return row


def _quote(db: Session, connection_id: str, quote_id: int) -> op.ErpShippingQuote:
    row = db.scalar(
        select(op.ErpShippingQuote).where(
            op.ErpShippingQuote.connection_id == connection_id, op.ErpShippingQuote.id == quote_id
        )
    )
    if row is None:
        raise http_error(404, "not_found", "Cotação não encontrada")
    return row


def is_stale(quote: op.ErpShippingQuote, order: m.ErpSalesOrder | None) -> bool:
    """Pedido removido ou alterado depois da cotação: a cotação não vale mais sem revisão."""
    if order is None:
        return True
    return (quote.order_fingerprint or "") != (order.fingerprint or "")


def _destination(order: m.ErpSalesOrder, override_zip: str | None) -> dict:
    address = order.shipping_address if isinstance(order.shipping_address, dict) else {}

    def pick(*keys: str) -> str | None:
        for key in keys:
            value = address.get(key)
            if value not in (None, ""):
                return str(value)
        return None

    return {
        "zip": override_zip or pick("cep", "zip", "zip_code", "zipCode"),
        "city": pick("cidade", "city"),
        "state": pick("estado", "state", "uf"),
    }


def _snapshot_items(db: Session, order: m.ErpSalesOrder) -> list[dict]:
    items = db.scalars(
        select(m.ErpSalesOrderItem)
        .where(m.ErpSalesOrderItem.order_id == order.id, m.ErpSalesOrderItem.excluded.is_(False))
        .order_by(m.ErpSalesOrderItem.position)
    )
    return [
        {
            "position": i.position,
            "productId": i.product_external_id,
            "code": i.code,
            "name": i.name,
            "quantity": ser.quantity(i.quantity),
        }
        for i in items
    ]


def serialize_option(option: op.ErpShippingOption) -> dict:
    expired = bool(option.valid_until and as_utc(option.valid_until) < utcnow())
    return {
        "id": option.id,
        "carrier": option.carrier,
        "service": option.service,
        "price": ser.money(option.price),
        "deadlineMinDays": option.deadline_min_days,
        "deadlineMaxDays": option.deadline_max_days,
        "validUntil": ser.iso(option.valid_until),
        "expired": expired,
        "tracking": option.tracking,
        "pickupMode": option.pickup_mode,
        "notes": option.notes,
        "source": option.source,
        "createdBy": option.created_by,
        "createdAt": ser.iso(option.created_at),
    }


def serialize_quote(
    db: Session, quote: op.ErpShippingQuote, order: m.ErpSalesOrder | None, user: ErpUser
) -> dict:
    volumes = db.scalars(
        select(op.ErpShippingVolume).where(op.ErpShippingVolume.quote_id == quote.id).order_by(op.ErpShippingVolume.position)
    ).all()
    options = db.scalars(
        select(op.ErpShippingOption).where(op.ErpShippingOption.quote_id == quote.id).order_by(op.ErpShippingOption.price, op.ErpShippingOption.id)
    ).all()
    stale = is_stale(quote, order)
    pii = user.can("pii:read")
    destination = dict(quote.destination or {})
    if destination.get("zip") and not pii:
        destination["zip"] = ser.mask(destination["zip"], 2)
    total_weight = sum((v.weight_kg for v in volumes), Decimal("0"))
    total_cubage = sum((v.length_cm * v.width_cm * v.height_cm for v in volumes), Decimal("0")) / Decimal("1000000")
    return {
        "id": quote.id,
        "orderId": quote.order_external_id,
        "status": "stale" if stale and quote.status in ("draft", "selected") else quote.status,
        "stored_status": quote.status,
        "stale": stale,
        "staleReason": "O pedido foi alterado depois desta cotação; revise antes de usar." if stale else None,
        "source": quote.source,
        "version": quote.version,
        "orderVersion": quote.order_version,
        "originZip": quote.origin_zip,
        "destination": destination,
        "declaredValue": ser.money(quote.declared_value),
        "volumes": [
            {
                "position": v.position,
                "weightKg": plain_decimal(v.weight_kg),
                "lengthCm": str(v.length_cm), "widthCm": str(v.width_cm), "heightCm": str(v.height_cm),
            }
            for v in volumes
        ],
        "totals": {
            "volumes": len(volumes),
            "weightKg": str(total_weight.quantize(Decimal("0.001"))),
            "cubageM3": str(total_cubage.quantize(Decimal("0.0001"))),
            "items": len(quote.items_snapshot or []),
        },
        "items": quote.items_snapshot or [],
        "options": [serialize_option(o) for o in options],
        "selectedOptionId": quote.selected_option_id,
        "selectedAt": ser.iso(quote.selected_at),
        "selectedBy": quote.selected_by,
        "selectionReason": quote.selection_reason,
        "notes": quote.notes,
        "createdBy": quote.created_by,
        "createdAt": ser.iso(quote.created_at),
        "statement": "Seleção local: não contrata o frete, não gera etiqueta e não confirma envio.",
    }


def list_quotes(db: Session, user: ErpUser, connection_id: str, order_id: str) -> dict:
    order = _order(db, connection_id, order_id)
    quotes = db.scalars(
        select(op.ErpShippingQuote)
        .where(op.ErpShippingQuote.connection_id == connection_id, op.ErpShippingQuote.order_external_id == order_id)
        .order_by(op.ErpShippingQuote.id.desc())
    ).all()
    return {
        "orderId": order_id,
        "provider": PROVIDER,
        "orderFingerprintChanged": False,
        "items": [serialize_quote(db, q, order, user) for q in quotes],
    }


def create_quote(
    db: Session, user: ErpUser, connection_id: str, order_id: str, body: ShippingQuoteCreate
) -> dict:
    if body.mode == "automatic":
        raise http_error(409, "provider_unavailable", PROVIDER["reason"], provider=PROVIDER)
    order = _order(db, connection_id, order_id)
    if order.kind == "cancelled":
        raise http_error(409, "order_cancelled", "Pedido cancelado não recebe cotação de frete")
    if not order.items_complete:
        raise http_error(
            409, "items_incomplete",
            "Itens do pedido ainda não foram sincronizados; a cotação depende da lista completa",
        )
    quote = op.ErpShippingQuote(
        connection_id=connection_id,
        order_external_id=order_id,
        order_version=order.version or 1,
        order_fingerprint=order.fingerprint,
        origin_zip=body.originZip,
        destination=_destination(order, body.destinationZip),
        declared_value=body.declaredValue if body.declaredValue is not None else order.net_total,
        items_snapshot=_snapshot_items(db, order),
        status="draft",
        source="manual",
        notes=body.notes,
        created_by=user.username,
    )
    db.add(quote)
    db.flush()
    for position, volume in enumerate(body.volumes):
        db.add(
            op.ErpShippingVolume(
                quote_id=quote.id, position=position, weight_kg=volume.weightKg,
                length_cm=volume.lengthCm, width_cm=volume.widthCm, height_cm=volume.heightCm,
            )
        )
    db.flush()
    audit(
        db, operator=user.username, action="shipping.quote_created", connection_id=connection_id,
        resource="sales_order", resource_id=order_id,
        detail={"quoteId": quote.id, "volumes": len(body.volumes), "source": "manual"},
    )
    return serialize_quote(db, quote, order, user)


def add_option(
    db: Session, user: ErpUser, connection_id: str, quote_id: int, body: ShippingOptionInput
) -> dict:
    quote = _quote(db, connection_id, quote_id)
    order = db.scalar(
        select(m.ErpSalesOrder).where(
            m.ErpSalesOrder.connection_id == connection_id, m.ErpSalesOrder.external_id == quote.order_external_id
        )
    )
    if quote.status not in ("draft", "selected"):
        raise http_error(409, "quote_closed", "Cotação encerrada não recebe novas opções")
    if is_stale(quote, order):
        raise http_error(409, "stale_order", "O pedido mudou depois desta cotação; crie uma nova cotação")
    if (
        body.deadlineMinDays is not None
        and body.deadlineMaxDays is not None
        and body.deadlineMaxDays < body.deadlineMinDays
    ):
        raise http_error(422, "invalid_deadline", "Prazo máximo menor que o mínimo")
    if body.validUntil is not None and as_utc(body.validUntil) <= utcnow():
        raise http_error(422, "already_expired", "A validade informada já passou")
    option = op.ErpShippingOption(
        quote_id=quote.id, carrier=body.carrier.strip(), service=body.service.strip(), price=body.price,
        deadline_min_days=body.deadlineMinDays, deadline_max_days=body.deadlineMaxDays,
        valid_until=body.validUntil, tracking=body.tracking, pickup_mode=body.pickupMode,
        notes=body.notes, source="manual", created_by=user.username,
    )
    db.add(option)
    db.flush()
    audit(
        db, operator=user.username, action="shipping.option_registered", connection_id=connection_id,
        resource="sales_order", resource_id=quote.order_external_id,
        detail={"quoteId": quote.id, "optionId": option.id, "carrier": option.carrier,
                "price": ser.money(option.price), "source": "manual"},
    )
    return serialize_quote(db, quote, order, user)


def select_option(
    db: Session, user: ErpUser, connection_id: str, quote_id: int, body: ShippingSelectInput
) -> dict:
    quote = _quote(db, connection_id, quote_id)
    order = db.scalar(
        select(m.ErpSalesOrder).where(
            m.ErpSalesOrder.connection_id == connection_id, m.ErpSalesOrder.external_id == quote.order_external_id
        )
    )
    if quote.status in ("superseded", "cancelled"):
        raise http_error(409, "quote_closed", "Cotação encerrada; use a cotação vigente do pedido")
    if is_stale(quote, order):
        raise http_error(
            409, "stale_order",
            "O pedido foi alterado depois desta cotação; refaça a cotação para selecionar o frete",
        )
    option = db.scalar(
        select(op.ErpShippingOption).where(
            op.ErpShippingOption.id == body.optionId, op.ErpShippingOption.quote_id == quote.id
        )
    )
    if option is None:
        raise http_error(404, "option_not_found", "Opção não pertence a esta cotação")
    if option.valid_until is not None and as_utc(option.valid_until) < utcnow():
        raise http_error(409, "option_expired", "A validade desta opção venceu; registre uma nova cotação")
    if quote.version != body.expectedVersion:
        raise http_error(
            409, "version_conflict",
            "A cotação foi alterada por outra pessoa; recarregue e confira antes de selecionar",
            currentVersion=quote.version,
        )
    if quote.status == "selected" and quote.selected_option_id == option.id:
        return serialize_quote(db, quote, order, user)  # mesma seleção repetida: nada a fazer
    changing = quote.status == "selected"
    other_selected = db.scalar(
        select(op.ErpShippingQuote.id).where(
            op.ErpShippingQuote.connection_id == connection_id,
            op.ErpShippingQuote.order_external_id == quote.order_external_id,
            op.ErpShippingQuote.status == "selected",
            op.ErpShippingQuote.id != quote.id,
        )
    )
    if (changing or other_selected) and not (body.reason and body.reason.strip()):
        raise http_error(422, "reason_required", "Informe o motivo para trocar o frete já selecionado")
    try:
        with db.begin_nested():
            if other_selected:
                db.execute(
                    update(op.ErpShippingQuote)
                    .where(
                        op.ErpShippingQuote.connection_id == connection_id,
                        op.ErpShippingQuote.order_external_id == quote.order_external_id,
                        op.ErpShippingQuote.status == "selected",
                        op.ErpShippingQuote.id != quote.id,
                    )
                    .values(status="superseded")
                )
            claimed = db.execute(
                update(op.ErpShippingQuote)
                .where(op.ErpShippingQuote.id == quote.id, op.ErpShippingQuote.version == body.expectedVersion)
                .values(
                    status="selected", selected_option_id=option.id, selected_at=utcnow(),
                    selected_by=user.username, selection_reason=(body.reason or "").strip() or None,
                    version=body.expectedVersion + 1,
                )
            )
            if claimed.rowcount != 1:
                raise IntegrityError("conflito", {}, Exception("version"))
    except IntegrityError as exc:
        raise http_error(
            409, "version_conflict",
            "Outra pessoa selecionou o frete deste pedido ao mesmo tempo; recarregue e confira",
        ) from exc
    db.refresh(quote)
    audit(
        db, operator=user.username, action="shipping.option_selected", connection_id=connection_id,
        resource="sales_order", resource_id=quote.order_external_id, reason=body.reason,
        detail={"quoteId": quote.id, "optionId": option.id, "carrier": option.carrier,
                "price": ser.money(option.price), "changed": changing or bool(other_selected),
                "note": "seleção local; sem contratação"},
    )
    return serialize_quote(db, quote, order, user)


# --- estados para a lista de pedidos --------------------------------------------


def _deadline_text(option: op.ErpShippingOption) -> str | None:
    low, high = option.deadline_min_days, option.deadline_max_days
    if low is None and high is None:
        return None
    if low is not None and high is not None and low != high:
        return f"{low} a {high} dias úteis"
    return f"{high if high is not None else low} dias úteis"


def state_provider(db: Session, connection_id: str, orders: list[Any]) -> dict[str, dict]:
    """Estado de frete de cada pedido da página: none | draft | selected | stale."""
    ids = [o.external_id for o in orders]
    if not ids:
        return {}
    by_order = {o.external_id: o for o in orders}
    chosen: dict[str, op.ErpShippingQuote] = {}
    for quote in db.scalars(
        select(op.ErpShippingQuote)
        .where(
            op.ErpShippingQuote.connection_id == connection_id,
            op.ErpShippingQuote.order_external_id.in_(ids),
            op.ErpShippingQuote.status.in_(("draft", "selected")),
        )
        .order_by(op.ErpShippingQuote.id)
    ):
        current = chosen.get(quote.order_external_id)
        # a cotação selecionada vale mais que um rascunho; entre iguais, a mais recente
        if current is None or current.status != "selected" or quote.status == "selected":
            chosen[quote.order_external_id] = quote
    states: dict[str, dict] = {}
    for external_id in ids:
        quote = chosen.get(external_id)
        if quote is None:
            states[external_id] = {"state": "none"}
        elif is_stale(quote, by_order[external_id]):
            states[external_id] = {
                "state": "stale", "quoteId": quote.id, "reason": "Pedido alterado depois da cotação",
            }
        elif quote.status == "selected":
            option = db.get(op.ErpShippingOption, quote.selected_option_id) if quote.selected_option_id else None
            states[external_id] = {
                "state": "selected", "quoteId": quote.id,
                "label": f"{option.carrier} · {option.service}" if option else None,
                "price": ser.money(option.price) if option else None,
                "deadline": _deadline_text(option) if option else None,
                "reason": "Seleção local; não é contratação",
            }
        else:
            states[external_id] = {"state": "draft", "quoteId": quote.id}
    return states


def filter_provider(connection_id: str, value: str):
    """Condição SQL sobre erp_sales_orders para o filtro `shipping` (none|draft|selected|stale)."""
    from sqlalchemy import and_, exists, not_

    M, Q = m.ErpSalesOrder, op.ErpShippingQuote
    live = and_(Q.connection_id == connection_id, Q.order_external_id == M.external_id,
                Q.status.in_(("draft", "selected")))
    fresh = Q.order_fingerprint == M.fingerprint
    if value == "none":
        return not_(exists().where(live))
    if value == "stale":
        return exists().where(live, not_(fresh))
    if value == "selected":
        return exists().where(live, fresh, Q.status == "selected")
    if value == "draft":
        return and_(
            exists().where(live, fresh, Q.status == "draft"),
            not_(exists().where(live, fresh, Q.status == "selected")),
        )
    raise http_error(422, "invalid_filter", "Valor inválido para o filtro de frete")


def summary_section(db: Session, connection_id: str, filtered_ids) -> dict:
    """Contagem por estado de frete restrita ao conjunto de pedidos já filtrado."""
    from sqlalchemy import func

    M = m.ErpSalesOrder
    counts = {}
    for state in ("none", "draft", "selected", "stale"):
        counts[state] = int(
            db.scalar(
                select(func.count()).select_from(M).where(
                    M.id.in_(filtered_ids), filter_provider(connection_id, state)
                )
            )
            or 0
        )
    return {"available": True, "counts": counts}


def register() -> None:
    oo.STATE_PROVIDERS["shipping"] = state_provider
    oo.FILTER_PROVIDERS["shipping"] = filter_provider
    oo.SUMMARY_SECTIONS["shipping"] = summary_section
    oo.AVAILABLE_FILTERS.add("shipping")


def get_quote(db: Session, user: ErpUser, connection_id: str, quote_id: int) -> dict:
    quote = _quote(db, connection_id, quote_id)
    order = db.scalar(
        select(m.ErpSalesOrder).where(
            m.ErpSalesOrder.connection_id == connection_id, m.ErpSalesOrder.external_id == quote.order_external_id
        )
    )
    return serialize_quote(db, quote, order, user)
