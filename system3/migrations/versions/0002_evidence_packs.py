"""Store versioned evidence snapshots for System 3."""

import sqlalchemy as sa
from alembic import op

revision = "0002_evidence_packs"
down_revision = "0001_disputes"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "evidence_packs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("payment_id", sa.String(255), nullable=False),
        sa.Column("dispute_id", sa.Integer(), sa.ForeignKey("disputes.id")),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.UniqueConstraint("payment_id", "version", name="uq_evidence_packs_payment_version"),
    )
    op.create_index("ix_evidence_packs_dispute_id", "evidence_packs", ["dispute_id"])


def downgrade():
    op.drop_table("evidence_packs")
