import logging
import secrets
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.analytics import dashboard, rankings
from app.config import settings
from app.database import Base, SessionLocal, db_session, engine
from app.models import Customer, Order, Product, Seller, SyncState
from app.sync import sync_all, sync_catalog_job, sync_orders_job, sync_resource

log = logging.getLogger("uvicorn.error")
scheduler = AsyncIOScheduler()
_sync_busy = False


@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    cfg = settings()
    log.info("CORS origins: %s", cfg.origins)
    if cfg.mercos_adaptor_url and cfg.mercos_adaptor_api_key:
        scheduler.add_job(
            sync_orders_job,
            "interval",
            minutes=max(1, cfg.sync_orders_minutes),
            id="sync_orders",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            sync_catalog_job,
            "interval",
            hours=max(1, cfg.sync_catalog_hours),
            id="sync_catalog",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        log.info(
            "Scheduler started (orders every %sm, catalog every %sh)",
            cfg.sync_orders_minutes,
            cfg.sync_catalog_hours,
        )
    else:
        log.warning("Scheduler disabled: MERCOS_ADAPTOR_URL/API_KEY missing")
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Xnamai BI API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings().origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def auth(x_api_key: str | None = Header(None)):
    expected = settings().bi_api_key
    if not x_api_key or len(x_api_key) != len(expected) or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(401, "Chave inválida")


@app.get("/health")
def health():
    return {"status": "ok", "service": "Xnamai BI API"}


@app.get("/api/v1/dashboard", dependencies=[Depends(auth)])
def get_dashboard(days: int = Query(30, ge=1, le=730), db: Session = Depends(db_session)):
    return dashboard(db, days)


@app.get("/api/v1/rankings", dependencies=[Depends(auth)])
def get_rankings(days: int = Query(30, ge=1, le=730), db: Session = Depends(db_session)):
    return rankings(db, days)


@app.get("/api/v1/orders", dependencies=[Depends(auth)])
def orders(limit: int = Query(100, le=500), db: Session = Depends(db_session)):
    customers = {x.mercos_id: x.name for x in db.scalars(select(Customer))}
    sellers = {x.mercos_id: x.name for x in db.scalars(select(Seller))}
    return [
        {
            "id": x.mercos_id,
            "number": x.number,
            "customerId": x.customer_mercos_id,
            "customerName": customers.get(x.customer_mercos_id) or x.customer_mercos_id or "—",
            "sellerId": x.seller_mercos_id,
            "sellerName": sellers.get(x.seller_mercos_id) or x.seller_mercos_id or "—",
            "status": x.status,
            "date": x.issued_at,
            "total": x.total,
        }
        for x in db.scalars(select(Order).order_by(desc(Order.issued_at)).limit(limit))
    ]


@app.get("/api/v1/products", dependencies=[Depends(auth)])
def products(limit: int = Query(100, le=500), db: Session = Depends(db_session)):
    return [
        {
            "id": x.mercos_id,
            "code": x.code,
            "name": x.name,
            "stock": x.stock,
            "price": x.list_price,
            "active": x.active,
        }
        for x in db.scalars(select(Product).limit(limit))
    ]


@app.get("/api/v1/customers", dependencies=[Depends(auth)])
def customers(limit: int = Query(100, le=500), db: Session = Depends(db_session)):
    return [
        {
            "id": x.mercos_id,
            "name": x.name,
            "city": x.city,
            "state": x.state,
            "email": x.email,
            "phone": x.phone,
        }
        for x in db.scalars(select(Customer).limit(limit))
    ]


@app.get("/api/v1/sellers", dependencies=[Depends(auth)])
def sellers(db: Session = Depends(db_session)):
    return [{"id": x.mercos_id, "name": x.name, "active": x.active} for x in db.scalars(select(Seller))]


@app.get("/api/v1/sync/status", dependencies=[Depends(auth)])
def sync_status(db: Session = Depends(db_session)):
    return [
        {
            "resource": x.resource,
            "status": x.status,
            "cursor": x.cursor,
            "lastSuccessAt": x.last_success_at,
            "records": x.records,
            "error": x.error,
        }
        for x in db.scalars(select(SyncState))
    ]


@app.post("/api/v1/sync/{resource}", dependencies=[Depends(auth)])
async def run_sync(resource: str, background_tasks: BackgroundTasks, full: bool = False):
    global _sync_busy
    if resource not in {"all", "customers", "products", "users", "orders"}:
        raise HTTPException(404, "Recurso inválido")

    targets = ("customers", "products", "users", "orders") if resource == "all" else (resource,)
    with SessionLocal() as db:
        running = any(x.status == "running" for x in db.scalars(select(SyncState)))
        if _sync_busy or running:
            return JSONResponse(
                status_code=202,
                content={"status": "running", "message": "Sync já em andamento", "resource": resource, "full": full},
            )
        for name in targets:
            state = db.get(SyncState, name) or SyncState(resource=name)
            state.status = "running"
            state.error = None
            db.add(state)
        db.commit()

    _sync_busy = True

    async def _job():
        global _sync_busy
        try:
            if resource == "all":
                await sync_all(full, raise_http=False)
            else:
                await sync_resource(resource, full, raise_http=False)
        finally:
            _sync_busy = False

    background_tasks.add_task(_job)
    return JSONResponse(
        status_code=202,
        content={
            "status": "started",
            "message": "Sync iniciada em background. Acompanhe em /api/v1/sync/status",
            "resource": resource,
            "full": full,
        },
    )
