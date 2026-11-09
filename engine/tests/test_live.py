"""Whole flow against the real PayPal sandbox. Skipped unless GAWAH_LIVE=1.

Uses a temporary database and keys; needs PAYPAL_* (and card or buyer) settings in .env.
"""

import os

import pytest
from conftest import build_services
from gawah_contract import LedgerEntryType, PaymentStatus
from gawah_engine.config import settings
from gawah_engine.payments import PaymentConfig
from world import World

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("GAWAH_LIVE") != "1", reason="set GAWAH_LIVE=1 to run"),
]


def test_full_flow_against_the_paypal_sandbox(tmp_path, clock):
    cfg = PaymentConfig(
        client_id=settings.paypal_client_id, client_secret=settings.paypal_client_secret,
        env=settings.paypal_env or "sandbox",
        card_number=settings.paypal_card_number, card_expiry=settings.paypal_card_expiry,
        card_cvv=settings.paypal_card_cvv, card_name=settings.paypal_card_name or "Test Buyer",
        buyer_email=settings.buyer_email, buyer_password=settings.buyer_password,
    )
    assert cfg.configured, "PayPal sandbox credentials missing"
    svc = build_services(tmp_path, clock, None, payment_config=cfg)
    w = World(svc, clock)
    p = w.proposal()
    result = svc.guardian.submit(p)
    pay = result.payment
    assert pay.status == PaymentStatus.PAID, svc.payments._row(pay.id)["failure_reason"]
    assert pay.paypal_order_id and pay.paypal_capture_id
    types = [e.type.value for e in w.entries(mandate_id=w.mandate.id)]
    assert types[-2:] == ["PAYMENT_STARTED", "PAYMENT_COMPLETED"]
    bundle = svc.evidence.bundle(pay.id)
    assert bundle.verification.ok
    refunded = svc.payments.refund(pay.id, "live test")
    assert refunded.status == PaymentStatus.REFUNDED
    assert svc.mandates.budget(w.mandate.id).spent_minor == 0
    assert svc.ledger.list(type=LedgerEntryType.REFUNDED)
    print(f"LIVE order={pay.paypal_order_id[:6]}… capture={pay.paypal_capture_id[:6]}…")
