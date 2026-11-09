import pytest
from gawah_contract.hashing import compute_entry_hash
from gawah_engine.canonical import canonical_json


def test_keys_are_sorted_and_no_whitespace():
    assert canonical_json({"b": 1, "a": {"d": [1, 2], "c": None}}) == b'{"a":{"c":null,"d":[1,2]},"b":1}'


def test_key_order_does_not_matter():
    assert canonical_json({"x": 1, "y": 2}) == canonical_json({"y": 2, "x": 1})


def test_unicode_is_deterministic_utf8_bytes():
    out = canonical_json({"name": "Zoë 靴 😀"})
    assert isinstance(out, bytes)
    assert out.decode("utf-8") == '{"name":"Zo\\u00eb \\u9774 \\ud83d\\ude00"}'
    assert out == canonical_json({"name": "Zoë 靴 😀"})


def test_money_must_be_integers():
    assert canonical_json({"amount_minor": 6500}) == b'{"amount_minor":6500}'
    for bad in ({"amount": 65.0}, {"nested": [{"amount": 0.5}]}, {"x": float("nan")}):
        with pytest.raises(TypeError):
            canonical_json(bad)


def test_rejects_non_json_values_and_non_string_keys():
    with pytest.raises(TypeError):
        canonical_json({"d": object()})
    with pytest.raises(TypeError):
        canonical_json({1: "a"})


def test_exclude_drops_top_level_keys_only():
    assert canonical_json({"a": 1, "signature": "s"}, exclude=("signature",)) == b'{"a":1}'


def test_matches_contract_hash_encoding():
    # The ledger hash must be reproducible with the shared contract helper, unicode included.
    import hashlib

    entry = {
        "seq": 1,
        "ts": "2026-11-09T10:00:00Z",
        "type": "MANDATE_CREATED",
        "subject_id": "man_1",
        "data": {"note": "Zoë 靴"},
        "prev_hash": "0" * 64,
    }
    assert hashlib.sha256(canonical_json(entry)).hexdigest() == compute_entry_hash(entry)
