"""Fornecedores e compras: rascunho, aprovação, recebimento parcial e cancelamento.

O recebimento encadeia, na mesma transação e com eventos causais únicos:
quantidade recebida (UPDATE condicional) → entrada de estoque (se o ERP for a
autoridade) → conta a pagar opcional. Custo real vem do recebimento, nunca do
preço de tabela de venda.
"""

from datetime import date
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.erp.audit import audit
from app.erp.auth import ErpUser
from app.erp.common import http_error, utcnow
from app.erp.models.local import (
    ErpFinTitle,
    ErpInventoryMovement,
    ErpPurchaseOrder,
    ErpPurchaseOrderItem,
    ErpPurchaseReceipt,
    ErpPurchaseReceiptItem,
    ErpSupplier,
    ErpWarehouse,
)
from app.erp.schemas.commands import PurchaseOrderInput, ReceiptInput, SupplierInput
from app.erp.services import finance, inventory


def create_supplier(db: Session, user: ErpUser, connection_id: str, body: SupplierInput) -> ErpSupplier:
    row = ErpSupplier(
        connection_id=connection_id,
        code=body.code.strip().upper(),
        name=body.name.strip(),
        document=body.document,
        email=body.email,
        phone=body.phone,
        city=body.city,
        state=body.state,
        payment_terms=body.paymentTerms,
        notes=body.notes,
        created_by=user.username,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise http_error(409, "duplicate_code", "Código de fornecedor já existe") from exc
    audit(db, operator=user.username, action="supplier.create", connection_id=connection_id,
          resource="supplier", resource_id=row.id)
    return row


def create_purchase_order(
    db: Session, user: ErpUser, connection_id: str, body: PurchaseOrderInput
) -> ErpPurchaseOrder:
    supplier = db.get(ErpSupplier, body.supplierId)
    if supplier is None or supplier.connection_id != connection_id or not supplier.active:
        raise http_error(422, "supplier_not_found", "Fornecedor não encontrado ou inativo")
    order = ErpPurchaseOrder(
        connection_id=connection_id,
        number=f"TMP-{uuid4().hex[:16]}",
        supplier_id=supplier.id,
        status="draft",
        expected_date=body.expectedDate,
        notes=body.notes,
        created_by=user.username,
    )
    total = Decimal("0")
    db.add(order)
    db.flush()
    order.number = f"PC-{order.id:06d}"  # único por construção, sem corrida
    for position, item in enumerate(body.items):
        db.add(
            ErpPurchaseOrderItem(
                purchase_order_id=order.id,
                position=position,
                product_external_id=item.productId,
                description=item.description,
                quantity=item.quantity,
                unit_cost=item.unitCost,
            )
        )
        total += item.quantity * item.unitCost
    order.total = total.quantize(Decimal("0.01"))
    db.add(order)
    audit(db, operator=user.username, action="purchase.create", connection_id=connection_id,
          resource="purchase_order", resource_id=order.id, detail={"total": str(order.total)})
    return order


def _order(db: Session, connection_id: str, order_id: int) -> ErpPurchaseOrder:
    order = db.get(ErpPurchaseOrder, order_id)
    if order is None or order.connection_id != connection_id:
        raise http_error(404, "purchase_not_found", "Pedido de compra não encontrado")
    return order


def approve(db: Session, user: ErpUser, connection_id: str, order_id: int) -> ErpPurchaseOrder:
    order = _order(db, connection_id, order_id)
    if order.status != "draft":
        raise http_error(409, "not_draft", "Somente rascunhos podem ser aprovados")
    order.status = "approved"
    order.approved_by = user.username
    order.approved_at = utcnow()
    order.version += 1
    db.add(order)
    audit(db, operator=user.username, action="purchase.approve", connection_id=connection_id,
          resource="purchase_order", resource_id=order.id)
    return order


def cancel(db: Session, user: ErpUser, connection_id: str, order_id: int, reason: str) -> ErpPurchaseOrder:
    order = _order(db, connection_id, order_id)
    if order.status in ("received", "cancelled"):
        raise http_error(409, "not_cancellable", "Pedido não pode ser cancelado neste estado")
    received = db.scalar(
        select(ErpPurchaseOrderItem.id).where(
            ErpPurchaseOrderItem.purchase_order_id == order.id,
            ErpPurchaseOrderItem.received_quantity > 0,
        )
    )
    if received is not None:
        raise http_error(409, "has_receipts", "Há recebimentos; o cancelamento exige estorno dedicado")
    order.status = "cancelled"
    order.cancelled_by = user.username
    order.cancelled_at = utcnow()
    order.cancel_reason = reason
    order.version += 1
    db.add(order)
    audit(db, operator=user.username, action="purchase.cancel", connection_id=connection_id,
          resource="purchase_order", resource_id=order.id, reason=reason)
    return order


def receive(
    db: Session,
    user: ErpUser,
    connection_id: str,
    order_id: int,
    body: ReceiptInput,
) -> dict:
    order = _order(db, connection_id, order_id)
    if order.status not in ("approved", "partially_received"):
        raise http_error(409, "not_receivable", "Somente pedidos aprovados recebem mercadoria")
    warehouse = db.get(ErpWarehouse, body.warehouseId)
    if warehouse is None or warehouse.connection_id != connection_id or not warehouse.active:
        raise http_error(404, "warehouse_not_found", "Depósito não encontrado ou inativo")
    receipt = ErpPurchaseReceipt(
        connection_id=connection_id,
        purchase_order_id=order.id,
        warehouse_id=warehouse.id,
        invoice_number=body.invoiceNumber,
        received_by=user.username,
        notes=body.notes,
    )
    db.add(receipt)
    db.flush()
    payable_total = Decimal("0")
    effects: list[str] = []
    for line in body.lines:
        item = db.get(ErpPurchaseOrderItem, line.itemId)
        if item is None or item.purchase_order_id != order.id:
            raise http_error(422, "item_not_in_order", "Item não pertence ao pedido de compra")
        result = db.execute(
            update(ErpPurchaseOrderItem)
            .where(
                ErpPurchaseOrderItem.id == item.id,
                ErpPurchaseOrderItem.received_quantity + line.quantity <= ErpPurchaseOrderItem.quantity,
            )
            .values(received_quantity=ErpPurchaseOrderItem.received_quantity + line.quantity)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise http_error(
                409, "over_receipt", "Recebimento acima da quantidade pendente do item",
                itemId=item.id,
            )
        db.add(
            ErpPurchaseReceiptItem(
                receipt_id=receipt.id,
                purchase_order_item_id=item.id,
                quantity=line.quantity,
                unit_cost=item.unit_cost,
            )
        )
        payable_total += line.quantity * item.unit_cost
        if item.product_external_id:
            effects.append(
                inventory.receive_stock(
                    db,
                    connection_id=connection_id,
                    warehouse_id=warehouse.id,
                    product=item.product_external_id,
                    quantity=line.quantity,
                    unit_cost=item.unit_cost,
                    causal_key=f"receipt:{receipt.id}:{item.id}",
                    reference_id=str(receipt.id),
                    operator=user.username,
                )
            )
        else:
            effects.append("no_product_link")
    db.flush()
    pending = db.scalar(
        select(ErpPurchaseOrderItem.id).where(
            ErpPurchaseOrderItem.purchase_order_id == order.id,
            ErpPurchaseOrderItem.received_quantity < ErpPurchaseOrderItem.quantity,
        ).limit(1)
    )
    order.status = "partially_received" if pending is not None else "received"
    order.version += 1
    db.add(order)
    payable_id = None
    if body.payable is not None and payable_total > 0:
        title, _ = finance.create_title(
            db, user, connection_id,
            kind="payable",
            description=f"Recebimento {order.number} / NF {body.invoiceNumber or 's/n'}",
            total=payable_total.quantize(Decimal("0.01")),
            first_due=body.payable.dueDate,
            installments=body.payable.installments,
            supplier_id=order.supplier_id,
            category_id=body.payable.categoryId,
            cost_center_id=body.payable.costCenterId,
            origin_type="purchase_receipt",
            origin_ref=str(receipt.id),
            causal_key=f"receipt:{receipt.id}:payable",
        )
        payable_id = title.id
    audit(db, operator=user.username, action="purchase.receive", connection_id=connection_id,
          resource="purchase_order", resource_id=order.id,
          detail={"receiptId": receipt.id, "lines": len(body.lines), "inventory": sorted(set(effects))})
    return {
        "receiptId": receipt.id,
        "purchaseOrderId": order.id,
        "status": order.status,
        "inventoryEffect": (
            "applied" if effects and all(e == "applied" for e in effects)
            else "not_applied_authority_mercos" if "not_applied_authority_mercos" in effects
            else "partial"
        ),
        "payableTitleId": payable_id,
    }


def reverse_receipt(
    db: Session,
    user: ErpUser,
    connection_id: str,
    order_id: int,
    receipt_id: int,
    reason: str,
) -> dict:
    """Estorna um recebimento inteiro, de forma atômica e no máximo uma vez.

    Na mesma transação: devolve a quantidade recebida dos itens, reverte cada
    movimento de estoque gerado (evento causal próprio, sem recalcular o
    histórico) e cancela a conta a pagar do recebimento. Se o estoque já foi
    consumido ou a conta já tem baixa, tudo é recusado e nada muda."""
    order = _order(db, connection_id, order_id)
    receipt = db.get(ErpPurchaseReceipt, receipt_id)
    if receipt is None or receipt.purchase_order_id != order.id or receipt.connection_id != connection_id:
        raise http_error(404, "receipt_not_found", "Recebimento não encontrado neste pedido")
    claimed = db.execute(
        update(ErpPurchaseReceipt)
        .where(ErpPurchaseReceipt.id == receipt.id, ErpPurchaseReceipt.reversed_at.is_(None))
        .values(reversed_at=utcnow(), reversed_by=user.username, reverse_reason=reason)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        raise http_error(409, "already_reversed", "Este recebimento já foi estornado")

    payable = db.scalar(
        select(ErpFinTitle).where(
            ErpFinTitle.connection_id == connection_id,
            ErpFinTitle.causal_key == f"receipt:{receipt.id}:payable",
        )
    )
    payable_cancelled = None
    if payable is not None and payable.status != "cancelled":
        finance.cancel_title(db, user, connection_id, payable.id, f"Estorno do recebimento {receipt.id}: {reason}")
        payable_cancelled = payable.id

    effects: list[str] = []
    lines = db.scalars(
        select(ErpPurchaseReceiptItem).where(ErpPurchaseReceiptItem.receipt_id == receipt.id)
    ).all()
    for line in lines:
        result = db.execute(
            update(ErpPurchaseOrderItem)
            .where(
                ErpPurchaseOrderItem.id == line.purchase_order_item_id,
                ErpPurchaseOrderItem.received_quantity - line.quantity >= 0,
            )
            .values(received_quantity=ErpPurchaseOrderItem.received_quantity - line.quantity)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise http_error(409, "inconsistent_received", "Quantidade recebida inconsistente para o estorno")
        movement = db.scalar(
            select(ErpInventoryMovement).where(
                ErpInventoryMovement.connection_id == connection_id,
                ErpInventoryMovement.causal_key == f"receipt:{receipt.id}:{line.purchase_order_item_id}",
            )
        )
        if movement is None:
            effects.append("no_stock_movement")
            continue
        inventory.reverse_movement(
            db, user, connection_id, movement.id,
            reason=f"Estorno do recebimento {receipt.id}: {reason}", enforce_authority=False,
        )
        effects.append("stock_reversed")

    db.flush()
    # colunas direto do banco: o UPDATE condicional não atualiza objetos já em memória
    remaining = db.execute(
        select(ErpPurchaseOrderItem.received_quantity, ErpPurchaseOrderItem.quantity).where(
            ErpPurchaseOrderItem.purchase_order_id == order.id
        )
    ).all()
    received_any = any(received > 0 for received, _ in remaining)
    pending_any = any(received < ordered for received, ordered in remaining)
    order.status = "partially_received" if received_any and pending_any else (
        "received" if received_any else "approved"
    )
    order.version += 1
    db.add(order)
    audit(db, operator=user.username, action="purchase.receipt_reverse", connection_id=connection_id,
          resource="purchase_order", resource_id=order.id, reason=reason,
          detail={"receiptId": receipt.id, "stock": sorted(set(effects)), "payableCancelled": payable_cancelled})
    return {
        "receiptId": receipt.id,
        "purchaseOrderId": order.id,
        "status": order.status,
        "stockEffect": "reversed" if "stock_reversed" in effects else "none",
        "payableTitleCancelled": payable_cancelled,
    }


def _date(value: date | None) -> str | None:
    return value.isoformat() if value else None
