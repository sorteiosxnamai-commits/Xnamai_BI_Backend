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


def test_revenue_reconciles_with_equivalent_sql() -> None:
    with make_session() as db:
        seed_orders(db)
        filters = AnalyticsFilters(period="all")

        kpi_revenue = overview(db, filters)["kpis"]["netRevenue"]["value"]
        sql_revenue = db.scalar(
            select(func.coalesce(func.sum(func.coalesce(Order.net_total, Order.total)), 0))
            .where(Order.status.in_(VALID_SALE_STATUSES))
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
            select(func.coalesce(func.sum(OrderItem.total), 0))
            .join(Order, Order.mercos_id == OrderItem.order_mercos_id)
            .where(Order.status.in_(VALID_SALE_STATUSES))
        )

        assert kpi_revenue == sql_revenue
        assert sum((row["revenue"] for row in product_rows), Decimal("0")) == item_revenue


def test_overview_separates_sales_and_cancellations_with_comparison() -> None:
    with make_session() as db:
        seed_orders(db)
        filters = AnalyticsFilters(
            dateFrom=date(2026, 8, 1),
            dateTo=date(2026, 8, 12),
            period="all",
        )

        result = overview(db, filters)

        assert result["kpis"]["grossRevenue"]["value"] == Decimal("110.00")
        assert result["kpis"]["netRevenue"]["value"] == Decimal("100.00")
        assert result["kpis"]["orders"]["value"] == Decimal("1")
        assert result["kpis"]["averageTicket"]["value"] == Decimal("100.00")
        assert result["kpis"]["cancellations"]["value"] == Decimal("1")
        assert result["kpis"]["cancelledValue"]["value"] == Decimal("20.00")
        assert result["kpis"]["netRevenue"]["previousValue"] == Decimal("50.00")
        assert result["kpis"]["netRevenue"]["percentageChange"] == 100.0
        assert result["kpis"]["newBuyers"]["value"] == Decimal("0")
        assert result["kpis"]["recurringBuyers"]["value"] == Decimal("1")
        series = timeseries(db, filters)
        assert len(series["items"]) == len(series["previousItems"])
        assert next(
            point for point in series["items"] if point["period"] == "2026-08-10"
        )["revenue"] == Decimal("100.00")
        assert sum(
            (point["revenue"] for point in series["previousItems"]),
            Decimal("0"),
        ) == Decimal("50.00")


def test_overview_does_not_silently_discard_high_value_sales() -> None:
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

        assert result["kpis"]["netRevenue"]["value"] == Decimal("750000.00")


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
        assert first["summary"]["largestOrderValue"] == Decimal("100.00")
        assert sales_only["totalItems"] == 1
        assert sales_only["items"][0]["id"] == "current-sale"
        detail = order_detail(db, "current-sale", filters)
        assert detail["order"]["number"] == "100"
        assert len(detail["items"]) == 2
        product_filtered = overview(
            db,
            filters.model_copy(update={"productIds": ["p2"]}),
        )
        state_filtered = overview(
            db,
            filters.model_copy(update={"states": ["PR"]}),
        )
        assert product_filtered["kpis"]["netRevenue"]["value"] == Decimal("100")
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
