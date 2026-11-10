"""Create System 3 disputes, status history, and synchronization logs."""

import sqlalchemy as sa
from alembic import op

revision = "0001_disputes"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "disputes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("paypal_dispute_id", sa.String(255), nullable=False),
        sa.Column("transaction_id", sa.String(255)),
        sa.Column("payment_id", sa.String(255)),
        sa.Column("link_status", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(128)),
        sa.Column("status", sa.String(128), nullable=False),
        sa.Column("amount_minor", sa.BigInteger()),
        sa.Column("currency", sa.String(3)),
        sa.Column("buyer", sa.JSON(none_as_null=True)),
        sa.Column("merchant", sa.JSON(none_as_null=True)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("deadline", sa.DateTime(timezone=True)),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("raw_response", sa.JSON(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "paypal_dispute_id", name="uq_disputes_source_paypal_id"),
        sa.CheckConstraint("amount_minor >= 0", name="ck_disputes_amount_nonnegative"),
    )
    for name in ("paypal_dispute_id", "transaction_id", "payment_id", "status", "created_at"):
        op.create_index(f"ix_disputes_{name}", "disputes", [name])
    op.create_table(
        "dispute_status_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dispute_id", sa.Integer(), sa.ForeignKey("disputes.id"), nullable=False),
        sa.Column("previous_status", sa.String(128)),
        sa.Column("new_status", sa.String(128), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
    )
    op.create_index(
        "ix_dispute_status_history_dispute_id", "dispute_status_history", ["dispute_id"]
    )
    op.create_table(
        "dispute_sync_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("retrieved", sa.Integer(), nullable=False),
        sa.Column("created", sa.Integer(), nullable=False),
        sa.Column("updated", sa.Integer(), nullable=False),
        sa.Column("unmatched", sa.Integer(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
    )
    op.create_index("ix_dispute_sync_logs_started_at", "dispute_sync_logs", ["started_at"])


def downgrade():
    op.drop_table("dispute_sync_logs")
    op.drop_table("dispute_status_history")
    op.drop_table("disputes")
