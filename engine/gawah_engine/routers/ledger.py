from fastapi import APIRouter
from gawah_contract import EvidenceBundle, LedgerEntry, LedgerVerifyResult
from gawah_contract.fixtures import load
from gawah_contract.hashing import verify_chain

from ..errors import GawahError

router = APIRouter(prefix="/v1", tags=["ledger"])


@router.get("/ledger", response_model=list[LedgerEntry])
def list_ledger(mandate_id: str | None = None, subject_id: str | None = None) -> list[LedgerEntry]:
    entries = [LedgerEntry.model_validate(e) for e in load("ledger_chain")]
    if mandate_id:
        entries = [e for e in entries if e.data.get("mandate_id") == mandate_id]
    if subject_id:
        entries = [e for e in entries if e.subject_id == subject_id]
    return entries


@router.get("/ledger/verify", response_model=LedgerVerifyResult)
def verify_ledger() -> LedgerVerifyResult:
    entries = [LedgerEntry.model_validate(e) for e in load("ledger_chain")]
    ok, first_broken = verify_chain(entries)
    return LedgerVerifyResult(ok=ok, checked=len(entries), first_broken_seq=first_broken)


@router.get("/evidence/{payment_id}", response_model=EvidenceBundle)
def get_evidence(payment_id: str) -> EvidenceBundle:
    names = {"pay_01": "evidence_bundle", "pay_04": "evidence_bundle_wrong_size"}
    if payment_id not in names:
        raise GawahError(404, "PAYMENT_NOT_FOUND", f"No evidence for payment {payment_id}")
    return EvidenceBundle.model_validate(load(names[payment_id]))
