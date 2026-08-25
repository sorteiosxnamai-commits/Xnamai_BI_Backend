from datetime import datetime
from decimal import Decimal
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Identity,
    Index,
    Integer,
    Float,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    mercos_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    document: Mapped[str | None] = mapped_column(String(30))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(5))
    email: Mapped[str | None] = mapped_column(String(300))
    phone: Mapped[str | None] = mapped_column(String(40))
    segment_mercos_id: Mapped[str | None] = mapped_column(String(80), index=True)
    created_at_source: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    mercos_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    code: Mapped[str | None] = mapped_column(String(100), index=True)
    name: Mapped[str] = mapped_column(String(400), default="")
    category_id: Mapped[str | None] = mapped_column(String(80))
    category_mercos_id: Mapped[str | None] = mapped_column(String(80), index=True)
    unit: Mapped[str | None] = mapped_column(String(30))
    list_price: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    minimum_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    stock: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at_source: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class Seller(Base):
    __tablename__ = "sellers"
    id: Mapped[int] = mapped_column(primary_key=True)
    mercos_id: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    mercos_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    number: Mapped[str] = mapped_column(String(100), index=True)
    customer_mercos_id: Mapped[str | None] = mapped_column(String(80), index=True)
    seller_mercos_id: Mapped[str | None] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(50), index=True)
    issued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    discount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    order_type_mercos_id: Mapped[str | None] = mapped_column(String(80), index=True)
    payment_condition_mercos_id: Mapped[str | None] = mapped_column(
        String(80), index=True
    )
    price_table_mercos_id: Mapped[str | None] = mapped_column(String(80), index=True)
    carrier_mercos_id: Mapped[str | None] = mapped_column(String(80), index=True)
    commercial_policy_mercos_id: Mapped[str | None] = mapped_column(
        String(80), index=True
    )
    gross_total: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    net_total: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    discount_value: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 2), nullable=True
    )
    discount_percent: Mapped[Decimal | None] = mapped_column(
        Numeric(9, 4), nullable=True
    )
    item_count: Mapped[int | None] = mapped_column(Integer)
    sku_count: Mapped[int | None] = mapped_column(Integer)
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        UniqueConstraint(
            "order_mercos_id", "position", name="uq_order_items_order_position"
        ),
        UniqueConstraint(
            "order_mercos_id", "mercos_item_id", name="uq_order_items_order_mercos_item"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    order_mercos_id: Mapped[str] = mapped_column(String(80), index=True)
    position: Mapped[int] = mapped_column(Integer)
    mercos_item_id: Mapped[str | None] = mapped_column(String(80), index=True)
    product_mercos_id: Mapped[str | None] = mapped_column(String(80), index=True)
    code: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(400), default="")
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    list_unit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 2),
        nullable=True,
    )
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    discount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    mercos_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    parent_mercos_id: Mapped[str | None] = mapped_column(String(80), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class CustomerSegment(Base):
    __tablename__ = "customer_segments"
    id: Mapped[int] = mapped_column(primary_key=True)
    mercos_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class OrderType(Base):
    __tablename__ = "order_types"
    id: Mapped[int] = mapped_column(primary_key=True)
    mercos_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class PaymentCondition(Base):
    __tablename__ = "payment_conditions"
    id: Mapped[int] = mapped_column(primary_key=True)
    mercos_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class PriceTable(Base):
    __tablename__ = "price_tables"
    id: Mapped[int] = mapped_column(primary_key=True)
    mercos_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class ProductPrice(Base):
    __tablename__ = "product_prices"
    __table_args__ = (
        UniqueConstraint(
            "product_mercos_id",
            "price_table_mercos_id",
            name="uq_product_prices_product_table",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    product_mercos_id: Mapped[str] = mapped_column(String(80), index=True)
    price_table_mercos_id: Mapped[str] = mapped_column(String(80), index=True)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class Carrier(Base):
    __tablename__ = "carriers"
    id: Mapped[int] = mapped_column(primary_key=True)
    mercos_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class CommercialPolicy(Base):
    __tablename__ = "commercial_policies"
    id: Mapped[int] = mapped_column(primary_key=True)
    mercos_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class SyncState(Base):
    __tablename__ = "sync_states"
    resource: Mapped[str] = mapped_column(String(50), primary_key=True)
    cursor: Mapped[str | None] = mapped_column(Text)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="never")
    records: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (
        Index("ix_sync_runs_resource_started_at", "resource", "started_at"),
    )
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        Identity(always=True),
        primary_key=True,
    )
    resource: Mapped[str] = mapped_column(String(50), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cursor_before: Mapped[str | None] = mapped_column(Text)
    cursor_after: Mapped[str | None] = mapped_column(Text)
    pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    persisted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class CrmAttendance(Base):
    __tablename__ = "crm_attendances"
    __table_args__ = (
        UniqueConstraint("customer_mercos_id", name="uq_crm_attendances_customer"),
        Index("ix_crm_attendances_status_finished", "status", "finished_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_mercos_id: Mapped[str] = mapped_column(String(80), index=True)
    seller_name: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="open", index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(String(20))
    sale_value: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    order_number: Mapped[str | None] = mapped_column(String(80))
    ai_analysis: Mapped[dict | None] = mapped_column(JSON, default=None)
    ai_analysis_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ai_priority_score: Mapped[float | None] = mapped_column(Float, index=True)
    ai_priority_reason: Mapped[str | None] = mapped_column(Text)
    ai_priority_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExportRun(Base):
    __tablename__ = "export_runs"
    __table_args__ = (Index("ix_export_runs_started_at", "started_at"),)
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        Identity(always=True),
        primary_key=True,
    )
    username: Mapped[str] = mapped_column(String(120), nullable=False)
    report: Mapped[str] = mapped_column(String(50), nullable=False)
    format: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    filters: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
