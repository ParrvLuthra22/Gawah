"""The whole chain, in process: shopper (SIMULATED Groq) -> signed proposal -> engine Guardian ->
SIMULATED PayPal -> signed ledger. Same flow as `make e2e`, without processes or ports."""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from gawah_contract.carts import load_public_key, verify_cart

from integration import e2e
from integration import scenarios as sc

from .conftest import S1_KEY, S3_KEY


@asynccontextmanager
async def clients(engine_app, shopper_app):
    async with (
        engine_app.router.lifespan_context(engine_app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=engine_app),
                          base_url="http://engine") as engine,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=shopper_app),
                          base_url="http://shopper", timeout=30) as shopper,
    ):
        yield engine, shopper


def run_flow(engine_app, shopper_app, merchant, expiry_s=2):
    lines: list[str] = []

    async def go():
        async with clients(engine_app, shopper_app) as (engine, shopper):
            runner = e2e.Runner(engine, shopper, s1_key=S1_KEY, s3_key=S3_KEY, merchant=merchant,
                                expiry_s=expiry_s, out=lines.append)
            return await runner.run()

    return asyncio.run(go()), lines


def test_full_chain_allows_the_honest_purchase_and_blocks_every_attack(
        engine_settings, engine_app_ref, stubbed_shopper, merchant):
    report, _ = run_flow(engine_app_ref, stubbed_shopper, merchant)
    rows = {r.scenario: r for r in report.rows}

    happy = rows["happy path"]
    assert happy.decision == "ALLOW" and happy.verdict == "PASS"
    assert happy.payment.startswith("PAID ") and "SIMULATED PayPal" in happy.payment

    assert not report.allowed_attacks, [r.scenario for r in report.allowed_attacks]
    expected_reasons = {
        "attack: over_budget": "OVER_BUDGET",
        "attack: wrong_merchant": "MERCHANT_NOT_ALLOWED",
        "attack: substitution": "CATEGORY_MISMATCH",
        "attack: authority_spoof + $120": "OVER_BUDGET",
        "guardian: over budget (2 pairs)": "OVER_BUDGET",
        "guardian: wrong merchant": "MERCHANT_NOT_ALLOWED",
        "guardian: wrong category": "CATEGORY_MISMATCH",
        "guardian: reused mandate (spent)": "OVER_BUDGET",
        "guardian: replayed cart": "DUPLICATE",
        "guardian: expired mandate": "MANDATE_EXPIRED",
    }
    for name, code in expected_reasons.items():
        assert rows[name].decision == "BLOCK" and code in rows[name].reasons, name
        assert rows[name].payment == "none", name
    # a compliant cart whose rationale carries an injection is allowed, and the injection is inert
    assert rows["attack: authority_spoof"].decision == "ALLOW"
    # every check (ledger order, budget, evidence, chain) passed
    assert report.failures == [], report.failures
    assert report.exit_code == 0
    assert "ok=True" in report.ledger


def test_the_known_shopper_proposal_issue_is_detected_not_hidden(
        engine_settings, engine_app_ref, stubbed_shopper, merchant):
    """INTEGRATION_ISSUES.md #5: the shopper sends product.id != cart sku and the Guardian blocks
    it as PRICE_MISMATCH. The run reports it (and works around it) rather than failing silently."""
    report, lines = run_flow(engine_app_ref, stubbed_shopper, merchant)
    assert any("PRICE_MISMATCH" in issue for issue in report.known_issues)
    assert any("KNOWN ISSUE" in line for line in lines)


def test_shopper_cart_is_signed_by_the_key_the_engine_registered(
        engine_settings, engine_app_ref, stubbed_shopper, merchant):
    async def go():
        async with clients(engine_app_ref, stubbed_shopper) as (_, shopper):
            r = await shopper.post("/shopping/demo/checkout", json={
                "message": sc.SHOPPER_MESSAGE, "mandate_id": "man_x", "agent_id": "a"})
            return r.json()

    body = asyncio.run(go())
    assert body["purchase_ready"] is True
    key = merchant.merchant_key(sc.MERCHANT_ID)
    from gawah_contract import SignedCart

    cart = SignedCart.model_validate(body["proposal"]["signed_cart"])
    assert verify_cart(cart, load_public_key(key.public_key_pem))
    assert cart.line_items[0].unit_price_minor == 5999  # integer cents, never a float


def test_an_allowed_attack_fails_the_run():
    assert e2e.judge("BLOCK", frozenset({"OVER_BUDGET"}), "ALLOW", []) == "FAIL: ALLOWED, should be blocked"
    assert e2e.judge("BLOCK", frozenset({"OVER_BUDGET"}), "BLOCK", ["OVER_BUDGET"]) == "PASS"
    assert e2e.judge("BLOCK", frozenset({"OVER_BUDGET"}), "BLOCK", ["DUPLICATE"]).startswith("FAIL")
    assert e2e.judge("ALLOW", frozenset(), "BLOCK", ["OVER_BUDGET"]).startswith("FAIL")

    report = e2e.Report()
    report.rows.append(e2e.Row("attack: x", "-", "ALLOW", [], "PAID", "BLOCK", frozenset(),
                               "FAIL: ALLOWED, should be blocked", allowed_attack=True))
    assert report.exit_code == 1 and report.allowed_attacks


def test_output_never_contains_keys_or_card_numbers(
        engine_settings, engine_app_ref, stubbed_shopper, merchant):
    report, lines = run_flow(engine_app_ref, stubbed_shopper, merchant)
    printed: list[str] = []
    e2e.print_table(report, printed.append)
    text = "\n".join(lines + printed)
    for secret in (S1_KEY, S3_KEY, "4111111111111111", "PRIVATE KEY"):
        assert secret not in text


def test_mock_engine_is_labelled_and_proves_nothing(
        engine_settings, engine_app_ref, stubbed_shopper, merchant, monkeypatch):
    monkeypatch.setattr(engine_settings, "gawah_mock", True)
    report, _ = run_flow(engine_app_ref, stubbed_shopper, merchant)
    assert report.mock and report.exit_code == 0
    assert all(r.verdict == "n/a (mock)" for r in report.rows)
    printed: list[str] = []
    e2e.print_table(report, printed.append)
    assert any("nothing about the Guardian was proven" in line for line in printed)


def test_unreachable_engine_is_a_harness_error_not_a_pass(stubbed_shopper, merchant):
    async def go():
        def down(_request):
            raise httpx.ConnectError("down")

        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(down),
                              base_url="http://engine") as engine,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=stubbed_shopper),
                              base_url="http://shopper") as shopper,
        ):
            await e2e.Runner(engine, shopper, s1_key="a", s3_key="b", merchant=merchant).run()

    with pytest.raises(e2e.E2EError):
        asyncio.run(go())
