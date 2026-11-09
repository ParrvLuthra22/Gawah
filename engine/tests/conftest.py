"""Shared fixtures for the real (non-mock) service tests. Everything lives under tmp_path."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from gawah_contract import MandateCreate
from gawah_contract.carts import generate_keypair, public_key_pem
from gawah_engine import services as services_module
from gawah_engine.config import settings
from gawah_engine.db import db_path, init_db
from gawah_engine.evidence import EvidenceService
from gawah_engine.guardian import GuardianService
from gawah_engine.ledger_service import LedgerService
from gawah_engine.main import app
from gawah_engine.mandate_service import MandateService
from gawah_engine.merchant_service import MerchantService
from gawah_engine.payments import PaymentConfig, PaymentService
from gawah_engine.semantic import SemanticChecker
from gawah_engine.services import Services
from gawah_engine.signing import ES256Signer
from paypal_fake import FakePayPal

# A public PayPal sandbox test card number; only ever used with the fake PayPal in tests.
TEST_CARD = {"number": "4111111111111111", "expiry": "2030-12", "security_code": "123",
             "name": "Test Card"}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def build_services(tmp_path, clock, paypal=None, payment_config=None) -> Services:
    """Real services on a temp database. With `paypal` (a FakePayPal) the Guardian also pays."""
    path = db_path(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(path)
    keys = tmp_path / "keys"
    ledger = LedgerService(path, ES256Signer(keys, "gawah-ledger-key"), clock=clock)
    mandates = MandateService(path, ES256Signer(keys, "gawah-mandate-key"), ledger, clock=clock)
    merchants = MerchantService(path, clock=clock)
    semantic = SemanticChecker(path, clock=clock)  # no key: deterministic fallback
    config = payment_config or PaymentConfig()
    if paypal is not None:
        config = PaymentConfig(
            client_id="test-client", client_secret="test-secret", transport=paypal.transport,
            card_number=TEST_CARD["number"], card_expiry=TEST_CARD["expiry"],
            card_cvv=TEST_CARD["security_code"], card_name=TEST_CARD["name"],
        )
    payments = PaymentService(path, mandates, ledger, config, clock=clock, sleep=lambda s: None)
    guardian = GuardianService(
        path, mandates, merchants, ledger, semantic, clock=clock,
        payments=payments if (paypal is not None or payment_config is not None) else None,
    )
    evidence = EvidenceService(path, mandates, merchants, ledger, payments)
    return Services(path, ledger, mandates, merchants, guardian, payments, evidence)


@pytest.fixture
def svc(tmp_path, clock) -> Services:
    """Guardian-only services (no payments): decisions are made, nothing is paid."""
    return build_services(tmp_path, clock)


@pytest.fixture
def paypal() -> FakePayPal:
    return FakePayPal()


@pytest.fixture
def psvc(tmp_path, clock, paypal) -> Services:
    """Full services: ALLOW decisions are reserved and paid through the fake PayPal."""
    return build_services(tmp_path, clock, paypal)


def make_body(clock: Clock | None = None, **over) -> MandateCreate:
    base = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC) if clock is None else clock()
    fields = {
        "user_id": "user_1",
        "category": "footwear",
        "item_description": "Black running shoes, size 9",
        "max_amount_minor": 8000,
        "currency": "USD",
        "allowed_merchants": ["demo-shoe-store"],
        "expires_at": base + timedelta(days=7),
    }
    fields.update(over)
    return MandateCreate(**fields)


_SECRET_SETTINGS = (
    "paypal_client_id", "paypal_client_secret", "paypal_card_number", "paypal_card_expiry",
    "paypal_card_cvv", "buyer_email", "buyer_password", "groq_api_key",
)


def _network_forbidden(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"test tried to reach the network: {request.method} {request.url}")


@pytest.fixture(autouse=True)
def no_network_or_real_credentials(request, monkeypatch):
    """The default test run never uses real credentials from .env and never touches PayPal/Groq.

    Tests that need PayPal use FakePayPal (see `paypal_fake.py`). Tests marked `live` opt out.
    """
    if request.node.get_closest_marker("live"):
        yield
        return
    for name in _SECRET_SETTINGS:
        monkeypatch.setattr(settings, name, "")
    monkeypatch.setattr(
        services_module, "paypal_transport_override", httpx.MockTransport(_network_forbidden)
    )
    yield


@pytest.fixture
def use_fake_paypal(monkeypatch, paypal):
    """Make get_services() (the HTTP API) pay through the fake PayPal."""
    monkeypatch.setattr(settings, "paypal_client_id", "test-client")
    monkeypatch.setattr(settings, "paypal_client_secret", "test-secret")
    monkeypatch.setattr(settings, "paypal_card_number", TEST_CARD["number"])
    monkeypatch.setattr(settings, "paypal_card_expiry", TEST_CARD["expiry"])
    monkeypatch.setattr(settings, "paypal_card_cvv", TEST_CARD["security_code"])
    monkeypatch.setattr(services_module, "paypal_transport_override", paypal.transport)
    return paypal


GUARDIAN_KEY = generate_keypair()
API_HEADERS = {"X-Gawah-Key": "real-key"}


@pytest.fixture
def api(tmp_path, monkeypatch, use_fake_paypal):
    monkeypatch.setattr(settings, "gawah_mock", False)
    monkeypatch.setattr(settings, "system1_key", KEY_S1)
    monkeypatch.setattr(settings, "system3_key", KEY_S3)
    monkeypatch.setattr(settings, "groq_api_key", "")
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'g.db'}")
    monkeypatch.setattr(settings, "ledger_signing_key_path", str(tmp_path / "keys"))
    monkeypatch.setattr(settings, "mandate_signing_key_path", str(tmp_path / "keys"))
    with AutoKeyClient(app) as c:
        reg = {"merchant_id": "demo-shoe-store", "key_id": "demo-shoe-store-key-1",
               "alg": "ES256", "public_key_pem": public_key_pem(GUARDIAN_KEY)}
        assert c.post("/v1/merchants", json=reg, headers=API_HEADERS).status_code == 201
        m = c.post("/v1/mandates", headers=API_HEADERS, json={
            "user_id": "u1", "category": "footwear",
            "item_description": "Black running shoes, size 9, under $80",
            "max_amount_minor": 8000, "currency": "USD",
            "allowed_merchants": ["demo-shoe-store"], "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat().replace('+00:00', 'Z'),
        }).json()
        c.mandate_id = m["id"]
        yield c




# ---- scoped test client -----------------------------------------------------------------------
# Every /v1 route now belongs to exactly one service key (engine/gawah_engine/auth.py SCOPES).
# Tests that exercise behaviour rather than authorization send this placeholder key and
# AutoKeyClient swaps in whichever real key is allowed for that route. test_scopes.py uses the
# real keys directly, so the authorization matrix itself is never auto-corrected.
AUTO_KEY = "real-key"
KEY_S1, KEY_S3 = "real-key-system1", "real-key-system3"


def _allowed_service(method: str, path: str) -> str | None:
    import re

    from gawah_engine.auth import SCOPES

    for service, routes in SCOPES.items():
        for route in routes:
            m, template = route.split(" ", 1)
            pattern = "^" + re.sub(r"\{[^}]+\}", "[^/]+", template) + "$"
            if m == method and re.match(pattern, path.split("?")[0]):
                return service
    return None


class AutoKeyClient(TestClient):
    def request(self, method, url, **kw):  # type: ignore[override]
        headers = dict(kw.get("headers") or {})
        if headers.get("X-Gawah-Key") == AUTO_KEY:
            service = _allowed_service(method.upper(), str(url)) or "system1"
            headers["X-Gawah-Key"] = KEY_S1 if service == "system1" else KEY_S3
            kw["headers"] = headers
        return super().request(method, url, **kw)


@pytest.fixture(autouse=True)
def _fresh_rate_limiter():
    from gawah_engine.auth import limiter

    limiter.reset()
    yield
