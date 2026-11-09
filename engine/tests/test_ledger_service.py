import sqlite3
import threading

import pytest
from conftest import make_body
from gawah_contract import LedgerEntryType
from gawah_contract.hashing import GENESIS_HASH, verify_chain
from gawah_engine.db import connect


def _fill(svc, n=5):
    for i in range(n):
        svc.ledger.append(LedgerEntryType.PROPOSAL_RECEIVED, f"prop_{i}", {"mandate_id": "man_1", "i": i})


def _raw(svc):
    conn = sqlite3.connect(svc.path, isolation_level=None)
    return conn


def test_chain_links_and_genesis(svc):
    _fill(svc, 3)
    e = svc.ledger.list()
    assert [x.seq for x in e] == [1, 2, 3]
    assert e[0].prev_hash == GENESIS_HASH
    assert e[1].prev_hash == e[0].hash and e[2].prev_hash == e[1].hash
    assert verify_chain(e) == (True, None)  # shared contract helper agrees with our hashes
    assert e[0].key_id == "gawah-ledger-key-1"


def test_verify_ok_and_empty(svc):
    assert svc.ledger.verify() == {"ok": True, "checked": 0, "first_broken_seq": None, "reason": None}
    _fill(svc)
    r = svc.ledger.verify()
    assert r["ok"] and r["checked"] == 5 and r["first_broken_seq"] is None


def test_list_filters(svc, clock):
    m = svc.mandates.create(make_body(clock))
    svc.ledger.append("PAYMENT_STARTED", "pay_1", {"mandate_id": m.id})
    svc.ledger.append("PAYMENT_COMPLETED", "pay_1", {"mandate_id": m.id})
    svc.ledger.append("PAYMENT_STARTED", "pay_2", {"mandate_id": "man_other"})
    assert len(svc.ledger.list(mandate_id=m.id)) == 3
    assert len(svc.ledger.list(subject_id="pay_1")) == 2
    assert [e.seq for e in svc.ledger.list(type="PAYMENT_STARTED")] == [2, 4]
    assert [e.seq for e in svc.ledger.list(since_seq=2)] == [3, 4]
    assert [e.seq for e in svc.ledger.list(mandate_id=m.id, type=LedgerEntryType.PAYMENT_STARTED)] == [2]
    with pytest.raises(ValueError):
        svc.ledger.list(type="NOT_A_TYPE")


def test_append_rejects_unknown_type_and_floats(svc):
    with pytest.raises(ValueError):
        svc.ledger.append("NOPE", "x", {})
    with pytest.raises(TypeError):
        svc.ledger.append("PAYMENT_STARTED", "x", {"amount": 65.0})
    assert svc.ledger.list() == []


def test_triggers_block_update_and_delete(svc):
    _fill(svc, 2)
    conn = _raw(svc)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE ledger SET data = '{}' WHERE seq = 1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM ledger WHERE seq = 2")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM ledger")
    conn.close()
    assert len(svc.ledger.list()) == 2 and svc.ledger.verify()["ok"]


def _tamper(svc, sql, args=()):
    conn = _raw(svc)
    conn.execute("DROP TRIGGER ledger_no_update")
    conn.execute("DROP TRIGGER ledger_no_delete")
    conn.execute(sql, args)
    conn.close()


def test_edited_data_is_detected_at_the_right_seq(svc):
    _fill(svc)
    _tamper(svc, "UPDATE ledger SET data = ? WHERE seq = 3", ('{"mandate_id":"man_1","i":99}',))
    r = svc.ledger.verify()
    assert not r["ok"] and r["first_broken_seq"] == 3 and "hash mismatch" in r["reason"]
    assert r["checked"] == 3


def test_rehashed_edit_is_caught_by_signature(svc):
    from gawah_engine.ledger_service import entry_hash

    _fill(svc)
    e = svc.ledger.list()[2]
    data = {"mandate_id": "man_1", "i": 99}
    new_hash = entry_hash(e.seq, e.ts.strftime("%Y-%m-%dT%H:%M:%SZ"), e.type.value, e.subject_id, data, e.prev_hash)
    _tamper(svc, "UPDATE ledger SET data = ?, hash = ? WHERE seq = 3", ('{"i":99,"mandate_id":"man_1"}', new_hash))
    r = svc.ledger.verify()
    assert r["first_broken_seq"] == 3 and "signature" in r["reason"]


def test_deleted_row_is_detected(svc):
    _fill(svc)
    _tamper(svc, "DELETE FROM ledger WHERE seq = 2")
    r = svc.ledger.verify()
    assert r["first_broken_seq"] == 3 and "sequence gap" in r["reason"]


def test_deleted_tail_row_is_not_a_gap_but_chain_still_valid(svc):
    # Documented limit: truncating the newest rows leaves a valid shorter chain.
    _fill(svc)
    _tamper(svc, "DELETE FROM ledger WHERE seq = 5")
    assert svc.ledger.verify()["ok"]


def test_swapped_prev_hash_is_detected(svc):
    _fill(svc)
    _tamper(svc, "UPDATE ledger SET prev_hash = ? WHERE seq = 4", ("f" * 64,))
    r = svc.ledger.verify()
    assert r["first_broken_seq"] == 4 and "prev_hash" in r["reason"]


def test_forged_signature_is_detected(svc):
    _fill(svc)
    _tamper(svc, "UPDATE ledger SET signature = ? WHERE seq = 2", ("A" * 86,))
    assert svc.ledger.verify()["first_broken_seq"] == 2


def test_concurrent_appends_do_not_fork_the_chain(svc):
    barrier = threading.Barrier(20)

    def worker(i):
        barrier.wait()
        svc.ledger.append("PROPOSAL_RECEIVED", f"prop_{i}", {"i": i})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    entries = svc.ledger.list()
    assert [e.seq for e in entries] == list(range(1, 21))
    assert svc.ledger.verify()["ok"]


def test_chain_survives_key_rotation(svc):
    _fill(svc, 2)
    svc.ledger.signer.rotate()
    _fill(svc, 2)
    entries = svc.ledger.list()
    assert [e.key_id for e in entries] == ["gawah-ledger-key-1"] * 2 + ["gawah-ledger-key-2"] * 2
    assert svc.ledger.verify()["ok"]


def test_payment_attempt_is_written_before_the_body_runs(svc):
    seen = {}
    with svc.ledger.payment_attempt("pay_1", mandate_id="man_1", data={"custom_id": "gawah:p1"}) as a:
        # a separate connection sees PAYMENT_STARTED => it is committed before "paying"
        seen["types"] = [e.type for e in svc.ledger.list(subject_id="pay_1")]
        a.completed({"paypal_order_id": "ORD1"})
    assert seen["types"] == [LedgerEntryType.PAYMENT_STARTED]
    entries = svc.ledger.list(subject_id="pay_1")
    assert [e.type for e in entries] == [LedgerEntryType.PAYMENT_STARTED, LedgerEntryType.PAYMENT_COMPLETED]
    assert entries[0].data == {"custom_id": "gawah:p1", "mandate_id": "man_1"}
    assert entries[1].data["paypal_order_id"] == "ORD1"
    assert svc.ledger.list(mandate_id="man_1")[0].subject_id == "pay_1"


def test_payment_attempt_records_failure_and_reraises(svc):
    with (
        pytest.raises(RuntimeError, match="paypal down"),
        svc.ledger.payment_attempt("pay_2", mandate_id="man_1"),
    ):
        raise RuntimeError("paypal down")
    e = svc.ledger.list(subject_id="pay_2")
    assert [x.type for x in e] == [LedgerEntryType.PAYMENT_STARTED, LedgerEntryType.PAYMENT_FAILED]
    assert e[1].data["error"] == "RuntimeError"


def test_payment_attempt_without_outcome_leaves_started_open(svc):
    with (
        pytest.raises(RuntimeError, match="without calling"),
        svc.ledger.payment_attempt("pay_3"),
    ):
        pass
    assert [x.type for x in svc.ledger.list(subject_id="pay_3")] == [LedgerEntryType.PAYMENT_STARTED]


def test_connect_helper_has_busy_timeout(svc):
    conn = connect(svc.path)
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    conn.close()
