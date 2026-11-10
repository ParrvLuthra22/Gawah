"""End-to-end proof: shopper agent -> signed proposal -> engine Guardian -> payment -> ledger.

    make e2e                                   # starts the engine and shopper, runs this, stops them
    uv run python -m integration.e2e           # against an engine (8000) and shopper (8001) you started

Needs SYSTEM1_KEY and SYSTEM3_KEY in the environment (the same values the engine uses), and
MERCHANT_SIGNING_KEY_PATH equal to the shopper's, because the demo merchant's public key is not
exposed by any shopper endpoint: this script imports `DemoMerchantAdapter` to get it (see
docs/INTEGRATION_ISSUES.md). ENGINE_URL / SHOPPER_URL override the default localhost ports.

What it does, in order:
  1. registers the demo merchant's public key with the engine (SYSTEM1_KEY);
  2. creates a mandate (SYSTEM3_KEY): $80, one category, one merchant, short expiry;
  3. happy path: the shopper picks a product and signs a cart, the proposal goes to the engine,
     and the engine must ALLOW and pay (status PAID, ledger PAYMENT_COMPLETED), then evidence verifies;
  4. attacks: the four fixtures in system1/shopper/security/attack_simulation.py plus Guardian
     cases (over budget, wrong merchant, wrong category, expired mandate, reused mandate, replayed
     cart). Each gets a fresh mandate, so one result never depends on another;
  5. verifies the whole ledger chain and prints one table.

Exit code: 0 all expectations met; 1 an attack was ALLOWED that should have been blocked, or any
other expectation failed; 2 the script could not run (engine down, unexpected HTTP error).

Honesty labels: attacker proposals are SIMULATED (built here, from System 1's attack fixtures,
because the shopper's attack runner does not produce a proposal). The LLM is SIMULATED unless the
shopper runs with a real Groq key. PayPal is SIMULATED when the engine reports env "test" and a
real sandbox call otherwise. Against a MOCK engine nothing can be proven (it returns fixtures and
blocks every merchant but demo-shoe-store), so the run only checks wire formats and exits 0.
"""

import argparse
import asyncio
import os
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from gawah_contract import Product as ContractProduct
from gawah_contract import ProposalResult, PurchaseProposal
from shopper.config import Settings
from shopper.merchant.demo_merchant import DemoMerchantAdapter, MerchantProduct

from integration import scenarios as sc

HAPPY_LEDGER_ORDER = ("MANDATE_CREATED", "PROPOSAL_RECEIVED", "DECISION_MADE",
                      "PAYMENT_STARTED", "PAYMENT_COMPLETED")



class E2EError(RuntimeError):
    """The flow could not run (not a failed expectation)."""


@dataclass
class Row:
    scenario: str
    shopper: str
    decision: str
    reasons: list[str]
    payment: str
    expect: str  # "ALLOW" or "BLOCK"
    need: frozenset[str]
    verdict: str  # PASS, FAIL (...), or "n/a (mock)"
    allowed_attack: bool = False


@dataclass
class Report:
    mock: bool = False
    env: str = ""
    rows: list[Row] = field(default_factory=list)
    checks: list[tuple[str, bool, str]] = field(default_factory=list)  # name, ok, detail
    known_issues: list[str] = field(default_factory=list)  # do not affect the exit code
    ledger: str = ""

    @property
    def allowed_attacks(self) -> list[Row]:
        return [r for r in self.rows if r.allowed_attack]

    @property
    def failures(self) -> list[str]:
        out = [f"{r.scenario}: {r.verdict}" for r in self.rows if r.verdict.startswith("FAIL")]
        out += [f"check {n}: {d}" for n, ok, d in self.checks if not ok]
        return out

    @property
    def exit_code(self) -> int:
        return 0 if self.mock or not self.failures else 1


def judge(expect: str, need: frozenset[str], decision: str, reasons: list[str]) -> str:
    """PASS or a FAIL message. A BLOCK expectation also fails on a BLOCK for the wrong reasons."""
    if expect == "ALLOW":
        return "PASS" if decision == "ALLOW" else f"FAIL: expected ALLOW, got {decision}"
    if decision == "ALLOW":
        return "FAIL: ALLOWED, should be blocked"
    missing = need - set(reasons)
    return "PASS" if not missing else f"FAIL: blocked without {sorted(missing)}"


def _clip(text: str, n: int = 300) -> str:
    return text if len(text) <= n else text[:n] + "..."


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Runner:
    def __init__(
        self,
        engine: httpx.AsyncClient,
        shopper: httpx.AsyncClient,
        *,
        s1_key: str,
        s3_key: str,
        merchant: DemoMerchantAdapter,
        expiry_s: int = 3,
        out: Callable[[str], None] = print,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.engine, self.shopper = engine, shopper
        self._keys = {1: s1_key, 3: s3_key}
        self.merchant, self.expiry_s, self.out, self._sleep = merchant, expiry_s, out, sleep
        self.report = Report()
        self.run_id = uuid.uuid4().hex[:8]

    # ---- plumbing -------------------------------------------------------------------------
    async def api(self, system: int, method: str, path: str, body: Any = None,
                  ok: tuple[int, ...] = (200, 201)) -> Any:
        try:
            r = await self.engine.request(method, path, json=body,
                                          headers={"X-Gawah-Key": self._keys[system]})
        except httpx.HTTPError as exc:
            raise E2EError(f"engine unreachable on {method} {path}: {type(exc).__name__}") from exc
        if r.status_code not in ok:
            raise E2EError(f"engine {method} {path} -> {r.status_code}: {_clip(r.text)}")
        return r.json()

    async def shop(self, path: str, body: dict[str, Any]) -> httpx.Response:
        try:
            return await self.shopper.post(path, json=body)
        except httpx.HTTPError as exc:
            raise E2EError(f"shopper unreachable on {path}: {type(exc).__name__}") from exc

    def check(self, name: str, ok: bool, detail: str) -> None:
        self.report.checks.append((name, ok, detail))
        self.out(f"  [{'ok' if ok else 'FAIL'}] {name}: {detail}")

    # ---- building blocks ------------------------------------------------------------------
    async def new_mandate(self, tag: str, *, category: str = sc.CATEGORY,
                          merchants: tuple[str, ...] = (sc.MERCHANT_ID,),
                          budget: int = sc.BUDGET_MINOR, ttl_s: int = 600) -> str:
        m = await self.api(3, "POST", "/v1/mandates", {
            "user_id": f"e2e-{self.run_id}-{tag}", "category": category,
            "item_description": sc.DESCRIPTION, "max_amount_minor": budget,
            "currency": sc.CURRENCY, "allowed_merchants": list(merchants),
            "expires_at": _iso(datetime.now(UTC) + timedelta(seconds=ttl_s)),
        })
        return m["id"]

    async def shopper_proposal(self, mandate_id: str, quantity: int = 1,
                               raw: bool = False) -> dict[str, Any]:
        """The honest path: the shopper's own checkout endpoint builds and signs the proposal.

        WORKAROUND (docs/INTEGRATION_ISSUES.md #5): the shopper sets `product.id` to its internal
        product id, but the Guardian (and the contract fixtures) expect the cart line's sku there,
        so every unmodified shopper proposal is blocked as PRICE_MISMATCH. Unless `raw`, the id is
        replaced by the cart sku here. Nothing else is touched and the cart signature is unaffected
        (only the cart is signed)."""
        r = await self.shop("/shopping/demo/checkout", {
            "message": sc.SHOPPER_MESSAGE, "mandate_id": mandate_id,
            "agent_id": "e2e-shopper", "quantity": quantity})
        if r.status_code != 200:
            raise E2EError(f"shopper checkout -> {r.status_code}: {_clip(r.text)}")
        body = r.json()
        if not (body.get("purchase_ready") and body.get("proposal")):
            raise E2EError(f"shopper made no proposal: {_clip(str(body.get('explanation')))}")
        proposal = body["proposal"]
        if not raw:
            proposal["product"]["id"] = proposal["signed_cart"]["line_items"][0]["sku"]
        return proposal

    def crafted_proposal(self, mandate_id: str, product: MerchantProduct, rationale: str,
                         quantity: int = 1) -> dict[str, Any]:
        """SIMULATED compromised agent: a cart really signed by the demo merchant's key (so the
        signature is valid) for a product the mandate does not cover."""
        cart = self.merchant.sign_cart(product, quantity)
        return PurchaseProposal(
            id=f"prop_{uuid.uuid4().hex}", mandate_id=mandate_id, agent_id="e2e-compromised-agent",
            merchant_id=product.merchant_id,
            product=ContractProduct(id=product.sku, title=product.title, url=product.url),
            quantity=quantity, unit_price_minor=product.unit_price_minor, currency=product.currency,
            rationale=rationale, signed_cart=cart, created_at=datetime.now(UTC),
        ).model_dump(mode="json")

    async def submit(self, proposal: dict[str, Any]) -> dict[str, Any]:
        body = await self.api(1, "POST", "/v1/proposals", proposal)
        ProposalResult.model_validate(body)  # the engine's answer matches the contract
        return body

    def pay_label(self, body: dict[str, Any]) -> str:
        pay = body.get("payment")
        if not pay:
            return "none"
        where = "mock" if self.report.mock else ("SIMULATED PayPal" if self.report.env == "test"
                                                  else "PayPal sandbox")
        return f"{pay['status']} {pay['id'][:18]}.. {pay['amount_minor']}c ({where})"

    def record(self, scenario: str, shopper: str, body: dict[str, Any], expect: str,
               need: set[str] = frozenset(), is_attack: bool = True) -> Row:  # type: ignore[assignment]
        decision = body["decision"]["verdict"]
        reasons = list(body["decision"]["reasons"])
        verdict = "n/a (mock)" if self.report.mock else judge(expect, frozenset(need), decision, reasons)
        row = Row(scenario, shopper, decision, reasons, self.pay_label(body), expect,
                  frozenset(need), verdict,
                  allowed_attack=is_attack and expect == "BLOCK" and decision == "ALLOW"
                  and not self.report.mock)
        self.report.rows.append(row)
        self.out(f"  {scenario:34} {decision:6} {','.join(reasons) or '-':44} {row.verdict}")
        return row

    # ---- the steps ------------------------------------------------------------------------
    async def run(self) -> Report:
        rep = self.report
        health = await self._health()
        rep.mock, rep.env = bool(health.get("mock")), str(health.get("env", ""))
        mode = ("MOCK engine: fixtures only, wire-format check, nothing is proven" if rep.mock
                else f"real engine, env={rep.env or 'dev'}, "
                + ("SIMULATED PayPal" if rep.env == "test" else "PayPal sandbox"))
        self.out(f"engine: {mode}\n")

        self.out("1. register the demo merchant key")
        await self.register_merchant()
        self.out("2. create the mandate")
        mandate_a = await self.new_mandate("happy")
        self.out(f"  {mandate_a}: ${sc.BUDGET_MINOR // 100} {sc.CURRENCY}, {sc.CATEGORY}, "
                 f"{sc.MERCHANT_ID}, expires in 10 minutes")
        # created now, checked last: the only way to test expiry is to let it expire
        expiring = await self.new_mandate("expiring", ttl_s=self.expiry_s)
        expiry_at = asyncio.get_running_loop().time() + self.expiry_s + 1.5

        self.out("3. happy path: shopper -> proposal -> Guardian -> payment")
        happy_proposal = await self.happy_path(mandate_a)

        self.out("4. attacks and Guardian cases (a fresh mandate for each)")
        await self.attacks()
        await self.guardian_cases(mandate_a, happy_proposal)
        await self._sleep(max(0.0, expiry_at - asyncio.get_running_loop().time()))
        body = await self.submit(await self.shopper_proposal(expiring))
        self.record("guardian: expired mandate", "proposal prepared", body, "BLOCK", {"MANDATE_EXPIRED"})

        self.out("5. ledger")
        await self.verify_ledger()
        return rep

    async def _health(self) -> dict[str, Any]:
        try:
            r = await self.engine.get("/health")
        except httpx.HTTPError as exc:
            raise E2EError(f"engine unreachable: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise E2EError(f"engine /health -> {r.status_code}")
        return r.json()

    async def register_merchant(self) -> None:
        key = self.merchant.merchant_key(sc.MERCHANT_ID)
        try:
            await self.api(1, "POST", "/v1/merchants", key.model_dump(mode="json"))
        except E2EError as exc:
            if "CONFLICT" in str(exc):
                raise E2EError(
                    "the engine already has a different key for demo-sports (is its database "
                    "left over from another run? make e2e uses a fresh one): " + str(exc)) from exc
            raise
        self.out(f"  {sc.MERCHANT_ID} key {key.key_id} registered (public key only)")

    async def happy_path(self, mandate_id: str) -> dict[str, Any]:
        agent = await self.shop("/shopping/agent", {"message": sc.SHOPPER_MESSAGE})
        if agent.status_code != 200:
            raise E2EError(f"shopper /shopping/agent -> {agent.status_code}: {_clip(agent.text)}")
        chosen = (agent.json().get("selected_product") or {}).get("id")
        self.out(f"  agent selected: {chosen}")
        await self.known_issue_probe(mandate_id)
        proposal = await self.shopper_proposal(mandate_id)
        body = await self.submit(proposal)
        row = self.record("happy path", "proposal prepared", body, "ALLOW", is_attack=False)
        if self.report.mock:
            return proposal
        expected_minor = sc.merchant_catalog()[sc.HAPPY_PRODUCT_ID].unit_price_minor
        pay = body.get("payment") or {}
        self.check("agent chose the catalog's product", chosen == sc.HAPPY_PRODUCT_ID, str(chosen))
        self.check("payment is PAID for the cart total",
                   pay.get("status") == "PAID" and pay.get("amount_minor") == expected_minor,
                   f"{pay.get('status')} {pay.get('amount_minor')} (cart {expected_minor})")
        if row.verdict == "PASS" and pay:
            entries = await self.api(3, "GET", f"/v1/ledger?mandate_id={mandate_id}")
            types = [e["type"] for e in entries]
            it = iter(types)
            in_order = all(t in it for t in HAPPY_LEDGER_ORDER)  # subsequence test
            self.check("ledger order (write first, pay second)", in_order, " > ".join(types))
            budget = await self.api(3, "GET", f"/v1/mandates/{mandate_id}/budget")
            self.check("budget spent equals payment", budget["spent_minor"] == expected_minor,
                       f"spent {budget['spent_minor']}c, remaining {budget['remaining_minor']}c")
            ev = await self.api(3, "GET", f"/v1/evidence/{pay['id']}")
            v = ev["verification"]
            self.check("evidence bundle verifies", bool(v["ok"]),
                       ", ".join(f"{c['name']}={'ok' if c['ok'] else 'BAD'}" for c in v["checks"]))
        return proposal

    async def known_issue_probe(self, mandate_id: str) -> None:
        """Dry run (no payment, no ledger write) of the shopper's UNMODIFIED proposal."""
        if self.report.mock:
            return
        raw = await self.shopper_proposal(mandate_id, raw=True)
        dec = await self.api(3, "POST", "/v1/guardian/check", raw)
        if dec["verdict"] == "ALLOW":
            self.out("  shopper's unmodified proposal passes the Guardian (issue #5 is fixed)")
            return
        msg = (f"#5 the shopper's unmodified proposal is {dec['verdict']} "
               f"{','.join(dec['reasons'])} (product.id is not the cart sku); worked around here")
        self.report.known_issues.append(msg)
        self.out(f"  [KNOWN ISSUE] {msg}")

    async def shopper_attack_outcome(self, scenario_id: str) -> str:
        r = await self.shop("/shopping/test/attacks/run", {"scenario_id": scenario_id})
        if r.status_code == 200:
            return r.json()["observed_outcome"]
        if r.status_code == 404:  # ATTACK_SIMULATION_ENABLED is off on that shopper
            from shopper.security.attack_simulation import run_attack

            return (await run_attack(scenario_id)).observed_outcome + " (in-process)"
        raise E2EError(f"shopper attack runner -> {r.status_code}: {_clip(r.text)}")

    async def attacks(self) -> None:
        spoof_text = sc.attack_description("authority_spoof")
        # (scenario id, extra mandate args, product, rationale, expectation)
        cases = [
            ("over_budget", sc.attack_product("over_budget"), sc.attack_description("over_budget"),
             "BLOCK", {"OVER_BUDGET"}),
            ("wrong_merchant", sc.attack_product("wrong_merchant"),
             sc.attack_description("wrong_merchant"), "BLOCK", {"MERCHANT_NOT_ALLOWED"}),
            ("substitution", sc.attack_product("substitution"),
             sc.attack_description("substitution"), "BLOCK", {"CATEGORY_MISMATCH"}),
            # A compliant cart: the injected text lives only in the rationale, which the Guardian
            # never reads, so the purchase is correctly ALLOWED and the injection changed nothing.
            ("authority_spoof", sc.attack_product("authority_spoof"), spoof_text, "ALLOW", set()),
        ]
        for sid, product, rationale, expect, need in cases:
            outcome = await self.shopper_attack_outcome(sid)
            mandate = await self.new_mandate(sid)
            body = await self.submit(self.crafted_proposal(mandate, product, rationale))
            self.record(f"attack: {sid}", f"{outcome}; crafted", body, expect, need,
                              is_attack=expect == "BLOCK")
            if sid == "authority_spoof" and not self.report.mock:
                echoed = spoof_text[:30].casefold() in body["decision"]["explanation"].casefold()
                self.check("injected text had no effect on the decision", not echoed,
                           "explanation does not repeat it" if not echoed else "REPEATED")
        # The same injection used as cover for a real violation must still be blocked.
        mandate = await self.new_mandate("spoof_over_budget")
        body = await self.submit(self.crafted_proposal(
            mandate, sc.attack_product("over_budget"), spoof_text))
        self.record("attack: authority_spoof + $120", "crafted", body, "BLOCK", {"OVER_BUDGET"})

    async def guardian_cases(self, mandate_a: str, happy_proposal: dict[str, Any]) -> None:
        # over budget: the real shopper, quantity 2 ($119.98 against $80)
        m = await self.new_mandate("over_budget")
        body = await self.submit(await self.shopper_proposal(m, quantity=2))
        self.record("guardian: over budget (2 pairs)", "proposal prepared", body, "BLOCK",
                    {"OVER_BUDGET"})
        # wrong merchant: a valid demo-sports cart against a mandate for another store
        m = await self.new_mandate("wrong_merchant", merchants=("demo-shoe-store",))
        body = await self.submit(await self.shopper_proposal(m))
        self.record("guardian: wrong merchant", "proposal prepared", body, "BLOCK",
                    {"MERCHANT_NOT_ALLOWED"})
        # wrong category: a real electronics title from System 1's catalog, sold by the allowed store
        m = await self.new_mandate("wrong_category")
        earbuds = sc.catalog_product("tech-001")
        body = await self.submit(self.crafted_proposal(m, earbuds, "Better value than shoes."))
        self.record("guardian: wrong category", "crafted", body, "BLOCK", {"CATEGORY_MISMATCH"})
        # reused mandate: it already paid for one pair, a second cart no longer fits
        body = await self.submit(await self.shopper_proposal(mandate_a))
        self.record("guardian: reused mandate (spent)", "proposal prepared", body, "BLOCK",
                    {"OVER_BUDGET"})
        # replayed cart: the paid cart again, new proposal id, fresh mandate
        m = await self.new_mandate("replay")
        replay = {**happy_proposal, "id": f"prop_{uuid.uuid4().hex}", "mandate_id": m}
        body = await self.submit(replay)
        self.record("guardian: replayed cart", "replayed", body, "BLOCK", {"DUPLICATE"})

    async def verify_ledger(self) -> None:
        res = await self.api(3, "GET", "/v1/ledger/verify")
        if self.report.mock:
            self.report.ledger = "mock fixture (not verified)"
            self.out(f"  {self.report.ledger}")
            return
        self.report.ledger = f"ok={res['ok']}, {res['checked']} entries checked"
        self.check("ledger chain verifies (hashes, links, signatures)", bool(res["ok"]),
                   f"{res['checked']} entries" + ("" if res["ok"] else f": {res.get('reason')}"))


def print_table(report: Report, out: Callable[[str], None] = print) -> None:
    head = ("scenario", "shopper outcome", "Guardian decision", "reason codes", "payment", "result")
    rows = [(r.scenario, r.shopper, r.decision, ", ".join(r.reasons) or "-", r.payment, r.verdict)
            for r in report.rows]
    widths = [max(len(str(x)) for x in col) for col in zip(head, *rows, strict=True)]
    line = "  ".join(f"{{:<{w}}}" for w in widths)
    out("\n" + line.format(*head))
    out(line.format(*("-" * w for w in widths)))
    for row in rows:
        out(line.format(*row))
    out("")
    if report.mock:
        out("MOCK engine: results are fixtures, not Guardian decisions. Use the default `make e2e`.")
    elif report.env == "test":
        out("Payments above are SIMULATED (the engine's fake PayPal); attacker proposals are SIMULATED.")
    for issue in report.known_issues:
        out(f"KNOWN ISSUE {issue}")
    for row in report.allowed_attacks:
        out(f"!! ATTACK ALLOWED: {row.scenario}")
    out(f"ledger: {report.ledger or 'not checked'}")
    if report.mock:
        out("RESULT: WIRE FORMATS OK (mock engine: nothing about the Guardian was proven)")
    else:
        out("RESULT: " + ("PASS" if report.exit_code == 0 else "FAIL"))
    for failure in report.failures:
        out(f"  - {failure}")


def _keys() -> tuple[str, str]:
    s1, s3 = os.environ.get("SYSTEM1_KEY", ""), os.environ.get("SYSTEM3_KEY", "")
    if not (s1 and s3):
        raise E2EError("set SYSTEM1_KEY and SYSTEM3_KEY (the values the engine was started with)")
    return s1, s3


async def amain(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--engine-url", default=os.environ.get("ENGINE_URL", "http://localhost:8000"))
    ap.add_argument("--shopper-url", default=os.environ.get("SHOPPER_URL", "http://localhost:8001"))
    ap.add_argument("--expiry-seconds", type=int, default=3)
    args = ap.parse_args(argv)
    try:
        s1, s3 = _keys()
        # Settings needs a Groq key to construct; this adapter only signs and never calls Groq.
        merchant = DemoMerchantAdapter(Settings(groq_api_key=os.environ.get("GROQ_API_KEY") or "unused"))
        async with httpx.AsyncClient(base_url=args.engine_url.rstrip("/"), timeout=60) as engine, \
                httpx.AsyncClient(base_url=args.shopper_url.rstrip("/"), timeout=90) as shopper:
            runner = Runner(engine, shopper, s1_key=s1, s3_key=s3, merchant=merchant,
                            expiry_s=args.expiry_seconds)
            report = await runner.run()
    except E2EError as exc:
        print(f"\ne2e could not run: {exc}", file=sys.stderr)
        return 2
    print_table(report)
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
