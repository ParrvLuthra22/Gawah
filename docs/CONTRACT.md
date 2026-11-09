# Gawah contract (System 2 API)

For Suvir (System 1) and Navya (System 3). Models live in `contract/gawah_contract/` (`from gawah_contract import Mandate, ...`), JSON Schemas in `contract/schemas/`, and example data in `contract/fixtures/`. Changing anything in `contract/` needs all three of us to agree. After a model change run `make contract`; a test fails if schemas are stale.

**Money rule:** always integer minor units. $65.00 is `6500` with `"currency": "USD"`. Never floats.

## Run the mock engine

```sh
uv sync
cp .env.example .env     # then set SYSTEM1_KEY and SYSTEM3_KEY to any strings, GAWAH_MOCK=1
make dev-engine          # http://localhost:8000
```

Ports: System 2 engine 8000, System 1 8001, System 3 8002, web UI 3000. In mock mode (`GAWAH_MOCK=1`, default) every `/v1` endpoint returns fixtures and validates request bodies against the real models. The story: Mandate `man_01` ($80 cap, merchant `demo-shoe-store`), proposal `prop_01` ($65 shoes, ALLOW, paid as `pay_01`), `prop_02` ($120, BLOCK OVER_BUDGET), `prop_03` (BLOCK MERCHANT_NOT_ALLOWED), and `pay_04` (cart size 10 vs mandate size 9, for the concede case). Unknown proposal ids get a mock verdict: BLOCK if the merchant isn't `demo-shoe-store` or total > 8000, else ALLOW.

## Auth, scopes and rate limit

Send `X-Gawah-Key: <key>` on every `/v1` call. System 1 uses `SYSTEM1_KEY`; System 3 and the UI use `SYSTEM3_KEY`. `/health` needs no key. **A valid key is not enough: each key may call only its own routes** (deny by default; a route in no list is closed to everyone). A call outside the key's scope is `403 FORBIDDEN` in the normal `ErrorResponse` shape. The table is `SCOPES` in `engine/gawah_engine/auth.py`, and `engine/tests/test_scopes.py` checks every route against every key.

| Route | System 1 (shopper) | System 3 (dashboard) |
|---|:-:|:-:|
| `POST /v1/proposals` | yes | no |
| `POST /v1/guardian/check` (dry run) | no | yes |
| `GET /v1/mandates`, `GET /v1/mandates/{id}` | yes | yes |
| `POST /v1/merchants`, `GET /v1/merchants/{id}/public-key` | yes | no |
| `POST /v1/mandates`, `POST /v1/mandates/{id}/revoke`, `GET /v1/mandates/{id}/budget` | no | yes |
| `GET /v1/payments`, `GET /v1/payments/{id}`, `GET /v1/evidence/{id}` | no | yes |
| `POST /v1/payments/{id}/refund` | **no** | yes |
| `GET /v1/ledger`, `/ledger/verify`, `/ledger/head`, `/ledger/stream` | no | yes |

The shopper's key therefore cannot pay or refund by any route except through the Guardian (`POST /v1/proposals`), and nothing but System 3 can refund. `guardian/check` is a dry run (no payment, no ledger write) and belongs to System 3 only: the shopper learns a decision only by proposing.

**Rate limit**: sliding 60-second window per key, default 60 requests (`RATE_LIMIT_PER_MINUTE`, 0 disables), in memory and per process. Over the limit: `429 RATE_LIMITED` with `details.retry_after_s`. Check order: 401, 429, 403.

## Errors

Every error has this shape (`ErrorResponse`): HTTP 400 `BAD_LAST_EVENT_ID`, 401 `UNAUTHORIZED`, 403 `FORBIDDEN` (key not allowed on that route), 404 not found, 409 conflict (`PROPOSAL_ID_CONFLICT`, `KEY_ID_CONFLICT`, `MERCHANT_KEY_CONFLICT`, `PAYMENT_NOT_REFUNDABLE`, `MANDATE_NOT_ACTIVE`), 413 `PAYLOAD_TOO_LARGE`, 422 `VALIDATION_ERROR` (including the input limits below), 429 `RATE_LIMITED`, 502 `REFUND_FAILED`. Real mode never answers 501.

```json
{"error": {"code": "UNAUTHORIZED", "message": "...", "details": null, "request_id": "req_ab12cd34ef56"}}
```

## Real mode (`GAWAH_MOCK=0`)

Shapes are identical to the mock; only behaviour differs. Everything is real (SQLite at `DATABASE_URL`, default `sqlite:///./gawah.db`): mandates, merchants, the ledger, the Guardian, payments (PayPal sandbox), refunds and evidence. No endpoint answers 501 in real mode. `POST /v1/proposals` returns `payment: null` when the Guardian blocks, otherwise the payment record (`PAID` or `FAILED`; `RESERVED` only if the outcome is unknown, see below).

- **Mandates**: `id` is `man_<12 hex>`; `issued_at` is set by the engine (UTC, whole seconds); `currency` is upper-cased; `expires_at` must have a timezone and be in the future, `max_amount_minor` > 0, `allowed_merchants` non-empty (422 otherwise). `signing_alg` is `ES256` and `key_id` looks like `gawah-mandate-key-1` (the mock fixtures say `ed25519`). The signature covers the immutable terms (id, user_id, category, item_description, max_amount_minor, currency, allowed_merchants, issued_at, expires_at), not `status`.
- **Status**: a mandate past `expires_at` becomes `expired` the next time it is read. `POST /v1/mandates/{id}/revoke` is idempotent for an already-revoked mandate (200, no second ledger entry) and returns 409 `MANDATE_NOT_ACTIVE` for an expired one.
- **Guardian rules** (real `POST /v1/proposals` and `POST /v1/guardian/check`). Every rule is evaluated and the decision lists ALL failing reason codes, in this order: `MANDATE_SIGNATURE_INVALID`, `MANDATE_REVOKED` / `MANDATE_EXPIRED`, `MERCHANT_NOT_REGISTERED` (the cart's merchant has no key on file), `CART_SIGNATURE_INVALID` (signature bad, or signed with a `key_id` that is not registered), `CART_EXPIRED`, `MERCHANT_MISMATCH` (the cart is from a different merchant than the proposal names), `PRICE_MISMATCH` (the proposal disagrees with the signed cart on unit price, quantity, total or currency, the product `sku` is not in the cart, or the cart total is not the sum of its lines; the signed cart is the source of truth), `MERCHANT_NOT_ALLOWED` (the cart's or the proposal's merchant is not in the mandate), `CURRENCY_MISMATCH` (cart vs mandate currency), `OVER_BUDGET` (cart total > remaining budget; remaining already subtracts reservations), `DUPLICATE`, then `CATEGORY_MISMATCH`. The decision `id` is `dec_<proposal id>`.
- **Duplicates and idempotency**: sending the same proposal `id` again returns the earlier decision unchanged and writes nothing new (409 `PROPOSAL_ID_CONFLICT` if the body differs). A different proposal with the same mandate, merchant and `cart_id` within 60 seconds of one that was allowed (or is still being decided) is blocked as `DUPLICATE`, and so is any proposal reusing a merchant+`cart_id` that was allowed under another mandate (see Input limits).
- **Semantic check**: runs only when every rule above passed, and can only turn ALLOW into BLOCK (`CATEGORY_MISMATCH`, model confidence >= 0.8). It sees only the mandate category and description and each cart line's sku/title/variant (never the rationale or anything else from the agent). Models are tried in order (`GROQ_MODEL`, then `GROQ_MODELS`): a 404, an HTTP 429, a 5xx, a timeout or an unusable answer moves to the next model; 429 honours `Retry-After` (60 s if absent) and the other failures cool the model down for 60 s, and cooled-down models are skipped until the cooldown ends. Only when every model is out, or there is no key, does the keyword matcher answer. The explanation ends with `(semantic check: <model|cache|fallback> [<model>]; <why>)`, e.g. `(semantic check: model [openai/gpt-oss-120b]; openai/gpt-oss-20b rate limited (429), cooling 30s)`, and `DECISION_MADE` data adds `semantic_model` (the model name or `"fallback"`) and `semantic_why`. `scripts/warm_semantic_cache.py` fills the cache for the demo inputs.
- **Advisories**: size or colour differences between the mandate description and the cart (e.g. mandate size 9, cart size 10) do NOT block. The verdict stays ALLOW; the issues are added to the explanation and stored as `data.advisories` (a list of strings, empty if none) in the `DECISION_MADE` ledger entry, whose `data` also has `mandate_id, proposal_id, verdict, reasons, explanation, amount_minor, currency, semantic_source, semantic_model, semantic_why`. `PROPOSAL_RECEIVED` is written before evaluation, `DECISION_MADE` after. `/v1/guardian/check` is a dry run and writes neither.
- **Errors**: unknown `mandate_id` is 404 `MANDATE_NOT_FOUND` (nothing is written). An unregistered merchant is not an error: it is a BLOCK with `MERCHANT_NOT_ALLOWED`.
- **Ledger**: creating a mandate appends `MANDATE_CREATED` and revoking appends `MANDATE_REVOKED` (`subject_id` and `data.mandate_id` are the mandate id), in the same transaction as the mandate change. `ts` has whole-second precision. `signature` is ES256 over the entry `hash`, with `key_id` naming the ledger key (`gawah-ledger-key-N`), so rotated keys still verify old entries. The database blocks UPDATE and DELETE on the ledger. `GET /v1/ledger` also accepts `type=` and `since_seq=` (also honoured in mock mode).
- **`GET /v1/ledger/verify`** recomputes every hash, `prev_hash` link, sequence number and signature. The response is `{ok, checked, first_broken_seq, reason}`; `reason` is a human-readable string when `ok` is false, otherwise null.
- Verifying a ledger by hand with `gawah_contract.hashing.verify_chain` still works on real entries; it checks hashes and links but not signatures.

## Payments (real mode)

**Flow of `POST /v1/proposals`** (write first, pay second):

1. `PROPOSAL_RECEIVED` is committed.
2. Under a per-mandate lock and one SQLite write transaction: evaluate the rules against the *current* mandate status and budget (remaining already subtracts reservations), write `DECISION_MADE`, and, on ALLOW, reserve the amount, create the payment as `RESERVED` and append `PAYMENT_STARTED`. Concurrent proposals for one mandate are serialised here, so two $65 proposals against an $80 mandate give exactly one ALLOW and one `OVER_BUDGET`. A revoke that lands before this step blocks the payment (`MANDATE_REVOKED`).
3. After the lock is released, PayPal is called: create the order with `custom_id = gawah:{proposal_id}:{ledger_seq}` (`ledger_seq` is the `PAYMENT_STARTED` entry, so PayPal's record points back to our ledger) and `PayPal-Request-Id = idem-{proposal_id}`, then pay it with the buyer card from the engine's environment. The order id is stored as soon as it exists, the capture id after capture.
4. Success: `PAID`, reservation committed (moves to `spent`), `PAYMENT_COMPLETED` with `paypal_order_id`, `paypal_capture_id`, `custom_id`. Failure: `FAILED`, reservation released, `PAYMENT_FAILED` with a safe reason (HTTP status, PayPal error name and issue codes, `debug_id`; never raw bodies or card data).

BLOCK means no reservation, no PayPal call and `payment: null`. Sending the same proposal `id` again returns the stored decision and payment; the database allows one payment per proposal (`proposal_id` is unique).

**Concurrency.** Proposals for one mandate are serialised by a per-process lock plus a SQLite `BEGIN IMMEDIATE` write transaction around evaluate + reserve; the transaction is what protects several engine processes sharing one database file (tested with two real uvicorn processes and 40 concurrent proposals: exactly one PAID, no overspend, one unforked ledger chain).

**Retries and unknown outcomes.** Timeouts, 429 and 5xx are retried up to 3 times with exponential backoff and the same `PayPal-Request-Id`, so PayPal replays rather than duplicates. Other 4xx fail at once. If the answer is lost after an order exists, the engine reads the order from PayPal and records what PayPal says. If the request failed with no response and no order id is known, the payment stays `RESERVED` (budget held, internal `paypal_status=UNKNOWN`) for manual review rather than releasing budget on a guess.

**Restart recovery.** On startup (real mode) every `RESERVED` payment is reconciled: with an order id the order is read and finalized as `PAID` or `FAILED`; without one it becomes `FAILED` ("interrupted before order creation") with `PAYMENT_FAILED` and the reservation released. Known risk: a crash after PayPal accepted the create but before the order id was saved is reconciled as "interrupted", so look the order up by `custom_id` if in doubt.

**Refunds.** `POST /v1/payments/{id}/refund` is a full refund of a `PAID` payment (`refund_capture`, idempotency key `idem-{proposal_id}-refund`). The payment becomes `REFUNDED` and `REFUNDED` is appended to the ledger. A second call returns the same record and does nothing. A payment that is not `PAID` (including `FAILED`) is 409 `PAYMENT_NOT_REFUNDABLE`; unknown id 404; PayPal rejecting the refund is 502 `REFUND_FAILED` and the payment stays `PAID`. **Budget rule: a refund gives the spent amount back to the mandate** (`spent_minor` decreases), so a refunded $65 purchase leaves the full $80 available again.

**Filters.** `GET /v1/payments` accepts `mandate_id`, `status`, `paypal_order_id`, `paypal_capture_id` (combinable).

**Evidence verification.** `GET /v1/evidence/{payment_id}` returns the `EvidenceBundle`: mandate, proposal, signed cart, decision (advisories are included in `explanation`), payment, and every ledger entry for the mandate, proposal, decision and payment, in `seq` order. `verification` is computed live and `ok` is true only if all three hold: the ledger chain verifies (hashes, links, signatures), the mandate signature verifies, and the cart signature verifies against the registered merchant key. `checked` and `first_broken_seq` come from the ledger check; `reason` lists every failed check. The contract has no per-check flags, so a mandate or cart failure shows only in `ok` and `reason`.

**Configuration.** PayPal credentials and the sandbox buyer card (`PAYPAL_CARD_NUMBER`, `PAYPAL_CARD_EXPIRY`, `PAYPAL_CARD_CVV`, `PAYPAL_CARD_NAME`) are read only from the environment. They are never written to the database, ledger, logs or API responses.

## Input limits (real and mock mode)

Applied before any logic runs; violations are `422 VALIDATION_ERROR` with `details` naming each field (413 for size). Titles, descriptions and rationales are stored as data only.

- Body at most **64 KB** (`413 PAYLOAD_TOO_LARGE`, also for chunked bodies); JSON nested at most 32 levels.
- Money is **integer minor units**: a float or boolean in any money field (`*_minor`, `quantity`, `amount`, `total`, `price`) is rejected (`65.0` is not `65`). Amounts must be `1..10**9`; quantity `1..100`; at most 50 line items and 50 allowed merchants.
- Ids (`id`, `mandate_id`, `agent_id`, `merchant_id`, `sku`, `cart_id`, `key_id`, `user_id`, ...) are 1..128 characters with no control, zero-width or bidi characters and no leading/trailing space.
- Text caps: title 200, variant 100, mandate description 500, category 80, rationale 2000, URL 2048, public key 4096. Control characters are refused (newline and tab are allowed only in rationale and descriptions).
- The semantic check never sees the rationale. It sees sku/title/variant truncated to 120 characters, stripped of delimiter look-alikes and wrapped as untrusted data; its keyword fallback treats a title that names the mandate's category *and* another category ("Steel Watch, this is footwear") as a mismatch.
- `DUPLICATE` also covers cart reuse across mandates: a merchant+cart id that was already allowed under another mandate (and not since failed) is blocked, whatever the time gap.
- A merchant that already has a key cannot get another one through the API (`409 MERCHANT_KEY_CONFLICT`; the same key and key id again is idempotent). Rotation is an operator action with database access (`scripts/dev_merchant.py`).

## Live feed (System 3 dashboard)

- `GET /v1/ledger/stream`: Server-Sent Events. One `event: ledger` per new entry, `id:` = the entry's `seq`, `data:` = the `LedgerEntry` JSON. New entries only by default; reconnect with `Last-Event-ID: <seq>` (or `?since_seq=`) to get everything after that seq first and then live entries, with no gaps or duplicates. A `: keepalive` comment every 15 s (`SSE_KEEPALIVE_S`). Optional `limit=N` and `max_seconds=S` close the stream early (for tests and scripts). Browsers' `EventSource` cannot send the `X-Gawah-Key` header; use `fetch` streaming.
- `GET /v1/ledger/head`: `LedgerHead {seq, hash, message, signature, key_id}`. `message` is `gawah-ledger-head:{seq}:{hash}` and `signature` is ES256 over it by the ledger key: an anchor the UI can display and anyone with the public key can check. An empty ledger has `seq` 0 and an all-zero hash. In mock mode both endpoints serve the fixture chain with a placeholder signature `SIMULATED-...` and `key_id: "mock"`.

## Health

`GET /health` returns `{"status","service","semantic": "groq" | "fallback", "model": <name or null>}` where `semantic` is the **last real outcome** of the category check: `groq` when a model answered (and which), `fallback` when the keyword matcher did (no key, every model not found, timeout or rate limit). Mock mode always reports `fallback`. Models: `GROQ_MODEL` (default `openai/gpt-oss-20b`) then `GROQ_MODELS` (default `openai/gpt-oss-20b,openai/gpt-oss-120b`); a 404 logs one warning, drops that model for this process and tries the next.

## Contract changes in this pass (all additive)

- `VerificationCheck {name, ok, detail}` (new model) and `LedgerVerifyResult.checks: list[VerificationCheck] = []`. `GET /v1/evidence/{id}` fills `verification.checks` with `ledger_chain`, `mandate_signature`, `cart_signature`, `payment_reference_match` (PayPal's `custom_id` points at this payment's `PAYMENT_STARTED` ledger entry and `PAYMENT_COMPLETED` repeats the same ids); `ok` is still the AND of all four. Plain `/v1/ledger/verify` leaves `checks` empty.
- `LedgerHead` (new model) for `GET /v1/ledger/head`.
- Earlier, also additive: four `ReasonCode` values (`MANDATE_SIGNATURE_INVALID`, `CART_EXPIRED`, `MERCHANT_MISMATCH`, `MERCHANT_NOT_REGISTERED`).
- Schemas regenerated with `make contract` (`contract/schemas/`).

## Signing carts (for System 1)

The Guardian verifies every cart against the public key the store registered, so a store must sign exactly the way the Guardian checks. Use the shared helper, do not re-implement it:

```python
from gawah_contract.carts import generate_keypair, public_key_pem, sign_cart
key = generate_keypair()                                   # once; keep the private key out of git
cart = sign_cart(cart_dict, key, key_id="demo-shoe-store-key-1")   # returns a SignedCart with key_id + signature set
```

Register the public key once: `POST /v1/merchants {"merchant_id","key_id","alg":"ES256","public_key_pem": <PEM or JWK JSON string>}`. Only ES256 (EC P-256) keys are accepted; `alg` must be `ES256` in real mode. A `(merchant_id, key_id)` pair is write-once (a different key under the same id is 409 `KEY_ID_CONFLICT`), and a merchant that already has a key cannot get a second one over the API (409 `MERCHANT_KEY_CONFLICT`). To rotate, an operator registers a new `key_id` with database access (`scripts/dev_merchant.py`) and signs new carts with it. `GET /v1/merchants/{id}/public-key` returns the most recently registered key.

What is signed: canonical JSON (sorted keys, no whitespace, ASCII-escaped) of the cart, `signature` removed, `key_id` included, timestamps normalised by the `SignedCart` model. Signature: base64url(r||s), no padding. `scripts/dev_merchant.py` makes a demo keypair for `demo-shoe-store` in `./keys/` (gitignored), registers it, and exposes `sign_cart(cart, private_key)`.

## The 7 objects

- **Mandate**: `{"id":"man_01","user_id":"user_parrv","category":"footwear","item_description":"Black running shoes, size 9, under $80","max_amount_minor":8000,"currency":"USD","allowed_merchants":["demo-shoe-store"],"issued_at":"2026-11-09T10:00:00Z","expires_at":"2026-11-16T10:00:00Z","status":"active","signature":"...","signing_alg":"ed25519","key_id":"gawah-ledger-key-1"}` (status: active, revoked, expired)
- **SignedCart** (made and signed by the store): `{"cart_id":"cart_01","merchant_id":"demo-shoe-store","line_items":[{"sku":"SHOE-BLK-RUN","title":"Black Runner","variant":"size 9","quantity":1,"unit_price_minor":6500}],"total_minor":6500,"currency":"USD","expires_at":"...","signature":"...","key_id":"demo-shoe-store-key-1"}`
- **PurchaseProposal** (System 1 sends): `id, mandate_id, agent_id, merchant_id, product{id,title,url}, quantity, unit_price_minor, currency, rationale, signed_cart, created_at`. See `contract/fixtures/proposal_shoes.json`.
- **GuardianDecision**: `{"id":"dec_02","proposal_id":"prop_02","verdict":"BLOCK","reasons":["OVER_BUDGET"],"explanation":"...","checked_at":"..."}`. Reason codes: OVER_BUDGET, MERCHANT_NOT_ALLOWED, CART_SIGNATURE_INVALID, MANDATE_EXPIRED, MANDATE_REVOKED, CURRENCY_MISMATCH, CATEGORY_MISMATCH, DUPLICATE, PRICE_MISMATCH, MANDATE_SIGNATURE_INVALID, CART_EXPIRED, MERCHANT_MISMATCH, MERCHANT_NOT_REGISTERED (the last four were added later; the mock never returns them).
- **PaymentRecord**: `{"id":"pay_01","proposal_id":"prop_01","decision_id":"dec_01","paypal_order_id":"FAKEORDER5O1234567","paypal_capture_id":"...","status":"PAID","amount_minor":6500,"currency":"USD","idempotency_key":"idem-prop_01","custom_id":"gawah:prop_01","created_at":"..."}` (status: RESERVED, PAID, FAILED, REFUNDED)
- **LedgerEntry**: `{"seq":3,"ts":"...","type":"DECISION_MADE","subject_id":"dec_01","data":{...},"prev_hash":"<64 hex>","hash":"<64 hex>","signature":"...","key_id":"..."}`. Hash is sha256 of canonical JSON of seq, ts, type, subject_id, data, prev_hash; helper `gawah_contract.hashing.verify_chain`.
- **DisputeCase**: `{"id":"dsp_02","paypal_dispute_id":"PP-D-FAKE-1002","payment_id":"pay_04","status":"OPEN","reason":"MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED","amount_minor":6500,"currency":"USD","opened_at":"...","source":"simulated"}` (source: paypal_sandbox or simulated)

## Endpoints

All examples assume `H='X-Gawah-Key: yourkey'` and `B=http://localhost:8000`.

| Method and path | Purpose |
|---|---|
| `POST /v1/mandates` | Create and sign a mandate (body `MandateCreate`) |
| `GET /v1/mandates`, `GET /v1/mandates/{id}` | List / fetch |
| `POST /v1/mandates/{id}/revoke` | Revoke |
| `GET /v1/mandates/{id}/budget` | `BudgetStatus`: `{mandate_id, max_amount_minor, spent_minor, reserved_minor, remaining_minor, currency}`; remaining = max - spent - reserved |
| `POST /v1/proposals` | Guardian, then payment, then ledger. Returns `{decision, payment or null}` |
| `POST /v1/guardian/check` | Dry run: decision only, no payment, no ledger write |
| `GET /v1/payments?mandate_id=&status=&paypal_order_id=&paypal_capture_id=`, `GET /v1/payments/{id}` | List (filters combine) / fetch |
| `POST /v1/payments/{id}/refund` | Full refund of a PAID payment; idempotent; returns the spent amount to the mandate budget |
| `GET /v1/ledger?mandate_id=&subject_id=` | Ledger entries |
| `GET /v1/ledger/verify` | `{ok, checked, first_broken_seq, reason, checks}` (`reason` is null when ok) |
| `GET /v1/ledger/head`, `GET /v1/ledger/stream` | Signed head of the ledger / live Server-Sent Events feed (see Live feed) |
| `GET /v1/evidence/{payment_id}` | Full bundle for Dispute Counsel: mandate, proposal, signed cart, decision, payment, ledger entries, verification |
| `POST /v1/merchants`, `GET /v1/merchants/{id}/public-key` | Register / fetch a store's public key (`MerchantKey`) |
| `GET /health` | `{"status":"ok","service":"gawah","semantic":"groq"\|"fallback","model":...}` |

```sh
curl -X POST $B/v1/proposals -H "$H" -H 'Content-Type: application/json' -d @contract/fixtures/proposal_shoes.json
curl -X POST $B/v1/guardian/check -H "$H" -H 'Content-Type: application/json' -d @contract/fixtures/proposal_poisoned_over_budget.json
curl -X POST $B/v1/mandates -H "$H" -H 'Content-Type: application/json' -d '{"user_id":"u1","category":"footwear","item_description":"Black running shoes, size 9","max_amount_minor":8000,"currency":"USD","allowed_merchants":["demo-shoe-store"],"expires_at":"2026-12-01T00:00:00Z"}'
curl $B/v1/mandates/man_01 -H "$H"
curl -X POST $B/v1/mandates/man_01/revoke -H "$H"
curl $B/v1/payments/pay_01 -H "$H"
curl -X POST $B/v1/payments/pay_01/refund -H "$H"
curl "$B/v1/ledger?subject_id=pay_01" -H "$H"
curl $B/v1/ledger/verify -H "$H"
curl $B/v1/evidence/pay_04 -H "$H"
curl -X POST $B/v1/merchants -H "$H" -H 'Content-Type: application/json' -d '{"merchant_id":"demo-shoe-store","key_id":"k1","alg":"ed25519","public_key_pem":"-----BEGIN PUBLIC KEY-----..."}'
curl $B/v1/merchants/demo-shoe-store/public-key -H "$H"
```
