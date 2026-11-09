"""Evidence bundle for Dispute Counsel (GET /v1/evidence/{payment_id}).

Everything is read from our own records, and the `verification` block is computed live on every
call: (1) the whole ledger chain (hashes, links, signatures), (2) the mandate's signature, and
(3) the signed cart against the merchant key registered for it.

The contract's `verification` field is a `LedgerVerifyResult` (`ok, checked, first_broken_seq,
reason`), which has no slot for the mandate and cart checks. So `ok` is the AND of all three,
`checked`/`first_broken_seq` describe the ledger, and `reason` is null when everything is fine
and otherwise lists each failed check. Advisories live in the DECISION_MADE ledger data; the
contract's `GuardianDecision` has no field for them, so they are appended to its `explanation`
(which already has them, since the Guardian writes them there).
"""

import json

from gawah_contract import (
    EvidenceBundle,
    GuardianDecision,
    LedgerVerifyResult,
    PurchaseProposal,
)
from gawah_contract.carts import load_public_key, verify_cart

from .db import connect
from .errors import GawahError
from .ledger_service import LedgerService
from .mandate_service import MandateService, signable
from .merchant_service import MerchantService
from .payments import PaymentService


class EvidenceService:
    def __init__(
        self,
        path: str,
        mandates: MandateService,
        merchants: MerchantService,
        ledger: LedgerService,
        payments: PaymentService,
    ):
        self.path, self.mandates, self.merchants = path, mandates, merchants
        self.ledger, self.payments = ledger, payments

    def bundle(self, payment_id: str) -> EvidenceBundle:
        payment = self.payments.get(payment_id)  # 404 PAYMENT_NOT_FOUND
        conn = connect(self.path)
        try:
            prow = conn.execute(
                "SELECT body FROM proposals WHERE id = ?", (payment.proposal_id,)
            ).fetchone()
            drow = conn.execute(
                "SELECT body, advisories FROM decisions WHERE proposal_id = ?",
                (payment.proposal_id,),
            ).fetchone()
        finally:
            conn.close()
        if prow is None or drow is None:
            raise GawahError(404, "EVIDENCE_INCOMPLETE", f"Missing proposal/decision for {payment_id}")
        proposal = PurchaseProposal.model_validate_json(prow["body"])
        decision = GuardianDecision.model_validate_json(drow["body"])
        advisories = json.loads(drow["advisories"])
        if advisories and not all(a in decision.explanation for a in advisories):
            decision = decision.model_copy(
                update={"explanation": decision.explanation + " Advisories: " + "; ".join(advisories) + "."}
            )
        mandate = self.mandates.get(proposal.mandate_id)
        cart = proposal.signed_cart

        entries = {e.seq: e for e in self.ledger.list(mandate_id=mandate.id)}
        for subject in (proposal.id, decision.id, payment.id, mandate.id):
            entries.update({e.seq: e for e in self.ledger.list(subject_id=subject)})

        chain = self.ledger.verify()
        problems = []
        if not chain["ok"]:
            problems.append(f"ledger: {chain['reason']}")
        if not self.mandates.signer.verify(signable(mandate), mandate.signature, mandate.key_id):
            problems.append("mandate signature does not verify")
        key = self.merchants.get(cart.merchant_id, cart.key_id)
        if key is None:
            problems.append(f"cart signing key '{cart.key_id}' is not registered for '{cart.merchant_id}'")
        elif not verify_cart(cart, load_public_key(key.public_key_pem)):
            problems.append("cart signature does not verify against the registered merchant key")

        return EvidenceBundle(
            mandate=mandate,
            proposal=proposal,
            signed_cart=cart,
            decision=decision,
            payment=payment,
            ledger_entries=[entries[s] for s in sorted(entries)],
            verification=LedgerVerifyResult(
                ok=not problems,
                checked=chain["checked"],
                first_broken_seq=chain["first_broken_seq"],
                reason="; ".join(problems) or None,
            ),
        )
