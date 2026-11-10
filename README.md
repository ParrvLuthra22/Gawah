# Gawah

Gawah (Urdu/Hindi for "witness") is a witness and dispute layer for AI agents that spend money through PayPal. It records what an agent was authorized to buy (the mandate), checks each purchase against it, keeps a signed ledger of what happened, and helps assemble evidence when a charge has to be disputed. Built for the PayPal AI Hackathon.

## Run it locally

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 20+, and make.

```sh
make install
cp .env.example .env
cp system3/.env.example system3/.env
cp web/.env.example web/.env.local
```

For the local mock engine, generate a random service key with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and put it in `SYSTEM3_KEY` in all three files. To use separate keys, set Counsel's `SYSTEM2_KEY` to the engine's `SYSTEM3_KEY`, and match Counsel's `SYSTEM3_KEY` in `web/.env.local`. The Next.js server adds the Counsel key to allowlisted API requests.

Start everything with one command, or each service in its own terminal.

```sh
make dev-all   # engine 8000, counsel 8002, web 3000 together; Ctrl-C stops all (shopper uses 8001)
```

| Command | Service |
| --- | --- |
| `make dev-engine` | System 2 on port 8000 |
| `make dev-counsel` | System 3 on port 8002. Applies migrations before starting. |
| `make dev-web` | Dashboard on port 3000 |

On Windows without make, run `uv sync` and `npm install` in the same directories as the Makefile's install recipe. Run the engine and web commands from their respective recipes. Start Counsel in `system3` with these two commands.

```sh
uv run --no-project --with-requirements requirements.txt --with-editable ../contract python -m alembic upgrade head
uv run --no-project --with-requirements requirements.txt --with-editable ../contract python -m uvicorn counsel.main:app --reload --port 8002
```

Counsel defaults to `system3/counsel.db` (SQLite). Set its `DATABASE_URL` to a PostgreSQL connection URL to use PostgreSQL. For deployment, set an absolute SQLite database path on persistent storage or use a PostgreSQL connection URL. Run the same Alembic migration command for the chosen database before starting Counsel.

To load simulated disputes, run this command in `system3` after migrating.

```sh
uv run --no-project --with-requirements requirements.txt --with-editable ../contract python -m counsel.main seed-fixtures
```

The `pay_04` case is a read-only fixture. For actual Sandbox disputes, put credentials from your own PayPal Sandbox app in `system3/.env` and sync from the dashboard. Keep that file private and use separate local files for Engine and Next.js.

To exercise resolution locally, set `ENABLE_SANDBOX_RESOLUTION=true` in `system3/.env` and restart Counsel. The console requires confirmation for every action. Full payment refunds require a matching real System 2 capture. PayPal evidence submission supports reviewed text where PayPal requests `OTHER` evidence, and claim acceptance requires PayPal to offer a full refund. Hosted browser sessions have read-only resolution access.

The API contract for the three systems is in `docs/CONTRACT.md`; ownership is in `OWNERS.md`.

`make install` installs all three systems; `make test` runs the engine and contract tests, Counsel's tests and the web tests; `make lint` runs ruff.

## Hosted demo (Render)

`render.yaml` deploys the engine, Counsel and the web app on Render; the steps and the checklist are in [docs/DEPLOY.md](docs/DEPLOY.md), and `scripts/smoke_remote.py` checks the deployed stack hop by hop. It is a **PayPal sandbox** demo on **free instances: the filesystem is ephemeral and instances sleep when idle**, so all hosted data (mandates, ledger, keys, Counsel's database) is reset on every restart or redeploy, and the first request after a sleep can take about a minute. The engine's `GAWAH_BOOTSTRAP=1` recreates an empty database, new signing keys and the demo merchant on each start, so the ledger and its keys always match. `POST /v1/admin/reset-demo` (admin key, `GAWAH_ENV=demo` only) resets the engine on demand for re-recording. Never commit `.env` or anything under `keys/`.

## Status

In progress.

AP2 here is AP2-inspired unless the SDK spike (C1 in `spikes/spikes.md`) passes.
