"""Sandbox card configuration: expiry normalisation, startup refusal, CARD_NOT_CONFIGURED, health."""

import re

import pytest
from conftest import build_services
from fastapi.testclient import TestClient
from gawah_contract import LedgerEntryType, PaymentStatus
from gawah_engine.config import Settings, card_problems, normalize_expiry, settings
from gawah_engine.main import app
from gawah_engine.payments import PaymentConfig
from world import World

NUMBER, CVV = "4111111111111111", "123"
SECRET_BITS = (NUMBER, CVV, "12/2030", "2030-12")


def leaks(text: str) -> bool:
    """A secret appears in `text`. The 3-digit CVV is matched as a standalone token: inside a random
    hex id ("..a123f..") "123" is chance, not a leak, and made this check flaky (about 1 in 100 runs)."""
    if any(bit in text for bit in SECRET_BITS if bit != CVV):
        return True
    return re.search(rf"(?<!\w){CVV}(?!\w)", text) is not None


# --- (a) expiry normalisation -----------------------------------------------------------------


@pytest.mark.parametrize(("raw", "want"), [
    ("2030-12", "2030-12"), ("12/2030", "2030-12"), ("12/30", "2030-12"), ("01/27", "2027-01"),
    (" 03/2031 ", "2031-03"),
])
def test_expiry_formats_are_normalised(raw, want):
    assert normalize_expiry(raw) == want


@pytest.mark.parametrize("raw", ["", "2030/12", "12-2030", "13/2030", "00/30", "2030-13", "1/30",
                                 "12/203", "Dec 2030", "12/2030/1", "2030-1"])
def test_other_expiry_formats_are_rejected(raw):
    assert normalize_expiry(raw) is None


def test_settings_normalise_the_expiry_when_loading(monkeypatch):
    monkeypatch.setenv("PAYPAL_CARD_EXPIRY", "12/2030")
    assert Settings().paypal_card_expiry == "2030-12"
    monkeypatch.setenv("PAYPAL_CARD_EXPIRY", "12/30")
    assert Settings().paypal_card_expiry == "2030-12"
    monkeypatch.setenv("PAYPAL_CARD_EXPIRY", "soon")  # kept as-is: startup reports it, not import
    assert Settings().paypal_card_expiry == "soon"


def test_the_card_sent_to_paypal_has_a_normalised_expiry():
    cfg = PaymentConfig(card_number=NUMBER, card_expiry="12/2030", card_cvv=CVV)
    assert cfg.card()["expiry"] == "2030-12"


# --- (a)/(b) startup refusal in real mode -----------------------------------------------------


@pytest.fixture
def real(tmp_path, monkeypatch):
    for name, value in (("gawah_mock", False), ("paypal_client_id", "id"),
                        ("paypal_client_secret", "secret"), ("paypal_card_number", NUMBER),
                        ("paypal_card_expiry", "2030-12"), ("paypal_card_cvv", CVV),
                        ("database_url", f"sqlite:///{tmp_path / 'c.db'}"),
                        ("ledger_signing_key_path", str(tmp_path / "keys")),
                        ("mandate_signing_key_path", str(tmp_path / "keys"))):
        monkeypatch.setattr(settings, name, value)


def test_a_valid_card_starts_fine(real):
    with TestClient(app) as c:
        assert c.get("/health").json()["card_configured"] is True


@pytest.mark.parametrize(("name", "value", "message"), [
    ("paypal_card_expiry", "12-2030", "PAYPAL_CARD_EXPIRY is malformed"),
    ("paypal_card_expiry", "", "PAYPAL_CARD_EXPIRY is missing"),
    ("paypal_card_number", "", "PAYPAL_CARD_NUMBER is missing"),
    ("paypal_card_number", "4111-abcd", "PAYPAL_CARD_NUMBER is malformed"),
    ("paypal_card_cvv", "", "PAYPAL_CARD_CVV is missing"),
    ("paypal_card_cvv", "12", "PAYPAL_CARD_CVV is malformed"),
])
def test_startup_refuses_a_missing_or_malformed_card_without_echoing_values(
        real, monkeypatch, name, value, message):
    monkeypatch.setattr(settings, name, value)
    with pytest.raises(RuntimeError) as e, TestClient(app):
        pass
    text = str(e.value)
    assert message in text and "MM/YYYY" in text
    assert not leaks(text)


def test_startup_does_not_demand_a_card_in_mock_mode_or_without_credentials(real, monkeypatch):
    monkeypatch.setattr(settings, "paypal_card_expiry", "bad")
    monkeypatch.setattr(settings, "gawah_mock", True)
    with TestClient(app):
        pass
    monkeypatch.setattr(settings, "gawah_mock", False)
    monkeypatch.setattr(settings, "paypal_client_id", "")
    with TestClient(app):
        pass


def test_card_problems_name_variables_never_values():
    problems = card_problems("4111x", "bad", "1")
    assert len(problems) == 3 and not any("4111" in p or "bad" in p for p in problems)


# --- (b) payment flow: CARD_NOT_CONFIGURED, no browser fallback -------------------------------


def wallet_world(tmp_path, clock, paypal, calls, **card):
    def approver(link, email, password):  # the browser-approval route: must NOT run
        calls.append(link)

    cfg = PaymentConfig(client_id="x", client_secret="y", transport=paypal.transport,
                        approver=approver, buyer_email="buyer@example.test",
                        buyer_password="not-a-real-password", require_card=True, **card)
    return World(build_services(tmp_path, clock, payment_config=cfg), clock)


@pytest.mark.parametrize("card", [
    {},  # nothing set
    {"card_number": NUMBER, "card_expiry": "2030-13", "card_cvv": CVV},
    {"card_number": NUMBER, "card_expiry": "12/2030"},  # cvv missing
])
def test_real_mode_without_a_valid_card_fails_explicitly_and_never_uses_the_browser(
        tmp_path, clock, paypal, card):
    calls = []
    w = wallet_world(tmp_path, clock, paypal, calls, **card)
    pay = w.svc.guardian.submit(w.proposal()).payment
    assert pay.status == PaymentStatus.FAILED
    assert pay.failure_reason.startswith("CARD_NOT_CONFIGURED: ")
    assert calls == [] and paypal.calls("create") == []  # no browser, no PayPal call at all
    failed = w.entries(type=LedgerEntryType.PAYMENT_FAILED)[0]
    assert failed.data["code"] == "CARD_NOT_CONFIGURED"
    assert failed.data["reason"].startswith("CARD_NOT_CONFIGURED: ")
    assert not leaks(str(failed.data))
    b = w.svc.mandates.budget(w.mandate.id)
    assert (b.spent_minor, b.reserved_minor) == (0, 0)  # the budget was released
    assert w.svc.ledger.verify()["ok"]


def test_real_mode_with_a_card_in_mm_yyyy_pays(tmp_path, clock, paypal):
    w = wallet_world(tmp_path, clock, paypal, [], card_number=NUMBER, card_expiry="12/2030",
                     card_cvv=CVV)
    pay = w.svc.guardian.submit(w.proposal()).payment
    assert pay.status == PaymentStatus.PAID
    assert paypal.calls("create")  # sent to PayPal as 2030-12 (see the normalisation test)


# --- (c) health ---------------------------------------------------------------------------------


def test_health_reports_card_configured_without_revealing_values(real, monkeypatch):
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.json()["card_configured"] is True
        assert not leaks(r.text)
    monkeypatch.setattr(settings, "paypal_card_expiry", "")
    monkeypatch.setattr(settings, "paypal_client_id", "")  # allow startup with a bad card
    with TestClient(app) as c:
        assert c.get("/health").json()["card_configured"] is False
