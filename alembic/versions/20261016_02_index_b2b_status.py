"""Extract B2B status once; avoid repeated full raw JSON scans in analytics."""

from alembic import op
import sqlalchemy as sa

revision = "20261016_02"
down_revision = "20261016_01"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("orders"):
        return  # Fresh installations create core BI tables during startup.
    if "b2b_status" not in {c["name"] for c in inspector.get_columns("orders")}:
        op.add_column("orders", sa.Column("b2b_status", sa.Integer(), nullable=True))
    orders = sa.table(
        "orders", sa.column("raw", sa.JSON()), sa.column("b2b_status", sa.Integer())
    )
    source = sa.cast(orders.c.raw["status_b2b"].as_string(), sa.String())
    op.execute(
        orders.update()
        .where(source.in_(("1", "2")))
        .values(b2b_status=sa.case((source == "1", 1), (source == "2", 2), else_=None))
    )
    if "ix_orders_b2b_created" not in {
        i["name"] for i in inspector.get_indexes("orders")
    }:
        op.create_index(
            "ix_orders_b2b_created", "orders", ["b2b_status", "source_created_at"]
        )


def downgrade():
    if not sa.inspect(op.get_bind()).has_table("orders"):
        return
    op.drop_index("ix_orders_b2b_created", table_name="orders")
    op.drop_column("orders", "b2b_status")
