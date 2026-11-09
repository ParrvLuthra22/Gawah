from fastapi import APIRouter, Depends
from gawah_contract import EvidenceBundle, LedgerEntry, LedgerEntryType, LedgerVerifyResult
from gawah_contract.fixtures import load
from gawah_contract.hashing import verify_chain

from ..auth import require_mock
from ..config import settings
from ..errors import GawahError
from ..services import get_services

router = APIRouter(prefix="/v1", tags=["ledger"])

# GAWAH_MOCK=1 serves the fixture chain; GAWAH_MOCK=0 serves the real SQLite ledger.


@router.get("/ledger", response_model=list[LedgerEntry])
def list_ledger(
    mandate_id: str | None = None,
    subject_id: str | None = None,
    type: LedgerEntryType | None = None,
    since_seq: int | None = None,
) -> list[LedgerEntry]:
    if not settings.gawah_mock:
        return get_services().ledger.list(
            mandate_id=mandate_id, subject_id=subject_id, type=type, since_seq=since_seq
        )
    entries = [LedgerEntry.model_validate(e) for e in load("ledger_chain")]
    if mandate_id:
        entries = [e for e in entries if e.data.get("mandate_id") == mandate_id]
    if subject_id:
        entries = [e for e in entries if e.subject_id == subject_id]
    if type:
        entries = [e for e in entries if e.type == type]
    if since_seq is not None:
        entries = [e for e in entries if e.seq > since_seq]
    return entries


@router.get("/ledger/verify", response_model=LedgerVerifyResult)
def verify_ledger() -> LedgerVerifyResult:
    if not settings.gawah_mock:
        r = get_services().ledger.verify()
        return LedgerVerifyResult(**r)
    entries = [LedgerEntry.model_validate(e) for e in load("ledger_chain")]
    ok, first_broken = verify_chain(entries)
    reason = None if ok else f"hash or prev_hash mismatch at seq {first_broken}"
    return LedgerVerifyResult(
        ok=ok, checked=len(entries), first_broken_seq=first_broken, reason=reason
    )


@router.get("/evidence/{payment_id}", response_model=EvidenceBundle, dependencies=[Depends(require_mock)])
def get_evidence(payment_id: str) -> EvidenceBundle:
    names = {"pay_01": "evidence_bundle", "pay_04": "evidence_bundle_wrong_size"}
    if payment_id not in names:
        raise GawahError(404, "PAYMENT_NOT_FOUND", f"No evidence for payment {payment_id}")
    return EvidenceBundle.model_validate(load(names[payment_id]))
