from decimal import Decimal

from fastapi.testclient import TestClient
from gawah_contract.carts import load_public_key, verify_cart
from shopper.agent.shopping_agent import ShoppingAgentResult
from shopper.config import Settings
from shopper.main import (
    app,
    get_demo_shopping_agent,
    get_demo_shopping_workflow,
    get_intent_parser,
)
from shopper.merchant.demo_merchant import DemoMerchantAdapter, ProposalPreparationService
from shopper.models.product import Product
from shopper.models.shopping_intent import ShoppingIntent
from shopper.services.demo_workflow import DemoShoppingWorkflow


def demo_intent() -> ShoppingIntent:
    return ShoppingIntent(
        category="running shoes",
        brand="Nike",
        color="black",
        size="12",
        max_budget=Decimal(60),
        currency="USD",
    )


def selected_demo_product() -> Product:
    return Product(
        id="shoe-001",
        title="Nike SwiftRun 12",
        brand="Nike",
        category="running shoes",
        merchant_id="demo-sports",
        price=Decimal("59.99"),
        currency="USD",
        attributes={"color": "black", "size": "12"},
        description="Untrusted catalog text.",
    )


def test_demo_checkout_creates_a_merchant_signed_contract_proposal(tmp_path) -> None:
    intent = demo_intent()
    selected = selected_demo_product()

    class FakeParser:
        async def parse(self, _message: str) -> ShoppingIntent:
            return intent

    class FakeAgent:
        async def run(self, _intent: ShoppingIntent) -> ShoppingAgentResult:
            return ShoppingAgentResult(
                intent=intent,
                selected_product=selected,
                explanation="Eligible demo product.",
                alternatives=[],
                trace=[],
            )

    merchant = DemoMerchantAdapter(
        Settings(groq_api_key="test", merchant_signing_key_path=str(tmp_path / "merchant"))
    )
    workflow = DemoShoppingWorkflow(ProposalPreparationService(merchant))
    app.dependency_overrides[get_intent_parser] = lambda: FakeParser()
    app.dependency_overrides[get_demo_shopping_agent] = lambda: FakeAgent()
    app.dependency_overrides[get_demo_shopping_workflow] = lambda: workflow
    try:
        response = TestClient(app).post(
            "/shopping/demo/checkout",
            json={
                "message": "Find black Nike running shoes size 12 under $60",
                "mandate_id": "mandate-test",
                "agent_id": "agent-test",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["purchase_ready"] is True
    assert body["proposal"]["merchant_id"] == "demo-sports"
    # Verify the response cart with the merchant's public key rather than trusting its fields.
    from gawah_contract import PurchaseProposal

    contract = PurchaseProposal.model_validate(body["proposal"])
    key = merchant.merchant_key(contract.merchant_id)
    assert verify_cart(contract.signed_cart, load_public_key(key.public_key_pem))
    assert "private_key" not in response.text


def test_demo_checkout_does_not_sign_when_agent_has_no_selection(tmp_path) -> None:
    intent = demo_intent()

    class FakeParser:
        async def parse(self, _message: str) -> ShoppingIntent:
            return intent

    class FakeAgent:
        async def run(self, _intent: ShoppingIntent) -> ShoppingAgentResult:
            return ShoppingAgentResult(
                intent=intent,
                selected_product=None,
                explanation="No match.",
                alternatives=[],
                trace=[],
            )

    merchant = DemoMerchantAdapter(Settings(groq_api_key="test", merchant_signing_key_path=str(tmp_path / "merchant")))
    app.dependency_overrides[get_intent_parser] = lambda: FakeParser()
    app.dependency_overrides[get_demo_shopping_agent] = lambda: FakeAgent()
    app.dependency_overrides[get_demo_shopping_workflow] = lambda: DemoShoppingWorkflow(ProposalPreparationService(merchant))
    try:
        response = TestClient(app).post(
            "/shopping/demo/checkout",
            json={"message": "No match", "mandate_id": "mandate-test", "agent_id": "agent-test"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["proposal"] is None
    assert response.json()["purchase_ready"] is False
