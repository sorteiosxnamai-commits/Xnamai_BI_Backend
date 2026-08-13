"""Persist historical list prices and derive gross order totals.

Revision ID: 20260813_05
Revises: 20260813_04
"""

from alembic import op
import sqlalchemy as sa


revision = "20260813_05"
down_revision = "20260813_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("order_items"):
        return
    columns = {column["name"] for column in inspector.get_columns("order_items")}
    if "list_unit_price" not in columns:
        with op.batch_alter_table("order_items") as batch:
            batch.add_column(
                sa.Column("list_unit_price", sa.Numeric(18, 2), nullable=True)
            )

    # Historical JSON backfill was removed: it timed out on production
    # (statement_timeout while rewriting every order_items.raw). Analytics
    # now uses current Product.list_price, and sync fills list_unit_price
    # for newly persisted items.


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("order_items"):
        return
    columns = {column["name"] for column in inspector.get_columns("order_items")}
    if "list_unit_price" in columns:
        with op.batch_alter_table("order_items") as batch:
            batch.drop_column("list_unit_price")
