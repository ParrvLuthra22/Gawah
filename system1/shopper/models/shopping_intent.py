"""Validated representation of constraints explicitly stated by a shopper."""

from decimal import Decimal
from numbers import Number

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ShoppingIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str | None = None
    brand: str | None = None
    color: str | None = None
    size: str | None = None
    max_budget: Decimal | None = Field(default=None, gt=0)
    currency: str | None = None
    preferences: list[str] = Field(default_factory=list)

    @field_validator("category", "brand", "color", "size", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        # Groq commonly emits shoe sizes as JSON numbers; size is intentionally text
        # so values such as "12W" remain representable as well.
        if isinstance(value, Number) and not isinstance(value, bool):
            return str(value)
        return value

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> object:
        if value is None or not isinstance(value, str):
            return value
        return value.strip().upper() or None

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str | None) -> str | None:
        if value is not None and (len(value) != 3 or not value.isalpha()):
            raise ValueError("currency must be a three-letter ISO currency code")
        return value

    @field_validator("preferences", mode="before")
    @classmethod
    def normalize_preferences(cls, value: object) -> object:
        if value is None:
            return []
        if not isinstance(value, list):
            return value
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
