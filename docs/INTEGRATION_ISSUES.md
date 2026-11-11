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

### 5. FIXED (2026-11-11): every shopper proposal was blocked as `PRICE_MISMATCH` (system1, Suvir)
System 1 now sends the cart sku as `product.id`. `integration/e2e.py` no longer rewrites it and a test
asserts the unmodified proposal is ALLOWED. Original report, kept for the record:

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

### 9. FIXED (2026-11-11): provider catalog and merchant catalog disagreed (system1, Suvir)
`PurchasableDemoCatalogProvider` now limits the demo checkout to what the merchant stocks. Original report:

`data/demo_catalog.json` has 13 products; the merchant catalog (`demo_merchant_catalog.json`) can only
sell `shoe-001`, `shoe-002` (stock 0), `grocery-001`, `grocery-004`. If the real model selects
`shoe-003` ($74, a valid pick for "black Nike running shoes size 12 under $80") the checkout answers
"The demo merchant could not verify the selected product; no cart was created." The e2e stub picks the
cheapest product, which happens to be `shoe-001`. Add the other shoes (and `demo-tech`) to the merchant
catalog, or restrict the demo provider to what the merchant sells.

### 10. FIXED (2026-11-11): `Settings` demanded `GROQ_API_KEY` even when no model is used (system1, Suvir)
`groq_api_key` now defaults to empty. The path and authentication points below still apply. Original report:

`Settings()` needs `groq_api_key`, so signing a cart or starting a standalone shopper without a key
raises. `integration/` passes a placeholder. Make it optional (`str | None = None`) and fail only where
Groq is called. Also: `MERCHANT_SIGNING_KEY_PATH` defaults to a path relative to the working
directory, and the `/shopping/*` routes have no authentication (see `docs/DEPLOY.md`, "gawah-shopper").

### 11. Root `pyproject.toml` (System 2, FYI for everyone)
`shopper` was not installed by a plain `uv sync` (only `--all-packages`), so `uv run pytest system1/tests`
failed on a fresh clone. The root dev dependency group now includes `gawah-shopper` (workspace member),
`pythonpath` adds `.`, and `testpaths` adds `integration/tests`. Side effect: the engine's Render build
(`uv sync --frozen`) also installs the shopper and `groq` (dev group), harmlessly.

### 12. FIXED (2026-11-11): `ruff check .` failed on three System 3 files (system3, Navya)
Import order fixed upstream; `ruff check .` is clean repo-wide. Original report:

`I001` (import order, auto-fixable with `ruff check --fix`) in `system3/counsel/demo.py`,
`system3/run_demo_scenarios.py` and `system3/tests/test_demo.py`. CLAUDE.md requires `ruff check .`
to pass; System 2 did not touch them.

### 13. FIXED (2026-11-11): the keyword fallback let groceries through (engine)
The fallback now fails closed for a known mandate category (Suvir), and recognises SwiftRun-style
footwear names (`engine/tests/test_keyword_fallback.py`); "Harvest Gold Atta Bread" is blocked. The
e2e uses a SIMULATED semantic model so it does not depend on either. Original report:

Without a Groq key the Guardian's fallback labels `Harvest Gold Atta Bread` and `Green Valley Apples`
under a footwear mandate as `unclear`, which does not block. With Groq all four demo grocery and
electronics titles are `mismatch` (cached by `scripts/warm_semantic_cache.py`). The e2e therefore uses
an electronics title for its wrong-category case, which the fallback does catch. Candidate fix: add
bread, produce and similar words to the `groceries` group in `engine/gawah_engine/semantic.py`.

### 14. `run_demo_scenarios.py` hardcodes Counsel's address (system3, Navya)
`system3/run_demo_scenarios.py` posts to `http://127.0.0.1:8002`, so Counsel cannot run on another
port for a rehearsal, and a leftover dev Counsel on 8002 would receive the call. Suggested: read the base
URL from `Settings` or an env var (for example `COUNSEL_URL`, default `http://127.0.0.1:8002`).
`scripts/rehearse.sh` pins Counsel to 8002 for this reason.

### 15. Merchant key persistence needs no System 1 change (resolved, FYI)
`MERCHANT_SIGNING_KEY_PATH` defaults to `keys/system1-merchant` (gitignored). `integration/e2e.py` and
`scripts/rehearse.sh` set it to the absolute `<repo>/keys/system1-merchant`, so every shopper restart reuses
the key. Run the shopper from the repo root or set the variable. If the engine's database remembers a
different key, `e2e.py` stops and prints the remedy; `scripts/register_merchant_key.py --rotate` applies it.
Still open for System 1: #6 (no endpoint exposes the public key) and #7 (the shopper never posts to the
engine); both are worked around in `integration/`.

### 16. Counsel's demo: DEFEND works, CONCEDE_REMEDY still declined by the model (PARTLY FIXED 2026-11-11; the rest is open in System 3)
Observed in a real-sandbox `scripts/rehearse.sh` run after the engine change below (payment ids masked;
DEFEND `pay_...a5c`, CONCEDE `pay_...22f`; rehearsal 49s, every step passed).

**FIXED, DEFEND (reason `UNAUTHORISED`).** The engine's `GuardianDecision` had no `mandate_id`, which
`_verified_authorization` requires (`decision.payload["mandate_id"]`). Now `GuardianDecision.mandate_id`
is an optional contract field (`docs/CONTRACT.md`, schemas regenerated), set from the proposal's mandate
on every ALLOW and BLOCK decision, stored in the DECISION_MADE ledger data (it already was) and returned
in the evidence bundle's `decision`. Decisions stored earlier have none and still validate (test:
`test_decisions_stored_before_mandate_id_existed_still_load`). Observed: all ten conditions of
`_verified_authorization` pass on the live pack, Counsel produces `AUTHORIZATION_CONSENT` and recommends
`DEFEND` (confidence 0.8), labelled "SIMULATED CASE".

**OPEN, CONCEDE (size mismatch), System 3.** Unchanged: the deterministic rule fires (finding
`AUTHORIZED_SIZE_MISMATCH` present; four primary checks VERIFIED; cart, mandate trusted; Guardian
advisory in the ledger) and the final answer is `REQUEST_MORE_EVIDENCE` with no `conflict` and null
confidence, so the Groq model chose it (`recommend()` honours that choice). Read, not run:
`system3/counsel/groq_counsel.py` INSTRUCTIONS still say a decisive outcome needs "the amount
comparison" and "a payment or signed cart cannot establish delivery", which would steer the model away.
The raw plan is not exposed, so this cause is an inference. Suggested for Navya: if the assessment's
supported outcome is decisive and the required finding is present, do not let the model downgrade it,
or update the prompt for the size-mismatch case.

**Refund, now demonstrable through the engine.** Counsel cannot refund a demo case itself
(`disabled_reason`: "Simulated disputes are read-only.", still observed with
`ENABLE_SANDBOX_RESOLUTION=true`; the resolution workflow API also answers 401 to the service key).
`scripts/refund_demo.py` (rehearse step 4b, right after Counsel's scenarios) is an OPERATOR refund via
the engine's route with `SYSTEM3_KEY`, labelled "Counsel recommended CONCEDE_REMEDY". Observed against
the real PayPal sandbox: status REFUNDED, 5500c; mandate budget spent 5500c -> 0c (reserved 0); one new
ledger entry `REFUNDED` (seq 36); chain ok, 36 entries. Tested with the fake PayPal
(`test_a_full_refund_restores_the_budget_and_writes_a_ledger_entry`,
`test_refund_demo_script_refunds_the_last_paid_payment_and_reports_it`). Counsel's own recommendation was
`REQUEST_MORE_EVIDENCE`, so the label states what Counsel recommended in principle for a size mismatch,
not what it output. Because the refund runs before the Counsel check, `counsel-check` and the probe
count PAID or REFUNDED payments.

Still true: Counsel lists `ORDER_UNAVAILABLE`, `FULFILLMENT_UNAVAILABLE`, `DELIVERY_UNAVAILABLE` (the
contract exposes no merchant order, fulfilment or delivery record).
