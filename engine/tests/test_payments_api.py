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


# --- GuardianDecision.mandate_id (issue #16: Counsel reads decision.payload["mandate_id"]) ------


def test_every_decision_carries_the_proposals_mandate_allow_and_block(api):
    from gawah_contract import GuardianDecision

    allowed = api.post("/v1/proposals", json=proposal(api), headers=H).json()
    blocked = api.post("/v1/proposals", json=proposal(api, unit=999_00), headers=H).json()
    assert allowed["decision"]["verdict"] == "ALLOW" and blocked["decision"]["verdict"] == "BLOCK"
    for result in (allowed, blocked):
        assert GuardianDecision.model_validate(result["decision"]).mandate_id == api.mandate_id
    # stored in the ledger's DECISION_MADE data
    for result in (allowed, blocked):
        entries = api.get("/v1/ledger", params={"subject_id": result["decision"]["id"]}, headers=H).json()
        made = [e for e in entries if e["type"] == "DECISION_MADE"]
        assert made and made[0]["data"]["mandate_id"] == api.mandate_id
    # and returned in the evidence bundle under the name Counsel reads
    a = allowed["payment"]
    body = api.get(f"/v1/evidence/{a['id']}", headers=H).json()
    assert body["decision"]["mandate_id"] == api.mandate_id


def test_decisions_stored_before_mandate_id_existed_still_load():
    import json
    from pathlib import Path

    from gawah_contract import EvidenceBundle, GuardianDecision

    fixtures = Path(__file__).resolve().parents[2] / "contract" / "fixtures"
    old = json.loads((fixtures / "decision_allow.json").read_text())
    assert "mandate_id" not in old
    assert GuardianDecision.model_validate(old).mandate_id is None
    assert GuardianDecision.model_validate_json(json.dumps(old)).verdict == "ALLOW"
    bundle = json.loads((fixtures / "evidence_bundle.json").read_text())
    assert "mandate_id" not in bundle["decision"]
    assert EvidenceBundle.model_validate(bundle).decision.mandate_id is None


def test_a_full_refund_restores_the_budget_and_writes_a_ledger_entry(api):
    pay = _pay(api, unit=5500)
    budget_url = f"/v1/mandates/{api.mandate_id}/budget"
    before = api.get(budget_url, headers=H).json()
    assert before["spent_minor"] == 5500
    refunded = api.post(f"/v1/payments/{pay['id']}/refund", json=None, headers=H).json()
    assert refunded["status"] == "REFUNDED"
    after = api.get(budget_url, headers=H).json()
    assert after["spent_minor"] == 0 and after["reserved_minor"] == 0
    entries = api.get("/v1/ledger", params={"subject_id": pay["id"]}, headers=H).json()
    refund_entries = [e for e in entries if e["type"] == "REFUNDED"]
    assert len(refund_entries) == 1
    assert refund_entries[0]["data"]["amount_minor"] == 5500
    assert refund_entries[0]["data"]["mandate_id"] == api.mandate_id
    # a repeat refund is a no-op: no second entry, budget unchanged
    api.post(f"/v1/payments/{pay['id']}/refund", headers=H)
    again = api.get("/v1/ledger", params={"subject_id": pay["id"]}, headers=H).json()
    assert len([e for e in again if e["type"] == "REFUNDED"]) == 1
    assert api.get("/v1/ledger/verify", headers=H).json()["ok"] is True


def test_refund_demo_script_refunds_the_last_paid_payment_and_reports_it(api):
    """scripts/refund_demo.py against the in-process engine (fake PayPal)."""
    import importlib.util
    import re
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "refund_demo.py"
    spec = importlib.util.spec_from_file_location("refund_demo", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _pay(api, unit=2500)
    last = _pay(api, unit=5500, variant="size 11")
    lines: list[str] = []
    code = mod.main([], env={"SYSTEM3_KEY": H["X-Gawah-Key"]}, client=api, out=lines.append)
    text = "\n".join(lines)
    assert code == 0, text
    assert "Counsel recommended CONCEDE_REMEDY" in text and "FAKE PayPal" in text
    assert re.search(r"before (\d+)c -> after (\d+)c", text)
    spent_before, spent_after = map(int, re.search(r"before (\d+)c -> after (\d+)c", text).groups())
    assert spent_before - spent_after == 5500, text
    assert "['REFUNDED']" in text and "ok=True" in text
    assert api.get(f"/v1/payments/{last['id']}", headers=H).json()["status"] == "REFUNDED"
    assert last["id"] not in text  # ids are masked (the fake PayPal's 7-character ids are too short to)
