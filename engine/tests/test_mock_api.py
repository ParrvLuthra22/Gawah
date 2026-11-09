import pytest
from fastapi.testclient import TestClient
from gawah_contract import ErrorResponse
from gawah_contract.fixtures import load
from gawah_engine.config import settings
from gawah_engine.main import app

H1 = {"X-Gawah-Key": "test-key-1"}
H3 = {"X-Gawah-Key": "test-key-3"}


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setattr(settings, "system1_key", "test-key-1")
    monkeypatch.setattr(settings, "system3_key", "test-key-3")
    monkeypatch.setattr(settings, "gawah_mock", True)


client = TestClient(app)


def test_health_needs_no_key():
    assert client.get("/health").json() == {"status": "ok", "service": "gawah"}


def test_missing_or_bad_key_is_401_with_error_envelope():
    for headers in ({}, {"X-Gawah-Key": "nope"}):
        r = client.get("/v1/mandates", headers=headers)
        assert r.status_code == 401
        err = ErrorResponse.model_validate(r.json())
        assert err.error.code == "UNAUTHORIZED" and err.error.request_id


def test_both_service_keys_work():
    assert client.get("/v1/mandates", headers=H1).status_code == 200
    assert client.get("/v1/mandates", headers=H3).status_code == 200


def test_unconfigured_empty_key_never_matches(monkeypatch):
    monkeypatch.setattr(settings, "system1_key", "")
    assert client.get("/v1/mandates", headers={"X-Gawah-Key": ""}).status_code == 401


def test_good_proposal_is_allowed_and_paid():
    r = client.post("/v1/proposals", json=load("proposal_shoes"), headers=H1)
    assert r.status_code == 200
    body = r.json()
    assert body["decision"]["verdict"] == "ALLOW"
    assert body["payment"]["status"] == "PAID"


def test_over_budget_is_blocked():
    r = client.post("/v1/proposals", json=load("proposal_poisoned_over_budget"), headers=H1)
    body = r.json()
    assert body["decision"]["verdict"] == "BLOCK"
    assert body["decision"]["reasons"] == ["OVER_BUDGET"]
    assert body["payment"] is None


def test_bad_merchant_is_blocked():
    r = client.post("/v1/proposals", json=load("proposal_poisoned_merchant"), headers=H1)
    assert r.json()["decision"]["reasons"] == ["MERCHANT_NOT_ALLOWED"]


def test_guardian_check_returns_decision_only():
    r = client.post("/v1/guardian/check", json=load("proposal_shoes"), headers=H1)
    assert r.json()["verdict"] == "ALLOW"


def test_invalid_body_is_422_envelope():
    r = client.post("/v1/proposals", json={"id": "x"}, headers=H1)
    assert r.status_code == 422
    assert ErrorResponse.model_validate(r.json()).error.code == "VALIDATION_ERROR"


def test_mandate_lifecycle():
    assert client.get("/v1/mandates/man_01", headers=H3).json()["id"] == "man_01"
    assert client.post("/v1/mandates/man_01/revoke", headers=H3).json()["status"] == "revoked"
    assert client.get("/v1/mandates/nope", headers=H3).status_code == 404
    body = {k: load("mandate_shoes")[k] for k in (
        "user_id", "category", "item_description", "max_amount_minor", "currency",
        "allowed_merchants", "expires_at")}
    r = client.post("/v1/mandates", json=body, headers=H3)
    assert r.status_code == 201 and r.json()["id"] != "man_01"


def test_payments_and_refund():
    assert client.get("/v1/payments/pay_01", headers=H3).json()["status"] == "PAID"
    assert client.post("/v1/payments/pay_01/refund", headers=H3).json()["status"] == "REFUNDED"
    assert len(client.get("/v1/payments", headers=H3).json()) == 1


def test_ledger_filters_and_verify():
    assert len(client.get("/v1/ledger", headers=H3).json()) == 5
    assert len(client.get("/v1/ledger?mandate_id=nope", headers=H3).json()) == 0
    assert len(client.get("/v1/ledger?subject_id=pay_01", headers=H3).json()) == 2
    assert client.get("/v1/ledger/verify", headers=H3).json() == {
        "ok": True, "checked": 5, "first_broken_seq": None, "reason": None}


def test_evidence_bundles():
    assert client.get("/v1/evidence/pay_01", headers=H3).status_code == 200
    wrong = client.get("/v1/evidence/pay_04", headers=H3).json()
    assert wrong["signed_cart"]["line_items"][0]["variant"] == "size 10"
    assert client.get("/v1/evidence/nope", headers=H3).status_code == 404


def test_merchants():
    r = client.get("/v1/merchants/demo-shoe-store/public-key", headers=H1)
    assert r.json()["merchant_id"] == "demo-shoe-store"
    new = {"merchant_id": "m2", "key_id": "k2", "alg": "ed25519", "public_key_pem": "PEM"}
    assert client.post("/v1/merchants", json=new, headers=H1).status_code == 201
    assert client.get("/v1/merchants/m2/public-key", headers=H1).json()["key_id"] == "k2"


def test_non_mock_mode_returns_501(monkeypatch):
    # Mandates and the ledger are real in GAWAH_MOCK=0; payments are still unbuilt.
    monkeypatch.setattr(settings, "gawah_mock", False)
    r = client.get("/v1/payments", headers=H1)
    assert r.status_code == 501


def test_mock_budget():
    r = client.get("/v1/mandates/man_01/budget", headers=H3)
    assert r.status_code == 200
    assert r.json() == {"mandate_id": "man_01", "max_amount_minor": 8000, "spent_minor": 6500,
                        "reserved_minor": 0, "remaining_minor": 1500, "currency": "USD"}
    assert client.get("/v1/mandates/nope/budget", headers=H3).status_code == 404
