"""Merchant registry: which public key may sign carts for which merchant.

Only ES256 (P-256) public keys are stored, as normalised PEM, whether the caller sent PEM or a
JWK. A (merchant_id, key_id) pair is write-once: re-registering the same key is a no-op, but
registering a different key under an existing key id is refused.

Takeover protection: over the API, a merchant that already has a key can NOT get a second one
(409 MERCHANT_KEY_CONFLICT), because the API has no proof that the caller owns the merchant.
Rotation is an operator action with database access (`register(..., allow_rotation=True)`, used
by scripts/dev_merchant.py): register a new key_id and sign new carts with it. Carts name their
key via `key_id`, so old and new keys coexist.
"""

import re
import sqlite3
from collections.abc import Callable
from datetime import datetime

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from gawah_contract import MerchantKey
from gawah_contract.carts import load_public_key

from .db import connect, write_tx
from .errors import GawahError
from .ledger_service import format_ts, utcnow

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
ALG = "ES256"


def _row_to_key(row: sqlite3.Row) -> MerchantKey:
    return MerchantKey(
        merchant_id=row["merchant_id"],
        key_id=row["key_id"],
        alg=row["alg"],
        public_key_pem=row["public_key_pem"],
    )


class MerchantService:
    def __init__(self, path: str, clock: Callable[[], datetime] = utcnow):
        self.path, self.clock = path, clock

    def register(self, body: MerchantKey, *, allow_rotation: bool = False) -> MerchantKey:
        if body.alg.upper() != ALG:
            raise GawahError(422, "VALIDATION_ERROR", f"alg must be {ALG} (EC P-256 keys only)")
        for name in ("merchant_id", "key_id"):
            if not _ID_RE.match(getattr(body, name)):
                raise GawahError(422, "VALIDATION_ERROR", f"{name} has invalid characters or length")
        try:
            key = load_public_key(body.public_key_pem)
        except ValueError as e:
            raise GawahError(422, "VALIDATION_ERROR", str(e)) from e
        pem = key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()
        stored = MerchantKey(merchant_id=body.merchant_id, key_id=body.key_id, alg=ALG, public_key_pem=pem)
        with write_tx(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM merchants WHERE merchant_id = ? AND key_id = ?",
                (body.merchant_id, body.key_id),
            ).fetchone()
            if row is not None:
                if row["public_key_pem"] != pem:
                    raise GawahError(
                        409,
                        "KEY_ID_CONFLICT",
                        f"Key id {body.key_id!r} is already registered with a different key; "
                        "use a new key_id to rotate",
                    )
                return _row_to_key(row)
            if not allow_rotation and conn.execute(
                "SELECT 1 FROM merchants WHERE merchant_id = ? LIMIT 1", (body.merchant_id,)
            ).fetchone():
                raise GawahError(
                    409,
                    "MERCHANT_KEY_CONFLICT",
                    f"Merchant {body.merchant_id!r} already has a registered key; "
                    "key rotation needs operator action",
                )
            conn.execute(
                "INSERT INTO merchants (merchant_id, key_id, alg, public_key_pem, registered_at)"
                " VALUES (?,?,?,?,?)",
                (body.merchant_id, body.key_id, ALG, pem, format_ts(self.clock())),
            )
        return stored

    def get(self, merchant_id: str, key_id: str | None = None) -> MerchantKey | None:
        """The named key, or the most recently registered key when `key_id` is None."""
        conn = connect(self.path)
        try:
            if key_id is None:
                row = conn.execute(
                    "SELECT * FROM merchants WHERE merchant_id = ? ORDER BY rowid DESC LIMIT 1",
                    (merchant_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM merchants WHERE merchant_id = ? AND key_id = ?",
                    (merchant_id, key_id),
                ).fetchone()
        finally:
            conn.close()
        return _row_to_key(row) if row else None

    @staticmethod
    def public_key(mk: MerchantKey) -> ec.EllipticCurvePublicKey:
        return load_public_key(mk.public_key_pem)
