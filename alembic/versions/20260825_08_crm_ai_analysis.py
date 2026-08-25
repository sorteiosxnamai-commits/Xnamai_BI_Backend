"""Cache AI lead analysis on CRM attendances.

Revision ID: 20260825_08
Revises: 20260825_07
"""

from alembic import op
import sqlalchemy as sa


revision = "20260825_08"
down_revision = "20260825_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("crm_attendances"):
        return
    columns = {column["name"] for column in inspector.get_columns("crm_attendances")}
    with op.batch_alter_table("crm_attendances") as batch:
        if "ai_analysis" not in columns:
            batch.add_column(sa.Column("ai_analysis", sa.JSON(), nullable=True))
        if "ai_analysis_at" not in columns:
            batch.add_column(sa.Column("ai_analysis_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("crm_attendances"):
        return
    columns = {column["name"] for column in inspector.get_columns("crm_attendances")}
    with op.batch_alter_table("crm_attendances") as batch:
        if "ai_analysis_at" in columns:
            batch.drop_column("ai_analysis_at")
        if "ai_analysis" in columns:
            batch.drop_column("ai_analysis")
