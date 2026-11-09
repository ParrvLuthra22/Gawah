import re
from datetime import UTC, datetime
from decimal import Decimal

from .evidence_collection import CollectedEvidence
from .evidence_schemas import EvidenceIssue, EvidenceItem, TimelineEvent

MANDATE_SIZE = re.compile(r"(?:^|,\s*)size (\d+(?:\.\d+)?)(?=\s*(?:,|$))", re.IGNORECASE)
CART_SIZE = re.compile(r"size (\d+(?:\.\d+)?)", re.IGNORECASE)


def timestamp(value: datetime | None) -> datetime | None:
    return value.astimezone(UTC) if value is not None and value.utcoffset() is not None else None


def timeline(items: list[EvidenceItem]) -> list[TimelineEvent]:
    event_names = {
        "mandate": ("MANDATE_CREATED", "Spending mandate was issued."),
        "proposal": ("PROPOSAL_CREATED", "Purchase proposal was created."),
        "signed_cart": ("SIGNED_CART_RECORDED", "The source supplied a signed cart."),
        "decision": ("DECISION_MADE", "Guardian recorded its decision."),
        "payment": ("PAYMENT_RECORDED", "The payment record was created."),
        "dispute": ("DISPUTE_OPENED", "The dispute was opened."),
    }
    events = []
    ledger_events = {
        (item.payload["type"], item.payload["subject_id"], item.timestamp)
        for item in items
        if item.type == "ledger_entry"
    }
    for item in items:
        record_id = item.source_record_id
        if item.type == "ledger_entry":
            kind = item.payload["type"]
            record_id = item.payload["subject_id"]
            description = f"Ledger recorded {kind.lower().replace('_', ' ')} for {record_id}."
        elif item.type in event_names:
            kind, description = event_names[item.type]
            if (kind, record_id, item.timestamp) in ledger_events:
                continue
            if item.type == "decision":
                description = f"Guardian recorded a {item.payload['verdict']} decision."
            if item.type == "dispute" and item.timestamp is None:
                kind, description = "DISPUTE_RECORDED", "The source supplied a dispute record."
        else:
            continue
        events.append(
            TimelineEvent(
                id=f"event:{item.id}",
                type=kind,
                timestamp=item.timestamp,
                source_system=item.source_system,
                record_id=record_id,
                evidence_id=item.id,
                description=description,
                verification_status=item.verification_status,
            )
        )
    # Input order breaks ties, preserving ledger sequence for whole-second timestamps.
    return sorted(events, key=lambda event: (event.timestamp is None, event.timestamp))


def issue(
    code,
    kind,
    explanation,
    *items: EvidenceItem | None,
    references=(),
    severity="WARNING",
) -> EvidenceIssue:
    evidence = [item for item in items if item is not None]
    return EvidenceIssue(
        code=code,
        type=kind,
        severity=severity,
        explanation=explanation,
        evidence_ids=[item.id for item in evidence],
        source_references=list(
            dict.fromkeys([*(item.source_reference for item in evidence), *references])
        ),
    )


def missing_and_invalid(data: CollectedEvidence, items: dict[str, EvidenceItem], payment_id: str):
    bundle_path = f"/v1/evidence/{payment_id}"
    missing = []
    invalid = list(data.invalid)
    for name, reference in (
        ("mandate", bundle_path),
        ("proposal", bundle_path),
        ("signed_cart", bundle_path),
        ("decision", bundle_path),
        ("payment", f"/v1/payments/{payment_id}"),
        ("dispute", "/api/disputes"),
    ):
        if name not in items:
            missing.append(
                issue(
                    f"MISSING_{name.upper()}",
                    "MISSING",
                    f"No {name.replace('_', ' ')} record was collected.",
                    references=[reference],
                )
            )
    if not data.ledger:
        missing.append(
            issue(
                "MISSING_LEDGER_ENTRIES",
                "MISSING",
                "No payment-scoped ledger entries were collected.",
                references=[bundle_path, f"/v1/ledger?subject_id={payment_id}"],
            )
        )
    if "dispute" in items or (data.payment and data.payment.status in {"PAID", "REFUNDED"}):
        missing.append(
            issue(
                "ORDER_UNAVAILABLE",
                "MISSING",
                "The shared contract exposes no merchant order record.",
                items.get("payment"),
                items.get("signed_cart"),
                references=["docs/CONTRACT.md"],
            )
        )
        if data.mandate and data.mandate.category.casefold() == "footwear":
            missing.append(
                issue(
                    "DELIVERY_UNAVAILABLE",
                    "MISSING",
                    "No documented source provides delivery confirmation for this physical purchase.",
                    items.get("signed_cart"),
                    references=["docs/CONTRACT.md"],
                )
            )
    assert data.verification is not None
    for name, check, affected in (
        ("MANDATE_SIGNATURE", data.verification.mandate_signature, items.get("mandate")),
        ("CART_SIGNATURE", data.verification.cart_signature, items.get("signed_cart")),
        ("LEDGER_VERIFICATION", data.verification.ledger, None),
        ("ENGINE_LEDGER_VERIFICATION", data.verification.engine_ledger, None),
    ):
        if name in {"MANDATE_SIGNATURE", "CART_SIGNATURE"} and affected is None:
            continue
        references = [check.reference] if check.reference else []
        if check.status == "INVALID":
            reason = (check.result or {}).get("reason")
            if name == "LEDGER_VERIFICATION" and check.result:
                affected = items.get(f"ledger:{check.result.get('first_broken_seq')}")
            invalid.append(
                issue(
                    f"INVALID_{name}",
                    "INVALID",
                    reason or f"{check.detail} The check failed.",
                    affected,
                    references=references,
                    severity="HIGH",
                )
            )
        elif check.status in {"UNAVAILABLE", "PENDING"}:
            missing.append(
                issue(
                    f"MISSING_{name}",
                    "MISSING",
                    check.detail,
                    affected,
                    references=references,
                )
            )
    return missing, invalid


def contradictions(data: CollectedEvidence, items: dict[str, EvidenceItem]) -> list[EvidenceIssue]:
    issues = list(data.contradictions)

    def flag(code, message, *kinds):
        issues.append(issue(code, "CONTRADICTION", message, *(items[kind] for kind in kinds)))

    mandate, proposal, cart, decision, payment = (
        data.mandate,
        data.proposal,
        data.cart,
        data.decision,
        data.payment,
    )
    if payment and proposal:
        if payment.proposal_id != proposal.id:
            flag(
                "PAYMENT_PROPOSAL_MISMATCH",
                "The payment references a different proposal.",
                "payment",
                "proposal",
            )
        else:
            amount = proposal.quantity * proposal.unit_price_minor
            if (amount, proposal.currency) != (payment.amount_minor, payment.currency):
                flag(
                    "PAYMENT_AMOUNT_MISMATCH",
                    f"The proposal totals {amount} {proposal.currency} minor units, while the payment records "
                    f"{payment.amount_minor} {payment.currency} minor units.",
                    "proposal",
                    "payment",
                )
    if payment and decision and payment.decision_id != decision.id:
        flag(
            "PAYMENT_DECISION_MISMATCH",
            "The payment references a different Guardian decision.",
            "payment",
            "decision",
        )
    if proposal and decision and decision.proposal_id != proposal.id:
        flag(
            "DECISION_PROPOSAL_MISMATCH",
            "The Guardian decision references a different proposal.",
            "decision",
            "proposal",
        )
    if proposal and mandate and proposal.mandate_id != mandate.id:
        flag(
            "PROPOSAL_MANDATE_MISMATCH",
            "The proposal references a different mandate.",
            "proposal",
            "mandate",
        )
    if proposal and cart:
        if cart != proposal.signed_cart:
            flag(
                "CART_SNAPSHOT_MISMATCH",
                "The supplied cart differs from the one embedded in the proposal.",
                "signed_cart",
                "proposal",
            )
        else:
            matching_lines = [line for line in cart.line_items if line.sku == proposal.product.id]
            if cart.line_items and not matching_lines:
                flag(
                    "PROPOSAL_PRODUCT_MISMATCH",
                    "The proposed SKU is absent from the signed cart.",
                    "proposal",
                    "signed_cart",
                )
            if mandate and proposal.mandate_id == mandate.id and len(matching_lines) == 1:
                expected = MANDATE_SIZE.findall(mandate.item_description)
                actual = CART_SIZE.fullmatch((matching_lines[0].variant or "").strip())
                if len(expected) == 1 and actual and Decimal(expected[0]) != Decimal(actual[1]):
                    flag(
                        "AUTHORIZED_SIZE_MISMATCH",
                        f"The mandate specifies size {expected[0]}, while the signed cart records size {actual[1]}.",
                        "mandate",
                        "signed_cart",
                    )
    if payment:
        for entry in data.ledger:
            if entry.subject_id != payment.id or entry.type != "PAYMENT_COMPLETED":
                continue
            mismatches = [
                key
                for key in ("paypal_order_id", "paypal_capture_id")
                if (value := entry.data.get(key))
                and isinstance(value, str)
                and getattr(payment, key)
                and value != getattr(payment, key)
            ]
            if mismatches:
                flag(
                    "LEDGER_PAYMENT_REFERENCE_MISMATCH",
                    "The payment record and completion entry disagree on "
                    + ", ".join(mismatches)
                    + ".",
                    "payment",
                    f"ledger:{entry.seq}",
                )
    for early, late, related in (
        ("mandate", "proposal", mandate and proposal and mandate.id == proposal.mandate_id),
        ("proposal", "decision", proposal and decision and proposal.id == decision.proposal_id),
        ("decision", "payment", decision and payment and decision.id == payment.decision_id),
        ("payment", "dispute", payment and "dispute" in items),
    ):
        if (
            related
            and (start := items[early].timestamp)
            and (end := items[late].timestamp)
            and start > end
        ):
            flag(
                f"TIMESTAMP_{early.upper()}_{late.upper()}",
                f"The recorded {early} timestamp is later than the related {late} timestamp.",
                early,
                late,
            )
    return issues
