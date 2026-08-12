from datetime import datetime, timedelta, timezone
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.models import Customer, Order, OrderItem, Product, Seller, SyncState

CANCELLED={"5","cancelled","cancelado"}; DELIVERED={"4","delivered","entregue"}
def period(days): return datetime.now(timezone.utc)-timedelta(days=days)
def dashboard(db:Session,days=30):
    start=period(days); prev=start-timedelta(days=days)
    def totals(a,b):
        rows=db.scalars(select(Order).where(Order.issued_at>=a,Order.issued_at<b)).all(); valid=[x for x in rows if x.status.lower() not in CANCELLED]
        return len(valid),sum(x.total for x in valid),len([x for x in rows if x.status.lower() in CANCELLED])
    count,revenue,cancelled=totals(start,datetime.now(timezone.utc)); pc,pr,_=totals(prev,start)
    pct=lambda a,b: round((a-b)/b*100,1) if b else (100 if a else 0)
    daily=db.execute(select(func.date(Order.issued_at),func.count(Order.id),func.sum(Order.total)).where(Order.issued_at>=start,~Order.status.in_(CANCELLED)).group_by(func.date(Order.issued_at)).order_by(func.date(Order.issued_at))).all()
    statuses=db.execute(select(Order.status,func.count(Order.id),func.sum(Order.total)).where(Order.issued_at>=start).group_by(Order.status)).all()
    return {"periodDays":days,"kpis":{"revenue":round(revenue,2),"revenueChange":pct(revenue,pr),"orders":count,"ordersChange":pct(count,pc),"ticketAverage":round(revenue/count,2) if count else 0,"customers":db.scalar(select(func.count(Customer.id))) or 0,"cancellations":cancelled},"salesEvolution":[{"date":str(d),"orders":c,"revenue":round(v or 0,2)} for d,c,v in daily],"status":[{"status":s,"orders":c,"value":round(v or 0,2)} for s,c,v in statuses]}
def rankings(db:Session,days=30):
    start=period(days)
    products=db.execute(select(OrderItem.name,func.sum(OrderItem.quantity),func.sum(OrderItem.total)).join(Order,Order.mercos_id==OrderItem.order_mercos_id).where(Order.issued_at>=start,~Order.status.in_(CANCELLED)).group_by(OrderItem.name).order_by(func.sum(OrderItem.total).desc()).limit(10)).all()
    customers=db.execute(select(Customer.name,func.count(Order.id),func.sum(Order.total)).join(Order,Order.customer_mercos_id==Customer.mercos_id).where(Order.issued_at>=start,~Order.status.in_(CANCELLED)).group_by(Customer.name).order_by(func.sum(Order.total).desc()).limit(10)).all()
    sellers=db.execute(select(Seller.name,func.count(Order.id),func.sum(Order.total)).join(Order,Order.seller_mercos_id==Seller.mercos_id).where(Order.issued_at>=start,~Order.status.in_(CANCELLED)).group_by(Seller.name).order_by(func.sum(Order.total).desc()).limit(10)).all()
    return {"products":[{"name":n,"quantity":q or 0,"revenue":v or 0} for n,q,v in products],"customers":[{"name":n,"orders":q,"revenue":v or 0} for n,q,v in customers],"sellers":[{"name":n,"orders":q,"revenue":v or 0} for n,q,v in sellers]}

