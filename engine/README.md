# engine

Python package `gawah_engine` (System 2, owner: Parrv). Port 8000: mandate, guardian, ledger, payments, PayPal adapter.

## Real mode (`GAWAH_MOCK=0`)

Mandates and the ledger run on SQLite (`DATABASE_URL`) with ES256 signing keys generated on first run into `LEDGER_SIGNING_KEY_PATH` / `MANDATE_SIGNING_KEY_PATH` (directories, default `./keys/`, gitignored; never commit them). Key modules: `signing.py` (Signer, rotation), `canonical.py`, `db.py` (migrations), `mandate_service.py` (mandates, policy checks, budget reservations), `ledger_service.py` (hash chain, write-ahead payment helper). Demo of tamper detection: `uv run python scripts/tamper_demo.py`.

Guardian: `guardian.py` (rules + service), `semantic.py` (constrained Groq category check with a keyword fallback and SQLite cache), `merchant_service.py` (registered ES256 merchant keys). Set up a demo store with `uv run python scripts/dev_merchant.py --url http://localhost:8000`.
