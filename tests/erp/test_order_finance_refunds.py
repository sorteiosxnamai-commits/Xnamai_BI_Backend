"""Tarefa 5 do plano: financeiro por pedido e reembolsos (fluxo interno)."""

import threading
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.erp.db import session_scope
from app.erp.models import (
    ErpExternalTitle,
    ErpFinSettlement,
    ErpFinTitle,
    ErpRefundEvent,
    ErpRefundRequest,
)
from tests.erp.helpers import add_operator, bearer, client
from tests.erp.pg import PG_URL
from tests.erp.test_order_operations import C, put_order

H = bearer()


def api(method, path, body=None, key=None, headers=None, params=None):
    h = dict(headers or H)
    if key:
        h["Idempotency-Key"] = key
    return client().request(method, f"/api/v1/erp{path}", json=body, headers=h, params=params)


def k(prefix: str, n: int = 0) -> str:
    return f"{prefix}-{n:08d}"[:60].ljust(12, "x")


@pytest.fixture
def seeded(erp_cfg, erp_session_factory):
    erp_cfg()
    with session_scope() as db:
        put_order(db, "10", net="300.00", customer="c1", items=[("A", "A-1")])
        put_order(db, "11", net="100.00", customer="c1", items=[("B", "B-1")])
        put_order(db, "12", net="50.00", customer="c1", kind="cancelled", items=[("C", "C-1")])
        put_order(db, "20", net="80.00", customer="c1", issue=date(2026, 9, 3), items=[("D", "D-1")])
    account = api("POST", "/finance/accounts", {"code": "CX", "name": "Caixa", "kind": "cash"}, key="acc-key-0000001")
    assert account.status_code == 201
    return account.json()["id"]


def receivable(order="10", key="rec-key-0000001", installments=1, **extra):
    body = {"firstDueDate": "2026-12-01", "installments": installments, **extra}
    return api("POST", f"/sales-orders/{order}/receivable", body, key=key)


def settle(installment_id, account_id, amount, key):
    return api("POST", f"/finance/installments/{installment_id}/settlements",
               {"accountId": account_id, "amount": amount, "reference": "teste"}, key=key)


def first_installment(order="10"):
    return api("GET", f"/sales-orders/{order}/finance").json()["titles"][0]["installments"][0]["id"]


def test_receivable_links_order_title_installments_and_settlements(seeded):
    created = receivable(installments=3)
    assert created.status_code == 201
    body = created.json()
    assert body["state"] == "open" and body["obligation"] == "300.00" and body["paid"] == "0.00"
    assert [i["amount"] for i in body["titles"][0]["installments"]] == ["100.00", "100.00", "100.00"]
    with session_scope() as db:
        title = db.scalar(select(ErpFinTitle))
        assert (title.origin_type, title.origin_ref, title.kind) == ("sales_order", "10", "receivable")
        assert title.customer_external_id == "c1" and title.causal_key == "sales_order:10:1"
    again = receivable(key="rec-key-0000002")
    assert again.status_code == 409 and again.json()["detail"]["code"] == "obligation_exists"


def test_payment_states_follow_settlements_and_reversals(seeded):
    receivable(installments=2)
    installments = api("GET", "/sales-orders/10/finance").json()["titles"][0]["installments"]
    first, second = installments[0]["id"], installments[1]["id"]
    partial = settle(first, seeded, "150.00", "settle-0000001")
    assert partial.status_code == 201
    view = api("GET", "/sales-orders/10/finance").json()
    assert view["state"] == "partial" and view["paid"] == "150.00" and view["open"] == "150.00"
    assert settle(second, seeded, "150.00", "settle-0000002").status_code == 201
    assert api("GET", "/sales-orders/10/finance").json()["state"] == "paid"
    over = settle(first, seeded, "0.01", "settle-0000003")
    assert over.status_code == 409  # acima do valor da parcela
    reversal = api("POST", f"/finance/settlements/{partial.json()['id']}/reversals", {"reason": "Pagamento devolvido"}, key="revers-0000001")
    assert reversal.status_code == 201
    after = api("GET", "/sales-orders/10/finance").json()
    assert after["state"] == "partial" and after["paid"] == "150.00"  # só uma baixa restou


def test_overdue_is_flagged_and_filterable(seeded):
    receivable(key="rec-key-overdue1", firstDueDate="2020-01-10")
    view = api("GET", "/sales-orders/10/finance").json()
    assert view["overdue"] is True
    assert [i["id"] for i in api("GET", "/sales-orders?financeStatus=overdue").json()["items"]] == ["10"]


def test_external_mercos_title_is_not_double_counted(seeded):
    with session_scope() as db:
        db.add(ErpExternalTitle(connection_id=C, external_id="T-9", order_external_id="10", amount=Decimal("300.00"), status="open"))
    blocked = receivable(key="rec-key-extern01")
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "external_title_exists"
    assert blocked.json()["detail"]["externalTitles"] == ["T-9"]
    ok = receivable(key="rec-key-extern02", acknowledgeExternalTitle=True)
    assert ok.status_code == 201
    view = ok.json()
    assert view["obligation"] == "300.00"  # só o local compõe a obrigação
    assert [t["id"] for t in view["externalTitles"]] == ["T-9"] and "não somados" in view["externalNote"]


def test_cancelled_or_unknown_orders_are_refused_and_new_obligation_after_cancel(seeded):
    assert receivable(order="12", key="rec-key-cancel01").json()["detail"]["code"] == "order_cancelled"
    assert receivable(order="ghost", key="rec-key-ghost001").status_code == 404
    created = receivable(key="rec-key-recreate1").json()
    title_id = created["titles"][0]["id"]
    assert api("POST", f"/finance/titles/{title_id}/cancel", {"reason": "Pedido refeito"}).status_code == 200
    assert api("GET", "/sales-orders/10/finance").json()["state"] == "none"
    assert receivable(key="rec-key-recreate2").status_code == 201  # chave causal nova


def test_metrics_keep_sales_cash_and_receivable_separate(seeded):
    receivable(installments=2)
    first = first_installment()
    settle(first, seeded, "100.00", "settle-met-00001")
    settle(first, seeded, "50.00", "settle-met-00002")
    receivable(order="11", key="rec-key-0000011")
    summary = api("GET", "/finance/orders-summary").json()
    assert summary["sales"]["net"] == "480.00"  # 300 + 100 + 80 (cancelado fora): valor VENDIDO
    assert summary["cash"] == {**summary["cash"], "received": "150.00", "reversed": "0.00", "net": "150.00"}
    assert summary["receivable"]["open"] == "250.00"  # 300 - 150 + 100
    assert summary["pix"]["available"] is False
    assert set(summary["formulas"]) == {"sales", "receivable", "cash", "refunds"}
    assert "Não é valor recebido" in summary["formulas"]["sales"]
    assert summary["sales"]["variation"]["available"] is False  # sem período


def test_variation_uses_the_comparable_previous_period_and_handles_zero_base(seeded):
    params = {"dateFrom": "2026-10-01", "dateTo": "2026-10-31"}
    zero_base = api("GET", "/finance/orders-summary", params={"dateFrom": "2026-12-01", "dateTo": "2026-12-31"}).json()
    assert zero_base["sales"]["variation"] == {"available": False, "reason": "Período anterior sem base de comparação"}
    with session_scope() as db:
        put_order(db, "30", net="200.00", issue=date(2026, 9, 10), items=[("E", "E-1")])  # setembro (anterior)
    varied = api("GET", "/finance/orders-summary", params=params).json()
    assert varied["sales"]["net"] == "400.00" and varied["sales"]["variation"]["previous"] == "280.00"
    assert varied["sales"]["variation"]["percent"] == "42.86"  # (400-280)/280
    assert api("GET", "/finance/orders-summary", params={"dateFrom": "2026-10-31", "dateTo": "2026-10-01"}).status_code == 422


def test_cash_period_uses_brasilia_day_not_utc(seeded):
    receivable(key="rec-key-brt000001")
    sid = settle(first_installment(), seeded, "10.00", "settle-brt-0001").json()["id"]
    with session_scope() as db:  # 01/11 01:30 UTC = 31/10 22:30 em Brasília
        db.get(ErpFinSettlement, sid).settled_at = datetime(2026, 11, 1, 1, 30, tzinfo=timezone.utc)
    october = api("GET", "/finance/orders-summary", params={"dateFrom": "2026-10-01", "dateTo": "2026-10-31"}).json()
    november = api("GET", "/finance/orders-summary", params={"dateFrom": "2026-11-01", "dateTo": "2026-11-30"}).json()
    assert october["cash"]["received"] == "10.00" and november["cash"]["received"] == "0.00"


def paid_order(seeded, order="10", amount="300.00"):
    receivable(order=order, key=f"rec-{order}-0000001", installments=1)
    inst = first_installment(order)
    return settle(inst, seeded, amount, f"settle-{order}-00001").json()["id"]


def refund(settlement_id, amount, key, order="10", headers=None, reason="Cliente desistiu"):
    return api("POST", "/refund-requests", {"orderId": order, "settlementId": settlement_id, "amount": amount, "reason": reason}, key=key, headers=headers)


def test_refund_request_changes_nothing_and_respects_the_refundable_limit(seeded):
    sid = paid_order(seeded)
    created = refund(sid, "100.00", "refund-key-000001")
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "requested" and "Não altera nenhuma baixa" in body["statement"]
    assert api("GET", "/sales-orders/10/finance").json()["paid"] == "300.00"  # baixa intacta
    second = refund(sid, "200.00", "refund-key-000002")
    assert second.status_code == 201  # 100 + 200 = 300: no limite
    over = refund(sid, "0.01", "refund-key-000003")
    assert over.status_code == 409 and over.json()["detail"]["code"] == "refund_exceeds_refundable"
    assert over.json()["detail"]["refundable"] == "0.00"
    payments = api("GET", "/sales-orders/10/finance").json()["payments"]
    assert payments[0]["refundable"] == "0.00" and payments[0]["refundCommitted"] == "300.00"
    assert api("POST", "/refund-requests", {"orderId": "10", "settlementId": sid, "amount": "0", "reason": "x"}, key="refund-key-000004").status_code == 422


def test_refund_for_a_settlement_of_another_order_or_unknown_is_rejected(seeded):
    sid = paid_order(seeded)
    assert refund(sid, "10.00", "refund-key-other01", order="11").json()["detail"]["code"] == "settlement_not_linked"
    assert refund(999999, "10.00", "refund-key-other02").status_code == 404


def test_repeated_refund_is_idempotent_and_never_double_counts(seeded):
    sid = paid_order(seeded)
    first = refund(sid, "50.00", "refund-key-idem001")
    again = refund(sid, "50.00", "refund-key-idem001")
    assert again.json()["replayed"] is True and again.json()["id"] == first.json()["id"]
    with session_scope() as db:
        assert db.scalar(select(func.count(ErpRefundRequest.id))) == 1
    reused = refund(sid, "60.00", "refund-key-idem001")
    assert reused.status_code == 409  # mesma chave, outro corpo


def test_approval_needs_its_own_permission_and_is_audited(seeded, erp_session_factory):
    add_operator(erp_session_factory, "vendas@x.com", ["comercial"])
    add_operator(erp_session_factory, "fin@x.com", ["financeiro"])
    sales, finance_user = bearer("vendas@x.com", "viewer"), bearer("fin@x.com", "viewer")
    sid = paid_order(seeded)
    created = refund(sid, "40.00", "refund-key-appr001", headers=sales)
    assert created.status_code == 201 and created.json()["requestedBy"] == "vendas@x.com"
    rid, version = created.json()["id"], created.json()["version"]
    denied = api("POST", f"/refund-requests/{rid}/approve", {"expectedVersion": version}, key="approve-key-0001", headers=sales)
    assert denied.status_code == 403
    approved = api("POST", f"/refund-requests/{rid}/approve", {"expectedVersion": version, "note": "ok"}, key="approve-key-0002", headers=finance_user)
    assert approved.status_code == 200
    body = approved.json()
    assert body["status"] == "approved" and body["decidedBy"] == "fin@x.com"
    assert "A devolução ainda não foi feita" in body["statement"]
    assert [e["action"] for e in body["events"]] == ["requested", "approved"]
    assert api("GET", "/sales-orders/10/finance").json()["paid"] == "300.00"  # aprovar não mexe na baixa
    again = api("POST", f"/refund-requests/{rid}/approve", {"expectedVersion": version}, key="approve-key-0003", headers=finance_user)
    assert again.status_code == 409 and again.json()["detail"]["code"] in ("invalid_transition", "version_conflict")
    history = api("GET", "/sales-orders/10/history").json()["items"]
    assert [h["action"] for h in history if h["action"].startswith("refund.")][:2] == ["refund.approved", "refund.requested"]


def test_reject_and_cancel_free_the_amount(seeded, erp_session_factory):
    add_operator(erp_session_factory, "vendas@x.com", ["comercial"])
    add_operator(erp_session_factory, "outra@x.com", ["comercial"])
    sales = bearer("vendas@x.com", "viewer")
    sid = paid_order(seeded)
    a = refund(sid, "300.00", "refund-key-rej0001", headers=sales).json()
    blocked = refund(sid, "1.00", "refund-key-rej0002")
    assert blocked.status_code == 409
    other = api("POST", f"/refund-requests/{a['id']}/cancel", {"expectedVersion": a["version"], "reason": "tentando"}, key="cancel-key-00001", headers=bearer("outra@x.com", "viewer"))
    assert other.status_code == 403  # só quem pediu ou quem aprova
    rejected = api("POST", f"/refund-requests/{a['id']}/reject", {"expectedVersion": a["version"], "reason": "Fora da política"}, key="reject-key-00001")
    assert rejected.json()["status"] == "rejected"
    assert refund(sid, "300.00", "refund-key-rej0003").status_code == 201  # o valor foi liberado
    no_reason = api("POST", f"/refund-requests/{a['id']}/reject", {"expectedVersion": 2, "reason": ""}, key="reject-key-00002")
    assert no_reason.status_code == 422


def test_full_refund_can_apply_the_existing_local_reversal_once(seeded):
    sid = paid_order(seeded)
    created = refund(sid, "300.00", "refund-key-full001").json()
    approved = api("POST", f"/refund-requests/{created['id']}/approve", {"expectedVersion": created["version"]}, key="approve-full-0001").json()
    done = api("POST", f"/refund-requests/{created['id']}/apply-local-reversal", {"expectedVersion": approved["version"]}, key="reverse-key-0001")
    assert done.status_code == 200
    body = done.json()
    assert body["status"] == "reversed_local" and body["reversalSettlementId"]
    assert "não é devolução bancária" in body["statement"]
    view = api("GET", "/sales-orders/10/finance").json()
    assert view["paid"] == "0.00" and view["payments"][0]["reversed"] is True and view["payments"][0]["refundable"] == "0.00"
    with session_scope() as db:
        assert db.scalar(select(func.count(ErpFinSettlement.id)).where(ErpFinSettlement.kind == "reversal")) == 1
    replay = api("POST", f"/refund-requests/{created['id']}/apply-local-reversal", {"expectedVersion": approved["version"]}, key="reverse-key-0001")
    assert replay.json()["replayed"] is True  # nunca estorna duas vezes
    assert refund(sid, "1.00", "refund-key-full002").json()["detail"]["code"] == "already_reversed"


def test_partial_refund_cannot_use_the_integral_local_reversal(seeded):
    sid = paid_order(seeded)
    created = refund(sid, "100.00", "refund-key-part001").json()
    approved = api("POST", f"/refund-requests/{created['id']}/approve", {"expectedVersion": created["version"]}, key="approve-part-0001").json()
    refused = api("POST", f"/refund-requests/{created['id']}/apply-local-reversal", {"expectedVersion": approved["version"]}, key="reverse-part-0001")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "partial_reversal_unsupported"
    assert api("GET", "/sales-orders/10/finance").json()["paid"] == "300.00"  # nada foi estornado
    confirmed = api("POST", f"/refund-requests/{created['id']}/confirm-external", {"expectedVersion": approved["version"], "reference": "TED 123456"}, key="confirm-key-0001")
    assert confirmed.status_code == 200
    body = confirmed.json()
    assert body["status"] == "external_confirmed" and body["externalReference"] == "TED 123456"
    assert "não comprova a devolução bancária" in body["statement"]
    assert api("GET", "/sales-orders/10/finance").json()["paid"] == "300.00"  # confirmação manual não altera a baixa


def test_external_confirmation_needs_a_reference_and_an_approved_request(seeded):
    sid = paid_order(seeded)
    created = refund(sid, "100.00", "refund-key-conf001").json()
    early = api("POST", f"/refund-requests/{created['id']}/confirm-external", {"expectedVersion": created["version"], "reference": "ref-1"}, key="confirm-early-001")
    assert early.status_code == 409 and early.json()["detail"]["code"] == "invalid_transition"
    approved = api("POST", f"/refund-requests/{created['id']}/approve", {"expectedVersion": created["version"]}, key="approve-conf-0001").json()
    assert api("POST", f"/refund-requests/{created['id']}/confirm-external", {"expectedVersion": approved["version"], "reference": ""}, key="confirm-empty-001").status_code == 422


def test_local_reversal_needs_the_settle_permission(seeded, erp_session_factory):
    add_operator(erp_session_factory, "fin@x.com", ["financeiro"])
    fin = bearer("fin@x.com", "viewer")
    sid = paid_order(seeded)
    created = refund(sid, "300.00", "refund-key-perm001").json()
    approved = api("POST", f"/refund-requests/{created['id']}/approve", {"expectedVersion": created["version"]}, key="approve-perm-0001", headers=fin).json()
    ok = api("POST", f"/refund-requests/{created['id']}/apply-local-reversal", {"expectedVersion": approved["version"]}, key="reverse-perm-0001", headers=fin)
    assert ok.status_code == 200  # financeiro tem finance:settle e refunds:approve


def test_list_filters_and_history(seeded):
    sid = paid_order(seeded)
    refund(sid, "10.00", "refund-key-list001")
    one = api("GET", "/refund-requests").json()
    assert one["totalItems"] == 1 and one["items"][0]["status"] == "requested"
    assert api("GET", "/refund-requests", params={"status": "approved"}).json()["totalItems"] == 0
    assert api("GET", "/refund-requests", params={"status": "bogus"}).status_code == 422
    assert api("GET", "/refund-requests", params={"orderId": "11"}).json()["totalItems"] == 0
    detail = api("GET", f"/refund-requests/{one['items'][0]['id']}").json()
    assert [e["action"] for e in detail["events"]] == ["requested"]
    assert api("GET", "/refund-requests/999").status_code == 404


def test_order_list_finance_state_filter_and_summary(seeded):
    receivable(installments=1)
    receivable(order="11", key="rec-key-0000011")
    settle(first_installment("11"), seeded, "100.00", "settle-list-0001")
    items = {i["id"]: i for i in api("GET", "/sales-orders?include=operational&page_size=100").json()["items"]}
    assert items["10"]["operational"]["finance"]["state"] == "open"
    assert items["11"]["operational"]["finance"]["state"] == "paid"
    assert items["20"]["operational"]["finance"]["state"] == "none"
    assert items["10"]["operational"]["payment"]["source"] == "mercos_mirror"  # espelho do Mercos segue separado
    assert [i["id"] for i in api("GET", "/sales-orders?financeStatus=paid").json()["items"]] == ["11"]
    assert {i["id"] for i in api("GET", "/sales-orders?financeStatus=none&page_size=100").json()["items"]} == {"12", "20"}
    assert api("GET", "/sales-orders?financeStatus=bogus").status_code == 422
    summary = api("GET", "/operational-summary").json()
    assert summary["finance"]["counts"] == {"none": 2, "open": 1, "partial": 0, "paid": 1, "overdue": 0}
    assert "financeStatus" in summary["filtersAvailable"]


def test_report_csv_is_scoped_filtered_and_safe(seeded):
    with session_scope() as db:
        put_order(db, "40", net="10.00", customer="c1", number="=cmd|calc", items=[("F", "F-1")])
    receivable(key="rec-key-report01")
    csv_text = api("GET", "/finance/orders-report.csv").text.lstrip("﻿")
    lines = csv_text.strip().split("\n")
    assert lines[0].startswith("pedido;cliente;status_mercos")
    assert any(line.startswith("10;") and "open" in line for line in lines)
    assert "'=cmd|calc" in csv_text and ";=cmd" not in csv_text  # sem injeção de fórmula
    only_paid = api("GET", "/finance/orders-report.csv", params={"financeStatus": "paid"}).text
    assert len(only_paid.strip().split("\n")) == 1  # só cabeçalho
    response = api("GET", "/finance/orders-report.csv")
    assert response.headers["x-report-truncated"] == "false"
    assert response.headers["content-type"].startswith("text/csv")


def test_permissions_and_isolation(seeded, erp_session_factory):
    add_operator(erp_session_factory, "leitor@x.com", ["consulta"])
    add_operator(erp_session_factory, "estoque@x.com", ["estoque"])
    reader = bearer("leitor@x.com", "viewer")
    assert api("GET", "/refund-requests", headers=reader).status_code == 200
    assert api("GET", "/sales-orders/10/finance", headers=reader).status_code == 403  # sem finance:read
    assert api("GET", "/finance/orders-summary", headers=reader).status_code == 403
    assert api("GET", "/refund-requests", headers=bearer("estoque@x.com", "viewer")).status_code == 403
    denied = refund(1, "1.00", "refund-key-denied01", headers=reader)
    assert denied.status_code == 403
    with session_scope() as db:
        put_order(db, "99", connection="outra-conexao", net="1.00", items=[("Z", "Z")])
    assert api("GET", "/sales-orders/99/finance").status_code == 404


@pytest.mark.skipif(not PG_URL, reason="concorrência real exige PostgreSQL")
def test_concurrent_refunds_never_exceed_the_settlement(seeded):
    sid = paid_order(seeded)
    results, errors = [], []

    def go(n):
        try:
            results.append(refund(sid, "200.00", f"refund-conc-{n:06d}").status_code)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(n,)) for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert results.count(201) == 1 and results.count(409) == 4  # 200 cabe uma vez em 300
    with session_scope() as db:
        total = db.scalar(select(func.coalesce(func.sum(ErpRefundRequest.amount), 0)))
        assert Decimal(str(total)) == Decimal("200.00")
        assert db.scalar(select(func.count(ErpRefundEvent.id))) == 1
