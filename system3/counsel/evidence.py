import hashlib
import json
from datetime import datetime
from typing import Annotated

from pydantic import StringConstraints
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from .clients import IntegrationError, System2Client
from .evidence_checks import contradictions, missing_and_invalid, timeline, timestamp
from .evidence_collection import REFERENCE_PATTERN, CollectedEvidence, collect_evidence
from .evidence_schemas import (
    Completeness,
    EvidenceItem,
    EvidencePackResponse,
    EvidenceSnapshot,
    SourceCheck,
)
from .models import Dispute, EvidencePack, Source, now
from .schemas import DisputeSummary

PaymentReference = Annotated[str, StringConstraints(pattern=REFERENCE_PATTERN)]


def find_dispute(db: Session, payment_id: str, dispute_id: int | None) -> DisputeSummary | None:
    rows = select(Dispute).where(
        or_(
            Dispute.payment_id == payment_id,
            # The shared simulation identifies pay_04 even when N1 cannot link it to /v1/payments.
            (Dispute.source == Source.SIMULATED_FIXTURE)
            & (Dispute.raw_response["payment_id"].as_string() == payment_id),
        )
    )
    if dispute_id is not None:
        rows = rows.where(Dispute.id == dispute_id)
    matches = db.scalars(rows.order_by(Dispute.id).limit(2)).all()
    if dispute_id is not None and not matches:
        raise IntegrationError(
            "DISPUTE_NOT_FOUND", "No matching dispute was found for this payment.", 404
        )
    if len(matches) > 1:
        raise IntegrationError(
            "DISPUTE_REQUIRED",
            "This payment has multiple disputes. Open the Evidence section from a specific dispute.",
            409,
        )
    return DisputeSummary.model_validate(matches[0]) if matches else None


def normalize(
    data: CollectedEvidence,
    payment_id: str,
    dispute: DisputeSummary | None,
    dispute_retrieved_at: datetime,
) -> EvidenceSnapshot:
    assert data.verification is not None
    paypal_id = dispute.paypal_dispute_id if dispute else None
    items: dict[str, EvidenceItem] = {}

    def add(
        name, key, model, record_id, original_time, reference, status="UNAVAILABLE", source=None
    ):
        if model is None:
            return
        exclusions = {"proposal": {"signed_cart"}, "dispute": {"last_synced_at"}}
        retrieved = (
            dispute_retrieved_at
            if name == "dispute"
            else data.retrieved_at["ledger_entries" if name == "ledger_entry" else name]
        )
        items[key] = EvidenceItem(
            id=f"{name}:{record_id}",
            type=name,
            source_system=source or data.source,
            source_record_id=str(record_id),
            source_reference=reference,
            dispute_id=paypal_id,
            payment_id=payment_id,
            timestamp=timestamp(original_time),
            retrieved_at=retrieved,
            verification_status=status,
            payload=model.model_dump(mode="json", exclude=exclusions.get(name)),
        )

    for name, record_id, time_field in (
        ("mandate", "id", "issued_at"),
        ("proposal", "id", "created_at"),
        ("signed_cart", "cart_id", None),
        ("decision", "id", "checked_at"),
        ("payment", "id", "created_at"),
        ("merchant_key", "key_id", None),
    ):
        record = data.records.get(name)
        if record is None:
            continue
        status = data.verification.cart_signature.status if name == "signed_cart" else "UNAVAILABLE"
        add(
            name,
            name,
            record,
            getattr(record, record_id),
            getattr(record, time_field) if time_field else None,
            data.references[name],
            status,
        )
    if dispute:
        reference = f"/api/disputes/{dispute.id}"
        add(
            "dispute",
            "dispute",
            dispute,
            paypal_id,
            dispute.created_at,
            reference,
            source="SYSTEM_3",
        )
        data.sources.append(
            SourceCheck(
                source_system="SYSTEM_3",
                reference=reference,
                status="AVAILABLE",
                detail=f"N1 dispute source is {dispute.source}.",
            )
        )
    else:
        data.sources.append(
            SourceCheck(
                source_system="SYSTEM_3",
                reference="/api/disputes",
                status="NOT_FOUND",
                detail="No N1 dispute is linked to this payment.",
            )
        )
    for entry in data.ledger:
        check = data.verification.ledger
        broken = (check.result or {}).get("first_broken_seq")
        status = check.status
        if status == "INVALID" and entry.seq != broken:
            status = "UNAVAILABLE"
        add(
            "ledger_entry",
            f"ledger:{entry.seq}",
            entry,
            entry.seq,
            entry.ts,
            data.references["ledger_entries"],
            status,
        )

    missing, invalid = missing_and_invalid(data, items, payment_id)
    evidence = list(items.values())
    verified = sum(item.verification_status == "VERIFIED" for item in evidence)
    return EvidenceSnapshot(
        dispute_id=paypal_id,
        dispute_record_id=dispute.id if dispute else None,
        payment_id=payment_id,
        evidence=evidence,
        timeline=timeline(evidence),
        verification_summary=data.verification,
        sources=data.sources,
        missing_evidence=missing,
        invalid_evidence=invalid,
        contradictions=contradictions(data, items),
        completeness=Completeness(
            available=len(evidence),
            missing=len(missing),
            verified=verified,
            unverified=len(evidence) - verified,
        ),
    )


def _payment_packs(payment_id: str):
    return select(EvidencePack).where(EvidencePack.payment_id == payment_id)


def _response(row: EvidencePack) -> EvidencePackResponse:
    return EvidencePackResponse(
        **row.snapshot,
        id=row.id,
        version=row.version,
        generated_at=row.generated_at,
    )


def get_pack(
    db: Session,
    payment_id: str,
    *,
    dispute_id: int | None = None,
    version: int | None = None,
) -> EvidencePackResponse:
    query = _payment_packs(payment_id)
    if dispute_id is not None:
        query = query.where(EvidencePack.dispute_id == dispute_id)
    if version is not None:
        query = query.where(EvidencePack.version == version)
    row = db.scalar(query.order_by(EvidencePack.version.desc()).limit(1))
    if row is None:
        raise IntegrationError(
            "EVIDENCE_PACK_NOT_FOUND",
            "No evidence pack has been assembled for this reference.",
            404,
        )
    return _response(row)


def assemble_pack(
    db: Session,
    client: System2Client,
    payment_id: str,
    *,
    dispute_id: int | None = None,
) -> EvidencePackResponse:
    dispute = find_dispute(db, payment_id, dispute_id)
    dispute_retrieved = now()
    db.rollback()  # Release the N1 read transaction before the external requests.
    snapshot = normalize(
        collect_evidence(client, payment_id, has_dispute=dispute is not None),
        payment_id,
        dispute,
        dispute_retrieved,
    )
    payload = snapshot.model_dump(mode="json")
    material = snapshot.model_dump(mode="json", exclude={"evidence": {"__all__": {"retrieved_at"}}})
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    # Serialize version allocation and comparison for the same payment in PostgreSQL.
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(731003002, hashtext(:payment_id))"),
            {"payment_id": payment_id},
        )
    else:  # SQLite is used by the local test environment.
        db.execute(text("BEGIN IMMEDIATE"))
    packs = _payment_packs(payment_id)
    latest_scope = db.scalar(
        packs.where(EvidencePack.dispute_id == snapshot.dispute_record_id)
        .order_by(EvidencePack.version.desc())
        .limit(1)
    )
    if latest_scope and latest_scope.fingerprint == digest:
        return _response(latest_scope)
    latest = db.scalar(packs.order_by(EvidencePack.version.desc()).limit(1))
    row = EvidencePack(
        payment_id=payment_id,
        dispute_id=snapshot.dispute_record_id,
        version=latest.version + 1 if latest else 1,
        fingerprint=digest,
        generated_at=now(),
        snapshot=payload,
    )
    db.add(row)
    db.commit()
    return _response(row)
