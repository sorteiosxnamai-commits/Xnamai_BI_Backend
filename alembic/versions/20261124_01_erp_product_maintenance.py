"""Foto e marcação Mercos na manutenção de produtos.

Revision ID: 20261124_01
Revises: 20261016_02
"""

from alembic import op
import sqlalchemy as sa


revision = "20261124_01"
down_revision = "20261016_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("erp_products"):
        return
    columns = {c["name"] for c in inspector.get_columns("erp_products")}
    if "image_url" not in columns:
        op.add_column("erp_products", sa.Column("image_url", sa.String(length=1000), nullable=True))
    if "publish_to_mercos" not in columns:
        op.add_column("erp_products", sa.Column("publish_to_mercos", sa.Boolean(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("erp_products"):
        return
    columns = {c["name"] for c in inspector.get_columns("erp_products")}
    if "publish_to_mercos" in columns:
        op.drop_column("erp_products", "publish_to_mercos")
    if "image_url" in columns:
        op.drop_column("erp_products", "image_url")
