from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Customer, Order, OrderItem, Product, Seller
from app.schemas.analytics import AnalyticsFilters
from app.services.price_savings import price_savings


NOW = datetime(2026, 9, 11, 18, tzinfo=timezone.utc)


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
                issued_at=datetime(2026, 7, 10, 12, tzinfo=timezone.utc),
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


def test_global_filters_do_not_change_club_discount() -> None:
    with make_session() as db:
        seed_price_drop(db)
        ignored = AnalyticsFilters(
            dateFrom=date(2026, 9, 1),
            dateTo=date(2026, 9, 2),
            period="7d",
            sellerIds=["missing-seller"],
            customerIds=["c3"],
            productIds=["p3"],
        )
        result = price_savings(db, ignored, now=NOW)

        assert result["appliedFilters"]["scope"] == "club-analysis"
        assert result["appliedFilters"]["currentDays"] == 45
        assert result["comparison"]["previousFrom"] == "2026-06-13"
        assert result["comparison"]["previousTo"] == "2026-07-28"
        assert result["comparison"]["currentFrom"] == "2026-07-28"
        assert result["comparison"]["currentTo"] == "2026-09-11"
        assert result["summary"]["droppedProductCount"] == 1
        assert result["summary"]["matchedSavings"] == Decimal("30.00")
        assert result["summary"]["matchedSavingsPct"] == 20.0
        assert "112" not in {row["currentNumber"] for row in result["matchedOrders"]}


def test_price_savings_counts_discounted_items_not_identical_baskets() -> None:
    with make_session() as db:
        seed_price_drop(db)
        result = price_savings(db, now=NOW)

        assert result["comparison"]["previousFrom"] == "2026-06-13"
        assert result["comparison"]["previousTo"] == "2026-07-28"
        assert result["summary"]["droppedProductCount"] == 1
        assert result["summary"]["matchedPairCount"] == 2
        assert result["summary"]["productSavings"] == Decimal("30.00")
        assert result["summary"]["productSavingsPct"] == 20.0
        assert result["summary"]["matchedSavings"] == Decimal("30.00")
        assert result["summary"]["matchedSavingsPct"] == 20.0
        assert result["summary"]["previousDroppedTotal"] == Decimal("150.00")
        assert result["summary"]["currentDroppedTotal"] == Decimal("120.00")
        assert result["summary"]["customersWithSavings"] == 2
        assert result["summary"]["simpleAvgDropPct"] == 20.0
        assert result["summary"]["qtyWeightedDropPct"] == 20.0
        assert result["summary"]["valueWeightedDropPct"] == 20.0
        assert result["summary"]["medianDropPct"] == 20.0
        assert result["summary"]["customerAvgDropPct"] == 20.0

        product = result["products"][0]
        assert product["id"] == "p1"
        assert product["previousAverageUnit"] == Decimal("50.00")
        assert product["currentAverageUnit"] == Decimal("40.00")
        assert product["dropPct"] == 20.0
        assert product["savings"] == Decimal("30.00")

        orders = {row["currentNumber"]: row for row in result["matchedOrders"]}
        assert "112" not in orders
        assert orders["110"]["previousTotal"] == Decimal("100.00")
        assert orders["110"]["currentTotal"] == Decimal("80.00")
        assert orders["110"]["savings"] == Decimal("20.00")
        assert orders["110"]["savingsPct"] == 20.0
        assert orders["111"]["savings"] == Decimal("10.00")
        assert orders["111"]["savingsPct"] == 20.0

        customer_ids = {row["id"] for row in result["customers"]}
        assert customer_ids == {"c1", "c2"}


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
        result = price_savings(db, now=NOW)

        orders = {row["currentNumber"]: row for row in result["matchedOrders"]}
        assert orders["111"]["savings"] == Decimal("10.00")
        assert orders["111"]["currentTotal"] == Decimal("40.00")
        assert result["summary"]["matchedSavings"] == Decimal("30.00")


def test_lookback_includes_prices_from_club_baseline_not_older_history() -> None:
    with make_session() as db:
        seed_price_drop(db)
        db.add_all(
            [
                Product(mercos_id="p4", code="P4", name="Produto 60d", active=True),
                Product(mercos_id="p5", code="P5", name="Produto 70d", active=True),
                Order(
                    mercos_id="prev-60d",
                    number="70",
                    customer_mercos_id="c1",
                    seller_mercos_id="s1",
                    status="2",
                    issued_at=datetime(2026, 7, 10, 12, tzinfo=timezone.utc),
                    total=Decimal("50.00"),
                    net_total=Decimal("50.00"),
                ),
                Order(
                    mercos_id="prev-70d",
                    number="60",
                    customer_mercos_id="c1",
                    seller_mercos_id="s1",
                    status="2",
                    issued_at=datetime(2026, 4, 20, 12, tzinfo=timezone.utc),
                    total=Decimal("50.00"),
                    net_total=Decimal("50.00"),
                ),
                Order(
                    mercos_id="curr-p4",
                    number="120",
                    customer_mercos_id="c1",
                    seller_mercos_id="s1",
                    status="2",
                    issued_at=datetime(2026, 9, 4, 12, tzinfo=timezone.utc),
                    total=Decimal("80.00"),
                    net_total=Decimal("80.00"),
                ),
                OrderItem(
                    order_mercos_id="prev-60d",
                    position=0,
                    mercos_item_id="e1",
                    product_mercos_id="p4",
                    code="P4",
                    name="Produto 60d",
                    quantity=Decimal("1"),
                    unit_price=Decimal("50.00"),
                    total=Decimal("50.00"),
                ),
                OrderItem(
                    order_mercos_id="prev-70d",
                    position=0,
                    mercos_item_id="e2",
                    product_mercos_id="p5",
                    code="P5",
                    name="Produto 70d",
                    quantity=Decimal("1"),
                    unit_price=Decimal("50.00"),
                    total=Decimal("50.00"),
                ),
                OrderItem(
                    order_mercos_id="curr-p4",
                    position=0,
                    mercos_item_id="e3",
                    product_mercos_id="p4",
                    code="P4",
                    name="Produto 60d",
                    quantity=Decimal("1"),
                    unit_price=Decimal("40.00"),
                    total=Decimal("40.00"),
                ),
                OrderItem(
                    order_mercos_id="curr-p4",
                    position=1,
                    mercos_item_id="e4",
                    product_mercos_id="p5",
                    code="P5",
                    name="Produto 70d",
                    quantity=Decimal("1"),
                    unit_price=Decimal("40.00"),
                    total=Decimal("40.00"),
                ),
            ]
        )
        db.commit()
        result = price_savings(db, now=NOW)

        product_ids = {row["id"] for row in result["products"]}
        assert "p4" in product_ids
        assert "p5" not in product_ids
        assert result["summary"]["matchedSavingsPct"] == result["summary"]["productSavingsPct"]
        p4 = next(row for row in result["products"] if row["id"] == "p4")
        assert p4["previousAverageUnit"] == Decimal("50.00")
        assert p4["currentAverageUnit"] == Decimal("40.00")
        assert p4["savings"] == Decimal("10.00")
        assert p4["dropPct"] == 20.0
        orders = {row["currentNumber"]: row for row in result["matchedOrders"]}
        assert orders["120"]["previousTotal"] == Decimal("50.00")
        assert orders["120"]["currentTotal"] == Decimal("40.00")
        assert orders["120"]["savingsPct"] == 20.0


def test_pre_club_sales_do_not_dilute_current_price() -> None:
    with make_session() as db:
        seed_price_drop(db)
        db.add_all(
            [
                Order(
                    mercos_id="pre-club",
                    number="90",
                    customer_mercos_id="c1",
                    seller_mercos_id="s1",
                    status="2",
                    issued_at=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
                    total=Decimal("100.00"),
                    net_total=Decimal("100.00"),
                ),
                OrderItem(
                    order_mercos_id="pre-club",
                    position=0,
                    mercos_item_id="pre1",
                    product_mercos_id="p1",
                    code="P1",
                    name="Produto 1",
                    quantity=Decimal("2"),
                    unit_price=Decimal("50.00"),
                    total=Decimal("100.00"),
                ),
            ]
        )
        db.commit()
        result = price_savings(db, now=NOW)

        p1 = next(row for row in result["products"] if row["id"] == "p1")
        assert p1["currentAverageUnit"] == Decimal("40.00")
        assert p1["previousAverageUnit"] == Decimal("50.00")
        assert p1["dropPct"] == 20.0
        assert "90" not in {row["currentNumber"] for row in result["matchedOrders"]}


def test_average_drop_separates_simple_qty_and_value() -> None:
    with make_session() as db:
        db.add_all(
            [
                Customer(mercos_id="c1", name="Cliente 1", active=True),
                Seller(mercos_id="s1", name="Vendedor", active=True),
                Product(mercos_id="pa", code="PA", name="SKU A", active=True),
                Product(mercos_id="pb", code="PB", name="SKU B", active=True),
                Order(
                    mercos_id="prev-a",
                    number="1",
                    customer_mercos_id="c1",
                    seller_mercos_id="s1",
                    status="2",
                    issued_at=datetime(2026, 7, 1, 12, tzinfo=timezone.utc),
                    total=Decimal("100.00"),
                    net_total=Decimal("100.00"),
                ),
                Order(
                    mercos_id="prev-b",
                    number="2",
                    customer_mercos_id="c1",
                    seller_mercos_id="s1",
                    status="2",
                    issued_at=datetime(2026, 7, 2, 12, tzinfo=timezone.utc),
                    total=Decimal("900.00"),
                    net_total=Decimal("900.00"),
                ),
                Order(
                    mercos_id="curr-mix",
                    number="3",
                    customer_mercos_id="c1",
                    seller_mercos_id="s1",
                    status="2",
                    issued_at=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
                    total=Decimal("860.00"),
                    net_total=Decimal("860.00"),
                ),
                OrderItem(
                    order_mercos_id="prev-a",
                    position=0,
                    mercos_item_id="a0",
                    product_mercos_id="pa",
                    quantity=Decimal("1"),
                    unit_price=Decimal("100.00"),
                    total=Decimal("100.00"),
                ),
                OrderItem(
                    order_mercos_id="prev-b",
                    position=0,
                    mercos_item_id="b0",
                    product_mercos_id="pb",
                    quantity=Decimal("9"),
                    unit_price=Decimal("100.00"),
                    total=Decimal("900.00"),
                ),
                OrderItem(
                    order_mercos_id="curr-mix",
                    position=0,
                    mercos_item_id="a1",
                    product_mercos_id="pa",
                    quantity=Decimal("1"),
                    unit_price=Decimal("50.00"),
                    total=Decimal("50.00"),
                ),
                OrderItem(
                    order_mercos_id="curr-mix",
                    position=1,
                    mercos_item_id="b1",
                    product_mercos_id="pb",
                    quantity=Decimal("9"),
                    unit_price=Decimal("90.00"),
                    total=Decimal("810.00"),
                ),
            ]
        )
        db.commit()
        result = price_savings(db, now=NOW)

        assert result["summary"]["simpleAvgDropPct"] == 30.0
        assert result["summary"]["qtyWeightedDropPct"] == 14.0
        assert result["summary"]["valueWeightedDropPct"] == 14.0
        assert result["summary"]["matchedSavings"] == Decimal("140.00")


def test_customer_tiers_split_top_and_rest() -> None:
    with make_session() as db:
        db.add(Seller(mercos_id="s1", name="Vendedor", active=True))
        db.add(Product(mercos_id="p1", code="P1", name="Produto 1", active=True))
        db.add(
            Order(
                mercos_id="prev",
                number="0",
                customer_mercos_id="c1",
                seller_mercos_id="s1",
                status="2",
                issued_at=datetime(2026, 7, 1, 12, tzinfo=timezone.utc),
                total=Decimal("100.00"),
                net_total=Decimal("100.00"),
            )
        )
        db.add(
            OrderItem(
                order_mercos_id="prev",
                position=0,
                mercos_item_id="prev-i",
                product_mercos_id="p1",
                quantity=Decimal("1"),
                unit_price=Decimal("100.00"),
                total=Decimal("100.00"),
            )
        )
        for index in range(12):
            customer_id = f"c{index + 1}"
            db.add(Customer(mercos_id=customer_id, name=f"Cliente {index + 1}", active=True))
            qty = Decimal(12 - index)
            current_unit = Decimal("80.00")
            db.add(
                Order(
                    mercos_id=f"curr-{index}",
                    number=str(100 + index),
                    customer_mercos_id=customer_id,
                    seller_mercos_id="s1",
                    status="2",
                    issued_at=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
                    total=qty * current_unit,
                    net_total=qty * current_unit,
                )
            )
            db.add(
                OrderItem(
                    order_mercos_id=f"curr-{index}",
                    position=0,
                    mercos_item_id=f"i{index}",
                    product_mercos_id="p1",
                    quantity=qty,
                    unit_price=current_unit,
                    total=qty * current_unit,
                )
            )
        db.commit()
        result = price_savings(db, now=NOW)

        tiers = {row["key"]: row for row in result["customerTiers"]}
        assert tiers["top10"]["count"] == 10
        assert tiers["top10"]["rankFrom"] == 1
        assert tiers["top10"]["rankTo"] == 10
        assert tiers["top20"]["count"] == 2
        assert tiers["top20"]["rankFrom"] == 11
        assert tiers["top20"]["rankTo"] == 12
        assert tiers["rest"]["count"] == 0
        assert tiers["top10"]["savingsSharePct"] > tiers["top20"]["savingsSharePct"]
        assert result["summary"]["customersWithSavings"] == 12
        assert result["summary"]["top10CustomerSavingsSharePct"] == tiers["top10"]["savingsSharePct"]
        assert {row["id"] for row in tiers["top10"]["members"]} == {
            f"c{index}" for index in range(1, 11)
        }


