"""Clientes, produtos, pedidos, catálogos auxiliares e recursos externos."""

from datetime import date

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.erp import capabilities, outbox
from app.erp import serializers as ser
from app.erp.auth import ErpUser, require
from app.erp.common import http_error, paginate
from app.erp.config import erp_settings
from app.erp.db import erp_db
from app.erp.models import commercial as m
from app.erp.registry import REGISTRY
from app.erp.schemas.commands import (
    CustomerCreate,
    CustomerPatch,
    OrderCreate,
    OrderPatch,
    TitleInput,
)

router = APIRouter(tags=["erp-commercial"])

PAGE = Query(1, ge=1)
PAGE_SIZE = Query(25, ge=1, le=100)
ORDER = Query("asc", pattern="^(asc|desc)$")


def cid() -> str:
    return erp_settings().erp_connection_id


def _like(column, text: str):
    return func.lower(column).contains(text.strip().lower(), autoescape=True)


def accepted(op, created: bool):
    status = 202 if created else 200
    return JSONResponse(status_code=status, content={**ser.operation(op), "replayed": not created})


def _get(db: Session, model, external_id: str):
    row = db.scalar(
        select(model).where(model.connection_id == cid(), model.external_id == external_id)
    )
    if row is None:
        raise http_error(404, "not_found", "Registro não encontrado")
    return row


# --- clientes ------------------------------------------------------------------


@router.get("/customers", summary="Clientes (paginado no servidor)")
def list_customers(
    search: str | None = None,
    state: str | None = None,
    segmentId: str | None = None,
    blocked: bool | None = None,
    active: bool | None = None,
    page: int = PAGE,
    page_size: int = PAGE_SIZE,
    sort: str | None = None,
    order: str = ORDER,
    user: ErpUser = Depends(require("read")),
    db: Session = Depends(erp_db),
):
    M = m.ErpCustomer
    query = select(M).where(M.connection_id == cid())
    if search:
        pattern = search.strip()
        query = query.where(
            _like(M.name, pattern) | _like(M.trade_name, pattern)
            | (_like(M.document, pattern) if user.can("pii:read") else _like(M.name, pattern))
        )
    if state:
        query = query.where(M.state == state.upper())
    if segmentId:
        query = query.where(M.segment_external_id == segmentId)
    if blocked is not None:
        query = query.where(M.blocked == blocked)
    if active is not None:
        query = query.where(M.active == active)
    result, key, direction = paginate(
        db, query, id_column=M.id,
        sort_columns={"name": M.name, "city": M.city, "state": M.state,
                      "updatedAt": M.source_updated_at},
        sort=sort, default_sort="name", order=order, page=page, page_size=page_size,
    )
    return result.envelope(
        lambda row: ser.customer(row, user), sort=key, order=direction,
        filters={"search": search, "state": state, "segmentId": segmentId,
                 "blocked": blocked, "active": active},
    )


@router.get("/customers/{external_id}", summary="Detalhe do cliente (espelho local)")
def get_customer(
    external_id: str,
    user: ErpUser = Depends(require("read")),
    db: Session = Depends(erp_db),
):
    row = _get(db, m.ErpCustomer, external_id)
    contacts = db.scalars(
        select(m.ErpCustomerContact).where(m.ErpCustomerContact.customer_id == row.id)
        .order_by(m.ErpCustomerContact.position)
    ).all()
    addresses = db.scalars(
        select(m.ErpCustomerAddress).where(m.ErpCustomerAddress.customer_id == row.id)
        .order_by(m.ErpCustomerAddress.position)
    ).all()
    return ser.customer(row, user, detail=True, contacts=contacts, addresses=addresses)


@router.post("/customers", status_code=202, summary="Cria cliente (assíncrono via outbox)")
def create_customer(
    body: CustomerCreate,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("customers:write")),
    db: Session = Depends(erp_db),
):
    op, created = outbox.submit(
        db, connection_id=cid(), user=user, kind="create_customer", body=body,
        idempotency_key=idempotency_key,
    )
    db.commit()
    return accepted(op, created)


@router.patch("/customers/{external_id}", status_code=202, summary="Edita cliente (outbox)")
def patch_customer(
    external_id: str,
    body: CustomerPatch,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("customers:write")),
    db: Session = Depends(erp_db),
):
    op, created = outbox.submit(
        db, connection_id=cid(), user=user, kind="update_customer", body=body,
        idempotency_key=idempotency_key, target_external_id=external_id,
    )
    db.commit()
    return accepted(op, created)


# --- produtos ------------------------------------------------------------------


@router.get("/products", summary="Catálogo de produtos")
def list_products(
    search: str | None = None,
    categoryId: str | None = None,
    kind: str | None = None,
    active: bool | None = None,
    page: int = PAGE,
    page_size: int = PAGE_SIZE,
    sort: str | None = None,
    order: str = ORDER,
    user: ErpUser = Depends(require("read")),
    db: Session = Depends(erp_db),
):
    M = m.ErpProduct
    query = select(M).where(M.connection_id == cid())
    if search:
        query = query.where(_like(M.name, search) | _like(M.code, search))
    if categoryId:
        query = query.where(M.category_external_id == categoryId)
    if kind:
        query = query.where(M.kind == kind)
    if active is not None:
        query = query.where(M.active == active)
    result, key, direction = paginate(
        db, query, id_column=M.id,
        sort_columns={"name": M.name, "code": M.code, "listPrice": M.list_price,
                      "externalStock": M.external_stock},
        sort=sort, default_sort="name", order=order, page=page, page_size=page_size,
    )
    return result.envelope(
        ser.product, sort=key, order=direction,
        filters={"search": search, "categoryId": categoryId, "kind": kind, "active": active},
    )


@router.get("/products/{external_id}", summary="Detalhe do produto")
def get_product(
    external_id: str,
    user: ErpUser = Depends(require("read")),
    db: Session = Depends(erp_db),
):
    row = _get(db, m.ErpProduct, external_id)
    variants = db.scalars(
        select(m.ErpProductVariant).where(m.ErpProductVariant.product_id == row.id)
        .order_by(m.ErpProductVariant.position)
    ).all()
    return ser.product(row, detail=True, variants=variants)


@router.get("/products/{external_id}/prices", summary="Preços por tabela")
def product_prices(
    external_id: str,
    user: ErpUser = Depends(require("read")),
    db: Session = Depends(erp_db),
):
    _get(db, m.ErpProduct, external_id)
    rows = db.scalars(
        select(m.ErpProductPrice).where(
            m.ErpProductPrice.connection_id == cid(),
            m.ErpProductPrice.product_external_id == external_id,
        ).order_by(m.ErpProductPrice.price_table_external_id)
    ).all()
    tables = {
        t.external_id: t.name
        for t in db.scalars(select(m.ErpPriceTable).where(m.ErpPriceTable.connection_id == cid()))
    }
    return {
        "items": [
            {**ser.product_price(row), "priceTableName": tables.get(row.price_table_external_id)}
            for row in rows
        ]
    }


@router.get("/products/{external_id}/variants", summary="Variantes (grade)")
def product_variants(
    external_id: str,
    user: ErpUser = Depends(require("read")),
    db: Session = Depends(erp_db),
):
    row = _get(db, m.ErpProduct, external_id)
    rows = db.scalars(
        select(m.ErpProductVariant).where(m.ErpProductVariant.product_id == row.id)
        .order_by(m.ErpProductVariant.position)
    ).all()
    return {"items": ser.product(row, detail=True, variants=rows)["variants"]}


def _disabled(db: Session, key: str):
    enabled, reason = capabilities.capability_enabled(db, cid(), key)
    raise http_error(
        409, "capability_disabled", "Capacidade desabilitada", capability=key, reason=reason,
        enabled=enabled,
    )


@router.post("/products", status_code=409, summary="Cria produto (capacidade pendente no Adaptor)")
def create_product(user: ErpUser = Depends(require("products:write")), db: Session = Depends(erp_db)):
    _disabled(db, "write.products")


@router.patch("/products/{external_id}", status_code=409, summary="Edita produto (capacidade pendente)")
def patch_product(
    external_id: str, user: ErpUser = Depends(require("products:write")), db: Session = Depends(erp_db)
):
    _disabled(db, "write.products")


# --- pedidos -------------------------------------------------------------------


@router.get("/sales-orders", summary="Pedidos e orçamentos")
def list_orders(
    search: str | None = None,
    customerId: str | None = None,
    kind: str | None = None,
    commercialStatus: str | None = None,
    itemsComplete: bool | None = None,
    dateFrom: date | None = None,
    dateTo: date | None = None,
    page: int = PAGE,
    page_size: int = PAGE_SIZE,
    sort: str | None = None,
    order: str = Query("desc", pattern="^(asc|desc)$"),
    user: ErpUser = Depends(require("read")),
    db: Session = Depends(erp_db),
):
    M = m.ErpSalesOrder
    query = select(M).where(M.connection_id == cid())
    if search:
        query = query.where(_like(M.number, search))
    if customerId:
        query = query.where(M.customer_external_id == customerId)
    if kind:
        query = query.where(M.kind == kind)
    if commercialStatus:
        query = query.where(M.commercial_status == commercialStatus)
    if itemsComplete is not None:
        query = query.where(M.items_complete == itemsComplete)
    if dateFrom:
        query = query.where(M.issue_date >= dateFrom)
    if dateTo:
        query = query.where(M.issue_date <= dateTo)
    result, key, direction = paginate(
        db, query, id_column=M.id,
        sort_columns={"issuedAt": M.issued_at, "number": M.number, "netTotal": M.net_total},
        sort=sort, default_sort="issuedAt", order=order, page=page, page_size=page_size,
    )
    customers = {
        c.external_id: c.name
        for c in db.scalars(
            select(m.ErpCustomer).where(
                m.ErpCustomer.connection_id == cid(),
                m.ErpCustomer.external_id.in_([r.customer_external_id for r in result.items if r.customer_external_id]),
            )
        )
    }
    envelope = result.envelope(
        lambda row: ser.order(row, user), sort=key, order=direction,
        filters={"search": search, "customerId": customerId, "kind": kind,
                 "commercialStatus": commercialStatus, "itemsComplete": itemsComplete,
                 "dateFrom": dateFrom and dateFrom.isoformat(),
                 "dateTo": dateTo and dateTo.isoformat()},
    )
    for item in envelope["items"]:
        item["customerName"] = customers.get(item["customerId"])
    return envelope


@router.get("/sales-orders/{external_id}", summary="Detalhe do pedido/orçamento")
def get_order(
    external_id: str,
    user: ErpUser = Depends(require("read")),
    db: Session = Depends(erp_db),
):
    row = _get(db, m.ErpSalesOrder, external_id)
    items = db.scalars(
        select(m.ErpSalesOrderItem).where(m.ErpSalesOrderItem.order_id == row.id)
        .order_by(m.ErpSalesOrderItem.position)
    ).all()
    data = ser.order(row, user, detail=True, items=items)
    customer = db.scalar(
        select(m.ErpCustomer).where(
            m.ErpCustomer.connection_id == cid(),
            m.ErpCustomer.external_id == row.customer_external_id,
        )
    )
    data["customerName"] = customer.name if customer else None
    return data


@router.post("/sales-orders", status_code=202, summary="Cria pedido (outbox)")
def create_order(
    body: OrderCreate,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("orders:write")),
    db: Session = Depends(erp_db),
):
    op, created = outbox.submit(
        db, connection_id=cid(), user=user, kind="create_order", body=body,
        idempotency_key=idempotency_key,
    )
    db.commit()
    return accepted(op, created)


@router.patch("/sales-orders/{external_id}", status_code=202, summary="Edita pedido (outbox)")
def patch_order(
    external_id: str,
    body: OrderPatch,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("orders:write")),
    db: Session = Depends(erp_db),
):
    op, created = outbox.submit(
        db, connection_id=cid(), user=user, kind="update_order", body=body,
        idempotency_key=idempotency_key, target_external_id=external_id,
    )
    db.commit()
    return accepted(op, created)


@router.post("/sales-orders/{external_id}/cancel", status_code=409,
             summary="Cancela pedido (capacidade pendente no Adaptor)")
def cancel_order(
    external_id: str, user: ErpUser = Depends(require("orders:cancel")), db: Session = Depends(erp_db)
):
    _disabled(db, "write.order_cancel")


@router.post("/sales-orders/{external_id}/billings", status_code=409,
             summary="Registra faturamento (capacidade pendente no Adaptor)")
def bill_order(
    external_id: str, user: ErpUser = Depends(require("orders:bill")), db: Session = Depends(erp_db)
):
    _disabled(db, "write.billing")


# --- catálogos auxiliares -------------------------------------------------------

CATALOGS = {
    alias: REGISTRY[alias]
    for alias in (
        "categories", "segments", "order-types", "payment-conditions",
        "price-tables", "carriers", "commercial-policies", "users",
    )
}


@router.get("/catalogs/{resource}", summary="Cadastros auxiliares (somente recursos configurados)")
def list_catalog(
    resource: str,
    search: str | None = None,
    active: bool | None = None,
    page: int = PAGE,
    page_size: int = PAGE_SIZE,
    sort: str | None = None,
    order: str = ORDER,
    user: ErpUser = Depends(require("read")),
    db: Session = Depends(erp_db),
):
    if resource not in CATALOGS:
        raise http_error(404, "unknown_catalog", "Catálogo não configurado", allowed=sorted(CATALOGS))
    M = CATALOGS[resource].model
    query = select(M).where(M.connection_id == cid())
    if search:
        query = query.where(_like(M.name, search))
    if active is not None:
        query = query.where(M.active == active)
    result, key, direction = paginate(
        db, query, id_column=M.id, sort_columns={"name": M.name, "updatedAt": M.source_updated_at},
        sort=sort, default_sort="name", order=order, page=page, page_size=page_size,
    )
    return result.envelope(
        lambda row: ser.catalog_entry(resource, row), sort=key, order=direction,
        filters={"search": search, "active": active},
    )


@router.get("/catalogs/{resource}/{local_id}", summary="Detalhe de cadastro por ID local")
def get_catalog_entry(
    resource: str,
    local_id: int,
    user: ErpUser = Depends(require("read")),
    db: Session = Depends(erp_db),
):
    if resource not in CATALOGS:
        raise http_error(404, "unknown_catalog", "Catálogo não configurado")
    M = CATALOGS[resource].model
    row = db.scalar(select(M).where(M.connection_id == cid(), M.id == local_id))
    if row is None:
        raise http_error(404, "not_found", "Registro não encontrado")
    return ser.catalog_entry(resource, row)


@router.get("/product-prices", summary="Preços por produto/tabela")
def list_product_prices(
    productId: str | None = None,
    priceTableId: str | None = None,
    page: int = PAGE,
    page_size: int = PAGE_SIZE,
    order: str = ORDER,
    user: ErpUser = Depends(require("read")),
    db: Session = Depends(erp_db),
):
    M = m.ErpProductPrice
    query = select(M).where(M.connection_id == cid())
    if productId:
        query = query.where(M.product_external_id == productId)
    if priceTableId:
        query = query.where(M.price_table_external_id == priceTableId)
    result, key, direction = paginate(
        db, query, id_column=M.id,
        sort_columns={"productId": M.product_external_id, "price": M.price},
        sort=None, default_sort="productId", order=order, page=page, page_size=page_size,
    )
    return result.envelope(
        ser.product_price, sort=key, order=direction,
        filters={"productId": productId, "priceTableId": priceTableId},
    )


# --- recursos externos dependentes de extensão ----------------------------------


def _availability(db: Session, key: str) -> dict:
    enabled, reason = capabilities.capability_enabled(db, cid(), key)
    item = capabilities.BY_KEY[key]
    return {
        "capability": key,
        "enabled": enabled,
        "implementedInErp": item.implemented,
        "supportedByAdaptor": item.adaptor,
        "reason": reason or item.reason or None,
    }


def _external_list(db: Session, model, key: str, serializer):
    rows = db.scalars(select(model).where(model.connection_id == cid()).limit(100)).all()
    return {
        "items": [serializer(r) for r in rows],
        "availability": _availability(db, key),
        "note": "Sem dados: recurso ainda não disponível. Ausência não é zero.",
    }


@router.get("/external-titles", summary="Títulos externos (Mercos)")
def list_external_titles(user: ErpUser = Depends(require("financial_links:read")), db: Session = Depends(erp_db)):
    return _external_list(
        db, m.ErpExternalTitle, "read.titles",
        lambda r: {"id": r.external_id, "customerId": r.customer_external_id,
                   "amount": ser.money(r.amount), "dueDate": r.due_date and r.due_date.isoformat(),
                   "status": r.status},
    )


@router.post("/external-titles", status_code=409, summary="Cria título Mercos (desabilitado)")
def create_external_title(
    body: TitleInput, user: ErpUser = Depends(require("finance:write")), db: Session = Depends(erp_db)
):
    _disabled(db, "write.titles")


@router.patch("/external-titles/{external_id}", status_code=409, summary="Altera título Mercos (desabilitado)")
def patch_external_title(
    external_id: str, user: ErpUser = Depends(require("finance:write")), db: Session = Depends(erp_db)
):
    _disabled(db, "write.titles")


@router.get("/payments", summary="Pagamentos externos (Mercos Pay)")
def list_payments(user: ErpUser = Depends(require("financial_links:read")), db: Session = Depends(erp_db)):
    show_token = user.can("financial_links:read")
    return _external_list(
        db, m.ErpExternalPayment, "read.payments",
        lambda r: {"id": r.external_id, "status": r.status, "settlementStatus": r.settlement_status,
                   "amount": ser.money(r.amount), "chargeback": r.chargeback,
                   "token": r.payment_token if show_token else None},
    )


@router.get("/commissions", summary="Comissões (pendente)")
def list_commissions(user: ErpUser = Depends(require("read")), db: Session = Depends(erp_db)):
    return {"items": [], "availability": _availability(db, "read.commissions")}


@router.get("/promotions", summary="Promoções (pendente)")
def list_promotions(user: ErpUser = Depends(require("read")), db: Session = Depends(erp_db)):
    return {"items": [], "availability": _availability(db, "read.promotions")}
