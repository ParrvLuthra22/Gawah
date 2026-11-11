# Gawah manual end-to-end testing guide

This guide covers the actual local FastAPI services in D:\Python Workspace\Gawah\Gawah. It was checked against the live OpenAPI schemas for Shopper, Engine, and Counsel.

> **Payment warning:** POST /v1/proposals is payment-capable in Engine real mode. It can reserve mandate budget, create a PayPal Sandbox order, and capture it. Do not call that request unless separate transaction-specific approval covers the exact fresh proposal, demo-sports, NIKE-SWIFTRUN-12-BLK, quantity 1, and 5999 USD minor units.

## 1. URLs, authentication, and demo values

| Service | Base URL | Swagger UI | Header |
|---|---|---|---|
| System 1 Shopper | http://127.0.0.1:8002 | http://127.0.0.1:8002/docs | none |
| System 2 Engine | http://127.0.0.1:8000 | http://127.0.0.1:8000/docs | X-Gawah-Key on every /v1 route |
| System 3 Counsel | http://127.0.0.1:8003 | http://127.0.0.1:8003/docs | X-Gawah-Key on every /api route except /api/health |

| Demo field | Required value |
|---|---|
| Product ID | shoe-001 |
| Merchant | demo-sports |
| SKU and proposal product ID | NIKE-SWIFTRUN-12-BLK |
| Quantity | 1 |
| Amount | 5999 USD minor units ($59.99) |
| Category | running shoes |

Use SYSTEM1_KEY for Engine merchant registration and payment proposal submission. Use SYSTEM3_KEY for mandates, Guardian dry-runs, payments, ledger, and Counsel API routes. System 1 has no inbound API key.

### Exact Swagger operation links

Use these exact operation links when following the requests below. They are generated from each
service's current OpenAPI operation IDs.

| Request | Swagger operation |
|---|---|
| System 1 GET /health | http://127.0.0.1:8002/docs#/default/health_health_get |
| System 1 POST /shopping/intent | http://127.0.0.1:8002/docs#/default/extract_shopping_intent_shopping_intent_post |
| System 1 POST /shopping/discover | http://127.0.0.1:8002/docs#/default/discover_products_shopping_discover_post |
| System 1 POST /shopping/agent | http://127.0.0.1:8002/docs#/default/run_shopping_agent_shopping_agent_post |
| System 1 POST /shopping/demo/checkout | http://127.0.0.1:8002/docs#/default/demo_checkout_shopping_demo_checkout_post |
| System 2 GET /health | http://127.0.0.1:8000/docs#/default/health_health_get |
| System 2 POST /v1/mandates | http://127.0.0.1:8000/docs#/mandates/create_mandate_v1_mandates_post |
| System 2 GET /v1/mandates/{mandate_id} | http://127.0.0.1:8000/docs#/mandates/get_mandate_v1_mandates__mandate_id__get |
| System 2 GET /v1/mandates/{mandate_id}/budget | http://127.0.0.1:8000/docs#/mandates/get_budget_v1_mandates__mandate_id__budget_get |
| System 2 POST /v1/merchants | http://127.0.0.1:8000/docs#/merchants/register_merchant_v1_merchants_post |
| System 2 GET merchant public key | http://127.0.0.1:8000/docs#/merchants/get_public_key_v1_merchants__merchant_id__public_key_get |
| System 2 POST /v1/guardian/check | http://127.0.0.1:8000/docs#/proposals/guardian_check_v1_guardian_check_post |
| System 2 POST /v1/proposals | http://127.0.0.1:8000/docs#/proposals/submit_proposal_v1_proposals_post |
| System 2 GET payments | http://127.0.0.1:8000/docs#/payments/list_payments_v1_payments_get |
| System 2 GET payment | http://127.0.0.1:8000/docs#/payments/get_payment_v1_payments__payment_id__get |
| System 2 GET /v1/ledger | http://127.0.0.1:8000/docs#/ledger/list_ledger_v1_ledger_get |
| System 2 GET /v1/ledger/verify | http://127.0.0.1:8000/docs#/ledger/verify_ledger_v1_ledger_verify_get |
| System 3 GET /api/health | http://127.0.0.1:8003/docs#/default/health_api_health_get |
| System 3 GET /api/dashboard/payments | http://127.0.0.1:8003/docs#/dashboard/payment_ledger_api_dashboard_payments_get |
| System 3 POST /api/evidence/{payment_id}/assemble | http://127.0.0.1:8003/docs#/default/assemble_evidence_api_evidence__payment_id__assemble_post |
| System 3 GET evidence pack | http://127.0.0.1:8003/docs#/default/evidence_pack_api_evidence__payment_id__get |
| System 3 GET evidence verification | http://127.0.0.1:8003/docs#/default/evidence_verification_api_evidence__payment_id__verification_get |
| System 3 GET evidence issues | http://127.0.0.1:8003/docs#/default/evidence_issues_api_evidence__payment_id__issues_get |
| System 3 GET payment-ledger verification | http://127.0.0.1:8003/docs#/dashboard/payment_verification_api_ledger_payments__payment_id__verification_get |

Set session variables without printing them:

~~~powershell
$env:SYSTEM1_KEY = Read-Host 'Paste SYSTEM1_KEY'
$env:SYSTEM3_KEY = Read-Host 'Paste SYSTEM3_KEY'
$HeadersS1 = @{ 'X-Gawah-Key' = $env:SYSTEM1_KEY; 'Content-Type' = 'application/json' }
$HeadersS3 = @{ 'X-Gawah-Key' = $env:SYSTEM3_KEY; 'Content-Type' = 'application/json' }
~~~

Never commit values, response dumps, local databases, key directories, or proposal JSON.

## 2. Start the services

Open three PowerShell windows.

### System 2 Engine, port 8000

~~~powershell
Set-Location 'D:\Python Workspace\Gawah\Gawah'
uv run uvicorn gawah_engine.main:app --reload --app-dir engine --port 8000
~~~

### System 1 Shopper, port 8002

~~~powershell
Set-Location 'D:\Python Workspace\Gawah\Gawah'
uv run --package gawah-shopper uvicorn shopper.main:app --app-dir system1 --port 8002
~~~

### System 3 Counsel, port 8003

The repository Makefile has an older Counsel port. Use 8003 here.

~~~powershell
Set-Location 'D:\Python Workspace\Gawah\Gawah\system3'
uv run --no-project --with-requirements requirements.txt --with-editable ../contract python -m alembic upgrade head
uv run --no-project --with-requirements requirements.txt --with-editable ../contract python -m uvicorn counsel.main:app --port 8003
~~~

### Health checks

| Request | Swagger URL | Header | Expected |
|---|---|---|---|
| GET /health, System 1 | http://127.0.0.1:8002/docs | none | status ok, system system1 |
| GET /health, System 2 | http://127.0.0.1:8000/docs | none | mock false, paypal sandbox, card_configured true |
| GET /api/health, System 3 | http://127.0.0.1:8003/docs | none | status ok, service gawah-system3 |

~~~powershell
Invoke-RestMethod http://127.0.0.1:8002/health
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8003/api/health
~~~

Stop if Engine is mock mode, is not sandbox, or does not report valid card configuration.

## 3. Confirm the trusted merchant

### Existing merchant key

**Request:** GET /v1/merchants/demo-sports/public-key
**Swagger:** http://127.0.0.1:8000/docs
**Header:** X-Gawah-Key: SYSTEM1_KEY
**Body:** none
**Expected:** HTTP 200 and merchant_id demo-sports.

~~~powershell
$merchantKey = Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:8000/v1/merchants/demo-sports/public-key' -Headers $HeadersS1
$merchantKey.merchant_id
~~~

If the result is 200, proceed. If a new database returns 404, register the actual public key once.

### Register only for a new local database

**Request:** POST /v1/merchants
**Swagger:** http://127.0.0.1:8000/docs
**Header:** X-Gawah-Key: SYSTEM1_KEY
**Body schema:** MerchantKey with merchant_id, key_id, alg, and public_key_pem.

Do not invent a PEM and do not use a private key. Generate the current demo adapter public key:

~~~powershell
Set-Location 'D:\Python Workspace\Gawah\Gawah'
$merchantKeyJson = uv run --package gawah-shopper python -c "from shopper.config import Settings; from shopper.merchant.demo_merchant import DemoMerchantAdapter; print(DemoMerchantAdapter(Settings()).merchant_key('demo-sports').model_dump_json())"
$merchantKey = $merchantKeyJson | ConvertFrom-Json
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/v1/merchants' -Headers $HeadersS1 -Body ($merchantKey | ConvertTo-Json -Depth 8)
~~~

The generated body has this valid shape, but the PEM must be the generated current public key:

~~~json
{
  "merchant_id": "demo-sports",
  "key_id": "demo-merchant-cart",
  "alg": "ES256",
  "public_key_pem": "-----BEGIN PUBLIC KEY-----\nCURRENT_GENERATED_PUBLIC_KEY\n-----END PUBLIC KEY-----\n"
}
~~~

Expected: HTTP 201. A 409 key conflict is a safety failure: do not overwrite the existing key.

## 4. Create a signed mandate

**Request:** POST /v1/mandates
**Swagger:** http://127.0.0.1:8000/docs
**Header:** X-Gawah-Key: SYSTEM3_KEY
**Body schema:** MandateCreate. Engine generates id, issued_at, signature, signing_alg, and key_id.

Ready-to-paste Swagger example. Set expires_at to a real future RFC 3339 time:

~~~json
{
  "user_id": "manual-demo-user",
  "category": "running shoes",
  "item_description": "Nike SwiftRun 12 black running shoes, size 12, quantity 1, SKU NIKE-SWIFTRUN-12-BLK",
  "max_amount_minor": 5999,
  "currency": "USD",
  "allowed_merchants": ["demo-sports"],
  "expires_at": "2030-12-31T12:00:00Z"
}
~~~

PowerShell creates a fresh 30-minute mandate:

~~~powershell
$expiresAt = (Get-Date).ToUniversalTime().AddMinutes(30).ToString('o')
$mandateBody = @{ user_id='manual-demo-user'; category='running shoes'; item_description='Nike SwiftRun 12 black running shoes, size 12, quantity 1, SKU NIKE-SWIFTRUN-12-BLK'; max_amount_minor=5999; currency='USD'; allowed_merchants=@('demo-sports'); expires_at=$expiresAt }
$mandate = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/v1/mandates' -Headers $HeadersS3 -Body ($mandateBody | ConvertTo-Json -Depth 6)
$MANDATE_ID = $mandate.id
$MANDATE_ID
~~~

Expected: HTTP 201 and a signed active Mandate. Do not provide or alter the signature.

**Request:** GET /v1/mandates/{mandate_id}
**Swagger:** http://127.0.0.1:8000/docs
**Header:** SYSTEM3_KEY
**Body:** none

~~~powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/mandates/$MANDATE_ID" -Headers $HeadersS3
~~~

Verify status active, max_amount_minor 5999, currency USD, and allowed_merchants ["demo-sports"]. Copy only the id for the next request.

## 5. Enter the shopping prompt and obtain the signed proposal

Use this exact natural-language prompt:

~~~text
Buy one pair of black Nike SwiftRun 12 running shoes, size 12, from demo-sports, for no more than $59.99 USD.
~~~

### Optional System 1 diagnostics

Each request has no header and this ready-to-paste body:

~~~json
{
  "message": "Buy one pair of black Nike SwiftRun 12 running shoes, size 12, from demo-sports, for no more than $59.99 USD."
}
~~~

| Request | Swagger URL | Expected use |
|---|---|---|
| POST /shopping/intent | http://127.0.0.1:8002/docs | Intent extraction only |
| POST /shopping/discover | http://127.0.0.1:8002/docs | Intent plus deterministic filtering |
| POST /shopping/agent | http://127.0.0.1:8002/docs | General configured-provider recommendations |

Expected intent constraints include running shoes, Nike, black, size 12, 59.99, and USD. Do not use an unverified third-party listing to create a demo merchant cart.

~~~powershell
$prompt = 'Buy one pair of black Nike SwiftRun 12 running shoes, size 12, from demo-sports, for no more than $59.99 USD.'
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8002/shopping/intent' -ContentType 'application/json' -Body (@{ message=$prompt } | ConvertTo-Json)
~~~

### Primary proposal path

**Request:** POST /shopping/demo/checkout
**Swagger:** http://127.0.0.1:8002/docs
**Header:** none
**Body schema:** DemoShoppingRequest.

Swagger body:

~~~json
{
  "message": "Buy one pair of black Nike SwiftRun 12 running shoes, size 12, from demo-sports, for no more than $59.99 USD.",
  "mandate_id": "PASTE_MANDATE_ID",
  "agent_id": "manual-demo-agent",
  "quantity": 1
}
~~~

PowerShell:

~~~powershell
$checkoutBody = @{ message=$prompt; mandate_id=$MANDATE_ID; agent_id='manual-demo-agent'; quantity=1 }
$checkout = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8002/shopping/demo/checkout' -ContentType 'application/json' -Body ($checkoutBody | ConvertTo-Json -Depth 8)
$checkout.purchase_ready
$checkout.agent.selected_product.id
$proposal = $checkout.proposal
$proposal | ConvertTo-Json -Depth 20 | Set-Content -Encoding utf8 .\manual-proposal.json
~~~

Expected: HTTP 200, purchase_ready true, selected product shoe-001, and a non-null proposal.

Before Guardian, verify the returned values:

- proposal.mandate_id equals MANDATE_ID.
- proposal.merchant_id is demo-sports.
- proposal.product.id and signed_cart.line_items[0].sku are both NIKE-SWIFTRUN-12-BLK.
- proposal.quantity is 1, proposal.unit_price_minor is 5999, and proposal.currency is USD.
- signed_cart.total_minor is 5999; signature is non-empty; expires_at remains future.

Copy the **entire** runtime proposal object. Do not create a replacement object by hand: cart ID, proposal ID, timestamps, expiry, signature, and key ID must be generated by the merchant. Do not commit manual-proposal.json.

## 6. Guardian authorized dry-run

**Request:** POST /v1/guardian/check
**Swagger:** http://127.0.0.1:8000/docs
**Header:** X-Gawah-Key: SYSTEM3_KEY
**Body:** the entire unchanged PurchaseProposal returned in section 5.

There is no static safe body for this request because it would require a real current cart signature. In Swagger paste the exact proposal object. In PowerShell:

~~~powershell
$guardianDecision = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/v1/guardian/check' -Headers $HeadersS3 -Body ($proposal | ConvertTo-Json -Depth 20)
$guardianDecision.verdict
$guardianDecision.reasons
~~~

Expected: HTTP 200, verdict ALLOW, normally with no reasons. A 403 when using SYSTEM1_KEY here is expected role separation.

Optional tamper regression: make a separate copy, change signed_cart.total_minor and unit_price_minor to 8999, retain the old signature, and send only that copy to this dry-run. Expected BLOCK with CART_SIGNATURE_INVALID, PRICE_MISMATCH, and OVER_BUDGET. Never submit the altered copy for payment.

## 7. Payment submission — stop gate

> **Separate approval required.** POST /v1/proposals uses SYSTEM1_KEY and can create/capture a PayPal Sandbox payment. Do not execute it from this guide unless the user separately authorizes the exact fresh proposal and amount.

**Request:** POST /v1/proposals
**Swagger:** http://127.0.0.1:8000/docs
**Header:** X-Gawah-Key: SYSTEM1_KEY
**Body:** the same exact unchanged proposal which returned ALLOW from section 6.

The valid request body is the generated proposal, not fabricated JSON. If approval has been granted, execute it **once only**:

~~~powershell
# PAYMENT-CAPABLE: one submission only after transaction-specific approval.
$submission = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/v1/proposals' -Headers $HeadersS1 -Body ($proposal | ConvertTo-Json -Depth 20)
$submission.decision.verdict
$submission.payment | Select-Object id,status,amount_minor,currency,paypal_order_id,paypal_capture_id,failure_reason
$PAYMENT_ID = $submission.payment.id
~~~

Expected response schema: ProposalResult with decision and payment. HTTP 200 or Guardian ALLOW does not prove capture. Record payment.id, status, paypal_order_id, paypal_capture_id, and failure_reason.

On timeout, ambiguous response, RESERVED state, or FAILED state: do not retry. Continue to the read-only verification requests below.

## 8. Verify payment, budget, and Engine ledger

All requests in this section use SYSTEM3_KEY.

### Payment record

**Request:** GET /v1/payments/{payment_id}
**Swagger:** http://127.0.0.1:8000/docs
**Header:** SYSTEM3_KEY
**Body:** none

~~~powershell
$payment = Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/payments/$PAYMENT_ID" -Headers $HeadersS3
$payment | Select-Object id,proposal_id,status,amount_minor,currency,paypal_order_id,paypal_capture_id,failure_reason
~~~

Payment success requires status PAID, amount_minor 5999, currency USD, and a capture ID. An order ID alone is not success.

For an ambiguous submission, list persisted records without resubmission:

**Request:** GET /v1/payments?mandate_id={mandate_id}
**Swagger:** http://127.0.0.1:8000/docs
**Header:** SYSTEM3_KEY
**Body:** none

~~~powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/payments?mandate_id=$MANDATE_ID" -Headers $HeadersS3
~~~

### Mandate budget

**Request:** GET /v1/mandates/{mandate_id}/budget
**Swagger:** http://127.0.0.1:8000/docs
**Header:** SYSTEM3_KEY
**Body:** none

~~~powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/mandates/$MANDATE_ID/budget" -Headers $HeadersS3
~~~

For a completed 5999-minor-unit purchase, expect spent_minor 5999, reserved_minor 0, remaining_minor 0. For a terminal failure before capture, expect spent 0, reserved 0, remaining 5999.

### Ledger

**Request:** GET /v1/ledger/verify
**Swagger:** http://127.0.0.1:8000/docs
**Header:** SYSTEM3_KEY
**Body:** none
**Expected:** ok true.

~~~powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:8000/v1/ledger/verify' -Headers $HeadersS3
Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/ledger?mandate_id=$MANDATE_ID" -Headers $HeadersS3
~~~

The filtered ledger should include mandate creation, payment-started, and then payment-completed or payment-failed activity.

## 9. Retrieve evidence through System 3

System 3 uses SYSTEM3_KEY. Evidence assembly does not create a payment or submit a PayPal dispute.

### Counsel payment list

**Request:** GET /api/dashboard/payments
**Swagger:** http://127.0.0.1:8003/docs
**Header:** SYSTEM3_KEY
**Body:** none

~~~powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:8003/api/dashboard/payments' -Headers $HeadersS3
~~~

Find PAYMENT_ID and confirm amount 5999 and currency USD.

### Assemble and retrieve an evidence pack

**Request:** POST /api/evidence/{payment_id}/assemble
**Swagger:** http://127.0.0.1:8003/docs
**Header:** SYSTEM3_KEY
**Body:** none
**Query:** optional dispute_id; omit it for payment evidence.

~~~powershell
$evidencePack = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8003/api/evidence/$PAYMENT_ID/assemble" -Headers $HeadersS3
$evidencePack.payment_id
$evidencePack.verification_summary
~~~

Expected: EvidencePackResponse with evidence, timeline, verification_summary, and any missing/invalid-evidence lists. A failed pre-order payment can legitimately have missing evidence or no pack. Report it; do not create another payment.

**Request:** GET /api/evidence/{payment_id}
**Swagger:** http://127.0.0.1:8003/docs
**Header:** SYSTEM3_KEY
**Body:** none

~~~powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8003/api/evidence/$PAYMENT_ID" -Headers $HeadersS3
~~~

Additional read-only verification routes are GET /api/evidence/{payment_id}/verification, GET /api/evidence/{payment_id}/issues, and GET /api/ledger/payments/{payment_id}/verification. Each uses SYSTEM3_KEY and has no body.

## 10. Transfer checklist and safe interpretation

1. Copy mandate.id into MANDATE_ID.
2. Pass MANDATE_ID to System 1 demo checkout.
3. Copy checkout.proposal whole and unchanged.
4. Use SYSTEM3_KEY for the Guardian dry-run. Continue only on ALLOW.
5. Only with separate approval use SYSTEM1_KEY to submit the same proposal once.
6. Copy submission.payment.id to PAYMENT_ID; use SYSTEM3_KEY for all subsequent Engine/Counsel reads.

| Result | Meaning |
|---|---|
| Guardian ALLOW | Authorization passed; it is not a payment success. |
| Guardian BLOCK | Do not submit. Correct the cause and start a fresh mandate/proposal. |
| Payment PAID plus capture ID | Payment success; verify budget, ledger, and evidence. |
| Payment FAILED | No success claim. Inspect the persisted record and ledger; no blind retry. |
| Payment RESERVED or timeout | Do not retry. Reconcile existing records first. |
| Missing evidence pack | Do not fabricate evidence; inspect missing-evidence diagnostics. |
| System 1 gets 403 on Guardian dry-run | Expected; System 1 is not allowed that route. |

## 11. Cleanup

Do not commit .env, private keys, local databases, manual-proposal.json, response dumps, or keys. Do not recreate signing keys while a cart/mandate is active. Never create an additional payment merely to test replay; use the existing automated security tests for replay and idempotency.
