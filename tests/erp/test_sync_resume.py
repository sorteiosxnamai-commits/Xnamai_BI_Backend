"""Carga de Pedidos: retomada pelo cursor confirmado, checkpoint que não regride e 403 sanitizado."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.erp import workers
from app.erp.common import cursor_not_before, sanitize_remote_message, utcnow
from app.erp.db import session_scope
from app.erp.integrations.mercos_client import AdaptorError, ListPage
from app.erp.models import ErpJob, ErpSyncCheckpoint, ErpSyncRun
from app.erp.sync import engine

C = "test"


class FakeClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls: list[str | None] = []

    async def list_page(self, alias, cursor):
        self.calls.append(cursor)
        item = self.pages.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def order(i, changed):
    return {"id": i, "numero": str(i), "cliente_id": 1, "status": 2, "total": "10",
            "ultima_alteracao": changed, "itens": [{"id": i, "produto_id": 1, "quantidade": 1, "preco_liquido": "10"}]}


def page(rows, page_cursor, next_cursor):
    return ListPage("orders", len(rows), page_cursor, next_cursor, rows)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _cfg(erp_cfg, erp_session_factory):
    erp_cfg()


def runs():
    with session_scope() as db:
        return [(r.mode, r.status, r.cursor_before, r.cursor_after, r.pages) for r in db.scalars(
            select(ErpSyncRun).order_by(ErpSyncRun.id))]


def checkpoint():
    with session_scope() as db:
        cp = db.scalar(select(ErpSyncCheckpoint).where(ErpSyncCheckpoint.resource == "orders"))
        return cp.cursor, cp.status


def test_interrupted_full_load_resumes_from_its_confirmed_cursor_not_from_page_one():
    first = FakeClient([
        page([order(1, "2020-01-01T10:00:00")], "2020-01-01T10:00:00", "2020-01-01T10:00:00"),
        page([order(2, "2020-02-01T10:00:00")], "2020-02-01T10:00:00", "2020-02-01T10:00:00"),
        AdaptorError("transport", "Adaptor inacessível"),
    ])
    result = run(engine.sync_resource(first, C, "orders", mode="full"))
    assert result.status == "interrupted" and result.resumed_from is None
    assert first.calls[0] is None  # primeira carga completa começa do início

    second = FakeClient([page([order(3, "2020-03-01T10:00:00")], "2020-03-01T10:00:00", None)])
    result = run(engine.sync_resource(second, C, "orders", mode="full"))
    assert result.status == "success"
    # retomou do cursor confirmado (menos a sobreposição de segundos), NÃO da primeira página
    assert second.calls[0] is not None and second.calls[0].startswith("2020-02-01T09:59")
    assert result.resumed_from == "2020-02-01T10:00:00"
    assert runs()[-1][2] == "2020-02-01T10:00:00"


def test_orphaned_running_run_is_interrupted_and_next_full_run_resumes():
    c1 = FakeClient([page([order(1, "2020-05-01T10:00:00")], "2020-05-01T10:00:00", "2020-05-01T10:00:00")])

    class Crash(FakeClient):
        async def list_page(self, alias, cursor):
            if self.pages:
                return await super().list_page(alias, cursor)
            raise asyncio.CancelledError  # processo morto: a execução fica "running"

    crashing = Crash(c1.pages)
    with pytest.raises(asyncio.CancelledError):
        run(engine.sync_resource(crashing, C, "orders", mode="full"))
    assert runs()[-1][1] == "running"
    after = FakeClient([page([order(2, "2020-06-01T10:00:00")], "2020-06-01T10:00:00", None)])
    result = run(engine.sync_resource(after, C, "orders", mode="full"))
    assert result.resumed_from == "2020-05-01T10:00:00"
    assert runs()[0][1] == "interrupted"


def test_explicit_full_load_after_a_finished_one_starts_over_without_regressing_the_checkpoint():
    done = FakeClient([page([order(1, "2020-01-01T10:00:00"), order(2, "2020-09-01T10:00:00")], "2020-09-01T10:00:00", None)])
    run(engine.sync_resource(done, C, "orders", mode="full"))
    again = FakeClient([
        page([order(1, "2020-01-01T10:00:00")], "2020-01-01T10:00:00", "2020-01-01T10:00:00"),
        AdaptorError("transport", "x"),
    ])
    result = run(engine.sync_resource(again, C, "orders", mode="full"))
    assert again.calls[0] is None and result.resumed_from is None  # pedido explícito: do início
    # a releitura de páginas antigas não regride o "dados até"
    assert checkpoint()[0] == "2020-09-01T10:00:00"
    # ...e a execução interrompida registra o PRÓPRIO progresso (janeiro), usado para retomar
    assert runs()[-1][3] == "2020-01-01T10:00:00"


def test_checkpoint_never_moves_backwards_on_overlapping_reads():
    first = FakeClient([page([order(1, "2020-09-01T10:00:00")], "2020-09-01T10:00:00", None)])
    run(engine.sync_resource(first, C, "orders", mode="incremental"))
    older = FakeClient([page([order(2, "2020-03-01T10:00:00")], "2020-03-01T10:00:00", None)])
    run(engine.sync_resource(older, C, "orders", mode="incremental"))
    assert checkpoint()[0] == "2020-09-01T10:00:00"
    assert cursor_not_before("2020-09-01T10:00:00", "2020-03-01T10:00:00")
    assert not cursor_not_before("2020-03-01T10:00:00", "2020-09-01T10:00:00")
    assert cursor_not_before("2020-09-01T10:00:00", None) and not cursor_not_before(None, "x")


def test_forbidden_is_recorded_with_resource_status_and_sanitized_message():
    raw = ('Acesso negado (403) em transportadoras: {"error":"Sem permissão Mercos para \'transportadoras\'. '
           'No painel Mercos, libere o recurso.","details":"x-api-key: abcdef0123456789abcdef0123456789abcdef"}')
    clean = sanitize_remote_message(raw)
    assert "abcdef0123456789" not in clean and "{" not in clean and "Sem permissão Mercos" in clean
    assert "x-api-key: abc" not in sanitize_remote_message("falha x-api-key: segredo123 e token=zzz999")
    long = sanitize_remote_message("a" * 500)
    assert len(long) <= 240
    client = FakeClient([AdaptorError("forbidden", "Acesso negado (403) em transportadoras: sem permissão", status_code=403)])
    result = run(engine.sync_resource(client, C, "carriers", mode="full"))
    assert result.status == "forbidden" and "403" in result.error and "carriers" not in {"x"}
    with session_scope() as db:
        cp = db.scalar(select(ErpSyncCheckpoint).where(ErpSyncCheckpoint.resource == "carriers"))
        assert cp.status == "forbidden" and "403" in cp.error


def test_sync_all_does_not_repeat_a_recently_denied_resource_but_the_resource_button_does(monkeypatch):
    with session_scope() as db:
        db.add(ErpSyncCheckpoint(connection_id=C, resource="carriers", scope="", status="forbidden",
                                 last_attempt_at=utcnow(), error="Acesso negado (403) em carriers"))
        db.add(ErpSyncCheckpoint(connection_id=C, resource="segments", scope="", status="forbidden",
                                 last_attempt_at=utcnow() - timedelta(hours=30), error="403"))
    assert engine.recently_denied(C) == {"carriers"}  # segments passou da janela

    seen: list[str] = []

    async def fake_sync(client, connection_id, resource, **kwargs):
        seen.append(resource)
        return engine.SyncResult(resource=resource, status="success")

    monkeypatch.setattr(engine, "sync_resource", fake_sync)

    def job(resource):
        return ErpJob(id=1, kind="sync", connection_id=C, resource=resource, mode="full", lease_token="t")

    out = run(workers.run_sync_job(object(), job("all")))
    assert "carriers" not in seen and "segments" in seen
    skipped = [r for r in out["results"] if r["status"] == "skipped_denied"]
    assert [r["resource"] for r in skipped] == ["carriers"]
    seen.clear()
    run(workers.run_sync_job(object(), job("carriers")))
    assert seen == ["carriers"]  # botão do recurso: tentativa explícita


def test_resume_uses_the_highest_confirmed_cursor_of_the_unfinished_chain():
    """Recomeços indevidos regrediram o último cursor; o maior já confirmado não é perdido."""
    base = utcnow() - timedelta(hours=5)
    with session_scope() as db:
        for status, after in (("interrupted", "2020-09-23T08:37:00"), ("interrupted", "2020-01-25T10:00:00"),
                              ("running", "2020-05-29T19:53:00")):
            db.add(ErpSyncRun(connection_id=C, resource="orders", mode="full", status=status, cursor_after=after,
                              started_at=base))
    client = FakeClient([page([order(1, "2020-09-24T10:00:00")], "2020-09-24T10:00:00", None)])
    result = run(engine.sync_resource(client, C, "orders", mode="full"))
    assert result.resumed_from == "2020-09-23T08:37:00"
    assert client.calls[0].startswith("2020-09-23T08:36")


def test_initial_load_progress_from_incremental_runs_counts_until_the_first_completion():
    with session_scope() as db:
        db.add(ErpSyncRun(connection_id=C, resource="orders", mode="incremental", status="interrupted",
                          cursor_after="2020-07-01T10:00:00"))
        db.add(ErpSyncRun(connection_id=C, resource="orders", mode="full", status="interrupted", cursor_after="2020-01-02T10:00:00"))
    client = FakeClient([page([order(1, "2020-07-02T10:00:00")], "2020-07-02T10:00:00", None)])
    assert run(engine.sync_resource(client, C, "orders", mode="full")).resumed_from == "2020-07-01T10:00:00"


def test_explicit_full_after_cancel_or_completion_starts_from_the_beginning():
    with session_scope() as db:
        db.add(ErpSyncRun(connection_id=C, resource="orders", mode="full", status="interrupted", cursor_after="2020-09-01T10:00:00"))
        db.add(ErpSyncRun(connection_id=C, resource="orders", mode="full", status="cancelled", cursor_after="2020-02-01T10:00:00"))
    client = FakeClient([page([order(1, "2020-01-02T10:00:00")], "2020-01-02T10:00:00", None)])
    assert run(engine.sync_resource(client, C, "orders", mode="full")).resumed_from is None  # cancelada: pedido explícito
    assert client.calls[0] is None
