from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Integer,
    String,
    and_,
    asc,
    case,
    cast,
    desc,
    exists,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.orm import Session, aliased
from sqlalchemy.exc import OperationalError

from app.domain.order_status import (
    CANCELLED_ORDER_STATUSES,
    VALID_SALE_STATUSES,
    status_sql_in,
)
from app.models import (
    Category,
    Customer,
    CustomerSegment,
    Order,
    OrderItem,
    OrderType,
    PaymentCondition,
    Product,
    Seller,
    SyncState,
)
from app.schemas.analytics import AnalyticsFilters
from app.services.analytics_filters import (
    applied_filters,
    date_bounds,
    order_conditions,
    previous_bounds,
)


ZERO = Decimal("0")
PLACEHOLDER_LIST_PRICES = frozenset({Decimal("1000")})
BR_TZ = ZoneInfo("America/Sao_Paulo")


def _decimal(value) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _valid_list_price_expression():
    return case(
        (Product.list_price.in_(PLACEHOLDER_LIST_PRICES), None),
        else_=Product.list_price,
    )


def _current_item_value_expression():
    return OrderItem.quantity * _valid_list_price_expression()


def _current_order_values(
    filters: AnalyticsFilters,
    bounds: tuple[datetime | None, datetime | None] | None = None,
    statuses: frozenset[str] | set[str] | None = None,
):
    conditions = [
        *order_conditions(filters, bounds=bounds),
        OrderItem.excluded.is_(False),
        _valid_list_price_expression().is_not(None),
    ]
    if statuses is not None:
        conditions.append(status_sql_in(Order.status, statuses))
    if filters.productIds:
        conditions.append(Product.mercos_id.in_(filters.productIds))
    if filters.categoryIds:
        conditions.append(
            or_(
                Product.category_mercos_id.in_(filters.categoryIds),
                Product.category_id.in_(filters.categoryIds),
            )
        )
    return (
        select(
            Order.mercos_id.label("order_id"),
            Order.customer_mercos_id.label("customer_id"),
            Order.seller_mercos_id.label("seller_id"),
            Order.issued_at.label("issued_at"),
            Order.status.label("status"),
            func.sum(_current_item_value_expression()).label("current_total"),
            func.sum(OrderItem.quantity).label("item_count"),
            func.count(func.distinct(OrderItem.product_mercos_id)).label(
                "sku_count"
            ),
        )
        .select_from(Order)
        .join(OrderItem, OrderItem.order_mercos_id == Order.mercos_id)
        .join(Product, Product.mercos_id == OrderItem.product_mercos_id)
        .where(*conditions)
        .group_by(
            Order.mercos_id,
            Order.customer_mercos_id,
            Order.seller_mercos_id,
            Order.issued_at,
            Order.status,
        )
        .subquery()
    )


def _header_revenue_expression():
    return func.coalesce(Order.net_total, Order.total)


def _header_order_values(
    filters: AnalyticsFilters,
    bounds: tuple[datetime | None, datetime | None] | None = None,
    statuses: frozenset[str] | set[str] | None = None,
):
    conditions = list(order_conditions(filters, bounds=bounds))
    if statuses is not None:
        conditions.append(status_sql_in(Order.status, statuses))
    return (
        select(
            Order.mercos_id.label("order_id"),
            Order.customer_mercos_id.label("customer_id"),
            Order.seller_mercos_id.label("seller_id"),
            Order.issued_at.label("issued_at"),
            Order.status.label("status"),
            _header_revenue_expression().label("current_total"),
            func.coalesce(Order.item_count, 0).label("item_count"),
            func.coalesce(Order.sku_count, 0).label("sku_count"),
        )
        .where(*conditions)
        .subquery()
    )


def _trend(current: Decimal | int, previous: Decimal | int) -> str:
    if current > previous:
        return "up"
    if current < previous:
        return "down"
    return "stable"


def _kpi(
    value,
    previous,
    definition: str,
    *,
    positive_when_up: bool = True,
) -> dict[str, Any]:
    current_value = _decimal(value)
    previous_value = _decimal(previous)
    absolute = current_value - previous_value
    percentage = (
        float((absolute / abs(previous_value)) * 100)
        if previous_value != 0
        else None
    )
    trend = _trend(current_value, previous_value)
    is_positive = trend == "stable" or (
        (trend == "up") if positive_when_up else (trend == "down")
    )
    return {
        "value": current_value,
        "previousValue": previous_value,
        "absoluteChange": absolute,
        "percentageChange": round(percentage, 2) if percentage is not None else None,
        "trend": trend,
        "isPositive": is_positive,
        "definition": definition,
    }


def analytics_metadata(db: Session) -> dict[str, Any]:
    total_orders = int(db.scalar(select(func.count(Order.id))) or 0)
    orders_with_items = int(
        db.scalar(
            select(func.count(Order.id)).where(Order.item_count > 0)
        )
        or 0
    )
    coverage = (
        round((orders_with_items / total_orders) * 100, 2)
        if total_orders
        else 0.0
    )
    data_through = db.scalar(select(func.max(Order.issued_at)))
    incomplete = list(
        db.scalars(
            select(SyncState.resource).where(
                SyncState.status.in_(
                    ("running", "partial", "interrupted", "error")
                )
            )
        )
    )
    warnings = []
    if coverage < 95:
        warnings.append(
            "Cobertura de itens abaixo de 95%; métricas por produto são parciais."
        )
    if incomplete:
        warnings.append(
            "Sincronização incompleta em: " + ", ".join(sorted(incomplete)) + "."
        )
    return {
        "generatedAt": datetime.now(timezone.utc),
        "dataThrough": data_through,
        "isPartial": bool(incomplete) or coverage < 95,
        "warnings": warnings,
        "quality": {
            "ordersWithItemsPct": coverage,
            "orders": total_orders,
            "ordersWithItems": orders_with_items,
        },
    }


def _sale_conditions(
    filters: AnalyticsFilters,
    bounds: tuple[datetime | None, datetime | None] | None = None,
) -> list:
    return [
        *order_conditions(filters, bounds=bounds),
        status_sql_in(Order.status, VALID_SALE_STATUSES),
    ]


def _summary_for_bounds(
    db: Session,
    filters: AnalyticsFilters,
    bounds: tuple[datetime | None, datetime | None],
) -> dict[str, Any]:
    common = order_conditions(filters, bounds=bounds)
    valid_values = _current_order_values(
        filters,
        bounds,
        VALID_SALE_STATUSES,
    )
    row = db.execute(
        select(
            func.count(valid_values.c.order_id),
            func.coalesce(func.sum(valid_values.c.current_total), 0),
            func.coalesce(func.sum(valid_values.c.current_total), 0),
            literal(ZERO),
            func.count(func.distinct(valid_values.c.customer_id)),
            func.coalesce(func.sum(valid_values.c.item_count), 0),
            func.coalesce(func.sum(valid_values.c.sku_count), 0),
        ).select_from(valid_values)
    ).one()
    cancelled_values = _current_order_values(
        filters,
        bounds,
        CANCELLED_ORDER_STATUSES,
    )
    cancelled_count = int(
        db.scalar(
            select(func.count(Order.id)).where(
                *common,
                status_sql_in(Order.status, CANCELLED_ORDER_STATUSES),
            )
        )
        or 0
    )
    cancelled_value = _decimal(
        db.scalar(
            select(
                func.coalesce(
                    func.sum(cancelled_values.c.current_total),
                    0,
                )
            ).select_from(cancelled_values)
        )
    )
    all_orders = int(
        db.scalar(select(func.count(Order.id)).where(*common)) or 0
    )
    valid_orders = int(row[0] or 0)
    net_revenue = _decimal(row[2])
    return {
        "orders": valid_orders,
        "grossRevenue": _decimal(row[1]),
        "netRevenue": net_revenue,
        "averageTicket": net_revenue / valid_orders if valid_orders else ZERO,
        "customers": int(row[4] or 0),
        "discountTotal": _decimal(row[3]),
        "items": int(row[5] or 0),
        "skus": int(row[6] or 0),
        "cancellations": cancelled_count,
        "cancelledValue": cancelled_value,
        "cancellationRate": (
            (Decimal(cancelled_count) / all_orders) * 100
            if all_orders
            else ZERO
        ),
    }


def _buyer_mix(
    db: Session,
    filters: AnalyticsFilters,
    bounds: tuple[datetime | None, datetime | None],
) -> tuple[int, int]:
    current_customers = (
        select(Order.customer_mercos_id.label("customer_id"))
        .where(
            *_sale_conditions(filters, bounds),
            Order.customer_mercos_id.is_not(None),
        )
        .distinct()
        .subquery()
    )
    start, _end = bounds
    if start is None:
        frequencies = (
            select(
                Order.customer_mercos_id.label("customer_id"),
                func.count(Order.id).label("order_count"),
            )
            .where(
                *_sale_conditions(filters, (None, None)),
                Order.customer_mercos_id.is_not(None),
            )
            .group_by(Order.customer_mercos_id)
            .subquery()
        )
        row = db.execute(
            select(
                func.coalesce(
                    func.sum(case((frequencies.c.order_count == 1, 1), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(case((frequencies.c.order_count > 1, 1), else_=0)),
                    0,
                ),
            ).select_from(frequencies)
        ).one()
        return int(row[0] or 0), int(row[1] or 0)

    earlier = exists(
        select(Order.id).where(
            Order.customer_mercos_id == current_customers.c.customer_id,
            *_sale_conditions(filters, (None, start)),
        )
    )
    row = db.execute(
        select(
            func.coalesce(func.sum(case((earlier, 0), else_=1)), 0),
            func.coalesce(func.sum(case((earlier, 1), else_=0)), 0),
        ).select_from(current_customers)
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


def overview(db: Session, filters: AnalyticsFilters) -> dict[str, Any]:
    current_bounds = date_bounds(filters)
    prior_bounds = previous_bounds(filters)
    current = _summary_for_bounds(db, filters, current_bounds)
    previous = (
        _summary_for_bounds(db, filters, prior_bounds)
        if prior_bounds[0] is not None
        else {key: ZERO for key in current}
    )
    current_new, current_recurring = _buyer_mix(db, filters, current_bounds)
    if prior_bounds[0] is not None:
        previous_new, previous_recurring = _buyer_mix(
            db,
            filters,
            prior_bounds,
        )
    else:
        previous_new, previous_recurring = 0, 0
    orders = current["orders"]
    items_per_order = Decimal(current["items"]) / orders if orders else ZERO
    previous_orders = previous["orders"]
    previous_items_per_order = (
        Decimal(previous["items"]) / previous_orders
        if previous_orders
        else ZERO
    )
    average_discount = (
        (current["discountTotal"] / current["grossRevenue"]) * 100
        if current["grossRevenue"]
        else ZERO
    )
    previous_average_discount = (
        (previous["discountTotal"] / previous["grossRevenue"]) * 100
        if previous["grossRevenue"]
        else ZERO
    )
    definitions = {
        "grossRevenue": (
            "Soma de quantidade × preço de tabela atual dos itens válidos. "
            "Itens excluídos e preços sentinela de R$ 1.000,00 não entram."
        ),
        "netRevenue": (
            "Mesma base do faturamento a preço de tabela atual. O valor "
            "histórico do pedido fica só para auditoria."
        ),
        "orders": "Quantidade de pedidos classificados como venda válida.",
        "averageTicket": "Faturamento a preço de tabela dividido pelos pedidos válidos.",
        "customers": "Clientes distintos com venda válida no período.",
        "newBuyers": "Clientes cuja primeira venda válida ocorreu no período.",
        "recurringBuyers": "Clientes do período cuja primeira venda válida ocorreu antes dele.",
        "cancellations": "Pedidos com status cancelado no período.",
        "cancellationRate": "Cancelamentos divididos por todos os pedidos filtrados.",
        "cancelledValue": "Soma a preço de tabela atual dos pedidos cancelados.",
        "discountTotal": (
            "Desconto analítico fica zerado porque o faturamento usa o preço "
            "de tabela atual, não o valor histórico do pedido."
        ),
        "averageDiscountPct": "Desconto total dividido pelo faturamento a preço de tabela.",
        "items": "Quantidade de linhas de item informada nos pedidos válidos.",
        "skus": "Soma dos SKUs distintos registrados por pedido válido.",
        "itemsPerOrder": "Quantidade de itens dividida pelos pedidos válidos.",
    }
    if current_bounds[0] is None:
        definitions["newBuyers"] = (
            "No histórico completo, clientes com exatamente uma venda válida."
        )
        definitions["recurringBuyers"] = (
            "No histórico completo, clientes com mais de uma venda válida."
        )
    values = {
        **current,
        "newBuyers": current_new,
        "recurringBuyers": current_recurring,
        "averageDiscountPct": average_discount,
        "itemsPerOrder": items_per_order,
    }
    prior_values = {
        **previous,
        "newBuyers": previous_new,
        "recurringBuyers": previous_recurring,
        "averageDiscountPct": previous_average_discount,
        "itemsPerOrder": previous_items_per_order,
    }
    negative_up = {"cancellations", "cancellationRate", "cancelledValue"}
    return {
        "kpis": {
            key: _kpi(
                value,
                prior_values[key],
                definitions[key],
                positive_when_up=key not in negative_up,
            )
            for key, value in values.items()
            if key in definitions
        },
        "appliedFilters": applied_filters(filters),
        "metadata": analytics_metadata(db),
    }


def _bucket_expression(db: Session, granularity: str):
    if db.bind is not None and db.bind.dialect.name == "sqlite":
        if granularity == "quarter":
            month = cast(func.strftime("%m", Order.issued_at), Integer)
            quarter = case(
                (month <= 3, "1"),
                (month <= 6, "2"),
                (month <= 9, "3"),
                else_="4",
            )
            return (
                func.strftime("%Y", Order.issued_at)
                + "-Q"
                + quarter
            )
        formats = {
            "day": "%Y-%m-%d",
            "week": "%Y-W%W",
            "month": "%Y-%m",
            "year": "%Y",
        }
        return func.strftime(formats[granularity], Order.issued_at)
    local_time = func.timezone("America/Sao_Paulo", Order.issued_at)
    return func.date_trunc(granularity, local_time)


def _bucket_key(value: object, granularity: str) -> str:
    if not isinstance(value, datetime):
        return str(value)
    if granularity == "day":
        return value.strftime("%Y-%m-%d")
    if granularity == "week":
        return value.strftime("%Y-W%W")
    if granularity == "month":
        return value.strftime("%Y-%m")
    if granularity == "quarter":
        return f"{value.year}-Q{((value.month - 1) // 3) + 1}"
    return value.strftime("%Y")


def _floor_bucket(value: datetime, granularity: str) -> datetime:
    if granularity == "day":
        return value.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "week":
        start = value - timedelta(days=value.weekday())
        return start.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "month":
        return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if granularity == "quarter":
        month = ((value.month - 1) // 3) * 3 + 1
        return value.replace(
            month=month,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    return value.replace(
        month=1,
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def _next_bucket(value: datetime, granularity: str) -> datetime:
    if granularity == "day":
        return value + timedelta(days=1)
    if granularity == "week":
        return value + timedelta(days=7)
    months = 1 if granularity == "month" else 3 if granularity == "quarter" else 12
    month_index = value.year * 12 + value.month - 1 + months
    return value.replace(year=month_index // 12, month=(month_index % 12) + 1)


def _dense_timeseries(
    items: list[dict[str, Any]],
    bounds: tuple[datetime | None, datetime | None],
    granularity: str,
) -> list[dict[str, Any]]:
    start, end = bounds
    if start is None or end is None:
        return items
    by_period = {str(item["period"]): item for item in items}
    cursor = _floor_bucket(start.astimezone(BR_TZ).replace(tzinfo=None), granularity)
    end_local = end.astimezone(BR_TZ).replace(tzinfo=None)
    dense = []
    while cursor < end_local:
        key = _bucket_key(cursor, granularity)
        dense.append(
            by_period.get(
                key,
                {
                    "period": key,
                    "revenue": ZERO,
                    "orders": 0,
                    "averageTicket": ZERO,
                    "customers": 0,
                    "items": 0,
                    "cancellations": 0,
                    "discounts": ZERO,
                },
            )
        )
        cursor = _next_bucket(cursor, granularity)
    return dense


def _timeseries_items(
    db: Session,
    filters: AnalyticsFilters,
    bounds: tuple[datetime | None, datetime | None],
) -> list[dict[str, Any]]:
    bucket = _bucket_expression(db, filters.granularity).label("bucket")
    common = order_conditions(filters, bounds=bounds)
    product_conditions = []
    if filters.productIds:
        product_conditions.append(Product.mercos_id.in_(filters.productIds))
    if filters.categoryIds:
        product_conditions.append(
            or_(
                Product.category_mercos_id.in_(filters.categoryIds),
                Product.category_id.in_(filters.categoryIds),
            )
        )
    rows = db.execute(
        select(
            bucket,
            func.count(func.distinct(Order.id)).label("orders"),
            func.coalesce(
                func.sum(_current_item_value_expression()),
                0,
            ).label("revenue"),
            func.count(func.distinct(Order.customer_mercos_id)).label("customers"),
            func.coalesce(func.sum(OrderItem.quantity), 0).label("items"),
            literal(ZERO).label("discounts"),
        )
        .select_from(Order)
        .join(OrderItem, OrderItem.order_mercos_id == Order.mercos_id)
        .join(Product, Product.mercos_id == OrderItem.product_mercos_id)
        .where(
            *common,
            *product_conditions,
            status_sql_in(Order.status, VALID_SALE_STATUSES),
            OrderItem.excluded.is_(False),
            _valid_list_price_expression().is_not(None),
        )
        .group_by(bucket)
        .order_by(bucket)
    ).all()
    cancellations = {
        _bucket_key(row.bucket, filters.granularity): int(row.cancellations)
        for row in db.execute(
            select(
                bucket,
                func.count(Order.id).label("cancellations"),
            )
            .where(
                *common,
                status_sql_in(Order.status, CANCELLED_ORDER_STATUSES),
            )
            .group_by(bucket)
        )
    }
    items = []
    for row in rows:
        orders = int(row.orders or 0)
        revenue = _decimal(row.revenue)
        key = _bucket_key(row.bucket, filters.granularity)
        items.append(
            {
                "period": key,
                "revenue": revenue,
                "orders": orders,
                "averageTicket": revenue / orders if orders else ZERO,
                "customers": int(row.customers or 0),
                "items": int(row.items or 0),
                "cancellations": cancellations.get(key, 0),
                "discounts": _decimal(row.discounts),
            }
        )
    return items


def timeseries(db: Session, filters: AnalyticsFilters) -> dict[str, Any]:
    current_bounds = date_bounds(filters)
    prior_bounds = previous_bounds(filters)
    current_items = _dense_timeseries(
        _timeseries_items(db, filters, current_bounds),
        current_bounds,
        filters.granularity,
    )
    previous_items = (
        _dense_timeseries(
            _timeseries_items(db, filters, prior_bounds),
            prior_bounds,
            filters.granularity,
        )
        if prior_bounds[0] is not None
        else []
    )
    return {
        "items": current_items,
        "previousItems": previous_items,
        "granularity": filters.granularity,
        "appliedFilters": applied_filters(filters),
        "metadata": analytics_metadata(db),
    }


ORDER_SORT = {
    "number": Order.number,
    "issued_at": Order.issued_at,
    "customer_name": Customer.name,
    "seller_name": Seller.name,
    "status": Order.status,
    "total": Order.total,
    "discount": Order.discount,
}


def orders_page(
    db: Session,
    filters: AnalyticsFilters,
    *,
    page: int,
    page_size: int,
    search: str | None,
    sort: str,
    order: str,
) -> dict[str, Any]:
    conditions = order_conditions(filters)
    order_values = _current_order_values(filters)
    if search:
        pattern = f"%{search.strip()}%"
        conditions.append(
            or_(
                Order.number.ilike(pattern),
                Customer.name.ilike(pattern),
                Seller.name.ilike(pattern),
                cast(Order.status, String).ilike(pattern),
            )
        )
    base = (
        select(
            Order,
            Customer.name.label("customer_name"),
            Customer.city.label("city"),
            Customer.state.label("state"),
            Seller.name.label("seller_name"),
            order_values.c.current_total,
        )
        .outerjoin(Customer, Customer.mercos_id == Order.customer_mercos_id)
        .outerjoin(Seller, Seller.mercos_id == Order.seller_mercos_id)
        .outerjoin(order_values, order_values.c.order_id == Order.mercos_id)
        .where(*conditions)
    )
    total = int(
        db.scalar(
            select(func.count())
            .select_from(Order)
            .outerjoin(Customer, Customer.mercos_id == Order.customer_mercos_id)
            .outerjoin(Seller, Seller.mercos_id == Order.seller_mercos_id)
            .where(*conditions)
        )
        or 0
    )
    sort_column = (
        order_values.c.current_total if sort == "total" else ORDER_SORT[sort]
    )
    ordering = asc(sort_column) if order == "asc" else desc(sort_column)
    rows = db.execute(
        base.order_by(ordering, desc(Order.id))
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    valid_summary = db.execute(
        select(
            func.count(Order.id),
            func.coalesce(func.sum(order_values.c.current_total), 0),
            func.coalesce(func.avg(order_values.c.current_total), 0),
            func.min(order_values.c.current_total),
            func.max(order_values.c.current_total),
        )
        .select_from(Order)
        .outerjoin(Customer, Customer.mercos_id == Order.customer_mercos_id)
        .outerjoin(Seller, Seller.mercos_id == Order.seller_mercos_id)
        .outerjoin(order_values, order_values.c.order_id == Order.mercos_id)
        .where(
            *conditions,
            status_sql_in(Order.status, VALID_SALE_STATUSES),
        )
    ).one()
    status_distribution = [
        {"status": str(status), "orders": int(count)}
        for status, count in db.execute(
            select(Order.status, func.count(Order.id))
            .select_from(Order)
            .outerjoin(Customer, Customer.mercos_id == Order.customer_mercos_id)
            .outerjoin(Seller, Seller.mercos_id == Order.seller_mercos_id)
            .where(*conditions)
            .group_by(Order.status)
            .order_by(desc(func.count(Order.id)))
        )
    ]
    value_band = case(
        (order_values.c.current_total < 100, "0–99"),
        (order_values.c.current_total < 500, "100–499"),
        (order_values.c.current_total < 1000, "500–999"),
        (order_values.c.current_total < 5000, "1.000–4.999"),
        else_="5.000+",
    )
    value_distribution = [
        {"band": band, "orders": int(count), "value": _decimal(value)}
        for band, count, value in db.execute(
            select(
                value_band.label("band"),
                func.count(Order.id),
                func.coalesce(func.sum(order_values.c.current_total), 0),
            )
            .select_from(Order)
            .outerjoin(Customer, Customer.mercos_id == Order.customer_mercos_id)
            .outerjoin(Seller, Seller.mercos_id == Order.seller_mercos_id)
            .join(order_values, order_values.c.order_id == Order.mercos_id)
            .where(
                *conditions,
                status_sql_in(Order.status, VALID_SALE_STATUSES),
            )
            .group_by(value_band)
            .order_by(func.min(order_values.c.current_total))
        )
    ]
    return {
        "items": [
            {
                "id": row.Order.mercos_id,
                "number": row.Order.number,
                "issuedAt": row.Order.issued_at,
                "customerId": row.Order.customer_mercos_id,
                "customerName": row.customer_name,
                "sellerId": row.Order.seller_mercos_id,
                "sellerName": row.seller_name,
                "status": row.Order.status,
                "grossTotal": _decimal(row.current_total),
                "netTotal": _decimal(row.current_total),
                "total": _decimal(row.current_total),
                "discount": ZERO,
                "discountPercent": ZERO,
                "itemCount": row.Order.item_count,
                "skuCount": row.Order.sku_count,
                "city": row.city,
                "state": row.state,
            }
            for row in rows
        ],
        "page": page,
        "pageSize": page_size,
        "totalItems": total,
        "totalPages": (total + page_size - 1) // page_size,
        "sort": sort,
        "order": order,
        "appliedFilters": applied_filters(filters),
        "metadata": analytics_metadata(db),
        "summary": {
            "validOrders": int(valid_summary[0] or 0),
            "orderValue": _decimal(valid_summary[1]),
            "averageOrderValue": _decimal(valid_summary[2]),
            "smallestOrderValue": _decimal(valid_summary[3]),
            "largestOrderValue": _decimal(valid_summary[4]),
            "statusDistribution": status_distribution,
            "valueDistribution": value_distribution,
        },
    }


def order_detail(
    db: Session,
    mercos_id: str,
    filters: AnalyticsFilters,
) -> dict[str, Any] | None:
    row = db.execute(
        select(
            Order,
            Customer.name.label("customer_name"),
            Customer.city,
            Customer.state,
            Seller.name.label("seller_name"),
        )
        .outerjoin(Customer, Customer.mercos_id == Order.customer_mercos_id)
        .outerjoin(Seller, Seller.mercos_id == Order.seller_mercos_id)
        .where(Order.mercos_id == mercos_id, *order_conditions(filters))
    ).one_or_none()
    if row is None:
        return None
    item_rows = db.execute(
        select(OrderItem)
        .where(
            OrderItem.order_mercos_id == mercos_id,
            OrderItem.excluded.is_(False),
        )
        .order_by(OrderItem.position)
    ).scalars().all()
    product_ids = [
        item.product_mercos_id
        for item in item_rows
        if item.product_mercos_id
    ]
    catalog = {
        product.mercos_id: product
        for product in (
            db.scalars(
                select(Product).where(Product.mercos_id.in_(product_ids))
            ).all()
            if product_ids
            else []
        )
    }
    order = row.Order

    def detail_item(item: OrderItem) -> dict[str, Any]:
        product = catalog.get(item.product_mercos_id or "")
        catalog_price = (
            None
            if product is None
            or _decimal(product.list_price) in PLACEHOLDER_LIST_PRICES
            else product.list_price
        )
        source_unit_price = _decimal(item.unit_price)
        current_unit_price = (
            _decimal(catalog_price) if catalog_price is not None else None
        )
        quantity = _decimal(item.quantity)
        return {
            "id": item.mercos_item_id,
            "position": item.position,
            "productId": item.product_mercos_id,
            "code": item.code,
            "name": item.name or (product.name if product is not None else ""),
            "quantity": quantity,
            "unitPrice": current_unit_price,
            "total": (
                quantity * current_unit_price
                if current_unit_price is not None
                else None
            ),
            "sourceUnitPrice": source_unit_price,
            "sourceTotal": item.total,
            "priceSource": (
                "catalog" if catalog_price is not None else "unavailable"
            ),
            "discount": item.discount,
        }

    detail_items = [detail_item(item) for item in item_rows]
    current_total = sum(
        (
            item["total"]
            for item in detail_items
            if item["total"] is not None
        ),
        ZERO,
    )
    return {
        "order": {
            "id": order.mercos_id,
            "number": order.number,
            "issuedAt": order.issued_at,
            "status": order.status,
            "customerId": order.customer_mercos_id,
            "customerName": row.customer_name,
            "sellerId": order.seller_mercos_id,
            "sellerName": row.seller_name,
            "city": row.city,
            "state": row.state,
            "grossTotal": current_total,
            "netTotal": current_total,
            "total": current_total,
            "discount": ZERO,
            "discountPercent": ZERO,
            "itemCount": order.item_count,
            "skuCount": order.sku_count,
            "orderTypeId": order.order_type_mercos_id,
            "paymentConditionId": order.payment_condition_mercos_id,
            "priceTableId": order.price_table_mercos_id,
            "carrierId": order.carrier_mercos_id,
            "commercialPolicyId": order.commercial_policy_mercos_id,
        },
        "items": detail_items,
        "appliedFilters": applied_filters(filters),
        "metadata": analytics_metadata(db),
    }


def _days_since(db: Session, column):
    if db.bind is not None and db.bind.dialect.name == "sqlite":
        return cast(
            func.julianday(func.current_timestamp()) - func.julianday(column),
            Integer,
        )
    return func.extract("day", func.now() - column)


def _product_aggregate(db: Session, filters: AnalyticsFilters):
    conditions = _sale_conditions(filters)
    product_conditions = []
    if filters.productIds:
        product_conditions.append(Product.mercos_id.in_(filters.productIds))
    if filters.categoryIds:
        product_conditions.append(
            or_(
                Product.category_mercos_id.in_(filters.categoryIds),
                Product.category_id.in_(filters.categoryIds),
            )
        )
    aggregate = (
        select(
            OrderItem.product_mercos_id.label("product_id"),
            func.coalesce(func.sum(OrderItem.quantity), 0).label("quantity_sold"),
            func.count(func.distinct(Order.mercos_id)).label("order_count"),
            func.coalesce(
                func.sum(_current_item_value_expression()),
                0,
            ).label("revenue"),
            (
                func.coalesce(func.sum(_current_item_value_expression()), 0)
                / func.nullif(func.sum(OrderItem.quantity), 0)
            ).label("average_price"),
            func.max(Order.issued_at).label("last_sale_at"),
        )
        .select_from(Order)
        .join(OrderItem, OrderItem.order_mercos_id == Order.mercos_id)
        .join(Product, Product.mercos_id == OrderItem.product_mercos_id)
        .where(
            *conditions,
            *product_conditions,
            OrderItem.excluded.is_(False),
            _valid_list_price_expression().is_not(None),
        )
        .group_by(OrderItem.product_mercos_id)
        .subquery()
    )
    total_revenue = func.sum(aggregate.c.revenue).over()
    cumulative_revenue = func.sum(aggregate.c.revenue).over(
        order_by=aggregate.c.revenue.desc()
    )
    return (
        select(
            aggregate,
            (
                (aggregate.c.revenue / func.nullif(total_revenue, 0)) * 100
            ).label("revenue_share"),
            (
                (cumulative_revenue / func.nullif(total_revenue, 0)) * 100
            ).label("cumulative_share"),
        )
        .subquery()
    )


PRODUCT_SORT_NAMES = {
    "code",
    "name",
    "quantity_sold",
    "order_count",
    "revenue",
    "average_price",
    "list_price",
    "stock",
    "stock_value",
    "last_sale_at",
    "days_without_sale",
}


def products_page(
    db: Session,
    filters: AnalyticsFilters,
    *,
    page: int,
    page_size: int,
    search: str | None,
    sort: str,
    order: str,
) -> dict[str, Any]:
    aggregate = _product_aggregate(db, filters)
    start, end = date_bounds(filters)
    effective_end = end or datetime.now(timezone.utc)
    period_days = (
        max((effective_end - start).total_seconds() / 86400, 1)
        if start is not None
        else None
    )
    valid_list_price = _valid_list_price_expression()
    stock_value = (Product.stock * valid_list_price).label("stock_value")
    days_without_sale = _days_since(db, aggregate.c.last_sale_at).label(
        "days_without_sale"
    )
    conditions = []
    if search:
        pattern = f"%{search.strip()}%"
        conditions.append(
            or_(Product.code.ilike(pattern), Product.name.ilike(pattern))
        )
    if filters.productIds:
        conditions.append(Product.mercos_id.in_(filters.productIds))
    if filters.categoryIds:
        conditions.append(
            or_(
                Product.category_mercos_id.in_(filters.categoryIds),
                Product.category_id.in_(filters.categoryIds),
            )
        )
    if filters.activeOnly:
        conditions.append(Product.active.is_(True))
    if any(
        (
            filters.sellerIds,
            filters.customerIds,
            filters.states,
            filters.cities,
            filters.segmentIds,
            filters.orderTypeIds,
            filters.paymentConditionIds,
        )
    ) or filters.minValue is not None or filters.maxValue is not None:
        conditions.append(aggregate.c.product_id.is_not(None))

    sort_columns = {
        "code": Product.code,
        "name": Product.name,
        "quantity_sold": func.coalesce(aggregate.c.quantity_sold, 0),
        "order_count": func.coalesce(aggregate.c.order_count, 0),
        "revenue": func.coalesce(aggregate.c.revenue, 0),
        "average_price": func.coalesce(aggregate.c.average_price, 0),
        "list_price": valid_list_price,
        "stock": Product.stock,
        "stock_value": stock_value,
        "last_sale_at": aggregate.c.last_sale_at,
        "days_without_sale": days_without_sale,
    }
    total = int(
        db.scalar(
            select(func.count(Product.id))
            .select_from(Product)
            .outerjoin(aggregate, aggregate.c.product_id == Product.mercos_id)
            .where(*conditions)
        )
        or 0
    )
    ordering = (
        asc(sort_columns[sort])
        if order == "asc"
        else desc(sort_columns[sort]).nulls_last()
    )
    rows = db.execute(
        select(
            Product,
            aggregate.c.quantity_sold,
            aggregate.c.order_count,
            aggregate.c.revenue,
            aggregate.c.average_price,
            aggregate.c.last_sale_at,
            aggregate.c.revenue_share,
            aggregate.c.cumulative_share,
            stock_value,
            days_without_sale,
        )
        .outerjoin(
            aggregate,
            aggregate.c.product_id == Product.mercos_id,
        )
        .where(*conditions)
        .order_by(ordering, Product.name)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    items = []
    for row in rows:
        quantity = _decimal(row.quantity_sold)
        revenue = _decimal(row.revenue)
        stock = _decimal(row.Product.stock)
        daily_velocity = (
            quantity / Decimal(str(period_days))
            if period_days and quantity > 0
            else ZERO
        )
        stock_coverage_days = (
            stock / daily_velocity
            if daily_velocity > 0 and stock > 0
            else None
        )
        last_sale = row.last_sale_at
        try:
            days = int(float(row.days_without_sale)) if row.days_without_sale is not None else None
        except (TypeError, ValueError):
            days = None
        items.append(
            {
                "id": row.Product.mercos_id,
                "code": row.Product.code,
                "name": row.Product.name,
                "categoryId": row.Product.category_mercos_id
                or row.Product.category_id,
                "active": row.Product.active,
                "quantitySold": quantity,
                "orderCount": int(row.order_count or 0),
                "revenue": revenue,
                "revenueShare": _decimal(row.revenue_share),
                "cumulativeRevenueShare": _decimal(row.cumulative_share),
                "abcClass": (
                    None
                    if last_sale is None
                    else "A"
                    if _decimal(row.cumulative_share) <= 80
                    else "B"
                    if _decimal(row.cumulative_share) <= 95
                    else "C"
                ),
                "averagePrice": _decimal(row.average_price),
                "listPrice": (
                    None
                    if _decimal(row.Product.list_price) in PLACEHOLDER_LIST_PRICES
                    else row.Product.list_price
                ),
                "minimumPrice": row.Product.minimum_price,
                "stock": stock,
                "stockValue": _decimal(row.stock_value),
                "averageDailyVelocity": daily_velocity,
                "estimatedCoverageDays": stock_coverage_days,
                "stockoutRisk": (
                    stock > 0
                    and stock_coverage_days is not None
                    and stock_coverage_days <= 30
                ),
                "excessStock": (
                    stock_coverage_days is not None
                    and stock_coverage_days >= 180
                ),
                "lastSaleAt": last_sale,
                "daysWithoutSale": days,
                "neverSold": last_sale is None,
                "classification": (
                    "sem_estoque"
                    if stock <= 0
                    else "risco_ruptura"
                    if stock_coverage_days is not None and stock_coverage_days <= 30
                    else "nunca_vendido"
                    if last_sale is None
                    else "estoque_parado"
                    if days is not None and days >= 90
                    else "excesso_estoque"
                    if stock_coverage_days is not None and stock_coverage_days >= 180
                    else "baixo_giro"
                    if quantity > 0
                    else "sem_venda_periodo"
                ),
            }
        )
    metadata = analytics_metadata(db)
    return {
        "items": items,
        "page": page,
        "pageSize": page_size,
        "totalItems": total,
        "totalPages": (total + page_size - 1) // page_size,
        "sort": sort,
        "order": order,
        "appliedFilters": applied_filters(filters),
        "metadata": metadata,
    }


def _customer_aggregate(filters: AnalyticsFilters):
    order_values = _header_order_values(
        filters,
        statuses=VALID_SALE_STATUSES,
    )
    aggregate = (
        select(
            order_values.c.customer_id,
            func.count(order_values.c.order_id).label("order_count"),
            func.coalesce(
                func.sum(order_values.c.current_total),
                0,
            ).label("revenue"),
            func.min(order_values.c.issued_at).label("first_order_at"),
            func.max(order_values.c.issued_at).label("last_order_at"),
        )
        .select_from(order_values)
        .group_by(order_values.c.customer_id)
        .subquery()
    )
    total_revenue = func.sum(aggregate.c.revenue).over()
    cumulative_revenue = func.sum(aggregate.c.revenue).over(
        order_by=aggregate.c.revenue.desc()
    )
    return (
        select(
            aggregate,
            (
                (aggregate.c.revenue / func.nullif(total_revenue, 0)) * 100
            ).label("revenue_share"),
            (
                (cumulative_revenue / func.nullif(total_revenue, 0)) * 100
            ).label("cumulative_share"),
            (
                6
                - func.ntile(5).over(
                    order_by=aggregate.c.last_order_at.desc()
                )
            ).label("recency_score"),
            (
                6
                - func.ntile(5).over(
                    order_by=aggregate.c.order_count.desc()
                )
            ).label("frequency_score"),
            (
                6
                - func.ntile(5).over(
                    order_by=aggregate.c.revenue.desc()
                )
            ).label("monetary_score"),
        )
        .subquery()
    )


CUSTOMER_SORT_NAMES = {
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
}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _period_months(
    filters: AnalyticsFilters,
    *,
    earliest: datetime | None = None,
) -> float:
    start, end = date_bounds(filters)
    effective_end = _aware(end) or datetime.now(timezone.utc)
    start = _aware(start) or _aware(earliest)
    if start is None:
        return 1.0
    days = max((effective_end - start).total_seconds() / 86400.0, 1.0)
    return days / 30.0


def _empty_customer_cohort() -> dict[str, Any]:
    return {
        "customerCount": 0,
        "orderCount": 0,
        "revenue": ZERO,
        "revenueSharePct": 0.0,
        "orderSharePct": 0.0,
        "averageMonthlyOrders": 0.0,
        "averageRevenuePerCustomer": ZERO,
        "averageOrderValue": ZERO,
    }


def _customer_cohort(rows, *, total_revenue: Decimal, total_orders: int, months: float):
    if not rows:
        return _empty_customer_cohort()
    revenue = sum((_decimal(row.revenue) for row in rows), ZERO)
    orders = sum(int(row.order_count or 0) for row in rows)
    count = len(rows)
    return {
        "customerCount": count,
        "orderCount": orders,
        "revenue": revenue,
        "revenueSharePct": round(
            float((revenue / total_revenue) * 100) if total_revenue else 0.0,
            2,
        ),
        "orderSharePct": round(
            (orders / total_orders) * 100 if total_orders else 0.0,
            2,
        ),
        "averageMonthlyOrders": round(
            orders / count / months if months else 0.0,
            2,
        ),
        "averageRevenuePerCustomer": (
            _decimal(revenue / count) if count else ZERO
        ),
        "averageOrderValue": _decimal(revenue / orders) if orders else ZERO,
    }


def customers_page(
    db: Session,
    filters: AnalyticsFilters,
    *,
    page: int,
    page_size: int,
    search: str | None,
    sort: str,
    order: str,
) -> dict[str, Any]:
    aggregate = _customer_aggregate(filters)
    average_ticket = (
        func.coalesce(aggregate.c.revenue, 0)
        / func.nullif(aggregate.c.order_count, 0)
    ).label("average_ticket")
    days_since = _days_since(db, aggregate.c.last_order_at).label("days_since")
    conditions = []
    if search:
        pattern = f"%{search.strip()}%"
        conditions.append(
            or_(
                Customer.name.ilike(pattern),
                Customer.document.ilike(pattern),
                Customer.city.ilike(pattern),
                Customer.state.ilike(pattern),
            )
        )
    if filters.customerIds:
        conditions.append(Customer.mercos_id.in_(filters.customerIds))
    if filters.states:
        conditions.append(Customer.state.in_(filters.states))
    if filters.cities:
        conditions.append(Customer.city.in_(filters.cities))
    if filters.segmentIds:
        conditions.append(Customer.segment_mercos_id.in_(filters.segmentIds))
    if filters.activeOnly:
        conditions.append(Customer.active.is_(True))
    if any(
        (
            filters.sellerIds,
            filters.productIds,
            filters.categoryIds,
            filters.orderTypeIds,
            filters.paymentConditionIds,
        )
    ) or filters.minValue is not None or filters.maxValue is not None:
        conditions.append(
            exists(
                select(Order.id).where(
                    Order.customer_mercos_id == Customer.mercos_id,
                    *_sale_conditions(filters),
                )
            )
        )

    sort_columns = {
        "name": Customer.name,
        "city": Customer.city,
        "state": Customer.state,
        "order_count": func.coalesce(aggregate.c.order_count, 0),
        "revenue": func.coalesce(aggregate.c.revenue, 0),
        "average_ticket": average_ticket,
        "first_order_at": aggregate.c.first_order_at,
        "last_order_at": aggregate.c.last_order_at,
        "days_since_last_order": days_since,
        "recency": days_since,
        "frequency": func.coalesce(aggregate.c.order_count, 0),
        "monetary": func.coalesce(aggregate.c.revenue, 0),
    }
    total = int(
        db.scalar(select(func.count(Customer.id)).where(*conditions)) or 0
    )
    ordering = (
        asc(sort_columns[sort])
        if order == "asc"
        else desc(sort_columns[sort]).nulls_last()
    )
    rows = db.execute(
        select(
            Customer,
            aggregate.c.order_count,
            aggregate.c.revenue,
            aggregate.c.first_order_at,
            aggregate.c.last_order_at,
            aggregate.c.recency_score,
            aggregate.c.frequency_score,
            aggregate.c.monetary_score,
            aggregate.c.revenue_share,
            aggregate.c.cumulative_share,
            average_ticket,
            days_since,
        )
        .outerjoin(
            aggregate,
            aggregate.c.customer_id == Customer.mercos_id,
        )
        .where(*conditions)
        .order_by(ordering, Customer.name)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    items = []
    for row in rows:
        try:
            days = int(float(row.days_since)) if row.days_since is not None else None
        except (TypeError, ValueError):
            days = None
        recency_score = int(row.recency_score or 0)
        frequency_score = int(row.frequency_score or 0)
        monetary_score = int(row.monetary_score or 0)
        rfm_total = recency_score + frequency_score + monetary_score
        average_interval_days = (
            (row.last_order_at - row.first_order_at).total_seconds()
            / 86400
            / (int(row.order_count) - 1)
            if row.first_order_at is not None
            and row.last_order_at is not None
            and int(row.order_count or 0) > 1
            else None
        )
        items.append(
            {
                "id": row.Customer.mercos_id,
                "name": row.Customer.name,
                "city": row.Customer.city,
                "state": row.Customer.state,
                "segmentId": row.Customer.segment_mercos_id,
                "active": row.Customer.active,
                "orderCount": int(row.order_count or 0),
                "revenue": _decimal(row.revenue),
                "revenueShare": _decimal(row.revenue_share),
                "cumulativeRevenueShare": _decimal(row.cumulative_share),
                "abcClass": (
                    None
                    if row.last_order_at is None
                    else "A"
                    if _decimal(row.cumulative_share) <= 80
                    else "B"
                    if _decimal(row.cumulative_share) <= 95
                    else "C"
                ),
                "averageTicket": _decimal(row.average_ticket),
                "firstOrderAt": row.first_order_at,
                "lastOrderAt": row.last_order_at,
                "daysSinceLastOrder": days,
                "averageOrderIntervalDays": average_interval_days,
                "recency": days,
                "frequency": int(row.order_count or 0),
                "monetary": _decimal(row.revenue),
                "rfm": {
                    "recency": recency_score,
                    "frequency": frequency_score,
                    "monetary": monetary_score,
                    "score": rfm_total,
                    "segment": (
                        "campeões"
                        if rfm_total >= 13
                        else "fiéis"
                        if rfm_total >= 10
                        else "em_risco"
                        if recency_score <= 2 and frequency_score >= 3
                        else "promissores"
                        if recency_score >= 4
                        else "regulares"
                    ),
                },
            }
        )
    ranked = db.execute(
        select(
            aggregate.c.revenue,
            aggregate.c.order_count,
            aggregate.c.first_order_at,
        )
        .where(func.coalesce(aggregate.c.order_count, 0) > 0)
        .order_by(aggregate.c.revenue.desc(), aggregate.c.order_count.desc())
    ).all()
    total_revenue = sum((_decimal(row.revenue) for row in ranked), ZERO)
    total_orders = sum(int(row.order_count or 0) for row in ranked)
    months = _period_months(
        filters,
        earliest=min(
            (row.first_order_at for row in ranked if row.first_order_at),
            default=None,
        ),
    )
    cohort_args = {
        "total_revenue": total_revenue,
        "total_orders": total_orders,
        "months": months,
    }
    top5 = _customer_cohort(ranked[:5], **cohort_args)
    top10 = _customer_cohort(ranked[:10], **cohort_args)
    top20 = _customer_cohort(ranked[:20], **cohort_args)
    ranks6to10 = _customer_cohort(ranked[5:10], **cohort_args)
    ranks11to20 = _customer_cohort(ranked[10:20], **cohort_args)
    rest = _customer_cohort(ranked[20:], **cohort_args)

    return {
        "items": items,
        "page": page,
        "pageSize": page_size,
        "totalItems": total,
        "totalPages": (total + page_size - 1) // page_size,
        "sort": sort,
        "order": order,
        "appliedFilters": applied_filters(filters),
        "metadata": analytics_metadata(db),
        "summary": {
            "periodMonths": round(months, 2),
            "totalRevenue": total_revenue,
            "concentrationTop5Pct": top5["revenueSharePct"],
            "concentrationTop10Pct": top10["revenueSharePct"],
            "concentrationTop20Pct": top20["revenueSharePct"],
            "concentrationRestPct": rest["revenueSharePct"],
            "top5": top5,
            "top10": top10,
            "top20": top20,
            "ranks6to10": ranks6to10,
            "ranks11to20": ranks11to20,
            "rest": rest,
        },
    }


SELLER_SORT_NAMES = {
    "name",
    "order_count",
    "revenue",
    "average_ticket",
    "customers",
    "new_customers",
    "cancellations",
    "discount_total",
}


def sellers_page(
    db: Session,
    filters: AnalyticsFilters,
    *,
    page: int,
    page_size: int,
    search: str | None,
    sort: str,
    order: str,
) -> dict[str, Any]:
    common = order_conditions(filters)
    order_values = _current_order_values(
        filters,
        statuses=VALID_SALE_STATUSES,
    )
    aggregate = (
        select(
            order_values.c.seller_id,
            func.count(order_values.c.order_id).label("order_count"),
            func.coalesce(
                func.sum(order_values.c.current_total),
                0,
            ).label("revenue"),
            func.count(func.distinct(order_values.c.customer_id)).label(
                "customers"
            ),
            literal(ZERO).label("discount_total"),
        )
        .select_from(order_values)
        .group_by(order_values.c.seller_id)
        .subquery()
    )
    cancelled = (
        select(
            Order.seller_mercos_id.label("seller_id"),
            func.count(Order.id).label("cancellations"),
        )
        .where(
            *common,
            status_sql_in(Order.status, CANCELLED_ORDER_STATUSES),
        )
        .group_by(Order.seller_mercos_id)
        .subquery()
    )
    first_order = (
        select(
            Order.customer_mercos_id.label("customer_id"),
            func.min(Order.issued_at).label("first_order_at"),
        )
        .where(
            status_sql_in(Order.status, VALID_SALE_STATUSES),
            Order.customer_mercos_id.is_not(None),
        )
        .group_by(Order.customer_mercos_id)
        .subquery()
    )
    new_customers = (
        select(
            Order.seller_mercos_id.label("seller_id"),
            func.count(func.distinct(Order.customer_mercos_id)).label(
                "new_customers"
            ),
        )
        .join(
            first_order,
            and_(
                first_order.c.customer_id == Order.customer_mercos_id,
                first_order.c.first_order_at == Order.issued_at,
            ),
        )
        .where(
            *common,
            status_sql_in(Order.status, VALID_SALE_STATUSES),
        )
        .group_by(Order.seller_mercos_id)
        .subquery()
    )
    average_ticket = (
        func.coalesce(aggregate.c.revenue, 0)
        / func.nullif(aggregate.c.order_count, 0)
    ).label("average_ticket")
    conditions = []
    if search:
        conditions.append(Seller.name.ilike(f"%{search.strip()}%"))
    if filters.sellerIds:
        conditions.append(Seller.mercos_id.in_(filters.sellerIds))
    if filters.activeOnly:
        conditions.append(Seller.active.is_(True))
    sort_columns = {
        "name": Seller.name,
        "order_count": func.coalesce(aggregate.c.order_count, 0),
        "revenue": func.coalesce(aggregate.c.revenue, 0),
        "average_ticket": average_ticket,
        "customers": func.coalesce(aggregate.c.customers, 0),
        "new_customers": func.coalesce(new_customers.c.new_customers, 0),
        "cancellations": func.coalesce(cancelled.c.cancellations, 0),
        "discount_total": func.coalesce(aggregate.c.discount_total, 0),
    }
    total = int(db.scalar(select(func.count(Seller.id)).where(*conditions)) or 0)
    ordering = (
        asc(sort_columns[sort])
        if order == "asc"
        else desc(sort_columns[sort]).nulls_last()
    )
    rows = db.execute(
        select(
            Seller,
            aggregate.c.order_count,
            aggregate.c.revenue,
            aggregate.c.customers,
            aggregate.c.discount_total,
            cancelled.c.cancellations,
            new_customers.c.new_customers,
            average_ticket,
        )
        .outerjoin(aggregate, aggregate.c.seller_id == Seller.mercos_id)
        .outerjoin(cancelled, cancelled.c.seller_id == Seller.mercos_id)
        .outerjoin(
            new_customers,
            new_customers.c.seller_id == Seller.mercos_id,
        )
        .where(*conditions)
        .order_by(ordering, Seller.name)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [
            {
                "id": row.Seller.mercos_id,
                "name": row.Seller.name,
                "active": row.Seller.active,
                "orderCount": int(row.order_count or 0),
                "revenue": _decimal(row.revenue),
                "averageTicket": _decimal(row.average_ticket),
                "customers": int(row.customers or 0),
                "newCustomers": int(row.new_customers or 0),
                "cancellations": int(row.cancellations or 0),
                "discountTotal": _decimal(row.discount_total),
            }
            for row in rows
        ],
        "page": page,
        "pageSize": page_size,
        "totalItems": total,
        "totalPages": (total + page_size - 1) // page_size,
        "sort": sort,
        "order": order,
        "appliedFilters": applied_filters(filters),
        "metadata": analytics_metadata(db),
    }


def inventory_page(
    db: Session,
    filters: AnalyticsFilters,
    *,
    page: int,
    page_size: int,
    search: str | None,
    sort: str,
    order: str,
) -> dict[str, Any]:
    result = products_page(
        db,
        filters,
        page=page,
        page_size=page_size,
        search=search,
        sort=sort,
        order=order,
    )
    aggregate = _product_aggregate(db, filters)
    conditions = []
    if filters.productIds:
        conditions.append(Product.mercos_id.in_(filters.productIds))
    if filters.categoryIds:
        conditions.append(
            or_(
                Product.category_mercos_id.in_(filters.categoryIds),
                Product.category_id.in_(filters.categoryIds),
            )
        )
    if filters.activeOnly:
        conditions.append(Product.active.is_(True))
    if any(
        (
            filters.sellerIds,
            filters.customerIds,
            filters.states,
            filters.cities,
            filters.segmentIds,
            filters.orderTypeIds,
            filters.paymentConditionIds,
        )
    ) or filters.minValue is not None or filters.maxValue is not None:
        conditions.append(aggregate.c.product_id.is_not(None))
    summary = db.execute(
        select(
            func.coalesce(
                func.sum(Product.stock * _valid_list_price_expression()),
                0,
            ),
            func.count(Product.id).filter(Product.stock > 0),
            func.count(Product.id).filter(Product.stock <= 0),
        )
        .select_from(Product)
        .outerjoin(aggregate, aggregate.c.product_id == Product.mercos_id)
        .where(*conditions)
    ).one()
    result["summary"] = {
        "stockValueAtListPrice": _decimal(summary[0]),
        "productsWithPositiveStock": int(summary[1] or 0),
        "productsWithoutStock": int(summary[2] or 0),
        "costValue": None,
        "costValueAvailability": "indisponível na fonte",
    }
    return result


def product_detail(
    db: Session,
    mercos_id: str,
    filters: AnalyticsFilters,
) -> dict[str, Any] | None:
    scoped = filters.model_copy(update={"productIds": [mercos_id]})
    product_page = products_page(
        db,
        scoped,
        page=1,
        page_size=1,
        search=None,
        sort="revenue",
        order="desc",
    )
    if not product_page["items"]:
        return None
    return {
        "product": product_page["items"][0],
        "recentOrders": orders_page(
            db,
            scoped,
            page=1,
            page_size=20,
            search=None,
            sort="issued_at",
            order="desc",
        ),
        "customers": customers_page(
            db,
            scoped,
            page=1,
            page_size=10,
            search=None,
            sort="revenue",
            order="desc",
        ),
        "associations": associations(db, scoped, limit=10),
        "appliedFilters": applied_filters(scoped),
        "metadata": analytics_metadata(db),
    }


def customer_detail(
    db: Session,
    mercos_id: str,
    filters: AnalyticsFilters,
) -> dict[str, Any] | None:
    scoped = filters.model_copy(update={"customerIds": [mercos_id]})
    customer_page = customers_page(
        db,
        scoped,
        page=1,
        page_size=1,
        search=None,
        sort="revenue",
        order="desc",
    )
    if not customer_page["items"]:
        return None
    return {
        "customer": customer_page["items"][0],
        "orders": orders_page(
            db,
            scoped,
            page=1,
            page_size=20,
            search=None,
            sort="issued_at",
            order="desc",
        ),
        "products": products_page(
            db,
            scoped,
            page=1,
            page_size=10,
            search=None,
            sort="revenue",
            order="desc",
        ),
        "appliedFilters": applied_filters(scoped),
        "metadata": analytics_metadata(db),
    }


def seller_detail(
    db: Session,
    mercos_id: str,
    filters: AnalyticsFilters,
) -> dict[str, Any] | None:
    scoped = filters.model_copy(update={"sellerIds": [mercos_id]})
    seller_page = sellers_page(
        db,
        scoped,
        page=1,
        page_size=1,
        search=None,
        sort="revenue",
        order="desc",
    )
    if not seller_page["items"]:
        return None
    return {
        "seller": seller_page["items"][0],
        "orders": orders_page(
            db,
            scoped,
            page=1,
            page_size=20,
            search=None,
            sort="issued_at",
            order="desc",
        ),
        "customers": customers_page(
            db,
            scoped,
            page=1,
            page_size=10,
            search=None,
            sort="revenue",
            order="desc",
        ),
        "products": products_page(
            db,
            scoped,
            page=1,
            page_size=10,
            search=None,
            sort="revenue",
            order="desc",
        ),
        "appliedFilters": applied_filters(scoped),
        "metadata": analytics_metadata(db),
    }


def breakdowns(db: Session, filters: AnalyticsFilters) -> dict[str, Any]:
    common = order_conditions(filters)
    status_rows = db.execute(
        select(
            Order.status,
            func.count(Order.id).label("orders"),
            func.coalesce(func.sum(_header_revenue_expression()), 0).label("value"),
        )
        .where(*common)
        .group_by(Order.status)
        .order_by(func.count(Order.id).desc())
    ).all()
    valid_values = _header_order_values(
        filters,
        statuses=VALID_SALE_STATUSES,
    )
    value_band = case(
        (valid_values.c.current_total < 500, "Até R$ 500"),
        (valid_values.c.current_total < 1000, "R$ 500–1.000"),
        (valid_values.c.current_total < 5000, "R$ 1.000–5.000"),
        else_="Acima de R$ 5.000",
    ).label("band")
    value_rows = db.execute(
        select(
            value_band,
            func.count(valid_values.c.order_id).label("orders"),
            func.coalesce(func.sum(valid_values.c.current_total), 0).label("value"),
        )
        .select_from(valid_values)
        .group_by(value_band)
    ).all()

    product_abc_rows = []
    try:
        product_aggregate = _product_aggregate(db, filters)
        product_class = case(
            (product_aggregate.c.cumulative_share <= 80, "A"),
            (product_aggregate.c.cumulative_share <= 95, "B"),
            else_="C",
        ).label("class_name")
        product_abc_rows = db.execute(
            select(
                product_class,
                func.count().label("entities"),
                func.coalesce(func.sum(product_aggregate.c.revenue), 0).label(
                    "revenue"
                ),
            )
            .select_from(product_aggregate)
            .group_by(product_class)
        ).all()
    except OperationalError:
        db.rollback()

    customer_aggregate = _customer_aggregate(filters)
    customer_class = case(
        (customer_aggregate.c.cumulative_share <= 80, "A"),
        (customer_aggregate.c.cumulative_share <= 95, "B"),
        else_="C",
    ).label("class_name")
    customer_abc_rows = db.execute(
        select(
            customer_class,
            func.count().label("entities"),
            func.coalesce(func.sum(customer_aggregate.c.revenue), 0).label("revenue"),
        )
        .select_from(customer_aggregate)
        .group_by(customer_class)
    ).all()

    return {
        "statuses": [
            {
                "status": row.status or "sem_status",
                "orders": int(row.orders or 0),
                "value": _decimal(row.value),
            }
            for row in status_rows
        ],
        "orderValueBands": [
            {
                "band": row.band,
                "orders": int(row.orders or 0),
                "value": _decimal(row.value),
            }
            for row in value_rows
        ],
        "productAbc": [
            {
                "class": row.class_name,
                "entities": int(row.entities or 0),
                "revenue": _decimal(row.revenue),
            }
            for row in product_abc_rows
        ],
        "customerAbc": [
            {
                "class": row.class_name,
                "entities": int(row.entities or 0),
                "revenue": _decimal(row.revenue),
            }
            for row in customer_abc_rows
        ],
        "appliedFilters": applied_filters(filters),
        "metadata": analytics_metadata(db),
    }


def _empty_ranking_page(
    filters: AnalyticsFilters,
    *,
    sort: str,
    page_size: int,
    metadata: dict[str, Any],
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "items": [],
        "page": 1,
        "pageSize": page_size,
        "totalItems": 0,
        "totalPages": 0,
        "sort": sort,
        "order": "desc",
        "appliedFilters": applied_filters(filters),
        "metadata": metadata,
        "summary": summary or {},
    }


def _ranking_customers(
    db: Session,
    filters: AnalyticsFilters,
    metadata: dict[str, Any],
    limit: int = 20,
) -> dict[str, Any]:
    conditions = _sale_conditions(filters)
    total_revenue = _decimal(
        db.scalar(
            select(
                func.coalesce(func.sum(_header_revenue_expression()), 0)
            ).where(*conditions)
        )
    )
    revenue_expr = func.coalesce(
        func.sum(_header_revenue_expression()),
        0,
    ).label("revenue")
    rows = db.execute(
        select(
            Customer.mercos_id,
            Customer.name,
            Customer.city,
            Customer.state,
            Customer.segment_mercos_id,
            Customer.active,
            func.count(Order.id).label("order_count"),
            revenue_expr,
        )
        .join(Order, Order.customer_mercos_id == Customer.mercos_id)
        .where(*conditions)
        .group_by(
            Customer.mercos_id,
            Customer.name,
            Customer.city,
            Customer.state,
            Customer.segment_mercos_id,
            Customer.active,
        )
        .order_by(desc(revenue_expr), Customer.name)
        .limit(limit)
    ).all()
    cumulative = ZERO
    items = []
    for row in rows:
        revenue = _decimal(row.revenue)
        orders = int(row.order_count or 0)
        cumulative += revenue
        share = float((revenue / total_revenue) * 100) if total_revenue else 0.0
        cumulative_share = (
            float((cumulative / total_revenue) * 100) if total_revenue else 0.0
        )
        items.append(
            {
                "id": row.mercos_id,
                "name": row.name,
                "city": row.city,
                "state": row.state,
                "segmentId": row.segment_mercos_id,
                "active": row.active,
                "orderCount": orders,
                "revenue": revenue,
                "revenueShare": share,
                "cumulativeRevenueShare": cumulative_share,
                "abcClass": (
                    "A"
                    if cumulative_share <= 80
                    else "B"
                    if cumulative_share <= 95
                    else "C"
                ),
                "averageTicket": revenue / orders if orders else ZERO,
                "firstOrderAt": None,
                "lastOrderAt": None,
                "daysSinceLastOrder": None,
                "averageOrderIntervalDays": None,
                "recency": None,
                "frequency": orders,
                "monetary": revenue,
                "rfm": {
                    "recency": 0,
                    "frequency": 0,
                    "monetary": 0,
                    "score": 0,
                    "segment": "regulares",
                },
            }
        )
    return {
        "items": items,
        "page": 1,
        "pageSize": limit,
        "totalItems": len(items),
        "totalPages": 1 if items else 0,
        "sort": "revenue",
        "order": "desc",
        "appliedFilters": applied_filters(filters),
        "metadata": metadata,
        "summary": {
            "concentrationTop5Pct": 0.0,
            "concentrationTop10Pct": 0.0,
            "concentrationTop20Pct": round(
                float((cumulative / total_revenue) * 100) if total_revenue else 0.0,
                2,
            ),
        },
    }


def _ranking_sellers(
    db: Session,
    filters: AnalyticsFilters,
    metadata: dict[str, Any],
    limit: int = 15,
) -> dict[str, Any]:
    conditions = _sale_conditions(filters)
    revenue_expr = func.coalesce(
        func.sum(_header_revenue_expression()),
        0,
    ).label("revenue")
    rows = db.execute(
        select(
            Seller.mercos_id,
            Seller.name,
            Seller.active,
            func.count(Order.id).label("order_count"),
            revenue_expr,
            func.count(func.distinct(Order.customer_mercos_id)).label("customers"),
        )
        .join(Order, Order.seller_mercos_id == Seller.mercos_id)
        .where(*conditions)
        .group_by(Seller.mercos_id, Seller.name, Seller.active)
        .order_by(desc(revenue_expr), Seller.name)
        .limit(limit)
    ).all()
    items = []
    for row in rows:
        revenue = _decimal(row.revenue)
        orders = int(row.order_count or 0)
        items.append(
            {
                "id": row.mercos_id,
                "name": row.name,
                "active": row.active,
                "orderCount": orders,
                "revenue": revenue,
                "averageTicket": revenue / orders if orders else ZERO,
                "customers": int(row.customers or 0),
                "newCustomers": None,
                "newCustomersAvailability": "indisponível neste recorte",
                "cancellations": 0,
                "discountTotal": ZERO,
            }
        )
    return {
        "items": items,
        "page": 1,
        "pageSize": limit,
        "totalItems": len(items),
        "totalPages": 1 if items else 0,
        "sort": "revenue",
        "order": "desc",
        "appliedFilters": applied_filters(filters),
        "metadata": metadata,
    }


def _ranking_products(
    db: Session,
    filters: AnalyticsFilters,
    metadata: dict[str, Any],
    limit: int = 20,
) -> dict[str, Any]:
    conditions = [
        *_sale_conditions(filters),
        OrderItem.excluded.is_(False),
        _valid_list_price_expression().is_not(None),
    ]
    revenue_expr = func.coalesce(
        func.sum(_current_item_value_expression()),
        0,
    ).label("revenue")
    rows = db.execute(
        select(
            Product.mercos_id,
            Product.name,
            Product.code,
            Product.list_price,
            func.coalesce(func.sum(OrderItem.quantity), 0).label("quantity_sold"),
            func.count(func.distinct(Order.mercos_id)).label("order_count"),
            revenue_expr,
        )
        .select_from(Order)
        .join(OrderItem, OrderItem.order_mercos_id == Order.mercos_id)
        .join(Product, Product.mercos_id == OrderItem.product_mercos_id)
        .where(*conditions)
        .group_by(
            Product.mercos_id,
            Product.name,
            Product.code,
            Product.list_price,
        )
        .order_by(desc(revenue_expr), Product.name)
        .limit(limit)
    ).all()
    total_revenue = sum((_decimal(row.revenue) for row in rows), ZERO)
    cumulative = ZERO
    items = []
    for row in rows:
        revenue = _decimal(row.revenue)
        cumulative += revenue
        list_price = (
            None
            if _decimal(row.list_price) in PLACEHOLDER_LIST_PRICES
            else row.list_price
        )
        items.append(
            {
                "id": row.mercos_id,
                "code": row.code,
                "name": row.name,
                "categoryId": None,
                "active": True,
                "quantitySold": _decimal(row.quantity_sold),
                "orderCount": int(row.order_count or 0),
                "revenue": revenue,
                "revenueShare": (
                    float((revenue / total_revenue) * 100) if total_revenue else 0.0
                ),
                "cumulativeRevenueShare": (
                    float((cumulative / total_revenue) * 100)
                    if total_revenue
                    else 0.0
                ),
                "abcClass": None,
                "averagePrice": (
                    revenue / _decimal(row.quantity_sold)
                    if row.quantity_sold
                    else ZERO
                ),
                "listPrice": list_price,
                "minimumPrice": None,
                "stock": ZERO,
                "stockValue": ZERO,
                "averageDailyVelocity": ZERO,
                "estimatedCoverageDays": None,
                "stockoutRisk": False,
                "excessStock": False,
                "lastSaleAt": None,
                "daysWithoutSale": None,
                "neverSold": False,
                "classification": "sem_venda_periodo",
            }
        )
    return {
        "items": items,
        "page": 1,
        "pageSize": limit,
        "totalItems": len(items),
        "totalPages": 1 if items else 0,
        "sort": "revenue",
        "order": "desc",
        "appliedFilters": applied_filters(filters),
        "metadata": metadata,
    }


def rankings(db: Session, filters: AnalyticsFilters) -> dict[str, Any]:
    metadata = analytics_metadata(db)

    def safe(builder, page_size: int, sort: str, summary: dict[str, Any] | None = None):
        try:
            return builder()
        except OperationalError:
            db.rollback()
            return _empty_ranking_page(
                filters,
                sort=sort,
                page_size=page_size,
                metadata=metadata,
                summary=summary,
            )

    return {
        "products": safe(
            lambda: _ranking_products(db, filters, metadata),
            20,
            "revenue",
        ),
        "customers": safe(
            lambda: _ranking_customers(db, filters, metadata),
            20,
            "revenue",
            {
                "concentrationTop5Pct": 0.0,
                "concentrationTop10Pct": 0.0,
                "concentrationTop20Pct": 0.0,
            },
        ),
        "sellers": safe(
            lambda: _ranking_sellers(db, filters, metadata),
            15,
            "revenue",
        ),
        "appliedFilters": applied_filters(filters),
        "metadata": metadata,
    }


def geography(db: Session, filters: AnalyticsFilters) -> dict[str, Any]:
    order_values = _current_order_values(
        filters,
        statuses=VALID_SALE_STATUSES,
    )
    states = db.execute(
        select(
            Customer.state,
            func.count(func.distinct(order_values.c.customer_id)).label("customers"),
            func.count(order_values.c.order_id).label("orders"),
            func.coalesce(
                func.sum(order_values.c.current_total),
                0,
            ).label("revenue"),
        )
        .select_from(order_values)
        .join(Customer, Customer.mercos_id == order_values.c.customer_id)
        .where(Customer.state.is_not(None))
        .group_by(Customer.state)
        .order_by(desc("revenue"))
    ).all()
    cities = db.execute(
        select(
            Customer.state,
            Customer.city,
            func.count(func.distinct(order_values.c.customer_id)).label("customers"),
            func.count(order_values.c.order_id).label("orders"),
            func.coalesce(
                func.sum(order_values.c.current_total),
                0,
            ).label("revenue"),
        )
        .select_from(order_values)
        .join(Customer, Customer.mercos_id == order_values.c.customer_id)
        .where(Customer.city.is_not(None))
        .group_by(Customer.state, Customer.city)
        .order_by(desc("revenue"))
    ).all()
    return {
        "states": [
            {
                "state": row.state,
                "customers": int(row.customers),
                "orders": int(row.orders),
                "revenue": _decimal(row.revenue),
            }
            for row in states
        ],
        "cities": [
            {
                "state": row.state,
                "city": row.city,
                "customers": int(row.customers),
                "orders": int(row.orders),
                "revenue": _decimal(row.revenue),
            }
            for row in cities
        ],
        "appliedFilters": applied_filters(filters),
        "metadata": analytics_metadata(db),
    }


def cohorts(db: Session, filters: AnalyticsFilters) -> dict[str, Any]:
    if db.bind is not None and db.bind.dialect.name == "sqlite":
        month = func.strftime("%Y-%m", Order.issued_at)
    else:
        month = func.to_char(
            func.date_trunc(
                "month",
                func.timezone("America/Sao_Paulo", Order.issued_at),
            ),
            "YYYY-MM",
        )
    rows = db.execute(
        select(Order.customer_mercos_id, month.label("month"))
        .where(
            *_sale_conditions(filters),
            Order.customer_mercos_id.is_not(None),
        )
        .distinct()
        .order_by(Order.customer_mercos_id, month)
    ).all()
    customer_months: dict[str, list[str]] = {}
    for row in rows:
        customer_months.setdefault(row.customer_mercos_id, []).append(str(row.month))
    matrix: dict[tuple[str, int], int] = {}
    cohort_sizes: dict[str, int] = {}
    for months in customer_months.values():
        if not months:
            continue
        cohort = months[0]
        cohort_sizes[cohort] = cohort_sizes.get(cohort, 0) + 1
        cohort_year, cohort_month = map(int, cohort.split("-"))
        for observed in months:
            year, month_number = map(int, observed.split("-"))
            offset = (year - cohort_year) * 12 + month_number - cohort_month
            matrix[(cohort, offset)] = matrix.get((cohort, offset), 0) + 1
    return {
        "cohorts": [
            {
                "cohort": cohort,
                "size": size,
                "retention": [
                    {
                        "monthOffset": offset,
                        "customers": matrix.get((cohort, offset), 0),
                        "rate": round(
                            (matrix.get((cohort, offset), 0) / size) * 100,
                            2,
                        ),
                    }
                    for offset in range(
                        max(
                            (
                                key_offset
                                for key_cohort, key_offset in matrix
                                if key_cohort == cohort
                            ),
                            default=0,
                        )
                        + 1
                    )
                ],
            }
            for cohort, size in sorted(cohort_sizes.items())
        ],
        "appliedFilters": applied_filters(filters),
        "metadata": analytics_metadata(db),
    }


def associations(
    db: Session,
    filters: AnalyticsFilters,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    left = aliased(OrderItem)
    right = aliased(OrderItem)
    rows = db.execute(
        select(
            left.product_mercos_id.label("product_a_id"),
            func.max(left.name).label("product_a_name"),
            right.product_mercos_id.label("product_b_id"),
            func.max(right.name).label("product_b_name"),
            func.count(func.distinct(left.order_mercos_id)).label("orders"),
        )
        .join(
            right,
            and_(
                right.order_mercos_id == left.order_mercos_id,
                right.product_mercos_id > left.product_mercos_id,
            ),
        )
        .join(Order, Order.mercos_id == left.order_mercos_id)
        .where(
            *_sale_conditions(filters),
            left.product_mercos_id.is_not(None),
            right.product_mercos_id.is_not(None),
            left.excluded.is_(False),
            right.excluded.is_(False),
        )
        .group_by(left.product_mercos_id, right.product_mercos_id)
        .order_by(desc("orders"))
        .limit(limit)
    ).all()
    return {
        "items": [
            {
                "productAId": row.product_a_id,
                "productAName": row.product_a_name,
                "productBId": row.product_b_id,
                "productBName": row.product_b_name,
                "ordersTogether": int(row.orders),
            }
            for row in rows
        ],
        "appliedFilters": applied_filters(filters),
        "metadata": analytics_metadata(db),
    }


FILTER_OPTION_MODELS = {
    "sellers": (Seller, Seller.mercos_id, Seller.name),
    "customers": (Customer, Customer.mercos_id, Customer.name),
    "products": (Product, Product.mercos_id, Product.name),
    "categories": (Category, Category.mercos_id, Category.name),
    "segments": (
        CustomerSegment,
        CustomerSegment.mercos_id,
        CustomerSegment.name,
    ),
    "order-types": (OrderType, OrderType.mercos_id, OrderType.name),
    "payment-conditions": (
        PaymentCondition,
        PaymentCondition.mercos_id,
        PaymentCondition.name,
    ),
}


def filter_options(
    db: Session,
    *,
    option: str,
    search: str | None,
    page: int,
    page_size: int,
    states: list[str] | None = None,
) -> dict[str, Any]:
    if option in {"states", "cities", "statuses"}:
        column = {
            "states": Customer.state,
            "cities": Customer.city,
            "statuses": Order.status,
        }[option]
        conditions = [column.is_not(None), func.trim(cast(column, String)) != ""]
        if search:
            conditions.append(cast(column, String).ilike(f"%{search.strip()}%"))
        if option == "cities" and states:
            conditions.append(Customer.state.in_(states))
        values = (
            select(column.label("value"))
            .where(*conditions)
            .distinct()
            .order_by(column)
            .subquery()
        )
        total = int(db.scalar(select(func.count()).select_from(values)) or 0)
        rows = db.execute(
            select(values.c.value)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        items = [
            {"id": str(row.value), "label": str(row.value)}
            for row in rows
        ]
    else:
        model, id_column, name_column = FILTER_OPTION_MODELS[option]
        conditions = []
        if search:
            conditions.append(name_column.ilike(f"%{search.strip()}%"))
        total = int(
            db.scalar(select(func.count(id_column)).where(*conditions)) or 0
        )
        rows = db.execute(
            select(id_column.label("id"), name_column.label("label"))
            .where(*conditions)
            .order_by(name_column)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        items = [{"id": row.id, "label": row.label} for row in rows]
    return {
        "items": items,
        "page": page,
        "pageSize": page_size,
        "totalItems": total,
        "totalPages": (total + page_size - 1) // page_size,
        "option": option,
    }
