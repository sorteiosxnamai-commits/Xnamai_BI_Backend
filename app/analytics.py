from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Customer, Order, OrderItem, Product, Seller

CANCELLED = {"5", "cancelled", "cancelado"}


def _now():
    return datetime.now(timezone.utc)


def period_start(days: int | None):
    if not days or days <= 0:
        return None
    return _now() - timedelta(days=days)


def _valid_orders(days: int | None = None):
    q = select(Order).where(~func.lower(Order.status).in_(CANCELLED))
    start = period_start(days)
    if start is not None:
        q = q.where(Order.issued_at >= start)
    return q


def dashboard(db: Session, days: int = 30):
    start = period_start(days) or (_now() - timedelta(days=30))
    prev = start - ( _now() - start )

    def totals(a, b):
        rows = db.scalars(select(Order).where(Order.issued_at >= a, Order.issued_at < b)).all()
        valid = [x for x in rows if (x.status or "").lower() not in CANCELLED]
        return len(valid), sum(x.total for x in valid), len([x for x in rows if (x.status or "").lower() in CANCELLED])

    count, revenue, cancelled = totals(start, _now())
    pc, pr, _ = totals(prev, start)
    pct = lambda a, b: round((a - b) / b * 100, 1) if b else (100.0 if a else 0.0)
    buyers = db.scalar(
        select(func.count(func.distinct(Order.customer_mercos_id))).where(
            Order.issued_at >= start,
            ~func.lower(Order.status).in_(CANCELLED),
            Order.customer_mercos_id.is_not(None),
        )
    ) or 0
    daily = db.execute(
        select(func.date(Order.issued_at), func.count(Order.id), func.sum(Order.total))
        .where(Order.issued_at >= start, ~func.lower(Order.status).in_(CANCELLED))
        .group_by(func.date(Order.issued_at))
        .order_by(func.date(Order.issued_at))
    ).all()
    statuses = db.execute(
        select(Order.status, func.count(Order.id), func.sum(Order.total))
        .where(Order.issued_at >= start)
        .group_by(Order.status)
    ).all()
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
        .where(~func.lower(Order.status).in_(CANCELLED))
        .group_by(OrderItem.name)
        .order_by(func.sum(OrderItem.total).desc())
        .limit(limit)
    )
    customer_q = (
        select(Customer.name, func.count(Order.id), func.sum(Order.total))
        .join(Order, Order.customer_mercos_id == Customer.mercos_id)
        .where(~func.lower(Order.status).in_(CANCELLED))
        .group_by(Customer.name)
        .order_by(func.sum(Order.total).desc())
        .limit(limit)
    )
    seller_q = (
        select(Seller.name, func.count(Order.id), func.sum(Order.total))
        .join(Order, Order.seller_mercos_id == Seller.mercos_id)
        .where(~func.lower(Order.status).in_(CANCELLED))
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
                ~func.lower(Order.status).in_(CANCELLED),
                Order.customer_mercos_id.is_not(None),
            )
            .group_by(Order.customer_mercos_id)
        ).all()
    }


def classify_customer(stats, *, inactive_days: int, risk_days: int, now: datetime) -> str:
    if stats is None or not stats.orders:
        return "lead_novo"
    last = stats.last_order_at
    if last is None:
        return "lead_novo"
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    days_since = (now - last).days
    if days_since <= risk_days:
        return "ativo"
    if days_since <= inactive_days:
        return "em_risco"
    return "recuperar"


def customer_intelligence(db: Session, *, inactive_days: int = 90, risk_days: int = 45, limit: int = 500):
    now = _now()
    stats_map = _customer_order_stats(db)
    customers = db.scalars(select(Customer)).all()
    rows = []
    summary = {"ativo": 0, "em_risco": 0, "recuperar": 0, "lead_novo": 0}

    for c in customers:
        st = stats_map.get(c.mercos_id)
        segment = classify_customer(st, inactive_days=inactive_days, risk_days=risk_days, now=now)
        summary[segment] = summary.get(segment, 0) + 1
        last = st.last_order_at if st else None
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        days_since = (now - last).days if last else None
        orders = int(st.orders) if st else 0
        revenue = float(st.revenue) if st else 0.0
        potential = "alto" if segment == "recuperar" and revenue >= 5000 else (
            "medio" if segment in {"em_risco", "recuperar"} and revenue >= 1000 else (
                "descobrir" if segment == "lead_novo" else "manter"
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
                "segment": segment,
                "potential": potential,
                "orders": orders,
                "revenue": round(revenue, 2),
                "ticketAverage": round(revenue / orders, 2) if orders else 0,
                "firstOrderAt": st.first_order_at.isoformat() if st and st.first_order_at else None,
                "lastOrderAt": last.isoformat() if last else None,
                "daysSinceLastOrder": days_since,
            }
        )

    # Prioritize recover / risk / revenue for the matrix view
    weight = {"recuperar": 0, "em_risco": 1, "lead_novo": 2, "ativo": 3}
    rows.sort(key=lambda r: (weight.get(r["segment"], 9), -(r["revenue"] or 0), r["name"] or ""))
    return {
        "inactiveDays": inactive_days,
        "riskDays": risk_days,
        "summary": summary,
        "total": len(rows),
        "customers": rows[:limit],
    }


def leads_to_recover(db: Session, *, inactive_days: int = 90, risk_days: int = 45, limit: int = 200):
    data = customer_intelligence(db, inactive_days=inactive_days, risk_days=risk_days, limit=10_000)
    leads = [c for c in data["customers"] if c["segment"] in {"recuperar", "em_risco"}]
    leads.sort(key=lambda r: (-(r["revenue"] or 0), -(r["daysSinceLastOrder"] or 0)))
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
            ~func.lower(Order.status).in_(CANCELLED),
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
                ~func.lower(Order.status).in_(CANCELLED),
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
        .where(~func.lower(Order.status).in_(CANCELLED))
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
