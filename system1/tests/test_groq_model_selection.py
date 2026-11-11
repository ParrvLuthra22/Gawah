import asyncio
from types import SimpleNamespace

import pytest
from shopper.config import Settings
from shopper.integrations import groq_client
from shopper.integrations.groq_client import (
    MODEL_PRIORITY,
    GroqAuthenticationError,
    GroqIntentClient,
    GroqServiceError,
)


class FakeStatusError(Exception):
    def __init__(self, status_code: int):
        self.status_code = status_code


class FakeModels:
    def __init__(self, model_ids: list[str]):
        self.model_ids = model_ids
        self.calls = 0

    async def list(self) -> SimpleNamespace:
        self.calls += 1
        return SimpleNamespace(
            data=[SimpleNamespace(id=model_id, active=True) for model_id in self.model_ids]
        )


class FakeCompletions:
    def __init__(self, outcomes: list[object]):
        self.outcomes = outcomes
        self.calls: list[str] = []
        self.response_formats: list[object] = []
        self.request_kwargs: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(str(kwargs["model"]))
        self.request_kwargs.append(kwargs)
        if "response_format" in kwargs:
            self.response_formats.append(kwargs["response_format"])
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))])


def client_with_backend(
    model_ids: list[str], outcomes: list[object], model: str = "auto"
) -> tuple[GroqIntentClient, FakeModels, FakeCompletions]:
    client = GroqIntentClient(Settings(groq_api_key="test-key", groq_model=model))
    models = FakeModels(model_ids)
    completions = FakeCompletions(outcomes)
    client._client = SimpleNamespace(models=models, chat=SimpleNamespace(completions=completions))
    return client, models, completions


def complete(client: GroqIntentClient) -> str:
    return asyncio.run(
        client.create_json_completion(system_prompt="extract", user_message="request")
    )


def complete_with_tools(client: GroqIntentClient) -> object:
    return asyncio.run(
        client.create_tool_completion(
            messages=[{"role": "user", "content": "Choose a product."}],
            tools=[],
            turn=1,
        )
    )


def test_auto_selection_uses_highest_priority_json_compatible_model_and_caches_list() -> None:
    client, models, completions = client_with_backend(
        ["audio-model", MODEL_PRIORITY[2], MODEL_PRIORITY[0], "text-embedding-3-small"],
        ["{}", "{}"],
    )

    assert complete(client) == "{}"
    assert complete(client) == "{}"
    assert completions.calls == [MODEL_PRIORITY[0], MODEL_PRIORITY[0]]
    assert completions.response_formats == [{"type": "json_object"}] * 2
    assert models.calls == 1


def test_auto_selection_falls_back_after_model_not_found() -> None:
    client, _models, completions = client_with_backend(
        list(MODEL_PRIORITY[:2]),
        [FakeStatusError(404), "{}"],
    )

    assert complete(client) == "{}"
    assert completions.calls == [MODEL_PRIORITY[0], MODEL_PRIORITY[1]]


def test_auto_selection_falls_back_after_rate_limit() -> None:
    client, _models, completions = client_with_backend(
        list(MODEL_PRIORITY[:2]),
        [FakeStatusError(429), "{}"],
    )

    assert complete(client) == "{}"
    assert completions.calls == [MODEL_PRIORITY[0], MODEL_PRIORITY[1]]


def test_exhausted_compatible_models_raise_service_error() -> None:
    client, _models, completions = client_with_backend(
        list(MODEL_PRIORITY),
        [FakeStatusError(503) for _ in MODEL_PRIORITY],
    )

    with pytest.raises(GroqServiceError):
        complete(client)
    assert completions.calls == list(MODEL_PRIORITY)


def test_authentication_failure_does_not_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAuthenticationError(Exception):
        pass

    monkeypatch.setattr(groq_client, "AuthenticationError", FakeAuthenticationError)
    client, _models, completions = client_with_backend(
        list(MODEL_PRIORITY[:2]),
        [FakeAuthenticationError()],
    )

    with pytest.raises(GroqAuthenticationError):
        complete(client)
    assert completions.calls == [MODEL_PRIORITY[0]]


def test_explicit_model_bypasses_automatic_discovery() -> None:
    client, models, completions = client_with_backend([], ["{}"], model="custom/model")

    assert complete(client) == "{}"
    assert completions.calls == ["custom/model"]
    assert models.calls == 0


def test_tool_completion_disables_parallel_calls_for_gpt_oss() -> None:
    client, _models, completions = client_with_backend(
        [MODEL_PRIORITY[0]],
        [SimpleNamespace(content="{}", tool_calls=None)],
    )

    complete_with_tools(client)

    assert completions.calls == [MODEL_PRIORITY[0]]
    assert completions.request_kwargs[0]["parallel_tool_calls"] is False


def test_missing_groq_key_allows_settings_but_rejects_ai_requests() -> None:
    client = GroqIntentClient(Settings(groq_api_key=""))

    with pytest.raises(GroqAuthenticationError, match="not configured"):
        complete(client)
