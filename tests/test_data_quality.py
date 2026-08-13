from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Customer, Order, OrderItem, Product, Seller, SyncState
from app.schemas.data_quality import DataQualityResponse
from app.services.data_quality import build_data_quality_report


def make_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def seed_dirty_data(db: Session) -> None:
    now = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
    db.add_all(
        [
            Customer(
                mercos_id="c1",
                name="Cliente 1",
                document="DUP",
                raw={"id": 1, "email": "nao-deve-sair"},
            ),
            Customer(
                mercos_id="c2",
                name="Cliente 2",
                document="DUP",
                raw={},
            ),
            Product(
                mercos_id="p1",
                code="DUP",
                name="Produto 1",
                category_id=None,
                raw={"id": 1, "preco_tabela": 10},
            ),
            Product(
                mercos_id="p2",
                code="DUP",
                name="Produto 2",
                category_id="cat",
                raw={},
            ),
            Seller(mercos_id="s1", name="Vendedor", raw={}),
            Order(
                mercos_id="o1",
                number="1",
                customer_mercos_id="c1",
                seller_mercos_id="s1",
                status="2",
                issued_at=now,
                total=100,
                raw={"id": 1, "status": "2"},
            ),
            Order(
                mercos_id="o2",
                number="2",
                customer_mercos_id="missing",
                seller_mercos_id=None,
                status="2",
                issued_at=now,
                total=0,
                raw={},
            ),
            Order(
                mercos_id="o3",
                number="3",
                customer_mercos_id="c1",
                seller_mercos_id="s1",
                status="0",
                issued_at=now,
                total=50,
                raw={},
            ),
            OrderItem(
                order_mercos_id="o1",
                position=0,
                product_mercos_id="p1",
                name="Produto 1",
                quantity=1,
                total=90,
                raw={"produto_id": 1},
            ),
            OrderItem(
                order_mercos_id="o3",
                position=0,
                product_mercos_id="missing",
                name="Sem produto",
                quantity=0,
                total=50,
                raw={},
            ),
            SyncState(
                resource="orders",
                status="interrupted",
                cursor="2026-08-01T00:00:00",
                records=3,
            ),
        ]
    )
    db.commit()


def test_data_quality_counts_coverage_and_warnings() -> None:
    with make_session() as db:
        seed_dirty_data(db)
        report = build_data_quality_report(db)
        parsed = DataQualityResponse.model_validate(report)

        assert parsed.counts == {
            "customers": 2,
            "products": 2,
            "sellers": 1,
            "orders": 3,
            "orderItems": 2,
        }
        assert parsed.integrity.ordersWithoutItems == 1
        assert parsed.integrity.ordersWithoutCustomer == 1
        assert parsed.integrity.ordersWithoutSeller == 1
        assert parsed.integrity.itemsWithoutProduct == 1
        assert parsed.integrity.orderTotalDivergences == 1
        assert parsed.coverage.ordersWithItemsPct == 66.67
        assert parsed.coverage.itemsWithProductPct == 50.0
        assert parsed.coverage.recognizedStatusPct == 100.0
        assert parsed.zeroValues["ordersWithZeroTotal"] == 1
        assert parsed.zeroValues["itemsWithZeroQuantity"] == 1
        assert parsed.duplicates["customerDocumentGroups"] == 1
        assert parsed.duplicates["productCodeGroups"] == 1
        assert parsed.metadata.isPartial is True
        assert any("95%" in warning for warning in parsed.warnings)
        assert any("Sincronização incompleta" in warning for warning in parsed.warnings)


def test_raw_inventory_only_returns_shape_not_values() -> None:
    with make_session() as db:
        seed_dirty_data(db)
        report = build_data_quality_report(
            db, include_raw_inventory=True, raw_sample_limit=10
        )

        inventory = report["rawFieldInventory"]
        assert inventory["customers"]["fields"]["email"]["types"] == {"str": 1}
        serialized = str(inventory)
        assert "nao-deve-sair" not in serialized
