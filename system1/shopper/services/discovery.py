"""Deterministic filtering over provider-supplied products."""

from typing import Protocol

from shopper.config import Settings
from shopper.models.product import DiscoveryResult, DiscoverySource, Product, RejectedProduct
from shopper.models.shopping_intent import ShoppingIntent
from shopper.providers.channel3 import Channel3Provider
from shopper.providers.demo_catalog import DemoCatalogProvider, ProductProviderError

MAX_CATALOG_PRODUCTS = 50
MAX_ELIGIBLE_PRODUCTS = 20
MAX_REJECTED_PRODUCTS = 50


class ProductProvider(Protocol):
    name: str

    async def list_products(self) -> list[Product]: ...


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _category_matches(product: Product, requested_category: str) -> bool:
    requested = _normalized(requested_category)
    category = _normalized(product.category)
    if requested == category or requested in category:
        return True
    if product.product_type is None:
        return False
    product_type = _normalized(product.product_type)
    # Exact product-type matches preserve qualifiers such as "atta bread".
    # A generic requested type such as "bread" can intentionally match bread types.
    return requested == product_type or product_type.endswith(f" {requested}")


def rejection_reasons(product: Product, intent: ShoppingIntent) -> list[str]:
    """Return deterministic reasons without inspecting untrusted descriptions."""
    reasons: list[str] = []
    if intent.currency and product.currency != intent.currency:
        reasons.append("currency_mismatch")
    if intent.max_budget is not None and product.price > intent.max_budget:
        reasons.append("over_budget")
    if intent.brand and _normalized(product.brand) != _normalized(intent.brand):
        reasons.append("brand_mismatch")
    if intent.category:
        if not _category_matches(product, intent.category):
            reasons.append("category_mismatch")
    for field, expected_value in (("color", intent.color), ("size", intent.size)):
        if expected_value is None:
            continue
        actual_value = product.attributes.get(field)
        if actual_value is None:
            reasons.append(f"missing_{field}")
        elif _normalized(actual_value) != _normalized(expected_value):
            reasons.append(f"{field}_mismatch")
    return reasons


class DiscoveryService:
    def __init__(self, provider: ProductProvider) -> None:
        self._provider = provider

    async def discover(self, intent: ShoppingIntent) -> DiscoveryResult:
        try:
            products = (await self._provider.list_products())[:MAX_CATALOG_PRODUCTS]
        except ProductProviderError:
            raise
        except Exception as exc:
            raise ProductProviderError("Product provider request failed") from exc

        eligible: list[Product] = []
        rejected: list[RejectedProduct] = []
        for product in products:
            reasons = rejection_reasons(product, intent)
            if reasons:
                if len(rejected) < MAX_REJECTED_PRODUCTS:
                    rejected.append(RejectedProduct(product=product, reasons=reasons))
            elif len(eligible) < MAX_ELIGIBLE_PRODUCTS:
                eligible.append(product)
        return DiscoveryResult(
            eligible=eligible,
            rejected=rejected,
            source=DiscoverySource(
                provider=self._provider.name,
                returned_count=len(products),
                eligible_count=len(eligible),
                rejected_count=len(rejected),
            ),
        )


def discovery_service_for(settings: Settings) -> DiscoveryService:
    provider: ProductProvider
    if settings.product_provider == "demo":
        provider = DemoCatalogProvider()
    else:
        provider = Channel3Provider()
    return DiscoveryService(provider)
