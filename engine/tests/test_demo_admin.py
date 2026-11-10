"""Hosted-demo support: bootstrap, POST /v1/admin/reset-demo, CORS from config, /health fields."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from conftest import KEY_S1, KEY_S3, AutoKeyClient
from fastapi.testclient import TestClient
from gawah_engine import bootstrap
from gawah_engine.config import cors_origins, settings
from gawah_engine.main import app
from gawah_engine.services import _cached, get_services

ADMIN = "admin-key-for-tests"


@pytest.fixture
def demo(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "gawah_mock", False)
    monkeypatch.setattr(settings, "gawah_env", "demo")
    monkeypatch.setattr(settings, "gawah_bootstrap", True)
    monkeypatch.setattr(settings, "admin_key", ADMIN)
    monkeypatch.setattr(settings, "system1_key", KEY_S1)
    monkeypatch.setattr(settings, "system3_key", KEY_S3)
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'demo.db'}")
    monkeypatch.setattr(settings, "ledger_signing_key_path", str(tmp_path / "keys"))
    monkeypatch.setattr(settings, "mandate_signing_key_path", str(tmp_path / "keys"))
    _cached.cache_clear()
    with AutoKeyClient(app) as c:
        yield c
    _cached.cache_clear()


def reset(client, key=ADMIN):
    return client.post("/v1/admin/reset-demo", headers={"X-Admin-Key": key} if key else {})


def make_mandate(client):
    exp = (datetime.now(UTC) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    r = client.post("/v1/mandates", headers={"X-Gawah-Key": "real-key"}, json={
        "user_id": "u", "category": "footwear", "item_description": "Black running shoes",
        "max_amount_minor": 8000, "currency": "USD", "allowed_merchants": ["demo-shoe-store"],
        "expires_at": exp})
    assert r.status_code == 201, r.text
    return r.json()


def test_bootstrap_creates_keys_and_registers_the_demo_merchant_and_nothing_else(demo, tmp_path):
    svc = get_services()
    assert (tmp_path / "keys" / "gawah-ledger-key.active").exists()
    assert (tmp_path / "keys" / "gawah-mandate-key.active").exists()
    assert (tmp_path / "keys" / "demo-shoe-store-key-1.pem").exists()
    assert svc.merchants.get("demo-shoe-store", "demo-shoe-store-key-1") is not None
    assert svc.ledger.head_seq() == 0 and svc.mandates.list() == []
    assert bootstrap.database_is_empty(svc.path)


def test_bootstrap_is_idempotent_and_keeps_existing_data(demo):
    m = make_mandate(demo)
    head = get_services().ledger.head_seq()
    assert head >= 1
    bootstrap.bootstrap_if_empty()
    _cached.cache_clear()
    assert get_services().ledger.head_seq() == head
    assert get_services().mandates.get(m["id"]).id == m["id"]


def test_bootstrap_refuses_data_without_its_keys(demo, tmp_path):
    make_mandate(demo)
    for f in (tmp_path / "keys").glob("gawah-ledger-key*"):
        f.unlink()
    _cached.cache_clear()
    with pytest.raises(RuntimeError, match="missing"):
        bootstrap.bootstrap_if_empty()


def test_reset_wipes_data_and_regenerates_keys_and_ledger(demo, tmp_path):
    m = make_mandate(demo)
    old_head = demo.get("/v1/ledger/head", headers={"X-Gawah-Key": "real-key"}).json()
    old_pem = (tmp_path / "keys" / "demo-shoe-store-key-1.pem").read_bytes()
    assert old_head["seq"] >= 1

    r = reset(demo)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "reset" and body["ledger_head_seq"] == 0
    assert body["merchant_id"] == "demo-shoe-store"

    assert demo.get(f"/v1/mandates/{m['id']}", headers={"X-Gawah-Key": "real-key"}).status_code == 404
    head = demo.get("/v1/ledger/head", headers={"X-Gawah-Key": "real-key"}).json()
    assert head["seq"] == 0
    assert (tmp_path / "keys" / "demo-shoe-store-key-1.pem").read_bytes() != old_pem
    # the new ledger works and verifies under the new keys
    make_mandate(demo)
    v = demo.get("/v1/ledger/verify", headers={"X-Gawah-Key": "real-key"}).json()
    assert v["ok"] is True and v["checked"] >= 1
    # the demo merchant is registered again
    assert demo.get("/v1/merchants/demo-shoe-store/public-key",
                    headers={"X-Gawah-Key": KEY_S1}).status_code == 200


def test_reset_refuses_a_wrong_or_missing_key_and_changes_nothing(demo):
    make_mandate(demo)
    seq = get_services().ledger.head_seq()
    for key in ("wrong", "", None):
        r = reset(demo, key)
        assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"
    # a service key is not an admin key
    assert reset(demo, KEY_S3).status_code == 401
    assert get_services().ledger.head_seq() == seq


def test_reset_refuses_outside_demo_mode(demo, monkeypatch):
    make_mandate(demo)
    for env in ("", "test", "production"):
        monkeypatch.setattr(settings, "gawah_env", env)
        r = reset(demo)
        assert r.status_code == 403 and r.json()["error"]["code"] == "ADMIN_DISABLED"
    assert get_services().ledger.head_seq() >= 1


def test_reset_is_closed_when_no_admin_key_is_configured(demo, monkeypatch):
    monkeypatch.setattr(settings, "admin_key", "")
    assert reset(demo, "").status_code == 401
    assert reset(demo, "anything").status_code == 401


def test_reset_attempts_are_rate_limited(demo):
    codes = [reset(demo, "wrong").status_code for _ in range(12)]
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]


def test_reset_in_mock_mode_is_refused(demo, monkeypatch):
    monkeypatch.setattr(settings, "gawah_mock", True)
    r = reset(demo)
    assert r.status_code == 409 and r.json()["error"]["code"] == "MOCK_MODE"


def test_health_reports_env_mock_paypal(demo):
    h = demo.get("/health").json()
    assert (h["env"], h["mock"], h["paypal"]) == ("demo", False, "sandbox")
    assert {"semantic", "model"} <= h.keys()


def test_health_in_mock_mode(monkeypatch):
    monkeypatch.setattr(settings, "gawah_mock", True)
    monkeypatch.setattr(settings, "gawah_env", "")
    assert TestClient(app).get("/health").json() == {
        "status": "ok", "service": "gawah", "env": "dev", "mock": True, "paypal": "sandbox", "card_configured": False,
        "semantic": "fallback", "model": None}


def test_cors_origins_parsing():
    assert cors_origins("https://a.example, https://b.example/ ,") == [
        "https://a.example", "https://b.example"]
    assert cors_origins("") == []


def test_cors_uses_the_configured_origins():
    """The middleware was built from ENGINE_CORS_ORIGINS at import; default is localhost:3000."""
    mw = next(m for m in app.user_middleware if m.cls.__name__ == "CORSMiddleware")
    assert mw.kwargs["allow_origins"] == cors_origins(settings.engine_cors_origins)


def test_cors_env_variable_is_read(monkeypatch):
    from gawah_engine.config import Settings

    monkeypatch.setenv("ENGINE_CORS_ORIGINS", "https://web.example.com")
    assert cors_origins(Settings().engine_cors_origins) == ["https://web.example.com"]


def test_no_secret_in_reset_response(demo):
    text = reset(demo).text
    assert ADMIN not in text and "PRIVATE" not in text


def test_database_is_empty_ignores_missing_tables(tmp_path):
    p = tmp_path / "x.db"
    assert bootstrap.database_is_empty(str(p))
    sqlite3.connect(p).close()
    assert bootstrap.database_is_empty(str(p))
