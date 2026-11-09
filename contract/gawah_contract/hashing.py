"""Ledger hash-chain helper shared by the engine and anyone verifying a chain."""

import hashlib
import json
from typing import Any

from .models import LedgerEntry

GENESIS_HASH = "0" * 64


def compute_entry_hash(entry: LedgerEntry | dict[str, Any]) -> str:
    """sha256 over canonical JSON of seq, ts, type, subject_id, data and prev_hash."""
    d = entry.model_dump(mode="json") if isinstance(entry, LedgerEntry) else entry
    body = {k: d[k] for k in ("seq", "ts", "type", "subject_id", "data", "prev_hash")}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def verify_chain(entries: list[LedgerEntry]) -> tuple[bool, int | None]:
    """Return (ok, first_broken_seq)."""
    prev = GENESIS_HASH
    for e in entries:
        if e.prev_hash != prev or e.hash != compute_entry_hash(e):
            return False, e.seq
        prev = e.hash
    return True, None
