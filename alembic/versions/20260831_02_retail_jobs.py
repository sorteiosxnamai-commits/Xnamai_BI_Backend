"""Retail analysis job table.

Revision ID: 20260831_02
Revises: 20260831_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260831_02"
down_revision = "20260831_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("retail_analysis_jobs"):
        return
    op.create_table(
        "retail_analysis_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("batch_size", sa.Integer(), nullable=False),
        sa.Column("product_ids", sa.JSON(), nullable=False),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("processed", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("skipped", sa.Integer(), nullable=False),
        sa.Column("current_product_id", sa.String(length=80), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_retail_analysis_jobs_status", "retail_analysis_jobs", ["status"], unique=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("retail_analysis_jobs"):
        return
    op.drop_index("ix_retail_analysis_jobs_status", table_name="retail_analysis_jobs")
    op.drop_table("retail_analysis_jobs")
