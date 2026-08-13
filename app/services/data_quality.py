from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from app.domain.order_status import RECOGNIZED_ORDER_STATUSES, status_sql_in
from app.models import Customer, Order, OrderItem, Product, Seller, SyncState


RAW_MODELS = {
    "customers": Customer,
    "products": Product,
    "sellers": Seller,
    "orders": Order,
    "orderItems": OrderItem,
}
SENSITIVE_RAW_KEYS = {
    "token",
    "applicationtoken",
    "companytoken",
    "password",
    "secret",
    "apikey",
    "api_key",
}


def _scalar_int(db: Session, statement) -> int:
    return int(db.scalar(statement) or 0)


def _pct(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100, 2)


def _duplicate_groups(db: Session, column) -> int:
    grouped = (
        select(column.label("value"))
        .where(column.is_not(None), func.trim(column) != "")
        .group_by(column)
        .having(func.count() > 1)
        .subquery()
    )
    return _scalar_int(db, select(func.count()).select_from(grouped))


def _empty_raw_count(db: Session, model) -> int:
    raw_text = func.lower(func.trim(cast(model.raw, String)))
    return _scalar_int(
        db,
        select(func.count(model.id)).where(
            or_(
                model.raw.is_(None),
                raw_text.in_(("", "{}", "null")),
            )
        ),
    )


def raw_field_inventory(db: Session, sample_limit: int = 100) -> dict[str, dict[str, Any]]:
    """Inspect raw payload shape without returning customer values or PII."""
    inventory: dict[str, dict[str, Any]] = {}
    safe_limit = max(1, min(sample_limit, 500))
    for table_name, model in RAW_MODELS.items():
        field_counts: Counter[str] = Counter()
        field_types: dict[str, Counter[str]] = defaultdict(Counter)
        samples = db.scalars(
            select(model.raw).where(model.raw.is_not(None)).limit(safe_limit)
        ).all()
        for raw in samples:
            if not isinstance(raw, dict):
                continue
            for key, value in raw.items():
                normalized_key = key.lower().replace("-", "").replace("_", "")
                if normalized_key in SENSITIVE_RAW_KEYS:
                    continue
                field_counts[key] += 1
                field_types[key][type(value).__name__] += 1
        inventory[table_name] = {
            "sampledRows": len(samples),
            "fields": {
                key: {
                    "occurrences": count,
                    "types": dict(field_types[key]),
                }
                for key, count in sorted(field_counts.items())
            },
        }
    return inventory


def build_data_quality_report(
    db: Session, *, include_raw_inventory: bool = False, raw_sample_limit: int = 100
) -> dict[str, Any]:
    total_customers = _scalar_int(db, select(func.count(Customer.id)))
    total_products = _scalar_int(db, select(func.count(Product.id)))
    total_sellers = _scalar_int(db, select(func.count(Seller.id)))
    total_orders = _scalar_int(db, select(func.count(Order.id)))
    total_items = _scalar_int(db, select(func.count(OrderItem.id)))

    orders_with_items = _scalar_int(
        db,
        select(func.count(func.distinct(Order.id)))
        .select_from(Order)
        .join(OrderItem, OrderItem.order_mercos_id == Order.mercos_id),
    )
    orders_with_customer = _scalar_int(
        db,
        select(func.count(func.distinct(Order.id)))
        .select_from(Order)
        .join(Customer, Customer.mercos_id == Order.customer_mercos_id),
    )
    orders_with_seller = _scalar_int(
        db,
        select(func.count(func.distinct(Order.id)))
        .select_from(Order)
        .join(Seller, Seller.mercos_id == Order.seller_mercos_id),
    )
    items_with_product = _scalar_int(
        db,
        select(func.count(func.distinct(OrderItem.id)))
        .select_from(OrderItem)
        .join(Product, Product.mercos_id == OrderItem.product_mercos_id),
    )
    recognized_statuses = _scalar_int(
        db,
        select(func.count(Order.id)).where(
            status_sql_in(Order.status, RECOGNIZED_ORDER_STATUSES)
        ),
    )

    item_totals = (
        select(
            OrderItem.order_mercos_id.label("order_id"),
            func.coalesce(func.sum(OrderItem.total), 0).label("items_total"),
        )
        .group_by(OrderItem.order_mercos_id)
        .subquery()
    )
    order_total_divergences = _scalar_int(
        db,
        select(func.count(Order.id))
        .select_from(Order)
        .join(item_totals, item_totals.c.order_id == Order.mercos_id)
        .where(func.abs(func.coalesce(Order.total, 0) - item_totals.c.items_total) > 0.01),
    )

    min_date, max_date = db.execute(
        select(func.min(Order.issued_at), func.max(Order.issued_at))
    ).one()

    sync_rows = list(db.scalars(select(SyncState).order_by(SyncState.resource)))
    sync = [
        {
            "resource": row.resource,
            "status": row.status,
            "cursor": row.cursor,
            "lastSuccessAt": row.last_success_at,
            "records": int(row.records or 0),
            "error": row.error,
        }
        for row in sync_rows
    ]

    coverage = {
        "ordersWithItemsPct": _pct(orders_with_items, total_orders),
        "ordersWithCustomerPct": _pct(orders_with_customer, total_orders),
        "ordersWithSellerPct": _pct(orders_with_seller, total_orders),
        "itemsWithProductPct": _pct(items_with_product, total_items),
        "recognizedStatusPct": _pct(recognized_statuses, total_orders),
    }
    integrity = {
        "ordersWithoutItems": max(total_orders - orders_with_items, 0),
        "ordersWithoutCustomer": max(total_orders - orders_with_customer, 0),
        "ordersWithoutSeller": max(total_orders - orders_with_seller, 0),
        "itemsWithoutProduct": max(total_items - items_with_product, 0),
        "orderTotalDivergences": order_total_divergences,
    }
    zero_values = {
        "ordersWithZeroTotal": _scalar_int(
            db, select(func.count(Order.id)).where(func.coalesce(Order.total, 0) == 0)
        ),
        "itemsWithZeroQuantity": _scalar_int(
            db,
            select(func.count(OrderItem.id)).where(
                func.coalesce(OrderItem.quantity, 0) == 0
            ),
        ),
        "itemsWithZeroTotal": _scalar_int(
            db,
            select(func.count(OrderItem.id)).where(
                func.coalesce(OrderItem.total, 0) == 0
            ),
        ),
    }
    missing_dimensions = {
        "productsWithoutCategory": _scalar_int(
            db,
            select(func.count(Product.id)).where(
                or_(Product.category_id.is_(None), func.trim(Product.category_id) == "")
            ),
        )
    }
    duplicates = {
        "customerDocumentGroups": _duplicate_groups(db, Customer.document),
        "productCodeGroups": _duplicate_groups(db, Product.code),
    }
    empty_raw = {
        table_name: _empty_raw_count(db, model)
        for table_name, model in RAW_MODELS.items()
    }

    warnings: list[str] = []
    if total_orders == 0:
        warnings.append("Nenhum pedido foi persistido.")
    if coverage["ordersWithItemsPct"] < 95:
        warnings.append(
            "Cobertura de itens abaixo de 95%; rankings de produtos não são confiáveis."
        )
    if coverage["ordersWithCustomerPct"] < 95:
        warnings.append("Cobertura de clientes nos pedidos abaixo de 95%.")
    if coverage["ordersWithSellerPct"] < 95:
        warnings.append("Cobertura de vendedores nos pedidos abaixo de 95%.")
    if order_total_divergences:
        warnings.append(
            f"{order_total_divergences} pedido(s) divergem da soma de seus itens."
        )
    incomplete_statuses = {"running", "partial", "interrupted", "error"}
    incomplete_sync = [row.resource for row in sync_rows if row.status in incomplete_statuses]
    if incomplete_sync:
        warnings.append(
            "Sincronização incompleta em: " + ", ".join(sorted(incomplete_sync)) + "."
        )

    generated_at = datetime.now(timezone.utc)
    is_partial = bool(incomplete_sync) or coverage["ordersWithItemsPct"] < 95
    return {
        "coverage": coverage,
        "integrity": integrity,
        "dateRange": {"min": min_date, "max": max_date},
        "sync": sync,
        "warnings": warnings,
        "counts": {
            "customers": total_customers,
            "products": total_products,
            "sellers": total_sellers,
            "orders": total_orders,
            "orderItems": total_items,
        },
        "zeroValues": zero_values,
        "duplicates": duplicates,
        "missingDimensions": missing_dimensions,
        "emptyRaw": empty_raw,
        "metadata": {
            "generatedAt": generated_at,
            "dataThrough": max_date,
            "isPartial": is_partial,
            "warnings": warnings,
        },
        "rawFieldInventory": (
            raw_field_inventory(db, raw_sample_limit)
            if include_raw_inventory
            else None
        ),
    }
