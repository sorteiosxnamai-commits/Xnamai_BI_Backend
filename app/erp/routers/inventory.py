"""Estoque: saldo externo separado do operacional, depósitos, razão e reservas."""

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.erp.auth import ErpUser, require
from app.erp.common import iso, paginate, quantity
from app.erp.config import erp_settings
from app.erp.db import erp_db
from app.erp.models import commercial as m
from app.erp.models.local import (
    ErpInventoryBalance,
    ErpInventoryMovement,
    ErpInventoryReservation,
    ErpWarehouse,
)
from app.erp.routers import idem
from app.erp.schemas.commands import (
    AdjustmentInput,
    AuthorityInput,
    ReservationInput,
    ReversalInput,
    TransferInput,
    WarehouseInput,
)
from app.erp.services import inventory

router = APIRouter(prefix="/inventory", tags=["erp-inventory"])
PAGE = Query(1, ge=1)
PAGE_SIZE = Query(25, ge=1, le=100)


def cid() -> str:
    return erp_settings().erp_connection_id


def _warehouse(w: ErpWarehouse) -> dict:
    return {"id": w.id, "code": w.code, "name": w.name, "active": w.active}


def _movement(mv: ErpInventoryMovement) -> dict:
    return {
        "id": mv.id, "warehouseId": mv.warehouse_id, "productId": mv.product_external_id,
        "kind": mv.kind, "quantityDelta": quantity(mv.quantity_delta),
        "reservedDelta": quantity(mv.reserved_delta), "unitCost": quantity(mv.unit_cost),
        "referenceType": mv.reference_type, "referenceId": mv.reference_id,
        "reversalOfId": mv.reversal_of_id, "operator": mv.operator, "reason": mv.reason,
        "occurredAt": iso(mv.occurred_at),
    }


def _authority(row) -> dict:
    return {
        "scope": row.scope or "default",
        "authority": row.authority,
        "effective": inventory.is_erp_authority(row),
        "cutoverAt": iso(row.cutover_at),
        "reconciledAt": iso(row.reconciled_at),
        "publishEnabled": row.publish_enabled,
        "publishReason": "Publicação de estoque ao Mercos depende da capacidade write.inventory_publish "
                         "(pendente no Adaptor) e de corte conciliado",
        "setBy": row.set_by,
        "reason": row.reason,
        "movementsEnabled": inventory.is_erp_authority(row),
        "movementsReason": None if inventory.is_erp_authority(row) else
        "Modo consulta ao saldo Mercos: movimentos oficiais exigem corte e autoridade ERP",
    }


@router.get("/authority", summary="Autoridade do saldo por escopo")
def get_authority(user: ErpUser = Depends(require("inventory:read")), db: Session = Depends(erp_db)):
    row = inventory.get_authority(db, cid())
    db.commit()
    return _authority(row)


@router.put("/authority", summary="Define a autoridade do saldo (explícita e auditada)")
def put_authority(
    body: AuthorityInput,
    user: ErpUser = Depends(require("*")),
    db: Session = Depends(erp_db),
):
    row = inventory.set_authority(
        db, user, cid(), authority=body.authority, scope=body.scope, reason=body.reason,
        cutover_reconciled=body.cutoverReconciled,
    )
    db.commit()
    return _authority(row)


@router.get("/warehouses", summary="Depósitos")
def list_warehouses(user: ErpUser = Depends(require("inventory:read")), db: Session = Depends(erp_db)):
    rows = db.scalars(
        select(ErpWarehouse).where(ErpWarehouse.connection_id == cid()).order_by(ErpWarehouse.code)
    ).all()
    return {"items": [_warehouse(w) for w in rows]}


@router.post("/warehouses", status_code=201, summary="Cria depósito")
def create_warehouse(
    body: WarehouseInput,
    user: ErpUser = Depends(require("inventory:adjust")),
    db: Session = Depends(erp_db),
):
    row = inventory.create_warehouse(db, user, cid(), body.code, body.name)
    db.commit()
    return _warehouse(row)


@router.get("/balances", summary="Saldo operacional e saldo externo (separados)")
def list_balances(
    productId: str | None = None,
    warehouseId: int | None = None,
    page: int = PAGE,
    page_size: int = PAGE_SIZE,
    user: ErpUser = Depends(require("inventory:read")),
    db: Session = Depends(erp_db),
):
    M = ErpInventoryBalance
    query = select(M).where(M.connection_id == cid())
    if productId:
        query = query.where(M.product_external_id == productId)
    if warehouseId:
        query = query.where(M.warehouse_id == warehouseId)
    result, key, direction = paginate(
        db, query, id_column=M.id,
        sort_columns={"productId": M.product_external_id, "onHand": M.on_hand},
        sort=None, default_sort="productId", order="asc", page=page, page_size=page_size,
    )
    external = {
        p.external_id: p
        for p in db.scalars(
            select(m.ErpProduct).where(
                m.ErpProduct.connection_id == cid(),
                m.ErpProduct.external_id.in_([r.product_external_id for r in result.items]),
            )
        )
    }
    authority = inventory.get_authority(db, cid())
    envelope = result.envelope(
        lambda b: {
            "warehouseId": b.warehouse_id, "productId": b.product_external_id,
            "operational": {"onHand": quantity(b.on_hand), "reserved": quantity(b.reserved),
                            "available": quantity(b.on_hand - b.reserved)},
        },
        sort=key, order=direction, filters={"productId": productId, "warehouseId": warehouseId},
    )
    for item in envelope["items"]:
        product = external.get(item["productId"])
        item["productName"] = product.name if product else None
        item["external"] = {
            "quantity": quantity(product.external_stock) if product else None,
            "source": "mercos",
        }
    envelope["authority"] = _authority(authority)
    return envelope


@router.get("/movements", summary="Razão de movimentos (imutável)")
def list_movements(
    productId: str | None = None,
    warehouseId: int | None = None,
    kind: str | None = None,
    page: int = PAGE,
    page_size: int = PAGE_SIZE,
    user: ErpUser = Depends(require("inventory:read")),
    db: Session = Depends(erp_db),
):
    M = ErpInventoryMovement
    query = select(M).where(M.connection_id == cid())
    if productId:
        query = query.where(M.product_external_id == productId)
    if warehouseId:
        query = query.where(M.warehouse_id == warehouseId)
    if kind:
        query = query.where(M.kind == kind)
    result, key, direction = paginate(
        db, query, id_column=M.id, sort_columns={"occurredAt": M.occurred_at},
        sort=None, default_sort="occurredAt", order="desc", page=page, page_size=page_size,
    )
    return result.envelope(
        _movement, sort=key, order=direction,
        filters={"productId": productId, "warehouseId": warehouseId, "kind": kind},
    )


@router.post("/adjustments", status_code=201, summary="Ajusta saldo (valor absoluto)")
def create_adjustment(
    body: AdjustmentInput,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("inventory:adjust")),
    db: Session = Depends(erp_db),
):
    def handler():
        movement = inventory.adjust_to(
            db, user, cid(), warehouse_id=body.warehouseId, product=body.productId,
            new_quantity=body.newQuantity, reason=body.reason, key=idempotency_key or "",
        )
        return _movement(movement)

    return idem.run(db, user, "inventory.adjust", idempotency_key, body.model_dump(mode="json"), handler)


@router.post("/transfers", status_code=201, summary="Transfere entre depósitos")
def create_transfer(
    body: TransferInput,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("inventory:transfer")),
    db: Session = Depends(erp_db),
):
    def handler():
        out, incoming = inventory.transfer(
            db, user, cid(), from_warehouse=body.fromWarehouseId, to_warehouse=body.toWarehouseId,
            product=body.productId, quantity=body.quantity, reason=body.reason,
            key=idempotency_key or "",
        )
        return {"out": _movement(out), "in": _movement(incoming)}

    return idem.run(db, user, "inventory.transfer", idempotency_key, body.model_dump(mode="json"), handler)


@router.post(
    "/movements/{movement_id}/reversals", status_code=201, summary="Reverte um movimento (causal, uma vez)"
)
def reverse_movement(
    movement_id: int,
    body: ReversalInput,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("inventory:adjust")),
    db: Session = Depends(erp_db),
):
    def handler():
        movement = inventory.reverse_movement(db, user, cid(), movement_id, reason=body.reason)
        return _movement(movement)

    return idem.run(
        db, user, f"inventory.reverse:{movement_id}", idempotency_key, body.model_dump(mode="json"), handler
    )


@router.get("/reservations", summary="Reservas")
def list_reservations(
    status: str | None = None,
    user: ErpUser = Depends(require("inventory:read")),
    db: Session = Depends(erp_db),
):
    M = ErpInventoryReservation
    query = select(M).where(M.connection_id == cid()).order_by(M.id.desc()).limit(200)
    if status:
        query = query.where(M.status == status)
    return {
        "items": [
            {"id": r.id, "warehouseId": r.warehouse_id, "productId": r.product_external_id,
             "quantity": quantity(r.quantity), "status": r.status, "orderId": r.order_external_id,
             "createdBy": r.created_by, "createdAt": iso(r.created_at), "closedAt": iso(r.closed_at)}
            for r in db.scalars(query)
        ]
    }


@router.post("/reservations", status_code=201, summary="Reserva saldo")
def create_reservation(
    body: ReservationInput,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("inventory:adjust")),
    db: Session = Depends(erp_db),
):
    def handler():
        row = inventory.reserve(
            db, user, cid(), warehouse_id=body.warehouseId, product=body.productId,
            quantity=body.quantity, order_id=body.orderId, key=idempotency_key or "",
        )
        return {"id": row.id, "status": row.status, "quantity": quantity(row.quantity)}

    return idem.run(db, user, "inventory.reserve", idempotency_key, body.model_dump(mode="json"), handler)


@router.post("/reservations/{reservation_id}/release", summary="Libera reserva")
def release_reservation(
    reservation_id: int,
    user: ErpUser = Depends(require("inventory:adjust")),
    db: Session = Depends(erp_db),
):
    row = inventory.release(db, user, cid(), reservation_id)
    db.commit()
    return {"id": row.id, "status": row.status}

