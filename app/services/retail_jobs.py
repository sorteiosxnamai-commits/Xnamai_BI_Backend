"""Async retail analysis jobs with parallel workers, heartbeat and resume."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics import _aware
from app.cache.redis_cache import RETAIL_JOB_KEY, cache_set, invalidate_retail_lists
from app.config import settings
from app.database import SessionLocal
from app.models import RetailAnalysisJob
from app.services.retail import catalog_progress
from app.services.retail_analysis import analyze_product, pending_candidate_ids

log = logging.getLogger(__name__)

HEARTBEAT_STALE = timedelta(minutes=6)
ACTIVE_STATUSES = ("queued", "running")
RESUMABLE_STATUSES = ("interrupted", "failed")
# Cap product_ids per job so POST /analyze-jobs stays fast (mode=all auto-chains).
JOB_CHUNK_MAX = 200
_worker_lock = threading.Lock()
_job_db_lock = threading.Lock()
_running_job_ids: set[int] = set()


def _job_chunk_size(batch_size: int) -> int:
    workers = _concurrency_hint(batch_size)
    return max(40, min(JOB_CHUNK_MAX, workers * max(1, batch_size) * 8))


def _concurrency_hint(batch_size: int) -> int:
    configured = int(getattr(settings(), "retail_concurrency", 5) or 5)
    batch = max(1, int(batch_size or 5))
    return max(1, min(12, configured, max(batch, 3)))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value) -> str | None:
    aware = _aware(value)
    return aware.isoformat() if aware else None


def _concurrency(job: RetailAnalysisJob | None = None) -> int:
    batch = int((job.batch_size if job else 5) or 5)
    return _concurrency_hint(batch)


def _job_done_count(job: RetailAnalysisJob) -> int:
    return int(job.processed or 0) + int(job.failed or 0) + int(job.skipped or 0)


def job_payload(
    job: RetailAnalysisJob,
    *,
    catalog_pending: int | None = None,
    catalog_analyzed: int | None = None,
    catalog_pool: int | None = None,
    batch_number: int | None = None,
    estimated_batches_remaining: int | None = None,
) -> dict[str, Any]:
    total = len(job.product_ids or [])
    cursor = int(job.cursor or 0)
    done = _job_done_count(job)
    remaining_in_job = max(0, total - done)
    claimed_remaining = max(0, total - cursor)
    # Progress by finished items (not only claimed cursor) so 200/200 claimed != 100% done.
    progress_pct = round((done / total) * 100, 1) if total else 100.0
    claim_pct = round((cursor / total) * 100, 1) if total else 100.0

    if job.status == "queued":
        phase = "queued"
        phase_label = "Lote na fila"
    elif job.status in ACTIVE_STATUSES and cursor >= total > 0 and done < total:
        phase = "finishing"
        phase_label = "Finalizando buscas do lote (aguardando IA)"
    elif job.status in ACTIVE_STATUSES:
        phase = "running"
        phase_label = "Analisando lote atual"
    elif job.status == "completed":
        phase = "completed"
        phase_label = "Lote concluido"
    elif job.status in RESUMABLE_STATUSES:
        phase = "interrupted"
        phase_label = "Lote interrompido (retomavel)"
    else:
        phase = job.status
        phase_label = str(job.status)

    return {
        "id": job.id,
        "status": job.status,
        "mode": job.mode,
        "batchSize": job.batch_size,
        "concurrency": _concurrency(job),
        "total": total,
        "cursor": cursor,
        "done": done,
        "processed": job.processed,
        "failed": job.failed,
        "skipped": job.skipped,
        "remainingInJob": remaining_in_job,
        "claimedRemaining": claimed_remaining,
        "progressPct": progress_pct,
        "claimPct": claim_pct,
        "phase": phase,
        "phaseLabel": phase_label,
        "batchNumber": batch_number,
        "estimatedBatchesRemaining": estimated_batches_remaining,
        "currentProductId": job.current_product_id,
        "lastError": job.last_error,
        "errors": (job.errors or [])[-10:],
        "catalogPending": catalog_pending,
        "catalogAnalyzed": catalog_analyzed,
        "catalogPoolSize": catalog_pool,
        "heartbeatAt": _iso(job.heartbeat_at),
        "startedAt": _iso(job.started_at),
        "finishedAt": _iso(job.finished_at),
        "createdAt": _iso(job.created_at),
        "updatedAt": _iso(job.updated_at),
        "resumable": job.status in RESUMABLE_STATUSES and cursor < total,
        "running": job.status in ACTIVE_STATUSES,
    }


def _batch_number(db: Session, job: RetailAnalysisJob) -> int:
    prior = int(
        db.scalar(
            select(func.count())
            .select_from(RetailAnalysisJob)
            .where(
                RetailAnalysisJob.mode == "all",
                RetailAnalysisJob.id <= job.id,
            )
        )
        or 0
    )
    return max(1, prior)


def _recent_batches(db: Session, *, limit: int = 5) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(select(RetailAnalysisJob).order_by(RetailAnalysisJob.id.desc()).limit(limit))
    )
    out: list[dict[str, Any]] = []
    for row in reversed(rows):
        total = len(row.product_ids or [])
        done = _job_done_count(row)
        out.append(
            {
                "id": row.id,
                "status": row.status,
                "mode": row.mode,
                "total": total,
                "done": done,
                "processed": int(row.processed or 0),
                "failed": int(row.failed or 0),
                "progressPct": round((done / total) * 100, 1) if total else 100.0,
                "finishedAt": _iso(row.finished_at),
                "startedAt": _iso(row.started_at),
            }
        )
    return out


def reclaim_stale_jobs(db: Session) -> int:
    now = _now()
    changed = 0
    rows = list(
        db.scalars(select(RetailAnalysisJob).where(RetailAnalysisJob.status.in_(list(ACTIVE_STATUSES))))
    )
    for job in rows:
        heartbeat = _aware(job.heartbeat_at) or _aware(job.updated_at) or _aware(job.started_at)
        if heartbeat and (now - heartbeat) < HEARTBEAT_STALE:
            continue
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
        invalidate_retail_lists()
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
            invalidate_retail_lists()
            return resumable, False

    # Never embed the full ~14k catalog in one request (proxy/browser timeout).
    chunk = _job_chunk_size(batch_size) if mode == "all" else batch_size
    pending = pending_candidate_ids(db, pool_size=None, limit=chunk)
    if not pending:
        raise HTTPException(400, "Nenhum produto pendente para analisar")

    ids = pending
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
    invalidate_retail_lists()
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
    invalidate_retail_lists()
    return job


def _append_error(job: RetailAnalysisJob, product_id: str, message: str) -> None:
    errors = list(job.errors or [])
    errors.append({"id": product_id, "error": message, "at": _now().isoformat()})
    job.errors = errors[-50:]
    job.last_error = message[:500]


def _claim_next(job_id: int) -> str | None:
    """Atomically claim next product id for parallel workers."""
    with _job_db_lock:
        with SessionLocal() as db:
            job = db.scalar(select(RetailAnalysisJob).where(RetailAnalysisJob.id == job_id))
            if not job or job.status == "cancelled":
                return None
            ids = list(job.product_ids or [])
            cursor = int(job.cursor or 0)
            if cursor >= len(ids):
                return None
            product_id = ids[cursor]
            job.cursor = cursor + 1
            job.current_product_id = product_id
            job.status = "running"
            job.heartbeat_at = _now()
            job.updated_at = _now()
            db.add(job)
            db.commit()
            return product_id


def _record_result(job_id: int, product_id: str, *, ok: bool, error: str | None = None) -> None:
    with _job_db_lock:
        with SessionLocal() as db:
            job = db.scalar(select(RetailAnalysisJob).where(RetailAnalysisJob.id == job_id))
            if not job:
                return
            if ok:
                job.processed = int(job.processed or 0) + 1
            else:
                job.failed = int(job.failed or 0) + 1
                _append_error(job, product_id, error or "erro")
            job.heartbeat_at = _now()
            job.updated_at = _now()
            db.add(job)
            db.commit()
    invalidate_retail_lists()


def _analyze_one(product_id: str) -> None:
    with SessionLocal() as db:
        analyze_product(db, product_id, refresh=False, allow_heuristic=False)


def run_job_worker(job_id: int) -> None:
    """Process products in parallel with per-item commit and resume-safe cursor."""
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
                if job.status != "queued":
                    return
            workers = _concurrency(job)
            _touch(
                db,
                job,
                status="running",
                started_at=job.started_at or _now(),
                finished_at=None,
            )

        def worker_loop() -> None:
            while True:
                with SessionLocal() as db:
                    job = db.scalar(select(RetailAnalysisJob).where(RetailAnalysisJob.id == job_id))
                    if not job or job.status == "cancelled":
                        return
                product_id = _claim_next(job_id)
                if not product_id:
                    return
                try:
                    _analyze_one(product_id)
                    _record_result(job_id, product_id, ok=True)
                except Exception as error:  # noqa: BLE001
                    log.exception("Retail job %s failed on %s", job_id, product_id)
                    _record_result(
                        job_id,
                        product_id,
                        ok=False,
                        error=str(getattr(error, "detail", error)),
                    )

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"retail-{job_id}") as pool:
            futures = [pool.submit(worker_loop) for _ in range(workers)]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:  # noqa: BLE001
                    log.exception("Retail worker crashed for job %s", job_id)

        with SessionLocal() as db:
            job = db.scalar(select(RetailAnalysisJob).where(RetailAnalysisJob.id == job_id))
            if not job:
                return
            if job.status != "cancelled":
                _touch(
                    db,
                    job,
                    status="completed",
                    finished_at=_now(),
                    current_product_id=None,
                )
            # Auto-chain next chunk while catalog still has pending items.
            if job.mode == "all" and job.status == "completed":
                progress = catalog_progress(db)
                if progress["pendingCount"] > 0:
                    next_job, created = start_job(
                        db,
                        mode="all",
                        batch_size=job.batch_size or 10,
                        resume=False,
                    )
                    if created or next_job.status in ACTIVE_STATUSES:
                        enqueue_job_worker(next_job.id)
        invalidate_retail_lists()
    finally:
        with _worker_lock:
            _running_job_ids.discard(job_id)


def enqueue_job_worker(job_id: int) -> None:
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
    progress = catalog_progress(db)
    pending = progress["pendingCount"]
    analyzed = progress["analyzedCount"]
    pool = progress["poolSize"]
    catalog_pct = round((analyzed / pool) * 100, 1) if pool else 0.0
    recent = _recent_batches(db, limit=6)

    chaining = False
    phase = "idle"
    phase_label = "Aguardando inicio"
    batch_number = None
    estimated_remaining = None
    chunk = JOB_CHUNK_MAX
    if job:
        chunk = max(1, len(job.product_ids or []) or _job_chunk_size(int(job.batch_size or 10)))
        batch_number = _batch_number(db, job)
        estimated_remaining = int((pending + chunk - 1) // chunk) if pending else 0
        job_body = job_payload(
            job,
            catalog_pending=pending,
            catalog_analyzed=analyzed,
            catalog_pool=pool,
            batch_number=batch_number,
            estimated_batches_remaining=estimated_remaining,
        )
        phase = job_body["phase"]
        phase_label = job_body["phaseLabel"]
        # Between lots: completed all-mode job with catalog still pending and no active worker yet.
        if (
            job.mode == "all"
            and job.status == "completed"
            and pending > 0
            and get_active_job(db) is None
        ):
            chaining = True
            phase = "chaining"
            phase_label = "Preparando proximo lote..."
            job_body = {
                **job_body,
                "phase": phase,
                "phaseLabel": phase_label,
            }
        elif job.status in ACTIVE_STATUSES:
            chaining = False
    else:
        job_body = None

    payload = {
        "job": job_body,
        "catalogPending": pending,
        "catalogAnalyzed": analyzed,
        "catalogPoolSize": pool,
        "catalogPct": catalog_pct,
        "hasActiveJob": bool(job and job.status in ACTIVE_STATUSES),
        "chainingNextBatch": chaining,
        "pipeline": {
            "phase": phase,
            "phaseLabel": phase_label,
            "batchNumber": batch_number,
            "estimatedBatchesRemaining": estimated_remaining,
            "chunkSize": chunk,
            "recentBatches": recent,
            "catalog": {
                "analyzed": analyzed,
                "pending": pending,
                "poolSize": pool,
                "pct": catalog_pct,
            },
        },
    }
    try:
        cache_set(RETAIL_JOB_KEY, payload, ttl_seconds=8)
    except Exception:
        pass
    return payload
