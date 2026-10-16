from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import B2bCartObservation, Order
from app.schemas.analytics import AnalyticsFilters
from app.services.b2b_carts import b2b_status, cart_report, observe_cart
from app.sync import _upsert_rows

NOW = datetime(2026, 10, 16, 15, tzinfo=timezone.utc)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def add(db, mid, b2b, status="1", value=100, age=48, created=None):
    obj = Order(
        mercos_id=mid,
        number=mid,
        status=status,
        b2b_status=b2b_status(b2b),
        total=Decimal(value),
        source_created_at=created or NOW - timedelta(days=3),
        issued_at=NOW - timedelta(days=400),
        source_updated_at=NOW - timedelta(hours=age),
        raw={"status_b2b": b2b},
    )
    db.add(obj)
    db.flush()
    return obj


def test_partition_conversion_values_and_exclusions(db):
    add(db, "abandoned", 1, value=300)
    add(db, "recent", "1", age=1)
    add(db, "converted", 2, status="2", value=250)
    add(db, "quote", 2, status="1", value=50)
    add(db, "cancelled", 1, status="0", value=75)
    add(db, "regular", None, status="2", value=999)
    add(db, "inconsistent", 1, status="2", value=20)
    report = cart_report(db, AnalyticsFilters(period="30d"), now=NOW)
    s = report["summary"]
    assert s["identified"] == {"count": 6, "value": 795.0}
    assert s["openTotal"]["count"] == 2
    assert s["possible_abandonment"] == {"count": 1, "value": 300.0}
    assert (
        s["converted"]["count"]
        == s["completed_quote"]["count"]
        == s["other"]["count"]
        == 1
    )
    assert s["abandonedMinusConvertedValue"] == 50
    assert s["conversionRate"] == 16.67
    assert report["metadata"]["isPartial"] is True


def test_creation_cohort_pagination_and_stage(db):
    add(db, "old", 1, created=NOW - timedelta(days=60))
    add(db, "a", 1)
    add(db, "b", 2, status="2")
    report = cart_report(
        db, AnalyticsFilters(period="30d"), now=NOW, stage="converted", page_size=1
    )
    assert report["summary"]["identified"]["count"] == 2
    assert report["totalItems"] == 1
    assert report["items"][0]["id"] == "b"


def test_no_timestamp_is_not_abandonment_and_excluded_from_dated_cohort(db):
    obj = add(db, "a", 1)
    obj.source_updated_at = None
    assert (
        cart_report(db, AnalyticsFilters(period="all"), now=NOW)["summary"]["open"][
            "count"
        ]
        == 1
    )
    obj.source_created_at = None
    report = cart_report(db, AnalyticsFilters(period="30d"), now=NOW)
    assert report["summary"]["identified"]["count"] == 0
    assert report["summary"]["conversionRate"] is None
    assert report["metadata"]["quality"]["b2bWithoutCreationDate"] == 1


def test_customer_exclusion_and_abandonment_boundary(db):
    obj = add(db, "a", 1, age=24)
    obj.customer_mercos_id = "customer"
    assert (
        cart_report(db, AnalyticsFilters(period="all"), now=NOW)["summary"][
            "possible_abandonment"
        ]["count"]
        == 1
    )
    assert (
        cart_report(db, AnalyticsFilters(period="all"), now=NOW, abandonment_hours=25)[
            "summary"
        ]["open"]["count"]
        == 1
    )
    assert (
        cart_report(
            db,
            AnalyticsFilters(period="all", excludedCustomerIds=["customer"]),
            now=NOW,
        )["summary"]["identified"]["count"]
        == 0
    )


def test_sync_observes_versions_idempotently_and_preserves_original_value(db):
    payload = {
        "id": 123,
        "status": 1,
        "status_b2b": 1,
        "total": 100,
        "data_criacao": "2026-10-10T12:00:00Z",
        "ultima_alteracao": "2026-10-10T12:00:00Z",
    }
    _upsert_rows(db, "orders", [payload])
    _upsert_rows(db, "orders", [payload])
    _upsert_rows(
        db,
        "orders",
        [
            {
                **payload,
                "status": 2,
                "status_b2b": 2,
                "total": 90,
                "ultima_alteracao": "2026-10-11T12:00:00Z",
            }
        ],
    )
    db.flush()
    observations = list(
        db.scalars(select(B2bCartObservation).order_by(B2bCartObservation.id))
    )
    assert len(observations) == 2
    assert [o.total for o in observations] == [100, 90]
    assert [o.b2b_status for o in observations] == [1, 2]
    assert all(o.observed_at for o in observations)


def test_backfill_is_idempotent_and_not_backdated(db):
    order = add(db, "a", 2, status="2")
    assert observe_cart(db, order, observed_at=NOW)
    db.flush()
    assert not observe_cart(db, order)
    db.commit()
    db.expire_all()
    assert not observe_cart(db, order)
    observation = db.scalar(select(B2bCartObservation))
    assert observation.observed_at.replace(tzinfo=timezone.utc) == NOW
    assert observation.b2b_status == 2
    assert db.scalar(select(func.count(B2bCartObservation.id))) == 1


def test_report_never_reads_raw_order_payloads(db):
    from sqlalchemy import event

    add(db, "a", 1)
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.bind, "before_cursor_execute", capture)
    try:
        report = cart_report(db, AnalyticsFilters(period="all"), now=NOW)
    finally:
        event.remove(db.bind, "before_cursor_execute", capture)
    assert report["summary"]["identified"]["count"] == 1
    assert not any("raw" in sql.lower() for sql in statements)
    assert len(statements) <= 4


def test_status_migration_backfills_existing_raw_and_creates_index():
    import json
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    # Load the repository migration without treating Alembic's installed package as our source tree.
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "b2b_status_migration",
        Path(__file__).resolve().parents[1]
        / "alembic/versions/20261016_02_index_b2b_status.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, raw JSON, source_created_at DATETIME)"
        )
        payloads = [
            {"status_b2b": 1},
            {"status_b2b": "2"},
            {"status_b2b": None},
            {},
            {"status_b2b": 3},
        ]
        for idx, payload in enumerate(payloads):
            connection.execute(
                text("INSERT INTO orders(id,raw) VALUES (:id,:raw)"),
                {"id": idx, "raw": json.dumps(payload)},
            )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert list(
            connection.execute(
                text("SELECT b2b_status FROM orders ORDER BY id")
            ).scalars()
        ) == [1, 2, None, None, None]
        assert "ix_orders_b2b_created" in {
            index["name"] for index in inspect(connection).get_indexes("orders")
        }
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert "b2b_status" not in {
            column["name"] for column in inspect(connection).get_columns("orders")
        }
    engine.dispose()


@pytest.mark.parametrize("value", [True, False, 3, "unknown", None, 1.5])
def test_invalid_b2b_status(value):
    assert b2b_status(value) is None


def test_api_contract_and_query_validation(db):
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient
    from app.auth import current_user
    from app.database import db_session
    from app.routers.analytics import router

    # A per-request session avoids sharing SQLite connections between threads.
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(router, dependencies=[Depends(current_user)])

    def session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[db_session] = session_override
    with TestClient(app) as client:
        assert client.get("/api/v1/analytics/b2b-carts").status_code == 401
        app.dependency_overrides[current_user] = lambda: {"role": "viewer"}
        response = client.get("/api/v1/analytics/b2b-carts?period=all")
        assert response.status_code == 200
        assert response.json()["summary"]["conversionRate"] is None
        for query in ("abandonmentHours=0", "page=0", "page_size=101", "stage=bad"):
            assert client.get("/api/v1/analytics/b2b-carts?" + query).status_code == 422
    engine.dispose()
