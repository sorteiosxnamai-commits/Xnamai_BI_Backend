from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Customer, Order, OrderItem
from app.schemas.analytics import AnalyticsFilters
from app.services.analytics_filters import (
    applied_filters,
    comparison_period,
    date_bounds,
    previous_bounds,
)
from app.services.analytics_v2 import (
    ZERO,
    _decimal,
    _sale_conditions,
    analytics_metadata,
)


PRODUCT_LIMIT = 50
PAIR_LIMIT = 100
CUSTOMER_LIMIT = 50
NO_COMPARISON_WARNING = (
    "Selecione um período ou datas para comparar com o mesmo recorte do mês anterior."
)


@dataclass
class _Line:
    product_id: str
    name: str
    code: str | None
    quantity: Decimal
    total: Decimal

    @property
    def unit_price(self) -> Decimal:
        if self.quantity <= 0:
            return ZERO
        return (self.total / self.quantity).quantize(Decimal("0.0001"))


@dataclass
class _Basket:
    order: Order
    customer_id: str
    customer_name: str
    signature: str
    total: Decimal
    lines: list[_Line]


@dataclass
class _ProductAgg:
    name: str = ""
    code: str | None = None
    quantity: Decimal = ZERO
    value: Decimal = ZERO
    orders: set[str] = field(default_factory=set)

    @property
    def average_unit(self) -> Decimal:
        if self.quantity <= 0:
            return ZERO
        return (self.value / self.quantity).quantize(Decimal("0.01"))


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


def _qty_token(quantity: Decimal) -> str:
    quantized = _decimal(quantity).quantize(Decimal("0.0001")).normalize()
    return format(quantized, "f")


def _signature(lines: list[_Line]) -> str:
    parts = [
        f"{line.product_id}:{_qty_token(line.quantity)}"
        for line in sorted(lines, key=lambda item: item.product_id)
        if line.product_id and line.quantity > 0
    ]
    return "|".join(parts)


def _empty_summary() -> dict[str, Any]:
    return {
        "droppedProductCount": 0,
        "currentOrdersWithDroppedProducts": 0,
        "productSavings": ZERO,
        "productSavingsPct": None,
        "matchedPairCount": 0,
        "matchedSavings": ZERO,
        "matchedSavingsPct": None,
        "customersWithSavings": 0,
    }


def _load_orders(
    db: Session,
    filters: AnalyticsFilters,
    bounds: tuple[datetime | None, datetime | None],
) -> list[tuple[Order, str | None]]:
    return list(
        db.execute(
            select(Order, Customer.name)
            .outerjoin(Customer, Customer.mercos_id == Order.customer_mercos_id)
            .where(*_sale_conditions(filters, bounds))
            .order_by(Order.issued_at.desc(), Order.number.desc())
        ).all()
    )


def _load_items(db: Session, order_ids: list[str]) -> dict[str, list[OrderItem]]:
    grouped: dict[str, list[OrderItem]] = defaultdict(list)
    if not order_ids:
        return grouped
    items = db.scalars(
        select(OrderItem).where(
            OrderItem.order_mercos_id.in_(order_ids),
            OrderItem.excluded.is_(False),
        )
    ).all()
    for item in items:
        grouped[item.order_mercos_id].append(item)
    return grouped


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
        if current is None:
            buckets[product_id] = _Line(
                product_id=product_id,
                name=item.name or product_id,
                code=item.code,
                quantity=quantity,
                total=_item_line_total(item),
            )
            continue
        current.quantity += quantity
        current.total += _item_line_total(item)
        if item.name and not current.name:
            current.name = item.name
        if item.code and not current.code:
            current.code = item.code
    return list(buckets.values())


def _baskets(
    rows: list[tuple[Order, str | None]],
    items_by_order: dict[str, list[OrderItem]],
) -> list[_Basket]:
    baskets: list[_Basket] = []
    for order, customer_name in rows:
        lines = _aggregate_lines(items_by_order.get(order.mercos_id, []))
        signature = _signature(lines)
        if not signature or not order.customer_mercos_id:
            continue
        baskets.append(
            _Basket(
                order=order,
                customer_id=order.customer_mercos_id,
                customer_name=customer_name or order.customer_mercos_id,
                signature=signature,
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


def _pair_baskets(
    current: list[_Basket],
    previous: list[_Basket],
) -> list[tuple[_Basket, _Basket]]:
    previous_by_key: dict[tuple[str, str], list[_Basket]] = defaultdict(list)
    for basket in previous:
        previous_by_key[(basket.customer_id, basket.signature)].append(basket)
    for group in previous_by_key.values():
        group.sort(
            key=lambda item: (
                item.order.issued_at or datetime.min.replace(tzinfo=timezone.utc),
                item.order.number,
            ),
            reverse=True,
        )

    current_by_key: dict[tuple[str, str], list[_Basket]] = defaultdict(list)
    for basket in current:
        current_by_key[(basket.customer_id, basket.signature)].append(basket)

    pairs: list[tuple[_Basket, _Basket]] = []
    for key, current_group in current_by_key.items():
        previous_group = previous_by_key.get(key, [])
        if not previous_group:
            continue
        current_group.sort(
            key=lambda item: (
                item.order.issued_at or datetime.min.replace(tzinfo=timezone.utc),
                item.order.number,
            ),
            reverse=True,
        )
        for current_basket, previous_basket in zip(current_group, previous_group):
            pairs.append((current_basket, previous_basket))
    pairs.sort(
        key=lambda item: item[1].total - item[0].total,
        reverse=True,
    )
    return pairs


def price_savings(db: Session, filters: AnalyticsFilters) -> dict[str, Any]:
    metadata = analytics_metadata(db)
    comparison = comparison_period(filters)
    current_bounds = date_bounds(filters)
    prior_bounds = previous_bounds(filters)

    if prior_bounds[0] is None:
        warnings = list(metadata["warnings"])
        warnings.append(NO_COMPARISON_WARNING)
        metadata = {**metadata, "warnings": warnings}
        return {
            "summary": _empty_summary(),
            "products": [],
            "matchedOrders": [],
            "customers": [],
            "comparison": comparison,
            "appliedFilters": applied_filters(filters),
            "metadata": metadata,
        }

    current_rows = _load_orders(db, filters, current_bounds)
    previous_rows = _load_orders(db, filters, prior_bounds)
    order_ids = [order.mercos_id for order, _ in current_rows] + [
        order.mercos_id for order, _ in previous_rows
    ]
    items_by_order = _load_items(db, order_ids)
    current_baskets = _baskets(current_rows, items_by_order)
    previous_baskets = _baskets(previous_rows, items_by_order)
    current_products = _product_agg(current_baskets)
    previous_products = _product_agg(previous_baskets)

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
                "currentOrders": len(current_stats.orders),
                "previousOrders": len(previous_stats.orders),
            }
        )

    dropped_products.sort(key=lambda row: row["savings"], reverse=True)
    dropped_products = dropped_products[:PRODUCT_LIMIT]

    for basket in current_baskets:
        if any(line.product_id in dropped_ids for line in basket.lines):
            orders_with_drop.add(basket.order.mercos_id)
            customers_with_savings.add(basket.customer_id)

    matched_orders: list[dict[str, Any]] = []
    matched_savings = ZERO
    previous_matched_total = ZERO
    customer_rollups: dict[str, dict[str, Any]] = {}

    for current_basket, previous_basket in _pair_baskets(
        current_baskets,
        previous_baskets,
    ):
        savings = (previous_basket.total - current_basket.total).quantize(
            Decimal("0.01")
        )
        if savings <= 0:
            continue
        matched_savings += savings
        previous_matched_total += previous_basket.total
        customers_with_savings.add(current_basket.customer_id)
        item_savings = ZERO
        for line in current_basket.lines:
            previous_avg = previous_products.get(line.product_id)
            if previous_avg is None or previous_avg.average_unit <= line.unit_price:
                continue
            item_savings += (
                line.quantity * (previous_avg.average_unit - line.unit_price)
            ).quantize(Decimal("0.01"))
        matched_orders.append(
            {
                "customerId": current_basket.customer_id,
                "customerName": current_basket.customer_name,
                "currentOrderId": current_basket.order.mercos_id,
                "currentNumber": current_basket.order.number,
                "currentIssuedAt": _iso(current_basket.order.issued_at),
                "currentTotal": current_basket.total,
                "previousOrderId": previous_basket.order.mercos_id,
                "previousNumber": previous_basket.order.number,
                "previousIssuedAt": _iso(previous_basket.order.issued_at),
                "previousTotal": previous_basket.total,
                "savings": savings,
                "savingsPct": _pct(savings, previous_basket.total),
                "itemSavings": item_savings,
                "skuCount": len(current_basket.lines),
            }
        )
        rollup = customer_rollups.setdefault(
            current_basket.customer_id,
            {
                "id": current_basket.customer_id,
                "name": current_basket.customer_name,
                "matchedOrders": 0,
                "previousTotal": ZERO,
                "currentTotal": ZERO,
                "savings": ZERO,
            },
        )
        rollup["matchedOrders"] += 1
        rollup["previousTotal"] += previous_basket.total
        rollup["currentTotal"] += current_basket.total
        rollup["savings"] += savings

    matched_orders = matched_orders[:PAIR_LIMIT]
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
        "droppedProductCount": len(dropped_products),
        "currentOrdersWithDroppedProducts": len(orders_with_drop),
        "productSavings": product_savings,
        "productSavingsPct": _pct(product_savings, previous_implied),
        "matchedPairCount": len(matched_orders),
        "matchedSavings": matched_savings,
        "matchedSavingsPct": _pct(matched_savings, previous_matched_total),
        "customersWithSavings": len(customers_with_savings),
    }
    return {
        "summary": summary,
        "products": dropped_products,
        "matchedOrders": matched_orders,
        "customers": customers,
        "comparison": comparison,
        "appliedFilters": applied_filters(filters),
        "metadata": metadata,
    }
