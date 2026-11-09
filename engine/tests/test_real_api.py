"""The /v1 API with GAWAH_MOCK=0: temp database, temp keys, FastAPI TestClient."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from gawah_contract import ErrorResponse, LedgerEntry, LedgerVerifyResult, Mandate
from gawah_engine.config import settings
from gawah_engine.main import app

H = {"X-Gawah-Key": "real-key"}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "gawah_mock", False)
    monkeypatch.setattr(settings, "system1_key", "real-key")
    monkeypatch.setattr(settings, "system3_key", "")
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setattr(settings, "ledger_signing_key_path", str(tmp_path / "keys"))
    monkeypatch.setattr(settings, "mandate_signing_key_path", str(tmp_path / "keys"))
    with TestClient(app) as client:  # runs the lifespan: init db + keys
        client.db_path = tmp_path / "api.db"
        yield client


def body(**over):
    b = {
        "user_id": "u1",
        "category": "footwear",
        "item_description": "Black running shoes, size 9",
        "max_amount_minor": 8000,
        "currency": "USD",
        "allowed_merchants": ["demo-shoe-store"],
        "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat().replace("+00:00", "Z"),
    }
    b.update(over)
    return b


def test_mandate_lifecycle(api):
    r = api.post("/v1/mandates", json=body(), headers=H)
    assert r.status_code == 201
    m = Mandate.model_validate(r.json())
    assert m.status == "active" and m.signing_alg == "ES256" and m.signature
    assert api.get(f"/v1/mandates/{m.id}", headers=H).json() == r.json()
    assert [x["id"] for x in api.get("/v1/mandates", headers=H).json()] == [m.id]
    rv = api.post(f"/v1/mandates/{m.id}/revoke", headers=H)
    assert rv.status_code == 200 and rv.json()["status"] == "revoked"
    assert api.get(f"/v1/mandates/{m.id}", headers=H).json()["status"] == "revoked"


def test_creating_and_revoking_append_to_the_ledger(api):
    m = api.post("/v1/mandates", json=body(), headers=H).json()
    api.post(f"/v1/mandates/{m['id']}/revoke", headers=H)
    entries = [LedgerEntry.model_validate(e) for e in api.get("/v1/ledger", headers=H).json()]
    assert [e.type.value for e in entries] == ["MANDATE_CREATED", "MANDATE_REVOKED"]
    assert api.get(f"/v1/ledger?mandate_id={m['id']}", headers=H).json() == [
        e.model_dump(mode="json") for e in entries
    ]
    assert len(api.get(f"/v1/ledger?subject_id={m['id']}", headers=H).json()) == 2
    assert len(api.get("/v1/ledger?type=MANDATE_REVOKED", headers=H).json()) == 1
    assert len(api.get("/v1/ledger?since_seq=1", headers=H).json()) == 1
    assert api.get("/v1/ledger?mandate_id=nope", headers=H).json() == []


def test_verify_endpoint_ok_then_broken(api):
    api.post("/v1/mandates", json=body(), headers=H)
    api.post("/v1/mandates", json=body(), headers=H)
    r = api.get("/v1/ledger/verify", headers=H)
    assert LedgerVerifyResult.model_validate(r.json()) == LedgerVerifyResult(
        ok=True, checked=2, first_broken_seq=None, reason=None
    )
    conn = sqlite3.connect(api.db_path, isolation_level=None)
    conn.execute("DROP TRIGGER ledger_no_update")
    conn.execute("""UPDATE ledger SET data = '{"mandate_id":"x"}' WHERE seq = 2""")
    conn.close()
    broken = api.get("/v1/ledger/verify", headers=H).json()
    assert broken["ok"] is False and broken["checked"] == 2 and broken["first_broken_seq"] == 2
    assert "hash mismatch" in broken["reason"]


def test_errors_use_the_contract_envelope(api):
    r = api.get("/v1/mandates/man_nope", headers=H)
    assert r.status_code == 404 and ErrorResponse.model_validate(r.json()).error.code == "MANDATE_NOT_FOUND"
    assert api.post("/v1/mandates/man_nope/revoke", headers=H).status_code == 404
    assert api.get("/v1/mandates", headers={}).status_code == 401
    assert api.post("/v1/mandates", json={"user_id": "u"}, headers=H).status_code == 422
    past = body(expires_at="2020-01-01T00:00:00Z")
    r = api.post("/v1/mandates", json=past, headers=H)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert api.post("/v1/mandates", json=body(max_amount_minor=0), headers=H).status_code == 422


def test_revoking_an_expired_mandate_is_409(api):
    soon = (datetime.now(UTC) + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    m = api.post("/v1/mandates", json=body(expires_at=soon), headers=H).json()
    conn = sqlite3.connect(api.db_path, isolation_level=None)
    conn.execute("UPDATE mandates SET expires_at = '2020-01-01T00:00:00Z' WHERE id = ?", (m["id"],))
    conn.close()
    assert api.get(f"/v1/mandates/{m['id']}", headers=H).json()["status"] == "expired"
    assert api.post(f"/v1/mandates/{m['id']}/revoke", headers=H).status_code == 409


def test_unbuilt_endpoints_still_answer_501(api):
    for path in ("/v1/payments", "/v1/evidence/pay_01", "/v1/merchants/demo-shoe-store/public-key"):
        assert api.get(path, headers=H).status_code == 501


def test_mock_mode_never_touches_the_database(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "gawah_mock", True)
    monkeypatch.setattr(settings, "system1_key", "real-key")
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'never.db'}")
    with TestClient(app) as client:
        assert client.get("/v1/mandates/man_01", headers=H).json()["id"] == "man_01"
        assert client.get("/v1/ledger/verify", headers=H).json()["checked"] == 5
    assert not (tmp_path / "never.db").exists()


def test_mock_verify_reports_reason_for_a_broken_chain(monkeypatch):
    from gawah_contract.fixtures import load
    from gawah_engine.routers import ledger as ledger_router

    chain = load("ledger_chain")
    chain[2]["data"]["verdict"] = "BLOCK"  # edit after hashing
    monkeypatch.setattr(settings, "gawah_mock", True)
    monkeypatch.setattr(settings, "system1_key", "real-key")
    monkeypatch.setattr(ledger_router, "load", lambda name: chain)
    r = TestClient(app).get("/v1/ledger/verify", headers=H).json()
    assert r["ok"] is False and r["first_broken_seq"] == 3 and "seq 3" in r["reason"]
