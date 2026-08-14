import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import desc, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.analytics import (
    customer_intelligence,
    dashboard,
    dead_stock,
    leads_to_recover,
    orders_insight,
    period_start,
    product_movers,
    rankings,
)
from app.auth import current_user, require_admin
from app.config import settings
from app.database import Base, SessionLocal, db_session, engine
from app.middleware.rate_limit import ApiRateLimitMiddleware
from app.models import Customer, Order, Product, Seller, SyncRun, SyncState
from app.routers.analytics import router as analytics_router
from app.routers.auth import router as auth_router
from app.routers.exports import router as exports_router
from app.schemas.data_quality import DataQualityResponse
from app.services.data_quality import build_data_quality_report
from app.adaptor import keep_adaptor_warm
from app.sync import (
    SYNC_LEASE_TTL,
    SYNC_RESOURCES,
    sync_all,
    sync_catalog_job,
    sync_orders_job,
    sync_resource,
)

log = logging.getLogger("uvicorn.error")
scheduler = AsyncIOScheduler()
_sync_busy = False


@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    cfg = settings()
    log.info("CORS origins: %s", cfg.origins)
    if not cfg.auth_admin_password:
        log.warning("AUTH_ADMIN_PASSWORD missing: interactive login is disabled")
    if cfg.jwt_secret == "change-me-in-production":
        log.warning("JWT_SECRET is using the development default")
    # Never auto-resume Mercos sync on boot — it starves dashboard reads on free Render.
    # User clicks Sincronizar / Primeira carga when they want to sync.
    with SessionLocal() as db:
        interrupted_at = datetime.now(timezone.utc)
        stale_before = interrupted_at - SYNC_LEASE_TTL
        stuck = list(
            db.scalars(
                select(SyncState).where(
                    SyncState.status == "running",
                    (SyncState.heartbeat_at.is_(None))
                    | (SyncState.heartbeat_at < stale_before),
                )
            )
        )
        stale_resources = {state.resource for state in stuck}
        for state in stuck:
            state.status = "interrupted"
            state.error = "Serviço reiniciou durante a sync — use Sincronizar para continuar"
            state.lease_token = None
            db.add(state)
        stuck_runs = (
            list(
                db.scalars(
                    select(SyncRun).where(
                        SyncRun.status == "running",
                        SyncRun.resource.in_(stale_resources),
                    )
                )
            )
            if stale_resources
            else []
        )
        for run in stuck_runs:
            run.status = "interrupted"
            run.finished_at = interrupted_at
            run.error = "Serviço reiniciou durante a sincronização"
            db.add(run)
        if stuck or stuck_runs:
            db.commit()
            log.warning(
                "Marked %s sync state(s) and %s run(s) interrupted (no auto-resume)",
                len(stuck),
                len(stuck_runs),
            )
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
        scheduler.add_job(
            keep_adaptor_warm,
            "interval",
            minutes=8,
            id="keep_adaptor_warm",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        log.info(
            "Scheduler started (orders every %sm, catalog every %sh, adaptor ping every 8m)",
            cfg.sync_orders_minutes,
            cfg.sync_catalog_hours,
        )
    else:
        log.warning("Scheduler disabled: MERCOS_ADAPTOR_URL/API_KEY missing")
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Xnamai BI API", version="1.0.0", lifespan=lifespan)
app.add_middleware(ApiRateLimitMiddleware, requests_per_minute=300)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings().origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.exception_handler(OperationalError)
async def database_timeout_handler(request, exc: OperationalError):
    log.warning("Database statement canceled: %s", exc)
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Consulta excedeu o tempo no banco. Tente um período menor."
        },
    )


auth = current_user
app.include_router(auth_router)
app.include_router(analytics_router, dependencies=[Depends(current_user)])
app.include_router(exports_router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "Xnamai BI API"}


@app.get(
    "/api/v1/data-quality",
    dependencies=[Depends(require_admin)],
    response_model=DataQualityResponse,
    tags=["admin"],
)
def data_quality(db: Session = Depends(db_session)):
    """Read-only coverage and integrity audit; never returns raw PII values."""
    return build_data_quality_report(db)


@app.get("/api/v1/dashboard", dependencies=[Depends(auth)])
def get_dashboard(days: int = Query(30, ge=0, le=3650), db: Session = Depends(db_session)):
    return dashboard(db, days)


@app.get("/api/v1/rankings", dependencies=[Depends(auth)])
def get_rankings(days: int = Query(30, ge=0, le=3650), db: Session = Depends(db_session)):
    return rankings(db, days)


@app.get("/api/v1/intelligence/customers", dependencies=[Depends(auth)])
def get_customer_intelligence(
    inactive_days: int = Query(90, ge=14, le=730),
    risk_days: int = Query(90, ge=7, le=365),
    limit: int = Query(500, ge=1, le=5000),
    segment: str | None = Query(None, description="todos|ativo|em_risco|recuperar|lead_novo"),
    sort: str | None = Query(None, description="name|orders|revenue|ticketAverage|lastOrderAt|daysSinceLastOrder|..."),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    db: Session = Depends(db_session),
):
    return customer_intelligence(
        db,
        inactive_days=inactive_days,
        risk_days=risk_days,
        limit=limit,
        segment=segment,
        sort=sort,
        order=order,
    )


@app.get("/api/v1/intelligence/leads", dependencies=[Depends(auth)])
def get_leads(
    inactive_days: int = Query(90, ge=14, le=730),
    risk_days: int = Query(45, ge=7, le=365),
    limit: int = Query(200, ge=1, le=2000),
    db: Session = Depends(db_session),
):
    return leads_to_recover(db, inactive_days=inactive_days, risk_days=risk_days, limit=limit)


@app.get("/api/v1/intelligence/dead-stock", dependencies=[Depends(auth)])
def get_dead_stock(
    no_sale_days: int = Query(90, ge=14, le=730),
    limit: int = Query(200, ge=1, le=2000),
    db: Session = Depends(db_session),
):
    return dead_stock(db, no_sale_days=no_sale_days, limit=limit)


@app.get("/api/v1/intelligence/product-movers", dependencies=[Depends(auth)])
def get_product_movers(days: int = Query(365, ge=0, le=3650), db: Session = Depends(db_session)):
    return product_movers(db, days=days)


@app.get("/api/v1/orders", dependencies=[Depends(auth)])
def orders(
    limit: int = Query(200, le=2000),
    days: int = Query(0, ge=0, le=3650),
    sort: str | None = Query(None),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    db: Session = Depends(db_session),
):
    customers = {x.mercos_id: x.name for x in db.scalars(select(Customer))}
    sellers = {x.mercos_id: x.name for x in db.scalars(select(Seller))}
    start = period_start(days)
    q = select(Order).where(Order.issued_at.is_not(None))
    if start is not None:
        q = q.where(Order.issued_at >= start)
    sort_map = {
        "number": Order.number,
        "status": Order.status,
        "date": Order.issued_at,
        "total": Order.total,
    }
    col = sort_map.get(sort or "date", Order.issued_at)
    q = q.order_by(col.asc() if order == "asc" else col.desc())
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
        for x in db.scalars(q.limit(limit))
    ]


@app.get("/api/v1/orders/insight", dependencies=[Depends(auth)])
def get_orders_insight(
    days: int = Query(30, ge=0, le=3650),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(db_session),
):
    return orders_insight(db, days=days, limit=limit)


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


@app.get("/api/v1/sync/runs", dependencies=[Depends(require_admin)])
def sync_runs(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    resource: str | None = Query(None),
    db: Session = Depends(db_session),
):
    filters = [SyncRun.resource == resource] if resource else []
    total = int(
        db.scalar(select(func.count(SyncRun.id)).where(*filters)) or 0
    )
    rows = db.scalars(
        select(SyncRun)
        .where(*filters)
        .order_by(desc(SyncRun.started_at), desc(SyncRun.id))
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [
            {
                "id": row.id,
                "resource": row.resource,
                "mode": row.mode,
                "status": row.status,
                "startedAt": row.started_at,
                "finishedAt": row.finished_at,
                "cursorBefore": row.cursor_before,
                "cursorAfter": row.cursor_after,
                "pages": row.pages,
                "received": row.received,
                "persisted": row.persisted,
                "failed": row.failed,
                "details": row.details,
                "error": row.error,
            }
            for row in rows
        ],
        "page": page,
        "pageSize": page_size,
        "totalItems": total,
        "totalPages": (total + page_size - 1) // page_size,
        "sort": "startedAt",
        "order": "desc",
        "appliedFilters": {"resource": resource} if resource else {},
    }


@app.post("/api/v1/sync/{resource}", dependencies=[Depends(require_admin)])
async def run_sync(resource: str, background_tasks: BackgroundTasks, full: bool = False):
    global _sync_busy
    if resource != "all" and resource not in SYNC_RESOURCES:
        raise HTTPException(404, "Recurso inválido")

    with SessionLocal() as db:
        running = any(x.status == "running" for x in db.scalars(select(SyncState)))
    if _sync_busy or running:
        return JSONResponse(
            status_code=202,
            content={"status": "running", "message": "Sync já em andamento", "resource": resource, "full": full},
        )

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
