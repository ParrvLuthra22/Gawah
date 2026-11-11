"""Redacted, opt-in Channel3 schema diagnostic for development environments.

It prints identifiers, field names, and availability flags only.  It never prints
credentials, descriptions, URLs, or full provider bodies.
"""

import argparse
import asyncio
import json
from typing import Any

import httpx
from shopper.config import Settings
from shopper.models.shopping_intent import ShoppingIntent
from shopper.providers.channel3 import Channel3Provider, Channel3ProviderError


def _summary(record: dict[str, Any]) -> dict[str, object]:
    variants = record.get("variants")
    options = variants.get("options") if isinstance(variants, dict) else []
    selected = variants.get("selected") if isinstance(variants, dict) else []
    return {
        "canonical_product_id": Channel3Provider._text(record.get("id")),
        "offer_count": len(record.get("offers", [])) if isinstance(record.get("offers"), list) else 0,
        "structured_attribute_keys": sorted(record.get("structured_attributes", {}).keys())
        if isinstance(record.get("structured_attributes"), dict)
        else [],
        "variant_option_names": [item.get("name") for item in options if isinstance(item, dict)],
        "selected_variant_names": [item.get("name") for item in selected if isinstance(item, dict)],
        "normalized_attribute_keys": sorted(Channel3Provider._attributes(record).keys()),
    }


async def diagnose() -> None:
    settings = Settings()
    provider = Channel3Provider(settings.channel3_api_key)
    intent = ShoppingIntent(
        brand="Nike",
        category="running shoes",
        audience="men",
        color="black",
        size="10",
        size_system="UK",
        max_budget=50,
        currency="USD",
    )
    if not settings.channel3_api_key:
        print(json.dumps({"result": "not_configured"}))
        return
    async with httpx.AsyncClient(timeout=provider._timeout_seconds) as client:
        body = await provider._search(client, intent)
        records = body.get("products", []) if isinstance(body, dict) else []
        print(json.dumps({"result": "search", "raw_product_count": len(records)}))
        for record in records[:5]:
            if not isinstance(record, dict):
                continue
            report: dict[str, object] = {"search": _summary(record)}
            params = provider._variant_parameters(record, intent)
            report["requested_option_names"] = sorted(params)
            if Channel3Provider._text(record.get("id")):
                _, detailed = await provider._resolve_product(client, record, intent)
                report["detail"] = _summary(detailed)
                detail_params = provider._variant_parameters(detailed, intent)
                report["detail_requested_option_names"] = sorted(detail_params)
                report["verified_attribute_keys"] = sorted(
                    provider._verified_variant_attributes(detailed, detail_params)
                )
            print(json.dumps(report, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args()
    if not args.confirm_live:
        print(json.dumps({"result": "not_run", "reason": "pass --confirm-live"}))
        return
    try:
        asyncio.run(diagnose())
    except Channel3ProviderError as exc:
        print(json.dumps({"result": "provider_error", "code": exc.code}))


if __name__ == "__main__":
    main()
