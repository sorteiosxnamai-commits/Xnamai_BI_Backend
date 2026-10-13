"""Atualização automática: fatias de tempo, retomada, ciclo incremental sem operador e atomicidade da página."""

import asyncio
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.erp import queue, workers
from app.erp.common import utcnow
from app.erp.db import session_scope
from app.erp.integrations.mercos_client import AdaptorError, ListPage
from app.erp.models import ErpCategory, ErpJob, ErpSalesOrder, ErpSyncCheckpoint, ErpSyncRun
from app.erp.sync import engine

C = "test"


class Client:
    """Adaptor simulado: nenhuma chamada real ao Mercos."""

    configured = True

    def __init__(self, orders_pages=(), categories=()):
        self.orders_pages = list(orders_pages)
        self.categories = list(categories)
        self.calls: list[tuple[str, str | None]] = []

    async def list_page(self, alias, cursor):
        self.calls.append((alias, cursor))
        if alias == "orders":
            if not self.orders_pages:
                return ListPage(alias, 0, None, None, [])
            return self.orders_pages.pop(0)
        if alias == "categories":
            rows = self.categories
            return ListPage(alias, len(rows), "2026-10-13T09:00:00" if rows else None, None, rows)
        raise AdaptorError("forbidden", f"Acesso negado (403) em {alias}", status_code=403)

    async def get_detail(self, resource, external_id):
        raise AdaptorError("forbidden", "detalhe por ID indisponível", status_code=403)


def order(i, changed, total="10"):
    return {"id": i, "numero": str(i), "cliente_id": 1, "status": 2, "total": total, "ultima_alteracao": changed,
            "itens": [{"id": i, "produto_id": 1, "quantidade": 1, "preco_liquido": total}]}


def opage(rows, cursor, nxt):
    return ListPage("orders", len(rows), cursor, nxt, rows)


@pytest.fixture(autouse=True)
def _cfg(erp_cfg, erp_session_factory, monkeypatch):
    erp_cfg(erp_auto_sync=False, erp_sync_slice_seconds=0.5)
    workers.reset_schedule_clock()
    ticks = iter(range(10_000))
    # relógio simulado: cada leitura avança 1 s, então a fatia (0,5 s) esgota depois da 1ª página, sem depender
    # da resolução do relógio do sistema operacional
    monkeypatch.setattr(engine, "time", SimpleNamespace(monotonic=lambda: float(next(ticks))))


def drain(client):
    async def go():
        for _ in range(20):
            if not await workers.drain_once(client, max_cycles=1):
                return
    asyncio.run(go())


def test_a_long_load_yields_so_other_resources_run_between_slices():
    client = Client(
        orders_pages=[
            opage([order(1, "2020-01-01T10:00:00")], "2020-01-01T10:00:00", "2020-01-01T10:00:00"),
            opage([order(2, "2020-02-01T10:00:00")], "2020-02-01T10:00:00", "2020-02-01T10:00:00"),
            opage([order(3, "2020-03-01T10:00:00")], "2020-03-01T10:00:00", None),
        ],
        categories=[{"id": 7, "nome": "Bebidas", "ultima_alteracao": "2026-10-13 09:00:00"}],
    )
    with session_scope() as db:
        queue.enqueue(db, kind="sync", connection_id=C, resource="orders", mode="full", requested_by="op")
        queue.enqueue(db, kind="sync", connection_id=C, resource="categories", mode="incremental", requested_by="op")
    drain(client)
    sequence = [alias for alias, _ in client.calls if alias in ("orders", "categories")]
    # categorias roda ENTRE as fatias de pedidos (não espera a carga terminar)
    assert sequence.index("categories") < len(sequence) - 1
    assert sequence[0] == "orders" and sequence[1] == "categories"
    with session_scope() as db:
        assert db.scalar(select(func.count(ErpSalesOrder.id))) == 3
        assert db.scalar(select(func.count(ErpCategory.id))) == 1
        runs = [(r.status, r.cursor_before) for r in db.scalars(
            select(ErpSyncRun).where(ErpSyncRun.resource == "orders").order_by(ErpSyncRun.id))]
        assert [s for s, _ in runs] == ["continuing", "continuing", "success"]
        # cada fatia retoma do cursor confirmado pela anterior (sobreposição de 5 s), não da página 1
        assert runs[0][1] is None and runs[1][1] == "2020-01-01T10:00:00" and runs[2][1] == "2020-02-01T10:00:00"
        cp = db.scalar(select(ErpSyncCheckpoint).where(ErpSyncCheckpoint.resource == "orders"))
        assert cp.status == "success" and cp.cursor == "2020-03-01T10:00:00"
        jobs = list(db.scalars(select(ErpJob).where(ErpJob.resource == "orders").order_by(ErpJob.id)))
        assert len(jobs) == 3 and {j.status for j in jobs} == {"succeeded"}  # sem job duplicado ou preso
        assert [j.requested_by for j in jobs] == ["op", "worker-continuation", "worker-continuation"]


def test_a_continuing_resource_is_shown_as_running_not_as_error_or_done():
    from app.erp.services.sync_phase import sync_phase

    assert sync_phase("continuing", None) == "running"
    assert sync_phase("continuing", utcnow()) == "done"  # já concluída antes; só atualizando


def test_a_job_picked_up_again_does_not_redo_what_it_already_finished(monkeypatch):
    with session_scope() as db:
        db.add(ErpSyncRun(connection_id=C, resource="customers", mode="full", status="success", job_id=11))
    seen: list[str] = []

    async def fake(client, connection_id, resource, **kwargs):
        seen.append(resource)
        return engine.SyncResult(resource=resource, status="success")

    monkeypatch.setattr(engine, "sync_resource", fake)
    job = ErpJob(id=11, kind="sync", connection_id=C, resource="all", mode="full", lease_token="t", attempts=2)
    out = asyncio.run(workers.run_sync_job(object(), job))
    assert "customers" not in seen and "orders" in seen
    assert next(r for r in out["results"] if r["resource"] == "customers")["status"] == "skipped_done"
    first_attempt = ErpJob(id=11, kind="sync", connection_id=C, resource="all", mode="full", lease_token="t", attempts=1)
    seen.clear()
    asyncio.run(workers.run_sync_job(object(), first_attempt))
    assert "customers" in seen  # primeira tomada: executa tudo


def test_new_changes_are_imported_without_browser_or_operator(erp_cfg):
    """Ciclo incremental sem ninguém logado: o agendador enfileira, o worker drena, o espelho muda."""
    erp_cfg(erp_auto_sync=True, erp_sync_slice_seconds=0)
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    with session_scope() as db:
        db.add(ErpSyncCheckpoint(connection_id=C, resource="orders", scope="", status="success", cursor="2026-10-12T09:00:00",
                                 last_success_at=old, last_attempt_at=old))
    with session_scope() as db:  # pedido já espelhado
        from tests.erp.test_order_operations import put_order

        row = put_order(db, "50", net="10.00", items=[("A", "A-1")])
        row.version = 1
    client = Client(orders_pages=[opage([order(50, "2026-10-13T08:00:00", total="25"), order(51, "2026-10-13T08:30:00")],
                                        "2026-10-13T08:30:00", None)])
    assert asyncio.run(workers.drain_once(client)) >= 1  # nenhum job foi criado por operador
    with session_scope() as db:
        changed = db.scalar(select(ErpSalesOrder).where(ErpSalesOrder.external_id == "50"))
        assert str(changed.net_total) == "25.00" and changed.version == 2  # alteração importada
        assert db.scalar(select(ErpSalesOrder).where(ErpSalesOrder.external_id == "51")) is not None  # pedido novo
        cp = db.scalar(select(ErpSyncCheckpoint).where(ErpSyncCheckpoint.resource == "orders"))
        assert cp.cursor == "2026-10-13T08:30:00" and cp.status == "success"
        requested = {j.requested_by for j in db.scalars(select(ErpJob).where(ErpJob.resource == "orders"))}
        assert requested == {"scheduler"}
    # a chamada ao Adaptor partiu do cursor confirmado (com a sobreposição), não de uma nova varredura completa
    orders_calls = [cursor for alias, cursor in client.calls if alias == "orders"]
    assert orders_calls[0].startswith("2026-10-12T08:59")


def test_page_and_checkpoint_are_one_transaction(monkeypatch):
    """Falha depois de gravar as linhas desfaz a página inteira: nem linha nem checkpoint avançam."""
    client = Client(orders_pages=[opage([order(1, "2020-01-01T10:00:00")], "2020-01-01T10:00:00", None)])

    def boom(*args, **kwargs):
        raise RuntimeError("falha depois de persist_rows")

    monkeypatch.setattr(engine, "update_field_inventory", boom)
    result = asyncio.run(engine.sync_resource(client, C, "orders", mode="full"))
    assert result.status == "failed"
    with session_scope() as db:
        assert db.scalar(select(func.count(ErpSalesOrder.id))) == 0
        cp = db.scalar(select(ErpSyncCheckpoint).where(ErpSyncCheckpoint.resource == "orders"))
        assert cp.cursor is None and cp.records in (0, None)


def test_abandoned_job_is_recovered_and_resumes_from_the_checkpoint(monkeypatch):
    past = utcnow() - timedelta(minutes=10)
    with session_scope() as db:
        job, _ = queue.enqueue(db, kind="sync", connection_id=C, resource="orders", mode="full", requested_by="op")
        job.status, job.lease_token, job.leased_until, job.attempts = "processing", "dead", past, 1
        db.add(ErpSyncRun(connection_id=C, resource="orders", mode="full", status="running", job_id=job.id,
                          cursor_after="2020-02-01T10:00:00", pages=2))
    client = Client(orders_pages=[opage([order(3, "2020-03-01T10:00:00")], "2020-03-01T10:00:00", None)])
    erp_slice = 0
    monkeypatch.setattr(workers.erp_settings(), "erp_sync_slice_seconds", erp_slice, raising=False)
    drain(client)
    with session_scope() as db:
        recovered = db.scalar(select(ErpJob).where(ErpJob.resource == "orders"))
        assert recovered.status == "succeeded" and recovered.attempts == 2  # assumido de novo, uma vez
        runs = list(db.scalars(select(ErpSyncRun).order_by(ErpSyncRun.id)))
        assert runs[0].status == "interrupted"  # a execução órfã é encerrada
        assert runs[1].cursor_before == "2020-02-01T10:00:00"  # retoma do progresso confirmado
    assert client.calls[0][1].startswith("2020-02-01T09:59")
