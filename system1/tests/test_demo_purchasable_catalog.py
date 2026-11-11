import asyncio
from decimal import Decimal

from shopper.models.shopping_intent import ShoppingIntent
from shopper.providers.demo_catalog import PurchasableDemoCatalogProvider
from shopper.services.discovery import DiscoveryService


def test_checkout_catalog_excludes_unstocked_and_unmerchanted_shoes() -> None:
    provider = PurchasableDemoCatalogProvider({"shoe-001", "grocery-001"})
    intent = ShoppingIntent(
        category="running shoes", brand="Nike", color="black", size="12",
        max_budget=Decimal(80), currency="USD",
    )

    result = asyncio.run(DiscoveryService(provider).discover(intent))

    assert [product.id for product in result.eligible] == ["shoe-001"]
    assert "shoe-003" not in [product.id for product in result.eligible]
