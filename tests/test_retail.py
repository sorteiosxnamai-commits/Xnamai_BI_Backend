from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base, db_session
from app.main import app
from app.models import Order, OrderItem, Product, RetailProductAnalysis
from app.services.retail_economics import channel_margin, estimated_cost, evaluate_channels


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed_products(session: Session):
    session.add_all(
        [
            Product(
                mercos_id="p-high-margin",
                code="HM1",
                name="Produto Alta Margem",
                list_price=Decimal("100"),
                stock=Decimal("50"),
                active=True,
            ),
            Product(
                mercos_id="p-high-revenue",
                code="HR1",
                name="Produto Alto Faturamento",
                list_price=Decimal("80"),
                stock=Decimal("20"),
                active=True,
            ),
            Product(
                mercos_id="p-low",
                code="LOW1",
                name="Produto Baixo Giro",
                list_price=Decimal("50"),
                stock=Decimal("5"),
                active=True,
            ),
            Order(
                mercos_id="o1",
                number="1",
                customer_mercos_id="c1",
                status="2",
                issued_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
                total=Decimal("5000"),
                net_total=Decimal("5000"),
            ),
            Order(
                mercos_id="o2",
                number="2",
                customer_mercos_id="c1",
                status="2",
                issued_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
                total=Decimal("20000"),
                net_total=Decimal("20000"),
            ),
            OrderItem(
                order_mercos_id="o1",
                product_mercos_id="p-high-margin",
                name="Produto Alta Margem",
                code="HM1",
                quantity=Decimal("10"),
                total=Decimal("1000"),
                excluded=False,
                position=1,
            ),
            OrderItem(
                order_mercos_id="o2",
                product_mercos_id="p-high-revenue",
                name="Produto Alto Faturamento",
                code="HR1",
                quantity=Decimal("100"),
                total=Decimal("15000"),
                excluded=False,
                position=1,
            ),
            OrderItem(
                order_mercos_id="o1",
                product_mercos_id="p-low",
                name="Produto Baixo Giro",
                code="LOW1",
                quantity=Decimal("1"),
                total=Decimal("50"),
                excluded=False,
                position=2,
            ),
        ]
    )
    session.commit()


def test_estimated_cost_and_channel_margin():
    assert estimated_cost(100, 40) == 60.0
    margin = channel_margin(
        retail_price=100,
        cost=60,
        fee_pct=16,
        freight=22,
        packaging=4,
    )
    assert margin["fee"] == 16.0
    assert margin["netMargin"] == -2.0
    assert margin["marginPct"] == -2.0


def test_evaluate_channels_picks_best_margin():
    rows = evaluate_channels(
        market_prices={
            "mercado_livre": {"price": 120},
            "shopee": {"price": 120},
            "site_proprio": {"price": 120},
        },
        list_price=100,
        economics={"custoPct": 40},
    )
    assert rows[0]["platform"] in {"site_proprio", "nuvemshop", "tiktok"}
    assert rows[0]["marginPct"] >= rows[-1]["marginPct"]


def test_recommended_orders_by_recommendation_not_mercos_revenue():
    session = _session()
    _seed_products(session)
    now = datetime.now(timezone.utc)
    session.add(
        RetailProductAnalysis(
            product_mercos_id="p-high-margin",
            ai_payload={
                "apelo": "alto",
                "potencialScore": 90,
                "melhorPlataforma": "site_proprio",
                "melhorEnvio": "melhor_envio",
                "motivoEscolha": "Alta margem no site proprio",
                "razoes": ["Apelo alto", "Margem forte"],
                "confidence": "alta",
                "sources": [],
            },
            market_prices={
                "site_proprio": {"price": 180, "freight": 15},
                "mercado_livre": {"price": 160, "freight": 22},
                "shopee": {"price": 155, "freight": 18},
            },
            scores={
                "potencialScore": 90,
                "appealScore": 90,
                "bestPlatform": "site_proprio",
                "bestShipping": "melhor_envio",
                "reasonShort": "Alta margem no site proprio",
                "reasonDetail": ["Apelo alto", "Margem forte"],
            },
            generated_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    session.add(
        RetailProductAnalysis(
            product_mercos_id="p-high-revenue",
            ai_payload={
                "apelo": "medio",
                "potencialScore": 55,
                "melhorPlataforma": "mercado_livre",
                "motivoEscolha": "Giro alto mas margem fraca",
                "razoes": ["Giro alto"],
                "confidence": "media",
            },
            market_prices={
                "mercado_livre": {"price": 85, "freight": 22},
                "shopee": {"price": 82, "freight": 18},
                "site_proprio": {"price": 90, "freight": 20},
            },
            scores={
                "potencialScore": 55,
                "appealScore": 55,
                "bestPlatform": "mercado_livre",
                "reasonShort": "Giro alto mas margem fraca",
            },
            generated_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    session.commit()

    def override_db():
        yield session

    app.dependency_overrides[db_session] = override_db
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/retail/recommended?top=10")
            assert response.status_code == 200
            body = response.json()
            assert body["items"][0]["id"] == "p-high-margin"
            assert body["items"][0]["melhorPlataforma"] == "site_proprio"
            assert "motivoCurto" in body["items"][0]
            assert body["analyzedCount"] == 2
            economics = client.get("/api/v1/retail/economics")
            assert economics.status_code == 200
            assert economics.json()["custoPct"] == 40
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_analyze_batch_uses_heuristic_without_openai(monkeypatch):
    session = _session()
    _seed_products(session)

    def override_db():
        yield session

    app.dependency_overrides[db_session] = override_db
    try:
        with patch("app.services.retail_analysis._call_openai") as mocked:
            mocked.side_effect = Exception("should not call when key missing path uses heuristic")
            # Force heuristic via HTTPException path inside analyze_product
            from fastapi import HTTPException

            mocked.side_effect = HTTPException(503, "OPENAI_API_KEY nao configurada")
            with TestClient(app) as client:
                response = client.post("/api/v1/retail/analyze-batch", json={"limit": 2})
                assert response.status_code == 200
                body = response.json()
                assert body["processedCount"] == 2
                assert all(item.get("heuristic") for item in body["processed"])
                recommended = client.get("/api/v1/retail/recommended?top=10")
                assert recommended.status_code == 200
                assert recommended.json()["analyzedCount"] >= 2
    finally:
        app.dependency_overrides.clear()
        session.close()
