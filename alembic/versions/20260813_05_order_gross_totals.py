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

    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(
        """
        UPDATE order_items
           SET list_unit_price =
               REPLACE(raw ->> 'preco_tabela', ',', '.')::numeric(18, 2)
         WHERE list_unit_price IS NULL
           AND raw ->> 'preco_tabela'
               ~ '^[[:space:]]*-?[0-9]+([.,][0-9]+)?[[:space:]]*$'
        """
    )
    op.execute(
        """
        WITH item_discounts AS (
            SELECT
                order_mercos_id,
                SUM(
                    GREATEST(
                        (list_unit_price - unit_price) * quantity,
                        0
                    )
                ) AS discount_value,
                COUNT(*) AS item_count,
                COUNT(list_unit_price) AS priced_item_count
            FROM order_items
            WHERE COALESCE(raw ->> 'excluido', 'false') <> 'true'
            GROUP BY order_mercos_id
        ),
        derived AS (
            SELECT
                orders.id,
                item_discounts.discount_value,
                COALESCE(orders.net_total, orders.total)
                    + item_discounts.discount_value AS gross_total
            FROM orders
            JOIN item_discounts
              ON item_discounts.order_mercos_id = orders.mercos_id
            WHERE item_discounts.item_count = item_discounts.priced_item_count
        )
        UPDATE orders
           SET gross_total = derived.gross_total,
               discount_value = derived.discount_value,
               discount_percent = CASE
                   WHEN derived.gross_total > 0
                   THEN derived.discount_value / derived.gross_total * 100
                   ELSE 0
               END
          FROM derived
         WHERE orders.id = derived.id
        """
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("order_items"):
        return
    columns = {column["name"] for column in inspector.get_columns("order_items")}
    if "list_unit_price" in columns:
        with op.batch_alter_table("order_items") as batch:
            batch.drop_column("list_unit_price")
