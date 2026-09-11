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
            Customer(mercos_id="c3", name="Cliente 3", active=True),
            Seller(mercos_id="s1", name="Vendedor", active=True),
            Product(mercos_id="p1", code="P1", name="Produto 1", active=True),
            Product(mercos_id="p2", code="P2", name="Produto 2", active=True),
            Product(mercos_id="p3", code="P3", name="Produto Club", active=True),
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
            Order(
                mercos_id="curr-c3",
                number="112",
                customer_mercos_id="c3",
                seller_mercos_id="s1",
                status="2",
                issued_at=datetime(2026, 9, 7, 12, tzinfo=timezone.utc),
                total=Decimal("80.00"),
                net_total=Decimal("80.00"),
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
            OrderItem(
                order_mercos_id="curr-c3",
                position=0,
                mercos_item_id="d1",
                product_mercos_id="p3",
                code="P3",
                name="Produto Club",
                quantity=Decimal("2"),
                list_unit_price=Decimal("50.00"),
                unit_price=Decimal("40.00"),
                discount=Decimal("20.00"),
                total=Decimal("80.00"),
            ),
        ]
    )
    db.commit()


def test_period_all_without_list_discount_needs_comparison_window() -> None:
    with make_session() as db:
        seed_price_drop(db)
        result = price_savings(db, AnalyticsFilters(period="all"))

        assert result["summary"]["droppedProductCount"] == 0
        assert result["products"] == []
        assert result["comparison"] is None
        assert NO_COMPARISON_WARNING in result["metadata"]["warnings"]
        assert result["summary"]["matchedPairCount"] == 1
        assert result["matchedOrders"][0]["currentNumber"] == "112"
        assert result["matchedOrders"][0]["savings"] == Decimal("20.00")


def test_price_savings_counts_discounted_items_not_identical_baskets() -> None:
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
        assert result["summary"]["matchedPairCount"] == 3
        assert result["summary"]["productSavings"] == Decimal("30.00")
        assert result["summary"]["productSavingsPct"] == 20.0
        assert result["summary"]["matchedSavings"] == Decimal("50.00")
        assert result["summary"]["customersWithSavings"] == 3

        product = result["products"][0]
        assert product["id"] == "p1"
        assert product["previousAverageUnit"] == Decimal("50.00")
        assert product["currentAverageUnit"] == Decimal("40.00")
        assert product["dropPct"] == 20.0
        assert product["savings"] == Decimal("30.00")

        orders = {row["currentNumber"]: row for row in result["matchedOrders"]}
        assert orders["110"]["previousTotal"] == Decimal("140.00")
        assert orders["110"]["currentTotal"] == Decimal("120.00")
        assert orders["110"]["savings"] == Decimal("20.00")
        assert orders["111"]["savings"] == Decimal("10.00")
        assert orders["112"]["previousTotal"] == Decimal("100.00")
        assert orders["112"]["currentTotal"] == Decimal("80.00")
        assert orders["112"]["savings"] == Decimal("20.00")
        assert orders["112"]["savingsPct"] == 20.0

        assert result["customers"][0]["savings"] in {
            Decimal("20.00"),
        }
        customer_ids = {row["id"] for row in result["customers"]}
        assert customer_ids == {"c1", "c2", "c3"}


def test_placeholder_list_price_does_not_inflate_club_savings() -> None:
    with make_session() as db:
        seed_price_drop(db)
        db.add(
            OrderItem(
                order_mercos_id="curr-c2",
                position=1,
                mercos_item_id="fake",
                product_mercos_id="p2",
                code="P2",
                name="Produto 2",
                quantity=Decimal("54"),
                list_unit_price=Decimal("1000.00"),
                unit_price=Decimal("9.60"),
                discount=Decimal("53500.00"),
                total=Decimal("518.40"),
            )
        )
        db.commit()
        result = price_savings(
            db,
            AnalyticsFilters(
                dateFrom=date(2026, 9, 1),
                dateTo=date(2026, 9, 9),
                period="30d",
            ),
        )

        orders = {row["currentNumber"]: row for row in result["matchedOrders"]}
        assert orders["111"]["savings"] == Decimal("10.00")
        assert orders["111"]["currentTotal"] == Decimal("40.00")
        assert result["summary"]["matchedSavings"] == Decimal("50.00")

