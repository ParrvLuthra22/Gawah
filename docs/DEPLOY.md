# Deploying the hosted demo on Render

Three web services from one Blueprint (`render.yaml`): **gawah-engine** (System 2), **gawah-counsel**
(System 3 API) and **gawah-web** (Next.js). System 1's shopper is an optional fourth, commented out
in `render.yaml` until Suvir's code is merged and runs.

```
browser ──> gawah-web ──(server adds X-Gawah-Key = SYSTEM3_KEY)──> gawah-counsel ──> gawah-engine ──> PayPal sandbox
```

The hosted demo is **PayPal sandbox only**. Payments there are real sandbox payments, not real money.

## Read this first: what the free tier does to the demo

- **Data does not persist.** Free Render instances have an ephemeral filesystem. On every restart
  or redeploy the engine's database, its signing keys and Counsel's database are gone, and the
  engine starts again with an empty ledger. This is by design: with `GAWAH_BOOTSTRAP=1` the engine
  creates the schema, **new** ledger, mandate and demo-merchant keys together with the empty
  database, so the chain is always consistent. Nothing from a previous run survives, including
  mandates, payments and evidence. (The payments themselves remain in PayPal's sandbox.)
- **Instances sleep when idle** (after about 15 minutes). The first request after a sleep can take
  **about a minute** while the service boots; the engine also rebuilds its state from nothing.
  **Warm all three services before recording** (step 8) and keep a tab open or run the smoke script
  right before you start.
- **Free-tier hours are limited** (Render gives a monthly pool of free instance hours per
  workspace; three always-on services use it up faster than one). Check the current limit in the
  Render dashboard under Billing. Deploy shortly before you need the demo and suspend the services
  afterwards.
- **No user login on the dashboard.** Anyone with the web URL can use it. Only sandbox data is
  there, but treat the URL as unlisted and do not put anything real in the demo.
- Rate limit: the engine allows `RATE_LIMIT_PER_MINUTE` calls per key per minute, per process
  (set to 300 in `render.yaml`).

## 1. Generate the keys

Every key must be a long random value you generate yourself. **Do not reuse the local-development
placeholders (`k1`, `k3`) or any key from a `.env` file.**

```sh
openssl rand -hex 24   # run once per key below; every run gives a different value
```

| Key | Used by | Must match |
| --- | --- | --- |
| `SYSTEM1_KEY` | engine (and the shopper, later) | the shopper's `SYSTEM1_KEY` |
| `SYSTEM3_KEY` | engine, Counsel, web | **the same value on all three services** |
| `ADMIN_KEY` | engine only | nothing; used with `POST /v1/admin/reset-demo` |

Counsel also has `SYSTEM2_KEY` (its key *to* the engine). Leave it empty and Counsel reuses its
`SYSTEM3_KEY`, which is right when you use one `SYSTEM3_KEY` everywhere. Never use a `NEXT_PUBLIC_`
variable for a key: browsers can read those.

Keep the values in a password manager, not in the repo.

## 2. Environment variables per service

`sync: false` variables are entered in the dashboard (step 4). Variables with a value are set by
`render.yaml`.

### gawah-engine (System 2)

| Variable | Set by | Value / notes |
| --- | --- | --- |
| `PYTHON_VERSION` | yaml | `3.12.8` |
| `GAWAH_MOCK` | yaml | `0` (real engine, not fixtures) |
| `GAWAH_ENV` | yaml | `demo` (enables the sandbox flow and `reset-demo`) |
| `GAWAH_BOOTSTRAP` | yaml | `1` (empty database -> schema, keys, demo merchant) |
| `PAYPAL_ENV` | yaml | `sandbox` |
| `RATE_LIMIT_PER_MINUTE` | yaml | `300` |
| `SYSTEM1_KEY` | dashboard | step 1 |
| `SYSTEM3_KEY` | dashboard | step 1 |
| `ADMIN_KEY` | dashboard | step 1 |
| `ENGINE_CORS_ORIGINS` | dashboard | gawah-web URL, e.g. `https://gawah-web.onrender.com` (no trailing slash; comma-separate several) |
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | dashboard | sandbox app credentials |
| `PAYPAL_CARD_NUMBER`, `PAYPAL_CARD_EXPIRY` (`YYYY-MM`), `PAYPAL_CARD_CVV`, `PAYPAL_CARD_NAME` | dashboard | PayPal sandbox test card for the card payment route |
| `BUYER_EMAIL`, `BUYER_PASSWORD` | dashboard | optional: sandbox buyer for browser approval |
| `GROQ_API_KEY` | dashboard | optional; without it the Guardian's category check uses its keyword fallback (`/health` says so) |
| `CHANNEL3_API_KEY` | dashboard | optional |

### gawah-counsel (System 3 API)

| Variable | Set by | Value / notes |
| --- | --- | --- |
| `PYTHON_VERSION` | yaml | `3.12.8` |
| `ENABLE_SANDBOX_RESOLUTION` | yaml | `false` |
| `DATABASE_URL` | (unset) | SQLite on the ephemeral disk. Set a PostgreSQL URL only if you want Counsel's data to survive restarts |
| `SYSTEM2_BASE_URL` | dashboard | gawah-engine URL (step 6) |
| `SYSTEM2_KEY` | dashboard | the engine's `SYSTEM3_KEY` (or leave empty to reuse `SYSTEM3_KEY`) |
| `SYSTEM3_KEY` | dashboard | step 1, same value as the engine's and the web's |
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | dashboard | sandbox app with Customer disputes enabled (Disputes API) |
| `GROQ_API_KEY` | dashboard | AI Counsel drafting |

Counsel runs `alembic upgrade head` on every start (`scripts/start_counsel.sh`).

### gawah-web (Next.js)

| Variable | Set by | Value / notes |
| --- | --- | --- |
| `NODE_VERSION` | yaml | `22.22.2` (the app needs Node 20.19+) |
| `SYSTEM3_API_URL` | dashboard | gawah-counsel URL (step 6) |
| `SYSTEM3_KEY` | dashboard | step 1. Read only by the Next.js server's proxy route. **Never a `NEXT_PUBLIC_` variable.** |

### gawah-shopper (optional, later)

Uncomment the block in `render.yaml` once System 1 runs. It needs `ENGINE_URL`, `SYSTEM1_KEY`
(same as the engine's), `GROQ_API_KEY`, `CHANNEL3_API_KEY`, and no PayPal or payment key.

## 3. Dashboard checklist (what you do by hand)

1. [ ] Push this repo to GitHub (the Blueprint reads `render.yaml` from it).
2. [ ] Render dashboard -> **New** -> **Blueprint** -> pick the repo and branch `main`.
3. [ ] Render lists the three services and asks for every `sync: false` variable. Paste the values
       from step 2. Leave `SYSTEM2_BASE_URL`, `SYSTEM3_API_URL` and `ENGINE_CORS_ORIGINS` as a
       placeholder like `https://placeholder.invalid`; you fix them in item 6 once the URLs exist.
       Use the **same `SYSTEM3_KEY`** for engine, Counsel and web.
4. [ ] **Apply** to create the services and trigger the first deploy.
5. [ ] Wait until all three show **Live** with a passing health check (first build: several
       minutes). `https://<engine>/health`, `https://<counsel>/api/health` and the web root should
       answer.
6. [ ] Copy the three service URLs (shown under each service name) into:
       - gawah-counsel -> `SYSTEM2_BASE_URL` = engine URL
       - gawah-web -> `SYSTEM3_API_URL` = Counsel URL
       - gawah-engine -> `ENGINE_CORS_ORIGINS` = web URL
       (no trailing slashes).
7. [ ] **Manual Deploy -> Deploy latest commit** (or save the variables, which redeploys) for all
       three, and wait for **Live** again.
8. [ ] Warm and check from your machine:
       ```sh
       ENGINE_URL=https://<engine>.onrender.com \
       COUNSEL_URL=https://<counsel>.onrender.com \
       WEB_URL=https://<web>.onrender.com \
       SYSTEM1_KEY=<value> SYSTEM3_KEY=<value> \
       uv run python scripts/smoke_remote.py
       ```
       It waits up to two minutes for each sleeping service, runs every step with PASS/FAIL and
       timings, and prints which hop failed (engine, counsel, web, web->counsel). It makes one real
       sandbox payment ($65) when the engine is in demo mode with PayPal configured.
9. [ ] Right before recording, run the script again so nothing is asleep.

## 4. Re-recording the video

`POST /v1/admin/reset-demo` wipes the engine's data and keys and starts a fresh ledger (only with
`GAWAH_ENV=demo`, and only with the right admin key):

```sh
curl -X POST https://<engine>.onrender.com/v1/admin/reset-demo -H "X-Admin-Key: $ADMIN_KEY"
```

Counsel keeps its own copy of the evidence it assembled earlier and disputes it synced, so for a
clean slate also restart gawah-counsel (**Manual Deploy -> Clear build cache & deploy**, or
**Restart**), which resets its ephemeral database too. The reset is for a single engine process;
the demo runs one.

## 5. Troubleshooting

- *Engine starts then exits with "missing signing key"*: the database has data but its keys are
  gone (only possible if `DATABASE_URL` points at persistent storage but the keys are not). Point
  `LEDGER_SIGNING_KEY_PATH` and `MANDATE_SIGNING_KEY_PATH` at the same persistent location.
- *Web shows errors from Counsel*: `SYSTEM3_API_URL` is wrong, or web's `SYSTEM3_KEY` differs from
  Counsel's. The smoke script's `web->counsel` hop isolates this.
- *Counsel cannot read the engine*: `SYSTEM2_BASE_URL` wrong, or `SYSTEM2_KEY` is not the engine's
  `SYSTEM3_KEY`. The smoke script's `counsel` hop isolates this.
- *PayPal step fails*: check the sandbox credentials and card variables; the engine log never
  contains card numbers or tokens.
