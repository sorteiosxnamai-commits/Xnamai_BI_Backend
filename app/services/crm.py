from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import Float, cast, func, or_, select
from sqlalchemy.orm import Session

from app.analytics import MAX_ORDER_TOTAL, REVENUE_STATUSES, classify_customer, _aware, _now
from app.domain.order_status import status_sql_in
from app.models import CrmAttendance, Customer, Order, OrderItem, Product, Seller


def _money(value, *, cap: bool = True) -> float:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if amount < 0:
        return 0.0
    if cap and amount > float(MAX_ORDER_TOTAL):
        return 0.0
    return round(amount, 2)


def _money_total(value) -> float:
    return _money(value, cap=False)


def _order_amount():
    return func.coalesce(Order.net_total, Order.total, Order.gross_total, 0)


def _days_since_last_order(db: Session, last_order_at):
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        return func.coalesce(
            func.julianday("now") - func.julianday(last_order_at),
            9999.0,
        )
    return func.coalesce(
        cast(func.extract("epoch", func.now() - last_order_at), Float) / 86400.0,
        9999.0,
    )


def _lead_priority_order(db: Session, stats_sq):
    days_since = _days_since_last_order(db, stats_sq.c.last_order_at)
    priority = cast(stats_sq.c.revenue, Float) * days_since
    return (
        priority.desc().nulls_last(),
        days_since.desc().nulls_last(),
        stats_sq.c.revenue.desc().nulls_last(),
        Customer.name.asc(),
    )


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
    amount = _order_amount()
    rows = db.execute(
        select(
            Order.customer_mercos_id.label("customer_mercos_id"),
            func.count(Order.id).label("orders"),
            func.coalesce(func.sum(amount), 0).label("revenue"),
            func.max(Order.issued_at).label("last_order_at"),
            func.min(Order.issued_at).label("first_order_at"),
            func.max(Order.seller_mercos_id).label("last_seller_id"),
        )
        .where(
            status_sql_in(Order.status, REVENUE_STATUSES),
            Order.customer_mercos_id.is_not(None),
            Order.customer_mercos_id != "",
            amount > 0,
            amount < MAX_ORDER_TOTAL,
        )
        .group_by(Order.customer_mercos_id)
    ).all()
    return {row.customer_mercos_id: row for row in rows}


def _last_products_map(db: Session, customer_ids: list[str], limit_each: int = 3) -> dict[str, list[dict]]:
    if not customer_ids:
        return {}
    ranked = (
        select(
            Order.customer_mercos_id,
            OrderItem.name,
            OrderItem.code,
            OrderItem.quantity,
            OrderItem.total,
            Order.issued_at,
            Order.number,
            func.row_number()
            .over(partition_by=Order.customer_mercos_id, order_by=Order.issued_at.desc())
            .label("rn"),
        )
        .join(Order, Order.mercos_id == OrderItem.order_mercos_id)
        .where(
            Order.customer_mercos_id.in_(customer_ids),
            status_sql_in(Order.status, REVENUE_STATUSES),
            OrderItem.excluded.is_(False),
        )
        .subquery("crm_last_products")
    )
    rows = db.execute(
        select(ranked).where(ranked.c.rn <= limit_each).order_by(ranked.c.customer_mercos_id, ranked.c.rn)
    ).all()
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        bucket = grouped.setdefault(row.customer_mercos_id, [])
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
    revenue = _money_total(stats.revenue) if stats else 0.0
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
        "aiScore": float(attendance.ai_priority_score) if attendance and attendance.ai_priority_score is not None else None,
        "aiReason": attendance.ai_priority_reason if attendance else None,
    }


def _order_stats_subquery():
    amount = _order_amount()
    return (
        select(
            Order.customer_mercos_id.label("customer_mercos_id"),
            func.count(Order.id).label("orders"),
            func.coalesce(func.sum(amount), 0).label("revenue"),
            func.max(Order.issued_at).label("last_order_at"),
            func.min(Order.issued_at).label("first_order_at"),
            func.max(Order.seller_mercos_id).label("last_seller_id"),
        )
        .where(
            status_sql_in(Order.status, REVENUE_STATUSES),
            Order.customer_mercos_id.is_not(None),
            Order.customer_mercos_id != "",
            amount > 0,
            amount < MAX_ORDER_TOTAL,
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


def _lead_select_stmt(stats_sq):
    return select(
        Customer,
        stats_sq.c.orders,
        stats_sq.c.revenue,
        stats_sq.c.last_order_at,
        stats_sq.c.first_order_at,
        stats_sq.c.last_seller_id,
    ).outerjoin(stats_sq, Customer.mercos_id == stats_sq.c.customer_mercos_id)


def _rows_to_leads(rows, attendances, sellers) -> list[dict]:
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
    return visible


def list_leads(
    db: Session,
    *,
    search: str | None = None,
    top: int = 20,
    queue_page: int = 1,
    queue_page_size: int = 40,
    view: str = "main",
    refresh_ai: bool = False,
) -> dict:
    attendances = _attendances_by_customer(db)
    finished_ids = {
        customer_id for customer_id, row in attendances.items() if row.status == "finished"
    }
    sellers = {row.mercos_id: row.name for row in db.scalars(select(Seller))}
    stats_sq = _order_stats_subquery()
    filters = _customer_filters(finished_ids=finished_ids, search=search)

    in_progress = int(
        db.scalar(
            select(func.count())
            .select_from(CrmAttendance)
            .where(CrmAttendance.status == "in_progress")
        )
        or 0
    )

    page = max(1, queue_page)
    page_size = max(1, min(queue_page_size, 100))
    top_n = max(1, min(top, 50))

    def count_with(extra_filters: list | None = None) -> int:
        stmt = select(func.count(Customer.id)).select_from(Customer).outerjoin(
            stats_sq, Customer.mercos_id == stats_sq.c.customer_mercos_id
        )
        all_filters = [*filters, *(extra_filters or [])]
        if all_filters:
            stmt = stmt.where(*all_filters)
        return int(db.scalar(stmt) or 0)

    new_count = count_with([stats_sq.c.customer_mercos_id.is_(None)])

    if view == "new":
        view_filters = [*filters, stats_sq.c.customer_mercos_id.is_(None)]
        total_count = new_count
        offset = (page - 1) * page_size
        stmt = _lead_select_stmt(stats_sq).where(*view_filters)
        rows = db.execute(
            stmt.order_by(
                Customer.created_at_source.desc().nulls_last(),
                Customer.name.asc(),
            )
            .offset(offset)
            .limit(page_size)
        ).all()
        visible = _rows_to_leads(rows, attendances, sellers)
        loaded = offset + len(visible)
        return {
            "view": "new",
            "count": total_count,
            "newCount": total_count,
            "topCount": 0,
            "top": [],
            "queue": visible,
            "queuePage": page,
            "queuePageSize": page_size,
            "queueTotal": total_count,
            "hasMore": loaded < total_count,
            "inProgress": in_progress,
            "open": max(0, total_count - in_progress),
        }

    if view == "ai":
        from app.services.lead_priority import (
            AI_BATCH_SIZE,
            _heuristic_score_expr,
            pick_buyers_for_ai_scoring,
            score_leads_with_ai,
        )

        total_count = count_with([stats_sq.c.customer_mercos_id.isnot(None)])
        buyer_filters = [*filters, stats_sq.c.customer_mercos_id.isnot(None)]
        if page == 1 or refresh_ai:
            to_score = pick_buyers_for_ai_scoring(
                db,
                finished_ids=finished_ids,
                search=search,
                limit=AI_BATCH_SIZE,
                refresh=refresh_ai,
            )
            if to_score:
                score_leads_with_ai(db, to_score, force=refresh_ai)
                attendances = _attendances_by_customer(db)

        heuristic = _heuristic_score_expr(db, stats_sq)
        offset = (page - 1) * page_size
        stmt = (
            select(
                Customer,
                stats_sq.c.orders,
                stats_sq.c.revenue,
                stats_sq.c.last_order_at,
                stats_sq.c.first_order_at,
                stats_sq.c.last_seller_id,
                CrmAttendance.ai_priority_score,
            )
            .select_from(Customer)
            .outerjoin(stats_sq, Customer.mercos_id == stats_sq.c.customer_mercos_id)
            .outerjoin(CrmAttendance, Customer.mercos_id == CrmAttendance.customer_mercos_id)
            .where(*buyer_filters)
        )
        rows = db.execute(
            stmt.order_by(
                CrmAttendance.ai_priority_score.desc().nulls_last(),
                heuristic.desc(),
                stats_sq.c.revenue.desc().nulls_last(),
                Customer.name.asc(),
            )
            .offset(offset)
            .limit(page_size)
        ).all()

        visible = []
        for customer, orders, revenue, last_order_at, first_order_at, last_seller_id, _ai_score in rows:
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

        ai_scored = int(
            db.scalar(
                select(func.count())
                .select_from(Customer)
                .outerjoin(stats_sq, Customer.mercos_id == stats_sq.c.customer_mercos_id)
                .join(CrmAttendance, Customer.mercos_id == CrmAttendance.customer_mercos_id)
                .where(*buyer_filters, CrmAttendance.ai_priority_score.isnot(None))
            )
            or 0
        )
        loaded = offset + len(visible)
        return {
            "view": "ai",
            "count": total_count,
            "newCount": new_count,
            "topCount": 0,
            "top": [],
            "queue": visible,
            "queuePage": page,
            "queuePageSize": page_size,
            "queueTotal": total_count,
            "hasMore": loaded < total_count,
            "inProgress": in_progress,
            "open": max(0, total_count - in_progress),
            "aiScored": ai_scored,
            "aiPending": max(0, total_count - ai_scored),
        }

    total_count = count_with([stats_sq.c.customer_mercos_id.isnot(None)])
    buyer_filters = [*filters, stats_sq.c.customer_mercos_id.isnot(None)]
    stmt = _lead_select_stmt(stats_sq).where(*buyer_filters)
    order = _lead_priority_order(db, stats_sq)

    top_rows = db.execute(stmt.order_by(*order).limit(top_n)).all()
    queue_offset = top_n + (page - 1) * page_size
    queue_rows = db.execute(
        stmt.order_by(*order).offset(queue_offset).limit(page_size)
    ).all()

    visible_top = _rows_to_leads(top_rows, attendances, sellers)
    visible_queue = _rows_to_leads(queue_rows, attendances, sellers)

    last_products = _last_products_map(db, [row["id"] for row in visible_top], 3)
    for row in visible_top:
        row["lastProducts"] = last_products.get(row["id"], [])

    queue_total = max(0, total_count - top_n)
    loaded_queue = (page - 1) * page_size + len(visible_queue)

    return {
        "view": "main",
        "count": total_count,
        "newCount": new_count,
        "topCount": len(visible_top),
        "top": visible_top,
        "queue": visible_queue,
        "queuePage": page,
        "queuePageSize": page_size,
        "queueTotal": queue_total,
        "hasMore": loaded_queue < queue_total,
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
        return round(
            sum(_money_total(stats_map.get(customer_id).revenue) for customer_id in ids if stats_map.get(customer_id)),
            2,
        )

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
        bucket["revenue"] = round(bucket["revenue"] + (_money_total(stats.revenue) if stats else 0), 2)

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
                "revenue": _money_total(stats_map[row.customer_mercos_id].revenue)
                if stats_map.get(row.customer_mercos_id)
                else 0,
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
        bucket["revenue"] = round(bucket["revenue"] + (_money_total(stats.revenue) if stats else 0), 2)
    return sorted(grouped.values(), key=lambda row: (-row["attendances"], -row["revenue"]))
