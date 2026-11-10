"""Create and register the demo merchant ("demo-shoe-store"), and sign carts with its key.

    uv run python scripts/dev_merchant.py                      # register in the local database
    uv run python scripts/dev_merchant.py --url http://localhost:8000   # register via the API

The private key is written to ./keys/demo-shoe-store-key-1.pem (gitignored, owner-only access)
the first time and reused after that. Only the public key is registered. Reuse in System 1 / tests:

    from dev_merchant import load_or_create_key, sign_cart
    cart = sign_cart(cart_dict, load_or_create_key())

`sign_cart` is `gawah_contract.carts.sign_cart`, the same code the Guardian verifies with.
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from gawah_contract import MerchantKey, SignedCart
from gawah_contract.carts import generate_keypair, public_key_pem
from gawah_contract.carts import sign_cart as _sign_cart
from gawah_engine.key_permissions import secure_private_key_directory, secure_private_key_file

MERCHANT_ID = "demo-shoe-store"
KEY_ID = "demo-shoe-store-key-1"
DEFAULT_KEY_DIR = Path("keys")


def load_or_create_key(key_dir: Path | str = DEFAULT_KEY_DIR) -> ec.EllipticCurvePrivateKey:
    path = Path(key_dir) / f"{KEY_ID}.pem"
    if path.exists():
        secure_private_key_directory(path.parent)
        secure_private_key_file(path)
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        assert isinstance(key, ec.EllipticCurvePrivateKey)
        return key
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    secure_private_key_directory(path.parent)
    key = generate_keypair()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        secure_private_key_file(path)
    except (OSError, RuntimeError):
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    return key


def merchant_key_body(private_key: ec.EllipticCurvePrivateKey) -> MerchantKey:
    return MerchantKey(
        merchant_id=MERCHANT_ID, key_id=KEY_ID, alg="ES256", public_key_pem=public_key_pem(private_key)
    )


def sign_cart(
    cart: SignedCart | dict[str, Any],
    private_key: ec.EllipticCurvePrivateKey,
    key_id: str = KEY_ID,
) -> SignedCart:
    """Sign a cart as the demo store. Fills `key_id` and `signature`."""
    return _sign_cart(cart, private_key, key_id)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", help="register through a running engine instead of the local DB")
    ap.add_argument("--key-dir", default=str(DEFAULT_KEY_DIR))
    args = ap.parse_args()
    key = load_or_create_key(args.key_dir)
    body = merchant_key_body(key)
    if args.url:
        import httpx
        from gawah_engine.config import settings

        service_key = settings.system1_key  # merchants are registered with the System 1 key
        r = httpx.post(
            f"{args.url.rstrip('/')}/v1/merchants",
            json=body.model_dump(),
            headers={"X-Gawah-Key": service_key},
            timeout=10,
        )
        r.raise_for_status()
    else:
        from gawah_engine.config import settings
        from gawah_engine.services import get_services

        get_services().merchants.register(body, allow_rotation=True)  # operator path
        print(f"database: {settings.database_url}")
    print(f"registered {MERCHANT_ID} key {KEY_ID} (private key stays in {args.key_dir}/)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
