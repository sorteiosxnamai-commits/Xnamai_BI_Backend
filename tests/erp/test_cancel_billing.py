"""Tarefa 6 do plano: cancelamento e faturamento pelo Adaptor existente (somente mocks)."""

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

from app.erp import outbox
from app.erp.auth import ErpUser
from app.erp.db import session_scope
from app.erp.integrations.mercos_client import MercosAdaptorClient
from app.erp.models import ErpOperation, ErpSalesOrder
from app.erp.schemas.commands import OrderBillingInput, OrderCancelInput
from app.erp.sync import rows as rows_mod
from app.erp.registry import REGISTRY
from tests.erp.helpers import add_operator, bearer, client as api_client
from tests.erp.test_order_operations import C, put_order

ADMIN = ErpUser("admin@xnamai.com", ["erp_admin"], permissions={"*"})


@pytest.fixture(autouse=True)
def _setup(erp_cfg, erp_session_factory):
    erp_cfg(erp_write_orders=True, erp_write_billing=True)
    with session_scope() as db:
        order = put_order(db, "10", net="100.00", items=[("A", "A-1")])
        order.version = 3
        order.fingerprint = "fp-3"
        order.captured_at = datetime.now(timezone.utc)
        incomplete = put_order(db, "11", complete=False)
        incomplete.version = 1
        quote = put_order(db, "12", kind="quote", items=[("B", "B-1")])
        quote.version = 1


class Recorder(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    async def handle_async_request(self, request):
        self.calls.append(request)
        return self.handler(request)


def make_client(handler):
    transport = Recorder(handler)
    return MercosAdaptorClient(base_url="https://adaptor.test", read_key="r", write_key="w", transport=transport), transport


def submit(kind, body, key, target="10"):
    with session_scope() as db:
        op, created = outbox.submit(
            db, connection_id=C, user=ADMIN, kind=kind, body=body, idempotency_key=key, target_external_id=target
        )
        return op.id, created


def op_state(op_id):
    with session_scope() as db:
        op = db.get(ErpOperation, op_id)
        return op.status, op.error_code, op.mirror_confirmed_at, op.external_id


def run(coro):
    return asyncio.run(coro)


def test_cancel_goes_to_the_adaptor_cancel_route_without_body_data_and_waits_for_the_mirror():
    op_id, _ = submit("cancel_order", OrderCancelInput(expectedVersion=3, reason="Cliente desistiu"), "cancel-key-0001")
    client, transport = make_client(lambda req: httpx.Response(200, json=None))
    assert run(outbox.dispatch_operation(client, op_id)) == "success"
    call = transport.calls[0]
    assert (call.method, call.url.path) == ("POST", "/v1/orders/10/cancel")
    assert call.headers["x-api-key"] == "w" and call.content in (b"{}", b"")  # sem dados além do alvo
    status, _, confirmed, external = op_state(op_id)
    assert status == "succeeded" and external == "10"
    assert confirmed is None  # aceito != confirmado: falta o espelho mostrar o pedido cancelado


def test_cancel_is_confirmed_only_when_the_mirror_shows_the_order_cancelled():
    op_id, _ = submit("cancel_order", OrderCancelInput(expectedVersion=3, reason="Duplicado"), "cancel-key-0002")
    client, _ = make_client(lambda req: httpx.Response(200, json=None))
    run(outbox.dispatch_operation(client, op_id))
    definition = REGISTRY["orders"]

    def mirror(status):
        with session_scope() as db:
            return rows_mod.persist_rows(db, C, definition, [{
                "id": 10, "numero": "10", "cliente_id": 5, "status": status, "total": "100",
                "ultima_alteracao": "2026-10-09 10:00:00",
                "itens": [{"id": 1, "produto_id": 2, "quantidade": 1, "preco_liquido": "100"}],
            }], None)

    mirror("2")  # o pedido volta ainda ativo: o espelho NÃO confirma o cancelamento
    assert op_state(op_id)[2] is None
    with session_scope() as db:
        assert db.scalar(select(ErpSalesOrder.kind).where(ErpSalesOrder.external_id == "10")) == "order"
    mirror("cancelado")  # status de cancelamento reconhecido pelo mapeamento
    with session_scope() as db:
        assert db.scalar(select(ErpSalesOrder.kind).where(ErpSalesOrder.external_id == "10")) == "cancelled"
    assert op_state(op_id)[2] is not None  # só agora a operação fica confirmada pelo espelho


def test_cancel_timeout_is_unknown_and_never_resent():
    op_id, _ = submit("cancel_order", OrderCancelInput(expectedVersion=3, reason="Teste"), "cancel-key-0003")

    def boom(request):
        raise httpx.ReadTimeout("timeout", request=request)

    client, transport = make_client(boom)
    assert run(outbox.dispatch_operation(client, op_id)) == "unknown"
    for _ in range(3):
        assert run(outbox.dispatch_operation(client, op_id)) is None
    assert len(transport.calls) == 1  # sem reenvio cego depois do possível sucesso remoto
    assert op_state(op_id)[0] == "unknown"


def test_cancel_rejection_is_a_known_failure():
    op_id, _ = submit("cancel_order", OrderCancelInput(expectedVersion=3, reason="Teste"), "cancel-key-0004")
    client, _ = make_client(lambda req: httpx.Response(412, json={"erro": "pedido inexistente"}))
    assert run(outbox.dispatch_operation(client, op_id)) == "rejected"
    status, code, _, _ = op_state(op_id)
    assert status == "failed" and code == "precondition_failed"


def test_cancel_validations_and_duplicate_protection():
    with pytest.raises(Exception) as stale:
        submit("cancel_order", OrderCancelInput(expectedVersion=2, reason="Teste"), "cancel-key-0005")
    assert "version_mismatch" in str(stale.value.detail)
    with pytest.raises(Exception) as incomplete:
        submit("cancel_order", OrderCancelInput(expectedVersion=1, reason="Teste"), "cancel-key-0006", target="11")
    assert "order_incomplete" in str(incomplete.value.detail)
    first, created = submit("cancel_order", OrderCancelInput(expectedVersion=3, reason="Teste"), "cancel-key-0007")
    again, replay_created = submit("cancel_order", OrderCancelInput(expectedVersion=3, reason="Teste"), "cancel-key-0007")
    assert again == first and replay_created is False  # mesma chave: repetição
    with pytest.raises(Exception) as dup:
        submit("cancel_order", OrderCancelInput(expectedVersion=3, reason="Teste"), "cancel-key-0008")
    assert "operation_in_progress" in str(dup.value.detail)  # outra chave: não duplica a operação
    with session_scope() as db:
        assert db.scalar(select(func.count(ErpOperation.id)).where(ErpOperation.kind == "cancel_order")) == 1


def test_already_cancelled_mirror_is_not_sent_again():
    op_id, _ = submit("cancel_order", OrderCancelInput(expectedVersion=3, reason="Teste"), "cancel-key-0009")
    with session_scope() as db:
        db.scalar(select(ErpSalesOrder).where(ErpSalesOrder.external_id == "10")).kind = "cancelled"
    client, transport = make_client(lambda req: httpx.Response(200, json=None))
    assert run(outbox.dispatch_operation(client, op_id)) is None
    assert transport.calls == []
    status, code, _, _ = op_state(op_id)
    assert status == "failed" and code == "already_cancelled"


def test_cancel_is_blocked_while_the_capability_is_off(erp_cfg):
    erp_cfg(erp_write_orders=False)  # padrão de produção
    with pytest.raises(Exception) as err:
        submit("cancel_order", OrderCancelInput(expectedVersion=3, reason="Teste"), "cancel-key-0010")
    assert "capability_disabled" in str(err.value.detail)


def test_billing_is_not_released_without_a_reconciliation_strategy():
    with pytest.raises(Exception) as err:
        submit("bill_order", OrderBillingInput(expectedVersion=3, billedValue=Decimal("100.00"), billedAt=date(2026, 10, 9)), "bill-key-00001")
    detail = str(err.value.detail)
    assert "capability_disabled" in detail and "write.billing" in detail


def test_api_routes_require_permission_idempotency_and_report_the_disabled_state(erp_cfg, erp_session_factory):
    add_operator(erp_session_factory, "vendas@x.com", ["comercial"])
    http = api_client()
    body = {"expectedVersion": 3, "reason": "Cliente desistiu"}
    denied = http.post("/api/v1/erp/sales-orders/10/cancel", json=body, headers={**bearer("vendas@x.com", "viewer"), "Idempotency-Key": "api-cancel-0001"})
    assert denied.status_code == 403  # comercial não tem orders:cancel
    missing = http.post("/api/v1/erp/sales-orders/10/cancel", json=body, headers=bearer())
    assert missing.status_code == 422
    ok = http.post("/api/v1/erp/sales-orders/10/cancel", json=body, headers={**bearer(), "Idempotency-Key": "api-cancel-0002"})
    assert ok.status_code == 202 and ok.json()["kind"] == "cancel_order"
    bill = http.post(
        "/api/v1/erp/sales-orders/10/billings",
        json={"expectedVersion": 3, "billedValue": "100.00", "billedAt": "2026-10-09"},
        headers={**bearer(), "Idempotency-Key": "api-bill-00001"},
    )
    assert bill.status_code == 409 and bill.json()["detail"]["capability"] == "write.billing"
    caps = {c["key"]: c for c in http.get("/api/v1/erp/capabilities", headers=bearer()).json()["items"]}
    assert caps["write.billing"]["enabled"] is False and "reconciliar" in caps["write.billing"]["reason"]
