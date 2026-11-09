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
from gawah_contract.carts import generate_keypair, public_key_pem, sign_cart
from gawah_contract.fixtures import load
from sqlalchemy import func, select
from sqlalchemy.orm import Session

HEADERS = {"X-Gawah-Key": "console-key"}
MOCK_BUNDLE = "/v1/evidence/pay_04"
ENGINE_VERIFY = "/v1/ledger/verify"
MERCHANT_KEY = "/v1/merchants/demo-shoe-store/public-key"


class Engine:
    def __init__(self):
        self.requests = []
        self.responses = {
            MOCK_BUNDLE: load("evidence_bundle_wrong_size"),
            ENGINE_VERIFY: {"ok": True, "checked": 5},
            MERCHANT_KEY: {
                "merchant_id": "demo-shoe-store",
                "key_id": "demo-shoe-store-key-1",
                "alg": "ed25519",
                "public_key_pem": "fixture",
            },
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


def test_collects_mock_bundle_and_reuses_n1_dispute_without_linking_it(environment):
    client, stub, engine = environment
    pack = assemble(client)
    assert stub.requests == [(MOCK_BUNDLE, {}), (MERCHANT_KEY, {}), (ENGINE_VERIFY, {})]
    assert pack["payment_id"] == "pay_04"
    assert pack["dispute_id"] == "PP-D-FAKE-1002"
    by_type = {item["type"]: item for item in pack["evidence"]}
    assert by_type["dispute"]["payload"]["source"] == "SIMULATED_FIXTURE"
    assert by_type["payment"]["source_system"] == "MOCK_ENGINE"
    assert by_type["signed_cart"]["timestamp"] is None
    assert by_type["signed_cart"]["verification_status"] == "UNAVAILABLE"
    assert by_type["proposal"]["timestamp"] == "2026-11-09T10:05:00Z"
    assert by_type["proposal"]["retrieved_at"]
    assert pack["verification_summary"]["ledger"]["status"] == "VERIFIED"
    assert "Mock bundle" in pack["verification_summary"]["ledger"]["detail"]
    assert pack["verification_summary"]["ledger"]["reference"] == f"{MOCK_BUNDLE}#verification"
    assert pack["verification_summary"]["mandate_signature"]["status"] == "UNAVAILABLE"
    assert {"ORDER_UNAVAILABLE", "DELIVERY_UNAVAILABLE"} <= codes(pack, "missing_evidence")
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


def test_partial_retrieval_reports_timeouts_and_filters_fallback_ledger(environment):
    client, stub, _ = environment
    bundle = stub.responses[MOCK_BUNDLE]
    stub.responses.update(
        {
            MOCK_BUNDLE: 501,
            ENGINE_VERIFY: "timeout",
            "/v1/payments/pay_04": bundle["payment"],
            "/v1/ledger": bundle["ledger_entries"],
        }
    )
    pack = assemble(client)
    assert ("/v1/ledger", {"subject_id": "pay_04"}) in stub.requests
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


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("valid", "VERIFIED"),
        ("tampered", "INVALID"),
        ("rotated", "UNAVAILABLE"),
    ],
)
def test_uses_shared_cart_verifier_only_with_matching_key(environment, scenario, expected):
    client, stub, _ = environment
    bundle = stub.responses[MOCK_BUNDLE]
    key = generate_keypair()
    cart = sign_cart(bundle["signed_cart"], key, key_id="merchant-key-1").model_dump(mode="json")
    if scenario == "tampered":
        cart["total_minor"] += 1
    bundle["signed_cart"] = bundle["proposal"]["signed_cart"] = cart
    stub.responses[MERCHANT_KEY] = {
        "merchant_id": cart["merchant_id"],
        "key_id": "merchant-key-2" if scenario == "rotated" else cart["key_id"],
        "alg": "ES256",
        "public_key_pem": public_key_pem(key),
    }
    pack = assemble(client)
    assert pack["verification_summary"]["cart_signature"]["status"] == expected
    assert (
        next(item for item in pack["evidence"] if item["type"] == "signed_cart")[
            "verification_status"
        ]
        == expected
    )


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
    assert pack["verification_summary"]["ledger"]["status"] == "UNAVAILABLE"
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
