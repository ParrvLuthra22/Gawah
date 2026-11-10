"""Authorization matrix: every route x every key, plus the rate limit.

The expected table below is written out by hand from the policy, NOT derived from
`auth.SCOPES`, so a mistake in SCOPES cannot hide itself.
"""

import pytest
from conftest import KEY_S1, KEY_S3
from fastapi.testclient import TestClient
from gawah_contract import ErrorResponse
from gawah_engine.config import settings
from gawah_engine.main import app

S1, S3 = "system1", "system3"

# (method, concrete path, route template, services allowed)
MATRIX = [
    ("POST", "/v1/proposals", "/v1/proposals", {S1}),
    ("POST", "/v1/guardian/check", "/v1/guardian/check", {S3}),
    ("GET", "/v1/mandates", "/v1/mandates", {S1, S3}),
    ("GET", "/v1/mandates/man_x", "/v1/mandates/{mandate_id}", {S1, S3}),
    ("POST", "/v1/mandates", "/v1/mandates", {S3}),
    ("POST", "/v1/mandates/man_x/revoke", "/v1/mandates/{mandate_id}/revoke", {S3}),
    ("GET", "/v1/mandates/man_x/budget", "/v1/mandates/{mandate_id}/budget", {S3}),
    ("POST", "/v1/merchants", "/v1/merchants", {S1}),
    ("GET", "/v1/merchants/m/public-key", "/v1/merchants/{merchant_id}/public-key", {S1}),
    ("GET", "/v1/payments", "/v1/payments", {S3}),
    ("GET", "/v1/payments/pay_x", "/v1/payments/{payment_id}", {S3}),
    ("POST", "/v1/payments/pay_x/refund", "/v1/payments/{payment_id}/refund", {S3}),
    ("GET", "/v1/ledger", "/v1/ledger", {S3}),
    ("GET", "/v1/ledger/verify", "/v1/ledger/verify", {S3}),
    ("GET", "/v1/ledger/head", "/v1/ledger/head", {S3}),
    ("GET", "/v1/ledger/stream?limit=1&max_seconds=1", "/v1/ledger/stream", {S3}),
    ("GET", "/v1/evidence/pay_x", "/v1/evidence/{payment_id}", {S3}),
]
# /v1/admin/reset-demo is outside the service scopes on purpose: it has its own X-Admin-Key check
# and only works with GAWAH_ENV=demo (tests in test_demo_admin.py).
OPEN_ROUTES = {"/v1/admin/reset-demo", "/health", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
KEYS = {"none": None, "wrong": "not-a-key", S1: KEY_S1, S3: KEY_S3}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "gawah_mock", False)
    monkeypatch.setattr(settings, "system1_key", KEY_S1)
    monkeypatch.setattr(settings, "system3_key", KEY_S3)
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'm.db'}")
    monkeypatch.setattr(settings, "ledger_signing_key_path", str(tmp_path / "keys"))
    monkeypatch.setattr(settings, "mandate_signing_key_path", str(tmp_path / "keys"))
    monkeypatch.setattr(settings, "rate_limit_per_minute", 0)  # the matrix is big; limit tested below
    with TestClient(app) as c:
        yield c


def call(client, method, path, key):
    headers = {"X-Gawah-Key": key} if key else {}
    return client.request(method, path, headers=headers, json={} if method == "POST" else None)


@pytest.mark.parametrize(("method", "path", "template", "allowed"), MATRIX)
@pytest.mark.parametrize("who", ["none", "wrong", S1, S3])
def test_route_by_key(client, method, path, template, allowed, who):
    r = call(client, method, path, KEYS[who])
    if who in ("none", "wrong"):
        assert r.status_code == 401
        assert ErrorResponse.model_validate(r.json()).error.code == "UNAUTHORIZED"
    elif who in allowed:
        # allowed: it may still be a 404/422 for our placeholder ids and empty bodies,
        # but never an authentication or authorization failure
        assert r.status_code not in (401, 403, 429), (method, path, who, r.status_code, r.text)
    else:
        assert r.status_code == 403, (method, path, who, r.status_code)
        err = ErrorResponse.model_validate(r.json()).error
        assert err.code == "FORBIDDEN" and err.request_id and template.split("{")[0] in err.message


def test_matrix_covers_every_route_in_the_app():
    paths = app.openapi()["paths"]  # every operation the app serves (routers are nested)
    seen = {(m.upper(), p) for p, ops in paths.items() for m in ops if p not in OPEN_ROUTES}
    listed = {(m, t) for m, _p, t, _a in MATRIX}
    assert seen == listed, f"routes missing from the matrix: {seen - listed}; stale: {listed - seen}"


def test_health_is_open_and_needs_no_key(client):
    assert call(client, "GET", "/health", None).status_code == 200


def test_summary_the_shopper_key_can_neither_pay_nor_refund_except_through_proposals(client):
    s1_allowed = {(m, t) for m, _p, t, a in MATRIX if S1 in a}
    assert s1_allowed == {
        ("POST", "/v1/proposals"),
        ("GET", "/v1/mandates"), ("GET", "/v1/mandates/{mandate_id}"),
        ("POST", "/v1/merchants"), ("GET", "/v1/merchants/{merchant_id}/public-key"),
    }
    refunders = {w for m, _p, t, a in MATRIX if t.endswith("/refund") for w in a}
    proposers = {w for m, _p, t, a in MATRIX if t == "/v1/proposals" for w in a}
    assert refunders == {S3} and proposers == {S1}


def test_wrong_path_params_cannot_dodge_the_scope(client):
    # route templates are matched, not raw paths: odd ids do not change who may call what
    for path in ("/v1/payments/..%2Fmandates/refund", "/v1/payments/%20/refund", "/v1/payments/a%2Fb/refund"):
        assert client.post(path, headers={"X-Gawah-Key": KEY_S1}).status_code in (403, 404)
    r = client.post("/v1/payments/pay_1/refund?x=/v1/proposals", headers={"X-Gawah-Key": KEY_S1})
    assert r.status_code == 403


def test_rate_limit_is_per_key_and_returns_429(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 5)
    from gawah_engine.auth import limiter

    limiter.reset()
    codes = [call(client, "GET", "/v1/mandates", KEY_S1).status_code for _ in range(7)]
    assert codes == [200] * 5 + [429, 429]
    r = call(client, "GET", "/v1/mandates", KEY_S1)
    err = ErrorResponse.model_validate(r.json()).error
    assert err.code == "RATE_LIMITED" and err.details["retry_after_s"] >= 1
    assert call(client, "GET", "/v1/mandates", KEY_S3).status_code == 200  # other key unaffected
    assert call(client, "GET", "/health", None).status_code == 200  # health not limited
    assert call(client, "GET", "/v1/mandates", "wrong").status_code == 401  # unauthenticated: not counted


def test_forbidden_calls_still_count_against_the_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 3)
    from gawah_engine.auth import limiter

    limiter.reset()
    codes = [call(client, "POST", "/v1/payments/p/refund", KEY_S1).status_code for _ in range(5)]
    assert codes == [403, 403, 403, 429, 429]


def test_rate_limit_window_slides():
    from gawah_engine.auth import RateLimiter

    rl = RateLimiter()
    assert [rl.check("k", 2, window_s=0.2) for _ in range(2)] == [None, None]
    assert rl.check("k", 2, window_s=0.2) is not None
    import time

    time.sleep(0.25)
    assert rl.check("k", 2, window_s=0.2) is None
    assert rl.check("k", 0) is None  # 0 disables


def test_default_limit_is_60_per_minute():
    assert settings.rate_limit_per_minute == 60
