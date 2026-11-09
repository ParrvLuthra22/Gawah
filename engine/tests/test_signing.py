import stat

import pytest
from gawah_engine.signing import ES256Signer


@pytest.fixture
def signer(tmp_path):
    return ES256Signer(tmp_path / "keys", "t-key")


def test_sign_verify_round_trip(signer):
    payload = {"id": "man_1", "max_amount_minor": 8000, "note": "靴"}
    sig, key_id = signer.sign(payload)
    assert key_id == "t-key-1"
    assert signer.verify(payload, sig, key_id)


def test_signature_field_is_excluded_and_key_order_is_irrelevant(signer):
    sig, key_id = signer.sign({"a": 1, "b": 2})
    assert signer.verify({"b": 2, "a": 1, "signature": "whatever"}, sig, key_id)


def test_tampered_payload_is_rejected(signer):
    sig, key_id = signer.sign({"max_amount_minor": 8000})
    assert not signer.verify({"max_amount_minor": 80000}, sig, key_id)


def test_tampered_or_garbage_signature_is_rejected(signer):
    payload = {"a": 1}
    sig, key_id = signer.sign(payload)
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    assert not signer.verify(payload, flipped, key_id)
    for bad in ("", "not-base64!!", sig[:-4], sig + "AAAA"):
        assert not signer.verify(payload, bad, key_id)


def test_unknown_and_malicious_key_ids_are_rejected(signer):
    sig, _ = signer.sign({"a": 1})
    for key_id in ("t-key-99", "../keys/t-key-1", "other-key-1", ""):
        assert not signer.verify({"a": 1}, sig, key_id)


def test_string_payload_is_signed_as_is(signer):
    sig, key_id = signer.sign("abc123")
    assert signer.verify("abc123", sig, key_id)
    assert not signer.verify("abc124", sig, key_id)


def test_keys_persist_across_instances(tmp_path):
    sig, key_id = ES256Signer(tmp_path, "t-key").sign({"a": 1})
    again = ES256Signer(tmp_path, "t-key")
    assert again.active_key_id == key_id
    assert again.verify({"a": 1}, sig, key_id)


def test_rotation_keeps_old_signatures_valid(signer):
    old_sig, old_id = signer.sign({"a": 1})
    new_id = signer.rotate()
    assert new_id == "t-key-2" and signer.active_key_id == new_id
    new_sig, used = signer.sign({"a": 1})
    assert used == new_id
    assert signer.verify({"a": 1}, old_sig, old_id)
    assert signer.verify({"a": 1}, new_sig, new_id)
    assert not signer.verify({"a": 1}, old_sig, new_id)  # a key id cannot be swapped


def test_purposes_do_not_cross_verify(tmp_path):
    ledger = ES256Signer(tmp_path, "ledger-key")
    mandate = ES256Signer(tmp_path, "mandate-key")
    sig, key_id = ledger.sign("x")
    assert not mandate.verify("x", sig, key_id)


def test_private_key_files_are_owner_only(signer, tmp_path):
    pem = tmp_path / "keys" / "t-key-1.pem"
    assert stat.S_IMODE(pem.stat().st_mode) == 0o600
    assert stat.S_IMODE(pem.parent.stat().st_mode) == 0o700


def test_key_path_must_be_a_directory(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    with pytest.raises(ValueError):
        ES256Signer(f, "t-key")
