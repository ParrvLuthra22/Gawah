# Gawah

Gawah (Urdu/Hindi for "witness") is a witness and dispute layer for AI agents that spend money through PayPal. It records what an agent was authorized to buy (the mandate), checks each purchase against it, keeps a signed ledger of what happened, and helps assemble evidence when a charge has to be disputed. Built for the PayPal AI Hackathon.

## Run it locally

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 20+, and make.

```sh
uv sync
cp .env.example .env      # then fill in your own sandbox keys and SYSTEM1_KEY / SYSTEM3_KEY
make dev-engine           # engine on http://localhost:8000
make dev-web              # web on http://localhost:3000 (in a second terminal)
```

The API contract for the three systems is in `docs/CONTRACT.md`; ownership is in `OWNERS.md`.

`make test` runs the tests and `make lint` runs ruff. Never commit `.env` or anything under `keys/`.

## Status

In progress.

AP2 here is AP2-inspired unless the SDK spike (C1 in `spikes/spikes.md`) passes.
