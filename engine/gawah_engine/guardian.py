"""The Guardian: deterministic purchase checks, then one constrained semantic check.

`Guardian.evaluate_full(...)` is a pure function of its inputs. It evaluates EVERY rule (it
does not stop at the first failure) and BLOCKs if any hard rule fails; the decision lists all
reason codes. The signed cart is the source of truth, never the proposal.

Rules, in the order reasons are reported:
  MANDATE_SIGNATURE_INVALID           mandate signature does not verify
  MANDATE_REVOKED / MANDATE_EXPIRED   mandate revoked, or past its expires_at
  MERCHANT_NOT_REGISTERED             the cart's merchant has no public key on file
  CART_SIGNATURE_INVALID              cart signature bad, or signed with an unknown key id
  CART_EXPIRED                        the cart is past its expires_at
  MERCHANT_MISMATCH                   the cart is from a different merchant than the proposal
  PRICE_MISMATCH                      proposal disagrees with the signed cart (price, quantity,
                                      total, currency, or the product is not in the cart)
  MERCHANT_NOT_ALLOWED                merchant (cart's or proposal's) not in the mandate
  CURRENCY_MISMATCH                   cart currency differs from the mandate currency
  OVER_BUDGET                         cart total exceeds the remaining budget
  DUPLICATE                           same merchant + cart + mandate proposed again within 60 s
  CATEGORY_MISMATCH                   semantic check (only if all of the above passed)

Variant problems (size, colour) never block. They are returned as `advisories`, stored in the
DECISION_MADE ledger entry and appended to the explanation, so Dispute Counsel can later
concede an allowed-but-wrong purchase.
"""

import json
import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from gawah_contract import (
    GuardianDecision,
    LedgerEntryType,
    Mandate,
    MerchantKey,
    ProposalResult,
    PurchaseProposal,
    ReasonCode,
    SignedCart,
    Verdict,
)
from gawah_contract.carts import load_public_key, verify_cart

from .canonical import canonical_json
from .db import connect, write_tx
from .errors import GawahError
from .ledger_service import LedgerService, format_ts, utcnow
from .mandate_service import (
    MandateService,
    check_currency,
    check_expiry,
    check_merchant,
    check_revocation,
)
from .merchant_service import MerchantService
from .payments import PaymentService
from .semantic import SemanticChecker, SemanticResult, keyword_check

DUPLICATE_WINDOW_S = 60

R = ReasonCode


def money(minor: int, currency: str) -> str:
    amount = f"{minor // 100}.{minor % 100:02d}"
    return f"${amount}" if currency == "USD" else f"{amount} {currency}"


# ---- deterministic variant advisories ---------------------------------------------------------

_SIZE = re.compile(r"\bsize\s*[:#]?\s*(\d+(?:\.\d+)?|xxs|xs|s|m|l|xl|xxl|xxxl)\b", re.IGNORECASE)
_COLORS = ["black", "white", "red", "blue", "green", "grey", "gray", "brown", "navy", "pink", "yellow", "orange", "purple", "beige", "silver", "gold"]


def _sizes(text: str | None) -> set[str]:
    return {m.casefold() for m in _SIZE.findall(text or "")}


def _colors(text: str | None) -> set[str]:
    words = set(re.findall(r"[a-z]+", (text or "").casefold()))
    return {"grey" if c == "gray" else c for c in _COLORS if c in words}


def variant_advisories(mandate: Mandate, cart: SignedCart) -> list[str]:
    """Explicit size/colour in the mandate description vs the cart lines (deterministic)."""
    want_sizes, want_colors = _sizes(mandate.item_description), _colors(mandate.item_description)
    out = []
    for li in cart.line_items:
        if want_sizes:
            got = _sizes(li.variant)
            if got and not (got & want_sizes):
                out.append(
                    f"Mandate asks for size {', '.join(sorted(want_sizes))} but the cart line "
                    f"'{li.title}' is {li.variant}"
                )
        if want_colors:
            got_c = _colors(f"{li.title} {li.variant or ''}")
            if got_c and not (got_c & want_colors):
                out.append(
                    f"Mandate asks for {', '.join(sorted(want_colors))} but the cart line "
                    f"'{li.title}' looks {', '.join(sorted(got_c))}"
                )
    return out


# ---- the pure evaluation ----------------------------------------------------------------------


@dataclass
class Evaluation:
    decision: GuardianDecision
    advisories: list[str] = field(default_factory=list)
    semantic: SemanticResult | None = None


SemanticFn = Callable[[Mandate, SignedCart], SemanticResult]


class Guardian:
    @staticmethod
    def evaluate(
        proposal: PurchaseProposal,
        mandate: Mandate,
        merchant_key: MerchantKey | None,
        remaining_minor: int,
        now: datetime,
        **kwargs,
    ) -> GuardianDecision:
        return Guardian.evaluate_full(
            proposal, mandate, merchant_key, remaining_minor, now, **kwargs
        ).decision

    @staticmethod
    def evaluate_full(
        proposal: PurchaseProposal,
        mandate: Mandate,
        merchant_key: MerchantKey | None,
        remaining_minor: int,
        now: datetime,
        *,
        mandate_check: tuple[bool, str | None] | None = None,
        merchant_registered: bool = True,
        duplicate_of: str | None = None,
        semantic: SemanticFn | None = None,
    ) -> Evaluation:
        """Evaluate every rule.

        mandate_check: result of MandateService.verify_mandate (signature + stored status). If
          None, only the status and expiry fields of `mandate` are checked (no signature check).
        merchant_key: the registered key for (cart.merchant_id, cart.key_id), or None.
        merchant_registered: False if the cart's merchant has no keys on file at all.
        duplicate_of: id of an earlier proposal for the same merchant, cart and mandate.
        semantic: called only if every rule above passed.
        """
        cart = proposal.signed_cart
        fails: list[tuple[ReasonCode, str]] = []

        def fail(code: ReasonCode, why: str) -> None:
            fails.append((code, why))

        # 1. mandate
        if mandate_check is not None and not mandate_check[0]:
            why = mandate_check[1]
            if why == "MANDATE_EXPIRED":
                fail(R.MANDATE_EXPIRED, "the mandate has expired")
            elif why == "MANDATE_REVOKED":
                fail(R.MANDATE_REVOKED, "the mandate was revoked")
            elif why == "MANDATE_SIGNATURE_INVALID":
                fail(R.MANDATE_SIGNATURE_INVALID, "the mandate signature does not verify")
            else:
                fail(R.MANDATE_SIGNATURE_INVALID, f"the mandate cannot be trusted ({why})")
        elif mandate_check is None and check_revocation(mandate):
            fail(R.MANDATE_REVOKED, "the mandate was revoked")
        if check_expiry(mandate, now) and not any(c == R.MANDATE_EXPIRED for c, _ in fails):
            fail(R.MANDATE_EXPIRED, f"the mandate expired at {format_ts(mandate.expires_at)}")

        # 2. cart authenticity
        if not merchant_registered:
            fail(R.MERCHANT_NOT_REGISTERED, f"merchant '{cart.merchant_id}' is not registered")
        elif merchant_key is None:
            fail(R.CART_SIGNATURE_INVALID, f"the cart is signed with unknown key '{cart.key_id}'")
        else:
            try:
                ok = verify_cart(cart, load_public_key(merchant_key.public_key_pem))
            except ValueError:
                ok = False
            if not ok:
                fail(R.CART_SIGNATURE_INVALID, "the cart signature does not verify")
        if cart.expires_at <= now:
            fail(R.CART_EXPIRED, f"the cart expired at {format_ts(cart.expires_at)}")
        if cart.merchant_id != proposal.merchant_id:
            fail(
                R.MERCHANT_MISMATCH,
                f"the cart is from '{cart.merchant_id}' but the proposal names "
                f"'{proposal.merchant_id}'",
            )

        # 3. proposal vs signed cart (the cart wins)
        lines_total = sum(li.quantity * li.unit_price_minor for li in cart.line_items)
        if lines_total != cart.total_minor:
            fail(R.PRICE_MISMATCH, "the cart total does not equal the sum of its line items")
        match = [li for li in cart.line_items if li.sku == proposal.product.id]
        if not match:
            skus = ", ".join(li.sku for li in cart.line_items) or "none"
            fail(
                R.PRICE_MISMATCH,
                f"the proposal's product '{proposal.product.id}' is not in the signed cart "
                f"(cart has: {skus})",
            )
        else:
            line = match[0]
            if line.quantity != proposal.quantity:
                fail(
                    R.PRICE_MISMATCH,
                    f"proposal quantity {proposal.quantity} differs from cart quantity "
                    f"{line.quantity}",
                )
            if line.unit_price_minor != proposal.unit_price_minor:
                fail(
                    R.PRICE_MISMATCH,
                    f"proposal unit price {money(proposal.unit_price_minor, proposal.currency)} "
                    f"differs from the signed cart price "
                    f"{money(line.unit_price_minor, cart.currency)}",
                )
        proposal_total = proposal.unit_price_minor * proposal.quantity
        if proposal_total != cart.total_minor:
            fail(
                R.PRICE_MISMATCH,
                f"proposal total {money(proposal_total, proposal.currency)} differs from the "
                f"signed cart total {money(cart.total_minor, cart.currency)}",
            )
        if proposal.currency != cart.currency:
            fail(
                R.PRICE_MISMATCH,
                f"proposal currency {proposal.currency} differs from cart currency {cart.currency}",
            )

        # 4. mandate scope
        for merchant in dict.fromkeys((cart.merchant_id, proposal.merchant_id)):
            if check_merchant(mandate, merchant):
                fail(R.MERCHANT_NOT_ALLOWED, f"merchant '{merchant}' is not allowed by the mandate")
        if check_currency(mandate, cart.currency):
            fail(
                R.CURRENCY_MISMATCH,
                f"cart currency {cart.currency} differs from the mandate currency {mandate.currency}",
            )
        if cart.total_minor > remaining_minor:
            fail(
                R.OVER_BUDGET,
                f"cart total {money(cart.total_minor, cart.currency)} exceeds the remaining "
                f"budget {money(remaining_minor, mandate.currency)}",
            )

        # 5. duplicate purchase
        if duplicate_of:
            fail(
                R.DUPLICATE,
                f"the same cart '{cart.cart_id}' was already proposed as '{duplicate_of}' "
                f"within {DUPLICATE_WINDOW_S} seconds",
            )

        # 6. the one semantic step: runs only if everything above passed, can only add a BLOCK
        sem: SemanticResult | None = None
        if not fails and semantic is not None:
            sem = semantic(mandate, cart)
            if sem.blocks:
                fail(
                    R.CATEGORY_MISMATCH,
                    f"the cart items do not match the mandate category '{mandate.category}' "
                    f"(confidence {sem.confidence:.2f})",
                )

        reasons = list(dict.fromkeys(code for code, _ in fails))
        advisories: list[str] = []
        if not fails:
            advisories = variant_advisories(mandate, cart)
            if sem is not None:
                advisories += [i for i in sem.variant_issues if i not in advisories]
                if sem.category_match == "mismatch":
                    advisories.append(
                        f"Category looks doubtful (confidence {sem.confidence:.2f}) but below the "
                        "blocking threshold"
                    )

        if fails:
            reasons_text = "; ".join(dict.fromkeys(why for _, why in fails))
            explanation = f"Blocked: {reasons_text[0].upper()}{reasons_text[1:]}."
        else:
            explanation = (
                f"Allowed: cart {money(cart.total_minor, cart.currency)} from "
                f"'{cart.merchant_id}' is within the mandate (remaining budget "
                f"{money(remaining_minor, mandate.currency)})."
            )
        if advisories:
            explanation += " Advisories: " + "; ".join(advisories) + "."
        if sem is not None:
            explanation += f" (semantic check: {sem.source})"
        decision = GuardianDecision(
            id=f"dec_{proposal.id}",
            proposal_id=proposal.id,
            verdict=Verdict.BLOCK if fails else Verdict.ALLOW,
            reasons=reasons,
            explanation=explanation,
            checked_at=now,
        )
        return Evaluation(decision, advisories, sem)


# ---- the service wrapper ----------------------------------------------------------------------


class GuardianService:
    """Loads Guardian inputs, writes the ledger (write first), stores one decision per proposal.

    Concurrency: proposals for the same mandate are serialised by a per-mandate lock. Inside it,
    the authoritative evaluation, the budget reservation, the payment row, DECISION_MADE and
    PAYMENT_STARTED are written in ONE `BEGIN IMMEDIATE` transaction, with the mandate status and
    remaining budget re-read inside that transaction (so a revocation that lands mid-flight is
    honoured and the budget already subtracts every reservation). PayPal is called only after
    the lock is released and the transaction has committed. (The lock is per process; across
    processes the SQLite write lock is what keeps reservations safe.)
    """

    def __init__(
        self,
        path: str,
        mandates: MandateService,
        merchants: MerchantService,
        ledger: LedgerService,
        semantic: SemanticChecker,
        clock: Callable[[], datetime] = utcnow,
        payments: PaymentService | None = None,
    ):
        self.path, self.mandates, self.merchants = path, mandates, merchants
        self.ledger, self.semantic, self.clock, self.payments = ledger, semantic, clock, payments
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _mandate_lock(self, mandate_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(mandate_id, threading.Lock())

    def _mandate(self, mandate_id: str) -> Mandate:
        return self.mandates.get(mandate_id)  # raises MandateNotFound (404)

    def _duplicate_of(
        self, conn: sqlite3.Connection, p: PurchaseProposal, now: datetime
    ) -> str | None:
        """An earlier proposal for the same mandate, merchant and cart within 60 s that was
        allowed or is still being decided."""
        row = conn.execute(
            "SELECT p.id FROM proposals p LEFT JOIN decisions d ON d.proposal_id = p.id"
            " WHERE p.id != ? AND p.mandate_id = ? AND p.merchant_id = ? AND p.cart_id = ?"
            " AND p.received_at >= ?"
            " AND (d.id IS NULL OR json_extract(d.body, '$.verdict') = 'ALLOW')"
            " ORDER BY p.received_at, p.rowid LIMIT 1",
            (
                p.id,
                p.mandate_id,
                p.merchant_id,
                p.signed_cart.cart_id,
                format_ts(now - timedelta(seconds=DUPLICATE_WINDOW_S)),
            ),
        ).fetchone()
        return row["id"] if row else None

    def _evaluate(
        self,
        p: PurchaseProposal,
        mandate: Mandate,
        remaining: int,
        mandate_check: tuple[bool, str | None],
        now: datetime,
        duplicate_of: str | None,
        semantic: SemanticFn,
    ) -> Evaluation:
        cart = p.signed_cart
        key = self.merchants.get(cart.merchant_id, cart.key_id)
        registered = key is not None or self.merchants.get(cart.merchant_id) is not None
        return Guardian.evaluate_full(
            p,
            mandate,
            key,
            remaining,
            now,
            mandate_check=mandate_check,
            merchant_registered=registered,
            duplicate_of=duplicate_of,
            semantic=semantic,
        )

    def check(self, p: PurchaseProposal) -> GuardianDecision:
        """Dry run: evaluates against current state, writes nothing to the ledger or proposals."""
        mandate = self._mandate(p.mandate_id)
        now = self.clock()
        conn = connect(self.path)
        try:
            dup = self._duplicate_of(conn, p, now)
        finally:
            conn.close()
        return self._evaluate(
            p, mandate, self.mandates.budget(mandate.id).remaining_minor,
            self.mandates.verify_mandate(mandate), now, dup, self.semantic.check,
        ).decision

    def _payment_of(self, proposal_id: str):
        return self.payments.get_for_proposal(proposal_id) if self.payments else None

    def _result(self, decision: GuardianDecision, proposal_id: str) -> ProposalResult:
        return ProposalResult(decision=decision, payment=self._payment_of(proposal_id))

    def submit(self, p: PurchaseProposal) -> ProposalResult:
        self._mandate(p.mandate_id)  # unknown mandate: 404 before anything is written
        now = self.clock()
        body = canonical_json(p.model_dump(mode="json")).decode()

        # Write first: store the proposal and PROPOSAL_RECEIVED before evaluating.
        with write_tx(self.path) as conn:
            prior = conn.execute("SELECT body FROM proposals WHERE id = ?", (p.id,)).fetchone()
            if prior is not None:
                if prior["body"] != body:
                    raise GawahError(
                        409,
                        "PROPOSAL_ID_CONFLICT",
                        f"Proposal id {p.id!r} was already used for a different proposal",
                    )
                done = conn.execute(
                    "SELECT body FROM decisions WHERE proposal_id = ?", (p.id,)
                ).fetchone()
                if done is not None:  # idempotent replay: earlier decision (and payment)
                    return self._result(GuardianDecision.model_validate_json(done["body"]), p.id)
            else:
                conn.execute(
                    "INSERT INTO proposals (id, mandate_id, merchant_id, cart_id, body,"
                    " received_at) VALUES (?,?,?,?,?,?)",
                    (p.id, p.mandate_id, p.merchant_id, p.signed_cart.cart_id, body, format_ts(now)),
                )
                self.ledger.append_in(
                    conn,
                    LedgerEntryType.PROPOSAL_RECEIVED,
                    p.id,
                    {
                        "mandate_id": p.mandate_id,
                        "agent_id": p.agent_id,
                        "merchant_id": p.merchant_id,
                        "cart_id": p.signed_cart.cart_id,
                        "amount_minor": p.signed_cart.total_minor,
                        "currency": p.signed_cart.currency,
                        "rationale": p.rationale[:500],
                    },
                )

        payment_id: str | None = None
        with self._mandate_lock(p.mandate_id):
            # Pass 1 (outside the DB write lock): may call the model; its answer is memoised.
            memo: dict[str, SemanticResult] = {}

            def semantic(m: Mandate, c: SignedCart) -> SemanticResult:
                memo["r"] = self.semantic.check(m, c)
                return memo["r"]

            mandate = self._mandate(p.mandate_id)
            conn = connect(self.path)
            try:
                dup = self._duplicate_of(conn, p, now)
            finally:
                conn.close()
            self._evaluate(
                p, mandate, self.mandates.budget(mandate.id).remaining_minor,
                self.mandates.verify_mandate(mandate), now, dup, semantic,
            )

            def semantic_no_network(m: Mandate, c: SignedCart) -> SemanticResult:
                return memo["r"] if "r" in memo else keyword_check(
                    m.category, m.item_description, [(i.sku, i.title, i.variant) for i in c.line_items]
                )

            # Pass 2: authoritative. Fresh mandate status and budget, then reserve and write.
            with write_tx(self.path) as conn:
                done = conn.execute(
                    "SELECT body FROM decisions WHERE proposal_id = ?", (p.id,)
                ).fetchone()
                if done is not None:  # a concurrent request for the same proposal decided first
                    return self._result(GuardianDecision.model_validate_json(done["body"]), p.id)
                mandate = self.mandates.get_in(conn, p.mandate_id)
                ev = self._evaluate(
                    p, mandate, self.mandates.budget_in(conn, mandate.id).remaining_minor,
                    self.mandates.verify_in(conn, mandate), now,
                    self._duplicate_of(conn, p, now), semantic_no_network,
                )
                d = ev.decision
                conn.execute(
                    "INSERT INTO decisions (id, proposal_id, body, advisories, decided_at)"
                    " VALUES (?,?,?,?,?)",
                    (d.id, p.id, d.model_dump_json(), json.dumps(ev.advisories), format_ts(now)),
                )
                self.ledger.append_in(
                    conn,
                    LedgerEntryType.DECISION_MADE,
                    d.id,
                    {
                        "mandate_id": p.mandate_id,
                        "proposal_id": p.id,
                        "verdict": d.verdict.value,
                        "reasons": [r.value for r in d.reasons],
                        "explanation": d.explanation,
                        "advisories": ev.advisories,
                        "amount_minor": p.signed_cart.total_minor,
                        "currency": p.signed_cart.currency,
                        "semantic_source": ev.semantic.source if ev.semantic else None,
                    },
                )
                if d.verdict == Verdict.ALLOW and self.payments is not None:
                    # Reserve + payment row + PAYMENT_STARTED, committed with the decision.
                    payment_id = self.payments.start_in(conn, p, d)

        # Outside every lock and transaction: the only place PayPal is called.
        if payment_id is not None and self.payments is not None:
            return ProposalResult(decision=d, payment=self.payments.execute(payment_id))
        return ProposalResult(decision=d, payment=None)
