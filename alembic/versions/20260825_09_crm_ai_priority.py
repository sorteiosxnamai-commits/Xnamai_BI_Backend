"""Cache AI priority scores for CRM lead ranking.

Revision ID: 20260825_09
Revises: 20260825_08
"""

from alembic import op
import sqlalchemy as sa


revision = "20260825_09"
down_revision = "20260825_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("crm_attendances"):
        return
    columns = {column["name"] for column in inspector.get_columns("crm_attendances")}
    indexes = {index["name"] for index in inspector.get_indexes("crm_attendances")}
    with op.batch_alter_table("crm_attendances") as batch:
        if "ai_priority_score" not in columns:
            batch.add_column(sa.Column("ai_priority_score", sa.Float(), nullable=True))
        if "ai_priority_reason" not in columns:
            batch.add_column(sa.Column("ai_priority_reason", sa.Text(), nullable=True))
        if "ai_priority_at" not in columns:
            batch.add_column(sa.Column("ai_priority_at", sa.DateTime(timezone=True), nullable=True))
        if "ix_crm_attendances_ai_priority_score" not in indexes:
            batch.create_index(
                "ix_crm_attendances_ai_priority_score",
                ["ai_priority_score"],
                unique=False,
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("crm_attendances"):
        return
    columns = {column["name"] for column in inspector.get_columns("crm_attendances")}
    indexes = {index["name"] for index in inspector.get_indexes("crm_attendances")}
    with op.batch_alter_table("crm_attendances") as batch:
        if "ix_crm_attendances_ai_priority_score" in indexes:
            batch.drop_index("ix_crm_attendances_ai_priority_score")
        if "ai_priority_at" in columns:
            batch.drop_column("ai_priority_at")
        if "ai_priority_reason" in columns:
            batch.drop_column("ai_priority_reason")
        if "ai_priority_score" in columns:
            batch.drop_column("ai_priority_score")
