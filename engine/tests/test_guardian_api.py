"""POST /v1/proposals and /v1/guardian/check in real mode: TestClient + temp database."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from gawah_contract import ErrorResponse, LedgerVerifyResult, ProposalResult
from gawah_contract.carts import generate_keypair, sign_cart
from gawah_engine.config import settings
from gawah_engine.main import app

H = {"X-Gawah-Key": "real-key"}
from conftest import GUARDIAN_KEY as KEY

N = {"n": 0}


def _iso(delta=timedelta(0)):
    return (datetime.now(UTC) + delta).isoformat().replace("+00:00", "Z")


def proposal(api, unit=6500, variant="size 9", pid=None, mandate_id=None):
    N["n"] += 1
    n = N["n"]
    cart = sign_cart({
        "cart_id": f"cart_{n}", "merchant_id": "demo-shoe-store",
        "line_items": [{"sku": "SHOE-BLK-RUN", "title": "Black Runner", "variant": variant,
                        "quantity": 1, "unit_price_minor": unit}],
        "total_minor": unit, "currency": "USD", "expires_at": _iso(timedelta(hours=1)),
        "signature": "", "key_id": "",
    }, KEY, "demo-shoe-store-key-1").model_dump(mode="json")
    return {
        "id": pid or f"prop_api_{n}", "mandate_id": mandate_id or api.mandate_id,
        "agent_id": "shopper-agent-1", "merchant_id": "demo-shoe-store",
        "product": {"id": "SHOE-BLK-RUN", "title": "Black Runner", "url": "https://x.example/p"},
        "quantity": 1, "unit_price_minor": unit, "currency": "USD",
        "rationale": "Shoes for $65.", "signed_cart": cart, "created_at": _iso(),
    }


def test_allow_case(api):
    r = api.post("/v1/proposals", json=proposal(api), headers=H)
    assert r.status_code == 200
    res = ProposalResult.model_validate(r.json())
    assert res.decision.verdict == "ALLOW" and res.decision.reasons == []
    assert res.payment is not None and res.payment.status == "PAID"  # paid via the fake PayPal


def test_over_budget_and_size_ten_cases(api):
    over = api.post("/v1/proposals", json=proposal(api, unit=12000), headers=H).json()
    assert over["decision"]["verdict"] == "BLOCK" and over["decision"]["reasons"] == ["OVER_BUDGET"]
    size10 = api.post("/v1/proposals", json=proposal(api, variant="size 10"), headers=H).json()
    assert size10["decision"]["verdict"] == "ALLOW"
    made = api.get("/v1/ledger?type=DECISION_MADE", headers=H).json()
    advisories = [e["data"]["advisories"] for e in made if e["data"]["verdict"] == "ALLOW"]
    assert advisories and "size 10" in advisories[0][0]


def test_blocked_proposal_leaves_exactly_two_ledger_entries(api):
    base = len(api.get("/v1/ledger", headers=H).json())  # MANDATE_CREATED
    p = proposal(api, unit=12000)
    api.post("/v1/proposals", json=p, headers=H)
    entries = api.get("/v1/ledger", headers=H).json()[base:]
    assert [e["type"] for e in entries] == ["PROPOSAL_RECEIVED", "DECISION_MADE"]
    assert entries[0]["subject_id"] == p["id"] and entries[1]["subject_id"] == f"dec_{p['id']}"
    v = LedgerVerifyResult.model_validate(api.get("/v1/ledger/verify", headers=H).json())
    assert v.ok and v.checked == base + 2


def test_guardian_check_is_a_dry_run(api):
    base = len(api.get("/v1/ledger", headers=H).json())
    r = api.post("/v1/guardian/check", json=proposal(api, unit=12000), headers=H)
    assert r.status_code == 200 and r.json()["reasons"] == ["OVER_BUDGET"]
    assert len(api.get("/v1/ledger", headers=H).json()) == base


def test_resubmitting_the_same_proposal_is_idempotent(api):
    p = proposal(api)
    a = api.post("/v1/proposals", json=p, headers=H).json()
    b = api.post("/v1/proposals", json=p, headers=H).json()
    assert a == b
    types = [e["type"] for e in api.get("/v1/ledger", headers=H).json()]
    assert types.count("DECISION_MADE") == 1
    changed = api.post("/v1/proposals", json={**p, "rationale": "different"}, headers=H)
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "PROPOSAL_ID_CONFLICT"


def test_errors_use_the_contract_envelope(api):
    r = api.post("/v1/proposals", json=proposal(api, mandate_id="man_nope"), headers=H)
    assert r.status_code == 404 and ErrorResponse.model_validate(r.json()).error.code == "MANDATE_NOT_FOUND"
    assert api.post("/v1/proposals", json={"id": "x"}, headers=H).status_code == 422
    assert api.post("/v1/proposals", json=proposal(api), headers={}).status_code == 401
    assert api.post("/v1/guardian/check", json=proposal(api, mandate_id="man_nope"), headers=H).status_code == 404


def test_unregistered_merchant_is_a_block_not_an_error(api):
    p = proposal(api)
    p["merchant_id"] = "ghost-store"
    p["signed_cart"] = sign_cart({**p["signed_cart"], "merchant_id": "ghost-store"}, generate_keypair(), "ghost-1").model_dump(mode="json")
    r = api.post("/v1/proposals", json=p, headers=H).json()
    assert r["decision"]["verdict"] == "BLOCK" and r["decision"]["reasons"] == [
        "MERCHANT_NOT_REGISTERED", "MERCHANT_NOT_ALLOWED"]


def test_mock_mode_proposals_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "gawah_mock", True)
    monkeypatch.setattr(settings, "system1_key", "real-key")
    from gawah_contract.fixtures import load

    r = TestClient(app).post("/v1/proposals", json=load("proposal_shoes"), headers=H).json()
    assert r["decision"]["id"] == "dec_01" and r["payment"]["status"] == "PAID"
