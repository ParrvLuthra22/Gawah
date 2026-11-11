"""Rehearsal step 4: are Counsel's two demo analyses actually COMPLETED?

`system3/run_demo_scenarios.py` exits 0 even when an analysis FAILED (usually Groq's rate limit, shown
as COUNSEL_RATE_LIMITED), so the rehearsal asks Counsel itself. For each of the last two PAID payments
(the DEFEND and CONCEDE cases) it reads the analysis status; if any is FAILED (or missing) it prints
the reason, waits (default 45s) and re-triggers the analysis ONCE. Exit 0 only if every analysis is
then not FAILED, 1 otherwise.

Local rehearsal stack only. Reads SYSTEM3_KEY from the environment; prints no keys, masks payment ids.
    ENGINE_URL=... COUNSEL_URL=... SYSTEM3_KEY=... python scripts/counsel_analysis_check.py [--wait 45]
"""

import os
import sys
import time
from collections.abc import Callable
from urllib.parse import urlparse

import httpx

LOCAL = {"127.0.0.1", "localhost"}


def mask(v: str | None) -> str:
    return "-" if not v else (v if len(v) <= 8 else f"{v[:4]}...{v[-3:]}")


def reason(analysis: dict) -> str:
    err = analysis.get("error") or {}
    if err:
        return f"{err.get('code')}: {err.get('message')}"
    return f"status {analysis.get('analysis_status')!r} with no outcome" if not analysis.get("outcome") else "none"


def read(cou: httpx.Client, pay_id: str) -> dict:
    r = cou.get(f"/counsel/{pay_id}")
    if r.status_code == 404:
        return {"analysis_status": "FAILED", "error": {"code": "NO_ANALYSIS", "message": "Counsel has no analysis for this payment"}}
    r.raise_for_status()
    return r.json()


def check(eng: httpx.Client, cou: httpx.Client, wait: float = 45, sleep: Callable[[float], None] = time.sleep,
          out: Callable[[str], None] = print) -> bool:
    paid = [p for p in eng.get("/v1/payments").json() if p["status"] in ("PAID", "REFUNDED")]
    if len(paid) < 2:
        out("fewer than two PAID payments on the engine: nothing for Counsel to analyse")
        return False
    cases = (("DEFEND", paid[-2]["id"]), ("CONCEDE", paid[-1]["id"]))
    failed = []
    for label, pid in cases:
        a = read(cou, pid)
        ok = a.get("analysis_status") != "FAILED"
        out(f"  {label} ({mask(pid)}): analysis {a.get('analysis_status')}"
            + ("" if ok else f" -- {reason(a)}"))
        if not ok:
            failed.append((label, pid))
    if not failed:
        return True
    out(f"  {len(failed)} analysis(es) FAILED; waiting {wait:g}s, then re-triggering each once")
    sleep(wait)
    for label, pid in failed:
        cou.post(f"/counsel/{pid}/analyze", params={"force": "true"})  # a 5xx body is fine: re-read below
    still = []
    for label, pid in failed:
        a = read(cou, pid)
        ok = a.get("analysis_status") != "FAILED"
        out(f"  retry {label} ({mask(pid)}): analysis {a.get('analysis_status')}"
            + ("" if ok else f" -- {reason(a)}"))
        if not ok:
            still.append(label)
    return not still


def main() -> int:
    engine_url = os.environ.get("ENGINE_URL", "http://127.0.0.1:8000")
    counsel_url = os.environ.get("COUNSEL_URL", "http://127.0.0.1:8002")
    for u in (engine_url, counsel_url):
        if urlparse(u).hostname not in LOCAL:
            print(f"refusing non-local target {u}: this check is for the local rehearsal", file=sys.stderr)
            return 2
    wait = float(sys.argv[sys.argv.index("--wait") + 1]) if "--wait" in sys.argv else 45
    h = {"X-Gawah-Key": os.environ["SYSTEM3_KEY"]}
    with httpx.Client(base_url=engine_url, headers=h, timeout=60) as eng, \
            httpx.Client(base_url=counsel_url + "/api", headers=h, timeout=120) as cou:
        return 0 if check(eng, cou, wait) else 1


if __name__ == "__main__":
    raise SystemExit(main())
