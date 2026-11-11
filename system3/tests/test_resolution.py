import hashlib
import json
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from counsel.clients import PayPalClient
from counsel.config import Settings
from counsel.main import create_app
from counsel.models import CounselAnalysis, Dispute, EvidencePack, Source, now
from counsel.resolution import disabled_reason
from fastapi.testclient import TestClient
from gawah_contract.fixtures import load
from sqlalchemy.orm import Session

KEY = {"X-Gawah-Key": "console-key"}
ACTORS = {
    "alice": ("reviewer", "a" * 42),
    "bob": ("reviewer", "b" * 42),
    "sam": ("operator", "s" * 42),
    "view": ("viewer", "v" * 42),
}
URL = "/api/resolution/workflows"
CAPTURE = "8JT668275T605350T"
PAYMENT = {**load("payment_paid"), "paypal_capture_id": CAPTURE}
CHECK = {"status": "VERIFIED", "detail": "Verified by System 2."}


def headers(actor):
    return {**KEY, "Authorization": f"Bearer {actor}.{ACTORS[actor][1]}"}


def publish(db, dispute_id, version=1, recommendation="DEFEND"):
    dispute = db.get(Dispute, dispute_id)
    evidence = {
        "id": "e-payment",
        "type": "PAYMENT",
        "source_system": "SYSTEM_2",
        "source_type": "REAL_RECORD",
        "source_record_id": "CAPTURE-RECORD",
        "source_reference": "/v1/payments/pay_01",
        "payment_id": dispute.payment_id,
        "dispute_id": dispute.paypal_dispute_id,
        "timestamp": now().isoformat(),
        "retrieved_at": now().isoformat(),
        "verification_status": "VERIFIED",
        "payload": {"private": "must not be exported"},
    }
    snapshot = {
        "dispute_id": dispute.paypal_dispute_id,
        "dispute_record_id": dispute.id,
        "payment_id": dispute.payment_id,
        "evidence": [evidence],
        "timeline": [],
        "sources": [],
        "missing_evidence": [],
        "invalid_evidence": [],
        "contradictions": [],
        "verification_summary": {
            key: CHECK for key in ("mandate_signature", "cart_signature", "ledger", "engine_ledger")
        },
        "completeness": {"available": 1, "missing": 0, "verified": 1, "unverified": 0},
    }
    pack = EvidencePack(
        payment_id=dispute.payment_id,
        dispute_id=dispute.id,
        version=version,
        fingerprint=f"{dispute_id}:{version}",
        snapshot=snapshot,
        generated_at=now(),
    )
    db.add(pack)
    db.flush()
    row = CounselAnalysis(
        payment_id=dispute.payment_id,
        dispute_id=dispute.id,
        evidence_pack_id=pack.id,
        evidence_pack_version=pack.version,
        analysis_status="COMPLETED",
        review_status="PENDING",
        model="fixture",
        created_at=now(),
        snapshot={
            "dispute_id": dispute.paypal_dispute_id,
            "dispute_reason": dispute.reason,
            "category": "TEST",
            "error": None,
            "classification": {
                key: []
                for key in (
                    "records",
                    "contradictory_evidence",
                    "missing_evidence",
                    "invalid_evidence",
                    "invalid_evidence_ids",
                    "unverified_evidence_ids",
                    "unavailable_evidence_ids",
                    "unavailable_sources",
                    "verification_warnings",
                )
            },
            "outcome": {
                "recommendation": recommendation,
                "confidence": 0.8,
                "executive_summary": "Review the capture.",
                "supporting_evidence_ids": ["e-payment"],
                "reasoning": [
                    {"id": "f1", "text": "Capture recorded.", "evidence_ids": ["e-payment"]},
                ],
                "draft_response": "Original AI text.",
                "draft_evidence_ids": ["e-payment"],
            },
        },
    )
    db.add(row)
    db.commit()
    return row.id


@pytest.fixture
def n5(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'n5.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
    settings = Settings(
        _env_file=None,
        database_url=url,
        system3_key="console-key",
        system2_key="engine-key",
        paypal_client_id="test-id",
        paypal_client_secret="test-secret",
        enable_sandbox_resolution=True,
        n5_operators={
            actor: {"role": role, "token_sha256": hashlib.sha256(token.encode()).hexdigest()}
            for actor, (role, token) in ACTORS.items()
        },
    )
    engine = settings.database_engine()
    with Session(engine) as db:
        db.add_all(
            [
                Dispute(
                    paypal_dispute_id=case,
                    payment_id=PAYMENT["id"],
                    link_status="LINKED",
                    status="OPEN",
                    source=source,
                    amount_minor=6500,
                    currency="USD",
                    reason="INCORRECT_AMOUNT",
                    raw_response={},
                    last_synced_at=now(),
                )
                for case, source in (
                    ("PP-D-ONE", Source.PAYPAL_API),
                    ("PP-D-SIM", Source.SIMULATED_FIXTURE),
                    ("PP-D-THREE", Source.PAYPAL_API),
                    ("PP-D-EMPTY", Source.PAYPAL_API),
                )
            ]
        )
        db.commit()
        publish(db, 1)
        publish(db, 3, version=2, recommendation="CONCEDE_REMEDY")
    writes = []
    state = {
        "status": "WAITING_FOR_SELLER_RESPONSE",
        "stage": "INQUIRY",
        "capture": CAPTURE,
        "timeout": False,
        "response": 200,
        "evidence": [],
        "messages": [],
        "outcome": None,
        "system2_failure": False,
        "forbid_paypal": False,
    }

    def paypal(request):
        if state["forbid_paypal"]:
            raise AssertionError("Simulated sandbox refunds must not call PayPal.")
        path = request.url.path
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "mock-token", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer mock-token"
        if request.method == "POST":
            writes.append((path, json.loads(request.content)))
            if state["timeout"]:
                raise httpx.ReadTimeout("sensitive provider details")
            return httpx.Response(
                state["response"], json={"links": []}, headers={"PayPal-Debug-Id": "debug-1"}
            )
        return httpx.Response(
            200,
            json={
                "dispute_id": path.rsplit("/", 1)[-1],
                "status": state["status"],
                "dispute_life_cycle_stage": state["stage"],
                "update_time": now().isoformat(),
                "dispute_amount": {"value": "65.00", "currency_code": "USD"},
                "disputed_transactions": [{"seller_transaction_id": state["capture"]}],
                "links": [
                    {"rel": action, "method": "POST", "href": str(request.url) + "/" + action}
                    for action in ("provide-evidence", "send-message", "accept-claim", "make-offer")
                ],
                "allowed_response_options": {
                    "accept_claim": {"accept_claim_types": ["REFUND"]},
                    "make_offer": {"offer_types": ["REFUND"]},
                },
                "evidences": [
                    {"source": "REQUESTED_FROM_SELLER", "evidence_type": "OTHER"},
                    *state["evidence"],
                ],
                "messages": state["messages"],
                "dispute_outcome": state["outcome"],
            },
        )

    def system2(request):
        assert request.headers["X-Gawah-Key"] == "engine-key"
        if state["system2_failure"]:
            return httpx.Response(503)
        if request.method == "POST":
            writes.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={**PAYMENT, "status": "REFUNDED"})
        if request.url.path == "/v1/ledger/head":
            return httpx.Response(
                200,
                json={
                    "seq": 1,
                    "hash": "hash",
                    "message": "head",
                    "signature": "real",
                    "key_id": "engine-key",
                },
            )
        assert request.url.path == f"/v1/payments/{PAYMENT['id']}"
        return httpx.Response(200, json=PAYMENT)

    with TestClient(
        create_app(
            settings,
            paypal_transport=httpx.MockTransport(paypal),
            system2_transport=httpx.MockTransport(system2),
        )
    ) as client:
        yield client, engine, writes, state
    engine.dispose()


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def start(client, dispute=1, actor="alice"):
    return ok(client.post(URL, headers=headers(actor), json={"dispute_id": dispute}))


def command_for(client, row, endpoint, actor="alice", **body):
    return client.post(
        f"{URL}/{row['id']}/{endpoint}",
        headers=headers(actor),
        json={"revision": row["revision"], **body},
    )


def review(client, row, name, actor="alice", **body):
    return ok(command_for(client, row, "review", actor, command=name, **body))


def approved(client, dispute=1, action="submit-evidence"):
    row = start(client, dispute)
    row = review(
        client,
        row,
        "prepare",
        action=action,
        text="Reviewed text.",
        evidence_ids=["e-payment"] if action == "submit-evidence" else [],
    )
    return review(client, row, "approve")


def test_review_and_execute_exact_approved_version(n5):
    client, _, writes, state = n5
    assert client.get(URL, headers=KEY).status_code == 401
    assert client.post(URL, headers=headers("view"), json={"dispute_id": 1}).status_code == 403
    assert client.post(URL, headers=headers("alice"), json={"dispute_id": 2}).status_code == 409
    assert (
        client.post(URL, headers=headers("alice"), json={"dispute_id": 4}).json()["error"]["code"]
        == "COUNSEL_REQUIRED"
    )
    row = start(client)
    assert (
        start(client)["id"] == row["id"] and row["counsel"]["draft_response"] == "Original AI text."
    )
    assert "must not be exported" not in str(row)
    assert command_for(client, row, "submit", "sam", confirmation="PP-D-ONE").status_code == 409
    assert command_for(client, row, "review", command="approve").status_code == 409
    missing = command_for(
        client, row, "review", command="prepare", action="submit-evidence", text="Text"
    )
    assert missing.json()["error"]["code"] == "EVIDENCE_REQUIRED"
    row = review(
        client, row, "prepare", action="submit-evidence", text="First", evidence_ids=["e-payment"]
    )
    assert command_for(client, row, "review", "sam", command="approve").status_code == 403
    row = review(client, row, "approve")
    first = row["approval"]
    row = review(
        client, row, "prepare", action="submit-evidence", text="Edited", evidence_ids=["e-payment"]
    )
    assert row["approval"] is None
    assert command_for(client, row, "submit", "sam", confirmation="PP-D-ONE").status_code == 409
    row = review(client, row, "approve")
    assert row["approval"]["actor_id"] == "alice" and row["approval"]["created_at"]
    assert first["details"]["package"] != row["approval"]["details"]["package"]
    assert first["id"] in {event["id"] for event in row["activity"]}
    assert command_for(client, row, "submit", confirmation="PP-D-ONE").status_code == 403
    assert command_for(client, row, "submit", "sam", confirmation="wrong").status_code == 422
    result = ok(command_for(client, row, "submit", "sam", confirmation="PP-D-ONE"))
    payload = row["approval"]["details"]["package"]["payload"]
    assert writes == [("/v1/customer/disputes/PP-D-ONE/provide-evidence", payload)]
    assert (
        result["status"] == "SUBMITTED" and result["attempts"][0]["response_reference"] == "debug-1"
    )
    assert command_for(client, row, "submit", "sam", confirmation="PP-D-ONE").status_code == 409
    state["evidence"] = [
        {
            "source": "SUBMITTED_BY_SELLER",
            "evidence_type": "OTHER",
            "date": now().isoformat(),
            "notes": payload["evidences"][0]["notes"],
        }
    ]
    result = ok(command_for(client, result, "reconcile"))
    assert result["status"] == "CONFIRMED"
    feed = ok(client.get("/api/dashboard/activity", headers=KEY))
    assert any(event["type"] == "RESOLUTION_APPROVED" for event in feed["items"])
    assert "Edited" not in str(feed) and "alice" not in str(feed)
    assert ok(client.get("/api/dashboard/resolution", headers=KEY))["confirmed"] == 1
    assert (
        ok(
            client.get(
                URL + "?search=PP-D-ONE&status=CONFIRMED&page_size=1", headers=headers("view")
            )
        )["total"]
        == 1
    )


def test_simulated_concede_refunds_through_system2_without_paypal(n5):
    client, engine, writes, state = n5
    with Session(engine) as db:
        publish(db, 2, version=3, recommendation="CONCEDE_REMEDY")
    state["forbid_paypal"] = True
    row = start(client, dispute=2)
    options = ok(client.get(f"{URL}/{row['id']}/options", headers=headers("alice")))
    assert options["options"] == [
        {
            "action": "refund",
            "amount_minor": 6500,
            "currency": "USD",
            "label": "Simulated dispute, sandbox refund",
        }
    ]
    row = review(client, row, "prepare", action="refund", text="Reviewed refund.")
    assert row["plan"]["paypal_status"] == "SIMULATED_SANDBOX"
    row = review(client, row, "approve")
    result = ok(command_for(client, row, "submit", "sam", confirmation="PP-D-SIM"))
    assert result["status"] == "CONFIRMED"
    assert writes == [(f"/v1/payments/{PAYMENT['id']}/refund", {"reason": "Reviewed refund."})]


def test_simulated_cases_remain_read_only_when_system2_is_not_local(n5):
    _, engine, _, _ = n5
    settings = Settings(
        _env_file=None,
        enable_sandbox_resolution=True,
        system2_base_url="https://system2.example.test",
    )
    with httpx.Client() as http, Session(engine) as db:
        assert disabled_reason(db.get(Dispute, 2), PayPalClient(settings, http)) == (
            "Simulated disputes are read-only."
        )


def test_assigned_review_and_fresh_inputs_are_required_at_execution(n5):
    client, engine, writes, _ = n5
    row = approved(client)
    denied = command_for(client, row, "review", "bob", command="cancel", text="Cancel.")
    assert denied.status_code == 403
    with Session(engine) as db:
        latest = publish(db, 1, version=3)
    stale = command_for(client, row, "submit", "sam", confirmation="PP-D-ONE")
    assert stale.json()["error"]["code"] == "REVIEW_STALE"
    row = review(client, row, "refresh")
    assert row["analysis_id"] == latest and row["status"] == "PENDING_REVIEW"
    assert row["approval"] is None and row["plan"] is None and not writes


def test_information_requests_and_new_analysis_require_review(n5):
    client, engine, writes, _ = n5
    with Session(engine) as db:
        publish(db, 1, version=3, recommendation="REQUEST_MORE_EVIDENCE")
    row = start(client)
    assert row["status"] == "ADDITIONAL_INFORMATION_REQUIRED"
    row = review(
        client, row, "request-info", team="Fulfillment", text="Please provide the receipt."
    )
    error = command_for(
        client, row, "review", command="prepare", action="reply-inquiry", text="Reply"
    )
    assert error.json()["error"]["code"] == "INFORMATION_OUTSTANDING"
    row = review(
        client, row, "receive-info", request_id=row["requests"][0]["id"], text="Receipt located."
    )
    assert row["status"] == "PENDING_REVIEW" and row["requests"][0]["fulfilled_at"]
    with Session(engine) as db:
        latest = publish(db, 1, version=4)
    assert ok(client.get(f"{URL}/{row['id']}", headers=headers("alice")))["stale"]
    assert (
        command_for(
            client, row, "review", command="prepare", action="reply-inquiry", text="Reply"
        ).status_code
        == 409
    )
    row = review(client, row, "refresh")
    assert row["analysis_id"] == latest and row["evidence_version"] == 4 and not row["stale"]
    assert row["requests"][0]["response"] == "Receipt located."
    assert not writes


@pytest.mark.parametrize("decision,status", [("cancel", "CANCELLED"), ("reject", "REJECTED")])
def test_terminal_decisions_prevent_submission_and_reopening_same_analysis(n5, decision, status):
    client, _, writes, _ = n5
    row = approved(client)
    result = review(client, row, decision, text="Operator decision.")
    assert result["status"] == status and result["approval"] is None
    assert start(client)["id"] == row["id"]
    assert command_for(client, result, "submit", "sam", confirmation="PP-D-ONE").status_code == 409
    assert not writes


@pytest.mark.parametrize(
    "action,expected",
    [
        ("refund", {"reason": "Reviewed text."}),
        ("accept-claim", {"accept_claim_type": "REFUND", "note": "Reviewed text."}),
        (
            "offer-refund",
            {
                "offer_type": "REFUND",
                "offer_amount": {"currency_code": "USD", "value": "65.00"},
                "note": "Reviewed text.",
            },
        ),
    ],
)
def test_monetary_actions_use_provider_amount_and_prevent_competing_remedies(n5, action, expected):
    client, _, writes, _ = n5
    row = approved(client, dispute=3, action=action)
    result = ok(command_for(client, row, "submit", "sam", confirmation="PP-D-THREE"))
    assert result["status"] == ("CONFIRMED" if action == "refund" else "SUBMITTED")
    assert writes[0][1] == expected and len(writes) == 1
    other = start(client)
    blocked = command_for(
        client,
        other,
        "review",
        command="prepare",
        action="refund",
        text="Additional refund",
        override_reason="Reviewed.",
    )
    assert blocked.json()["error"]["code"] == "ALREADY_ATTEMPTED"


def test_invalid_case_and_partial_engine_failures_do_not_execute_money(n5):
    client, _, writes, state = n5
    row = approved(client, dispute=3, action="accept-claim")
    state["system2_failure"] = True
    response = command_for(client, row, "submit", "sam", confirmation="PP-D-THREE")
    assert response.json()["error"]["code"] == "ACTION_UNAVAILABLE"
    state["system2_failure"] = False
    state["status"] = "UNDER_REVIEW"
    assert command_for(client, row, "submit", "sam", confirmation="PP-D-THREE").status_code == 409
    assert not writes


def test_provider_auth_failure_records_failure_and_requires_new_approval(n5):
    client, _, writes, state = n5
    row = approved(client, action="reply-inquiry")
    state["response"] = 403
    result = ok(command_for(client, row, "submit", "sam", confirmation="PP-D-ONE"))
    assert result["status"] == "FAILED" and result["approval"] is None
    assert result["activity"][-1]["details"]["upstream_status"] == 403
    assert command_for(client, result, "submit", "sam", confirmation="PP-D-ONE").status_code == 409
    assert len(writes) == 1


def test_inquiry_reply_checks_existing_seller_message_and_reconciles(n5):
    client, _, writes, state = n5
    row = approved(client, action="reply-inquiry")
    payload = row["plan"]["payload"]
    message = {
        "posted_by": "SELLER",
        "content": payload["message"],
        "time_posted": now().isoformat(),
    }
    state["messages"] = [message]
    blocked = command_for(client, row, "submit", "sam", confirmation="PP-D-ONE")
    assert blocked.json()["error"]["code"] == "RESPONSE_ALREADY_RECORDED"
    assert not writes
    state["messages"] = []
    result = ok(command_for(client, row, "submit", "sam", confirmation="PP-D-ONE"))
    assert writes == [
        ("/v1/customer/disputes/PP-D-ONE/send-message", {"message": "Reviewed text."})
    ]
    state["messages"] = [{**message, "time_posted": now().isoformat()}]
    assert ok(command_for(client, result, "reconcile"))["status"] == "CONFIRMED"


def test_timeout_reconciles_only_provider_confirmed_outcome_without_resubmitting(n5):
    client, _, writes, state = n5
    row = approved(client, dispute=3, action="accept-claim")
    state["timeout"] = True
    result = ok(command_for(client, row, "submit", "sam", confirmation="PP-D-THREE"))
    assert result["status"] == "UNKNOWN" and "sensitive provider" not in str(result)
    assert (
        command_for(client, result, "submit", "sam", confirmation="PP-D-THREE").status_code == 409
    )
    state["status"] = "RESOLVED"
    result = ok(command_for(client, result, "reconcile"))
    assert result["status"] == "UNKNOWN"
    state["outcome"] = {
        "outcome_code": "ACCEPTED",
        "amount_refunded": {"value": "65.00", "currency_code": "USD"},
    }
    result = ok(command_for(client, result, "reconcile"))
    assert result["status"] == "CONFIRMED" and result["paypal_status"] == "RESOLVED"
    assert len(writes) == 1
