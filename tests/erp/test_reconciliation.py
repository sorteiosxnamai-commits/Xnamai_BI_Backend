"""Conciliação somente leitura ERP × BI: mesmo ID Mercos, mesmo escopo, mesmo instante; sem escrita."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app import models as bi
from app.erp.db import session_scope
from app.erp.models import ErpCustomer, ErpProduct, ErpSalesOrder, ErpSalesOrderItem, ErpSyncCheckpoint
from tests.erp.helpers import add_operator, bearer, client

C = "test"
LEGACY = (bi.Customer, bi.Product, bi.Order, bi.OrderItem, bi.SyncState)


def at(day, hour=10):
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc)


@pytest.fixture
def both(erp_cfg, erp_session_factory):
    erp_cfg()
    with erp_session_factory() as db:
        bind = db.get_bind()
    tables = [model.__table__ for model in LEGACY]
    bi.Base.metadata.create_all(bind, tables=tables)
    try:
        with session_scope() as db:
            # --- clientes
            db.add_all([
                ErpCustomer(connection_id=C, external_id="c1", name="Mercado Central", document="12.345.678/0001-90", active=True, source_updated_at=at(5)),
                ErpCustomer(connection_id=C, external_id="c2", name="Padaria Aurora", document="111", active=True, source_updated_at=at(5)),
                ErpCustomer(connection_id=C, external_id="c4", name="Só no ERP", active=True, source_updated_at=at(11)),
            ])
            db.add_all([
                bi.Customer(mercos_id="c1", name="MERCADO  central", document="12345678000190", active=True, source_updated_at=at(5)),
                bi.Customer(mercos_id="c2", name="Padaria Aurora", document="222", active=False, source_updated_at=at(5)),
                bi.Customer(mercos_id="c3", name="Só no BI", active=True, source_updated_at=at(5)),
            ])
            # --- produtos
            db.add_all([ErpProduct(connection_id=C, external_id="p1", code="A-1", name="Vaso", list_price=Decimal("10.00"), active=True, source_updated_at=at(5))])
            db.add_all([bi.Product(mercos_id="p1", code="A-2", name="Vaso", list_price=Decimal("12.00"), active=True, source_updated_at=at(5))])
            # --- pedidos
            def erp_order(ext, **kw):
                defaults = dict(connection_id=C, external_id=ext, number=ext, kind="order", net_total=Decimal("100.00"),
                                gross_total=Decimal("100.00"), discount_total=Decimal("0.00"), customer_external_id="c1",
                                seller_external_id="s1", issue_date=date(2026, 10, 3), items_complete=True, item_count=1,
                                source_updated_at=at(5), commercial_status="2")
                defaults.update(kw)
                order = ErpSalesOrder(**defaults)
                db.add(order)
                db.flush()
                if defaults["items_complete"]:
                    db.add(ErpSalesOrderItem(order_id=order.id, position=0, external_id=f"{ext}-0", name="x", quantity=Decimal("1"),
                                             total=defaults["net_total"] or Decimal("0"), excluded=False))
                return order

            def bi_order(ext, **kw):
                defaults = dict(mercos_id=ext, number=ext, customer_mercos_id="c1", seller_mercos_id="s1", status="2",
                                issued_at=datetime(2026, 10, 3, 15, tzinfo=timezone.utc), total=Decimal("100.00"),
                                net_total=Decimal("100.00"), gross_total=Decimal("100.00"), discount_value=Decimal("0.00"),
                                item_count=1, source_updated_at=at(5))
                defaults.update(kw)
                db.add(bi.Order(**defaults))
                db.add(bi.OrderItem(order_mercos_id=ext, position=0, mercos_item_id=f"{ext}-0", name="x",
                                    quantity=Decimal("1"), total=defaults["net_total"] or Decimal("0"), excluded=False))

            erp_order("o1")
            bi_order("o1")  # idêntico
            erp_order("o2")
            bi_order("o2", net_total=Decimal("90.00"), source_updated_at=at(5))  # mesma versão, valor diferente
            erp_order("o3", net_total=Decimal("50.00"), source_updated_at=at(4))
            bi_order("o3", net_total=Decimal("60.00"), source_updated_at=at(5))  # versões diferentes
            erp_order("o4", kind="cancelled")
            bi_order("o4")  # cancelamento diverge
            erp_order("o5", customer_external_id="gone")  # só no ERP, dentro do cursor do BI
            bi_order("o6", source_updated_at=at(6))  # só no BI, antes do cursor do ERP
            bi_order("o7", source_updated_at=at(12))  # só no BI, depois do cursor do ERP (carga não alcançou)
            erp_order("o8", items_complete=False, net_total=Decimal("0.00"), item_count=None)
            bi_order("o8", net_total=Decimal("77.00"))  # ERP incompleto
            erp_order("o9", customer_external_id="c3")
            bi_order("o9", customer_mercos_id="c3")  # cliente c3 só no BI
            # cursores confirmados dos dois lados
            db.add(ErpSyncCheckpoint(connection_id=C, resource="orders", scope="", status="running", cursor="2026-10-10T00:00:00"))
            db.add(bi.SyncState(resource="orders", cursor="2026-10-20T00:00:00", status="success"))
        yield
    finally:
        bi.Base.metadata.drop_all(bind, tables=list(reversed(tables)))


def get(path, user="admin@xnamai.com", role="admin", **params):
    return client().get(f"/api/v1/erp/integration/reconciliation{path}", params=params, headers=bearer(user, role))


def by_code(body):
    return {d["code"]: d for d in body["divergences"]}


def test_orders_are_compared_by_mercos_id_with_explained_and_unexplained_divergences(both):
    body = get("/orders").json()
    assert body["readOnly"] is True and body["erp"]["count"] + 0 >= 7
    codes = by_code(body)
    # mesma versão (mesmo instante), valor diferente → INEXPLICADA
    assert codes["net_total_mismatch"]["unexplained"] == 1
    assert [s["id"] for s in codes["net_total_mismatch"]["samples"] if not s["explained"]] == ["o2"]
    # versões diferentes → explicada (um lado está desatualizado)
    assert any(s["id"] == "o3" and s["explained"] for s in codes["net_total_mismatch"]["samples"])
    assert codes["kind_mismatch"]["count"] == 1 and codes["kind_mismatch"]["samples"][0]["erp"] == "cancelled"
    # só no BI: antes do cursor do ERP é inexplicada; depois do cursor, a carga ainda não chegou (explicada)
    only_bi = {s["id"]: s["explained"] for s in codes["only_in_bi"]["samples"]}
    assert only_bi == {"o6": False}  # o7 está depois do instante comum e nem entra na comparação
    # cliente que existe no BI e falta no ERP
    assert [s["customerId"] for s in codes["customer_link_gap"]["samples"]] == ["c3"]
    # ERP incompleto não é comparado como se o total fosse zero
    assert "net_total_mismatch" in codes and all(s["id"] != "o8" for s in codes["net_total_mismatch"]["samples"])
    assert any("sem itens" in note for note in body["notes"])
    for item in body["divergences"]:
        assert item["cause"] and item["impact"] and item["fix"]


def test_same_instant_scope_uses_the_lower_confirmed_cursor_and_can_be_overridden(both):
    body = get("/orders").json()
    assert body["asOf"].startswith("2026-10-10") and "menor cursor" in body["asOfBasis"]
    ids = {s["id"] for d in body["divergences"] for s in d["samples"]}
    assert "o7" not in ids  # alterado depois do instante comum: fora da comparação nos dois lados
    wide = get("/orders", asOf="2026-12-31T00:00:00Z", sample=50).json()
    assert wide["asOfBasis"] == "informado"
    assert "o7" in {s["id"] for d in wide["divergences"] for s in d["samples"]}
    periodic = get("/orders", dateFrom="2026-11-01", dateTo="2026-11-30").json()
    assert periodic["erp"]["count"] == 0 and periodic["matched"] == 0  # período sem dados nos dois lados


def test_reconciliation_never_writes_to_either_side(both):
    with session_scope() as db:
        before = (db.scalar(select(func.count(ErpSalesOrder.id))), db.scalar(select(func.count(bi.Order.id))),
                  db.scalar(select(func.max(ErpSalesOrder.version))))
    for resource in ("orders", "customers", "products"):
        assert get(f"/{resource}").status_code == 200
    with session_scope() as db:
        after = (db.scalar(select(func.count(ErpSalesOrder.id))), db.scalar(select(func.count(bi.Order.id))),
                 db.scalar(select(func.max(ErpSalesOrder.version))))
    assert before == after


def test_customers_and_products_compare_names_status_and_documents_without_leaking_values(both, erp_session_factory):
    customers = by_code(get("/customers").json())
    assert "name_mismatch" not in customers  # espaços e caixa não contam
    assert customers["active_mismatch"]["samples"][0]["id"] == "c2"
    assert customers["document_mismatch"]["count"] == 1 and customers["document_mismatch"]["samples"] == [
        {"id": "c2", "explained": False}
    ]  # só o ID: documento nunca sai
    assert customers["only_in_bi"]["samples"][0]["id"] == "c3"
    products = by_code(get("/products").json())
    assert products["code_mismatch"]["count"] == 1 and products["price_mismatch"]["count"] == 1


def test_permissions_and_unknown_resource(both, erp_session_factory):
    assert get("/pedidos").status_code == 404
    add_operator(erp_session_factory, "estoque@x.com", ["estoque"])  # tem integration:read
    assert get("/orders", "estoque@x.com", "viewer").status_code == 200
    assert client().get("/api/v1/erp/integration/reconciliation/orders").status_code == 401
    assert get("/orders", dateFrom="2026-12-01", dateTo="2026-11-01").status_code == 422


def test_a_scope_that_is_too_large_is_refused_instead_of_scanning_everything(both, monkeypatch):
    from app.erp.services import reconciliation as rec

    monkeypatch.setattr(rec, "MAX_SCOPE_ROWS", 3)
    refused = get("/orders")
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "scope_too_large"
    ok = get("/orders", dateFrom="2026-10-03", dateTo="2026-10-03")
    assert ok.status_code == 422  # ainda acima do limite artificial; o período curto de produção cabe no limite real
    monkeypatch.setattr(rec, "MAX_SCOPE_ROWS", 60_000)
    assert get("/orders", dateFrom="2026-10-03", dateTo="2026-10-03").status_code == 200


def test_item_and_customer_lookups_are_scoped_to_the_compared_orders(both):
    """Pedido fora do período não influencia a conferência (itens/clientes só do escopo)."""
    out = get("/orders", dateFrom="2026-11-01", dateTo="2026-11-30").json()
    assert out["erp"]["count"] == 0 and out["divergences"] == []
