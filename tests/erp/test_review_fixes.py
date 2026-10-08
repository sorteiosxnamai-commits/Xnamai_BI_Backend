import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.erp import inbox, queue
from app.erp.auth import ErpUser
from app.erp.models import (
    ErpFinInstallment,
    ErpFinTitle,
    ErpInventoryBalance,
    ErpInventoryMovement,
    ErpJob,
    ErpPurchaseOrder,
    ErpPurchaseOrderItem,
    ErpPurchaseReceipt,
    ErpSyncRun,
    ErpWebhookInbox,
)
from app.erp.schemas.commands import PayableInput, PurchaseOrderInput, ReceiptInput, SupplierInput
from app.erp.services import finance, inventory, purchasing
from app.erp.sync import engine
from tests.erp.helpers import bearer, client

ADMIN = ErpUser("admin@xnamai.com", ["erp_admin"], permissions={"*"})
C = "test"
D = Decimal


@pytest.fixture(autouse=True)
def _setup(erp_cfg, erp_session_factory):
    erp_cfg()


def setup_purchase(db, *, authority_erp=True, qty="10", payable=True):
    warehouse = inventory.create_warehouse(db, ADMIN, C, "MAIN", "Principal")
    if authority_erp:
        inventory.set_authority(db, ADMIN, C, authority="erp", scope="", reason="corte aprovado",
                                cutover_reconciled=True)
    supplier = purchasing.create_supplier(db, ADMIN, C, SupplierInput(code="F1", name="Forn"))
    order = purchasing.create_purchase_order(db, ADMIN, C, PurchaseOrderInput(
        supplierId=supplier.id,
        items=[{"productId": "77", "description": "Caixa", "quantity": qty, "unitCost": "2.50"}]))
    purchasing.approve(db, ADMIN, C, order.id)
    item = db.scalar(select(ErpPurchaseOrderItem))
    return warehouse, order, item


def receive(db, warehouse, order, item, qty, payable=True):
    return purchasing.receive(db, ADMIN, C, order.id, ReceiptInput(
        warehouseId=warehouse.id, invoiceNumber="NF1", lines=[{"itemId": item.id, "quantity": qty}],
        payable=PayableInput(dueDate=date(2026, 12, 1), installments=2) if payable else None))


def test_receipt_reversal_undoes_stock_item_status_and_payable_atomically(erp_session_factory):
    with erp_session_factory() as db:
        w, order, item = setup_purchase(db)
        r = receive(db, w, order, item, "10")
        assert r["status"] == "received" and r["payableTitleId"]
        out = purchasing.reverse_receipt(db, ADMIN, C, order.id, r["receiptId"], "NF recusada")
        assert out["status"] == "approved" and out["stockEffect"] == "reversed"
        assert out["payableTitleCancelled"] == r["payableTitleId"]
        assert db.scalar(select(ErpInventoryBalance.on_hand)) == D(0)
        db.refresh(item)
        assert item.received_quantity == D(0)
        assert db.get(ErpFinTitle, r["payableTitleId"]).status == "cancelled"
        # histórico preservado: entrada original + reversão
        kinds = [m.kind for m in db.scalars(select(ErpInventoryMovement).order_by(ErpInventoryMovement.id))]
        assert kinds == ["receipt", "reversal"]
        # no máximo uma vez
        with pytest.raises(Exception) as err:
            purchasing.reverse_receipt(db, ADMIN, C, order.id, r["receiptId"], "de novo")
        assert err.value.detail["code"] == "already_reversed"
        # e o item volta a poder ser recebido
        again = receive(db, w, order, item, "4")
        assert again["status"] == "partially_received"


def test_receipt_reversal_is_blocked_when_stock_was_consumed_and_changes_nothing(erp_session_factory):
    with erp_session_factory() as db:
        w, order, item = setup_purchase(db)
        r = receive(db, w, order, item, "10")
        inventory.apply_movement(db, connection_id=C, warehouse_id=w.id, product="77", kind="sale_issue",
                                 quantity_delta=D(-8), causal_key="sale-x", operator="t")
        db.commit()
    with erp_session_factory() as db:
        with pytest.raises(Exception) as err:
            purchasing.reverse_receipt(db, ADMIN, C, 1, r["receiptId"], "devolução")
        assert err.value.detail["code"] == "insufficient_stock"
        db.rollback()  # o router não comita quando há erro
    with erp_session_factory() as db:
        receipt = db.scalar(select(ErpPurchaseReceipt))
        assert receipt.reversed_at is None  # nada mudou
        assert db.scalar(select(ErpInventoryBalance.on_hand)) == D(2)
        assert db.get(ErpFinTitle, r["payableTitleId"]).status == "open"
        assert db.scalar(select(ErpPurchaseOrderItem.received_quantity)) == D(10)


def test_receipt_reversal_is_blocked_when_the_payable_has_settlements(erp_session_factory):
    with erp_session_factory() as db:
        w, order, item = setup_purchase(db)
        r = receive(db, w, order, item, "10")
        account = finance.create_account(db, ADMIN, C, code="CX", name="Caixa", kind="cash", opening=D(0))
        inst = db.scalar(select(ErpFinInstallment).order_by(ErpFinInstallment.number))
        finance.settle(db, ADMIN, C, installment_id=inst.id, account_id=account.id, amount=D("5.00"),
                       reference=None, key="k-settle-rcpt1")
        db.commit()
    with erp_session_factory() as db:
        with pytest.raises(Exception) as err:
            purchasing.reverse_receipt(db, ADMIN, C, 1, r["receiptId"], "erro")
        assert err.value.detail["code"] == "has_settlements"
        db.rollback()
    with erp_session_factory() as db:
        assert db.scalar(select(ErpPurchaseReceipt.reversed_at)) is None
        assert db.scalar(select(ErpInventoryBalance.on_hand)) == D(10)


def test_partial_receipt_reversal_keeps_the_other_receipt(erp_session_factory):
    with erp_session_factory() as db:
        w, order, item = setup_purchase(db)
        r1 = receive(db, w, order, item, "4", payable=False)
        r2 = receive(db, w, order, item, "6", payable=False)
        out = purchasing.reverse_receipt(db, ADMIN, C, order.id, r1["receiptId"], "NF1 devolvida")
        assert out["status"] == "partially_received"
        assert db.scalar(select(ErpPurchaseOrderItem.received_quantity)) == D(6)
        assert db.scalar(select(ErpInventoryBalance.on_hand)) == D(6)
        assert r2["status"] == "received"


def test_receipt_reversal_without_erp_authority_has_no_stock_effect(erp_session_factory):
    with erp_session_factory() as db:
        w, order, item = setup_purchase(db, authority_erp=False)
        r = receive(db, w, order, item, "10", payable=False)
        assert r["inventoryEffect"] == "not_applied_authority_mercos"
        out = purchasing.reverse_receipt(db, ADMIN, C, order.id, r["receiptId"], "erro")
        assert out["stockEffect"] == "none" and out["status"] == "approved"


def test_receipt_reversal_http_requires_key_permission_and_is_idempotent(erp_session_factory):
    from tests.erp.helpers import add_operator

    with erp_session_factory() as db:
        w, order, item = setup_purchase(db)
        r = receive(db, w, order, item, "10", payable=False)
        db.commit()
        oid = order.id
    add_operator(erp_session_factory, "fin@x.com", ["financeiro"])
    c = client()
    url = f"/api/v1/erp/purchase-orders/{oid}/receipts/{r['receiptId']}/reversals"
    body = {"reason": "NF recusada pelo fiscal"}
    assert c.post(url, json=body, headers=bearer()).status_code == 422  # sem Idempotency-Key
    forbidden = c.post(url, json=body, headers={**bearer("fin@x.com", "viewer"), "Idempotency-Key": "estorno-0001"})
    assert forbidden.status_code == 403
    first = c.post(url, json=body, headers={**bearer(), "Idempotency-Key": "estorno-0001"})
    replay = c.post(url, json=body, headers={**bearer(), "Idempotency-Key": "estorno-0001"})
    assert first.status_code == 201 and replay.json()["replayed"] is True
    again = c.post(url, json=body, headers={**bearer(), "Idempotency-Key": "estorno-0002"})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "already_reversed"
    detail = c.get(f"/api/v1/erp/purchase-orders/{oid}", headers=bearer()).json()
    assert detail["receipts"][0]["reversedBy"] == "admin@xnamai.com"
    assert detail["receipts"][0]["lines"][0]["quantity"] == "10"


def test_cash_flow_groups_by_brasilia_day_not_utc(erp_session_factory):
    with erp_session_factory() as db:
        acc = finance.create_account(db, ADMIN, C, code="CX", name="Caixa", kind="cash", opening=D(0))
        sup = purchasing.create_supplier(db, ADMIN, C, SupplierInput(code="F9", name="F9"))
        finance.create_title(db, ADMIN, C, kind="payable", description="x", total=D("10.00"),
                             first_due=date(2026, 10, 7), installments=1, supplier_id=sup.id)
        inst = db.scalar(select(ErpFinInstallment))
        s = finance.settle(db, ADMIN, C, installment_id=inst.id, account_id=acc.id, amount=D("10.00"),
                           reference=None, key="k-settle-tz-01")
        # 22:30 de 7/out em Brasília = 01:30 de 8/out em UTC
        s.settled_at = datetime(2026, 10, 8, 1, 30, tzinfo=timezone.utc)
        db.add(s)
        db.flush()
        flow = finance.cash_flow(db, C, date(2026, 10, 7), date(2026, 10, 7))
        assert [d["date"] for d in flow["days"]] == ["2026-10-07"]
        assert flow["days"][0]["realized"] == "-10.00"


def test_active_job_is_unique_even_under_a_direct_duplicate_insert(erp_session_factory):
    with erp_session_factory() as db:
        a, created = queue.enqueue(db, kind="sync", connection_id=C, resource="customers")
        db.commit()
        a_id = a.id
        db.add(ErpJob(kind="sync", connection_id=C, resource="customers", mode="incremental"))
        with pytest.raises(IntegrityError):
            db.flush()  # o índice único parcial impede a duplicata mesmo sem passar por enqueue
        db.rollback()
    with erp_session_factory() as db:
        again, created_again = queue.enqueue(db, kind="sync", connection_id=C, resource="customers")
        assert created is True and created_again is False and again.id == a_id
        # concluído, o mesmo job pode ser enfileirado de novo
        job = db.get(ErpJob, a_id)
        job.status = "succeeded"
        db.commit()
        _, created_third = queue.enqueue(db, kind="sync", connection_id=C, resource="customers")
        assert created_third is True


def test_poison_webhook_stops_after_max_attempts(erp_session_factory, monkeypatch):
    from app.erp import workers

    with erp_session_factory() as db:
        db.add(ErpWebhookInbox(connection_id=C, dedupe_key="k", body_hash="h", raw_body=b"{}", status="received"))
        db.commit()

    def boom(_id):
        raise RuntimeError("falha inesperada")

    monkeypatch.setattr(inbox, "process", boom)
    for _ in range(inbox.MAX_ATTEMPTS + 3):
        asyncio.run(workers.process_inbox())
    with erp_session_factory() as db:
        row = db.scalar(select(ErpWebhookInbox))
        assert row.status == "failed" and row.attempts == inbox.MAX_ATTEMPTS
        assert "falha inesperada" in row.error
    assert inbox.pending_ids() == []


def test_run_left_running_by_a_crash_is_closed_on_the_next_run(erp_session_factory):
    from app.erp.integrations.mercos_client import ListPage

    with erp_session_factory() as db:
        db.add(ErpSyncRun(connection_id=C, resource="customers", mode="incremental", status="running"))
        db.commit()

    class Fake:
        async def list_page(self, alias, cursor):
            return ListPage(alias, 0, None, None, [])

    asyncio.run(engine.sync_resource(Fake(), C, "customers"))
    with erp_session_factory() as db:
        statuses = sorted(r.status for r in db.scalars(select(ErpSyncRun)))
        assert statuses == ["interrupted", "success"]
        assert db.scalar(select(func.count(ErpSyncRun.id)).where(ErpSyncRun.status == "running")) == 0
    assert ErpPurchaseOrder is not None and timedelta(0) is not None


def test_sandbox_probe_reports_unmapped_fields_without_values():
    import importlib.util
    from pathlib import Path

    from app.erp.integrations.mercos_client import AdaptorError, ListPage

    spec = importlib.util.spec_from_file_location(
        "probe", Path(__file__).resolve().parents[2] / "scripts" / "erp_sandbox_probe.py"
    )
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)

    class Fake:
        async def list_page(self, alias, cursor):
            if alias == "segments":
                raise AdaptorError("forbidden", "negado", status_code=403)
            return ListPage(alias, 1, "2026-10-07T10:00:00", "2026-10-07T10:00:00",
                            [{"id": 1, "nome": "SEGREDO-PII", "campo_novo": 1, "itens": [{"id": 1, "x": 2}]}])

    ok = asyncio.run(probe.probe_resource(Fake(), "customers"))
    assert ok["ok"] and ok["nextCursorPresent"] and ok["pageCursorPresent"]
    assert "campo_novo" in ok["unmapped"] and "itens[].x" in ok["unmapped"]
    assert "SEGREDO-PII" not in json.dumps(ok)  # só nomes e tipos, nunca valores
    denied = asyncio.run(probe.probe_resource(Fake(), "segments"))
    assert denied == {"resource": "segments", "ok": False, "kind": "forbidden", "status": 403,
                      "retryAfter": None, "message": "negado"}
