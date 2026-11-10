import time
from collections.abc import Iterator
from threading import Lock

import httpx
from gawah_contract import PaymentRecord, RefundRequest
from pydantic import BaseModel, Field, SecretStr, TypeAdapter, ValidationError

from .config import Settings

DISPUTES_PATH = "/v1/customer/disputes"


class IntegrationError(Exception):
    def __init__(self, code: str, message: str, status: int = 502, *, upstream_status=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.upstream_status = upstream_status


def request_upstream(client: httpx.Client, service: str, method: str, path: str, **kwargs):
    try:
        response = client.request(method, path, **kwargs)
        response.raise_for_status()
        return response
    except httpx.TimeoutException:
        raise IntegrationError("UPSTREAM_TIMEOUT", f"{service} timed out.", 504) from None
    except httpx.RequestError:
        raise IntegrationError("UPSTREAM_UNAVAILABLE", f"{service} could not be reached.") from None
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        code = "UPSTREAM_AUTH_FAILED" if status in (401, 403) else "UPSTREAM_HTTP_ERROR"
        raise IntegrationError(
            code, f"{service} returned HTTP {status}.", upstream_status=status
        ) from None


def request_json(client: httpx.Client, service: str, method: str, path: str, **kwargs):
    response = request_upstream(client, service, method, path, **kwargs)
    try:
        return response.json()
    except ValueError:
        raise IntegrationError("INVALID_RESPONSE", f"{service} returned invalid JSON.") from None


class TokenResponse(BaseModel):
    access_token: SecretStr
    expires_in: int = Field(gt=0)


class PageLink(BaseModel):
    rel: str
    href: str


class DisputePage(BaseModel):
    items: list[dict] = Field(default_factory=list)
    links: list[PageLink] = Field(default_factory=list)


class PayPalClient:
    def __init__(self, settings: Settings, http: httpx.Client, clock=time.monotonic):
        self.settings = settings
        self.http = http
        self.clock = clock
        self.token: str | None = None
        self.refresh_at = 0.0
        self.lock = Lock()

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.paypal_client_id.get_secret_value()
            and self.settings.paypal_client_secret.get_secret_value()
        )

    def _access_token(self, rejected: str | None = None) -> str:
        if not self.configured:
            raise IntegrationError(
                "PAYPAL_NOT_CONFIGURED", "PayPal Sandbox credentials are missing.", 503
            )
        with self.lock:
            if self.token and self.token != rejected and self.clock() < self.refresh_at:
                return self.token
            started = self.clock()
            payload = request_json(
                self.http,
                "PayPal authentication",
                "POST",
                "/v1/oauth2/token",
                auth=(
                    self.settings.paypal_client_id.get_secret_value(),
                    self.settings.paypal_client_secret.get_secret_value(),
                ),
                data={"grant_type": "client_credentials"},
            )
            try:
                token = TokenResponse.model_validate(payload)
                self.token = token.access_token.get_secret_value()
                if not self.token:
                    raise ValueError
            except ValueError:
                raise IntegrationError(
                    "INVALID_RESPONSE", "PayPal returned an invalid access token."
                ) from None
            self.refresh_at = started + token.expires_in - min(60, token.expires_in / 10)
            return self.token

    def _get(self, path: str, params=None):
        token = self._access_token()
        try:
            return request_json(
                self.http,
                "PayPal disputes",
                "GET",
                path,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        except IntegrationError as exc:
            if exc.upstream_status != 401:
                raise
        return request_json(
            self.http,
            "PayPal disputes",
            "GET",
            path,
            params=params,
            headers={"Authorization": f"Bearer {self._access_token(rejected=token)}"},
        )

    def _page(self, params) -> DisputePage:
        try:
            return DisputePage.model_validate(self._get(DISPUTES_PATH, params))
        except ValidationError:
            raise IntegrationError(
                "INVALID_RESPONSE", "PayPal returned an invalid disputes page."
            ) from None

    def probe(self) -> None:
        self._page({"page_size": 1})

    def list_disputes(self) -> Iterator[dict]:
        params = httpx.QueryParams({"page_size": 50})
        visited = set()
        while str(params) not in visited:
            visited.add(str(params))
            page = self._page(params)
            yield from page.items
            next_link = next((link.href for link in page.links if link.rel == "next"), None)
            if next_link is None:
                return
            try:
                target = httpx.URL(next_link)
            except httpx.InvalidURL:
                raise IntegrationError(
                    "INVALID_RESPONSE", "Invalid PayPal pagination link."
                ) from None
            if (
                target.path != DISPUTES_PATH
                or target.fragment
                or target.userinfo
                or (
                    target.host
                    and (
                        target.scheme != "https"
                        or target.port not in (None, 443)
                        or target.host not in {"api-m.sandbox.paypal.com", "api.sandbox.paypal.com"}
                    )
                )
                or not target.params
            ):
                raise IntegrationError("INVALID_RESPONSE", "Invalid PayPal pagination link.")
            # Follow the pagination parameters while keeping requests on the configured origin.
            params = target.params
        raise IntegrationError("INVALID_RESPONSE", "PayPal pagination repeated a page.")

    def get_dispute(self, dispute_id: str) -> dict:
        payload = self._get(f"{DISPUTES_PATH}/{dispute_id}")
        if not isinstance(payload, dict) or payload.get("dispute_id") != dispute_id:
            raise IntegrationError("INVALID_RESPONSE", "PayPal returned a mismatched dispute ID.")
        return payload

    def submit_action(self, path: str, payload: dict) -> httpx.Response:
        return request_upstream(
            self.http,
            "PayPal Sandbox resolution",
            "POST",
            path,
            json=payload,
            headers={"Authorization": f"Bearer {self._access_token()}"},
        )


PAYMENTS = TypeAdapter(list[PaymentRecord])


class System2Client:
    def __init__(self, settings: Settings, http: httpx.Client):
        self.http = http
        self.key = (
            settings.system2_key.get_secret_value() or settings.system3_key.get_secret_value()
        )

    def headers(self) -> dict[str, str]:
        if not self.key:
            raise IntegrationError("SYSTEM2_NOT_CONFIGURED", "The System 2 service key is missing.")
        return {"X-Gawah-Key": self.key}

    def get_json(self, path: str, *, service: str = "System 2 evidence", params=None):
        return request_json(
            self.http,
            service,
            "GET",
            path,
            headers=self.headers(),
            params=params,
        )

    def refund_payment(self, payment_id: str, body: RefundRequest) -> dict:
        return request_json(
            self.http,
            "System 2 refund",
            "POST",
            f"/v1/payments/{payment_id}/refund",
            headers=self.headers(),
            json=body.model_dump(exclude_none=True),
        )

    def list_payments(self) -> list[PaymentRecord]:
        payload = self.get_json("/v1/payments", service="System 2 payments")
        try:
            return PAYMENTS.validate_python(payload)
        except ValidationError:
            raise IntegrationError(
                "INVALID_RESPONSE", "System 2 returned an invalid payment list."
            ) from None
