from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.models import Customer, Order, OrderItem, Product, Seller

# Mercos v2: 0=Cancelado, 1=Orçamento, 2=Pedido. Also keep legacy text/codes.
CANCELLED = {"0", "5", "cancelled", "cancelado"}
# Orçamentos e cancelados fora do faturamento
NON_REVENUE = CANCELLED | {"1", "orcamento", "orçamento", "budget", "quote"}
# Só pedidos confirmados entram em faturamento de clientes (evita lixo de orçamento/cancelado)
REVENUE_STATUSES = {"2", "pedido", "order"}
# Totais absurdos (dado sujo) não entram na soma
MAX_ORDER_TOTAL = 500_000.0


def _now():
    return datetime.now(timezone.utc)


def period_start(days: int | None):
    if not days or days <= 0:
        return None
    return _now() - timedelta(days=days)


def _status_key(value) -> str:
    return (str(value) if value is not None else "").lower()


def _is_revenue_status(status) -> bool:
    return _status_key(status) in REVENUE_STATUSES


def _valid_orders(days: int | None = None):
    q = select(Order).where(
        func.lower(Order.status).in_(REVENUE_STATUSES),
        Order.total > 0,
        Order.total < MAX_ORDER_TOTAL,
    )
    start = period_start(days)
    if start is not None:
        q = q.where(Order.issued_at >= start)
    return q


def dashboard(db: Session, days: int = 30):
    """days<=0 = all-time (no date filter)."""
    start = period_start(days)
    end = _now()

    def totals(a, b):
        q = select(Order).where(Order.issued_at.is_not(None))
        if a is not None:
            q = q.where(Order.issued_at >= a)
        if b is not None:
            q = q.where(Order.issued_at < b)
        rows = db.scalars(q).all()
        valid = [x for x in rows if _is_revenue_status(x.status)]
        cancelled = [x for x in rows if _status_key(x.status) in CANCELLED]
        return len(valid), sum(x.total or 0 for x in valid), len(cancelled)

    if start is None:
        count, revenue, cancelled = totals(None, None)
        pc, pr = 0, 0
        date_filter = []
    else:
        prev = start - (end - start)
        count, revenue, cancelled = totals(start, end)
        pc, pr, _ = totals(prev, start)
        date_filter = [Order.issued_at >= start]

    pct = lambda a, b: round((a - b) / b * 100, 1) if b else (100.0 if a else 0.0)
    buyers = db.scalar(
        select(func.count(func.distinct(Order.customer_mercos_id))).where(
            Order.issued_at.is_not(None),
            func.lower(Order.status).in_(REVENUE_STATUSES),
            Order.customer_mercos_id.is_not(None),
            *date_filter,
        )
    ) or 0
    daily_q = (
        select(func.date(Order.issued_at), func.count(Order.id), func.sum(Order.total))
        .where(Order.issued_at.is_not(None), func.lower(Order.status).in_(REVENUE_STATUSES), *date_filter)
        .group_by(func.date(Order.issued_at))
        .order_by(func.date(Order.issued_at))
    )
    daily = db.execute(daily_q).all()
    status_q = (
        select(Order.status, func.count(Order.id), func.sum(Order.total))
        .where(Order.issued_at.is_not(None), *date_filter)
        .group_by(Order.status)
    )
    statuses = db.execute(status_q).all()
    return {
        "periodDays": days,
        "kpis": {
            "revenue": round(revenue, 2),
            "revenueChange": pct(revenue, pr),
            "orders": count,
            "ordersChange": pct(count, pc),
            "ticketAverage": round(revenue / count, 2) if count else 0,
            "customers": buyers,
            "customersTotal": db.scalar(select(func.count(Customer.id))) or 0,
            "cancellations": cancelled,
        },
        "salesEvolution": [{"date": str(d), "orders": c, "revenue": round(v or 0, 2)} for d, c, v in daily],
        "status": [{"status": s, "orders": c, "value": round(v or 0, 2)} for s, c, v in statuses],
    }


def rankings(db: Session, days: int = 30, limit: int = 10):
    start = period_start(days)
    product_q = (
        select(OrderItem.name, func.sum(OrderItem.quantity), func.sum(OrderItem.total))
        .join(Order, Order.mercos_id == OrderItem.order_mercos_id)
        .where(func.lower(Order.status).in_(REVENUE_STATUSES))
        .group_by(OrderItem.name)
        .order_by(func.sum(OrderItem.total).desc())
        .limit(limit)
    )
    customer_q = (
        select(Customer.name, func.count(Order.id), func.sum(Order.total))
        .join(Order, Order.customer_mercos_id == Customer.mercos_id)
        .where(func.lower(Order.status).in_(REVENUE_STATUSES))
        .group_by(Customer.name)
        .order_by(func.sum(Order.total).desc())
        .limit(limit)
    )
    seller_q = (
        select(Seller.name, func.count(Order.id), func.sum(Order.total))
        .join(Order, Order.seller_mercos_id == Seller.mercos_id)
        .where(func.lower(Order.status).in_(REVENUE_STATUSES))
        .group_by(Seller.name)
        .order_by(func.sum(Order.total).desc())
        .limit(limit)
    )
    if start is not None:
        product_q = product_q.where(Order.issued_at >= start)
        customer_q = customer_q.where(Order.issued_at >= start)
        seller_q = seller_q.where(Order.issued_at >= start)
    products = db.execute(product_q).all()
    customers = db.execute(customer_q).all()
    sellers = db.execute(seller_q).all()
    return {
        "products": [{"name": n, "quantity": q or 0, "revenue": float(v or 0)} for n, q, v in products],
        "customers": [{"name": n, "orders": q, "revenue": float(v or 0)} for n, q, v in customers],
        "sellers": [{"name": n, "orders": q, "revenue": float(v or 0)} for n, q, v in sellers],
    }


def _customer_order_stats(db: Session):
    return {
        row.customer_mercos_id: row
        for row in db.execute(
            select(
                Order.customer_mercos_id.label("customer_mercos_id"),
                func.count(Order.id).label("orders"),
                func.coalesce(func.sum(Order.total), 0).label("revenue"),
                func.max(Order.issued_at).label("last_order_at"),
                func.min(Order.issued_at).label("first_order_at"),
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
    }


def _aware(dt_value):
    if dt_value is None:
        return None
    if dt_value.tzinfo is None:
        return dt_value.replace(tzinfo=timezone.utc)
    return dt_value


def classify_customer(stats, *, inactive_days: int, risk_days: int, now: datetime) -> str:
    """
    Ativo: comprou nos últimos 3 meses (`inactive_days`).
    Em risco: sem compra há pelo menos 3 meses (91+ dias).
    Recuperar: parado há mais de 6 meses (2× inactive_days) — foco de recuperação.
    Lead novo: sem pedidos, ou primeiros pedidos recentes.
    """
    _ = risk_days
    if stats is None or not stats.orders:
        return "lead_novo"
    first = _aware(stats.first_order_at)
    last = _aware(stats.last_order_at)
    if last is None:
        return "lead_novo"
    days_since = (now - last).days
    days_since_first = (now - first).days if first else days_since
    if days_since_first <= inactive_days and int(stats.orders) <= 5:
        return "lead_novo"
    if days_since <= inactive_days:
        return "ativo"
    # 91+ → em risco; >180 → recuperar (ainda entra em “não compra há 3+ meses”)
    if days_since > inactive_days * 2:
        return "recuperar"
    return "em_risco"


def _sort_customers(rows: list, segment: str | None, sort: str | None, order: str):
    reverse = (order or "asc").lower() == "desc"
    key = (sort or "").strip()

    def by_num(field, default=0):
        return lambda r: (r.get(field) is None, r.get(field) if r.get(field) is not None else default)

    def by_str(field):
        return lambda r: (r.get(field) or "").lower()

    sorters = {
        "name": by_str("name"),
        "segment": by_str("segment"),
        "potential": by_str("potential"),
        "orders": by_num("orders", 0),
        "revenue": by_num("revenue", 0),
        "ticketAverage": by_num("ticketAverage", 0),
        "lastOrderAt": by_str("lastOrderAt"),
        "firstOrderAt": by_str("firstOrderAt"),
        "daysSinceLastOrder": by_num("daysSinceLastOrder", 10**9),
        "email": lambda r: (r.get("email") or r.get("phone") or "").lower(),
    }
    if key in sorters:
        rows.sort(key=sorters[key], reverse=reverse)
        return rows

    # Defaults por aba
    if segment == "ativo":
        rows.sort(key=lambda r: r.get("lastOrderAt") or "", reverse=True)
    elif segment == "em_risco":
        rows.sort(key=lambda r: r.get("daysSinceLastOrder") if r.get("daysSinceLastOrder") is not None else 10**9)
    elif segment == "recuperar":
        rows.sort(
            key=lambda r: (
                -(r.get("revenue") or 0),
                r.get("daysSinceLastOrder") if r.get("daysSinceLastOrder") is not None else 10**9,
            )
        )
    elif segment == "lead_novo":
        with_orders = [r for r in rows if (r.get("orders") or 0) > 0]
        without = [r for r in rows if (r.get("orders") or 0) <= 0]
        with_orders.sort(key=lambda r: r.get("firstOrderAt") or r.get("lastOrderAt") or "", reverse=True)
        without.sort(key=lambda r: (r.get("name") or "").lower())
        rows[:] = with_orders + without
    else:
        weight = {"ativo": 0, "lead_novo": 1, "em_risco": 2, "recuperar": 3}
        rows.sort(
            key=lambda r: (
                weight.get(r["segment"], 9),
                -(r.get("revenue") or 0),
                r.get("daysSinceLastOrder") if r.get("daysSinceLastOrder") is not None else 10**9,
            )
        )
    return rows


def customer_intelligence(
    db: Session,
    *,
    inactive_days: int = 90,
    risk_days: int = 90,
    limit: int = 500,
    segment: str | None = None,
    sort: str | None = None,
    order: str = "asc",
):
    now = _now()
    stats_map = _customer_order_stats(db)
    customers = db.scalars(select(Customer)).all()
    rows = []
    summary = {"ativo": 0, "em_risco": 0, "recuperar": 0, "lead_novo": 0}
    seg_filter = (segment or "").strip().lower() or None
    if seg_filter in {"", "todos", "all"}:
        seg_filter = None

    for c in customers:
        st = stats_map.get(c.mercos_id)
        seg = classify_customer(st, inactive_days=inactive_days, risk_days=risk_days, now=now)
        summary[seg] = summary.get(seg, 0) + 1
        # Aba "Em risco" = todos sem compra há 3+ meses (inclui Recuperar)
        if seg_filter == "em_risco":
            if seg not in {"em_risco", "recuperar"}:
                continue
        elif seg_filter and seg != seg_filter:
            continue
        last = _aware(st.last_order_at) if st else None
        first = _aware(st.first_order_at) if st else None
        days_since = (now - last).days if last else None
        orders = int(st.orders) if st else 0
        revenue = float(st.revenue) if st else 0.0
        # Soma inválida (outliers) → zera faturamento exibido
        if orders and revenue / orders > MAX_ORDER_TOTAL:
            revenue = 0.0
        if revenue < 0:
            revenue = 0.0
        potential = (
            "alto"
            if seg == "recuperar" and revenue >= 5000
            else (
                "medio"
                if seg in {"em_risco", "recuperar"} and revenue >= 1000
                else ("descobrir" if seg == "lead_novo" else "manter")
            )
        )
        rows.append(
            {
                "id": c.mercos_id,
                "name": c.name,
                "city": c.city,
                "state": c.state,
                "email": c.email,
                "phone": c.phone,
                "segment": seg,
                "potential": potential,
                "orders": orders,
                "revenue": round(revenue, 2),
                "ticketAverage": round(revenue / orders, 2) if orders else 0,
                "firstOrderAt": first.isoformat() if first else None,
                "lastOrderAt": last.isoformat() if last else None,
                "daysSinceLastOrder": days_since,
            }
        )

    # Em risco: ordenar 91, 92, … mesmo quando inclui “recuperar”
    sort_segment = "em_risco" if seg_filter == "em_risco" else seg_filter
    _sort_customers(rows, sort_segment, sort, order)
    return {
        "inactiveDays": inactive_days,
        "riskDays": risk_days,
        "segment": seg_filter or "todos",
        "sort": sort,
        "order": order,
        "summary": summary,
        "total": sum(summary.values()),
        "matched": len(rows),
        "customers": rows[:limit],
    }


def leads_to_recover(db: Session, *, inactive_days: int = 90, risk_days: int = 90, limit: int = 200):
    data = customer_intelligence(
        db,
        inactive_days=inactive_days,
        risk_days=risk_days,
        limit=10_000,
        segment=None,
    )
    leads = [c for c in data["customers"] if c["segment"] in {"recuperar", "em_risco"}]
    leads.sort(key=lambda r: (r.get("daysSinceLastOrder") if r.get("daysSinceLastOrder") is not None else 10**9, -(r["revenue"] or 0)))
    return {
        "inactiveDays": inactive_days,
        "riskDays": risk_days,
        "count": len(leads),
        "leads": leads[:limit],
    }


def dead_stock(db: Session, *, no_sale_days: int = 90, limit: int = 200):
    start = period_start(no_sale_days)
    sold_q = (
        select(func.distinct(OrderItem.product_mercos_id))
        .join(Order, Order.mercos_id == OrderItem.order_mercos_id)
        .where(
            func.lower(Order.status).in_(REVENUE_STATUSES),
            OrderItem.product_mercos_id.is_not(None),
        )
    )
    if start is not None:
        sold_q = sold_q.where(Order.issued_at >= start)
    sold_ids = set(db.scalars(sold_q).all())
    products = db.scalars(select(Product).where(Product.stock > 0, Product.active.is_(True))).all()
    last_sale = {
        row.product_mercos_id: row.last_sale_at
        for row in db.execute(
            select(
                OrderItem.product_mercos_id.label("product_mercos_id"),
                func.max(Order.issued_at).label("last_sale_at"),
            )
            .join(Order, Order.mercos_id == OrderItem.order_mercos_id)
            .where(
                func.lower(Order.status).in_(REVENUE_STATUSES),
                OrderItem.product_mercos_id.is_not(None),
            )
            .group_by(OrderItem.product_mercos_id)
        ).all()
    }
    now = _now()
    rows = []
    for p in products:
        if p.mercos_id in sold_ids:
            continue
        last = last_sale.get(p.mercos_id)
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        days_since = (now - last).days if last else None
        stock_value = float(p.stock or 0) * float(p.list_price or 0)
        rows.append(
            {
                "id": p.mercos_id,
                "code": p.code,
                "name": p.name,
                "stock": float(p.stock or 0),
                "price": float(p.list_price or 0),
                "stockValue": round(stock_value, 2),
                "lastSaleAt": last.isoformat() if last else None,
                "daysSinceLastSale": days_since,
                "neverSold": last is None,
            }
        )
    rows.sort(key=lambda r: (-(r["stockValue"] or 0), -(r["stock"] or 0)))
    return {
        "noSaleDays": no_sale_days,
        "count": len(rows),
        "totalStockValue": round(sum(r["stockValue"] for r in rows), 2),
        "products": rows[:limit],
    }


def product_movers(db: Session, *, days: int = 365, limit: int = 20):
    """Top sellers and bottom sellers (with stock) in the window."""
    start = period_start(days)
    q = (
        select(
            OrderItem.product_mercos_id,
            OrderItem.name,
            OrderItem.code,
            func.sum(OrderItem.quantity).label("qty"),
            func.sum(OrderItem.total).label("revenue"),
        )
        .join(Order, Order.mercos_id == OrderItem.order_mercos_id)
        .where(func.lower(Order.status).in_(REVENUE_STATUSES))
        .group_by(OrderItem.product_mercos_id, OrderItem.name, OrderItem.code)
    )
    if start is not None:
        q = q.where(Order.issued_at >= start)
    sold = db.execute(q.order_by(func.sum(OrderItem.total).desc())).all()
    top = [
        {"id": pid, "name": name, "code": code, "quantity": float(qty or 0), "revenue": float(rev or 0)}
        for pid, name, code, qty, rev in sold[:limit]
    ]
    bottom = [
        {"id": pid, "name": name, "code": code, "quantity": float(qty or 0), "revenue": float(rev or 0)}
        for pid, name, code, qty, rev in list(reversed(sold[-limit:])) if sold
    ]
    return {"periodDays": days, "top": top, "slow": bottom}


def orders_insight(db: Session, *, days: int = 30, limit: int = 10):
    """Macros e rankings de pedidos no período."""
    start = period_start(days)
    q = select(Order).where(
        Order.issued_at.is_not(None),
        func.lower(Order.status).in_(REVENUE_STATUSES),
        Order.total > 0,
        Order.total < MAX_ORDER_TOTAL,
    )
    if start is not None:
        q = q.where(Order.issued_at >= start)
    rows = list(db.scalars(q.order_by(desc(Order.total))).all())
    customers = {c.mercos_id: c.name for c in db.scalars(select(Customer)).all()}
    sellers = {s.mercos_id: s.name for s in db.scalars(select(Seller)).all()}

    def pack(o: Order):
        return {
            "id": o.mercos_id,
            "number": o.number,
            "customerId": o.customer_mercos_id,
            "customerName": customers.get(o.customer_mercos_id) or o.customer_mercos_id or "—",
            "sellerId": o.seller_mercos_id,
            "sellerName": sellers.get(o.seller_mercos_id) or o.seller_mercos_id or "—",
            "status": o.status,
            "date": o.issued_at.isoformat() if o.issued_at else None,
            "total": float(o.total or 0),
        }

    count = len(rows)
    revenue = sum(float(o.total or 0) for o in rows)
    totals = [float(o.total or 0) for o in rows]
    biggest = [pack(o) for o in rows[:limit]]
    smallest = [pack(o) for o in sorted(rows, key=lambda x: float(x.total or 0))[:limit]]

    by_customer: dict[str, dict] = {}
    for o in rows:
        cid = o.customer_mercos_id or ""
        if not cid:
            continue
        slot = by_customer.setdefault(cid, {"orders": 0, "revenue": 0.0, "last": o.issued_at})
        slot["orders"] += 1
        slot["revenue"] += float(o.total or 0)
        if o.issued_at and (slot["last"] is None or o.issued_at > slot["last"]):
            slot["last"] = o.issued_at

    top_by_orders = sorted(by_customer.items(), key=lambda kv: (-kv[1]["orders"], -kv[1]["revenue"]))[:limit]
    top_by_revenue = sorted(by_customer.items(), key=lambda kv: (-kv[1]["revenue"], -kv[1]["orders"]))[:limit]

    # Clientes da base que há mais tempo não pedem (com pelo menos 1 pedido histórico)
    now = _now()
    last_map = {
        row.customer_mercos_id: row.last_at
        for row in db.execute(
            select(Order.customer_mercos_id, func.max(Order.issued_at).label("last_at"))
            .where(
                func.lower(Order.status).in_(REVENUE_STATUSES),
                Order.customer_mercos_id.is_not(None),
                Order.customer_mercos_id != "",
                Order.issued_at.is_not(None),
            )
            .group_by(Order.customer_mercos_id)
        ).all()
    }
    idle = []
    for cid, last in last_map.items():
        last_a = _aware(last)
        if not last_a:
            continue
        idle.append(
            {
                "id": cid,
                "name": customers.get(cid) or cid,
                "lastOrderAt": last_a.isoformat(),
                "daysSinceLastOrder": (now - last_a).days,
            }
        )
    idle.sort(key=lambda r: -(r["daysSinceLastOrder"] or 0))

    return {
        "periodDays": days,
        "kpis": {
            "orders": count,
            "revenue": round(revenue, 2),
            "ticketAverage": round(revenue / count, 2) if count else 0,
            "maxOrder": round(max(totals), 2) if totals else 0,
            "minOrder": round(min(totals), 2) if totals else 0,
        },
        "biggestOrders": biggest,
        "smallestOrders": smallest,
        "topCustomersByOrders": [
            {
                "id": cid,
                "name": customers.get(cid) or cid,
                "orders": data["orders"],
                "revenue": round(data["revenue"], 2),
            }
            for cid, data in top_by_orders
        ],
        "topCustomersByRevenue": [
            {
                "id": cid,
                "name": customers.get(cid) or cid,
                "orders": data["orders"],
                "revenue": round(data["revenue"], 2),
            }
            for cid, data in top_by_revenue
        ],
        "idleCustomers": idle[:limit],
    }
