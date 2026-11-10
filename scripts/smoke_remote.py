"""Smoke test for a deployed (or local) Gawah: engine, Counsel and the web app.

    ENGINE_URL=https://gawah-engine.onrender.com \
    COUNSEL_URL=https://gawah-counsel.onrender.com \
    WEB_URL=https://gawah-web.onrender.com \
    SYSTEM1_KEY=... SYSTEM3_KEY=... uv run python scripts/smoke_remote.py

Base URLs come from --engine/--counsel/--web or ENGINE_URL/COUNSEL_URL/WEB_URL. Keys come from the
environment only (SYSTEM1_KEY for the shopper routes, SYSTEM3_KEY for the dashboard routes and for
Counsel; COUNSEL_KEY overrides the Counsel key). Keys are never printed.

Steps (each PASS/FAIL with timings; the first call to each service gets a long timeout because a
sleeping free Render instance needs up to about a minute to wake):

  engine  health, create mandate, register merchant, propose a $65 purchase, ledger verify, evidence
  counsel health, Counsel assembles and reads that payment's evidence
  web     root returns 200, a request through the web proxy reaches Counsel (health, evidence)

The $65 purchase is a REAL PayPal SANDBOX payment when the engine runs with GAWAH_ENV=demo and
PayPal configured (then it must end PAID). On any other engine the payment steps are SKIPPED if the
engine did not pay. The script registers its own throwaway merchant ("smoke-<id>") so it never
collides with the engine's demo merchant. Exit code 1 if any step failed; the failing hop (engine,
counsel, web, web->counsel) is named.
"""

import argparse
import os
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from gawah_contract import MerchantKey
from gawah_contract.carts import generate_keypair, public_key_pem, sign_cart

RUN = uuid.uuid4().hex[:8]
MERCHANT_ID, KEY_ID = f"smoke-{RUN}", f"smoke-{RUN}-key-1"


def iso(delta: timedelta = timedelta(0)) -> str:
    return (datetime.now(UTC) + delta).isoformat().replace("+00:00", "Z")


def mask(value: str | None) -> str:
    return "-" if not value else (value[:6] + "…" if len(value) > 8 else "***")


class Smoke:
    def __init__(self, engine: str, counsel: str, web: str, k1: str, k3: str, kc: str,
                 warm_timeout: float):
        self.engine, self.counsel, self.web = (u.rstrip("/") for u in (engine, counsel, web))
        self.k1, self.k3, self.kc = k1, k3, kc
        self.warm_timeout = warm_timeout
        self.results: list[tuple[str, str, bool | None, float, str]] = []  # hop,name,ok,secs,note
        self.client = httpx.Client(timeout=60, follow_redirects=False)
        self.warm: set[str] = set()
        self.state: dict = {}

    # -- plumbing --------------------------------------------------------------------------
    def req(self, hop: str, method: str, url: str, key: str | None = None, **kw) -> httpx.Response:
        headers = {"X-Gawah-Key": key} if key else {}
        timeout = self.warm_timeout if hop not in self.warm else 60
        r = None
        deadline = time.monotonic() + (self.warm_timeout if hop not in self.warm else 0)
        while True:  # a sleeping instance may refuse or 502 until it is up
            try:
                r = self.client.request(method, url, headers=headers, timeout=timeout, **kw)
                if r.status_code not in (502, 503, 504) or time.monotonic() > deadline:
                    break
            except httpx.TransportError:
                if time.monotonic() > deadline:
                    raise
            time.sleep(3)
        self.warm.add(hop)
        return r

    def step(self, hop: str, name: str, fn) -> bool | None:
        t0 = time.monotonic()
        try:
            ok, note = True, fn() or ""
        except SkipStep as e:
            ok, note = None, str(e)
        except Exception as e:  # noqa: BLE001  a failed assertion or transport error: report, keep going
            ok, note = False, f"{type(e).__name__}: {str(e)[:300]}"
        secs = time.monotonic() - t0
        self.results.append((hop, name, ok, secs, note))
        tag = "SKIP" if ok is None else "PASS" if ok else "FAIL"
        print(f"[{tag}] {hop:<7} {name:<34} {secs:6.2f}s  {note}", flush=True)
        return ok

    @staticmethod
    def expect(r: httpx.Response, *codes: int) -> dict:
        if r.status_code not in codes:
            raise AssertionError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else {}

    # -- engine ----------------------------------------------------------------------------
    def engine_health(self):
        h = self.expect(self.req("engine", "GET", f"{self.engine}/health"), 200)
        self.state["env"], self.state["mock"] = h.get("env"), h.get("mock")
        if h.get("mock"):
            raise AssertionError("engine is in mock mode (GAWAH_MOCK=1); smoke needs the real engine")
        return f"env={h.get('env')} mock={h.get('mock')} paypal={h.get('paypal')} " \
               f"semantic={h.get('semantic')} model={h.get('model')}"

    def create_mandate(self):
        m = self.expect(self.req("engine", "POST", f"{self.engine}/v1/mandates", self.k3, json={
            "user_id": f"smoke-user-{RUN}", "category": "footwear",
            "item_description": "Black running shoes, size 9, under $80",
            "max_amount_minor": 8000, "currency": "USD", "allowed_merchants": [MERCHANT_ID],
            "expires_at": iso(timedelta(days=1))}), 200, 201)
        self.state["mandate"] = m["id"]
        return f"{m['id']} ${m['max_amount_minor'] / 100:.2f} signed {m['signing_alg']}"

    def register_merchant(self):
        self.state["mkey"] = key = generate_keypair()
        body = MerchantKey(merchant_id=MERCHANT_ID, key_id=KEY_ID, alg="ES256",
                           public_key_pem=public_key_pem(key)).model_dump(mode="json")
        self.expect(self.req("engine", "POST", f"{self.engine}/v1/merchants", self.k1, json=body),
                    200, 201)
        return f"{MERCHANT_ID}/{KEY_ID}"

    def propose(self):
        cart = sign_cart({
            "cart_id": f"cart_{RUN}", "merchant_id": MERCHANT_ID,
            "line_items": [{"sku": "SHOE-BLK-RUN", "title": "Black Runner", "variant": "size 9",
                            "quantity": 1, "unit_price_minor": 6500}],
            "total_minor": 6500, "currency": "USD", "expires_at": iso(timedelta(hours=1)),
            "signature": "", "key_id": ""}, self.state["mkey"], KEY_ID).model_dump(mode="json")
        res = self.expect(self.req("engine", "POST", f"{self.engine}/v1/proposals", self.k1, json={
            "id": f"prop_{RUN}", "mandate_id": self.state["mandate"], "agent_id": "smoke-agent",
            "merchant_id": MERCHANT_ID,
            "product": {"id": "SHOE-BLK-RUN", "title": "Black Runner",
                        "url": "https://demo-shoe-store.example/p/1"},
            "quantity": 1, "unit_price_minor": 6500, "currency": "USD",
            "rationale": "Matches the mandate: black runners, size 9.",
            "signed_cart": cart, "created_at": iso()}), 200, 201)
        d, p = res["decision"], res["payment"]
        if d["verdict"] != "ALLOW":
            raise AssertionError(f"decision {d['verdict']} {d['reasons']}: {d['explanation'][:160]}")
        demo = self.state.get("env") == "demo"
        if p is None or p["status"] != "PAID":
            if demo:
                raise AssertionError(f"expected a PAID sandbox payment, got {p and p['status']} "
                                     f"{p and p.get('failure_reason')}")
            raise SkipStep(f"ALLOW but no PAID payment (engine env={self.state.get('env')}): "
                           "payment, evidence and Counsel steps need one")
        self.state["payment"] = p["id"]
        return (f"ALLOW; payment {p['id']} PAID order={mask(p['paypal_order_id'])} "
                f"capture={mask(p['paypal_capture_id'])} "
                f"({'PayPal SANDBOX' if demo else 'engine env=' + str(self.state.get('env'))})")

    def ledger_verify(self):
        v = self.expect(self.req("engine", "GET", f"{self.engine}/v1/ledger/verify", self.k3), 200)
        if not v["ok"]:
            raise AssertionError(f"ledger broken at seq {v['first_broken_seq']}: {v['reason']}")
        return f"ok, {v['checked']} entries"

    def need_payment(self):
        if "payment" not in self.state:
            raise SkipStep("no payment from the propose step")
        return self.state["payment"]

    def evidence(self):
        pid = self.need_payment()
        e = self.expect(self.req("engine", "GET", f"{self.engine}/v1/evidence/{pid}", self.k3), 200)
        v = e["verification"]
        if not v["ok"]:
            raise AssertionError(f"verification failed: {v['reason']}")
        names = ",".join(c["name"] for c in v.get("checks", []))
        return f"verification.ok, {len(e['ledger_entries'])} entries, checks: {names}"

    # -- counsel ---------------------------------------------------------------------------
    def counsel_health(self):
        h = self.expect(self.req("counsel", "GET", f"{self.counsel}/api/health"), 200)
        return str({k: h[k] for k in list(h)[:4]})

    def counsel_reads_evidence(self):
        pid = self.need_payment()
        a = self.expect(self.req("counsel", "POST", f"{self.counsel}/api/evidence/{pid}/assemble",
                                 self.kc), 200, 201)
        g = self.expect(self.req("counsel", "GET", f"{self.counsel}/api/evidence/{pid}", self.kc), 200)
        return f"assembled and read back ({', '.join(sorted(a)[:4])}…) payment {g.get('payment_id', pid)}"

    # -- web -------------------------------------------------------------------------------
    def web_root(self):
        r = self.req("web", "GET", f"{self.web}/", follow_redirects=True)  # / redirects to the app
        if r.status_code != 200:
            raise AssertionError(f"HTTP {r.status_code}")
        hops = f" after {len(r.history)} redirect(s) to {r.url.path}" if r.history else ""
        return f"200{hops}, {len(r.content)} bytes"

    def web_proxy_health(self):
        r = self.req("web->counsel", "GET", f"{self.web}/api/health")
        h = self.expect(r, 200)
        return f"web proxy reached Counsel: { {k: h[k] for k in list(h)[:3]}!s}"

    def web_proxy_evidence(self):
        pid = self.need_payment()
        self.expect(self.req("web->counsel", "GET", f"{self.web}/api/evidence/{pid}"), 200)
        return "web proxy added its key and Counsel accepted it"

    def run(self) -> int:
        print(f"engine={self.engine}\ncounsel={self.counsel}\nweb={self.web}\nrun={RUN}\n")
        e, c, w, wc = "engine", "counsel", "web", "web->counsel"
        steps = [
            (e, "health", self.engine_health), (e, "create mandate", self.create_mandate),
            (e, "register merchant", self.register_merchant),
            (e, "propose $65 purchase", self.propose), (e, "ledger verify", self.ledger_verify),
            (e, "evidence bundle", self.evidence),
            (c, "health", self.counsel_health),
            (c, "reads payment evidence", self.counsel_reads_evidence),
            (w, "root returns 200", self.web_root),
            (wc, "proxy -> Counsel health", self.web_proxy_health),
            (wc, "proxy -> Counsel evidence", self.web_proxy_evidence),
        ]
        for hop, name, fn in steps:
            self.step(hop, name, fn)
        failed = [(h, n, note) for h, n, ok, _s, note in self.results if ok is False]
        total = sum(s for *_x, s, _n in self.results)
        skipped = sum(1 for r in self.results if r[2] is None)
        print(f"\n{len(self.results) - len(failed) - skipped} passed, {len(failed)} failed, "
              f"{skipped} skipped in {total:.1f}s")
        if failed:
            print("FAILED hops: " + ", ".join(sorted({h for h, *_ in failed})))
            for h, n, note in failed:
                print(f"  - {h}: {n}: {note}")
            return 1
        return 0


class SkipStep(Exception):
    pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--engine", default=os.environ.get("ENGINE_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--counsel", default=os.environ.get("COUNSEL_URL", "http://127.0.0.1:8002"))
    ap.add_argument("--web", default=os.environ.get("WEB_URL", "http://127.0.0.1:3000"))
    ap.add_argument("--warm-timeout", type=float, default=120.0,
                    help="seconds to wait for a sleeping service on its first request")
    a = ap.parse_args()
    k1, k3 = os.environ.get("SYSTEM1_KEY", ""), os.environ.get("SYSTEM3_KEY", "")
    if not (k1 and k3):
        sys.exit("set SYSTEM1_KEY and SYSTEM3_KEY in the environment (the engine's service keys)")
    kc = os.environ.get("COUNSEL_KEY") or k3
    return Smoke(a.engine, a.counsel, a.web, k1, k3, kc, a.warm_timeout).run()


if __name__ == "__main__":
    sys.exit(main())
