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

Every error has this shape (`ErrorResponse`): HTTP 401 bad key, 404 not found, 422 invalid body, 501 real mode not built yet.

```json
{"error": {"code": "UNAUTHORIZED", "message": "...", "details": null, "request_id": "req_ab12cd34ef56"}}
```

## The 7 objects

- **Mandate**: `{"id":"man_01","user_id":"user_parrv","category":"footwear","item_description":"Black running shoes, size 9, under $80","max_amount_minor":8000,"currency":"USD","allowed_merchants":["demo-shoe-store"],"issued_at":"2026-11-09T10:00:00Z","expires_at":"2026-11-16T10:00:00Z","status":"active","signature":"...","signing_alg":"ed25519","key_id":"gawah-ledger-key-1"}` (status: active, revoked, expired)
- **SignedCart** (made and signed by the store): `{"cart_id":"cart_01","merchant_id":"demo-shoe-store","line_items":[{"sku":"SHOE-BLK-RUN","title":"Black Runner","variant":"size 9","quantity":1,"unit_price_minor":6500}],"total_minor":6500,"currency":"USD","expires_at":"...","signature":"...","key_id":"demo-shoe-store-key-1"}`
- **PurchaseProposal** (System 1 sends): `id, mandate_id, agent_id, merchant_id, product{id,title,url}, quantity, unit_price_minor, currency, rationale, signed_cart, created_at`. See `contract/fixtures/proposal_shoes.json`.
- **GuardianDecision**: `{"id":"dec_02","proposal_id":"prop_02","verdict":"BLOCK","reasons":["OVER_BUDGET"],"explanation":"...","checked_at":"..."}`. Reason codes: OVER_BUDGET, MERCHANT_NOT_ALLOWED, CART_SIGNATURE_INVALID, MANDATE_EXPIRED, MANDATE_REVOKED, CURRENCY_MISMATCH, CATEGORY_MISMATCH, DUPLICATE, PRICE_MISMATCH.
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
| `POST /v1/proposals` | Guardian, then payment, then ledger. Returns `{decision, payment or null}` |
| `POST /v1/guardian/check` | Dry run: decision only, no payment, no ledger write |
| `GET /v1/payments`, `GET /v1/payments/{id}` | List / fetch |
| `POST /v1/payments/{id}/refund` | Refund |
| `GET /v1/ledger?mandate_id=&subject_id=` | Ledger entries |
| `GET /v1/ledger/verify` | `{ok, checked, first_broken_seq}` |
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
