# Integration issues

Findings from System 2 about code that other owners maintain (`system1/`, `system3/`, `web/`).
System 2 does not edit those directories; each item says who needs to act.

## Checks that came back clean (2026-11-10, after merging System 3)

- `pytest` (engine + contract): 425 passed, 1 skipped before this work; `ruff check .` clean.
- System 3 tests (Counsel, Python 3.14 and 3.12): 77 passed.
- `web`: `npx vitest run` 28 passed; `npm run build` succeeds.
- **No key can reach a browser.** `grep` over `web/` (excluding `node_modules` and `.next`) for
  `NEXT_PUBLIC`, `SYSTEM3_KEY`, `X-Gawah-Key` and `process.env`: no `NEXT_PUBLIC_` variable exists;
  `SYSTEM3_KEY` is read only in the server route `web/app/api/[...segments]/route.ts` (and tests);
  the only other `process.env` use is `NODE_ENV` in two page components. Nothing sets a key in a
  client component. (System 2's own `.env.example` had an unused `NEXT_PUBLIC_ENGINE_URL`; removed.)

## Open

### 1. `npm start` binds to loopback (web, Navya)
`web/package.json` has `"start": "next start --hostname 127.0.0.1"`. On Render the service is
unreachable from outside the container. Workaround in `render.yaml`: the start command is
`cd web && npx next start --hostname 0.0.0.0 --port $PORT`. Suggested fix: `"start": "next start"`
(Next reads `HOSTNAME`/`PORT`) or `--hostname 0.0.0.0` behind an env switch.

### 2. The hosted dashboard has no user login (web + Counsel, Navya)
`web/README.md` says a shared deployment "requires its own user access controls". The web server
adds `SYSTEM3_KEY` to every allow-listed request for any visitor, including `POST /api/disputes/sync`
(calls PayPal), `POST /api/evidence/{id}/assemble` and `POST /api/counsel/{id}/analyze` (calls
Groq). Same-origin checks stop cross-site posts, not a visitor using the site. For the hackathon
demo this is accepted (sandbox data only, unlisted URL), but anyone with the URL can spend the
Groq/PayPal sandbox quota. Suggested: a shared demo password (middleware) before wider sharing.

### 3. Resolution actions are disabled when hosted (web, by design)
`route.ts` allows resolution POSTs only when `NODE_ENV === "development"` and the host is
localhost, so the hosted Resolution Center is read-only. `ENABLE_SANDBOX_RESOLUTION=false` is set
in `render.yaml` to match. Not a bug; recorded so nobody expects refunds from the hosted UI.

### 4. `web/AGENTS.md` (web, Navya): resolved
It is tracked upstream now (an untracked local copy blocked `git pull` until removed; it was identical).

## From the end-to-end proof (`make e2e`, 2026-11-10)

`integration/e2e.py` runs shopper -> signed proposal -> engine Guardian -> payment -> ledger. It
did not edit `system1/`; everything below is what System 1 would have to change, with workarounds
already in `integration/`.

### 5. BLOCKER: every shopper proposal is blocked as `PRICE_MISMATCH` (system1, Suvir)
The Guardian finds the product in the signed cart by `proposal.product.id == cart line sku`
(`engine/gawah_engine/guardian.py:206`; `contract/fixtures/proposal_shoes.json` has both equal to
`SHOE-BLK-RUN`). The shopper sets `product.id` to its internal product id (`shoe-001`) while the cart
line sku is `NIKE-SWIFTRUN-12-BLK`, so even the honest happy path is blocked. Exact change, in
`system1/shopper/merchant/demo_merchant.py`, `ProposalPreparationService.prepare`:

```diff
-            product=ContractProduct(id=product.product_id, title=product.title, url=product.url),
+            product=ContractProduct(id=product.sku, title=product.title, url=product.url),
```
(and update any test in `system1/tests` that asserts the old id). Workaround in `integration/e2e.py`:
it overwrites `product.id` with the cart sku before posting, and a dry run of the unmodified proposal
(`POST /v1/guardian/check`) prints `KNOWN ISSUE #5` for as long as the bug exists.

### 6. No endpoint exposes the merchant public key (system1, Suvir)
The engine must have the store's public key before it can verify a cart, and the shopper offers no
way to read it. Workaround: `integration/e2e.py` builds a `DemoMerchantAdapter` on the same
`MERCHANT_SIGNING_KEY_PATH` and registers `adapter.merchant_key("demo-sports")` (public key only)
with `POST /v1/merchants`. Suggested change in `system1/shopper/main.py`:

```python
@app.get("/shopping/demo/merchant-key", response_model=MerchantKey)
def demo_merchant_key(service: Annotated[ProposalPreparationService, Depends(get_proposal_preparation_service)]):
    """Public key only: register it with the engine (POST /v1/merchants)."""
    return service.merchant.merchant_key("demo-sports")
```
`merchant_key()` reaches into `self._signer._load` (private); a public `ES256Signer.public_key_pem(key_id)`
would be cleaner (that class is in `engine/`; System 2 can add it).

### 7. The shopper never talks to the engine (system1, Suvir)
`shopper/config.py` has no engine URL or `SYSTEM1_KEY`, and nothing in `system1/` posts a proposal.
`/shopping/demo/checkout` ends at "proposal prepared"; `integration/e2e.py` does the `POST /v1/proposals`.
Needed for a real demo: `ENGINE_URL` and `SYSTEM1_KEY` settings and one call with header
`X-Gawah-Key`. The shopper's key may only call `POST /v1/proposals`, `GET /v1/mandates*` and the
merchants routes (`engine/gawah_engine/auth.py` SCOPES); it must never get a payment or refund tool.

### 8. The attack runner never produces a proposal (system1, Suvir)
`/shopping/test/attacks/run` (`attack_simulation.py`) filters fixture products and reports
`observed_outcome` (all four say `ATTACK_RESISTED`), but `purchase_proposal_id` is always `None`, so
nothing reaches the Guardian. `integration/e2e.py` therefore builds, for each fixture, the proposal a
tricked agent would send (a cart signed by the real demo-merchant key, the fixture's injected
description as `rationale`) and labels it SIMULATED. Note `authority_spoof` is a compliant cart: the
Guardian rightly ALLOWS it because the injected text only ever lives in a description or rationale,
which the Guardian never reads. The run also sends the same text on a $120 cart and checks it is blocked.

### 9. Provider catalog and merchant catalog disagree (system1, Suvir)
`data/demo_catalog.json` has 13 products; the merchant catalog (`demo_merchant_catalog.json`) can only
sell `shoe-001`, `shoe-002` (stock 0), `grocery-001`, `grocery-004`. If the real model selects
`shoe-003` ($74, a valid pick for "black Nike running shoes size 12 under $80") the checkout answers
"The demo merchant could not verify the selected product; no cart was created." The e2e stub picks the
cheapest product, which happens to be `shoe-001`. Add the other shoes (and `demo-tech`) to the merchant
catalog, or restrict the demo provider to what the merchant sells.

### 10. `Settings` demands `GROQ_API_KEY` even when no model is used (system1, Suvir)
`Settings()` needs `groq_api_key`, so signing a cart or starting a standalone shopper without a key
raises. `integration/` passes a placeholder. Make it optional (`str | None = None`) and fail only where
Groq is called. Also: `MERCHANT_SIGNING_KEY_PATH` defaults to a path relative to the working
directory, and the `/shopping/*` routes have no authentication (see `docs/DEPLOY.md`, "gawah-shopper").

### 11. Root `pyproject.toml` (System 2, FYI for everyone)
`shopper` was not installed by a plain `uv sync` (only `--all-packages`), so `uv run pytest system1/tests`
failed on a fresh clone. The root dev dependency group now includes `gawah-shopper` (workspace member),
`pythonpath` adds `.`, and `testpaths` adds `integration/tests`. Side effect: the engine's Render build
(`uv sync --frozen`) also installs the shopper and `groq` (dev group), harmlessly.

### 12. `ruff check .` fails on three System 3 files (system3, Navya)
`I001` (import order, auto-fixable with `ruff check --fix`) in `system3/counsel/demo.py`,
`system3/run_demo_scenarios.py` and `system3/tests/test_demo.py`. CLAUDE.md requires `ruff check .`
to pass; System 2 did not touch them.

### 13. System 2 follow-up: the keyword fallback has no grocery words (engine, Parrv)
Without a Groq key the Guardian's fallback labels `Harvest Gold Atta Bread` and `Green Valley Apples`
under a footwear mandate as `unclear`, which does not block. With Groq all four demo grocery and
electronics titles are `mismatch` (cached by `scripts/warm_semantic_cache.py`). The e2e therefore uses
an electronics title for its wrong-category case, which the fallback does catch. Candidate fix: add
bread, produce and similar words to the `groceries` group in `engine/gawah_engine/semantic.py`.
