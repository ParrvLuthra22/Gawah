"""After the rehearsal: what Counsel's two demo cases recommend, whether a refund can be prepared for
the CONCEDE case through Counsel's resolution API, and the engine's payment-ordering check.

Talks only to the local rehearsal stack (refuses anything else). Reads SYSTEM3_KEY from the
environment, prints no keys, and masks every PayPal id. The disputes are SIMULATED_FIXTURE records.
    ENGINE_URL=... COUNSEL_URL=... SYSTEM3_KEY=... python scripts/rehearse_probe.py
"""

import os
import sys
from urllib.parse import urlparse

import httpx

LOCAL = {"127.0.0.1", "localhost"}


def mask(v: str | None) -> str:
    return "-" if not v else (v if len(v) <= 8 else f"{v[:4]}...{v[-3:]}")


def main() -> int:
    engine_url = os.environ.get("ENGINE_URL", "http://127.0.0.1:8000")
    counsel_url = os.environ.get("COUNSEL_URL", "http://127.0.0.1:8002")
    for u in (engine_url, counsel_url):
        if urlparse(u).hostname not in LOCAL:
            print(f"refusing non-local target {u}: this probe is for the local sandbox rehearsal", file=sys.stderr)
            return 2
    h = {"X-Gawah-Key": os.environ["SYSTEM3_KEY"]}
    eng = httpx.Client(base_url=engine_url, headers=h, timeout=60)
    cou = httpx.Client(base_url=counsel_url + "/api", headers=h, timeout=120)

    print("A. /v1/payments ordering (Counsel's demo takes payments[-2:])")
    pays = eng.get("/v1/payments").json()
    times = [p["created_at"] for p in pays]
    ordered = times == sorted(times)
    print(f"   {len(pays)} payments, created_at ascending: {ordered}")
    paid = [p for p in pays if p["status"] == "PAID"]
    for label, p in (("second-to-last PAID (DEFEND)", paid[-2]), ("last PAID (CONCEDE)", paid[-1])):
        print(f"   {label}: {mask(p['id'])} {p['amount_minor']}c order {mask(p['paypal_order_id'])}")

    print("B. Counsel's recommendation and reasons")
    cases = {}
    for label, p in (("DEFEND", paid[-2]), ("CONCEDE", paid[-1])):
        a = cou.get(f"/counsel/{p['id']}").json()
        out = a.get("outcome") or {}
        cases[label] = (p, a)
        print(f"   {label}: dispute reason {a.get('dispute_reason')}, status {a.get('analysis_status')}, "
              f"recommendation {out.get('recommendation')} (confidence {out.get('confidence')})")
        print(f"      summary: {out.get('executive_summary')}")
        for r in out.get("reasoning", []):
            print(f"      - {r['id']}: {r['text']}")

    print("B2. why not decisive: model output vs. verified evidence")
    for label, (p, a) in cases.items():
        out = a.get("outcome") or {}
        print(f"   {label}: outcome keys {sorted(out)}; conflict: {out.get('conflict')}")
        print(f"      analysis keys {sorted(k for k in a if k != 'outcome')}")
        pack = cou.get(f"/evidence/{p['id']}").json()
        print("      evidence: " + "; ".join(
            f"{e['type']}={e.get('source_type')}/{e.get('verification_status')}" for e in pack.get("evidence", [])))
        vs = pack.get("verification_summary") or {}
        print("      verification_summary: " + ", ".join(
            f"{k}={(v or {}).get('status')}" for k, v in vs.items()))
        print(f"      contradictions: {[c['code'] for c in pack.get('contradictions', [])]}")

    print("B3. DEFEND: each condition of system3 counsel_case._verified_authorization, re-evaluated here")
    pack = cou.get(f"/evidence/{paid[-2]['id']}").json()
    ev = pack["evidence"]
    by = {e["type"]: e for e in ev if e["type"] != "ledger_entry"}
    ok = lambda e: e.get("source_type") == "REAL_RECORD" and e.get("verification_status") == "VERIFIED"
    m, pay, d = by["mandate"], by["payment"], by["decision"]
    approvals = [e for e in ev if e["type"] == "ledger_entry" and ok(e)
                 and e["payload"].get("type") == "DECISION_MADE"
                 and e["payload"].get("subject_id") == d["source_record_id"]]
    conds = {
        "mandate trusted": ok(m), "signed_cart trusted": ok(by["signed_cart"]), "payment trusted": ok(pay),
        "decision is REAL_RECORD": d.get("source_type") == "REAL_RECORD",
        "decision verdict ALLOW": d["payload"].get("verdict") == "ALLOW",
        "payment status PAID": pay["payload"].get("status") == "PAID",
        "payment.decision_id == decision id": pay["payload"].get("decision_id") == d["source_record_id"],
        "payment.proposal_id == decision.proposal_id": pay["payload"].get("proposal_id") == d["payload"].get("proposal_id"),
        "decision.mandate_id == mandate id": d["payload"].get("mandate_id") == m["source_record_id"],
        "exactly one trusted DECISION_MADE ledger entry": len(approvals) == 1,
    }
    for name, value in conds.items():
        print(f"   {'ok ' if value else 'NO '} {name}")
    print(f"   decision verification: {d.get('verification_status')} ({(d.get('verification') or {}).get('detail')})")
    print(f"   payload keys: decision {sorted(d['payload'])}; payment {sorted(pay['payload'])}")
    print(f"   ids: decision.id={d['id']} decision.source_record_id={d['source_record_id']}")

    print("C. refund for the CONCEDE case through Counsel's resolution API")
    p, a = cases["CONCEDE"]
    disputes = cou.get("/disputes", params={"search": p["id"]}).json()
    items = disputes.get("items") or disputes.get("data") or []
    dispute_id = next((d["id"] for d in items), None)
    if dispute_id is None:
        dispute_id = next((d["id"] for d in (cou.get("/disputes").json().get("items") or [])
                           if d.get("payment_id") == p["id"]), None)
    print(f"   dispute id {dispute_id}")
    opts = cou.get(f"/disputes/{dispute_id}/resolution").json()
    print(f"   options: {[o['action'] for o in opts.get('options', [])]}; message: {opts.get('message')}")
    wr = cou.get("/resolution/workflows")
    wf = wr.json()
    print(f"   GET /resolution/workflows -> HTTP {wr.status_code}: {str(wf)[:240] if 'error' in wf else 'ok'}")
    ar = cou.get("/resolution/actor")
    print(f"   GET /resolution/actor -> HTTP {ar.status_code}: {ar.text[:200]}")
    rows = wf.get("items") or wf.get("data") or wf.get("rows") or []
    print(f"   workflow list: keys {sorted(wf)}, {len(rows)} rows")
    wid = None
    for w in rows:
        detail = cou.get(f"/resolution/workflows/{w['id']}").json()
        if detail.get("dispute_id") == dispute_id or (detail.get("dispute") or {}).get("id") == dispute_id:
            wid = w["id"]
            break
    print(f"   workflow id for dispute {dispute_id}: {wid}")
    if wid:
        print(f"   workflow status {detail.get('status')}, revision {detail.get('revision')}")
        r = cou.post(f"/resolution/workflows/{wid}/review",
                     json={"revision": detail["revision"], "command": "prepare", "action": "refund",
                           "text": "SIMULATED sandbox refund for a wrong-size shipment"})
        print(f"   prepare refund -> HTTP {r.status_code}: {r.text[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
