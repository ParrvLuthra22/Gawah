"""Store append-only Counsel analysis history."""

import sqlalchemy as sa
from alembic import op

revision = "0003_counsel_analyses"
down_revision = "0002_evidence_packs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "counsel_analyses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("payment_id", sa.String(255), nullable=False),
        sa.Column("dispute_id", sa.Integer(), sa.ForeignKey("disputes.id")),
        sa.Column(
            "evidence_pack_id", sa.Integer(), sa.ForeignKey("evidence_packs.id"), nullable=False
        ),
        sa.Column("evidence_pack_version", sa.Integer(), nullable=False),
        sa.Column("analysis_status", sa.String(24), nullable=False),
        sa.Column("review_status", sa.String(16), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
    )
    op.create_index("ix_counsel_analyses_payment_id", "counsel_analyses", ["payment_id"])
    op.create_index(
        "ix_counsel_analyses_evidence_pack_id", "counsel_analyses", ["evidence_pack_id"]
    )


def downgrade():
    op.drop_table("counsel_analyses")
