from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.erp.models import ErpAuditEvent, ErpCustomer, ErpOperation
from tests.erp.helpers import add_operator, bearer, client


@pytest.fixture(autouse=True)
def _setup(erp_cfg, erp_session_factory):
    erp_cfg()


def seed_customer(factory, external_id="10", **overrides):
    with factory() as db:
        db.add(
            ErpCustomer(
                connection_id="test",
                external_id=external_id,
                name="Cliente Teste",
                document=overrides.get("document", "12.345.678/0001-90"),
                email="c@teste.com",
                phone="11999990000",
                city="São Paulo",
                state="SP",
                blocked=overrides.get("blocked", False),
                fingerprint="fp-1",
                captured_at=datetime.now(timezone.utc),
            )
        )
        db.commit()


def test_disabled_flag_blocks_by_url(erp_cfg):
    erp_cfg(erp_enabled=False)
    response = client().get("/api/v1/erp/me", headers=bearer())
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "erp_disabled"
    webhook = client().post("/api/v1/erp/webhooks/mercos", content=b"{}")
    assert webhook.status_code == 404


def test_default_deny_matrix(erp_session_factory, monkeypatch):
    from app.auth import settings as _bi_settings  # noqa: F401
    from app import auth as bi_auth
    from app.config import Settings

    cfg = Settings(bi_api_key="service-key", jwt_secret="change-me-in-production")
    monkeypatch.setattr(bi_auth, "settings", lambda: cfg)
    monkeypatch.setattr("app.erp.auth.settings", lambda: cfg)

    c = client()
    assert c.get("/api/v1/erp/me").status_code == 401
    # chave de serviço do BI não concede ERP
    key = c.get("/api/v1/erp/me", headers={"X-API-Key": "service-key"})
    assert key.status_code == 403 and key.json()["detail"]["code"] == "service_key_not_allowed"
    # viewer BI sem vínculo
    viewer = c.get("/api/v1/erp/me", headers=bearer("viewer", "viewer"))
    assert viewer.status_code == 403 and viewer.json()["detail"]["code"] == "no_erp_access"
    # admin BI fora da lista de bootstrap e sem linha de operador
    other = c.get("/api/v1/erp/me", headers=bearer("outro@xnamai.com", "admin"))
    assert other.status_code == 403
    # bootstrap explícito
    boot = c.get("/api/v1/erp/me", headers=bearer())
    assert boot.status_code == 200 and boot.json()["roles"] == ["erp_admin"]
    assert boot.json()["contractVersion"] == "erp-1"


def test_operator_roles_and_inactive(erp_session_factory):
    add_operator(erp_session_factory, "ana@x.com", ["consulta"])
    add_operator(erp_session_factory, "off@x.com", ["erp_admin"], active=False)
    c = client()
    ana = c.get("/api/v1/erp/me", headers=bearer("ana@x.com", "viewer"))
    assert ana.status_code == 200 and "pii:read" not in ana.json()["permissions"]
    off = c.get("/api/v1/erp/me", headers=bearer("off@x.com", "admin"))
    assert off.status_code == 403 and off.json()["detail"]["code"] == "operator_inactive"
    # consulta não escreve
    denied = c.post(
        "/api/v1/erp/customers",
        json={"name": "X"},
        headers={**bearer("ana@x.com", "viewer"), "Idempotency-Key": "k-12345678"},
    )
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "forbidden"


def test_pii_is_masked_without_permission(erp_session_factory):
    seed_customer(erp_session_factory)
    add_operator(erp_session_factory, "ana@x.com", ["consulta"])
    add_operator(erp_session_factory, "vend@x.com", ["comercial"])
    c = client()
    masked = c.get("/api/v1/erp/customers/10", headers=bearer("ana@x.com", "viewer")).json()
    assert masked["piiRestricted"] is True
    assert masked["document"].endswith("90") and "12.345" not in masked["document"]
    assert masked["email"] == "•" * len("c@teste.com")
    assert masked["street"] is None
    full = c.get("/api/v1/erp/customers/10", headers=bearer("vend@x.com", "viewer")).json()
    assert full["document"] == "12.345.678/0001-90" and full["email"] == "c@teste.com"
    listing = c.get("/api/v1/erp/customers", headers=bearer("ana@x.com", "viewer")).json()
    assert listing["items"][0]["document"] != "12.345.678/0001-90"
    assert listing["totalItems"] == 1 and listing["sort"] == "name"


def test_capabilities_matrix_is_complete_and_honest():
    response = client().get("/api/v1/erp/capabilities", headers=bearer())
    items = {i["key"]: i for i in response.json()["items"]}
    for alias in (
        "customers", "products", "orders", "price-tables", "payment-conditions",
        "carriers", "commercial-policies", "categories", "segments", "order-types",
        "product-prices", "users",
    ):
        assert items[f"read.{alias}"]["implementedInErp"] is True
    required = {
        "accountAccess", "supportedByAdaptor", "documentedByProvider",
        "implementedInErp", "enabled", "lastValidatedAt", "reason",
    }
    assert required <= set(items["read.titles"])
    # pendente de extensão do Adaptor: nunca "habilitado"
    assert items["read.titles"]["enabled"] is False
    assert items["write.billing"]["implementedInErp"] is False
    assert items["write.customers"]["enabled"] is False
    assert "ERP_WRITE_CUSTOMERS" in items["write.customers"]["reason"]


def test_write_blocked_until_flag_then_outbox_202(erp_cfg, erp_session_factory):
    seed_customer(erp_session_factory)
    c = client()
    headers = {**bearer(), "Idempotency-Key": "chave-001-abcdef"}
    blocked = c.post("/api/v1/erp/customers", json={"name": "Novo"}, headers=headers)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "capability_disabled"

    erp_cfg(erp_write_customers=True)
    no_key = c.post("/api/v1/erp/customers", json={"name": "Novo"}, headers=bearer())
    assert no_key.status_code == 422
    created = c.post(
        "/api/v1/erp/customers",
        json={"name": "Novo", "document": "AB12CD34000199", "email": "n@x.com"},
        headers=headers,
    )
    assert created.status_code == 202
    body = created.json()
    assert body["status"] == "queued" and body["synchronized"] is False
    assert body["statusUrl"].endswith(body["operationId"])
    replay = c.post(
        "/api/v1/erp/customers",
        json={"name": "Novo", "document": "AB12CD34000199", "email": "n@x.com"},
        headers=headers,
    )
    assert replay.status_code == 200 and replay.json()["operationId"] == body["operationId"]
    assert replay.json()["replayed"] is True
    conflict = c.post(
        "/api/v1/erp/customers", json={"name": "Outro corpo"}, headers=headers
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_key_reuse"
    with erp_session_factory() as db:
        ops = db.scalars(select(ErpOperation)).all()
        assert len(ops) == 1
        assert ops[0].payload["mercos"]["razao_social"] == "Novo"
        assert ops[0].payload["mercos"]["cnpj"] == "AB12CD34000199"  # alfanumérico preservado
        actions = [e.action for e in db.scalars(select(ErpAuditEvent))]
        assert "operation.create_customer" in actions


def test_patch_requires_matching_version_and_blocks_incomplete_order(erp_cfg, erp_session_factory):
    erp_cfg(erp_write_customers=True)
    seed_customer(erp_session_factory)
    c = client()
    headers = {**bearer(), "Idempotency-Key": "chave-002-abcdef"}
    stale = c.patch(
        "/api/v1/erp/customers/10", json={"expectedVersion": 9, "city": "Rio"}, headers=headers
    )
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "version_mismatch"
    ok = c.patch(
        "/api/v1/erp/customers/10", json={"expectedVersion": 1, "city": "Rio"}, headers=headers
    )
    assert ok.status_code == 202
    with erp_session_factory() as db:
        op = db.scalar(select(ErpOperation))
        assert op.base_snapshot == {"city": "São Paulo"}
        assert op.payload["mercos"] == {"cidade": "Rio"}


def test_validation_errors_use_422(erp_cfg):
    erp_cfg(erp_write_customers=True)
    c = client()
    response = c.post(
        "/api/v1/erp/customers",
        json={"name": "X", "document": "12$%34"},
        headers={**bearer(), "Idempotency-Key": "chave-003-abcdef"},
    )
    assert response.status_code == 422


def test_unimplemented_capabilities_return_409_not_fake_success():
    c = client()
    for path, method in (
        ("/api/v1/erp/sales-orders/1/cancel", "post"),
        ("/api/v1/erp/sales-orders/1/billings", "post"),
        ("/api/v1/erp/products", "post"),
    ):
        response = getattr(c, method)(path, headers=bearer())
        assert response.status_code == 409, path
        assert response.json()["detail"]["code"] == "capability_disabled"
    titles = c.get("/api/v1/erp/external-titles", headers=bearer()).json()
    assert titles["items"] == [] and titles["availability"]["enabled"] is False
    assert "ausência não é zero" in titles["note"].lower()
    assert titles["availability"]["reason"]


def test_cors_allows_patch_preflight_for_erp():
    response = client().options(
        "/api/v1/erp/customers/10",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "idempotency-key,authorization",
        },
    )
    assert response.status_code == 200
    assert "PATCH" in response.headers["access-control-allow-methods"]


def test_orders_conflicts_and_intents_hide_personal_data_without_pii_permission(erp_cfg, erp_session_factory):
    from app.erp.models import ErpConflict, ErpSalesOrder

    erp_cfg(erp_write_customers=True)
    add_operator(erp_session_factory, "ana@x.com", ["consulta"])
    add_operator(erp_session_factory, "vend@x.com", ["comercial"])
    with erp_session_factory() as db:
        db.add(ErpSalesOrder(
            connection_id="test", external_id="9", number="9", items_complete=True, fingerprint="f",
            captured_at=datetime.now(timezone.utc), notes="ligar para 11 99999-0000",
            shipping_address={"rua": "Rua A", "cep": "01000-000"}, extras={"cpf": "123"},
        ))
        db.add(ErpConflict(
            connection_id="test", entity_type="customers", entity_external_id="10",
            fields=["email", "city"], base={"email": "a@x.com", "city": "SP"},
            local={"email": "b@x.com", "city": "Rio"}, external={"email": "c@x.com", "city": "BH"},
        ))
        db.commit()
    c = client()
    ana, vend = bearer("ana@x.com", "viewer"), bearer("vend@x.com", "viewer")
    hidden = c.get("/api/v1/erp/sales-orders/9", headers=ana).json()
    assert hidden["notes"] is None and hidden["shippingAddress"] is None and hidden["extras"] is None
    assert hidden["piiRestricted"] is True
    shown = c.get("/api/v1/erp/sales-orders/9", headers=vend).json()
    assert shown["shippingAddress"]["rua"] == "Rua A" and shown["notes"]
    conflict = c.get("/api/v1/erp/integration/conflicts", headers=ana).json()["items"][0]
    flat = str(conflict)
    assert "a@x.com" not in flat and "b@x.com" not in flat and "c@x.com" not in flat
    assert conflict["base"]["city"] == "SP"  # campo não pessoal continua visível
    full = c.get("/api/v1/erp/integration/conflicts", headers=vend).json()["items"][0]
    assert full["base"]["email"] == "a@x.com"
    # intenção da operação: o corpo enviado pelo comercial não vaza para a consulta
    created = c.post("/api/v1/erp/customers", json={"name": "N", "email": "n@x.com", "document": "AB123456"},
                     headers={**vend, "Idempotency-Key": "pii-intent-0001"}).json()
    url = f"/api/v1/erp/integration/operations/{created['operationId']}"
    assert c.get(url, headers=ana).status_code == 403  # operação de outro operador
    seen = c.get(url, headers=bearer()).json()["intent"]
    assert seen["email"] == "n@x.com"  # admin (pii) vê
