"""scripts/counsel_analysis_check.py with fake engine and Counsel transports (no network, no Groq)."""

import importlib.util
from pathlib import Path

import httpx

spec = importlib.util.spec_from_file_location(
    "counsel_analysis_check", Path(__file__).resolve().parents[2] / "scripts" / "counsel_analysis_check.py")
cac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cac)

PAYMENTS = [{"id": f"pay_prop_{n}0000000000", "status": "PAID"} for n in (1, 2, 3)]
RATE = {"analysis_status": "FAILED", "outcome": None,
        "error": {"code": "COUNSEL_RATE_LIMITED", "message": "Groq is rate limited. Try again later."}}
DONE = {"analysis_status": "COMPLETED", "outcome": {"recommendation": "DEFEND"}, "error": None}


def clients(analyses: dict[str, list[dict]], posts: list[str]):
    def engine(request):
        return httpx.Response(200, json=PAYMENTS)

    def counsel(request):
        pid = request.url.path.split("/")[3]
        if request.method == "POST":
            posts.append(pid)
            return httpx.Response(503, json={"error": {"code": "COUNSEL_RATE_LIMITED"}})
        queue = analyses[pid]
        return httpx.Response(200, json=queue.pop(0) if len(queue) > 1 else queue[0])
    return (httpx.Client(base_url="http://e", transport=httpx.MockTransport(engine)),
            httpx.Client(base_url="http://c/api", transport=httpx.MockTransport(counsel)))


def run(analyses):
    posts, lines, sleeps = [], [], []
    eng, cou = clients(analyses, posts)
    ok = cac.check(eng, cou, wait=45, sleep=sleeps.append, out=lines.append)
    return ok, posts, lines, sleeps


def test_all_completed_needs_no_retry():
    ok, posts, _, sleeps = run({PAYMENTS[1]["id"]: [DONE], PAYMENTS[2]["id"]: [DONE]})
    assert ok and not posts and not sleeps


def test_failed_analysis_waits_45s_retriggers_once_and_passes_when_it_recovers():
    ok, posts, lines, sleeps = run({PAYMENTS[1]["id"]: [DONE], PAYMENTS[2]["id"]: [RATE, DONE]})
    assert ok and sleeps == [45] and posts == [PAYMENTS[2]["id"]]
    assert any("COUNSEL_RATE_LIMITED: Groq is rate limited" in line for line in lines)


def test_still_failed_after_one_retry_fails_with_the_reason():
    ok, posts, lines, _ = run({PAYMENTS[1]["id"]: [DONE], PAYMENTS[2]["id"]: [RATE]})
    assert not ok and posts == [PAYMENTS[2]["id"]]
    assert sum("COUNSEL_RATE_LIMITED" in line for line in lines) == 2  # first read and after the retry


def test_refuses_a_non_local_target(monkeypatch):
    monkeypatch.setenv("SYSTEM3_KEY", "x")
    monkeypatch.setenv("COUNSEL_URL", "https://gawah-counsel.onrender.com")
    assert cac.main() == 2
