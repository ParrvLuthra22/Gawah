# GAWAH final PayPal Sandbox end-to-end execution report

**Execution date:** 2026-11-12, Asia/Kolkata
**Git revision:** f0e4696 on branch suvir (the recorded payment was completed before this
post-merge verification)
**Overall result:** **PASS** for one authorized Sandbox purchase
**Payment verdict:** **PAYMENT SUCCESS**

## A. Executive summary

One explicitly authorized purchase for demo-sports, Nike SwiftRun 12, SKU
NIKE-SWIFTRUN-12-BLK, quantity 1, and 5999 USD minor units completed through the
real-mode Engine. System 1 created the merchant-signed proposal, Guardian allowed it,
PayPal Sandbox produced an order and capture, the Engine marked the payment PAID, the
mandate budget settled, the ledger verified, and System 3 assembled verified evidence.

No production PayPal endpoint was used. No retry or second payment attempt occurred. No
PayPal dispute was created or submitted.

## B. Environment and preflight

| Component | Port | Result |
|---|---:|---|
| System 1 Shopper | 8002 | GET /health returned 200 |
| System 2 Engine | 8000 | GET /health returned 200; mock false, Sandbox, card configured |
| System 3 Counsel | 8003 | GET /api/health returned 200 |
| PayPal OAuth | Engine internal client | HTTP 200; token obtained without logging it |
| Counsel PayPal probe | System 3 | credentials configured, connection CONNECTED, environment sandbox |
| Demo merchant registry | Engine | demo-sports key found; algorithm ES256 |
| Ledger preflight | Engine | verify returned ok true |

The root and System 3 settings were checked for non-empty, distinct service keys. Database
readiness was demonstrated by mandate creation, merchant lookup, payment persistence, and
ledger reads. Private signing keys, card values, tokens, and credentials were neither output nor
included in this report.

## C. Transaction timeline

| Step | Actual API/function | HTTP | Result |
|---|---|---:|---|
| 1 | GET System 1 health | 200 | Shopper ready |
| 2 | GET System 2 health | 200 | Real mode, Sandbox, card configured |
| 3 | GET System 3 health | 200 | Counsel ready |
| 4 | PayPal OAuth token preflight | 200 | Sandbox credentials accepted |
| 5 | GET merchant public key with System 1 key | 200 | demo-sports trusted ES256 key present |
| 6 | POST /v1/mandates with System 3 key | 201 | Fresh active, signed 5999-USD mandate |
| 7 | POST /shopping/demo/checkout | 200 | System 1 chose shoe-001 and produced a signed proposal |
| 8 | GET /v1/payments before submission | 200 | No record for the fresh proposal |
| 9 | POST /v1/guardian/check with System 3 key | 200 | ALLOW; dry run did not spend funds |
| 10 | POST /v1/proposals with System 1 key | 200 | One authorized Sandbox submission |
| 11 | GET payment, mandate budget, ledger | 200 | PAID, budget settled, ledger valid |
| 12 | POST System 3 evidence assembly | 200 | Evidence pack version 1 assembled |
| 13 | GET System 3 evidence verification | 200 | Verification summary returned |

## D. Mandate, product, cart, and proposal

A fresh future-expiry mandate was created for:

- Merchant restriction: demo-sports
- Category: running shoes
- Maximum: 5999 USD minor units
- Product description: Nike SwiftRun 12 black running shoes, size 12, quantity 1
- Mandate signature: verified by Engine
- Sanitized mandate ID: man_16cd…

System 1 received the exact user request:

> Buy one pair of black Nike SwiftRun 12 running shoes, size 12, from demo-sports, for no more than $59.99 USD.

Observed System 1 result:

| Field | Actual value |
|---|---|
| Selected catalog product | shoe-001 |
| Merchant | demo-sports |
| Proposal product ID | NIKE-SWIFTRUN-12-BLK |
| Signed cart line-item SKU | NIKE-SWIFTRUN-12-BLK |
| Quantity | 1 |
| Unit price | 5999 USD minor units |
| Cart total | 5999 USD minor units |
| Merchant signature | ES256 signature verified |
| Proposal mandate | fresh mandate above |
| Sanitized proposal ID | prop_a4cb… |

The proposal product ID matched the merchant-authoritative signed-cart SKU. This confirms the
previous PRICE_MISMATCH integration issue is not present in this successful flow.

## E. Guardian authorization

The System 3-key Guardian dry run returned HTTP 200 with verdict ALLOW and no blocking reasons.

The payment submission used the System 1 key, not the System 3 key. System 1 remains correctly
forbidden from calling the Guardian dry-run route directly. The valid proposal was evaluated against
the mandate signature, merchant registration, cart ES256 signature, cart expiry, SKU consistency,
merchant restriction, quantity, currency, and 5999-minor-unit budget.

## F. PayPal Sandbox payment

One and only one payment-capable POST /v1/proposals request was sent for the fresh proposal.

| Field | Actual result |
|---|---|
| Submission HTTP status | 200 |
| Guardian decision | ALLOW |
| Environment | PayPal Sandbox only |
| Amount | 5999 USD minor units |
| Engine payment state | PAID |
| PayPal order | created; ID retained in local Engine record |
| PayPal capture | created; ID retained in local Engine record |
| Order/capture identifiers in this report | intentionally sanitized |
| Payment failure reason | none |
| Retry count | zero additional attempts |

A successful order alone was not treated as success. The final Engine payment state was PAID and
the payment record contained both order and capture identifiers. The identifiers are intentionally
not reproduced in this tracked-style report; they are available from the local authenticated
GET /v1/payments/{payment_id} response.

## G. Budget, ledger, idempotency, and reconciliation

| Check | Actual result |
|---|---|
| Payment lookup | one matching persisted payment record |
| Amount/currency | 5999 USD minor units |
| Mandate spent | 5999 |
| Mandate reserved | 0 |
| Mandate remaining | 0 |
| Ledger verification | ok true, 13 entries checked |
| Mandate-specific ledger events | MANDATE_CREATED, PROPOSAL_RECEIVED, DECISION_MADE, PAYMENT_STARTED, PAYMENT_COMPLETED |
| Submission duplicate prevention | fresh proposal had no existing payment record before submission |

The proposal was submitted once. No replay POST was made. Existing regression tests cover
idempotency and replay without risking a second Sandbox charge.

## H. System 3 evidence results

System 3 authenticated to Engine with its configured role key and found the new payment in
GET /api/dashboard/payments.

POST /api/evidence/{payment_id}/assemble returned HTTP 200:

- payment ID matched the persisted Engine payment
- evidence-pack version: 1
- ledger verification: VERIFIED
- GET /api/evidence/{payment_id}/verification returned the expected mandate, cart, ledger,
  Engine-ledger, and payment-reference verification fields

No PayPal dispute was created or submitted. Counsel evidence retrieval and verification passed for
the completed transaction.

## I. Security and automated tests

These tests are non-live regressions; they did not create additional Sandbox purchases.

| Command / suite | Result |
|---|---|
| Engine Guardian, payment API, security, scopes, hardening regression subset | 171 passed |
| System 1 merchant, demo-workflow, and attack regression subset | 23 passed |
| System 3 dashboard and System 2-client authentication subset | 17 passed; 13 dependency deprecation warnings |
| Ruff | passed |

### Post-merge verification (no payment operation)

After merging the then-current `origin/main` into `suvir`, a fresh non-payment verification
created a short-lived test mandate, obtained a new System 1 proposal, and used only Guardian's
dry-run route. It selected `shoe-001`; the proposal product ID and signed-cart SKU were both
`NIKE-SWIFTRUN-12-BLK`; and the cart total was 5999 USD minor units. Guardian received both
requests successfully and rejected the altered cart with `CART_SIGNATURE_INVALID`,
`PRICE_MISMATCH`, and `OVER_BUDGET`. The existing paid Sandbox record, its Engine evidence, and
its authenticated System 3 verification remained retrievable. No proposal-submission, PayPal
order, capture, refund, or dispute endpoint was called.

| Command / suite | Result |
|---|---|
| `uv run pytest -q` | 511 passed, 1 skipped |
| `uv run --package gawah-shopper pytest system1/tests -q` | 72 passed |
| System 3 documented suite | 87 passed; 13 dependency deprecation warnings |
| In-process integration suite | 15 passed |
| Frontend tests | 7 files / 28 tests passed |
| Frontend lint and TypeScript check | passed |
| Frontend production build | passed |
| `uv run ruff check .` | passed |

The live successful flow independently verified the valid merchant cart and Guardian ALLOW. The
existing test suites cover signature tampering, price mismatch, budget/merchant restrictions,
authority spoofing, replay/idempotency, and ledger integrity.

## J. Errors, root causes, and remaining blockers

No failure occurred in this final transaction.

A prior attempt from a separate run was not reused and is intentionally excluded from this report.
This run used a fresh mandate and fresh proposal after OAuth preflight succeeded.

Remaining operational constraints:

1. PayPal remains Sandbox-only. Production credentials and production URLs were never tested.
2. A new payment attempt requires a new mandate/proposal and fresh explicit authorization.
3. Do not use the successful payment record to test replay by resubmission; use regression tests.
4. Counsel dispute submission was deliberately not exercised because it is outside this authorization.

## K. Files modified during this run

- docs/GAWAH_E2E_EXECUTION_REPORT.md — final payment record and post-merge non-payment verification.
- Local untracked reconciliation artifacts beginning with .final_sandbox_ — generated for local
  reconciliation only; do not commit them.

No application source, shared contracts, Guardian policy, payment code, credentials, or signing keys
were changed. No commit, push, deployment, reset, or database deletion was performed.

## L. Final verdict

| Area | Verdict |
|---|---|
| System 1 shopping and merchant proposal | PASS |
| Merchant ES256 signing | PASS |
| System 2 Guardian authorization | PASS |
| PayPal Sandbox order/capture | PASS |
| Mandate budget accounting | PASS |
| Guardian ledger integrity | PASS |
| System 3 payment/evidence retrieval | PASS |
| Production payment readiness | NOT TESTED |
| Full authorized local Sandbox lifecycle | PASS |
