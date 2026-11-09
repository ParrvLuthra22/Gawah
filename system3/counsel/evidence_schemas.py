from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

VerificationStatus = Literal["VERIFIED", "INVALID", "PENDING", "UNAVAILABLE"]
SourceSystem = Literal["SYSTEM_2", "MOCK_ENGINE", "SYSTEM_3", "SYSTEM_1"]


class SourceCheck(BaseModel):
    source_system: SourceSystem
    reference: str
    status: Literal["AVAILABLE", "NOT_FOUND", "UNAVAILABLE", "INVALID"]
    detail: str | None = None


class Verification(BaseModel):
    status: VerificationStatus
    reference: str | None = None
    detail: str
    result: dict[str, Any] | None = None


class VerificationSummary(BaseModel):
    mandate_signature: Verification
    cart_signature: Verification
    ledger: Verification
    engine_ledger: Verification


class EvidenceItem(BaseModel):
    id: str
    type: str
    source_system: SourceSystem
    source_record_id: str
    source_reference: str
    dispute_id: str | None
    payment_id: str
    timestamp: datetime | None
    retrieved_at: datetime
    verification_status: VerificationStatus = "UNAVAILABLE"
    payload: dict[str, Any]


class TimelineEvent(BaseModel):
    id: str
    type: str
    timestamp: datetime | None
    source_system: SourceSystem
    record_id: str
    evidence_id: str
    description: str
    verification_status: VerificationStatus


class EvidenceIssue(BaseModel):
    code: str
    type: Literal["MISSING", "INVALID", "CONTRADICTION"]
    severity: Literal["INFO", "WARNING", "HIGH"]
    explanation: str
    evidence_ids: list[str]
    source_references: list[str]


class EvidenceIssues(BaseModel):
    missing_evidence: list[EvidenceIssue]
    invalid_evidence: list[EvidenceIssue]
    contradictions: list[EvidenceIssue]


class Completeness(BaseModel):
    available: int
    missing: int
    verified: int
    unverified: int


class EvidenceSnapshot(EvidenceIssues):
    dispute_id: str | None
    dispute_record_id: int | None
    payment_id: str
    evidence: list[EvidenceItem]
    timeline: list[TimelineEvent]
    verification_summary: VerificationSummary
    sources: list[SourceCheck]
    completeness: Completeness


class EvidencePackResponse(EvidenceSnapshot):
    id: int
    generated_at: datetime
    version: int
