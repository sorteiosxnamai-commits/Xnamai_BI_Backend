"""ERP Xnamai: tabelas erp_* (metadata separado do legado).

Revision ID: 20261007_01
Revises: 20260831_02

Somente estruturas novas. Nenhuma tabela legada de BI/CRM/varejo é alterada.
O downgrade remove apenas tabelas erp_*; dados financeiros do ERP são
destruídos nesse caso, por isso use-o somente em ambientes sem dados reais.
"""

from alembic import op
import sqlalchemy as sa


revision = "20261007_01"
down_revision = "20260831_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('erp_audit_events',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=True),
    sa.Column('operator', sa.String(length=200), nullable=False),
    sa.Column('action', sa.String(length=80), nullable=False),
    sa.Column('resource', sa.String(length=50), nullable=True),
    sa.Column('resource_id', sa.String(length=200), nullable=True),
    sa.Column('correlation_id', sa.String(length=64), nullable=True),
    sa.Column('result', sa.String(length=30), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('detail', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_audit_events'))
    )
    op.create_index('ix_erp_audit_at', 'erp_audit_events', ['at'], unique=False)
    op.create_table('erp_capabilities',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('key', sa.String(length=80), nullable=False),
    sa.Column('supported_by_adaptor', sa.Boolean(), nullable=False),
    sa.Column('documented_by_provider', sa.Boolean(), nullable=False),
    sa.Column('account_access', sa.String(length=12), nullable=False),
    sa.Column('implemented_in_erp', sa.Boolean(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('last_validated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_capabilities')),
    sa.UniqueConstraint('connection_id', 'key', name='uq_erp_capabilities_conn_key')
    )
    op.create_index(op.f('ix_erp_capabilities_connection_id'), 'erp_capabilities', ['connection_id'], unique=False)
    op.create_table('erp_carriers',
    sa.Column('document', sa.String(length=40), nullable=True),
    sa.Column('phone', sa.String(length=60), nullable=True),
    sa.Column('email', sa.String(length=300), nullable=True),
    sa.Column('city', sa.String(length=120), nullable=True),
    sa.Column('state', sa.String(length=5), nullable=True),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('name', sa.String(length=300), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=True),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('scope', sa.String(length=80), nullable=False),
    sa.Column('source_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source_deleted', sa.Boolean(), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=True),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_carriers')),
    sa.UniqueConstraint('connection_id', 'external_id', name='uq_erp_carriers_identity')
    )
    op.create_table('erp_categories',
    sa.Column('parent_external_id', sa.String(length=200), nullable=True),
    sa.Column('represented_external_id', sa.String(length=200), nullable=True),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('name', sa.String(length=300), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=True),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('scope', sa.String(length=80), nullable=False),
    sa.Column('source_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source_deleted', sa.Boolean(), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=True),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_categories')),
    sa.UniqueConstraint('connection_id', 'external_id', name='uq_erp_categories_identity')
    )
    op.create_index(op.f('ix_erp_categories_parent_external_id'), 'erp_categories', ['parent_external_id'], unique=False)
    op.create_index(op.f('ix_erp_categories_represented_external_id'), 'erp_categories', ['represented_external_id'], unique=False)
    op.create_table('erp_commercial_policies',
    sa.Column('slug', sa.String(length=200), nullable=True),
    sa.Column('valid_from', sa.Date(), nullable=True),
    sa.Column('valid_to', sa.Date(), nullable=True),
    sa.Column('id_history', sa.JSON(), nullable=True),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('name', sa.String(length=300), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=True),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('scope', sa.String(length=80), nullable=False),
    sa.Column('source_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source_deleted', sa.Boolean(), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=True),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_commercial_policies')),
    sa.UniqueConstraint('connection_id', 'external_id', name='uq_erp_commercial_policies_identity')
    )
    op.create_index(op.f('ix_erp_commercial_policies_slug'), 'erp_commercial_policies', ['slug'], unique=False)
    op.create_table('erp_conflicts',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('operation_id', sa.String(length=36), nullable=True),
    sa.Column('entity_type', sa.String(length=50), nullable=False),
    sa.Column('entity_external_id', sa.String(length=200), nullable=True),
    sa.Column('fields', sa.JSON(), nullable=False),
    sa.Column('base', sa.JSON(), nullable=True),
    sa.Column('local', sa.JSON(), nullable=True),
    sa.Column('external', sa.JSON(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('resolution', sa.String(length=30), nullable=True),
    sa.Column('resolved_by', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_conflicts'))
    )
    op.create_index(op.f('ix_erp_conflicts_operation_id'), 'erp_conflicts', ['operation_id'], unique=False)
    op.create_index(op.f('ix_erp_conflicts_status'), 'erp_conflicts', ['status'], unique=False)
    op.create_table('erp_connections',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_connections')),
    sa.UniqueConstraint('connection_id', name=op.f('uq_erp_connections_connection_id'))
    )
    op.create_table('erp_cost_centers',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('code', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_cost_centers')),
    sa.UniqueConstraint('connection_id', 'code', name='uq_erp_cost_centers_code')
    )
    op.create_table('erp_customer_addresses',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('customer_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=True),
    sa.Column('kind', sa.String(length=40), nullable=True),
    sa.Column('street', sa.String(length=300), nullable=True),
    sa.Column('number', sa.String(length=40), nullable=True),
    sa.Column('complement', sa.String(length=200), nullable=True),
    sa.Column('district', sa.String(length=200), nullable=True),
    sa.Column('zip_code', sa.String(length=20), nullable=True),
    sa.Column('city', sa.String(length=120), nullable=True),
    sa.Column('state', sa.String(length=5), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_customer_addresses')),
    sa.UniqueConstraint('customer_id', 'position', name='uq_erp_customer_addresses_position')
    )
    op.create_index(op.f('ix_erp_customer_addresses_customer_id'), 'erp_customer_addresses', ['customer_id'], unique=False)
    op.create_table('erp_customer_contacts',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('customer_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=True),
    sa.Column('name', sa.String(length=300), nullable=True),
    sa.Column('role', sa.String(length=120), nullable=True),
    sa.Column('email', sa.String(length=300), nullable=True),
    sa.Column('phone', sa.String(length=60), nullable=True),
    sa.Column('mobile', sa.String(length=60), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_customer_contacts')),
    sa.UniqueConstraint('customer_id', 'position', name='uq_erp_customer_contacts_position')
    )
    op.create_index(op.f('ix_erp_customer_contacts_customer_id'), 'erp_customer_contacts', ['customer_id'], unique=False)
    op.create_table('erp_customers',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('name', sa.String(length=300), nullable=False),
    sa.Column('trade_name', sa.String(length=300), nullable=True),
    sa.Column('person_type', sa.String(length=1), nullable=True),
    sa.Column('document', sa.String(length=40), nullable=True),
    sa.Column('state_registration', sa.String(length=40), nullable=True),
    sa.Column('suframa', sa.String(length=40), nullable=True),
    sa.Column('street', sa.String(length=300), nullable=True),
    sa.Column('number', sa.String(length=40), nullable=True),
    sa.Column('complement', sa.String(length=200), nullable=True),
    sa.Column('district', sa.String(length=200), nullable=True),
    sa.Column('zip_code', sa.String(length=20), nullable=True),
    sa.Column('city', sa.String(length=120), nullable=True),
    sa.Column('state', sa.String(length=5), nullable=True),
    sa.Column('email', sa.String(length=300), nullable=True),
    sa.Column('phone', sa.String(length=60), nullable=True),
    sa.Column('mobile', sa.String(length=60), nullable=True),
    sa.Column('segment_external_id', sa.String(length=200), nullable=True),
    sa.Column('seller_external_id', sa.String(length=200), nullable=True),
    sa.Column('blocked', sa.Boolean(), nullable=True),
    sa.Column('block_reason', sa.Text(), nullable=True),
    sa.Column('credit_limit', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('extras', sa.JSON(), nullable=True),
    sa.Column('source_created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('scope', sa.String(length=80), nullable=False),
    sa.Column('source_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source_deleted', sa.Boolean(), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=True),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_customers')),
    sa.UniqueConstraint('connection_id', 'external_id', name='uq_erp_customers_identity')
    )
    op.create_index('ix_erp_customers_document', 'erp_customers', ['connection_id', 'document'], unique=False)
    op.create_index('ix_erp_customers_name', 'erp_customers', ['connection_id', 'name'], unique=False)
    op.create_index(op.f('ix_erp_customers_segment_external_id'), 'erp_customers', ['segment_external_id'], unique=False)
    op.create_index(op.f('ix_erp_customers_seller_external_id'), 'erp_customers', ['seller_external_id'], unique=False)
    op.create_table('erp_external_payments',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('title_external_id', sa.String(length=200), nullable=True),
    sa.Column('order_external_id', sa.String(length=200), nullable=True),
    sa.Column('method', sa.String(length=50), nullable=True),
    sa.Column('amount', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('status', sa.String(length=50), nullable=True),
    sa.Column('settlement_status', sa.String(length=50), nullable=True),
    sa.Column('chargeback', sa.Boolean(), nullable=False),
    sa.Column('payment_token', sa.Text(), nullable=True),
    sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('scope', sa.String(length=80), nullable=False),
    sa.Column('source_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source_deleted', sa.Boolean(), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=True),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_external_payments')),
    sa.UniqueConstraint('connection_id', 'external_id', name='uq_erp_external_payments_identity')
    )
    op.create_table('erp_external_titles',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('customer_external_id', sa.String(length=200), nullable=True),
    sa.Column('order_external_id', sa.String(length=200), nullable=True),
    sa.Column('number', sa.String(length=100), nullable=True),
    sa.Column('amount', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('due_date', sa.Date(), nullable=True),
    sa.Column('status', sa.String(length=50), nullable=True),
    sa.Column('managed_by_erp', sa.Boolean(), nullable=False),
    sa.Column('payment_link', sa.Text(), nullable=True),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('scope', sa.String(length=80), nullable=False),
    sa.Column('source_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source_deleted', sa.Boolean(), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=True),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_external_titles')),
    sa.UniqueConstraint('connection_id', 'external_id', name='uq_erp_external_titles_identity')
    )
    op.create_table('erp_field_inventory',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('resource', sa.String(length=50), nullable=False),
    sa.Column('source_key', sa.String(length=160), nullable=False),
    sa.Column('mapped', sa.Boolean(), nullable=False),
    sa.Column('seen_count', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sample_type', sa.String(length=20), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_field_inventory')),
    sa.UniqueConstraint('connection_id', 'resource', 'source_key', name='uq_erp_field_inventory_key')
    )
    op.create_table('erp_fin_accounts',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('code', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('opening_balance', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_fin_accounts')),
    sa.UniqueConstraint('connection_id', 'code', name='uq_erp_fin_accounts_code')
    )
    op.create_table('erp_fin_categories',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('code', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('parent_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_fin_categories')),
    sa.UniqueConstraint('connection_id', 'code', name='uq_erp_fin_categories_code')
    )
    op.create_table('erp_idempotency_keys',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('operator', sa.String(length=200), nullable=False),
    sa.Column('operation', sa.String(length=80), nullable=False),
    sa.Column('key', sa.String(length=120), nullable=False),
    sa.Column('payload_hash', sa.String(length=64), nullable=False),
    sa.Column('response_status', sa.Integer(), nullable=True),
    sa.Column('response_body', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_idempotency_keys')),
    sa.UniqueConstraint('connection_id', 'operator', 'operation', 'key', name='uq_erp_idempotency_identity')
    )
    op.create_table('erp_integration_operations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('operator', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=50), nullable=False),
    sa.Column('target_resource', sa.String(length=50), nullable=False),
    sa.Column('target_external_id', sa.String(length=200), nullable=True),
    sa.Column('local_ref', sa.String(length=80), nullable=True),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('idempotency_key', sa.String(length=120), nullable=False),
    sa.Column('payload_hash', sa.String(length=64), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('base_fingerprint', sa.String(length=64), nullable=True),
    sa.Column('base_snapshot', sa.JSON(), nullable=True),
    sa.Column('expected_version', sa.Integer(), nullable=True),
    sa.Column('response_status', sa.Integer(), nullable=True),
    sa.Column('response_body', sa.JSON(), nullable=True),
    sa.Column('external_id', sa.String(length=200), nullable=True),
    sa.Column('error_code', sa.String(length=60), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('lease_token', sa.String(length=36), nullable=True),
    sa.Column('leased_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('dispatch_started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('mirror_confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reconcile_evidence', sa.JSON(), nullable=True),
    sa.Column('correlation_id', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_integration_operations')),
    sa.UniqueConstraint('connection_id', 'operator', 'kind', 'idempotency_key', name='uq_erp_operations_idempotency')
    )
    op.create_index('ix_erp_operations_pick', 'erp_integration_operations', ['status', 'next_attempt_at'], unique=False)
    op.create_table('erp_inventory_authority',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('scope', sa.String(length=80), nullable=False),
    sa.Column('authority', sa.String(length=10), nullable=False),
    sa.Column('cutover_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reconciled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('publish_enabled', sa.Boolean(), nullable=False),
    sa.Column('set_by', sa.String(length=200), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_inventory_authority')),
    sa.UniqueConstraint('connection_id', 'scope', name='uq_erp_inventory_authority_scope')
    )
    op.create_table('erp_jobs',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('resource', sa.String(length=50), nullable=True),
    sa.Column('mode', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('lease_token', sa.String(length=36), nullable=True),
    sa.Column('leased_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('run_after', sa.DateTime(timezone=True), nullable=False),
    sa.Column('requested_by', sa.String(length=200), nullable=True),
    sa.Column('result', sa.JSON(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('cancel_requested', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_jobs'))
    )
    op.create_index('ix_erp_jobs_pick', 'erp_jobs', ['status', 'run_after'], unique=False)
    op.create_index('uq_erp_jobs_active', 'erp_jobs', ['kind', 'connection_id', 'resource', 'mode'], unique=True, postgresql_where=sa.text("status IN ('queued','processing','waiting_rate_limit')"), sqlite_where=sa.text("status IN ('queued','processing','waiting_rate_limit')"))
    op.create_table('erp_operators',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('username', sa.String(length=200), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=False),
    sa.Column('roles', sa.JSON(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('password_hash', sa.String(length=300), nullable=True),
    sa.Column('must_change_password', sa.Boolean(), nullable=False),
    sa.Column('password_changed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('failed_attempts', sa.Integer(), nullable=False),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_operators')),
    sa.UniqueConstraint('username', name=op.f('uq_erp_operators_username'))
    )
    op.create_table('erp_order_types',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('name', sa.String(length=300), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=True),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('scope', sa.String(length=80), nullable=False),
    sa.Column('source_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source_deleted', sa.Boolean(), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=True),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_order_types')),
    sa.UniqueConstraint('connection_id', 'external_id', name='uq_erp_order_types_identity')
    )
    op.create_table('erp_payment_conditions',
    sa.Column('minimum_order_value', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('consider_credit_limit', sa.Boolean(), nullable=True),
    sa.Column('available_b2b', sa.Boolean(), nullable=True),
    sa.Column('represented_external_id', sa.String(length=200), nullable=True),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('name', sa.String(length=300), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=True),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('scope', sa.String(length=80), nullable=False),
    sa.Column('source_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source_deleted', sa.Boolean(), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=True),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_payment_conditions')),
    sa.UniqueConstraint('connection_id', 'external_id', name='uq_erp_payment_conditions_identity')
    )
    op.create_index(op.f('ix_erp_payment_conditions_represented_external_id'), 'erp_payment_conditions', ['represented_external_id'], unique=False)
    op.create_table('erp_price_tables',
    sa.Column('price_type', sa.String(length=20), nullable=True),
    sa.Column('percentage', sa.Numeric(precision=9, scale=4), nullable=True),
    sa.Column('surcharge_percent', sa.Numeric(precision=9, scale=4), nullable=True),
    sa.Column('discount_percent', sa.Numeric(precision=9, scale=4), nullable=True),
    sa.Column('represented_external_id', sa.String(length=200), nullable=True),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('name', sa.String(length=300), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=True),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('scope', sa.String(length=80), nullable=False),
    sa.Column('source_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source_deleted', sa.Boolean(), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=True),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_price_tables')),
    sa.UniqueConstraint('connection_id', 'external_id', name='uq_erp_price_tables_identity')
    )
    op.create_index(op.f('ix_erp_price_tables_represented_external_id'), 'erp_price_tables', ['represented_external_id'], unique=False)
    op.create_table('erp_product_prices',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('product_external_id', sa.String(length=200), nullable=False),
    sa.Column('price_table_external_id', sa.String(length=200), nullable=False),
    sa.Column('price', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('scope', sa.String(length=80), nullable=False),
    sa.Column('source_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source_deleted', sa.Boolean(), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=True),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_product_prices')),
    sa.UniqueConstraint('connection_id', 'external_id', name='uq_erp_product_prices_identity')
    )
    op.create_index('ix_erp_product_prices_product', 'erp_product_prices', ['connection_id', 'product_external_id'], unique=False)
    op.create_index('ix_erp_product_prices_table', 'erp_product_prices', ['connection_id', 'price_table_external_id'], unique=False)
    op.create_table('erp_product_variants',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('product_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=True),
    sa.Column('code', sa.String(length=100), nullable=True),
    sa.Column('name', sa.String(length=400), nullable=True),
    sa.Column('attributes', sa.JSON(), nullable=True),
    sa.Column('external_stock', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('price', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_product_variants')),
    sa.UniqueConstraint('product_id', 'position', name='uq_erp_product_variants_position')
    )
    op.create_index(op.f('ix_erp_product_variants_product_id'), 'erp_product_variants', ['product_id'], unique=False)
    op.create_table('erp_products',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('code', sa.String(length=100), nullable=True),
    sa.Column('name', sa.String(length=400), nullable=False),
    sa.Column('unit', sa.String(length=30), nullable=True),
    sa.Column('category_external_id', sa.String(length=200), nullable=True),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('sellable', sa.Boolean(), nullable=False),
    sa.Column('list_price', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('minimum_price', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('external_stock', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('commission_percent', sa.Numeric(precision=9, scale=4), nullable=True),
    sa.Column('ipi_percent', sa.Numeric(precision=9, scale=4), nullable=True),
    sa.Column('ncm', sa.String(length=20), nullable=True),
    sa.Column('multiple', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('gross_weight', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('width', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('height', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('length', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=True),
    sa.Column('image_hashes', sa.JSON(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('source_created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('scope', sa.String(length=80), nullable=False),
    sa.Column('source_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source_deleted', sa.Boolean(), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=True),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_products')),
    sa.UniqueConstraint('connection_id', 'external_id', name='uq_erp_products_identity')
    )
    op.create_index(op.f('ix_erp_products_category_external_id'), 'erp_products', ['category_external_id'], unique=False)
    op.create_index('ix_erp_products_code', 'erp_products', ['connection_id', 'code'], unique=False)
    op.create_index('ix_erp_products_name', 'erp_products', ['connection_id', 'name'], unique=False)
    op.create_table('erp_quarantine',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('resource', sa.String(length=50), nullable=False),
    sa.Column('external_key', sa.String(length=200), nullable=False),
    sa.Column('run_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_quarantine')),
    sa.UniqueConstraint('connection_id', 'resource', 'external_key', name='uq_erp_quarantine_entity')
    )
    op.create_table('erp_sales_order_items',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('order_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=True),
    sa.Column('product_external_id', sa.String(length=200), nullable=True),
    sa.Column('code', sa.String(length=100), nullable=True),
    sa.Column('name', sa.String(length=400), nullable=True),
    sa.Column('quantity', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('list_unit_price', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('unit_price', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('discount', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('total', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('excluded', sa.Boolean(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_sales_order_items')),
    sa.UniqueConstraint('order_id', 'position', name='uq_erp_sales_order_items_position')
    )
    op.create_index(op.f('ix_erp_sales_order_items_order_id'), 'erp_sales_order_items', ['order_id'], unique=False)
    op.create_index(op.f('ix_erp_sales_order_items_product_external_id'), 'erp_sales_order_items', ['product_external_id'], unique=False)
    op.create_table('erp_sales_orders',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('number', sa.String(length=100), nullable=True),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('commercial_status', sa.String(length=50), nullable=True),
    sa.Column('billing_status', sa.String(length=50), nullable=True),
    sa.Column('fulfillment_status', sa.String(length=30), nullable=False),
    sa.Column('payment_status', sa.String(length=50), nullable=True),
    sa.Column('customer_external_id', sa.String(length=200), nullable=True),
    sa.Column('seller_external_id', sa.String(length=200), nullable=True),
    sa.Column('order_type_external_id', sa.String(length=200), nullable=True),
    sa.Column('payment_condition_external_id', sa.String(length=200), nullable=True),
    sa.Column('price_table_external_id', sa.String(length=200), nullable=True),
    sa.Column('carrier_external_id', sa.String(length=200), nullable=True),
    sa.Column('commercial_policy_external_id', sa.String(length=200), nullable=True),
    sa.Column('issued_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('issue_date', sa.Date(), nullable=True),
    sa.Column('expected_delivery_date', sa.Date(), nullable=True),
    sa.Column('gross_total', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('discount_total', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('freight_total', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('net_total', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('items_complete', sa.Boolean(), nullable=False),
    sa.Column('item_count', sa.Integer(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('shipping_address', sa.JSON(), nullable=True),
    sa.Column('extras', sa.JSON(), nullable=True),
    sa.Column('source_created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('scope', sa.String(length=80), nullable=False),
    sa.Column('source_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source_deleted', sa.Boolean(), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=True),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_sales_orders')),
    sa.UniqueConstraint('connection_id', 'external_id', name='uq_erp_sales_orders_identity')
    )
    op.create_index(op.f('ix_erp_sales_orders_commercial_status'), 'erp_sales_orders', ['commercial_status'], unique=False)
    op.create_index('ix_erp_sales_orders_customer', 'erp_sales_orders', ['connection_id', 'customer_external_id'], unique=False)
    op.create_index('ix_erp_sales_orders_issued', 'erp_sales_orders', ['connection_id', 'issued_at'], unique=False)
    op.create_index(op.f('ix_erp_sales_orders_number'), 'erp_sales_orders', ['number'], unique=False)
    op.create_index(op.f('ix_erp_sales_orders_seller_external_id'), 'erp_sales_orders', ['seller_external_id'], unique=False)
    op.create_table('erp_segments',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('name', sa.String(length=300), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=True),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('scope', sa.String(length=80), nullable=False),
    sa.Column('source_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source_deleted', sa.Boolean(), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=True),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_segments')),
    sa.UniqueConstraint('connection_id', 'external_id', name='uq_erp_segments_identity')
    )
    op.create_table('erp_sellers',
    sa.Column('email', sa.String(length=300), nullable=True),
    sa.Column('phone', sa.String(length=60), nullable=True),
    sa.Column('is_admin', sa.Boolean(), nullable=True),
    sa.Column('access_blocked', sa.Boolean(), nullable=True),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('name', sa.String(length=300), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=True),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('scope', sa.String(length=80), nullable=False),
    sa.Column('source_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source_deleted', sa.Boolean(), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=True),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_sellers')),
    sa.UniqueConstraint('connection_id', 'external_id', name='uq_erp_sellers_identity')
    )
    op.create_table('erp_sessions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('operator_id', sa.Integer(), nullable=False),
    sa.Column('refresh_jti', sa.String(length=36), nullable=False),
    sa.Column('previous_jti', sa.String(length=36), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_reason', sa.String(length=80), nullable=True),
    sa.Column('ip', sa.String(length=64), nullable=True),
    sa.Column('user_agent', sa.String(length=300), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_sessions'))
    )
    op.create_index('ix_erp_sessions_operator', 'erp_sessions', ['operator_id', 'revoked_at'], unique=False)
    op.create_index(op.f('ix_erp_sessions_refresh_jti'), 'erp_sessions', ['refresh_jti'], unique=False)
    op.create_table('erp_source_snapshots',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('resource', sa.String(length=50), nullable=False),
    sa.Column('external_key', sa.String(length=200), nullable=False),
    sa.Column('scope', sa.String(length=80), nullable=False),
    sa.Column('source_version', sa.String(length=64), nullable=True),
    sa.Column('fingerprint', sa.String(length=64), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('retention_until', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_source_snapshots')),
    sa.UniqueConstraint('connection_id', 'resource', 'external_key', 'fingerprint', name='uq_erp_snapshots_identity_version')
    )
    op.create_index('ix_erp_snapshots_entity', 'erp_source_snapshots', ['connection_id', 'resource', 'external_key'], unique=False)
    op.create_table('erp_suppliers',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('code', sa.String(length=60), nullable=False),
    sa.Column('name', sa.String(length=300), nullable=False),
    sa.Column('document', sa.String(length=40), nullable=True),
    sa.Column('email', sa.String(length=300), nullable=True),
    sa.Column('phone', sa.String(length=60), nullable=True),
    sa.Column('city', sa.String(length=120), nullable=True),
    sa.Column('state', sa.String(length=5), nullable=True),
    sa.Column('payment_terms', sa.String(length=200), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_by', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_suppliers')),
    sa.UniqueConstraint('connection_id', 'code', name='uq_erp_suppliers_code')
    )
    op.create_index('ix_erp_suppliers_name', 'erp_suppliers', ['connection_id', 'name'], unique=False)
    op.create_table('erp_sync_checkpoints',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('resource', sa.String(length=50), nullable=False),
    sa.Column('scope', sa.String(length=80), nullable=False),
    sa.Column('cursor', sa.Text(), nullable=True),
    sa.Column('transport_cursor', sa.Text(), nullable=True),
    sa.Column('data_through', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('records', sa.Integer(), nullable=False),
    sa.Column('unresolved', sa.Integer(), nullable=False),
    sa.Column('retry_after', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_sync_checkpoints')),
    sa.UniqueConstraint('connection_id', 'resource', 'scope', name='uq_erp_checkpoints_identity')
    )
    op.create_table('erp_sync_runs',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('resource', sa.String(length=50), nullable=False),
    sa.Column('mode', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('job_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cursor_before', sa.Text(), nullable=True),
    sa.Column('cursor_after', sa.Text(), nullable=True),
    sa.Column('pages', sa.Integer(), nullable=False),
    sa.Column('received', sa.Integer(), nullable=False),
    sa.Column('persisted', sa.Integer(), nullable=False),
    sa.Column('unchanged', sa.Integer(), nullable=False),
    sa.Column('quarantined', sa.Integer(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_sync_runs'))
    )
    op.create_index('ix_erp_sync_runs_started', 'erp_sync_runs', ['connection_id', 'started_at'], unique=False)
    op.create_table('erp_warehouses',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('code', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_warehouses')),
    sa.UniqueConstraint('connection_id', 'code', name='uq_erp_warehouses_code')
    )
    op.create_table('erp_webhook_inbox',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('delivery_id', sa.String(length=120), nullable=True),
    sa.Column('event', sa.String(length=120), nullable=True),
    sa.Column('dedupe_key', sa.String(length=80), nullable=False),
    sa.Column('body_hash', sa.String(length=64), nullable=False),
    sa.Column('raw_body', sa.LargeBinary(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('result', sa.JSON(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_webhook_inbox'))
    )
    op.create_index('ix_erp_inbox_dedupe', 'erp_webhook_inbox', ['connection_id', 'dedupe_key', 'received_at'], unique=False)
    op.create_index('ix_erp_inbox_pick', 'erp_webhook_inbox', ['status', 'received_at'], unique=False)
    op.create_table('erp_fin_titles',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=12), nullable=False),
    sa.Column('number', sa.String(length=60), nullable=True),
    sa.Column('description', sa.String(length=400), nullable=False),
    sa.Column('supplier_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('customer_external_id', sa.String(length=200), nullable=True),
    sa.Column('category_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('cost_center_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('origin_type', sa.String(length=40), nullable=True),
    sa.Column('origin_ref', sa.String(length=80), nullable=True),
    sa.Column('causal_key', sa.String(length=160), nullable=True),
    sa.Column('total', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('issued_date', sa.Date(), nullable=True),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('extra', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['category_id'], ['erp_fin_categories.id'], name=op.f('fk_erp_fin_titles_category_id_erp_fin_categories')),
    sa.ForeignKeyConstraint(['cost_center_id'], ['erp_cost_centers.id'], name=op.f('fk_erp_fin_titles_cost_center_id_erp_cost_centers')),
    sa.ForeignKeyConstraint(['supplier_id'], ['erp_suppliers.id'], name=op.f('fk_erp_fin_titles_supplier_id_erp_suppliers')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_fin_titles')),
    sa.UniqueConstraint('connection_id', 'causal_key', name='uq_erp_fin_titles_causal')
    )
    op.create_index('ix_erp_fin_titles_status', 'erp_fin_titles', ['connection_id', 'kind', 'status'], unique=False)
    op.create_table('erp_inventory_balances',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('warehouse_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('product_external_id', sa.String(length=200), nullable=False),
    sa.Column('on_hand', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('reserved', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('on_hand >= 0', name=op.f('ck_erp_inventory_balances_on_hand_non_negative')),
    sa.CheckConstraint('reserved <= on_hand', name=op.f('ck_erp_inventory_balances_reserved_within_on_hand')),
    sa.CheckConstraint('reserved >= 0', name=op.f('ck_erp_inventory_balances_reserved_non_negative')),
    sa.ForeignKeyConstraint(['warehouse_id'], ['erp_warehouses.id'], name=op.f('fk_erp_inventory_balances_warehouse_id_erp_warehouses')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_inventory_balances')),
    sa.UniqueConstraint('warehouse_id', 'product_external_id', name='uq_erp_balances_warehouse_product')
    )
    op.create_index(op.f('ix_erp_inventory_balances_product_external_id'), 'erp_inventory_balances', ['product_external_id'], unique=False)
    op.create_table('erp_inventory_movements',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('warehouse_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('product_external_id', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('quantity_delta', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('reserved_delta', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('unit_cost', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('causal_key', sa.String(length=160), nullable=False),
    sa.Column('reference_type', sa.String(length=40), nullable=True),
    sa.Column('reference_id', sa.String(length=80), nullable=True),
    sa.Column('reversal_of_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('operator', sa.String(length=200), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['warehouse_id'], ['erp_warehouses.id'], name=op.f('fk_erp_inventory_movements_warehouse_id_erp_warehouses')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_inventory_movements')),
    sa.UniqueConstraint('connection_id', 'causal_key', name='uq_erp_movements_causal')
    )
    op.create_index('ix_erp_movements_product', 'erp_inventory_movements', ['connection_id', 'product_external_id'], unique=False)
    op.create_table('erp_inventory_reservations',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('warehouse_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('product_external_id', sa.String(length=200), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('order_external_id', sa.String(length=200), nullable=True),
    sa.Column('causal_key', sa.String(length=160), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['warehouse_id'], ['erp_warehouses.id'], name=op.f('fk_erp_inventory_reservations_warehouse_id_erp_warehouses')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_inventory_reservations')),
    sa.UniqueConstraint('connection_id', 'causal_key', name='uq_erp_reservations_causal')
    )
    op.create_table('erp_purchase_orders',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('number', sa.String(length=40), nullable=False),
    sa.Column('supplier_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('expected_date', sa.Date(), nullable=True),
    sa.Column('total', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('approved_by', sa.String(length=200), nullable=True),
    sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_by', sa.String(length=200), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancel_reason', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['supplier_id'], ['erp_suppliers.id'], name=op.f('fk_erp_purchase_orders_supplier_id_erp_suppliers')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_purchase_orders')),
    sa.UniqueConstraint('connection_id', 'number', name='uq_erp_purchase_orders_number')
    )
    op.create_index('ix_erp_purchase_orders_status', 'erp_purchase_orders', ['connection_id', 'status'], unique=False)
    op.create_index(op.f('ix_erp_purchase_orders_supplier_id'), 'erp_purchase_orders', ['supplier_id'], unique=False)
    op.create_table('erp_fin_installments',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('title_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('number', sa.Integer(), nullable=False),
    sa.Column('due_date', sa.Date(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('settled_amount', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.CheckConstraint('settled_amount <= amount', name=op.f('ck_erp_fin_installments_settled_within_amount')),
    sa.CheckConstraint('settled_amount >= 0', name=op.f('ck_erp_fin_installments_settled_non_negative')),
    sa.ForeignKeyConstraint(['title_id'], ['erp_fin_titles.id'], name=op.f('fk_erp_fin_installments_title_id_erp_fin_titles')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_fin_installments')),
    sa.UniqueConstraint('title_id', 'number', name='uq_erp_fin_installments_number')
    )
    op.create_index('ix_erp_fin_installments_due', 'erp_fin_installments', ['due_date'], unique=False)
    op.create_index(op.f('ix_erp_fin_installments_title_id'), 'erp_fin_installments', ['title_id'], unique=False)
    op.create_table('erp_purchase_order_items',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('purchase_order_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('product_external_id', sa.String(length=200), nullable=True),
    sa.Column('description', sa.String(length=400), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('received_quantity', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('unit_cost', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.CheckConstraint('received_quantity <= quantity', name=op.f('ck_erp_purchase_order_items_received_within_ordered')),
    sa.CheckConstraint('received_quantity >= 0', name=op.f('ck_erp_purchase_order_items_received_non_negative')),
    sa.ForeignKeyConstraint(['purchase_order_id'], ['erp_purchase_orders.id'], name=op.f('fk_erp_purchase_order_items_purchase_order_id_erp_purchase_orders')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_purchase_order_items')),
    sa.UniqueConstraint('purchase_order_id', 'position', name='uq_erp_po_items_position')
    )
    op.create_index(op.f('ix_erp_purchase_order_items_purchase_order_id'), 'erp_purchase_order_items', ['purchase_order_id'], unique=False)
    op.create_table('erp_purchase_receipts',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('purchase_order_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('warehouse_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('invoice_number', sa.String(length=60), nullable=True),
    sa.Column('received_by', sa.String(length=200), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('reversed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reversed_by', sa.String(length=200), nullable=True),
    sa.Column('reverse_reason', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['purchase_order_id'], ['erp_purchase_orders.id'], name=op.f('fk_erp_purchase_receipts_purchase_order_id_erp_purchase_orders')),
    sa.ForeignKeyConstraint(['warehouse_id'], ['erp_warehouses.id'], name=op.f('fk_erp_purchase_receipts_warehouse_id_erp_warehouses')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_purchase_receipts'))
    )
    op.create_index(op.f('ix_erp_purchase_receipts_purchase_order_id'), 'erp_purchase_receipts', ['purchase_order_id'], unique=False)
    op.create_table('erp_fin_settlements',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('connection_id', sa.String(length=64), nullable=False),
    sa.Column('installment_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('account_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('kind', sa.String(length=12), nullable=False),
    sa.Column('amount', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('settled_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('reversal_of_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('causal_key', sa.String(length=160), nullable=False),
    sa.Column('reference', sa.String(length=120), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('operator', sa.String(length=200), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['erp_fin_accounts.id'], name=op.f('fk_erp_fin_settlements_account_id_erp_fin_accounts')),
    sa.ForeignKeyConstraint(['installment_id'], ['erp_fin_installments.id'], name=op.f('fk_erp_fin_settlements_installment_id_erp_fin_installments')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_fin_settlements')),
    sa.UniqueConstraint('connection_id', 'causal_key', name='uq_erp_fin_settlements_causal')
    )
    op.create_index(op.f('ix_erp_fin_settlements_installment_id'), 'erp_fin_settlements', ['installment_id'], unique=False)
    op.create_table('erp_purchase_receipt_items',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('receipt_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('purchase_order_item_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('unit_cost', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.ForeignKeyConstraint(['purchase_order_item_id'], ['erp_purchase_order_items.id'], name=op.f('fk_erp_purchase_receipt_items_purchase_order_item_id_erp_purchase_order_items')),
    sa.ForeignKeyConstraint(['receipt_id'], ['erp_purchase_receipts.id'], name=op.f('fk_erp_purchase_receipt_items_receipt_id_erp_purchase_receipts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_erp_purchase_receipt_items'))
    )
    op.create_index(op.f('ix_erp_purchase_receipt_items_receipt_id'), 'erp_purchase_receipt_items', ['receipt_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_erp_purchase_receipt_items_receipt_id'), table_name='erp_purchase_receipt_items')
    op.drop_table('erp_purchase_receipt_items')
    op.drop_index(op.f('ix_erp_fin_settlements_installment_id'), table_name='erp_fin_settlements')
    op.drop_table('erp_fin_settlements')
    op.drop_index(op.f('ix_erp_purchase_receipts_purchase_order_id'), table_name='erp_purchase_receipts')
    op.drop_table('erp_purchase_receipts')
    op.drop_index(op.f('ix_erp_purchase_order_items_purchase_order_id'), table_name='erp_purchase_order_items')
    op.drop_table('erp_purchase_order_items')
    op.drop_index(op.f('ix_erp_fin_installments_title_id'), table_name='erp_fin_installments')
    op.drop_index('ix_erp_fin_installments_due', table_name='erp_fin_installments')
    op.drop_table('erp_fin_installments')
    op.drop_index(op.f('ix_erp_purchase_orders_supplier_id'), table_name='erp_purchase_orders')
    op.drop_index('ix_erp_purchase_orders_status', table_name='erp_purchase_orders')
    op.drop_table('erp_purchase_orders')
    op.drop_table('erp_inventory_reservations')
    op.drop_index('ix_erp_movements_product', table_name='erp_inventory_movements')
    op.drop_table('erp_inventory_movements')
    op.drop_index(op.f('ix_erp_inventory_balances_product_external_id'), table_name='erp_inventory_balances')
    op.drop_table('erp_inventory_balances')
    op.drop_index('ix_erp_fin_titles_status', table_name='erp_fin_titles')
    op.drop_table('erp_fin_titles')
    op.drop_index('ix_erp_inbox_pick', table_name='erp_webhook_inbox')
    op.drop_index('ix_erp_inbox_dedupe', table_name='erp_webhook_inbox')
    op.drop_table('erp_webhook_inbox')
    op.drop_table('erp_warehouses')
    op.drop_index('ix_erp_sync_runs_started', table_name='erp_sync_runs')
    op.drop_table('erp_sync_runs')
    op.drop_table('erp_sync_checkpoints')
    op.drop_index('ix_erp_suppliers_name', table_name='erp_suppliers')
    op.drop_table('erp_suppliers')
    op.drop_index('ix_erp_snapshots_entity', table_name='erp_source_snapshots')
    op.drop_table('erp_source_snapshots')
    op.drop_index(op.f('ix_erp_sessions_refresh_jti'), table_name='erp_sessions')
    op.drop_index('ix_erp_sessions_operator', table_name='erp_sessions')
    op.drop_table('erp_sessions')
    op.drop_table('erp_sellers')
    op.drop_table('erp_segments')
    op.drop_index(op.f('ix_erp_sales_orders_seller_external_id'), table_name='erp_sales_orders')
    op.drop_index(op.f('ix_erp_sales_orders_number'), table_name='erp_sales_orders')
    op.drop_index('ix_erp_sales_orders_issued', table_name='erp_sales_orders')
    op.drop_index('ix_erp_sales_orders_customer', table_name='erp_sales_orders')
    op.drop_index(op.f('ix_erp_sales_orders_commercial_status'), table_name='erp_sales_orders')
    op.drop_table('erp_sales_orders')
    op.drop_index(op.f('ix_erp_sales_order_items_product_external_id'), table_name='erp_sales_order_items')
    op.drop_index(op.f('ix_erp_sales_order_items_order_id'), table_name='erp_sales_order_items')
    op.drop_table('erp_sales_order_items')
    op.drop_table('erp_quarantine')
    op.drop_index('ix_erp_products_name', table_name='erp_products')
    op.drop_index('ix_erp_products_code', table_name='erp_products')
    op.drop_index(op.f('ix_erp_products_category_external_id'), table_name='erp_products')
    op.drop_table('erp_products')
    op.drop_index(op.f('ix_erp_product_variants_product_id'), table_name='erp_product_variants')
    op.drop_table('erp_product_variants')
    op.drop_index('ix_erp_product_prices_table', table_name='erp_product_prices')
    op.drop_index('ix_erp_product_prices_product', table_name='erp_product_prices')
    op.drop_table('erp_product_prices')
    op.drop_index(op.f('ix_erp_price_tables_represented_external_id'), table_name='erp_price_tables')
    op.drop_table('erp_price_tables')
    op.drop_index(op.f('ix_erp_payment_conditions_represented_external_id'), table_name='erp_payment_conditions')
    op.drop_table('erp_payment_conditions')
    op.drop_table('erp_order_types')
    op.drop_table('erp_operators')
    op.drop_index('uq_erp_jobs_active', table_name='erp_jobs', postgresql_where=sa.text("status IN ('queued','processing','waiting_rate_limit')"), sqlite_where=sa.text("status IN ('queued','processing','waiting_rate_limit')"))
    op.drop_index('ix_erp_jobs_pick', table_name='erp_jobs')
    op.drop_table('erp_jobs')
    op.drop_table('erp_inventory_authority')
    op.drop_index('ix_erp_operations_pick', table_name='erp_integration_operations')
    op.drop_table('erp_integration_operations')
    op.drop_table('erp_idempotency_keys')
    op.drop_table('erp_fin_categories')
    op.drop_table('erp_fin_accounts')
    op.drop_table('erp_field_inventory')
    op.drop_table('erp_external_titles')
    op.drop_table('erp_external_payments')
    op.drop_index(op.f('ix_erp_customers_seller_external_id'), table_name='erp_customers')
    op.drop_index(op.f('ix_erp_customers_segment_external_id'), table_name='erp_customers')
    op.drop_index('ix_erp_customers_name', table_name='erp_customers')
    op.drop_index('ix_erp_customers_document', table_name='erp_customers')
    op.drop_table('erp_customers')
    op.drop_index(op.f('ix_erp_customer_contacts_customer_id'), table_name='erp_customer_contacts')
    op.drop_table('erp_customer_contacts')
    op.drop_index(op.f('ix_erp_customer_addresses_customer_id'), table_name='erp_customer_addresses')
    op.drop_table('erp_customer_addresses')
    op.drop_table('erp_cost_centers')
    op.drop_table('erp_connections')
    op.drop_index(op.f('ix_erp_conflicts_status'), table_name='erp_conflicts')
    op.drop_index(op.f('ix_erp_conflicts_operation_id'), table_name='erp_conflicts')
    op.drop_table('erp_conflicts')
    op.drop_index(op.f('ix_erp_commercial_policies_slug'), table_name='erp_commercial_policies')
    op.drop_table('erp_commercial_policies')
    op.drop_index(op.f('ix_erp_categories_represented_external_id'), table_name='erp_categories')
    op.drop_index(op.f('ix_erp_categories_parent_external_id'), table_name='erp_categories')
    op.drop_table('erp_categories')
    op.drop_table('erp_carriers')
    op.drop_index(op.f('ix_erp_capabilities_connection_id'), table_name='erp_capabilities')
    op.drop_table('erp_capabilities')
    op.drop_index('ix_erp_audit_at', table_name='erp_audit_events')
    op.drop_table('erp_audit_events')
