"""Operator tool: put the shopper's demo-merchant public key into the engine's database.

    uv run python scripts/register_merchant_key.py            # register the current key (idempotent)
    uv run python scripts/register_merchant_key.py --rotate   # make a NEW key id first, then register it

Why it exists: the API registers a merchant key once and never replaces it (409 MERCHANT_KEY_CONFLICT;
CONTRACT.md "Signing carts"). If the engine's database remembers an older shopper key (the shopper's
key directory was deleted, or the engine was set up with another one), integration/e2e.py stops and
points here. `--rotate` asks the shopper's own signer for a new key id (demo-merchant-cart-N+1) and
registers it with operator rights, so the running shopper (it reads the active key on every cart)
and the engine agree again. Only the public key is written or printed (as a short fingerprint).

Uses the engine's DATABASE_URL (environment or .env, default ./gawah.db) and the shopper's
MERCHANT_SIGNING_KEY_PATH (default keys/system1-merchant, gitignored). SQLite is shared with a running
engine, so it does not need to be stopped.
"""

import argparse
import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MERCHANT_SIGNING_KEY_PATH", str(ROOT / "keys" / "system1-merchant"))

from gawah_engine.config import settings
from gawah_engine.db import db_path, init_db
from gawah_engine.merchant_service import MerchantService
from shopper.config import Settings
from shopper.merchant.demo_merchant import DemoMerchantAdapter

from integration.scenarios import MERCHANT_ID


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rotate", action="store_true", help="create a new key id before registering")
    args = ap.parse_args()
    adapter = DemoMerchantAdapter(Settings())
    if args.rotate:
        # an operator action on the shopper's own key directory (no public API exists for it yet)
        new_id = adapter._signer.rotate()
        print(f"shopper key rotated: now signing with {new_id}")
    key = adapter.merchant_key(MERCHANT_ID)
    path = db_path(settings.database_url)
    init_db(path)
    MerchantService(path).register(key, allow_rotation=True)
    fp = hashlib.sha256(key.public_key_pem.strip().encode()).hexdigest()[:12]
    print(f"registered {MERCHANT_ID} key {key.key_id} (fingerprint {fp}) in {settings.database_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
