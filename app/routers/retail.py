from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import db_session
from app.services import retail as retail_service
from app.services import retail_analysis as retail_ai

router = APIRouter(prefix="/api/v1/retail", tags=["retail"])


class AnalyzeBatchRequest(BaseModel):
    limit: int = Field(default=10, ge=1, le=20)
    refresh: bool = False


@router.get("/economics")
def get_economics():
    return retail_service.economics_config()


@router.get("/recommended")
def get_recommended(
    top: int = Query(default=100, ge=1, le=250),
    days: int | None = Query(default=90, ge=1, le=365),
    db: Session = Depends(db_session),
):
    return retail_service.recommended_products(db, top=top, pool_size=None, days=days)


@router.get("/candidates")
def get_candidates(
    days: int | None = Query(default=90, ge=1, le=365),
    db: Session = Depends(db_session),
):
    return retail_service.list_candidates(db, pool_size=None, days=days)


@router.get("/products/{product_id}/analysis")
def get_product_analysis(
    product_id: str,
    refresh: bool = Query(default=False),
    db: Session = Depends(db_session),
):
    if refresh:
        return retail_ai.get_or_analyze(db, product_id, refresh=True)
    return retail_service.product_analysis_detail(db, product_id)


@router.post("/analyze-batch")
def post_analyze_batch(payload: AnalyzeBatchRequest, db: Session = Depends(db_session)):
    return retail_ai.analyze_batch(db, limit=payload.limit, refresh=payload.refresh)
