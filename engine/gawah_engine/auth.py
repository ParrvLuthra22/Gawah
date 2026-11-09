import hmac

from fastapi import Header

from .config import settings
from .errors import GawahError


def require_service_key(x_gawah_key: str | None = Header(default=None)) -> str:
    """Return the calling service name ('system1' or 'system3'), or raise 401."""
    if x_gawah_key:
        for name, key in (("system1", settings.system1_key), ("system3", settings.system3_key)):
            if key and hmac.compare_digest(x_gawah_key, key):
                return name
    raise GawahError(401, "UNAUTHORIZED", "Missing or invalid X-Gawah-Key header")


def require_mock() -> None:
    if not settings.gawah_mock:
        raise GawahError(501, "NOT_IMPLEMENTED", "Real logic is not implemented yet; set GAWAH_MOCK=1")
