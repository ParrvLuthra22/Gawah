"""Tamper demo: show that editing a ledger entry directly in SQLite is detected.

    uv run python scripts/tamper_demo.py

Uses a throwaway database and throwaway keys in a temp directory; touches nothing else.
Steps: create a mandate and a 5-entry ledger, verify (ok), try a plain UPDATE (the trigger
blocks it), drop the trigger and rewrite entry 3 with raw SQL, verify again (broken at seq 3).
"""

import json
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

from gawah_contract import LedgerEntryType, MandateCreate
from gawah_engine.services import build_services

TAMPER_SEQ = 3
COLOR = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def _bar(title: str) -> None:
    print("\n" + _c("1;36", f"== {title} " + "=" * max(0, 70 - len(title))))


def _show_verify(result: dict) -> None:
    if result["ok"]:
        print(_c("1;32", f"  RESULT: OK    chain intact, {result['checked']} entries verified"))
    else:
        print(_c("1;31", f"  RESULT: BROKEN at seq {result['first_broken_seq']}"))
        print(_c("31", f"  reason: {result['reason']}"))
        print(f"  entries checked before stopping: {result['checked']}")


def _show_entries(entries) -> None:
    for e in entries:
        print(f"  seq {e.seq}  {e.type.value:<18} {e.subject_id:<18} hash {e.hash[:12]}…")


def run_demo(workdir: Path, verbose: bool = True) -> tuple[dict, dict]:
    """Run the whole demo in `workdir`; return (verify_before, verify_after)."""
    out = print if verbose else (lambda *a, **k: None)
    svc = build_services(f"sqlite:///{workdir / 'demo.db'}", str(workdir / "keys"), str(workdir / "keys"))
    db = str(workdir / "demo.db")

    _bar("1. Build a signed ledger")
    mandate = svc.mandates.create(
        MandateCreate(
            user_id="user_demo",
            category="footwear",
            item_description="Black running shoes, size 9, under $80",
            max_amount_minor=8000,
            currency="USD",
            allowed_merchants=["demo-shoe-store"],
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
    )
    mid = mandate.id
    for type_, subject, data in (
        (LedgerEntryType.PROPOSAL_RECEIVED, "prop_01", {"mandate_id": mid, "amount_minor": 12000}),
        (
            LedgerEntryType.DECISION_MADE,
            "dec_01",
            {"mandate_id": mid, "proposal_id": "prop_01", "verdict": "BLOCK", "reasons": ["OVER_BUDGET"]},
        ),
        (LedgerEntryType.PAYMENT_STARTED, "pay_01", {"mandate_id": mid, "amount_minor": 6500}),
        (LedgerEntryType.PAYMENT_COMPLETED, "pay_01", {"mandate_id": mid, "amount_minor": 6500}),
    ):
        svc.ledger.append(type_, subject, data)
    entries = svc.ledger.list()
    if verbose:
        print(f"  mandate {mid} created and signed (key {mandate.key_id})")
        _show_entries(entries)

    _bar("2. Verify the untouched ledger")
    before = svc.ledger.verify()
    if verbose:
        _show_verify(before)

    _bar(f"3. Attacker edits entry {TAMPER_SEQ} directly in the database")
    original = dict(entries[TAMPER_SEQ - 1].data)
    forged = {**original, "verdict": "ALLOW", "reasons": []}
    out(f"  original data : {json.dumps(original, sort_keys=True)}")
    out(f"  forged data   : {json.dumps(forged, sort_keys=True)}")
    sql = "UPDATE ledger SET data = ? WHERE seq = ?"
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        out(f"\n  a) plain UPDATE:  {sql}")
        try:
            conn.execute(sql, (json.dumps(forged), TAMPER_SEQ))
            out(_c("1;31", "     !! the UPDATE went through (this should never happen)"))
        except sqlite3.DatabaseError as e:
            out(_c("32", f"     blocked by the database trigger: {e}"))
        out("\n  b) attacker drops the trigger, then retries:")
        out("     DROP TRIGGER ledger_no_update")
        conn.execute("DROP TRIGGER ledger_no_update")
        out(f"     {sql}")
        conn.execute(sql, (json.dumps(forged), TAMPER_SEQ))
        out(_c("33", "     edit succeeded at the SQL level. Can anyone tell?"))
    finally:
        conn.close()

    _bar("4. Verify again")
    after = svc.ledger.verify()
    if verbose:
        _show_verify(after)
        print()
    return before, after


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="gawah-tamper-") as tmp:
        before, after = run_demo(Path(tmp))
    detected = before["ok"] and not after["ok"] and after["first_broken_seq"] == TAMPER_SEQ
    print(_c("1;32" if detected else "1;31", "Tampering detected." if detected else "DEMO FAILED"))
    return 0 if detected else 1


if __name__ == "__main__":
    sys.exit(main())
