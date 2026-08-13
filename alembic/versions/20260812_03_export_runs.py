"""Add export execution audit.

Revision ID: 20260812_03
Revises: 20260812_02
Create Date: 2026-08-12
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260812_03"
down_revision: str | None = "20260812_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "export_runs" in tables:
        return
    op.create_table(
        "export_runs",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=True),
            nullable=False,
        ),
        sa.Column("username", sa.String(length=120), nullable=False),
        sa.Column("report", sa.String(length=50), nullable=False),
        sa.Column("format", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("filters", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_export_runs_started_at",
        "export_runs",
        ["started_at"],
    )


def downgrade() -> None:
    if "export_runs" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("export_runs")
