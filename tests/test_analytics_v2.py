from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.domain.order_status import VALID_SALE_STATUSES
from app.models import Customer, Order, OrderItem, Product, Seller, SyncState
from app.schemas.analytics import AnalyticsFilters
from app.services.analytics_filters import data_through_timestamp, date_bounds
from app.services.analytics_v2 import (
    analytics_metadata,
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
                issued_at=datetime(2026, 7, 10, 12, tzinfo=timezone.utc),
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


def test_placeholder_catalog_price_does_not_change_mercos_header_revenue() -> None:
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

        assert result["kpis"]["netRevenue"]["value"] == Decimal("100.00")
        placeholder = next(row for row in products["items"] if row["id"] == "placeholder")
        assert placeholder["revenue"] == Decimal("5000")


def test_revenue_reconciles_with_equivalent_sql() -> None:
    with make_session() as db:
        seed_orders(db)
        filters = AnalyticsFilters(period="all")

        kpi_revenue = overview(db, filters)["kpis"]["netRevenue"]["value"]
        sql_revenue = db.scalar(
            select(
                func.coalesce(
                    func.sum(func.coalesce(Order.net_total, Order.total)),
                    0,
                )
            ).where(Order.status.in_(VALID_SALE_STATUSES))
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
                    func.sum(OrderItem.total),
                    0,
                )
            )
            .join(Order, Order.mercos_id == OrderItem.order_mercos_id)
            .join(Product, Product.mercos_id == OrderItem.product_mercos_id)
            .where(
                Order.status.in_(VALID_SALE_STATUSES),
                OrderItem.excluded.is_(False),
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

        assert result["kpis"]["grossRevenue"]["value"] == Decimal("110.00")
        assert result["kpis"]["netRevenue"]["value"] == Decimal("100.00")
        assert result["kpis"]["orders"]["value"] == Decimal("1")
        assert result["kpis"]["averageTicket"]["value"] == Decimal("100.00")
        assert result["kpis"]["cancellations"]["value"] == Decimal("1")
        assert result["kpis"]["cancelledValue"]["value"] == Decimal("20.00")
        assert result["kpis"]["discountTotal"]["value"] == Decimal("10.00")
        assert result["kpis"]["netRevenue"]["previousValue"] == Decimal("50.00")
        assert result["kpis"]["netRevenue"]["percentageChange"] == 100.0
        assert result["comparison"] == {
            "currentFrom": "2026-08-01",
            "currentTo": "2026-08-12",
            "previousFrom": "2026-07-01",
            "previousTo": "2026-07-12",
        }
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


def test_overview_uses_mercos_order_total_without_items() -> None:
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
        assert result["kpis"]["orders"]["value"] == Decimal("1")


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
        assert detail["order"]["total"] == Decimal("100.00")
        assert len(detail["items"]) == 2
        assert detail["items"][0]["unitPrice"] == Decimal("50")
        assert detail["items"][0]["total"] == Decimal("100")
        assert detail["items"][0]["priceSource"] == "mercos"
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
        assert products["items"][0]["revenue"] == Decimal("100")
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


def test_cohorts_calculate_retention_and_cumulative_ltv() -> None:
    with make_session() as db:
        db.add(
            Product(
                mercos_id="p1",
                code="P1",
                name="Produto",
                list_price=Decimal("50"),
                stock=Decimal("10"),
                active=True,
            )
        )
        db.add_all(
            [
                Customer(mercos_id="c1", name="Cliente 1", active=True),
                Customer(mercos_id="c2", name="Cliente 2", active=True),
                Customer(mercos_id="c3", name="Cliente 3", active=True),
            ]
        )
        orders = [
            ("o1", "c1", datetime(2026, 1, 5, tzinfo=timezone.utc), Decimal("2")),
            ("o2", "c1", datetime(2026, 2, 5, tzinfo=timezone.utc), Decimal("1")),
            ("o3", "c2", datetime(2026, 1, 7, tzinfo=timezone.utc), Decimal("1")),
            ("o4", "c3", datetime(2026, 2, 8, tzinfo=timezone.utc), Decimal("4")),
            ("o5", "c3", datetime(2026, 3, 8, tzinfo=timezone.utc), Decimal("2")),
        ]
        for position, (order_id, customer_id, issued_at, quantity) in enumerate(orders):
            db.add(
                Order(
                    mercos_id=order_id,
                    number=order_id,
                    customer_mercos_id=customer_id,
                    status="2",
                    issued_at=issued_at,
                    total=quantity * Decimal("50"),
                    item_count=1,
                    sku_count=1,
                )
            )
            db.add(
                OrderItem(
                    order_mercos_id=order_id,
                    position=0,
                    mercos_item_id=str(position),
                    product_mercos_id="p1",
                    name="Produto",
                    quantity=quantity,
                    total=quantity * Decimal("50"),
                )
            )
        db.commit()

        result = cohorts(db, AnalyticsFilters(period="all"))

        assert result["summary"] == {
            "customers": 3,
            "repeatCustomers": 2,
            "repeatRate": 66.67,
            "month1RetainedCustomers": 2,
            "month1EligibleCustomers": 3,
            "month1RetentionRate": 66.67,
            "totalRevenue": Decimal("500"),
            "realizedLtv": Decimal("166.6666666666666666666666667"),
        }
        january, february = result["cohorts"]
        assert january["size"] == 2
        assert [cell["rate"] for cell in january["retention"]] == [100.0, 50.0, 0.0]
        assert january["retention"][1]["cumulativeLtv"] == Decimal("100")
        assert february["realizedLtv"] == Decimal("300")
        assert result["retentionCurve"][1]["retentionRate"] == 66.67
        assert result["ltvCurve"][1]["ltv"] == Decimal("166.6666666666666666666666667")
        assert result["ltvCurve"][2]["ltv"] == Decimal("100")


def test_excluded_customers_leave_the_totals_and_the_list() -> None:
    with make_session() as db:
        seed_orders(db)
        included = customers_page(
            db,
            AnalyticsFilters(period="all"),
            page=1,
            page_size=50,
            search=None,
            sort="revenue",
            order="desc",
        )
        excluded = customers_page(
            db,
            AnalyticsFilters(period="all", excludedCustomerIds=["c1"]),
            page=1,
            page_size=50,
            search=None,
            sort="revenue",
            order="desc",
        )

        assert "c1" in [item["id"] for item in included["items"]]
        assert included["summary"]["totalRevenue"] == Decimal("150")
        assert "c1" not in [item["id"] for item in excluded["items"]]
        assert excluded["summary"]["totalRevenue"] == Decimal("0")
        assert excluded["summary"]["top5"]["members"] == []
        assert excluded["appliedFilters"]["excludedCustomerIds"] == ["c1"]
        assert included["summary"]["totalRevenue"] == overview(
            db, AnalyticsFilters(period="all")
        )["kpis"]["netRevenue"]["value"]
def test_data_through_uses_orders_sync_success_time() -> None:
    with make_session() as db:
        db.add(
            Order(
                mercos_id="o1",
                number="1",
                status="2",
                issued_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
                total=10,
            )
        )
        db.add(
            SyncState(
                resource="orders",
                status="success",
                last_success_at=datetime(2026, 9, 11, 17, tzinfo=timezone.utc),
                records=1,
            )
        )
        db.commit()
        through = data_through_timestamp(db)
        assert through == datetime(2026, 9, 11, 17, tzinfo=timezone.utc)
        assert analytics_metadata(db)["dataThrough"] == through
