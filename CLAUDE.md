# Gawah: standing rules for every session

Gawah is an AP2-inspired authorization layer: an agent proposes a purchase, the Guardian decides,
PayPal pays, a signed hash-chained ledger records everything. Details: `docs/CONTRACT.md`,
`OWNERS.md`, `spikes/spikes.md`.

## 1. Ownership
- Edit only your own system's directories:
  - System 1 (shopper agent, demo store): `system1/`
  - System 2 (engine, Guardian, payments, ledger): `engine/`, `contract/`, `scripts/`, `docs/`
  - System 3 (Dispute Counsel, dashboard): `system3/`, `web/`
- Anything in `contract/` must be additive (new models, new optional fields, new enum values)
  and listed in `docs/CONTRACT.md`. Regenerate schemas with `make contract`.

## 2. Secrets
- Never commit secrets, keys, `.env` or `.db` files. Keys live in gitignored `keys/` dirs.
- Never log or store card numbers, CVVs, tokens or passwords. Card details come only from
  config/env and never reach the database, ledger, logs or API responses.

## 3. Tests
- Default tests make no network calls (an autouse fixture enforces it). PayPal is faked with an
  `httpx` transport.
- Tests that need the real sandbox use `@pytest.mark.live` and run only with `GAWAH_LIVE=1`.

## 4. Every change
- `uv run pytest` and `uv run ruff check .` pass, then `git pull --rebase origin main`, commit in
  logical commits, push. Run every check and fix failures; do not loosen a test to make it pass.

## 5. Money
- Always integer minor units (cents). Never floats; the API rejects them in money fields.

## 6. Honesty
- Label anything simulated as simulated, in code, docs and UI (mock mode, fake PayPal, fallback
  checks). Never claim full AP2 compliance: say "AP2-inspired".

## 7. The agent proposes, the Guardian decides
- The shopper agent never gets a payment or refund tool. Its key can only submit proposals and
  read mandates (see `engine/gawah_engine/auth.py` SCOPES).
- The Guardian never reads untrusted text (rationale, page content, agent messages). The only
  exception is the constrained semantic check: sku/title/variant, truncated, wrapped as untrusted,
  and it can only add a BLOCK.

## 8. Write first, pay second
- `PAYMENT_STARTED` is committed to the ledger before any PayPal call. PayPal calls happen outside
  locks and transactions, with idempotency keys. Crashes are reconciled from PayPal, never guessed.
