"""Async retail analysis jobs with per-product commit, heartbeat and resume."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics import _aware
from app.database import SessionLocal
from app.models import RetailAnalysisJob
from app.services.retail_analysis import analyze_product, pending_candidate_ids

log = logging.getLogger(__name__)

HEARTBEAT_STALE = timedelta(minutes=4)
ACTIVE_STATUSES = ("queued", "running")
RESUMABLE_STATUSES = ("interrupted", "failed")
_worker_lock = threading.Lock()
_running_job_ids: set[int] = set()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value) -> str | None:
    aware = _aware(value)
    return aware.isoformat() if aware else None


def job_payload(job: RetailAnalysisJob, *, catalog_pending: int | None = None) -> dict[str, Any]:
    total = len(job.product_ids or [])
    cursor = int(job.cursor or 0)
    remaining_in_job = max(0, total - cursor)
    return {
        "id": job.id,
        "status": job.status,
        "mode": job.mode,
        "batchSize": job.batch_size,
        "total": total,
        "cursor": cursor,
        "processed": job.processed,
        "failed": job.failed,
        "skipped": job.skipped,
        "remainingInJob": remaining_in_job,
        "progressPct": round((cursor / total) * 100, 1) if total else 100.0,
        "currentProductId": job.current_product_id,
        "lastError": job.last_error,
        "errors": (job.errors or [])[-10:],
        "catalogPending": catalog_pending,
        "heartbeatAt": _iso(job.heartbeat_at),
        "startedAt": _iso(job.started_at),
        "finishedAt": _iso(job.finished_at),
        "createdAt": _iso(job.created_at),
        "updatedAt": _iso(job.updated_at),
        "resumable": job.status in RESUMABLE_STATUSES and remaining_in_job > 0,
        "running": job.status in ACTIVE_STATUSES,
    }


def reclaim_stale_jobs(db: Session) -> int:
    """Mark running/queued jobs without fresh heartbeat as interrupted."""
    now = _now()
    changed = 0
    rows = list(
        db.scalars(select(RetailAnalysisJob).where(RetailAnalysisJob.status.in_(list(ACTIVE_STATUSES))))
    )
    for job in rows:
        heartbeat = _aware(job.heartbeat_at) or _aware(job.updated_at) or _aware(job.started_at)
        if heartbeat and (now - heartbeat) < HEARTBEAT_STALE:
            continue
        # Still claimed by this process worker?
        if job.id in _running_job_ids and heartbeat and (now - heartbeat) < HEARTBEAT_STALE:
            continue
        job.status = "interrupted"
        job.last_error = job.last_error or "Job interrompido por timeout/heartbeat (pode retomar)"
        job.current_product_id = None
        job.updated_at = now
        job.finished_at = now
        db.add(job)
        changed += 1
    if changed:
        db.commit()
    return changed


def get_active_job(db: Session) -> RetailAnalysisJob | None:
    reclaim_stale_jobs(db)
    return db.scalar(
        select(RetailAnalysisJob)
        .where(RetailAnalysisJob.status.in_(list(ACTIVE_STATUSES)))
        .order_by(RetailAnalysisJob.id.desc())
    )


def get_job(db: Session, job_id: int) -> RetailAnalysisJob:
    job = db.scalar(select(RetailAnalysisJob).where(RetailAnalysisJob.id == job_id))
    if not job:
        raise HTTPException(404, "Job nao encontrado")
    return job


def latest_job(db: Session) -> RetailAnalysisJob | None:
    reclaim_stale_jobs(db)
    return db.scalar(select(RetailAnalysisJob).order_by(RetailAnalysisJob.id.desc()).limit(1))


def _touch(db: Session, job: RetailAnalysisJob, **fields) -> None:
    now = _now()
    for key, value in fields.items():
        setattr(job, key, value)
    job.heartbeat_at = now
    job.updated_at = now
    db.add(job)
    db.commit()
    db.refresh(job)


def start_job(
    db: Session,
    *,
    mode: str = "batch",
    batch_size: int = 10,
    resume: bool = True,
) -> tuple[RetailAnalysisJob, bool]:
    """Create or resume a job. Returns (job, created_new)."""
    mode = mode if mode in {"batch", "all"} else "batch"
    batch_size = max(1, min(50, int(batch_size)))
    reclaim_stale_jobs(db)

    active = get_active_job(db)
    if active:
        return active, False

    if resume:
        resumable = db.scalar(
            select(RetailAnalysisJob)
            .where(RetailAnalysisJob.status.in_(list(RESUMABLE_STATUSES)))
            .order_by(RetailAnalysisJob.id.desc())
            .limit(1)
        )
        if resumable and int(resumable.cursor or 0) < len(resumable.product_ids or []):
            _touch(
                db,
                resumable,
                status="queued",
                finished_at=None,
                last_error=None,
                current_product_id=None,
            )
            return resumable, False

    pending = pending_candidate_ids(db, pool_size=None)
    if not pending:
        raise HTTPException(400, "Nenhum produto pendente para analisar")

    ids = pending if mode == "all" else pending[:batch_size]
    now = _now()
    job = RetailAnalysisJob(
        status="queued",
        mode=mode,
        batch_size=batch_size,
        product_ids=ids,
        cursor=0,
        processed=0,
        failed=0,
        skipped=0,
        current_product_id=None,
        last_error=None,
        errors=[],
        heartbeat_at=now,
        started_at=None,
        finished_at=None,
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job, True


def cancel_job(db: Session, job_id: int) -> RetailAnalysisJob:
    job = get_job(db, job_id)
    if job.status not in ACTIVE_STATUSES and job.status not in RESUMABLE_STATUSES:
        return job
    _touch(
        db,
        job,
        status="cancelled",
        finished_at=_now(),
        current_product_id=None,
        last_error="Cancelado pelo usuario",
    )
    return job


def _append_error(job: RetailAnalysisJob, product_id: str, message: str) -> None:
    errors = list(job.errors or [])
    errors.append({"id": product_id, "error": message, "at": _now().isoformat()})
    job.errors = errors[-50:]
    job.last_error = message[:500]


def run_job_worker(job_id: int) -> None:
    """Process one product at a time with commit + heartbeat (safe to resume)."""
    with _worker_lock:
        if job_id in _running_job_ids:
            return
        _running_job_ids.add(job_id)

    try:
        with SessionLocal() as db:
            job = db.scalar(select(RetailAnalysisJob).where(RetailAnalysisJob.id == job_id))
            if not job:
                return
            if job.status == "cancelled":
                return
            if job.status not in ACTIVE_STATUSES and job.status not in RESUMABLE_STATUSES:
                # Allow queued/running only; interrupted needs start_job resume first
                if job.status != "queued":
                    return

            _touch(
                db,
                job,
                status="running",
                started_at=job.started_at or _now(),
                finished_at=None,
            )

            ids = list(job.product_ids or [])
            while True:
                db.refresh(job)
                if job.status == "cancelled":
                    break
                cursor = int(job.cursor or 0)
                if cursor >= len(ids):
                    break

                product_id = ids[cursor]
                _touch(db, job, current_product_id=product_id, status="running")

                try:
                    analyze_product(
                        db,
                        product_id,
                        refresh=False,
                        allow_heuristic=False,
                    )
                    job.processed = int(job.processed or 0) + 1
                except Exception as error:  # noqa: BLE001
                    message = str(getattr(error, "detail", error))
                    log.exception("Retail job %s failed on %s", job_id, product_id)
                    job.failed = int(job.failed or 0) + 1
                    _append_error(job, product_id, message)

                job.cursor = cursor + 1
                job.current_product_id = None
                job.heartbeat_at = _now()
                job.updated_at = _now()
                db.add(job)
                db.commit()

            db.refresh(job)
            if job.status != "cancelled":
                _touch(
                    db,
                    job,
                    status="completed",
                    finished_at=_now(),
                    current_product_id=None,
                )

            # Auto-chain next batch when mode=all still has catalog pending
            if job.mode == "all" and job.status == "completed":
                remaining = pending_candidate_ids(db, pool_size=None)
                if remaining:
                    next_job, _ = start_job(db, mode="all", batch_size=job.batch_size or 10, resume=False)
                    # Recursion via new background thread to avoid deep stack
                    threading.Thread(
                        target=run_job_worker,
                        args=(next_job.id,),
                        daemon=True,
                        name=f"retail-job-{next_job.id}",
                    ).start()
    finally:
        with _worker_lock:
            _running_job_ids.discard(job_id)


def enqueue_job_worker(job_id: int) -> None:
    """Start worker in a daemon thread (survives HTTP response)."""
    thread = threading.Thread(
        target=run_job_worker,
        args=(job_id,),
        daemon=True,
        name=f"retail-job-{job_id}",
    )
    thread.start()


def status_snapshot(db: Session, job: RetailAnalysisJob | None = None) -> dict[str, Any]:
    reclaim_stale_jobs(db)
    job = job or get_active_job(db) or latest_job(db)
    pending = len(pending_candidate_ids(db, pool_size=None))
    if not job:
        return {
            "job": None,
            "catalogPending": pending,
            "hasActiveJob": False,
        }
    return {
        "job": job_payload(job, catalog_pending=pending),
        "catalogPending": pending,
        "hasActiveJob": job.status in ACTIVE_STATUSES,
    }
