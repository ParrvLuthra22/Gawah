import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from shopper.agent.intent_parser import IntentExtractionError, IntentParser
from shopper.config import Settings
from shopper.integrations.groq_client import GroqServiceError
from shopper.main import app, get_intent_parser
from shopper.models.shopping_intent import ShoppingIntent


class FakeGroqClient:
    def __init__(self, response: str | Exception):
        self.response = response

    async def create_json_completion(self, **_kwargs: object) -> str:
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def parser_for(response: str | Exception) -> IntentParser:
    settings = Settings(groq_api_key="test-key")
    return IntentParser(settings=settings, client=FakeGroqClient(response))  # type: ignore[arg-type]


def test_valid_shopping_request() -> None:
    parser = parser_for(
        '{"category":"running shoes","brand":"Nike","color":"black",'
        '"size":"9","max_budget":80,"currency":"usd","preferences":[]}'
    )
    intent = asyncio.run(parser.parse("Find black Nike running shoes size 9 under $80"))
    assert intent.category == "running shoes"
    assert intent.brand == "Nike"
    assert intent.max_budget == 80
    assert intent.currency == "USD"


def test_nike_black_size_12_request_accepts_numeric_llm_size() -> None:
    parser = parser_for(
        '{"category":"shoes","brand":"Nike","color":"black",'
        '"size":12,"max_budget":60,"currency":"USD","preferences":[]}'
    )
    intent = asyncio.run(
        parser.parse("Find me nike shoes in black colour of size 12 under 60$")
    )
    assert intent.size == "12"
    assert intent.color == "black"
    assert intent.max_budget == 60


def test_mens_us_size_requirement_is_preserved() -> None:
    parser = parser_for(
        '{"category":"running shoes","brand":"Nike","color":"black","size":10,'
        '"audience":"men","size_system":"us","max_budget":50,"currency":"USD","preferences":[]}'
    )
    intent = asyncio.run(parser.parse("Find black Nike running shoes US size 10 under $50 for men"))
    assert intent.audience == "men"
    assert intent.size_system == "US"


def test_request_without_budget() -> None:
    parser = parser_for(
        '{"category":"running shoes","brand":null,"color":null,"size":null,'
        '"max_budget":null,"currency":null,"preferences":[]}'
    )
    assert asyncio.run(parser.parse("Find running shoes")).max_budget is None


def test_multiple_preferences() -> None:
    parser = parser_for(
        '{"category":"jacket","brand":null,"color":null,"size":null,'
        '"max_budget":null,"currency":null,"preferences":["waterproof", "lightweight"]}'
    )
    intent = asyncio.run(parser.parse("Find a waterproof lightweight jacket"))
    assert intent.preferences == ["waterproof", "lightweight"]


def test_negative_budget_is_invalid() -> None:
    with pytest.raises(ValidationError):
        ShoppingIntent(max_budget=-1)


def test_malformed_json_from_groq() -> None:
    with pytest.raises(IntentExtractionError):
        asyncio.run(parser_for("not json").parse("shoes"))


def test_empty_groq_response_is_an_upstream_failure() -> None:
    with pytest.raises(IntentExtractionError):
        asyncio.run(parser_for("").parse("shoes"))


def test_groq_api_failure_is_propagated() -> None:
    with pytest.raises(GroqServiceError):
        asyncio.run(parser_for(GroqServiceError("offline")).parse("shoes"))


def test_fastapi_request_validation() -> None:
    client = TestClient(app)
    assert client.post("/shopping/intent", json={"message": "   "}).status_code == 422
    assert client.post("/shopping/intent", json={"message": "x" * 4_001}).status_code == 422


def test_intent_endpoint_returns_validated_response() -> None:
    app.dependency_overrides[get_intent_parser] = lambda: parser_for(
        '{"category":"running shoes","brand":"Nike","color":"black",'
        '"size":"9","max_budget":80,"currency":"USD","preferences":[]}'
    )
    try:
        response = TestClient(app).post("/shopping/intent", json={"message": "Find Nike shoes"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["max_budget"] == "80"
