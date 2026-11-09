"""Sign and verify carts the same way the Guardian does (ES256, ECDSA P-256 + SHA-256).

Stores (System 1) call `sign_cart` with their private key; the Guardian calls `verify_cart`
with the public key the store registered at POST /v1/merchants. What is signed: the canonical
JSON (sorted keys, no whitespace, ASCII-escaped) of the cart after it has been validated as a
`SignedCart`, with the `signature` field removed. `key_id` IS part of the signed bytes.
Signature encoding: base64url(r || s), 64 bytes, no padding (the JWS ES256 layout).
"""

import base64
import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

from .models import SignedCart


def generate_keypair() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def public_key_pem(private_key: ec.EllipticCurvePrivateKey) -> str:
    return (
        private_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def load_public_key(text: str) -> ec.EllipticCurvePublicKey:
    """Parse a P-256 public key given as PEM or as a JWK JSON string. Raises ValueError."""
    text = text.strip()
    try:
        if text.startswith("{"):
            jwk = json.loads(text)
            if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
                raise ValueError("JWK must have kty=EC and crv=P-256")
            if "d" in jwk:
                raise ValueError("JWK contains a private component; send the public key only")
            x, y = (int.from_bytes(_b64d(jwk[k]), "big") for k in ("x", "y"))
            key: Any = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
        else:
            key = serialization.load_pem_public_key(text.encode())
    except (KeyError, TypeError, ValueError) as e:  # JSONDecodeError is a ValueError
        raise ValueError(f"not a valid public key: {e}") from e
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise ValueError("public key must be an EC P-256 key")
    return key


def cart_signing_bytes(cart: SignedCart | dict[str, Any]) -> bytes:
    """The exact bytes that are signed."""
    model = cart if isinstance(cart, SignedCart) else SignedCart.model_validate(cart)
    payload = model.model_dump(mode="json")
    payload.pop("signature")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sign_cart(
    cart: SignedCart | dict[str, Any], private_key: ec.EllipticCurvePrivateKey, key_id: str
) -> SignedCart:
    """Return the cart with `key_id` and `signature` filled in. Any existing signature is ignored."""
    data = cart.model_dump(mode="json") if isinstance(cart, SignedCart) else dict(cart)
    data.update(key_id=key_id, signature="")
    unsigned = SignedCart.model_validate(data)
    r, s = decode_dss_signature(
        private_key.sign(cart_signing_bytes(unsigned), ec.ECDSA(hashes.SHA256()))
    )
    sig = _b64e(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return unsigned.model_copy(update={"signature": sig})


def verify_cart(cart: SignedCart | dict[str, Any], public_key: ec.EllipticCurvePublicKey) -> bool:
    try:
        model = cart if isinstance(cart, SignedCart) else SignedCart.model_validate(cart)
        raw = _b64d(model.signature)
        if len(raw) != 64:
            return False
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        public_key.verify(der, cart_signing_bytes(model), ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
