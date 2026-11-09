from fastapi import APIRouter
from gawah_contract import (
    GuardianDecision,
    PaymentRecord,
    ProposalResult,
    PurchaseProposal,
    Verdict,
)
from gawah_contract.fixtures import load

router = APIRouter(prefix="/v1", tags=["proposals"])

# Mock behaviour: known fixture proposal ids get their fixture decision. Any other proposal
# is ALLOWed unless total > 8000 minor units (OVER_BUDGET) or merchant != demo-shoe-store.
_DECISIONS = {
    "prop_01": "decision_allow",
    "prop_02": "decision_block_over_budget",
    "prop_03": "decision_block_merchant",
}


def mock_decide(p: PurchaseProposal) -> GuardianDecision:
    if p.id in _DECISIONS:
        return GuardianDecision.model_validate(load(_DECISIONS[p.id]))
    if p.merchant_id != "demo-shoe-store":
        base = load("decision_block_merchant")
    elif p.unit_price_minor * p.quantity > 8000:
        base = load("decision_block_over_budget")
    else:
        base = load("decision_allow")
    d = GuardianDecision.model_validate(base)
    return d.model_copy(update={"id": f"dec_{p.id}", "proposal_id": p.id})


@router.post("/proposals", response_model=ProposalResult)
def submit_proposal(p: PurchaseProposal) -> ProposalResult:
    d = mock_decide(p)
    if d.verdict == Verdict.BLOCK:
        return ProposalResult(decision=d, payment=None)
    pay = PaymentRecord.model_validate(load("payment_paid")).model_copy(
        update={
            "id": f"pay_{p.id}",
            "proposal_id": p.id,
            "decision_id": d.id,
            "amount_minor": p.unit_price_minor * p.quantity,
            "currency": p.currency,
            "idempotency_key": f"idem-{p.id}",
            "custom_id": f"gawah:{p.id}",
        }
    )
    return ProposalResult(decision=d, payment=pay)


@router.post("/guardian/check", response_model=GuardianDecision)
def guardian_check(p: PurchaseProposal) -> GuardianDecision:
    """Dry run: no payment, no ledger write."""
    return mock_decide(p)

