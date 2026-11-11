"""Fixtures for the in-process end-to-end tests.

Everything runs in this process: the engine's FastAPI app (real Guardian, ledger and signing, with
the engine's own SIMULATED PayPal, GAWAH_ENV=test) and System 1's shopper app (with a SIMULATED
Groq). No socket may be opened: an autouse fixture makes any connection attempt an error.
"""

import inspect
import socket

import httpx
import pytest
from gawah_engine import services as services_module
from gawah_engine.auth import limiter
from gawah_engine.config import settings
from gawah_engine.main import app as engine_app
from shopper.config import Settings
from shopper.merchant.demo_merchant import DemoMerchantAdapter

from integration.stubs import install_stubs, remove_stubs, semantic_checker_factory

S1_KEY, S3_KEY = "test-system1-key", "test-system3-key"
_SECRETS = ("paypal_client_id", "paypal_client_secret", "paypal_card_number", "paypal_card_expiry",
            "paypal_card_cvv", "buyer_email", "buyer_password", "groq_api_key")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    original_connect = socket.socket.connect

    def refuse(self, address, *args, **kwargs):
        # Python 3.14's Windows Proactor event loop implements socketpair() by making a
        # temporary loopback connection. Allow only that stdlib-internal mechanism; the test
        # harness still rejects every application or external network connection.
        if (
            isinstance(address, tuple)
            and address[0] == "127.0.0.1"
            and any(frame.function == "_fallback_socketpair" for frame in inspect.stack())
        ):
            return original_connect(self, address, *args, **kwargs)
        raise AssertionError(f"test tried to open a network connection to {address!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    for name in _SECRETS:  # never use credentials from a developer's .env
        monkeypatch.setattr(settings, name, "")
    monkeypatch.setattr(services_module, "paypal_transport_override",
                        httpx.MockTransport(lambda r: pytest.fail(f"network call {r.url}")))
    monkeypatch.setenv("GROQ_API_KEY", "stub-not-used")
    monkeypatch.setenv("ATTACK_SIMULATION_ENABLED", "true")
    monkeypatch.setenv("PRODUCT_PROVIDER", "demo")
    limiter.reset()


@pytest.fixture
def engine_settings(tmp_path, monkeypatch):
    """Engine in real mode on a temp database, paying through its SIMULATED PayPal."""
    monkeypatch.setattr(settings, "gawah_mock", False)
    monkeypatch.setattr(settings, "gawah_env", "test")
    monkeypatch.setattr(settings, "gawah_fake_paypal", True)
    monkeypatch.setattr(settings, "system1_key", S1_KEY)
    monkeypatch.setattr(settings, "system3_key", S3_KEY)
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'engine.db'}")
    monkeypatch.setattr(settings, "ledger_signing_key_path", str(tmp_path / "engine-keys"))
    monkeypatch.setattr(settings, "mandate_signing_key_path", str(tmp_path / "engine-keys"))
    monkeypatch.setattr(settings, "rate_limit_per_minute", 0)
    # The Guardian fails closed without a model: answer its category check with the SIMULATED one.
    monkeypatch.setattr(services_module, "SemanticChecker",
                        semantic_checker_factory(services_module.SemanticChecker))
    return settings


@pytest.fixture
def merchant(tmp_path):
    """The demo merchant's signer, in a temp key directory the stubbed shopper shares."""
    return DemoMerchantAdapter(
        Settings(groq_api_key="stub-not-used", merchant_signing_key_path=str(tmp_path / "merchant")))


@pytest.fixture
def stubbed_shopper(tmp_path, merchant):
    from shopper.main import app

    install_stubs(Settings(groq_api_key="stub-not-used",
                           merchant_signing_key_path=str(tmp_path / "merchant")))
    yield app
    remove_stubs()


@pytest.fixture
def engine_app_ref():
    return engine_app
