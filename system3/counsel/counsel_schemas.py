from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .evidence_schemas import (
    EvidenceIssue,
    SourceCheck,
    SourceType,
    Verification,
    VerificationStatus,
)


class EvidenceTrust(BaseModel):
    id: str
    type: str
    source_type: SourceType
    source_reference: str
    verification_status: VerificationStatus
    reported_status: VerificationStatus
    verification: Verification | None

    @property
    def reliable(self) -> bool:
        return self.source_type == "REAL_RECORD" and self.verification_status == "VERIFIED"


class Classification(BaseModel):
    records: list[EvidenceTrust]
    contradictory_evidence: list[EvidenceIssue]
    missing_evidence: list[EvidenceIssue]
    invalid_evidence: list[EvidenceIssue]
    invalid_evidence_ids: list[str]
    unverified_evidence_ids: list[str]
    unavailable_evidence_ids: list[str]
    unavailable_sources: list[SourceCheck]
    verification_warnings: list[str]


Recommendation = Literal["DEFEND", "CONCEDE_REMEDY", "REQUEST_MORE_EVIDENCE"]


class Finding(BaseModel):
    id: str
    text: str
    evidence_ids: list[str]


class CaseAssessment(BaseModel):
    category: str
    dispute_reason: str | None
    supported_outcome: Recommendation
    findings: list[Finding]
    documentation_needed: str


class ModelPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    recommendation: Recommendation
    confidence: float | None = Field(ge=0, le=1)
    reasoning_finding_ids: list[str] = Field(max_length=10)
    draft_finding_ids: list[str] = Field(max_length=10)


class CounselDecision(BaseModel):
    recommendation: Recommendation
    confidence: float | None = Field(ge=0, le=1)
    conflict: str | None = None


class CounselOutcome(CounselDecision):
    executive_summary: str
    supporting_evidence_ids: list[str]
    reasoning: list[Finding]
    draft_response: str
    draft_evidence_ids: list[str]
    human_review_required: Literal[True] = True


class AnalysisError(BaseModel):
    code: str
    message: str
    status: int


class AnalysisSnapshot(BaseModel):
    dispute_id: str | None
    dispute_reason: str | None
    category: str
    classification: Classification
    outcome: CounselOutcome | None
    error: AnalysisError | None

    @model_validator(mode="after")
    def validate_result(self):
        if (self.error is None) == (self.outcome is None):
            raise ValueError("An analysis has exactly one outcome or error.")
        return self


class CounselAnalysisResponse(AnalysisSnapshot):
    id: int
    payment_id: str
    dispute_record_id: int | None
    evidence_pack_id: int
    evidence_pack_version: int
    analysis_status: Literal["COMPLETED", "REVIEW_REQUIRED", "FAILED"]
    review_status: Literal["PENDING"] = "PENDING"
    human_review_required: Literal[True] = True
    model: str
    created_at: datetime
