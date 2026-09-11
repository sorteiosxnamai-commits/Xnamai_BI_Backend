import asyncio
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal

from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Category, Order, OrderItem, Product, ProductPrice, SyncRun, SyncState
from app import sync

BR = ZoneInfo("America/Sao_Paulo")


@pytest.fixture(autouse=True)
def reset_order_detail_block():
    sync._order_detail_blocked = False
    yield
    sync._order_detail_blocked = False


@pytest.fixture
def sync_db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(sync, "SessionLocal", factory)
    return factory


class SuccessfulAdaptor:
    def __init__(self):
        self.list_cursors: list[str | None] = []
        self.detail_calls: list[str] = []

    async def list(self, resource: str, cursor: str | None):
        assert resource == "orders"
        self.list_cursors.append(cursor)
        return {
            "data": [
                {
                    "id": 10,
                    "numero": 100,
                    "ultima_alteracao": "2026-08-12T12:00:00+00:00",
                }
            ],
            "pageCursor": "2026-08-12T12:00:00+00:00",
            "nextCursor": None,
        }

    async def detail(self, resource: str, mercos_id: str):
        assert resource == "orders"
        self.detail_calls.append(mercos_id)
        return {
            "id": 10,
            "numero": 100,
            "status": 2,
            "total": "25,00",
            "itens": [
                {
                    "id": 501,
                    "produto_id": 1,
                    "quantidade": 2,
                    "preco_liquido": "10,00",
                    "subtotal": "20,00",
                },
                {
                    "id": 502,
                    "produto_id": 2,
                    "quantidade": 1,
                    "preco_liquido": "5,00",
                    "subtotal": "5,00",
                },
            ],
        }


@pytest.mark.asyncio
async def test_order_sync_fetches_detail_and_is_idempotent(sync_db, monkeypatch):
    fake = SuccessfulAdaptor()
    monkeypatch.setattr(sync, "adaptor", fake)

    first = await sync.sync_resource("orders", full=False)
    second = await sync.sync_resource("orders", full=False)

    with sync_db() as db:
        assert db.scalar(select(func.count(Order.id))) == 1
        assert db.scalar(select(func.count(OrderItem.id))) == 2
        assert set(db.scalars(select(OrderItem.mercos_item_id))) == {"501", "502"}
        order = db.scalar(select(Order))
        assert order.total == Decimal("25.00")
        assert order.net_total == Decimal("25.00")
        assert order.item_count == 2
        assert order.sku_count == 2
        state = db.get(SyncState, "orders")
        assert state.cursor == "2026-08-12T12:00:00+00:00"
        runs = list(db.scalars(select(SyncRun).order_by(SyncRun.id)))
        assert len(runs) == 2
        assert all(run.status == "success" for run in runs)
        assert all(run.details["detailsConsulted"] == 1 for run in runs)
        assert all(run.details["itemsPersisted"] == 2 for run in runs)

    assert first["records"] == 1
    assert second["records"] == 1
    assert fake.list_cursors == [None, "2026-08-12T12:00:00+00:00"]
    assert fake.detail_calls == ["10", "10"]


class ListWithItemsAdaptor:
    async def list(self, resource: str, cursor: str | None):
        assert resource == "orders"
        return {
            "data": [
                {
                    "id": 20,
                    "numero": 200,
                    "status": 2,
                    "total": "30,00",
                    "ultima_alteracao": "2026-08-13T13:00:00+00:00",
                    "itens": [
                        {
                            "id": 601,
                            "produto_id": 3,
                            "quantidade": 2,
                            "preco_tabela": "20,00",
                            "preco_liquido": "15,00",
                            "subtotal": "30,00",
                        },
                        {
                            "id": 602,
                            "produto_id": 3,
                            "quantidade": 100,
                            "preco_tabela": "1.000,00",
                            "preco_liquido": "1.000,00",
                            "subtotal": "100.000,00",
                            "excluido": True,
                        }
                    ],
                }
            ],
            "pageCursor": "2026-08-13T13:00:00+00:00",
            "nextCursor": None,
        }

    async def detail(self, resource: str, mercos_id: str):
        raise AssertionError("Detalhe não deve ser consultado quando a lista contém itens")


@pytest.mark.asyncio
async def test_order_sync_uses_items_from_v2_list_without_detail(
    sync_db,
    monkeypatch,
):
    monkeypatch.setattr(sync, "adaptor", ListWithItemsAdaptor())

    result = await sync.sync_resource("orders", full=False)

    assert result["status"] == "success"
    with sync_db() as db:
        assert db.scalar(select(func.count(Order.id))) == 1
        assert db.scalar(select(func.count(OrderItem.id))) == 2
        order = db.scalar(select(Order))
        item = db.scalar(select(OrderItem).where(OrderItem.excluded.is_(False)))
        excluded = db.scalar(select(OrderItem).where(OrderItem.excluded.is_(True)))
        assert order.item_count == 1
        assert excluded.mercos_item_id == "602"
        assert order.net_total == Decimal("30.00")
        assert order.gross_total == Decimal("40.00")
        assert order.discount_value == Decimal("10.00")
        assert order.discount_percent == Decimal("25.00")
        assert item.list_unit_price == Decimal("20.00")
        run = db.scalar(select(SyncRun))
        assert run.details["detailsConsulted"] == 0
        assert run.details["itemsPersisted"] == 2


class ListWithEmptyItemsAdaptor:
    def __init__(self):
        self.detail_calls: list[str] = []

    async def list(self, resource: str, cursor: str | None):
        assert resource == "orders"
        return {
            "data": [
                {
                    "id": 30,
                    "numero": 300,
                    "status": 2,
                    "total": "25,00",
                    "ultima_alteracao": "2026-08-14T14:00:00+00:00",
                    "itens": [],
                }
            ],
            "pageCursor": "2026-08-14T14:00:00+00:00",
            "nextCursor": None,
        }

    async def detail(self, resource: str, mercos_id: str):
        assert resource == "orders"
        self.detail_calls.append(mercos_id)
        return {
            "id": 30,
            "numero": 300,
            "status": 2,
            "total": "25,00",
            "itens": [
                {
                    "id": 701,
                    "produto_id": 1,
                    "quantidade": 1,
                    "preco_liquido": "25,00",
                    "subtotal": "25,00",
                }
            ],
        }


@pytest.mark.asyncio
async def test_order_sync_keeps_empty_list_items_without_detail(
    sync_db,
    monkeypatch,
):
    fake = ListWithEmptyItemsAdaptor()
    monkeypatch.setattr(sync, "adaptor", fake)

    result = await sync.sync_resource("orders", full=False)

    assert result["status"] == "success"
    assert fake.detail_calls == []
    with sync_db() as db:
        order = db.scalar(select(Order))
        assert order.item_count == 0
        assert db.scalar(select(func.count(OrderItem.id))) == 0
        run = db.scalar(select(SyncRun))
        assert run.details["detailsConsulted"] == 0


class ListWithoutItemsForbiddenDetailAdaptor:
    def __init__(self):
        self.detail_calls: list[str] = []

    async def list(self, resource: str, cursor: str | None):
        return {
            "data": [
                {
                    "id": 40,
                    "numero": 400,
                    "status": 2,
                    "total": "80,00",
                    "ultima_alteracao": "2026-08-15T15:00:00+00:00",
                },
                {
                    "id": 41,
                    "numero": 401,
                    "status": 2,
                    "total": "90,00",
                    "ultima_alteracao": "2026-08-15T16:00:00+00:00",
                },
            ],
            "pageCursor": "2026-08-15T16:00:00+00:00",
            "nextCursor": None,
        }

    async def detail(self, resource: str, mercos_id: str):
        self.detail_calls.append(mercos_id)
        raise HTTPException(403, "GET por ID não permitido em produção")


@pytest.mark.asyncio
async def test_order_sync_persists_list_when_production_forbids_detail(
    sync_db,
    monkeypatch,
):
    fake = ListWithoutItemsForbiddenDetailAdaptor()
    monkeypatch.setattr(sync, "adaptor", fake)

    result = await sync.sync_resource("orders", full=False)

    assert result["status"] == "success"
    assert fake.detail_calls == ["40"]
    with sync_db() as db:
        assert db.scalar(select(func.count(Order.id))) == 2
        assert db.scalar(select(func.count(OrderItem.id))) == 0
        state = db.get(SyncState, "orders")
        assert state.cursor == "2026-08-15T16:00:00+00:00"
        assert state.status == "success"


@pytest.mark.asyncio
async def test_active_lease_prevents_duplicate_resource_sync(sync_db, monkeypatch):
    with sync_db() as db:
        db.add(
            SyncState(
                resource="orders",
                status="running",
                lease_token="active-token",
                heartbeat_at=datetime.now(timezone.utc),
            )
        )
        db.commit()

    class UnexpectedAdaptor:
        async def list(self, resource: str, cursor: str | None):
            raise AssertionError("duplicate sync must not call the adaptor")

    monkeypatch.setattr(sync, "adaptor", UnexpectedAdaptor())
    result = await sync.sync_resource("orders")
    assert result["status"] == "running"


@pytest.mark.asyncio
async def test_interrupt_running_syncs_releases_stuck_lease(sync_db):
    with sync_db() as db:
        db.add(
            SyncState(
                resource="orders",
                status="running",
                lease_token="stuck-token",
                heartbeat_at=datetime.now(timezone.utc),
                error=None,
            )
        )
        db.add(
            SyncRun(
                resource="orders",
                mode="incremental",
                status="running",
                started_at=datetime.now(timezone.utc),
            )
        )
        db.commit()

    released = sync.interrupt_running_syncs("Interrompida pelo operador")
    assert released == 1
    with sync_db() as db:
        state = db.scalar(select(SyncState).where(SyncState.resource == "orders"))
        run = db.scalar(select(SyncRun))
        assert state.status == "interrupted"
        assert state.lease_token is None
        assert run.status == "interrupted"


@pytest.mark.asyncio
async def test_claim_blocks_other_resources_while_one_is_running(sync_db, monkeypatch):
    with sync_db() as db:
        db.add(
            SyncState(
                resource="orders",
                status="running",
                lease_token="active-token",
                heartbeat_at=datetime.now(timezone.utc),
            )
        )
        db.commit()

    class UnexpectedAdaptor:
        async def list(self, resource: str, cursor: str | None):
            raise AssertionError("Mercos must stay idle while another resource is syncing")

    monkeypatch.setattr(sync, "adaptor", UnexpectedAdaptor())
    result = await sync.sync_resource("categories")
    assert result["status"] == "running"


@pytest.mark.asyncio
async def test_sync_all_continues_after_rate_limit(sync_db, monkeypatch):
    called: list[str] = []

    class RateLimitedThenOkAdaptor:
        async def list(self, resource: str, cursor: str | None):
            called.append(resource)
            if resource == "categories":
                raise HTTPException(429, "Too Many Requests")
            return {"data": [], "nextCursor": None}

    monkeypatch.setattr(sync, "adaptor", RateLimitedThenOkAdaptor())
    monkeypatch.setattr(sync, "SYNC_RESOURCES", ("categories", "orders"))
    monkeypatch.setattr(sync, "RESOURCE_PAUSE_SECONDS", 0)
    monkeypatch.setattr(sync, "RATE_LIMIT_PAUSE_SECONDS", 0)

    results = await sync.sync_all(full=False, raise_http=False)

    assert called == ["categories", "orders"]
    assert results[0]["status"] == "interrupted"
    assert results[1]["status"] == "success"
    assert len(results) == 2


@pytest.mark.asyncio
async def test_sync_all_stops_after_operator_cancel(sync_db, monkeypatch):
    called: list[str] = []

    class CancelledAdaptor:
        async def list(self, resource: str, cursor: str | None):
            called.append(resource)
            raise HTTPException(409, "Sincronização interrompida pelo operador")

    monkeypatch.setattr(sync, "adaptor", CancelledAdaptor())
    monkeypatch.setattr(sync, "SYNC_RESOURCES", ("categories", "orders"))
    monkeypatch.setattr(sync, "RESOURCE_PAUSE_SECONDS", 0)
    monkeypatch.setattr(sync, "RATE_LIMIT_PAUSE_SECONDS", 0)

    results = await sync.sync_all(full=False, raise_http=False)

    assert called == ["categories"]
    assert results[0]["status"] == "interrupted"
    assert len(results) == 1


@pytest.mark.asyncio
async def test_page_persistence_does_not_block_event_loop(sync_db, monkeypatch):
    monkeypatch.setattr(sync, "adaptor", ListWithItemsAdaptor())
    original = sync._persist_sync_page

    def slow_persist(*args, **kwargs):
        time.sleep(0.1)
        return original(*args, **kwargs)

    monkeypatch.setattr(sync, "_persist_sync_page", slow_persist)
    task = asyncio.create_task(sync.sync_resource("orders"))
    started = time.perf_counter()
    await asyncio.sleep(0.02)
    elapsed = time.perf_counter() - started
    result = await task

    assert elapsed < 0.08
    assert result["status"] == "success"


class FailingDetailAdaptor:
    async def list(self, resource: str, cursor: str | None):
        return {
            "data": [{"id": 11, "ultima_alteracao": "2026-08-13T12:00:00+00:00"}],
            "pageCursor": "2026-08-13T12:00:00+00:00",
            "nextCursor": None,
        }

    async def detail(self, resource: str, mercos_id: str):
        raise HTTPException(502, "Adaptor inacessível")


class UnavailableOptionalResourceAdaptor:
    def __init__(self, status_code: int):
        self.status_code = status_code

    async def list(self, resource: str, cursor: str | None):
        raise HTTPException(self.status_code, "Recurso indisponível na Mercos")


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [403, 404])
async def test_optional_resource_without_permission_is_unavailable(
    sync_db,
    monkeypatch,
    status_code,
):
    monkeypatch.setattr(
        sync,
        "adaptor",
        UnavailableOptionalResourceAdaptor(status_code),
    )

    result = await sync.sync_resource("carriers", full=False)

    assert result["status"] == "unavailable"
    with sync_db() as db:
        state = db.get(SyncState, "carriers")
        run = db.scalar(select(SyncRun))
        assert state.status == "unavailable"
        assert run.status == "unavailable"
        assert run.failed == 0


@pytest.mark.asyncio
async def test_detail_failure_does_not_advance_cursor(sync_db, monkeypatch):
    with sync_db() as db:
        db.add(
            SyncState(
                resource="orders",
                cursor="2026-08-01T00:00:00+00:00",
                status="success",
                records=20,
            )
        )
        db.commit()
    monkeypatch.setattr(sync, "adaptor", FailingDetailAdaptor())

    result = await sync.sync_resource("orders", full=False, raise_http=False)

    with sync_db() as db:
        state = db.get(SyncState, "orders")
        run = db.scalar(select(SyncRun).order_by(SyncRun.id.desc()))
        assert state.cursor == "2026-08-01T00:00:00+00:00"
        assert state.status == "interrupted"
        assert db.scalar(select(func.count(Order.id))) == 0
        assert run.cursor_before == "2026-08-01T00:00:00+00:00"
        assert run.cursor_after == "2026-08-01T00:00:00+00:00"
        assert run.received == 1
        assert run.persisted == 0
        assert run.failed == 1

    assert result["status"] == "interrupted"


class InvalidItemBatchAdaptor:
    async def list(self, resource: str, cursor: str | None):
        return {
            "data": [{"id": 12, "ultima_alteracao": "2026-08-14T12:00:00+00:00"}],
            "pageCursor": "2026-08-14T12:00:00+00:00",
            "nextCursor": None,
        }

    async def detail(self, resource: str, mercos_id: str):
        return {
            "id": 12,
            "status": 2,
            "total": 20,
            "itens": [
                {"id": 99, "produto_id": 1, "quantidade": 1, "subtotal": 10},
                {"id": 99, "produto_id": 2, "quantidade": 1, "subtotal": 10},
            ],
        }


@pytest.mark.asyncio
async def test_database_batch_failure_rolls_back_order_items_and_cursor(
    sync_db,
    monkeypatch,
):
    with sync_db() as db:
        db.add(
            SyncState(
                resource="orders",
                cursor="2026-08-01T00:00:00+00:00",
                status="success",
                records=20,
            )
        )
        db.commit()
    monkeypatch.setattr(sync, "adaptor", InvalidItemBatchAdaptor())

    result = await sync.sync_resource("orders", full=False, raise_http=False)

    with sync_db() as db:
        state = db.get(SyncState, "orders")
        run = db.scalar(select(SyncRun).order_by(SyncRun.id.desc()))
        assert state.cursor == "2026-08-01T00:00:00+00:00"
        assert db.scalar(select(func.count(Order.id))) == 0
        assert db.scalar(select(func.count(OrderItem.id))) == 0
        assert run.persisted == 0
        assert run.failed == 1
    assert result["status"] == "error"


def test_dimensions_and_product_prices_are_decimal_and_idempotent(sync_db):
    with sync_db() as db:
        sync._upsert_rows(
            db,
            "categories",
            [
                {
                    "id": 5,
                    "nome": "Bebidas",
                    "categoria_pai_id": 1,
                    "ativo": True,
                }
            ],
        )
        sync._upsert_rows(
            db,
            "products",
            [
                {
                    "id": 9,
                    "nome": "Produto",
                    "categoria_id": 5,
                    "preco_tabela": "1.234,56",
                    "preco_minimo": "1.000,00",
                    "saldo_estoque": "2,5000",
                }
            ],
        )
        sync._upsert_rows(
            db,
            "product-prices",
            [{"id": 1, "produto_id": 9, "tabela_preco_id": 3, "preco": "1.100,25"}],
        )
        sync._upsert_rows(
            db,
            "product-prices",
            [{"id": 2, "produto_id": 9, "tabela_preco_id": 3, "preco": "1.050,10"}],
        )
        sync._upsert_rows(
            db,
            "products",
            [
                {
                    "id": 9,
                    "nome": "Produto atualizado",
                    "categoria_id": 5,
                    "preco_tabela": "1.234,56",
                    "saldo_estoque": "2,5000",
                }
            ],
        )
        db.commit()

        category = db.scalar(select(Category))
        product = db.scalar(select(Product))
        price = db.scalar(select(ProductPrice))
        assert category.parent_mercos_id == "1"
        assert product.category_mercos_id == "5"
        assert product.list_price == Decimal("1234.56")
        assert product.minimum_price == Decimal("1000.00")
        assert product.stock == Decimal("2.5000")
        assert price.price == Decimal("1050.10")
        assert db.scalar(select(func.count(ProductPrice.id))) == 1


def test_invalid_nonempty_numeric_value_is_not_silently_zeroed():
    with pytest.raises(ValueError, match="Valor numérico inválido"):
        sync.f("valor-corrompido")


def test_dt_treats_date_only_and_naive_as_brasilia():
    date_only = sync.dt("2026-09-11")
    naive = sync.dt("2026-09-11T14:00:00")
    aware = sync.dt("2026-09-11T14:00:00+00:00")
    assert date_only == datetime(2026, 9, 11, tzinfo=BR).astimezone(timezone.utc)
    assert naive == datetime(2026, 9, 11, 14, tzinfo=BR).astimezone(timezone.utc)
    assert aware == datetime(2026, 9, 11, 14, tzinfo=timezone.utc)
    assert date_only.astimezone(BR).date().isoformat() == "2026-09-11"
