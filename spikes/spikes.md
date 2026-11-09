# Spikes

| Spike | Question | Status | Result | Fallback used |
|-------|----------|--------|--------|---------------|
| A1 | Can the agent pay a sandbox order without a human clicking? | blocked (route 1 failed, route 2 not run) | Route 1 (card payment_source, one call): 422 `PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING` (debug_id f971324f1f9f5). Sandbox business account/app has no Advanced Card Payments. Route 2 (headless Playwright buyer approval) is implemented (`pay_order` in `paypal.py`, `paypal_browser.py`, `spikes/a1_browser.py`) but not run: needs a sandbox PERSONAL buyer in `.env` (BUYER_EMAIL / BUYER_PASSWORD). Fix for route 1 instead: enable Advanced Credit and Debit Card Payments on the sandbox business account in the PayPal dashboard. | none yet |
| A2 | Does the remote PayPal MCP accept our sandbox token? | pass (SSE only) | Token accepted by `https://mcp.sandbox.paypal.com/sse` (Bearer header); 42 tools listed incl. create_order, get_order, pay_order, create_refund, list_disputes, get_dispute, accept_dispute_claim. `get_order` (arg `id`) returned an unpaid REST-created order with custom_id intact. Streamable HTTP at `/http` fails: MCPError Not Found. Script: `spikes/a2_mcp.py`. | Engine stays on plain REST (`PayPalClient`); MCP is optional |
| A3 | Can we attach a ledger reference via custom_id? | partial | custom_id set at create_order is returned by MCP `get_order` at `purchase_units[0].custom_id` on an unpaid order. Not yet verified on a paid order or in the capture response (needs A1). Check is built into `a1_pay_order.py` / `a1_browser.py`. | n/a |
| B1 | Can we create a sandbox dispute? | todo | | |
| B2 | Does provide-evidence work? | todo | | |
| B3 | Can we settle a dispute? | todo | | |
| C1 | Does the AP2 SDK install and verify a mandate? | todo | | |
| D1 | Does Channel3 return real products? | todo | | |
| D2 | Does a free-tier LLM survive 10 demo runs? | todo | | |
