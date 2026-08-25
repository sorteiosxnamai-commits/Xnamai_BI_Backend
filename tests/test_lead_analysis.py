import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth import AuthUser, current_user
from app.config import Settings
from app.database import Base, db_session
from app.main import app
from app.models import Customer, Order
from app import config as config_module


@pytest.fixture
def crm_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add_all(
        [
            Customer(
                mercos_id="c1",
                name="Loja Exemplo LTDA",
                city="Sao Paulo",
                state="SP",
                phone="11987654321",
                document="12345678000199",
                active=True,
            ),
            Order(
                mercos_id="o1",
                number="100",
                customer_mercos_id="c1",
                status="2",
                issued_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
                total=Decimal("1200"),
            ),
        ]
    )
    session.commit()
    yield session
    session.close()


def test_lead_analysis_uses_openai_and_caches(crm_session, monkeypatch):
    cfg = Settings(
        jwt_secret="test-secret-with-enough-entropy",
        openai_api_key="test-key",
        openai_model="gpt-4o-mini",
    )
    monkeypatch.setattr(config_module, "settings", lambda: cfg)

    analysis = {
        "companyProfile": "Varejo de eletronicos",
        "sector": "Eletronicos",
        "website": "https://exemplo.com.br",
        "publicProducts": ["Fones", "Cabos"],
        "purchasePreferences": ["Itens de audio"],
        "approachStrategy": "Oferecer lancamentos com margem",
        "openingMessage": "Ola! Vi que compram fones conosco...",
        "talkingPoints": ["Reposicao", "Margem"],
        "risksOrCautions": ["Concorrencia"],
        "sources": [],
        "confidence": "media",
    }

    def override_db():
        yield crm_session

    app.dependency_overrides[db_session] = override_db
    app.dependency_overrides[current_user] = lambda: AuthUser(username="admin@xnamai.com", role="admin")
    try:
        with patch("app.services.lead_analysis._call_openai", return_value=analysis):
            with TestClient(app) as client:
                first = client.get("/api/v1/crm/leads/c1/analysis")
                assert first.status_code == 200
                body = first.json()
                assert body["cached"] is False
                assert body["contact"]["whatsappUrl"] == "https://wa.me/5511987654321"
                assert body["analysis"]["sector"] == "Eletronicos"
                assert "Reposicao" in body["analysis"]["talkingPoints"][0]

                second = client.get("/api/v1/crm/leads/c1/analysis")
                assert second.status_code == 200
                assert second.json()["cached"] is True
    finally:
        app.dependency_overrides.clear()


def test_lead_analysis_requires_api_key(crm_session, monkeypatch):
    cfg = Settings(jwt_secret="test-secret-with-enough-entropy", openai_api_key="")
    monkeypatch.setattr(config_module, "settings", lambda: cfg)

    def override_db():
        yield crm_session

    app.dependency_overrides[db_session] = override_db
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/crm/leads/c1/analysis")
            assert response.status_code == 503
    finally:
        app.dependency_overrides.clear()
