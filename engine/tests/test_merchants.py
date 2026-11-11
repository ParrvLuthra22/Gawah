import base64
import importlib.util
import json
from pathlib import Path

import pytest
from conftest import KEY_S1, KEY_S3, AutoKeyClient
from cryptography.hazmat.primitives import serialization
from gawah_contract import MerchantKey
from gawah_contract.carts import generate_keypair, public_key_pem, verify_cart
from gawah_contract.fixtures import load
from gawah_engine.config import settings
from gawah_engine.errors import GawahError
from gawah_engine.key_permissions import private_key_path_is_secure
from gawah_engine.main import app

H = {"X-Gawah-Key": "real-key"}


def _body(key=None, **over):
    key = key or generate_keypair()
    d = {"merchant_id": "m1", "key_id": "m1-key-1", "alg": "ES256", "public_key_pem": public_key_pem(key)}
    d.update(over)
    return MerchantKey(**d)


def test_register_and_get_pem(svc):
    key = generate_keypair()
    out = svc.merchants.register(_body(key))
    assert out.alg == "ES256" and out.public_key_pem.startswith("-----BEGIN PUBLIC KEY-----")
    assert svc.merchants.get("m1") == out
    assert svc.merchants.get("m1", "m1-key-1") == out
    assert svc.merchants.get("nope") is None and svc.merchants.get("m1", "other") is None


def test_register_jwk(svc):
    n = generate_keypair().public_key().public_numbers()

    def b64(i):
        return base64.urlsafe_b64encode(i.to_bytes(32, "big")).rstrip(b"=").decode()

    jwk = json.dumps({"kty": "EC", "crv": "P-256", "x": b64(n.x), "y": b64(n.y)})
    out = svc.merchants.register(_body(public_key_pem=jwk))
    assert out.public_key_pem.startswith("-----BEGIN PUBLIC KEY-----")
    assert svc.merchants.public_key(out).public_numbers() == n


def test_invalid_registrations_are_422(svc):
    private_pem = generate_keypair().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    for over in (
        {"alg": "ed25519"},
        {"public_key_pem": "garbage"},
        {"public_key_pem": private_pem},
        {"merchant_id": "../x"},
        {"key_id": ""},
    ):
        with pytest.raises(GawahError) as e:
            svc.merchants.register(_body(**over))
        assert e.value.status == 422, over


def test_key_id_is_write_once_but_idempotent_and_rotatable(svc):
    k1, k2 = generate_keypair(), generate_keypair()
    a = svc.merchants.register(_body(k1))
    assert svc.merchants.register(_body(k1)) == a  # same key again: fine
    with pytest.raises(GawahError) as e:
        svc.merchants.register(_body(k2))  # same key id, different key
    assert e.value.status == 409 and e.value.code == "KEY_ID_CONFLICT"
    with pytest.raises(GawahError) as e2:  # the API path: a second key for the merchant is refused
        svc.merchants.register(_body(k2, key_id="m1-key-2"))
    assert e2.value.status == 409 and e2.value.code == "MERCHANT_KEY_CONFLICT"
    b = svc.merchants.register(_body(k2, key_id="m1-key-2"), allow_rotation=True)  # operator
    assert svc.merchants.get("m1") == b  # latest wins when no key id is given
    assert svc.merchants.get("m1", "m1-key-1") == a  # old key still resolvable by id


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "gawah_mock", False)
    monkeypatch.setattr(settings, "system1_key", KEY_S1)
    monkeypatch.setattr(settings, "system3_key", KEY_S3)
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'm.db'}")
    monkeypatch.setattr(settings, "ledger_signing_key_path", str(tmp_path / "keys"))
    monkeypatch.setattr(settings, "mandate_signing_key_path", str(tmp_path / "keys"))
    with AutoKeyClient(app) as c:
        yield c


def test_merchant_api(api):
    b = _body().model_dump()
    r = api.post("/v1/merchants", json=b, headers=H)
    assert r.status_code == 201 and r.json()["merchant_id"] == "m1"
    assert api.get("/v1/merchants/m1/public-key", headers=H).json()["key_id"] == "m1-key-1"
    r = api.get("/v1/merchants/nope/public-key", headers=H)
    assert r.status_code == 404 and r.json()["error"]["code"] == "MERCHANT_NOT_FOUND"
    bad = api.post("/v1/merchants", json={**b, "public_key_pem": "x"}, headers=H)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "VALIDATION_ERROR"
    other = api.post("/v1/merchants", json=_body().model_dump(), headers=H)
    assert other.status_code == 409


def _load_dev_merchant():
    path = Path(__file__).resolve().parents[2] / "scripts" / "dev_merchant.py"
    spec = importlib.util.spec_from_file_location("dev_merchant", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dev_merchant_key_is_private_reused_and_signs_carts(tmp_path, svc):
    dm = _load_dev_merchant()
    key = dm.load_or_create_key(tmp_path / "k")
    pem = tmp_path / "k" / "demo-shoe-store-key-1.pem"
    assert private_key_path_is_secure(pem)
    assert private_key_path_is_secure(pem.parent, directory=True)
    assert dm.load_or_create_key(tmp_path / "k").private_numbers() == key.private_numbers()
    svc.merchants.register(dm.merchant_key_body(key))
    stored = svc.merchants.get("demo-shoe-store", "demo-shoe-store-key-1")
    cart = dm.sign_cart(load("signed_cart_shoes"), key)
    assert verify_cart(cart, svc.merchants.public_key(stored))
