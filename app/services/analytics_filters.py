from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import Query
from sqlalchemy import and_, exists, func, or_, select

from app.models import Customer, Order, OrderItem, Product
from app.schemas.analytics import AnalyticsFilters, Granularity, Period


BR_TZ = ZoneInfo("America/Sao_Paulo")
PERIOD_DAYS = {"7d": 7, "30d": 30, "90d": 90, "365d": 365}


def analytics_filters(
    date_from: date | None = Query(None, alias="dateFrom"),
    date_to: date | None = Query(None, alias="dateTo"),
    period: Period = Query("30d"),
    granularity: Granularity = Query("day"),
    statuses: list[str] | None = Query(None),
    seller_ids: list[str] | None = Query(None, alias="sellerIds"),
    customer_ids: list[str] | None = Query(None, alias="customerIds"),
    product_ids: list[str] | None = Query(None, alias="productIds"),
    category_ids: list[str] | None = Query(None, alias="categoryIds"),
    states: list[str] | None = Query(None),
    cities: list[str] | None = Query(None),
    segment_ids: list[str] | None = Query(None, alias="segmentIds"),
    order_type_ids: list[str] | None = Query(None, alias="orderTypeIds"),
    payment_condition_ids: list[str] | None = Query(
        None,
        alias="paymentConditionIds",
    ),
    min_value: Decimal | None = Query(None, alias="minValue"),
    max_value: Decimal | None = Query(None, alias="maxValue"),
    active_only: bool = Query(False, alias="activeOnly"),
) -> AnalyticsFilters:
    return AnalyticsFilters(
        dateFrom=date_from,
        dateTo=date_to,
        period=period,
        granularity=granularity,
        statuses=statuses or [],
        sellerIds=seller_ids or [],
        customerIds=customer_ids or [],
        productIds=product_ids or [],
        categoryIds=category_ids or [],
        states=states or [],
        cities=cities or [],
        segmentIds=segment_ids or [],
        orderTypeIds=order_type_ids or [],
        paymentConditionIds=payment_condition_ids or [],
        minValue=min_value,
        maxValue=max_value,
        activeOnly=active_only,
    )


def date_bounds(
    filters: AnalyticsFilters,
    *,
    now: datetime | None = None,
) -> tuple[datetime | None, datetime | None]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    start = None
    end = None
    if filters.dateFrom:
        start = datetime.combine(filters.dateFrom, time.min, BR_TZ).astimezone(
            timezone.utc
        )
    elif filters.period in PERIOD_DAYS:
        start = current - timedelta(days=PERIOD_DAYS[filters.period])
    elif filters.period == "ytd":
        local = current.astimezone(BR_TZ)
        start = datetime(local.year, 1, 1, tzinfo=BR_TZ).astimezone(timezone.utc)

    if filters.dateTo:
        end = datetime.combine(
            filters.dateTo + timedelta(days=1),
            time.min,
            BR_TZ,
        ).astimezone(timezone.utc)
    return start, end


def previous_bounds(
    filters: AnalyticsFilters,
    *,
    now: datetime | None = None,
) -> tuple[datetime | None, datetime | None]:
    start, end = date_bounds(filters, now=now)
    if start is None:
        return None, None
    effective_end = end or now or datetime.now(timezone.utc)
    duration = effective_end - start
    return start - duration, start


def order_conditions(
    filters: AnalyticsFilters,
    *,
    bounds: tuple[datetime | None, datetime | None] | None = None,
) -> list:
    conditions = []
    start, end = bounds if bounds is not None else date_bounds(filters)
    if start is not None:
        conditions.append(Order.issued_at >= start)
    if end is not None:
        conditions.append(Order.issued_at < end)
    if filters.statuses:
        normalized = tuple(status.strip().lower() for status in filters.statuses)
        conditions.append(func.lower(func.trim(Order.status)).in_(normalized))
    if filters.sellerIds:
        conditions.append(Order.seller_mercos_id.in_(filters.sellerIds))
    if filters.customerIds:
        conditions.append(Order.customer_mercos_id.in_(filters.customerIds))
    if filters.orderTypeIds:
        conditions.append(Order.order_type_mercos_id.in_(filters.orderTypeIds))
    if filters.paymentConditionIds:
        conditions.append(
            Order.payment_condition_mercos_id.in_(filters.paymentConditionIds)
        )
    if filters.minValue is not None:
        conditions.append(Order.total >= filters.minValue)
    if filters.maxValue is not None:
        conditions.append(Order.total <= filters.maxValue)

    customer_predicates = [Customer.mercos_id == Order.customer_mercos_id]
    if filters.states:
        customer_predicates.append(Customer.state.in_(filters.states))
    if filters.cities:
        customer_predicates.append(Customer.city.in_(filters.cities))
    if filters.segmentIds:
        customer_predicates.append(
            Customer.segment_mercos_id.in_(filters.segmentIds)
        )
    if filters.activeOnly:
        customer_predicates.append(Customer.active.is_(True))
    if len(customer_predicates) > 1:
        conditions.append(
            exists(select(Customer.id).where(and_(*customer_predicates)))
        )

    if filters.productIds or filters.categoryIds:
        product_predicates = [
            OrderItem.order_mercos_id == Order.mercos_id,
            Product.mercos_id == OrderItem.product_mercos_id,
        ]
        if filters.productIds:
            product_predicates.append(Product.mercos_id.in_(filters.productIds))
        if filters.categoryIds:
            product_predicates.append(
                or_(
                    Product.category_mercos_id.in_(filters.categoryIds),
                    Product.category_id.in_(filters.categoryIds),
                )
            )
        conditions.append(
            exists(
                select(OrderItem.id)
                .select_from(OrderItem)
                .join(Product, Product.mercos_id == OrderItem.product_mercos_id)
                .where(and_(*product_predicates))
            )
        )
    return conditions


def applied_filters(filters: AnalyticsFilters) -> dict:
    payload = filters.model_dump(exclude_none=True)
    for key in ("minValue", "maxValue"):
        if key in payload:
            payload[key] = float(payload[key])
    for key in ("dateFrom", "dateTo"):
        if key in payload:
            payload[key] = payload[key].isoformat()
    return payload
