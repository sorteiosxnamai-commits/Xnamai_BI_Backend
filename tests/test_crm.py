from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth import AuthUser, authenticate, current_user, decode_token, issue_tokens
from app.config import Settings
from app.database import Base, db_session
from app.main import app
from app.models import Customer, Order, OrderItem
from app import auth as auth_module


def test_admin_email_login_issues_token(monkeypatch):
    config = Settings(
        jwt_secret="test-secret-with-enough-entropy",
        auth_admin_username="admin@xnamai.com",
        auth_admin_password="123456",
        auth_cookie_secure=False,
        bi_api_key="service-key",
    )
    monkeypatch.setattr(auth_module, "settings", lambda: config)
    user = authenticate(auth_module.LoginRequest(username="Admin@Xnamai.com", password="123456"))
    assert user.username == "admin@xnamai.com"
    from fastapi import Response

    result = issue_tokens(user, Response())
    decoded = decode_token(result.accessToken, "access")
    assert decoded.role == "admin"


def test_current_user_requires_token_or_api_key(monkeypatch):
    config = Settings(
        jwt_secret="test-secret-with-enough-entropy",
        auth_admin_username="admin@xnamai.com",
        auth_admin_password="123456",
        bi_api_key="service-key",
    )
    monkeypatch.setattr(auth_module, "settings", lambda: config)
    from fastapi import HTTPException

    try:
        current_user(credentials=None, x_api_key=None)
        raise AssertionError("should require auth")
    except HTTPException as error:
        assert error.status_code == 401

    service = current_user(credentials=None, x_api_key="service-key")
    assert service.username == "service"


def test_crm_prioritizes_high_revenue_inactive_clients():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add_all(
        [
            Customer(mercos_id="c-recent", name="Comprou Ontem", city="SP", state="SP", active=True),
            Customer(mercos_id="c-idle", name="Parado Ha Meses", city="SP", state="SP", active=True),
            Order(
                mercos_id="o-recent",
                number="1",
                customer_mercos_id="c-recent",
                status="2",
                issued_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
                total=Decimal("400000"),
                net_total=Decimal("400000"),
            ),
            Order(
                mercos_id="o-idle",
                number="2",
                customer_mercos_id="c-idle",
                status="2",
                issued_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
                total=Decimal("300000"),
                net_total=Decimal("300000"),
            ),
        ]
    )
    session.commit()

    def override_db():
        yield session

    app.dependency_overrides[db_session] = override_db
    try:
        with TestClient(app) as client:
            listed = client.get("/api/v1/crm/leads?top=1")
            assert listed.status_code == 200
            assert listed.json()["top"][0]["id"] == "c-idle"
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_crm_revenue_keeps_lifetime_totals_above_order_cap():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add_all(
        [
            Customer(mercos_id="c-big", name="Cliente Grande", city="SP", state="SP", active=True),
            Order(
                mercos_id="o-big-1",
                number="900",
                customer_mercos_id="c-big",
                status="2",
                issued_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
                total=Decimal("300000"),
                net_total=Decimal("300000"),
            ),
            Order(
                mercos_id="o-big-2",
                number="901",
                customer_mercos_id="c-big",
                status="2",
                issued_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
                total=Decimal("300000"),
                net_total=Decimal("300000"),
            ),
        ]
    )
    session.commit()

    def override_db():
        yield session

    app.dependency_overrides[db_session] = override_db
    try:
        with TestClient(app) as client:
            listed = client.get("/api/v1/crm/leads?top=1")
            assert listed.status_code == 200
            body = listed.json()
            assert body["top"][0]["id"] == "c-big"
            assert body["top"][0]["revenue"] == 600000.0
            assert body["top"][0]["ticketAverage"] == 300000.0
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_crm_queue_hides_finished_leads_and_exposes_top_20():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add_all(
        [
            Customer(mercos_id="c-top", name="Top Cliente", city="S?o Paulo", state="SP", phone="1199999", active=True),
            Customer(mercos_id="c-queue", name="Fila Cliente", city="Campinas", state="SP", active=True),
            Customer(mercos_id="c-done", name="Finalizado", city="Santos", state="SP", active=True),
            Order(
                mercos_id="o1",
                number="100",
                customer_mercos_id="c-top",
                status="2",
                issued_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
                total=Decimal("5000"),
            ),
            OrderItem(
                order_mercos_id="o1",
                position=0,
                product_mercos_id="p1",
                name="Fone X",
                code="FX",
                quantity=Decimal("3"),
                unit_price=Decimal("100"),
                total=Decimal("300"),
            ),
            Order(
                mercos_id="o2",
                number="101",
                customer_mercos_id="c-queue",
                status="2",
                issued_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
                total=Decimal("80"),
            ),
            Order(
                mercos_id="o3",
                number="102",
                customer_mercos_id="c-done",
                status="2",
                issued_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                total=Decimal("10"),
            ),
        ]
    )
    session.commit()

    def override_db():
        yield session

    app.dependency_overrides[db_session] = override_db
    app.dependency_overrides[current_user] = lambda: AuthUser(username="admin@xnamai.com", role="admin")
    try:
        with TestClient(app) as client:
            listed = client.get("/api/v1/crm/leads?top=1")
            assert listed.status_code == 200
            body = listed.json()
            assert body["count"] == 3
            assert body["top"][0]["id"] == "c-top"
            assert body["queue"][0]["id"] in {"c-queue", "c-done"}
            assert body["top"][0]["lastProducts"][0]["name"] == "Fone X"
            assert body["hasMore"] is False
            assert body["queueTotal"] == 2

            page2 = client.get("/api/v1/crm/leads?top=1&queuePage=2&queuePageSize=1")
            assert page2.status_code == 200
            page2_body = page2.json()
            assert len(page2_body["queue"]) == 1

            session.add(Customer(mercos_id="c-new", name="Lead Novo", city="Curitiba", state="PR", active=True))
            session.commit()
            new_view = client.get("/api/v1/crm/leads?view=new")
            assert new_view.status_code == 200
            new_body = new_view.json()
            assert new_body["view"] == "new"
            assert new_body["count"] == 1
            assert new_body["queue"][0]["id"] == "c-new"
            assert new_body["queue"][0]["orders"] == 0

            never_in_main = client.get("/api/v1/crm/leads?top=20")
            assert never_in_main.status_code == 200
            main_ids = {row["id"] for row in never_in_main.json()["top"]}
            assert "c-new" not in main_ids
            assert all(row["orders"] > 0 for row in never_in_main.json()["top"])

            ai_view = client.get("/api/v1/crm/leads?view=ai&queuePageSize=10")
            assert ai_view.status_code == 200
            ai_body = ai_view.json()
            assert ai_body["view"] == "ai"
            assert ai_body["count"] == 3
            assert ai_body["aiScored"] >= 3
            assert len(ai_body["queue"]) >= 1
            assert ai_body["queue"][0]["aiScore"] is not None
            assert ai_body["queue"][0]["orders"] > 0
            scores = [row["aiScore"] for row in ai_body["queue"] if row["aiScore"] is not None]
            assert scores == sorted(scores, reverse=True)

            ai_page2 = client.get("/api/v1/crm/leads?view=ai&queuePage=2&queuePageSize=1")
            assert ai_page2.status_code == 200
            page2_ids = {row["id"] for row in ai_page2.json()["queue"]}
            page1_ids = {row["id"] for row in ai_body["queue"][:1]}
            assert page1_ids.isdisjoint(page2_ids)

            detail = client.get("/api/v1/crm/leads/c-top")
            assert detail.status_code == 200
            payload = detail.json()
            assert payload["mostBoughtProducts"][0]["name"] == "Fone X"
            assert payload["orders"] == 1
            assert payload["orderHistory"]

            claimed = client.post("/api/v1/crm/leads/c-top/claim", json={"sellerName": "Ana"})
            assert claimed.status_code == 200
            assert claimed.json()["attendanceStatus"] == "in_progress"

            finished = client.post("/api/v1/crm/leads/c-top/finish", json={"sellerName": "Ana", "outcome": "won", "saleValue": 1500, "orderNumber": "999"})
            assert finished.status_code == 200
            after = client.get("/api/v1/crm/leads")
            ids = [row["id"] for row in after.json()["top"] + after.json()["queue"]]
            assert "c-top" not in ids

            dash = client.get("/api/v1/crm/dashboard")
            assert dash.status_code == 200
            assert dash.json()["kpis"]["finishedToday"] == 1
            assert dash.json()["kpis"]["openLeads"] == 3
    finally:
        app.dependency_overrides.clear()
        session.close()
