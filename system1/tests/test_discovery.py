import asyncio
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from shopper.main import app, get_discovery_service, get_intent_parser
from shopper.models.product import Product
from shopper.models.shopping_intent import ShoppingIntent
from shopper.providers.demo_catalog import DemoCatalogProvider, ProductProviderError
from shopper.services.discovery import DiscoveryService


def product(**overrides: object) -> Product:
    data: dict[str, object] = {
        "id": "shoe-1",
        "title": "Nike Runner",
        "brand": "Nike",
        "category": "running shoes",
        "merchant_id": "demo-sports",
        "price": Decimal("59.99"),
        "currency": "USD",
        "attributes": {"color": "black", "size": "12"},
        "description": "Untrusted catalog text.",
    }
    data.update(overrides)
    return Product.model_validate(data)


class StaticProvider:
    name = "test"

    def __init__(self, products: list[Product]) -> None:
        self.products = products

    async def list_products(self, _intent: ShoppingIntent) -> list[Product]:
        return self.products


class FailingProvider:
    name = "test"

    async def list_products(self, _intent: ShoppingIntent) -> list[Product]:
        raise ProductProviderError("offline")


def shoe_intent() -> ShoppingIntent:
    return ShoppingIntent(
        category="shoes",
        brand="Nike",
        color="black",
        size="12",
        max_budget=Decimal(60),
        currency="USD",
    )


def test_filtering_preserves_rejection_reasons_and_requires_attributes() -> None:
    service = DiscoveryService(
        StaticProvider(
            [
                product(),
                product(id="missing-size", attributes={"color": "black"}),
                product(id="over-budget", price=Decimal(61)),
            ]
        )
    )

    result = asyncio.run(service.discover(shoe_intent()))

    assert [item.id for item in result.eligible] == ["shoe-1"]
    assert result.needs_verification[0].reasons == ["missing_size"]
    assert result.rejected[0].reasons == ["over_budget"]
    verification = {item.product_id: item for item in result.verifications}
    assert verification["shoe-1"].outcome == "verified"
    assert verification["shoe-1"].purchase_ready is False
    assert verification["missing-size"].attributes["size"].reason == "missing_size"


def test_currency_mismatch_is_rejected() -> None:
    service = DiscoveryService(StaticProvider([product(currency="EUR")]))

    result = asyncio.run(service.discover(shoe_intent()))

    assert result.eligible == []
    assert result.rejected[0].reasons == ["currency_mismatch"]


def test_footwear_taxonomy_is_needs_verification_without_conflating_audiences() -> None:
    service = DiscoveryService(
        StaticProvider(
            [
                product(id="athletic", category="shoes", product_type="Athletic Shoes"),
                product(id="kids", category="kids shoes", product_type="Kids Sneakers"),
            ]
        )
    )
    adult_running = ShoppingIntent(
        category="men's running shoes",
        brand="Nike",
        color="black",
        size="12",
        max_budget=Decimal(60),
        currency="USD",
    )

    result = asyncio.run(service.discover(adult_running))

    assert result.eligible == []
    assert result.needs_verification[0].product.id == "athletic"
    assert result.needs_verification[0].reasons == ["category_needs_verification"]
    assert result.rejected[0].product.id == "kids"
    assert result.rejected[0].reasons == ["category_mismatch"]


def test_specific_bread_type_matches_only_atta_bread() -> None:
    service = DiscoveryService(
        StaticProvider(
            [
                product(id="atta-1", category="groceries", product_type="atta bread", price=Decimal("4.50")),
                product(id="atta-2", category="groceries", product_type="atta bread", price=Decimal("4.00")),
                product(id="white", category="groceries", product_type="white bread", price=Decimal("3.25")),
                product(id="apples", category="groceries", product_type="apples", price=Decimal("6.00")),
            ]
        )
    )
    bread_intent = ShoppingIntent(category="atta bread", max_budget=Decimal(10), currency="USD")

    result = asyncio.run(service.discover(bread_intent))

    assert [item.id for item in result.eligible] == ["atta-1", "atta-2"]
    assert [item.product.id for item in result.rejected] == ["white", "apples"]
    assert all(item.reasons == ["category_mismatch"] for item in result.rejected)


def test_demo_catalog_matches_two_atta_breads_not_white_bread_or_apples() -> None:
    result = asyncio.run(
        DiscoveryService(DemoCatalogProvider()).discover(
            ShoppingIntent(category="atta bread", max_budget=Decimal(10), currency="USD")
        )
    )

    assert [item.id for item in result.eligible] == ["grocery-001", "grocery-004"]
    rejected_ids = {item.product.id for item in result.rejected}
    assert {"grocery-002", "grocery-003"}.issubset(rejected_ids)


def test_provider_errors_are_propagated() -> None:
    with pytest.raises(ProductProviderError):
        asyncio.run(DiscoveryService(FailingProvider()).discover(shoe_intent()))


def test_discovery_endpoint_returns_intent_and_results() -> None:
    intent = shoe_intent()

    class FakeParser:
        async def parse(self, _message: str) -> ShoppingIntent:
            return intent

    service = DiscoveryService(StaticProvider([product()]))
    app.dependency_overrides[get_intent_parser] = lambda: FakeParser()
    app.dependency_overrides[get_discovery_service] = lambda: service
    try:
        response = TestClient(app).post(
            "/shopping/discover",
            json={"message": "Find Nike shoes in black size 12 under $60"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["intent"]["brand"] == "Nike"
    assert body["discovery"]["eligible"][0]["id"] == "shoe-1"
