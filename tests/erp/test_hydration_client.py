import asyncio
import json

import httpx
import pytest
from sqlalchemy import select

from app.erp import capabilities
from app.erp.integrations.mercos_client import AdaptorError, MercosAdaptorClient
from app.erp.models import ErpCapability, ErpSalesOrder
from app.erp.sync import engine


@pytest.fixture(autouse=True)
def _setup(erp_cfg, erp_session_factory):
    erp_cfg()


class Transport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[httpx.Request] = []

    async def handle_async_request(self, request):
        self.calls.append(request)
        return self.handler(request)


def client_for(handler) -> tuple[MercosAdaptorClient, Transport]:
    transport = Transport(handler)
    return (
        MercosAdaptorClient(
            base_url="https://adaptor.test", read_key="read-key", write_key="w", transport=transport
        ),
        transport,
    )


def seed_incomplete(factory, *ids):
    from datetime import datetime, timezone

    with factory() as db:
        for external_id in ids:
            db.add(ErpSalesOrder(connection_id="test", external_id=external_id, number=external_id,
                                 items_complete=False, fingerprint=f"f{external_id}",
                                 captured_at=datetime.now(timezone.utc)))
        db.commit()


def test_hydration_completes_items_from_detail(erp_session_factory):
    seed_incomplete(erp_session_factory, "900")
    detail = {"id": 900, "numero": "9", "status": 2, "ultima_alteracao": "2026-10-07T10:00:00",
              "itens": [{"id": 1, "produto_id": 5, "quantidade": 3, "preco_liquido": "2.00"}]}
    client, transport = client_for(lambda req: httpx.Response(200, json=detail))
    result = asyncio.run(engine.hydrate_orders(client, "test"))
    assert result.status == "success" and result.persisted == 1
    assert str(transport.calls[0].url) == "https://adaptor.test/v1/orders/900"
    assert transport.calls[0].headers["x-api-key"] == "read-key"
    with erp_session_factory() as db:
        order = db.scalar(select(ErpSalesOrder))
        assert order.items_complete is True and order.item_count == 1


def test_hydration_blocked_by_account_is_readable_not_fatal(erp_session_factory):
    seed_incomplete(erp_session_factory, "900", "901")
    client, transport = client_for(lambda req: httpx.Response(401, text="unauthorized"))
    result = asyncio.run(engine.hydrate_orders(client, "test"))
    assert result.status == "forbidden"
    assert len(transport.calls) == 1  # para no primeiro bloqueio; sem tempestade de chamadas
    with erp_session_factory() as db:
        assert db.scalar(select(ErpSalesOrder).where(ErpSalesOrder.external_id == "900")) is not None
        row = db.scalar(select(ErpCapability).where(ErpCapability.key == "read.orders"))
        assert "detalhe por id indisponível" in row.reason.lower()


def test_hydration_rate_limit_and_divergent_id(erp_session_factory):
    seed_incomplete(erp_session_factory, "900")
    limited, _ = client_for(lambda req: httpx.Response(429, headers={"Retry-After": "17"}))
    result = asyncio.run(engine.hydrate_orders(limited, "test"))
    assert result.status == "waiting_rate_limit" and result.retry_after == 17
    divergent, _ = client_for(lambda req: httpx.Response(200, json={"id": 999, "itens": []}))
    result = asyncio.run(engine.hydrate_orders(divergent, "test"))
    assert result.status == "failed" and "divergente" in result.error


def test_client_list_page_classifies_errors_without_retrying():
    cases = {
        429: "rate_limited",
        403: "forbidden",
        401: "forbidden",
        404: "not_found",
        502: "unavailable",
    }
    for status, kind in cases.items():
        client, transport = client_for(lambda req, s=status: httpx.Response(s, text="x"))
        with pytest.raises(AdaptorError) as err:
            asyncio.run(client.list_page("customers", None))
        assert err.value.kind == kind
        assert len(transport.calls) == 1  # sem retry em camadas


def test_client_rejects_unknown_alias_and_sends_only_allowed_filter():
    client, transport = client_for(lambda req: httpx.Response(
        200, json={"resource": "orders", "count": 0, "pageCursor": None, "nextCursor": None, "data": []}))
    with pytest.raises(AdaptorError):
        asyncio.run(client.list_page("../secrets", None))
    asyncio.run(client.list_page("orders", "2026-10-07T10:00:00"))
    assert dict(transport.calls[0].url.params) == {"alterado_apos": "2026-10-07T10:00:00"}
    assert transport.calls[0].url.path == "/v1/orders"


def test_client_write_never_retries_and_encodes_ids():
    client, transport = client_for(lambda req: httpx.Response(200, json={"id": "a/b"}))
    outcome = asyncio.run(client.write("update_customer", {"cidade": "X"}, "a/b ?x"))
    assert outcome.kind == "success"
    assert transport.calls[0].url.raw_path == b"/v1/customers/a%2Fb%20%3Fx"
    assert json.loads(transport.calls[0].content) == {"cidade": "X"}
    assert asyncio.run(client.write("delete_everything", {})).kind == "rejected"
    assert asyncio.run(client.write("update_order", {})).error_code == "missing_id"


def test_capability_access_is_recorded_from_real_evidence(erp_session_factory):
    from app.erp.integrations.mercos_client import ListPage

    class Denied:
        async def list_page(self, alias, cursor):
            raise AdaptorError("forbidden", "negado", status_code=403)

    class Allowed:
        async def list_page(self, alias, cursor):
            return ListPage(alias, 0, None, None, [])

    asyncio.run(engine.sync_resource(Denied(), "test", "segments"))
    asyncio.run(engine.sync_resource(Allowed(), "test", "carriers"))
    with erp_session_factory() as db:
        matrix = {i["key"]: i for i in capabilities.build_matrix(db, "test")}
    assert matrix["read.segments"]["accountAccess"] == "denied"
    assert matrix["read.segments"]["enabled"] is False
    assert matrix["read.carriers"]["accountAccess"] == "allowed"
    assert matrix["read.carriers"]["lastValidatedAt"] is not None
