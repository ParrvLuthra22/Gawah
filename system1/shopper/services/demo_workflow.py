"""Explicit demo-only path from a verified selection to a signed proposal."""

from dataclasses import dataclass, field

from gawah_contract import PurchaseProposal
from pydantic import BaseModel, Field

from shopper.agent.shopping_agent import ShoppingAgentResult
from shopper.merchant.demo_merchant import (
    MerchantError,
    ProposalPreparationRequest,
    ProposalPreparationService,
)
from shopper.models.shopping_intent import ShoppingIntent
from shopper.services.discovery import ProductVerifier


class DemoShoppingRequest(BaseModel):
    """Input for the deliberately explicit demo merchant workflow."""

    message: str = Field(min_length=1, max_length=4_000)
    mandate_id: str = Field(min_length=1, max_length=100)
    agent_id: str = Field(min_length=1, max_length=100)
    quantity: int = Field(default=1, ge=1, le=10)


class DemoShoppingResponse(BaseModel):
    intent: ShoppingIntent
    agent: ShoppingAgentResult
    proposal: PurchaseProposal | None = None
    purchase_ready: bool = False
    explanation: str


@dataclass
class DemoShoppingWorkflow:
    """Joins existing services without treating third-party listings as merchant carts."""

    proposals: ProposalPreparationService
    verifier: ProductVerifier = field(default_factory=ProductVerifier)

    def prepare(
        self,
        request: DemoShoppingRequest,
        intent: ShoppingIntent,
        result: ShoppingAgentResult,
    ) -> DemoShoppingResponse:
        selected = result.selected_product
        if selected is None:
            return DemoShoppingResponse(
                intent=intent,
                agent=result,
                explanation="No eligible demo merchant product was selected; no cart was created.",
            )
        verification = self.verifier.verify(selected, intent)
        if verification.outcome != "verified":
            return DemoShoppingResponse(
                intent=intent,
                agent=result,
                explanation="The selected product still requires verification; no cart was created.",
            )
        try:
            proposal = self.proposals.prepare(
                ProposalPreparationRequest(
                    selected_product_id=selected.id,
                    merchant_id=selected.merchant_id,
                    mandate_id=request.mandate_id,
                    agent_id=request.agent_id,
                    quantity=request.quantity,
                )
            )
        except MerchantError:
            return DemoShoppingResponse(
                intent=intent,
                agent=result,
                explanation="The demo merchant could not verify the selected product; no cart was created.",
            )
        return DemoShoppingResponse(
            intent=intent,
            agent=result,
            proposal=proposal,
            purchase_ready=True,
            explanation="Demo merchant verified and signed a cart for Guardian review.",
        )
