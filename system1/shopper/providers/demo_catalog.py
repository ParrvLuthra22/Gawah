"""Local, deterministic catalog provider for development and tests."""

import json
from pathlib import Path

from shopper.models.product import Product
from shopper.models.shopping_intent import ShoppingIntent

CATALOG_FILE = Path(__file__).resolve().parents[1] / "data" / "demo_catalog.json"


class DemoCatalogProvider:
    name = "demo"

    async def list_products(self, _intent: ShoppingIntent) -> list[Product]:
        try:
            records = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
            return [Product.model_validate(record) for record in records]
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ProductProviderError("Demo catalog is unavailable") from exc


class PurchasableDemoCatalogProvider(DemoCatalogProvider):
    """Demo-checkout view: only merchant-stocked catalog IDs are selectable."""

    name = "demo-merchant"

    def __init__(self, purchasable_ids: set[str]) -> None:
        self._purchasable_ids = purchasable_ids

    async def list_products(self, intent: ShoppingIntent) -> list[Product]:
        products = await super().list_products(intent)
        return [product for product in products if product.id in self._purchasable_ids]


class ProductProviderError(RuntimeError):
    """A configured product provider could not return catalog data."""
