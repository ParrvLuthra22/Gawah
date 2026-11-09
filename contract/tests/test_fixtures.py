import pytest
from gawah_contract import (
    DisputeCase,
    EvidenceBundle,
    GuardianDecision,
    LedgerEntry,
    Mandate,
    PaymentRecord,
    PurchaseProposal,
    SignedCart,
    Verdict,
)
from gawah_contract.fixtures import FIXTURES_DIR, load
from gawah_contract.hashing import verify_chain

MODELS = {
    "mandate_shoes": Mandate,
    "signed_cart_shoes": SignedCart,
    "proposal_shoes": PurchaseProposal,
    "decision_allow": GuardianDecision,
    "payment_paid": PaymentRecord,
    "evidence_bundle": EvidenceBundle,
    "proposal_poisoned_over_budget": PurchaseProposal,
    "decision_block_over_budget": GuardianDecision,
    "proposal_poisoned_merchant": PurchaseProposal,
    "decision_block_merchant": GuardianDecision,
    "proposal_wrong_size": PurchaseProposal,
    "evidence_bundle_wrong_size": EvidenceBundle,
    "dispute_defendable": DisputeCase,
    "dispute_wrong_size": DisputeCase,
}


def test_every_fixture_file_is_covered():
    names = {p.stem for p in FIXTURES_DIR.glob("*.json")}
    assert names == set(MODELS) | {"ledger_chain"}


@pytest.mark.parametrize("name", sorted(MODELS))
def test_fixture_validates(name):
    MODELS[name].model_validate(load(name))


def test_ledger_chain_is_valid_and_has_five_entries():
    entries = [LedgerEntry.model_validate(e) for e in load("ledger_chain")]
    assert len(entries) == 5
    assert verify_chain(entries) == (True, None)


def test_tampered_chain_is_detected():
    entries = [LedgerEntry.model_validate(e) for e in load("ledger_chain")]
    entries[2].data["verdict"] = "BLOCK"
    assert verify_chain(entries) == (False, 3)


def test_story_numbers():
    assert Mandate.model_validate(load("mandate_shoes")).max_amount_minor == 8000
    assert PurchaseProposal.model_validate(load("proposal_shoes")).unit_price_minor == 6500
    block = GuardianDecision.model_validate(load("decision_block_over_budget"))
    assert block.verdict == Verdict.BLOCK and [r.value for r in block.reasons] == ["OVER_BUDGET"]


def test_wrong_size_story():
    b = EvidenceBundle.model_validate(load("evidence_bundle_wrong_size"))
    assert "size 9" in b.mandate.item_description
    assert b.signed_cart.line_items[0].variant == "size 10"
