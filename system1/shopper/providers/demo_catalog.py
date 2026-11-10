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


class ProductProviderError(RuntimeError):
    """A configured product provider could not return catalog data."""
