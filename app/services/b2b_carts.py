"""B2B snapshots from Pedidos V2. Open carts are not confirmed abandonments."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import String, case, cast, func, select

from app.domain.order_status import (
    CANCELLED_ORDER_STATUSES,
    VALID_SALE_STATUSES,
    QUOTE_ORDER_STATUSES,
    status_sql_in,
)
from app.models import B2bCartObservation, Customer, Order, Seller, SyncState
from app.services.analytics_filters import (
    applied_filters,
    date_bounds,
    order_conditions,
    data_through_timestamp,
)


STAGES = (
    "open",
    "possible_abandonment",
    "converted",
    "completed_quote",
    "cancelled",
    "other",
)


def b2b_status(value):
    # Reject booleans and undocumented values instead of silently treating them as carts.
    if type(value) in (int, str) and str(value).strip() in {"1", "2"}:
        return int(value)
    return None


def observe_cart(db, order, *, observed_at=None):
    raw = order.raw if isinstance(order.raw, dict) else {}
    status = b2b_status(raw.get("status_b2b"))
    if status is None and not db.scalar(
        select(B2bCartObservation.id)
        .where(B2bCartObservation.order_mercos_id == order.mercos_id)
        .limit(1)
    ):
        return False
    updated = order.source_updated_at
    if updated is not None:
        updated = (
            updated.replace(tzinfo=timezone.utc)
            if updated.tzinfo is None
            else updated.astimezone(timezone.utc)
        )
    version = [
        order.mercos_id,
        status,
        order.status,
        str(Decimal(order.total).quantize(Decimal("0.01"))),
        updated.isoformat() if updated else None,
    ]
    fingerprint = hashlib.sha256(json.dumps(version).encode()).hexdigest()
    if db.scalar(
        select(B2bCartObservation.id).where(
            B2bCartObservation.fingerprint == fingerprint
        )
    ):
        return False
    db.add(
        B2bCartObservation(
            order_mercos_id=order.mercos_id,
            fingerprint=fingerprint,
            b2b_status=status,
            order_status=order.status,
            total=order.total,
            source_updated_at=order.source_updated_at,
            observed_at=observed_at or datetime.now(timezone.utc),
        )
    )
    return True


def cart_report(
    db, filters, *, abandonment_hours=24, page=1, page_size=50, stage="all", now=None
):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=abandonment_hours)
    source_status = cast(Order.raw["status_b2b"].as_string(), String)
    identified = source_status.in_(("1", "2"))
    # Never use issue date as cart creation date: it can change when converted.
    conditions = order_conditions(filters, bounds=(None, None))
    start, end = date_bounds(filters, now=now)
    if start is not None:
        conditions.append(Order.source_created_at >= start)
    if end is not None:
        conditions.append(Order.source_created_at < end)
    classification = case(
        (status_sql_in(Order.status, CANCELLED_ORDER_STATUSES), "cancelled"),
        (
            (source_status == "2") & status_sql_in(Order.status, VALID_SALE_STATUSES),
            "converted",
        ),
        (
            (source_status == "2") & status_sql_in(Order.status, QUOTE_ORDER_STATUSES),
            "completed_quote",
        ),
        (
            (source_status == "1")
            & status_sql_in(Order.status, QUOTE_ORDER_STATUSES)
            & (Order.source_updated_at <= cutoff),
            "possible_abandonment",
        ),
        (
            (source_status == "1") & status_sql_in(Order.status, QUOTE_ORDER_STATUSES),
            "open",
        ),
        else_="other",
    )
    records = (
        select(
            Order.mercos_id.label("id"),
            Order.number,
            Order.customer_mercos_id,
            Order.seller_mercos_id,
            Order.status,
            Order.total,
            Order.source_created_at,
            Order.source_updated_at,
            source_status.label("b2b_status"),
            classification.label("stage"),
        )
        .where(identified, *conditions)
        .subquery()
    )
    groups = {key: {"count": 0, "value": Decimal("0")} for key in STAGES}
    for key, count, value in db.execute(
        select(records.c.stage, func.count(), func.sum(records.c.total)).group_by(
            records.c.stage
        )
    ):
        groups[key] = {"count": count, "value": value or Decimal("0")}
    total = sum(g["count"] for g in groups.values())
    value = sum((g["value"] for g in groups.values()), Decimal("0"))
    opened = groups["open"]["count"] + groups["possible_abandonment"]["count"]
    open_value = groups["open"]["value"] + groups["possible_abandonment"]["value"]
    rows_query = (
        select(
            records,
            Customer.name.label("customer_name"),
            Seller.name.label("seller_name"),
        )
        .outerjoin(Customer, Customer.mercos_id == records.c.customer_mercos_id)
        .outerjoin(Seller, Seller.mercos_id == records.c.seller_mercos_id)
    )
    if stage != "all":
        rows_query = rows_query.where(records.c.stage == stage)
    items = [
        {
            "id": r.id,
            "number": r.number,
            "customerName": r.customer_name,
            "sellerName": r.seller_name,
            "status": r.status,
            "b2bStatus": int(r.b2b_status),
            "stage": r.stage,
            "total": float(r.total),
            "createdAt": r.source_created_at,
            "updatedAt": r.source_updated_at,
        }
        for r in db.execute(
            rows_query.order_by(records.c.source_created_at.desc(), records.c.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ]
    matched = total if stage == "all" else groups[stage]["count"]
    all_orders = db.scalar(select(func.count(Order.id))) or 0
    all_b2b = db.scalar(select(func.count(Order.id)).where(identified)) or 0
    undated = (
        db.scalar(
            select(func.count(Order.id)).where(
                identified, Order.source_created_at.is_(None)
            )
        )
        or 0
    )
    warnings = [
        "Cobertura de todos os carrinhos desde a primeira adição ainda não validada com o Mercos; os totais representam registros B2B recebidos pela API.",
        "Possível abandono = orçamento B2B em aberto sem alteração do registro há pelo menos "
        f"{abandonment_hours} horas; não confirma inatividade do cliente nem perda de receita.",
        "Período baseado na criação do registro Mercos; situações e valores são os atuais. Conclusão do B2B não significa pagamento ou faturamento.",
    ]
    if undated:
        warnings.append(
            f"{undated} registros B2B sem data de criação não entram em períodos com limites de datas."
        )
    sync = db.get(SyncState, "orders")
    if not sync or sync.status != "success":
        warnings.append(
            "A carga de pedidos ainda não foi concluída com sucesso ou está em andamento."
        )
    return {
        "summary": {
            "identified": {"count": total, "value": float(value)},
            "openTotal": {"count": opened, "value": float(open_value)},
            **{
                k: {"count": v["count"], "value": float(v["value"])}
                for k, v in groups.items()
            },
            "conversionRate": round(groups["converted"]["count"] / total * 100, 2)
            if total
            else None,
            "abandonedMinusConvertedValue": float(
                groups["possible_abandonment"]["value"] - groups["converted"]["value"]
            ),
        },
        "items": items,
        "page": page,
        "pageSize": page_size,
        "totalItems": matched,
        "totalPages": (matched + page_size - 1) // page_size,
        "abandonmentHours": abandonment_hours,
        "stage": stage,
        "appliedFilters": applied_filters(filters),
        "metadata": {
            "generatedAt": now,
            "dataThrough": data_through_timestamp(db),
            "isPartial": True,
            "warnings": warnings,
            "quality": {
                "orders": all_orders,
                "identifiedB2bRecords": all_b2b,
                "unclassifiedOrNonB2bOrders": all_orders - all_b2b,
                "b2bWithoutCreationDate": undated,
            },
        },
    }
