import asyncio

import pytest
from sqlalchemy import func, select

from app.erp.integrations.mercos_client import AdaptorError, ListPage
from app.erp.models import (
    ErpCustomer,
    ErpCustomerContact,
    ErpFieldInventory,
    ErpQuarantine,
    ErpSalesOrder,
    ErpSalesOrderItem,
    ErpSourceSnapshot,
    ErpSyncCheckpoint,
    ErpSyncRun,
)
from app.erp.sync import engine


class FakeClient:
    def __init__(self, pages: list):
        self.pages = list(pages)
        self.calls: list[str | None] = []

    async def list_page(self, alias, cursor):
        self.calls.append(cursor)
        item = self.pages.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def page(rows, *, page_cursor=None, next_cursor=None, resource="customers"):
    return ListPage(resource, len(rows), page_cursor, next_cursor, rows)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _cfg(erp_cfg, erp_session_factory):
    erp_cfg()


def customer(i, **extra):
    return {
        "id": i,
        "razao_social": f"Cliente {i}",
        "cnpj": "12.345.678/0001-90",
        "emails": [{"email": f"c{i}@x.com"}, {"email": "outro@x.com"}],
        "contatos": [{"id": 1, "nome": "Ana", "emails": [{"email": "ana@x.com"}]}],
        "ultima_alteracao": "2026-10-07T10:00:00",
        "campo_novo_desconhecido": "x",
        **extra,
    }


def test_two_pages_checkpoint_uses_page_cursor_and_inventory(erp_session_factory):
    client = FakeClient(
        [
            page([customer(1)], page_cursor="2026-10-07T10:00:00", next_cursor="2026-10-07T10:00:00"),
            page([customer(2)], page_cursor="2026-10-07T11:00:00", next_cursor=None),
        ]
    )
    result = run(engine.sync_resource(client, "test", "customers"))
    assert result.status == "success" and result.pages == 2 and result.persisted == 2
    with erp_session_factory() as db:
        cp = db.scalar(select(ErpSyncCheckpoint))
        assert cp.cursor == "2026-10-07T11:00:00"
        assert cp.unresolved == 0
        assert db.scalar(select(func.count(ErpCustomer.id))) == 2
        row = db.scalar(select(ErpCustomer).where(ErpCustomer.external_id == "1"))
        assert row.document == "12.345.678/0001-90"
        assert row.email == "c1@x.com"
        assert row.extras["emails"][1]["email"] == "outro@x.com"
        inv = {i.source_key: i.mapped for i in db.scalars(select(ErpFieldInventory))}
        assert inv["campo_novo_desconhecido"] is False
        assert inv["razao_social"] is True
        assert inv["contatos[].nome"] is True
    assert result.unmapped == ["campo_novo_desconhecido"]


def test_empty_page_does_not_erase_checkpoint(erp_session_factory):
    run(engine.sync_resource(FakeClient([page([customer(1)], page_cursor="2026-10-07T10:00:00")]), "test", "customers"))
    run(engine.sync_resource(FakeClient([page([])]), "test", "customers"))
    with erp_session_factory() as db:
        assert db.scalar(select(ErpSyncCheckpoint)).cursor == "2026-10-07T10:00:00"


def test_reprocessing_does_not_duplicate_and_overlaps_cursor(erp_session_factory):
    rows = [customer(1)]
    run(engine.sync_resource(FakeClient([page(rows, page_cursor="2026-10-07T10:00:00")]), "test", "customers"))
    client = FakeClient([page(rows, page_cursor="2026-10-07T10:00:00")])
    result = run(engine.sync_resource(client, "test", "customers"))
    assert result.unchanged == 1 and result.persisted == 0
    assert client.calls == ["2026-10-07T09:59:55"]
    with erp_session_factory() as db:
        assert db.scalar(select(func.count(ErpCustomer.id))) == 1
        assert db.scalar(select(func.count(ErpSourceSnapshot.id))) == 1


def test_invalid_row_goes_to_quarantine_and_blocks_checkpoint(erp_session_factory):
    bad = customer(2, limite_credito="abc")
    result = run(
        engine.sync_resource(
            FakeClient([page([customer(1), bad], page_cursor="2026-10-07T10:00:00")]),
            "test",
            "customers",
        )
    )
    assert result.persisted == 1 and result.quarantined == 1
    with erp_session_factory() as db:
        cp = db.scalar(select(ErpSyncCheckpoint))
        assert cp.cursor is None and cp.unresolved == 1
        assert cp.transport_cursor == "2026-10-07T10:00:00"
        assert cp.status == "partial_quarantine"
        assert db.scalar(select(func.count(ErpQuarantine.id))) == 1
    run(
        engine.sync_resource(
            FakeClient([page([customer(2)], page_cursor="2026-10-07T10:00:00")]),
            "test",
            "customers",
        )
    )
    with erp_session_factory() as db:
        cp = db.scalar(select(ErpSyncCheckpoint))
        assert cp.unresolved == 0 and cp.cursor == "2026-10-07T10:00:00"


def test_repeated_cursor_with_continuation_fails_visibly(erp_session_factory):
    client = FakeClient(
        [
            page([customer(1)], page_cursor="2026-10-07T10:00:00", next_cursor="2026-10-07T10:00:00"),
            page([customer(2)], page_cursor="2026-10-07T10:00:00", next_cursor="2026-10-07T10:00:00"),
        ]
    )
    result = run(engine.sync_resource(client, "test", "customers", mode="full"))
    assert result.status == "failed"
    assert "Cursor repetido" in result.error


def test_rate_limit_schedules_and_keeps_cursor(erp_session_factory):
    run(engine.sync_resource(FakeClient([page([customer(1)], page_cursor="2026-10-07T10:00:00")]), "test", "customers"))
    client = FakeClient([AdaptorError("rate_limited", "429", status_code=429, retry_after=42)])
    result = run(engine.sync_resource(client, "test", "customers"))
    assert result.status == "waiting_rate_limit" and result.retry_after == 42
    with erp_session_factory() as db:
        cp = db.scalar(select(ErpSyncCheckpoint))
        assert cp.cursor == "2026-10-07T10:00:00"
        assert cp.retry_after is not None


def test_forbidden_is_readable_state_not_loop(erp_session_factory):
    client = FakeClient([AdaptorError("forbidden", "negado", status_code=403)])
    result = run(engine.sync_resource(client, "test", "segments"))
    assert result.status == "forbidden"
    assert len(client.pages) == 0


def test_stale_version_does_not_regress_entity(erp_session_factory):
    new = customer(1, razao_social="Novo", ultima_alteracao="2026-10-07T12:00:00")
    old = customer(1, razao_social="Antigo", ultima_alteracao="2026-10-07T09:00:00")
    run(engine.sync_resource(FakeClient([page([new], page_cursor="2026-10-07T12:00:00")]), "test", "customers"))
    run(engine.sync_resource(FakeClient([page([old], page_cursor="2026-10-07T12:00:00")]), "test", "customers", mode="full"))
    with erp_session_factory() as db:
        assert db.scalar(select(ErpCustomer.name)) == "Novo"
        assert db.scalar(select(func.count(ErpSourceSnapshot.id))) == 2


def test_absent_field_keeps_value_and_null_clears(erp_session_factory):
    run(engine.sync_resource(FakeClient([page([customer(1, observacao="nota")], page_cursor="2026-10-07T10:00:00")]), "test", "customers"))
    partial = {"id": 1, "razao_social": "Renomeado", "ultima_alteracao": "2026-10-07T11:00:00"}
    run(engine.sync_resource(FakeClient([page([partial], page_cursor="2026-10-07T11:00:00")]), "test", "customers"))
    with erp_session_factory() as db:
        row = db.scalar(select(ErpCustomer))
        assert row.name == "Renomeado" and row.notes == "nota"
        assert row.email == "c1@x.com"  # payload parcial não apaga
    cleared = {"id": 1, "observacao": None, "contatos": [], "ultima_alteracao": "2026-10-07T12:00:00"}
    run(engine.sync_resource(FakeClient([page([cleared], page_cursor="2026-10-07T12:00:00")]), "test", "customers"))
    with erp_session_factory() as db:
        row = db.scalar(select(ErpCustomer))
        assert row.notes is None
        assert db.scalar(select(func.count(ErpCustomerContact.id))) == 0


def test_orders_items_and_status_separation(erp_session_factory):
    order = {
        "id": 900,
        "numero": 55,
        "cliente_id": 1,
        "status": 2,
        "total": "150,50",
        "data_emissao": "2026-10-07",
        "itens": [
            {"id": 1, "produto_id": 7, "quantidade": 2, "preco_tabela": "10.00", "preco_liquido": "9.50", "excluido": False},
            {"id": 2, "produto_id": 8, "quantidade": 1, "preco_liquido": "5", "excluido": True},
        ],
        "ultima_alteracao": "2026-10-07T10:00:00",
    }
    run(engine.sync_resource(FakeClient([page([order], page_cursor="2026-10-07T10:00:00", resource="orders")]), "test", "orders"))
    with erp_session_factory() as db:
        row = db.scalar(select(ErpSalesOrder))
        assert row.kind == "order" and row.commercial_status == "2"
        assert row.items_complete is True and row.item_count == 1
        assert float(row.net_total) == 150.5
        assert row.issue_date.isoformat() == "2026-10-07"
        assert db.scalar(select(func.count(ErpSalesOrderItem.id))) == 2
    no_items = {"id": 901, "status": 1, "ultima_alteracao": "2026-10-07T10:30:00"}
    run(engine.sync_resource(FakeClient([page([no_items], page_cursor="2026-10-07T10:30:00", resource="orders")]), "test", "orders"))
    with erp_session_factory() as db:
        row = db.scalar(select(ErpSalesOrder).where(ErpSalesOrder.external_id == "901"))
        assert row.kind == "quote" and row.items_complete is False


def test_run_history_recorded(erp_session_factory):
    run(engine.sync_resource(FakeClient([page([customer(1)], page_cursor="2026-10-07T10:00:00")]), "test", "customers"))
    with erp_session_factory() as db:
        r = db.scalar(select(ErpSyncRun))
        assert r.status == "success" and r.received == 1 and r.persisted == 1
