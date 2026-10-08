import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.erp import outbox
from app.erp.auth import ErpUser
from app.erp.integrations.mercos_client import MercosAdaptorClient
from app.erp.models import ErpConflict, ErpCustomer, ErpOperation, ErpSalesOrder
from app.erp.schemas.commands import CustomerCreate, CustomerPatch, OrderCreate

ADMIN = ErpUser("admin@xnamai.com", ["erp_admin"], permissions={"*"})


@pytest.fixture(autouse=True)
def _setup(erp_cfg, erp_session_factory):
    erp_cfg(erp_write_customers=True, erp_write_orders=True)


class Recorder(httpx.AsyncBaseTransport):
    """Transporte simulado: nenhum teste toca o Mercos real."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[httpx.Request] = []

    async def handle_async_request(self, request):
        self.calls.append(request)
        return self.handler(request)


def make_client(handler) -> tuple[MercosAdaptorClient, Recorder]:
    transport = Recorder(handler)
    client = MercosAdaptorClient(
        base_url="https://adaptor.test", read_key="r", write_key="w", transport=transport
    )
    return client, transport


def seed_customer(factory, **extra):
    with factory() as db:
        db.add(
            ErpCustomer(
                connection_id="test", external_id="10", name="Original", city="SP",
                state="SP", fingerprint="fp-base", captured_at=datetime.now(timezone.utc),
                **extra,
            )
        )
        db.commit()


def submit(factory, kind, body, key, target=None):
    with factory() as db:
        op, _ = outbox.submit(
            db, connection_id="test", user=ADMIN, kind=kind, body=body,
            idempotency_key=key, target_external_id=target,
        )
        db.commit()
        return op.id


def get(factory, op_id) -> ErpOperation:
    with factory() as db:
        return db.get(ErpOperation, op_id)


def run(coro):
    return asyncio.run(coro)


def test_success_records_external_id_and_waits_for_mirror_confirmation(erp_session_factory):
    op_id = submit(erp_session_factory, "create_customer", CustomerCreate(name="Novo"), "key-success-1")
    client, transport = make_client(
        lambda req: httpx.Response(201, json={"id": 777, "razao_social": "Novo"})
    )
    assert run(outbox.dispatch_operation(client, op_id)) == "success"
    op = get(erp_session_factory, op_id)
    assert op.status == "succeeded" and op.external_id == "777"
    assert op.mirror_confirmed_at is None  # só depois do retorno da origem
    assert transport.calls[0].method == "POST"
    assert transport.calls[0].headers["x-api-key"] == "w"
    # terminal: nunca reenviado
    assert run(outbox.dispatch_operation(client, op_id)) is None
    assert len(transport.calls) == 1


def test_timeout_becomes_unknown_and_is_never_resent(erp_session_factory):
    op_id = submit(erp_session_factory, "create_customer", CustomerCreate(name="Novo"), "key-timeout-1")

    def boom(request):
        raise httpx.ReadTimeout("timeout", request=request)

    client, transport = make_client(boom)
    assert run(outbox.dispatch_operation(client, op_id)) == "unknown"
    op = get(erp_session_factory, op_id)
    assert op.status == "unknown" and op.error_code == "transport"
    assert op.dispatch_started_at is not None
    # sem reenvio automático: nova tentativa não faz chamada
    for _ in range(3):
        assert run(outbox.dispatch_operation(client, op_id)) is None
    assert len(transport.calls) == 1
    assert outbox.due_operation_ids() == []


def test_success_without_id_is_unknown_not_failed(erp_session_factory):
    op_id = submit(erp_session_factory, "create_customer", CustomerCreate(name="Novo"), "key-noid-0001")
    client, _ = make_client(lambda req: httpx.Response(200, json=None))
    assert run(outbox.dispatch_operation(client, op_id)) == "unknown"
    assert get(erp_session_factory, op_id).error_code == "success_without_id"


def test_5xx_after_send_is_unknown_and_4xx_is_failed(erp_session_factory):
    a = submit(erp_session_factory, "create_customer", CustomerCreate(name="A"), "key-5xx-00001")
    b = submit(erp_session_factory, "create_customer", CustomerCreate(name="B"), "key-412-00001")
    client5, _ = make_client(lambda req: httpx.Response(502, text="bad gateway"))
    client4, _ = make_client(lambda req: httpx.Response(412, json={"erro": "precondition"}))
    run(outbox.dispatch_operation(client5, a))
    run(outbox.dispatch_operation(client4, b))
    assert get(erp_session_factory, a).status == "unknown"
    op_b = get(erp_session_factory, b)
    assert op_b.status == "failed" and op_b.error_code == "precondition_failed"


def test_rate_limit_reschedules_without_losing_intent_and_resends_once(erp_session_factory):
    op_id = submit(erp_session_factory, "create_customer", CustomerCreate(name="Novo"), "key-429-00001")
    client, transport = make_client(
        lambda req: httpx.Response(429, json={"tempo_ate_permitir_novamente": 12})
    )
    assert run(outbox.dispatch_operation(client, op_id)) == "rate_limited"
    op = get(erp_session_factory, op_id)
    assert op.status == "waiting_rate_limit" and op.dispatch_started_at is None
    assert op.next_attempt_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc) + timedelta(seconds=5)
    assert outbox.due_operation_ids() == []  # ainda dentro da espera
    with erp_session_factory() as db:
        row = db.get(ErpOperation, op_id)
        row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    ok, ok_transport = make_client(lambda req: httpx.Response(201, json={"id": 5}))
    assert run(outbox.dispatch_operation(ok, op_id)) == "success"
    assert len(ok_transport.calls) == 1 and len(transport.calls) == 1


def test_crash_after_dispatch_marks_unknown_not_queued(erp_session_factory):
    op_id = submit(erp_session_factory, "create_customer", CustomerCreate(name="Novo"), "key-crash-001")
    with erp_session_factory() as db:
        op = db.get(ErpOperation, op_id)
        op.status = "processing"
        op.lease_token = "dead"
        op.leased_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        op.dispatch_started_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        db.commit()
    with erp_session_factory() as db:
        assert outbox.recover_stale(db) == 1
        db.commit()
    op = get(erp_session_factory, op_id)
    assert op.status == "unknown" and op.error_code == "crash_after_dispatch"
    assert outbox.due_operation_ids() == []


def test_crash_before_dispatch_is_requeued(erp_session_factory):
    op_id = submit(erp_session_factory, "create_customer", CustomerCreate(name="Novo"), "key-crash-002")
    with erp_session_factory() as db:
        op = db.get(ErpOperation, op_id)
        op.status = "processing"
        op.lease_token = "dead"
        op.leased_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
        outbox.recover_stale(db)
        db.commit()
    assert get(erp_session_factory, op_id).status == "queued"


def test_illegal_transitions_are_rejected():
    op = ErpOperation(status="unknown")
    with pytest.raises(Exception) as err:
        outbox.transition(op, "queued")
    assert "Transição inválida" in str(err.value.detail["message"])
    op = ErpOperation(status="succeeded")
    with pytest.raises(Exception):
        outbox.transition(op, "failed")


def test_update_conflict_blocks_send_and_resolution_paths(erp_session_factory):
    seed_customer(erp_session_factory)
    op_id = submit(
        erp_session_factory, "update_customer",
        CustomerPatch(expectedVersion=1, city="Local"), "key-conf-0001", target="10",
    )
    # Mercos mudou a MESMA cidade desde a base
    with erp_session_factory() as db:
        c = db.scalar(select(ErpCustomer))
        c.city = "Externa"
        c.fingerprint = "fp-outro"
        c.version += 1
        db.commit()
    client, transport = make_client(lambda req: httpx.Response(200, json={"id": 10}))
    assert run(outbox.dispatch_operation(client, op_id)) is None
    assert transport.calls == []
    op = get(erp_session_factory, op_id)
    assert op.status == "conflict"
    with erp_session_factory() as db:
        conflict = db.scalar(select(ErpConflict))
        assert conflict.fields == ["city"]
        assert conflict.base == {"city": "SP"} and conflict.external == {"city": "Externa"}
        outbox.resolve_conflict(db, conflict, ADMIN, resolution="use_local", note=None)
        db.commit()
    assert get(erp_session_factory, op_id).status == "queued"
    assert run(outbox.dispatch_operation(client, op_id)) == "success"
    assert transport.calls[0].method == "PUT"
    assert str(transport.calls[0].url).endswith("/v1/customers/10")


def test_other_field_changed_externally_is_not_a_conflict(erp_session_factory):
    seed_customer(erp_session_factory)
    op_id = submit(
        erp_session_factory, "update_customer",
        CustomerPatch(expectedVersion=1, city="Local"), "key-conf-0002", target="10",
    )
    with erp_session_factory() as db:
        c = db.scalar(select(ErpCustomer))
        c.name = "Renomeado na origem"
        c.fingerprint = "fp-outro"
        db.commit()
    client, transport = make_client(lambda req: httpx.Response(200, json={"id": 10}))
    assert run(outbox.dispatch_operation(client, op_id)) == "success"
    import json

    assert json.loads(transport.calls[0].content) == {"cidade": "Local"}  # só o intencional


def test_conflict_use_external_discards_intent(erp_session_factory):
    seed_customer(erp_session_factory)
    op_id = submit(
        erp_session_factory, "update_customer",
        CustomerPatch(expectedVersion=1, city="Local"), "key-conf-0003", target="10",
    )
    with erp_session_factory() as db:
        c = db.scalar(select(ErpCustomer))
        c.city = "Externa"
        c.fingerprint = "fp-outro"
        db.commit()
    client, transport = make_client(lambda req: httpx.Response(200, json={"id": 10}))
    run(outbox.dispatch_operation(client, op_id))
    with erp_session_factory() as db:
        outbox.resolve_conflict(db, db.scalar(select(ErpConflict)), ADMIN, resolution="use_external", note="ok")
        db.commit()
    op = get(erp_session_factory, op_id)
    assert op.status == "failed" and op.error_code == "conflict_external_wins"
    assert transport.calls == []


def test_reconcile_unknown_needs_human_and_only_from_unknown(erp_session_factory):
    op_id = submit(erp_session_factory, "create_customer", CustomerCreate(name="Novo", document="999.888"), "key-recon-001")
    client, _ = make_client(lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("t", request=req)))
    run(outbox.dispatch_operation(client, op_id))
    with erp_session_factory() as db:
        # um candidato no espelho com o mesmo documento, capturado depois do envio
        db.add(ErpCustomer(connection_id="test", external_id="55", name="Qualquer", document="999.888",
                           captured_at=datetime.now(timezone.utc) + timedelta(seconds=5)))
        db.commit()
    with erp_session_factory() as db:
        op = db.get(ErpOperation, op_id)
        outbox.reconcile(db, op, ADMIN, decision="check", external_id=None, note=None)
        db.commit()
        assert op.status == "unknown"  # check nunca decide sozinho
        assert op.reconcile_evidence["candidates"][0]["externalId"] == "55"
        assert op.reconcile_evidence["ambiguous"] is False
        outbox.reconcile(db, op, ADMIN, decision="confirm_created", external_id="55", note="conferido")
        db.commit()
        assert op.status == "succeeded" and op.external_id == "55"
        with pytest.raises(Exception) as err:
            outbox.reconcile(db, op, ADMIN, decision="confirm_not_created", external_id=None, note=None)
        assert err.value.detail["code"] == "not_unknown"


def test_confirm_not_created_allows_new_intent_with_new_key(erp_session_factory):
    op_id = submit(erp_session_factory, "create_customer", CustomerCreate(name="Novo"), "key-recon-002")
    client, _ = make_client(lambda req: httpx.Response(503, text="x"))
    run(outbox.dispatch_operation(client, op_id))
    with erp_session_factory() as db:
        op = db.get(ErpOperation, op_id)
        outbox.reconcile(db, op, ADMIN, decision="confirm_not_created", external_id=None, note="não achei")
        db.commit()
        assert op.status == "failed"
    new_id = submit(erp_session_factory, "create_customer", CustomerCreate(name="Novo"), "key-recon-003")
    assert new_id != op_id


def test_mirror_confirmation_closes_outbox_without_new_write(erp_session_factory):
    from app.erp.integrations.mercos_client import ListPage
    from app.erp.sync import engine

    op_id = submit(erp_session_factory, "create_customer", CustomerCreate(name="Novo"), "key-mirror-01")
    client, transport = make_client(lambda req: httpx.Response(201, json={"id": 4242}))
    run(outbox.dispatch_operation(client, op_id))

    class Fake:
        async def list_page(self, alias, cursor):
            return ListPage("customers", 1, "2026-10-07T10:00:00", None,
                            [{"id": 4242, "razao_social": "Novo", "ultima_alteracao": "2026-10-07T10:00:00"}])

    run(engine.sync_resource(Fake(), "test", "customers"))
    op = get(erp_session_factory, op_id)
    assert op.mirror_confirmed_at is not None
    assert len(transport.calls) == 1  # confirmou o outbox; nada de escrita circular
    with erp_session_factory() as db:
        assert len(db.scalars(select(ErpOperation)).all()) == 1


def test_order_create_validates_customer_and_blocks_incomplete_updates(erp_session_factory):
    from app.erp.schemas.commands import OrderPatch

    body = OrderCreate(customerId="999", items=[{"productId": "1", "quantity": "2"}])
    with erp_session_factory() as db, pytest.raises(Exception) as err:
        outbox.submit(db, connection_id="test", user=ADMIN, kind="create_order", body=body,
                      idempotency_key="key-order-0001")
    assert err.value.detail["code"] == "unknown_customer"
    with erp_session_factory() as db:
        db.add(ErpSalesOrder(connection_id="test", external_id="900", number="9", items_complete=False,
                             fingerprint="f", captured_at=datetime.now(timezone.utc)))
        db.commit()
    with erp_session_factory() as db, pytest.raises(Exception) as err:
        outbox.submit(db, connection_id="test", user=ADMIN, kind="update_order",
                      body=OrderPatch(expectedVersion=1, notes="x"),
                      idempotency_key="key-order-0002", target_external_id="900")
    assert err.value.detail["code"] == "order_incomplete"


def test_blocked_customer_cannot_receive_new_order(erp_session_factory):
    seed_customer(erp_session_factory, blocked=True)
    body = OrderCreate(customerId="10", items=[{"productId": "1", "quantity": "1"}])
    with erp_session_factory() as db, pytest.raises(Exception) as err:
        outbox.submit(db, connection_id="test", user=ADMIN, kind="create_order", body=body,
                      idempotency_key="key-order-0003")
    assert err.value.detail["code"] == "customer_blocked"


def test_write_key_missing_never_calls_adaptor(erp_session_factory):
    op_id = submit(erp_session_factory, "create_customer", CustomerCreate(name="Novo"), "key-nokey-001")
    transport = Recorder(lambda req: httpx.Response(201, json={"id": 1}))
    client = MercosAdaptorClient(base_url="https://adaptor.test", read_key="r", write_key="", transport=transport)
    run(outbox.dispatch_operation(client, op_id))
    assert transport.calls == []
    assert get(erp_session_factory, op_id).status == "failed"
