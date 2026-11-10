from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from gawah_contract import PurchaseProposal
from gawah_contract.carts import load_public_key, verify_cart
from pydantic import ValidationError
from shopper.config import Settings
from shopper.main import app, get_proposal_preparation_service
from shopper.merchant.demo_merchant import (
    DemoMerchantAdapter,
    MerchantError,
    ProposalPreparationRequest,
    ProposalPreparationService,
)


def service_for(tmp_path) -> ProposalPreparationService:
    settings = Settings(groq_api_key="test-key", merchant_signing_key_path=str(tmp_path / "merchant-keys"))
    return ProposalPreparationService(DemoMerchantAdapter(settings))


def request(**overrides: object) -> ProposalPreparationRequest:
    body: dict[str, object] = {
        "selected_product_id": "shoe-001",
        "merchant_id": "demo-sports",
        "mandate_id": "mandate-test",
        "agent_id": "shopper-agent-test",
        "quantity": 1,
    }
    body.update(overrides)
    return ProposalPreparationRequest.model_validate(body)


def test_prepares_merchant_verified_signed_cart(tmp_path) -> None:
    service = service_for(tmp_path)

    proposal = service.prepare(request())

    assert proposal.merchant_id == "demo-sports"
    assert proposal.product.id == "NIKE-SWIFTRUN-12-BLK"
    assert proposal.product.id == proposal.signed_cart.line_items[0].sku
    assert proposal.unit_price_minor == 5999
    assert proposal.signed_cart.total_minor == 5999
    merchant_key = service.merchant.merchant_key(proposal.merchant_id)
    assert verify_cart(proposal.signed_cart, load_public_key(merchant_key.public_key_pem))
    assert service.merchant.cart_is_current(proposal.signed_cart)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"selected_product_id": "missing"}, "unknown merchant product"),
        ({"selected_product_id": "shoe-002"}, "insufficient stock"),
        ({"merchant_id": "other-merchant"}, "merchant mismatch"),
    ],
)
def test_merchant_rejects_invalid_fulfillment_requests(tmp_path, body, message) -> None:
    with pytest.raises(MerchantError, match=message):
        service_for(tmp_path).prepare(request(**body))


def test_cart_uses_authoritative_price_and_client_cannot_supply_one(tmp_path) -> None:
    with pytest.raises(ValidationError):
        ProposalPreparationRequest.model_validate(
            {**request().model_dump(), "unit_price_minor": 1}
        )

    service = service_for(tmp_path)
    proposal = service.prepare(request(quantity=2))
    line_item = proposal.signed_cart.line_items[0]
    assert proposal.unit_price_minor == line_item.unit_price_minor == 5999
    assert proposal.signed_cart.total_minor == 11_998
    assert line_item.quantity == proposal.quantity == 2


def test_signed_cart_rejects_all_commercial_field_tampering(tmp_path) -> None:
    service = service_for(tmp_path)
    cart = service.prepare(request()).signed_cart
    line_item = cart.line_items[0]
    tampered_carts = [
        cart.model_copy(update={"total_minor": 1}),
        cart.model_copy(update={"line_items": [line_item.model_copy(update={"unit_price_minor": 1})]}),
        cart.model_copy(update={"line_items": [line_item.model_copy(update={"quantity": 2})]}),
        cart.model_copy(update={"merchant_id": "other-merchant"}),
        cart.model_copy(update={"expires_at": cart.expires_at + timedelta(seconds=1)}),
    ]

    assert all(not service.merchant.verify_cart(tampered) for tampered in tampered_carts)


def test_expired_cart_is_not_current(tmp_path) -> None:
    service = service_for(tmp_path)
    proposal = service.prepare(request())
    expired_unsigned = proposal.signed_cart.model_copy(
        update={"expires_at": datetime.now(UTC) - timedelta(seconds=1), "signature": ""}
    )
    signature, key_id = service.merchant._signer.sign(expired_unsigned.model_dump(mode="json"))
    expired = expired_unsigned.model_copy(update={"signature": signature, "key_id": key_id})

    assert service.merchant.verify_cart(expired) is True
    assert service.merchant.cart_is_current(expired) is False


def test_prepare_endpoint_returns_contract_proposal(tmp_path) -> None:
    service = service_for(tmp_path)
    app.dependency_overrides[get_proposal_preparation_service] = lambda: service
    try:
        response = TestClient(app).post("/shopping/proposals/prepare", json=request().model_dump())
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    proposal = PurchaseProposal.model_validate(body)
    assert proposal.unit_price_minor == 5999
    assert "private_key" not in response.text
    assert "BEGIN PRIVATE KEY" not in response.text
    assert "merchant-keys" not in response.text
