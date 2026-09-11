from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Customer, Order, OrderItem, Product, Seller
from app.schemas.analytics import AnalyticsFilters
from app.services.price_savings import NO_COMPARISON_WARNING, price_savings


def make_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def seed_price_drop(db: Session) -> None:
    db.add_all(
        [
            Customer(mercos_id="c1", name="Cliente 1", active=True),
            Customer(mercos_id="c2", name="Cliente 2", active=True),
            Seller(mercos_id="s1", name="Vendedor", active=True),
            Product(mercos_id="p1", code="P1", name="Produto 1", active=True),
            Product(mercos_id="p2", code="P2", name="Produto 2", active=True),
            Order(
                mercos_id="prev-c1",
                number="80",
                customer_mercos_id="c1",
                seller_mercos_id="s1",
                status="2",
                issued_at=datetime(2026, 8, 5, 12, tzinfo=timezone.utc),
                total=Decimal("140.00"),
                net_total=Decimal("140.00"),
            ),
            Order(
                mercos_id="curr-c1",
                number="110",
                customer_mercos_id="c1",
                seller_mercos_id="s1",
                status="2",
                issued_at=datetime(2026, 9, 5, 12, tzinfo=timezone.utc),
                total=Decimal("120.00"),
                net_total=Decimal("120.00"),
            ),
            Order(
                mercos_id="curr-c2",
                number="111",
                customer_mercos_id="c2",
                seller_mercos_id="s1",
                status="2",
                issued_at=datetime(2026, 9, 6, 12, tzinfo=timezone.utc),
                total=Decimal("40.00"),
                net_total=Decimal("40.00"),
            ),
            OrderItem(
                order_mercos_id="prev-c1",
                position=0,
                mercos_item_id="a1",
                product_mercos_id="p1",
                code="P1",
                name="Produto 1",
                quantity=Decimal("2"),
                unit_price=Decimal("50.00"),
                total=Decimal("100.00"),
            ),
            OrderItem(
                order_mercos_id="prev-c1",
                position=1,
                mercos_item_id="a2",
                product_mercos_id="p2",
                code="P2",
                name="Produto 2",
                quantity=Decimal("1"),
                unit_price=Decimal("40.00"),
                total=Decimal("40.00"),
            ),
            OrderItem(
                order_mercos_id="curr-c1",
                position=0,
                mercos_item_id="b1",
                product_mercos_id="p1",
                code="P1",
                name="Produto 1",
                quantity=Decimal("2"),
                unit_price=Decimal("40.00"),
                total=Decimal("80.00"),
            ),
            OrderItem(
                order_mercos_id="curr-c1",
                position=1,
                mercos_item_id="b2",
                product_mercos_id="p2",
                code="P2",
                name="Produto 2",
                quantity=Decimal("1"),
                unit_price=Decimal("40.00"),
                total=Decimal("40.00"),
            ),
            OrderItem(
                order_mercos_id="curr-c2",
                position=0,
                mercos_item_id="c1i",
                product_mercos_id="p1",
                code="P1",
                name="Produto 1",
                quantity=Decimal("1"),
                unit_price=Decimal("40.00"),
                total=Decimal("40.00"),
            ),
        ]
    )
    db.commit()


def test_period_all_asks_for_comparison_window() -> None:
    with make_session() as db:
        seed_price_drop(db)
        result = price_savings(db, AnalyticsFilters(period="all"))

        assert result["summary"]["matchedPairCount"] == 0
        assert result["products"] == []
        assert result["comparison"] is None
        assert NO_COMPARISON_WARNING in result["metadata"]["warnings"]


def test_price_savings_highlights_dropped_products_and_same_baskets() -> None:
    with make_session() as db:
        seed_price_drop(db)
        result = price_savings(
            db,
            AnalyticsFilters(
                dateFrom=date(2026, 9, 1),
                dateTo=date(2026, 9, 9),
                period="30d",
            ),
        )

        assert result["comparison"]["previousFrom"] == "2026-08-01"
        assert result["comparison"]["previousTo"] == "2026-08-09"
        assert result["summary"]["droppedProductCount"] == 1
        assert result["summary"]["matchedPairCount"] == 1
        assert result["summary"]["productSavings"] == Decimal("30.00")
        assert result["summary"]["productSavingsPct"] == 20.0
        assert result["summary"]["matchedSavings"] == Decimal("20.00")
        assert result["summary"]["matchedSavingsPct"] == 14.29
        assert result["summary"]["customersWithSavings"] == 2

        product = result["products"][0]
        assert product["id"] == "p1"
        assert product["previousAverageUnit"] == Decimal("50.00")
        assert product["currentAverageUnit"] == Decimal("40.00")
        assert product["dropPct"] == 20.0
        assert product["savings"] == Decimal("30.00")
        assert [row["id"] for row in result["products"]] == ["p1"]

        pair = result["matchedOrders"][0]
        assert pair["customerId"] == "c1"
        assert pair["currentNumber"] == "110"
        assert pair["previousNumber"] == "80"
        assert pair["savings"] == Decimal("20.00")
        assert pair["savingsPct"] == 14.29

        customer = result["customers"][0]
        assert customer["id"] == "c1"
        assert customer["savings"] == Decimal("20.00")
