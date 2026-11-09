"""Attack scenarios against the real API (fake PayPal, temp database)."""

from datetime import timedelta

import pytest
from conftest import GUARDIAN_KEY, KEY_S1, KEY_S3
from gawah_contract.carts import generate_keypair, public_key_pem, sign_cart
from test_guardian_api import H, _iso, proposal


def decide(api, p, expect=200):
    r = api.post("/v1/proposals", json=p, headers=H)
    assert r.status_code == expect, r.text
    return r.json()


def signed(api, key=GUARDIAN_KEY, key_id="demo-shoe-store-key-1", merchant="demo-shoe-store", n=0):
    return sign_cart(
        {
            "cart_id": f"cart_sec_{n}", "merchant_id": merchant,
            "line_items": [{"sku": "SHOE-BLK-RUN", "title": "Black Runner", "variant": "size 9",
                            "quantity": 1, "unit_price_minor": 6500}],
            "total_minor": 6500, "currency": "USD", "expires_at": _iso(timedelta(hours=1)),
            "signature": "", "key_id": "",
        },
        key, key_id,
    ).model_dump(mode="json")


def with_cart(api, cart, pid, **over):
    p = proposal(api, pid=pid)
    p["signed_cart"] = cart
    p.update(over)
    return p


def second_mandate(api):
    r = api.post("/v1/mandates", headers=H, json={
        "user_id": "u2", "category": "footwear", "item_description": "Black running shoes, size 9",
        "max_amount_minor": 8000, "currency": "USD", "allowed_merchants": ["demo-shoe-store"],
        "expires_at": _iso(timedelta(days=7))})
    assert r.status_code == 201
    return r.json()["id"]


# 1. replay -----------------------------------------------------------------------------------


def test_replaying_an_allowed_proposal_with_a_modified_body_is_409(api):
    p = proposal(api)
    first = decide(api, p)
    assert first["decision"]["verdict"] == "ALLOW"
    for change in ({"unit_price_minor": 1}, {"quantity": 2}, {"rationale": "different"},
                   {"mandate_id": second_mandate(api)}, {"agent_id": "someone-else"}):
        r = api.post("/v1/proposals", json={**p, **change}, headers=H)
        assert r.status_code == 409 and r.json()["error"]["code"] == "PROPOSAL_ID_CONFLICT", change
    assert decide(api, p) == first  # the honest replay is still idempotent
    assert len(api.get("/v1/payments", headers=H).json()) == 1


# 2. one signed cart, two mandates ----------------------------------------------------------------


def test_a_signed_cart_cannot_be_paid_again_under_a_different_mandate(api, use_fake_paypal):
    cart = signed(api, n=1)
    other = second_mandate(api)
    a = decide(api, with_cart(api, cart, "prop_sec_a"))
    assert a["payment"]["status"] == "PAID"
    b = decide(api, with_cart(api, cart, "prop_sec_b", mandate_id=other))
    assert b["decision"]["verdict"] == "BLOCK" and "DUPLICATE" in b["decision"]["reasons"]
    assert b["payment"] is None
    assert len(use_fake_paypal.calls("create")) == 1
    assert api.get(f"/v1/mandates/{other}/budget", headers=H).json()["spent_minor"] == 0


def test_a_failed_payment_does_not_burn_the_cart_for_other_mandates(api, use_fake_paypal):
    cart = signed(api, n=2)
    other = second_mandate(api)
    use_fake_paypal.fail("create", 422)
    assert decide(api, with_cart(api, cart, "prop_sec_c"))["payment"]["status"] == "FAILED"
    ok = decide(api, with_cart(api, cart, "prop_sec_d", mandate_id=other))
    assert ok["decision"]["verdict"] == "ALLOW" and ok["payment"]["status"] == "PAID"


# 3-4. broken signatures ----------------------------------------------------------------------


@pytest.mark.parametrize("mutate", [
    lambda c: c.update(signature=""),
    lambda c: c.update(signature=c["signature"][:20]),
    lambda c: c.update(signature=c["signature"][:-4] + ("AAAA" if not c["signature"].endswith("AAAA") else "BBBB")),
    lambda c: c.update(signature="not-base64-!!!"),
    lambda c: c.update(total_minor=1),  # signed content changed after signing
    lambda c: c["line_items"][0].update(unit_price_minor=100),
], ids=["stripped", "truncated", "flipped", "garbage", "total-edited", "price-edited"])
def test_tampered_or_missing_cart_signatures_block_and_never_pay(api, use_fake_paypal, mutate):
    cart = signed(api, n=3)
    mutate(cart)
    p = with_cart(api, cart, "prop_sec_sig")
    if cart["total_minor"] == 1:
        p["unit_price_minor"] = 1
    r = decide(api, p)
    assert r["decision"]["verdict"] == "BLOCK" and r["payment"] is None
    assert use_fake_paypal.requests == []
    assert api.get(f"/v1/mandates/{api.mandate_id}/budget", headers=H).json()["reserved_minor"] == 0


def test_signature_made_with_an_unknown_key_id_is_blocked(api, use_fake_paypal):
    cart = signed(api, key_id="demo-shoe-store-key-99", n=4)  # genuine key, unregistered key id
    r = decide(api, with_cart(api, cart, "prop_sec_kid"))
    assert r["decision"]["verdict"] == "BLOCK" and "CART_SIGNATURE_INVALID" in r["decision"]["reasons"]
    assert use_fake_paypal.requests == []


def test_a_cart_signed_with_an_attackers_own_key_is_blocked(api, use_fake_paypal):
    cart = signed(api, key=generate_keypair(), n=5)  # right key_id, wrong private key
    r = decide(api, with_cart(api, cart, "prop_sec_own"))
    assert "CART_SIGNATURE_INVALID" in r["decision"]["reasons"] and r["payment"] is None


# 5. merchant takeover -------------------------------------------------------------------------


def test_attacker_cannot_register_a_different_key_for_an_existing_merchant(api, use_fake_paypal):
    attacker = generate_keypair()
    for key_id in ("demo-shoe-store-key-1", "demo-shoe-store-key-2"):  # same id, and a "rotation"
        r = api.post("/v1/merchants", headers=H, json={
            "merchant_id": "demo-shoe-store", "key_id": key_id, "alg": "ES256",
            "public_key_pem": public_key_pem(attacker)})
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] in ("KEY_ID_CONFLICT", "MERCHANT_KEY_CONFLICT")
    cart = signed(api, key=attacker, key_id="demo-shoe-store-key-2", n=6)
    r = decide(api, with_cart(api, cart, "prop_sec_take"))
    assert r["decision"]["verdict"] == "BLOCK" and r["payment"] is None
    again = api.post("/v1/merchants", headers=H, json={  # the legitimate key re-registers fine
        "merchant_id": "demo-shoe-store", "key_id": "demo-shoe-store-key-1", "alg": "ES256",
        "public_key_pem": public_key_pem(GUARDIAN_KEY)})
    assert again.status_code == 201


# 6. merchant A's cart under merchant B's id -------------------------------------------------------


def test_cart_signed_by_merchant_a_cannot_be_used_with_merchant_b(api, use_fake_paypal):
    b_key = generate_keypair()
    for mid in ("evil-store",):
        assert api.post("/v1/merchants", headers=H, json={
            "merchant_id": mid, "key_id": f"{mid}-key-1", "alg": "ES256",
            "public_key_pem": public_key_pem(b_key)}).status_code == 201
    cart_a = signed(api, n=7)  # merchant A = demo-shoe-store
    # proposal claims merchant B but carries A's cart
    r = decide(api, with_cart(api, cart_a, "prop_sec_x1", merchant_id="evil-store"))
    assert r["decision"]["verdict"] == "BLOCK" and r["payment"] is None
    assert "MERCHANT_MISMATCH" in r["decision"]["reasons"]
    # cart rewritten to say merchant B but still carrying A's signature
    forged = dict(cart_a, merchant_id="evil-store")
    r = decide(api, with_cart(api, forged, "prop_sec_x2", merchant_id="evil-store"))
    assert r["decision"]["verdict"] == "BLOCK" and r["payment"] is None
    assert use_fake_paypal.requests == []


# 7. refund with the shopper's key -------------------------------------------------------------------


def test_refund_with_the_shopper_key_is_403_and_changes_nothing(api, use_fake_paypal):
    paid = decide(api, proposal(api))["payment"]
    r = api.post(f"/v1/payments/{paid['id']}/refund", headers={"X-Gawah-Key": KEY_S1})
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"
    assert use_fake_paypal.calls("refund") == []
    after = api.get(f"/v1/payments/{paid['id']}", headers={"X-Gawah-Key": KEY_S3}).json()
    assert after["status"] == "PAID"
    assert api.get(f"/v1/mandates/{api.mandate_id}/budget", headers=H).json()["spent_minor"] == 6500
    ok = api.post(f"/v1/payments/{paid['id']}/refund", headers={"X-Gawah-Key": KEY_S3})
    assert ok.status_code == 200 and ok.json()["status"] == "REFUNDED"  # only System 3 can


def test_dashboard_key_cannot_create_proposals(api):
    r = api.post("/v1/proposals", json=proposal(api), headers={"X-Gawah-Key": KEY_S3})
    assert r.status_code == 403
    assert api.get("/v1/payments", headers=H).json() == []


# 8. revoke while a proposal is in flight -------------------------------------------------------------


def test_mandate_revoked_while_a_proposal_is_in_flight_is_blocked(api, use_fake_paypal):
    # the revoke lands in the middle of the semantic check, after evaluation began
    from gawah_engine.services import get_services

    svc = get_services()

    class RevokeInFlight:
        def check(self, mandate, cart):
            api.post(f"/v1/mandates/{mandate.id}/revoke", headers=H)
            from gawah_engine.semantic import SemanticResult

            return SemanticResult(category_match="match", confidence=1.0, source="model")

        def status(self):
            return {"semantic": "groq", "model": "x", "checked": True}

    svc.guardian.semantic = RevokeInFlight()
    r = decide(api, proposal(api))
    assert r["decision"]["verdict"] == "BLOCK" and r["decision"]["reasons"] == ["MANDATE_REVOKED"]
    assert r["payment"] is None and use_fake_paypal.requests == []
    assert api.get(f"/v1/mandates/{api.mandate_id}/budget", headers=H).json()["reserved_minor"] == 0
