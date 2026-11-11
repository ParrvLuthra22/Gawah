"""Operator refund via the engine, Counsel recommended CONCEDE_REMEDY.

Refunds the LAST PAID payment (the wrong-size CONCEDE case that `integration/e2e.py --phase variant`
seeds) through the engine's refund route (POST /v1/payments/{id}/refund, SYSTEM3_KEY), PayPal SANDBOX
only. This is an OPERATOR action: Counsel's demo disputes are SIMULATED_FIXTURE records and Counsel
cannot refund them itself; the engine's own route does the refund. Prints, masked: the refund result,
the mandate's budget spent before and after (a full refund restores it), the new ledger entry and
the ledger chain verification. Exit 0 only if all of that holds.

    ENGINE_URL=http://127.0.0.1:8000 SYSTEM3_KEY=... python scripts/refund_demo.py [--payment-id ID]
"""

import argparse
import os
import sys
from urllib.parse import urlparse

import httpx

LOCAL = {"127.0.0.1", "localhost", "::1"}
REASON = "Operator refund via the engine, Counsel recommended CONCEDE_REMEDY (size 11 shipped, size 12 authorised)"


def mask(v: str | None) -> str:
    return "-" if not v else (v if len(v) <= 8 else f"{v[:4]}...{v[-3:]}")


def main(argv: list[str] | None = None, env: dict[str, str] | None = None,
         client: httpx.Client | None = None, out=print) -> int:
    env = dict(os.environ) if env is None else env
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--engine-url", default=env.get("ENGINE_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--payment-id", help="default: the last PAID payment")
    args = ap.parse_args(argv or [])
    key = env.get("SYSTEM3_KEY", "")
    if not key:
        print("set SYSTEM3_KEY (the value the engine was started with)", file=sys.stderr)
        return 2
    if client is None:
        if urlparse(args.engine_url).hostname not in LOCAL and env.get("E2E_CONFIRM_REMOTE") != "1":
            print(f"refusing a non-local engine ({urlparse(args.engine_url).hostname}) without "
                  "E2E_CONFIRM_REMOTE=1: this refunds a payment", file=sys.stderr)
            return 2
        client = httpx.Client(base_url=args.engine_url, timeout=60)
    h = {"X-Gawah-Key": key}

    health = client.get("/health").json()
    if health.get("paypal") != "sandbox":
        print("not a PayPal sandbox engine; refusing", file=sys.stderr)
        return 2
    out_header = "operator refund via the engine, Counsel recommended CONCEDE_REMEDY"
    pays = client.get("/v1/payments", headers=h).json()
    paid = [p for p in pays if p["status"] == "PAID"]
    pid = args.payment_id or (paid[-1]["id"] if paid else None)
    if pid is None:
        print("no PAID payment to refund", file=sys.stderr)
        return 2
    bundle = client.get(f"/v1/evidence/{pid}", headers=h).json()
    capture = bundle["payment"].get("paypal_capture_id") or ""
    # The engine only runs its fake PayPal with GAWAH_ENV=test; its ids are short (CAP0001), a real
    # sandbox capture id is 17 characters. A heuristic, used only to LABEL the output as simulated.
    fake = bool(health.get("mock") or health.get("env") == "test" or (capture and len(capture) < 12))
    label = "FAKE PayPal (simulated)" if fake else "PayPal SANDBOX"
    out(f"{out_header}  [{label}, env={health.get('env')}]")
    out("(the label is the operator's premise: the deterministic Counsel rule supports CONCEDE_REMEDY for "
    "a size mismatch, but the demo's model output can differ; see docs/INTEGRATION_ISSUES.md #16)")
    mandate_id = bundle["mandate"]["id"]
    line = bundle["signed_cart"]["line_items"][0]
    out(f"payment {mask(pid)}: {bundle['payment']['amount_minor']}c, cart '{line['title']}' / {line['variant']}, "
        f"mandate says: {bundle['mandate']['item_description']}")

    before = client.get(f"/v1/mandates/{mandate_id}/budget", headers=h).json()
    n_before = len(client.get("/v1/ledger", params={"subject_id": pid}, headers=h).json())
    r = client.post(f"/v1/payments/{pid}/refund", json={"reason": REASON}, headers=h)
    if r.status_code != 200:
        print(f"refund failed: HTTP {r.status_code} {r.json().get('error', {}).get('code')}", file=sys.stderr)
        return 1
    refund = r.json()
    after = client.get(f"/v1/mandates/{mandate_id}/budget", headers=h).json()
    entries = client.get("/v1/ledger", params={"subject_id": pid}, headers=h).json()
    new = entries[n_before:]
    verify = client.get("/v1/ledger/verify", headers=h).json()

    out(f"refund result : status {refund['status']}, amount {refund['amount_minor']}c {refund['currency']}, "
        f"capture {mask(refund.get('paypal_capture_id'))}")
    out(f"budget spent  : before {before['spent_minor']}c -> after {after['spent_minor']}c "
        f"(max {after['max_amount_minor']}c, reserved {after['reserved_minor']}c)")
    out(f"new ledger    : {[e['type'] for e in new]} (seq {[e['seq'] for e in new]}); "
        f"refund id {mask((new[-1]['data'] if new else {}).get('paypal_refund_id'))}")
    out(f"ledger chain  : ok={verify['ok']}, {verify['checked']} entries checked")

    problems = []
    if refund["status"] != "REFUNDED":
        problems.append("payment is not REFUNDED")
    if after["spent_minor"] != before["spent_minor"] - refund["amount_minor"]:
        problems.append("budget spent was not restored by the refunded amount")
    if [e["type"] for e in new] != ["REFUNDED"]:
        problems.append("expected exactly one new REFUNDED ledger entry")
    if not verify["ok"]:
        problems.append("ledger chain does not verify")
    for p in problems:
        out(f"PROBLEM: {p}")
    out("RESULT: " + ("PASS" if not problems else "FAIL"))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
