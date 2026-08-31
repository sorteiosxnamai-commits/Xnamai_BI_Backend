from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base, db_session
from app.main import app
from app.models import Product, RetailAnalysisJob
from app.services import retail_jobs


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed(session: Session):
    session.add_all(
        [
            Product(
                mercos_id="p1",
                code="A1",
                name="Produto 1",
                list_price=Decimal("50"),
                stock=Decimal("10"),
                active=True,
            ),
            Product(
                mercos_id="p2",
                code="A2",
                name="Produto 2",
                list_price=Decimal("60"),
                stock=Decimal("10"),
                active=True,
            ),
            Product(
                mercos_id="p3",
                code="A3",
                name="Produto 3",
                list_price=Decimal("70"),
                stock=Decimal("10"),
                active=True,
            ),
        ]
    )
    session.commit()


def test_job_commits_per_product_and_resumes_after_interrupt():
    session = _session()
    _seed(session)

    calls = {"n": 0}

    def fake_analyze(db, product_id, **kwargs):
        calls["n"] += 1
        if product_id == "p2" and calls["n"] <= 2:
            raise RuntimeError("boom mid-job")
        return {"id": product_id, "name": product_id, "recomendacaoScore": 10}

    def override_db():
        yield session

    app.dependency_overrides[db_session] = override_db
    try:
        from contextlib import contextmanager

        @contextmanager
        def session_factory():
            yield session

        with patch("app.services.retail_jobs.analyze_product", side_effect=fake_analyze):
            with patch("app.services.retail_jobs.SessionLocal", session_factory):
                job, created = retail_jobs.start_job(session, mode="batch", batch_size=10, resume=False)
                assert created
                retail_jobs.run_job_worker(job.id)
                session.refresh(job)
                assert job.status == "completed"
                assert job.processed >= 1
                assert job.failed >= 1
                assert job.cursor == 3

                # Simulate interrupted mid-way job for resume
                job2, _ = retail_jobs.start_job(session, mode="batch", batch_size=10, resume=False)
                job2.status = "running"
                job2.cursor = 1
                job2.processed = 1
                job2.heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=10)
                session.add(job2)
                session.commit()

                reclaimed = retail_jobs.reclaim_stale_jobs(session)
                assert reclaimed >= 1
                session.refresh(job2)
                assert job2.status == "interrupted"

                resumed, created2 = retail_jobs.start_job(session, mode="batch", batch_size=10, resume=True)
                assert not created2
                assert resumed.id == job2.id
                retail_jobs.run_job_worker(resumed.id)
                session.refresh(resumed)
                assert resumed.status == "completed"
                assert resumed.cursor == len(resumed.product_ids)
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_analyze_jobs_endpoint_returns_202_and_status():
    session = _session()
    _seed(session)

    def fake_analyze(db, product_id, **kwargs):
        return {"id": product_id, "recomendacaoScore": 1}

    def override_db():
        yield session

    app.dependency_overrides[db_session] = override_db
    try:
        with patch("app.services.retail_jobs.analyze_product", side_effect=fake_analyze):
            with patch("app.services.retail_jobs.enqueue_job_worker") as enqueue:
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/retail/analyze-jobs",
                        json={"mode": "batch", "batchSize": 2, "resume": False},
                    )
                    assert response.status_code == 202
                    body = response.json()
                    assert body["job"]["status"] in {"queued", "running"}
                    assert body["job"]["total"] == 2
                    assert enqueue.called

                    status = client.get("/api/v1/retail/analyze-jobs/active")
                    assert status.status_code == 200
                    assert status.json()["job"]["id"] == body["job"]["id"]
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_reclaim_marks_stale_running_job():
    session = _session()
    now = datetime.now(timezone.utc)
    job = RetailAnalysisJob(
        status="running",
        mode="batch",
        batch_size=10,
        product_ids=["p1", "p2"],
        cursor=0,
        processed=0,
        failed=0,
        skipped=0,
        errors=[],
        heartbeat_at=now - timedelta(minutes=10),
        created_at=now - timedelta(minutes=11),
        updated_at=now - timedelta(minutes=10),
    )
    session.add(job)
    session.commit()
    assert retail_jobs.reclaim_stale_jobs(session) == 1
    session.refresh(job)
    assert job.status == "interrupted"
    session.close()
