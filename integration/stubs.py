"""SIMULATED Groq, so the end-to-end flow needs no network and no API key. Two stand-ins:

* `StubGroqClient`: the shopper's model (intent parsing and the tool-calling agent).
* `StubSemanticModel`: the engine Guardian's category check, which now fails closed when no model
  answers, so a run without Groq needs a model that does.

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


STUB_SEMANTIC_MODEL = "simulated-groq-stub"
_TITLE_RE = re.compile(r"title=(.*?) \| variant=")


def footwear_titles() -> frozenset[str]:
    """Titles the stub model knows are footwear: System 1's shoe catalog plus the honest title the
    attack fixtures reuse. Everything else (electronics, groceries, the substituted product) is a
    mismatch. Read from System 1's files so a catalog change is picked up."""
    from integration import scenarios as sc

    shoes = {r["title"] for r in sc.provider_catalog().values() if "shoe" in r["category"].casefold()}
    shoes |= {sc.attack_product(i).title for i in ("over_budget", "wrong_merchant", "authority_spoof")}
    return frozenset(shoes)


class StubSemanticModel:
    """SIMULATED model for the Guardian's `SemanticChecker(client=...)` seam: deterministic,
    never opens a connection. `match` only if every cart line title is a known footwear title."""

    def __init__(self) -> None:
        self._shoes = footwear_titles()

    def __call__(self, _model: str, messages: list[dict[str, str]]) -> str:
        titles = _TITLE_RE.findall(messages[-1]["content"])
        ok = bool(titles) and all(t in self._shoes for t in titles)
        return json.dumps({"category_match": "match" if ok else "mismatch",
                           "variant_issues": [], "confidence": 0.95})


def semantic_checker_factory(real_cls):
    """A drop-in for `gawah_engine.services.SemanticChecker` that always uses the stub model."""
    def factory(path, api_key="", **kwargs):
        kwargs.pop("models", None)
        return real_cls(path, api_key, client=StubSemanticModel(), models=[STUB_SEMANTIC_MODEL],
                        **kwargs)

    return factory


def install_engine_semantic_stub() -> None:
    """For an engine PROCESS (integration/engine_stub_app.py). Tests use monkeypatch instead."""
    from gawah_engine import services

    services.SemanticChecker = semantic_checker_factory(services.SemanticChecker)


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
