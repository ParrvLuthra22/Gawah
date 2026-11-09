"""Local product-discovery models; these are not shared purchase contracts."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Product(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    brand: str
    category: str
    product_type: str | None = None
    merchant_id: str
    price: Decimal = Field(gt=0)
    currency: str
    attributes: dict[str, str] = Field(default_factory=dict)
    description: str
    url: str | None = None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 3 or not value.isalpha():
            raise ValueError("currency must be a three-letter ISO currency code")
        return value


class RejectedProduct(BaseModel):
    product: Product
    reasons: list[str] = Field(min_length=1)


class DiscoverySource(BaseModel):
    provider: str
    returned_count: int
    eligible_count: int
    rejected_count: int


class DiscoveryResult(BaseModel):
    eligible: list[Product]
    rejected: list[RejectedProduct]
    source: DiscoverySource
