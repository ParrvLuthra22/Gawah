from fastapi import APIRouter
from gawah_contract import MerchantKey

from ..config import settings
from ..errors import GawahError
from ..services import get_services

router = APIRouter(prefix="/v1/merchants", tags=["merchants"])

# GAWAH_MOCK=1: a fixed demo merchant plus an in-memory dict. GAWAH_MOCK=0: the SQLite registry
# (ES256 public keys only, PEM or JWK in, normalised PEM out).

_MOCK_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MCowBQYDK2VwAyEAMOCKMOCKMOCKMOCKMOCKMOCKMOCKMOCKMOCKMOCKMOCK=\n"
    "-----END PUBLIC KEY-----\n"
)
_REGISTERED: dict[str, MerchantKey] = {
    "demo-shoe-store": MerchantKey(
        merchant_id="demo-shoe-store",
        key_id="demo-shoe-store-key-1",
        public_key_pem=_MOCK_PEM,
    )
}


@router.post("", response_model=MerchantKey, status_code=201)
def register_merchant(body: MerchantKey) -> MerchantKey:
    if not settings.gawah_mock:
        return get_services().merchants.register(body)
    _REGISTERED[body.merchant_id] = body
    return body


@router.get("/{merchant_id}/public-key", response_model=MerchantKey)
def get_public_key(merchant_id: str) -> MerchantKey:
    if not settings.gawah_mock:
        found = get_services().merchants.get(merchant_id)
        if found is None:
            raise GawahError(404, "MERCHANT_NOT_FOUND", f"No merchant {merchant_id}")
        return found
    if merchant_id not in _REGISTERED:
        raise GawahError(404, "MERCHANT_NOT_FOUND", f"No merchant {merchant_id}")
    return _REGISTERED[merchant_id]
