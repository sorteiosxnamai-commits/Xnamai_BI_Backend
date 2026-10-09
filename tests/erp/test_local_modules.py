import threading
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.erp.auth import ErpUser
from app.erp.common import utcnow
from app.erp.db import ErpBase
from app.erp.models import (
    ErpFinInstallment,
    ErpFinSettlement,
    ErpInventoryBalance,
    ErpInventoryMovement,
    ErpPurchaseOrderItem,
    ErpWarehouse,
)
from app.erp.schemas.commands import (
    PayableInput,
    PurchaseOrderInput,
    ReceiptInput,
    SupplierInput,
)
from app.erp.services import finance, inventory, purchasing
from tests.erp.helpers import add_operator, bearer, client
from tests.erp.pg import PG_URL, fresh_database

ADMIN = ErpUser("admin@xnamai.com", ["erp_admin"], permissions={"*"})
C = "test"
D = Decimal


@pytest.fixture(autouse=True)
def _setup(erp_cfg, erp_session_factory):
    erp_cfg()


def make_warehouse(db, code="MAIN"):
    return inventory.create_warehouse(db, ADMIN, C, code, "Principal")


def enable_erp_authority(db):
    inventory.set_authority(
        db, ADMIN, C, authority="erp", scope="", reason="corte aprovado", cutover_reconciled=True
    )


def test_authority_defaults_to_mercos_and_blocks_official_movements(erp_session_factory):
    with erp_session_factory() as db:
        w = make_warehouse(db)
        with pytest.raises(Exception) as err:
            inventory.adjust_to(db, ADMIN, C, warehouse_id=w.id, product="1", new_quantity=D(5),
                                reason="inventário", key="k-adjust-0001")
        assert err.value.detail["code"] == "inventory_authority_not_erp"
        with pytest.raises(Exception) as err:
            inventory.set_authority(db, ADMIN, C, authority="erp", scope="", reason="quero",
                                    cutover_reconciled=False)
        assert err.value.detail["code"] == "cutover_not_reconciled"
        row = inventory.get_authority(db, C)
        assert row.authority == "mercos" and row.publish_enabled is False


def test_authority_setting_alone_is_not_enough(erp_cfg, erp_session_factory):
    erp_cfg(erp_inventory_authority="erp")  # configuração sem corte conciliado
    with erp_session_factory() as db:
        w = make_warehouse(db)
        with pytest.raises(Exception) as err:
            inventory.adjust_to(db, ADMIN, C, warehouse_id=w.id, product="1", new_quantity=D(1),
                                reason="teste teste", key="k-adjust-0002")
        assert err.value.detail["code"] == "inventory_authority_not_erp"


def test_adjust_is_absolute_idempotent_by_cause_and_never_negative(erp_session_factory):
    with erp_session_factory() as db:
        w = make_warehouse(db)
        enable_erp_authority(db)
        m1 = inventory.adjust_to(db, ADMIN, C, warehouse_id=w.id, product="1", new_quantity=D(10),
                                 reason="inventário inicial", key="k-adjust-0003")
        again = inventory.adjust_to(db, ADMIN, C, warehouse_id=w.id, product="1", new_quantity=D(10),
                                    reason="inventário inicial", key="k-adjust-0003")
        assert again.id == m1.id  # mesmo evento causal, mesmo efeito (uma vez)
        m2 = inventory.adjust_to(db, ADMIN, C, warehouse_id=w.id, product="1", new_quantity=D(4),
                                 reason="perda", key="k-adjust-0004")
        assert m2.quantity_delta == D(-6)
        balance = db.scalar(select(ErpInventoryBalance))
        assert balance.on_hand == D(4)
        assert db.scalar(select(func.count(ErpInventoryMovement.id))) == 2
        with pytest.raises(Exception) as err:
            inventory.apply_movement(db, connection_id=C, warehouse_id=w.id, product="1",
                                     kind="sale_issue", quantity_delta=D(-5), causal_key="x-1",
                                     operator="t")
        assert err.value.detail["code"] == "insufficient_stock"


def test_transfer_and_reservation_rules(erp_session_factory):
    with erp_session_factory() as db:
        a = make_warehouse(db, "A")
        b = make_warehouse(db, "B")
        enable_erp_authority(db)
        inventory.adjust_to(db, ADMIN, C, warehouse_id=a.id, product="9", new_quantity=D(10),
                            reason="carga inicial", key="k-adjust-0005")
        inventory.transfer(db, ADMIN, C, from_warehouse=a.id, to_warehouse=b.id, product="9",
                           quantity=D(3), reason=None, key="k-transfer-001")
        inventory.transfer(db, ADMIN, C, from_warehouse=a.id, to_warehouse=b.id, product="9",
                           quantity=D(3), reason=None, key="k-transfer-001")  # replay
        balances = {x.warehouse_id: x.on_hand for x in db.scalars(select(ErpInventoryBalance))}
        assert balances == {a.id: D(7), b.id: D(3)}
        with pytest.raises(Exception) as err:
            inventory.transfer(db, ADMIN, C, from_warehouse=a.id, to_warehouse=a.id, product="9",
                               quantity=D(1), reason=None, key="k-transfer-002")
        assert err.value.detail["code"] == "same_warehouse"
        r = inventory.reserve(db, ADMIN, C, warehouse_id=a.id, product="9", quantity=D(5),
                              order_id="o1", key="k-reserve-0001")
        with pytest.raises(Exception) as err:  # reserva acima do disponível
            inventory.reserve(db, ADMIN, C, warehouse_id=a.id, product="9", quantity=D(3),
                              order_id="o2", key="k-reserve-0002")
        assert err.value.detail["code"] == "insufficient_stock"
        with pytest.raises(Exception):  # baixa que invadiria o reservado
            inventory.apply_movement(db, connection_id=C, warehouse_id=a.id, product="9",
                                     kind="sale_issue", quantity_delta=D(-3), causal_key="x-2",
                                     operator="t")
        inventory.release(db, ADMIN, C, r.id)
        inventory.release(db, ADMIN, C, r.id)  # liberar de novo não repete o efeito
        bal = db.scalar(select(ErpInventoryBalance).where(ErpInventoryBalance.warehouse_id == a.id))
        assert bal.reserved == D(0) and bal.on_hand == D(7)


def run_threads(n, target):
    results, lock = [], threading.Lock()

    def wrapper(i):
        try:
            value = target(i)
        except Exception as exc:  # noqa: BLE001
            value = exc
        with lock:
            results.append(value)

    threads = [threading.Thread(target=wrapper, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


@pytest.fixture
def file_factory(tmp_path):
    """Concorrência real: PostgreSQL se configurado; senão SQLite em arquivo."""
    if PG_URL:
        engine = create_engine(fresh_database("erp_concurrency"), pool_size=20, max_overflow=20)
    else:
        url = f"sqlite:///{(tmp_path / 'concurrency.db').as_posix()}"
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 60})
    ErpBase.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def attempt(factory, work):
    for _ in range(40):
        with factory() as db:
            try:
                result = work(db)
                db.commit()
                return result
            except OperationalError:
                db.rollback()  # contenção do SQLite; o efeito ainda não foi aplicado
    raise AssertionError("contenção do banco não resolveu")


def test_concurrent_stock_outflows_never_go_negative(file_factory):
    with file_factory() as db:
        w = make_warehouse(db)
        enable_erp_authority(db)
        inventory.adjust_to(db, ADMIN, C, warehouse_id=w.id, product="p", new_quantity=D(5),
                            reason="carga inicial", key="k-adjust-conc1")
        db.commit()
        wid = w.id

    def outflow(i):
        def work(db):
            inventory.apply_movement(db, connection_id=C, warehouse_id=wid, product="p",
                                     kind="sale_issue", quantity_delta=D(-1),
                                     causal_key=f"sale:{i}", operator="t")
            return "ok"
        try:
            return attempt(file_factory, work)
        except Exception as exc:  # noqa: BLE001
            return exc

    results = run_threads(12, outflow)
    assert results.count("ok") == 5
    rejected = [r for r in results if r != "ok"]
    assert len(rejected) == 7 and all(getattr(r, "detail", {}).get("code") == "insufficient_stock" for r in rejected)
    with file_factory() as db:
        assert db.scalar(select(ErpInventoryBalance.on_hand)) == D(0)


def test_concurrent_settlements_never_exceed_open_amount(file_factory):
    with file_factory() as db:
        acc = finance.create_account(db, ADMIN, C, code="CX", name="Caixa", kind="cash", opening=D(0))
        supplier = purchasing.create_supplier(
            db, ADMIN, C, SupplierInput(code="F1", name="Fornecedor"))
        title, _ = finance.create_title(db, ADMIN, C, kind="payable", description="NF 1", total=D("100.00"),
                                        first_due=date(2026, 11, 10), installments=1,
                                        supplier_id=supplier.id)
        db.commit()
        installment_id = db.scalar(select(ErpFinInstallment.id))
        account_id = acc.id

    def settle(i):
        def work(db):
            finance.settle(db, ADMIN, C, installment_id=installment_id, account_id=account_id,
                           amount=D("40.00"), reference=None, key=f"k-settle-{i:04d}")
            return "ok"
        try:
            return attempt(file_factory, work)
        except Exception as exc:  # noqa: BLE001
            return exc

    results = run_threads(6, settle)
    assert results.count("ok") == 2  # 40 + 40 = 80; a terceira passaria de 100
    assert all(getattr(r, "detail", {}).get("code") == "over_settlement" for r in results if r != "ok")
    with file_factory() as db:
        assert db.scalar(select(ErpFinInstallment.settled_amount)) == D("80.00")


def test_finance_settle_reverse_and_cash_flow(erp_session_factory):
    with erp_session_factory() as db:
        acc = finance.create_account(db, ADMIN, C, code="BB", name="Banco", kind="bank", opening=D("1000"))
        sup = purchasing.create_supplier(db, ADMIN, C, SupplierInput(code="F2", name="Forn 2"))
        title, _ = finance.create_title(db, ADMIN, C, kind="payable", description="Compra", total=D("100.01"),
                                        first_due=date(2026, 1, 31), installments=3, supplier_id=sup.id)
        parts = [i.amount for i in db.scalars(select(ErpFinInstallment).order_by(ErpFinInstallment.number))]
        assert sum(parts) == D("100.01") and parts[0] == D("33.34") or sum(parts) == D("100.01")
        dues = [i.due_date for i in db.scalars(select(ErpFinInstallment).order_by(ErpFinInstallment.number))]
        assert dues == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]
        inst = db.scalar(select(ErpFinInstallment).order_by(ErpFinInstallment.number))
        s1 = finance.settle(db, ADMIN, C, installment_id=inst.id, account_id=acc.id, amount=inst.amount,
                            reference="boleto", key="k-settle-aaaa")
        assert finance.settle(db, ADMIN, C, installment_id=inst.id, account_id=acc.id, amount=inst.amount,
                              reference="boleto", key="k-settle-aaaa").id == s1.id  # replay
        db.refresh(inst)
        assert inst.status == "settled"
        with pytest.raises(Exception) as err:
            finance.settle(db, ADMIN, C, installment_id=inst.id, account_id=acc.id, amount=D("0.01"),
                           reference=None, key="k-settle-bbbb")
        assert err.value.detail["code"] == "over_settlement"
        assert finance.account_balance(db, acc) == D("1000") - inst.amount
        with pytest.raises(Exception) as err:
            finance.cancel_title(db, ADMIN, C, title.id, "engano")
        assert err.value.detail["code"] == "has_settlements"
        rev = finance.reverse(db, ADMIN, C, settlement_id=s1.id, reason="boleto devolvido", key="k-reverse-001")
        again = finance.reverse(db, ADMIN, C, settlement_id=s1.id, reason="boleto devolvido", key="k-reverse-002")
        assert rev.id == again.id and rev.kind == "reversal"  # no máximo um estorno por baixa
        db.refresh(inst)
        assert inst.settled_amount == D(0) and inst.status == "open"
        assert finance.account_balance(db, acc) == D("1000")
        assert db.scalar(select(func.count(ErpFinSettlement.id))) == 2  # histórico preservado
        flow = finance.cash_flow(db, C, date(2026, 1, 1), date(2026, 3, 31))
        assert flow["totals"]["planned"] == "-100.01"
        finance.cancel_title(db, ADMIN, C, title.id, "engano")


def test_purchase_flow_with_receipt_stock_and_payable(erp_session_factory):
    with erp_session_factory() as db:
        w = make_warehouse(db)
        enable_erp_authority(db)
        sup = purchasing.create_supplier(db, ADMIN, C, SupplierInput(code="F3", name="Forn 3"))
        order = purchasing.create_purchase_order(db, ADMIN, C, PurchaseOrderInput(
            supplierId=sup.id,
            items=[{"productId": "77", "description": "Caixa", "quantity": "10", "unitCost": "2.5000"}]))
        assert order.number == f"PC-{order.id:06d}" and order.total == D("25.00")
        with pytest.raises(Exception) as err:
            purchasing.receive(db, ADMIN, C, order.id, ReceiptInput(
                warehouseId=w.id, lines=[{"itemId": 1, "quantity": "1"}]))
        assert err.value.detail["code"] == "not_receivable"  # rascunho não recebe
        purchasing.approve(db, ADMIN, C, order.id)
        item = db.scalar(select(ErpPurchaseOrderItem))
        first = purchasing.receive(db, ADMIN, C, order.id, ReceiptInput(
            warehouseId=w.id, invoiceNumber="123", lines=[{"itemId": item.id, "quantity": "4"}],
            payable=PayableInput(dueDate=date(2026, 12, 1), installments=2)))
        assert first["status"] == "partially_received" and first["inventoryEffect"] == "applied"
        assert first["payableTitleId"] is not None
        with pytest.raises(Exception) as err:  # acima do pendente
            purchasing.receive(db, ADMIN, C, order.id, ReceiptInput(
                warehouseId=w.id, lines=[{"itemId": item.id, "quantity": "7"}]))
        assert err.value.detail["code"] == "over_receipt"
        second = purchasing.receive(db, ADMIN, C, order.id, ReceiptInput(
            warehouseId=w.id, lines=[{"itemId": item.id, "quantity": "6"}]))
        assert second["status"] == "received" and second["payableTitleId"] is None
        balance = db.scalar(select(ErpInventoryBalance))
        assert balance.on_hand == D(10)
        assert db.scalar(select(func.count(ErpInventoryMovement.id))) == 2
        movement = db.scalar(select(ErpInventoryMovement).order_by(ErpInventoryMovement.id))
        assert movement.unit_cost == D("2.5000")  # custo real do recebimento
        with pytest.raises(Exception) as err:
            purchasing.cancel(db, ADMIN, C, order.id, "tarde demais")
        assert err.value.detail["code"] in ("not_cancellable", "has_receipts")


def test_receipt_without_erp_authority_is_explicit_about_stock(erp_session_factory):
    with erp_session_factory() as db:
        w = make_warehouse(db)
        sup = purchasing.create_supplier(db, ADMIN, C, SupplierInput(code="F4", name="Forn 4"))
        order = purchasing.create_purchase_order(db, ADMIN, C, PurchaseOrderInput(
            supplierId=sup.id,
            items=[{"productId": "8", "description": "Item", "quantity": "2", "unitCost": "1"}]))
        purchasing.approve(db, ADMIN, C, order.id)
        item = db.scalar(select(ErpPurchaseOrderItem))
        result = purchasing.receive(db, ADMIN, C, order.id, ReceiptInput(
            warehouseId=w.id, lines=[{"itemId": item.id, "quantity": "2"}]))
        assert result["inventoryEffect"] == "not_applied_authority_mercos"
        assert db.scalar(select(func.count(ErpInventoryMovement.id))) == 0
        assert db.scalar(select(func.count(ErpInventoryBalance.id))) == 0


def test_http_idempotency_for_local_commands(erp_session_factory):
    add_operator(erp_session_factory, "fin@x.com", ["financeiro"])
    c = client()
    h = bearer("fin@x.com", "viewer")
    acc = c.post("/api/v1/erp/finance/accounts", json={"code": "CX", "name": "Caixa"}, headers=h)
    assert acc.status_code == 201
    dup = c.post("/api/v1/erp/finance/accounts", json={"code": "CX", "name": "Caixa"}, headers=h)
    assert dup.status_code == 409 and dup.json()["detail"]["code"] == "duplicate_code"
    body = {"kind": "receivable", "description": "Venda", "total": "50.00",
            "firstDueDate": "2026-11-01", "customerId": "10"}
    no_key = c.post("/api/v1/erp/finance/titles", json=body, headers=h)
    assert no_key.status_code == 422
    first = c.post("/api/v1/erp/finance/titles", json=body, headers={**h, "Idempotency-Key": "titulo-0001"})
    replay = c.post("/api/v1/erp/finance/titles", json=body, headers={**h, "Idempotency-Key": "titulo-0001"})
    assert first.status_code == 201 and replay.json()["replayed"] is True
    assert replay.json()["id"] == first.json()["id"]
    diff = c.post("/api/v1/erp/finance/titles", json={**body, "total": "51.00"},
                  headers={**h, "Idempotency-Key": "titulo-0001"})
    assert diff.status_code == 409 and diff.json()["detail"]["code"] == "idempotency_key_reuse"
    # permissão: financeiro não mexe em estoque
    forbidden = c.post("/api/v1/erp/inventory/adjustments",
                       json={"warehouseId": 1, "productId": "1", "newQuantity": "1", "reason": "teste"},
                       headers={**h, "Idempotency-Key": "ajuste-0001"})
    assert forbidden.status_code == 403
    assert utcnow() is not None and ErpWarehouse is not None


def test_movement_reversal_is_causal_once_and_never_edits_history(erp_session_factory):
    with erp_session_factory() as db:
        w = make_warehouse(db)
        enable_erp_authority(db)
        adj = inventory.adjust_to(db, ADMIN, C, warehouse_id=w.id, product="r1", new_quantity=D(8),
                                  reason="carga inicial", key="k-adjust-rev1")
        rev = inventory.reverse_movement(db, ADMIN, C, adj.id, reason="contagem errada")
        again = inventory.reverse_movement(db, ADMIN, C, adj.id, reason="contagem errada")
        assert rev.id == again.id and rev.kind == "reversal" and rev.reversal_of_id == adj.id
        assert rev.quantity_delta == D(-8)
        assert db.scalar(select(ErpInventoryBalance.on_hand)) == D(0)
        assert db.scalar(select(func.count(ErpInventoryMovement.id))) == 2  # histórico preservado
        original = db.get(ErpInventoryMovement, adj.id)
        assert original.quantity_delta == D(8)
        # reverter sem saldo suficiente é recusado, sem saldo negativo
        recv = inventory.apply_movement(db, connection_id=C, warehouse_id=w.id, product="r1", kind="receipt",
                                        quantity_delta=D(5), causal_key="rc-1", operator="t")[0]
        inventory.apply_movement(db, connection_id=C, warehouse_id=w.id, product="r1", kind="sale_issue",
                                 quantity_delta=D(-4), causal_key="sale-1", operator="t")
        with pytest.raises(Exception) as err:
            inventory.reverse_movement(db, ADMIN, C, recv.id, reason="devolução ao fornecedor")
        assert err.value.detail["code"] == "insufficient_stock"
        # transferência e reserva não revertem isoladamente
        mv = inventory.apply_movement(db, connection_id=C, warehouse_id=w.id, product="r1", kind="transfer_out",
                                      quantity_delta=D(0), causal_key="tr-1", operator="t")[0]
        with pytest.raises(Exception) as err:
            inventory.reverse_movement(db, ADMIN, C, mv.id, reason="x x x")
        assert err.value.detail["code"] == "not_reversible"


def test_page_crash_never_advances_cursor_or_leaves_partial_rows(erp_session_factory, monkeypatch):
    import asyncio

    from app.erp.integrations.mercos_client import ListPage
    from app.erp.models import ErpCustomer, ErpSyncCheckpoint
    from app.erp.sync import engine, rows

    class Crash(BaseException):
        """Simula morte do processo no meio da página (não é Exception)."""

    original = rows.map_row  # usado tanto pelo caminho em lote quanto pelo linha a linha
    seen = []

    def exploding(definition, row):
        seen.append(row["id"])
        if row["id"] == 2:
            raise Crash()
        return original(definition, row)

    monkeypatch.setattr(rows, "map_row", exploding)

    class Fake:
        async def list_page(self, alias, cursor):
            data = [{"id": i, "razao_social": f"C{i}", "ultima_alteracao": "2026-10-07T10:00:00"} for i in (1, 2, 3)]
            return ListPage("customers", 3, "2026-10-07T10:00:00", None, data)

    with pytest.raises(Crash):
        asyncio.run(engine.sync_resource(Fake(), "test", "customers"))
    with erp_session_factory() as db:
        assert db.scalar(select(func.count(ErpCustomer.id))) == 0  # página inteira reverteu
        cp = db.scalar(select(ErpSyncCheckpoint))
        assert cp.cursor is None and cp.transport_cursor is None  # cursor não avançou
    monkeypatch.setattr(rows, "map_row", original)
    result = asyncio.run(engine.sync_resource(Fake(), "test", "customers"))
    assert result.status == "success" and result.persisted == 3
    with erp_session_factory() as db:
        assert db.scalar(select(ErpSyncCheckpoint)).cursor == "2026-10-07T10:00:00"
