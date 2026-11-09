"""Test world: a mandate, a registered demo store, and a factory for signed proposals."""

import json
from datetime import timedelta

from conftest import make_body
from gawah_contract import MerchantKey, Product, PurchaseProposal
from gawah_contract.carts import generate_keypair, public_key_pem, sign_cart
from gawah_engine.semantic import SemanticChecker

STORE = "demo-shoe-store"
KEY_ID = "demo-shoe-store-key-1"


class World:
    """A mandate ($80 footwear, size 9), a registered demo store, and a proposal factory."""

    def __init__(self, svc, clock, max_amount_minor=8000):
        self.svc, self.clock, self.n = svc, clock, 0
        self.key = generate_keypair()
        self.register(STORE, self.key)
        self.mandate = svc.mandates.create(make_body(clock, max_amount_minor=max_amount_minor))
        self.model_calls: list[list[dict]] = []

    def register(self, merchant, key, key_id=KEY_ID):
        self.svc.merchants.register(
            MerchantKey(merchant_id=merchant, key_id=key_id, alg="ES256", public_key_pem=public_key_pem(key))
        )

    def set_model(self, reply, clock=None):
        """Install a fake model. `reply` is a dict/str to return, or an exception to raise."""

        def call(messages):
            self.model_calls.append(messages)
            if isinstance(reply, Exception):
                raise reply
            return reply if isinstance(reply, str) else json.dumps(reply)

        self.svc.guardian.semantic = SemanticChecker(self.svc.path, llm_call=call, clock=self.clock)

    def cart(self, *, merchant=STORE, signer=None, key_id=KEY_ID, sku="SHOE-BLK-RUN",
             title="Black Runner", variant="size 9", qty=1, unit=6500, currency="USD",
             expires_in=3600, cart_id=None, tamper=None):
        self.n += 1
        cart = {
            "cart_id": cart_id or f"cart_{self.n}",
            "merchant_id": merchant,
            "line_items": [{"sku": sku, "title": title, "variant": variant, "quantity": qty,
                            "unit_price_minor": unit}],
            "total_minor": qty * unit,
            "currency": currency,
            "expires_at": (self.clock() + timedelta(seconds=expires_in)).isoformat(),
            "signature": "",
            "key_id": key_id,
        }
        signed = sign_cart(cart, signer or self.key, key_id).model_dump(mode="json")
        if tamper:
            tamper(signed)
        return signed

    def proposal(self, cart=None, *, pid=None, rationale="Black running shoes, size 9.", **over):
        self.n += 1
        cart = cart or self.cart()
        line = cart["line_items"][0]
        fields = {
            "id": pid or f"prop_{self.n}",
            "mandate_id": self.mandate.id,
            "agent_id": "shopper-agent-1",
            "merchant_id": cart["merchant_id"],
            "product": Product(id=line["sku"], title=line["title"], url="https://x.example/p"),
            "quantity": line["quantity"],
            "unit_price_minor": line["unit_price_minor"],
            "currency": cart["currency"],
            "rationale": rationale,
            "signed_cart": cart,
            "created_at": self.clock(),
        }
        fields.update(over)
        return PurchaseProposal(**fields)

    def entries(self, **kw):
        return self.svc.ledger.list(**kw)


