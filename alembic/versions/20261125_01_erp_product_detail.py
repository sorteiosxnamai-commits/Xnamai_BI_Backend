"""Detalhe local do produto: descrição, mídia, lote, custo e local de estoque.

Revision ID: 20261125_01
Revises: 20261124_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20261125_01"
down_revision = "20261124_01"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("description", sa.Text()),
    ("photos", sa.JSON()),
    ("videos", sa.JSON()),
    ("supplier_code", sa.String(length=80)),
    ("barcode", sa.String(length=80)),
    ("lot", sa.String(length=80)),
    ("received_at", sa.Date()),
    ("pack_kind", sa.String(length=10)),
    ("cost_price", sa.Numeric(precision=18, scale=2)),
    ("stock_location", sa.String(length=200)),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("erp_products"):
        return
    present = {c["name"] for c in inspector.get_columns("erp_products")}
    for name, column in _COLUMNS:
        if name not in present:
            op.add_column("erp_products", sa.Column(name, column, nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("erp_products"):
        return
    present = {c["name"] for c in inspector.get_columns("erp_products")}
    for name, _column in reversed(_COLUMNS):
        if name in present:
            op.drop_column("erp_products", name)
