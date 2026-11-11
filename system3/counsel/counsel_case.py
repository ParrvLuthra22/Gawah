from dataclasses import dataclass

from sqlalchemy.orm import Session

from .clients import IntegrationError
from .counsel_schemas import CaseAssessment, Classification, EvidenceTrust, Finding
from .evidence import find_dispute, get_pack
from .evidence_schemas import EvidencePackResponse
from .models import Source
from .schemas import DisputeSummary


@dataclass(frozen=True)
class CounselCase:
    pack: EvidencePackResponse
    dispute: DisputeSummary | None


def load_case(db: Session, payment_id: str, dispute_id: int | None = None) -> CounselCase:
    # Resolve ambiguity before choosing a pack when one payment has several linked disputes.
    dispute = find_dispute(db, payment_id, dispute_id)
    scope = dispute.id if dispute else None
    pack = get_pack(db, payment_id, dispute_id=scope)
    if pack.dispute_record_id != scope or (
        dispute and pack.dispute_id != dispute.paypal_dispute_id
    ):
        raise IntegrationError(
            "COUNSEL_EVIDENCE_STALE",
            "The saved pack no longer matches the dispute. Assemble evidence again before analysis.",
            409,
        )
    if dispute:
        for item in pack.evidence:
            if item.type == "dispute" and any(
                key in item.payload and item.payload[key] != getattr(dispute, key)
                for key in ("reason", "transaction_id", "status", "source")
            ):
                raise IntegrationError(
                    "COUNSEL_EVIDENCE_STALE",
                    "The dispute has changed. Assemble evidence again before analysis.",
                    409,
                )
    return CounselCase(pack, dispute)


def classify_evidence(case: CounselCase) -> Classification:
    pack = case.pack
    ids = {item.id for item in pack.evidence}
    issues = [*pack.missing_evidence, *pack.invalid_evidence, *pack.contradictions]
    if (
        len(ids) != len(pack.evidence)
        or any(
            item.payment_id != pack.payment_id or item.dispute_id != pack.dispute_id
            for item in pack.evidence
        )
        or any(not set(issue.evidence_ids) <= ids for issue in issues)
        or any(event.evidence_id not in ids for event in pack.timeline)
    ):
        raise IntegrationError(
            "COUNSEL_EVIDENCE_INVALID",
            "The evidence pack contains inconsistent references. Reassemble it.",
            422,
        )

    warnings = []
    invalid = {evidence_id for issue in pack.invalid_evidence for evidence_id in issue.evidence_ids}
    records = []
    for item in pack.evidence:
        check = item.verification
        status = item.verification_status
        reported_ok = (check.result or {}).get("ok") if check else None
        if (
            item.id in invalid
            or status == "INVALID"
            or (check and check.status == "INVALID")
            or reported_ok is False
        ):
            status = "INVALID"
        elif status == "VERIFIED" and (
            not check or check.status != "VERIFIED" or reported_ok is not True
        ):
            status = "UNAVAILABLE" if check and check.status == "UNAVAILABLE" else "UNVERIFIED"
            warnings.append(f"{item.id} has no matching verified result.")
        elif status == "PENDING":
            status = "UNVERIFIED"
        records.append(
            EvidenceTrust(
                id=item.id,
                type=item.type,
                source_type=item.source_type,
                source_reference=item.source_reference,
                reported_status=item.verification_status,
                verification_status=status,
                verification=check,
            )
        )
    summary = pack.verification_summary
    for name, check in (
        ("Mandate signature", summary.mandate_signature),
        ("Cart signature", summary.cart_signature),
        ("Payment reference", summary.payment_reference_match),
        ("Payment ledger", summary.ledger),
        ("Engine ledger", summary.engine_ledger),
    ):
        if check is None or check.status != "VERIFIED":
            warnings.append(f"{name} check is {check.status.lower() if check else 'unverified'}.")

    return Classification(
        records=records,
        contradictory_evidence=pack.contradictions,
        missing_evidence=pack.missing_evidence,
        invalid_evidence=pack.invalid_evidence,
        invalid_evidence_ids=[item.id for item in records if item.verification_status == "INVALID"],
        unverified_evidence_ids=[
            item.id for item in records if item.verification_status == "UNVERIFIED"
        ],
        unavailable_evidence_ids=[
            item.id for item in records if item.verification_status == "UNAVAILABLE"
        ],
        unavailable_sources=[source for source in pack.sources if source.status == "UNAVAILABLE"],
        verification_warnings=warnings,
    )


DISPUTE_CONTEXT = {
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": (
        "Non-receipt",
        "Provide delivery confirmation or service completion tied to this transaction.",
    ),
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": (
        "Item or service not as described",
        "Provide the order description and item-level records of what was actually fulfilled.",
    ),
    "UNAUTHORISED": (
        "Unauthorized transaction",
        "Provide payer consent and authorization evidence for this transaction.",
    ),
    "INCORRECT_AMOUNT": (
        "Incorrect amount",
        "Provide the signed price and capture records for this exact transaction.",
    ),
    "DUPLICATE_TRANSACTION": (
        "Duplicate transaction",
        "Provide the order and capture references for both alleged charges.",
    ),
    "CREDIT_NOT_PROCESSED": (
        "Credit not processed",
        "Provide the refund or credit confirmation and its transaction reference.",
    ),
    "PAYMENT_BY_OTHER_MEANS": (
        "Payment by other means",
        "Provide the alternative payment receipt and matching order reference.",
    ),
    "CANCELED_RECURRING_BILLING": (
        "Canceled recurring billing",
        "Provide the billing agreement, cancellation record, and disputed charge.",
    ),
}

RELEVANT_CHECKS = {
    "UNAUTHORISED": ("mandate", "payment"),
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": ("payment",),
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": ("signed_cart",),
    "DUPLICATE_TRANSACTION": ("payment",),
    "INCORRECT_AMOUNT": ("payment", "signed_cart"),
}

CHECK_FINDINGS = {
    "mandate": (
        "MANDATE_CHECK",
        "System 2 verified the spending mandate signature. Payer consent still needs separate evidence.",
    ),
    "payment": ("PAYMENT_CHECK", "System 2 verified the payment reference against the ledger."),
    "signed_cart": (
        "CART_CHECK",
        "System 2 verified the signed cart signature. Fulfillment requires separate evidence.",
    ),
}


def assess_case(case: CounselCase, classification: Classification) -> CaseAssessment:
    reason = case.dispute.reason if case.dispute else None
    category, needed = DISPUTE_CONTEXT.get(
        reason,
        (
            "Unclassified dispute",
            "Provide the dispute reason and records addressing the buyer's claim.",
        ),
    )
    assessment = CaseAssessment(
        category=category,
        dispute_reason=reason,
        supported_outcome="REQUEST_MORE_EVIDENCE",
        findings=[],
        documentation_needed=needed,
    )
    dispute_record = next(
        (
            item
            for item in case.pack.evidence
            if item.type == "dispute"
            and item.source_record_id == case.pack.dispute_id
            and item.payload.get("reason") == reason
        ),
        None,
    )
    if dispute_record and reason in DISPUTE_CONTEXT:
        assessment.findings.append(
            Finding(
                id="COMPLAINT",
                text=f"The recorded dispute reason is {category.lower()}.",
                evidence_ids=[dispute_record.id],
            )
        )
        for record in classification.records:
            if record.reliable and record.type in RELEVANT_CHECKS.get(reason, ()):
                finding_id, statement = CHECK_FINDINGS[record.type]
                if all(finding.id != finding_id for finding in assessment.findings):
                    assessment.findings.append(
                        Finding(
                            id=finding_id,
                            text=statement,
                            evidence_ids=[record.id],
                        )
                    )
    if reason == "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED":
        for issue in case.pack.contradictions:
            if issue.code == "AUTHORIZED_SIZE_MISMATCH" and issue.evidence_ids:
                assessment.findings.append(
                    Finding(
                        id="ORDER_VARIANT_CONFLICT",
                        text="Evidence assembly flagged a difference between the authorized size and signed cart. "
                        "The delivered size is still unconfirmed.",
                        evidence_ids=issue.evidence_ids,
                    )
                )
                break
    if reason == "UNAUTHORISED":
        if references := _verified_authorization(case, classification):
            assessment.findings.append(
                Finding(
                    id="AUTHORIZATION_CONSENT",
                    text=(
                        "The signed mandate, Guardian ALLOW decision, and verified payment record "
                        "show consent for this transaction within the mandate."
                    ),
                    evidence_ids=references,
                )
            )
            assessment.supported_outcome = "DEFEND"
        return assessment
    if reason == "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED":
        if references := _verified_authorized_size_mismatch(case, classification):
            assessment.findings.append(
                Finding(
                    id="AUTHORIZED_SIZE_MISMATCH",
                    text=(
                        "The verified signed cart differs from the mandate's authorized size; "
                        "the Guardian recorded the authorized-size mismatch advisory."
                    ),
                    evidence_ids=references,
                )
            )
            assessment.supported_outcome = "CONCEDE_REMEDY"
        return assessment
    if (
        not dispute_record
        or reason != "INCORRECT_AMOUNT"
        or dispute_record.source_type != "REAL_RECORD"
        or case.dispute.source != Source.PAYPAL_API
        or dispute_record.id in classification.invalid_evidence_ids
    ):
        return assessment

    amounts = _verified_billing(case, classification, dispute_record.payload.get("transaction_id"))
    if amounts is None:
        return assessment
    signed, charged, currency, references = amounts
    assessment.findings.append(
        Finding(
            id="AMOUNT_COMPARISON",
            text=f"The signed cart records {signed} {currency} minor units. The verified completion entry records "
            f"{charged} {currency} minor units for the linked capture.",
            evidence_ids=references,
        )
    )
    assessment.supported_outcome = (
        "DEFEND"
        if charged == signed
        else "CONCEDE_REMEDY"
        if charged > signed
        else "REQUEST_MORE_EVIDENCE"
    )
    return assessment


def _all_primary_checks_verified(case: CounselCase) -> bool:
    summary = case.pack.verification_summary
    return all(
        check is not None and check.status == "VERIFIED"
        for check in (
            summary.mandate_signature,
            summary.cart_signature,
            summary.payment_reference_match,
            summary.ledger,
        )
    )


def _verified_authorization(case: CounselCase, classification: Classification) -> list[str] | None:
    if not _all_primary_checks_verified(case):
        return None
    records = {item.type: item for item in case.pack.evidence if item.type != "ledger_entry"}
    trusted = {record.id for record in classification.records if record.reliable}
    required = {"mandate", "signed_cart", "payment", "decision"}
    if not required <= records.keys() or any(
        records[kind].id not in trusted for kind in required - {"decision"}
    ):
        return None
    mandate = records["mandate"]
    payment = records["payment"]
    decision = records["decision"]
    if (
        decision.source_type != "REAL_RECORD"
        or decision.id in classification.invalid_evidence_ids
        or decision.payload.get("verdict") != "ALLOW"
        or payment.payload.get("status") != "PAID"
        or payment.payload.get("decision_id") != decision.source_record_id
        or payment.payload.get("proposal_id") != decision.payload.get("proposal_id")
        or decision.payload.get("mandate_id") != mandate.source_record_id
    ):
        return None
    approvals = [
        item
        for item in case.pack.evidence
        if item.type == "ledger_entry"
        and item.id in trusted
        and item.payload.get("type") == "DECISION_MADE"
        and item.payload.get("subject_id") == decision.source_record_id
    ]
    if len(approvals) != 1:
        return None
    approved = approvals[0].payload.get("data")
    if not isinstance(approved, dict) or (
        approved.get("verdict"), approved.get("mandate_id"), approved.get("proposal_id")
    ) != ("ALLOW", mandate.source_record_id, payment.payload.get("proposal_id")):
        return None
    return [mandate.id, decision.id, payment.id]


def _verified_authorized_size_mismatch(
    case: CounselCase, classification: Classification
) -> list[str] | None:
    if not _all_primary_checks_verified(case):
        return None
    trusted = {record.id for record in classification.records if record.reliable}
    records = {item.type: item for item in case.pack.evidence if item.type != "ledger_entry"}
    mandate = records.get("mandate")
    cart = records.get("signed_cart")
    if mandate is None or cart is None:
        return None
    for issue in case.pack.contradictions:
        if issue.code != "AUTHORIZED_SIZE_MISMATCH":
            continue
        references = set(issue.evidence_ids)
        if references == {mandate.id, cart.id} and references <= trusted:
            return issue.evidence_ids
    return None


def _verified_billing(
    case: CounselCase,
    classification: Classification,
    transaction_id: str | None,
) -> tuple[int, int, str, list[str]] | None:
    summary = case.pack.verification_summary
    if (
        any(
            not check or check.status != "VERIFIED"
            for check in (
                summary.mandate_signature,
                summary.cart_signature,
                summary.payment_reference_match,
                summary.ledger,
            )
        )
        or summary.engine_ledger.status == "INVALID"
        or any(issue.code != "PAYMENT_AMOUNT_MISMATCH" for issue in case.pack.contradictions)
    ):
        return None
    trusted = {record.id for record in classification.records if record.reliable}
    records = {item.type: item for item in case.pack.evidence if item.type != "ledger_entry"}
    required = {"mandate", "payment", "signed_cart", "proposal"}
    if not required <= records.keys() or any(
        records[kind].id not in trusted for kind in required - {"proposal"}
    ):
        return None
    mandate, payment, cart, proposal = (
        records[kind] for kind in ("mandate", "payment", "signed_cart", "proposal")
    )
    if proposal.source_type != "REAL_RECORD" or proposal.id in classification.invalid_evidence_ids:
        return None
    pay, signed, proposed = payment.payload, cart.payload, proposal.payload
    capture = pay.get("paypal_capture_id")
    if (
        not capture
        or not transaction_id
        or transaction_id not in (capture, pay.get("paypal_order_id"))
        or pay.get("status") != "PAID"
        or payment.source_record_id != case.pack.payment_id
        or not proposed.get("id")
        or pay.get("proposal_id") != proposed["id"]
        or proposed.get("mandate_id") != mandate.source_record_id
        or not proposed.get("merchant_id")
        or proposed["merchant_id"] != signed.get("merchant_id")
    ):
        return None
    ledger = [
        item for item in case.pack.evidence if item.id in trusted and item.type == "ledger_entry"
    ]
    completions = [
        item
        for item in ledger
        if item.payload.get("type") == "PAYMENT_COMPLETED"
        and item.payload.get("subject_id") == payment.source_record_id
    ]
    approvals = [
        item
        for item in ledger
        if item.payload.get("type") == "DECISION_MADE"
        and pay.get("decision_id")
        and item.payload.get("subject_id") == pay["decision_id"]
    ]
    if len(completions) != 1 or len(approvals) != 1:
        return None
    completed = completions[0].payload.get("data")
    approved = approvals[0].payload.get("data")
    if not isinstance(completed, dict) or not isinstance(approved, dict):
        return None
    amount, charged, currency = (
        signed.get("total_minor"),
        completed.get("amount_minor"),
        signed.get("currency"),
    )
    quantity, unit_price = proposed.get("quantity"), proposed.get("unit_price_minor")
    if (
        type(amount) is not int
        or type(charged) is not int
        or min(amount, charged) < 0
        or not isinstance(currency, str)
        or len(currency) != 3
        or not currency.isascii()
        or not currency.isalpha()
        or (charged, currency) != (pay.get("amount_minor"), pay.get("currency"))
        or type(quantity) is not int
        or type(unit_price) is not int
        or quantity <= 0
        or unit_price < 0
        or (quantity * unit_price, proposed.get("currency")) != (amount, currency)
        or completed.get("currency") != currency
        or completed.get("paypal_capture_id") != capture
        or completed.get("proposal_id") != proposed["id"]
        or approved.get("proposal_id") != proposed["id"]
        or approved.get("verdict") != "ALLOW"
        or (approved.get("amount_minor"), approved.get("currency")) != (amount, currency)
    ):
        return None
    return amount, charged, currency, [cart.id, payment.id, approvals[0].id, completions[0].id]
