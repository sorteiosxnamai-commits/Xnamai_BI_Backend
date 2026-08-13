"""Mark excluded Mercos order items.

Revision ID: 20260813_06
Revises: 20260813_05
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


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
        op.execute("SET LOCAL statement_timeout TO 0")
        bind = op.get_bind()
        while True:
            result = bind.execute(
                text(
                    """
                    UPDATE order_items
                       SET excluded = true
                     WHERE id IN (
                        SELECT id
                          FROM order_items
                         WHERE excluded = false
                           AND raw->>'excluido' IN ('true', '1', 'True', 'TRUE')
                         LIMIT 5000
                     )
                    """
                )
            )
            if result.rowcount == 0:
                break


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
