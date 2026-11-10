"""Record resolution attempts before calling an upstream service."""

import sqlalchemy as sa
from alembic import op

revision = "0004_resolution_attempts"
down_revision = "0003_counsel_analyses"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "resolution_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dispute_id", sa.Integer(), sa.ForeignKey("disputes.id"), nullable=False),
        sa.Column("capture_id", sa.String(255)),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_resolution_attempts_dispute_id", "resolution_attempts", ["dispute_id"])
    op.create_index("ix_resolution_attempts_capture_id", "resolution_attempts", ["capture_id"])


def downgrade():
    op.drop_table("resolution_attempts")
