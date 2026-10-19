"""Keep the revision chain while deferring the legacy orders table rewrite.

The B2B cart view reads ``raw.status_b2b`` directly. Materializing that field
requires an exclusive lock on ``orders`` and can prevent web startup while
the existing service is synchronizing orders.
"""

revision = "20261016_02"
down_revision = "20261016_01"
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
