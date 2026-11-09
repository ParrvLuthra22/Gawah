from fastapi import APIRouter
from gawah_contract import MerchantKey

from ..errors import GawahError

router = APIRouter(prefix="/v1/merchants", tags=["merchants"])

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
    _REGISTERED[body.merchant_id] = body
    return body


@router.get("/{merchant_id}/public-key", response_model=MerchantKey)
def get_public_key(merchant_id: str) -> MerchantKey:
    if merchant_id not in _REGISTERED:
        raise GawahError(404, "MERCHANT_NOT_FOUND", f"No merchant {merchant_id}")
    return _REGISTERED[merchant_id]
