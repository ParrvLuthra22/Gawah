# System 3: Dispute Counsel backend

N1 reads PayPal Sandbox disputes and their details, stores normalized snapshots and status changes, and links payment references through System 2. System 3 runs on port 8002. System 2 uses 8000 and System 1 uses 8001.

## Run locally

1. Create a dedicated PostgreSQL database (for example, `createdb -U postgres gawah_system3`). Use its DSN for `DATABASE_URL`. SQLite (`sqlite:///./n1.db`) is supported for local testing.
2. Copy `system3/.env.example` to `system3/.env`. Set `SYSTEM3_KEY` for the console. Set `SYSTEM2_KEY` to the mock engine's `SYSTEM3_KEY` if it differs from the console key. Follow the repository README to start System 2 with `GAWAH_MOCK=1` on port 8000.
3. From `system3/`, run the following in PowerShell.

```powershell
python -m venv .venv
./.venv/Scripts/python -m pip install -r requirements.txt -e ../contract pytest
./.venv/Scripts/python -m alembic upgrade head
./.venv/Scripts/python -m uvicorn counsel.main:app --reload --port 8002
```

On Unix, use `.venv/bin/python` in the same commands. For the Next.js setup, see [the web README](../../web/README.md).

## PayPal and fixtures

Use credentials from a Sandbox merchant app with Customer disputes enabled. Open a dispute for an eligible sandbox transaction as the buyer if the merchant's dispute list is empty. Check the PayPal integration page to confirm that the merchant app can read disputes, then select **Sync disputes**. Field visibility depends on the Sandbox account and its access. [PayPal Sandbox setup](https://developer.paypal.com/disputes/set-up/)

With a local database, the following command loads the shared simulated cases. It is safe to repeat. The console labels these **Simulated fixture** and excludes them from the PayPal total. The mock engine supplies fixture payments. Its documented payment list contains `pay_01`, so the fixture for `pay_04` stays unmatched.

```powershell
./.venv/Scripts/python -m counsel.main seed-fixtures
./.venv/Scripts/python -m pytest tests
```

## API and synchronization

Send `X-Gawah-Key` with the configured `SYSTEM3_KEY` on every route except health.

| Method | Route | Behavior |
| --- | --- | --- |
| GET | `/api/health` | Check access to dispute storage |
| GET | `/api/disputes` | List with `page`, `page_size`, `search`, `status`, `source`, `link_status`, `sort`, and `order` |
| GET | `/api/disputes/{id}` | Get a stored case by its System 3 ID, including status history |
| POST | `/api/disputes/sync` | List PayPal disputes, fetch their details, and reconcile payment links |
| GET | `/api/disputes/sync/history` | List attempts, newest first, with `page` and `page_size` |
| GET | `/api/paypal/status` | Probe Sandbox dispute list access and report the latest sync and stored PayPal count |

Sync traverses the pages returned by PayPal and fetches details for each unique case. It uses exact capture, order, or custom IDs from System 2's `GET /v1/payments`. The next sync retries unresolved links. If PayPal retrieval fails, the run rolls back its dispute changes and records the error. If System 2 is unavailable, PayPal data is saved with a partial result. The current System 2 real-mode payment list returns HTTP 501. PayPal controls which cases its list returns and which detail fields it exposes. The local console is intended for a trusted operator and binds to loopback by default.

## N2 evidence assembly

Run `python -m alembic upgrade head` to add the evidence pack table. Open **Evidence** and enter a Gawah payment ID, or choose **View evidence** from a linked dispute. With the mock engine and the optional seeded disputes, `pay_04` shows the authorized size 9 and the cart's size 10. Delivery remains unavailable.

| Method | Route |
| --- | --- |
| POST | `/api/evidence/{payment_id}/assemble` |
| GET | `/api/evidence/{payment_id}` |
| GET | `/api/evidence/{payment_id}/timeline` |
| GET | `/api/evidence/{payment_id}/verification` |
| GET | `/api/evidence/{payment_id}/issues` |

Use the same `X-Gawah-Key` as for N1. Pass the local N1 `dispute_id` query parameter to select a specific dispute for a payment. Assembly requires that selection when multiple disputes match. GET requests accept an optional `version` to retrieve a saved snapshot. Reassembling identical evidence for the same dispute returns its existing version.

Mock bundle verification reports hash and link checks. The engine-wide result is stored separately. Cart verification uses the shared verifier when the merchant registry provides the exact ES256 signing key. The shared contract exposes no independent mandate-signature result or merchant order and delivery read API. Real-mode evidence and payment endpoints return HTTP 501. If a local dispute or payment-scoped ledger entry is available, N2 can still save a partial pack with the gaps recorded.
