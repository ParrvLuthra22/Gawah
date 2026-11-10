"""Hosted-demo bootstrap and reset.

`bootstrap_if_empty()` (GAWAH_BOOTSTRAP=1): if the database holds no ledger entries and no
mandates, create the schema, generate the ledger and mandate signing keys, generate the demo
merchant's key and register its public key. Nothing else is seeded. On a free Render instance the
filesystem is ephemeral, so the database and the keys vanish together and come back together:
every signature in a ledger is made by a key that lives next to that ledger.

Refused on purpose: a database that HAS data but is missing its signing keys. Minting new keys
would silently break verification of the old entries, so the engine stops with an error instead.

`reset_demo()` (POST /v1/admin/reset-demo, GAWAH_ENV=demo only): deletes the database file and the
signing keys, then bootstraps again, so the ledger starts at a new genesis. Single-process only
(the hosted demo runs one uvicorn worker). PayPal sandbox payments already made stay in PayPal.
"""

import logging
import os
import sqlite3
import threading
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from gawah_contract import MerchantKey
from gawah_contract.carts import generate_keypair, public_key_pem

from .config import settings
from .db import db_path
from .errors import GawahError

log = logging.getLogger("gawah.bootstrap")

DEMO_MERCHANT_ID = "demo-shoe-store"
DEMO_MERCHANT_KEY_ID = "demo-shoe-store-key-1"
_LEDGER_PREFIX, _MANDATE_PREFIX = "gawah-ledger-key", "gawah-mandate-key"
_reset_lock = threading.Lock()


def _key_dirs() -> tuple[Path, Path]:
    return (Path(settings.ledger_signing_key_path or "./keys"),
            Path(settings.mandate_signing_key_path or "./keys"))


def _file_path() -> str:
    path = db_path(settings.database_url)
    if path == ":memory:":
        raise GawahError(409, "UNSUPPORTED", "bootstrap and reset need a database file")
    return path


def database_is_empty(path: str) -> bool:
    """True if there is no database yet or it holds no ledger entries and no mandates."""
    if not Path(path).exists():
        return True
    conn = sqlite3.connect(path, timeout=30)
    try:
        counts = []
        for table in ("ledger", "mandates"):
            try:
                counts.append(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except sqlite3.OperationalError:  # table does not exist yet
                counts.append(0)
        return sum(counts) == 0
    finally:
        conn.close()


def _demo_merchant_key(key_dir: Path):
    """Load the demo merchant's private key, or create it (0600, never overwritten)."""
    path = key_dir / f"{DEMO_MERCHANT_KEY_ID}.pem"
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        return key
    key_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = generate_keypair()
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    return key


def _check_keys_present_for_existing_data() -> None:
    ledger_dir, mandate_dir = _key_dirs()
    for d, prefix in ((ledger_dir, _LEDGER_PREFIX), (mandate_dir, _MANDATE_PREFIX)):
        if not (d / f"{prefix}.active").exists():
            raise RuntimeError(
                f"the database has data but the signing key {prefix} is missing from {d}; "
                "refusing to create a new key, which would break verification of the old ledger"
            )


def bootstrap_if_empty() -> dict:
    """Create schema, keys and the demo merchant if the database is empty. Idempotent."""
    from .services import get_services

    path = _file_path()
    empty = database_is_empty(path)
    if not empty:
        _check_keys_present_for_existing_data()
    svc = get_services()  # creates the database, runs migrations, generates missing signing keys
    key = _demo_merchant_key(_key_dirs()[0])
    svc.merchants.register(
        MerchantKey(merchant_id=DEMO_MERCHANT_ID, key_id=DEMO_MERCHANT_KEY_ID, alg="ES256",
                    public_key_pem=public_key_pem(key)),
    )
    log.info("bootstrap: %s database, demo merchant %s registered",
             "new" if empty else "existing", DEMO_MERCHANT_ID)
    return {"created": empty, "merchant_id": DEMO_MERCHANT_ID}


def _forget_key_files() -> None:
    seen = set()
    for d in _key_dirs():
        if d in seen or not d.is_dir():
            continue
        seen.add(d)
        for pattern in (f"{_LEDGER_PREFIX}*", f"{_MANDATE_PREFIX}*", f"{DEMO_MERCHANT_KEY_ID}*"):
            for f in d.glob(pattern):
                if f.is_file():
                    f.unlink()


def reset_demo() -> dict:
    """Wipe the demo data and keys and start a fresh ledger genesis."""
    from .auth import limiter
    from .services import _cached, get_services

    if settings.gawah_mock:
        raise GawahError(409, "MOCK_MODE", "Nothing to reset: the engine is serving mock fixtures")
    path = _file_path()
    with _reset_lock:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(path + suffix).unlink(missing_ok=True)
        _forget_key_files()
        _cached.cache_clear()
        limiter.reset()
        info = bootstrap_if_empty()
        svc = get_services()
        head = svc.ledger.head()
        return {
            "status": "reset",
            "ledger_head_seq": head.seq,
            "ledger_key_id": head.key_id,
            "mandate_key_id": svc.mandates.signer.active_key_id,
            "merchant_id": info["merchant_id"],
        }
