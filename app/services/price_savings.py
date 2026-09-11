from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, defer

from app.models import Customer, Order, OrderItem
from app.schemas.analytics import AnalyticsFilters
from app.services.analytics_filters import (
    adjacent_previous_bounds,
    applied_filters,
    comparison_period,
    date_bounds,
)
from app.services.analytics_v2 import (
    PLACEHOLDER_LIST_PRICES,
    ZERO,
    _decimal,
    _sale_conditions,
    analytics_metadata,
)


PRODUCT_LIMIT = 50
ORDER_LIMIT = 200
CUSTOMER_LIMIT = 100
MAX_DROP_PCT = Decimal("40")
NO_COMPARISON_WARNING = (
    "Selecione um período ou datas para comparar com o recorte anterior, "
    "sem misturar o preço atual do Club."
)


@dataclass
class _Line:
    product_id: str
    name: str
    code: str | None
    quantity: Decimal
    total: Decimal
    list_quantity: Decimal = ZERO
    list_value: Decimal = ZERO
    discount: Decimal = ZERO

    @property
    def unit_price(self) -> Decimal:
        if self.quantity <= 0:
            return ZERO
        return (self.total / self.quantity).quantize(Decimal("0.0001"))

    @property
    def list_unit_price(self) -> Decimal:
        if self.list_quantity <= 0:
            return ZERO
        return (self.list_value / self.list_quantity).quantize(Decimal("0.0001"))


@dataclass
class _Basket:
    order: Order
    customer_id: str
    customer_name: str
    total: Decimal
    lines: list[_Line]


@dataclass
class _ProductAgg:
    name: str = ""
    code: str | None = None
    quantity: Decimal = ZERO
    value: Decimal = ZERO
    orders: set[str] = field(default_factory=set)
    order_count: int = 0

    @property
    def average_unit(self) -> Decimal:
        if self.quantity <= 0:
            return ZERO
        return (self.value / self.quantity).quantize(Decimal("0.01"))

    @property
    def n_orders(self) -> int:
        return self.order_count or len(self.orders)


def _pct(part: Decimal, whole: Decimal) -> float | None:
    if whole <= 0:
        return None
    return round(float((part / whole) * 100), 2)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _order_total(order: Order) -> Decimal:
    if order.net_total is not None:
        return _decimal(order.net_total)
    return _decimal(order.total)


def _item_unit_price(item: OrderItem) -> Decimal:
    unit = _decimal(item.unit_price)
    if unit > 0:
        return unit
    quantity = _decimal(item.quantity)
    if quantity > 0:
        return (_decimal(item.total) / quantity).quantize(Decimal("0.0001"))
    return ZERO


def _item_line_total(item: OrderItem) -> Decimal:
    total = _decimal(item.total)
    if total > 0:
        return total
    return (_decimal(item.quantity) * _item_unit_price(item)).quantize(Decimal("0.01"))


def _item_line_total_sql():
    return case(
        (OrderItem.total > 0, OrderItem.total),
        else_=func.coalesce(OrderItem.quantity * OrderItem.unit_price, 0),
    )


def _load_orders(
    db: Session,
    filters: AnalyticsFilters,
    bounds: tuple[datetime | None, datetime | None],
) -> list[tuple[Order, str | None]]:
    return list(
        db.execute(
            select(Order, Customer.name)
            .options(defer(Order.raw))
            .outerjoin(Customer, Customer.mercos_id == Order.customer_mercos_id)
            .where(*_sale_conditions(filters, bounds))
            .order_by(Order.issued_at.desc(), Order.number.desc())
        ).all()
    )


def _load_items(
    db: Session,
    filters: AnalyticsFilters,
    bounds: tuple[datetime | None, datetime | None],
) -> dict[str, list[OrderItem]]:
    grouped: dict[str, list[OrderItem]] = defaultdict(list)
    items = db.scalars(
        select(OrderItem)
        .options(defer(OrderItem.raw))
        .join(Order, Order.mercos_id == OrderItem.order_mercos_id)
        .where(
            *_sale_conditions(filters, bounds),
            OrderItem.excluded.is_(False),
        )
        .execution_options(yield_per=1000)
    ).all()
    for item in items:
        grouped[item.order_mercos_id].append(item)
    return grouped


def _product_agg_from_db(
    db: Session,
    filters: AnalyticsFilters,
    bounds: tuple[datetime | None, datetime | None],
) -> dict[str, _ProductAgg]:
    aggregated: dict[str, _ProductAgg] = {}
    rows = db.execute(
        select(
            OrderItem.product_mercos_id,
            func.max(OrderItem.name),
            func.max(OrderItem.code),
            func.coalesce(func.sum(OrderItem.quantity), 0),
            func.coalesce(func.sum(_item_line_total_sql()), 0),
            func.count(func.distinct(OrderItem.order_mercos_id)),
        )
        .select_from(OrderItem)
        .join(Order, Order.mercos_id == OrderItem.order_mercos_id)
        .where(
            *_sale_conditions(filters, bounds),
            OrderItem.excluded.is_(False),
            OrderItem.product_mercos_id.isnot(None),
            OrderItem.quantity > 0,
        )
        .group_by(OrderItem.product_mercos_id)
    ).all()
    for product_id, name, code, quantity, value, order_count in rows:
        if not product_id:
            continue
        aggregated[str(product_id)] = _ProductAgg(
            name=name or str(product_id),
            code=code,
            quantity=_decimal(quantity),
            value=_decimal(value),
            order_count=int(order_count or 0),
        )
    return aggregated


def _aggregate_lines(items: list[OrderItem]) -> list[_Line]:
    buckets: dict[str, _Line] = {}
    for item in items:
        product_id = item.product_mercos_id
        if not product_id:
            continue
        quantity = _decimal(item.quantity)
        if quantity <= 0:
            continue
        current = buckets.get(product_id)
        list_unit = _decimal(item.list_unit_price)
        if current is None:
            current = _Line(
                product_id=product_id,
                name=item.name or product_id,
                code=item.code,
                quantity=quantity,
                total=_item_line_total(item),
                discount=_decimal(item.discount),
            )
            buckets[product_id] = current
        else:
            current.quantity += quantity
            current.total += _item_line_total(item)
            current.discount += _decimal(item.discount)
            if item.name and not current.name:
                current.name = item.name
            if item.code and not current.code:
                current.code = item.code
        if list_unit > 0:
            current.list_quantity += quantity
            current.list_value += list_unit * quantity
    return list(buckets.values())


def _baskets(
    rows: list[tuple[Order, str | None]],
    items_by_order: dict[str, list[OrderItem]],
) -> list[_Basket]:
    baskets: list[_Basket] = []
    for order, customer_name in rows:
        lines = _aggregate_lines(items_by_order.get(order.mercos_id, []))
        if not lines:
            continue
        baskets.append(
            _Basket(
                order=order,
                customer_id=order.customer_mercos_id or "",
                customer_name=customer_name or order.customer_mercos_id or order.number,
                total=_order_total(order),
                lines=lines,
            )
        )
    return baskets


def _product_agg(baskets: list[_Basket]) -> dict[str, _ProductAgg]:
    aggregated: dict[str, _ProductAgg] = {}
    for basket in baskets:
        for line in basket.lines:
            stats = aggregated.setdefault(line.product_id, _ProductAgg())
            stats.name = line.name or stats.name or line.product_id
            stats.code = line.code or stats.code
            stats.quantity += line.quantity
            stats.value += line.total
            stats.orders.add(basket.order.mercos_id)
    return aggregated


def _is_placeholder_unit(unit: Decimal) -> bool:
    return unit.quantize(Decimal("0.01")) in PLACEHOLDER_LIST_PRICES


def _plausible_before(before: Decimal, net: Decimal) -> Decimal:
    if before <= 0 or net <= 0 or before <= net:
        return ZERO
    if _is_placeholder_unit(before):
        return ZERO
    drop_pct = (before - net) / before * 100
    if drop_pct > MAX_DROP_PCT:
        return ZERO
    return before


def _before_unit(
    line: _Line,
    previous_products: dict[str, _ProductAgg],
) -> Decimal:
    net = line.unit_price
    previous = previous_products.get(line.product_id)
    if previous is None:
        return ZERO
    return _plausible_before(previous.average_unit, net)


def _line_savings(
    line: _Line,
    previous_products: dict[str, _ProductAgg],
) -> Decimal:
    net = line.unit_price
    before = _before_unit(line, previous_products)
    if before > net and line.quantity > 0:
        return (line.quantity * (before - net)).quantize(Decimal("0.01"))
    return ZERO


def _dropped_line_totals(
    line: _Line,
    previous_products: dict[str, _ProductAgg],
) -> tuple[Decimal, Decimal, Decimal] | None:
    savings = _line_savings(line, previous_products)
    if savings <= 0:
        return None
    after = line.total.quantize(Decimal("0.01"))
    before = (after + savings).quantize(Decimal("0.01"))
    return before, after, savings


def price_savings(db: Session, filters: AnalyticsFilters) -> dict[str, Any]:
    metadata = analytics_metadata(db)
    comparison = comparison_period(filters, adjacent=True)
    current_bounds = date_bounds(filters)
    prior_bounds = adjacent_previous_bounds(filters)
    warnings = list(metadata["warnings"])

    current_rows = _load_orders(db, filters, current_bounds)
    if prior_bounds[0] is None:
        warnings.append(NO_COMPARISON_WARNING)
        previous_products: dict[str, _ProductAgg] = {}
    else:
        previous_products = _product_agg_from_db(db, filters, prior_bounds)
    metadata = {**metadata, "warnings": warnings}

    items_by_order = _load_items(db, filters, current_bounds)
    current_baskets = _baskets(current_rows, items_by_order)
    current_products = _product_agg(current_baskets)

    dropped_products: list[dict[str, Any]] = []
    product_savings = ZERO
    previous_implied = ZERO
    orders_with_drop: set[str] = set()
    customers_with_savings: set[str] = set()
    dropped_ids: set[str] = set()

    for product_id, current_stats in current_products.items():
        previous_stats = previous_products.get(product_id)
        if previous_stats is None or previous_stats.average_unit <= 0:
            continue
        if current_stats.average_unit >= previous_stats.average_unit:
            continue
        if _plausible_before(
            previous_stats.average_unit,
            current_stats.average_unit,
        ) <= 0:
            continue
        unit_drop = previous_stats.average_unit - current_stats.average_unit
        savings = (current_stats.quantity * unit_drop).quantize(Decimal("0.01"))
        if savings <= 0:
            continue
        dropped_ids.add(product_id)
        product_savings += savings
        previous_implied += (
            current_stats.quantity * previous_stats.average_unit
        ).quantize(Decimal("0.01"))
        dropped_products.append(
            {
                "id": product_id,
                "code": current_stats.code or previous_stats.code,
                "name": current_stats.name or previous_stats.name or product_id,
                "previousAverageUnit": previous_stats.average_unit,
                "currentAverageUnit": current_stats.average_unit,
                "unitDrop": unit_drop.quantize(Decimal("0.01")),
                "dropPct": _pct(unit_drop, previous_stats.average_unit),
                "quantitySold": current_stats.quantity,
                "currentRevenue": current_stats.value.quantize(Decimal("0.01")),
                "savings": savings,
                "currentOrders": current_stats.n_orders,
                "previousOrders": previous_stats.n_orders,
            }
        )

    dropped_products.sort(key=lambda row: row["savings"], reverse=True)
    dropped_product_count = len(dropped_products)
    dropped_products = dropped_products[:PRODUCT_LIMIT]

    for basket in current_baskets:
        if any(line.product_id in dropped_ids for line in basket.lines):
            orders_with_drop.add(basket.order.mercos_id)
            customers_with_savings.add(basket.customer_id)

    discounted_orders: list[dict[str, Any]] = []
    club_savings = ZERO
    before_club_total = ZERO
    club_order_count = 0
    customer_rollups: dict[str, dict[str, Any]] = {}

    for basket in current_baskets:
        item_savings = ZERO
        before_dropped = ZERO
        after_dropped = ZERO
        discounted_skus = 0
        for line in basket.lines:
            dropped = _dropped_line_totals(line, previous_products)
            if dropped is None:
                continue
            before, after, savings = dropped
            before_dropped += before
            after_dropped += after
            item_savings += savings
            discounted_skus += 1
        if item_savings <= 0:
            continue
        club_savings += item_savings
        before_club_total += before_dropped
        if basket.customer_id:
            customers_with_savings.add(basket.customer_id)
        club_order_count += 1
        discounted_orders.append(
            {
                "customerId": basket.customer_id,
                "customerName": basket.customer_name,
                "currentOrderId": basket.order.mercos_id,
                "currentNumber": basket.order.number,
                "currentIssuedAt": _iso(basket.order.issued_at),
                "currentTotal": after_dropped,
                "previousTotal": before_dropped,
                "savings": item_savings,
                "savingsPct": _pct(item_savings, before_dropped),
                "itemSavings": item_savings,
                "skuCount": discounted_skus,
            }
        )
        rollup = customer_rollups.setdefault(
            basket.customer_id or basket.order.mercos_id,
            {
                "id": basket.customer_id,
                "name": basket.customer_name,
                "matchedOrders": 0,
                "previousTotal": ZERO,
                "currentTotal": ZERO,
                "savings": ZERO,
            },
        )
        rollup["matchedOrders"] += 1
        rollup["previousTotal"] += before_dropped
        rollup["currentTotal"] += after_dropped
        rollup["savings"] += item_savings

    discounted_orders.sort(key=lambda row: row["savings"], reverse=True)
    discounted_orders = discounted_orders[:ORDER_LIMIT]
    customers = []
    for rollup in customer_rollups.values():
        customers.append(
            {
                **rollup,
                "previousTotal": rollup["previousTotal"].quantize(Decimal("0.01")),
                "currentTotal": rollup["currentTotal"].quantize(Decimal("0.01")),
                "savings": rollup["savings"].quantize(Decimal("0.01")),
                "savingsPct": _pct(rollup["savings"], rollup["previousTotal"]),
            }
        )
    customers.sort(key=lambda row: row["savings"], reverse=True)
    customers = customers[:CUSTOMER_LIMIT]

    summary = {
        "droppedProductCount": dropped_product_count,
        "currentOrdersWithDroppedProducts": len(orders_with_drop),
        "productSavings": product_savings,
        "productSavingsPct": _pct(product_savings, previous_implied),
        "matchedPairCount": club_order_count,
        "matchedSavings": club_savings,
        "matchedSavingsPct": _pct(club_savings, before_club_total),
        "customersWithSavings": len(customers_with_savings),
    }
    return {
        "summary": summary,
        "products": dropped_products,
        "matchedOrders": discounted_orders,
        "customers": customers,
        "comparison": comparison,
        "appliedFilters": applied_filters(filters),
        "metadata": metadata,
    }
