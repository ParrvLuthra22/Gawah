import json
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from counsel.config import Settings
from counsel.main import create_app
from counsel.models import Dispute, Source, now
from fastapi.testclient import TestClient
from gawah_contract.fixtures import load
from sqlalchemy.orm import Session

KEY = {"X-Gawah-Key": "console-key"}
BASE = "https://api-m.sandbox.paypal.com/v1/customer/disputes"
CAPTURE = "8JT668275T605350T"
PAYMENT = {**load("payment_paid"), "paypal_capture_id": CAPTURE}
CASE = "/api/disputes/1/resolution"
CHECKS = [{"name": "cart_signature", "ok": False, "detail": "Signature mismatch."}]


def test_default_database_and_optional_postgres(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    url = Settings(_env_file=None).database_dsn
    assert url.get_backend_name() == "sqlite"
    assert Path(url.database) == Path(__file__).resolve().parents[1] / "counsel.db"
    assert Settings(
        _env_file=None, database_url="postgres://host/test"
    ).database_dsn.drivername == ("postgresql+psycopg")


@pytest.fixture
def console(tmp_path, monkeypatch):
    dsn = f"sqlite:///{(tmp_path / 'counsel.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", dsn)
    migrations = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(migrations, "head")
    settings = Settings(
        _env_file=None,
        database_url=dsn,
        system3_key="console-key",
        system2_key="engine-key",
        paypal_client_id="test-id",
        paypal_client_secret="test-secret",
        enable_sandbox_resolution=True,
    )
    engine = settings.database_engine()
    with Session(engine) as db:
        for dispute_id, source, payment_id in [
            ("PP-D-ONE", Source.PAYPAL_API, PAYMENT["id"]),
            ("PP-D-SIMULATED", Source.SIMULATED_FIXTURE, "pay_04"),
            ("PP-D-OTHER", Source.PAYPAL_API, PAYMENT["id"]),
        ]:
            db.add(
                Dispute(
                    paypal_dispute_id=dispute_id,
                    source=source,
                    payment_id=payment_id,
                    link_status="LINKED",
                    status="OPEN",
                    raw_response={},
                    last_synced_at=now(),
                )
            )
        db.commit()
    engine.dispose()

    writes = []
    flags = {
        "mock_head": False,
        "paypal_timeout": False,
        "foreign_action": False,
        "capture": CAPTURE,
    }

    def paypal(request):
        path = request.url.path
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "test-token", "expires_in": 300})
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.method == "POST":
            writes.append((path, json.loads(request.content)))
            if flags["paypal_timeout"]:
                raise httpx.ReadTimeout("private upstream timeout")
            return httpx.Response(204)
        case = path.rsplit("/", 1)[-1]
        base = "https://other.test/v1/customer/disputes" if flags["foreign_action"] else BASE
        return httpx.Response(
            200,
            json={
                "dispute_id": case,
                "status": "WAITING_FOR_SELLER_RESPONSE",
                "dispute_amount": {"value": "65.00", "currency_code": "USD"},
                "disputed_transactions": [{"seller_transaction_id": flags["capture"]}],
                "links": [
                    {"rel": rel, "href": f"{base}/{case}/{rel}", "method": "POST"}
                    for rel in ("accept-claim", "provide-evidence")
                ],
                "allowed_response_options": {"accept_claim": {"accept_claim_types": ["REFUND"]}},
                "evidences": [{"evidence_type": "OTHER", "source": "REQUESTED_FROM_SELLER"}],
            },
        )

    def system2(request):
        assert request.headers["X-Gawah-Key"] == "engine-key"
        path = request.url.path
        head = {
            "seq": 5,
            "hash": "hash",
            "message": "gawah-ledger-head:5:hash",
            "signature": "SIMULATED-no-signature" if flags["mock_head"] else "signature",
            "key_id": "mock" if flags["mock_head"] else "gawah-ledger-key-1",
        }
        bundle = load("evidence_bundle_wrong_size")
        bundle["verification"] = {"ok": False, "checked": 5, "checks": CHECKS}
        responses = {
            "/v1/mandates": [load("mandate_shoes")],
            "/v1/mandates/man_01": load("mandate_shoes"),
            "/v1/mandates/man_01/budget": load("budget_status"),
            "/v1/ledger": load("ledger_chain"),
            "/v1/ledger/head": head,
            "/v1/ledger/verify": {"ok": True, "checked": 5},
            "/v1/evidence/pay_04": bundle,
            f"/v1/payments/{PAYMENT['id']}": PAYMENT,
        }
        if path == f"/v1/payments/{PAYMENT['id']}/refund":
            assert request.method == "POST"
            writes.append((path, json.loads(request.content)))
            return httpx.Response(200, json={**PAYMENT, "status": "REFUNDED"})
        assert request.method == "GET"
        return (
            httpx.Response(200, json=responses[path]) if path in responses else httpx.Response(404)
        )

    def stream(request):
        assert request.headers["X-Gawah-Key"] == "engine-key"
        assert request.url.path == "/v1/ledger/stream"
        if "Last-Event-ID" in request.headers:
            assert request.headers["Last-Event-ID"] == "4"
            assert not request.url.params
        else:
            assert request.url.params["since_seq"] == "4"
        entry = load("ledger_chain")[-1]
        body = f"id: 5\nevent: ledger\ndata: {json.dumps(entry)}\n\n"
        return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})

    with TestClient(
        create_app(
            settings,
            paypal_transport=httpx.MockTransport(paypal),
            system2_transport=httpx.MockTransport(system2),
            system2_stream_transport=httpx.MockTransport(stream),
        )
    ) as client:
        yield client, settings, writes, flags


def test_authenticated_mandates_ledger_and_named_checks(console):
    client, *_ = console
    assert client.get("/api/mandates").status_code == 401
    assert client.get("/api/ledger/stream").status_code == 401
    assert client.get("/api/mandates", headers=KEY).json()[0]["id"] == "man_01"
    assert client.get("/api/mandates/man_01", headers=KEY).json()["id"] == "man_01"
    assert client.get("/api/mandates/man_01/budget", headers=KEY).json() == load("budget_status")
    assert client.get("/api/mandates/unknown", headers=KEY).status_code == 404
    assert client.get("/api/ledger", headers=KEY).json()[-1]["seq"] == 5
    assert client.get("/api/ledger/head", headers=KEY).json()["seq"] == 5
    assert client.get("/api/ledger/verify", headers=KEY).json()["checks"] == []
    assert (
        client.get("/api/ledger/payments/pay_04/verification", headers=KEY).json()["checks"]
        == CHECKS
    )


def test_ledger_stream_preserves_sse_and_prefers_reconnect_cursor(console):
    client, *_ = console
    first = client.get("/api/ledger/stream?since_seq=4", headers=KEY)
    reconnect = client.get("/api/ledger/stream?since_seq=0", headers={**KEY, "Last-Event-ID": "4"})
    assert first.status_code == reconnect.status_code == 200
    assert reconnect.headers["content-type"].startswith("text/event-stream")
    assert first.text == reconnect.text and "id: 5\nevent: ledger" in first.text
    assert (
        client.get("/api/ledger/stream", headers={**KEY, "Last-Event-ID": "bad"}).status_code == 422
    )


def test_legacy_direct_write_requires_resolution_workflow(console):
    client, _, writes, _ = console
    assert client.post(f"{CASE}/refund").status_code == 401
    result = client.post(f"{CASE}/refund", headers=KEY, json={"confirmation": "PP-D-ONE"})
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "APPROVAL_REQUIRED"
    assert not writes


def test_legacy_options_label_simulations_and_reject_mock_or_foreign_actions(console):
    client, settings, writes, flags = console
    simulated = "/api/disputes/2/resolution"
    assert client.get(simulated, headers=KEY).json()["message"] == (
        "No supported resolution action is currently available for this case."
    )
    settings.enable_sandbox_resolution = False
    assert not client.get(CASE, headers=KEY).json()["options"]
    settings.enable_sandbox_resolution = True
    flags["mock_head"] = flags["foreign_action"] = True
    assert not client.get(CASE, headers=KEY).json()["options"]
    flags["mock_head"] = False
    flags["capture"] = "DIFFERENT"
    assert not client.get(CASE, headers=KEY).json()["options"]
    assert not writes
