"""Explicit Channel3 integration boundary pending verified API documentation."""

from shopper.models.product import Product
from shopper.providers.demo_catalog import ProductProviderError


class Channel3Provider:
    name = "channel3"

    async def list_products(self) -> list[Product]:
        # No endpoint or response schema is implemented until Channel3 documentation
        # is verified. This intentionally never substitutes local demo data.
        raise ProductProviderError(
            "Channel3 discovery requires verified endpoint and schema documentation"
        )
