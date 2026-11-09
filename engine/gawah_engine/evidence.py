"""Evidence bundle for Dispute Counsel (GET /v1/evidence/{payment_id}).

Everything is read from our own records, and the `verification` block is computed live on every
call. `verification.checks` lists four named checks: ledger_chain (hashes, links, signatures),
mandate_signature, cart_signature (against the merchant key registered for it) and
payment_reference_match (PayPal's custom_id points at this payment's PAYMENT_STARTED ledger
entry). `ok` is the AND of all four, `checked`/`first_broken_seq` describe the ledger, and
`reason` is null when everything is fine, otherwise it lists each failed check.

Advisories live in the DECISION_MADE ledger data; the contract's `GuardianDecision` has no field
for them, so they are appended to its `explanation` (the Guardian already writes them there).
"""

import json

from gawah_contract import (
    EvidenceBundle,
    GuardianDecision,
    LedgerEntryType,
    LedgerVerifyResult,
    PurchaseProposal,
    VerificationCheck,
)
from gawah_contract.carts import load_public_key, verify_cart

from .db import connect
from .errors import GawahError
from .ledger_service import LedgerService
from .mandate_service import MandateService, signable
from .merchant_service import MerchantService
from .payments import PaymentService, make_custom_id


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
        checks = [
            VerificationCheck(
                name="ledger_chain",
                ok=bool(chain["ok"]),
                detail=(f"{chain['checked']} entries verified" if chain["ok"]
                        else f"{chain['reason']}"),
            )
        ]
        mandate_ok = self.mandates.signer.verify(signable(mandate), mandate.signature, mandate.key_id)
        checks.append(VerificationCheck(
            name="mandate_signature", ok=mandate_ok,
            detail=f"ES256 by {mandate.key_id}" if mandate_ok else "mandate signature does not verify",
        ))
        key = self.merchants.get(cart.merchant_id, cart.key_id)
        if key is None:
            cart_ok, cart_detail = False, (
                f"cart signing key '{cart.key_id}' is not registered for '{cart.merchant_id}'")
        elif verify_cart(cart, load_public_key(key.public_key_pem)):
            cart_ok, cart_detail = True, f"ES256 by {cart.merchant_id}/{cart.key_id}"
        else:
            cart_ok, cart_detail = False, (
                "cart signature does not verify against the registered merchant key")
        checks.append(VerificationCheck(name="cart_signature", ok=cart_ok, detail=cart_detail))
        ref_ok, ref_detail = self._reference_match(payment, list(entries.values()))
        checks.append(VerificationCheck(name="payment_reference_match", ok=ref_ok, detail=ref_detail))

        problems = []
        if not chain["ok"]:
            problems.append(f"ledger: {chain['reason']}")
        problems += [c.detail for c in checks[1:] if not c.ok]

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
                checks=checks,
            ),
        )

    @staticmethod
    def _reference_match(payment, entries) -> tuple[bool, str]:
        """PayPal's custom_id must point at this payment's PAYMENT_STARTED ledger entry, and the
        PAYMENT_COMPLETED entry (if any) must repeat the same order, capture and custom_id."""
        started = [e for e in entries
                   if e.type == LedgerEntryType.PAYMENT_STARTED and e.subject_id == payment.id]
        if not started:
            return False, "no PAYMENT_STARTED ledger entry for this payment"
        expected = make_custom_id(payment.proposal_id, started[0].seq)
        if payment.custom_id != expected:
            return False, f"custom_id {payment.custom_id!r} does not match ledger reference {expected!r}"
        done = [e for e in entries
                if e.type == LedgerEntryType.PAYMENT_COMPLETED and e.subject_id == payment.id]
        if done:
            d = done[0].data
            if (d.get("custom_id"), d.get("paypal_order_id"), d.get("paypal_capture_id")) != (
                payment.custom_id, payment.paypal_order_id, payment.paypal_capture_id):
                return False, "PAYMENT_COMPLETED entry disagrees with the payment record"
        return True, f"custom_id {payment.custom_id} matches ledger seq {started[0].seq}"
