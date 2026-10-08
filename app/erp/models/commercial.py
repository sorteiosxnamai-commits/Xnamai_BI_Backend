"""Entidades comerciais espelhadas do Mercos (12 recursos do Adaptor).

Identidade: (connection_id, external_id). O ID Mercos nunca é PK global.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.erp.common import utcnow
from app.erp.db import ErpBase
from app.erp.models.core import BigId


class SourceMixin:
    """Metadados de origem comuns a toda entidade espelhada."""

    connection_id: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str] = mapped_column(String(200))
    scope: Mapped[str] = mapped_column(String(80), default="")
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    version: Mapped[int] = mapped_column(Integer, default=1)


class ErpCustomer(SourceMixin, ErpBase):
    __tablename__ = "erp_customers"
    __table_args__ = (
        UniqueConstraint("connection_id", "external_id", name="uq_erp_customers_identity"),
        Index("ix_erp_customers_document", "connection_id", "document"),
        Index("ix_erp_customers_name", "connection_id", "name"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    trade_name: Mapped[str | None] = mapped_column(String(300))
    person_type: Mapped[str | None] = mapped_column(String(1))
    document: Mapped[str | None] = mapped_column(String(40))
    state_registration: Mapped[str | None] = mapped_column(String(40))
    suframa: Mapped[str | None] = mapped_column(String(40))
    street: Mapped[str | None] = mapped_column(String(300))
    number: Mapped[str | None] = mapped_column(String(40))
    complement: Mapped[str | None] = mapped_column(String(200))
    district: Mapped[str | None] = mapped_column(String(200))
    zip_code: Mapped[str | None] = mapped_column(String(20))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(5))
    email: Mapped[str | None] = mapped_column(String(300))
    phone: Mapped[str | None] = mapped_column(String(60))
    mobile: Mapped[str | None] = mapped_column(String(60))
    segment_external_id: Mapped[str | None] = mapped_column(String(200), index=True)
    seller_external_id: Mapped[str | None] = mapped_column(String(200), index=True)
    blocked: Mapped[bool | None] = mapped_column(Boolean)
    block_reason: Mapped[str | None] = mapped_column(Text)
    credit_limit: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    active: Mapped[bool | None] = mapped_column(Boolean)
    notes: Mapped[str | None] = mapped_column(Text)
    extras: Mapped[dict | None] = mapped_column(JSON)
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ErpCustomerContact(ErpBase):
    __tablename__ = "erp_customer_contacts"
    __table_args__ = (
        UniqueConstraint(
            "customer_id", "position", name="uq_erp_customer_contacts_position"
        ),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    customer_id: Mapped[int] = mapped_column(BigId, index=True)
    position: Mapped[int] = mapped_column(Integer)
    external_id: Mapped[str | None] = mapped_column(String(200))
    name: Mapped[str | None] = mapped_column(String(300))
    role: Mapped[str | None] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(300))
    phone: Mapped[str | None] = mapped_column(String(60))
    mobile: Mapped[str | None] = mapped_column(String(60))


class ErpCustomerAddress(ErpBase):
    __tablename__ = "erp_customer_addresses"
    __table_args__ = (
        UniqueConstraint(
            "customer_id", "position", name="uq_erp_customer_addresses_position"
        ),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    customer_id: Mapped[int] = mapped_column(BigId, index=True)
    position: Mapped[int] = mapped_column(Integer)
    external_id: Mapped[str | None] = mapped_column(String(200))
    kind: Mapped[str | None] = mapped_column(String(40))
    street: Mapped[str | None] = mapped_column(String(300))
    number: Mapped[str | None] = mapped_column(String(40))
    complement: Mapped[str | None] = mapped_column(String(200))
    district: Mapped[str | None] = mapped_column(String(200))
    zip_code: Mapped[str | None] = mapped_column(String(20))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(5))


class ErpProduct(SourceMixin, ErpBase):
    __tablename__ = "erp_products"
    __table_args__ = (
        UniqueConstraint("connection_id", "external_id", name="uq_erp_products_identity"),
        Index("ix_erp_products_code", "connection_id", "code"),
        Index("ix_erp_products_name", "connection_id", "name"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    code: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(400), default="")
    unit: Mapped[str | None] = mapped_column(String(30))
    category_external_id: Mapped[str | None] = mapped_column(String(200), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="simple")
    sellable: Mapped[bool] = mapped_column(Boolean, default=True)
    list_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    minimum_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    external_stock: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    commission_percent: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    ipi_percent: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    ncm: Mapped[str | None] = mapped_column(String(20))
    multiple: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    gross_weight: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    width: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    height: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    length: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    active: Mapped[bool | None] = mapped_column(Boolean)
    image_hashes: Mapped[list | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text)
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ErpProductVariant(ErpBase):
    __tablename__ = "erp_product_variants"
    __table_args__ = (
        UniqueConstraint(
            "product_id", "position", name="uq_erp_product_variants_position"
        ),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    product_id: Mapped[int] = mapped_column(BigId, index=True)
    position: Mapped[int] = mapped_column(Integer)
    external_id: Mapped[str | None] = mapped_column(String(200))
    code: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str | None] = mapped_column(String(400))
    attributes: Mapped[dict | None] = mapped_column(JSON)
    external_stock: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))


class ErpProductPrice(SourceMixin, ErpBase):
    """Preço por produto/tabela. Chave externa: (produto_id, tabela_preco_id)."""

    __tablename__ = "erp_product_prices"
    __table_args__ = (
        UniqueConstraint("connection_id", "external_id", name="uq_erp_product_prices_identity"),
        Index("ix_erp_product_prices_product", "connection_id", "product_external_id"),
        Index("ix_erp_product_prices_table", "connection_id", "price_table_external_id"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    product_external_id: Mapped[str] = mapped_column(String(200))
    price_table_external_id: Mapped[str] = mapped_column(String(200))
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))


class ErpSalesOrder(SourceMixin, ErpBase):
    __tablename__ = "erp_sales_orders"
    __table_args__ = (
        UniqueConstraint("connection_id", "external_id", name="uq_erp_sales_orders_identity"),
        Index("ix_erp_sales_orders_issued", "connection_id", "issued_at"),
        Index("ix_erp_sales_orders_customer", "connection_id", "customer_external_id"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    number: Mapped[str | None] = mapped_column(String(100), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="order")
    commercial_status: Mapped[str | None] = mapped_column(String(50), index=True)
    billing_status: Mapped[str | None] = mapped_column(String(50))
    fulfillment_status: Mapped[str] = mapped_column(String(30), default="not_started")
    payment_status: Mapped[str | None] = mapped_column(String(50))
    customer_external_id: Mapped[str | None] = mapped_column(String(200))
    seller_external_id: Mapped[str | None] = mapped_column(String(200), index=True)
    order_type_external_id: Mapped[str | None] = mapped_column(String(200))
    payment_condition_external_id: Mapped[str | None] = mapped_column(String(200))
    price_table_external_id: Mapped[str | None] = mapped_column(String(200))
    carrier_external_id: Mapped[str | None] = mapped_column(String(200))
    commercial_policy_external_id: Mapped[str | None] = mapped_column(String(200))
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issue_date: Mapped[date | None] = mapped_column(Date)
    expected_delivery_date: Mapped[date | None] = mapped_column(Date)
    gross_total: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    discount_total: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    freight_total: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    net_total: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    items_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    item_count: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    shipping_address: Mapped[dict | None] = mapped_column(JSON)
    extras: Mapped[dict | None] = mapped_column(JSON)
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ErpSalesOrderItem(ErpBase):
    __tablename__ = "erp_sales_order_items"
    __table_args__ = (
        UniqueConstraint("order_id", "position", name="uq_erp_sales_order_items_position"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    order_id: Mapped[int] = mapped_column(BigId, index=True)
    position: Mapped[int] = mapped_column(Integer)
    external_id: Mapped[str | None] = mapped_column(String(200))
    product_external_id: Mapped[str | None] = mapped_column(String(200), index=True)
    code: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str | None] = mapped_column(String(400))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    list_unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    discount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    total: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)


class CatalogMixin(SourceMixin):
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    active: Mapped[bool | None] = mapped_column(Boolean)


def _identity(table: str) -> UniqueConstraint:
    return UniqueConstraint("connection_id", "external_id", name=f"uq_{table}_identity")


class ErpSeller(CatalogMixin, ErpBase):
    """Vendedor Mercos. Não é operador de login do ERP."""

    __tablename__ = "erp_sellers"
    __table_args__ = (_identity("erp_sellers"),)
    email: Mapped[str | None] = mapped_column(String(300))
    phone: Mapped[str | None] = mapped_column(String(60))  # restrito: não sai na API
    is_admin: Mapped[bool | None] = mapped_column(Boolean)
    access_blocked: Mapped[bool | None] = mapped_column(Boolean)


class ErpPriceTable(CatalogMixin, ErpBase):
    __tablename__ = "erp_price_tables"
    __table_args__ = (_identity("erp_price_tables"),)
    price_type: Mapped[str | None] = mapped_column(String(20))
    percentage: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    surcharge_percent: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    discount_percent: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    represented_external_id: Mapped[str | None] = mapped_column(String(200), index=True)


class ErpPaymentCondition(CatalogMixin, ErpBase):
    __tablename__ = "erp_payment_conditions"
    __table_args__ = (_identity("erp_payment_conditions"),)
    minimum_order_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    consider_credit_limit: Mapped[bool | None] = mapped_column(Boolean)
    available_b2b: Mapped[bool | None] = mapped_column(Boolean)
    # representada/divisão: escopo da conta; preservado para filtros e autorização
    represented_external_id: Mapped[str | None] = mapped_column(String(200), index=True)


class ErpCarrier(CatalogMixin, ErpBase):
    __tablename__ = "erp_carriers"
    __table_args__ = (_identity("erp_carriers"),)
    document: Mapped[str | None] = mapped_column(String(40))
    phone: Mapped[str | None] = mapped_column(String(60))
    email: Mapped[str | None] = mapped_column(String(300))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(5))


class ErpCommercialPolicy(CatalogMixin, ErpBase):
    """Identidade por slug + conta; IDs anteriores ficam no histórico."""

    __tablename__ = "erp_commercial_policies"
    __table_args__ = (_identity("erp_commercial_policies"),)
    slug: Mapped[str | None] = mapped_column(String(200), index=True)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    id_history: Mapped[list | None] = mapped_column(JSON)


class ErpCategory(CatalogMixin, ErpBase):
    __tablename__ = "erp_categories"
    __table_args__ = (_identity("erp_categories"),)
    parent_external_id: Mapped[str | None] = mapped_column(String(200), index=True)
    represented_external_id: Mapped[str | None] = mapped_column(String(200), index=True)


class ErpSegment(CatalogMixin, ErpBase):
    __tablename__ = "erp_segments"
    __table_args__ = (_identity("erp_segments"),)


class ErpOrderType(CatalogMixin, ErpBase):
    __tablename__ = "erp_order_types"
    __table_args__ = (_identity("erp_order_types"),)


class ErpExternalTitle(SourceMixin, ErpBase):
    """Título/fatura do cliente no Mercos. Não é o financeiro da empresa."""

    __tablename__ = "erp_external_titles"
    __table_args__ = (
        UniqueConstraint("connection_id", "external_id", name="uq_erp_external_titles_identity"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    customer_external_id: Mapped[str | None] = mapped_column(String(200))
    order_external_id: Mapped[str | None] = mapped_column(String(200))
    number: Mapped[str | None] = mapped_column(String(100))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    due_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str | None] = mapped_column(String(50))
    managed_by_erp: Mapped[bool] = mapped_column(Boolean, default=False)
    payment_link: Mapped[str | None] = mapped_column(Text)


class ErpExternalPayment(SourceMixin, ErpBase):
    """Estado externo de pagamento (Mercos Pay). Confirmação não é repasse."""

    __tablename__ = "erp_external_payments"
    __table_args__ = (
        UniqueConstraint("connection_id", "external_id", name="uq_erp_external_payments_identity"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    title_external_id: Mapped[str | None] = mapped_column(String(200))
    order_external_id: Mapped[str | None] = mapped_column(String(200))
    method: Mapped[str | None] = mapped_column(String(50))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    status: Mapped[str | None] = mapped_column(String(50))
    settlement_status: Mapped[str | None] = mapped_column(String(50))
    chargeback: Mapped[bool] = mapped_column(Boolean, default=False)
    payment_token: Mapped[str | None] = mapped_column(Text)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
