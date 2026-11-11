.PHONY: install dev-engine dev-counsel dev-web dev-all test lint smoke-remote e2e rehearse

COUNSEL_UV = uv run --no-project --with-requirements requirements.txt --with-editable ../contract

install:
	uv sync
	cd system3 && $(COUNSEL_UV) python -c "import counsel.main"  # fetch Counsel's dependencies
	cd web && npm ci

dev-engine:
	uv run uvicorn gawah_engine.main:app --reload --app-dir engine --port 8000

dev-counsel:
	cd system3 && $(COUNSEL_UV) python -m alembic upgrade head
	cd system3 && $(COUNSEL_UV) python -m uvicorn counsel.main:app --reload --port 8002

dev-web:
	cd web && npm run dev

# Ports: engine 8000, shopper 8001 (System 1, not started here), counsel 8002, web 3000.
# Ctrl-C stops all three. Needs .env, system3/.env and web/.env.local (see the README).
dev-all:
	@trap 'kill 0' INT TERM EXIT; \
	$(MAKE) dev-engine & $(MAKE) dev-counsel & $(MAKE) dev-web & wait

test:
	uv run pytest
	cd system3 && $(COUNSEL_UV) --with pytest python -m pytest tests -q
	cd web && npx vitest run

# Needs ENGINE_URL, COUNSEL_URL, WEB_URL, SYSTEM1_KEY, SYSTEM3_KEY in the environment.
smoke-remote:
	uv run python scripts/smoke_remote.py

lint:
	uv run ruff check .

# End to end: shopper -> signed proposal -> engine Guardian -> payment -> ledger. Starts both
# services, runs integration/e2e.py, stops them. Real Guardian and ledger, SIMULATED PayPal and Groq,
# no network. `make e2e E2E_MODE=mock` uses the mock engine; `E2E_LIVE_GROQ=1` the real Groq.
e2e:
	bash scripts/e2e.sh

# The full demo in video order (real PayPal sandbox payment, attacks, Counsel scenarios, ledger tamper
# demo on a copy). Starts engine, shopper, Counsel and web; stops them on exit. Needs .env and network.
rehearse:
	bash scripts/rehearse.sh

.PHONY: contract
contract:
	uv run python -m gawah_contract.export
