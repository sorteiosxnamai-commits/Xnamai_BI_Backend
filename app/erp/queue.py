"""Fila persistida do ERP: claim atômico, lease e heartbeat.

O claim é um UPDATE condicional (`WHERE status='queued'`): duas instâncias do
worker nunca pegam o mesmo job, mesmo sem lock distribuído.
"""

from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.erp.common import as_utc, utcnow
from app.erp.config import erp_settings
from app.erp.models.core import ErpJob

ACTIVE = ("queued", "processing", "waiting_rate_limit")


def enqueue(
    db: Session,
    *,
    kind: str,
    connection_id: str,
    resource: str | None = None,
    mode: str = "incremental",
    requested_by: str | None = None,
    payload: dict | None = None,
    run_after: datetime | None = None,
) -> tuple[ErpJob, bool]:
    """Devolve (job, criado). Não duplica job ativo equivalente."""
    existing = db.scalar(
        select(ErpJob).where(
            ErpJob.kind == kind,
            ErpJob.connection_id == connection_id,
            ErpJob.resource == resource,
            ErpJob.mode == mode,
            ErpJob.status.in_(ACTIVE),
        )
    )
    if existing is not None:
        return existing, False
    job = ErpJob(
        kind=kind,
        connection_id=connection_id,
        resource=resource,
        mode=mode,
        requested_by=requested_by,
        payload=payload or {},
        run_after=run_after or utcnow(),
    )
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        # Outro processo enfileirou o mesmo job ao mesmo tempo (índice único ativo).
        existing = db.scalar(
            select(ErpJob).where(
                ErpJob.kind == kind,
                ErpJob.connection_id == connection_id,
                ErpJob.resource == resource,
                ErpJob.mode == mode,
                ErpJob.status.in_(ACTIVE),
            )
        )
        if existing is None:
            raise
        return existing, False
    return job, True


class LeaseLost(Exception):
    """O worker perdeu o lease do job: nada do que ele fizer a partir daqui vale."""


def assert_lease(db: Session, job_id: int | None, token: str | None) -> None:
    """Chamado DENTRO da transação que confirma alterações.

    Trava a linha do job (FOR UPDATE) e exige que o lease ainda seja nosso e válido.
    Como a verificação e a gravação ocupam a mesma transação, um worker que perdeu o
    lease (expirado e reivindicado por outro) não consegue confirmar página, run ou
    checkpoint. Execuções fora de job (token ausente) não têm lease a verificar."""
    if job_id is None or token is None:
        return
    row = db.scalar(
        select(ErpJob)
        .where(ErpJob.id == job_id, ErpJob.lease_token == token, ErpJob.status == "processing")
        .with_for_update()
    )
    if row is None or (row.leased_until is not None and as_utc(row.leased_until) < utcnow()):
        raise LeaseLost(f"Lease do job {job_id} perdido")


def lease_alive(job_id: int | None, token: str | None) -> bool:
    """Checagem barata (sessão própria) usada entre páginas para parar cedo."""
    if job_id is None or token is None:
        return True
    from app.erp.db import session_scope

    with session_scope() as db:
        row = db.scalar(
            select(ErpJob.leased_until).where(
                ErpJob.id == job_id, ErpJob.lease_token == token, ErpJob.status == "processing"
            )
        )
        return row is not None and as_utc(row) >= utcnow()


def release_expired(db: Session, now: datetime | None = None) -> int:
    """Lease vencida: o job volta à fila. Jobs de sync são idempotentes."""
    now = now or utcnow()
    result = db.execute(
        update(ErpJob)
        .where(ErpJob.status == "processing", ErpJob.leased_until < now)
        .values(status="queued", lease_token=None, leased_until=None)
    )
    return int(result.rowcount or 0)


def claim_next(
    db: Session, *, kinds: tuple[str, ...] | None = None, now: datetime | None = None
) -> ErpJob | None:
    now = now or utcnow()
    query = (
        select(ErpJob.id)
        .where(ErpJob.status == "queued", ErpJob.run_after <= now)
        .order_by(ErpJob.run_after, ErpJob.id)
        .limit(5)
    )
    if kinds:
        query = query.where(ErpJob.kind.in_(kinds))
    lease = timedelta(seconds=erp_settings().erp_job_lease_seconds)
    for job_id in db.scalars(query).all():
        token = str(uuid4())
        claimed = db.execute(
            update(ErpJob)
            .where(ErpJob.id == job_id, ErpJob.status == "queued")
            .values(
                status="processing",
                lease_token=token,
                leased_until=now + lease,
                heartbeat_at=now,
                attempts=ErpJob.attempts + 1,
            )
        )
        if claimed.rowcount == 1:
            db.commit()
            job = db.get(ErpJob, job_id)
            db.refresh(job)
            return job
    return None


def heartbeat(db: Session, job_id: int, token: str) -> bool:
    """Renova o lease sem esperar por lock.

    A gravação de uma página trava a linha do job só ao final (milissegundos), mas se o
    heartbeat cair nessa janela ele não deve bloquear até estourar o lock_timeout: pula a
    renovação e apenas confirma, sem lock, que o lease continua sendo nosso."""
    locked = db.scalar(
        select(ErpJob.id)
        .where(ErpJob.id == job_id, ErpJob.lease_token == token)
        .with_for_update(skip_locked=True)
    )
    if locked is None:
        current = db.scalar(select(ErpJob.lease_token).where(ErpJob.id == job_id))
        db.rollback()
        return current == token
    now = utcnow()
    db.execute(
        update(ErpJob)
        .where(ErpJob.id == job_id, ErpJob.lease_token == token)
        .values(
            heartbeat_at=now,
            leased_until=now + timedelta(seconds=erp_settings().erp_job_lease_seconds),
        )
    )
    db.commit()
    return True


def finish(
    db: Session,
    job_id: int,
    token: str,
    *,
    status: str,
    result: dict | None = None,
    error: str | None = None,
    run_after: datetime | None = None,
) -> None:
    values: dict = {
        "status": status,
        "result": result,
        "error": error,
        "lease_token": None,
        "leased_until": None,
    }
    if status in ("succeeded", "failed", "cancelled"):
        values["finished_at"] = utcnow()
    if run_after is not None:
        values["run_after"] = run_after
    db.execute(
        update(ErpJob).where(ErpJob.id == job_id, ErpJob.lease_token == token).values(**values)
    )
    db.commit()


def cancel(db: Session, job_id: int) -> ErpJob | None:
    """Interrompe pendências futuras; não desfaz o que o Mercos já aceitou."""
    job = db.get(ErpJob, job_id)
    if job is None:
        return None
    if job.status in ("queued", "waiting_rate_limit"):
        job.status = "cancelled"
        job.finished_at = utcnow()
    elif job.status == "processing":
        job.cancel_requested = True
    db.add(job)
    db.flush()
    return job


def is_cancel_requested(db: Session, job_id: int) -> bool:
    db.expire_all()
    job = db.get(ErpJob, job_id)
    return bool(job and job.cancel_requested)


def next_run_delay(job: ErpJob) -> float:
    after = as_utc(job.run_after)
    return max(0.0, (after - utcnow()).total_seconds()) if after else 0.0
