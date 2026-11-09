"""Payments: reserve, write ahead, pay through PayPal, then settle. Write first, pay second.

Flow for an ALLOW decision (steps a-c run inside the Guardian's locked write transaction, so
they are committed before any PayPal call):
  a. reserve budget (idempotency key = proposal id) and insert the payment, status RESERVED
  b. append PAYMENT_STARTED to the ledger
  c. set custom_id = "gawah:{proposal_id}:{ledger_seq}" (<= 127 chars) so the PayPal order
     points back to the ledger entry
  d. (outside any lock) create + pay the order with PayPalClient.pay_order; the PayPal request
     id is derived from the proposal id, so a retry can never create a second order. The order
     id is stored the moment the order exists.
  e. success: status PAID, reservation committed (spent += amount), PAYMENT_COMPLETED
  f. failure: status FAILED with a reason, reservation released, PAYMENT_FAILED

Transient errors (timeouts, network errors, 429, 5xx) are retried up to 3 times with exponential
backoff and the same request id. Other 4xx fail at once. If an order id exists when a call fails,
the outcome is never guessed: the order is read back from PayPal and the payment is finalised
from its real status. `reconcile()` does the same for payments left RESERVED by a crash.

Secrets: card details come only from `PaymentConfig` (env/config). Nothing about the card, the
PayPal tokens or raw PayPal error bodies is ever logged, ledgered, stored or returned.
"""

import hashlib
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from gawah_contract import (
    GuardianDecision,
    LedgerEntryType,
    PaymentRecord,
    PaymentStatus,
    PurchaseProposal,
)

from .db import connect, write_tx
from .errors import GawahError
from .ledger_service import LedgerService, format_ts, utcnow
from .mandate_service import MandateService
from .paypal import PayPalClient, PayPalError, capture_id_of, pay_order

log = logging.getLogger("gawah.payments")

MAX_RETRIES = 3
BACKOFF_BASE_S = 0.5
CUSTOM_ID_MAX = 127
PENDING = "PENDING"


@dataclass(frozen=True, repr=False)
class PaymentConfig:
    client_id: str = ""
    client_secret: str = ""
    env: str = "sandbox"
    card_number: str = ""
    card_expiry: str = ""
    card_cvv: str = ""
    card_name: str = "Gawah Demo"
    buyer_email: str = ""
    buyer_password: str = ""
    transport: Any = None  # tests inject an httpx.MockTransport

    def __repr__(self) -> str:  # never print secrets, even by accident
        return "PaymentConfig(<redacted>)"

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def card(self) -> dict[str, str] | None:
        if not (self.card_number and self.card_expiry and self.card_cvv):
            return None
        return {
            "number": self.card_number,
            "expiry": self.card_expiry,
            "security_code": self.card_cvv,
            "name": self.card_name,
        }


def idempotency_key(proposal_id: str) -> str:
    """Deterministic PayPal-Request-Id base for a proposal (<= 93 chars; suffixes add <= 7)."""
    if len(proposal_id) <= 88 and proposal_id.isprintable():
        return f"idem-{proposal_id}"
    return "idem-" + hashlib.sha256(proposal_id.encode()).hexdigest()[:40]


def make_custom_id(proposal_id: str, ledger_seq: int) -> str:
    ref = proposal_id
    custom = f"gawah:{ref}:{ledger_seq}"
    if len(custom) > CUSTOM_ID_MAX:
        ref = hashlib.sha256(proposal_id.encode()).hexdigest()[:32]
        custom = f"gawah:{ref}:{ledger_seq}"
    if len(custom) > CUSTOM_ID_MAX:
        raise ValueError("custom_id exceeds 127 characters")
    return custom


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):  # includes timeouts
        return True
    return isinstance(exc, PayPalError) and (exc.status == 429 or exc.status >= 500)


def describe_error(exc: BaseException) -> str:
    """A short, safe reason: status, PayPal error name/issue codes and debug id. Never raw bodies."""
    if isinstance(exc, PayPalError):
        if exc.status == 0:
            return str(exc)[:200]
        body = exc.body if isinstance(exc.body, dict) else {}
        issues = [str(d.get("issue")) for d in body.get("details", []) if isinstance(d, dict)]
        parts = [f"PayPal {exc.status}", str(body.get("name") or "")] + issues[:3]
        return (" ".join(p for p in parts if p) + f" (debug_id={exc.debug_id})")[:300]
    if isinstance(exc, httpx.TimeoutException):
        return "PayPal request timed out"
    if isinstance(exc, httpx.HTTPError):
        return "PayPal network error"
    return f"unexpected error: {type(exc).__name__}"


def classify_order(order: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """(outcome, capture_id, failure_reason) from a PayPal order. outcome: PAID|FAILED|PENDING."""
    status = order.get("status")
    if status == "COMPLETED":
        for pu in order.get("purchase_units", []):
            for cap in pu.get("payments", {}).get("captures", []):
                cs = cap.get("status")
                if cs == "COMPLETED":
                    return "PAID", cap["id"], None
                if cs == "PENDING":
                    return PENDING, cap["id"], None
                return "FAILED", cap.get("id"), f"capture status {cs}"
        return PENDING, None, None  # completed but no capture visible yet: do not guess
    return "FAILED", None, f"order not completed (status {status})"


class PaymentService:
    def __init__(
        self,
        path: str,
        mandates: MandateService,
        ledger: LedgerService,
        config: PaymentConfig,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.path, self.mandates, self.ledger = path, mandates, ledger
        self.config, self.clock, self.sleep = config, clock, sleep
        self._client: PayPalClient | None = None

    # ---- PayPal -------------------------------------------------------------------------
    def client(self) -> PayPalClient:
        if self._client is None:
            if not self.config.configured:
                raise PayPalError(0, {}, None, "PayPal credentials are not configured")
            c = self.config
            self._client = PayPalClient(
                c.client_id, c.client_secret, c.env or "sandbox", transport=c.transport
            )
        return self._client

    def _retry(self, fn: Callable[[], Any], counter: dict[str, int]) -> Any:
        """Run `fn`; retry transient failures up to MAX_RETRIES times with exponential backoff."""
        retries = 0
        while True:
            counter["n"] += 1
            try:
                return fn()
            except Exception as e:
                if not is_transient(e) or retries >= MAX_RETRIES:
                    raise
                self.sleep(BACKOFF_BASE_S * 2**retries)
                retries += 1

    # ---- rows ---------------------------------------------------------------------------
    @staticmethod
    def to_record(row: sqlite3.Row) -> PaymentRecord:
        return PaymentRecord(
            id=row["id"],
            proposal_id=row["proposal_id"],
            decision_id=row["decision_id"],
            paypal_order_id=row["paypal_order_id"],
            paypal_capture_id=row["paypal_capture_id"],
            status=row["status"],
            amount_minor=row["amount_minor"],
            currency=row["currency"],
            idempotency_key=row["idempotency_key"],
            custom_id=row["custom_id"],
            created_at=row["created_at"],
        )

    def _row(self, payment_id: str) -> sqlite3.Row | None:
        conn = connect(self.path)
        try:
            return conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
        finally:
            conn.close()

    def get(self, payment_id: str) -> PaymentRecord:
        row = self._row(payment_id)
        if row is None:
            raise GawahError(404, "PAYMENT_NOT_FOUND", f"No payment {payment_id}")
        return self.to_record(row)

    def get_for_proposal(self, proposal_id: str) -> PaymentRecord | None:
        conn = connect(self.path)
        try:
            row = conn.execute(
                "SELECT * FROM payments WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        finally:
            conn.close()
        return self.to_record(row) if row else None

    def list(
        self,
        *,
        mandate_id: str | None = None,
        status: PaymentStatus | str | None = None,
        paypal_order_id: str | None = None,
        paypal_capture_id: str | None = None,
    ) -> list[PaymentRecord]:
        where, args = [], []
        for col, val in (
            ("mandate_id", mandate_id),
            ("status", PaymentStatus(status).value if status else None),
            ("paypal_order_id", paypal_order_id),
            ("paypal_capture_id", paypal_capture_id),
        ):
            if val is not None:
                where.append(f"{col} = ?")
                args.append(val)
        sql = "SELECT * FROM payments" + (" WHERE " + " AND ".join(where) if where else "")
        conn = connect(self.path)
        try:
            return [self.to_record(r) for r in conn.execute(sql + " ORDER BY created_at, rowid", args)]
        finally:
            conn.close()

    # ---- steps a-c (inside the Guardian's write transaction) ----------------------------
    def start_in(
        self, conn: sqlite3.Connection, p: PurchaseProposal, decision: GuardianDecision
    ) -> str:
        """Reserve budget, insert the payment (RESERVED), append PAYMENT_STARTED, set custom_id."""
        cart = p.signed_cart
        payment_id = f"pay_{p.id}"
        now = format_ts(self.clock())
        self.mandates.reserve_in(conn, p.mandate_id, cart.total_minor, p.id)  # a
        conn.execute(
            "INSERT INTO payments (id, proposal_id, decision_id, mandate_id, status, amount_minor,"
            " currency, idempotency_key, custom_id, created_at, updated_at)"
            " VALUES (?,?,?,?, 'RESERVED', ?,?,?, 'pending', ?, ?)",
            (payment_id, p.id, decision.id, p.mandate_id, cart.total_minor, cart.currency,
             idempotency_key(p.id), now, now),
        )
        entry = self.ledger.append_in(  # b
            conn,
            LedgerEntryType.PAYMENT_STARTED,
            payment_id,
            {
                "mandate_id": p.mandate_id,
                "proposal_id": p.id,
                "decision_id": decision.id,
                "amount_minor": cart.total_minor,
                "currency": cart.currency,
                "idempotency_key": idempotency_key(p.id),
            },
        )
        conn.execute(  # c
            "UPDATE payments SET custom_id = ?, ledger_seq = ? WHERE id = ?",
            (make_custom_id(p.id, entry.seq), entry.seq, payment_id),
        )
        return payment_id

    # ---- step d: talk to PayPal (never called while holding a lock) -----------------------
    def execute(self, payment_id: str) -> PaymentRecord:
        row = self._row(payment_id)
        if row is None:
            raise GawahError(404, "PAYMENT_NOT_FOUND", f"No payment {payment_id}")
        if row["status"] != "RESERVED":
            return self.to_record(row)
        counter = {"n": 0}

        def remember_order(order: dict[str, Any]) -> None:
            with write_tx(self.path) as conn:
                conn.execute(
                    "UPDATE payments SET paypal_order_id = ?, paypal_status = ?, updated_at = ?"
                    " WHERE id = ?",
                    (order.get("id"), order.get("status"), format_ts(self.clock()), payment_id),
                )

        def attempt() -> dict[str, Any]:
            c = self.config
            return pay_order(
                self.client(),
                row["amount_minor"],
                row["currency"],
                row["custom_id"],
                f"Gawah purchase {row['proposal_id']}"[:120],
                row["idempotency_key"],
                card=c.card(),
                buyer_email=c.buyer_email,
                buyer_password=c.buyer_password,
                on_order=remember_order,
            )

        try:
            order = self._retry(attempt, counter)
        except Exception as exc:  # noqa: BLE001  every failure must end in a defined state
            return self._after_failure(payment_id, exc, counter["n"])
        return self._apply_order(payment_id, order, counter["n"])

    def _after_failure(self, payment_id: str, exc: Exception, attempts: int) -> PaymentRecord:
        reason = describe_error(exc)
        row = self._row(payment_id)
        if row is not None and row["paypal_order_id"]:
            # The order exists: never guess. Ask PayPal what really happened.
            return self.reconcile_one(payment_id, attempts=attempts, hint=reason)
        if isinstance(exc, httpx.TransportError):
            # No answer at all and no order id: PayPal may or may not have created (and even
            # captured) the order. Do not release the budget on a guess; hold it for review.
            return self._mark_unknown(
                payment_id,
                f"outcome unknown after {attempts} attempts ({reason}); check PayPal for this "
                "payment's custom_id before releasing",
            )
        return self._finalize_failed(payment_id, reason, attempts)

    def _mark_unknown(self, payment_id: str, why: str) -> PaymentRecord:
        with write_tx(self.path) as conn:
            conn.execute(
                "UPDATE payments SET failure_reason = ?, paypal_status = 'UNKNOWN', updated_at = ?"
                " WHERE id = ? AND status = 'RESERVED'",
                (why[:300], format_ts(self.clock()), payment_id),
            )
        return self.get(payment_id)

    def _apply_order(self, payment_id: str, order: dict[str, Any], attempts: int) -> PaymentRecord:
        outcome, capture_id, reason = classify_order(order)
        if outcome == "PAID":
            return self._finalize_paid(payment_id, order, capture_id, attempts)
        if outcome == "FAILED":
            return self._finalize_failed(payment_id, reason or "payment failed", attempts, order)
        with write_tx(self.path) as conn:  # PENDING: keep RESERVED, record what we know
            conn.execute(
                "UPDATE payments SET paypal_order_id = ?, paypal_status = ?, attempts = ?,"
                " updated_at = ? WHERE id = ?",
                (order.get("id"), PENDING, attempts, format_ts(self.clock()), payment_id),
            )
        return self.get(payment_id)

    # ---- steps e/f -----------------------------------------------------------------------
    def _finalize_paid(
        self, payment_id: str, order: dict[str, Any], capture_id: str | None, attempts: int
    ) -> PaymentRecord:
        with write_tx(self.path) as conn:
            row = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
            if row["status"] != "RESERVED":  # someone else already finalised: nothing to do
                return self.to_record(row)
            conn.execute(
                "UPDATE payments SET status = 'PAID', paypal_order_id = ?, paypal_capture_id = ?,"
                " paypal_status = 'COMPLETED', failure_reason = NULL, attempts = ?, updated_at = ?"
                " WHERE id = ?",
                (order.get("id"), capture_id or capture_id_of(order), attempts,
                 format_ts(self.clock()), payment_id),
            )
            self.mandates.settle_in(conn, row["mandate_id"], row["proposal_id"], "committed")
            self.ledger.append_in(
                conn,
                LedgerEntryType.PAYMENT_COMPLETED,
                payment_id,
                {
                    "mandate_id": row["mandate_id"],
                    "proposal_id": row["proposal_id"],
                    "amount_minor": row["amount_minor"],
                    "currency": row["currency"],
                    "paypal_order_id": order.get("id"),
                    "paypal_capture_id": capture_id or capture_id_of(order),
                    "custom_id": row["custom_id"],
                },
            )
        return self.get(payment_id)

    def _finalize_failed(
        self, payment_id: str, reason: str, attempts: int, order: dict[str, Any] | None = None
    ) -> PaymentRecord:
        with write_tx(self.path) as conn:
            row = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
            if row["status"] != "RESERVED":
                return self.to_record(row)
            conn.execute(
                "UPDATE payments SET status = 'FAILED', failure_reason = ?, attempts = ?,"
                " paypal_status = COALESCE(?, paypal_status), updated_at = ? WHERE id = ?",
                (reason[:300], attempts, (order or {}).get("status"), format_ts(self.clock()),
                 payment_id),
            )
            self.mandates.settle_in(conn, row["mandate_id"], row["proposal_id"], "released")
            self.ledger.append_in(
                conn,
                LedgerEntryType.PAYMENT_FAILED,
                payment_id,
                {
                    "mandate_id": row["mandate_id"],
                    "proposal_id": row["proposal_id"],
                    "amount_minor": row["amount_minor"],
                    "currency": row["currency"],
                    "reason": reason[:300],
                    "paypal_order_id": row["paypal_order_id"],
                    "custom_id": row["custom_id"],
                },
            )
        return self.get(payment_id)

    # ---- reconciliation ------------------------------------------------------------------
    def reconcile_one(
        self, payment_id: str, attempts: int = 0, hint: str | None = None
    ) -> PaymentRecord:
        """Finalise a RESERVED payment from PayPal's real state (or fail it if no order exists)."""
        row = self._row(payment_id)
        if row is None or row["status"] != "RESERVED":
            return self.get(payment_id)
        if not row["paypal_order_id"]:
            if row["paypal_status"] == "UNKNOWN":  # a call may have reached PayPal: needs a human
                return self.to_record(row)
            return self._finalize_failed(payment_id, "interrupted before order creation", attempts)
        counter = {"n": 0}
        try:
            order = self._retry(lambda: self.client().get_order(row["paypal_order_id"]), counter)
        except Exception as exc:  # noqa: BLE001  PayPal unreachable: stay RESERVED, retry later
            return self._mark_unknown(payment_id, f"outcome unknown: {hint or describe_error(exc)}")
        return self._apply_order(payment_id, order, attempts)

    def reconcile(self) -> dict[str, int]:
        """Startup recovery: settle every payment a crash left in RESERVED."""
        conn = connect(self.path)
        try:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM payments WHERE status = 'RESERVED' ORDER BY rowid")]
        finally:
            conn.close()
        counts = {"checked": len(ids), "paid": 0, "failed": 0, "still_reserved": 0}
        for pid in ids:
            status = self.reconcile_one(pid).status
            key = {"PAID": "paid", "FAILED": "failed"}.get(status.value, "still_reserved")
            counts[key] += 1
        if ids:
            log.info("payment reconciliation: %s", counts)
        return counts

    # ---- refunds -------------------------------------------------------------------------
    def refund(self, payment_id: str, reason: str | None = None) -> PaymentRecord:
        """Full refund of a PAID payment. Idempotent; budget spent is returned to the mandate."""
        row = self._row(payment_id)
        if row is None:
            raise GawahError(404, "PAYMENT_NOT_FOUND", f"No payment {payment_id}")
        if row["status"] == "REFUNDED":
            return self.to_record(row)
        if row["status"] != "PAID":
            raise GawahError(
                409,
                "PAYMENT_NOT_REFUNDABLE",
                f"Payment {payment_id} is {row['status']}; only PAID payments can be refunded",
            )
        counter = {"n": 0}
        note = (reason or "Refund issued by Gawah")[:200]
        try:
            refund = self._retry(
                lambda: self.client().refund_capture(
                    row["paypal_capture_id"], row["amount_minor"], row["currency"], note,
                    f"{row['idempotency_key']}-refund",
                ),
                counter,
            )
        except Exception as exc:
            raise GawahError(502, "REFUND_FAILED", describe_error(exc)) from exc
        if refund.get("status") not in ("COMPLETED", "PENDING"):
            raise GawahError(502, "REFUND_FAILED", f"PayPal refund status {refund.get('status')}")
        with write_tx(self.path) as conn:
            cur = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
            if cur["status"] == "REFUNDED":  # a concurrent identical request got here first
                return self.to_record(cur)
            conn.execute(
                "UPDATE payments SET status = 'REFUNDED', refund_id = ?, paypal_status = ?,"
                " updated_at = ? WHERE id = ?",
                (refund.get("id"), f"REFUND_{refund.get('status')}", format_ts(self.clock()),
                 payment_id),
            )
            self.mandates.refund_in(conn, cur["mandate_id"], cur["amount_minor"])
            self.ledger.append_in(
                conn,
                LedgerEntryType.REFUNDED,
                payment_id,
                {
                    "mandate_id": cur["mandate_id"],
                    "proposal_id": cur["proposal_id"],
                    "amount_minor": cur["amount_minor"],
                    "currency": cur["currency"],
                    "paypal_refund_id": refund.get("id"),
                    "paypal_capture_id": cur["paypal_capture_id"],
                    "reason": note,
                },
            )
        return self.get(payment_id)
