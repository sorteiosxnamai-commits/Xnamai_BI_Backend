import asyncio

import pytest

from app.erp.integrations.mercos_client import ListPage
from app.erp.registry import SYNC_ORDER
from app.erp.sync import engine
from tests.erp.helpers import bearer, client

TS = "2026-10-07T10:00:00"

FIXTURES = {
    "categories": [{"id": 1, "nome": "Bebidas", "ultima_alteracao": TS}],
    "segments": [{"id": 2, "nome": "Varejo", "ultima_alteracao": TS}],
    "order-types": [{"id": 3, "nome": "Venda", "ultima_alteracao": TS}],
    "payment-conditions": [{"id": 4, "nome": "30 dias", "numero_parcelas": 1, "ultima_alteracao": TS}],
    "price-tables": [{"id": 5, "nome": "Tabela A", "tipo": "livre", "ultima_alteracao": TS}],
    "carriers": [{"id": 6, "nome": "Transp", "cidade": "SP", "ultima_alteracao": TS}],
    "commercial-policies": [{"id": 7, "nome": "Política", "slug": "politica", "ultima_alteracao": TS}],
    "users": [{"id": 8, "nome": "Vendedora", "email": "v@x.com", "ultima_alteracao": TS}],
    "customers": [
        {"id": 10, "razao_social": "Cliente X", "cnpj": "11.222.333/0001-44", "segmento_id": 2,
         "contatos": [{"id": 1, "nome": "Ana"}], "ultima_alteracao": TS},
    ],
    "products": [
        {"id": 20, "codigo": "P-1", "nome": "Produto 1", "categoria_id": 1, "preco_tabela": "9.90",
         "saldo_estoque": "12", "ativo": True, "ultima_alteracao": TS},
    ],
    "product-prices": [{"produto_id": 20, "tabela_preco_id": 5, "preco": "8.50", "ultima_alteracao": TS}],
    "orders": [
        {"id": 30, "numero": "100", "cliente_id": 10, "criador_id": 8, "status": 2, "total": "99.00",
         "data_emissao": "2026-10-06", "itens": [{"id": 1, "produto_id": 20, "quantidade": 2,
                                                  "preco_liquido": "49.50"}],
         "ultima_alteracao": TS},
        {"id": 31, "numero": "101", "cliente_id": 404, "status": 1, "total": "10", "ultima_alteracao": TS},
    ],
}


class Fixture:
    async def list_page(self, alias, cursor):
        rows = FIXTURES[alias]
        return ListPage(alias, len(rows), TS, None, rows)


@pytest.fixture
def seeded(erp_cfg, erp_session_factory):
    erp_cfg()
    for alias in SYNC_ORDER:
        result = asyncio.run(engine.sync_resource(Fixture(), "test", alias))
        assert result.status == "success", (alias, result.error)


GETS = [
    "/api/v1/erp/me",
    "/api/v1/erp/capabilities",
    "/api/v1/erp/overview",
    "/api/v1/erp/customers",
    "/api/v1/erp/customers/10",
    "/api/v1/erp/products",
    "/api/v1/erp/products/20",
    "/api/v1/erp/products/20/prices",
    "/api/v1/erp/products/20/variants",
    "/api/v1/erp/product-prices",
    "/api/v1/erp/sales-orders",
    "/api/v1/erp/sales-orders/30",
    "/api/v1/erp/external-titles",
    "/api/v1/erp/payments",
    "/api/v1/erp/commissions",
    "/api/v1/erp/promotions",
    "/api/v1/erp/integration/status",
    "/api/v1/erp/integration/runs",
    "/api/v1/erp/integration/operations",
    "/api/v1/erp/integration/conflicts",
    "/api/v1/erp/integration/quarantine",
    "/api/v1/erp/integration/fields",
    "/api/v1/erp/integration/webhooks",
    "/api/v1/erp/integration/snapshots/customers/10",
    "/api/v1/erp/suppliers",
    "/api/v1/erp/purchase-orders",
    "/api/v1/erp/inventory/authority",
    "/api/v1/erp/inventory/warehouses",
    "/api/v1/erp/inventory/balances",
    "/api/v1/erp/inventory/movements",
    "/api/v1/erp/inventory/reservations",
    "/api/v1/erp/finance/accounts",
    "/api/v1/erp/finance/categories",
    "/api/v1/erp/finance/cost-centers",
    "/api/v1/erp/finance/titles",
    "/api/v1/erp/finance/cash-flow",
    "/api/v1/erp/operators",
    "/api/v1/erp/audit-events",
] + [f"/api/v1/erp/catalogs/{alias}" for alias in (
    "categories", "segments", "order-types", "payment-conditions", "price-tables",
    "carriers", "commercial-policies", "users")]


@pytest.mark.parametrize("path", GETS)
def test_every_get_endpoint_responds(seeded, path):
    response = client().get(path, headers=bearer())
    assert response.status_code == 200, (path, response.text[:300])


def test_overview_reports_coverage_and_pending_references(seeded):
    data = client().get("/api/v1/erp/overview", headers=bearer()).json()
    by_resource = {r["resource"]: r for r in data["resources"]}
    assert len(by_resource) == 12 and all(r["status"] == "success" for r in by_resource.values())
    assert by_resource["orders"]["records"] == 2
    assert data["dataThrough"] is not None
    pending = {(p["resource"], p["field"]): p["pending"] for p in data["pendingReferences"]}
    assert pending[("orders", "customerId")] == 1  # cliente 404 ainda não veio: pendente, não fabricado


def test_overview_without_sync_has_unavailable_not_zero(erp_cfg, erp_session_factory):
    erp_cfg()
    data = client().get("/api/v1/erp/overview", headers=bearer()).json()
    assert data["dataThrough"] is None
    assert all(r["records"] is None and r["status"] == "never" for r in data["resources"])


def test_orders_list_filters_statuses_and_incomplete_marking(seeded):
    c = client()
    page = c.get("/api/v1/erp/sales-orders?page_size=1&sort=number&order=asc", headers=bearer()).json()
    assert page["pageSize"] == 1 and page["totalItems"] == 2 and page["totalPages"] == 2
    assert page["items"][0]["number"] == "100"
    quotes = c.get("/api/v1/erp/sales-orders?kind=quote", headers=bearer()).json()
    assert [i["id"] for i in quotes["items"]] == ["31"] and quotes["items"][0]["itemsComplete"] is False
    detail = c.get("/api/v1/erp/sales-orders/31", headers=bearer()).json()
    assert detail["incomplete"] is True and "Itens" in detail["incompleteReason"]
    assert set(detail["statuses"]) == {"commercial", "billing", "fulfillment", "payment"}
    full = c.get("/api/v1/erp/sales-orders/30", headers=bearer()).json()
    assert full["netTotal"] == "99.00" and full["items"][0]["quantity"] == "2"
    assert full["customerName"] == "Cliente X"
    # ordenação fora da allowlist cai no padrão (sem injeção)
    safe = c.get("/api/v1/erp/sales-orders?sort=id;drop", headers=bearer())
    assert safe.status_code == 200 and safe.json()["sort"] == "issuedAt"


def test_catalog_allowlist_and_unknown_resource(seeded):
    c = client()
    assert c.get("/api/v1/erp/catalogs/customers", headers=bearer()).status_code == 404
    assert c.get("/api/v1/erp/catalogs/price-tables", headers=bearer()).json()["items"][0]["price_type"] == "livre"
    assert c.get("/api/v1/erp/catalogs/users", headers=bearer()).json()["items"][0]["name"] == "Vendedora"


def test_money_is_decimal_string_and_cost_is_unknown(seeded):
    product = client().get("/api/v1/erp/products/20", headers=bearer()).json()
    assert product["listPrice"] == "9.90" and product["externalStock"] == "12"
    assert product["cost"] is None  # custo desconhecido permanece desconhecido
    prices = client().get("/api/v1/erp/products/20/prices", headers=bearer()).json()
    assert prices["items"][0]["price"] == "8.50" and prices["items"][0]["priceTableName"] == "Tabela A"


def test_snapshots_are_admin_only(seeded, erp_session_factory):
    from tests.erp.helpers import add_operator

    add_operator(erp_session_factory, "com@x.com", ["comercial"])
    denied = client().get("/api/v1/erp/integration/snapshots/customers/10", headers=bearer("com@x.com", "viewer"))
    assert denied.status_code == 403
    allowed = client().get("/api/v1/erp/integration/snapshots/customers/10", headers=bearer())
    assert allowed.json()["items"][0]["payload"]["razao_social"] == "Cliente X"


def test_unmapped_fields_are_visible_in_inventory(erp_cfg, erp_session_factory):
    erp_cfg()

    class WithNew:
        async def list_page(self, alias, cursor):
            return ListPage(alias, 1, TS, None,
                            [{"id": 1, "nome": "X", "campo_futuro": 1, "ultima_alteracao": TS}])

    asyncio.run(engine.sync_resource(WithNew(), "test", "segments"))
    items = client().get("/api/v1/erp/integration/fields?mapped=false", headers=bearer()).json()["items"]
    assert [i["sourceKey"] for i in items] == ["campo_futuro"]
    status = client().get("/api/v1/erp/integration/status", headers=bearer()).json()
    segments = next(r for r in status["resources"] if r["resource"] == "segments")
    assert segments["unmappedFields"] == 1
