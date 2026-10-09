"""Tarefa 1 do plano: consulta operacional de pedidos."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.erp.db import session_scope
from app.erp.models import ErpCustomer, ErpSalesOrder, ErpSalesOrderItem, ErpSeller, ErpSyncCheckpoint
from tests.erp.helpers import add_operator, bearer, client

C = "test"


def put_order(db, ext, *, customer="c1", number=None, net="100.00", gross=None, kind="order",
              issue=date(2026, 10, 5), complete=True, payment=None, connection=C, seller=None, items=()):
    order = ErpSalesOrder(
        connection_id=connection, external_id=ext, number=number or ext, kind=kind,
        customer_external_id=customer, net_total=Decimal(net), gross_total=Decimal(gross or net),
        issue_date=issue, issued_at=datetime(issue.year, issue.month, issue.day, 12, tzinfo=timezone.utc),
        items_complete=complete, payment_status=payment, seller_external_id=seller,
        commercial_status="2", item_count=len(items),
    )
    db.add(order)
    db.flush()
    for position, (name, code) in enumerate(items):
        db.add(ErpSalesOrderItem(order_id=order.id, position=position, name=name, code=code,
                                 quantity=Decimal("1"), external_id=f"{ext}-{position}"))
    return order


@pytest.fixture
def seeded(erp_cfg, erp_session_factory):
    erp_cfg()
    with session_scope() as db:
        db.add(ErpCustomer(connection_id=C, external_id="c1", name="Mercado Central"))
        db.add(ErpCustomer(connection_id=C, external_id="c2", name="Padaria Aurora"))
        db.add(ErpSeller(connection_id=C, external_id="s1", name="Vendedora Ana"))
        put_order(db, "1", customer="c1", seller="s1", payment="paid",
                  items=[("Cerveja Pilsen 600ml", "CP-600"), ("Cerveja Pilsen lata", "CP-350")])
        put_order(db, "2", customer="c2", issue=date(2026, 10, 6), items=[("Refrigerante Cola", "RC-2L")])
        put_order(db, "3", customer="c1", complete=False, issue=date(2026, 10, 7))
        put_order(db, "4", customer="ghost", issue=date(2026, 10, 8), net="50.00")  # cliente não sincronizado
        put_order(db, "9", customer="c1", connection="outra-conexao", items=[("Cerveja Pilsen 600ml", "CP-600")])


def get(path, **params):
    return client().get(f"/api/v1/erp{path}", params=params, headers=bearer())


def test_search_by_product_name_sku_and_customer(seeded):
    two_matching_items = get("/sales-orders", search="cerveja").json()
    assert [i["id"] for i in two_matching_items["items"]] == ["1"]  # 2 itens casam, 1 pedido
    assert two_matching_items["totalItems"] == 1
    assert [i["id"] for i in get("/sales-orders", search="rc-2l").json()["items"]] == ["2"]
    assert {i["id"] for i in get("/sales-orders", search="mercado").json()["items"]} == {"1", "3"}
    assert [i["id"] for i in get("/sales-orders", search="4").json()["items"]] == ["4"]  # número, como antes


def test_connection_isolation_and_customer_filter(seeded):
    ids = {i["id"] for i in get("/sales-orders", page_size=100).json()["items"]}
    assert "9" not in ids  # outra conexão nunca aparece
    assert {i["id"] for i in get("/sales-orders", customerId="c2").json()["items"]} == {"2"}
    assert get("/sales-orders", search="cerveja", customerId="c2").json()["totalItems"] == 0


def test_pagination_is_stable_with_search(seeded):
    first = get("/sales-orders", search="o", page=1, page_size=2, sort="number", order="asc").json()
    second = get("/sales-orders", search="o", page=2, page_size=2, sort="number", order="asc").json()
    seen = [i["id"] for i in first["items"]] + [i["id"] for i in second["items"]]
    assert len(seen) == len(set(seen))  # sem repetição entre páginas


def test_operational_view_has_separated_states_and_pendencies(seeded):
    body = get("/sales-orders", include="operational", page_size=100).json()
    by_id = {i["id"]: i for i in body["items"]}
    one = by_id["1"]["operational"]
    assert one["customerName"] == "Mercado Central" and one["responsible"]["name"] == "Vendedora Ana"
    assert len(one["itemsPreview"]) == 2 and one["totals"]["net"] == "100.00"
    assert one["state"]["code"] == "in_progress"  # nunca "completed"
    assert one["payment"] == {"state": "paid", "source": "mercos_mirror"}
    assert one["pix"]["state"] == "unknown"  # Pix desconhecido não é pendente nem gerado
    assert by_id["1"]["statuses"]["commercial"] == "2"  # estado Mercos preservado à parte
    assert [p["code"] for p in by_id["3"]["operational"]["pendencies"]] == ["items_incomplete"]
    assert [p["code"] for p in by_id["4"]["operational"]["pendencies"]] == ["customer_missing"]
    assert by_id["1"]["operational"]["lastHumanAction"] is None
    assert "operational" not in get("/sales-orders").json()["items"][0]  # contrato antigo intacto


def test_pending_and_payment_filters(seeded):
    assert {i["id"] for i in get("/sales-orders", pending=True).json()["items"]} == {"3", "4"}
    assert {i["id"] for i in get("/sales-orders", pending=False).json()["items"]} == {"1", "2"}
    assert [i["id"] for i in get("/sales-orders", paymentStatus="paid").json()["items"]] == ["1"]
    assert {i["id"] for i in get("/sales-orders", itemsComplete=False).json()["items"]} == {"3"}


def test_summary_aggregates_the_whole_filtered_set_not_one_page(seeded):
    with session_scope() as db:
        for n in range(100, 160):  # 60 pedidos extras, bem acima de uma página
            put_order(db, str(n), net="10.00", issue=date(2026, 10, 3))
    summary = get("/operational-summary").json()
    assert summary["orders"]["count"] == 64
    assert summary["values"]["net"] == "950.00"  # 100+100+100+50+60*10, via Decimal
    page = get("/sales-orders", page_size=5).json()
    assert len(page["items"]) == 5 and page["totalItems"] == 64
    filtered = get("/operational-summary", dateFrom="2026-10-03", dateTo="2026-10-03").json()
    assert filtered["orders"]["count"] == 60 and filtered["values"]["net"] == "600.00"
    assert filtered["filters"]["dateFrom"] == "2026-10-03"


def test_summary_marks_unavailable_metrics_instead_of_zero(seeded):
    summary = get("/operational-summary").json()
    assert summary["variation"]["available"] is False and "período" in summary["variation"]["reason"]
    assert summary["payment"]["pix"]["available"] is False
    assert summary["coverage"]["complete"] is False  # nada sincronizado de forma completa
    assert summary["coverage"]["note"]
    assert summary["payment"]["byStatus"] == {"paid": 1, "unknown": 3}
    assert summary["pendencies"] == {"itemsIncomplete": 1, "customerMissing": 1, "any": 2}


def test_variation_needs_complete_coverage_and_a_comparable_base(seeded):
    params = {"dateFrom": "2026-10-05", "dateTo": "2026-10-08"}
    partial = get("/operational-summary", **params).json()["variation"]
    assert partial["available"] is False and "parcial" in partial["reason"]
    with session_scope() as db:
        for resource in ("orders", "customers"):
            db.add(ErpSyncCheckpoint(connection_id=C, resource=resource, scope="", status="success"))
        put_order(db, "70", issue=date(2026, 10, 2), net="200.00")  # período anterior (01 a 04/10)
    varied = get("/operational-summary", **params).json()
    assert varied["coverage"]["complete"] is True
    assert varied["variation"]["available"] is True
    assert varied["variation"]["previousPeriod"] == {"from": "2026-10-01", "to": "2026-10-04"}
    assert varied["variation"]["percent"] == "75.00"  # (350-200)/200
    no_base = get("/operational-summary", dateFrom="2026-09-20", dateTo="2026-09-25").json()["variation"]
    assert no_base["available"] is False  # período sem pedidos: sem base, não vira zero


def test_read_permission_is_required(seeded, erp_session_factory):
    add_operator(erp_session_factory, "sem@x.com", [])
    denied = client().get("/api/v1/erp/operational-summary", headers=bearer("sem@x.com", "viewer"))
    assert denied.status_code in (401, 403)


def test_detail_includes_operational_and_history_separates_human_from_mirror(seeded):
    from app.erp.audit import audit

    with session_scope() as db:
        audit(db, operator="ana@x.com", action="shipping.select", connection_id=C,
              resource="sales_order", resource_id="1", reason="Menor prazo")
        audit(db, operator="outra@x.com", action="shipping.select", connection_id=C,
              resource="sales_order", resource_id="2")
    detail = get("/sales-orders/1").json()
    assert detail["operational"]["customerName"] == "Mercado Central"
    assert detail["operational"]["lastHumanAction"]["operator"] == "ana@x.com"
    history = get("/sales-orders/1/history").json()["items"]
    assert [(h["kind"], h["operator"], h["action"], h["reason"]) for h in history] == [
        ("local", "ana@x.com", "shipping.select", "Menor prazo")
    ]  # nada do pedido 2 e nada da sincronização
    assert get("/sales-orders/ghost/history").status_code == 404
