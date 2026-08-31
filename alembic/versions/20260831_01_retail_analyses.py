"""Retail product AI analysis cache.

Revision ID: 20260831_01
Revises: 20260825_10
"""

from alembic import op
import sqlalchemy as sa


revision = "20260831_01"
down_revision = "20260825_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("retail_product_analyses"):
        return
    op.create_table(
        "retail_product_analyses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_mercos_id", sa.String(length=80), nullable=False),
        sa.Column("ai_payload", sa.JSON(), nullable=True),
        sa.Column("market_prices", sa.JSON(), nullable=True),
        sa.Column("scores", sa.JSON(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("product_mercos_id", name="uq_retail_product_analyses_product"),
    )
    op.create_index(
        "ix_retail_product_analyses_product_mercos_id",
        "retail_product_analyses",
        ["product_mercos_id"],
        unique=False,
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("retail_product_analyses"):
        return
    op.drop_index("ix_retail_product_analyses_product_mercos_id", table_name="retail_product_analyses")
    op.drop_table("retail_product_analyses")
