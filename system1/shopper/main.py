import logging
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from gawah_contract import PurchaseProposal
from pydantic import BaseModel, Field, field_validator

from shopper.agent.intent_parser import IntentExtractionError, IntentParser
from shopper.agent.shopping_agent import ShoppingAgent, ShoppingAgentError, ShoppingAgentResult
from shopper.config import Settings
from shopper.integrations.groq_client import (
    GroqAuthenticationError,
    GroqRateLimitError,
    GroqServiceError,
)
from shopper.merchant.demo_merchant import (
    DemoMerchantAdapter,
    MerchantError,
    MerchantProduct,
    ProposalPreparationRequest,
    ProposalPreparationService,
)
from shopper.models.product import DiscoveryResult
from shopper.models.shopping_intent import ShoppingIntent
from shopper.providers.channel3 import Channel3ProviderError
from shopper.providers.demo_catalog import DemoCatalogProvider, ProductProviderError
from shopper.security.attack_simulation import AttackRequest, AttackResult, run_attack
from shopper.services.demo_workflow import (
    DemoShoppingRequest,
    DemoShoppingResponse,
    DemoShoppingWorkflow,
)
from shopper.services.discovery import DiscoveryService, discovery_service_for

app = FastAPI(title="Gawah Shopping Agent")
logger = logging.getLogger(__name__)


@app.get("/health")
def health():
    return {"status": "ok", "system": "system1"}


class ShoppingIntentRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4_000)

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be blank")
        return value


@lru_cache
def get_intent_parser() -> IntentParser:
    return IntentParser(settings=Settings())


@lru_cache
def get_discovery_service() -> DiscoveryService:
    service = discovery_service_for(Settings())
    logger.info("Shopping discovery configured provider=%s", service.provider_name)
    return service


@lru_cache
def get_shopping_agent() -> ShoppingAgent:
    discovery_service = get_discovery_service()
    logger.info("Shopping agent configured provider=%s", discovery_service.provider_name)
    return ShoppingAgent(settings=Settings(), discovery_service=discovery_service)


@lru_cache
def get_demo_shopping_agent() -> ShoppingAgent:
    """An explicit demo catalog agent; never a fallback for Channel3."""
    return ShoppingAgent(settings=Settings(), discovery_service=DiscoveryService(DemoCatalogProvider()))


@lru_cache
def get_proposal_preparation_service() -> ProposalPreparationService:
    return ProposalPreparationService(merchant=DemoMerchantAdapter(Settings()))


@lru_cache
def get_demo_shopping_workflow() -> DemoShoppingWorkflow:
    return DemoShoppingWorkflow(proposals=get_proposal_preparation_service())


class ShoppingDiscoveryResponse(BaseModel):
    intent: ShoppingIntent
    discovery: DiscoveryResult


@app.post("/shopping/intent", response_model=ShoppingIntent)
async def extract_shopping_intent(
    request: ShoppingIntentRequest,
    parser: Annotated[IntentParser, Depends(get_intent_parser)],
) -> ShoppingIntent:
    """Extract stated shopping constraints; this endpoint never searches or purchases."""
    try:
        return await parser.parse(request.message)
    except GroqRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="AI service rate limit reached",
        ) from exc
    except (GroqAuthenticationError, GroqServiceError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI service unavailable",
        ) from exc
    except IntentExtractionError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI service returned an invalid response",
        ) from exc


@app.post("/shopping/discover", response_model=ShoppingDiscoveryResponse)
async def discover_products(
    request: ShoppingIntentRequest,
    parser: Annotated[IntentParser, Depends(get_intent_parser)],
    discovery_service: Annotated[DiscoveryService, Depends(get_discovery_service)],
) -> ShoppingDiscoveryResponse:
    """Extract constraints and deterministically filter products; never purchases."""
    try:
        intent = await parser.parse(request.message)
    except GroqRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="AI service rate limit reached",
        ) from exc
    except (GroqAuthenticationError, GroqServiceError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI service unavailable",
        ) from exc
    except IntentExtractionError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI service returned an invalid response",
        ) from exc

    try:
        discovery = await discovery_service.discover(intent)
    except Channel3ProviderError as exc:
        logger.warning("Product provider failure: provider=channel3 code=%s", exc.code)
        if exc.code == "rate_limited":
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Product provider rate limit reached",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Product provider unavailable",
        ) from exc
    except ProductProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Product provider unavailable",
        ) from exc
    return ShoppingDiscoveryResponse(intent=intent, discovery=discovery)


@app.post("/shopping/agent", response_model=ShoppingAgentResult)
async def run_shopping_agent(
    request: ShoppingIntentRequest,
    parser: Annotated[IntentParser, Depends(get_intent_parser)],
    agent: Annotated[ShoppingAgent, Depends(get_shopping_agent)],
) -> ShoppingAgentResult:
    """Use bounded local tools to select one already eligible product, if any."""
    try:
        intent = await parser.parse(request.message)
    except GroqRateLimitError as exc:
        logger.warning("Shopping agent failure: stage=%s exception_type=%s", "intent", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="AI service rate limit reached",
        ) from exc
    except (GroqAuthenticationError, GroqServiceError) as exc:
        logger.warning("Shopping agent failure: stage=%s exception_type=%s", "intent", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Shopping service unavailable",
        ) from exc
    except (IntentExtractionError, ShoppingAgentError) as exc:
        logger.warning("Shopping agent failure: stage=%s exception_type=%s", "intent", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Shopping agent returned an invalid response",
        ) from exc

    try:
        return await agent.run(intent)
    except GroqRateLimitError as exc:
        logger.warning(
            "Shopping agent failure: stage=%s exception_type=%s",
            "tool_completion",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="AI service rate limit reached",
        ) from exc
    except (GroqAuthenticationError, GroqServiceError) as exc:
        logger.warning(
            "Shopping agent failure: stage=%s exception_type=%s",
            "tool_completion",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Shopping service unavailable",
        ) from exc
    except Channel3ProviderError as exc:
        logger.warning("Shopping agent provider failure: provider=channel3 code=%s", exc.code)
        if exc.code == "rate_limited":
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="AI service rate limit reached",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Shopping service unavailable",
        ) from exc
    except ProductProviderError as exc:
        logger.warning(
            "Shopping agent failure: stage=%s exception_type=%s",
            "product_provider",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Shopping service unavailable",
        ) from exc
    except ShoppingAgentError as exc:
        logger.warning("Shopping agent failure: stage=%s exception_type=%s", "agent", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Shopping agent returned an invalid response",
        ) from exc


@app.post("/shopping/proposals/prepare", response_model=PurchaseProposal)
async def prepare_purchase_proposal(
    request: ProposalPreparationRequest,
    service: Annotated[ProposalPreparationService, Depends(get_proposal_preparation_service)],
) -> PurchaseProposal:
    """Prepare a merchant-signed proposal; Guardian authorization is intentionally external."""
    try:
        return service.prepare(request)
    except MerchantError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Merchant verification failed",
        ) from exc


@app.post("/shopping/demo/checkout", response_model=DemoShoppingResponse)
async def demo_checkout(
    request: DemoShoppingRequest,
    parser: Annotated[IntentParser, Depends(get_intent_parser)],
    agent: Annotated[ShoppingAgent, Depends(get_demo_shopping_agent)],
    workflow: Annotated[DemoShoppingWorkflow, Depends(get_demo_shopping_workflow)],
) -> DemoShoppingResponse:
    """Explicit demo-only request-to-proposal flow; it never contacts Guardian or payments."""
    try:
        intent = await parser.parse(request.message)
        result = await agent.run(intent)
    except GroqRateLimitError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="AI service rate limit reached") from exc
    except (GroqAuthenticationError, GroqServiceError, ProductProviderError) as exc:
        logger.warning("Demo checkout failure: stage=selection exception_type=%s", type(exc).__name__)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Shopping service unavailable") from exc
    except (IntentExtractionError, ShoppingAgentError) as exc:
        logger.warning("Demo checkout failure: stage=selection exception_type=%s", type(exc).__name__)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Shopping agent returned an invalid response") from exc
    return workflow.prepare(request, intent, result)


@app.get("/shopping/demo/products", response_model=list[MerchantProduct])
def list_demo_products(
    service: Annotated[ProposalPreparationService, Depends(get_proposal_preparation_service)],
) -> list[MerchantProduct]:
    """List only the merchant's public catalog; this does not create a cart."""
    return service.merchant.list_products()


@app.post("/shopping/test/attacks/run", response_model=AttackResult, include_in_schema=False)
async def run_attack_simulation(request: AttackRequest) -> AttackResult:
    """Test-only local fixture runner; no live products, signing, Guardian, or payments."""
    if not Settings().attack_simulation_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return await run_attack(request.scenario_id)
