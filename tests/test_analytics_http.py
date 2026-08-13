from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth import AuthUser, current_user
from app.database import Base, db_session
from app.main import app
from app.models import Customer, Order, OrderItem, Product, Seller


@pytest.fixture
def http_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add_all(
        [
            Customer(mercos_id="c1", name="Cliente", active=True),
            Seller(mercos_id="s1", name="Vendedor", active=True),
            Product(
                mercos_id="p1",
                name="Produto",
                list_price=Decimal("100"),
                stock=Decimal("5"),
                active=True,
            ),
            Order(
                mercos_id="o1",
                number="1",
                customer_mercos_id="c1",
                seller_mercos_id="s1",
                status="2",
                issued_at=datetime(2026, 8, 12, 12, tzinfo=timezone.utc),
                total=Decimal("100"),
                net_total=Decimal("100"),
            ),
            OrderItem(
                order_mercos_id="o1",
                mercos_item_id="i1",
                position=0,
                product_mercos_id="p1",
                name="Produto",
                quantity=Decimal("1"),
                total=Decimal("100"),
            ),
        ]
    )
    session.commit()

    def override_db():
        yield session

    app.dependency_overrides[db_session] = override_db
    app.dependency_overrides[current_user] = lambda: AuthUser(
        username="admin",
        role="admin",
    )
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
    session.close()


def test_analytics_http_contracts_cover_pages_charts_and_drilldowns(http_client):
    query = "?period=all&granularity=month"
    expected_shapes = {
        "/api/v1/analytics/overview": {"kpis", "metadata", "appliedFilters"},
        "/api/v1/analytics/timeseries": {
            "items",
            "previousItems",
            "metadata",
        },
        "/api/v1/analytics/breakdowns": {
            "statuses",
            "orderValueBands",
            "productAbc",
            "customerAbc",
        },
        "/api/v1/analytics/rankings": {
            "products",
            "customers",
            "sellers",
        },
        "/api/v1/analytics/orders": {
            "items",
            "page",
            "pageSize",
            "totalItems",
        },
        "/api/v1/analytics/products": {"items", "page", "metadata"},
        "/api/v1/analytics/customers": {"items", "page", "metadata"},
        "/api/v1/analytics/sellers": {"items", "page", "metadata"},
        "/api/v1/analytics/inventory": {"items", "summary", "metadata"},
        "/api/v1/analytics/geography": {"states", "cities", "metadata"},
        "/api/v1/analytics/cohorts": {"cohorts", "metadata"},
        "/api/v1/analytics/associations": {"items", "metadata"},
    }
    for path, expected in expected_shapes.items():
        response = http_client.get(path + query)
        assert response.status_code == 200, (path, response.text)
        assert expected <= set(response.json()), path

    for path, entity_key in (
        ("/api/v1/analytics/orders/o1", "order"),
        ("/api/v1/analytics/products/p1", "product"),
        ("/api/v1/analytics/customers/c1", "customer"),
        ("/api/v1/analytics/sellers/s1", "seller"),
    ):
        response = http_client.get(path + query)
        assert response.status_code == 200, response.text
        assert entity_key in response.json()


def test_analytics_rejects_missing_authentication():
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        response = client.get("/api/v1/analytics/overview?period=all")
    assert response.status_code == 401
