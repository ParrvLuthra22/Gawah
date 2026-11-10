import hmac
from typing import Any

from fastapi import APIRouter, Header

from ..auth import limiter
from ..bootstrap import reset_demo
from ..config import settings
from ..errors import GawahError

router = APIRouter(prefix="/v1/admin", tags=["admin"])


@router.post("/reset-demo")
def reset_demo_route(x_admin_key: str | None = Header(default=None)) -> dict[str, Any]:
    """Re-record helper for the hosted demo: wipe data and keys, new ledger genesis.

    Only when GAWAH_ENV=demo (403 otherwise), and only with the X-Admin-Key header matching
    ADMIN_KEY (401). Failed attempts are rate limited. This route is NOT in any service scope.
    """
    if settings.gawah_env != "demo":
        raise GawahError(403, "ADMIN_DISABLED", "reset-demo is only available when GAWAH_ENV=demo")
    wait = limiter.check("admin-reset", 10)
    if wait is not None:
        raise GawahError(429, "RATE_LIMITED", "Too many reset attempts",
                         details={"retry_after_s": int(wait) + 1})
    expected = settings.admin_key
    if not expected or not x_admin_key or not hmac.compare_digest(
        x_admin_key.encode(), expected.encode()
    ):
        raise GawahError(401, "UNAUTHORIZED", "Missing or invalid X-Admin-Key header")
    return reset_demo()
