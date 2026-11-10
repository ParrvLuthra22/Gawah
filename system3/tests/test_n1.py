import base64
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from counsel.clients import IntegrationError, PayPalClient, System2Client
from counsel.config import Settings
from counsel.main import create_app
from counsel.models import Dispute, DisputeStatusHistory
from counsel.sync import (
    PayPalDispute,
    Transaction,
    acquire_sync_lock,
    payment_reference,
    seed_fixtures,
)
from fastapi.testclient import TestClient
from gawah_contract import PaymentRecord
from gawah_contract.fixtures import load
from sqlalchemy import select, text
from sqlalchemy.orm import Session

HEADERS = {"X-Gawah-Key": "console-key"}
BASE = "https://api-m.sandbox.paypal.com"
PAYMENT = load("payment_paid")


class PayPalStub:
    def __init__(self):
        self.tokens = 0
        self.details = []
        self.calls = []
        self.status = "OPEN"
        self.update_time = "2026-11-09T14:00:00Z"
        self.failure = None
        self.expired = False
        self.next_link = f"{BASE}/v1/customer/disputes?page_size=50&page=2"

    def __call__(self, request):
        self.calls.append(request)
        path = request.url.path
        if path == "/v1/oauth2/token":
            assert request.method == "POST"
            assert request.headers["Authorization"] == (
                "Basic " + base64.b64encode(b"client-id:client-secret").decode()
            )
            assert request.content == b"grant_type=client_credentials"
            self.tokens += 1
            return httpx.Response(
                200, json={"access_token": f"token-{self.tokens}", "expires_in": 100}
            )
        assert request.headers["Authorization"] == f"Bearer token-{self.tokens}"
        assert request.method == "GET"
        if self.expired:
            self.expired = False
            return httpx.Response(401)
        if path == "/v1/customer/disputes":
            second = request.url.params.get("page") == "2"
            return httpx.Response(
                200,
                json={
                    "items": [{"dispute_id": "PP-D-TWO" if second else "PP-D-ONE"}],
                    "links": [] if second else [{"rel": "next", "href": self.next_link}],
                },
            )
        self.details.append(path)
        if path.endswith("TWO") and self.failure:
            if self.failure == "timeout":
                raise httpx.ReadTimeout("private timeout detail")
            return httpx.Response(403, json={"message": "private upstream response"})
        return httpx.Response(
            200,
            json={
                "dispute_id": path.rsplit("/", 1)[-1],
                "status": self.status,
                "update_time": self.update_time,
                "create_time": "2026-11-09T13:00:00+01:00",
                "seller_response_due_date": "2026-11-11T13:00:00Z",
                "dispute_amount": {"value": "65.00", "currency_code": "USD"},
                "reason": "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED",
                "disputed_transactions": [
                    {
                        "seller_transaction_id": PAYMENT["paypal_capture_id"]
                        if path.endswith("ONE")
                        else "NONE",
                        "buyer": {"name": "Sandbox Buyer"},
                        "seller": {"merchant_id": "merchant-one"},
                    }
                ],
                "messages": [{"content": "Stored in raw payload only."}],
            },
        )


class System2Stub:
    def __init__(self):
        self.payments = [PAYMENT]
        self.status = 200
        self.calls = 0

    def __call__(self, request):
        assert request.method == "GET" and request.url.path == "/v1/payments"
        assert request.headers["X-Gawah-Key"] == "engine-key"
        self.calls += 1
        return httpx.Response(self.status, json=self.payments)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    dsn = f"sqlite:///{(tmp_path / 'n1.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", dsn)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    settings = Settings(
        _env_file=None,
        database_url=dsn,
        paypal_client_id="client-id",
        paypal_client_secret="client-secret",
        system3_key="console-key",
        system2_key="engine-key",
    )
    paypal = PayPalStub()
    system2 = System2Stub()
    return settings, paypal, system2


def app_for(environment):
    settings, paypal, system2 = environment
    return create_app(
        settings,
        paypal_transport=httpx.MockTransport(paypal),
        system2_transport=httpx.MockTransport(system2),
    )


def test_oauth_pagination_details_expiration_and_rejected_token(environment):
    settings, stub, _ = environment
    clock = [0]
    with httpx.Client(base_url=BASE, transport=httpx.MockTransport(stub)) as http:
        paypal = PayPalClient(settings, http, clock=lambda: clock[0])
        assert [item["dispute_id"] for item in paypal.list_disputes()] == ["PP-D-ONE", "PP-D-TWO"]
        assert paypal.get_dispute("PP-D-TWO")["status"] == "OPEN"
        assert stub.tokens == 1
        clock[0] = 91
        paypal.probe()
        assert stub.tokens == 2
        stub.expired = True
        paypal.probe()
        assert stub.tokens == 3


@pytest.mark.parametrize(
    "link",
    [
        "https://other.example/v1/customer/disputes?page=2",
        f"{BASE}/v1/customer/disputes?page_size=50",
    ],
)
def test_rejects_foreign_and_cyclic_pagination(environment, link):
    settings, stub, _ = environment
    stub.next_link = link
    with (
        httpx.Client(base_url=BASE, transport=httpx.MockTransport(stub)) as http,
        pytest.raises(IntegrationError, match="pagination"),
    ):
        list(PayPalClient(settings, http).list_disputes())
    assert all(call.url.host == "api-m.sandbox.paypal.com" for call in stub.calls)


def test_normalization_preserves_null_and_integer_money():
    basic = {"dispute_id": "PP-D-ID", "status": "OPEN"}
    assert PayPalDispute.model_validate(basic).fields()["amount_minor"] is None
    model = PayPalDispute.model_validate(
        {
            **basic,
            "dispute_amount": {"value": "1.00", "currency_code": "JPY"},
            "buyer_response_due_date": "2026-11-11T15:00:00+02:00",
        }
    )
    assert model.fields()["amount_minor"] == 1
    assert model.fields()["deadline"].isoformat() == "2026-11-11T13:00:00+00:00"
    assert model.fields()["buyer"] is None
    with pytest.raises(ValueError, match="currency precision"):
        PayPalDispute.model_validate(
            {
                **basic,
                "dispute_amount": {"value": "1.50", "currency_code": "JPY"},
            }
        ).fields()


def test_payment_matching_is_exact_and_ambiguous_links_are_not_saved():
    payment = PaymentRecord.model_validate(PAYMENT)
    other = payment.model_copy(update={"id": "pay_other", "paypal_capture_id": "OTHER"})
    assert payment_reference([Transaction(custom=payment.custom_id)], [payment]) == (
        payment.id,
        "LINKED",
    )
    assert payment_reference([Transaction(seller_transaction_id="prefix")], [payment]) == (
        None,
        "UNMATCHED",
    )
    assert payment_reference(
        [
            Transaction(
                seller_transaction_id=payment.paypal_capture_id,
                buyer_transaction_id="OTHER",
            )
        ],
        [payment, other],
    ) == (None, "AMBIGUOUS")
    assert payment_reference(
        [
            Transaction(buyer_transaction_id=payment.paypal_order_id),
            Transaction(seller_transaction_id="missing"),
        ],
        [payment],
    ) == (None, "UNMATCHED")


def test_sync_list_detail_status_history_retry_and_idempotency(environment):
    settings, paypal, system2 = environment
    with TestClient(app_for(environment)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/disputes").status_code == 401
        assert client.get("/api/disputes?page=0", headers=HEADERS).status_code == 422
        first = client.post("/api/disputes/sync", headers=HEADERS)
        assert first.status_code == 200, first.text
        assert (first.json()["retrieved"], first.json()["created"], first.json()["unmatched"]) == (
            2,
            2,
            1,
        )
        listed = client.get(
            "/api/disputes?search=pp-d&sort=paypal_dispute_id&order=asc&page_size=1",
            headers=HEADERS,
        ).json()
        assert listed["total"] == 2
        [case] = listed["items"]
        assert case["payment_id"] == "pay_01"
        assert case["source"] == "PAYPAL_API"
        assert case["amount_minor"] == 6500
        detail_url = f"/api/disputes/{case['id']}"
        detail = client.get(detail_url, headers=HEADERS).json()
        assert detail["buyer"]["name"] == "Sandbox Buyer"
        assert detail["merchant"]["merchant_id"] == "merchant-one"
        assert detail["created_at"] == "2026-11-09T12:00:00Z"
        assert "raw_response" not in detail
        assert detail["status_history"][0]["previous_status"] is None

        repeated = client.post("/api/disputes/sync", headers=HEADERS).json()
        assert (repeated["created"], repeated["updated"]) == (0, 0)
        assert len(client.get(detail_url, headers=HEADERS).json()["status_history"]) == 1

        paypal.status, paypal.update_time = "RESOLVED", "2026-11-10T14:00:00Z"
        system2.payments.append({**PAYMENT, "id": "pay_02", "paypal_capture_id": "NONE"})
        changed = client.post("/api/disputes/sync", headers=HEADERS).json()
        assert (changed["updated"], changed["unmatched"]) == (2, 0)
        detail = client.get(detail_url, headers=HEADERS).json()
        assert detail["status"] == "RESOLVED"
        assert detail["status_history"][0]["previous_status"] == "OPEN"

        paypal.status, paypal.update_time = "OPEN", "2026-11-09T14:00:00Z"
        stale = client.post("/api/disputes/sync", headers=HEADERS).json()
        assert stale["updated"] == 0
        history = client.get("/api/disputes/sync/history", headers=HEADERS).json()
        assert history["total"] == 4
        assert system2.calls == 4
        status = client.get("/api/paypal/status", headers=HEADERS).json()
        assert status["total_disputes"] == 2 and status["connection"] == "CONNECTED"
        assert "token" not in status and "client_secret" not in status
    with Session(settings.database_engine()) as db:
        row = db.get(Dispute, case["id"])
        assert row.raw_response["messages"][0]["content"] == "Stored in raw payload only."


@pytest.mark.parametrize(("failure", "status"), [("timeout", 504), ("forbidden", 502)])
def test_upstream_failure_is_logged_and_rolls_back_partial_data(environment, failure, status):
    _, paypal, _ = environment
    paypal.failure = failure
    with TestClient(app_for(environment)) as client:
        response = client.post("/api/disputes/sync", headers=HEADERS)
        assert response.status_code == status
        assert "private" not in response.text
        assert response.json()["error"]["details"]["result"] == "FAILED"
        assert client.get("/api/disputes", headers=HEADERS).json()["total"] == 0
        [run] = client.get("/api/disputes/sync/history", headers=HEADERS).json()["items"]
        assert (run["result"], run["retrieved"], run["created"]) == ("FAILED", 1, 0)


def test_system2_outage_is_partial_and_retries_links(environment):
    _, _, system2 = environment
    system2.status = 501
    with TestClient(app_for(environment)) as client:
        run = client.post("/api/disputes/sync", headers=HEADERS).json()
        assert run["result"] == "PARTIAL" and run["unmatched"] == 2
        assert (
            client.get("/api/disputes?link_status=LOOKUP_FAILED", headers=HEADERS).json()["total"]
            == 2
        )
        system2.status = 200
        run = client.post("/api/disputes/sync", headers=HEADERS).json()
        assert run["result"] == "SUCCESS" and run["unmatched"] == 1
        system2.status = 501
        client.post("/api/disputes/sync", headers=HEADERS)
        assert client.get("/api/disputes?link_status=LINKED", headers=HEADERS).json()["total"] == 1


def test_missing_credentials_status_and_sync_error(environment):
    settings, paypal, system2 = environment
    settings.paypal_client_secret = type(settings.paypal_client_secret)("")
    with TestClient(app_for((settings, paypal, system2))) as client:
        health = client.get("/api/paypal/status", headers=HEADERS).json()
        assert (
            health["connection"] == "NOT_CONFIGURED" and health["credentials_configured"] is False
        )
        assert client.post("/api/disputes/sync", headers=HEADERS).status_code == 503
        assert paypal.calls == []


def test_fixture_sources_remain_separate_and_seeding_is_idempotent(environment):
    settings, _, stub = environment
    engine = settings.database_engine()
    with (
        Session(engine) as db,
        httpx.Client(
            base_url=str(settings.system2_base_url), transport=httpx.MockTransport(stub)
        ) as http,
    ):
        system2 = System2Client(settings, http)
        seed_fixtures(db, system2)
        seed_fixtures(db, system2)
        assert len(db.scalars(select(DisputeStatusHistory)).all()) == 2
    with TestClient(app_for(environment)) as client:
        rows = client.get("/api/disputes?source=SIMULATED_FIXTURE", headers=HEADERS).json()["items"]
        assert len(rows) == 2 and {row["source"] for row in rows} == {"SIMULATED_FIXTURE"}
        assert {row["payment_id"] for row in rows} == {None, "pay_01"}
        assert client.get("/api/paypal/status", headers=HEADERS).json()["total_disputes"] == 0
    engine.dispose()


def test_concurrent_sync_is_rejected(environment):
    settings, _, _ = environment
    engine = settings.database_engine()
    with Session(engine) as first, Session(engine) as second:
        acquire_sync_lock(first)
        second.execute(text("PRAGMA busy_timeout=0"))
        with pytest.raises(IntegrationError, match="already running"):
            acquire_sync_lock(second)
    engine.dispose()
