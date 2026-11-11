# Gawah dispute console

The System 3 console shows PayPal Sandbox disputes, linked payment references, status history, and sync results.

1. Start the [System 3 backend](../system3/counsel/README.md) on port 8002.
2. In `web/`, copy `.env.example` to `.env.local` and set `SYSTEM3_KEY` to the backend's value. The Next.js server uses `SYSTEM3_API_URL` to forward requests to FastAPI.
3. Run the following with Node.js 20.19+.

```sh
npm ci
npm run dev
```

Open [the local console](http://127.0.0.1:3000). To check the frontend, run `npm test`, `npm run lint`, and `npm run build`. The local server binds to loopback. A shared deployment requires its own user access controls.

## AI Shopper (`/shop`)

A natural-language shopping flow inside the dashboard: intent and recommendations (System 1) → select an eligible product →
approve spending limits → System 2 creates and signs the mandate → System 1 fetches the merchant-signed cart and builds the
proposal → Guardian dry run → you confirm → the engine's Guardian decides again and pays through PayPal **Sandbox** →
ledger, budget and Counsel evidence.

- Only products sold by the controlled demo merchant (price and stock matching the merchant's own catalog, status VERIFIED)
  can be checked out. Channel3 and other results are shown but never purchasable.
- Server routes (`app/api/shop/*`) hold the keys and the proposals; the browser sends only ids and the confirmation.
  `SYSTEM1_KEY` is used only to submit the proposal; `SYSTEM3_KEY` only for the mandate and the dry run. There is no refund route.
- Duplicate protection: a prepared proposal is claimed once, synchronously. A timeout or 5xx answer is reported as
  "outcome unknown" and is never retried; check the Ledger.
- **Limitation:** the dashboard has no login, so these routes work only on the local dev server (`npm run dev`) at
  localhost/127.0.0.1 with `ENABLE_SHOPPING_CHECKOUT=true`. They are not for the hosted deployment.
- The proposal store is in memory (30 min); restarting the web server discards pending proposals.

Start (four terminals; keys must match across `.env` files):

```sh
make dev-engine                                   # engine :8000
uv run python scripts/register_merchant_key.py    # once: registers the demo merchant key with the engine
uv run python -m uvicorn shopper.main:app --port 8001   # shopper, from the repo root (needs GROQ_API_KEY; see docs/SYSTEM1_API.md)
make dev-counsel                                  # Counsel :8002
make dev-web                                      # dashboard :3000 -> http://localhost:3000/shop
```
