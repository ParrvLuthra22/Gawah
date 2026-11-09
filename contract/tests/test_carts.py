import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from gawah_contract import SignedCart
from gawah_contract.carts import (
    cart_signing_bytes,
    generate_keypair,
    load_public_key,
    public_key_pem,
    sign_cart,
    verify_cart,
)
from gawah_contract.fixtures import load


@pytest.fixture
def key():
    return generate_keypair()


def test_sign_and_verify_round_trip(key):
    cart = sign_cart(load("signed_cart_shoes"), key, "k1")
    assert cart.key_id == "k1" and cart.signature != "mock-signature-cart_01"
    assert verify_cart(cart, key.public_key())
    assert verify_cart(cart.model_dump(mode="json"), key.public_key())


def test_any_edit_after_signing_is_detected(key):
    d = sign_cart(load("signed_cart_shoes"), key, "k1").model_dump(mode="json")
    for edit in (
        lambda c: c.update(total_minor=100),
        lambda c: c["line_items"][0].update(unit_price_minor=1),
        lambda c: c["line_items"][0].update(variant="size 10"),
        lambda c: c.update(merchant_id="evil"),
        lambda c: c.update(key_id="k2"),
        lambda c: c.update(expires_at="2030-01-01T00:00:00Z"),
    ):
        bad = json.loads(json.dumps(d))
        edit(bad)
        assert not verify_cart(bad, key.public_key())


def test_wrong_key_and_garbage_signature(key):
    cart = sign_cart(load("signed_cart_shoes"), key, "k1")
    assert not verify_cart(cart, generate_keypair().public_key())
    for sig in ("", "AAAA", "mock-signature-cart_01"):
        assert not verify_cart(cart.model_copy(update={"signature": sig}), key.public_key())


def test_equivalent_timestamp_spellings_sign_identically(key):
    a = load("signed_cart_shoes")
    b = {**a, "expires_at": "2026-11-09T11:00:00+00:00"}
    assert cart_signing_bytes({**a, "key_id": "k1"}) == cart_signing_bytes({**b, "key_id": "k1"})
    assert verify_cart(sign_cart(b, key, "k1"), key.public_key())


def test_load_public_key_pem_and_jwk(key):
    n = key.public_key().public_numbers()
    assert load_public_key(public_key_pem(key)).public_numbers() == n

    def b64(i):
        return base64.urlsafe_b64encode(i.to_bytes(32, "big")).rstrip(b"=").decode()

    jwk = json.dumps({"kty": "EC", "crv": "P-256", "x": b64(n.x), "y": b64(n.y)})
    assert load_public_key(jwk).public_numbers() == n


def test_load_public_key_rejects_bad_input(key):
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    p384 = ec.generate_private_key(ec.SECP384R1())
    for bad in (
        "",
        "garbage",
        private_pem,
        public_key_pem(p384),
        '{"kty":"EC","crv":"P-256","x":"AA","y":"AA"}',
        '{"kty":"RSA"}',
        '{"kty":"EC","crv":"P-256","x":"AA","y":"AA","d":"AA"}',
        "{not json",
    ):
        with pytest.raises(ValueError):
            load_public_key(bad)


def test_signed_cart_is_a_valid_model(key):
    SignedCart.model_validate(sign_cart(load("signed_cart_shoes"), key, "k1").model_dump())
