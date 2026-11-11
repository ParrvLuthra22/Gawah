import importlib.util
from pathlib import Path

DEMO = Path(__file__).resolve().parents[2] / "scripts" / "tamper_demo.py"


def _load():
    spec = importlib.util.spec_from_file_location("tamper_demo", DEMO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_demo_detects_the_edit_at_seq_3(tmp_path):
    before, after = _load().run_demo(tmp_path, verbose=False)
    assert before["ok"] and before["checked"] == 5
    assert not after["ok"] and after["first_broken_seq"] == 3
    assert "hash mismatch" in after["reason"]


def test_demo_script_output_and_exit_code(capsys):
    assert _load().main() == 0
    out = capsys.readouterr().out
    assert "RESULT: OK" in out and "RESULT: BROKEN at seq 3" in out
    assert "blocked by the database trigger" in out


def test_tampering_with_a_copy_is_detected_and_the_original_is_untouched(tmp_path, capsys):
    import shutil
    from datetime import UTC, datetime, timedelta

    from gawah_contract import LedgerEntryType, MandateCreate
    from gawah_engine.services import build_services

    keys = tmp_path / "keys"
    db = tmp_path / "engine.db"
    svc = build_services(f"sqlite:///{db}", str(keys), str(keys))
    mandate = svc.mandates.create(MandateCreate(
        user_id="u", category="footwear", item_description="shoes", max_amount_minor=8000,
        currency="USD", allowed_merchants=["m"], expires_at=datetime.now(UTC) + timedelta(days=1)))
    svc.ledger.append(LedgerEntryType.DECISION_MADE, "dec_1",
                      {"mandate_id": mandate.id, "verdict": "BLOCK", "reasons": ["OVER_BUDGET"]})
    svc.ledger.append(LedgerEntryType.PROPOSAL_RECEIVED, "prop_2", {"mandate_id": mandate.id})
    original_bytes = db.read_bytes()

    assert _load().main(["--copy-of", str(db), "--keys", str(keys)]) == 0
    out = capsys.readouterr().out
    assert "Tampering detected at seq 2" in out and "original database was not touched" in out
    assert db.read_bytes() == original_bytes
    assert svc.ledger.verify()["ok"]
    shutil.rmtree(keys)
