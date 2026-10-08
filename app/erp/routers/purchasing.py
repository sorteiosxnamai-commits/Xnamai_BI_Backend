"""Fornecedores e compras."""

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.erp.auth import ErpUser, require
from app.erp.common import http_error, iso, money, paginate, quantity
from app.erp.config import erp_settings
from app.erp.db import erp_db
from app.erp.models.local import (
    ErpPurchaseOrder,
    ErpPurchaseOrderItem,
    ErpPurchaseReceipt,
    ErpPurchaseReceiptItem,
    ErpSupplier,
)
from app.erp.routers import idem
from app.erp.schemas.commands import (
    PurchaseCancelInput,
    PurchaseOrderInput,
    ReceiptInput,
    ReversalInput,
    SupplierInput,
)
from app.erp.services import purchasing

router = APIRouter(tags=["erp-purchasing"])
PAGE = Query(1, ge=1)
PAGE_SIZE = Query(25, ge=1, le=100)


def cid() -> str:
    return erp_settings().erp_connection_id


def supplier_view(s: ErpSupplier) -> dict:
    return {
        "id": s.id, "code": s.code, "name": s.name, "document": s.document, "email": s.email,
        "phone": s.phone, "city": s.city, "state": s.state, "paymentTerms": s.payment_terms,
        "active": s.active, "notes": s.notes, "createdAt": iso(s.created_at),
    }


def order_view(o: ErpPurchaseOrder, items=None, supplier: ErpSupplier | None = None) -> dict:
    data = {
        "id": o.id, "number": o.number, "supplierId": o.supplier_id,
        "supplierName": supplier.name if supplier else None, "status": o.status,
        "expectedDate": o.expected_date.isoformat() if o.expected_date else None,
        "total": money(o.total), "version": o.version, "createdBy": o.created_by,
        "approvedBy": o.approved_by, "approvedAt": iso(o.approved_at),
        "cancelReason": o.cancel_reason, "createdAt": iso(o.created_at), "notes": o.notes,
    }
    if items is not None:
        data["items"] = [
            {"id": i.id, "position": i.position, "productId": i.product_external_id,
             "description": i.description, "quantity": quantity(i.quantity),
             "receivedQuantity": quantity(i.received_quantity),
             "pendingQuantity": quantity(i.quantity - i.received_quantity),
             "unitCost": quantity(i.unit_cost)}
            for i in items
        ]
    return data


@router.get("/suppliers", summary="Fornecedores")
def list_suppliers(
    search: str | None = None,
    page: int = PAGE,
    page_size: int = PAGE_SIZE,
    order: str = Query("asc", pattern="^(asc|desc)$"),
    user: ErpUser = Depends(require("purchases:read")),
    db: Session = Depends(erp_db),
):
    from sqlalchemy import func

    M = ErpSupplier
    query = select(M).where(M.connection_id == cid())
    if search:
        query = query.where(func.lower(M.name).contains(search.strip().lower(), autoescape=True))
    result, key, direction = paginate(
        db, query, id_column=M.id, sort_columns={"name": M.name, "code": M.code},
        sort=None, default_sort="name", order=order, page=page, page_size=page_size,
    )
    return result.envelope(supplier_view, sort=key, order=direction, filters={"search": search})


@router.post("/suppliers", status_code=201, summary="Cria fornecedor")
def create_supplier(
    body: SupplierInput,
    user: ErpUser = Depends(require("purchases:write")),
    db: Session = Depends(erp_db),
):
    row = purchasing.create_supplier(db, user, cid(), body)
    db.commit()
    return supplier_view(row)


@router.get("/purchase-orders", summary="Pedidos de compra")
def list_purchase_orders(
    status: str | None = None,
    supplierId: int | None = None,
    page: int = PAGE,
    page_size: int = PAGE_SIZE,
    order: str = Query("desc", pattern="^(asc|desc)$"),
    user: ErpUser = Depends(require("purchases:read")),
    db: Session = Depends(erp_db),
):
    M = ErpPurchaseOrder
    query = select(M).where(M.connection_id == cid())
    if status:
        query = query.where(M.status == status)
    if supplierId:
        query = query.where(M.supplier_id == supplierId)
    result, key, direction = paginate(
        db, query, id_column=M.id, sort_columns={"createdAt": M.created_at, "number": M.number},
        sort=None, default_sort="createdAt", order=order, page=page, page_size=page_size,
    )
    suppliers = {
        s.id: s for s in db.scalars(select(ErpSupplier).where(ErpSupplier.connection_id == cid()))
    }
    return result.envelope(
        lambda o: order_view(o, supplier=suppliers.get(o.supplier_id)),
        sort=key, order=direction, filters={"status": status, "supplierId": supplierId},
    )


@router.get("/purchase-orders/{order_id}", summary="Detalhe do pedido de compra")
def get_purchase_order(
    order_id: int,
    user: ErpUser = Depends(require("purchases:read")),
    db: Session = Depends(erp_db),
):
    order = db.get(ErpPurchaseOrder, order_id)
    if order is None or order.connection_id != cid():
        raise http_error(404, "purchase_not_found", "Pedido de compra não encontrado")
    items = db.scalars(
        select(ErpPurchaseOrderItem).where(ErpPurchaseOrderItem.purchase_order_id == order.id)
        .order_by(ErpPurchaseOrderItem.position)
    ).all()
    data = order_view(order, items, db.get(ErpSupplier, order.supplier_id))
    receipts = db.scalars(
        select(ErpPurchaseReceipt).where(ErpPurchaseReceipt.purchase_order_id == order.id)
        .order_by(ErpPurchaseReceipt.id)
    ).all()
    lines = db.scalars(
        select(ErpPurchaseReceiptItem).where(
            ErpPurchaseReceiptItem.receipt_id.in_([r.id for r in receipts] or [0])
        )
    ).all()
    data["receipts"] = [
        {
            "id": r.id, "invoiceNumber": r.invoice_number, "receivedBy": r.received_by,
            "receivedAt": iso(r.received_at), "warehouseId": r.warehouse_id,
            "reversedAt": iso(r.reversed_at), "reversedBy": r.reversed_by,
            "reverseReason": r.reverse_reason,
            "lines": [
                {"itemId": line.purchase_order_item_id, "quantity": quantity(line.quantity)}
                for line in lines if line.receipt_id == r.id
            ],
        }
        for r in receipts
    ]
    return data


@router.post("/purchase-orders", status_code=201, summary="Cria pedido de compra (rascunho)")
def create_purchase_order(
    body: PurchaseOrderInput,
    user: ErpUser = Depends(require("purchases:write")),
    db: Session = Depends(erp_db),
):
    order = purchasing.create_purchase_order(db, user, cid(), body)
    db.commit()
    items = db.scalars(
        select(ErpPurchaseOrderItem).where(ErpPurchaseOrderItem.purchase_order_id == order.id)
        .order_by(ErpPurchaseOrderItem.position)
    ).all()
    return order_view(order, items, db.get(ErpSupplier, order.supplier_id))


@router.post("/purchase-orders/{order_id}/approve", summary="Aprova pedido de compra")
def approve_purchase_order(
    order_id: int,
    user: ErpUser = Depends(require("purchases:approve")),
    db: Session = Depends(erp_db),
):
    order = purchasing.approve(db, user, cid(), order_id)
    db.commit()
    return order_view(order)


@router.post("/purchase-orders/{order_id}/cancel", summary="Cancela pedido de compra")
def cancel_purchase_order(
    order_id: int,
    body: PurchaseCancelInput,
    user: ErpUser = Depends(require("purchases:approve")),
    db: Session = Depends(erp_db),
):
    order = purchasing.cancel(db, user, cid(), order_id, body.reason)
    db.commit()
    return order_view(order)


@router.post(
    "/purchase-orders/{order_id}/receipts/{receipt_id}/reversals",
    status_code=201,
    summary="Estorna um recebimento (estoque e conta a pagar, atômico, uma vez)",
)
def reverse_receipt(
    order_id: int,
    receipt_id: int,
    body: ReversalInput,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("purchases:receive")),
    db: Session = Depends(erp_db),
):
    return idem.run(
        db, user, f"purchase.receipt_reverse:{order_id}:{receipt_id}", idempotency_key,
        body.model_dump(mode="json"),
        lambda: purchasing.reverse_receipt(db, user, cid(), order_id, receipt_id, body.reason),
        status=201,
    )


@router.post("/purchase-orders/{order_id}/receive", summary="Recebe mercadoria (parcial ou total)")
def receive_purchase_order(
    order_id: int,
    body: ReceiptInput,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("purchases:receive")),
    db: Session = Depends(erp_db),
):
    return idem.run(
        db, user, f"purchase.receive:{order_id}", idempotency_key,
        body.model_dump(mode="json"),
        lambda: purchasing.receive(db, user, cid(), order_id, body),
        status=201,
    )
