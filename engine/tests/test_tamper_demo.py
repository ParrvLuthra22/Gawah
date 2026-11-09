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
