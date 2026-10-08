import asyncio

import pytest
from sqlalchemy import select

from app.erp import queue
from app.erp.integrations.mercos_client import ListPage
from app.erp.models import ErpCategory, ErpJob
from app.erp.workers import drain_once, reset_schedule_clock


class OnePage:
    configured = True

    async def list_page(self, alias, cursor):
        rows = [{"id": 1, "nome": "Bebidas", "ultima_alteracao": "2026-10-08 09:00:00"}]
        return ListPage(alias, 1, "2026-10-08T09:00:00", None, rows)


@pytest.fixture(autouse=True)
def _setup(erp_cfg, erp_session_factory):
    erp_cfg(erp_auto_sync=False)
    reset_schedule_clock()


def test_drain_runs_queued_job_and_returns(erp_session_factory):
    with erp_session_factory() as db:
        queue.enqueue(db, kind="sync", connection_id="test", resource="categories", requested_by="op")
        db.commit()
    assert asyncio.run(drain_once(OnePage())) >= 1
    with erp_session_factory() as db:
        assert db.scalar(select(ErpJob)).status == "succeeded"
        assert [c.external_id for c in db.scalars(select(ErpCategory))] == ["1"]
    assert asyncio.run(drain_once(OnePage())) == 0  # fila vazia: volta sem laço


def test_drain_is_noop_when_disabled(erp_cfg, erp_session_factory):
    erp_cfg(erp_queue_in_scheduler=False)
    with erp_session_factory() as db:
        queue.enqueue(db, kind="sync", connection_id="test", resource="categories", requested_by="op")
        db.commit()
    assert asyncio.run(drain_once(OnePage())) == 0
    with erp_session_factory() as db:
        assert db.scalar(select(ErpJob)).status == "queued"
