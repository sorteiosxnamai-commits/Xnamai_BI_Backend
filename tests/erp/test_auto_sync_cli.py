"""Carga automática, 401 x 403 e comando administrativo."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.erp import cli, queue
from app.erp.common import utcnow
from app.erp.integrations.mercos_client import AdaptorError, ListPage
from app.erp.models import ErpCategory, ErpJob, ErpSyncCheckpoint
from app.erp.registry import SYNC_ORDER
from app.erp.workers import drain_once, reset_schedule_clock, schedule_periodic

C = "test"


class Scripted:
    """Adaptor simulado: por recurso devolve uma página, erro 403 ou erro 401."""

    configured = True

    def __init__(self, forbidden=(), unauthorized=()):
        self.forbidden, self.unauthorized = set(forbidden), set(unauthorized)
        self.calls = []

    async def list_page(self, alias, cursor):
        self.calls.append(alias)
        if alias in self.unauthorized:
            raise AdaptorError("unauthorized", "Autenticação recusada pelo Adaptor (401)", status_code=401)
        if alias in self.forbidden:
            raise AdaptorError("forbidden", "Acesso negado (403)", status_code=403)
        rows = [{"id": 1, "nome": f"{alias}-1", "ultima_alteracao": "2026-10-08 09:00:00"}]
        return ListPage(alias, 1, "2026-10-08T09:00:00", None, rows)


@pytest.fixture(autouse=True)
def _setup(erp_cfg, erp_session_factory):
    erp_cfg()
    reset_schedule_clock()


def test_schedule_is_idempotent_and_ordered(erp_session_factory):
    first = schedule_periodic(C)
    assert first == list(SYNC_ORDER)  # carga inicial: todos, na ordem de dependência
    assert schedule_periodic(C) == []  # já ativos: não duplica
    with erp_session_factory() as db:
        assert db.scalar(select(func.count(ErpJob.id))) == len(SYNC_ORDER)


def test_forbidden_resource_does_not_block_others_nor_repeat(erp_session_factory):
    client = Scripted(forbidden={"segments"})
    schedule_periodic(C)
    for _ in range(len(SYNC_ORDER) + 2):
        asyncio.run(drain_once(client, max_cycles=1))
    with erp_session_factory() as db:
        states = {c.resource: c.status for c in db.scalars(select(ErpSyncCheckpoint))}
        assert states["segments"] == "forbidden"
        assert states["categories"] == "success"  # demais recursos seguiram
    # um recurso negado só é reavaliado muito depois (24x o intervalo), sem loop
    reset_schedule_clock()
    assert "segments" not in schedule_periodic(C)


def test_unauthorized_is_an_auth_failure_not_a_denied_resource(erp_cfg, erp_session_factory):
    erp_cfg(erp_auto_sync=False)
    client = Scripted(unauthorized={"categories"})
    with erp_session_factory() as db:
        queue.enqueue(db, kind="sync", connection_id=C, resource=None, mode="incremental")
        db.commit()
    asyncio.run(drain_once(client, max_cycles=1))
    with erp_session_factory() as db:
        cp = db.scalar(select(ErpSyncCheckpoint).where(ErpSyncCheckpoint.resource == "categories"))
        assert cp.status == "auth_failed"
        job = db.scalar(select(ErpJob).order_by(ErpJob.id))
        assert job.status == "failed" and "401" in (job.error or "")
    assert client.calls == ["categories"]  # credencial vale para todos: não insistiu nos demais


def test_auto_sync_does_not_restart_full_load_after_restart(erp_session_factory):
    client = Scripted()
    for _ in range(len(SYNC_ORDER) + 2):
        asyncio.run(drain_once(client, max_cycles=1))
    first_calls = len(client.calls)
    assert first_calls >= len(SYNC_ORDER)
    reset_schedule_clock()  # "reinício": relógio zerado, checkpoints preservados
    with erp_session_factory() as db:
        assert db.scalar(select(func.count(ErpCategory.id))) == 1
    assert schedule_periodic(C) == []  # recente: nada vencido, nenhuma nova carga
    # passou o intervalo: só incremental pelo checkpoint
    with erp_session_factory() as db:
        for cp in db.scalars(select(ErpSyncCheckpoint)):
            cp.last_attempt_at = utcnow() - timedelta(hours=1)
        db.commit()
    reset_schedule_clock()
    assert schedule_periodic(C) == list(SYNC_ORDER)


def test_cli_status_and_run_use_the_same_services(erp_session_factory, monkeypatch, capsys):
    monkeypatch.setattr(cli, "MercosAdaptorClient", lambda: Scripted())
    assert cli.main(["status"]) == 0
    assert "never" in capsys.readouterr().out
    code = asyncio.run(cli.run(["categories"], timeout=60))
    out = capsys.readouterr().out
    assert code == 2 or code == 0  # demais recursos ainda não sincronizados => pendentes
    assert "categories" in out and "success" in out
    snap = cli.collect_status(C)
    row = next(r for r in snap["resources"] if r["resource"] == "categories")
    assert row["state"] == "success" and row["local"] == 1 and row["persisted"] == 1


def test_build_endpoint_reports_version_flags_without_secrets(erp_cfg, monkeypatch):
    from tests.erp.helpers import client

    monkeypatch.setenv("RENDER_GIT_COMMIT", "8132258abcdef")
    body = client().get("/api/v1/erp/build").json()
    assert body["commit"] == "8132258" and body["queueInScheduler"] is True
    assert body["writes"] == {
        "customers": False, "orders": False, "titles": False,
        "products": False, "inventoryPublish": False, "billing": False,
    }
    assert "secret" not in str(body).lower() and "key" not in str(body).lower()
    erp_cfg(erp_enabled=False)
    assert client().get("/api/v1/erp/build").status_code == 404
