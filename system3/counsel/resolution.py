import re
from datetime import datetime
from decimal import Decimal
from typing import Literal

import httpx
from gawah_contract import LedgerHead, PaymentRecord, PaymentStatus, RefundRequest
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .clients import DISPUTES_PATH, IntegrationError, PayPalClient, System2Client
from .dashboard import system2_record
from .evidence_collection import REFERENCE_PATTERN
from .models import Dispute, ResolutionAttempt, Source
from .sync import PAYPAL_ID, Money, PayPalDispute

Action = Literal["refund", "submit-evidence", "accept-claim", "reply-inquiry", "offer-refund"]
MONETARY = ("refund", "accept-claim", "offer-refund")
PAYPAL_ACTION = {
    "submit-evidence": "provide-evidence",
    "accept-claim": "accept-claim",
    "reply-inquiry": "send-message",
    "offer-refund": "make-offer",
}


class Attempt(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    action: Action
    status: Literal["RUNNING", "SUCCEEDED", "FAILED", "UNKNOWN"]
    created_at: datetime
    finished_at: datetime | None
    request_id: str | None
    response_reference: str | None


class Option(BaseModel):
    action: Action
    amount_minor: int | None = None
    currency: str | None = None
    label: str | None = None


class ResolutionOptions(BaseModel):
    options: list[Option] = Field(default_factory=list)
    attempts: list[Attempt] = Field(default_factory=list)
    message: str | None = None


class ActionLink(BaseModel):
    rel: str
    href: str
    method: str | None = None


class EvidenceRequirement(BaseModel):
    evidence_type: str
    source: str | None = None
    action: str | None = None
    response_option: str | None = None
    mandatory: bool = False
    notes: str | None = None
    date: AwareDatetime | None = None


class AcceptClaim(BaseModel):
    accept_claim_types: list[str] = Field(default_factory=list)


class MakeOffer(BaseModel):
    offer_types: list[str] = Field(default_factory=list)


class ResponseOptions(BaseModel):
    accept_claim: AcceptClaim | None = None
    make_offer: MakeOffer | None = None


class CaseMessage(BaseModel):
    posted_by: str
    time_posted: AwareDatetime | None = None
    content: str | None = None


class Outcome(BaseModel):
    outcome_code: str | None = None
    amount_refunded: Money | None = None


class SandboxCase(PayPalDispute):
    links: list[ActionLink] = Field(default_factory=list)
    evidences: list[EvidenceRequirement] = Field(default_factory=list)
    allowed_response_options: ResponseOptions | None = None
    dispute_life_cycle_stage: str | None = None
    messages: list[CaseMessage] = Field(default_factory=list)
    dispute_outcome: Outcome | None = None

    @property
    def capture_id(self) -> str | None:
        if len(self.disputed_transactions) != 1:
            return None
        return self.disputed_transactions[0].seller_transaction_id

    def path(self, rel: str) -> str:
        return f"{DISPUTES_PATH}/{self.dispute_id}/{rel}"

    def offers(self, rel: str) -> bool:
        for link in self.links:
            if link.rel != rel or link.method != "POST":
                continue
            try:
                url = httpx.URL(link.href)
            except httpx.InvalidURL:
                continue
            if (
                url.scheme == "https"
                and url.host in {"api-m.sandbox.paypal.com", "api.sandbox.paypal.com"}
                and url.port in (None, 443)
                and not (url.userinfo or url.query or url.fragment)
                and url.path == self.path(rel)
            ):
                return True
        return False


def get_dispute(db: Session, id: int) -> Dispute:
    dispute = db.get(Dispute, id)
    if dispute is None:
        raise IntegrationError("DISPUTE_NOT_FOUND", "Dispute not found.", 404)
    return dispute


def disabled_reason(dispute: Dispute, paypal: PayPalClient) -> str | None:
    if dispute.source == Source.SIMULATED_FIXTURE:
        if paypal.settings.local_sandbox_resolution:
            return None
        return "Simulated disputes are read-only."
    if dispute.source != Source.PAYPAL_API:
        return "This dispute source is read-only."
    if not paypal.settings.enable_sandbox_resolution:
        return "Sandbox resolution is disabled."
    if not paypal.configured:
        return "PayPal Sandbox credentials are missing."
    return None


def simulated_system2_payment(system2: System2Client, dispute: Dispute) -> PaymentRecord | None:
    """Load the only financial source permitted for a simulated dispute: System 2."""
    payment_id = dispute.payment_id
    if (
        dispute.link_status != "LINKED"
        or not payment_id
        or not re.fullmatch(REFERENCE_PATTERN, payment_id)
    ):
        return None
    payment = system2_record(system2, f"/v1/payments/{payment_id}", TypeAdapter(PaymentRecord))
    capture = payment.paypal_capture_id
    if (
        payment.id != payment_id
        or not capture
        or capture.upper().startswith(("FAKE", "SIMULATED"))
    ):
        return None
    return payment


def simulated_refund_option(system2: System2Client, dispute: Dispute) -> Option | None:
    payment = simulated_system2_payment(system2, dispute)
    if payment is None or payment.status != PaymentStatus.PAID or payment.amount_minor <= 0:
        return None
    return Option(
        action="refund",
        amount_minor=payment.amount_minor,
        currency=payment.currency,
        label="Simulated dispute, sandbox refund",
    )


def current_case(paypal: PayPalClient, dispute: Dispute) -> SandboxCase:
    try:
        dispute_id = PAYPAL_ID.validate_python(dispute.paypal_dispute_id)
        case = SandboxCase.model_validate(paypal.get_dispute(dispute_id))
        if case.dispute_amount:
            case.dispute_amount.minor_units()
        return case
    except ValueError:
        raise IntegrationError(
            "INVALID_RESPONSE", "PayPal returned invalid dispute details."
        ) from None


def load_payment(
    system2: System2Client, dispute: Dispute, case: SandboxCase
) -> PaymentRecord | None:
    payment_id = dispute.payment_id
    if (
        dispute.link_status != "LINKED"
        or not payment_id
        or not re.fullmatch(REFERENCE_PATTERN, payment_id)
        or not case.capture_id
        or case.capture_id.upper().startswith("FAKE")
    ):
        return None
    payment = system2_record(system2, f"/v1/payments/{payment_id}", TypeAdapter(PaymentRecord))
    if payment.id != payment_id or payment.paypal_capture_id != case.capture_id:
        return None
    return payment


def candidates(system2: System2Client, dispute: Dispute, case: SandboxCase) -> list[Option]:
    options = []
    seller_requests = [
        evidence for evidence in case.evidences if evidence.source == "REQUESTED_FROM_SELLER"
    ]
    if (
        case.status == "WAITING_FOR_SELLER_RESPONSE"
        and case.offers("provide-evidence")
        and any(
            e.evidence_type == "OTHER" and e.action in (None, "PROVIDE_EVIDENCE")
            for e in seller_requests
        )
        and not any(
            e.mandatory and e.evidence_type != "OTHER" and e.action in (None, "PROVIDE_EVIDENCE")
            for e in seller_requests
        )
    ):
        options.append(Option(action="submit-evidence"))
    if (
        case.dispute_life_cycle_stage == "INQUIRY"
        and case.status in {"OPEN", "WAITING_FOR_SELLER_RESPONSE"}
        and case.offers("send-message")
    ):
        options.append(Option(action="reply-inquiry"))

    if case.status in {"RESOLVED", "UNDER_REVIEW", "WAITING_FOR_BUYER_RESPONSE"}:
        return options
    payment = None
    try:
        payment = load_payment(system2, dispute, case)
        if payment and payment.status == PaymentStatus.PAID and payment.amount_minor > 0:
            head = system2_record(system2, "/v1/ledger/head", TypeAdapter(LedgerHead))
            if head.key_id.lower() != "mock" and not head.signature.startswith("SIMULATED-"):
                options.append(
                    Option(
                        action="refund",
                        amount_minor=payment.amount_minor,
                        currency=payment.currency,
                    )
                )
    except IntegrationError:
        # Leave payment-dependent actions unavailable if the capture cannot be matched.
        pass
    eligible_refund = (
        case.capture_id
        and not case.capture_id.upper().startswith("FAKE")
        and case.dispute_amount
        and payment
        and payment.status == PaymentStatus.PAID
        and case.dispute_amount.currency_code == payment.currency
        and 0 < case.dispute_amount.minor_units() <= payment.amount_minor
    )
    response_options = case.allowed_response_options
    claim = response_options.accept_claim if response_options else None
    offer = response_options.make_offer if response_options else None
    if (
        eligible_refund
        and case.offers("accept-claim")
        and claim
        and "REFUND" in claim.accept_claim_types
        and not any(
            e.mandatory and e.action == "ACCEPT_CLAIM" and e.response_option in (None, "REFUND")
            for e in seller_requests
        )
    ):
        options.append(
            Option(
                action="accept-claim",
                amount_minor=case.dispute_amount.minor_units(),
                currency=case.dispute_amount.currency_code,
            )
        )
    if (
        eligible_refund
        and case.dispute_life_cycle_stage == "INQUIRY"
        and case.status == "WAITING_FOR_SELLER_RESPONSE"
        and case.offers("make-offer")
        and offer
        and "REFUND" in offer.offer_types
    ):
        options.append(
            Option(
                action="offer-refund",
                amount_minor=case.dispute_amount.minor_units(),
                currency=case.dispute_amount.currency_code,
            )
        )
    return options


def conflicting_attempt(db: Session, id: int, action: Action, capture: str | None) -> bool:
    same_case = ResolutionAttempt.dispute_id == id
    in_flight = ResolutionAttempt.status.in_(("RUNNING", "UNKNOWN"))
    succeeded = ResolutionAttempt.status == "SUCCEEDED"
    condition = same_case & (
        in_flight
        | (succeeded & ResolutionAttempt.action.in_((action, "accept-claim", "offer-refund")))
    )
    if action in MONETARY:
        same_capture = ResolutionAttempt.capture_id == capture if capture else same_case
        condition |= (
            or_(same_case, same_capture)
            & ResolutionAttempt.action.in_(MONETARY)
            & (in_flight | succeeded)
        )
    return db.scalar(select(ResolutionAttempt.id).where(condition).limit(1)) is not None


def get_options(
    db: Session, paypal: PayPalClient, system2: System2Client, id: int
) -> ResolutionOptions:
    dispute = get_dispute(db, id)
    reason = disabled_reason(dispute, paypal)
    attempts = db.scalars(
        select(ResolutionAttempt)
        .where(ResolutionAttempt.dispute_id == id, ResolutionAttempt.workflow_id.is_(None))
        .order_by(ResolutionAttempt.id.desc())
        .limit(20)
    ).all()
    options = []
    if not reason:
        if dispute.source == Source.SIMULATED_FIXTURE:
            try:
                option = simulated_refund_option(system2, dispute)
            except IntegrationError:
                option = None
            options = (
                [option]
                if option and not conflicting_attempt(db, id, option.action, None)
                else []
            )
        else:
            case = current_case(paypal, dispute)
            options = [
                option
                for option in candidates(system2, dispute, case)
                if not conflicting_attempt(db, id, option.action, case.capture_id)
            ]
        if not options:
            reason = "No supported resolution action is currently available for this case."
    return ResolutionOptions(options=options, attempts=attempts, message=reason)


class ActionPlan(BaseModel):
    option: Option
    paypal_status: str
    paypal_stage: str | None
    capture_id: str | None
    payload: dict
    evidence_ids: list[str] = Field(default_factory=list)
    override_reason: str | None = None


def eligible_option(
    system2: System2Client, dispute: Dispute, case: SandboxCase | None, action: Action
) -> Option:
    if dispute.source == Source.SIMULATED_FIXTURE:
        option = simulated_refund_option(system2, dispute)
        if option and action == option.action:
            return option
        raise IntegrationError(
            "ACTION_UNAVAILABLE",
            "Only a paid System 2 sandbox refund is available for this simulated dispute.",
            409,
        )
    if case is None:
        raise IntegrationError("ACTION_UNAVAILABLE", "PayPal case details are unavailable.", 409)
    for option in candidates(system2, dispute, case):
        if option.action == action:
            return option
    raise IntegrationError(
        "ACTION_UNAVAILABLE",
        "PayPal or System 2 does not currently offer this action. Review the case in PayPal Sandbox Resolution Center.",
        409,
    )


def action_payload(option: Option, note: str) -> dict:
    if option.action in ("submit-evidence", "reply-inquiry") and not note:
        raise IntegrationError("NOTE_REQUIRED", "Enter the response to submit.", 422)
    if len(note) > 2000:
        raise IntegrationError(
            "RESPONSE_TOO_LONG", "The full response must fit within 2,000 characters.", 422
        )
    if option.action == "submit-evidence":
        return {"evidences": [{"evidence_type": "OTHER", "notes": note}]}
    if option.action == "reply-inquiry":
        return {"message": note}
    if option.action == "refund":
        return {"reason": note} if note else {}
    payload = {"note": note} if note else {}
    if option.action == "accept-claim":
        return {**payload, "accept_claim_type": "REFUND"}
    digits = 0 if option.currency in {"JPY", "HUF", "TWD"} else 2
    value = Decimal(option.amount_minor).scaleb(-digits)
    return {
        **payload,
        "offer_type": "REFUND",
        "offer_amount": {
            "currency_code": option.currency,
            "value": format(value, "f"),
        },
    }


def execute_action(
    paypal: PayPalClient, system2: System2Client, case_id: str, payment_id: str, plan: ActionPlan
) -> str | None:
    action = plan.option.action
    if action == "refund":
        payload = system2.refund_payment(payment_id, RefundRequest.model_validate(plan.payload))
        payment = PaymentRecord.model_validate(payload)
        if (
            payment.id != payment_id
            or payment.paypal_capture_id != plan.capture_id
            or payment.status != PaymentStatus.REFUNDED
            or (payment.amount_minor, payment.currency)
            != (plan.option.amount_minor, plan.option.currency)
        ):
            raise IntegrationError("INVALID_RESPONSE", "System 2 did not confirm the refund.")
        return payment.paypal_capture_id
    response = paypal.submit_action(
        f"{DISPUTES_PATH}/{case_id}/{PAYPAL_ACTION[action]}", plan.payload
    )
    reference = response.headers.get("PayPal-Debug-Id")
    return reference if reference and re.fullmatch(r"[A-Za-z0-9_-]{1,255}", reference) else case_id


def matching_text_times(case: SandboxCase, plan: ActionPlan) -> list[datetime | None]:
    action = plan.option.action
    if action == "submit-evidence":
        notes = plan.payload["evidences"][0]["notes"]
        return [
            evidence.date
            for evidence in case.evidences
            if evidence.source == "SUBMITTED_BY_SELLER"
            and evidence.evidence_type == "OTHER"
            and evidence.notes == notes
        ]
    if action == "reply-inquiry":
        return [
            message.time_posted
            for message in case.messages
            if message.posted_by == "SELLER" and message.content == plan.payload["message"]
        ]
    return []


def confirmed_by_upstream(
    system2: System2Client,
    dispute: Dispute,
    case: SandboxCase,
    plan: ActionPlan,
    attempt: ResolutionAttempt,
) -> bool:
    action = plan.option.action
    since = attempt.created_at.replace(microsecond=0)
    if plan.capture_id and case.capture_id != plan.capture_id:
        return False
    if action == "refund":
        payment = load_payment(system2, dispute, case)
        return bool(
            payment
            and payment.status == PaymentStatus.REFUNDED
            and (payment.amount_minor, payment.currency)
            == (plan.option.amount_minor, plan.option.currency)
        )
    if action in ("submit-evidence", "reply-inquiry"):
        return any(
            timestamp and timestamp >= since for timestamp in matching_text_times(case, plan)
        )
    outcome = case.dispute_outcome
    codes = {"ACCEPTED"}
    if attempt.status == "SUCCEEDED":
        codes.add("RESOLVED_BUYER_FAVOUR")
    if not (
        case.status == "RESOLVED"
        and case.update_time
        and case.update_time >= since
        and outcome
        and outcome.outcome_code in codes
        and outcome.amount_refunded
    ):
        return False
    try:
        return (outcome.amount_refunded.minor_units(), outcome.amount_refunded.currency_code) == (
            plan.option.amount_minor,
            plan.option.currency,
        )
    except ValueError:
        return False
