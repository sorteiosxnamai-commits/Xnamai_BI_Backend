from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import db_session
from app.services.crm import claim_lead, crm_dashboard, finish_lead, lead_detail, list_leads
from app.services.lead_analysis import analyze_lead

router = APIRouter(prefix="/api/v1/crm", tags=["crm"])


class ClaimRequest(BaseModel):
    sellerName: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)


@router.get("/leads")
def get_leads(
    search: str | None = Query(default=None, max_length=200),
    top: int = Query(default=20, ge=1, le=50),
    view: str = Query(default="main", pattern="^(main|new|ai)$"),
    queuePage: int = Query(default=1, ge=1),
    queuePageSize: int = Query(default=40, ge=1, le=100),
    refreshAi: bool = Query(default=False),
    db: Session = Depends(db_session),
):
    return list_leads(
        db,
        search=search,
        top=top,
        queue_page=queuePage,
        queue_page_size=queuePageSize,
        view=view,
        refresh_ai=refreshAi,
    )


@router.get("/leads/{customer_id}")
def get_lead(customer_id: str, db: Session = Depends(db_session)):
    return lead_detail(db, customer_id)


@router.get("/leads/{customer_id}/analysis")
def get_lead_analysis(
    customer_id: str,
    refresh: bool = Query(default=False),
    db: Session = Depends(db_session),
):
    return analyze_lead(db, customer_id, refresh=refresh)


@router.post("/leads/{customer_id}/claim")
def post_claim(customer_id: str, payload: ClaimRequest | None = None, db: Session = Depends(db_session)):
    body = payload or ClaimRequest()
    return claim_lead(db, customer_id, body.sellerName)


@router.post("/leads/{customer_id}/finish")
def post_finish(customer_id: str, payload: ClaimRequest | None = None, db: Session = Depends(db_session)):
    body = payload or ClaimRequest()
    return finish_lead(db, customer_id, body.sellerName, body.notes)


@router.get("/dashboard")
def get_dashboard(days: int = Query(default=30, ge=1, le=365), db: Session = Depends(db_session)):
    return crm_dashboard(db, days=days)
