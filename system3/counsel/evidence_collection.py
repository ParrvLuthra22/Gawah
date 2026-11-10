import re
from dataclasses import dataclass, field
from datetime import datetime

from gawah_contract import (
    EvidenceBundle,
    GuardianDecision,
    LedgerEntry,
    LedgerVerifyResult,
    Mandate,
    PaymentRecord,
    PurchaseProposal,
    SignedCart,
)
from pydantic import TypeAdapter, ValidationError

from .clients import IntegrationError, System2Client
from .evidence_schemas import (
    EvidenceIssue,
    SourceCheck,
    SourceSystem,
    SourceType,
    Verification,
    VerificationSummary,
)
from .models import now

REFERENCE_PATTERN = r"^[A-Za-z0-9_-]{1,255}$"
BUNDLE_FIELDS = {
    name: TypeAdapter(info.annotation) for name, info in EvidenceBundle.model_fields.items()
}
OBJECT = TypeAdapter(dict)
LEDGER_VERIFY = "/v1/ledger/verify"


@dataclass
class CollectedEvidence:
    source_type: SourceType = "UNKNOWN"
    records: dict = field(default_factory=dict)
    references: dict[str, str] = field(default_factory=dict)
    retrieved_at: dict[str, datetime] = field(default_factory=dict)
    sources: list[SourceCheck] = field(default_factory=list)
    invalid: list[EvidenceIssue] = field(default_factory=list)
    contradictions: list[EvidenceIssue] = field(default_factory=list)
    verification: VerificationSummary | None = None
    bundle_available: bool = False
    bundle_verification: dict | None = None
    engine_verification: dict | None = None

    @property
    def source(self) -> SourceSystem:
        return "MOCK_ENGINE" if self.source_type == "MOCK_RESPONSE" else "SYSTEM_2"

    @property
    def payment(self) -> PaymentRecord | None:
        return self.records.get("payment")

    @property
    def proposal(self) -> PurchaseProposal | None:
        return self.records.get("proposal")

    @property
    def mandate(self) -> Mandate | None:
        return self.records.get("mandate")

    @property
    def cart(self) -> SignedCart | None:
        return self.records.get("signed_cart")

    @property
    def decision(self) -> GuardianDecision | None:
        return self.records.get("decision")

    @property
    def ledger(self) -> list[LedgerEntry]:
        return self.records.get("ledger_entries", [])

    def put(self, name: str, value, reference: str) -> None:
        self.records[name] = value
        self.references[name] = reference
        self.retrieved_at[name] = now()

    def bad_record(self, name: str, reference: str) -> None:
        self.invalid.append(
            EvidenceIssue(
                code=f"INVALID_{name.upper()}",
                type="INVALID",
                severity="WARNING",
                explanation=f"The {name.replace('_', ' ')} response did not match the shared schema.",
                evidence_ids=[],
                source_references=[reference],
            )
        )

    def wrong_payment(self, expected: str, actual: str, reference: str) -> None:
        self.contradictions.append(
            EvidenceIssue(
                code="PAYMENT_REFERENCE_MISMATCH",
                type="CONTRADICTION",
                severity="HIGH",
                explanation=f"Requested payment {expected} but the source returned {actual}.",
                evidence_ids=[],
                source_references=[reference],
            )
        )


def _fetch(client, data, reference, adapter, *, params=None, name):
    path = reference.partition("?")[0]
    try:
        payload = client.get_json(path, params=params)
        if reference == LEDGER_VERIFY and isinstance(payload, dict):
            data.engine_verification = payload
        result = adapter.validate_python(payload, strict=reference == LEDGER_VERIFY)
    except IntegrationError as exc:
        status = "NOT_FOUND" if exc.upstream_status == 404 else "UNAVAILABLE"
        if exc.code == "INVALID_RESPONSE":
            status = "INVALID"
            data.bad_record(name, reference)
        data.sources.append(
            SourceCheck(
                source_system="SYSTEM_2",
                reference=reference,
                status=status,
                detail=str(exc),
            )
        )
        return None
    except ValidationError:
        data.bad_record(name, reference)
        data.sources.append(
            SourceCheck(
                source_system="SYSTEM_2",
                reference=reference,
                status="INVALID",
                detail="Response did not match the shared schema.",
            )
        )
        return None
    data.sources.append(
        SourceCheck(
            source_system="SYSTEM_2",
            reference=reference,
            status="AVAILABLE",
        )
    )
    return result


def _collect_bundle(client, data: CollectedEvidence, payment_id: str) -> None:
    path = f"/v1/evidence/{payment_id}"
    bundle = _fetch(client, data, path, OBJECT, name="bundle")
    if bundle is None:
        return
    records = {}
    for name, adapter in BUNDLE_FIELDS.items():
        if bundle.get(name) is None:
            continue
        try:
            records[name] = adapter.validate_python(bundle[name], strict=name == "verification")
        except ValidationError:
            data.bad_record(name, f"{path}#{name}")
    payment = records.get("payment")
    if payment is not None and payment.id != payment_id:
        data.wrong_payment(payment_id, payment.id, f"{path}#payment")
        data.sources[-1].status = "INVALID"
        data.sources[-1].detail = "Bundle belongs to another payment."
        return
    if isinstance(bundle.get("verification"), dict):
        data.bundle_verification = bundle["verification"]
    data.bundle_available = True
    for name, value in records.items():
        data.put(name, value, f"{path}#{name}")


def _classify_source(data: CollectedEvidence) -> SourceType:
    signed = (data.mandate, data.cart, *data.ledger)
    payment = data.payment
    if (
        any(record and record.signature.startswith("mock-signature-") for record in signed)
        or (data.mandate and data.mandate.signing_alg == "ed25519")
        or (
            payment
            and (
                (payment.paypal_order_id or "").startswith("FAKEORDER")
                or (payment.paypal_capture_id or "").startswith("FAKECAPTURE")
            )
        )
    ):
        return "MOCK_RESPONSE"
    if data.mandate and data.mandate.signing_alg == "ES256":
        return "REAL_RECORD"
    # The wire format has no explicit engine-mode field.
    return "UNKNOWN"


def _ledger_check(
    result: LedgerVerifyResult | None,
    reference: str,
    detail: str,
    raw: dict | None,
) -> Verification:
    if result is None:
        return Verification(
            status="UNAVAILABLE",
            reference=reference,
            detail="No usable check was returned.",
            result=raw,
        )
    if result.checked < 0 or (result.ok and result.first_broken_seq is not None):
        return Verification(
            status="INVALID",
            reference=reference,
            detail="The verification result contains inconsistent coverage fields.",
            result=raw,
        )
    status = "INVALID" if not result.ok else "VERIFIED" if result.checked > 0 else "UNVERIFIED"
    return Verification(
        status=status,
        reference=reference,
        detail=detail
        if result.checked or not result.ok
        else "The verifier did not check any entries.",
        result=raw,
    )


def _verify(client: System2Client, data: CollectedEvidence, payment_id: str) -> VerificationSummary:
    bundle = data.records.get("verification")
    engine = _fetch(client, data, LEDGER_VERIFY, BUNDLE_FIELDS["verification"], name="ledger_check")
    global_check = _ledger_check(
        engine,
        LEDGER_VERIFY,
        "Engine-wide mock chain check (hashes and links)."
        if data.source_type == "MOCK_RESPONSE"
        else "Engine-wide chain check reported by System 2.",
        data.engine_verification,
    )
    reference = f"/v1/evidence/{payment_id}#verification"

    def named(name: str) -> Verification:
        check_reference = f"{reference}.checks/{name}"
        matches = [check for check in bundle.checks if check.name == name] if bundle else []
        if len(matches) == 1:
            check = matches[0]
            return Verification(
                status="VERIFIED" if check.ok else "INVALID",
                reference=check_reference,
                detail=check.detail or "System 2 reported this check.",
                result=check.model_dump(mode="json"),
            )
        if matches:
            return Verification(
                status="UNAVAILABLE",
                reference=check_reference,
                detail="System 2 returned multiple results for this check.",
            )
        if not data.bundle_available or any(
            issue.code == "INVALID_VERIFICATION" for issue in data.invalid
        ):
            return Verification(
                status="UNAVAILABLE",
                reference=check_reference,
                detail="The evidence bundle verification could not be retrieved or read.",
            )
        return Verification(
            status="UNVERIFIED",
            reference=check_reference,
            detail="The evidence bundle did not report this check.",
        )

    scoped = named("ledger_chain")
    entries_reference = data.references.get("ledger_entries", "")
    if scoped.status == "VERIFIED":
        if (
            not data.ledger
            or entries_reference != reference.replace("#verification", "#ledger_entries")
            or bundle.first_broken_seq is not None
            or max(entry.seq for entry in data.ledger) > bundle.checked
            or any(issue.code == "INVALID_LEDGER_SEQUENCE" for issue in data.invalid)
        ):
            scoped.status = "UNAVAILABLE"
            scoped.detail = (
                "The bundle's ledger check does not establish coverage for these entries."
            )
    elif (
        scoped.status == "UNVERIFIED"
        and bundle
        and not bundle.ok
        and bundle.first_broken_seq is not None
    ):
        scoped = Verification(
            status="INVALID",
            reference=reference,
            detail="System 2 reported a broken ledger sequence.",
            result=data.bundle_verification,
        )
    elif data.ledger and entries_reference.startswith("/v1/ledger?") and scoped.status != "INVALID":
        if (
            global_check.status == "VERIFIED"
            and max(entry.seq for entry in data.ledger) <= engine.checked
        ):
            scoped = global_check.model_copy(
                update={
                    "detail": "System 2 reported a successful chain check covering the collected sequences."
                }
            )
        elif global_check.status == "INVALID" and any(
            entry.seq == engine.first_broken_seq for entry in data.ledger
        ):
            scoped = global_check
        elif global_check.status == "UNAVAILABLE":
            scoped = Verification(
                status="UNAVAILABLE",
                reference=LEDGER_VERIFY,
                detail=global_check.detail,
                result=global_check.result,
            )
    return VerificationSummary(
        mandate_signature=named("mandate_signature"),
        cart_signature=named("cart_signature"),
        ledger=scoped,
        engine_ledger=global_check,
        payment_reference_match=named("payment_reference_match"),
        bundle_result=data.bundle_verification,
    )


def collect_evidence(
    client: System2Client, payment_id: str, *, has_dispute: bool
) -> CollectedEvidence:
    data = CollectedEvidence()
    _collect_bundle(client, data, payment_id)
    if data.payment is None:
        path = f"/v1/payments/{payment_id}"
        payment = _fetch(client, data, path, BUNDLE_FIELDS["payment"], name="payment")
        if payment is not None:
            if payment.id == payment_id:
                data.put("payment", payment, path)
            else:
                data.wrong_payment(payment_id, payment.id, path)
                data.sources[-1].status = "INVALID"
    if (
        data.mandate is None
        and data.proposal
        and re.fullmatch(REFERENCE_PATTERN, data.proposal.mandate_id)
    ):
        path = f"/v1/mandates/{data.proposal.mandate_id}"
        mandate = _fetch(client, data, path, BUNDLE_FIELDS["mandate"], name="mandate")
        if mandate is not None and mandate.id == data.proposal.mandate_id:
            data.put("mandate", mandate, path)
    if data.cart is None and data.proposal:
        data.put(
            "signed_cart", data.proposal.signed_cart, f"{data.references['proposal']}.signed_cart"
        )
        data.retrieved_at["signed_cart"] = data.retrieved_at["proposal"]
    if not data.ledger:
        path = f"/v1/ledger?subject_id={payment_id}"
        entries = _fetch(
            client,
            data,
            path,
            BUNDLE_FIELDS["ledger_entries"],
            params={"subject_id": payment_id},
            name="ledger_entries",
        )
        if entries:
            matched = [entry for entry in entries if entry.subject_id == payment_id]
            if matched:
                data.put("ledger_entries", matched, path)
    if data.ledger:
        # Keep one row per sequence. A conflicting sequence cannot establish coverage.
        by_seq = {}
        conflicts = set()
        for entry in data.ledger:
            previous = by_seq.setdefault(entry.seq, entry)
            if previous != entry:
                conflicts.add(entry.seq)
        if conflicts:
            data.bad_record("ledger_sequence", data.references["ledger_entries"])
        data.records["ledger_entries"] = [by_seq[seq] for seq in sorted(by_seq.keys() - conflicts)]
    if not has_dispute and not (
        data.ledger or set(data.records) - {"ledger_entries", "verification"}
    ):
        if any(check.status in {"UNAVAILABLE", "INVALID"} for check in data.sources):
            raise IntegrationError(
                "EVIDENCE_SOURCE_UNAVAILABLE",
                "No payment evidence could be collected from System 2.",
                502,
            )
        raise IntegrationError("EVIDENCE_NOT_FOUND", "No evidence was found for this payment.", 404)
    data.source_type = _classify_source(data)
    data.verification = _verify(client, data, payment_id)
    if data.source_type == "MOCK_RESPONSE":
        for source in data.sources:
            source.source_system = "MOCK_ENGINE"
    data.sources.append(
        SourceCheck(
            source_system="SYSTEM_1",
            reference="docs/CONTRACT.md",
            status="UNAVAILABLE",
            detail="No merchant order or fulfillment read endpoint is documented.",
        )
    )
    return data
