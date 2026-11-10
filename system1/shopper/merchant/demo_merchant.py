"""Demo merchant catalog, verification, and ES256 signed-cart generation."""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gawah_contract import LineItem, MerchantKey, PurchaseProposal, SignedCart
from gawah_contract import Product as ContractProduct
from gawah_engine.signing import ES256Signer
from pydantic import BaseModel, ConfigDict, Field

from shopper.config import Settings

MERCHANT_CATALOG_FILE = Path(__file__).resolve().parents[1] / "data" / "demo_merchant_catalog.json"
CART_TTL = timedelta(minutes=10)


class MerchantError(ValueError):
    """A merchant-side verification or fulfillment rule rejected the request."""


class MerchantProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str
    sku: str
    title: str
    url: str
    merchant_id: str
    unit_price_minor: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    stock: int = Field(ge=0)
    variant: str | None = None


class ProposalPreparationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_product_id: str = Field(min_length=1, max_length=100)
    merchant_id: str = Field(min_length=1, max_length=100)
    mandate_id: str = Field(min_length=1, max_length=100)
    agent_id: str = Field(min_length=1, max_length=100)
    quantity: int = Field(default=1, ge=1, le=10)


class DemoMerchantAdapter:
    """Merchant-controlled source of stock, pricing, currency, and cart signatures."""

    def __init__(self, settings: Settings, catalog_file: Path = MERCHANT_CATALOG_FILE) -> None:
        self._catalog_file = catalog_file
        self._signer = ES256Signer(settings.merchant_signing_key_path, "demo-merchant-cart")

    def _products(self) -> dict[str, MerchantProduct]:
        try:
            records = json.loads(self._catalog_file.read_text(encoding="utf-8"))
            products = [MerchantProduct.model_validate(record) for record in records]
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise MerchantError("merchant catalog unavailable") from exc
        return {product.product_id: product for product in products}

    def list_products(self) -> list[MerchantProduct]:
        """Public demo catalog only; signing material remains private to this adapter."""
        return list(self._products().values())

    def verify_product(
        self, product_id: str, expected_merchant_id: str, quantity: int
    ) -> MerchantProduct:
        product = self._products().get(product_id)
        if product is None:
            raise MerchantError("unknown merchant product")
        if product.merchant_id != expected_merchant_id:
            raise MerchantError("merchant mismatch")
        if product.stock < quantity:
            raise MerchantError("insufficient stock")
        return product

    def sign_cart(self, product: MerchantProduct, quantity: int) -> SignedCart:
        expires_at = datetime.now(UTC) + CART_TTL
        unsigned_cart = SignedCart.model_validate({
            "cart_id": f"cart_{uuid.uuid4().hex}",
            "merchant_id": product.merchant_id,
            "line_items": [
                LineItem(
                    sku=product.sku,
                    title=product.title,
                    variant=product.variant,
                    quantity=quantity,
                    unit_price_minor=product.unit_price_minor,
                ).model_dump(mode="json")
            ],
            "total_minor": product.unit_price_minor * quantity,
            "currency": product.currency,
            "expires_at": expires_at,
            "key_id": self._signer.active_key_id,
            "signature": "",
        })
        signature, key_id = self._signer.sign(unsigned_cart.model_dump(mode="json"))
        return unsigned_cart.model_copy(update={"signature": signature, "key_id": key_id})

    def verify_cart(self, cart: SignedCart) -> bool:
        return self._signer.verify(cart.model_dump(mode="json"), cart.signature, cart.key_id)

    def cart_is_current(self, cart: SignedCart) -> bool:
        return self.verify_cart(cart) and cart.expires_at > datetime.now(UTC)

    def merchant_key(self, merchant_id: str) -> MerchantKey:
        if merchant_id not in {product.merchant_id for product in self._products().values()}:
            raise MerchantError("unknown merchant")
        key_id = self._signer.active_key_id
        key = self._signer._load(key_id)
        if key is None:
            raise MerchantError("merchant signing key unavailable")
        from cryptography.hazmat.primitives import serialization

        public_pem = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")
        return MerchantKey(merchant_id=merchant_id, key_id=key_id, alg="ES256", public_key_pem=public_pem)


@dataclass
class ProposalPreparationService:
    merchant: DemoMerchantAdapter

    def prepare(self, request: ProposalPreparationRequest) -> PurchaseProposal:
        product = self.merchant.verify_product(
            request.selected_product_id,
            request.merchant_id,
            request.quantity,
        )
        cart = self.merchant.sign_cart(product, request.quantity)
        return PurchaseProposal(
            id=f"prop_{uuid.uuid4().hex}",
            mandate_id=request.mandate_id,
            agent_id=request.agent_id,
            merchant_id=product.merchant_id,
            product=ContractProduct(id=product.product_id, title=product.title, url=product.url),
            quantity=request.quantity,
            unit_price_minor=product.unit_price_minor,
            currency=product.currency,
            rationale="Merchant-verified cart prepared for Guardian review.",
            signed_cart=cart,
            created_at=datetime.now(UTC),
        )
