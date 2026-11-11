"""LLM-backed parser that turns a request into a validated ShoppingIntent."""

import json
import logging

from pydantic import ValidationError

from shopper.agent.prompts import SYSTEM_PROMPT
from shopper.config import Settings
from shopper.integrations.groq_client import GroqIntentClient
from shopper.models.shopping_intent import ShoppingIntent

logger = logging.getLogger(__name__)


class IntentExtractionError(ValueError):
    """The model output was not valid shopping-intent JSON."""


class IntentParser:
    def __init__(self, settings: Settings, client: GroqIntentClient | None = None) -> None:
        self._client = client or GroqIntentClient(settings)

    async def parse(self, message: str) -> ShoppingIntent:
        try:
            content = await self._client.create_json_completion(
                system_prompt=SYSTEM_PROMPT,
                user_message=message,
            )
            payload = json.loads(content)
            if not isinstance(payload, dict):
                logger.warning(
                    "Shopping intent response rejected: type=%s field=%s reason=%s",
                    type(payload).__name__,
                    "root",
                    "expected JSON object",
                )
                raise IntentExtractionError("expected a JSON object")
            return ShoppingIntent.model_validate(payload)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Shopping intent JSON parsing failed: type=%s field=%s reason=%s",
                type(exc).__name__,
                "root",
                exc.msg,
            )
            raise IntentExtractionError("invalid shopping intent response") from exc
        except ValidationError as exc:
            for error in exc.errors():
                field = ".".join(str(part) for part in error["loc"]) or "root"
                logger.warning(
                    "Shopping intent validation failed: type=%s field=%s reason=%s",
                    error["type"],
                    field,
                    error["msg"],
                )
            raise IntentExtractionError("invalid shopping intent response") from exc
        except TypeError as exc:
            logger.warning(
                "Shopping intent response rejected: type=%s field=%s reason=%s",
                type(exc).__name__,
                "root",
                "unexpected response type",
            )
            raise IntentExtractionError("invalid shopping intent response") from exc
