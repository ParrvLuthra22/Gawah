from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .config import OperatorId
from .counsel_schemas import CounselOutcome, Recommendation
from .evidence_schemas import EvidenceIssue, SourceType, VerificationStatus, VerificationSummary
from .resolution import Action, ActionPlan, Attempt
from .resolution_auth import Actor
from .schemas import PageParams

WorkflowStatus = Literal[
    "PENDING_REVIEW",
    "ADDITIONAL_INFORMATION_REQUIRED",
    "READY_FOR_APPROVAL",
    "APPROVED",
    "SUBMITTING",
    "SUBMITTED",
    "CONFIRMED",
    "FAILED",
    "UNKNOWN",
    "REJECTED",
    "CANCELLED",
]
Strategy = Literal["DEFEND", "REMEDY", "INFORMATION"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OpenWorkflow(Input):
    dispute_id: int = Field(ge=1, le=2_147_483_647)
    assign_to_self: bool = True


class Revision(Input):
    revision: int = Field(ge=1, le=2_147_483_647)


class SubmitWorkflow(Revision):
    confirmation: str = Field(min_length=1, max_length=255)


class Review(Revision):
    command: Literal[
        "prepare",
        "approve",
        "request-info",
        "receive-info",
        "note",
        "refresh",
        "reject",
        "cancel",
        "assign",
    ]
    action: Action | None = None
    text: str = Field(default="", max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    override_reason: str | None = Field(default=None, max_length=500)
    team: str | None = Field(default=None, max_length=100)
    request_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    reviewer: OperatorId | None = None


class QueueFilters(PageParams):
    search: str | None = Field(default=None, max_length=255)
    status: WorkflowStatus | None = None
    strategy: Strategy | None = None
    sort: Literal[
        "paypal_dispute_id",
        "payment_id",
        "reason",
        "amount_minor",
        "recommendation",
        "strategy",
        "status",
        "assigned_reviewer",
        "deadline",
        "updated_at",
    ] = "updated_at"
    order: Literal["asc", "desc"] = "desc"


class WorkflowRow(BaseModel):
    id: int
    dispute_id: int
    paypal_dispute_id: str
    payment_id: str
    reason: str | None
    amount_minor: int | None
    currency: str | None
    recommendation: Recommendation
    strategy: Strategy
    status: WorkflowStatus
    assigned_reviewer: str | None
    deadline: datetime | None
    updated_at: datetime


class CaseEvidence(BaseModel):
    id: str
    type: str
    source_record_id: str
    source_type: SourceType
    timestamp: datetime | None
    verification_status: VerificationStatus


class InformationRequest(BaseModel):
    id: str
    team: str
    description: str
    response: str | None = None
    requested_at: datetime
    fulfilled_at: datetime | None = None


class ReviewEvent(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    revision: int
    actor_id: str
    event_type: str
    previous_status: WorkflowStatus | None
    new_status: WorkflowStatus
    details: dict
    created_at: datetime


class WorkflowDetail(WorkflowRow):
    revision: int
    analysis_id: int
    evidence_version: int
    stale: bool
    actor: Actor
    counsel: CounselOutcome
    plan: ActionPlan | None
    approval: ReviewEvent | None
    evidence: list[CaseEvidence]
    verification: VerificationSummary
    gaps: list[EvidenceIssue]
    requests: list[InformationRequest]
    attempts: list[Attempt]
    activity: list[ReviewEvent]
    dispute_status: str
    paypal_status: str | None
    paypal_checked_at: datetime | None
    submitted_at: datetime | None
    completed_at: datetime | None


class ResolutionStats(BaseModel):
    total: int
    awaiting_approval: int
    pending_information: int
    submitted: int
    confirmed: int
    failed: int
    unknown: int
    by_status: dict[str, int]
