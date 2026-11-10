"""Channel3 product search adapter using its documented server-side API."""

import asyncio
import logging
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from shopper.models.product import Product
from shopper.models.shopping_intent import ShoppingIntent
from shopper.providers.demo_catalog import ProductProviderError

CHANNEL3_SEARCH_URL = "https://api.trychannel3.com/v1/search"
CHANNEL3_PRODUCT_URL = "https://api.trychannel3.com/v1/products"
CHANNEL3_TIMEOUT_SECONDS = 8.0
CHANNEL3_RESULT_LIMIT = 30  # Documented API maximum.
CHANNEL3_DETAIL_LIMIT = 10
logger = logging.getLogger(__name__)


class Channel3ProviderError(ProductProviderError):
    """Safe, structured failure; never includes external response bodies or credentials."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Channel3 discovery failed: {code}")


class Channel3Provider:
    name = "channel3"

    def __init__(
        self,
        api_key: str | None,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = CHANNEL3_TIMEOUT_SECONDS,
    ) -> None:
        self._api_key = api_key
        self._client = client
        self._timeout_seconds = timeout_seconds

    async def list_products(self, intent: ShoppingIntent) -> list[Product]:
        if not self._api_key:
            raise Channel3ProviderError("missing_api_key")

        if self._client is not None:
            body = await self._search(self._client, intent)
            products = self._map_response(await self._resolve_variants(self._client, body, intent))
            logger.info("Channel3 discovery stage=normalized products=%d", len(products))
            return products
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            body = await self._search(client, intent)
            products = self._map_response(await self._resolve_variants(client, body, intent))
            logger.info("Channel3 discovery stage=normalized products=%d", len(products))
            return products

    async def _search(self, client: httpx.AsyncClient, intent: ShoppingIntent) -> object:
        body = self._search_body(intent)
        logger.info(
            "Channel3 discovery stage=search query_terms=%d has_budget=%s currency_set=%s",
            len(str(body["query"]).split()),
            intent.max_budget is not None,
            intent.currency is not None,
        )
        try:
            response = await client.post(
                CHANNEL3_SEARCH_URL,
                headers={"x-api-key": self._api_key},
                json=body,
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise Channel3ProviderError("timeout") from exc
        except httpx.HTTPError as exc:
            raise Channel3ProviderError("network_error") from exc

        response_body = self._response_body(response)
        if not isinstance(response_body, dict) or not isinstance(response_body.get("products"), list):
            raise Channel3ProviderError("invalid_response")
        logger.info("Channel3 discovery stage=search raw_products=%d", len(response_body["products"]))
        return response_body

    async def _resolve_variants(
        self, client: httpx.AsyncClient, body: object, intent: ShoppingIntent
    ) -> object:
        if not isinstance(body, dict) or not isinstance(body.get("products"), list):
            raise Channel3ProviderError("invalid_response")
        records = body["products"]
        resolution_requests = []
        needs_resolution = 0
        for product in records[:CHANNEL3_DETAIL_LIMIT]:
            if not isinstance(product, dict) or not self._needs_variant_resolution(product, intent):
                continue
            needs_resolution += 1
            # Search records frequently omit variant options while the documented
            # canonical product detail response includes them.  Fetch the bounded
            # detail record first; do not mistake absent search options for absence
            # of selectable variants.
            resolution_requests.append(self._resolve_product(client, product, intent))
        logger.info(
            "Channel3 discovery stage=detail_resolution candidates=%d requested=%d",
            needs_resolution,
            len(resolution_requests),
        )
        if not resolution_requests:
            return body
        try:
            resolved = await asyncio.gather(*resolution_requests)
        except Channel3ProviderError as exc:
            logger.warning(
                "Channel3 discovery stage=detail_resolution requested=%d succeeded=0 failed_at_least=1 code=%s",
                len(resolution_requests),
                exc.code,
            )
            raise
        logger.info(
            "Channel3 discovery stage=detail_resolution requested=%d succeeded=%d failed=0",
            len(resolution_requests),
            len(resolved),
        )
        replacements = {original_id: resolved_product for original_id, resolved_product in resolved}
        return {
            **body,
            "products": [
                replacements.get(self._text(product.get("id")), product)
                if isinstance(product, dict)
                else product
                for product in records
            ],
        }

    async def _resolve_product(
        self, client: httpx.AsyncClient, product: dict[str, Any], intent: ShoppingIntent
    ) -> tuple[str, dict[str, Any]]:
        product_id = self._text(product.get("id"))
        if product_id is None:
            return "", product
        params = self._variant_parameters(product, intent)
        try:
            response = await client.get(
                f"{CHANNEL3_PRODUCT_URL}/{quote(product_id, safe='')}",
                headers={"x-api-key": self._api_key},
                params=params,
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise Channel3ProviderError("timeout") from exc
        except httpx.HTTPError as exc:
            raise Channel3ProviderError("network_error") from exc
        detailed = self._response_body(response)
        if not isinstance(detailed, dict):
            return product_id, product
        detail_params = self._variant_parameters(detailed, intent)
        if not params and detail_params:
            try:
                response = await client.get(
                    f"{CHANNEL3_PRODUCT_URL}/{quote(product_id, safe='')}",
                    headers={"x-api-key": self._api_key},
                    params=detail_params,
                    timeout=self._timeout_seconds,
                )
            except httpx.TimeoutException as exc:
                raise Channel3ProviderError("timeout") from exc
            except httpx.HTTPError as exc:
                raise Channel3ProviderError("network_error") from exc
            selected = self._response_body(response)
            if isinstance(selected, dict):
                detailed = selected
        return product_id, {
            **detailed,
            "_gawah_verified_attributes": self._verified_variant_attributes(
                detailed, params or detail_params
            ),
        }

    @staticmethod
    def _response_body(response: httpx.Response) -> object:
        if response.status_code == 429:
            raise Channel3ProviderError("rate_limited")
        if response.status_code >= 500:
            raise Channel3ProviderError("service_unavailable")
        if response.status_code >= 400:
            raise Channel3ProviderError("request_rejected")
        try:
            return response.json()
        except ValueError as exc:
            raise Channel3ProviderError("invalid_response") from exc

    @staticmethod
    def _needs_variant_resolution(product: dict[str, Any], intent: ShoppingIntent) -> bool:
        attributes = Channel3Provider._attributes(product)
        return (
            (intent.color is not None and "color" not in attributes)
            or (intent.size is not None and "size" not in attributes)
        )

    @staticmethod
    def _variant_parameters(product: dict[str, Any], intent: ShoppingIntent) -> dict[str, str]:
        variants = product.get("variants")
        options = variants.get("options") if isinstance(variants, dict) else None
        if not isinstance(options, list):
            return {}
        requested = {"color": intent.color, "size": intent.size}
        params: dict[str, str] = {}
        for option in options:
            if not isinstance(option, dict):
                continue
            name = Channel3Provider._text(option.get("name"))
            field = Channel3Provider._attribute_field(name) if name else None
            wanted = requested.get(field) if field else None
            values = option.get("values")
            if not name or not wanted or not isinstance(values, list):
                continue
            for value in values:
                if not isinstance(value, dict):
                    continue
                label = Channel3Provider._text(value.get("label"))
                exists = value.get("exists")
                if label and exists is not False and Channel3Provider._same_text(label, wanted):
                    params[f"option_{name}"] = label
                    break
        return params

    @staticmethod
    def _verified_variant_attributes(
        product: dict[str, Any], params: dict[str, str]
    ) -> dict[str, str]:
        """Accept only the requested detail variant when Channel3 confirms it purchasable."""
        variants = product.get("variants")
        if not isinstance(variants, dict):
            return {}
        selected = variants.get("selected")
        selected_values: dict[str, str] = {}
        if isinstance(selected, list):
            for item in selected:
                if not isinstance(item, dict):
                    continue
                name = Channel3Provider._text(item.get("name"))
                label = Channel3Provider._text(item.get("label"))
                if name and label:
                    selected_values[name] = label
        options = variants.get("options")
        verified: dict[str, str] = {}
        for name, expected in params.items():
            option_name = name.removeprefix("option_")
            field = Channel3Provider._attribute_field(option_name)
            if field is None:
                continue
            if Channel3Provider._same_text(selected_values.get(option_name, ""), expected):
                verified[field] = expected
                continue
            if not isinstance(options, list):
                continue
            for option in options:
                if not isinstance(option, dict) or not Channel3Provider._same_text(
                    Channel3Provider._text(option.get("name")) or "", option_name
                ):
                    continue
                values = option.get("values")
                if not isinstance(values, list):
                    continue
                for value in values:
                    if not isinstance(value, dict):
                        continue
                    label = Channel3Provider._text(value.get("label"))
                    if (
                        label
                        and Channel3Provider._same_text(label, expected)
                        and value.get("exists") is not False
                        and value.get("available") == "InStock"
                    ):
                        verified[field] = label
                        break
        return verified

    @staticmethod
    def _search_body(intent: ShoppingIntent) -> dict[str, object]:
        query_parts = [
            intent.brand,
            intent.category,
            intent.color,
            intent.audience,
            intent.size_system,
            intent.size,
            *intent.preferences,
        ]
        body: dict[str, object] = {
            "query": " ".join(part for part in query_parts if part) or "products",
            "limit": CHANNEL3_RESULT_LIMIT,
        }
        if intent.max_budget is not None:
            body["filters"] = {"price": {"max_price": float(intent.max_budget)}}
        if intent.currency:
            body["config"] = {"currency": intent.currency}
        return body

    @staticmethod
    def _map_response(body: object) -> list[Product]:
        if not isinstance(body, dict) or not isinstance(body.get("products"), list):
            raise Channel3ProviderError("invalid_response")

        products: list[Product] = []
        for product_data in body["products"]:
            if isinstance(product_data, dict):
                products.extend(Channel3Provider._map_product(product_data))
        return products

    @staticmethod
    def _map_product(product_data: dict[str, Any]) -> list[Product]:
        product_id = Channel3Provider._text(product_data.get("id"))
        title = Channel3Provider._text(product_data.get("title"))
        if not product_id or not title:
            return []

        brands = product_data.get("brands")
        category = product_data.get("category")
        brand = ""
        if isinstance(brands, list) and brands and isinstance(brands[0], dict):
            brand = Channel3Provider._text(brands[0].get("name")) or ""
        category_name = ""
        if isinstance(category, dict):
            category_name = (
                Channel3Provider._text(category.get("title"))
                or Channel3Provider._text(category.get("slug"))
                or ""
            )
        normalized_category, product_type = Channel3Provider._category_fields(category_name)

        attributes = Channel3Provider._attributes(product_data)
        gender = Channel3Provider._text(product_data.get("gender"))
        if gender:
            attributes["gender"] = gender
        description = Channel3Provider._text(product_data.get("description")) or ""
        offers = product_data.get("offers")
        if not isinstance(offers, list):
            return []

        mapped: list[Product] = []
        for index, offer in enumerate(offers):
            if not isinstance(offer, dict):
                continue
            domain = Channel3Provider._text(offer.get("domain"))
            url = Channel3Provider._url(offer.get("url"))
            price_data = offer.get("price")
            if not domain or not url or not isinstance(price_data, dict):
                continue
            try:
                price = Decimal(str(price_data.get("price")))
            except (InvalidOperation, ValueError):
                continue
            currency = Channel3Provider._text(price_data.get("currency"))
            if not currency:
                continue
            try:
                mapped.append(
                    Product(
                        id=f"{product_id}:{domain}:{index}",
                        title=title,
                        brand=brand,
                        category=normalized_category,
                        product_type=product_type,
                        merchant_id=domain,
                        price=price,
                        currency=currency,
                        attributes=attributes,
                        description=description,
                        url=url,
                    )
                )
            except ValueError:
                continue
        return mapped

    @staticmethod
    def _attributes(product_data: dict[str, Any]) -> dict[str, str]:
        attributes: dict[str, str] = {}
        verified = product_data.get("_gawah_verified_attributes")
        if isinstance(verified, dict):
            for key, value in verified.items():
                if isinstance(key, str) and isinstance(value, str):
                    attributes[key] = value
        structured = product_data.get("structured_attributes")
        if isinstance(structured, dict):
            for key, values in structured.items():
                if not isinstance(key, str) or not isinstance(values, list):
                    continue
                normalized_values = {
                    value
                    for item in values
                    if (value := Channel3Provider._text(item)) is not None
                }
                value = next(iter(normalized_values), None) if len(normalized_values) == 1 else None
                field = Channel3Provider._attribute_field(key)
                if value and field:
                    attributes[field] = value
        variants = product_data.get("variants")
        if isinstance(variants, dict) and isinstance(variants.get("selected"), list):
            for selected in variants["selected"]:
                if not isinstance(selected, dict):
                    continue
                name = Channel3Provider._text(selected.get("name"))
                label = Channel3Provider._text(selected.get("label"))
                field = Channel3Provider._attribute_field(name) if name else None
                if field and label:
                    attributes[field] = label
                if name and (size_system := Channel3Provider._size_system_from_name(name)):
                    attributes["size_system"] = size_system
        return attributes

    @staticmethod
    def _category_fields(category: str) -> tuple[str, str | None]:
        """Map documented taxonomy labels without deriving type from free-form text."""
        normalized = " ".join(category.casefold().replace("-", " ").split())
        if not any(token in normalized for token in ("shoe", "sneaker", "trainer", "footwear")):
            return category, category or None
        if any(token in normalized for token in ("toddler", "(td)", "grade school", "kid", "child", "boy", "girl", "youth")):
            return "kids shoes", category
        if any(token in normalized for token in ("women", "woman", "female", "ladies")):
            return "women's shoes", category
        if any(token in normalized for token in ("men", "man", "male")):
            return "men's shoes", category
        return "shoes", category

    @staticmethod
    def _text(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value[:2_000] or None

    @staticmethod
    def _same_text(left: str, right: str) -> bool:
        return " ".join(left.casefold().split()) == " ".join(right.casefold().split())

    @staticmethod
    def _attribute_field(name: str) -> str | None:
        normalized = " ".join(name.casefold().replace("-", " ").replace("_", " ").split())
        if normalized in {"color", "colour"}:
            return "color"
        if normalized in {"size", "shoe size", "us size", "uk size", "eu size"}:
            return "size"
        if normalized == "gender":
            return "gender"
        if normalized in {"audience", "age group", "age_group"}:
            return "audience"
        if normalized in {"size system", "sizing system"}:
            return "size_system"
        return None

    @staticmethod
    def _size_system_from_name(name: str) -> str | None:
        normalized = " ".join(name.casefold().replace("-", " ").split())
        for system in ("us", "uk", "eu"):
            if normalized.startswith(f"{system} size"):
                return system.upper()
        return None

    @staticmethod
    def _url(value: object) -> str | None:
        url = Channel3Provider._text(value)
        if url is None:
            return None
        parsed = urlparse(url)
        return url if parsed.scheme in {"http", "https"} and parsed.netloc else None
