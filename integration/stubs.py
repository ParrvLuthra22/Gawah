"""A SIMULATED Groq for the shopper, so the end-to-end flow needs no network and no API key.

`StubGroqClient` replaces `shopper.integrations.groq_client.GroqIntentClient` at its seam (the two
`create_*_completion` methods), so the shopper's own intent parser, discovery filter, tool-calling
loop, selection checks and merchant signing all run for real. Only the language model is faked: it
reads the request with a few regexes and always picks the cheapest eligible product.
"""

import json
import re
from decimal import Decimal
from typing import Any

from shopper.agent.intent_parser import IntentParser
from shopper.agent.shopping_agent import ShoppingAgent
from shopper.config import Settings
from shopper.main import (
    app as shopper_app,
)
from shopper.main import (
    get_demo_shopping_agent,
    get_demo_shopping_workflow,
    get_intent_parser,
    get_proposal_preparation_service,
    get_shopping_agent,
)
from shopper.merchant.demo_merchant import DemoMerchantAdapter, ProposalPreparationService
from shopper.providers.demo_catalog import DemoCatalogProvider
from shopper.services.demo_workflow import DemoShoppingWorkflow
from shopper.services.discovery import DiscoveryService

_COLORS = ("black", "blue", "white", "red", "silver")


class StubGroqClient:
    """SIMULATED model. Deterministic; never opens a connection."""

    async def create_json_completion(self, *, system_prompt: str, user_message: str) -> str:
        text = user_message.casefold()
        intent: dict[str, Any] = {"category": None, "brand": None, "color": None, "size": None,
                                  "audience": None, "size_system": None, "max_budget": None,
                                  "currency": None, "preferences": []}
        if "running shoes" in text:
            intent["category"] = "running shoes"
        if "nike" in text:
            intent["brand"] = "Nike"
        intent["color"] = next((c for c in _COLORS if c in text), None)
        if m := re.search(r"size (\d+)", text):
            intent["size"] = m.group(1)
        if m := re.search(r"\$(\d+(?:\.\d+)?)", text):
            intent["max_budget"], intent["currency"] = m.group(1), "USD"
        return json.dumps(intent)

    async def create_tool_completion(self, *, messages: list[dict[str, Any]], tools: Any,
                                     turn: int) -> dict[str, Any]:
        results = [m for m in messages if m.get("role") == "tool"]
        if not results:
            call = {"id": "stub-call-1", "type": "function",
                    "function": {"name": "search_products", "arguments": "{}"}}
            return {"content": "", "tool_calls": [call]}
        products = json.loads(results[-1]["content"]).get("products", [])
        best = min(products, key=lambda p: Decimal(p["price"]), default=None)
        return {"tool_calls": [], "content": json.dumps({
            "selected_product_id": best["id"] if best else None,
            "explanation": "SIMULATED model: the cheapest product that meets every constraint.",
        })}


def install_stubs(settings: Settings) -> None:
    """Point the shopper app's dependencies at the stub model. Call `remove_stubs` to undo.

    The demo merchant adapter is built from `settings`, so its signing key lives where
    `settings.merchant_signing_key_path` says (the e2e script reads the same path)."""
    stub = StubGroqClient()
    demo_discovery = DiscoveryService(DemoCatalogProvider())
    parser = IntentParser(settings, client=stub)  # type: ignore[arg-type]
    agent = ShoppingAgent(settings, demo_discovery, client=stub)
    service = ProposalPreparationService(merchant=DemoMerchantAdapter(settings))
    workflow = DemoShoppingWorkflow(proposals=service)
    overrides = shopper_app.dependency_overrides
    overrides[get_intent_parser] = lambda: parser
    overrides[get_shopping_agent] = lambda: agent
    overrides[get_demo_shopping_agent] = lambda: agent
    overrides[get_proposal_preparation_service] = lambda: service
    overrides[get_demo_shopping_workflow] = lambda: workflow


def remove_stubs() -> None:
    shopper_app.dependency_overrides.clear()
