# System 1 shopper API

The shopper service is server-side only.  It never executes a payment and does
not return Groq/Channel3 credentials or merchant private keys.  Requests are
JSON; no browser authentication is currently required for these development
endpoints.  Deployments should place the service behind their normal gateway.

## Discovery and recommendations

`POST /shopping/intent` accepts `{"message":"Find black Nike running shoes size UK10 under $50 for men"}`
and returns the validated `ShoppingIntent` (including optional `audience` and
`size_system`).  It returns `429` for an AI rate limit, `503` for unavailable
AI service, or `502` for invalid model output.

`POST /shopping/discover` accepts the same request and returns `{intent,
discovery}`.  `discovery` separates `eligible`, `needs_verification`, and
`rejected`; `verifications` contains per-constraint `verified`,
`needs_verification`, or `rejected` evidence.  A `verified` listing is only a
suitability match, never checkout-ready.  `source.provider` is `demo` or
`channel3`.  Channel3 failures return `429` or `503` without provider bodies.

`POST /shopping/agent` accepts the same request and returns the immutable
intent, safe tool trace, selected eligible product (if any), recommendations,
and verification candidates.  Product descriptions and URLs are not passed to
the model tools. `purchase_ready` is always false here because a listing is not
a signed merchant cart.

Set `PRODUCT_PROVIDER=demo` (default) or `PRODUCT_PROVIDER=channel3`. The
latter additionally requires `CHANNEL3_API_KEY`; it never falls back to demo.

## Explicit demo merchant flow

`GET /shopping/demo/products` returns public demo catalog records only.

`POST /shopping/proposals/prepare` accepts:

```json
{"selected_product_id":"shoe-001","merchant_id":"demo-sports","mandate_id":"mandate-ref","agent_id":"shopper-agent","quantity":1}
```

It returns a shared-contract `PurchaseProposal` with a merchant-signed cart,
or `422` when the controlled merchant rejects identity, stock, or merchant.
Price and stock come from the merchant catalog, never the request or LLM.

`POST /shopping/demo/checkout` is the explicit end-to-end demo path. It
accepts `message`, `mandate_id`, `agent_id`, and optional `quantity`; it runs
the demo catalog agent and creates a proposal only for its eligible selected
product. Its response includes `{intent, agent, proposal, purchase_ready,
explanation}`. `purchase_ready` is true only when `proposal` contains the
merchant-signed cart. A mandate reference is required but System 1 does not
invent or validate a mandate signature.

## Diagnostics and security

`POST /shopping/test/attacks/run` is absent (`404`) unless
`ATTACK_SIMULATION_ENABLED=true`; it uses local fixtures and neither contacts
Guardian nor signs carts. The safe local Channel3 diagnostic is opt-in:

```sh
uv run --package gawah-shopper python system1/scripts/diagnose_channel3.py --confirm-live
```

It prints only IDs and response-field names, never descriptions, URLs, raw
provider responses, or credentials. `system1/scripts/diagnose_groq_attack.py
--confirm-live` is likewise development-only.

## Guardian handoff

System 2 provides `POST /v1/guardian/check`, but its current service-key scope
permits that dry-run endpoint only to System 3. System 1 is limited to
`POST /v1/proposals`, which may create a payment path and is therefore not
called by this service. No Guardian decision is claimed until System 2 exposes
a non-payment System 1 scope or an approved handoff service.
