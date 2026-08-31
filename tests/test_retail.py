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
            Product(
                mercos_id="p-no-sales",
                code="NS1",
                name="Produto Sem Venda",
                list_price=Decimal("30"),
                stock=Decimal("10"),
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


def test_cost_is_list_price():
    assert estimated_cost(1000) == 1000.0
    assert estimated_cost(16.9) == 16.9


def test_evaluate_channels_ignores_list_price_as_retail():
    rows = evaluate_channels(
        market_prices={
            "mercado_livre": {"price": 16.9, "seller": "Loja ML"},
            "shopee": {"price": 16.9, "seller": "Seller SP"},
            "site_proprio": {"price": 1000},  # same as list -> rejected
            "nuvemshop": {"price": 1000},
            "tiktok": {},
        },
        list_price=1000,
        economics={},
    )
    priced = [row for row in rows if row["hasPrice"]]
    assert len(priced) == 2
    assert priced[0]["cost"] == 1000.0
    assert all(row["retailPrice"] != 1000 for row in priced)
    missing = [row for row in rows if not row["hasPrice"]]
    assert len(missing) == 3


def test_channel_margin_with_table_cost():
    margin = channel_margin(
        retail_price=180,
        cost=100,
        fee_pct=3.5,
        freight=20,
        packaging=4,
    )
    assert margin["cost"] == 100
    assert margin["netMargin"] == 49.7


def test_recommended_uses_full_catalog_and_real_prices():
    session = _session()
    _seed_products(session)
    now = datetime.now(timezone.utc)
    session.add(
        RetailProductAnalysis(
            product_mercos_id="p-high-margin",
            ai_payload={
                "apelo": "alto",
                "potencialScore": 90,
                "melhorPlataforma": "shopee",
                "melhorEnvio": "shopee_entrega",
                "motivoEscolha": "Boa margem no anuncio real",
                "razoes": ["Apelo alto"],
                "confidence": "alta",
                "sources": [],
            },
            market_prices={
                "shopee": {"price": 180, "freight": 18, "seller": "Loja Shopee"},
                "mercado_livre": {"price": 175, "freight": 22, "seller": "Loja ML"},
                "site_proprio": {"price": 100},  # equals cost -> ignored
            },
            scores={
                "potencialScore": 90,
                "appealScore": 90,
                "bestPlatform": "shopee",
                "bestShipping": "shopee_entrega",
                "reasonShort": "Boa margem no anuncio real",
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
            assert body["poolSize"] == 4  # full active catalog including no-sales
            assert body["items"][0]["id"] == "p-high-margin"
            assert body["items"][0]["custo"] == 100.0
            assert body["items"][0]["custoEstimado"] == 100.0
            priced = [c for c in body["items"][0]["channels"] if c.get("hasPrice")]
            assert priced
            assert all(c["retailPrice"] != 100 for c in priced)
            economics = client.get("/api/v1/retail/economics")
            assert economics.status_code == 200
            assert economics.json()["costMode"] == "list_price"
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_analyze_batch_starts_async_job():
    session = _session()
    _seed_products(session)

    def override_db():
        yield session

    app.dependency_overrides[db_session] = override_db
    try:
        with patch("app.services.retail_jobs.enqueue_job_worker") as enqueue:
            with TestClient(app) as client:
                response = client.post("/api/v1/retail/analyze-batch", json={"limit": 2})
                assert response.status_code == 202
                body = response.json()
                assert body["job"]["total"] == 2
                assert enqueue.called
    finally:
        app.dependency_overrides.clear()
        session.close()
