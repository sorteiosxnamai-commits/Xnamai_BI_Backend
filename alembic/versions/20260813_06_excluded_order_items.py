"""Mark excluded Mercos order items.

Revision ID: 20260813_06
Revises: 20260813_05
"""

from alembic import op
import sqlalchemy as sa


revision = "20260813_06"
down_revision = "20260813_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("order_items"):
        return
    columns = {column["name"] for column in inspector.get_columns("order_items")}
    indexes = {
        index["name"] for index in inspector.get_indexes("order_items")
    }
    with op.batch_alter_table("order_items") as batch:
        if "excluded" not in columns:
            batch.add_column(
                sa.Column(
                    "excluded",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )
        if "ix_order_items_excluded" not in indexes:
            batch.create_index(
                "ix_order_items_excluded",
                ["excluded"],
                unique=False,
            )

    if op.get_bind().dialect.name == "postgresql":
        # JSON backfill of excluded items timed out in production and locked
        # order_items. Sync marks new/updated items; do not scan raw here.
        return


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("order_items"):
        return
    columns = {column["name"] for column in inspector.get_columns("order_items")}
    indexes = {
        index["name"] for index in inspector.get_indexes("order_items")
    }
    with op.batch_alter_table("order_items") as batch:
        if "ix_order_items_excluded" in indexes:
            batch.drop_index("ix_order_items_excluded")
        if "excluded" in columns:
            batch.drop_column("excluded")
