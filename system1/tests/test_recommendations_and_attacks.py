import asyncio
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from shopper.agent.shopping_agent import ShoppingAgent
from shopper.config import Settings
from shopper.main import app
from shopper.models.product import Product
from shopper.models.shopping_intent import ShoppingIntent
from shopper.security.attack_simulation import fixtures, run_attack
from shopper.services.discovery import DiscoveryService


class Provider:
    name = "test"

    def __init__(self, products):
        self.products = products

    async def list_products(self, _intent):
        return self.products


class FinalClient:
    async def create_tool_completion(self, **_kwargs):
        return {"content": '{"selected_product_id":null,"explanation":"Verify first."}', "tool_calls": []}


def intent():
    return ShoppingIntent(category="shoes", brand="Nike", color="black", size="12", max_budget=Decimal(80), currency="USD")


def product(**changes):
    data = {"id": "shoe", "title": "Nike Runner", "brand": "Nike", "category": "shoes", "merchant_id": "m", "price": Decimal(60), "currency": "USD", "attributes": {"color": "black", "size": "12"}, "description": "untrusted"}
    data.update(changes)
    return Product(**data)


def test_unverified_product_is_ranked_but_not_purchase_ready():
    unverified = product(id="verify", attributes={"color": "black"})
    agent = ShoppingAgent(Settings(groq_api_key="test-key"), DiscoveryService(Provider([unverified])), FinalClient())
    result = asyncio.run(agent.run(intent()))
    assert result.selected_product is None and result.purchase_ready is False
    assert result.recommended_product is not None
    assert result.recommended_product.verification_status == "needs_verification"
    assert result.recommended_product.missing_verifications == ["missing_size"]


def test_verified_product_is_ranked_deterministically_and_selectable():
    a, b = product(id="b", price=Decimal(70)), product(id="a", price=Decimal(60))
    service = DiscoveryService(Provider([a, b]))
    discovery = asyncio.run(service.discover(intent()))
    ranked = ShoppingAgent._rank_recommendations(intent(), discovery.eligible, [])
    assert [item.product.id for item in ranked] == ["a", "b"]


def test_gendered_or_child_size_is_not_ranked_as_verified_for_ambiguous_size():
    women = product(
        id="women", product_type="Women's Running Shoes", attributes={"color": "black", "size": "12"}
    )
    child = product(
        id="child", product_type="Children's Running Shoes", attributes={"color": "black", "size": "12"}
    )
    result = asyncio.run(DiscoveryService(Provider([women, child])).discover(intent()))
    assert result.eligible == []
    assert result.needs_verification[0].product.id == "women"
    assert "ambiguous_size_system" in result.needs_verification[0].reasons
    assert result.rejected[0].product.id == "child"


def test_verified_relevance_beats_cheaper_unverified_listing():
    verified = product(id="verified", price=Decimal(59), attributes={"color": "black", "size": "12"})
    uncertain = product(id="uncertain", price=Decimal(1), attributes={"color": "black"})
    result = asyncio.run(DiscoveryService(Provider([uncertain, verified])).discover(intent()))
    ranked = ShoppingAgent._rank_recommendations(intent(), result.eligible, result.needs_verification)
    assert [item.product.id for item in ranked] == ["verified", "uncertain"]


def test_mens_requirement_rejects_women_and_kids_and_ranks_running_first():
    mens_running = product(
        id="run", price=Decimal(49), product_type="Men's Running Shoes",
        attributes={"color": "black", "size": "10", "audience": "men", "size_system": "US"},
    )
    cheap_sneaker = product(
        id="sneaker", price=Decimal(1), product_type="Men's Sneakers",
        attributes={"color": "black", "size": "10", "audience": "men", "size_system": "US"},
    )
    women = product(id="women", product_type="Women's Running Shoes", attributes={"color": "black", "size": "10", "audience": "women", "size_system": "US"})
    kids = product(id="kids", product_type="Kids Running Shoes", attributes={"color": "black", "size": "10", "audience": "kids", "size_system": "US"})
    mens_intent = intent().model_copy(update={"size": "10", "audience": "men", "size_system": "US", "category": "running shoes"})
    result = asyncio.run(DiscoveryService(Provider([cheap_sneaker, women, kids, mens_running])).discover(mens_intent))
    ranked = ShoppingAgent._rank_recommendations(mens_intent, result.eligible, result.needs_verification)
    assert [item.product.id for item in result.rejected] == ["women", "kids"]
    assert [item.product.id for item in ranked] == ["run", "sneaker"]


def test_male_gender_without_adult_audience_is_not_treated_as_mens():
    unknown = product(
        id="unknown", product_type="Running Shoes",
        attributes={"color": "black", "size": "10", "gender": "male", "size_system": "UK"},
    )
    toddler = product(
        id="toddler", product_type="(TD) Nike Downshifter 8 Running Shoes",
        attributes={"color": "black", "size": "10", "size_system": "UK"},
    )
    grade_school = product(
        id="grade-school", product_type="Grade School Boys Cosmic Runner Running Shoe",
        attributes={"color": "black", "size": "10", "size_system": "UK"},
    )
    mens_uk = intent().model_copy(update={"size": "10", "audience": "men", "size_system": "UK", "category": "running shoes"})
    result = asyncio.run(DiscoveryService(Provider([unknown, toddler, grade_school])).discover(mens_uk))
    assert result.eligible == []
    assert result.needs_verification[0].product.id == "unknown"
    assert result.needs_verification[0].reasons == ["missing_audience"]
    assert [item.product.id for item in result.rejected] == ["toddler", "grade-school"]


def test_conflicting_requested_size_system_is_rejected_without_ambiguous_reason():
    us_product = product(
        id="us", attributes={"color": "black", "size": "10", "audience": "men", "size_system": "US"}
    )
    uk_intent = intent().model_copy(update={"size": "10", "audience": "men", "size_system": "UK"})
    result = asyncio.run(DiscoveryService(Provider([us_product])).discover(uk_intent))
    assert result.rejected[0].reasons == ["size_system_mismatch"]


@pytest.mark.parametrize("scenario_id", list(fixtures()))
def test_attack_fixtures_are_isolated_and_resisted(scenario_id):
    result = asyncio.run(run_attack(scenario_id))
    assert result.observed_outcome == "ATTACK_RESISTED"
    assert result.selected_product_id is None and result.purchase_proposal_id is None
    assert result.original_intent.max_budget == Decimal(80)


def test_attack_endpoint_is_disabled_without_explicit_test_mode(monkeypatch):
    monkeypatch.setattr("shopper.main.Settings", lambda: Settings(groq_api_key="test-key"))
    response = TestClient(app).post("/shopping/test/attacks/run", json={"scenario_id": "over_budget"})
    assert response.status_code == 404


def test_attack_endpoint_runs_only_when_test_mode_is_explicit(monkeypatch):
    monkeypatch.setattr(
        "shopper.main.Settings", lambda: Settings(groq_api_key="test-key", attack_simulation_enabled=True)
    )
    response = TestClient(app).post("/shopping/test/attacks/run", json={"scenario_id": "authority_spoof"})
    assert response.status_code == 200
    assert response.json()["observed_outcome"] == "ATTACK_RESISTED"
