"""Correções de apresentação: totais desconhecidos, ações por estado, referências, documentos e fases."""

import json
from decimal import Decimal
from datetime import datetime, timedelta, timezone

import pytest

from app.erp.auth import ErpUser
from app.erp.db import session_scope
from app.erp.models import (
    ErpCarrier,
    ErpCustomer,
    ErpPaymentCondition,
    ErpSalesOrder,
    ErpSalesOrderItem,
    ErpSeller,
    ErpSyncCheckpoint,
    ErpSyncRun,
)
from app.erp.services import order_finance
from app.erp.services import order_operations as oo
from tests.erp.helpers import add_operator, bearer, client
from tests.erp.test_order_operations import C, put_order

CPF = "36447922837"
CNPJ_BASE = "39.491.679"


@pytest.fixture
def data(erp_cfg, erp_session_factory):
    erp_cfg()
    with session_scope() as db:
        db.add(ErpCustomer(connection_id=C, external_id="c1", name="Mercado Central", document="12.345.678/0001-90"))
        db.add(ErpCustomer(connection_id=C, external_id="c2", name=f"SAMUEL APARECIDO DE OLIVEIRA {CPF}", document=CPF))
        db.add(ErpCustomer(connection_id=C, external_id="c3", name=f"{CNPJ_BASE} JOAO VICTOR ROCHA TORRES"))
        db.add(ErpSeller(connection_id=C, external_id="s1", name="Vendedora Ana"))
        db.add(ErpPaymentCondition(connection_id=C, external_id="p1", name="30 dias"))
        db.add(ErpCarrier(connection_id=C, external_id="k1", name="Transportes Veloz"))
        put_order(db, "1", customer="c1", net="0.00", complete=False)  # incompleto: total desconhecido
        put_order(db, "2", customer="c1", net="0.00", items=[("Brinde", "B-1")])  # zero REAL (itens completos)
        put_order(db, "3", customer="c1", net="250.00", complete=False)  # incompleto, mas a origem trouxe o total
        put_order(db, "4", customer="c2", net="100.00", kind="cancelled", items=[("X", "X-1")])
        put_order(db, "5", customer="c3", net="80.00", kind="quote", items=[("Y", "Y-1")], seller="s1")
        put_order(db, "6", customer="c2", net="300.00", items=[("Z", "Z-1")], seller="s1")
        put_order(db, "7", customer="ghost", net="50.00", items=[("W", "W-1")])
        db.flush()
        for order in db.query(ErpSalesOrder).all():
            order.captured_at = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)
            order.source_updated_at = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
        seven = db.query(ErpSalesOrder).filter_by(external_id="6").one()
        seven.payment_condition_external_id = "p1"
        seven.carrier_external_id = "0"
        seven.price_table_external_id = "tp-ausente"
        seven.order_type_external_id = None
        six = db.query(ErpSalesOrder).filter_by(external_id="5").one()
        six.payment_condition_external_id = "pc-ausente"
        six.carrier_external_id = "k-ausente"


def get(path, user="admin@xnamai.com", role="admin", **params):
    return client().get(f"/api/v1/erp{path}", params=params, headers=bearer(user, role))


def restricted(erp_session_factory):
    add_operator(erp_session_factory, "consulta@x.com", ["consulta"])
    return "consulta@x.com", "viewer"


def test_unknown_totals_are_null_but_real_zeros_are_preserved(data):
    items = {i["id"]: i for i in get("/sales-orders", page_size=100).json()["items"]}
    assert items["1"]["netTotal"] is None and items["1"]["totalsUnknown"] is True  # incompleto + 0
    assert items["2"]["netTotal"] == "0.00" and items["2"]["totalsUnknown"] is False  # zero real
    assert items["3"]["netTotal"] == "250.00" and items["3"]["totalsUnknown"] is False  # origem trouxe o total
    detail = get("/sales-orders/1").json()
    assert detail["netTotal"] is None and detail["grossTotal"] is None and detail["freightTotal"] is None
    operational = get("/sales-orders", include="operational", page_size=100).json()["items"]
    first = next(i for i in operational if i["id"] == "1")["operational"]["totals"]
    assert first["net"] is None and first["unknown"] is True


def test_actions_follow_the_backend_rules_and_the_backend_still_refuses(data):
    ops = {i["id"]: i["operational"]["actions"] for i in
           get("/sales-orders", include="operational", page_size=100).json()["items"]}
    assert ops["4"]["shippingQuote" if "shippingQuote" in ops["4"] else "shipping"]["allowed"] is False
    cancelled = ops["4"]
    assert cancelled["shipping"]["code"] == "order_cancelled" and "cancelado" in cancelled["shipping"]["reason"]
    assert cancelled["invoice"]["code"] == "order_cancelled" and cancelled["receivable"]["code"] == "order_cancelled"
    assert ops["1"]["shipping"]["code"] == "items_incomplete" and ops["1"]["invoice"]["allowed"] is False
    assert ops["6"]["shipping"]["allowed"] and ops["6"]["invoice"]["allowed"] and ops["6"]["receivable"]["allowed"]
    assert ops["7"]["receivable"]["allowed"]  # cliente ainda não sincronizado ≠ pedido sem cliente
    # a validação do backend permanece, com os mesmos códigos
    http = client()
    headers = {**bearer(), "Idempotency-Key": "pres-key-0001"}
    quote = http.post("/api/v1/erp/sales-orders/4/shipping-quotes", json={
        "volumes": [{"weightKg": "1", "lengthCm": "10", "widthCm": "10", "heightCm": "10"}]}, headers=headers)
    assert quote.status_code == 409 and quote.json()["detail"]["code"] == "order_cancelled"
    draft = http.post("/api/v1/erp/sales-orders/4/invoice-drafts", json={"percent": "50", "organize": True},
                      headers={**bearer(), "Idempotency-Key": "pres-key-0002"})
    assert draft.status_code == 409 and draft.json()["detail"]["code"] == "order_cancelled"
    title = http.post("/api/v1/erp/sales-orders/4/receivable", json={"firstDueDate": "2026-12-01", "installments": 1},
                      headers={**bearer(), "Idempotency-Key": "pres-key-0003"})
    assert title.status_code == 409 and title.json()["detail"]["code"] == "order_cancelled"


def test_references_show_names_or_the_documented_reason(data):
    with session_scope() as db:
        for resource, status in (("carriers", "forbidden"), ("payment-conditions", "success"), ("price-tables", "running")):
            db.add(ErpSyncCheckpoint(connection_id=C, resource=resource, scope="", status=status,
                                     last_success_at=datetime.now(timezone.utc) if status == "success" else None))
    refs = get("/sales-orders/6").json()["references"]
    assert refs["seller"] == {"id": "s1", "name": "Vendedora Ana", "state": "resolved", "reasonCode": None, "reason": None}
    assert refs["paymentCondition"]["name"] == "30 dias" and refs["paymentCondition"]["state"] == "resolved"
    assert refs["orderType"]["state"] == "absent" and refs["orderType"]["id"] is None
    # 0 NÃO é tratado como ausência: o contrato não confirma esse significado
    assert refs["carrier"]["state"] == "unavailable" and refs["carrier"]["reasonCode"] == "zero_id_unconfirmed"
    assert refs["priceTable"]["reasonCode"] == "loading" and refs["priceTable"]["id"] == "tp-ausente"
    other = get("/sales-orders/5").json()["references"]
    assert other["carrier"]["reasonCode"] == "access_denied" and "403" in other["carrier"]["reason"]
    assert other["paymentCondition"]["reasonCode"] == "not_in_synced_data"
    assert get("/sales-orders/7").json()["references"]["customer"]["reasonCode"] in ("loading", "not_in_synced_data")


def test_pending_references_say_why_and_do_not_blame_the_order_load(data):
    with session_scope() as db:
        db.add(ErpSyncCheckpoint(connection_id=C, resource="carriers", scope="", status="forbidden"))
        db.add(ErpSyncCheckpoint(connection_id=C, resource="customers", scope="", status="success",
                                 last_success_at=datetime.now(timezone.utc)))
    status = get("/integration/status").json()
    refs = {(r["field"]): r for r in status["pendingReferences"]}
    assert refs["carrierId"]["reasonCode"] == "access_denied" and refs["carrierId"]["distinctIds"] == 2
    # cliente com carga concluída e ID ausente: terminar Pedidos NÃO resolve
    assert refs["customerId"]["reasonCode"] == "not_in_synced_data" and refs["customerId"]["pending"] == 1


def test_documents_embedded_in_names_are_redacted_without_pii_permission(data, erp_session_factory):
    user, role = restricted(erp_session_factory)
    for path, params in (
        ("/sales-orders", {"page_size": 100, "include": "operational"}),
        ("/sales-orders/6", {}),
        ("/sales-orders/5", {}),
        ("/customers", {"page_size": 100}),
        ("/customers/c2", {}),
        ("/customers/c3", {}),
    ):
        response = get(path, user, role, **params)
        assert response.status_code == 200, path
        body = response.text
        assert CPF not in body and CNPJ_BASE not in body, path  # nada de CPF/CNPJ na resposta da API
    names = {i["id"]: i["customerName"] for i in get("/sales-orders", user, role, page_size=100).json()["items"]}
    assert names["6"] == "SAMUEL APARECIDO DE OLIVEIRA •••"
    assert names["5"] == "••• JOAO VICTOR ROCHA TORRES"
    # quem tem a permissão continua vendo o dado original
    assert CPF in get("/sales-orders/6").text and CNPJ_BASE in get("/sales-orders/5").text
    assert CPF in get("/customers/c2").text


def test_restricted_search_cannot_probe_documents_in_names(data, erp_session_factory):
    user, role = restricted(erp_session_factory)
    assert get("/sales-orders", search=CPF).json()["totalItems"] == 2  # admin acha pelo CPF no nome (#4 e #6)
    assert get("/sales-orders", user, role, search=CPF).json()["totalItems"] == 0  # perfil restrito, não
    assert get("/sales-orders", user, role, search="samuel").json()["totalItems"] == 2  # nome continua buscável
    for probe in ("36447", "3644792", "447922", "836"):  # fragmentos curtos também não casam (sem oráculo)
        assert get("/sales-orders", user, role, search=probe).json()["totalItems"] == 0, probe
    # o mesmo vale para o relatório financeiro (defesa em profundidade, mesmo sem papel que combine)
    with session_scope() as db:
        body, _ = order_finance.report_csv(
            db, ErpUser("x", ["consulta"], permissions={"read"}), C, oo.OrderFilters(restricted=True)
        )
    assert CPF not in body and CNPJ_BASE not in body and "SAMUEL APARECIDO DE OLIVEIRA •••" in body
    unknown_row = [line for line in body.splitlines() if line.startswith("1;")][0]
    assert ";;" in unknown_row or unknown_row.split(";")[4] == ""  # total desconhecido sai vazio, não 0.00


def test_summary_separates_sold_from_cancelled_quotes_dates_and_unknown_totals(data):
    body = get("/operational-summary").json()
    values = body["values"]
    assert values["sold"]["count"] == 5 and values["sold"]["excludes"] == ["cancelled", "quote"]
    assert values["sold"]["net"] == "600.00"  # 0 + 0 + 250 + 300 + 50 (pedidos), sem cancelado/orçamento
    assert values["byKind"]["cancelled"] == {"count": 1, "net": "100.00"}
    assert values["byKind"]["quote"] == {"count": 1, "net": "80.00"}
    assert values["net"] == "780.00"  # total do filtro inclui os dois (rótulo próprio)
    assert values["sold"]["unknownTotals"] == 1  # só o #1 (incompleto e zerado)
    dates = body["dates"]
    assert dates["orderDate"]["min"] == "2026-10-05" and dates["sourceUpdatedAtMax"].startswith("2026-10-11")
    assert dates["capturedAtMax"].startswith("2026-10-12")


def test_resource_phases_do_not_count_attempts_or_403_as_completed(data):
    now = datetime.now(timezone.utc)
    rows = {
        "customers": ("success", now), "products": ("running", now), "orders": ("running", None),
        "carriers": ("forbidden", None), "segments": ("unavailable", None), "categories": ("failed", None),
        "users": ("interrupted", None), "price-tables": ("success", now),
    }
    with session_scope() as db:
        for resource, (status, success) in rows.items():
            db.add(ErpSyncCheckpoint(connection_id=C, resource=resource, scope="", status=status,
                                     last_success_at=success, last_attempt_at=now))
    overview = get("/overview").json()
    phase = {r["resource"]: r["phase"] for r in overview["resources"]}
    assert phase["customers"] == "done" and phase["price-tables"] == "done"
    assert phase["products"] == "done"  # já concluído antes; só está atualizando agora
    assert phase["orders"] == "running" and phase["carriers"] == "denied" and phase["segments"] == "denied"
    assert phase["categories"] == "error" and phase["users"] == "error"
    assert phase["commercial-policies"] == "not_started"
    counts = overview["phases"]
    assert counts["done"] == 3 and counts["denied"] == 2 and counts["error"] == 2 and counts["running"] == 1
    assert sum(counts.values()) == len(overview["resources"]) == 12
    orders = next(r for r in overview["resources"] if r["resource"] == "orders")
    assert orders["records"] == 7 and orders["recordsPartial"] is True  # local existe, carga não concluída


def test_orders_diagnostics_reports_restarts_reread_and_gaps(data):
    base = datetime.now(timezone.utc) - timedelta(hours=3)
    with session_scope() as db:
        db.add(ErpSyncCheckpoint(connection_id=C, resource="orders", scope="", status="running",
                                 cursor="2020-05-29T19:53:00", records=5080))
        db.add(ErpSyncRun(connection_id=C, resource="orders", mode="full", status="interrupted", started_at=base,
                          finished_at=base + timedelta(minutes=30), cursor_before=None, cursor_after="2020-09-23T08:37:00",
                          pages=50, received=1000, persisted=0, unchanged=1000,
                          error="Processo reiniciado durante a execução; retomada pelo checkpoint"))
        db.add(ErpSyncRun(connection_id=C, resource="orders", mode="full", status="running",
                          started_at=base + timedelta(hours=1), cursor_before=None, pages=19, received=380,
                          persisted=0, unchanged=380))
    body = get("/integration/diagnostics/orders").json()
    assert body["runs"]["restartsRecorded"] == 1 and body["runs"]["rereadRatio"] == 1.0
    codes = {f["code"] for f in body["findings"]}
    assert codes == {"full_run_restarted_from_start", "all_records_reread"}
    assert body["ordersWithoutItems"]["total"] == 2 and body["ordersWithoutItems"]["byKind"] == {"order": 2}
    gap = body["ordersWithoutCustomer"]["customerIdMissingInMirror"]
    assert gap["orders"] == 1 and gap["sampleCustomerIds"] == ["ghost"]
    assert body["checkpoint"]["cursor"] == "2020-05-29T19:53:00"
    assert body["hydration"]["pendingOrders"] == 2 and body["mirror"]["orders"] == 7
    assert get("/integration/diagnostics/orders", "sem-acesso@x.com", "viewer").status_code in (401, 403)
    assert json.dumps(body).count(CPF) == 0


def test_finance_summary_says_what_it_excludes_and_whether_the_load_is_partial(data):
    body = get("/finance/orders-summary").json()
    assert body["sales"]["excludes"] == ["cancelled", "quote"]
    assert body["sales"]["orders"] == 5 and body["coverage"]["complete"] is False


def test_stored_403_messages_are_sanitized_on_every_read_surface(data):
    raw = ('Acesso negado (403) em carriers: {"error":"Sem permissão Mercos para \'transportadoras\'. Libere o recurso.",'
           '"details":"x-api-key: abcdef0123456789abcdef0123456789abcdef"}')
    with session_scope() as db:
        db.add(ErpSyncCheckpoint(connection_id=C, resource="carriers", scope="", status="forbidden", error=raw,
                                 last_attempt_at=datetime.now(timezone.utc)))
        db.add(ErpSyncRun(connection_id=C, resource="carriers", mode="full", status="forbidden", error=raw))
    for path in ("/overview", "/integration/status", "/integration/runs"):
        text = get(path).text
        assert "abcdef0123456789" not in text and "{\\\"error\\\"" not in text, path
    carriers = next(r for r in get("/overview").json()["resources"] if r["resource"] == "carriers")
    assert carriers["error"].startswith("Acesso negado (403) em carriers") and "Libere o recurso" in carriers["error"]
    assert carriers["phase"] == "denied"


def test_diagnostics_flags_a_job_claimed_more_than_once(data):
    from app.erp.models import ErpJob

    with session_scope() as db:
        db.add(ErpJob(kind="sync", connection_id=C, resource="all", mode="full", status="processing", attempts=3,
                      leased_until=datetime.now(timezone.utc) + timedelta(minutes=5)))
    findings = get("/integration/diagnostics/orders").json()["findings"]
    assert any(f["code"] == "job_reclaimed_after_lease_expiry" and f["attempts"] == 3 for f in findings)


def test_diagnostics_errors_are_sanitized(data):
    raw = 'Falha: {"error":"negado","details":"x-api-key: abcdef0123456789abcdef0123456789abcdef"}'
    with session_scope() as db:
        db.add(ErpSyncRun(connection_id=C, resource="orders", mode="full", status="failed", error=raw))
    text = get("/integration/diagnostics/orders").text
    assert "abcdef0123456789" not in text and "x-api-key" not in text


def test_status_separates_implemented_enabled_account_and_load_and_shows_incremental_evidence(data):
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        db.add(ErpSyncCheckpoint(connection_id=C, resource="customers", scope="", status="success", last_success_at=now))
        db.add(ErpSyncRun(connection_id=C, resource="customers", mode="incremental", status="success", finished_at=now,
                          received=5, persisted=2, unchanged=3, cursor_after="2026-10-13T08:00:00"))
        db.add(ErpSyncCheckpoint(connection_id=C, resource="carriers", scope="", status="forbidden", error="Acesso negado (403) em carriers"))
    rows = {r["resource"]: r for r in get("/integration/status").json()["resources"]}
    customers, carriers = rows["customers"], rows["carriers"]
    assert customers["phase"] == "done" and customers["capability"]["implementedInErp"] is True
    assert customers["lastIncremental"]["persisted"] == 2 and customers["lastIncremental"]["cursorAfter"] == "2026-10-13T08:00:00"
    assert carriers["phase"] == "denied" and carriers["lastIncremental"] is None
    assert rows["segments"]["phase"] == "not_started" and set(customers["capability"]) >= {"enabled", "accountAccess"}


def test_diagnostics_lists_complete_orders_whose_zero_total_contradicts_their_items(data):
    with session_scope() as db:
        db.add(ErpSalesOrderItem(order_id=db.query(ErpSalesOrder).filter_by(external_id="2").one().id, position=5,
                                 external_id="2-x", name="Brinde", quantity=Decimal("1"), total=Decimal("40.00"), excluded=False))
    body = get("/integration/diagnostics/orders").json()["ordersTotalZeroWithItems"]
    assert body["total"] == 1 and body["byKind"] == {"order": 1}
    assert body["sample"][0]["id"] == "2" and body["sample"][0]["itemsTotal"] == "40.00"
