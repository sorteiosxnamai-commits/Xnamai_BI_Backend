"""Add distributed sync lease fields.

Revision ID: 20260813_04
Revises: 20260812_03
"""

from alembic import op
import sqlalchemy as sa


revision = "20260813_04"
down_revision = "20260812_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("sync_states"):
        return
    columns = {column["name"] for column in inspector.get_columns("sync_states")}
    with op.batch_alter_table("sync_states") as batch:
        if "lease_token" not in columns:
            batch.add_column(
                sa.Column("lease_token", sa.String(length=36), nullable=True)
            )
        if "heartbeat_at" not in columns:
            batch.add_column(
                sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("sync_states"):
        return
    columns = {column["name"] for column in inspector.get_columns("sync_states")}
    with op.batch_alter_table("sync_states") as batch:
        if "heartbeat_at" in columns:
            batch.drop_column("heartbeat_at")
        if "lease_token" in columns:
            batch.drop_column("lease_token")
