"""Payments and evidence endpoints in real mode (fake PayPal, temp database)."""

from conftest import TEST_CARD
from gawah_contract import ErrorResponse, EvidenceBundle, PaymentRecord
from test_guardian_api import H, proposal


def _pay(api, **kw):
    r = api.post("/v1/proposals", json=proposal(api, **kw), headers=H)
    assert r.status_code == 200
    return r.json()["payment"]


def test_list_get_and_filters(api):
    a = _pay(api)
    b = _pay(api, unit=1000)
    got = api.get(f"/v1/payments/{a['id']}", headers=H)
    assert got.status_code == 200 and PaymentRecord.model_validate(got.json()).status == "PAID"
    assert [p["id"] for p in api.get("/v1/payments", headers=H).json()] == [a["id"], b["id"]]
    q = api.get("/v1/payments", params={"mandate_id": api.mandate_id, "status": "PAID"}, headers=H)
    assert len(q.json()) == 2
    one = api.get("/v1/payments", params={"paypal_order_id": a["paypal_order_id"]}, headers=H)
    assert [p["id"] for p in one.json()] == [a["id"]]
    cap = api.get("/v1/payments", params={"paypal_capture_id": b["paypal_capture_id"]}, headers=H)
    assert [p["id"] for p in cap.json()] == [b["id"]]
    assert api.get("/v1/payments", params={"status": "FAILED"}, headers=H).json() == []
    missing = api.get("/v1/payments/pay_nope", headers=H)
    assert missing.status_code == 404 and ErrorResponse.model_validate(missing.json())


def test_refund_endpoint_once_twice_and_non_paid(api):
    a = _pay(api)
    r1 = api.post(f"/v1/payments/{a['id']}/refund", headers=H)
    assert r1.status_code == 200 and r1.json()["status"] == "REFUNDED"
    r2 = api.post(f"/v1/payments/{a['id']}/refund", headers=H)
    assert r2.status_code == 200 and r2.json() == r1.json()
    assert api.get(f"/v1/mandates/{api.mandate_id}/budget", headers=H).json()["spent_minor"] == 0
    nope = api.post("/v1/payments/pay_nope/refund", headers=H)
    assert nope.status_code == 404


def test_refund_of_a_failed_payment_is_rejected(api, use_fake_paypal):
    use_fake_paypal.fail("create", 422)
    r = api.post("/v1/proposals", json=proposal(api), headers=H).json()
    assert r["payment"]["status"] == "FAILED"
    resp = api.post(f"/v1/payments/{r['payment']['id']}/refund", headers=H)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "PAYMENT_NOT_REFUNDABLE"


def test_evidence_endpoint(api):
    a = _pay(api, variant="size 10")
    r = api.get(f"/v1/evidence/{a['id']}", headers=H)
    assert r.status_code == 200
    bundle = EvidenceBundle.model_validate(r.json())
    assert bundle.verification.ok and bundle.payment.id == a["id"]
    assert "size 10" in bundle.decision.explanation
    assert api.get("/v1/evidence/pay_nope", headers=H).status_code == 404


def test_no_secret_in_any_response(api):
    a = _pay(api)
    texts = [
        api.get("/v1/payments", headers=H).text,
        api.get(f"/v1/evidence/{a['id']}", headers=H).text,
        api.get("/v1/ledger", headers=H).text,
    ]
    for t in texts:
        assert TEST_CARD["number"] not in t  # (a 3-digit cvv would match hashes by chance)


def test_get_payment_includes_failure_reason(api, use_fake_paypal):
    ok = _pay(api)
    assert api.get(f"/v1/payments/{ok['id']}", headers=H).json()["failure_reason"] is None
    use_fake_paypal.fail("create", 422)
    bad = _pay(api, unit=1000)
    got = api.get(f"/v1/payments/{bad['id']}", headers=H).json()
    assert got["status"] == "FAILED" and "INSTRUMENT_DECLINED" in got["failure_reason"]
    assert PaymentRecord.model_validate(got).failure_reason == got["failure_reason"]
    assert TEST_CARD["number"] not in str(got)
