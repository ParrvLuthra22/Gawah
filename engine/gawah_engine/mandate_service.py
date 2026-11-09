"""Mandates: create/sign, read, revoke, expire, verify, and budget accounting.

A mandate's signature covers its immutable terms (id, user_id, category, item_description,
max_amount_minor, currency, allowed_merchants, issued_at, expires_at). `status`, `key_id` and
`signing_alg` are not signed: status changes over the mandate's life (revocation/expiry are
recorded in the DB and the ledger), and the key id travels next to the signature.

Budget: remaining = max_amount_minor - spent_minor - reserved_minor. `reserve` / `release` /
`commit` each run in one BEGIN IMMEDIATE transaction, and the table has a CHECK constraint
(spent + reserved <= max), so concurrent proposals can never overspend. Reserving twice with
the same idempotency key (per mandate) never double counts.

Money is integer minor units everywhere.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from gawah_contract import (
    LedgerEntryType,
    Mandate,
    MandateCreate,
    MandateStatus,
    ReasonCode,
)

from .db import connect, write_tx
from .errors import GawahError
from .ledger_service import LedgerService, format_ts, utcnow
from .signing import ALG, Signer

SIGNED_FIELDS = (
    "id",
    "user_id",
    "category",
    "item_description",
    "max_amount_minor",
    "currency",
    "allowed_merchants",
    "issued_at",
    "expires_at",
)


class MandateNotFound(GawahError):
    def __init__(self, mandate_id: str):
        super().__init__(404, "MANDATE_NOT_FOUND", f"No mandate {mandate_id}")


class MandateNotActive(GawahError):
    def __init__(self, mandate_id: str, status: str):
        super().__init__(409, "MANDATE_NOT_ACTIVE", f"Mandate {mandate_id} is {status}")


class BudgetExceeded(GawahError):
    def __init__(self, mandate_id: str, amount: int, remaining: int):
        super().__init__(
            409,
            "OVER_BUDGET",
            f"Mandate {mandate_id} has {remaining} left; cannot reserve {amount}",
        )


class IdempotencyConflict(GawahError):
    def __init__(self, key: str, detail: str):
        super().__init__(409, "IDEMPOTENCY_CONFLICT", f"Idempotency key {key!r}: {detail}")


@dataclass(frozen=True)
class Budget:
    max_minor: int
    spent_minor: int
    reserved_minor: int

    @property
    def remaining_minor(self) -> int:
        return self.max_minor - self.spent_minor - self.reserved_minor


@dataclass(frozen=True)
class Reservation:
    mandate_id: str
    idempotency_key: str
    amount_minor: int
    state: str  # reserved | committed | released
    replayed: bool = False


# ---- pure policy helpers (reused by the Guardian). Each returns a ReasonCode or None. ------


def check_category(mandate: Mandate, category: str) -> ReasonCode | None:
    ok = mandate.category.strip().casefold() == category.strip().casefold()
    return None if ok else ReasonCode.CATEGORY_MISMATCH


def check_merchant(mandate: Mandate, merchant_id: str) -> ReasonCode | None:
    return None if merchant_id in mandate.allowed_merchants else ReasonCode.MERCHANT_NOT_ALLOWED


def check_currency(mandate: Mandate, currency: str) -> ReasonCode | None:
    return None if mandate.currency == currency else ReasonCode.CURRENCY_MISMATCH


def check_expiry(mandate: Mandate, now: datetime) -> ReasonCode | None:
    expired = mandate.status == MandateStatus.EXPIRED or mandate.expires_at <= now
    return ReasonCode.MANDATE_EXPIRED if expired else None


def check_revocation(mandate: Mandate) -> ReasonCode | None:
    return ReasonCode.MANDATE_REVOKED if mandate.status == MandateStatus.REVOKED else None


def check_spend_limit(budget: Budget, amount_minor: int) -> ReasonCode | None:
    return None if 0 < amount_minor <= budget.remaining_minor else ReasonCode.OVER_BUDGET


def _norm_dt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise GawahError(422, "VALIDATION_ERROR", "expires_at must include a timezone")
    return dt.astimezone(UTC).replace(microsecond=0)


def _row_to_mandate(row: sqlite3.Row) -> Mandate:
    return Mandate(
        id=row["id"],
        user_id=row["user_id"],
        category=row["category"],
        item_description=row["item_description"],
        max_amount_minor=row["max_amount_minor"],
        currency=row["currency"],
        allowed_merchants=json.loads(row["allowed_merchants"]),
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
        status=row["status"],
        signature=row["signature"],
        signing_alg=row["signing_alg"],
        key_id=row["key_id"],
    )


def signable(mandate: Mandate) -> dict:
    """The exact dict that is signed for a mandate."""
    d = mandate.model_dump(mode="json")
    d["issued_at"] = format_ts(mandate.issued_at)
    d["expires_at"] = format_ts(mandate.expires_at)
    return {k: d[k] for k in SIGNED_FIELDS}


class MandateService:
    def __init__(self, path: str, signer: Signer, ledger: LedgerService, clock=utcnow):
        self.path, self.signer, self.ledger, self.clock = path, signer, ledger, clock

    # -- helpers -----------------------------------------------------------------------
    def _expire_due(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE mandates SET status = 'expired' WHERE status = 'active' AND expires_at <= ?",
            (format_ts(self.clock()),),
        )

    def _row(self, conn: sqlite3.Connection, mandate_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM mandates WHERE id = ?", (mandate_id,)).fetchone()
        if row is None:
            raise MandateNotFound(mandate_id)
        return row

    # -- create / read / revoke --------------------------------------------------------
    def create(self, body: MandateCreate) -> Mandate:
        currency = body.currency.upper()
        if not currency.isalpha() or not currency.isascii():
            raise GawahError(422, "VALIDATION_ERROR", "currency must be a 3-letter ISO code")
        merchants = [m.strip() for m in body.allowed_merchants]
        if not merchants or any(not m for m in merchants):
            raise GawahError(422, "VALIDATION_ERROR", "allowed_merchants must be non-empty ids")
        if body.max_amount_minor <= 0:
            raise GawahError(422, "VALIDATION_ERROR", "max_amount_minor must be positive")
        for name in ("user_id", "category", "item_description"):
            if not getattr(body, name).strip():
                raise GawahError(422, "VALIDATION_ERROR", f"{name} must not be empty")
        now = self.clock()
        expires_at = _norm_dt(body.expires_at)
        if expires_at <= now:
            raise GawahError(422, "VALIDATION_ERROR", "expires_at must be in the future")

        mandate_id = f"man_{uuid4().hex[:12]}"
        unsigned = Mandate(
            id=mandate_id,
            user_id=body.user_id,
            category=body.category.strip(),
            item_description=body.item_description,
            max_amount_minor=body.max_amount_minor,
            currency=currency,
            allowed_merchants=list(dict.fromkeys(merchants)),
            issued_at=now,
            expires_at=expires_at,
            status=MandateStatus.ACTIVE,
            signature="",
            signing_alg=ALG,
            key_id="",
        )
        signature, key_id = self.signer.sign(signable(unsigned))
        mandate = unsigned.model_copy(update={"signature": signature, "key_id": key_id})
        with write_tx(self.path) as conn:
            conn.execute(
                "INSERT INTO mandates (id, user_id, category, item_description, max_amount_minor,"
                " currency, allowed_merchants, issued_at, expires_at, status, signature,"
                " signing_alg, key_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    mandate.id,
                    mandate.user_id,
                    mandate.category,
                    mandate.item_description,
                    mandate.max_amount_minor,
                    mandate.currency,
                    json.dumps(mandate.allowed_merchants),
                    format_ts(mandate.issued_at),
                    format_ts(mandate.expires_at),
                    mandate.status.value,
                    signature,
                    ALG,
                    key_id,
                ),
            )
            self.ledger.append_in(
                conn,
                LedgerEntryType.MANDATE_CREATED,
                mandate.id,
                {
                    "mandate_id": mandate.id,
                    "user_id": mandate.user_id,
                    "category": mandate.category,
                    "max_amount_minor": mandate.max_amount_minor,
                    "currency": mandate.currency,
                    "allowed_merchants": mandate.allowed_merchants,
                    "expires_at": format_ts(mandate.expires_at),
                    "key_id": key_id,
                },
            )
        return mandate

    def get(self, mandate_id: str) -> Mandate:
        with write_tx(self.path) as conn:  # write tx: persists auto-expiry
            self._expire_due(conn)
            return _row_to_mandate(self._row(conn, mandate_id))

    def list(self) -> list[Mandate]:
        with write_tx(self.path) as conn:
            self._expire_due(conn)
            rows = conn.execute("SELECT * FROM mandates ORDER BY issued_at, id").fetchall()
        return [_row_to_mandate(r) for r in rows]

    def revoke(self, mandate_id: str) -> Mandate:
        """Revoke an active mandate. Revoking an already-revoked one is a no-op (200)."""
        with write_tx(self.path) as conn:
            self._expire_due(conn)
            row = self._row(conn, mandate_id)
            if row["status"] == "expired":
                raise MandateNotActive(mandate_id, "expired")
            if row["status"] == "active":
                conn.execute("UPDATE mandates SET status = 'revoked' WHERE id = ?", (mandate_id,))
                self.ledger.append_in(
                    conn,
                    LedgerEntryType.MANDATE_REVOKED,
                    mandate_id,
                    {"mandate_id": mandate_id, "user_id": row["user_id"]},
                )
            return _row_to_mandate(self._row(conn, mandate_id))

    # -- verification ------------------------------------------------------------------
    def verify_mandate(self, mandate: Mandate) -> tuple[bool, str | None]:
        """(ok, reason). Checks signature, then revocation, then expiry.

        Signature is checked on the object passed in; revocation and expiry use the stored
        state, so a stale or forged copy that claims status=active is still rejected.
        """
        if not self.signer.verify(signable(mandate), mandate.signature, mandate.key_id):
            return False, "MANDATE_SIGNATURE_INVALID"
        try:
            current = self.get(mandate.id)
        except MandateNotFound:
            return False, "MANDATE_NOT_FOUND"
        return self._status_reason(current)

    def _status_reason(self, current: Mandate) -> tuple[bool, str | None]:
        if reason := check_revocation(current):
            return False, reason.value
        if reason := check_expiry(current, self.clock()):
            return False, reason.value
        return True, None

    def get_in(self, conn: sqlite3.Connection, mandate_id: str) -> Mandate:
        """`get` inside a write transaction the caller already holds."""
        self._expire_due(conn)
        return _row_to_mandate(self._row(conn, mandate_id))

    def verify_in(self, conn: sqlite3.Connection, mandate: Mandate) -> tuple[bool, str | None]:
        """`verify_mandate` inside a write transaction the caller already holds."""
        if not self.signer.verify(signable(mandate), mandate.signature, mandate.key_id):
            return False, "MANDATE_SIGNATURE_INVALID"
        try:
            current = self.get_in(conn, mandate.id)
        except MandateNotFound:
            return False, "MANDATE_NOT_FOUND"
        return self._status_reason(current)

    # -- budget ------------------------------------------------------------------------
    def budget(self, mandate_id: str) -> Budget:
        conn = connect(self.path)
        try:
            return self.budget_in(conn, mandate_id)
        finally:
            conn.close()

    def budget_in(self, conn: sqlite3.Connection, mandate_id: str) -> Budget:
        row = self._row(conn, mandate_id)
        return Budget(row["max_amount_minor"], row["spent_minor"], row["reserved_minor"])

    def reserve(self, mandate_id: str, amount_minor: int, idempotency_key: str) -> Reservation:
        with write_tx(self.path) as conn:
            return self.reserve_in(conn, mandate_id, amount_minor, idempotency_key)

    def reserve_in(
        self, conn: sqlite3.Connection, mandate_id: str, amount_minor: int, idempotency_key: str
    ) -> Reservation:
        """Reserve inside a write transaction the caller already holds (BEGIN IMMEDIATE)."""
        if amount_minor <= 0:
            raise GawahError(422, "VALIDATION_ERROR", "amount_minor must be positive")
        self._expire_due(conn)
        row = self._row(conn, mandate_id)
        existing = conn.execute(
            "SELECT * FROM reservations WHERE mandate_id = ? AND idempotency_key = ?",
            (mandate_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing["amount_minor"] != amount_minor:
                raise IdempotencyConflict(idempotency_key, "reused with a different amount")
            return Reservation(
                mandate_id, idempotency_key, amount_minor, existing["state"], replayed=True
            )
        if row["status"] != "active":
            raise MandateNotActive(mandate_id, row["status"])
        budget = Budget(row["max_amount_minor"], row["spent_minor"], row["reserved_minor"])
        if check_spend_limit(budget, amount_minor):
            raise BudgetExceeded(mandate_id, amount_minor, budget.remaining_minor)
        conn.execute(
            "INSERT INTO reservations (mandate_id, idempotency_key, amount_minor, state,"
            " created_at) VALUES (?,?,?,'reserved',?)",
            (mandate_id, idempotency_key, amount_minor, format_ts(self.clock())),
        )
        conn.execute(
            "UPDATE mandates SET reserved_minor = reserved_minor + ? WHERE id = ?",
            (amount_minor, mandate_id),
        )
        return Reservation(mandate_id, idempotency_key, amount_minor, "reserved")

    def settle_in(
        self, conn: sqlite3.Connection, mandate_id: str, key: str, target: str
    ) -> Reservation:
        """Move a reservation from 'reserved' to 'committed' or 'released' (idempotent)."""
        res = conn.execute(
            "SELECT * FROM reservations WHERE mandate_id = ? AND idempotency_key = ?",
            (mandate_id, key),
        ).fetchone()
        if res is None:
            raise GawahError(404, "RESERVATION_NOT_FOUND", f"No reservation {key!r}")
        amount = res["amount_minor"]
        if res["state"] == target:
            return Reservation(mandate_id, key, amount, target, replayed=True)
        if res["state"] != "reserved":
            raise IdempotencyConflict(key, f"already {res['state']}, cannot {target}")
        conn.execute(
            "UPDATE reservations SET state = ? WHERE mandate_id = ? AND idempotency_key = ?",
            (target, mandate_id, key),
        )
        if target == "committed":
            conn.execute(
                "UPDATE mandates SET reserved_minor = reserved_minor - ?,"
                " spent_minor = spent_minor + ? WHERE id = ?",
                (amount, amount, mandate_id),
            )
        else:
            conn.execute(
                "UPDATE mandates SET reserved_minor = reserved_minor - ? WHERE id = ?",
                (amount, mandate_id),
            )
        return Reservation(mandate_id, key, amount, target)

    def _settle(self, mandate_id: str, key: str, target: str) -> Reservation:
        with write_tx(self.path) as conn:
            return self.settle_in(conn, mandate_id, key, target)

    def commit(self, mandate_id: str, idempotency_key: str) -> Reservation:
        """The payment succeeded: reserved -> spent. Works even if the mandate was revoked since."""
        return self._settle(mandate_id, idempotency_key, "committed")

    def release(self, mandate_id: str, idempotency_key: str) -> Reservation:
        """The payment did not happen: give the reserved amount back."""
        return self._settle(mandate_id, idempotency_key, "released")

    def refund_in(self, conn: sqlite3.Connection, mandate_id: str, amount_minor: int) -> None:
        """A paid purchase was refunded in full: return its amount to the mandate's budget."""
        cur = conn.execute(
            "UPDATE mandates SET spent_minor = spent_minor - ? WHERE id = ? AND spent_minor >= ?",
            (amount_minor, mandate_id, amount_minor),
        )
        if cur.rowcount != 1:
            raise GawahError(409, "BUDGET_INCONSISTENT", "refund exceeds the spent amount")
