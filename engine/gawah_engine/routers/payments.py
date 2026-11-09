from fastapi import APIRouter
from gawah_contract import PaymentRecord, PaymentStatus, RefundRequest
from gawah_contract.fixtures import load

from ..errors import GawahError

router = APIRouter(prefix="/v1/payments", tags=["payments"])


def _get(payment_id: str) -> PaymentRecord:
    p = PaymentRecord.model_validate(load("payment_paid"))
    if payment_id != p.id:
        raise GawahError(404, "PAYMENT_NOT_FOUND", f"No payment {payment_id}")
    return p


@router.get("", response_model=list[PaymentRecord])
def list_payments() -> list[PaymentRecord]:
    return [PaymentRecord.model_validate(load("payment_paid"))]


@router.get("/{payment_id}", response_model=PaymentRecord)
def get_payment(payment_id: str) -> PaymentRecord:
    return _get(payment_id)


@router.post("/{payment_id}/refund", response_model=PaymentRecord)
def refund_payment(payment_id: str, body: RefundRequest | None = None) -> PaymentRecord:
    return _get(payment_id).model_copy(update={"status": PaymentStatus.REFUNDED})
