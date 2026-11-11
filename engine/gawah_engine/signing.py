"""ES256 signing behind a small `Signer` interface.

Why not the Google AP2 SDK: see spikes/spikes.md (C1). The format here is "AP2-inspired ES256
signed JSON": ECDSA P-256 + SHA-256 over canonical JSON (`canonical.py`), signature excluded,
encoded as base64url(r || s) without padding (the JWS ES256 layout).

Keys live in a directory outside git (default ./keys/, gitignored), one PEM per key id:
`<prefix>-<n>.pem` plus an `<prefix>.active` pointer. They are generated on first run. To rotate,
call `rotate()`: new signatures use the new key, old signatures still verify because every
signature carries its key_id. Private keys are never logged or returned.
"""

import base64
import os
import re
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

from .canonical import canonical_json
from .key_permissions import secure_private_key_directory, secure_private_key_file

ALG = "ES256"
DEFAULT_KEY_DIR = "./keys"
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

Payload = Mapping[str, Any] | str | bytes


class Signer(Protocol):
    def sign(self, payload: Payload) -> tuple[str, str]:
        """Return (signature, key_id)."""

    def verify(self, payload: Payload, signature: str, key_id: str) -> bool: ...


def _payload_bytes(payload: Payload) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return canonical_json(payload, exclude=("signature",))


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class ES256Signer:
    """File-backed ES256 signer with key ids and rotation. One instance per purpose."""

    alg = ALG

    def __init__(self, key_dir: str | os.PathLike[str] | None, prefix: str):
        if not _KEY_ID_RE.match(prefix):
            raise ValueError("invalid key prefix")
        self.prefix = prefix
        self.dir = Path(key_dir or DEFAULT_KEY_DIR)
        if self.dir.exists() and not self.dir.is_dir():
            raise ValueError(f"signing key path {self.dir} must be a directory")
        self._lock = threading.Lock()
        self._cache: dict[str, ec.EllipticCurvePrivateKey] = {}
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        secure_private_key_directory(self.dir)
        for existing_key in self.dir.glob("*.pem"):
            secure_private_key_file(existing_key)
        with self._lock:
            if not self._active_pointer().exists():
                try:
                    self._create_key(1)
                except FileExistsError:
                    # another engine process is creating the first key right now: wait for it
                    for _ in range(100):
                        if self._active_pointer().exists() and self._active_pointer().read_text().strip():
                            break
                        time.sleep(0.05)
                    else:
                        raise

    # -- key files ---------------------------------------------------------------------
    def _active_pointer(self) -> Path:
        return self.dir / f"{self.prefix}.active"

    def _key_path(self, key_id: str) -> Path:
        return self.dir / f"{key_id}.pem"

    def _create_key(self, n: int) -> str:
        key_id = f"{self.prefix}-{n}"
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        # O_EXCL: never overwrite an existing key; mode 0600 from the start.
        path = self._key_path(key_id)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            secure_private_key_file(path)
        except (OSError, RuntimeError):
            os.close(fd)
            path.unlink(missing_ok=True)
            raise
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        tmp = self._active_pointer().with_suffix(".tmp")
        tmp.write_text(key_id)
        os.replace(tmp, self._active_pointer())  # atomic: readers never see a half-written pointer
        self._cache[key_id] = key
        return key_id

    def _load(self, key_id: str) -> ec.EllipticCurvePrivateKey | None:
        if not _KEY_ID_RE.match(key_id) or not key_id.startswith(f"{self.prefix}-"):
            return None
        with self._lock:
            if key_id not in self._cache:
                path = self._key_path(key_id)
                if not path.is_file():
                    return None
                key = serialization.load_pem_private_key(path.read_bytes(), password=None)
                if not isinstance(key, ec.EllipticCurvePrivateKey):
                    return None
                self._cache[key_id] = key
            return self._cache[key_id]

    @property
    def active_key_id(self) -> str:
        return self._active_pointer().read_text().strip()

    def rotate(self) -> str:
        """Create a new key, make it the active one, and return its key id."""
        with self._lock:
            n = 1 + max(
                (int(p.stem.rsplit("-", 1)[1]) for p in self.dir.glob(f"{self.prefix}-*.pem")),
                default=0,
            )
            return self._create_key(n)

    # -- Signer interface --------------------------------------------------------------
    def sign(self, payload: Payload) -> tuple[str, str]:
        key_id = self.active_key_id
        key = self._load(key_id)
        if key is None:
            raise RuntimeError(f"active signing key {key_id} is missing from {self.dir}")
        r, s = decode_dss_signature(key.sign(_payload_bytes(payload), ec.ECDSA(hashes.SHA256())))
        return _b64e(r.to_bytes(32, "big") + s.to_bytes(32, "big")), key_id

    def verify(self, payload: Payload, signature: str, key_id: str) -> bool:
        try:
            key = self._load(key_id)
            raw = _b64d(signature)
            if key is None or len(raw) != 64:
                return False
            der = encode_dss_signature(
                int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
            )
            key.public_key().verify(der, _payload_bytes(payload), ec.ECDSA(hashes.SHA256()))
            return True
        except (InvalidSignature, ValueError, TypeError):
            return False
