import re
from dataclasses import dataclass, field
from datetime import datetime

from cryptography.exceptions import UnsupportedAlgorithm
from gawah_contract import (
    EvidenceBundle,
    GuardianDecision,
    LedgerEntry,
    LedgerVerifyResult,
    Mandate,
    MerchantKey,
    PaymentRecord,
    PurchaseProposal,
    SignedCart,
)
from gawah_contract.carts import load_public_key, verify_cart
from pydantic import TypeAdapter, ValidationError

from .clients import IntegrationError, System2Client
from .evidence_schemas import EvidenceIssue, SourceCheck, Verification, VerificationSummary
from .models import now

REFERENCE_PATTERN = r"^[A-Za-z0-9_-]{1,255}$"
BUNDLE_FIELDS = {
    name: TypeAdapter(info.annotation) for name, info in EvidenceBundle.model_fields.items()
}
OBJECT = TypeAdapter(dict)
LEDGER_VERIFY = "/v1/ledger/verify"


@dataclass
class CollectedEvidence:
    source: str = "SYSTEM_2"
    records: dict = field(default_factory=dict)
    references: dict[str, str] = field(default_factory=dict)
    retrieved_at: dict[str, datetime] = field(default_factory=dict)
    sources: list[SourceCheck] = field(default_factory=list)
    invalid: list[EvidenceIssue] = field(default_factory=list)
    contradictions: list[EvidenceIssue] = field(default_factory=list)
    verification: VerificationSummary | None = None

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
        result = adapter.validate_python(client.get_json(path, params=params))
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
    # The current contract exposes this route in mock mode only.
    data.source = "MOCK_ENGINE"
    data.sources[-1].source_system = "MOCK_ENGINE"
    records = {}
    for name, adapter in BUNDLE_FIELDS.items():
        if bundle.get(name) is None:
            continue
        try:
            records[name] = adapter.validate_python(bundle[name])
        except ValidationError:
            data.bad_record(name, f"{path}#{name}")
    payment = records.get("payment")
    if payment is not None and payment.id != payment_id:
        data.wrong_payment(payment_id, payment.id, f"{path}#payment")
        data.sources[-1].status = "INVALID"
        data.sources[-1].detail = "Bundle belongs to another payment."
        return
    for name, value in records.items():
        data.put(name, value, f"{path}#{name}")


def _signature(cart: SignedCart | None, merchant: MerchantKey | None, data) -> Verification:
    reference = data.references.get("merchant_key")
    if not cart:
        return Verification(status="UNAVAILABLE", detail="No signed cart was collected.")
    if data.source == "MOCK_ENGINE" and cart.signature.startswith("mock-signature-"):
        return Verification(
            status="UNAVAILABLE",
            reference=reference,
            detail="Mock cart signatures do not establish cryptographic verification.",
        )
    if not merchant or merchant.key_id != cart.key_id or merchant.alg != "ES256":
        return Verification(
            status="UNAVAILABLE",
            reference=reference,
            detail="The merchant registry did not supply the cart's ES256 signing key.",
        )
    try:
        key = load_public_key(merchant.public_key_pem)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        return Verification(
            status="UNAVAILABLE",
            reference=reference,
            detail="The registered key could not be read.",
        )
    ok = verify_cart(cart, key)
    return Verification(
        status="VERIFIED" if ok else "INVALID",
        reference=reference,
        detail="Cart signature checked with the shared verifier and the matching merchant key.",
        result={"ok": ok, "merchant_id": merchant.merchant_id, "key_id": merchant.key_id},
    )


def _ledger_check(
    result: LedgerVerifyResult | None,
    reference: str,
    detail: str,
) -> Verification:
    if result is None:
        return Verification(
            status="UNAVAILABLE", reference=reference, detail="No check was returned."
        )
    if result.checked < 0 or (result.ok and result.first_broken_seq is not None):
        return Verification(
            status="INVALID",
            reference=reference,
            detail="The verification result contains inconsistent coverage fields.",
            result=result.model_dump(mode="json"),
        )
    status = "INVALID" if not result.ok else "VERIFIED" if result.checked > 0 else "UNAVAILABLE"
    return Verification(
        status=status,
        reference=reference,
        detail=detail
        if result.checked or not result.ok
        else "The verifier did not check any entries.",
        result=result.model_dump(mode="json"),
    )


def _verify(client: System2Client, data: CollectedEvidence, merchant) -> VerificationSummary:
    bundle = data.records.get("verification")
    engine = _fetch(client, data, LEDGER_VERIFY, BUNDLE_FIELDS["verification"], name="ledger_check")
    mock = data.source == "MOCK_ENGINE"
    global_check = _ledger_check(
        engine,
        LEDGER_VERIFY,
        "Engine-wide mock chain check (hashes and links)."
        if mock
        else "Engine-wide chain check reported by System 2. Engine mode is not reported.",
    )
    reference = data.references.get("verification")
    if reference and data.references.get("ledger_entries") == reference.replace(
        "#verification", "#ledger_entries"
    ):
        scoped = _ledger_check(
            bundle, reference, "Mock bundle reports a chain check (hashes and links)."
        )
        if scoped.status == "VERIFIED" and bundle.checked != len(data.ledger):
            scoped.status = "UNAVAILABLE"
            scoped.detail = "The bundle's check count does not match its collected ledger entries."
    elif data.ledger and data.references["ledger_entries"].startswith("/v1/ledger?") and engine:
        if (
            global_check.status == "VERIFIED"
            and max(entry.seq for entry in data.ledger) <= engine.checked
        ):
            scoped = global_check.model_copy(
                update={
                    "detail": "System 2 reported a successful chain check covering the collected sequences."
                }
            )
        else:
            scoped = Verification(
                status="UNAVAILABLE",
                reference=LEDGER_VERIFY,
                detail="The engine-wide check did not establish coverage for these entries.",
                result=global_check.result,
            )
    else:
        scoped = Verification(
            status="UNAVAILABLE",
            reference=reference or LEDGER_VERIFY,
            detail="No verification covers the payment's collected ledger entries.",
        )
    return VerificationSummary(
        mandate_signature=Verification(
            status="UNAVAILABLE",
            detail="The contract exposes no standalone mandate-signature verification result.",
        ),
        cart_signature=_signature(data.cart, merchant, data),
        ledger=scoped,
        engine_ledger=global_check,
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
    merchant = None
    merchant_id = (
        data.cart.merchant_id if data.cart else data.proposal.merchant_id if data.proposal else None
    )
    if merchant_id and re.fullmatch(REFERENCE_PATTERN, merchant_id):
        path = f"/v1/merchants/{merchant_id}/public-key"
        merchant = _fetch(client, data, path, TypeAdapter(MerchantKey), name="merchant_key")
        if merchant is not None and merchant.merchant_id == merchant_id:
            data.put("merchant_key", merchant, path)
        else:
            merchant = None
    data.verification = _verify(client, data, merchant)
    data.sources.append(
        SourceCheck(
            source_system="SYSTEM_1",
            reference="docs/CONTRACT.md",
            status="UNAVAILABLE",
            detail="No merchant order or fulfillment read endpoint is documented.",
        )
    )
    return data
