from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.analytics import MAX_ORDER_TOTAL, REVENUE_STATUSES, classify_customer, _aware, _now
from app.models import CrmAttendance, Customer, Order, OrderItem, Product, Seller


def _money(value) -> float:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if amount < 0 or amount > float(MAX_ORDER_TOTAL):
        return 0.0
    return round(amount, 2)


def _iso(value):
    aware = _aware(value)
    return aware.isoformat() if aware else None


def _pick(raw: dict, *keys):
    for key in keys:
        value = raw.get(key)
        if value not in (None, "", []):
            return value
    return None


def _customer_extras(customer: Customer) -> dict:
    raw = customer.raw if isinstance(customer.raw, dict) else {}
    emails = raw.get("emails") or []
    first_email = emails[0] if emails and isinstance(emails[0], dict) else {}
    address_parts = [
        _pick(raw, "endereco", "logradouro", "rua"),
        _pick(raw, "numero"),
        _pick(raw, "complemento"),
        _pick(raw, "bairro"),
        _pick(raw, "cep"),
    ]
    return {
        "document": customer.document or _pick(raw, "cnpj", "cpf"),
        "tradeName": _pick(raw, "nome_fantasia", "nomeFantasia"),
        "legalName": _pick(raw, "razao_social", "razaoSocial") or customer.name,
        "address": " ".join(str(part) for part in address_parts if part) or None,
        "neighborhood": _pick(raw, "bairro"),
        "zipCode": _pick(raw, "cep"),
        "ie": _pick(raw, "inscricao_estadual", "ie"),
        "branch": _pick(raw, "ramo_atividade", "ramo"),
        "blocked": bool(_pick(raw, "bloqueado") or False),
        "type": _pick(raw, "tipo"),
        "extraPhone": _pick(raw, "telefone", "fone"),
        "mobile": _pick(raw, "celular") or customer.phone,
        "extraEmail": first_email.get("email") if isinstance(first_email, dict) else customer.email,
        "createdAtSource": _iso(customer.created_at_source),
        "updatedAtSource": _iso(customer.source_updated_at),
        "active": customer.active,
    }


def _attendances_by_customer(db: Session) -> dict[str, CrmAttendance]:
    return {row.customer_mercos_id: row for row in db.scalars(select(CrmAttendance))}


def _order_stats(db: Session):
    rows = db.execute(
        select(
            Order.customer_mercos_id.label("customer_mercos_id"),
            func.count(Order.id).label("orders"),
            func.coalesce(func.sum(Order.total), 0).label("revenue"),
            func.max(Order.issued_at).label("last_order_at"),
            func.min(Order.issued_at).label("first_order_at"),
            func.max(Order.seller_mercos_id).label("last_seller_id"),
        )
        .where(
            func.lower(Order.status).in_(REVENUE_STATUSES),
            Order.customer_mercos_id.is_not(None),
            Order.customer_mercos_id != "",
            Order.total > 0,
            Order.total < MAX_ORDER_TOTAL,
        )
        .group_by(Order.customer_mercos_id)
    ).all()
    return {row.customer_mercos_id: row for row in rows}


def _last_products_map(db: Session, customer_ids: list[str], limit_each: int = 3) -> dict[str, list[dict]]:
    if not customer_ids:
        return {}
    rows = db.execute(
        select(
            Order.customer_mercos_id,
            OrderItem.name,
            OrderItem.code,
            OrderItem.quantity,
            OrderItem.total,
            Order.issued_at,
            Order.number,
        )
        .join(Order, Order.mercos_id == OrderItem.order_mercos_id)
        .where(
            Order.customer_mercos_id.in_(customer_ids),
            func.lower(Order.status).in_(REVENUE_STATUSES),
            OrderItem.excluded.is_(False),
        )
        .order_by(Order.issued_at.desc())
    ).all()
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        bucket = grouped.setdefault(row.customer_mercos_id, [])
        if len(bucket) >= limit_each:
            continue
        bucket.append(
            {
                "name": row.name or row.code or "Produto",
                "code": row.code,
                "quantity": float(row.quantity or 0),
                "total": _money(row.total),
                "orderNumber": row.number,
                "date": _iso(row.issued_at),
            }
        )
    return grouped


def _lead_summary(customer: Customer, stats, attendance: CrmAttendance | None, sellers: dict[str, str], last_products: list[dict]) -> dict:
    now = _now()
    last = _aware(stats.last_order_at) if stats else None
    first = _aware(stats.first_order_at) if stats else None
    orders = int(stats.orders) if stats else 0
    revenue = _money(stats.revenue) if stats else 0.0
    segment = classify_customer(stats, inactive_days=90, risk_days=90, now=now)
    status = attendance.status if attendance else "open"
    seller_id = stats.last_seller_id if stats else None
    return {
        "id": customer.mercos_id,
        "name": customer.name,
        "city": customer.city,
        "state": customer.state,
        "email": customer.email,
        "phone": customer.phone,
        "document": customer.document,
        "segment": segment,
        "orders": orders,
        "revenue": revenue,
        "ticketAverage": round(revenue / orders, 2) if orders else 0,
        "firstOrderAt": _iso(first),
        "lastOrderAt": _iso(last),
        "daysSinceLastOrder": (now - last).days if last else None,
        "sellerName": sellers.get(seller_id) if seller_id else None,
        "attendanceStatus": status,
        "claimedBy": attendance.seller_name if attendance else None,
        "claimedAt": _iso(attendance.claimed_at) if attendance else None,
        "lastProducts": last_products,
    }


def _order_stats_subquery():
    return (
        select(
            Order.customer_mercos_id.label("customer_mercos_id"),
            func.count(Order.id).label("orders"),
            func.coalesce(func.sum(Order.total), 0).label("revenue"),
            func.max(Order.issued_at).label("last_order_at"),
            func.min(Order.issued_at).label("first_order_at"),
            func.max(Order.seller_mercos_id).label("last_seller_id"),
        )
        .where(
            func.lower(Order.status).in_(REVENUE_STATUSES),
            Order.customer_mercos_id.is_not(None),
            Order.customer_mercos_id != "",
            Order.total > 0,
            Order.total < MAX_ORDER_TOTAL,
        )
        .group_by(Order.customer_mercos_id)
        .subquery("crm_order_stats")
    )


def _customer_filters(*, finished_ids: set[str], search: str | None):
    filters = []
    if finished_ids:
        filters.append(Customer.mercos_id.notin_(finished_ids))
    needle = (search or "").strip()
    if needle:
        pattern = f"%{needle}%"
        filters.append(
            or_(
                Customer.name.ilike(pattern),
                Customer.city.ilike(pattern),
                Customer.state.ilike(pattern),
                Customer.email.ilike(pattern),
                Customer.phone.ilike(pattern),
                Customer.document.ilike(pattern),
            )
        )
    return filters


def list_leads(db: Session, *, search: str | None = None, top: int = 20, queue_limit: int = 80) -> dict:
    attendances = _attendances_by_customer(db)
    finished_ids = {
        customer_id for customer_id, row in attendances.items() if row.status == "finished"
    }
    sellers = {row.mercos_id: row.name for row in db.scalars(select(Seller))}
    stats_sq = _order_stats_subquery()
    filters = _customer_filters(finished_ids=finished_ids, search=search)

    count_stmt = select(func.count(Customer.id))
    if filters:
        count_stmt = count_stmt.where(*filters)
    total_count = int(db.scalar(count_stmt) or 0)

    in_progress = int(
        db.scalar(
            select(func.count())
            .select_from(CrmAttendance)
            .where(CrmAttendance.status == "in_progress")
        )
        or 0
    )

    top_n = max(1, min(top, 50))
    queue_n = max(1, min(queue_limit, 200))
    limit = top_n + queue_n

    stmt = select(
        Customer,
        stats_sq.c.orders,
        stats_sq.c.revenue,
        stats_sq.c.last_order_at,
        stats_sq.c.first_order_at,
        stats_sq.c.last_seller_id,
    ).outerjoin(stats_sq, Customer.mercos_id == stats_sq.c.customer_mercos_id)
    if filters:
        stmt = stmt.where(*filters)
    rows = db.execute(
        stmt.order_by(
            stats_sq.c.revenue.desc().nulls_last(),
            stats_sq.c.last_order_at.asc().nulls_first(),
            Customer.name.asc(),
        ).limit(limit)
    ).all()

    visible = []
    for customer, orders, revenue, last_order_at, first_order_at, last_seller_id in rows:
        stats = None
        if orders is not None:
            stats = type(
                "Stats",
                (),
                {
                    "customer_mercos_id": customer.mercos_id,
                    "orders": orders,
                    "revenue": revenue,
                    "last_order_at": last_order_at,
                    "first_order_at": first_order_at,
                    "last_seller_id": last_seller_id,
                },
            )()
        visible.append(
            _lead_summary(
                customer,
                stats,
                attendances.get(customer.mercos_id),
                sellers,
                [],
            )
        )

    last_products = _last_products_map(db, [row["id"] for row in visible], 3)
    for row in visible:
        row["lastProducts"] = last_products.get(row["id"], [])

    return {
        "count": total_count,
        "topCount": min(top_n, len(visible)),
        "top": visible[:top_n],
        "queue": visible[top_n:],
        "hidden": max(0, total_count - len(visible)),
        "inProgress": in_progress,
        "open": max(0, total_count - in_progress),
    }


def _product_breakdown(db: Session, customer_id: str) -> tuple[list[dict], list[dict], list[dict]]:
    rows = db.execute(
        select(
            OrderItem.product_mercos_id,
            OrderItem.name,
            OrderItem.code,
            OrderItem.quantity,
            OrderItem.unit_price,
            OrderItem.total,
            Order.issued_at,
            Order.number,
            Order.status,
            Order.total.label("order_total"),
            Order.mercos_id,
            Order.seller_mercos_id,
        )
        .join(Order, Order.mercos_id == OrderItem.order_mercos_id)
        .where(
            Order.customer_mercos_id == customer_id,
            OrderItem.excluded.is_(False),
        )
        .order_by(Order.issued_at.desc(), OrderItem.position)
    ).all()
    product_ids = {row.product_mercos_id for row in rows if row.product_mercos_id}
    products = {
        row.mercos_id: row
        for row in db.scalars(select(Product).where(Product.mercos_id.in_(product_ids)))
    } if product_ids else {}
    last_products = []
    most_map: dict[str, dict] = {}
    orders: dict[str, dict] = {}
    for row in rows:
        key = row.product_mercos_id or row.code or row.name
        product = products.get(row.product_mercos_id) if row.product_mercos_id else None
        item = {
            "productId": row.product_mercos_id,
            "name": (product.name if product else None) or row.name or "Produto",
            "code": (product.code if product else None) or row.code,
            "quantity": float(row.quantity or 0),
            "unitPrice": _money(row.unit_price),
            "total": _money(row.total),
            "date": _iso(row.issued_at),
            "orderNumber": row.number,
            "stock": float(product.stock) if product else None,
            "listPrice": _money(product.list_price) if product else None,
        }
        if len(last_products) < 12:
            last_products.append(item)
        bucket = most_map.setdefault(
            key,
            {
                "productId": row.product_mercos_id,
                "name": item["name"],
                "code": item["code"],
                "quantity": 0.0,
                "revenue": 0.0,
                "orders": 0,
                "lastDate": None,
            },
        )
        bucket["quantity"] += float(row.quantity or 0)
        bucket["revenue"] += _money(row.total)
        bucket["orders"] += 1
        if not bucket["lastDate"] or (item["date"] or "") > bucket["lastDate"]:
            bucket["lastDate"] = item["date"]
        order = orders.setdefault(
            row.mercos_id,
            {
                "id": row.mercos_id,
                "number": row.number,
                "status": row.status,
                "date": _iso(row.issued_at),
                "total": _money(row.order_total),
                "sellerId": row.seller_mercos_id,
                "items": [],
            },
        )
        order["items"].append(item)
    most_bought = sorted(
        ({**value, "quantity": round(value["quantity"], 2), "revenue": round(value["revenue"], 2)} for value in most_map.values()),
        key=lambda row: (-row["quantity"], -row["revenue"]),
    )[:12]
    order_history = list(orders.values())[:40]
    return last_products, most_bought, order_history


def lead_detail(db: Session, customer_id: str) -> dict:
    customer = db.scalar(select(Customer).where(Customer.mercos_id == customer_id))
    if not customer:
        raise HTTPException(404, "Lead nao encontrado")
    attendance = db.scalar(select(CrmAttendance).where(CrmAttendance.customer_mercos_id == customer_id))
    if attendance and attendance.status == "finished":
        raise HTTPException(410, "Atendimento ja finalizado")
    stats_map = _order_stats(db)
    sellers = {row.mercos_id: row.name for row in db.scalars(select(Seller))}
    last_products, most_bought, order_history = _product_breakdown(db, customer_id)
    for order in order_history:
        order["sellerName"] = sellers.get(order.get("sellerId"))
    summary = _lead_summary(
        customer,
        stats_map.get(customer_id),
        attendance,
        sellers,
        last_products[:3],
    )
    extras = _customer_extras(customer)
    return {
        **summary,
        **extras,
        "lastProducts": last_products,
        "mostBoughtProducts": most_bought,
        "orderHistory": order_history,
        "notes": attendance.notes if attendance else None,
    }


def _upsert_attendance(db: Session, customer_id: str) -> CrmAttendance:
    customer = db.scalar(select(Customer).where(Customer.mercos_id == customer_id))
    if not customer:
        raise HTTPException(404, "Lead nao encontrado")
    row = db.scalar(select(CrmAttendance).where(CrmAttendance.customer_mercos_id == customer_id))
    now = datetime.now(timezone.utc)
    if row is None:
        row = CrmAttendance(
            customer_mercos_id=customer_id,
            status="open",
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.flush()
    return row


def claim_lead(db: Session, customer_id: str, seller_name: str | None) -> dict:
    row = _upsert_attendance(db, customer_id)
    if row.status == "finished":
        raise HTTPException(409, "Este lead ja foi finalizado")
    now = datetime.now(timezone.utc)
    row.status = "in_progress"
    row.seller_name = (seller_name or row.seller_name or "").strip() or None
    row.claimed_at = row.claimed_at or now
    row.updated_at = now
    db.add(row)
    db.commit()
    return lead_detail(db, customer_id)


def finish_lead(db: Session, customer_id: str, seller_name: str | None, notes: str | None) -> dict:
    row = _upsert_attendance(db, customer_id)
    if row.status == "finished":
        raise HTTPException(409, "Este lead ja foi finalizado")
    now = datetime.now(timezone.utc)
    row.status = "finished"
    row.seller_name = (seller_name or row.seller_name or "").strip() or None
    row.finished_at = now
    row.updated_at = now
    if notes:
        row.notes = notes.strip()
    if not row.claimed_at:
        row.claimed_at = now
    db.add(row)
    db.commit()
    return {"id": customer_id, "status": "finished", "finishedAt": _iso(now)}


def crm_dashboard(db: Session, *, days: int = 30) -> dict:
    now = _now()
    start = now - timedelta(days=days)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month = today.replace(day=1)
    attendances = list(db.scalars(select(CrmAttendance)))
    stats_map = _order_stats(db)
    finished = [row for row in attendances if row.status == "finished"]
    in_progress = [row for row in attendances if row.status == "in_progress"]
    finished_ids = {row.customer_mercos_id for row in finished}
    customer_rows = list(db.execute(select(Customer.mercos_id, Customer.name)).all())
    open_count = sum(1 for row in customer_rows if row.mercos_id not in finished_ids)

    def revenue_for(ids: set[str]) -> float:
        return round(sum(_money(stats_map.get(customer_id).revenue) for customer_id in ids if stats_map.get(customer_id)), 2)

    finished_today = [row for row in finished if row.finished_at and _aware(row.finished_at) >= today]
    finished_month = [row for row in finished if row.finished_at and _aware(row.finished_at) >= month]
    finished_period = [row for row in finished if row.finished_at and _aware(row.finished_at) >= start]

    durations = []
    for row in finished_period:
        if row.claimed_at and row.finished_at:
            durations.append((_aware(row.finished_at) - _aware(row.claimed_at)).total_seconds() / 60)

    daily: dict[str, dict] = {}
    for row in finished_period:
        finished_at = _aware(row.finished_at)
        if not finished_at:
            continue
        key = finished_at.date().isoformat()
        bucket = daily.setdefault(key, {"date": key, "attendances": 0, "revenue": 0.0})
        bucket["attendances"] += 1
        stats = stats_map.get(row.customer_mercos_id)
        bucket["revenue"] = round(bucket["revenue"] + (_money(stats.revenue) if stats else 0), 2)

    recent = sorted(finished, key=lambda row: row.finished_at or row.updated_at, reverse=True)[:12]
    names = {row.mercos_id: row.name for row in customer_rows}
    return {
        "periodDays": days,
        "kpis": {
            "openLeads": open_count,
            "inProgress": len(in_progress),
            "finishedToday": len(finished_today),
            "finishedMonth": len(finished_month),
            "finishedPeriod": len(finished_period),
            "billingOpen": revenue_for({row.mercos_id for row in customer_rows if row.mercos_id not in finished_ids}),
            "billingFinished": revenue_for({row.customer_mercos_id for row in finished}),
            "billingFinishedPeriod": revenue_for({row.customer_mercos_id for row in finished_period}),
            "averageHandleMinutes": round(sum(durations) / len(durations), 1) if durations else 0,
        },
        "series": [daily[key] for key in sorted(daily)],
        "recentFinished": [
            {
                "id": row.customer_mercos_id,
                "name": names.get(row.customer_mercos_id) or row.customer_mercos_id,
                "sellerName": row.seller_name,
                "finishedAt": _iso(row.finished_at),
                "revenue": _money(stats_map[row.customer_mercos_id].revenue) if stats_map.get(row.customer_mercos_id) else 0,
            }
            for row in recent
        ],
        "sellers": _seller_breakdown(finished_period, stats_map),
    }


def _seller_breakdown(finished: list[CrmAttendance], stats_map) -> list[dict]:
    grouped: dict[str, dict] = {}
    for row in finished:
        name = row.seller_name or "Sem vendedor"
        bucket = grouped.setdefault(name, {"sellerName": name, "attendances": 0, "revenue": 0.0})
        bucket["attendances"] += 1
        stats = stats_map.get(row.customer_mercos_id)
        bucket["revenue"] = round(bucket["revenue"] + (_money(stats.revenue) if stats else 0), 2)
    return sorted(grouped.values(), key=lambda row: (-row["attendances"], -row["revenue"]))
