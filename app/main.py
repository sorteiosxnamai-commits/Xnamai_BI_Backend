import secrets
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import desc, select
from sqlalchemy.orm import Session
from app.analytics import dashboard, rankings
from app.config import settings
from app.database import Base, db_session, engine
from app.models import Customer, Order, Product, Seller, SyncState
from app.sync import sync_all, sync_resource

@asynccontextmanager
async def lifespan(app): Base.metadata.create_all(engine); yield
app=FastAPI(title="Xnamai BI API",version="1.0.0",lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=settings().origins,allow_credentials=False,allow_methods=["GET","POST"],allow_headers=["*"])
def auth(x_api_key:str|None=Header(None)):
    if not x_api_key or not secrets.compare_digest(x_api_key,settings().bi_api_key): raise HTTPException(401,"Chave inválida")
@app.get("/health")
def health(): return {"status":"ok","service":"Xnamai BI API"}
@app.get("/api/v1/dashboard",dependencies=[Depends(auth)])
def get_dashboard(days:int=Query(30,ge=1,le=730),db:Session=Depends(db_session)): return dashboard(db,days)
@app.get("/api/v1/rankings",dependencies=[Depends(auth)])
def get_rankings(days:int=Query(30,ge=1,le=730),db:Session=Depends(db_session)): return rankings(db,days)
@app.get("/api/v1/orders",dependencies=[Depends(auth)])
def orders(limit:int=Query(100,le=500),db:Session=Depends(db_session)):
    return [{"id":x.mercos_id,"number":x.number,"customerId":x.customer_mercos_id,"sellerId":x.seller_mercos_id,"status":x.status,"date":x.issued_at,"total":x.total} for x in db.scalars(select(Order).order_by(desc(Order.issued_at)).limit(limit))]
@app.get("/api/v1/products",dependencies=[Depends(auth)])
def products(limit:int=Query(100,le=500),db:Session=Depends(db_session)): return [{"id":x.mercos_id,"code":x.code,"name":x.name,"stock":x.stock,"price":x.list_price,"active":x.active} for x in db.scalars(select(Product).limit(limit))]
@app.get("/api/v1/customers",dependencies=[Depends(auth)])
def customers(limit:int=Query(100,le=500),db:Session=Depends(db_session)): return [{"id":x.mercos_id,"name":x.name,"city":x.city,"state":x.state,"email":x.email,"phone":x.phone} for x in db.scalars(select(Customer).limit(limit))]
@app.get("/api/v1/sellers",dependencies=[Depends(auth)])
def sellers(db:Session=Depends(db_session)): return [{"id":x.mercos_id,"name":x.name,"active":x.active} for x in db.scalars(select(Seller))]
@app.get("/api/v1/sync/status",dependencies=[Depends(auth)])
def sync_status(db:Session=Depends(db_session)): return [{"resource":x.resource,"status":x.status,"cursor":x.cursor,"lastSuccessAt":x.last_success_at,"records":x.records,"error":x.error} for x in db.scalars(select(SyncState))]
@app.post("/api/v1/sync/{resource}",dependencies=[Depends(auth)])
async def run_sync(resource:str,full:bool=False):
    if resource=="all": return await sync_all(full)
    if resource not in {"customers","products","users","orders"}: raise HTTPException(404,"Recurso inválido")
    return await sync_resource(resource,full)

