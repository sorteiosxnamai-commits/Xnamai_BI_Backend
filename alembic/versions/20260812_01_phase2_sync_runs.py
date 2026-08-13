"""Add durable sync runs and Mercos item identity.

Revision ID: 20260812_01
Revises:
Create Date: 2026-08-12
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260812_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _unique_names(table: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints(table)
        if constraint.get("name")
    }


def _index_names(table: str) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table)
        if index.get("name")
    }


def upgrade() -> None:
    tables = _tables()
    if "sync_runs" not in tables:
        op.create_table(
            "sync_runs",
            sa.Column(
                "id",
                sa.BigInteger(),
                sa.Identity(always=True),
                nullable=False,
            ),
            sa.Column("resource", sa.String(length=50), nullable=False),
            sa.Column("mode", sa.String(length=20), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("cursor_before", sa.Text(), nullable=True),
            sa.Column("cursor_after", sa.Text(), nullable=True),
            sa.Column("pages", sa.Integer(), server_default="0", nullable=False),
            sa.Column("received", sa.Integer(), server_default="0", nullable=False),
            sa.Column("persisted", sa.Integer(), server_default="0", nullable=False),
            sa.Column("failed", sa.Integer(), server_default="0", nullable=False),
            sa.Column(
                "details",
                sa.JSON(),
                server_default=sa.text("'{}'"),
                nullable=False,
            ),
            sa.Column("error", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.execute(
            "CREATE INDEX ix_sync_runs_resource_started_at "
            "ON sync_runs (resource, started_at DESC)"
        )

    if "order_items" in tables:
        columns = _columns("order_items")
        indexes = _index_names("order_items")
        unique_constraints = _unique_names("order_items")
        with op.batch_alter_table("order_items") as batch:
            if "mercos_item_id" not in columns:
                batch.add_column(
                    sa.Column("mercos_item_id", sa.String(length=80), nullable=True)
                )
            if "ix_order_items_mercos_item_id" not in indexes:
                batch.create_index(
                    "ix_order_items_mercos_item_id",
                    ["mercos_item_id"],
                    unique=False,
                )
            if "uq_order_items_order_mercos_item" not in unique_constraints:
                batch.create_unique_constraint(
                    "uq_order_items_order_mercos_item",
                    ["order_mercos_id", "mercos_item_id"],
                )


def downgrade() -> None:
    tables = _tables()
    if "order_items" in tables and "mercos_item_id" in _columns("order_items"):
        unique_constraints = _unique_names("order_items")
        indexes = _index_names("order_items")
        with op.batch_alter_table("order_items") as batch:
            if "uq_order_items_order_mercos_item" in unique_constraints:
                batch.drop_constraint(
                    "uq_order_items_order_mercos_item",
                    type_="unique",
                )
            if "ix_order_items_mercos_item_id" in indexes:
                batch.drop_index("ix_order_items_mercos_item_id")
            batch.drop_column("mercos_item_id")
    if "sync_runs" in tables:
        op.drop_table("sync_runs")
