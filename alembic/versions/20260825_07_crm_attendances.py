"""CRM attendances for the sales lead queue.

Revision ID: 20260825_07
Revises: 20260813_06
"""

from alembic import op
import sqlalchemy as sa


revision = "20260825_07"
down_revision = "20260813_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("crm_attendances"):
        return
    op.create_table(
        "crm_attendances",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_mercos_id", sa.String(length=80), nullable=False),
        sa.Column("seller_name", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="open"),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("customer_mercos_id", name="uq_crm_attendances_customer"),
    )
    op.create_index("ix_crm_attendances_customer_mercos_id", "crm_attendances", ["customer_mercos_id"])
    op.create_index("ix_crm_attendances_status", "crm_attendances", ["status"])
    op.create_index("ix_crm_attendances_status_finished", "crm_attendances", ["status", "finished_at"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("crm_attendances"):
        op.drop_table("crm_attendances")
