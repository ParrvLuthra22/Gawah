"""Input hardening for everything an outside caller can send.

Two layers, because pydantic alone is too forgiving (it turns 6500.0 into 6500 and accepts any
length of string):

1. `BodyLimitMiddleware` (raw bytes, before routing): bodies over 64 KB are refused with 413, and
   any JSON float or boolean in a money field (`*_minor`, `quantity`, `amount`, `total`, `price`)
   is refused with 422. Money is integer minor units everywhere.
2. `check_*` functions (parsed models, called by the routes): positive amounts up to 10**9 minor
   units, quantity 1..100, string length caps, no control characters in ids, bounded lists.

Titles, descriptions and rationales are stored as data only: they are length-capped, never
evaluated, never put into a ledger key, and the only code that ever shows them to a model
(`semantic.build_prompt`) truncates them and wraps them as untrusted.
"""

import json
import unicodedata
from typing import Any
from uuid import uuid4

from gawah_contract import MandateCreate, MerchantKey, PurchaseProposal, SignedCart

from .errors import GawahError

MAX_BODY_BYTES = 64 * 1024
MAX_MINOR = 10**9
MAX_QUANTITY = 100
MAX_LINE_ITEMS = 50
MAX_MERCHANTS = 50

ID_MAX = 128
TITLE_MAX = 200
VARIANT_MAX = 100
DESCRIPTION_MAX = 500
RATIONALE_MAX = 2000
URL_MAX = 2048
PEM_MAX = 4096

_MONEY_NAMES = {"quantity", "amount", "total", "price"}


def is_money_field(name: str) -> bool:
    n = name.lower()
    return n.endswith("_minor") or n in _MONEY_NAMES


MAX_JSON_DEPTH = 32


def find_bad_money(root: Any) -> str | None:
    """Return the JSON path of the first float/bool in a money field, or of the point where
    nesting exceeds MAX_JSON_DEPTH (prefixed "too deep:"), else None. Iterative, so hostile
    nesting can never overflow the stack, and paths are built only for the one that is reported."""
    stack: list[tuple[Any, tuple, int]] = [(root, (), 0)]  # (node, path-as-tuple, depth)
    while stack:
        node, path, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            return "too deep:" + _fmt(path[:8])
        if isinstance(node, dict):
            for k, v in node.items():
                if is_money_field(str(k)) and isinstance(v, (float, bool)):
                    return _fmt((*path, k))
                if isinstance(v, (dict, list)):
                    stack.append((v, (*path, k), depth + 1))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, (dict, list)):
                    stack.append((v, (*path, i), depth + 1))
    return None


def _fmt(path: tuple) -> str:
    return "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in path)


def has_control_chars(text: str, allow_whitespace: bool = False) -> bool:
    for ch in text:
        cat = unicodedata.category(ch)
        if allow_whitespace and ch in "\n\t":
            continue
        if cat.startswith("C"):  # Cc control, Cf format (zero-width, bidi), Cs, Co, Cn
            return True
    return False


class _Problems:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, field: str, msg: str) -> None:
        self.items.append({"field": field, "msg": msg})

    def ident(self, field: str, value: str | None, *, required: bool = True) -> None:
        if value is None:
            if required:
                self.add(field, "is required")
            return
        if not value or len(value) > ID_MAX:
            self.add(field, f"must be 1..{ID_MAX} characters")
        elif has_control_chars(value) or value != value.strip():
            self.add(field, "must not contain control or invisible characters or edge whitespace")

    def text(self, field: str, value: str | None, limit: int, *, multiline: bool = False) -> None:
        if value is None:
            return
        if len(value) > limit:
            self.add(field, f"must be at most {limit} characters")
        elif has_control_chars(value, allow_whitespace=multiline):
            self.add(field, "must not contain control characters")

    def money(self, field: str, value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            self.add(field, "must be an integer number of minor units")
        elif value <= 0:
            self.add(field, "must be greater than zero")
        elif value > MAX_MINOR:
            self.add(field, f"must be at most {MAX_MINOR} minor units")

    def quantity(self, field: str, value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            self.add(field, "must be an integer of at least 1")
        elif value > MAX_QUANTITY:
            self.add(field, f"must be at most {MAX_QUANTITY}")

    def currency(self, field: str, value: str) -> None:
        if len(value) != 3 or not value.isascii() or not value.isalpha():
            self.add(field, "must be a 3-letter currency code")

    def raise_if_any(self) -> None:
        if self.items:
            raise GawahError(
                422, "VALIDATION_ERROR", "Input rejected by safety limits", self.items[:20]
            )


def _cart(p: _Problems, c: SignedCart, prefix: str) -> None:
    p.ident(f"{prefix}.cart_id", c.cart_id)
    p.ident(f"{prefix}.merchant_id", c.merchant_id)
    p.ident(f"{prefix}.key_id", c.key_id, required=False)
    p.text(f"{prefix}.signature", c.signature, 512)
    p.money(f"{prefix}.total_minor", c.total_minor)
    p.currency(f"{prefix}.currency", c.currency)
    if not c.line_items or len(c.line_items) > MAX_LINE_ITEMS:
        p.add(f"{prefix}.line_items", f"must have 1..{MAX_LINE_ITEMS} items")
    for i, li in enumerate(c.line_items[:MAX_LINE_ITEMS]):
        f = f"{prefix}.line_items[{i}]"
        p.ident(f"{f}.sku", li.sku)
        p.text(f"{f}.title", li.title, TITLE_MAX)
        p.text(f"{f}.variant", li.variant, VARIANT_MAX)
        p.quantity(f"{f}.quantity", li.quantity)
        p.money(f"{f}.unit_price_minor", li.unit_price_minor)


def check_proposal(pr: PurchaseProposal) -> None:
    p = _Problems()
    p.ident("id", pr.id)
    p.ident("mandate_id", pr.mandate_id)
    p.ident("agent_id", pr.agent_id)
    p.ident("merchant_id", pr.merchant_id)
    p.ident("product.id", pr.product.id)
    p.text("product.title", pr.product.title, TITLE_MAX)
    p.text("product.url", pr.product.url, URL_MAX)
    p.quantity("quantity", pr.quantity)
    p.money("unit_price_minor", pr.unit_price_minor)
    p.currency("currency", pr.currency)
    p.text("rationale", pr.rationale, RATIONALE_MAX, multiline=True)  # untrusted: data only
    _cart(p, pr.signed_cart, "signed_cart")
    p.raise_if_any()


def check_mandate_create(m: MandateCreate) -> None:
    p = _Problems()
    p.ident("user_id", m.user_id)
    p.text("category", m.category, 80)
    p.text("item_description", m.item_description, DESCRIPTION_MAX, multiline=True)
    p.money("max_amount_minor", m.max_amount_minor)
    p.currency("currency", m.currency)
    if len(m.allowed_merchants) > MAX_MERCHANTS:
        p.add("allowed_merchants", f"at most {MAX_MERCHANTS} merchants")
    for i, mid in enumerate(m.allowed_merchants[:MAX_MERCHANTS]):
        p.ident(f"allowed_merchants[{i}]", mid)
    p.raise_if_any()


def check_merchant_key(k: MerchantKey) -> None:
    p = _Problems()
    p.ident("merchant_id", k.merchant_id)
    p.ident("key_id", k.key_id)
    p.text("alg", k.alg, 32)
    p.text("public_key_pem", k.public_key_pem, PEM_MAX, multiline=True)
    p.raise_if_any()


def check_path_id(name: str, value: str) -> None:
    p = _Problems()
    p.ident(name, value)
    p.raise_if_any()


# ---- ASGI middleware -----------------------------------------------------------------------


async def _send_error(send, status: int, code: str, message: str, details: Any = None) -> None:
    body = json.dumps(
        {"error": {"code": code, "message": message, "details": details,
                   "request_id": f"req_{uuid4().hex[:12]}"}}
    ).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class BodyLimitMiddleware:
    """Refuse bodies over MAX_BODY_BYTES (413) and floats/booleans in money fields (422)."""

    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            return await _send_error(send, 413, "PAYLOAD_TOO_LARGE",
                                     f"Request body is limited to {self.max_bytes} bytes")
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return None
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                return await _send_error(send, 413, "PAYLOAD_TOO_LARGE",
                                         f"Request body is limited to {self.max_bytes} bytes")
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        if body:
            try:
                parsed = json.loads(body)
            except (ValueError, RecursionError):
                parsed = None  # let FastAPI answer with its normal 422
            bad = find_bad_money(parsed) if parsed is not None else None
            if bad and bad.startswith("too deep:"):
                return await _send_error(
                    send, 422, "VALIDATION_ERROR", f"JSON is nested more than {MAX_JSON_DEPTH} levels deep"
                )
            if bad:
                return await _send_error(
                    send, 422, "VALIDATION_ERROR",
                    "Money fields must be integers in minor units (no floats or booleans)",
                    [{"field": bad, "msg": "must be an integer number of minor units"}],
                )
        sent = False

        async def replay():
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return await self.app(scope, replay, send)
