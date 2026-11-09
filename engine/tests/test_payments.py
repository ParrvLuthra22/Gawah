"""Payment flow against the fake PayPal: write first / pay second, retries, recovery, races."""

import sqlite3
import threading

import httpx
import pytest
from conftest import TEST_CARD, build_services
from gawah_contract import LedgerEntryType, PaymentStatus, ReasonCode, Verdict
from gawah_engine.db import connect
from gawah_engine.errors import GawahError
from gawah_engine.payments import (
    classify_order,
    describe_error,
    idempotency_key,
    make_custom_id,
)
from gawah_engine.paypal import PayPalError
from paypal_fake import Crash
from world import World

T = LedgerEntryType


@pytest.fixture
def pw(psvc, clock):
    return World(psvc, clock)


def types(w):
    return [e.type.value for e in w.entries(mandate_id=w.mandate.id)]


def budget(w):
    b = w.svc.mandates.budget(w.mandate.id)
    return b.spent_minor, b.reserved_minor, b.remaining_minor


# --- the happy path ------------------------------------------------------------------------


def test_happy_path_pays_and_writes_the_ledger_in_order(pw, paypal):
    p = pw.proposal()
    result = pw.svc.guardian.submit(p)
    pay = result.payment
    assert result.decision.verdict == Verdict.ALLOW
    assert pay.status == PaymentStatus.PAID and pay.id == f"pay_{p.id}"
    assert pay.paypal_order_id.startswith("ORDER") and pay.paypal_capture_id.startswith("CAP")
    assert types(pw) == ["MANDATE_CREATED", "PROPOSAL_RECEIVED", "DECISION_MADE",
                         "PAYMENT_STARTED", "PAYMENT_COMPLETED"]
    started = pw.entries(type=T.PAYMENT_STARTED)[0]
    assert pay.custom_id == f"gawah:{p.id}:{started.seq}"  # points back to the ledger entry
    assert paypal.orders[pay.paypal_order_id]["purchase_units"][0]["custom_id"] == pay.custom_id
    done = pw.entries(type=T.PAYMENT_COMPLETED)[0]
    assert done.data["paypal_order_id"] == pay.paypal_order_id
    assert done.data["paypal_capture_id"] == pay.paypal_capture_id
    assert done.data["custom_id"] == pay.custom_id
    assert budget(pw) == (6500, 0, 1500)
    assert [c["request_id"] for c in paypal.calls("create")] == [idempotency_key(p.id)]
    assert pw.svc.ledger.verify()["ok"]


def test_payment_started_is_committed_before_any_paypal_call(pw, paypal):
    seen = []

    def hook(endpoint):
        conn = connect(pw.svc.path)
        seen.append([r["type"] for r in conn.execute("SELECT type FROM ledger ORDER BY seq")])
        conn.close()

    paypal.hooks.append(hook)
    pw.svc.guardian.submit(pw.proposal())
    assert seen and all("PAYMENT_STARTED" in s and "PAYMENT_COMPLETED" not in s for s in seen[:1])


def test_paypal_is_called_outside_the_mandate_lock_and_write_transaction(pw, paypal):
    states = []

    def hook(endpoint):
        locked = pw.svc.guardian._mandate_lock(pw.mandate.id).locked()
        other = sqlite3.connect(pw.svc.path, timeout=0.2, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")  # would fail if a write transaction were open
            other.execute("ROLLBACK")
            writable = True
        except sqlite3.OperationalError:
            writable = False
        other.close()
        states.append((locked, writable))

    paypal.hooks.append(hook)
    pw.svc.guardian.submit(pw.proposal())
    assert states and all(s == (False, True) for s in states)


def test_blocked_proposal_makes_no_paypal_call_and_no_reservation(pw, paypal):
    result = pw.svc.guardian.submit(pw.proposal(pw.cart(unit=12000)))
    assert result.decision.reasons == [ReasonCode.OVER_BUDGET] and result.payment is None
    assert paypal.requests == []
    assert budget(pw) == (0, 0, 8000)
    assert pw.svc.payments.list() == []
    assert "PAYMENT_STARTED" not in types(pw)


# --- failures ------------------------------------------------------------------------------


def test_4xx_fails_immediately_without_retry_and_releases_the_reservation(pw, paypal):
    paypal.fail("create", 422)
    pay = pw.svc.guardian.submit(pw.proposal()).payment
    assert pay.status == PaymentStatus.FAILED and pay.paypal_order_id is None
    assert len(paypal.calls("create")) == 1  # no retry
    assert budget(pw) == (0, 0, 8000)
    failed = pw.entries(type=T.PAYMENT_FAILED)[0]
    assert "INSTRUMENT_DECLINED" in failed.data["reason"] and "dbg-injected" in failed.data["reason"]
    row = pw.svc.payments._row(pay.id)
    assert row["failure_reason"] == failed.data["reason"] and row["attempts"] == 1
    assert types(pw)[-2:] == ["PAYMENT_STARTED", "PAYMENT_FAILED"]


@pytest.mark.parametrize("status", [500, 503, 429])
def test_transient_errors_retry_with_the_same_request_id(pw, paypal, status):
    paypal.fail("create", status, status)
    p = pw.proposal()
    pay = pw.svc.guardian.submit(p).payment
    assert pay.status == PaymentStatus.PAID
    calls = paypal.calls("create")
    assert len(calls) == 3 and {c["request_id"] for c in calls} == {idempotency_key(p.id)}
    assert len(paypal.orders) == 1  # never a second order
    assert pw.svc.payments._row(pay.id)["attempts"] == 3


def test_transient_errors_give_up_after_three_retries(pw, paypal):
    paypal.fail("create", 503, 503, 503, 503)
    pay = pw.svc.guardian.submit(pw.proposal()).payment
    assert pay.status == PaymentStatus.FAILED and len(paypal.calls("create")) == 4
    assert "503" in pw.svc.payments._row(pay.id)["failure_reason"]
    assert budget(pw) == (0, 0, 8000)


def test_timeout_after_the_order_was_created_is_resolved_by_the_idempotent_retry(pw, paypal):
    paypal.fail("create", "timeout_after")
    pay = pw.svc.guardian.submit(pw.proposal()).payment
    assert pay.status == PaymentStatus.PAID
    assert len(paypal.orders) == 1 and len(paypal.calls("create")) == 2


def test_unknown_outcome_with_an_order_id_is_read_back_not_guessed(pw, paypal):
    paypal.create_status = "CREATED"  # the engine has to capture separately
    paypal.fail("capture", "timeout", "timeout", "timeout", "timeout")
    pay = pw.svc.guardian.submit(pw.proposal()).payment
    order = paypal.orders[pay.paypal_order_id]
    assert len(paypal.calls("capture")) == 4 and len(paypal.calls("get")) == 1
    assert order["status"] == "CREATED"  # PayPal says it was never captured ...
    assert pay.status == PaymentStatus.FAILED  # ... so that is what we record
    assert "order not completed (status CREATED)" in pw.svc.payments._row(pay.id)["failure_reason"]
    assert budget(pw) == (0, 0, 8000)


def test_capture_that_succeeded_at_paypal_but_timed_out_is_recorded_as_paid(pw, paypal):
    paypal.create_status = "CREATED"
    paypal.fail("capture", "timeout_after")  # captured at PayPal, answer lost; retry replays it
    pay = pw.svc.guardian.submit(pw.proposal()).payment
    assert pay.status == PaymentStatus.PAID and budget(pw) == (6500, 0, 1500)
    assert sum(len(o["purchase_units"][0]["payments"]["captures"]) for o in paypal.orders.values()) == 1


def test_no_response_and_no_order_id_holds_the_budget_for_review(pw, paypal):
    paypal.fail("create", "timeout", "timeout", "timeout", "timeout")
    pay = pw.svc.guardian.submit(pw.proposal()).payment
    assert pay.status == PaymentStatus.RESERVED
    row = pw.svc.payments._row(pay.id)
    assert row["paypal_status"] == "UNKNOWN" and "outcome unknown" in row["failure_reason"]
    assert budget(pw) == (0, 6500, 1500)  # not released on a guess
    assert pw.svc.payments.reconcile()["still_reserved"] == 1  # and a restart does not release it
    assert pw.svc.payments.get(pay.id).status == PaymentStatus.RESERVED


def test_unconfigured_card_fails_cleanly(tmp_path, clock, paypal):
    svc = build_services(tmp_path, clock, paypal)
    svc.payments.config = type(svc.payments.config)(client_id="x", client_secret="y",
                                                    transport=paypal.transport)
    w = World(svc, clock)
    pay = svc.guardian.submit(w.proposal()).payment
    assert pay.status == PaymentStatus.FAILED and paypal.calls("create") == []
    assert "no buyer credentials" in svc.payments._row(pay.id)["failure_reason"]


# --- crash recovery ------------------------------------------------------------------------


def _restart(tmp_path, clock, paypal):
    return build_services(tmp_path, clock, paypal)  # same database and keys, fresh objects


def test_crash_between_payment_started_and_order_creation_is_reconciled(
    tmp_path, pw, clock, paypal
):
    paypal.fail("create", "crash")
    p = pw.proposal()
    with pytest.raises(Crash):
        pw.svc.guardian.submit(p)
    # state after the crash: reserved, STARTED written, nothing at PayPal
    assert pw.svc.payments.get(f"pay_{p.id}").status == PaymentStatus.RESERVED
    assert budget(pw) == (0, 6500, 1500) and types(pw)[-1] == "PAYMENT_STARTED"
    assert paypal.orders == {}

    svc2 = _restart(tmp_path, clock, paypal)
    assert svc2.payments.reconcile() == {"checked": 1, "paid": 0, "failed": 1, "still_reserved": 0}
    pay = svc2.payments.get(f"pay_{p.id}")
    assert pay.status == PaymentStatus.FAILED
    failed = svc2.ledger.list(type=T.PAYMENT_FAILED)[0]
    assert failed.data["reason"] == "interrupted before order creation"
    b = svc2.mandates.budget(pw.mandate.id)
    assert (b.spent_minor, b.reserved_minor) == (0, 0)
    assert svc2.ledger.verify()["ok"]
    assert svc2.payments.reconcile()["checked"] == 0  # nothing left to do


def test_crash_after_order_creation_before_capture_with_uncaptured_order(
    tmp_path, pw, clock, paypal
):
    paypal.create_status = "CREATED"
    paypal.fail("capture", "crash")
    p = pw.proposal()
    with pytest.raises(Crash):
        pw.svc.guardian.submit(p)
    row = pw.svc.payments._row(f"pay_{p.id}")
    assert row["status"] == "RESERVED" and row["paypal_order_id"]  # order id was saved at once
    svc2 = _restart(tmp_path, clock, paypal)
    svc2.payments.reconcile()
    pay = svc2.payments.get(f"pay_{p.id}")
    assert pay.status == PaymentStatus.FAILED  # PayPal's real status: CREATED, never captured
    assert "CREATED" in svc2.payments._row(pay.id)["failure_reason"]
    assert svc2.mandates.budget(pw.mandate.id).reserved_minor == 0


def test_crash_after_order_creation_when_paypal_did_capture(tmp_path, pw, clock, paypal):
    paypal.create_status = "CREATED"
    paypal.fail("capture", "crash")
    p = pw.proposal()
    with pytest.raises(Crash):
        pw.svc.guardian.submit(p)
    order = paypal.orders[pw.svc.payments._row(f"pay_{p.id}")["paypal_order_id"]]
    paypal._capture(order)  # PayPal captured it while we were down
    svc2 = _restart(tmp_path, clock, paypal)
    svc2.payments.reconcile()
    pay = svc2.payments.get(f"pay_{p.id}")
    assert pay.status == PaymentStatus.PAID
    assert pay.paypal_capture_id == order["purchase_units"][0]["payments"]["captures"][0]["id"]
    b = svc2.mandates.budget(pw.mandate.id)
    assert (b.spent_minor, b.reserved_minor) == (6500, 0)
    assert svc2.ledger.list(type=T.PAYMENT_COMPLETED)[0].data["paypal_order_id"] == pay.paypal_order_id


def test_reconciler_leaves_the_payment_alone_when_paypal_is_unreachable(
    tmp_path, pw, clock, paypal
):
    paypal.create_status = "CREATED"
    paypal.fail("capture", "crash")
    p = pw.proposal()
    with pytest.raises(Crash):
        pw.svc.guardian.submit(p)
    paypal.fail("get", 503, 503, 503, 503)
    svc2 = _restart(tmp_path, clock, paypal)
    assert svc2.payments.reconcile()["still_reserved"] == 1
    assert svc2.payments.get(f"pay_{p.id}").status == PaymentStatus.RESERVED
    assert svc2.payments.reconcile()["failed"] == 1  # PayPal back: order CREATED -> FAILED


# --- races and idempotency -----------------------------------------------------------------


def _race(w, proposals):
    barrier = threading.Barrier(len(proposals))
    results = [None] * len(proposals)

    def go(i):
        barrier.wait()
        results[i] = w.svc.guardian.submit(proposals[i])

    threads = [threading.Thread(target=go, args=(i,)) for i in range(len(proposals))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results


def test_two_simultaneous_65_dollar_proposals_one_paid_one_over_budget(pw, paypal):
    results = _race(pw, [pw.proposal(), pw.proposal()])
    verdicts = sorted((r.decision.verdict.value, tuple(r.decision.reasons)) for r in results)
    assert verdicts == [("ALLOW", ()), ("BLOCK", ("OVER_BUDGET",))]
    paid = [r for r in results if r.payment is not None]
    assert len(paid) == 1 and paid[0].payment.status == PaymentStatus.PAID
    assert len(paypal.calls("create")) == 1
    assert budget(pw) == (6500, 0, 1500)


def test_twenty_concurrent_proposals_never_overspend(tmp_path, clock, psvc, paypal):
    w = World(psvc, clock, max_amount_minor=100000)  # $1000: room for 15 x $65 = $975
    results = _race(w, [w.proposal() for _ in range(20)])
    paid = [r for r in results if r.payment and r.payment.status == PaymentStatus.PAID]
    blocked = [r for r in results if r.decision.verdict == Verdict.BLOCK]
    assert len(paid) == 15 and len(blocked) == 5
    assert all(r.decision.reasons == [ReasonCode.OVER_BUDGET] for r in blocked)
    spent, reserved, remaining = budget(w)
    assert (spent, reserved, remaining) == (15 * 6500, 0, 100000 - 15 * 6500)
    assert len(paypal.calls("create")) == 15 and len(paypal.orders) == 15
    assert psvc.ledger.verify()["ok"]


def test_twenty_concurrent_proposals_against_80_dollars_pay_exactly_once(pw, paypal):
    results = _race(pw, [pw.proposal() for _ in range(20)])
    assert sum(1 for r in results if r.payment is not None) == 1
    assert budget(pw) == (6500, 0, 1500) and len(paypal.calls("create")) == 1


def test_same_proposal_id_twice_is_one_payment_and_one_ledger_sequence(pw, paypal):
    p = pw.proposal()
    first = pw.svc.guardian.submit(p)
    second = pw.svc.guardian.submit(p)
    assert second == first and second.payment.status == PaymentStatus.PAID
    assert len(paypal.calls("create")) == 1 and len(pw.svc.payments.list()) == 1
    t = types(pw)
    for kind in ("PROPOSAL_RECEIVED", "DECISION_MADE", "PAYMENT_STARTED", "PAYMENT_COMPLETED"):
        assert t.count(kind) == 1
    assert budget(pw) == (6500, 0, 1500)


def test_same_proposal_id_concurrently_is_one_payment(pw, paypal):
    p = pw.proposal()
    results = _race(pw, [p] * 6)
    assert len({r.decision.id for r in results}) == 1
    assert len(paypal.calls("create")) == 1 and len(pw.svc.payments.list()) == 1
    assert budget(pw) == (6500, 0, 1500)


def test_revoking_mid_flight_blocks_the_payment(pw, paypal):
    class RevokeWhileThinking:
        """A semantic checker that lets the revocation land between evaluation and reservation."""

        def check(self, mandate, cart):
            pw.svc.mandates.revoke(mandate.id)
            from gawah_engine.semantic import SemanticResult

            return SemanticResult(category_match="match", confidence=1.0, source="model")

    pw.svc.guardian.semantic = RevokeWhileThinking()
    result = pw.svc.guardian.submit(pw.proposal())
    assert result.decision.verdict == Verdict.BLOCK
    assert result.decision.reasons == [ReasonCode.MANDATE_REVOKED]
    assert result.payment is None and paypal.requests == []
    assert budget(pw) == (0, 0, 8000)


def test_the_payment_unique_constraint_blocks_a_second_row_per_proposal(pw):
    p = pw.proposal()
    pw.svc.guardian.submit(p)
    conn = connect(pw.svc.path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO payments (id, proposal_id, decision_id, mandate_id, status, amount_minor,"
            " currency, idempotency_key, custom_id, created_at, updated_at)"
            " VALUES ('pay_dup', ?, 'd', ?, 'RESERVED', 1, 'USD', 'other-key', 'c', 't', 't')",
            (p.id, pw.mandate.id),
        )
    conn.close()


# --- refunds -------------------------------------------------------------------------------


def _paid(w):
    return w.svc.guardian.submit(w.proposal()).payment


def test_refund_once_returns_budget_and_is_idempotent(pw, paypal):
    pay = _paid(pw)
    r1 = pw.svc.payments.refund(pay.id, "customer changed their mind")
    assert r1.status == PaymentStatus.REFUNDED
    assert budget(pw) == (0, 0, 8000)  # spent budget given back to the mandate
    calls = paypal.calls("refund")
    assert len(calls) == 1 and calls[0]["request_id"] == f"{pay.idempotency_key}-refund"
    r2 = pw.svc.payments.refund(pay.id)
    assert r2 == r1 and len(paypal.calls("refund")) == 1
    assert types(pw).count("REFUNDED") == 1
    refunded = pw.entries(type=T.REFUNDED)[0]
    assert refunded.subject_id == pay.id and refunded.data["paypal_capture_id"] == pay.paypal_capture_id
    assert pw.svc.ledger.verify()["ok"]


def test_refund_rejects_payments_that_are_not_paid(pw, paypal):
    paypal.fail("create", 422)
    failed = pw.svc.guardian.submit(pw.proposal()).payment
    with pytest.raises(GawahError) as e:
        pw.svc.payments.refund(failed.id)
    assert e.value.status == 409 and e.value.code == "PAYMENT_NOT_REFUNDABLE"
    assert paypal.calls("refund") == []
    with pytest.raises(GawahError) as e:
        pw.svc.payments.refund("pay_nope")
    assert e.value.status == 404


def test_refund_failure_leaves_the_payment_paid(pw, paypal):
    pay = _paid(pw)
    paypal.fail("refund", 422)
    with pytest.raises(GawahError) as e:
        pw.svc.payments.refund(pay.id)
    assert e.value.status == 502 and e.value.code == "REFUND_FAILED"
    assert pw.svc.payments.get(pay.id).status == PaymentStatus.PAID
    assert budget(pw) == (6500, 0, 1500) and "REFUNDED" not in types(pw)


def test_refund_retries_transient_errors_with_the_same_request_id(pw, paypal):
    pay = _paid(pw)
    paypal.fail("refund", 503, 503)
    assert pw.svc.payments.refund(pay.id).status == PaymentStatus.REFUNDED
    calls = paypal.calls("refund")
    assert len(calls) == 3 and len({c["request_id"] for c in calls}) == 1


def test_concurrent_refunds_refund_once(pw, paypal):
    pay = _paid(pw)
    barrier = threading.Barrier(5)

    def go():
        barrier.wait()
        pw.svc.payments.refund(pay.id)

    threads = [threading.Thread(target=go) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert types(pw).count("REFUNDED") == 1 and budget(pw) == (0, 0, 8000)
    assert len(paypal.refunds) == 1  # PayPal deduplicated by request id


# --- evidence ------------------------------------------------------------------------------


def test_evidence_bundle_verifies_clean_then_shows_the_broken_seq(pw):
    p = pw.proposal(pw.cart(variant="size 10"))
    pay = pw.svc.guardian.submit(p).payment
    b = pw.svc.evidence.bundle(pay.id)
    assert b.verification.ok and b.verification.reason is None
    assert b.verification.checked == len(pw.entries()) and b.verification.first_broken_seq is None
    assert b.payment == pay and b.proposal.id == p.id and b.signed_cart == p.signed_cart
    assert b.mandate.id == pw.mandate.id and b.decision.id == f"dec_{p.id}"
    assert "size 10" in b.decision.explanation  # the advisory travels with the decision
    assert {"PAYMENT_STARTED", "PAYMENT_COMPLETED", "DECISION_MADE"} <= {
        e.type.value for e in b.ledger_entries}
    assert [e.seq for e in b.ledger_entries] == sorted(e.seq for e in b.ledger_entries)

    decision_seq = pw.entries(type=T.DECISION_MADE)[0].seq
    conn = sqlite3.connect(pw.svc.path, isolation_level=None)
    conn.execute("DROP TRIGGER ledger_no_update")
    conn.execute("UPDATE ledger SET data = '{\"mandate_id\":\"x\"}' WHERE seq = ?", (decision_seq,))
    conn.close()
    bad = pw.svc.evidence.bundle(pay.id).verification
    assert not bad.ok and bad.first_broken_seq == decision_seq
    assert bad.reason.startswith("ledger:") and "hash mismatch" in bad.reason


def test_evidence_checks_the_mandate_and_cart_signatures_too(pw):
    pay = _paid(pw)
    conn = sqlite3.connect(pw.svc.path, isolation_level=None)
    conn.execute("UPDATE mandates SET max_amount_minor = 900000, category = 'x'")
    conn.close()
    v = pw.svc.evidence.bundle(pay.id).verification
    assert not v.ok and "mandate signature does not verify" in v.reason
    conn = sqlite3.connect(pw.svc.path, isolation_level=None)
    conn.execute("DELETE FROM merchants")
    conn.close()
    assert "is not registered" in pw.svc.evidence.bundle(pay.id).verification.reason


def test_evidence_for_unknown_payment_is_404(pw):
    with pytest.raises(GawahError) as e:
        pw.svc.evidence.bundle("pay_nope")
    assert e.value.status == 404


# --- listing, helpers, secrets ---------------------------------------------------------------


def test_payment_list_filters(pw, paypal):
    a = _paid(pw)
    paypal.fail("create", 422)
    b = pw.svc.guardian.submit(pw.proposal(pw.cart(unit=1000))).payment
    svc = pw.svc.payments
    assert [p.id for p in svc.list()] == [a.id, b.id]
    assert [p.id for p in svc.list(mandate_id=pw.mandate.id)] == [a.id, b.id]
    assert svc.list(mandate_id="man_other") == []
    assert [p.id for p in svc.list(status="PAID")] == [a.id]
    assert [p.id for p in svc.list(status=PaymentStatus.FAILED)] == [b.id]
    assert [p.id for p in svc.list(paypal_order_id=a.paypal_order_id)] == [a.id]
    assert [p.id for p in svc.list(paypal_capture_id=a.paypal_capture_id)] == [a.id]
    assert svc.list(paypal_capture_id="nope") == []
    assert svc.list(status="PAID", paypal_order_id="nope") == []


def test_card_and_tokens_never_reach_the_database_ledger_or_logs(pw, paypal, caplog):
    caplog.set_level("DEBUG")
    paypal.fail("create", 422)
    pw.svc.guardian.submit(pw.proposal())  # a failure ...
    _paid(pw)  # ... and a success
    dump = "\n".join(pw.svc.ledger.list()[i].model_dump_json() for i in range(len(pw.entries())))
    conn = connect(pw.svc.path)
    dump += "\n".join(conn.iterdump())
    conn.close()
    dump += caplog.text
    for secret in (TEST_CARD["number"], "test-secret", "fake-token"):  # (a 3-digit cvv matches hashes by chance)
        assert secret not in dump
    assert "redacted" in repr(pw.svc.payments.config) and "test-secret" not in repr(pw.svc.payments.config)


def test_idempotency_key_and_custom_id_are_bounded():
    assert idempotency_key("prop_1") == "idem-prop_1"
    long_id = "p" * 300
    key = idempotency_key(long_id)
    assert len(key) <= 93 and key == idempotency_key(long_id) and key != idempotency_key("q" * 300)
    assert len(key + "-refund") <= 108
    cid = make_custom_id(long_id, 123456)
    assert len(cid) <= 127 and cid.endswith(":123456") and cid.startswith("gawah:")
    assert make_custom_id("prop_1", 4) == "gawah:prop_1:4"


def test_describe_error_never_includes_raw_bodies():
    err = PayPalError(422, {"name": "UNPROCESSABLE_ENTITY", "message": "card 4111111111111111 bad",
                            "details": [{"issue": "INSTRUMENT_DECLINED", "description": "secret"}]},
                      "dbg1")
    text = describe_error(err)
    assert "4111" not in text and "secret" not in text and "INSTRUMENT_DECLINED" in text
    assert describe_error(httpx.ReadTimeout("x")) == "PayPal request timed out"


@pytest.mark.parametrize(
    ("order", "outcome"),
    [
        ({"status": "COMPLETED", "purchase_units": [{"payments": {"captures": [
            {"id": "C1", "status": "COMPLETED"}]}}]}, "PAID"),
        ({"status": "COMPLETED", "purchase_units": [{"payments": {"captures": [
            {"id": "C1", "status": "PENDING"}]}}]}, "PENDING"),
        ({"status": "COMPLETED", "purchase_units": [{"payments": {"captures": [
            {"id": "C1", "status": "DECLINED"}]}}]}, "FAILED"),
        ({"status": "COMPLETED", "purchase_units": [{}]}, "PENDING"),
        ({"status": "CREATED"}, "FAILED"),
        ({"status": "APPROVED"}, "FAILED"),
        ({"status": "VOIDED"}, "FAILED"),
    ],
)
def test_classify_order(order, outcome):
    assert classify_order(order)[0] == outcome
