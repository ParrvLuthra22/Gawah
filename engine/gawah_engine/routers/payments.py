from fastapi import APIRouter
from gawah_contract import PaymentRecord, PaymentStatus, RefundRequest
from gawah_contract.fixtures import load

from ..config import settings
from ..errors import GawahError
from ..services import get_services

router = APIRouter(prefix="/v1/payments", tags=["payments"])

# GAWAH_MOCK=1 serves the fixture payment; GAWAH_MOCK=0 uses PaymentService (PayPal sandbox).


def _get(payment_id: str) -> PaymentRecord:
    p = PaymentRecord.model_validate(load("payment_paid"))
    if payment_id != p.id:
        raise GawahError(404, "PAYMENT_NOT_FOUND", f"No payment {payment_id}")
    return p


@router.get("", response_model=list[PaymentRecord])
def list_payments(
    mandate_id: str | None = None,
    status: PaymentStatus | None = None,
    paypal_order_id: str | None = None,
    paypal_capture_id: str | None = None,
) -> list[PaymentRecord]:
    if not settings.gawah_mock:
        return get_services().payments.list(
            mandate_id=mandate_id,
            status=status,
            paypal_order_id=paypal_order_id,
            paypal_capture_id=paypal_capture_id,
        )
    p = PaymentRecord.model_validate(load("payment_paid"))
    wanted = {
        "status": status,
        "paypal_order_id": paypal_order_id,
        "paypal_capture_id": paypal_capture_id,
    }
    ok = all(v is None or getattr(p, k) == v for k, v in wanted.items())
    return [p] if ok and mandate_id in (None, "man_01") else []


@router.get("/{payment_id}", response_model=PaymentRecord)
def get_payment(payment_id: str) -> PaymentRecord:
    if not settings.gawah_mock:
        return get_services().payments.get(payment_id)
    return _get(payment_id)


@router.post("/{payment_id}/refund", response_model=PaymentRecord)
def refund_payment(payment_id: str, body: RefundRequest | None = None) -> PaymentRecord:
    if not settings.gawah_mock:
        return get_services().payments.refund(payment_id, body.reason if body else None)
    return _get(payment_id).model_copy(update={"status": PaymentStatus.REFUNDED})
