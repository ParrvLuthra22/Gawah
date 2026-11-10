"""Persist approval-gated workflows and append-only review history."""

import sqlalchemy as sa
from alembic import op

revision = "0005_resolution_workflows"
down_revision = "0004_resolution_attempts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "resolution_workflows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dispute_id", sa.Integer(), sa.ForeignKey("disputes.id"), nullable=False),
        sa.Column("payment_id", sa.String(255), nullable=False),
        sa.Column(
            "analysis_id", sa.Integer(), sa.ForeignKey("counsel_analyses.id"), nullable=False
        ),
        sa.Column(
            "evidence_pack_id", sa.Integer(), sa.ForeignKey("evidence_packs.id"), nullable=False
        ),
        sa.Column("recommendation", sa.String(32), nullable=False),
        sa.Column("strategy", sa.String(24), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("assigned_reviewer", sa.String(64)),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("approval_revision", sa.Integer()),
        sa.Column("plan", sa.JSON()),
        sa.Column("information", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("paypal_status", sa.String(128)),
        sa.Column("paypal_checked_at", sa.DateTime(timezone=True)),
    )
    for column in ("dispute_id", "payment_id", "status", "updated_at"):
        op.create_index(f"ix_resolution_workflows_{column}", "resolution_workflows", [column])
    op.create_table(
        "resolution_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workflow_id", sa.Integer(), sa.ForeignKey("resolution_workflows.id"), nullable=False
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("previous_status", sa.String(32)),
        sa.Column("new_status", sa.String(32), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workflow_id", "revision", name="uq_resolution_event_revision"),
    )
    for column in ("workflow_id", "created_at"):
        op.create_index(f"ix_resolution_events_{column}", "resolution_events", [column])
    with op.batch_alter_table("resolution_attempts") as batch:
        batch.add_column(sa.Column("workflow_id", sa.Integer()))
        batch.add_column(sa.Column("request_id", sa.String(32)))
        batch.add_column(sa.Column("finished_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("response_reference", sa.String(255)))
        batch.create_foreign_key(
            "fk_resolution_attempt_workflow", "resolution_workflows", ["workflow_id"], ["id"]
        )
        batch.create_unique_constraint("uq_resolution_attempt_request_id", ["request_id"])
        batch.create_index("ix_resolution_attempts_workflow_id", ["workflow_id"])


def downgrade():
    with op.batch_alter_table("resolution_attempts") as batch:
        batch.drop_index("ix_resolution_attempts_workflow_id")
        batch.drop_constraint("uq_resolution_attempt_request_id", type_="unique")
        batch.drop_constraint("fk_resolution_attempt_workflow", type_="foreignkey")
        for column in ("workflow_id", "request_id", "finished_at", "response_reference"):
            batch.drop_column(column)
    op.drop_table("resolution_events")
    op.drop_table("resolution_workflows")
