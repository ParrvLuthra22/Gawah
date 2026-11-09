import threading
from datetime import timedelta

import pytest
from conftest import make_body
from gawah_contract import LedgerEntryType, MandateStatus, ReasonCode
from gawah_engine.errors import GawahError
from gawah_engine.mandate_service import (
    BudgetExceeded,
    IdempotencyConflict,
    MandateNotActive,
    MandateNotFound,
    check_category,
    check_currency,
    check_expiry,
    check_merchant,
    check_revocation,
    check_spend_limit,
    signable,
)


def test_create_sets_fields_signs_and_writes_ledger(svc, clock):
    m = svc.mandates.create(make_body(clock))
    assert m.id.startswith("man_") and m.status == MandateStatus.ACTIVE
    assert m.issued_at == clock() and m.signing_alg == "ES256" and m.key_id.startswith("gawah-mandate")
    assert svc.mandates.verify_mandate(m) == (True, None)
    entries = svc.ledger.list(mandate_id=m.id)
    assert [e.type for e in entries] == [LedgerEntryType.MANDATE_CREATED]
    assert entries[0].data["max_amount_minor"] == 8000
    assert svc.mandates.get(m.id) == m


@pytest.mark.parametrize(
    "over",
    [
        {"max_amount_minor": 0},
        {"allowed_merchants": []},
        {"allowed_merchants": ["  "]},
        {"currency": "U$D"},
        {"category": " "},
    ],
)
def test_create_validation(svc, clock, over):
    with pytest.raises(GawahError) as e:
        svc.mandates.create(make_body(clock, **over))
    assert e.value.status == 422


def test_create_rejects_past_and_naive_expiry(svc, clock):
    with pytest.raises(GawahError):
        svc.mandates.create(make_body(clock, expires_at=clock() - timedelta(seconds=1)))
    with pytest.raises(GawahError):
        svc.mandates.create(make_body(clock, expires_at=clock().replace(tzinfo=None) + timedelta(days=1)))
    assert svc.ledger.list() == []  # nothing half-written


def test_get_unknown_is_404(svc):
    with pytest.raises(MandateNotFound):
        svc.mandates.get("man_nope")


def test_list(svc, clock):
    a, b = svc.mandates.create(make_body(clock)), svc.mandates.create(make_body(clock))
    assert {m.id for m in svc.mandates.list()} == {a.id, b.id}


def test_revoke_writes_ledger_once_and_is_idempotent(svc, clock):
    m = svc.mandates.create(make_body(clock))
    assert svc.mandates.revoke(m.id).status == MandateStatus.REVOKED
    assert svc.mandates.revoke(m.id).status == MandateStatus.REVOKED
    types = [e.type for e in svc.ledger.list(mandate_id=m.id)]
    assert types == [LedgerEntryType.MANDATE_CREATED, LedgerEntryType.MANDATE_REVOKED]
    ok, reason = svc.mandates.verify_mandate(m)  # stale copy still says "active"
    assert (ok, reason) == (False, "MANDATE_REVOKED")


def test_expired_mandate_auto_expires_on_read(svc, clock):
    m = svc.mandates.create(make_body(clock))
    clock.advance(days=7, seconds=1)
    assert svc.mandates.get(m.id).status == MandateStatus.EXPIRED
    assert svc.mandates.verify_mandate(m) == (False, "MANDATE_EXPIRED")
    with pytest.raises(MandateNotActive):
        svc.mandates.revoke(m.id)
    with pytest.raises(MandateNotActive):
        svc.mandates.reserve(m.id, 100, "k1")


def test_verify_rejects_tampered_mandate(svc, clock):
    m = svc.mandates.create(make_body(clock))
    for change in ({"max_amount_minor": 800000}, {"allowed_merchants": ["evil"]}, {"category": "x"}):
        assert svc.mandates.verify_mandate(m.model_copy(update=change)) == (
            False,
            "MANDATE_SIGNATURE_INVALID",
        )
    assert svc.mandates.verify_mandate(m.model_copy(update={"signature": "AAAA"}))[0] is False


def test_verify_unknown_mandate(svc, clock):
    m = svc.mandates.create(make_body(clock))
    renamed = m.model_copy(update={"id": "man_ghost"})  # id is signed, so this is a forgery
    assert svc.mandates.verify_mandate(renamed)[1] == "MANDATE_SIGNATURE_INVALID"
    sig, key_id = svc.mandates.signer.sign(signable(renamed))  # validly signed but never stored
    ghost = renamed.model_copy(update={"signature": sig, "key_id": key_id})
    assert svc.mandates.verify_mandate(ghost) == (False, "MANDATE_NOT_FOUND")


def test_verify_survives_mandate_key_rotation(svc, clock):
    m = svc.mandates.create(make_body(clock))
    svc.mandates.signer.rotate()
    assert svc.mandates.verify_mandate(m) == (True, None)
    m2 = svc.mandates.create(make_body(clock))
    assert m2.key_id != m.key_id and svc.mandates.verify_mandate(m2) == (True, None)


def test_policy_helpers(svc, clock):
    m = svc.mandates.create(make_body(clock))
    assert check_merchant(m, "demo-shoe-store") is None
    assert check_merchant(m, "evil-store") == ReasonCode.MERCHANT_NOT_ALLOWED
    assert check_category(m, "Footwear") is None
    assert check_category(m, "electronics") == ReasonCode.CATEGORY_MISMATCH
    assert check_currency(m, "USD") is None and check_currency(m, "EUR") == ReasonCode.CURRENCY_MISMATCH
    assert check_expiry(m, clock()) is None
    assert check_expiry(m, clock() + timedelta(days=8)) == ReasonCode.MANDATE_EXPIRED
    assert check_revocation(m) is None
    assert check_revocation(svc.mandates.revoke(m.id)) == ReasonCode.MANDATE_REVOKED
    budget = svc.mandates.budget(m.id)
    assert check_spend_limit(budget, 8000) is None
    assert check_spend_limit(budget, 8001) == ReasonCode.OVER_BUDGET
    assert check_spend_limit(budget, 0) == ReasonCode.OVER_BUDGET


def test_budget_accounting(svc, clock):
    m = svc.mandates.create(make_body(clock))  # 8000
    b = svc.mandates.budget(m.id)
    assert (b.spent_minor, b.reserved_minor, b.remaining_minor) == (0, 0, 8000)
    svc.mandates.reserve(m.id, 6500, "k1")
    assert svc.mandates.budget(m.id).remaining_minor == 1500
    with pytest.raises(BudgetExceeded):
        svc.mandates.reserve(m.id, 1501, "k2")
    svc.mandates.commit(m.id, "k1")
    b = svc.mandates.budget(m.id)
    assert (b.spent_minor, b.reserved_minor, b.remaining_minor) == (6500, 0, 1500)
    svc.mandates.reserve(m.id, 1500, "k3")
    svc.mandates.release(m.id, "k3")
    assert svc.mandates.budget(m.id).remaining_minor == 1500


def test_reserve_is_idempotent(svc, clock):
    m = svc.mandates.create(make_body(clock))
    first = svc.mandates.reserve(m.id, 5000, "same")
    again = svc.mandates.reserve(m.id, 5000, "same")
    assert not first.replayed and again.replayed
    assert svc.mandates.budget(m.id).reserved_minor == 5000
    with pytest.raises(IdempotencyConflict):
        svc.mandates.reserve(m.id, 4000, "same")
    assert svc.mandates.budget(m.id).reserved_minor == 5000


def test_commit_and_release_are_idempotent_and_exclusive(svc, clock):
    m = svc.mandates.create(make_body(clock))
    svc.mandates.reserve(m.id, 1000, "k")
    svc.mandates.commit(m.id, "k")
    assert svc.mandates.commit(m.id, "k").replayed
    assert svc.mandates.budget(m.id).spent_minor == 1000
    with pytest.raises(IdempotencyConflict):
        svc.mandates.release(m.id, "k")
    with pytest.raises(GawahError):
        svc.mandates.commit(m.id, "unknown")


def test_commit_still_works_after_revocation(svc, clock):
    m = svc.mandates.create(make_body(clock))
    svc.mandates.reserve(m.id, 1000, "k")
    svc.mandates.revoke(m.id)
    svc.mandates.commit(m.id, "k")
    assert svc.mandates.budget(m.id).spent_minor == 1000
    with pytest.raises(MandateNotActive):
        svc.mandates.reserve(m.id, 100, "k2")


def test_concurrent_reservations_never_overspend(svc, clock):
    m = svc.mandates.create(make_body(clock, max_amount_minor=1000))
    results, barrier = [], threading.Barrier(20)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            svc.mandates.reserve(m.id, 100, f"key-{i}")
            results.append("ok")
        except BudgetExceeded:
            results.append("over")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    b = svc.mandates.budget(m.id)
    assert results.count("ok") == 10 and results.count("over") == 10
    assert b.reserved_minor == 1000 and b.remaining_minor == 0
    assert b.spent_minor + b.reserved_minor <= b.max_minor


def test_concurrent_reservations_with_one_key_count_once(svc, clock):
    m = svc.mandates.create(make_body(clock, max_amount_minor=1000))
    barrier = threading.Barrier(20)

    def worker() -> None:
        barrier.wait()
        svc.mandates.reserve(m.id, 400, "only-one")

    threads = [threading.Thread(target=worker) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert svc.mandates.budget(m.id).reserved_minor == 400
