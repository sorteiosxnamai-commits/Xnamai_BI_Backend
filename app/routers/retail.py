from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import db_session
from app.services import retail as retail_service
from app.services import retail_analysis as retail_ai
from app.services import retail_jobs

router = APIRouter(prefix="/api/v1/retail", tags=["retail"])


class AnalyzeBatchRequest(BaseModel):
    limit: int = Field(default=10, ge=1, le=20)
    refresh: bool = False


class StartJobRequest(BaseModel):
    mode: str = Field(default="batch", pattern="^(batch|all)$")
    batchSize: int = Field(default=10, ge=1, le=50)
    resume: bool = True


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
def post_analyze_batch(
    payload: AnalyzeBatchRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(db_session),
):
    """Compatibility endpoint: starts async batch job and returns immediately."""
    job, _created = retail_jobs.start_job(
        db,
        mode="batch",
        batch_size=payload.limit,
        resume=True,
    )
    background_tasks.add_task(retail_jobs.enqueue_job_worker, job.id)
    return JSONResponse(status_code=202, content=retail_jobs.status_snapshot(db, job))


@router.post("/analyze-jobs")
def post_analyze_job(
    payload: StartJobRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(db_session),
):
    job, created = retail_jobs.start_job(
        db,
        mode=payload.mode,
        batch_size=payload.batchSize,
        resume=payload.resume,
    )
    background_tasks.add_task(retail_jobs.enqueue_job_worker, job.id)
    snapshot = retail_jobs.status_snapshot(db, job)
    snapshot["created"] = created
    return JSONResponse(status_code=202, content=snapshot)


@router.get("/analyze-jobs/active")
def get_active_analyze_job(db: Session = Depends(db_session)):
    return retail_jobs.status_snapshot(db)


@router.get("/analyze-jobs/{job_id}")
def get_analyze_job(job_id: int, db: Session = Depends(db_session)):
    job = retail_jobs.get_job(db, job_id)
    retail_jobs.reclaim_stale_jobs(db)
    db.refresh(job)
    return retail_jobs.status_snapshot(db, job)


@router.post("/analyze-jobs/{job_id}/resume")
def resume_analyze_job(
    job_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(db_session),
):
    job = retail_jobs.get_job(db, job_id)
    retail_jobs.reclaim_stale_jobs(db)
    db.refresh(job)
    if job.status in {"completed", "cancelled"}:
        raise HTTPException(400, f"Job {job.id} esta {job.status} e nao pode ser retomado")
    started, _ = retail_jobs.start_job(
        db,
        mode=job.mode or "batch",
        batch_size=job.batch_size or 10,
        resume=True,
    )
    background_tasks.add_task(retail_jobs.enqueue_job_worker, started.id)
    return JSONResponse(status_code=202, content=retail_jobs.status_snapshot(db, started))


@router.post("/analyze-jobs/{job_id}/cancel")
def cancel_analyze_job(job_id: int, db: Session = Depends(db_session)):
    job = retail_jobs.cancel_job(db, job_id)
    return retail_jobs.status_snapshot(db, job)
