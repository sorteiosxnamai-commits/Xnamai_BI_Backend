"""Tarefa 3 do plano: frete local (cadastro manual, seleção local, sem contratação)."""

import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update

from app.erp.db import session_scope
from app.erp.models import ErpAuditEvent, ErpSalesOrder, ErpShippingOption, ErpShippingQuote
from tests.erp.helpers import add_operator, bearer, client
from tests.erp.pg import PG_URL
from tests.erp.test_order_operations import put_order

H = bearer()


def api(method, path, body=None, key=None, headers=None):
    h = dict(headers or H)
    if key:
        h["Idempotency-Key"] = key
    return client().request(method, f"/api/v1/erp{path}", json=body, headers=h)


VOLUMES = [
    {"weightKg": "1.250", "lengthCm": "30", "widthCm": "20", "heightCm": "15"},
    {"weightKg": "2", "lengthCm": "40", "widthCm": "40", "heightCm": "10"},
]


@pytest.fixture
def seeded(erp_cfg, erp_session_factory):
    erp_cfg()
    with session_scope() as db:
        o = put_order(db, "10", items=[("Vaso", "VAS-1")])
        o.fingerprint = "fp-v1"
        o.version = 1
        o2 = put_order(db, "11", complete=False)
        o2.fingerprint = "fp-x"
        c = put_order(db, "12", kind="cancelled", items=[("X", "X-1")])
        c.fingerprint = "fp-c"


def new_quote(order="10", key="quote-key-000001", volumes=VOLUMES, **extra):
    return api("POST", f"/sales-orders/{order}/shipping-quotes", {"volumes": volumes, **extra}, key=key)


def option(quote_id, key, **overrides):
    body = {"carrier": "Correios", "service": "PAC", "price": "23.90", "deadlineMinDays": 5,
            "deadlineMaxDays": 7, "tracking": True, "pickupMode": "Postagem"}
    body.update(overrides)
    return api("POST", f"/shipping-quotes/{quote_id}/options", body, key=key)


def test_quote_with_multiple_volumes_computes_totals_and_never_simulates_prices(seeded):
    created = new_quote()
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "draft" and body["source"] == "manual"
    assert body["totals"]["volumes"] == 2
    assert body["totals"]["weightKg"] == "3.250"
    assert body["totals"]["cubageM3"] == "0.0250"  # 30x20x15 + 40x40x10 em m3
    assert body["options"] == []  # nenhuma opção inventada
    assert "não contrata" in body["statement"]


def test_automatic_quote_explains_missing_provider(seeded):
    r = new_quote(key="auto-key-0000001", mode="automatic")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "provider_unavailable" and "conector" in detail["message"]
    listing = api("GET", "/sales-orders/10/shipping-quotes").json()
    assert listing["provider"]["available"] is False


@pytest.mark.parametrize(
    "volume",
    [
        {"weightKg": "0", "lengthCm": "30", "widthCm": "20", "heightCm": "15"},
        {"weightKg": "-1", "lengthCm": "30", "widthCm": "20", "heightCm": "15"},
        {"weightKg": "1", "lengthCm": "0", "widthCm": "20", "heightCm": "15"},
        {"weightKg": "1", "lengthCm": "30", "widthCm": "-5", "heightCm": "15"},
        {"weightKg": "1", "lengthCm": "30", "widthCm": "20", "heightCm": "9999"},
        {"weightKg": "1", "lengthCm": "30", "widthCm": "20"},
    ],
)
def test_invalid_volumes_are_rejected(seeded, volume):
    assert new_quote(volumes=[volume], key="bad-volume-0001").status_code == 422


def test_no_volumes_and_negative_values_are_rejected(seeded):
    assert new_quote(volumes=[], key="no-volume-00001").status_code == 422
    assert new_quote(declaredValue="-1", key="neg-declared-0001").status_code == 422
    q = new_quote().json()
    assert option(q["id"], "opt-neg-0000001", price="-0.01").status_code == 422
    assert option(q["id"], "opt-deadline-001", deadlineMinDays=9, deadlineMaxDays=3).status_code == 422
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert option(q["id"], "opt-past-000001", validUntil=past).status_code == 422


def test_incomplete_and_cancelled_orders_block_quotes(seeded):
    assert new_quote("11", key="incomplete-00001").json()["detail"]["code"] == "items_incomplete"
    assert new_quote("12", key="cancelled-000001").json()["detail"]["code"] == "order_cancelled"
    assert new_quote("ghost", key="ghost-0000001").status_code == 404


def test_manual_option_is_audited_and_selection_is_local_only(seeded):
    q = new_quote().json()
    after = option(q["id"], "opt-key-0000001").json()
    assert [o["carrier"] for o in after["options"]] == ["Correios"]
    assert after["options"][0]["source"] == "manual"
    selected = api("POST", f"/shipping-quotes/{q['id']}/select",
                   {"optionId": after["options"][0]["id"], "expectedVersion": after["version"]},
                   key="sel-key-0000001")
    assert selected.status_code == 200
    body = selected.json()
    assert body["status"] == "selected" and body["version"] == after["version"] + 1
    assert body["selectedBy"] == "admin@xnamai.com"
    assert "não contrata" in body["statement"]
    with session_scope() as db:
        actions = [e.action for e in db.scalars(select(ErpAuditEvent).where(ErpAuditEvent.resource_id == "10"))]
    assert actions == ["shipping.quote_created", "shipping.option_registered", "shipping.option_selected"]


def test_selecting_after_the_order_changed_is_refused_as_stale(seeded):
    q = new_quote().json()
    opt = option(q["id"], "opt-stale-00001").json()["options"][0]
    with session_scope() as db:  # a sincronização alterou o pedido depois da cotação
        order = db.scalar(select(ErpSalesOrder).where(ErpSalesOrder.external_id == "10"))
        order.fingerprint = "fp-v2"
        order.version = 2
    stale = api("GET", "/sales-orders/10/shipping-quotes").json()["items"][0]
    assert stale["stale"] is True and stale["status"] == "stale"
    refused = api("POST", f"/shipping-quotes/{q['id']}/select",
                  {"optionId": opt["id"], "expectedVersion": stale["version"]}, key="sel-stale-00001")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "stale_order"
    assert option(q["id"], "opt-stale-00002").json()["detail"]["code"] == "stale_order"
    row = api("GET", "/sales-orders?include=operational").json()["items"][0]
    shipping_state = next(i for i in api("GET", "/sales-orders?include=operational").json()["items"] if i["id"] == "10")
    assert shipping_state["operational"]["shipping"]["state"] == "stale"
    assert row  # lista continua respondendo


def test_expired_option_cannot_be_selected(seeded):
    q = new_quote().json()
    opt = option(q["id"], "opt-exp-0000001").json()["options"][0]
    with session_scope() as db:
        db.execute(update(ErpShippingOption).where(ErpShippingOption.id == opt["id"])
                   .values(valid_until=datetime.now(timezone.utc) - timedelta(hours=1)))
    quote = api("GET", f"/shipping-quotes/{q['id']}").json()
    assert quote["options"][0]["expired"] is True
    refused = api("POST", f"/shipping-quotes/{q['id']}/select",
                  {"optionId": opt["id"], "expectedVersion": quote["version"]}, key="sel-exp-0000001")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "option_expired"


def test_duplicate_selection_is_idempotent_and_conflicting_version_is_rejected(seeded):
    q = new_quote().json()
    full = option(q["id"], "opt-dup-0000001").json()
    opt = full["options"][0]
    payload = {"optionId": opt["id"], "expectedVersion": full["version"]}
    first = api("POST", f"/shipping-quotes/{q['id']}/select", payload, key="sel-dup-0000001")
    assert first.status_code == 200
    replay = api("POST", f"/shipping-quotes/{q['id']}/select", payload, key="sel-dup-0000001")
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    # outra chamada com chave nova e versão antiga: versão mudou, recusa sem duplicar
    stale_version = api("POST", f"/shipping-quotes/{q['id']}/select", payload, key="sel-dup-0000002")
    assert stale_version.status_code == 409 and stale_version.json()["detail"]["code"] == "version_conflict"
    with session_scope() as db:
        assert db.scalar(select(func.count(ErpShippingQuote.id)).where(ErpShippingQuote.status == "selected")) == 1
        assert db.scalar(select(func.count(ErpAuditEvent.id)).where(ErpAuditEvent.action == "shipping.option_selected")) == 1


def test_changing_the_selection_requires_a_reason_and_supersedes_the_previous_quote(seeded):
    a = new_quote(key="quote-key-aaaaaaa").json()
    opt_a = option(a["id"], "opt-a-000000001").json()
    api("POST", f"/shipping-quotes/{a['id']}/select",
        {"optionId": opt_a["options"][0]["id"], "expectedVersion": opt_a["version"]}, key="sel-a-000000001")
    b = new_quote(key="quote-key-bbbbbbb").json()
    opt_b = option(b["id"], "opt-b-000000001", carrier="Jadlog", service="Normal", price="29.80").json()
    payload = {"optionId": opt_b["options"][0]["id"], "expectedVersion": opt_b["version"]}
    refused = api("POST", f"/shipping-quotes/{b['id']}/select", payload, key="sel-b-000000001")
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "reason_required"
    ok = api("POST", f"/shipping-quotes/{b['id']}/select", {**payload, "reason": "Menor prazo"}, key="sel-b-000000002")
    assert ok.status_code == 200 and ok.json()["selectionReason"] == "Menor prazo"
    with session_scope() as db:
        statuses = {q.id: q.status for q in db.scalars(select(ErpShippingQuote))}
    assert statuses == {a["id"]: "superseded", b["id"]: "selected"}  # nunca duas selecionadas
    closed = api("POST", f"/shipping-quotes/{a['id']}/select",
                 {"optionId": opt_a["options"][0]["id"], "expectedVersion": 1}, key="sel-a-000000002")
    assert closed.json()["detail"]["code"] == "quote_closed"


def test_order_list_states_filter_and_summary_use_the_local_source(seeded):
    with session_scope() as db:
        o = put_order(db, "20", items=[("Y", "Y-1")])
        o.fingerprint = "fp-20"
    q = new_quote().json()
    opt = option(q["id"], "opt-list-000001").json()
    api("POST", f"/shipping-quotes/{q['id']}/select",
        {"optionId": opt["options"][0]["id"], "expectedVersion": opt["version"]}, key="sel-list-000001")
    new_quote(order="20", key="quote-key-order20")
    items = {i["id"]: i for i in api("GET", "/sales-orders?include=operational&page_size=100").json()["items"]}
    assert items["10"]["operational"]["shipping"]["state"] == "selected"
    assert "Correios" in items["10"]["operational"]["shipping"]["label"]
    assert items["20"]["operational"]["shipping"]["state"] == "draft"
    assert items["11"]["operational"]["shipping"]["state"] == "none"
    assert [i["id"] for i in api("GET", "/sales-orders?shipping=selected").json()["items"]] == ["10"]
    assert [i["id"] for i in api("GET", "/sales-orders?shipping=draft").json()["items"]] == ["20"]
    none_ids = {i["id"] for i in api("GET", "/sales-orders?shipping=none&page_size=100").json()["items"]}
    assert none_ids == {"11", "12"}
    assert api("GET", "/sales-orders?shipping=bogus").status_code == 422
    summary = api("GET", "/operational-summary").json()
    assert summary["shipping"] == {"available": True, "counts": {"none": 2, "draft": 1, "selected": 1, "stale": 0}}
    assert "shipping" in summary["filtersAvailable"]
    narrowed = api("GET", "/operational-summary?shipping=selected").json()
    assert narrowed["orders"]["count"] == 1


def test_permissions_and_connection_isolation(seeded, erp_session_factory):
    add_operator(erp_session_factory, "leitor@x.com", ["consulta"])
    add_operator(erp_session_factory, "estoque@x.com", ["estoque"])
    reader = bearer("leitor@x.com", "viewer")
    assert api("GET", "/sales-orders/10/shipping-quotes", headers=reader).status_code == 200
    denied = api("POST", "/sales-orders/10/shipping-quotes", {"volumes": VOLUMES}, key="perm-0000000001", headers=reader)
    assert denied.status_code == 403
    assert api("GET", "/sales-orders/10/shipping-quotes", headers=bearer("estoque@x.com", "viewer")).status_code == 403
    with session_scope() as db:
        other = put_order(db, "99", connection="outra-conexao", items=[("Z", "Z-1")])
        other.fingerprint = "fp-99"
    assert api("GET", "/sales-orders/99/shipping-quotes").status_code == 404  # outra conexão


def test_destination_zip_is_masked_without_pii_permission(seeded, erp_session_factory):
    add_operator(erp_session_factory, "comercial@x.com", ["consulta"])
    created = new_quote(destinationZip="01234567", key="quote-key-zip0001").json()
    assert created["destination"]["zip"] == "01234567"  # administrador vê
    masked = api("GET", f"/shipping-quotes/{created['id']}", headers=bearer("comercial@x.com", "viewer")).json()
    assert masked["destination"]["zip"] != "01234567" and masked["destination"]["zip"].endswith("67")


def test_idempotency_key_is_required_and_replay_does_not_duplicate(seeded):
    assert api("POST", "/sales-orders/10/shipping-quotes", {"volumes": VOLUMES}).status_code == 422
    first = new_quote(key="idem-quote-000001")
    again = new_quote(key="idem-quote-000001")
    assert first.status_code == 201 and again.json()["replayed"] is True
    assert again.json()["id"] == first.json()["id"]
    with session_scope() as db:
        assert db.scalar(select(func.count(ErpShippingQuote.id))) == 1
    reused = new_quote(key="idem-quote-000001", volumes=VOLUMES[:1])
    assert reused.status_code == 409  # mesma chave com corpo diferente


@pytest.mark.skipif(not PG_URL, reason="concorrência real exige PostgreSQL")
def test_concurrent_selection_has_exactly_one_winner(seeded):
    q = new_quote().json()
    full = option(q["id"], "opt-conc-000001").json()
    option_ids = [full["options"][0]["id"]]
    other = option(q["id"], "opt-conc-000002", carrier="Jadlog", service="Normal", price="29.80").json()
    option_ids = [o["id"] for o in other["options"]]
    results, errors = [], []

    def do_select(index, option_id):
        try:
            r = api("POST", f"/shipping-quotes/{q['id']}/select",
                    {"optionId": option_id, "expectedVersion": other["version"], "reason": "x"},
                    key=f"sel-conc-{index:08d}")
            results.append(r.status_code)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=do_select, args=(i, option_ids[i % 2])) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert results.count(200) == 1 and results.count(409) == len(results) - 1
    with session_scope() as db:
        assert db.scalar(select(func.count(ErpShippingQuote.id)).where(ErpShippingQuote.status == "selected")) == 1
        assert db.scalar(select(func.count(ErpAuditEvent.id)).where(ErpAuditEvent.action == "shipping.option_selected")) == 1
