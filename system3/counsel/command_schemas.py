from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .models import Source
from .schemas import LinkStatus, PageParams


class Period(BaseModel):
    start: date | None = None
    end: date | None = None
    days: int | None = Field(default=None, ge=1, le=90)
    source: Source | None = None

    @model_validator(mode="after")
    def validate_period(self):
        if self.days is not None and (self.start is not None or self.end is not None):
            raise ValueError("Use a day count or an explicit date range.")
        if (self.start is None) != (self.end is None) or (
            self.start and self.end and self.start > self.end
        ):
            raise ValueError("Provide both dates in chronological order.")
        return self


class CaseFilters(Period, PageParams):
    search: str | None = Field(default=None, max_length=255)
    status: str | None = Field(default=None, max_length=128)
    reason: str | None = Field(default=None, max_length=128)
    sort: Literal[
        "paypal_dispute_id",
        "payment_id",
        "reason",
        "amount_minor",
        "status",
        "created_at",
        "updated_at",
        "deadline",
        "evidence_status",
        "counsel_status",
    ] = "created_at"
    order: Literal["asc", "desc"] = "desc"


class Group(BaseModel):
    label: str | None
    count: int


class Amount(BaseModel):
    currency: str
    amount_minor: int


class DailyDisputes(BaseModel):
    day: date
    count: int
    amounts: list[Amount]


class SyncState(BaseModel):
    finished_at: datetime
    result: str


class Overview(BaseModel):
    total: int
    open: int
    resolved: int
    awaiting_merchant: int
    due_soon: int
    overdue: int
    unlinked: int
    amounts: list[Amount]
    unknown_amounts: int
    unknown_open_dates: int
    resolution_rate: float | None
    average_observed_resolution_hours: float | None
    resolution_samples: int
    daily: list[DailyDisputes]
    by_status: list[Group]
    by_reason: list[Group]
    by_outcome: list[Group]
    latest_sync: SyncState | None
    as_of: datetime


class CaseRow(BaseModel):
    id: int
    paypal_dispute_id: str
    payment_id: str | None
    link_status: LinkStatus
    source: Source
    reason: str | None
    status: str
    amount_minor: int | None
    currency: str | None
    created_at: datetime | None
    updated_at: datetime | None
    deadline: datetime | None
    evidence_status: str | None
    evidence_version: int | None
    counsel_status: str | None
    counsel_version: int | None


class CasePage(BaseModel):
    items: list[CaseRow]
    total: int
    page: int
    page_size: int
    as_of: datetime
    unavailable: list[Literal["evidence", "counsel"]]


class ArtifactRef(BaseModel):
    id: int
    dispute_id: int | None
    paypal_dispute_id: str | None
    source: Source | None
    payment_id: str


class PackRow(ArtifactRef):
    version: int
    generated_at: datetime
    missing: int
    invalid: int
    contradictions: int


class EvidenceStats(BaseModel):
    packs_generated: int
    latest_packs: int
    packs_with_gaps: int
    missing_records: int
    invalid_records: int
    contradictions: int
    verification_rate: float | None
    verification: list[Group]
    recent: list[PackRow]


class AnalysisRow(ArtifactRef):
    created_at: datetime
    status: str
    review_status: str
    recommendation: str | None
    evidence_version: int


class CounselStats(BaseModel):
    analyses: int
    recommendations: int
    pending_reviews: int
    drafts: int
    accepted: int | None = None
    rejected: int | None = None
    by_recommendation: list[Group]
    by_status: list[Group]
    recent: list[AnalysisRow]


class ActivityEvent(BaseModel):
    id: str
    timestamp: datetime
    type: str
    status: str | None
    dispute_id: int | None
    paypal_dispute_id: str | None
    source: Source | None
    payment_id: str | None = None
    evidence_version: int | None = None


class ActivityFeed(BaseModel):
    items: list[ActivityEvent]
    unavailable: list[str]
