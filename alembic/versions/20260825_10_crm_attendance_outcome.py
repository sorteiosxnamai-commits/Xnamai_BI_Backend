"""Attendance outcome and sale fields for CRM.

Revision ID: 20260825_10
Revises: 20260825_09
"""

from alembic import op
import sqlalchemy as sa


revision = "20260825_10"
down_revision = "20260825_09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("crm_attendances"):
        return
    columns = {column["name"] for column in inspector.get_columns("crm_attendances")}
    with op.batch_alter_table("crm_attendances") as batch:
        if "outcome" not in columns:
            batch.add_column(sa.Column("outcome", sa.String(length=20), nullable=True))
        if "sale_value" not in columns:
            batch.add_column(sa.Column("sale_value", sa.Numeric(14, 2), nullable=True))
        if "order_number" not in columns:
            batch.add_column(sa.Column("order_number", sa.String(length=80), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("crm_attendances"):
        return
    columns = {column["name"] for column in inspector.get_columns("crm_attendances")}
    with op.batch_alter_table("crm_attendances") as batch:
        if "order_number" in columns:
            batch.drop_column("order_number")
        if "sale_value" in columns:
            batch.drop_column("sale_value")
        if "outcome" in columns:
            batch.drop_column("outcome")
