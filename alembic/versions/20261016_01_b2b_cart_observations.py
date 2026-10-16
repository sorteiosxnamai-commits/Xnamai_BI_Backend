"""Preserve observed B2B order versions without changing legacy orders."""

from alembic import op
import sqlalchemy as sa

revision = "20261016_01"
down_revision = "20261009_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "b2b_cart_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_mercos_id", sa.String(80), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False, unique=True),
        sa.Column("b2b_status", sa.Integer(), nullable=True),
        sa.Column("order_status", sa.String(50), nullable=False),
        sa.Column("total", sa.Numeric(18, 2), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_b2b_cart_observations_order_mercos_id",
        "b2b_cart_observations",
        ["order_mercos_id"],
    )


def downgrade():
    op.drop_index(
        "ix_b2b_cart_observations_order_mercos_id", table_name="b2b_cart_observations"
    )
    op.drop_table("b2b_cart_observations")
