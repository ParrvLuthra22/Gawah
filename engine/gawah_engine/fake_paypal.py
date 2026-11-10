"""SIMULATED PayPal: an in-memory stand-in served through httpx.MockTransport (no network).

Never used in production: it is only reachable from tests and from an engine started with
GAWAH_FAKE_PAYPAL=1 AND GAWAH_ENV=test (anything else is refused, see services.py).

Behaves like PayPal where it matters here: POSTs with a PayPal-Request-Id they have already seen
return the original response (idempotency), a card create confirms and captures in one call, and
failures can be injected per endpoint (HTTP status, timeout, timeout-after-the-order-exists, or a
hard crash of the whole process, modelled as a BaseException).
"""

import itertools
import re
import threading

import httpx


class Crash(BaseException):
    """Simulates the engine process dying at that exact point."""


class FakePayPal:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.orders: dict[str, dict] = {}
        self.refunds: dict[str, dict] = {}
        self._seen: dict[str, httpx.Response] = {}
        self._ids = itertools.count(1)
        self.create_status = "COMPLETED"  # "CREATED" makes the engine call capture separately
        self.failures: dict[str, list] = {}  # endpoint -> queue of failures to inject
        self.approved: set[str] = set()  # order ids the (simulated) buyer has approved
        self.hooks: list = []  # called (outside the fake's lock) at the start of every API call
        self.transport = httpx.MockTransport(self)

    # ---- helpers for tests ----------------------------------------------------------------
    def fail(self, endpoint: str, *failures) -> None:
        """endpoint: create | capture | get | refund. Each failure is consumed by one request:
        an int (HTTP status), 'timeout', 'timeout_after', or 'crash'."""
        self.failures.setdefault(endpoint, []).extend(failures)

    def calls(self, endpoint: str | None = None) -> list[dict]:
        return [r for r in self.requests if endpoint is None or r["endpoint"] == endpoint]

    # ---- the transport --------------------------------------------------------------------
    def __call__(self, request: httpx.Request) -> httpx.Response:
        response = self._dispatch(request)
        if response.status_code < 400:  # like PayPal: every answer carries a debug id header
            response.headers.setdefault("paypal-debug-id", "dbg-fake-ok")
        return response

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 32400})
        endpoint = self._endpoint(method, path)
        for hook in self.hooks:
            hook(endpoint)
        rid = request.headers.get("paypal-request-id")
        body = request.content.decode() if request.content else ""
        with self.lock:
            self.requests.append({"endpoint": endpoint, "method": method, "path": path,
                                  "request_id": rid, "body": body})
            queue = self.failures.get(endpoint, [])
            failure = queue.pop(0) if queue else None
            if failure == "crash":
                raise Crash(endpoint)
            if failure == "timeout":
                raise httpx.ReadTimeout("injected timeout", request=request)
            if isinstance(failure, int):
                return httpx.Response(
                    failure,
                    json={"name": "INJECTED", "message": "injected", "debug_id": "dbg-injected",
                          "details": [{"issue": "INSTRUMENT_DECLINED"}] if failure == 422 else []},
                )
            if method == "POST" and rid and rid in self._seen:
                return self._seen[rid]
            response = self._handle(request, endpoint, path, body)
            if method == "POST" and rid and response.status_code < 400:
                self._seen[rid] = response
            if failure == "timeout_after":  # the work happened, the answer never arrived
                raise httpx.ReadTimeout("injected timeout after create", request=request)
            return response

    @staticmethod
    def _endpoint(method: str, path: str) -> str:
        if path == "/v2/checkout/orders" and method == "POST":
            return "create"
        if path.endswith("/capture"):
            return "capture"
        if "/refund" in path:
            return "refund"
        return "get"

    def _handle(self, request, endpoint, path, body) -> httpx.Response:
        import json

        if endpoint == "create":
            data = json.loads(body)
            pu = data["purchase_units"][0]
            oid = f"ORDER{next(self._ids):04d}"
            order = {
                "id": oid,
                "status": self.create_status,
                "purchase_units": [{"custom_id": pu.get("custom_id"), "amount": pu["amount"],
                                    "payments": {}}],
            }
            if self.create_status == "PAYER_ACTION_REQUIRED":
                order["links"] = [{"rel": "payer-action",
                                   "href": f"https://fake.sandbox.paypal.test/checkoutnow?token={oid}"}]
            if self.create_status == "COMPLETED":
                self._capture(order)
            self.orders[oid] = order
            return httpx.Response(201, json=order)
        if endpoint == "capture":
            oid = re.search(r"/orders/([^/]+)/capture", path).group(1)
            order = self.orders[oid]
            if order["status"] == "PAYER_ACTION_REQUIRED" and oid not in self.approved:
                return httpx.Response(422, json={
                    "name": "UNPROCESSABLE_ENTITY", "message": "not approved",
                    "debug_id": "dbg-not-approved", "details": [{"issue": "ORDER_NOT_APPROVED"}]})
            if order["status"] != "COMPLETED":
                self._capture(order)
            return httpx.Response(201, json=order)
        if endpoint == "refund":
            cid = re.search(r"/captures/([^/]+)/refund", path).group(1)
            ref = {"id": f"REFUND{next(self._ids):04d}", "status": "COMPLETED", "capture": cid}
            self.refunds[ref["id"]] = ref
            return httpx.Response(201, json=ref)
        oid = path.rsplit("/", 1)[1]
        if oid not in self.orders:
            return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND", "message": "nope"})
        return httpx.Response(200, json=self.orders[oid])

    def _capture(self, order: dict) -> None:
        pu = order["purchase_units"][0]
        pu["payments"] = {"captures": [{"id": f"CAP{next(self._ids):04d}", "status": "COMPLETED",
                                        "custom_id": pu["custom_id"]}]}
        order["status"] = "COMPLETED"
