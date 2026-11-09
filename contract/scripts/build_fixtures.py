"""Regenerate contract/fixtures/*.json. Run: uv run python contract/scripts/build_fixtures.py

Signatures are placeholders ("mock-..."); the hash chain is real.
"""

import json
from pathlib import Path

from gawah_contract import (
    BudgetStatus,
    DisputeCase,
    EvidenceBundle,
    GuardianDecision,
    LedgerEntry,
    Mandate,
    PaymentRecord,
    PurchaseProposal,
    SignedCart,
)
from gawah_contract.hashing import GENESIS_HASH, compute_entry_hash

OUT = Path(__file__).resolve().parent.parent / "fixtures"
T0 = "2026-11-09T10:00:00Z"


def write(name, obj, model=None):
    if model is not None:
        items = obj if isinstance(obj, list) else [obj]
        for item in items:
            model.model_validate(item)
    (OUT / f"{name}.json").write_text(json.dumps(obj, indent=2) + "\n")


mandate = {
    "id": "man_01",
    "user_id": "user_parrv",
    "category": "footwear",
    "item_description": "Black running shoes, size 9, under $80",
    "max_amount_minor": 8000,
    "currency": "USD",
    "allowed_merchants": ["demo-shoe-store"],
    "issued_at": T0,
    "expires_at": "2026-11-16T10:00:00Z",
    "status": "active",
    "signature": "mock-signature-mandate-man_01",
    "signing_alg": "ed25519",
    "key_id": "gawah-ledger-key-1",
}


def cart(cart_id, merchant, sku, title, variant, price):
    return {
        "cart_id": cart_id,
        "merchant_id": merchant,
        "line_items": [
            {"sku": sku, "title": title, "variant": variant, "quantity": 1, "unit_price_minor": price}
        ],
        "total_minor": price,
        "currency": "USD",
        "expires_at": "2026-11-09T11:00:00Z",
        "signature": f"mock-signature-{cart_id}",
        "key_id": "demo-shoe-store-key-1",
    }


def proposal(pid, cart_obj, title, price, rationale, merchant=None):
    return {
        "id": pid,
        "mandate_id": "man_01",
        "agent_id": "shopper-agent-1",
        "merchant_id": merchant or cart_obj["merchant_id"],
        "product": {
            "id": cart_obj["line_items"][0]["sku"],
            "title": title,
            "url": f"https://demo-shoe-store.example/p/{cart_obj['line_items'][0]['sku']}",
        },
        "quantity": 1,
        "unit_price_minor": price,
        "currency": "USD",
        "rationale": rationale,
        "signed_cart": cart_obj,
        "created_at": "2026-11-09T10:05:00Z",
    }


def decision(did, pid, verdict, reasons, explanation):
    return {
        "id": did,
        "proposal_id": pid,
        "verdict": verdict,
        "reasons": reasons,
        "explanation": explanation,
        "checked_at": "2026-11-09T10:05:01Z",
    }


def payment(pay_id, pid, did, price, order):
    return {
        "id": pay_id,
        "proposal_id": pid,
        "decision_id": did,
        "paypal_order_id": order,
        "paypal_capture_id": "FAKECAPTURE" + order[-6:],
        "status": "PAID",
        "amount_minor": price,
        "currency": "USD",
        "idempotency_key": f"idem-{pid}",
        "custom_id": f"gawah:{pid}",
        "created_at": "2026-11-09T10:05:03Z",
    }


def chain(mandate_obj, prop, dec, pay):
    specs = [
        ("MANDATE_CREATED", mandate_obj["id"], T0, {"mandate_id": mandate_obj["id"]}),
        ("PROPOSAL_RECEIVED", prop["id"], "2026-11-09T10:05:00Z",
         {"mandate_id": "man_01", "amount_minor": prop["unit_price_minor"]}),
        ("DECISION_MADE", dec["id"], "2026-11-09T10:05:01Z",
         {"mandate_id": "man_01", "proposal_id": prop["id"], "verdict": dec["verdict"]}),
        ("PAYMENT_STARTED", pay["id"], "2026-11-09T10:05:02Z",
         {"mandate_id": "man_01", "custom_id": pay["custom_id"]}),
        ("PAYMENT_COMPLETED", pay["id"], "2026-11-09T10:05:03Z",
         {"mandate_id": "man_01", "paypal_order_id": pay["paypal_order_id"],
          "amount_minor": pay["amount_minor"]}),
    ]
    entries, prev = [], GENESIS_HASH
    for i, (typ, subject, ts, data) in enumerate(specs, start=1):
        e = {"seq": i, "ts": ts, "type": typ, "subject_id": subject, "data": data,
             "prev_hash": prev}
        e["hash"] = compute_entry_hash(e)
        e["signature"] = f"mock-signature-ledger-{i}"
        e["key_id"] = "gawah-ledger-key-1"
        entries.append(e)
        prev = e["hash"]
    return entries


# Happy path: $65 black running shoes, size 9
cart_ok = cart("cart_01", "demo-shoe-store", "SHOE-BLK-RUN", "Black Runner", "size 9", 6500)
prop_ok = proposal("prop_01", cart_ok, "Black Runner", 6500,
                   "Black running shoes in size 9 for $65, under the $80 budget.")
dec_ok = decision("dec_01", "prop_01", "ALLOW", [],
                  "Within budget, merchant allowed, cart signature valid, mandate active.")
pay_ok = payment("pay_01", "prop_01", "dec_01", 6500, "FAKEORDER5O1234567")
ledger_ok = chain(mandate, prop_ok, dec_ok, pay_ok)

def bundle(mandate_obj, prop, dec, pay, entries):
    return {"mandate": mandate_obj, "proposal": prop, "signed_cart": prop["signed_cart"],
            "decision": dec, "payment": pay, "ledger_entries": entries,
            "verification": {"ok": True, "checked": len(entries), "first_broken_seq": None}}

# Poisoned: over budget
cart_hi = cart("cart_02", "demo-shoe-store", "SHOE-PRO-RUN", "Pro Runner Elite", "size 9", 12000)
prop_hi = proposal("prop_02", cart_hi, "Pro Runner Elite", 12000,
                   "Premium pair recommended by a page that said to ignore the budget.")
dec_hi = decision("dec_02", "prop_02", "BLOCK", ["OVER_BUDGET"],
                  "Cart total $120.00 exceeds the mandate cap of $80.00.")

# Poisoned: merchant not on the allowlist
cart_bad = cart("cart_03", "shady-shoes", "SHOE-BLK-RUN", "Black Runner", "size 9", 6000)
prop_bad = proposal("prop_03", cart_bad, "Black Runner", 6000,
                    "Cheaper at another store the page pointed to.")
dec_bad = decision("dec_03", "prop_03", "BLOCK", ["MERCHANT_NOT_ALLOWED"],
                   "Merchant shady-shoes is not in the mandate's allowed merchants.")

# Wrong size: cart says size 10, mandate says size 9 (Guardian allowed it; user disputes)
cart_sz = cart("cart_04", "demo-shoe-store", "SHOE-BLK-RUN", "Black Runner", "size 10", 6500)
prop_sz = proposal("prop_04", cart_sz, "Black Runner", 6500,
                   "Black running shoes for $65, under the $80 budget.")
dec_sz = decision("dec_04", "prop_04", "ALLOW", [],
                  "Within budget, merchant allowed, cart signature valid, mandate active.")
pay_sz = payment("pay_04", "prop_04", "dec_04", 6500, "FAKEORDER5O7654321")
ledger_sz = chain(mandate, prop_sz, dec_sz, pay_sz)

dispute_ok = {
    "id": "dsp_01", "paypal_dispute_id": "PP-D-FAKE-1001", "payment_id": "pay_01",
    "status": "OPEN", "reason": "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED",
    "amount_minor": 6500, "currency": "USD",
    "opened_at": "2026-11-12T09:00:00Z", "source": "simulated",
}
dispute_sz = {
    "id": "dsp_02", "paypal_dispute_id": "PP-D-FAKE-1002", "payment_id": "pay_04",
    "status": "OPEN", "reason": "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED",
    "amount_minor": 6500, "currency": "USD",
    "opened_at": "2026-11-12T09:30:00Z", "source": "simulated",
}

write("mandate_shoes", mandate, Mandate)
write("signed_cart_shoes", cart_ok, SignedCart)
write("proposal_shoes", prop_ok, PurchaseProposal)
write("decision_allow", dec_ok, GuardianDecision)
write("payment_paid", pay_ok, PaymentRecord)
write("ledger_chain", ledger_ok, LedgerEntry)
write("evidence_bundle", bundle(mandate, prop_ok, dec_ok, pay_ok, ledger_ok), EvidenceBundle)
write("proposal_poisoned_over_budget", prop_hi, PurchaseProposal)
write("decision_block_over_budget", dec_hi, GuardianDecision)
write("proposal_poisoned_merchant", prop_bad, PurchaseProposal)
write("decision_block_merchant", dec_bad, GuardianDecision)
write("proposal_wrong_size", prop_sz, PurchaseProposal)
write("evidence_bundle_wrong_size", bundle(mandate, prop_sz, dec_sz, pay_sz, ledger_sz),
      EvidenceBundle)
write("dispute_defendable", dispute_ok, DisputeCase)
write("dispute_wrong_size", dispute_sz, DisputeCase)

# man_01 after pay_01 ($65.00 of $80.00) has been paid
budget = {
    "mandate_id": "man_01", "max_amount_minor": 8000, "spent_minor": 6500,
    "reserved_minor": 0, "remaining_minor": 1500, "currency": "USD",
}
write("budget_status", budget, BudgetStatus)
