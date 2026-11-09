import json

import httpx
import pytest
from gawah_engine.paypal import PayPalClient, PayPalError, capture_id_of, minor_to_decimal


class Fake:
    """Records requests; token lifetime controllable via `expires_in`."""

    def __init__(self, expires_in=3600):
        self.requests: list[httpx.Request] = []
        self.expires_in = expires_in
        self.tokens = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/oauth2/token":
            self.tokens += 1
            return httpx.Response(
                200, json={"access_token": f"tok{self.tokens}", "expires_in": self.expires_in}
            )
        if request.url.path == "/v2/checkout/orders" and request.method == "POST":
            return httpx.Response(201, json={"id": "ORD1", "status": "CREATED"})
        if request.url.path.endswith("/boom"):
            return httpx.Response(
                422, json={"name": "UNPROCESSABLE", "message": "bad", "debug_id": "dbg123"}
            )
        return httpx.Response(200, json={"ok": True})


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def make(fake, clock=None):
    return PayPalClient("cid", "secret", transport=httpx.MockTransport(fake),
                        clock=clock or Clock())


@pytest.mark.parametrize(
    "minor,cur,out",
    [(6500, "USD", "65.00"), (5, "USD", "0.05"), (0, "USD", "0.00"), (1999, "usd", "19.99"),
     (500, "JPY", "500")],
)
def test_minor_to_decimal(minor, cur, out):
    assert minor_to_decimal(minor, cur) == out


@pytest.mark.parametrize("bad", [-1, 65.0, True, "6500"])
def test_minor_to_decimal_rejects_bad_input(bad):
    with pytest.raises((TypeError, ValueError)):
        minor_to_decimal(bad, "USD")


def test_token_cached_then_refreshed_5_min_early():
    fake, clock = Fake(expires_in=3600), Clock()
    c = make(fake, clock)
    assert c.get_token() == "tok1"
    clock.t += 3600 - 301  # just before the 5-minute margin
    assert c.get_token() == "tok1" and fake.tokens == 1
    clock.t += 2  # inside the margin
    assert c.get_token() == "tok2" and fake.tokens == 2


def test_token_request_uses_basic_auth_and_client_credentials():
    fake = Fake()
    make(fake).get_token()
    req = fake.requests[0]
    assert req.headers["authorization"].startswith("Basic ")
    assert b"grant_type=client_credentials" in req.content


def test_post_has_idempotency_header_and_bearer_and_minor_units():
    fake = Fake()
    c = make(fake)
    c.create_order(6500, "USD", "ref-1", "shoes", idempotency_key="idem-1")
    req = fake.requests[-1]
    assert req.headers["paypal-request-id"] == "idem-1"
    assert req.headers["authorization"] == "Bearer tok1"
    body = json.loads(req.content)
    pu = body["purchase_units"][0]
    assert pu["amount"] == {"currency_code": "USD", "value": "65.00"}
    assert pu["custom_id"] == "ref-1" and body["intent"] == "CAPTURE"


def test_default_idempotency_key_is_generated_and_unique():
    fake = Fake()
    c = make(fake)
    c.create_order(100, "USD", "a", "d")
    c.create_order(100, "USD", "a", "d")
    ids = [r.headers["paypal-request-id"] for r in fake.requests if r.method == "POST"
           and r.url.path != "/v1/oauth2/token"]
    assert len(ids) == 2 and ids[0] != ids[1]


def test_get_does_not_send_request_id():
    fake = Fake()
    make(fake).get_order("ORD1")
    assert "paypal-request-id" not in fake.requests[-1].headers


def test_error_maps_to_paypal_error_with_debug_id():
    fake = Fake()
    c = make(fake)
    with pytest.raises(PayPalError) as e:
        c._request("POST", "/v2/boom", json={})
    assert e.value.status == 422 and e.value.debug_id == "dbg123"
    assert e.value.body["name"] == "UNPROCESSABLE"


def test_token_failure_maps_to_error():
    def handler(request):
        return httpx.Response(401, json={"error": "invalid_client"},
                              headers={"paypal-debug-id": "d9"})

    c = PayPalClient("cid", "secret", transport=httpx.MockTransport(handler))
    with pytest.raises(PayPalError) as e:
        c.get_token()
    assert e.value.status == 401 and e.value.debug_id == "d9"


def test_refund_body():
    fake = Fake()
    make(fake).refund_capture("CAP1", 1000, "USD", "conceded", idempotency_key="r1")
    req = fake.requests[-1]
    assert req.url.path == "/v2/payments/captures/CAP1/refund"
    assert json.loads(req.content)["amount"]["value"] == "10.00"


def test_empty_credentials_rejected():
    with pytest.raises(ValueError):
        PayPalClient("", "")


def test_capture_id_of():
    order = {"purchase_units": [{"payments": {"captures": [{"id": "CAP9"}]}}]}
    assert capture_id_of(order) == "CAP9" and capture_id_of({}) is None


def _routing_transport(card_blocked: bool):
    seen = []

    def handler(request):
        path = request.url.path
        seen.append((request.method, path))
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if path == "/v2/checkout/orders":
            body = json.loads(request.content)
            if "card" in body.get("payment_source", {}) and card_blocked:
                return httpx.Response(422, json={"details": [
                    {"issue": "PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING"}]})
            if "card" in body.get("payment_source", {}):
                return httpx.Response(201, json={"id": "O1", "status": "COMPLETED"})
            return httpx.Response(201, json={"id": "O2", "status": "PAYER_ACTION_REQUIRED",
                                             "links": [{"rel": "payer-action", "href": "http://a"}]})
        if path.endswith("/capture"):
            return httpx.Response(201, json={"id": "O2", "status": "COMPLETED"})
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler), seen


def test_pay_order_card_route():
    from gawah_engine.paypal import pay_order

    t, seen = _routing_transport(card_blocked=False)
    c = PayPalClient("a", "b", transport=t)
    order = pay_order(c, 1000, "USD", "ref", "d", "k", card={"number": "x"})
    assert order["status"] == "COMPLETED" and ("POST", "/v2/checkout/orders/O1/capture") not in seen


def test_pay_order_falls_back_to_browser_when_card_not_enabled():
    from gawah_engine.paypal import pay_order

    t, seen = _routing_transport(card_blocked=True)
    approved = []
    c = PayPalClient("a", "b", transport=t)
    order = pay_order(c, 1000, "USD", "ref", "d", "k", card={"number": "x"},
                      buyer_email="e", buyer_password="p",
                      approver=lambda url, e, p: approved.append(url))
    assert approved == ["http://a"] and order["id"] == "O2"
    assert ("POST", "/v2/checkout/orders/O2/capture") in seen


def test_pay_order_without_buyer_creds_and_card_blocked_raises():
    from gawah_engine.paypal import pay_order

    t, _ = _routing_transport(card_blocked=True)
    with pytest.raises(PayPalError):
        pay_order(PayPalClient("a", "b", transport=t), 1000, "USD", "r", "d", "k",
                  card={"number": "x"})
