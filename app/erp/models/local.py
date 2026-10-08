"""Processos próprios do ERP: compras, estoque operacional e financeiro local."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
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

# --- Compras -----------------------------------------------------------------


class ErpSupplier(ErpBase):
    __tablename__ = "erp_suppliers"
    __table_args__ = (
        UniqueConstraint("connection_id", "code", name="uq_erp_suppliers_code"),
        Index("ix_erp_suppliers_name", "connection_id", "name"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(300))
    document: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(300))
    phone: Mapped[str | None] = mapped_column(String(60))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(5))
    payment_terms: Mapped[str | None] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ErpPurchaseOrder(ErpBase):
    __tablename__ = "erp_purchase_orders"
    __table_args__ = (
        UniqueConstraint("connection_id", "number", name="uq_erp_purchase_orders_number"),
        Index("ix_erp_purchase_orders_status", "connection_id", "status"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    number: Mapped[str] = mapped_column(String(40))
    supplier_id: Mapped[int] = mapped_column(
        BigId, ForeignKey("erp_suppliers.id"), index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="draft")
    expected_date: Mapped[date | None] = mapped_column(Date)
    total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str] = mapped_column(String(200))
    approved_by: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[str | None] = mapped_column(String(200))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ErpPurchaseOrderItem(ErpBase):
    __tablename__ = "erp_purchase_order_items"
    __table_args__ = (
        UniqueConstraint("purchase_order_id", "position", name="uq_erp_po_items_position"),
        CheckConstraint("received_quantity >= 0", name="received_non_negative"),
        CheckConstraint("received_quantity <= quantity", name="received_within_ordered"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    purchase_order_id: Mapped[int] = mapped_column(
        BigId, ForeignKey("erp_purchase_orders.id"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    product_external_id: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(400))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    received_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(18, 4))


class ErpPurchaseReceipt(ErpBase):
    __tablename__ = "erp_purchase_receipts"
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    purchase_order_id: Mapped[int] = mapped_column(
        BigId, ForeignKey("erp_purchase_orders.id"), index=True
    )
    warehouse_id: Mapped[int] = mapped_column(BigId, ForeignKey("erp_warehouses.id"))
    invoice_number: Mapped[str | None] = mapped_column(String(60))
    received_by: Mapped[str] = mapped_column(String(200))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    notes: Mapped[str | None] = mapped_column(Text)
    # Estorno de recebimento: no máximo um por recebimento (UPDATE condicional).
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversed_by: Mapped[str | None] = mapped_column(String(200))
    reverse_reason: Mapped[str | None] = mapped_column(Text)


class ErpPurchaseReceiptItem(ErpBase):
    __tablename__ = "erp_purchase_receipt_items"
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    receipt_id: Mapped[int] = mapped_column(
        BigId, ForeignKey("erp_purchase_receipts.id"), index=True
    )
    purchase_order_item_id: Mapped[int] = mapped_column(
        BigId, ForeignKey("erp_purchase_order_items.id")
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(18, 4))


# --- Estoque operacional -----------------------------------------------------


class ErpWarehouse(ErpBase):
    __tablename__ = "erp_warehouses"
    __table_args__ = (
        UniqueConstraint("connection_id", "code", name="uq_erp_warehouses_code"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ErpInventoryBalance(ErpBase):
    """Saldo derivado do razão. Constraints impedem saldo negativo."""

    __tablename__ = "erp_inventory_balances"
    __table_args__ = (
        UniqueConstraint(
            "warehouse_id", "product_external_id", name="uq_erp_balances_warehouse_product"
        ),
        CheckConstraint("on_hand >= 0", name="on_hand_non_negative"),
        CheckConstraint("reserved >= 0", name="reserved_non_negative"),
        CheckConstraint("reserved <= on_hand", name="reserved_within_on_hand"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    warehouse_id: Mapped[int] = mapped_column(BigId, ForeignKey("erp_warehouses.id"))
    product_external_id: Mapped[str] = mapped_column(String(200), index=True)
    on_hand: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    reserved: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ErpInventoryMovement(ErpBase):
    """Razão imutável. `causal_key` único garante efeito único por evento."""

    __tablename__ = "erp_inventory_movements"
    __table_args__ = (
        UniqueConstraint("connection_id", "causal_key", name="uq_erp_movements_causal"),
        Index("ix_erp_movements_product", "connection_id", "product_external_id"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    warehouse_id: Mapped[int] = mapped_column(BigId, ForeignKey("erp_warehouses.id"))
    product_external_id: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30))
    quantity_delta: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    reserved_delta: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    causal_key: Mapped[str] = mapped_column(String(160))
    reference_type: Mapped[str | None] = mapped_column(String(40))
    reference_id: Mapped[str | None] = mapped_column(String(80))
    reversal_of_id: Mapped[int | None] = mapped_column(BigId)
    operator: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ErpInventoryReservation(ErpBase):
    __tablename__ = "erp_inventory_reservations"
    __table_args__ = (
        UniqueConstraint("connection_id", "causal_key", name="uq_erp_reservations_causal"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    warehouse_id: Mapped[int] = mapped_column(BigId, ForeignKey("erp_warehouses.id"))
    product_external_id: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    status: Mapped[str] = mapped_column(String(20), default="active")
    order_external_id: Mapped[str | None] = mapped_column(String(200))
    causal_key: Mapped[str] = mapped_column(String(160))
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ErpInventoryAuthority(ErpBase):
    """Quem manda no saldo, por escopo. Padrão seguro: Mercos (consulta)."""

    __tablename__ = "erp_inventory_authority"
    __table_args__ = (
        UniqueConstraint("connection_id", "scope", name="uq_erp_inventory_authority_scope"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(80), default="")
    authority: Mapped[str] = mapped_column(String(10), default="mercos")
    cutover_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publish_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    set_by: Mapped[str | None] = mapped_column(String(200))
    reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# --- Financeiro local --------------------------------------------------------


class ErpFinAccount(ErpBase):
    __tablename__ = "erp_fin_accounts"
    __table_args__ = (
        UniqueConstraint("connection_id", "code", name="uq_erp_fin_accounts_code"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(10), default="bank")
    opening_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ErpFinCategory(ErpBase):
    __tablename__ = "erp_fin_categories"
    __table_args__ = (
        UniqueConstraint("connection_id", "code", name="uq_erp_fin_categories_code"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(10), default="expense")
    parent_id: Mapped[int | None] = mapped_column(BigId)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ErpCostCenter(ErpBase):
    __tablename__ = "erp_cost_centers"
    __table_args__ = (
        UniqueConstraint("connection_id", "code", name="uq_erp_cost_centers_code"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ErpFinTitle(ErpBase):
    """Título local (a pagar/receber). Diferente do título Mercos do cliente."""

    __tablename__ = "erp_fin_titles"
    __table_args__ = (
        UniqueConstraint("connection_id", "causal_key", name="uq_erp_fin_titles_causal"),
        Index("ix_erp_fin_titles_status", "connection_id", "kind", "status"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(12))
    number: Mapped[str | None] = mapped_column(String(60))
    description: Mapped[str] = mapped_column(String(400), default="")
    supplier_id: Mapped[int | None] = mapped_column(BigId, ForeignKey("erp_suppliers.id"))
    customer_external_id: Mapped[str | None] = mapped_column(String(200))
    category_id: Mapped[int | None] = mapped_column(BigId, ForeignKey("erp_fin_categories.id"))
    cost_center_id: Mapped[int | None] = mapped_column(BigId, ForeignKey("erp_cost_centers.id"))
    origin_type: Mapped[str | None] = mapped_column(String(40))
    origin_ref: Mapped[str | None] = mapped_column(String(80))
    causal_key: Mapped[str | None] = mapped_column(String(160))
    total: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    status: Mapped[str] = mapped_column(String(20), default="open")
    issued_date: Mapped[date | None] = mapped_column(Date)
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extra: Mapped[dict | None] = mapped_column(JSON)


class ErpFinInstallment(ErpBase):
    __tablename__ = "erp_fin_installments"
    __table_args__ = (
        UniqueConstraint("title_id", "number", name="uq_erp_fin_installments_number"),
        CheckConstraint("settled_amount >= 0", name="settled_non_negative"),
        CheckConstraint("settled_amount <= amount", name="settled_within_amount"),
        Index("ix_erp_fin_installments_due", "due_date"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    title_id: Mapped[int] = mapped_column(BigId, ForeignKey("erp_fin_titles.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    due_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    settled_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    status: Mapped[str] = mapped_column(String(20), default="open")


class ErpFinSettlement(ErpBase):
    """Baixas e estornos. Estorno é novo registro, nunca edição."""

    __tablename__ = "erp_fin_settlements"
    __table_args__ = (
        UniqueConstraint("connection_id", "causal_key", name="uq_erp_fin_settlements_causal"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    installment_id: Mapped[int] = mapped_column(
        BigId, ForeignKey("erp_fin_installments.id"), index=True
    )
    account_id: Mapped[int] = mapped_column(BigId, ForeignKey("erp_fin_accounts.id"))
    kind: Mapped[str] = mapped_column(String(12), default="payment")
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reversal_of_id: Mapped[int | None] = mapped_column(BigId)
    causal_key: Mapped[str] = mapped_column(String(160))
    reference: Mapped[str | None] = mapped_column(String(120))
    reason: Mapped[str | None] = mapped_column(Text)
    operator: Mapped[str] = mapped_column(String(200))
