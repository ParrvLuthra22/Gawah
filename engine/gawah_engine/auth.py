"""Authentication, per-service authorization scopes and a per-key rate limit.

A valid key is no longer enough: each service key may call only the routes listed in SCOPES.
The headline property: the shopper (System 1) cannot refund or pay by any route except through
the Guardian (`POST /v1/proposals`), and nothing but System 1 can create proposals.

Order of checks: 401 (unknown key) -> 429 (rate limit) -> 403 (route outside the key's scope).
Scopes match the route *template* (`/v1/mandates/{mandate_id}`), so path parameters cannot be
used to dodge them, and a route that is in no scope list is closed to everyone (deny by default).
"""

import hmac
import threading
import time
from collections import defaultdict, deque

from fastapi import Request

from .config import settings
from .errors import GawahError

# "METHOD route-template". System 1 = shopper agent + demo store, System 3 = dashboard + counsel.
SCOPES: dict[str, frozenset[str]] = {
    "system1": frozenset(
        {
            "POST /v1/proposals",
            "GET /v1/mandates",
            "GET /v1/mandates/{mandate_id}",
            "POST /v1/merchants",
            "GET /v1/merchants/{merchant_id}/public-key",
        }
    ),
    "system3": frozenset(
        {
            "GET /v1/ledger",
            "GET /v1/ledger/verify",
            "GET /v1/ledger/head",
            "GET /v1/ledger/stream",
            "GET /v1/payments",
            "GET /v1/payments/{payment_id}",
            "GET /v1/evidence/{payment_id}",
            "GET /v1/mandates",
            "GET /v1/mandates/{mandate_id}",
            "GET /v1/mandates/{mandate_id}/budget",
            "POST /v1/mandates",
            "POST /v1/mandates/{mandate_id}/revoke",
            "POST /v1/payments/{payment_id}/refund",
            "POST /v1/guardian/check",  # dry run: no payment, no ledger write
        }
    ),
}


class RateLimiter:
    """In-memory sliding window, one bucket per key. Per process (documented limit)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def check(self, bucket: str, limit: int, window_s: float = 60.0) -> float | None:
        """Record a hit. Return None if allowed, else seconds until the next slot."""
        if limit <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            q = self._hits[bucket]
            while q and now - q[0] >= window_s:
                q.popleft()
            if len(q) >= limit:
                return max(1.0, window_s - (now - q[0]))
            q.append(now)
            return None


limiter = RateLimiter()


def authenticate(key: str | None) -> str:
    """Return the calling service name ('system1' or 'system3'), or raise 401."""
    if key:
        for name, expected in (("system1", settings.system1_key), ("system3", settings.system3_key)):
            if expected and hmac.compare_digest(key.encode(), expected.encode()):
                return name
    raise GawahError(401, "UNAUTHORIZED", "Missing or invalid X-Gawah-Key header")


def route_key(request: Request) -> str:
    route = request.scope.get("route")
    template = getattr(route, "path_format", None) or request.url.path
    return f"{request.method} {template}"


def require_service_key(request: Request) -> str:
    """Dependency for every /v1 route: authenticate, rate-limit, then check the scope."""
    service = authenticate(request.headers.get("x-gawah-key"))
    retry = limiter.check(service, settings.rate_limit_per_minute)
    if retry is not None:
        raise GawahError(
            429, "RATE_LIMITED", "Too many requests for this key", {"retry_after_s": round(retry)}
        )
    needed = route_key(request)
    if needed not in SCOPES[service]:
        raise GawahError(403, "FORBIDDEN", f"This key is not allowed to call {needed}")
    request.state.service = service
    return service

