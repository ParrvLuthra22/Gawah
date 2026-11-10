import asyncio
import json
from decimal import Decimal

import httpx
import pytest
from shopper.models.shopping_intent import ShoppingIntent
from shopper.providers.channel3 import Channel3Provider, Channel3ProviderError
from shopper.services.discovery import DiscoveryService


def intent() -> ShoppingIntent:
    return ShoppingIntent(
        brand="Nike",
        category="shoes",
        color="black",
        size="12",
        max_budget=Decimal(60),
        currency="USD",
    )


def response_body(*, price: float = 59.99, color: str = "Black") -> dict[str, object]:
    return {
        "products": [
            {
                "id": "c3-shoe-1",
                "title": "Nike Runner",
                "description": "Untrusted external catalog text.",
                "brands": [{"id": "nike", "name": "Nike"}],
                "category": {"slug": "running-shoes", "title": "Running Shoes"},
                "structured_attributes": {"color": [color], "size": ["12"]},
                "offers": [
                    {
                        "url": "https://merchant.example/shoe",
                        "domain": "merchant.example",
                        "price": {"price": price, "currency": "USD"},
                    }
                ],
            }
        ]
    }


def provider(handler) -> tuple[Channel3Provider, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Channel3Provider("test-channel3-key", client=client), client


def test_channel3_maps_documented_search_response_and_request() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://api.trychannel3.com/v1/search")
        assert request.headers["x-api-key"] == "test-channel3-key"
        assert json.loads(request.content) == {
            "query": "Nike shoes black 12",
            "limit": 30,
            "filters": {"price": {"max_price": 60.0}},
            "config": {"currency": "USD"},
        }
        return httpx.Response(200, json=response_body())

    channel3, client = provider(handler)
    try:
        products = asyncio.run(channel3.list_products(intent()))
    finally:
        asyncio.run(client.aclose())

    assert len(products) == 1
    product = products[0]
    assert product.id == "c3-shoe-1:merchant.example:0"
    assert product.merchant_id == "merchant.example"
    assert product.price == Decimal("59.99")
    assert product.attributes == {"color": "Black", "size": "12"}


def test_channel3_results_still_use_deterministic_local_filtering() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_body(price=61.0, color="White"))

    channel3, client = provider(handler)
    try:
        result = asyncio.run(DiscoveryService(channel3).discover(intent()))
    finally:
        asyncio.run(client.aclose())

    assert result.eligible == []
    assert result.rejected[0].reasons == ["over_budget", "color_mismatch"]


def test_channel3_fetches_canonical_detail_when_search_omits_variant_options() -> None:
    detail = response_body()
    detail_product = detail["products"][0]
    assert isinstance(detail_product, dict)
    detail_product.pop("structured_attributes")
    detail_product["variants"] = {
        "options": [
            {"name": "Color", "values": [{"label": "Black", "available": "InStock"}]},
            {"name": "UK Size", "values": [{"label": "12", "available": "InStock"}]},
        ]
    }
    selected = {**detail_product, "variants": {"selected": [{"name": "Color", "label": "Black"}, {"name": "UK Size", "label": "12"}]}}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            # The realistic search record has neither options nor selected variants.
            search = response_body()
            search_product = search["products"][0]
            assert isinstance(search_product, dict)
            search_product.pop("structured_attributes")
            return httpx.Response(200, json=search)
        if not request.url.params:
            return httpx.Response(200, json=detail_product)
        assert request.url.params["option_Color"] == "Black"
        assert request.url.params["option_UK Size"] == "12"
        return httpx.Response(200, json=selected)

    channel3, client = provider(handler)
    try:
        product = asyncio.run(channel3.list_products(intent()))[0]
    finally:
        asyncio.run(client.aclose())

    assert product.attributes == {"color": "Black", "size": "12", "size_system": "UK"}


def test_channel3_handles_no_results_and_malformed_responses() -> None:
    async def no_results(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"products": []})

    channel3, client = provider(no_results)
    try:
        assert asyncio.run(channel3.list_products(intent())) == []
    finally:
        asyncio.run(client.aclose())

    async def malformed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"products": {}})

    channel3, client = provider(malformed)
    try:
        with pytest.raises(Channel3ProviderError, match="invalid_response"):
            asyncio.run(channel3.list_products(intent()))
    finally:
        asyncio.run(client.aclose())


@pytest.mark.parametrize(
    ("status_code", "code"),
    [(429, "rate_limited"), (503, "service_unavailable"), (400, "request_rejected")],
)
def test_channel3_maps_api_failures_to_safe_structured_errors(status_code: int, code: str) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="external response must not be exposed")

    channel3, client = provider(handler)
    try:
        with pytest.raises(Channel3ProviderError) as exc_info:
            asyncio.run(channel3.list_products(intent()))
    finally:
        asyncio.run(client.aclose())

    assert exc_info.value.code == code
    assert "external response" not in str(exc_info.value)


def test_channel3_requires_server_side_api_key() -> None:
    with pytest.raises(Channel3ProviderError, match="missing_api_key"):
        asyncio.run(Channel3Provider(None).list_products(intent()))
