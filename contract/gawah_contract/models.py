"""The Gawah contract. All money is integer minor units (cents for USD)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    DisputeSource,
    DisputeStatus,
    LedgerEntryType,
    MandateStatus,
    PaymentStatus,
    ReasonCode,
    Verdict,
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Mandate(_Model):
    id: str
    user_id: str
    category: str
    item_description: str
    max_amount_minor: int = Field(ge=0, description="Spending cap in minor units")
    currency: str = Field(min_length=3, max_length=3)
    allowed_merchants: list[str]
    issued_at: datetime
    expires_at: datetime
    status: MandateStatus
    signature: str
    signing_alg: str
    key_id: str


class MandateCreate(_Model):
    """Body of POST /v1/mandates. The engine assigns id, timestamps and signature."""

    user_id: str
    category: str
    item_description: str
    max_amount_minor: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    allowed_merchants: list[str]
    expires_at: datetime


class BudgetStatus(_Model):
    """Body of GET /v1/mandates/{id}/budget. remaining = max - spent - reserved."""

    mandate_id: str
    max_amount_minor: int = Field(ge=0)
    spent_minor: int = Field(ge=0)
    reserved_minor: int = Field(ge=0)
    remaining_minor: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)


class Product(_Model):
    id: str
    title: str
    url: str


class LineItem(_Model):
    sku: str
    title: str
    variant: str | None = Field(default=None, description="Size or variant, e.g. 'size 9'")
    quantity: int = Field(ge=1)
    unit_price_minor: int = Field(ge=0)


class SignedCart(_Model):
    cart_id: str
    merchant_id: str
    line_items: list[LineItem]
    total_minor: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    expires_at: datetime
    signature: str
    key_id: str


class PurchaseProposal(_Model):
    id: str
    mandate_id: str
    agent_id: str
    merchant_id: str
    product: Product
    quantity: int = Field(ge=1)
    unit_price_minor: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    rationale: str
    signed_cart: SignedCart
    created_at: datetime


class GuardianDecision(_Model):
    id: str
    proposal_id: str
    verdict: Verdict
    reasons: list[ReasonCode]
    explanation: str
    checked_at: datetime


class PaymentRecord(_Model):
    id: str
    proposal_id: str
    decision_id: str
    paypal_order_id: str | None
    paypal_capture_id: str | None
    status: PaymentStatus
    amount_minor: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    idempotency_key: str
    custom_id: str = Field(description="Ledger reference attached to the PayPal order")
    created_at: datetime


class LedgerEntry(_Model):
    seq: int = Field(ge=1)
    ts: datetime
    type: LedgerEntryType
    subject_id: str
    data: dict[str, Any]
    prev_hash: str
    hash: str
    signature: str
    key_id: str


class DisputeCase(_Model):
    id: str
    paypal_dispute_id: str | None
    payment_id: str
    status: DisputeStatus
    reason: str
    amount_minor: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    opened_at: datetime
    source: DisputeSource


class ErrorBody(_Model):
    code: str
    message: str
    details: dict[str, Any] | list[Any] | None = None
    request_id: str


class ErrorResponse(_Model):
    error: ErrorBody


# ---- Request/response wrappers used by the System 2 API ----


class ProposalResult(_Model):
    decision: GuardianDecision
    payment: PaymentRecord | None


class VerificationCheck(_Model):
    """One named check inside a verification result (additive; see docs/CONTRACT.md)."""

    name: str = Field(
        description="ledger_chain | mandate_signature | cart_signature | payment_reference_match"
    )
    ok: bool
    detail: str = Field(default="", description="Human-readable outcome of this check")


class LedgerVerifyResult(_Model):
    ok: bool
    checked: int
    first_broken_seq: int | None = None
    reason: str | None = Field(default=None, description="Why verification failed; null when ok")
    checks: list[VerificationCheck] = Field(
        default_factory=list,
        description="Per-check breakdown (evidence bundles); empty for a plain ledger verify",
    )


class LedgerHead(_Model):
    """Signed head of the ledger: an external anchor the UI can display (additive)."""

    seq: int = Field(description="Sequence number of the latest entry; 0 for an empty ledger")
    hash: str = Field(description="Hash of the latest entry; 64 zeros for an empty ledger")
    message: str = Field(description="Exact text signed: gawah-ledger-head:{seq}:{hash}")
    signature: str = Field(description="ES256 (base64url r||s) over `message` by the ledger key")
    key_id: str


class EvidenceBundle(_Model):
    mandate: Mandate
    proposal: PurchaseProposal
    signed_cart: SignedCart
    decision: GuardianDecision
    payment: PaymentRecord
    ledger_entries: list[LedgerEntry]
    verification: LedgerVerifyResult


class MerchantKey(_Model):
    merchant_id: str
    key_id: str
    alg: str = "ed25519"
    public_key_pem: str


class RefundRequest(_Model):
    reason: str | None = None
