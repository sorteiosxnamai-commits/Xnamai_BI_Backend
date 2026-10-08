"""Concorrência e recuperação do worker em PostgreSQL REAL.

SQLite não prova bloqueios de linha nem isolamento; estes testes só rodam com
ERP_TEST_DATABASE_URL apontando para um banco isolado (nunca produção).
"""

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select, update

from app.erp import outbox, queue
from app.erp.auth import ErpUser
from app.erp.integrations.mercos_client import ListPage, MercosAdaptorClient
from app.erp.models import ErpCustomer, ErpJob, ErpOperation, ErpSyncCheckpoint, ErpSyncRun
from app.erp.schemas.commands import CustomerCreate
from app.erp.sync import engine
from app.erp.workers import process_next_job
from tests.erp.pg import PG_URL

pytestmark = pytest.mark.skipif(not PG_URL, reason="requer ERP_TEST_DATABASE_URL (PostgreSQL real)")

C = "test"
ADMIN = ErpUser("admin@xnamai.com", ["erp_admin"], permissions={"*"})


@pytest.fixture(autouse=True)
def _setup(erp_cfg, erp_session_factory):
    erp_cfg(erp_write_customers=True, erp_job_lease_seconds=3)


def run_threads(targets):
    results, errors = [None] * len(targets), []

    def wrap(i, fn):
        try:
            results[i] = fn()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=wrap, args=(i, fn)) for i, fn in enumerate(targets)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


class SlowClient:
    """Mercos simulado: cada página demora e o paralelismo por recurso é medido."""

    def __init__(self, pages, delay=0.0, on_page=None):
        self.pages, self.delay, self.on_page = pages, delay, on_page
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls = 0
        self.lock = threading.Lock()

    async def list_page(self, alias, cursor):
        with self.lock:
            self.in_flight += 1
            self.calls += 1
            index = min(self.calls, len(self.pages)) - 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            if self.on_page:
                self.on_page(index + 1)
            return self.pages[index]
        finally:
            with self.lock:
                self.in_flight -= 1


def page(ids, cursor, nxt=None):
    rows = [{"id": i, "razao_social": f"C{i}", "ultima_alteracao": cursor} for i in ids]
    return ListPage("customers", len(rows), cursor, nxt, rows)


def test_many_workers_claim_the_same_job_exactly_once(erp_session_factory):
    with erp_session_factory() as db:
        queue.enqueue(db, kind="sync", connection_id=C, resource="customers")
        db.commit()

    def claim():
        with erp_session_factory() as db:
            job = queue.claim_next(db)
            return job.lease_token if job else None

    results, errors = run_threads([claim] * 10)
    assert not errors
    assert len([r for r in results if r]) == 1


def test_two_workers_never_sync_the_same_resource_at_once(erp_session_factory):
    with erp_session_factory() as db:
        queue.enqueue(db, kind="sync", connection_id=C, resource="customers", mode="incremental")
        queue.enqueue(db, kind="sync", connection_id=C, resource="customers", mode="full")
        db.commit()
    client = SlowClient([page([1, 2], "2026-10-07T10:00:00")], delay=0.8)

    def worker():
        return asyncio.run(process_next_job(client))

    _, errors = run_threads([worker, worker])
    assert not errors
    assert client.max_in_flight == 1  # nunca dois pedidos ao mesmo recurso em paralelo
    with erp_session_factory() as db:
        statuses = sorted(j.status for j in db.scalars(select(ErpJob)))
        assert statuses == ["queued", "succeeded"]  # o "ocupado" voltou à fila com espera
        waiting = db.scalar(select(ErpJob).where(ErpJob.status == "queued"))
        assert waiting.run_after.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc) + timedelta(seconds=20)
        assert db.scalar(select(func.count(ErpCustomer.id))) == 2  # sem duplicata
        assert db.scalar(select(func.count(ErpSyncRun.id)).where(ErpSyncRun.status == "running")) == 0
        waiting.run_after = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert asyncio.run(process_next_job(client)) is True
    with erp_session_factory() as db:
        assert sorted(j.status for j in db.scalars(select(ErpJob))) == ["succeeded", "succeeded"]


def test_lease_is_renewed_during_a_long_sync_and_nobody_steals_it(erp_session_factory):
    pages = [page([1], "2026-10-07T10:00:00", "2026-10-07T10:00:00"),
             page([2], "2026-10-07T11:00:00", "2026-10-07T11:00:00"),
             page([3], "2026-10-07T12:00:00", "2026-10-07T12:00:00"),
             page([4], "2026-10-07T13:00:00", None)]
    client = SlowClient(pages, delay=1.6)  # ~6,5 s no total, lease de 3 s
    with erp_session_factory() as db:
        queue.enqueue(db, kind="sync", connection_id=C, resource="customers")
        db.commit()
    stolen = []
    started_flag: list[int] = []
    stop = threading.Event()

    def thief():
        # outro worker tentando reivindicar jobs com lease vencido, o tempo todo
        while not stop.is_set():
            with erp_session_factory() as db:
                if db.scalar(select(func.count(ErpJob.id)).where(ErpJob.status == "processing")) == 0                         and not started_flag:
                    time.sleep(0.05)  # só começa a roubar depois que o dono real pegou o job
                    continue
                started_flag.append(1)
                queue.release_expired(db)
                db.commit()
                job = queue.claim_next(db)
                if job:
                    stolen.append(job.id)
            time.sleep(0.25)

    t = threading.Thread(target=thief)
    t.start()
    started = time.monotonic()
    try:
        assert asyncio.run(process_next_job(client)) is True
    finally:
        stop.set()
        t.join()
    assert time.monotonic() - started > 5  # de fato passou várias vezes pelo lease de 3 s
    assert stolen == []
    with erp_session_factory() as db:
        job = db.scalar(select(ErpJob))
        assert job.status == "succeeded" and job.attempts == 1
        assert db.scalar(select(func.count(ErpCustomer.id))) == 4


def test_worker_that_lost_the_lease_cannot_commit_anything(erp_session_factory):
    with erp_session_factory() as db:
        queue.enqueue(db, kind="sync", connection_id=C, resource="customers")
        db.commit()
        old = queue.claim_next(db)
        old_id, old_token = old.id, old.lease_token

    def steal(page_number):
        if page_number != 2:
            return
        # a página 2 já foi buscada pelo worker antigo; antes de ele gravar, o lease vence
        # e OUTRO worker reivindica o mesmo job
        with erp_session_factory() as db:
            db.execute(update(ErpJob).where(ErpJob.id == old_id)
                       .values(leased_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
            db.commit()
            queue.release_expired(db)
            db.commit()
            new = queue.claim_next(db)
            steal.new_token = new.lease_token

    pages = [page([1], "2026-10-07T10:00:00", "2026-10-07T10:00:00"), page([2], "2026-10-07T11:00:00", None)]
    client = SlowClient(pages, on_page=steal)
    result = asyncio.run(engine.sync_resource(
        client, C, "customers", job_id=old_id, lease_token=old_token))
    assert result.status == "lease_lost"
    with erp_session_factory() as db:
        ids = sorted(db.scalars(select(ErpCustomer.external_id)))
        assert ids == ["1"]  # só a página confirmada ANTES da perda; a página 2 sofreu rollback
        cp = db.scalar(select(ErpSyncCheckpoint))
        assert cp.cursor == "2026-10-07T10:00:00"  # checkpoint não avançou para a página 2
        run = db.scalar(select(ErpSyncRun))
        assert run.status == "running"  # o perdedor não confirmou nem o fechamento
        job = db.get(ErpJob, old_id)
        assert job.lease_token == steal.new_token and job.status == "processing"
    # o novo dono retoma pelo checkpoint e conclui sem perder nem duplicar
    fresh = SlowClient([page([2], "2026-10-07T11:00:00", None)])
    done = asyncio.run(engine.sync_resource(
        fresh, C, "customers", job_id=old_id, lease_token=steal.new_token))
    assert done.status == "success"
    with erp_session_factory() as db:
        assert sorted(db.scalars(select(ErpCustomer.external_id))) == ["1", "2"]
        assert sorted(r.status for r in db.scalars(select(ErpSyncRun))) == ["interrupted", "success"]


def test_stale_worker_cannot_finish_the_job_either(erp_session_factory):
    with erp_session_factory() as db:
        queue.enqueue(db, kind="sync", connection_id=C, resource="customers")
        db.commit()
        old = queue.claim_next(db)
        old_id, old_token = old.id, old.lease_token
        job = db.get(ErpJob, old_id)
        job.lease_token, job.leased_until = "novo-dono", datetime.now(timezone.utc) + timedelta(minutes=5)
        db.commit()
    with erp_session_factory() as db:
        queue.finish(db, old_id, old_token, status="succeeded", result={"x": 1})
        job = db.get(ErpJob, old_id)
        assert job.status == "processing" and job.result is None  # o `finish` antigo não pegou
        with pytest.raises(queue.LeaseLost):
            queue.assert_lease(db, old_id, old_token)


def test_concurrent_identical_submissions_create_one_operation(erp_session_factory):
    body = CustomerCreate(name="Concorrente")

    def submit():
        with erp_session_factory() as db:
            op, created = outbox.submit(
                db, connection_id=C, user=ADMIN, kind="create_customer", body=body,
                idempotency_key="idem-concorrente-01")
            db.commit()
            return op.id, created

    results, errors = run_threads([submit] * 8)
    assert not errors
    assert len({r[0] for r in results}) == 1
    assert sum(1 for r in results if r[1]) == 1
    with erp_session_factory() as db:
        assert db.scalar(select(func.count(ErpOperation.id))) == 1


def test_two_dispatchers_on_one_operation_call_mercos_once(erp_session_factory):
    with erp_session_factory() as db:
        op, _ = outbox.submit(db, connection_id=C, user=ADMIN, kind="create_customer",
                              body=CustomerCreate(name="Uma vez"), idempotency_key="idem-uma-vez-0001")
        db.commit()
        op_id = op.id
    calls = []

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            calls.append(request)
            await asyncio.sleep(0.4)
            return httpx.Response(201, json={"id": 99})

    client = MercosAdaptorClient(base_url="https://a.test", read_key="r", write_key="w", transport=Transport())

    def dispatch():
        return asyncio.run(outbox.dispatch_operation(client, op_id))

    results, errors = run_threads([dispatch] * 6)
    assert not errors
    assert len(calls) == 1 and results.count("success") == 1
    with erp_session_factory() as db:
        assert db.get(ErpOperation, op_id).status == "succeeded"


def test_restart_after_crash_mid_dispatch_never_resends(erp_session_factory):
    with erp_session_factory() as db:
        op, _ = outbox.submit(db, connection_id=C, user=ADMIN, kind="create_customer",
                              body=CustomerCreate(name="Queda"), idempotency_key="idem-queda-0000001")
        db.commit()
        op = db.get(ErpOperation, op.id)
        op.status, op.lease_token = "processing", "worker-morto"
        op.leased_until = datetime.now(timezone.utc) - timedelta(seconds=5)
        op.dispatch_started_at = datetime.now(timezone.utc) - timedelta(seconds=30)
        db.commit()
        op_id = op.id
    calls = []
    client = MercosAdaptorClient(
        base_url="https://a.test", read_key="r", write_key="w",
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(201, json={"id": 1})),
    )
    # "reinício": o novo processo recupera leases vencidos e tenta despachar
    with erp_session_factory() as db:
        assert outbox.recover_stale(db) == 1
        db.commit()
    for _ in range(3):
        assert asyncio.run(outbox.dispatch_operation(client, op_id)) is None
    assert calls == []
    with erp_session_factory() as db:
        op = db.get(ErpOperation, op_id)
        assert op.status == "unknown" and op.error_code == "crash_after_dispatch"


def test_heartbeat_never_blocks_on_a_locked_job_row(erp_session_factory):
    with erp_session_factory() as db:
        queue.enqueue(db, kind="sync", connection_id=C, resource="customers")
        db.commit()
        job = queue.claim_next(db)
        job_id, token = job.id, job.lease_token
    holder = erp_session_factory()
    holder.execute(select(ErpJob).where(ErpJob.id == job_id).with_for_update())  # transação longa
    try:
        started = time.monotonic()
        with erp_session_factory() as db:
            assert queue.heartbeat(db, job_id, token) is True  # lease ainda é nosso
            assert queue.heartbeat(db, job_id, "outro-token") is False
        assert time.monotonic() - started < 2  # não esperou pelo lock
    finally:
        holder.rollback()
        holder.close()


def test_page_commit_holds_the_job_lock_only_at_the_end(erp_session_factory):
    """Lock tomado no fim da página: a gravação longa não bloqueia o heartbeat."""
    with erp_session_factory() as db:
        queue.enqueue(db, kind="sync", connection_id=C, resource="customers")
        db.commit()
        job = queue.claim_next(db)
        job_id, token = job.id, job.lease_token
    beats = []

    def slow(page_number):
        # durante a "gravação" da página, outro fio renova o lease
        with erp_session_factory() as db:
            beats.append(queue.heartbeat(db, job_id, token))

    client = SlowClient([page([1, 2], "2026-10-07T10:00:00")], on_page=slow)
    result = asyncio.run(engine.sync_resource(client, C, "customers", job_id=job_id, lease_token=token))
    assert result.status == "success" and beats == [True]
