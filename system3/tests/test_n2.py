from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from counsel.clients import System2Client
from counsel.config import Settings
from counsel.main import create_app
from counsel.models import Dispute, EvidencePack, now
from counsel.sync import seed_fixtures
from fastapi.testclient import TestClient
from gawah_contract.fixtures import load
from sqlalchemy import func, select
from sqlalchemy.orm import Session

HEADERS = {"X-Gawah-Key": "console-key"}
MOCK_BUNDLE = "/v1/evidence/pay_04"
ENGINE_VERIFY = "/v1/ledger/verify"


class Engine:
    def __init__(self):
        self.requests = []
        self.responses = {
            MOCK_BUNDLE: load("evidence_bundle_wrong_size"),
            ENGINE_VERIFY: {"ok": True, "checked": 5},
            "/v1/payments": [load("payment_paid")],
        }

    def __call__(self, request):
        assert request.method == "GET"
        assert request.headers["X-Gawah-Key"] == "engine-key"
        self.requests.append((request.url.path, dict(request.url.params)))
        response = self.responses.get(request.url.path, 404)
        if response == "timeout":
            raise httpx.ReadTimeout("private upstream detail")
        if isinstance(response, int):
            return httpx.Response(response)
        return httpx.Response(200, json=response)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'evidence.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    migrations = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(migrations, "0001_disputes")
    settings = Settings(
        _env_file=None,
        database_url=url,
        system3_key="console-key",
        system2_key="engine-key",
    )
    stub = Engine()
    transport = httpx.MockTransport(stub)
    engine = settings.database_engine()
    with (
        Session(engine) as db,
        httpx.Client(base_url=str(settings.system2_base_url), transport=transport) as http,
    ):
        seed_fixtures(db, System2Client(settings, http))
    command.upgrade(migrations, "head")
    stub.requests.clear()
    with TestClient(create_app(settings, system2_transport=transport)) as client:
        yield client, stub, engine
    engine.dispose()


def assemble(client, payment="pay_04"):
    response = client.post(f"/api/evidence/{payment}/assemble", headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def codes(pack, group):
    return {issue["code"] for issue in pack[group]}


def real_bundle(stub):
    bundle = stub.responses[MOCK_BUNDLE]
    bundle["mandate"].update(signing_alg="ES256", signature="es256-mandate")
    for cart in (bundle["signed_cart"], bundle["proposal"]["signed_cart"]):
        cart["signature"] = "es256-cart"
    for entry in bundle["ledger_entries"]:
        entry["signature"] = "es256-ledger"
        if entry["type"] == "PAYMENT_COMPLETED":
            entry["data"]["paypal_order_id"] = "ORDER1"
    bundle["payment"].update(paypal_order_id="ORDER1", paypal_capture_id="CAPTURE1")
    return bundle


def test_collects_mock_bundle_and_reuses_n1_dispute_without_linking_it(environment):
    client, stub, engine = environment
    pack = assemble(client)
    assert stub.requests == [(MOCK_BUNDLE, {}), (ENGINE_VERIFY, {})]
    assert pack["payment_id"] == "pay_04"
    assert pack["dispute_id"] == "PP-D-FAKE-1002"
    by_type = {item["type"]: item for item in pack["evidence"]}
    assert by_type["dispute"]["payload"]["source"] == "SIMULATED_FIXTURE"
    assert by_type["dispute"]["source_type"] == "SIMULATED_FIXTURE"
    assert by_type["payment"]["source_system"] == "MOCK_ENGINE"
    assert by_type["payment"]["source_type"] == "MOCK_RESPONSE"
    assert by_type["signed_cart"]["timestamp"] is None
    assert by_type["signed_cart"]["verification_status"] == "UNVERIFIED"
    assert by_type["proposal"]["timestamp"] == "2026-11-09T10:05:00Z"
    assert by_type["proposal"]["retrieved_at"]
    assert pack["verification_summary"]["ledger"]["status"] == "UNVERIFIED"
    assert (
        pack["verification_summary"]["bundle_result"] == stub.responses[MOCK_BUNDLE]["verification"]
    )
    assert pack["verification_summary"]["engine_ledger"]["status"] == "VERIFIED"
    assert "mock chain" in pack["verification_summary"]["engine_ledger"]["detail"]
    assert pack["verification_summary"]["mandate_signature"]["status"] == "UNVERIFIED"
    assert {"ORDER_UNAVAILABLE", "FULFILLMENT_UNAVAILABLE", "DELIVERY_UNAVAILABLE"} <= codes(
        pack, "missing_evidence"
    )
    [contradiction] = pack["contradictions"]
    assert contradiction["code"] == "AUTHORIZED_SIZE_MISMATCH"
    assert contradiction["evidence_ids"] == ["mandate:man_01", "signed_cart:cart_04"]
    assert len(contradiction["source_references"]) == 2
    events = pack["timeline"]
    dated = [event["timestamp"] for event in events if event["timestamp"]]
    assert dated == sorted(dated)
    assert events[-1]["timestamp"] is None
    assert len([event for event in events if event["type"] == "MANDATE_CREATED"]) == 1
    assert "DELIVERED" not in {event["type"] for event in events}
    assert len({item["id"] for item in pack["evidence"]}) == len(pack["evidence"])
    with Session(engine) as db:
        assert db.get(Dispute, pack["dispute_record_id"]).payment_id is None
    for subpath, field in (("timeline", "timeline"), ("verification", "verification_summary")):
        assert client.get(f"/api/evidence/pay_04/{subpath}", headers=HEADERS).json() == pack[field]
    assert (
        client.get("/api/evidence/pay_04/issues", headers=HEADERS).json()["contradictions"]
        == pack["contradictions"]
    )


def test_persisted_versions_ignore_retrieval_times_and_regenerate_changed_evidence(environment):
    client, stub, engine = environment
    first = assemble(client)
    with Session(engine) as db:
        db.get(Dispute, first["dispute_record_id"]).last_synced_at = now()
        db.commit()
    assert assemble(client) == first
    stub.responses[MOCK_BUNDLE]["payment"]["amount_minor"] = 6400
    revised = assemble(client)
    assert revised["version"] == 2 and revised["id"] != first["id"]
    assert "PAYMENT_AMOUNT_MISMATCH" in codes(revised, "contradictions")
    assert client.get("/api/evidence/pay_04", headers=HEADERS).json() == revised
    assert client.get("/api/evidence/pay_04?version=1", headers=HEADERS).json() == first
    with Session(engine) as db:
        assert db.scalar(select(func.count(EvidencePack.id))) == 2


def test_concurrent_identical_assembly_reuses_the_saved_version(environment):
    client, _, engine = environment
    with ThreadPoolExecutor(max_workers=2) as pool:
        packs = list(pool.map(assemble, [client, client]))
    assert packs[0] == packs[1]
    with Session(engine) as db:
        assert db.scalar(select(func.count(EvidencePack.id))) == 1


@pytest.mark.parametrize("bundle_status", [403, 429, 503])
def test_partial_retrieval_reports_timeouts_and_filters_fallback_ledger(environment, bundle_status):
    client, stub, _ = environment
    bundle = stub.responses[MOCK_BUNDLE]
    stub.responses.update(
        {
            MOCK_BUNDLE: bundle_status,
            ENGINE_VERIFY: "timeout",
            "/v1/payments/pay_04": bundle["payment"],
            "/v1/ledger": bundle["ledger_entries"],
        }
    )
    pack = assemble(client)
    assert ("/v1/ledger", {"subject_id": "pay_04"}) in stub.requests
    assert ("/v1/payments/pay_04", {}) in stub.requests
    assert {
        item["source_record_id"] for item in pack["evidence"] if item["type"] == "ledger_entry"
    } == {"4", "5"}
    assert pack["verification_summary"]["ledger"]["status"] == "UNAVAILABLE"
    assert pack["completeness"]["verified"] == 0
    assert {
        "MISSING_MANDATE",
        "MISSING_PROPOSAL",
        "MISSING_SIGNED_CART",
        "MISSING_DECISION",
        "MISSING_LEDGER_VERIFICATION",
    } <= codes(pack, "missing_evidence")
    assert "private upstream detail" not in str(pack)
    assert not pack["invalid_evidence"]
    assert any(
        check["status"] == "UNAVAILABLE" and check["reference"] == MOCK_BUNDLE
        for check in pack["sources"]
    )
    assert any(
        check["status"] == "UNAVAILABLE" and check["reference"] == ENGINE_VERIFY
        for check in pack["sources"]
    )


def test_invalid_verification_and_malformed_records_are_separate(environment):
    client, stub, _ = environment
    bundle = stub.responses[MOCK_BUNDLE]
    bundle["decision"] = {"id": "not-a-decision"}
    bundle["verification"] = {
        "ok": False,
        "checked": 3,
        "first_broken_seq": 3,
        "reason": "Hash mismatch.",
    }
    stub.responses[ENGINE_VERIFY] = {"ok": False, "checked": 1, "first_broken_seq": 1}
    pack = assemble(client)
    assert pack["verification_summary"]["ledger"]["result"]["reason"] == "Hash mismatch."
    assert {
        "INVALID_DECISION",
        "INVALID_LEDGER_VERIFICATION",
        "INVALID_ENGINE_LEDGER_VERIFICATION",
    } <= codes(pack, "invalid_evidence")
    assert "MISSING_DECISION" in codes(pack, "missing_evidence")
    assert [
        item["source_record_id"]
        for item in pack["evidence"]
        if item["verification_status"] == "INVALID"
    ] == ["3"]


def test_real_bundle_maps_named_checks_and_preserves_wire_results(environment):
    client, stub, _ = environment
    bundle = real_bundle(stub)
    bundle["verification"] = {
        "ok": False,
        "checked": 12,
        "first_broken_seq": None,
        "reason": "Cart signature failed.",
        "checks": [
            {"name": "ledger_chain", "ok": True, "detail": "12 entries verified"},
            {"name": "mandate_signature", "ok": True, "detail": "ES256 by mandate key"},
            {"name": "cart_signature", "ok": False, "detail": "Cart signature failed."},
            {"name": "payment_reference_match", "ok": True, "detail": "References match"},
        ],
    }
    stub.responses[ENGINE_VERIFY] = {"ok": True, "checked": 12, "checks": []}
    pack = assemble(client)
    summary = pack["verification_summary"]
    assert summary["bundle_result"] == bundle["verification"]
    assert summary["engine_ledger"]["result"] == stub.responses[ENGINE_VERIFY]
    items = {item["type"]: item for item in pack["evidence"]}
    for name, item_type, expected in (
        ("ledger", "ledger_entry", "VERIFIED"),
        ("mandate_signature", "mandate", "VERIFIED"),
        ("cart_signature", "signed_cart", "INVALID"),
        ("payment_reference_match", "payment", "VERIFIED"),
    ):
        item = items[item_type]
        assert item["source_type"] == "REAL_RECORD"
        assert item["source_system"] == "SYSTEM_2"
        assert item["verification_status"] == expected
        assert item["verification"] == summary[name]
    assert items["proposal"]["verification_status"] == "UNVERIFIED"
    assert "INVALID_CART_SIGNATURE" in codes(pack, "invalid_evidence")
    assert "INVALID_LEDGER_VERIFICATION" not in codes(pack, "invalid_evidence")
    assert stub.requests == [(MOCK_BUNDLE, {}), (ENGINE_VERIFY, {})]


def test_missing_named_result_does_not_inherit_bundle_success(environment):
    client, stub, _ = environment
    bundle = real_bundle(stub)
    bundle["verification"]["checks"] = [
        {"name": "mandate_signature", "ok": True, "detail": "ES256 by mandate key"},
    ]
    pack = assemble(client)
    items = {item["type"]: item for item in pack["evidence"]}
    assert items["payment"]["source_type"] == "REAL_RECORD"
    assert items["payment"]["verification_status"] == "UNVERIFIED"
    assert pack["verification_summary"]["ledger"]["status"] == "UNVERIFIED"
    assert pack["verification_summary"]["cart_signature"]["status"] == "UNVERIFIED"
    assert "MISSING_PAYMENT_REFERENCE_MATCH" in codes(pack, "missing_evidence")


def test_named_ledger_failure_marks_only_the_broken_entry_and_versions_the_change(environment):
    client, stub, _ = environment
    bundle = real_bundle(stub)
    bundle["verification"]["checks"] = [
        {"name": "ledger_chain", "ok": True, "detail": "Chain verified"},
    ]
    first = assemble(client)
    bundle["verification"].update(ok=False, first_broken_seq=3)
    bundle["verification"]["checks"][0].update(ok=False, detail="Broken sequence 3")
    revised = assemble(client)
    assert revised["version"] == first["version"] + 1
    items = {
        item["source_record_id"]: item
        for item in revised["evidence"]
        if item["type"] == "ledger_entry"
    }
    assert items["3"]["verification_status"] == "INVALID"
    assert items["4"]["verification_status"] == "UNVERIFIED"
    [issue] = [i for i in revised["invalid_evidence"] if i["code"] == "INVALID_LEDGER_VERIFICATION"]
    assert issue["evidence_ids"] == ["ledger_entry:3"]
    assert client.get("/api/evidence/pay_04?version=1", headers=HEADERS).json() == first


def test_named_results_are_bound_to_the_bundle_records(environment):
    client, stub, _ = environment
    bundle = real_bundle(stub)
    stub.responses["/v1/payments/pay_04"] = bundle.pop("payment")
    stub.responses["/v1/mandates/man_01"] = bundle.pop("mandate")
    bundle["verification"]["checks"] = [
        {"name": "mandate_signature", "ok": True, "detail": "Signature verified"},
        {"name": "payment_reference_match", "ok": False, "detail": "References disagree"},
    ]
    pack = assemble(client)
    items = {item["type"]: item for item in pack["evidence"]}
    assert ("/v1/mandates/man_01", {}) in stub.requests
    assert items["mandate"]["verification_status"] == "UNVERIFIED"
    assert items["payment"]["verification_status"] == "UNVERIFIED"
    [issue] = [
        i for i in pack["invalid_evidence"] if i["code"] == "INVALID_PAYMENT_REFERENCE_MATCH"
    ]
    assert not issue["evidence_ids"]


def test_malformed_verification_cannot_verify_a_record(environment):
    client, stub, _ = environment
    bundle = real_bundle(stub)
    bundle["verification"]["checks"] = [
        {"name": "mandate_signature", "ok": "true", "detail": "Malformed result"},
    ]
    pack = assemble(client)
    summary = pack["verification_summary"]
    assert summary["bundle_result"] == bundle["verification"]
    assert summary["mandate_signature"]["status"] == "UNAVAILABLE"
    assert "INVALID_VERIFICATION" in codes(pack, "invalid_evidence")


def test_comparisons_require_matching_references_and_real_timestamps(environment):
    client, stub, _ = environment
    bundle = stub.responses[MOCK_BUNDLE]
    bundle["payment"].update(
        proposal_id="prop_other", amount_minor=1, created_at="2026-11-09T10:04:00Z"
    )
    bundle["ledger_entries"][-1]["data"]["paypal_order_id"] = "DIFFERENT_ORDER"
    pack = assemble(client)
    contradictions = codes(pack, "contradictions")
    assert {
        "PAYMENT_PROPOSAL_MISMATCH",
        "LEDGER_PAYMENT_REFERENCE_MISMATCH",
        "TIMESTAMP_DECISION_PAYMENT",
    } <= contradictions
    assert "PAYMENT_AMOUNT_MISMATCH" not in contradictions
    assert "DELIVERED_SIZE_MISMATCH" not in contradictions


def test_global_chain_check_cannot_substitute_for_uncovered_mock_bundle(environment):
    client, stub, _ = environment
    stub.responses[MOCK_BUNDLE]["verification"]["checked"] = 4
    pack = assemble(client)
    assert pack["verification_summary"]["engine_ledger"]["status"] == "VERIFIED"
    assert pack["verification_summary"]["ledger"]["status"] == "UNVERIFIED"
    assert pack["completeness"]["verified"] == 0


def test_unknown_and_ambiguous_references_and_auth(environment):
    client, stub, engine = environment
    assert client.get("/api/evidence/pay_04").status_code == 401
    assert (
        client.get("/api/evidence/pay_04", headers=HEADERS).json()["error"]["code"]
        == "EVIDENCE_PACK_NOT_FOUND"
    )
    assert client.post("/api/evidence/pay_unknown/assemble", headers=HEADERS).status_code == 404
    assert client.post("/api/evidence/bad.id/assemble", headers=HEADERS).status_code == 422
    with Session(engine) as db:
        original = db.scalar(select(Dispute).where(Dispute.paypal_dispute_id == "PP-D-FAKE-1002"))
        db.add(
            Dispute(
                paypal_dispute_id="PP-D-ANOTHER",
                payment_id="pay_04",
                link_status="LINKED",
                status="OPEN",
                source="PAYPAL_API",
                raw_response={},
                last_synced_at=now(),
            )
        )
        original_id = original.id
        db.commit()
    stub.requests.clear()
    assert client.post("/api/evidence/pay_04/assemble", headers=HEADERS).status_code == 409
    assert not stub.requests
    response = client.post(
        f"/api/evidence/pay_04/assemble?dispute_id={original_id}", headers=HEADERS
    )
    assert response.status_code == 200
    assert response.json()["dispute_id"] == "PP-D-FAKE-1002"
