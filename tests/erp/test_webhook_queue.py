import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.erp import inbox, queue
from app.erp.integrations.mercos_client import ListPage
from app.erp.models import ErpJob, ErpWebhookInbox
from app.erp.workers import process_inbox, process_next_job
from tests.erp.helpers import bearer, client

SECRET_HEX = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90"


def sign(body: bytes, secret_hex: str = SECRET_HEX) -> str:
    digest = hmac.new(bytes.fromhex(secret_hex), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@pytest.fixture(autouse=True)
def _setup(erp_cfg, erp_session_factory):
    erp_cfg(erp_webhook_secret_hex=SECRET_HEX)


def post(body: bytes, signature: str | None, **headers):
    h = {"Content-Type": "application/json", **headers}
    if signature is not None:
        h["X-Hub-Signature-256"] = signature
    return client().post("/api/v1/erp/webhooks/mercos", content=body, headers=h)


def test_missing_or_invalid_signature_is_rejected_and_nothing_persisted(erp_session_factory):
    body = json.dumps({"evento": "pedido.gerado"}).encode()
    assert post(body, None).status_code == 401
    assert post(body, "sha256=" + "0" * 64).status_code == 401
    # segredo errado
    assert post(body, sign(body, "ff" * 32)).status_code == 401
    # assinatura sobre corpo diferente
    assert post(body + b" ", sign(body)).status_code == 401
    with erp_session_factory() as db:
        assert db.scalar(select(func.count(ErpWebhookInbox.id))) == 0


def test_no_secret_configured_is_503(erp_cfg):
    erp_cfg(erp_webhook_secret_hex="")
    assert post(b"{}", "sha256=" + "0" * 64).status_code == 503


def test_valid_signature_persists_before_2xx_and_signature_uses_raw_bytes(erp_session_factory):
    raw = b'{"evento":  "pedido.gerado",   "dados": {"id": 1}}'  # espaços preservados
    response = post(raw, sign(raw))
    assert response.status_code == 200 and response.json()["status"] == "accepted"
    with erp_session_factory() as db:
        row = db.scalar(select(ErpWebhookInbox))
        assert row.raw_body == raw and row.status == "received"


def test_processing_schedules_incremental_sync_instead_of_trusting_payload(erp_session_factory):
    raw = json.dumps([
        {"evento": "pedido.faturado", "dados": {"id": 1}},
        {"evento": "cliente.atualizado", "dados": {"id": 2}},
        {"evento": "produto.atualizado", "dados": {"id": 3}},  # fora do catálogo documentado
        {"evento": "pagamento.atualizado", "dados": {"id": 4}},
    ]).encode()
    post(raw, sign(raw))
    handled = asyncio.run(process_inbox())
    assert handled == 1
    with erp_session_factory() as db:
        row = db.scalar(select(ErpWebhookInbox))
        assert row.status == "processed"
        assert sorted(row.result["scheduled"]) == ["customers", "orders"]
        reasons = {i["event"]: i["reason"] for i in row.result["ignored"]}
        assert "produto.atualizado" in reasons and "pagamento.atualizado" in reasons
        jobs = db.scalars(select(ErpJob)).all()
        assert sorted(j.resource for j in jobs) == ["customers", "orders"]
        assert all(j.mode == "incremental" for j in jobs)


def test_repeated_delivery_and_window_dedupe(erp_session_factory):
    raw = b'{"evento":"cliente.cadastrado"}'
    first = post(raw, sign(raw), **{"X-Delivery-Id": "entrega-1"})
    again = post(raw, sign(raw), **{"X-Delivery-Id": "entrega-1"})
    assert first.json()["status"] == "accepted" and again.json()["status"] == "duplicate"
    other = post(raw, sign(raw), **{"X-Delivery-Id": "entrega-2"})
    assert other.json()["status"] == "accepted"  # outro ID de entrega = evento legítimo
    # sem ID: deduplica só dentro da janela
    plain = b'{"evento":"cliente.excluido"}'
    assert post(plain, sign(plain)).json()["status"] == "accepted"
    assert post(plain, sign(plain)).json()["status"] == "duplicate"
    with erp_session_factory() as db:
        for row in db.scalars(select(ErpWebhookInbox).where(ErpWebhookInbox.delivery_id.is_(None))):
            row.received_at = datetime.now(timezone.utc) - timedelta(hours=2)
        db.commit()
    assert post(plain, sign(plain)).json()["status"] == "accepted"


def test_oversized_and_invalid_json(erp_cfg, erp_session_factory):
    erp_cfg(erp_webhook_secret_hex=SECRET_HEX, erp_webhook_max_bytes=100)
    big = b"{" + b" " * 200 + b"}"
    assert post(big, sign(big)).status_code == 413
    erp_cfg(erp_webhook_secret_hex=SECRET_HEX)
    broken = b"{nao e json"
    assert post(broken, sign(broken)).status_code == 200
    asyncio.run(process_inbox())
    with erp_session_factory() as db:
        assert db.scalar(select(ErpWebhookInbox)).status == "failed"


def test_inbox_unavailable_is_never_2xx(monkeypatch):
    def boom(*args, **kwargs):
        raise OperationalError("insert", {}, Exception("db down"))

    monkeypatch.setattr(inbox, "receive", boom)
    raw = b'{"evento":"pedido.gerado"}'
    response = post(raw, sign(raw))
    assert response.status_code == 503


def test_out_of_order_webhook_does_not_regress_mirror(erp_session_factory):
    from app.erp.sync import engine
    from app.erp.models import ErpCustomer

    class Fake:
        def __init__(self, rows, cursor):
            self.rows, self.cursor = rows, cursor

        async def list_page(self, alias, cursor):
            return ListPage("customers", len(self.rows), self.cursor, None, self.rows)

    newer = {"id": 1, "razao_social": "Mais novo", "ultima_alteracao": "2026-10-07T12:00:00"}
    asyncio.run(engine.sync_resource(Fake([newer], "2026-10-07T12:00:00"), "test", "customers"))
    # webhook atrasado só agenda consulta; a consulta devolve o que a origem tem agora
    raw = b'{"evento":"cliente.atualizado","dados":{"id":1,"razao_social":"Antigo"}}'
    post(raw, sign(raw))
    asyncio.run(process_inbox())
    with erp_session_factory() as db:
        assert db.scalar(select(ErpCustomer.name)) == "Mais novo"  # payload do webhook nunca aplicado


def test_queue_claim_is_atomic_and_skips_future_jobs(erp_session_factory):
    with erp_session_factory() as db:
        a, created = queue.enqueue(db, kind="sync", connection_id="test", resource="customers")
        dup, created_dup = queue.enqueue(db, kind="sync", connection_id="test", resource="customers")
        queue.enqueue(db, kind="sync", connection_id="test", resource="products",
                      run_after=datetime.now(timezone.utc) + timedelta(hours=1))
        db.commit()
        assert created and not created_dup and dup.id == a.id
    with erp_session_factory() as db1, erp_session_factory() as db2:
        first = queue.claim_next(db1)
        second = queue.claim_next(db2)
        assert first is not None and first.resource == "customers" and first.status == "processing"
        assert second is None  # o outro job está no futuro; o primeiro já foi reivindicado


def test_expired_lease_requeues_and_cancel_semantics(erp_session_factory):
    with erp_session_factory() as db:
        job, _ = queue.enqueue(db, kind="sync", connection_id="test", resource="customers")
        db.commit()
        claimed = queue.claim_next(db)
        claimed.leased_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.add(claimed)
        db.commit()
        assert queue.release_expired(db) == 1
        db.commit()
        assert db.get(ErpJob, job.id).status == "queued"
        queue.cancel(db, job.id)
        assert db.get(ErpJob, job.id).status == "cancelled"


def test_worker_job_runs_sync_and_reschedules_on_rate_limit(erp_session_factory):
    from app.erp.integrations.mercos_client import AdaptorError
    from app.erp.models import ErpCustomer

    class Client:
        def __init__(self, results):
            self.results = results

        async def list_page(self, alias, cursor):
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    with erp_session_factory() as db:
        queue.enqueue(db, kind="sync", connection_id="test", resource="customers")
        db.commit()
    limited = Client([AdaptorError("rate_limited", "429", status_code=429, retry_after=90)])
    assert asyncio.run(process_next_job(limited)) is True
    with erp_session_factory() as db:
        job = db.scalar(select(ErpJob))
        assert job.status == "queued" and job.run_after.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc) + timedelta(seconds=60)
    assert asyncio.run(process_next_job(limited)) is False  # ainda dentro da espera
    with erp_session_factory() as db:
        job = db.scalar(select(ErpJob))
        job.run_after = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    ok = Client([ListPage("customers", 1, "2026-10-07T10:00:00", None,
                          [{"id": 1, "razao_social": "A", "ultima_alteracao": "2026-10-07T10:00:00"}])])
    assert asyncio.run(process_next_job(ok)) is True
    with erp_session_factory() as db:
        assert db.scalar(select(ErpJob)).status == "succeeded"
        assert db.scalar(select(func.count(ErpCustomer.id))) == 1


def test_manual_sync_endpoint_returns_202_and_does_not_read_catalog(erp_session_factory):
    c = client()
    response = c.post("/api/v1/erp/integration/sync", json={"resource": "customers"}, headers=bearer())
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued" and body["statusUrl"].endswith(str(body["jobId"]))
    again = c.post("/api/v1/erp/integration/sync", json={"resource": "customers"}, headers=bearer())
    assert again.json()["jobId"] == body["jobId"] and again.json()["created"] is False
    bad = c.post("/api/v1/erp/integration/sync", json={"resource": "nope"}, headers=bearer())
    assert bad.status_code == 404
    status = c.get(f"/api/v1/erp/integration/jobs/{body['jobId']}", headers=bearer()).json()
    assert status["status"] == "queued"
