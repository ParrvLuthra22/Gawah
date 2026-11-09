"""Canonical JSON: the one byte encoding used for every hash and signature in the engine.

Rules: object keys sorted, no whitespace (`,` and `:` separators), UTF-8 bytes. Non-ASCII
characters are written as \\uXXXX escapes (`ensure_ascii=True`), which is exactly how
`gawah_contract.hashing.compute_entry_hash` serialises, so ledger hashes stay verifiable with
the shared contract helper. Floats are rejected everywhere: money is integer minor units.
"""

import json
from collections.abc import Mapping
from typing import Any


def _check(value: Any, path: str = "$") -> None:
    if isinstance(value, float):
        raise TypeError(f"floats are not allowed in canonical JSON (at {path}); use integer minor units")
    if isinstance(value, Mapping):
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError(f"object keys must be strings (at {path}): {k!r}")
            _check(v, f"{path}.{k}")
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            _check(v, f"{path}[{i}]")
    elif value is not None and not isinstance(value, str | int | bool):
        raise TypeError(f"{type(value).__name__} is not JSON-serialisable (at {path})")


def canonical_json(value: Any, *, exclude: tuple[str, ...] = ()) -> bytes:
    """Canonical UTF-8 JSON bytes. `exclude` drops those top-level keys (e.g. "signature")."""
    if exclude and isinstance(value, Mapping):
        value = {k: v for k, v in value.items() if k not in exclude}
    _check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
