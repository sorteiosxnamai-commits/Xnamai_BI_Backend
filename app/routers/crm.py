from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import db_session
from app.services.crm import claim_lead, crm_dashboard, finish_lead, lead_detail, list_leads

router = APIRouter(prefix="/api/v1/crm", tags=["crm"])


class ClaimRequest(BaseModel):
    sellerName: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)


@router.get("/leads")
def get_leads(
    search: str | None = Query(default=None, max_length=200),
    top: int = Query(default=20, ge=1, le=50),
    queueLimit: int = Query(default=80, ge=1, le=200),
    db: Session = Depends(db_session),
):
    return list_leads(db, search=search, top=top, queue_limit=queueLimit)


@router.get("/leads/{customer_id}")
def get_lead(customer_id: str, db: Session = Depends(db_session)):
    return lead_detail(db, customer_id)


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
