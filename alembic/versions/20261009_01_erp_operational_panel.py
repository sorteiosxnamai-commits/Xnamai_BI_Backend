"""ERP painel operacional: frete, rascunho fiscal e reembolso (tabelas novas).

Revision ID: 20261009_01
Revises: 20261007_01

Somente estruturas NOVAS e aditivas (erp_shipping_*, erp_invoice_draft*, erp_refund_*).
Nenhuma tabela existente (legada ou ERP) é alterada. O downgrade remove apenas estas
tabelas novas; os dados delas se perdem nesse caso, então use-o só em ambiente sem dados reais.
"""

from alembic import op
import sqlalchemy as sa


revision = "20261009_01"
down_revision = "20261007_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('erp_invoice_drafts',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('order_external_id', sa.String(length=200), nullable=False),
    sa.Column('order_version', sa.Integer(), nullable=False),
    sa.Column('order_fingerprint', sa.String(length=64), nullable=True),
    sa.Column('order_total', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('target_percent', sa.Numeric(precision=7, scale=4), nullable=False),
    sa.Column('target_value', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_by', sa.String(length=200), nullable=True),
    sa.Column('cancel_reason', sa.Text(), nullable=True),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status IN ('draft','cancelled')", name=op.f('ck_erp_invoice_drafts_ck_erp_invoice_drafts_status')),
    sa.CheckConstraint('target_percent > 0 AND target_percent <= 100', name=op.f('ck_erp_invoice_drafts_ck_erp_invoice_drafts_percent')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_invoice_drafts'))
    )
    op.create_index('ix_erp_invoice_drafts_order', 'erp_invoice_drafts', ['connection_id', 'order_external_id'], unique=False)
    op.create_table('erp_shipping_quotes',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('order_external_id', sa.String(length=200), nullable=False),
    sa.Column('order_version', sa.Integer(), nullable=False),
    sa.Column('order_fingerprint', sa.String(length=64), nullable=True),
    sa.Column('origin_zip', sa.String(length=12), nullable=True),
    sa.Column('destination', sa.JSON(), nullable=True),
    sa.Column('declared_value', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('items_snapshot', sa.JSON(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('selected_option_id', sa.Integer(), nullable=True),
    sa.Column('selected_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('selected_by', sa.String(length=200), nullable=True),
    sa.Column('selection_reason', sa.Text(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status IN ('draft','selected','superseded','cancelled')", name=op.f('ck_erp_shipping_quotes_ck_erp_shipping_quotes_status')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_shipping_quotes'))
    )
    op.create_index('ix_erp_shipping_quotes_order', 'erp_shipping_quotes', ['connection_id', 'order_external_id'], unique=False)
    op.create_index('uq_erp_shipping_quotes_selected', 'erp_shipping_quotes', ['connection_id', 'order_external_id'], unique=True, postgresql_where=sa.text("status = 'selected'"), sqlite_where=sa.text("status = 'selected'"))
    op.create_table('erp_invoice_draft_items',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('draft_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('source_key', sa.String(length=260), nullable=False),
    sa.Column('product_external_id', sa.String(length=200), nullable=True),
    sa.Column('code', sa.String(length=100), nullable=True),
    sa.Column('name', sa.String(length=400), nullable=True),
    sa.Column('source_quantity', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('source_line_total', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('source_unit_price', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('quantity', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('line_value', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.CheckConstraint('quantity >= 0', name=op.f('ck_erp_invoice_draft_items_ck_erp_invoice_draft_items_qty')),
    sa.ForeignKeyConstraint(['draft_id'], ['erp_invoice_drafts.id'], name=op.f('fk_erp_invoice_draft_items_draft_id_erp_invoice_drafts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_invoice_draft_items')),
    sa.UniqueConstraint('draft_id', 'source_key', name='uq_erp_invoice_draft_items_source')
    )
    op.create_index(op.f('ix_erp_invoice_draft_items_draft_id'), 'erp_invoice_draft_items', ['draft_id'], unique=False)
    op.create_index('ix_erp_invoice_draft_items_source', 'erp_invoice_draft_items', ['source_key'], unique=False)
    op.create_table('erp_shipping_options',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('quote_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('carrier', sa.String(length=200), nullable=False),
    sa.Column('service', sa.String(length=100), nullable=False),
    sa.Column('price', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('deadline_min_days', sa.Integer(), nullable=True),
    sa.Column('deadline_max_days', sa.Integer(), nullable=True),
    sa.Column('valid_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('tracking', sa.Boolean(), nullable=True),
    sa.Column('pickup_mode', sa.String(length=40), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('price >= 0', name=op.f('ck_erp_shipping_options_ck_erp_shipping_options_price')),
    sa.ForeignKeyConstraint(['quote_id'], ['erp_shipping_quotes.id'], name=op.f('fk_erp_shipping_options_quote_id_erp_shipping_quotes')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_shipping_options'))
    )
    op.create_index(op.f('ix_erp_shipping_options_quote_id'), 'erp_shipping_options', ['quote_id'], unique=False)
    op.create_table('erp_shipping_volumes',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('quote_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('weight_kg', sa.Numeric(precision=10, scale=3), nullable=False),
    sa.Column('length_cm', sa.Numeric(precision=8, scale=2), nullable=False),
    sa.Column('width_cm', sa.Numeric(precision=8, scale=2), nullable=False),
    sa.Column('height_cm', sa.Numeric(precision=8, scale=2), nullable=False),
    sa.CheckConstraint('length_cm > 0 AND width_cm > 0 AND height_cm > 0', name=op.f('ck_erp_shipping_volumes_ck_erp_shipping_volumes_dims')),
    sa.CheckConstraint('weight_kg > 0', name=op.f('ck_erp_shipping_volumes_ck_erp_shipping_volumes_weight')),
    sa.ForeignKeyConstraint(['quote_id'], ['erp_shipping_quotes.id'], name=op.f('fk_erp_shipping_volumes_quote_id_erp_shipping_quotes')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_shipping_volumes')),
    sa.UniqueConstraint('quote_id', 'position', name='uq_erp_shipping_volumes_position')
    )
    op.create_index(op.f('ix_erp_shipping_volumes_quote_id'), 'erp_shipping_volumes', ['quote_id'], unique=False)
    op.create_table('erp_refund_requests',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('order_external_id', sa.String(length=200), nullable=False),
    sa.Column('settlement_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('amount', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('requested_by', sa.String(length=200), nullable=False),
    sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('decided_by', sa.String(length=200), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decision_note', sa.Text(), nullable=True),
    sa.Column('external_reference', sa.String(length=200), nullable=True),
    sa.Column('external_confirmed_by', sa.String(length=200), nullable=True),
    sa.Column('external_confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reversal_settlement_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status IN ('requested','approved','rejected','cancelled','external_confirmed','reversed_local')", name=op.f('ck_erp_refund_requests_ck_erp_refund_requests_status')),
    sa.CheckConstraint('amount > 0', name=op.f('ck_erp_refund_requests_ck_erp_refund_requests_amount')),
    sa.ForeignKeyConstraint(['settlement_id'], ['erp_fin_settlements.id'], name=op.f('fk_erp_refund_requests_settlement_id_erp_fin_settlements')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_refund_requests'))
    )
    op.create_index('ix_erp_refund_requests_order', 'erp_refund_requests', ['connection_id', 'order_external_id'], unique=False)
    op.create_index('ix_erp_refund_requests_settlement', 'erp_refund_requests', ['connection_id', 'settlement_id'], unique=False)
    op.create_table('erp_refund_events',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('request_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('operator', sa.String(length=200), nullable=False),
    sa.Column('action', sa.String(length=40), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('detail', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['request_id'], ['erp_refund_requests.id'], name=op.f('fk_erp_refund_events_request_id_erp_refund_requests')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_refund_events'))
    )
    op.create_index(op.f('ix_erp_refund_events_request_id'), 'erp_refund_events', ['request_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_erp_refund_events_request_id'), table_name='erp_refund_events')
    op.drop_table('erp_refund_events')
    op.drop_index('ix_erp_refund_requests_settlement', table_name='erp_refund_requests')
    op.drop_index('ix_erp_refund_requests_order', table_name='erp_refund_requests')
    op.drop_table('erp_refund_requests')
    op.drop_index(op.f('ix_erp_shipping_volumes_quote_id'), table_name='erp_shipping_volumes')
    op.drop_table('erp_shipping_volumes')
    op.drop_index(op.f('ix_erp_shipping_options_quote_id'), table_name='erp_shipping_options')
    op.drop_table('erp_shipping_options')
    op.drop_index('ix_erp_invoice_draft_items_source', table_name='erp_invoice_draft_items')
    op.drop_index(op.f('ix_erp_invoice_draft_items_draft_id'), table_name='erp_invoice_draft_items')
    op.drop_table('erp_invoice_draft_items')
    op.drop_index('uq_erp_shipping_quotes_selected', table_name='erp_shipping_quotes', postgresql_where=sa.text("status = 'selected'"), sqlite_where=sa.text("status = 'selected'"))
    op.drop_index('ix_erp_shipping_quotes_order', table_name='erp_shipping_quotes')
    op.drop_table('erp_shipping_quotes')
    op.drop_index('ix_erp_invoice_drafts_order', table_name='erp_invoice_drafts')
    op.drop_table('erp_invoice_drafts')
