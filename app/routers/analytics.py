from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import db_session
from app.schemas.analytics import AnalyticsFilters, PageResponse
from app.services.analytics_filters import analytics_filters
from app.services.analytics_v2 import (
    associations,
    breakdowns,
    cohorts,
    customer_detail,
    customers_page,
    filter_options,
    geography,
    inventory_page,
    order_detail,
    orders_page,
    overview,
    product_detail,
    products_page,
    rankings,
    seller_detail,
    sellers_page,
    timeseries,
)


router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


@router.get("/overview")
def get_overview(
    filters: AnalyticsFilters = Depends(analytics_filters),
    db: Session = Depends(db_session),
):
    return overview(db, filters)


@router.get("/timeseries")
def get_timeseries(
    filters: AnalyticsFilters = Depends(analytics_filters),
    db: Session = Depends(db_session),
):
    return timeseries(db, filters)


@router.get("/breakdowns")
def get_breakdowns(
    filters: AnalyticsFilters = Depends(analytics_filters),
    db: Session = Depends(db_session),
):
    return breakdowns(db, filters)


@router.get("/rankings")
def get_rankings(
    filters: AnalyticsFilters = Depends(analytics_filters),
    db: Session = Depends(db_session),
):
    return rankings(db, filters)


@router.get("/orders", response_model=PageResponse)
def get_orders(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    search: str | None = Query(None, max_length=200),
    sort: Literal[
        "number",
        "issued_at",
        "customer_name",
        "seller_name",
        "status",
        "total",
        "discount",
    ] = Query("issued_at"),
    order: Literal["asc", "desc"] = Query("desc"),
    filters: AnalyticsFilters = Depends(analytics_filters),
    db: Session = Depends(db_session),
):
    return orders_page(
        db,
        filters,
        page=page,
        page_size=page_size,
        search=search,
        sort=sort,
        order=order,
    )


@router.get("/orders/{mercos_id}")
def get_order_detail(
    mercos_id: str,
    filters: AnalyticsFilters = Depends(analytics_filters),
    db: Session = Depends(db_session),
):
    result = order_detail(db, mercos_id, filters)
    if result is None:
        raise HTTPException(404, "Pedido não encontrado nos filtros aplicados")
    return result


@router.get("/products", response_model=PageResponse)
def get_products(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    search: str | None = Query(None, max_length=200),
    sort: Literal[
        "code",
        "name",
        "quantity_sold",
        "order_count",
        "revenue",
        "average_price",
        "stock",
        "stock_value",
        "last_sale_at",
        "days_without_sale",
    ] = Query("revenue"),
    order: Literal["asc", "desc"] = Query("desc"),
    filters: AnalyticsFilters = Depends(analytics_filters),
    db: Session = Depends(db_session),
):
    return products_page(
        db,
        filters,
        page=page,
        page_size=page_size,
        search=search,
        sort=sort,
        order=order,
    )


@router.get("/products/{mercos_id}")
def get_product_detail(
    mercos_id: str,
    filters: AnalyticsFilters = Depends(analytics_filters),
    db: Session = Depends(db_session),
):
    result = product_detail(db, mercos_id, filters)
    if result is None:
        raise HTTPException(404, "Produto não encontrado nos filtros aplicados")
    return result


@router.get("/customers", response_model=PageResponse)
def get_customers(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    search: str | None = Query(None, max_length=200),
    sort: Literal[
        "name",
        "city",
        "state",
        "order_count",
        "revenue",
        "average_ticket",
        "first_order_at",
        "last_order_at",
        "days_since_last_order",
        "recency",
        "frequency",
        "monetary",
    ] = Query("revenue"),
    order: Literal["asc", "desc"] = Query("desc"),
    filters: AnalyticsFilters = Depends(analytics_filters),
    db: Session = Depends(db_session),
):
    return customers_page(
        db,
        filters,
        page=page,
        page_size=page_size,
        search=search,
        sort=sort,
        order=order,
    )


@router.get("/customers/{mercos_id}")
def get_customer_detail(
    mercos_id: str,
    filters: AnalyticsFilters = Depends(analytics_filters),
    db: Session = Depends(db_session),
):
    result = customer_detail(db, mercos_id, filters)
    if result is None:
        raise HTTPException(404, "Cliente não encontrado nos filtros aplicados")
    return result


@router.get("/sellers", response_model=PageResponse)
def get_sellers(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    search: str | None = Query(None, max_length=200),
    sort: Literal[
        "name",
        "order_count",
        "revenue",
        "average_ticket",
        "customers",
        "new_customers",
        "cancellations",
        "discount_total",
    ] = Query("revenue"),
    order: Literal["asc", "desc"] = Query("desc"),
    filters: AnalyticsFilters = Depends(analytics_filters),
    db: Session = Depends(db_session),
):
    return sellers_page(
        db,
        filters,
        page=page,
        page_size=page_size,
        search=search,
        sort=sort,
        order=order,
    )


@router.get("/sellers/{mercos_id}")
def get_seller_detail(
    mercos_id: str,
    filters: AnalyticsFilters = Depends(analytics_filters),
    db: Session = Depends(db_session),
):
    result = seller_detail(db, mercos_id, filters)
    if result is None:
        raise HTTPException(404, "Vendedor não encontrado nos filtros aplicados")
    return result


@router.get("/inventory", response_model=PageResponse)
def get_inventory(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    search: str | None = Query(None, max_length=200),
    sort: Literal[
        "code",
        "name",
        "quantity_sold",
        "order_count",
        "revenue",
        "average_price",
        "stock",
        "stock_value",
        "last_sale_at",
        "days_without_sale",
    ] = Query("stock_value"),
    order: Literal["asc", "desc"] = Query("desc"),
    filters: AnalyticsFilters = Depends(analytics_filters),
    db: Session = Depends(db_session),
):
    return inventory_page(
        db,
        filters,
        page=page,
        page_size=page_size,
        search=search,
        sort=sort,
        order=order,
    )


@router.get("/geography")
def get_geography(
    filters: AnalyticsFilters = Depends(analytics_filters),
    db: Session = Depends(db_session),
):
    return geography(db, filters)


@router.get("/cohorts")
def get_cohorts(
    filters: AnalyticsFilters = Depends(analytics_filters),
    db: Session = Depends(db_session),
):
    return cohorts(db, filters)


@router.get("/associations")
def get_associations(
    limit: int = Query(50, ge=1, le=200),
    filters: AnalyticsFilters = Depends(analytics_filters),
    db: Session = Depends(db_session),
):
    return associations(db, filters, limit=limit)


@router.get("/filter-options")
def get_filter_options(
    option: Literal[
        "sellers",
        "customers",
        "products",
        "categories",
        "states",
        "cities",
        "statuses",
        "segments",
        "order-types",
        "payment-conditions",
    ],
    search: str | None = Query(None, max_length=200),
    states: list[str] | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: Session = Depends(db_session),
):
    return filter_options(
        db,
        option=option,
        search=search,
        page=page,
        page_size=page_size,
        states=states,
    )
