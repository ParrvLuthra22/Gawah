from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter
from gawah_contract import Mandate, MandateCreate, MandateStatus
from gawah_contract.fixtures import load

from ..errors import GawahError

router = APIRouter(prefix="/v1/mandates", tags=["mandates"])


def _fixture() -> Mandate:
    return Mandate.model_validate(load("mandate_shoes"))


def _get(mandate_id: str) -> Mandate:
    m = _fixture()
    if mandate_id != m.id:
        raise GawahError(404, "MANDATE_NOT_FOUND", f"No mandate {mandate_id}")
    return m


@router.post("", response_model=Mandate, status_code=201)
def create_mandate(body: MandateCreate) -> Mandate:
    base = _fixture()
    mock_id = f"man_{uuid4().hex[:8]}"
    return base.model_copy(
        update={
            **body.model_dump(),
            "id": mock_id,
            "issued_at": datetime.now(UTC),
            "status": MandateStatus.ACTIVE,
            "signature": f"mock-signature-mandate-{mock_id}",
        }
    )


@router.get("", response_model=list[Mandate])
def list_mandates() -> list[Mandate]:
    return [_fixture()]


@router.get("/{mandate_id}", response_model=Mandate)
def get_mandate(mandate_id: str) -> Mandate:
    return _get(mandate_id)


@router.post("/{mandate_id}/revoke", response_model=Mandate)
def revoke_mandate(mandate_id: str) -> Mandate:
    return _get(mandate_id).model_copy(update={"status": MandateStatus.REVOKED})
