"""Deterministic filtering over provider-supplied products."""

import logging
from typing import Protocol

from shopper.config import Settings
from shopper.models.product import (
    AttributeVerification,
    DiscoveryResult,
    DiscoverySource,
    NeedsVerificationProduct,
    Product,
    ProductVerification,
    RejectedProduct,
)
from shopper.models.shopping_intent import ShoppingIntent
from shopper.providers.channel3 import Channel3Provider
from shopper.providers.demo_catalog import DemoCatalogProvider, ProductProviderError

MAX_CATALOG_PRODUCTS = 50
MAX_ELIGIBLE_PRODUCTS = 20
MAX_REJECTED_PRODUCTS = 50
logger = logging.getLogger(__name__)


class ProductProvider(Protocol):
    name: str

    async def list_products(self, intent: ShoppingIntent) -> list[Product]: ...


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


def _footwear_audience(value: str) -> str | None:
    normalized = _normalized(value)
    if any(token in normalized for token in ("kid", "child", "boy", "girl", "youth")):
        return "kids"
    if any(token in normalized for token in ("women", "woman", "female", "ladies")):
        return "women"
    if any(token in normalized for token in ("men", "man", "male")):
        return "men"
    return None


def footwear_audience(product: Product) -> str | None:
    """Use structured taxonomy/attributes only; never product descriptions."""
    explicit = _normalized(product.attributes.get("audience", ""))
    audience_aliases = {
        "men": "men", "women": "women", "unisex": "unisex", "kids": "kids",
        "children": "kids", "boys": "kids", "girls": "kids", "toddler": "toddler", "grade school": "kids",
    }
    if explicit in audience_aliases:
        return audience_aliases[explicit]
    value = product.product_type or product.category
    audience = _footwear_audience(value)
    normalized = _normalized(value)
    if any(token in normalized for token in ("toddler", "(td)", " td ")):
        return "toddler"
    if audience == "kids" or any(token in normalized for token in ("grade school", "boys", "girls")):
        return "kids"
    return audience


def _is_footwear(value: str) -> bool:
    normalized = _normalized(value)
    return any(token in normalized for token in ("shoe", "sneaker", "trainer", "footwear"))


def _category_verification_reason(product: Product, intent: ShoppingIntent) -> str | None:
    if intent.category is None:
        return None
    product_category = product.product_type or product.category
    requested_audience = intent.audience or _footwear_audience(intent.category)
    product_audience = footwear_audience(product)
    if product_audience in {"kids", "toddler"} and requested_audience is None:
        return "category_mismatch"
    if _category_matches(product, intent.category):
        return None
    if not (_is_footwear(intent.category) and _is_footwear(product_category)):
        return "category_mismatch"
    if requested_audience and product_audience and requested_audience != product_audience:
        return "category_mismatch"
    return "category_needs_verification"


def rejection_reasons(product: Product, intent: ShoppingIntent) -> list[str]:
    """Return deterministic reasons without inspecting untrusted descriptions."""
    reasons: list[str] = []
    if intent.currency and product.currency != intent.currency:
        reasons.append("currency_mismatch")
    if intent.max_budget is not None and product.price > intent.max_budget:
        reasons.append("over_budget")
    if intent.brand and _normalized(product.brand) != _normalized(intent.brand):
        reasons.append("brand_mismatch")
    if intent.audience:
        audience = footwear_audience(product)
        if audience not in {None, "unisex", intent.audience}:
            reasons.append("audience_mismatch")
    category_reason = _category_verification_reason(product, intent)
    if category_reason == "category_mismatch":
        reasons.append(category_reason)
    for field, expected_value in (("color", intent.color), ("size", intent.size)):
        if expected_value is None:
            continue
        actual_value = product.attributes.get(field)
        if actual_value is not None and _normalized(actual_value) != _normalized(expected_value):
            reasons.append(f"{field}_mismatch")
    if (
        intent.size_system
        and product.attributes.get("size_system") is not None
        and _normalized(product.attributes["size_system"]) != _normalized(intent.size_system)
    ):
        reasons.append("size_system_mismatch")
    return reasons


def verification_reasons(product: Product, intent: ShoppingIntent) -> list[str]:
    """Required constraints lacking explicit provider verification, never inferred from text."""
    reasons: list[str] = []
    category_reason = _category_verification_reason(product, intent)
    if category_reason == "category_needs_verification":
        reasons.append(category_reason)
    for field, expected_value in (("color", intent.color), ("size", intent.size)):
        if expected_value is not None and product.attributes.get(field) is None:
            reasons.append(f"missing_{field}")
    if intent.audience and footwear_audience(product) is None:
        reasons.append("missing_audience")
    if intent.size_system:
        actual_system = product.attributes.get("size_system")
        if actual_system is None:
            reasons.append("missing_size_system")
    if intent.size is not None and intent.size_system is None and _is_footwear(intent.category or ""):
        audience = footwear_audience(product)
        if audience in {"men", "women", "unisex"} and "size_system" not in product.attributes:
            reasons.append("ambiguous_size_system")
    return reasons


class DiscoveryService:
    def __init__(self, provider: ProductProvider) -> None:
        self._provider = provider

    @property
    def provider_name(self) -> str:
        return self._provider.name

    async def discover(self, intent: ShoppingIntent) -> DiscoveryResult:
        try:
            products = (await self._provider.list_products(intent))[:MAX_CATALOG_PRODUCTS]
        except ProductProviderError:
            raise
        except Exception as exc:
            raise ProductProviderError("Product provider request failed") from exc

        remaining = products
        stage_counts = {"normalized": len(remaining)}
        for stage, blocked_reasons in (
            ("currency", {"currency_mismatch"}),
            ("budget", {"over_budget"}),
            ("brand", {"brand_mismatch"}),
            ("category", {"category_mismatch", "category_needs_verification"}),
            ("color", {"missing_color", "color_mismatch"}),
            ("size", {"missing_size", "size_mismatch"}),
        ):
            if stage in {"color", "size"} and getattr(intent, stage) is None:
                continue
            if stage == "currency" and intent.currency is None:
                continue
            if stage == "budget" and intent.max_budget is None:
                continue
            if stage in {"brand", "category"} and getattr(intent, stage) is None:
                continue
            remaining = [
                product
                for product in remaining
                if not (
                    (set(rejection_reasons(product, intent)) | set(verification_reasons(product, intent)))
                    & blocked_reasons
                )
            ]
            stage_counts[f"after_{stage}"] = len(remaining)
        logger.info(
            "Product discovery provider=%s counts=%s",
            self._provider.name,
            stage_counts,
        )

        eligible: list[Product] = []
        needs_verification: list[NeedsVerificationProduct] = []
        rejected: list[RejectedProduct] = []
        verifications: list[ProductVerification] = []
        for product in products:
            reasons = rejection_reasons(product, intent)
            verification = verification_for(product, intent, reasons)
            verifications.append(verification)
            if reasons:
                if len(rejected) < MAX_REJECTED_PRODUCTS:
                    rejected.append(RejectedProduct(product=product, reasons=reasons))
                continue
            missing = verification_reasons(product, intent)
            if missing:
                if len(needs_verification) < MAX_REJECTED_PRODUCTS:
                    needs_verification.append(
                        NeedsVerificationProduct(product=product, reasons=missing)
                    )
            elif len(eligible) < MAX_ELIGIBLE_PRODUCTS:
                eligible.append(product)
        return DiscoveryResult(
            eligible=eligible,
            needs_verification=needs_verification,
            rejected=rejected,
            source=DiscoverySource(
                provider=self._provider.name,
                returned_count=len(products),
                eligible_count=len(eligible),
                needs_verification_count=len(needs_verification),
                rejected_count=len(rejected),
            ),
            verifications=verifications,
        )


def verification_for(
    product: Product, intent: ShoppingIntent, rejected_reasons: list[str] | None = None
) -> ProductVerification:
    """Reusable suitability verifier based only on structured provider evidence.

    Descriptions, URLs, and LLM output are intentionally excluded.  A verified
    product is still not purchase-ready: that requires a merchant-signed cart.
    """
    rejected_reasons = rejected_reasons if rejected_reasons is not None else rejection_reasons(product, intent)
    missing = verification_reasons(product, intent)
    attributes: dict[str, AttributeVerification] = {}
    requested = {
        "brand": intent.brand,
        "category": intent.category,
        "audience": intent.audience,
        "color": intent.color,
        "size": intent.size,
        "size_system": intent.size_system,
        "currency": intent.currency,
        "price": str(intent.max_budget) if intent.max_budget is not None else None,
    }
    for field, expected in requested.items():
        if expected is None:
            continue
        rejection_fields = {
            "brand": {"brand_mismatch"},
            "category": {"category_mismatch"},
            "audience": {"audience_mismatch"},
            "color": {"color_mismatch"},
            "size": {"size_mismatch"},
            "size_system": {"size_system_mismatch"},
            "currency": {"currency_mismatch"},
            "price": {"over_budget"},
        }
        mismatch = next(
            (reason for reason in rejected_reasons if reason in rejection_fields[field]), None
        )
        missing_reason = next((reason for reason in missing if field in reason), None)
        if mismatch:
            attributes[field] = AttributeVerification(
                status="rejected", source="provider_structured", reason=mismatch
            )
        elif missing_reason:
            attributes[field] = AttributeVerification(
                status="needs_verification", source="provider_structured", reason=missing_reason
            )
        else:
            attributes[field] = AttributeVerification(status="verified", source="provider_structured")
    outcome = "rejected" if rejected_reasons else "needs_verification" if missing else "verified"
    return ProductVerification(product_id=product.id, outcome=outcome, attributes=attributes)


class ProductVerifier:
    """Public verifier facade for discovery, agent orchestration, and API consumers."""

    def verify(self, product: Product, intent: ShoppingIntent) -> ProductVerification:
        return verification_for(product, intent)


def discovery_service_for(settings: Settings) -> DiscoveryService:
    provider: ProductProvider
    if settings.product_provider == "demo":
        provider = DemoCatalogProvider()
    else:
        provider = Channel3Provider(settings.channel3_api_key)
    return DiscoveryService(provider)
