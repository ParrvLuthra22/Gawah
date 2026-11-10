from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def now() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_result_value(self, value, dialect):
        # SQLite drops timezone metadata. Values written there are always UTC.
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class Source(StrEnum):
    PAYPAL_API = "PAYPAL_API"
    MOCK_ENGINE = "MOCK_ENGINE"
    SIMULATED_FIXTURE = "SIMULATED_FIXTURE"


class Base(DeclarativeBase):
    pass


class Dispute(Base):
    __tablename__ = "disputes"
    __table_args__ = (
        UniqueConstraint("source", "paypal_dispute_id", name="uq_disputes_source_paypal_id"),
        CheckConstraint("amount_minor >= 0", name="ck_disputes_amount_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    paypal_dispute_id: Mapped[str] = mapped_column(String(255), index=True)
    transaction_id: Mapped[str | None] = mapped_column(String(255), index=True)
    payment_id: Mapped[str | None] = mapped_column(String(255), index=True)
    link_status: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(128), index=True)
    amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str | None] = mapped_column(String(3))
    buyer: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    merchant: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    deadline: Mapped[datetime | None] = mapped_column(UTCDateTime())
    source: Mapped[str] = mapped_column(String(32))
    raw_response: Mapped[dict] = mapped_column(JSON)
    last_synced_at: Mapped[datetime] = mapped_column(UTCDateTime())


class DisputeStatusHistory(Base):
    __tablename__ = "dispute_status_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    dispute_id: Mapped[int] = mapped_column(ForeignKey("disputes.id"), index=True)
    previous_status: Mapped[str | None] = mapped_column(String(128))
    new_status: Mapped[str] = mapped_column(String(128))
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    source: Mapped[str] = mapped_column(String(32))


class DisputeSyncLog(Base):
    __tablename__ = "dispute_sync_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    finished_at: Mapped[datetime] = mapped_column(UTCDateTime())
    result: Mapped[str] = mapped_column(String(16))
    retrieved: Mapped[int]
    created: Mapped[int]
    updated: Mapped[int]
    unmatched: Mapped[int]
    errors: Mapped[list[str]] = mapped_column(JSON)


class EvidencePack(Base):
    __tablename__ = "evidence_packs"
    __table_args__ = (
        UniqueConstraint("payment_id", "version", name="uq_evidence_packs_payment_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[str] = mapped_column(String(255))
    dispute_id: Mapped[int | None] = mapped_column(ForeignKey("disputes.id"), index=True)
    version: Mapped[int]
    fingerprint: Mapped[str] = mapped_column(String(64))
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    snapshot: Mapped[dict] = mapped_column(JSON)


class CounselAnalysis(Base):
    __tablename__ = "counsel_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[str] = mapped_column(String(255), index=True)
    dispute_id: Mapped[int | None] = mapped_column(ForeignKey("disputes.id"))
    evidence_pack_id: Mapped[int] = mapped_column(ForeignKey("evidence_packs.id"), index=True)
    evidence_pack_version: Mapped[int]
    analysis_status: Mapped[str] = mapped_column(String(24))
    review_status: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    snapshot: Mapped[dict] = mapped_column(JSON)


class ResolutionAttempt(Base):
    __tablename__ = "resolution_attempts"
    __table_args__ = (UniqueConstraint("request_id", name="uq_resolution_attempt_request_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    dispute_id: Mapped[int] = mapped_column(ForeignKey("disputes.id"), index=True)
    capture_id: Mapped[str | None] = mapped_column(String(255), index=True)
    action: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    workflow_id: Mapped[int | None] = mapped_column(
        ForeignKey("resolution_workflows.id", name="fk_resolution_attempt_workflow"), index=True
    )
    request_id: Mapped[str | None] = mapped_column(String(32))
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    response_reference: Mapped[str | None] = mapped_column(String(255))


class ResolutionWorkflow(Base):
    __tablename__ = "resolution_workflows"

    id: Mapped[int] = mapped_column(primary_key=True)
    dispute_id: Mapped[int] = mapped_column(ForeignKey("disputes.id"), index=True)
    payment_id: Mapped[str] = mapped_column(String(255), index=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("counsel_analyses.id"))
    evidence_pack_id: Mapped[int] = mapped_column(ForeignKey("evidence_packs.id"))
    recommendation: Mapped[str] = mapped_column(String(32))
    strategy: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(32), index=True)
    assigned_reviewer: Mapped[str | None] = mapped_column(String(64))
    revision: Mapped[int]
    approval_revision: Mapped[int | None]
    plan: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    information: Mapped[list[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    paypal_status: Mapped[str | None] = mapped_column(String(128))
    paypal_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ResolutionEvent(Base):
    __tablename__ = "resolution_events"
    __table_args__ = (
        UniqueConstraint("workflow_id", "revision", name="uq_resolution_event_revision"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("resolution_workflows.id"), index=True)
    revision: Mapped[int]
    actor_id: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(48))
    previous_status: Mapped[str | None] = mapped_column(String(32))
    new_status: Mapped[str] = mapped_column(String(32))
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
