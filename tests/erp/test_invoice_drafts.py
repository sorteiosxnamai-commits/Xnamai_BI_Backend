"""Tarefa 4 do plano: montagem fiscal (rascunho; nunca emite nota)."""

import threading
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select

from app.erp.db import session_scope
from app.erp.models import ErpAuditEvent, ErpInvoiceDraft, ErpInvoiceDraftItem, ErpSalesOrder, ErpSalesOrderItem
from tests.erp.helpers import add_operator, bearer, client
from tests.erp.pg import PG_URL
from tests.erp.test_order_operations import put_order

H = bearer()


def api(method, path, body=None, key=None, headers=None):
    h = dict(headers or H)
    if key:
        h["Idempotency-Key"] = key
    return client().request(method, f"/api/v1/erp{path}", json=body, headers=h)


def add_items(db, order, rows):
    """rows: (external_id, nome, quantidade, total da linha)."""
    for position, (ext, name, qty, total) in enumerate(rows):
        db.add(ErpSalesOrderItem(
            order_id=order.id, position=position, external_id=ext, name=name, code=f"SKU-{position}",
            product_external_id=f"p{position}", quantity=Decimal(qty), total=Decimal(total),
            unit_price=Decimal(total) / Decimal(qty),
        ))


@pytest.fixture
def seeded(erp_cfg, erp_session_factory):
    erp_cfg()
    with session_scope() as db:
        order = put_order(db, "10", net="12480.00", items=[])
        order.fingerprint = "fp-1"
        order.version = 1
        add_items(db, order, [
            ("a", "Vaso Aurora Cerâmica G", "10", "1200.00"),
            ("b", "Kit Jardim Premium", "5", "1250.00"),
            ("c", "Suporte para Plantas", "8", "720.00"),
            ("d", "Terra Vegetal 10kg", "15", "555.00"),
            ("e", "Fertilizante Orgânico 1L", "12", "696.00"),
            ("f", "Vaso Decorativo P", "20", "1780.00"),
        ])
        order.item_count = 6
        incomplete = put_order(db, "11", complete=False)
        incomplete.fingerprint = "fp-i"
        cancelled = put_order(db, "12", kind="cancelled", items=[("X", "X")])
        cancelled.fingerprint = "fp-c"


def create(percent="30", organize=False, key="draft-key-0000001", order="10"):
    return api("POST", f"/sales-orders/{order}/invoice-drafts", {"percent": percent, "organize": organize}, key=key)


def test_target_comes_from_the_percent_and_effective_value_from_the_items(seeded):
    created = create(organize=True)
    assert created.status_code == 201
    body = created.json()
    assert body["target"] == {"percent": "30", "value": "3744.00"}  # 30% de 12.480,00
    assert body["orderTotal"] == "12480.00"
    allocated = sum(Decimal(i["lineValue"]) for i in body["items"])
    assert Decimal(body["effective"]["value"]) == allocated
    # a diferença é assinada e coerente com a soma dos itens
    assert Decimal(body["difference"]["value"]) == allocated - Decimal("3744.00")
    assert body["difference"]["direction"] in ("below", "above", "exact")
    assert Decimal(body["effective"]["percentOfOrder"]) == (allocated / Decimal("12480") * 100).quantize(Decimal("0.01"))
    assert allocated <= Decimal("3744.00")  # a gulosa não passa do alvo
    assert body["statement"].startswith("Rascunho de planejamento")


def test_organize_is_deterministic_and_does_not_promise_optimality(seeded):
    a = create(organize=True, key="draft-key-aaaaaaa").json()
    # cancela e refaz: mesmo conjunto de quantidades
    api("POST", f"/invoice-drafts/{a['id']}/cancel", {"expectedVersion": a["version"], "reason": "refazer"}, key="cancel-key-aaaaa1")
    b = create(organize=True, key="draft-key-bbbbbbb").json()
    assert [(i["sourceKey"], i["quantity"]) for i in a["items"]] == [(i["sourceKey"], i["quantity"]) for i in b["items"]]
    assert "sem garantia de combinação ótima" in b["organizeNote"]


def test_partial_quantity_uses_rounded_unit_value_and_full_quantity_the_exact_line(seeded):
    with session_scope() as db:
        order = put_order(db, "20", net="100.00", items=[])
        order.fingerprint = "fp-20"
        add_items(db, order, [("z", "Item dízima", "3", "100.00")])
    draft = create(order="20", key="draft-key-round01").json()
    z = draft["items"][0]
    two = api("PATCH", f"/invoice-drafts/{draft['id']}", {"expectedVersion": draft["version"], "items": [{"sourceKey": z["sourceKey"], "quantity": "2"}]}, key="patch-round-0001").json()
    assert two["items"][0]["lineValue"] == "66.67"  # 2 x 33,3333… arredondado em centavos
    full = api("PATCH", f"/invoice-drafts/{draft['id']}", {"expectedVersion": two["version"], "items": [{"sourceKey": z["sourceKey"], "quantity": "3"}]}, key="patch-round-0002").json()
    assert full["items"][0]["lineValue"] == "100.00"  # integral: valor exato da linha, sem deriva


def test_quantity_above_the_order_or_negative_is_rejected(seeded):
    draft = create().json()
    key = draft["items"][0]["sourceKey"]
    over = api("PATCH", f"/invoice-drafts/{draft['id']}", {"expectedVersion": draft["version"], "items": [{"sourceKey": key, "quantity": "11"}]}, key="patch-over-00001")
    assert over.status_code == 409 and over.json()["detail"]["code"] == "allocation_exceeds_balance"
    assert over.json()["detail"]["available"] == "10"
    negative = api("PATCH", f"/invoice-drafts/{draft['id']}", {"expectedVersion": draft["version"], "items": [{"sourceKey": key, "quantity": "-1"}]}, key="patch-neg-000001")
    assert negative.status_code == 422
    unknown = api("PATCH", f"/invoice-drafts/{draft['id']}", {"expectedVersion": draft["version"], "items": [{"sourceKey": "i:zzz", "quantity": "1"}]}, key="patch-unk-000001")
    assert unknown.json()["detail"]["code"] == "unknown_item"


def test_percent_must_be_in_range(seeded):
    assert create(percent="0", key="pct-key-000000a").status_code == 422
    assert create(percent="100.01", key="pct-key-000000b").status_code == 422
    assert create(percent="100", key="pct-key-000000c").status_code == 201


def test_incomplete_cancelled_or_zero_total_orders_are_blocked(seeded):
    assert create(order="11", key="blk-key-0000001").json()["detail"]["code"] == "items_incomplete"
    assert create(order="12", key="blk-key-0000002").json()["detail"]["code"] == "order_cancelled"
    assert create(order="ghost", key="blk-key-0000003").status_code == 404


def test_two_documents_cannot_allocate_the_same_quantity(seeded):
    first = create(organize=False, key="draft-key-first01").json()
    second = create(organize=False, key="draft-key-secnd01").json()
    key = first["items"][0]["sourceKey"]
    ok = api("PATCH", f"/invoice-drafts/{first['id']}", {"expectedVersion": first["version"], "items": [{"sourceKey": key, "quantity": "7"}]}, key="patch-first-0001")
    assert ok.status_code == 200
    clash = api("PATCH", f"/invoice-drafts/{second['id']}", {"expectedVersion": second["version"], "items": [{"sourceKey": key, "quantity": "5"}]}, key="patch-secnd-0001")
    assert clash.status_code == 409 and clash.json()["detail"]["code"] == "allocation_exceeds_balance"
    assert clash.json()["detail"]["available"] == "3"  # 10 - 7 já usados pelo outro documento
    fits = api("PATCH", f"/invoice-drafts/{second['id']}", {"expectedVersion": second["version"], "items": [{"sourceKey": key, "quantity": "3"}]}, key="patch-secnd-0002")
    assert fits.status_code == 200
    view = api("GET", f"/invoice-drafts/{first['id']}").json()["items"][0]
    assert view["allocatedElsewhere"] == "3" and view["available"] == "7"


def test_cancelling_releases_the_allocation_and_requires_a_reason(seeded):
    first = create(key="draft-key-cancl01").json()
    key = first["items"][0]["sourceKey"]
    held = api("PATCH", f"/invoice-drafts/{first['id']}", {"expectedVersion": first["version"], "items": [{"sourceKey": key, "quantity": "10"}]}, key="patch-cancl-0001").json()
    second = create(key="draft-key-cancl02").json()
    blocked = api("PATCH", f"/invoice-drafts/{second['id']}", {"expectedVersion": second["version"], "items": [{"sourceKey": key, "quantity": "1"}]}, key="patch-cancl-0002")
    assert blocked.status_code == 409
    no_reason = api("POST", f"/invoice-drafts/{first['id']}/cancel", {"expectedVersion": held["version"], "reason": ""}, key="cancel-key-0000a")
    assert no_reason.status_code == 422
    assert api("POST", f"/invoice-drafts/{first['id']}/cancel", {"expectedVersion": held["version"], "reason": "Cliente desistiu"}, key="cancel-key-0000b").status_code == 200
    assert api("PATCH", f"/invoice-drafts/{second['id']}", {"expectedVersion": second["version"], "items": [{"sourceKey": key, "quantity": "10"}]}, key="patch-cancl-0003").status_code == 200
    closed = api("PATCH", f"/invoice-drafts/{first['id']}", {"expectedVersion": held["version"] + 1, "notes": "x"}, key="patch-cancl-0004")
    assert closed.json()["detail"]["code"] == "draft_closed"


def test_stale_version_is_rejected_without_changing_anything(seeded):
    draft = create().json()
    key = draft["items"][0]["sourceKey"]
    assert api("PATCH", f"/invoice-drafts/{draft['id']}", {"expectedVersion": draft["version"], "items": [{"sourceKey": key, "quantity": "2"}]}, key="patch-ver-000001").status_code == 200
    old = api("PATCH", f"/invoice-drafts/{draft['id']}", {"expectedVersion": draft["version"], "items": [{"sourceKey": key, "quantity": "9"}]}, key="patch-ver-000002")
    assert old.status_code == 409 and old.json()["detail"]["code"] == "version_conflict"
    assert api("GET", f"/invoice-drafts/{draft['id']}").json()["items"][0]["quantity"] == "2"


def test_sync_replacing_local_item_ids_keeps_the_draft_consistent(seeded):
    draft = create(organize=True).json()
    before = [(i["sourceKey"], i["quantity"], i["lineValue"]) for i in draft["items"]]
    with session_scope() as db:  # a sincronização apaga e recria os itens: ids locais novos, mesma origem
        order = db.scalar(select(ErpSalesOrder).where(ErpSalesOrder.external_id == "10"))
        db.execute(delete(ErpSalesOrderItem).where(ErpSalesOrderItem.order_id == order.id))
        db.flush()
        add_items(db, order, [
            ("a", "Vaso Aurora Cerâmica G", "10", "1200.00"), ("b", "Kit Jardim Premium", "5", "1250.00"),
            ("c", "Suporte para Plantas", "8", "720.00"), ("d", "Terra Vegetal 10kg", "15", "555.00"),
            ("e", "Fertilizante Orgânico 1L", "12", "696.00"), ("f", "Vaso Decorativo P", "20", "1780.00"),
        ])
    again = api("GET", f"/invoice-drafts/{draft['id']}").json()
    assert [(i["sourceKey"], i["quantity"], i["lineValue"]) for i in again["items"]] == before
    assert again["review"]["required"] is False and again["stale"] is False


def test_item_removed_at_source_requires_review_and_history_is_preserved(seeded):
    draft = create(organize=True).json()
    removed = next(i for i in draft["items"] if i["included"])
    with session_scope() as db:  # o item some do pedido e o pedido muda de versão
        order = db.scalar(select(ErpSalesOrder).where(ErpSalesOrder.external_id == "10"))
        db.execute(delete(ErpSalesOrderItem).where(ErpSalesOrderItem.order_id == order.id, ErpSalesOrderItem.external_id == removed["sourceKey"].removeprefix("i:")))
        order.fingerprint = "fp-2"
        order.version = 2
    stale = api("GET", f"/invoice-drafts/{draft['id']}").json()
    assert stale["stale"] is True and stale["review"]["required"] is True
    change = next(c for c in stale["review"]["changes"] if c["kind"] == "removed")
    assert change["sourceKey"] == removed["sourceKey"] and change["before"]["quantity"]  # preserva o "antes"
    assert Decimal(change["valueDifference"]) == -Decimal(removed["lineValue"])
    assert next(i for i in stale["items"] if i["sourceKey"] == removed["sourceKey"])["status"] == "removed_at_source"
    refused = api("PATCH", f"/invoice-drafts/{draft['id']}", {"expectedVersion": stale["version"], "notes": "x"}, key="patch-rev-0000001")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "review_required"
    organize = api("POST", f"/invoice-drafts/{draft['id']}/organize", {"expectedVersion": stale["version"]}, key="org-rev-00000001")
    assert organize.json()["detail"]["code"] == "review_required"
    revised = api("PATCH", f"/invoice-drafts/{draft['id']}", {"expectedVersion": stale["version"], "acknowledgeReview": True}, key="patch-rev-0000002")
    assert revised.status_code == 200
    body = revised.json()
    assert body["stale"] is False and body["review"]["required"] is False
    assert removed["sourceKey"] not in [i["sourceKey"] for i in body["items"]]
    with session_scope() as db:
        event = db.scalar(select(ErpAuditEvent).where(ErpAuditEvent.action == "invoice.review_applied"))
        assert event is not None and event.detail["changes"][0]["kind"] == "removed"  # histórico na auditoria


def test_quantity_changed_at_source_is_flagged_and_clamped_on_review(seeded):
    draft = create(key="draft-key-clamp01").json()
    key = draft["items"][0]["sourceKey"]
    patched = api("PATCH", f"/invoice-drafts/{draft['id']}", {"expectedVersion": draft["version"], "items": [{"sourceKey": key, "quantity": "10"}]}, key="patch-clamp-0001").json()
    with session_scope() as db:
        order = db.scalar(select(ErpSalesOrder).where(ErpSalesOrder.external_id == "10"))
        item = db.scalar(select(ErpSalesOrderItem).where(ErpSalesOrderItem.order_id == order.id, ErpSalesOrderItem.external_id == "a"))
        item.quantity, item.total = Decimal("4"), Decimal("480.00")
        order.fingerprint = "fp-3"
    stale = api("GET", f"/invoice-drafts/{draft['id']}").json()
    assert any(c["kind"] == "quantity_changed" and c["before"]["quantity"] == "10" and c["after"]["quantity"] == "4" for c in stale["review"]["changes"])
    revised = api("PATCH", f"/invoice-drafts/{draft['id']}", {"expectedVersion": patched["version"], "acknowledgeReview": True}, key="patch-clamp-0002").json()
    item = next(i for i in revised["items"] if i["sourceKey"] == key)
    assert item["quantity"] == "4" and item["sourceQuantity"] == "4" and item["lineValue"] == "480.00"  # nunca excede o saldo


def test_issuing_is_unavailable_and_nothing_fiscal_is_fabricated(seeded):
    draft = create(organize=True).json()
    issue = api("POST", f"/invoice-drafts/{draft['id']}/issue")
    assert issue.status_code == 409 and issue.json()["detail"]["code"] == "issuer_unavailable"
    assert "emissor" in issue.json()["detail"]["message"]
    body = api("GET", f"/invoice-drafts/{draft['id']}").json()

    def keys(value):
        if isinstance(value, dict):
            for key, inner in value.items():
                yield str(key).lower()
                yield from keys(inner)
        elif isinstance(value, list):
            for inner in value:
                yield from keys(inner)

    forbidden = {"chave", "chavenfe", "accesskey", "protocolo", "protocol", "xml", "danfe", "numeronota", "nfenumber"}
    assert not (set(keys(body)) & forbidden)  # nenhum campo fiscal que o sistema não recebeu de verdade
    assert body["status"] == "draft"  # jamais "emitted"/"authorized"
    assert api("GET", f"/invoice-drafts/{draft['id']}").json()["issuance"]["available"] is False


def test_list_states_filter_and_summary_use_the_drafts(seeded):
    with session_scope() as db:
        other = put_order(db, "30", net="50.00", items=[])
        other.fingerprint = "fp-30"
        add_items(db, other, [("q", "Item", "1", "50.00")])
    create(key="draft-key-state01")
    create(order="30", key="draft-key-state02")
    with session_scope() as db:  # pedido 30 muda: seu rascunho fica para revisão
        db.scalar(select(ErpSalesOrder).where(ErpSalesOrder.external_id == "30")).fingerprint = "fp-30b"
    items = {i["id"]: i for i in api("GET", "/sales-orders?include=operational&page_size=100").json()["items"]}
    assert items["10"]["operational"]["invoice"]["state"] == "draft"
    assert items["30"]["operational"]["invoice"]["state"] == "stale"
    assert items["11"]["operational"]["invoice"]["state"] == "none"
    assert [i["id"] for i in api("GET", "/sales-orders?fiscal=draft").json()["items"]] == ["10"]
    assert [i["id"] for i in api("GET", "/sales-orders?fiscal=stale").json()["items"]] == ["30"]
    assert api("GET", "/sales-orders?fiscal=bogus").status_code == 422
    summary = api("GET", "/operational-summary").json()
    assert summary["invoice"]["available"] is True
    assert summary["invoice"]["counts"] == {"none": 2, "draft": 1, "stale": 1}
    assert summary["invoice"]["issuance"]["available"] is False
    assert "fiscal" in summary["filtersAvailable"]


def test_permissions_idempotency_and_connection_isolation(seeded, erp_session_factory):
    add_operator(erp_session_factory, "leitor@x.com", ["consulta"])
    add_operator(erp_session_factory, "estoque@x.com", ["estoque"])
    reader = bearer("leitor@x.com", "viewer")
    draft = create().json()
    assert api("GET", f"/invoice-drafts/{draft['id']}", headers=reader).status_code == 200
    assert api("POST", "/sales-orders/10/invoice-drafts", {"percent": "10"}, key="perm-key-0000001", headers=reader).status_code == 403
    assert api("GET", "/sales-orders/10/invoice-drafts", headers=bearer("estoque@x.com", "viewer")).status_code == 403
    assert api("POST", "/sales-orders/10/invoice-drafts", {"percent": "10"}).status_code == 422  # sem Idempotency-Key
    again = create()  # mesma chave e mesmo corpo
    assert again.json()["replayed"] is True and again.json()["id"] == draft["id"]
    with session_scope() as db:
        assert db.scalar(select(func.count(ErpInvoiceDraft.id))) == 1
        other = put_order(db, "99", connection="outra-conexao", net="10.00", items=[])
        other.fingerprint = "fp-99"
    assert api("GET", "/sales-orders/99/invoice-drafts").status_code == 404


@pytest.mark.skipif(not PG_URL, reason="concorrência real exige PostgreSQL")
def test_concurrent_documents_never_exceed_the_orders_quantity(seeded):
    drafts = [create(key=f"draft-key-conc{i:03d}").json() for i in range(4)]
    key = drafts[0]["items"][0]["sourceKey"]
    results, errors = [], []

    def allocate(draft):
        try:
            r = api("PATCH", f"/invoice-drafts/{draft['id']}",
                    {"expectedVersion": draft["version"], "items": [{"sourceKey": key, "quantity": "10"}]},
                    key=f"patch-conc-{draft['id']:06d}")
            results.append(r.status_code)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=allocate, args=(d,)) for d in drafts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert results.count(200) == 1 and results.count(409) == 3  # o pedido é travado: só um documento fica com a quantidade
    with session_scope() as db:
        total = db.scalar(select(func.coalesce(func.sum(ErpInvoiceDraftItem.quantity), 0)).where(ErpInvoiceDraftItem.source_key == key))
        assert Decimal(str(total)) == Decimal("10")
