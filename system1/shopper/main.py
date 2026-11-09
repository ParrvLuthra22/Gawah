import logging
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from shopper.agent.intent_parser import IntentExtractionError, IntentParser
from shopper.agent.shopping_agent import ShoppingAgent, ShoppingAgentError, ShoppingAgentResult
from shopper.config import Settings
from shopper.integrations.groq_client import (
    GroqAuthenticationError,
    GroqRateLimitError,
    GroqServiceError,
)
from shopper.models.product import DiscoveryResult
from shopper.models.shopping_intent import ShoppingIntent
from shopper.providers.demo_catalog import ProductProviderError
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
    return discovery_service_for(Settings())


@lru_cache
def get_shopping_agent() -> ShoppingAgent:
    return ShoppingAgent(settings=Settings(), discovery_service=get_discovery_service())


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
        logger.warning("Shopping agent failure: stage=%s exception_type=%s", "tool_completion", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="AI service rate limit reached",
        ) from exc
    except (GroqAuthenticationError, GroqServiceError) as exc:
        logger.warning("Shopping agent failure: stage=%s exception_type=%s", "tool_completion", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Shopping service unavailable",
        ) from exc
    except ProductProviderError as exc:
        logger.warning("Shopping agent failure: stage=%s exception_type=%s", "product_provider", type(exc).__name__)
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
