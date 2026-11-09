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

## Auth

Send `X-Gawah-Key: <key>` on every `/v1` call. System 1 uses `SYSTEM1_KEY`; System 3 and the UI use `SYSTEM3_KEY`. `/health` needs no key.

## Errors

Every error has this shape (`ErrorResponse`): HTTP 401 bad key, 404 not found, 409 conflict (real mode: revoking an expired mandate), 422 invalid body, 501 real mode not built yet.

```json
{"error": {"code": "UNAUTHORIZED", "message": "...", "details": null, "request_id": "req_ab12cd34ef56"}}
```

## Real mode (`GAWAH_MOCK=0`)

Shapes are identical to the mock; only behaviour differs. Everything is real (SQLite at `DATABASE_URL`, default `sqlite:///./gawah.db`): mandates, merchants, the ledger, the Guardian, payments (PayPal sandbox), refunds and evidence. No endpoint answers 501 in real mode. `POST /v1/proposals` returns `payment: null` when the Guardian blocks, otherwise the payment record (`PAID` or `FAILED`; `RESERVED` only if the outcome is unknown, see below).

- **Mandates**: `id` is `man_<12 hex>`; `issued_at` is set by the engine (UTC, whole seconds); `currency` is upper-cased; `expires_at` must have a timezone and be in the future, `max_amount_minor` > 0, `allowed_merchants` non-empty (422 otherwise). `signing_alg` is `ES256` and `key_id` looks like `gawah-mandate-key-1` (the mock fixtures say `ed25519`). The signature covers the immutable terms (id, user_id, category, item_description, max_amount_minor, currency, allowed_merchants, issued_at, expires_at), not `status`.
- **Status**: a mandate past `expires_at` becomes `expired` the next time it is read. `POST /v1/mandates/{id}/revoke` is idempotent for an already-revoked mandate (200, no second ledger entry) and returns 409 `MANDATE_NOT_ACTIVE` for an expired one.
- **Guardian rules** (real `POST /v1/proposals` and `POST /v1/guardian/check`). Every rule is evaluated and the decision lists ALL failing reason codes, in this order: `MANDATE_SIGNATURE_INVALID`, `MANDATE_REVOKED` / `MANDATE_EXPIRED`, `MERCHANT_NOT_REGISTERED` (the cart's merchant has no key on file), `CART_SIGNATURE_INVALID` (signature bad, or signed with a `key_id` that is not registered), `CART_EXPIRED`, `MERCHANT_MISMATCH` (the cart is from a different merchant than the proposal names), `PRICE_MISMATCH` (the proposal disagrees with the signed cart on unit price, quantity, total or currency, the product `sku` is not in the cart, or the cart total is not the sum of its lines; the signed cart is the source of truth), `MERCHANT_NOT_ALLOWED` (the cart's or the proposal's merchant is not in the mandate), `CURRENCY_MISMATCH` (cart vs mandate currency), `OVER_BUDGET` (cart total > remaining budget; remaining already subtracts reservations), `DUPLICATE`, then `CATEGORY_MISMATCH`. The decision `id` is `dec_<proposal id>`.
- **Duplicates and idempotency**: sending the same proposal `id` again returns the earlier decision unchanged and writes nothing new (409 `PROPOSAL_ID_CONFLICT` if the body differs). A different proposal with the same mandate, merchant and `cart_id` within 60 seconds of one that was allowed (or is still being decided) is blocked as `DUPLICATE`.
- **Semantic check**: runs only when every rule above passed, and can only turn ALLOW into BLOCK (`CATEGORY_MISMATCH`, model confidence >= 0.8). It sees only the mandate category and description and each cart line's sku/title/variant (never the rationale or anything else from the agent). Without a Groq key, on timeout, or on a rate limit it uses a keyword matcher and the explanation ends with `(semantic check: fallback)`.
- **Advisories**: size or colour differences between the mandate description and the cart (e.g. mandate size 9, cart size 10) do NOT block. The verdict stays ALLOW; the issues are added to the explanation and stored as `data.advisories` (a list of strings, empty if none) in the `DECISION_MADE` ledger entry, whose `data` also has `mandate_id, proposal_id, verdict, reasons, explanation, amount_minor, currency, semantic_source`. `PROPOSAL_RECEIVED` is written before evaluation, `DECISION_MADE` after. `/v1/guardian/check` is a dry run and writes neither.
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

**Retries and unknown outcomes.** Timeouts, 429 and 5xx are retried up to 3 times with exponential backoff and the same `PayPal-Request-Id`, so PayPal replays rather than duplicates. Other 4xx fail at once. If the answer is lost after an order exists, the engine reads the order from PayPal and records what PayPal says. If the request failed with no response and no order id is known, the payment stays `RESERVED` (budget held, internal `paypal_status=UNKNOWN`) for manual review rather than releasing budget on a guess.

**Restart recovery.** On startup (real mode) every `RESERVED` payment is reconciled: with an order id the order is read and finalized as `PAID` or `FAILED`; without one it becomes `FAILED` ("interrupted before order creation") with `PAYMENT_FAILED` and the reservation released. Known risk: a crash after PayPal accepted the create but before the order id was saved is reconciled as "interrupted", so look the order up by `custom_id` if in doubt.

**Refunds.** `POST /v1/payments/{id}/refund` is a full refund of a `PAID` payment (`refund_capture`, idempotency key `idem-{proposal_id}-refund`). The payment becomes `REFUNDED` and `REFUNDED` is appended to the ledger. A second call returns the same record and does nothing. A payment that is not `PAID` (including `FAILED`) is 409 `PAYMENT_NOT_REFUNDABLE`; unknown id 404; PayPal rejecting the refund is 502 `REFUND_FAILED` and the payment stays `PAID`. **Budget rule: a refund gives the spent amount back to the mandate** (`spent_minor` decreases), so a refunded $65 purchase leaves the full $80 available again.

**Filters.** `GET /v1/payments` accepts `mandate_id`, `status`, `paypal_order_id`, `paypal_capture_id` (combinable).

**Evidence verification.** `GET /v1/evidence/{payment_id}` returns the `EvidenceBundle`: mandate, proposal, signed cart, decision (advisories are included in `explanation`), payment, and every ledger entry for the mandate, proposal, decision and payment, in `seq` order. `verification` is computed live and `ok` is true only if all three hold: the ledger chain verifies (hashes, links, signatures), the mandate signature verifies, and the cart signature verifies against the registered merchant key. `checked` and `first_broken_seq` come from the ledger check; `reason` lists every failed check. The contract has no per-check flags, so a mandate or cart failure shows only in `ok` and `reason`.

**Configuration.** PayPal credentials and the sandbox buyer card (`PAYPAL_CARD_NUMBER`, `PAYPAL_CARD_EXPIRY`, `PAYPAL_CARD_CVV`, `PAYPAL_CARD_NAME`) are read only from the environment. They are never written to the database, ledger, logs or API responses.

## Signing carts (for System 1)

The Guardian verifies every cart against the public key the store registered, so a store must sign exactly the way the Guardian checks. Use the shared helper, do not re-implement it:

```python
from gawah_contract.carts import generate_keypair, public_key_pem, sign_cart
key = generate_keypair()                                   # once; keep the private key out of git
cart = sign_cart(cart_dict, key, key_id="demo-shoe-store-key-1")   # returns a SignedCart with key_id + signature set
```

Register the public key once: `POST /v1/merchants {"merchant_id","key_id","alg":"ES256","public_key_pem": <PEM or JWK JSON string>}`. Only ES256 (EC P-256) keys are accepted; `alg` must be `ES256` in real mode. A `(merchant_id, key_id)` pair is write-once (a different key under the same id is 409 `KEY_ID_CONFLICT`); to rotate, register a new `key_id` and sign new carts with it. `GET /v1/merchants/{id}/public-key` returns the most recently registered key.

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
| `GET /v1/ledger/verify` | `{ok, checked, first_broken_seq, reason}` (`reason` is null when ok) |
| `GET /v1/evidence/{payment_id}` | Full bundle for Dispute Counsel: mandate, proposal, signed cart, decision, payment, ledger entries, verification |
| `POST /v1/merchants`, `GET /v1/merchants/{id}/public-key` | Register / fetch a store's public key (`MerchantKey`) |
| `GET /health` | `{"status":"ok","service":"gawah"}` |

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
