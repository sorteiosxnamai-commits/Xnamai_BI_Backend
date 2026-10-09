"""Processos locais do painel operacional: frete, rascunho fiscal e reembolso.

Tudo aqui é registro interno do ERP. Cotação selecionada não é contratação, rascunho fiscal não é
nota emitida e solicitação de reembolso não é devolução: nenhuma tabela guarda chave fiscal,
protocolo, etiqueta ou comprovante bancário que o sistema não tenha de fato recebido.

As tabelas são aditivas (migração própria); nenhuma tabela existente é alterada.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.erp.common import utcnow
from app.erp.db import ErpBase
from app.erp.models.core import BigId

# --- Frete -------------------------------------------------------------------


class ErpShippingQuote(ErpBase):
    """Cotação de frete de UM pedido, atrelada à versão do pedido em que foi feita.

    Mudança posterior do pedido (fingerprint diferente) torna a cotação desatualizada: a seleção
    baseada nela deixa de valer até nova revisão."""

    __tablename__ = "erp_shipping_quotes"
    __table_args__ = (
        Index("ix_erp_shipping_quotes_order", "connection_id", "order_external_id"),
        # no máximo UMA cotação selecionada por pedido (protege a seleção duplicada/concorrente)
        Index(
            "uq_erp_shipping_quotes_selected",
            "connection_id",
            "order_external_id",
            unique=True,
            postgresql_where=text("status = 'selected'"),
            sqlite_where=text("status = 'selected'"),
        ),
        CheckConstraint("status IN ('draft','selected','superseded','cancelled')", name="ck_erp_shipping_quotes_status"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    order_external_id: Mapped[str] = mapped_column(String(200))
    order_version: Mapped[int] = mapped_column(Integer)
    order_fingerprint: Mapped[str | None] = mapped_column(String(64))
    origin_zip: Mapped[str | None] = mapped_column(String(12))
    destination: Mapped[dict | None] = mapped_column(JSON)  # {zip, city, state}; CEP é dado pessoal
    declared_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    items_snapshot: Mapped[list | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual | provider
    selected_option_id: Mapped[int | None] = mapped_column(Integer)
    selected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    selected_by: Mapped[str | None] = mapped_column(String(200))
    selection_reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)  # controle otimista da seleção
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ErpShippingVolume(ErpBase):
    __tablename__ = "erp_shipping_volumes"
    __table_args__ = (
        UniqueConstraint("quote_id", "position", name="uq_erp_shipping_volumes_position"),
        CheckConstraint("weight_kg > 0", name="ck_erp_shipping_volumes_weight"),
        CheckConstraint("length_cm > 0 AND width_cm > 0 AND height_cm > 0", name="ck_erp_shipping_volumes_dims"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    quote_id: Mapped[int] = mapped_column(BigId, ForeignKey("erp_shipping_quotes.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(10, 3))
    length_cm: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    width_cm: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    height_cm: Mapped[Decimal] = mapped_column(Numeric(8, 2))


class ErpShippingOption(ErpBase):
    """Opção de frete. `source='manual'` = cotação obtida fora do sistema e registrada por pessoa."""

    __tablename__ = "erp_shipping_options"
    __table_args__ = (CheckConstraint("price >= 0", name="ck_erp_shipping_options_price"),)
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    quote_id: Mapped[int] = mapped_column(BigId, ForeignKey("erp_shipping_quotes.id"), index=True)
    carrier: Mapped[str] = mapped_column(String(200))
    service: Mapped[str] = mapped_column(String(100))
    price: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    deadline_min_days: Mapped[int | None] = mapped_column(Integer)
    deadline_max_days: Mapped[int | None] = mapped_column(Integer)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tracking: Mapped[bool | None] = mapped_column(Boolean)
    pickup_mode: Mapped[str | None] = mapped_column(String(40))
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- Rascunho fiscal -----------------------------------------------------------


class ErpInvoiceDraft(ErpBase):
    """Montagem de nota fiscal: PLANEJAMENTO. Não emite NF-e e não guarda chave, protocolo,
    XML autorizado nem DANFE. Itens ficam ligados à origem e a um snapshot versionado."""

    __tablename__ = "erp_invoice_drafts"
    __table_args__ = (
        Index("ix_erp_invoice_drafts_order", "connection_id", "order_external_id"),
        CheckConstraint("status IN ('draft','cancelled')", name="ck_erp_invoice_drafts_status"),
        CheckConstraint("target_percent > 0 AND target_percent <= 100", name="ck_erp_invoice_drafts_percent"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    order_external_id: Mapped[str] = mapped_column(String(200))
    order_version: Mapped[int] = mapped_column(Integer)
    order_fingerprint: Mapped[str | None] = mapped_column(String(64))
    order_total: Mapped[Decimal] = mapped_column(Numeric(18, 2))  # valor total do pedido no snapshot
    target_percent: Mapped[Decimal] = mapped_column(Numeric(7, 4))
    target_value: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    notes: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[str | None] = mapped_column(String(200))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ErpInvoiceDraftItem(ErpBase):
    """Item do rascunho. `source_key` identifica o item NA ORIGEM (id do item no Mercos, ou
    produto#ocorrência); o id local do item do pedido é trocado a cada sincronização e não serve."""

    __tablename__ = "erp_invoice_draft_items"
    __table_args__ = (
        UniqueConstraint("draft_id", "source_key", name="uq_erp_invoice_draft_items_source"),
        Index("ix_erp_invoice_draft_items_source", "source_key"),
        CheckConstraint("quantity >= 0", name="ck_erp_invoice_draft_items_qty"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    draft_id: Mapped[int] = mapped_column(BigId, ForeignKey("erp_invoice_drafts.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    source_key: Mapped[str] = mapped_column(String(260))
    product_external_id: Mapped[str | None] = mapped_column(String(200))
    code: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str | None] = mapped_column(String(400))
    source_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4))  # quantidade total no pedido
    source_line_total: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    source_unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)  # alocada à nota
    line_value: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)  # calculado no backend


# --- Reembolso -----------------------------------------------------------------


class ErpRefundRequest(ErpBase):
    """Solicitação de reembolso ligada a uma baixa (pagamento) LOCAL de um título do pedido.

    Solicitar ou aprovar NÃO altera baixa nem comprova devolução bancária. Os únicos desfechos
    com efeito são: estorno local (serviço financeiro existente) e confirmação MANUAL de devolução
    externa (com referência informada por pessoa)."""

    __tablename__ = "erp_refund_requests"
    __table_args__ = (
        Index("ix_erp_refund_requests_order", "connection_id", "order_external_id"),
        Index("ix_erp_refund_requests_settlement", "connection_id", "settlement_id"),
        CheckConstraint("amount > 0", name="ck_erp_refund_requests_amount"),
        CheckConstraint(
            "status IN ('requested','approved','rejected','cancelled','external_confirmed','reversed_local')",
            name="ck_erp_refund_requests_status",
        ),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    order_external_id: Mapped[str] = mapped_column(String(200))
    settlement_id: Mapped[int] = mapped_column(BigId, ForeignKey("erp_fin_settlements.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="requested")
    version: Mapped[int] = mapped_column(Integer, default=1)
    requested_by: Mapped[str] = mapped_column(String(200))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_by: Mapped[str | None] = mapped_column(String(200))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    external_reference: Mapped[str | None] = mapped_column(String(200))
    external_confirmed_by: Mapped[str | None] = mapped_column(String(200))
    external_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversal_settlement_id: Mapped[int | None] = mapped_column(BigId)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ErpRefundEvent(ErpBase):
    """Histórico imutável da solicitação (quem, quando, o quê, por quê)."""

    __tablename__ = "erp_refund_events"
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    request_id: Mapped[int] = mapped_column(BigId, ForeignKey("erp_refund_requests.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    operator: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict | None] = mapped_column(JSON)
