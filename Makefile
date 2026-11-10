.PHONY: install dev-engine dev-counsel dev-web dev-all test lint smoke-remote

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

.PHONY: contract
contract:
	uv run python -m gawah_contract.export
