"""Shared definitions for the end-to-end proof (e2e.py) and the semantic cache warmer.

Everything here is data about SIMULATED attackers and a SIMULATED LLM. Titles, skus and prices are
read from System 1's own files (demo catalogs and attack fixtures), never retyped, so a change
there shows up here. Nothing in this module is secret.
"""

import json
from dataclasses import dataclass
from decimal import Decimal

from shopper.merchant.demo_merchant import MERCHANT_CATALOG_FILE, MerchantProduct
from shopper.providers.demo_catalog import CATALOG_FILE
from shopper.security.attack_simulation import fixtures as attack_fixtures

MERCHANT_ID = "demo-sports"
CATEGORY = "footwear"
DESCRIPTION = "Black Nike running shoes, size 12, under $80"
BUDGET_MINOR = 8000
CURRENCY = "USD"
SHOPPER_MESSAGE = "Find black Nike running shoes size 12 under $80"
HAPPY_PRODUCT_ID = "shoe-001"
WRONG_VARIANT_PRODUCT_ID = "shoe-002"  # the merchant's size 11 (stock 0 in the shopper's catalog)
ATTACK_IDS = ("over_budget", "wrong_merchant", "substitution", "authority_spoof")


def to_minor(price: Decimal) -> int:
    """Decimal dollars -> integer cents, exactly (never through a float)."""
    return int((Decimal(price) * 100).to_integral_exact())


def merchant_catalog() -> dict[str, MerchantProduct]:
    records = json.loads(MERCHANT_CATALOG_FILE.read_text(encoding="utf-8"))
    return {r["product_id"]: MerchantProduct.model_validate(r) for r in records}


def provider_catalog() -> dict[str, dict]:
    records = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    return {r["id"]: r for r in records}


def _variant(attributes: dict[str, str]) -> str | None:
    return f"size {attributes['size']}" if attributes.get("size") else None


def attack_description(scenario_id: str) -> str:
    """The injected text the attack fixture hides in a product description."""
    return attack_fixtures()[scenario_id].product.description


def attack_product(scenario_id: str) -> MerchantProduct:
    """The cart line a tricked agent would end up proposing for an attack fixture's product.

    The fixture changes only some fields (the substitution fixture keeps the old title but says
    brand "Other", category "electronics"), so for substitution the title is built from brand and
    category: that is what a real cart for that product would say."""
    p = attack_fixtures()[scenario_id].product
    title = f"{p.brand} {p.category}" if scenario_id == "substitution" else p.title
    return MerchantProduct(
        product_id=p.id, sku=p.id.upper(), title=title, url=f"https://demo.invalid/{p.id}",
        merchant_id=p.merchant_id, unit_price_minor=to_minor(p.price), currency=p.currency,
        stock=1, variant=_variant(p.attributes),
    )


def wrong_variant_product() -> MerchantProduct:
    """The merchant's size-11 shoe, signed and shipped although the mandate asks for size 12. The
    shopper cannot sell it (stock 0), so e2e signs it with the real merchant key: SIMULATED."""
    return merchant_catalog()[WRONG_VARIANT_PRODUCT_ID].model_copy(update={"stock": 1})


def catalog_product(product_id: str, merchant_id: str = MERCHANT_ID) -> MerchantProduct:
    """A real item from System 1's provider catalog, as if `merchant_id` sold it."""
    r = provider_catalog()[product_id]
    return MerchantProduct(
        product_id=r["id"], sku=r["id"].upper(), title=r["title"], url=f"https://demo.invalid/{r['id']}",
        merchant_id=merchant_id, unit_price_minor=to_minor(Decimal(r["price"])), currency=r["currency"],
        stock=1, variant=_variant(r.get("attributes", {})),
    )


@dataclass(frozen=True)
class SemanticInput:
    label: str
    category: str
    description: str
    sku: str
    title: str
    variant: str | None


def semantic_inputs() -> list[SemanticInput]:
    """Every (mandate, cart line) pair the e2e flow can put in front of the Guardian's semantic
    check, so scripts/warm_semantic_cache.py can cache the answers for the demo."""
    out: list[SemanticInput] = []

    def add(label: str, p: MerchantProduct) -> None:
        out.append(SemanticInput(label, CATEGORY, DESCRIPTION, p.sku, p.title, p.variant))

    shoe = merchant_catalog()[HAPPY_PRODUCT_ID]
    add("shopper cart (happy path, wrong-merchant, reused, expired)", shoe)
    add("wrong variant (size 11 against a size 12 mandate)", wrong_variant_product())
    for sid in ATTACK_IDS:
        add(f"attack fixture: {sid}", attack_product(sid))
    for pid in ("tech-001", "tech-002", "tech-003", "grocery-001"):
        add(f"wrong category: {pid}", catalog_product(pid))
    seen, unique = set(), []
    for item in out:
        key = (item.category, item.description, item.sku, item.title, item.variant)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique
