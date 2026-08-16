from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.domain.order_status import VALID_SALE_STATUSES
from app.models import Customer, Order, OrderItem, Product, Seller
from app.schemas.analytics import AnalyticsFilters
from app.services.analytics_filters import date_bounds
from app.services.analytics_v2 import (
    associations,
    breakdowns,
    cohorts,
    customer_detail,
    customers_page,
    filter_options,
    geography,
    inventory_page,
    order_detail,
    orders_page,
    overview,
    product_detail,
    products_page,
    rankings,
    seller_detail,
    sellers_page,
    timeseries,
)


def make_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def seed_orders(db: Session) -> None:
    db.add_all(
        [
            Customer(
                mercos_id="c1",
                name="Zeta",
                city="São Paulo",
                state="SP",
                active=True,
            ),
            Customer(
                mercos_id="c2",
                name="Alfa",
                city="Curitiba",
                state="PR",
                active=True,
            ),
            Seller(mercos_id="s1", name="Vendedor", active=True),
            Product(
                mercos_id="p1",
                code="P1",
                name="Produto 1",
                list_price=Decimal("60"),
                stock=Decimal("10"),
                active=True,
            ),
            Product(
                mercos_id="p2",
                code="P2",
                name="Produto 2",
                list_price=Decimal("40"),
                stock=Decimal("0"),
                active=True,
            ),
            Order(
                mercos_id="current-sale",
                number="100",
                customer_mercos_id="c1",
                seller_mercos_id="s1",
                status="2",
                issued_at=datetime(2026, 8, 10, 12, tzinfo=timezone.utc),
                total=Decimal("100.00"),
                gross_total=Decimal("110.00"),
                net_total=Decimal("100.00"),
                discount_value=Decimal("10.00"),
                item_count=2,
                sku_count=1,
            ),
            Order(
                mercos_id="current-cancelled",
                number="101",
                customer_mercos_id="c2",
                seller_mercos_id="s1",
                status="0",
                issued_at=datetime(2026, 8, 11, 12, tzinfo=timezone.utc),
                total=Decimal("20.00"),
                item_count=1,
                sku_count=1,
            ),
            Order(
                mercos_id="previous-sale",
                number="099",
                customer_mercos_id="c1",
                seller_mercos_id="s1",
                status="2",
                issued_at=datetime(2026, 7, 25, 12, tzinfo=timezone.utc),
                total=Decimal("50.00"),
                item_count=1,
                sku_count=1,
            ),
            OrderItem(
                order_mercos_id="current-sale",
                position=0,
                mercos_item_id="1",
                product_mercos_id="p1",
                name="Produto 1",
                quantity=Decimal("2"),
                total=Decimal("100"),
            ),
            OrderItem(
                order_mercos_id="current-sale",
                position=1,
                mercos_item_id="2",
                product_mercos_id="p2",
                name="Produto 2",
                quantity=Decimal("1"),
                total=Decimal("40"),
            ),
        ]
    )
    db.commit()


def test_explicit_dates_use_sao_paulo_day_boundaries() -> None:
    filters = AnalyticsFilters(
        dateFrom=date(2026, 8, 12),
        dateTo=date(2026, 8, 12),
        period="all",
    )

    start, end = date_bounds(filters)

    assert start == datetime(2026, 8, 12, 3, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 13, 3, tzinfo=timezone.utc)


def test_entity_drilldowns_share_filters_and_return_related_data() -> None:
    with make_session() as db:
        seed_orders(db)
        filters = AnalyticsFilters(period="all")

        product = product_detail(db, "p1", filters)
        customer = customer_detail(db, "c1", filters)
        seller = seller_detail(db, "s1", filters)

        assert product is not None
        assert product["product"]["id"] == "p1"
        assert product["recentOrders"]["items"][0]["id"] == "current-sale"
        assert product["customers"]["items"][0]["id"] == "c1"
        assert customer is not None
        assert customer["customer"]["id"] == "c1"
        assert customer["orders"]["totalItems"] == 2
        assert customer["products"]["items"][0]["id"] == "p1"
        assert seller is not None
        assert seller["seller"]["id"] == "s1"
        assert seller["orders"]["totalItems"] == 3
        assert seller["customers"]["totalItems"] == 1
        assert product_detail(db, "missing", filters) is None


def test_breakdowns_are_aggregated_server_side() -> None:
    with make_session() as db:
        seed_orders(db)

        result = breakdowns(db, AnalyticsFilters(period="all"))

        assert sum(row["orders"] for row in result["statuses"]) == 3
        assert sum(row["orders"] for row in result["orderValueBands"]) == 2
        assert sum(row["entities"] for row in result["productAbc"]) == 2
        assert sum(row["entities"] for row in result["customerAbc"]) == 1


def test_rankings_share_the_same_server_side_filters() -> None:
    with make_session() as db:
        seed_orders(db)

        result = rankings(
            db,
            AnalyticsFilters(period="all", customerIds=["c1"]),
        )

        assert result["products"]["items"][0]["id"] == "p1"
        assert result["customers"]["items"][0]["id"] == "c1"
        assert result["sellers"]["items"][0]["id"] == "s1"
        assert result["products"]["appliedFilters"]["customerIds"] == ["c1"]
        assert result["customers"]["appliedFilters"]["customerIds"] == ["c1"]
        assert result["sellers"]["appliedFilters"]["customerIds"] == ["c1"]


def test_inventory_summary_respects_product_filters() -> None:
    with make_session() as db:
        seed_orders(db)

        result = inventory_page(
            db,
            AnalyticsFilters(period="all", productIds=["p1"]),
            page=1,
            page_size=50,
            search=None,
            sort="stock_value",
            order="desc",
        )

        assert result["totalItems"] == 1
        assert result["summary"]["stockValueAtListPrice"] == Decimal("600")
        assert result["summary"]["productsWithPositiveStock"] == 1
        assert result["summary"]["productsWithoutStock"] == 0


def test_inventory_ignores_placeholder_list_price() -> None:
    with make_session() as db:
        db.add(
            Product(
                mercos_id="placeholder",
                code="SJK-6685",
                name="Produto com preço sentinela",
                list_price=Decimal("1000"),
                stock=Decimal("2000"),
                active=True,
            )
        )
        db.commit()

        result = inventory_page(
            db,
            AnalyticsFilters(period="all", productIds=["placeholder"]),
            page=1,
            page_size=50,
            search=None,
            sort="stock_value",
            order="desc",
        )

        assert result["items"][0]["listPrice"] is None
        assert result["items"][0]["stockValue"] == Decimal("0")
        assert result["summary"]["stockValueAtListPrice"] == Decimal("0")


def test_placeholder_list_price_does_not_inflate_revenue() -> None:
    with make_session() as db:
        seed_orders(db)
        db.add(
            Product(
                mercos_id="placeholder",
                code="SJK-6685",
                name="Produto com preço sentinela",
                list_price=Decimal("1000"),
                stock=Decimal("2000"),
                active=True,
            )
        )
        db.add(
            OrderItem(
                order_mercos_id="current-sale",
                position=2,
                mercos_item_id="placeholder-item",
                product_mercos_id="placeholder",
                name="Produto com preço sentinela",
                quantity=Decimal("5"),
                unit_price=Decimal("1000"),
                total=Decimal("5000"),
            )
        )
        db.commit()

        filters = AnalyticsFilters(
            dateFrom=date(2026, 8, 1),
            dateTo=date(2026, 8, 12),
            period="all",
        )
        result = overview(db, filters)
        products = products_page(
            db,
            filters,
            page=1,
            page_size=50,
            search=None,
            sort="revenue",
            order="desc",
        )

        assert result["kpis"]["netRevenue"]["value"] == Decimal("160.00")
        assert all(row["id"] != "placeholder" or row["revenue"] == 0 for row in products["items"])


def test_revenue_reconciles_with_equivalent_sql() -> None:
    with make_session() as db:
        seed_orders(db)
        filters = AnalyticsFilters(period="all")

        kpi_revenue = overview(db, filters)["kpis"]["netRevenue"]["value"]
        sql_revenue = db.scalar(
            select(
                func.coalesce(
                    func.sum(OrderItem.quantity * Product.list_price),
                    0,
                )
            )
            .join(Order, Order.mercos_id == OrderItem.order_mercos_id)
            .join(Product, Product.mercos_id == OrderItem.product_mercos_id)
            .where(
                Order.status.in_(VALID_SALE_STATUSES),
                OrderItem.excluded.is_(False),
                Product.list_price != Decimal("1000"),
            )
        )
        product_rows = products_page(
            db,
            filters,
            page=1,
            page_size=100,
            search=None,
            sort="revenue",
            order="desc",
        )["items"]
        item_revenue = db.scalar(
            select(
                func.coalesce(
                    func.sum(OrderItem.quantity * Product.list_price),
                    0,
                )
            )
            .join(Order, Order.mercos_id == OrderItem.order_mercos_id)
            .join(Product, Product.mercos_id == OrderItem.product_mercos_id)
            .where(
                Order.status.in_(VALID_SALE_STATUSES),
                OrderItem.excluded.is_(False),
                Product.list_price != Decimal("1000"),
            )
        )

        assert kpi_revenue == sql_revenue
        assert sum((row["revenue"] for row in product_rows), Decimal("0")) == item_revenue


def test_overview_separates_sales_and_cancellations_with_comparison() -> None:
    with make_session() as db:
        seed_orders(db)
        db.add(
            OrderItem(
                order_mercos_id="current-sale",
                position=99,
                mercos_item_id="excluded-item",
                product_mercos_id="p1",
                name="Produto excluído",
                quantity=Decimal("100"),
                unit_price=Decimal("1000"),
                total=Decimal("100000"),
                excluded=True,
            )
        )
        db.commit()
        filters = AnalyticsFilters(
            dateFrom=date(2026, 8, 1),
            dateTo=date(2026, 8, 12),
            period="all",
        )

        result = overview(db, filters)

        assert result["kpis"]["grossRevenue"]["value"] == Decimal("160.00")
        assert result["kpis"]["netRevenue"]["value"] == Decimal("160.00")
        assert result["kpis"]["orders"]["value"] == Decimal("1")
        assert result["kpis"]["averageTicket"]["value"] == Decimal("160.00")
        assert result["kpis"]["cancellations"]["value"] == Decimal("1")
        assert result["kpis"]["cancelledValue"]["value"] == Decimal("0.00")
        assert result["kpis"]["netRevenue"]["previousValue"] == Decimal("0.00")
        assert result["kpis"]["netRevenue"]["percentageChange"] is None
        assert result["kpis"]["newBuyers"]["value"] == Decimal("0")
        assert result["kpis"]["recurringBuyers"]["value"] == Decimal("1")
        series = timeseries(db, filters)
        assert len(series["items"]) == len(series["previousItems"])
        assert next(
            point for point in series["items"] if point["period"] == "2026-08-10"
        )["revenue"] == Decimal("160.00")
        assert sum(
            (point["revenue"] for point in series["previousItems"]),
            Decimal("0"),
        ) == Decimal("0.00")


def test_overview_requires_items_with_valid_current_list_price() -> None:
    with make_session() as db:
        db.add(
            Order(
                mercos_id="high-value",
                number="HV-1",
                status="2",
                issued_at=datetime(2026, 8, 10, 12, tzinfo=timezone.utc),
                total=Decimal("750000.00"),
                net_total=Decimal("750000.00"),
                gross_total=Decimal("750000.00"),
            )
        )
        db.commit()

        result = overview(
            db,
            AnalyticsFilters(
                dateFrom=date(2026, 8, 1),
                dateTo=date(2026, 8, 12),
                period="all",
            ),
        )

        assert result["kpis"]["netRevenue"]["value"] == Decimal("0.00")


def test_all_history_buyer_mix_uses_purchase_frequency() -> None:
    with make_session() as db:
        seed_orders(db)

        result = overview(db, AnalyticsFilters(period="all"))

        assert result["kpis"]["newBuyers"]["value"] == Decimal("0")
        assert result["kpis"]["recurringBuyers"]["value"] == Decimal("1")
        assert "exatamente uma" in result["kpis"]["newBuyers"]["definition"]


def test_orders_pagination_sort_and_status_filter_are_server_side() -> None:
    with make_session() as db:
        seed_orders(db)
        filters = AnalyticsFilters(
            dateFrom=date(2026, 8, 1),
            dateTo=date(2026, 8, 12),
            period="all",
        )

        first = orders_page(
            db,
            filters,
            page=1,
            page_size=1,
            search=None,
            sort="total",
            order="asc",
        )
        sales_only = orders_page(
            db,
            filters.model_copy(update={"statuses": ["2"]}),
            page=1,
            page_size=50,
            search=None,
            sort="issued_at",
            order="desc",
        )

        assert first["totalItems"] == 2
        assert first["totalPages"] == 2
        assert first["items"][0]["id"] == "current-cancelled"
        assert first["summary"]["validOrders"] == 1
        assert first["summary"]["largestOrderValue"] == Decimal("160.00")
        assert sales_only["totalItems"] == 1
        assert sales_only["items"][0]["id"] == "current-sale"
        detail = order_detail(db, "current-sale", filters)
        assert detail["order"]["number"] == "100"
        assert len(detail["items"]) == 2
        assert detail["items"][0]["unitPrice"] == Decimal("60")
        assert detail["items"][0]["total"] == Decimal("120")
        assert detail["items"][0]["sourceUnitPrice"] == Decimal("0")
        assert detail["items"][0]["sourceTotal"] == Decimal("100")
        assert detail["items"][0]["priceSource"] == "catalog"
        product_filtered = overview(
            db,
            filters.model_copy(update={"productIds": ["p2"]}),
        )
        state_filtered = overview(
            db,
            filters.model_copy(update={"states": ["PR"]}),
        )
        assert product_filtered["kpis"]["netRevenue"]["value"] == Decimal("40")
        assert state_filtered["kpis"]["netRevenue"]["value"] == Decimal("0")
        assert state_filtered["kpis"]["cancellations"]["value"] == Decimal("1")


def test_paginated_entities_and_advanced_analytics_execute() -> None:
    with make_session() as db:
        seed_orders(db)
        filters = AnalyticsFilters(
            dateFrom=date(2026, 8, 1),
            dateTo=date(2026, 8, 12),
            period="all",
        )

        products = products_page(
            db,
            filters,
            page=1,
            page_size=50,
            search=None,
            sort="revenue",
            order="desc",
        )
        products_by_price = products_page(
            db,
            filters,
            page=1,
            page_size=50,
            search=None,
            sort="list_price",
            order="asc",
        )
        customers = customers_page(
            db,
            filters,
            page=1,
            page_size=50,
            search=None,
            sort="revenue",
            order="desc",
        )
        sellers = sellers_page(
            db,
            filters,
            page=1,
            page_size=50,
            search=None,
            sort="revenue",
            order="desc",
        )

        assert products["totalItems"] == 2
        assert products["items"][0]["id"] == "p1"
        assert products["items"][0]["quantitySold"] == Decimal("2")
        assert products["items"][0]["revenue"] == Decimal("120")
        assert [item["id"] for item in products_by_price["items"]] == ["p2", "p1"]
        assert customers["items"][0]["id"] == "c1"
        assert sellers["items"][0]["id"] == "s1"
        assert geography(db, filters)["states"][0]["state"] == "SP"
        assert cohorts(db, filters)["cohorts"][0]["cohort"] == "2026-08"
        assert associations(db, filters)["items"][0]["ordersTogether"] == 1
        statuses = filter_options(
            db,
            option="statuses",
            search=None,
            page=1,
            page_size=50,
        )
        assert {item["id"] for item in statuses["items"]} == {"0", "2"}
        cities = filter_options(
            db,
            option="cities",
            search=None,
            page=1,
            page_size=50,
            states=["SP"],
        )
        assert cities["items"] == [{"id": "São Paulo", "label": "São Paulo"}]


def test_customers_summary_splits_top_cohorts_and_long_tail() -> None:
    with make_session() as db:
        db.add(Seller(mercos_id="s1", name="Vendedor", active=True))
        db.add(
            Product(
                mercos_id="p1",
                code="P1",
                name="Produto",
                list_price=Decimal("100"),
                stock=Decimal("10"),
                active=True,
            )
        )
        issued_at = datetime(2026, 8, 1, 15, tzinfo=timezone.utc)
        rows: list = []
        item_id = 1
        for index in range(1, 26):
            customer_id = f"c{index:02d}"
            rows.append(
                Customer(
                    mercos_id=customer_id,
                    name=f"Cliente {index:02d}",
                    city="São Paulo",
                    state="SP",
                    active=True,
                )
            )
            if index <= 5:
                order_count, quantity = 3, Decimal("10")
            elif index <= 20:
                order_count, quantity = 1, Decimal("5")
            else:
                order_count, quantity = 1, Decimal("1")
            for order_n in range(order_count):
                order_id = f"{customer_id}-{order_n}"
                rows.append(
                    Order(
                        mercos_id=order_id,
                        number=str(item_id),
                        customer_mercos_id=customer_id,
                        seller_mercos_id="s1",
                        status="2",
                        issued_at=issued_at,
                        total=quantity * Decimal("100"),
                        item_count=1,
                        sku_count=1,
                    )
                )
                rows.append(
                    OrderItem(
                        order_mercos_id=order_id,
                        position=0,
                        mercos_item_id=str(item_id),
                        product_mercos_id="p1",
                        name="Produto",
                        quantity=quantity,
                        total=quantity * Decimal("100"),
                    )
                )
                item_id += 1
        db.add_all(rows)
        db.commit()

        result = customers_page(
            db,
            AnalyticsFilters(
                dateFrom=date(2026, 7, 17),
                dateTo=date(2026, 8, 15),
            ),
            page=1,
            page_size=10,
            search=None,
            sort="revenue",
            order="desc",
        )

        summary = result["summary"]
        exclusive = [
            summary["top5"],
            summary["ranks6to10"],
            summary["ranks11to20"],
            summary["rest"],
        ]
        assert summary["periodMonths"] == 1.0
        assert summary["totalRevenue"] == Decimal("23000")
        assert summary["top5"]["customerCount"] == 5
        assert summary["top5"]["averageMonthlyOrders"] == 3.0
        assert summary["top5"]["averageRevenuePerCustomer"] == Decimal("3000")
        assert summary["top5"]["averageOrderValue"] == Decimal("1000")
        assert summary["top5"]["revenueSharePct"] == 65.22
        assert summary["ranks6to10"]["customerCount"] == 5
        assert summary["ranks6to10"]["revenueSharePct"] == 10.87
        assert summary["ranks6to10"]["averageRevenuePerCustomer"] == Decimal("500")
        assert summary["ranks6to10"]["averageOrderValue"] == Decimal("500")
        assert summary["ranks11to20"]["customerCount"] == 10
        assert summary["ranks11to20"]["revenueSharePct"] == 21.74
        assert summary["rest"]["customerCount"] == 5
        assert summary["rest"]["averageMonthlyOrders"] == 1.0
        assert summary["rest"]["revenueSharePct"] == 2.17
        assert summary["rest"]["averageRevenuePerCustomer"] == Decimal("100")
        assert summary["rest"]["averageOrderValue"] == Decimal("100")
        assert sum(band["revenueSharePct"] for band in exclusive) == 100.0
        assert sum((band["revenue"] for band in exclusive), Decimal("0")) == summary[
            "totalRevenue"
        ]
        assert summary["concentrationTop10Pct"] == 76.09
        assert summary["concentrationTop20Pct"] == 97.83
        assert [member["name"] for member in summary["top5"]["members"]] == [
            "Cliente 01",
            "Cliente 02",
            "Cliente 03",
            "Cliente 04",
            "Cliente 05",
        ]
        assert summary["top5"]["members"][0]["rank"] == 1
        assert summary["ranks6to10"]["members"][0]["name"] == "Cliente 06"
        assert summary["ranks6to10"]["members"][0]["rank"] == 6
        assert len(summary["rest"]["members"]) == 5
        assert summary["rest"]["members"][0]["id"] == "c21"
