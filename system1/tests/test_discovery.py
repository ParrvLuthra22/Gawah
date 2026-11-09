import asyncio
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from shopper.main import app, get_discovery_service, get_intent_parser
from shopper.models.product import Product
from shopper.models.shopping_intent import ShoppingIntent
from shopper.providers.demo_catalog import ProductProviderError
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

    async def list_products(self) -> list[Product]:
        return self.products


class FailingProvider:
    name = "test"

    async def list_products(self) -> list[Product]:
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
    assert result.rejected[0].reasons == ["missing_size"]
    assert result.rejected[1].reasons == ["over_budget"]


def test_currency_mismatch_is_rejected() -> None:
    service = DiscoveryService(StaticProvider([product(currency="EUR")]))

    result = asyncio.run(service.discover(shoe_intent()))

    assert result.eligible == []
    assert result.rejected[0].reasons == ["currency_mismatch"]


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
    bread_intent = ShoppingIntent(category="atta bread", max_budget=Decimal("10"), currency="USD")

    result = asyncio.run(service.discover(bread_intent))

    assert [item.id for item in result.eligible] == ["atta-1", "atta-2"]
    assert [item.product.id for item in result.rejected] == ["white", "apples"]
    assert all(item.reasons == ["category_mismatch"] for item in result.rejected)


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
