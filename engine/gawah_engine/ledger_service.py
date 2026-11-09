"""Append-only, hash-chained, signed ledger.

Entry hash = sha256(canonical JSON of {seq, ts, type, subject_id, data, prev_hash}); the genesis
`prev_hash` is 64 zeros; `signature` is ES256 over the hash by the ledger key (key id stored per
row, so key rotation never breaks old entries). `seq` assignment and the `prev_hash` lookup
happen inside one `BEGIN IMMEDIATE` transaction, so two writers cannot fork the chain, and
SQLite triggers abort any UPDATE or DELETE on the table.

WRITE-AHEAD RULE FOR PAYMENTS: write first, pay second.
    PAYMENT_STARTED must be committed to the ledger BEFORE any PayPal call is made, and the
    outcome is recorded afterwards as PAYMENT_COMPLETED or PAYMENT_FAILED. Use
    `LedgerService.payment_attempt(...)`: it commits PAYMENT_STARTED before the `with` body
    runs, records PAYMENT_FAILED if the body raises, and PAYMENT_COMPLETED when the body calls
    `attempt.completed(...)`. A PAYMENT_STARTED with no outcome means "crashed mid-payment,
    reconcile with PayPal using custom_id", which is exactly why it is written first.

    with ledger.payment_attempt("pay_01", mandate_id="man_01", data={"custom_id": "gawah:prop_01"}) as a:
        order = paypal.create_and_capture(...)     # only runs after PAYMENT_STARTED is durable
        a.completed({"paypal_order_id": order.id})
"""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from gawah_contract import LedgerEntry, LedgerEntryType
from gawah_contract.hashing import GENESIS_HASH

from .canonical import canonical_json
from .db import connect, write_tx
from .signing import Signer

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def format_ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(TS_FORMAT)


def entry_hash(seq: int, ts: str, type_: str, subject_id: str, data: dict, prev_hash: str) -> str:
    body = {
        "seq": seq,
        "ts": ts,
        "type": type_,
        "subject_id": subject_id,
        "data": data,
        "prev_hash": prev_hash,
    }
    return hashlib.sha256(canonical_json(body)).hexdigest()


def _to_entry(row: sqlite3.Row) -> LedgerEntry:
    return LedgerEntry(
        seq=row["seq"],
        ts=row["ts"],
        type=row["type"],
        subject_id=row["subject_id"],
        data=json.loads(row["data"]),
        prev_hash=row["prev_hash"],
        hash=row["hash"],
        signature=row["signature"],
        key_id=row["key_id"],
    )


class PaymentAttempt:
    def __init__(self, ledger: "LedgerService", payment_id: str, mandate_id: str | None):
        self._ledger, self.payment_id, self.mandate_id = ledger, payment_id, mandate_id
        self.outcome: LedgerEntry | None = None

    def _data(self, extra: dict[str, Any] | None) -> dict[str, Any]:
        data = dict(extra or {})
        if self.mandate_id:
            data.setdefault("mandate_id", self.mandate_id)
        return data

    def completed(self, data: dict[str, Any] | None = None) -> LedgerEntry:
        self.outcome = self._ledger.append(
            LedgerEntryType.PAYMENT_COMPLETED, self.payment_id, self._data(data)
        )
        return self.outcome

    def failed(self, data: dict[str, Any] | None = None) -> LedgerEntry:
        self.outcome = self._ledger.append(
            LedgerEntryType.PAYMENT_FAILED, self.payment_id, self._data(data)
        )
        return self.outcome


class LedgerService:
    def __init__(self, path: str, signer: Signer, clock=utcnow):
        self.path, self.signer, self.clock = path, signer, clock

    # -- writing -----------------------------------------------------------------------
    def append_in(
        self,
        conn: sqlite3.Connection,
        type_: LedgerEntryType | str,
        subject_id: str,
        data: dict[str, Any],
    ) -> LedgerEntry:
        """Append inside a write transaction the caller already opened with BEGIN IMMEDIATE."""
        type_ = LedgerEntryType(type_)
        last = conn.execute("SELECT seq, hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
        seq, prev_hash = (last["seq"] + 1, last["hash"]) if last else (1, GENESIS_HASH)
        ts = format_ts(self.clock())
        data_json = canonical_json(data).decode()  # also rejects floats / non-JSON values
        h = entry_hash(seq, ts, type_.value, subject_id, json.loads(data_json), prev_hash)
        signature, key_id = self.signer.sign(h)
        conn.execute(
            "INSERT INTO ledger (seq, ts, type, subject_id, data, prev_hash, hash, signature,"
            " key_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (seq, ts, type_.value, subject_id, data_json, prev_hash, h, signature, key_id),
        )
        return _to_entry(conn.execute("SELECT * FROM ledger WHERE seq = ?", (seq,)).fetchone())

    def append(
        self, type_: LedgerEntryType | str, subject_id: str, data: dict[str, Any]
    ) -> LedgerEntry:
        """Append one entry in its own committed transaction."""
        with write_tx(self.path) as conn:
            return self.append_in(conn, type_, subject_id, data)

    @contextmanager
    def payment_attempt(
        self,
        payment_id: str,
        *,
        mandate_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> Iterator[PaymentAttempt]:
        """Write-ahead helper. PAYMENT_STARTED is committed before the body runs."""
        attempt = PaymentAttempt(self, payment_id, mandate_id)
        self.append(LedgerEntryType.PAYMENT_STARTED, payment_id, attempt._data(data))
        try:
            yield attempt
        except BaseException as exc:
            if attempt.outcome is None:
                attempt.failed({"error": type(exc).__name__, "message": str(exc)[:500]})
            raise
        if attempt.outcome is None:
            raise RuntimeError(
                "payment_attempt body finished without calling completed() or failed(); "
                "PAYMENT_STARTED stays open for reconciliation"
            )

    # -- reading -----------------------------------------------------------------------
    def list(
        self,
        *,
        mandate_id: str | None = None,
        subject_id: str | None = None,
        type: LedgerEntryType | str | None = None,
        since_seq: int | None = None,
    ) -> list[LedgerEntry]:
        where, args = [], []
        if mandate_id is not None:
            where.append("json_extract(data, '$.mandate_id') = ?")
            args.append(mandate_id)
        if subject_id is not None:
            where.append("subject_id = ?")
            args.append(subject_id)
        if type is not None:
            where.append("type = ?")
            args.append(LedgerEntryType(type).value)
        if since_seq is not None:
            where.append("seq > ?")
            args.append(since_seq)
        sql = "SELECT * FROM ledger" + (" WHERE " + " AND ".join(where) if where else "")
        conn = connect(self.path)
        try:
            return [_to_entry(r) for r in conn.execute(sql + " ORDER BY seq", args)]
        finally:
            conn.close()

    def verify(self) -> dict[str, Any]:
        """Recompute every hash, prev_hash link and signature.

        Returns {ok, checked, first_broken_seq, reason}. `checked` counts rows examined up to
        and including the first broken one.
        """
        conn = connect(self.path)
        try:
            rows = conn.execute("SELECT * FROM ledger ORDER BY seq").fetchall()
        finally:
            conn.close()

        def broken(row: sqlite3.Row, n: int, reason: str) -> dict[str, Any]:
            return {"ok": False, "checked": n, "first_broken_seq": row["seq"], "reason": reason}

        prev = GENESIS_HASH
        for n, row in enumerate(rows, start=1):
            if row["seq"] != n:
                return broken(row, n, f"sequence gap: expected seq {n}, found {row['seq']}")
            if row["prev_hash"] != prev:
                return broken(row, n, "prev_hash does not match the previous entry's hash")
            try:
                data = json.loads(row["data"])
                expected = entry_hash(
                    row["seq"], row["ts"], row["type"], row["subject_id"], data, row["prev_hash"]
                )
            except (ValueError, TypeError):
                return broken(row, n, "entry data is not valid canonical JSON")
            if row["hash"] != expected:
                return broken(row, n, "hash mismatch: entry contents were modified after signing")
            if not self.signer.verify(row["hash"], row["signature"], row["key_id"]):
                return broken(row, n, f"invalid signature for key {row['key_id']}")
            prev = row["hash"]
        return {"ok": True, "checked": len(rows), "first_broken_seq": None, "reason": None}
