"""Thin, reusable adapter around the official asynchronous Groq SDK."""

import asyncio
import logging
import time
from typing import Any

from groq import (
    APIConnectionError,
    APITimeoutError,
    AsyncGroq,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)

from shopper.config import Settings

logger = logging.getLogger(__name__)


def _log_bad_request(
    stage: str,
    exc: BadRequestError,
    model: str | None = None,
    turn: int | None = None,
) -> None:
    """Log only safe structured API error fields, never request or response content."""
    body = exc.body if isinstance(exc.body, dict) else {}
    error = body.get("error", body)
    if not isinstance(error, dict):
        error = {}
    logger.warning(
        "Groq request rejected: stage=%s turn=%s model=%s status=%s code=%s type=%s param=%s",
        stage,
        turn,
        model or "none",
        exc.status_code,
        error.get("code"),
        error.get("type"),
        error.get("param"),
    )

# These text-generation models are approved for Groq JSON-object responses. This
# allowlist means audio, safety-classification, and embedding models cannot be selected.
MODEL_PRIORITY = (
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b",
    "llama-3.1-8b-instant",
)
MODEL_LIST_CACHE_TTL_SECONDS = 300


class GroqServiceError(RuntimeError):
    """The upstream AI service could not satisfy a request."""


class GroqAuthenticationError(GroqServiceError):
    """The configured key was not accepted by Groq."""


class GroqRateLimitError(GroqServiceError):
    """All eligible Groq models were rate limited."""


class GroqIntentClient:
    """Reusable Groq client with cached model discovery and bounded fallback."""

    def __init__(self, settings: Settings) -> None:
        self._configured_model = settings.groq_model.strip()
        self._api_key_configured = bool(settings.groq_api_key.strip())
        self._client = AsyncGroq(api_key=settings.groq_api_key, timeout=15.0, max_retries=0)
        self._cached_models: tuple[str, ...] | None = None
        self._models_cached_at = 0.0
        self._model_list_lock = asyncio.Lock()

    @property
    def is_auto_model_selection(self) -> bool:
        return self._configured_model.lower() == "auto"

    async def _available_compatible_models(self) -> tuple[str, ...]:
        """Return enabled, JSON-compatible priority models, using a short TTL cache."""
        now = time.monotonic()
        if (
            self._cached_models is not None
            and now - self._models_cached_at < MODEL_LIST_CACHE_TTL_SECONDS
        ):
            return self._cached_models

        async with self._model_list_lock:
            now = time.monotonic()
            if (
                self._cached_models is not None
                and now - self._models_cached_at < MODEL_LIST_CACHE_TTL_SECONDS
            ):
                return self._cached_models
            try:
                page = await self._client.models.list()
            except AuthenticationError as exc:
                logger.warning("Groq failure: stage=%s exception_type=%s", "model_discovery", type(exc).__name__)
                raise GroqAuthenticationError("Groq authentication failed") from exc
            except BadRequestError as exc:
                _log_bad_request("model_discovery", exc)
                raise GroqServiceError("Groq model discovery rejected") from exc
            except Exception as exc:
                logger.warning("Groq failure: stage=%s exception_type=%s", "model_discovery", type(exc).__name__)
                raise GroqServiceError("Groq model discovery failed") from exc

            available_ids = {
                model_id
                for model in getattr(page, "data", page)
                if (model_id := getattr(model, "id", None))
                and getattr(model, "active", True) is not False
            }
            self._cached_models = tuple(model for model in MODEL_PRIORITY if model in available_ids)
            self._models_cached_at = time.monotonic()
            return self._cached_models

    async def _candidate_models(self) -> tuple[str, ...]:
        if not self._api_key_configured:
            raise GroqAuthenticationError("Groq API key is not configured")
        if not self.is_auto_model_selection:
            # An explicit model is deliberately not replaced by automatic selection.
            return (self._configured_model,)
        models = await self._available_compatible_models()
        if not models:
            logger.warning("Groq failure: stage=%s detail=%s", "model_selection", "no_compatible_model")
            raise GroqServiceError("No JSON-compatible Groq model is available")
        return models

    @staticmethod
    def _fallback_reason(error: Exception) -> str | None:
        """Classify only transient or model-not-found failures as safe to retry."""
        if isinstance(error, RateLimitError):
            return "rate_limited"
        if isinstance(error, (APIConnectionError, APITimeoutError)):
            return "temporary_service_error"
        status_code = getattr(error, "status_code", None)
        if status_code == 404:
            return "model_not_found"
        if status_code in {408, 409, 425, 429} or (
            isinstance(status_code, int) and status_code >= 500
        ):
            return "temporary_service_error"
        return None

    async def create_json_completion(self, *, system_prompt: str, user_message: str) -> str:
        candidates = await self._candidate_models()
        last_fallback_reason: str | None = None

        for index, model in enumerate(candidates):
            logger.info("Using Groq model %s", model)
            try:
                completion = await self._client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0,
                )
            except AuthenticationError as exc:
                # Authentication and invalid-key failures must never be retried.
                logger.warning(
                    "Groq failure: stage=%s exception_type=%s",
                    "intent_completion",
                    type(exc).__name__,
                )
                raise GroqAuthenticationError("Groq authentication failed") from exc
            except BadRequestError as exc:
                _log_bad_request("intent_completion", exc, model)
                raise GroqServiceError("Groq intent request rejected") from exc
            except Exception as exc:
                reason = self._fallback_reason(exc)
                logger.warning(
                    "Groq failure: stage=%s model=%s category=%s exception_type=%s",
                    "intent_completion",
                    model,
                    reason or "non_retryable",
                    type(exc).__name__,
                )
                if (
                    reason is not None
                    and self.is_auto_model_selection
                    and index + 1 < len(candidates)
                ):
                    last_fallback_reason = reason
                    logger.warning(
                        "Groq model %s failed (%s); trying the next model", model, reason
                    )
                    continue
                if reason == "rate_limited":
                    raise GroqRateLimitError("Groq rate limit exceeded") from exc
                raise GroqServiceError("Groq request failed") from exc

            content: Any = completion.choices[0].message.content if completion.choices else None
            if not isinstance(content, str) or not content.strip():
                raise GroqServiceError("Groq returned an empty completion")
            if last_fallback_reason:
                logger.info(
                    "Groq fallback succeeded with model %s after %s", model, last_fallback_reason
                )
            return content

        raise GroqServiceError("No Groq model could complete the request")

    async def create_tool_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        turn: int,
    ) -> Any:
        """Create one tool-calling turn; callers execute tools locally in Python."""
        candidates = await self._candidate_models()
        for index, model in enumerate(candidates):
            logger.info("Using Groq model %s for shopping tools at turn %s", model, turn)
            try:
                completion = await self._client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    parallel_tool_calls=False,
                    temperature=0,
                )
            except AuthenticationError as exc:
                logger.warning("Groq failure: stage=%s exception_type=%s", "tool_completion", type(exc).__name__)
                raise GroqAuthenticationError("Groq authentication failed") from exc
            except BadRequestError as exc:
                _log_bad_request("tool_completion", exc, model, turn)
                raise GroqServiceError("Groq tool request rejected") from exc
            except Exception as exc:
                reason = self._fallback_reason(exc)
                logger.warning(
                    "Groq failure: stage=%s turn=%s model=%s category=%s exception_type=%s",
                    "tool_completion",
                    turn,
                    model,
                    reason or "non_retryable",
                    type(exc).__name__,
                )
                if (
                    reason is not None
                    and self.is_auto_model_selection
                    and index + 1 < len(candidates)
                ):
                    logger.warning(
                        "Groq tool model %s failed (%s); trying the next model", model, reason
                    )
                    continue
                if reason == "rate_limited":
                    raise GroqRateLimitError("Groq rate limit exceeded") from exc
                raise GroqServiceError("Groq tool request failed") from exc

            if not completion.choices:
                raise GroqServiceError("Groq returned an empty tool completion")
            return completion.choices[0].message
        raise GroqServiceError("No Groq model could complete the tool request")
