"""PayPal REST adapter (sandbox by default). Amounts in, integer minor units only."""

from __future__ import annotations

import base64
import threading
import time
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx

BASE_URLS = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}
TOKEN_REFRESH_MARGIN_S = 300  # refresh 5 minutes early
ZERO_DECIMAL = {"JPY", "HUF", "TWD"}


class PayPalError(Exception):
    def __init__(self, status: int, body: Any, debug_id: str | None = None, message: str = ""):
        self.status = status
        self.body = body
        self.debug_id = debug_id
        text = message or (body.get("message") if isinstance(body, dict) else None) or "error"
        self.message = text
        super().__init__(f"PayPal {status}: {text} (debug_id={debug_id})")


def minor_to_decimal(amount_minor: int, currency: str) -> str:
    """6500, 'USD' -> '65.00'. Zero-decimal currencies (JPY, HUF, TWD) have no fraction."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise TypeError("amount_minor must be an int")
    if amount_minor < 0:
        raise ValueError("amount_minor must be >= 0")
    if currency.upper() in ZERO_DECIMAL:
        return str(amount_minor)
    return str((Decimal(amount_minor) / 100).quantize(Decimal("0.01")))


def mask(value: str) -> str:
    return "***" if len(value) < 8 else f"{value[:3]}...{value[-2:]}"


class PayPalClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        env: str = "sandbox",
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = 30.0,
    ):
        if not client_id or not client_secret:
            raise ValueError("PayPal client id/secret are empty")
        self._id, self._secret = client_id, client_secret
        self._base = BASE_URLS[(env or "sandbox").lower()]
        self._clock = clock
        self._http = httpx.Client(base_url=self._base, transport=transport, timeout=timeout)
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._last = threading.local()  # per thread: PayPal-Debug-Id of the latest API response

    @property
    def last_debug_id(self) -> str | None:
        """PayPal's debug id for this thread's most recent API call (for support and the ledger)."""
        return getattr(self._last, "debug_id", None)

    # ---- auth ----
    def get_token(self) -> str:
        if self._token and self._clock() < self._token_expires_at - TOKEN_REFRESH_MARGIN_S:
            return self._token
        basic = base64.b64encode(f"{self._id}:{self._secret}".encode()).decode()
        r = self._http.post(
            "/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {basic}"},
        )
        body = self._json(r)
        if r.status_code != 200:
            raise self._error(r, body)
        self._token = body["access_token"]
        self._token_expires_at = self._clock() + int(body.get("expires_in", 0))
        return self._token

    # ---- request helpers ----
    @staticmethod
    def _json(r: httpx.Response) -> Any:
        try:
            return r.json() if r.content else {}
        except ValueError:
            return {"raw": r.text[:500]}

    @staticmethod
    def _error(r: httpx.Response, body: Any) -> PayPalError:
        debug_id = r.headers.get("paypal-debug-id") or (
            body.get("debug_id") if isinstance(body, dict) else None
        )
        return PayPalError(r.status_code, body, debug_id)

    def _request(
        self, method: str, path: str, *, json: Any = None, idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        h = {"Authorization": f"Bearer {self.get_token()}", **(headers or {})}
        if method == "POST":
            h["PayPal-Request-Id"] = idempotency_key or str(uuid.uuid4())
        r = self._http.request(method, path, json=json, headers=h)
        self._last.debug_id = r.headers.get("paypal-debug-id")
        body = self._json(r)
        if r.status_code >= 400:
            raise self._error(r, body)
        return body

    # ---- orders ----
    def create_order(
        self,
        amount_minor: int,
        currency: str,
        custom_id: str,
        description: str,
        idempotency_key: str | None = None,
        payment_source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "amount": {
                        "currency_code": currency.upper(),
                        "value": minor_to_decimal(amount_minor, currency),
                    },
                    "custom_id": custom_id,
                    "description": description,
                }
            ],
        }
        if payment_source:
            body["payment_source"] = payment_source
        return self._request(
            "POST", "/v2/checkout/orders", json=body, idempotency_key=idempotency_key,
            headers={"Prefer": "return=representation"},
        )

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v2/checkout/orders/{order_id}")

    def capture_order(self, order_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
        return self._request(
            "POST", f"/v2/checkout/orders/{order_id}/capture", json={},
            idempotency_key=idempotency_key, headers={"Prefer": "return=representation"},
        )

    def refund_capture(
        self,
        capture_id: str,
        amount_minor: int,
        currency: str,
        note: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "amount": {
                "currency_code": currency.upper(),
                "value": minor_to_decimal(amount_minor, currency),
            },
            "note_to_payer": note[:255],
        }
        return self._request(
            "POST", f"/v2/payments/captures/{capture_id}/refund", json=body,
            idempotency_key=idempotency_key,
        )


def capture_id_of(order: dict[str, Any]) -> str | None:
    """Pull the first capture id out of an order/capture response."""
    for pu in order.get("purchase_units", []):
        caps = pu.get("payments", {}).get("captures", [])
        if caps:
            return caps[0]["id"]
    return None


def approve_link(order: dict[str, Any]) -> str | None:
    for link in order.get("links", []):
        if link.get("rel") in ("payer-action", "approve"):
            return link["href"]
    return None


def pay_order(
    client: PayPalClient,
    amount_minor: int,
    currency: str,
    custom_id: str,
    description: str,
    idempotency_key: str,
    *,
    card: dict[str, Any] | None = None,
    buyer_email: str = "",
    buyer_password: str = "",
    approver: Callable[[str, str, str], None] | None = None,
    on_order: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Create and pay an order with no human click. Returns the captured order.

    Route 1: card payment_source, confirmed in the create call (needs card processing enabled).
    Route 2: PayPal approval driven by a headless browser logged in as the sandbox buyer.
    Callers pass the same inputs either way and get the same shape back. `on_order` is called
    with the order as soon as it exists (before capture) so the caller can record its id.
    """
    if card:
        try:
            order = client.create_order(
                amount_minor, currency, custom_id, description, idempotency_key, {"card": card}
            )
            if on_order:
                on_order(order)
            if order.get("status") == "PAYER_ACTION_REQUIRED":
                # The card needs a 3D Secure challenge. It is not completed headlessly here (see
                # docs/CONTRACT.md); fail with an explicit reason rather than a blind capture.
                raise PayPalError(
                    0, {}, client.last_debug_id,
                    "card payment needs a 3D Secure payer-action step that is not completed "
                    "headlessly",
                )
            if order.get("status") != "COMPLETED":
                order = client.capture_order(order["id"], f"{idempotency_key}-cap")
            return order
        except PayPalError as e:
            issues = {d.get("issue") for d in (e.body or {}).get("details", [])}
            if "PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING" not in issues:
                raise
    if not (buyer_email and buyer_password):
        raise PayPalError(0, {}, None, "card route unavailable and no buyer credentials given")
    if approver is None:
        from .paypal_browser import approve_in_browser as approver
    from .paypal_browser import RETURN_URL

    source = {
        "paypal": {
            "experience_context": {
                "return_url": RETURN_URL,
                "cancel_url": RETURN_URL + "?cancel=1",
                "user_action": "PAY_NOW",
                "shipping_preference": "NO_SHIPPING",
            }
        }
    }
    order = client.create_order(
        amount_minor, currency, custom_id, description, idempotency_key, source
    )
    link = approve_link(order)
    if not link:
        raise PayPalError(0, order, None, "order has no approval link")
    if on_order:
        on_order(order)
    try:
        approver(link, buyer_email, buyer_password)
    except PayPalError:
        raise
    except Exception as e:
        first = (str(e).strip().splitlines() or [""])[0][:160]
        raise PayPalError(
            0, {}, client.last_debug_id,
            f"headless buyer approval failed ({type(e).__name__}: {first})",
        ) from e
    return client.capture_order(order["id"], f"{idempotency_key}-cap")
