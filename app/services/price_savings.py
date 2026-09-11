from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import median
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, defer

from app.models import Customer, Order, OrderItem
from app.schemas.analytics import AnalyticsFilters
from app.services.analytics_filters import BR_TZ
from app.services.analytics_v2 import (
    PLACEHOLDER_LIST_PRICES,
    ZERO,
    _decimal,
    _sale_conditions,
    analytics_metadata,
)


PRODUCT_LIMIT = 2000
ORDER_LIMIT = 200
CUSTOMER_LIMIT = 50
REST_MEMBER_LIMIT = 40
MAX_DROP_PCT = Decimal("70")
CURRENT_WINDOW_DAYS = 45
PRIOR_LOOKBACK_DAYS = 45
NO_COMPARISON_WARNING = (
    "Não foi possível montar a janela de 45 dias para comparar o preço do Club."
)
CLUB_FILTERS = AnalyticsFilters(period="all")
CUSTOMER_TIER_BOUNDS = (
    ("top10", "Top 10", 0, 10),
    ("top20", "11 a 20", 10, 20),
    ("top50", "21 a 50", 20, 50),
    ("rest", "Restantes", 50, None),
)
PRODUCT_TIER_BOUNDS = (
    ("top10", "Top 10 SKUs", 0, 10),
    ("top20", "11 a 20", 10, 20),
    ("top50", "21 a 50", 20, 50),
    ("rest", "Demais SKUs", 50, None),
)
DROP_BUCKETS = (
    ("Até 10%", Decimal("0"), Decimal("10")),
    ("10–20%", Decimal("10"), Decimal("20")),
    ("20–30%", Decimal("20"), Decimal("30")),
    ("30–50%", Decimal("30"), Decimal("50")),
    ("Acima de 50%", Decimal("50"), Decimal("1000")),
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


def _club_bounds(
    *,
    now: datetime | None = None,
) -> tuple[
    tuple[datetime, datetime],
    tuple[datetime, datetime],
]:
    current_end = now or datetime.now(timezone.utc)
    if current_end.tzinfo is None:
        current_end = current_end.replace(tzinfo=timezone.utc)
    current_start = current_end - timedelta(days=CURRENT_WINDOW_DAYS)
    prior_start = current_start - timedelta(days=PRIOR_LOOKBACK_DAYS)
    return (current_start, current_end), (prior_start, current_start)


def _br_date(value: datetime) -> str:
    return value.astimezone(BR_TZ).date().isoformat()


def _br_last_included_date(end: datetime) -> str:
    return (end.astimezone(BR_TZ) - timedelta(microseconds=1)).date().isoformat()


def _club_comparison(
    current_bounds: tuple[datetime, datetime],
    prior_bounds: tuple[datetime, datetime],
) -> dict[str, str]:
    current_start, current_end = current_bounds
    prior_start, prior_end = prior_bounds
    return {
        "currentFrom": _br_date(current_start),
        "currentTo": _br_last_included_date(current_end),
        "previousFrom": _br_date(prior_start),
        "previousTo": _br_last_included_date(prior_end),
    }


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


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _mean_pct(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _median_pct(values: list[float]) -> float | None:
    if not values:
        return None
    return round(float(median(values)), 2)


def _week_bounds(issued_at: datetime) -> tuple[str, str, str]:
    local = issued_at.astimezone(BR_TZ).date()
    monday = local - timedelta(days=local.weekday())
    sunday = monday + timedelta(days=6)
    iso = monday.isocalendar()
    return f"{iso.year}-W{iso.week:02d}", monday.isoformat(), sunday.isoformat()


def _customer_payload(rollup: dict[str, Any], *, rank: int | None = None) -> dict[str, Any]:
    previous_total = _money(rollup["previousTotal"])
    payload = {
        "id": rollup["id"],
        "name": rollup["name"],
        "matchedOrders": rollup["matchedOrders"],
        "previousTotal": previous_total,
        "currentTotal": _money(rollup["currentTotal"]),
        "savings": _money(rollup["savings"]),
        "savingsPct": _pct(rollup["savings"], rollup["previousTotal"]),
    }
    if rank is not None:
        payload["rank"] = rank
    return payload


def _slice_rows(rows: list[dict[str, Any]], start: int, end: int | None) -> list[dict[str, Any]]:
    if end is None:
        return rows[start:]
    return rows[start:end]


def _tier_from_rows(
    rows: list[dict[str, Any]],
    *,
    key: str,
    label: str,
    rank_from: int,
    rank_to: int | None,
    total_savings: Decimal,
    member_limit: int | None = None,
) -> dict[str, Any]:
    sliced = _slice_rows(rows, rank_from, rank_to)
    previous_total = sum((row["previousTotal"] for row in sliced), ZERO)
    current_total = sum((row["currentTotal"] for row in sliced), ZERO)
    savings = sum((row["savings"] for row in sliced), ZERO)
    orders = sum(int(row.get("matchedOrders") or row.get("currentOrders") or 0) for row in sliced)
    drop_pcts = [row["savingsPct"] for row in sliced if row.get("savingsPct") is not None]
    members = sliced if member_limit is None else sliced[:member_limit]
    occupied_from = rank_from + 1 if sliced else 0
    occupied_to = rank_from + len(sliced) if sliced else 0
    return {
        "key": key,
        "label": label,
        "rankFrom": occupied_from,
        "rankTo": occupied_to,
        "count": len(sliced),
        "orderCount": orders,
        "previousTotal": _money(previous_total),
        "currentTotal": _money(current_total),
        "savings": _money(savings),
        "savingsPct": _pct(savings, previous_total),
        "savingsSharePct": _pct(savings, total_savings),
        "avgDropPct": _mean_pct(drop_pcts),
        "truncated": member_limit is not None and len(sliced) > member_limit,
        "members": members,
    }


def _bucket_drop(drop_pct: float) -> str:
    value = Decimal(str(drop_pct))
    for label, start, end in DROP_BUCKETS:
        if start <= value < end:
            return label
    return DROP_BUCKETS[-1][0]


def price_savings(
    db: Session,
    filters: AnalyticsFilters | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    del filters
    metadata = analytics_metadata(db)
    current_bounds, prior_bounds = _club_bounds(now=now)
    comparison = _club_comparison(current_bounds, prior_bounds)
    warnings = list(metadata["warnings"])

    current_rows = _load_orders(db, CLUB_FILTERS, current_bounds)
    previous_products = _product_agg_from_db(db, CLUB_FILTERS, prior_bounds)
    metadata = {**metadata, "warnings": warnings}

    items_by_order = _load_items(db, CLUB_FILTERS, current_bounds)
    current_baskets = _baskets(current_rows, items_by_order)

    product_rollups: dict[str, dict[str, Any]] = {}
    discounted_orders: list[dict[str, Any]] = []
    club_savings = ZERO
    before_club_total = ZERO
    after_club_total = ZERO
    club_order_count = 0
    customers_with_savings: set[str] = set()
    customer_rollups: dict[str, dict[str, Any]] = {}
    weekly_rollups: dict[str, dict[str, Any]] = {}
    current_revenue = ZERO
    dropped_sku_revenue = ZERO
    unchanged_sku_revenue = ZERO
    new_sku_revenue = ZERO

    for basket in current_baskets:
        current_revenue += basket.total
        item_savings = ZERO
        before_dropped = ZERO
        after_dropped = ZERO
        discounted_skus = 0
        week_key = None
        if basket.order.issued_at:
            week_key, week_from, week_to = _week_bounds(basket.order.issued_at)
        for line in basket.lines:
            dropped = _dropped_line_totals(line, previous_products)
            if dropped is None:
                if line.product_id in previous_products:
                    unchanged_sku_revenue += line.total
                else:
                    new_sku_revenue += line.total
                continue
            before, after, savings = dropped
            before_dropped += before
            after_dropped += after
            item_savings += savings
            discounted_skus += 1
            dropped_sku_revenue += after
            previous = previous_products[line.product_id]
            stats = product_rollups.setdefault(
                line.product_id,
                {
                    "id": line.product_id,
                    "code": line.code or previous.code,
                    "name": line.name or previous.name or line.product_id,
                    "quantity": ZERO,
                    "before": ZERO,
                    "after": ZERO,
                    "savings": ZERO,
                    "current_orders": set(),
                },
            )
            stats["code"] = line.code or stats["code"] or previous.code
            stats["name"] = line.name or stats["name"] or previous.name
            stats["quantity"] += line.quantity
            stats["before"] += before
            stats["after"] += after
            stats["savings"] += savings
            stats["current_orders"].add(basket.order.mercos_id)
            if week_key:
                week = weekly_rollups.setdefault(
                    week_key,
                    {
                        "week": week_key,
                        "from": week_from,
                        "to": week_to,
                        "before": ZERO,
                        "after": ZERO,
                        "savings": ZERO,
                        "orders": set(),
                        "skuIds": set(),
                    },
                )
                week["before"] += before
                week["after"] += after
                week["savings"] += savings
                week["orders"].add(basket.order.mercos_id)
                week["skuIds"].add(line.product_id)
        if item_savings <= 0:
            continue
        club_savings += item_savings
        before_club_total += before_dropped
        after_club_total += after_dropped
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

    dropped_products: list[dict[str, Any]] = []
    sku_drop_pcts: list[float] = []
    qty_drop_weight = ZERO
    qty_drop_total = ZERO
    for product_id, stats in product_rollups.items():
        previous = previous_products[product_id]
        quantity = stats["quantity"]
        current_avg = (
            (stats["after"] / quantity).quantize(Decimal("0.01"))
            if quantity > 0
            else ZERO
        )
        drop_pct = _pct(stats["savings"], stats["before"])
        if drop_pct is not None:
            sku_drop_pcts.append(drop_pct)
            qty_drop_weight += quantity * Decimal(str(drop_pct))
            qty_drop_total += quantity
        dropped_products.append(
            {
                "id": product_id,
                "code": stats["code"] or previous.code,
                "name": stats["name"] or previous.name or product_id,
                "previousAverageUnit": previous.average_unit,
                "currentAverageUnit": current_avg,
                "unitDrop": (previous.average_unit - current_avg).quantize(
                    Decimal("0.01")
                ),
                "dropPct": drop_pct,
                "quantitySold": quantity,
                "currentRevenue": stats["after"].quantize(Decimal("0.01")),
                "savings": stats["savings"].quantize(Decimal("0.01")),
                "currentOrders": len(stats["current_orders"]),
                "previousOrders": previous.n_orders,
            }
        )

    dropped_products.sort(key=lambda row: row["savings"], reverse=True)
    dropped_product_count = len(dropped_products)
    qty_weighted_drop = (
        round(float(qty_drop_weight / qty_drop_total), 2)
        if qty_drop_total > 0
        else None
    )
    value_weighted_drop = _pct(club_savings, before_club_total)

    bucket_rollups = {
        label: {
            "bucket": label,
            "skuCount": 0,
            "quantity": ZERO,
            "savings": ZERO,
            "before": ZERO,
        }
        for label, *_ in DROP_BUCKETS
    }
    for product in dropped_products:
        if product["dropPct"] is None:
            continue
        bucket = bucket_rollups[_bucket_drop(product["dropPct"])]
        bucket["skuCount"] += 1
        bucket["quantity"] += product["quantitySold"]
        bucket["savings"] += product["savings"]
        bucket["before"] += product["savings"] + product["currentRevenue"]
    drop_buckets = [
        {
            **row,
            "quantity": _money(row["quantity"]),
            "savings": _money(row["savings"]),
            "dropPct": _pct(row["savings"], row["before"]),
            "savingsSharePct": _pct(row["savings"], club_savings),
        }
        for row in bucket_rollups.values()
        if row["skuCount"]
    ]

    weekly = []
    for row in sorted(weekly_rollups.values(), key=lambda item: item["from"]):
        weekly.append(
            {
                "week": row["week"],
                "from": row["from"],
                "to": row["to"],
                "previousTotal": _money(row["before"]),
                "currentTotal": _money(row["after"]),
                "savings": _money(row["savings"]),
                "dropPct": _pct(row["savings"], row["before"]),
                "orders": len(row["orders"]),
                "skuCount": len(row["skuIds"]),
            }
        )

    customers = [
        _customer_payload(rollup, rank=index + 1)
        for index, rollup in enumerate(
            sorted(customer_rollups.values(), key=lambda row: row["savings"], reverse=True)
        )
    ]
    customer_drop_pcts = [
        row["savingsPct"] for row in customers if row["savingsPct"] is not None
    ]
    customer_tiers = [
        _tier_from_rows(
            customers,
            key=key,
            label=label,
            rank_from=start,
            rank_to=end,
            total_savings=club_savings,
            member_limit=None if end is not None else REST_MEMBER_LIMIT,
        )
        for key, label, start, end in CUSTOMER_TIER_BOUNDS
    ]
    product_rows_for_tiers = [
        {
            **product,
            "previousTotal": product["savings"] + product["currentRevenue"],
            "currentTotal": product["currentRevenue"],
            "matchedOrders": product["currentOrders"],
            "savingsPct": product["dropPct"],
        }
        for product in dropped_products
    ]
    product_tiers = [
        _tier_from_rows(
            product_rows_for_tiers,
            key=key,
            label=label,
            rank_from=start,
            rank_to=end,
            total_savings=club_savings,
            member_limit=0,
        )
        for key, label, start, end in PRODUCT_TIER_BOUNDS
    ]
    for tier in product_tiers:
        tier["members"] = []

    def _share(limit: int, rows: list[dict[str, Any]]) -> float | None:
        if not rows:
            return None
        return _pct(sum((row["savings"] for row in rows[:limit]), ZERO), club_savings)

    discounted_orders.sort(key=lambda row: row["savings"], reverse=True)
    discounted_orders = discounted_orders[:ORDER_LIMIT]
    listed_products = dropped_products[:PRODUCT_LIMIT]
    listed_customers = customers[:CUSTOMER_LIMIT]

    discount_pct = value_weighted_drop
    summary = {
        "droppedProductCount": dropped_product_count,
        "currentOrdersWithDroppedProducts": club_order_count,
        "productSavings": club_savings,
        "productSavingsPct": discount_pct,
        "previousDroppedTotal": before_club_total,
        "currentDroppedTotal": after_club_total,
        "matchedPairCount": club_order_count,
        "matchedSavings": club_savings,
        "matchedSavingsPct": discount_pct,
        "customersWithSavings": len(customers_with_savings),
        "currentWindowDays": CURRENT_WINDOW_DAYS,
        "previousWindowDays": PRIOR_LOOKBACK_DAYS,
        "simpleAvgDropPct": _mean_pct(sku_drop_pcts),
        "medianDropPct": _median_pct(sku_drop_pcts),
        "qtyWeightedDropPct": qty_weighted_drop,
        "valueWeightedDropPct": value_weighted_drop,
        "customerAvgDropPct": _mean_pct(customer_drop_pcts),
        "customerMedianDropPct": _median_pct(customer_drop_pcts),
        "currentRevenue": _money(current_revenue),
        "currentOrderCount": len(current_baskets),
        "droppedSkuRevenue": _money(dropped_sku_revenue),
        "unchangedSkuRevenue": _money(unchanged_sku_revenue),
        "newSkuRevenue": _money(new_sku_revenue),
        "droppedSkuRevenueSharePct": _pct(dropped_sku_revenue, current_revenue),
        "billImpactPct": _pct(club_savings, current_revenue),
        "top10CustomerSavingsSharePct": _share(10, customers),
        "top20CustomerSavingsSharePct": _share(20, customers),
        "top50CustomerSavingsSharePct": _share(50, customers),
        "top10ProductSavingsSharePct": _share(10, dropped_products),
        "top20ProductSavingsSharePct": _share(20, dropped_products),
        "top50ProductSavingsSharePct": _share(50, dropped_products),
    }
    return {
        "summary": summary,
        "products": listed_products,
        "matchedOrders": discounted_orders,
        "customers": listed_customers,
        "customerTiers": customer_tiers,
        "productTiers": product_tiers,
        "dropBuckets": drop_buckets,
        "weekly": weekly,
        "comparison": comparison,
        "appliedFilters": {
            "scope": "club-analysis",
            "currentDays": CURRENT_WINDOW_DAYS,
            "previousDays": PRIOR_LOOKBACK_DAYS,
        },
        "metadata": metadata,
    }
