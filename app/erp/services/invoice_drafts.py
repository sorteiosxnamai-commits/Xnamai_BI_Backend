"""Montagem fiscal (rascunho). NÃO emite nota.

Garantias deste módulo:

- salvar a montagem não emite NF-e; sem emissor configurado a emissão responde indisponível com
  motivo. Nenhuma chave, protocolo, XML ou DANFE é produzido;
- o percentual define um ALVO de planejamento; o valor efetivo vem dos itens, das quantidades e dos
  descontos (valor líquido do item). A diferença é exibida assinada, em reais e em percentual;
- itens ficam ligados à origem (`source_key` = id do item no Mercos) com snapshot versionado: a
  sincronização troca os ids locais dos itens e o rascunho continua íntegro;
- a mesma quantidade de um item nunca é alocada duas vezes em documentos ativos do mesmo pedido
  (o pedido é travado durante a alocação, então documentos concorrentes se serializam);
- a organização automática é determinística (ordem dos itens, sem promessa de combinação ótima),
  nunca excede o saldo alocável e nunca altera o pedido;
- mudança do pedido na origem (item removido/alterado) gera revisão obrigatória; o histórico do
  que mudou fica na auditoria.
"""

from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.erp import serializers as ser
from app.erp.audit import audit
from app.erp.auth import ErpUser
from app.erp.common import http_error, plain_decimal, utcnow
from app.erp.models import commercial as m
from app.erp.models import operations as op
from app.erp.schemas.operations import InvoiceCancel, InvoiceDraftCreate, InvoiceDraftPatch, InvoiceVersioned
from app.erp.services import order_operations as oo

CENT = Decimal("0.01")
ZERO = Decimal("0")
ISSUER = {
    "available": False,
    "reason": "Nenhum emissor fiscal está configurado. A emissão depende da escolha do emissor ou "
    "integração, dos dados da empresa emitente, de ambiente de homologação e das regras de "
    "faturamento parcial definidas com o responsável fiscal. Faturamento do Mercos não substitui isso.",
}
STATEMENT = "Rascunho de planejamento: não emite nota fiscal e não gera chave, protocolo, XML nem DANFE."


def q2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _order(db: Session, connection_id: str, external_id: str, *, lock: bool = False) -> m.ErpSalesOrder:
    query = select(m.ErpSalesOrder).where(
        m.ErpSalesOrder.connection_id == connection_id, m.ErpSalesOrder.external_id == external_id
    )
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise http_error(404, "not_found", "Pedido não encontrado")
    return row


def _draft(db: Session, connection_id: str, draft_id: int) -> op.ErpInvoiceDraft:
    row = db.scalar(
        select(op.ErpInvoiceDraft).where(
            op.ErpInvoiceDraft.connection_id == connection_id, op.ErpInvoiceDraft.id == draft_id
        )
    )
    if row is None:
        raise http_error(404, "not_found", "Rascunho não encontrado")
    return row


def current_items(db: Session, order: m.ErpSalesOrder) -> dict[str, dict]:
    """Itens vivos do pedido, indexados pela identidade de ORIGEM (nunca pelo id local)."""
    items: dict[str, dict] = {}
    occurrence: dict[str, int] = {}
    rows = db.scalars(
        select(m.ErpSalesOrderItem).where(m.ErpSalesOrderItem.order_id == order.id).order_by(m.ErpSalesOrderItem.position)
    )
    for position, item in enumerate(rows):
        if item.excluded:
            continue
        if item.external_id:
            key = f"i:{item.external_id}"
        else:
            base = item.product_external_id or item.code or "sem-produto"
            occurrence[base] = occurrence.get(base, 0) + 1
            key = f"p:{base}#{occurrence[base]}"
        items[key] = {
            "position": position,
            "productId": item.product_external_id,
            "code": item.code,
            "name": item.name,
            "quantity": item.quantity or ZERO,
            "lineTotal": item.total,
            "unitPrice": item.unit_price,
        }
    return items


def unit_value(source_quantity: Decimal, line_total: Decimal | None, unit_price: Decimal | None) -> Decimal:
    """Valor líquido por unidade (já com desconto do item), sem arredondar."""
    if line_total is not None and source_quantity > 0:
        return line_total / source_quantity
    return unit_price or ZERO


def line_value_for(quantity: Decimal, source_quantity: Decimal, line_total: Decimal | None, unit: Decimal) -> Decimal:
    if quantity <= 0:
        return ZERO
    if quantity == source_quantity and line_total is not None:
        return q2(line_total)  # quantidade integral: valor exato do item, sem erro de arredondamento
    return q2(quantity * unit)


def _elsewhere(db: Session, connection_id: str, order_id: str, source_key: str, exclude: int | None) -> Decimal:
    query = (
        select(func.coalesce(func.sum(op.ErpInvoiceDraftItem.quantity), 0))
        .join(op.ErpInvoiceDraft, op.ErpInvoiceDraft.id == op.ErpInvoiceDraftItem.draft_id)
        .where(
            op.ErpInvoiceDraft.connection_id == connection_id,
            op.ErpInvoiceDraft.order_external_id == order_id,
            op.ErpInvoiceDraft.status == "draft",
            op.ErpInvoiceDraftItem.source_key == source_key,
        )
    )
    if exclude is not None:
        query = query.where(op.ErpInvoiceDraft.id != exclude)
    return Decimal(str(db.scalar(query) or 0))


def is_stale(draft: op.ErpInvoiceDraft, order: m.ErpSalesOrder | None) -> bool:
    return order is None or (draft.order_fingerprint or "") != (order.fingerprint or "")


def review_changes(items: list[op.ErpInvoiceDraftItem], current: dict[str, dict]) -> list[dict]:
    """O que mudou na origem desde o snapshot do rascunho (preserva o 'antes')."""
    changes: list[dict] = []
    known = set()
    for item in items:
        known.add(item.source_key)
        now = current.get(item.source_key)
        if now is None:
            changes.append({
                "sourceKey": item.source_key, "kind": "removed", "name": item.name,
                "before": {"quantity": ser.quantity(item.source_quantity), "lineTotal": ser.money(item.source_line_total)},
                "after": None,
                "valueDifference": ser.money(-item.line_value),
                "reason": "Item removido do pedido na origem",
            })
            continue
        if now["quantity"] != item.source_quantity:
            changes.append({
                "sourceKey": item.source_key, "kind": "quantity_changed", "name": item.name,
                "before": {"quantity": ser.quantity(item.source_quantity)},
                "after": {"quantity": ser.quantity(now["quantity"])},
                "valueDifference": None, "reason": "Quantidade do item alterada na origem",
            })
        elif now["lineTotal"] != item.source_line_total:
            changes.append({
                "sourceKey": item.source_key, "kind": "price_changed", "name": item.name,
                "before": {"lineTotal": ser.money(item.source_line_total)},
                "after": {"lineTotal": ser.money(now["lineTotal"])},
                "valueDifference": None, "reason": "Valor do item alterado na origem",
            })
    for key, now in current.items():
        if key not in known:
            changes.append({
                "sourceKey": key, "kind": "added", "name": now["name"], "before": None,
                "after": {"quantity": ser.quantity(now["quantity"]), "lineTotal": ser.money(now["lineTotal"])},
                "valueDifference": None, "reason": "Item novo no pedido na origem",
            })
    return changes


def serialize(db: Session, draft: op.ErpInvoiceDraft, order: m.ErpSalesOrder | None, user: ErpUser) -> dict:
    items = db.scalars(
        select(op.ErpInvoiceDraftItem).where(op.ErpInvoiceDraftItem.draft_id == draft.id).order_by(op.ErpInvoiceDraftItem.position)
    ).all()
    current = current_items(db, order) if order is not None else {}
    stale = is_stale(draft, order)
    changes = review_changes(items, current) if stale else []
    allocated = sum((i.line_value for i in items), ZERO)
    target = draft.target_value
    out_items = []
    for i in items:
        elsewhere = _elsewhere(db, draft.connection_id, draft.order_external_id, i.source_key, draft.id) if draft.status == "draft" else ZERO
        available = max(i.source_quantity - elsewhere, ZERO)
        unit = unit_value(i.source_quantity, i.source_line_total, i.source_unit_price)
        removed = any(c["sourceKey"] == i.source_key and c["kind"] == "removed" for c in changes)
        out_items.append({
            "sourceKey": i.source_key,
            "position": i.position,
            "productId": i.product_external_id,
            "code": i.code,
            "name": i.name,
            "sourceQuantity": ser.quantity(i.source_quantity),
            "unitValue": ser.money(q2(unit)),
            "sourceLineTotal": ser.money(i.source_line_total),
            "quantity": ser.quantity(i.quantity),
            "lineValue": ser.money(i.line_value),
            "allocatedElsewhere": ser.quantity(elsewhere),
            "available": ser.quantity(available),
            "included": i.quantity > 0,
            "status": "removed_at_source" if removed else ("included" if i.quantity > 0 else "excluded"),
        })
    difference = allocated - target
    return {
        "id": draft.id,
        "orderId": draft.order_external_id,
        "status": draft.status,
        "version": draft.version,
        "stale": stale and draft.status == "draft",
        "orderVersion": draft.order_version,
        "percent": plain_decimal(draft.target_percent),
        "orderTotal": ser.money(draft.order_total),
        "target": {"percent": plain_decimal(draft.target_percent), "value": ser.money(target)},
        "effective": {
            "value": ser.money(allocated),
            "percentOfOrder": str((allocated / draft.order_total * Decimal(100)).quantize(CENT, ROUND_HALF_UP))
            if draft.order_total
            else None,
            "achievedOfTarget": str((allocated / target * Decimal(100)).quantize(CENT, ROUND_HALF_UP)) if target else None,
        },
        "difference": {"value": ser.money(difference), "direction": "above" if difference > 0 else "below" if difference < 0 else "exact"},
        "counts": {"included": sum(1 for i in items if i.quantity > 0), "total": len(items)},
        "items": out_items,
        "review": {
            "required": draft.status == "draft" and stale,
            "changes": changes,
            "note": "O pedido mudou na origem depois deste rascunho; revise e confirme antes de editar."
            if stale and draft.status == "draft"
            else None,
        },
        "notes": draft.notes,
        "issuance": ISSUER,
        "cancelledAt": ser.iso(draft.cancelled_at),
        "cancelledBy": draft.cancelled_by,
        "cancelReason": draft.cancel_reason,
        "createdBy": draft.created_by,
        "createdAt": ser.iso(draft.created_at),
        "statement": STATEMENT,
        "organizeNote": "Organização automática: ordem dos itens até o alvo, sem garantia de combinação ótima.",
    }


def list_drafts(db: Session, user: ErpUser, connection_id: str, order_id: str) -> dict:
    order = _order(db, connection_id, order_id)
    drafts = db.scalars(
        select(op.ErpInvoiceDraft)
        .where(op.ErpInvoiceDraft.connection_id == connection_id, op.ErpInvoiceDraft.order_external_id == order_id)
        .order_by(op.ErpInvoiceDraft.id.desc())
    ).all()
    return {"orderId": order_id, "issuance": ISSUER, "items": [serialize(db, d, order, user) for d in drafts]}


def get_draft(db: Session, user: ErpUser, connection_id: str, draft_id: int) -> dict:
    draft = _draft(db, connection_id, draft_id)
    order = db.scalar(
        select(m.ErpSalesOrder).where(
            m.ErpSalesOrder.connection_id == connection_id, m.ErpSalesOrder.external_id == draft.order_external_id
        )
    )
    return serialize(db, draft, order, user)


def _target(order_total: Decimal, percent: Decimal) -> Decimal:
    return q2(order_total * percent / Decimal(100))


def _organize(db: Session, draft: op.ErpInvoiceDraft, items: list[op.ErpInvoiceDraftItem]) -> None:
    """Gulosa e determinística: percorre os itens na ordem do pedido e aloca quantidades inteiras (ou
    em passos de 0,0001 se o item for fracionário) enquanto couberem no alvo e no saldo alocável."""
    remaining = draft.target_value
    for item in items:
        item.quantity = ZERO
        item.line_value = ZERO
    for item in items:
        unit = unit_value(item.source_quantity, item.source_line_total, item.source_unit_price)
        if unit <= 0 or remaining <= 0:
            continue
        elsewhere = _elsewhere(db, draft.connection_id, draft.order_external_id, item.source_key, draft.id)
        available = item.source_quantity - elsewhere
        if available <= 0:
            continue
        integral = item.source_quantity == item.source_quantity.to_integral_value()
        step = Decimal(1) if integral else Decimal("0.0001")
        fits = (remaining / unit / step).to_integral_value(rounding=ROUND_FLOOR) * step
        take = min((available / step).to_integral_value(rounding=ROUND_FLOOR) * step, fits)
        if take <= 0:
            continue
        value = line_value_for(take, item.source_quantity, item.source_line_total, unit)
        if value > remaining and take == item.source_quantity:
            continue
        item.quantity = take
        item.line_value = value
        remaining -= value


def create_draft(db: Session, user: ErpUser, connection_id: str, order_id: str, body: InvoiceDraftCreate) -> dict:
    order = _order(db, connection_id, order_id, lock=True)
    if order.kind == "cancelled":
        raise http_error(409, "order_cancelled", "Pedido cancelado não recebe rascunho fiscal")
    if not order.items_complete:
        raise http_error(409, "items_incomplete", "Itens do pedido ainda não foram sincronizados; o rascunho depende da lista completa")
    if order.net_total is None or order.net_total <= 0:
        raise http_error(409, "order_total_unavailable", "O pedido não tem valor total conhecido")
    current = current_items(db, order)
    if not current:
        raise http_error(409, "no_items", "O pedido não tem itens vivos para compor a nota")
    draft = op.ErpInvoiceDraft(
        connection_id=connection_id, order_external_id=order_id, order_version=order.version or 1,
        order_fingerprint=order.fingerprint, order_total=order.net_total, target_percent=body.percent,
        target_value=_target(order.net_total, body.percent), status="draft", notes=body.notes, created_by=user.username,
    )
    db.add(draft)
    db.flush()
    rows = []
    for index, (key, data) in enumerate(current.items()):
        row = op.ErpInvoiceDraftItem(
            draft_id=draft.id, position=index, source_key=key, product_external_id=data["productId"],
            code=data["code"], name=data["name"], source_quantity=data["quantity"],
            source_line_total=data["lineTotal"], source_unit_price=data["unitPrice"], quantity=ZERO, line_value=ZERO,
        )
        db.add(row)
        rows.append(row)
    db.flush()
    if body.organize:
        _organize(db, draft, rows)
    audit(
        db, operator=user.username, action="invoice.draft_created", connection_id=connection_id,
        resource="sales_order", resource_id=order_id,
        detail={"draftId": draft.id, "percent": str(body.percent), "organize": body.organize},
    )
    db.flush()
    return serialize(db, draft, order, user)


def _claim(db: Session, draft: op.ErpInvoiceDraft, expected: int) -> None:
    claimed = db.execute(
        update(op.ErpInvoiceDraft)
        .where(op.ErpInvoiceDraft.id == draft.id, op.ErpInvoiceDraft.version == expected)
        .values(version=expected + 1)
    )
    if claimed.rowcount != 1:
        raise http_error(
            409, "version_conflict",
            "O rascunho foi alterado por outra pessoa; recarregue e confira antes de salvar",
            currentVersion=draft.version,
        )


def _items(db: Session, draft: op.ErpInvoiceDraft) -> list[op.ErpInvoiceDraftItem]:
    return list(
        db.scalars(
            select(op.ErpInvoiceDraftItem).where(op.ErpInvoiceDraftItem.draft_id == draft.id).order_by(op.ErpInvoiceDraftItem.position)
        )
    )


def _rebase(db: Session, draft: op.ErpInvoiceDraft, order: m.ErpSalesOrder, items: list[op.ErpInvoiceDraftItem], user: ErpUser) -> list[dict]:
    """Aplica a revisão: realinha o snapshot à origem. O 'antes' vai para a auditoria."""
    current = current_items(db, order)
    changes = review_changes(items, current)
    kept: list[op.ErpInvoiceDraftItem] = []
    for item in items:
        now = current.get(item.source_key)
        if now is None:
            db.delete(item)  # removido na origem; fica registrado em `changes` (histórico)
            continue
        item.name, item.code, item.product_external_id = now["name"], now["code"], now["productId"]
        item.source_quantity, item.source_line_total, item.source_unit_price = now["quantity"], now["lineTotal"], now["unitPrice"]
        elsewhere = _elsewhere(db, draft.connection_id, draft.order_external_id, item.source_key, draft.id)
        item.quantity = min(item.quantity, max(item.source_quantity - elsewhere, ZERO))
        unit = unit_value(item.source_quantity, item.source_line_total, item.source_unit_price)
        item.line_value = line_value_for(item.quantity, item.source_quantity, item.source_line_total, unit)
        kept.append(item)
    next_position = len(kept)
    known = {i.source_key for i in kept}
    for key, now in current.items():
        if key not in known:
            row = op.ErpInvoiceDraftItem(
                draft_id=draft.id, position=next_position, source_key=key, product_external_id=now["productId"],
                code=now["code"], name=now["name"], source_quantity=now["quantity"],
                source_line_total=now["lineTotal"], source_unit_price=now["unitPrice"], quantity=ZERO, line_value=ZERO,
            )
            db.add(row)
            kept.append(row)
            next_position += 1
    for index, item in enumerate(sorted(kept, key=lambda i: i.position)):
        item.position = index
    draft.order_version = order.version or 1
    draft.order_fingerprint = order.fingerprint
    if order.net_total and order.net_total > 0:
        draft.order_total = order.net_total
        draft.target_value = _target(order.net_total, draft.target_percent)
    audit(
        db, operator=user.username, action="invoice.review_applied", connection_id=draft.connection_id,
        resource="sales_order", resource_id=draft.order_external_id,
        detail={"draftId": draft.id, "changes": changes},
    )
    db.flush()
    return changes


def patch_draft(db: Session, user: ErpUser, connection_id: str, draft_id: int, body: InvoiceDraftPatch) -> dict:
    draft = _draft(db, connection_id, draft_id)
    order = _order(db, connection_id, draft.order_external_id, lock=True)
    if draft.status != "draft":
        raise http_error(409, "draft_closed", "Rascunho cancelado não pode ser editado")
    items = _items(db, draft)
    if is_stale(draft, order) and not body.acknowledgeReview:
        raise http_error(
            409, "review_required",
            "O pedido mudou na origem depois deste rascunho; confirme a revisão antes de editar",
            changes=review_changes(items, current_items(db, order)),
        )
    _claim(db, draft, body.expectedVersion)
    db.refresh(draft)
    if is_stale(draft, order):
        _rebase(db, draft, order, items, user)
        items = _items(db, draft)
    if body.percent is not None:
        draft.target_percent = body.percent
        draft.target_value = _target(draft.order_total, body.percent)
    if body.notes is not None:
        draft.notes = body.notes
    if body.items is not None:
        by_key = {i.source_key: i for i in items}
        seen = set()
        for allocation in body.items:
            item = by_key.get(allocation.sourceKey)
            if item is None:
                raise http_error(422, "unknown_item", "Item não pertence a este rascunho", sourceKey=allocation.sourceKey)
            if allocation.sourceKey in seen:
                raise http_error(422, "duplicate_item", "Item repetido na edição", sourceKey=allocation.sourceKey)
            seen.add(allocation.sourceKey)
            elsewhere = _elsewhere(db, connection_id, draft.order_external_id, item.source_key, draft.id)
            available = item.source_quantity - elsewhere
            if allocation.quantity > available:
                raise http_error(
                    409, "allocation_exceeds_balance",
                    "Quantidade acima do saldo alocável (outros documentos já usam parte do item)",
                    sourceKey=item.source_key, available=ser.quantity(max(available, ZERO)),
                )
            item.quantity = allocation.quantity
            unit = unit_value(item.source_quantity, item.source_line_total, item.source_unit_price)
            item.line_value = line_value_for(item.quantity, item.source_quantity, item.source_line_total, unit)
    audit(
        db, operator=user.username, action="invoice.draft_updated", connection_id=connection_id,
        resource="sales_order", resource_id=draft.order_external_id,
        detail={"draftId": draft.id, "percent": body.percent and str(body.percent),
                "itemsEdited": len(body.items or []), "reviewAcknowledged": body.acknowledgeReview},
    )
    db.flush()
    db.refresh(draft)
    return serialize(db, draft, order, user)


def organize_draft(db: Session, user: ErpUser, connection_id: str, draft_id: int, body: InvoiceVersioned) -> dict:
    draft = _draft(db, connection_id, draft_id)
    order = _order(db, connection_id, draft.order_external_id, lock=True)
    if draft.status != "draft":
        raise http_error(409, "draft_closed", "Rascunho cancelado não pode ser editado")
    items = _items(db, draft)
    if is_stale(draft, order):
        raise http_error(
            409, "review_required", "O pedido mudou na origem; aplique a revisão antes de reorganizar",
            changes=review_changes(items, current_items(db, order)),
        )
    _claim(db, draft, body.expectedVersion)
    db.refresh(draft)
    _organize(db, draft, items)
    audit(
        db, operator=user.username, action="invoice.draft_organized", connection_id=connection_id,
        resource="sales_order", resource_id=draft.order_external_id, detail={"draftId": draft.id},
    )
    db.flush()
    db.refresh(draft)
    return serialize(db, draft, order, user)


def cancel_draft(db: Session, user: ErpUser, connection_id: str, draft_id: int, body: InvoiceCancel) -> dict:
    draft = _draft(db, connection_id, draft_id)
    order = _order(db, connection_id, draft.order_external_id, lock=True)
    if draft.status == "cancelled":
        raise http_error(409, "draft_closed", "Rascunho já está cancelado")
    _claim(db, draft, body.expectedVersion)
    db.refresh(draft)
    draft.status = "cancelled"
    draft.cancelled_at = utcnow()
    draft.cancelled_by = user.username
    draft.cancel_reason = body.reason.strip()
    audit(
        db, operator=user.username, action="invoice.draft_cancelled", connection_id=connection_id,
        resource="sales_order", resource_id=draft.order_external_id, reason=body.reason,
        detail={"draftId": draft.id, "releasedValue": ser.money(sum((i.line_value for i in _items(db, draft)), ZERO))},
    )
    db.flush()
    return serialize(db, draft, order, user)


def issue_unavailable() -> None:
    raise http_error(409, "issuer_unavailable", ISSUER["reason"], issuance=ISSUER)


# --- estados para a lista de pedidos --------------------------------------------


def state_provider(db: Session, connection_id: str, orders: list[Any]) -> dict[str, dict]:
    ids = [o.external_id for o in orders]
    if not ids:
        return {}
    by_order = {o.external_id: o for o in orders}
    states = {i: {"state": "none"} for i in ids}
    for draft in db.scalars(
        select(op.ErpInvoiceDraft)
        .where(
            op.ErpInvoiceDraft.connection_id == connection_id,
            op.ErpInvoiceDraft.order_external_id.in_(ids),
            op.ErpInvoiceDraft.status == "draft",
        )
        .order_by(op.ErpInvoiceDraft.id)
    ):
        current = states[draft.order_external_id]
        stale = is_stale(draft, by_order[draft.order_external_id])
        if stale:
            states[draft.order_external_id] = {"state": "stale", "draftId": draft.id,
                                               "reason": "Pedido alterado na origem; revisar o rascunho"}
        elif current["state"] != "stale":
            states[draft.order_external_id] = {"state": "draft", "draftId": draft.id,
                                               "reason": "Rascunho de planejamento; nota não emitida"}
    return states


def filter_provider(connection_id: str, value: str):
    from sqlalchemy import and_, exists, not_

    M, D = m.ErpSalesOrder, op.ErpInvoiceDraft
    live = and_(D.connection_id == connection_id, D.order_external_id == M.external_id, D.status == "draft")
    stale = D.order_fingerprint != M.fingerprint
    if value == "none":
        return not_(exists().where(live))
    if value == "stale":
        return exists().where(live, stale)
    if value == "draft":
        return and_(exists().where(live), not_(exists().where(live, stale)))
    raise http_error(422, "invalid_filter", "Valor inválido para o filtro fiscal")


def summary_section(db: Session, connection_id: str, filtered_ids) -> dict:
    M = m.ErpSalesOrder
    counts = {}
    for state in ("none", "draft", "stale"):
        counts[state] = int(
            db.scalar(
                select(func.count()).select_from(M).where(M.id.in_(filtered_ids), filter_provider(connection_id, state))
            )
            or 0
        )
    return {"available": True, "counts": counts, "issuance": ISSUER}


def register() -> None:
    oo.STATE_PROVIDERS["invoice"] = state_provider
    oo.FILTER_PROVIDERS["fiscal"] = filter_provider
    oo.SUMMARY_SECTIONS["invoice"] = summary_section
    oo.AVAILABLE_FILTERS.add("fiscal")
