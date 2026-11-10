.PHONY: install dev-engine dev-counsel dev-web test lint

COUNSEL_UV = uv run --no-project --with-requirements requirements.txt --with-editable ../contract

install:
	uv sync
	cd web && npm install

dev-engine:
	uv run uvicorn gawah_engine.main:app --reload --app-dir engine --port 8000

dev-counsel:
	cd system3 && $(COUNSEL_UV) python -m alembic upgrade head
	cd system3 && $(COUNSEL_UV) python -m uvicorn counsel.main:app --reload --port 8002

dev-web:
	cd web && npm run dev

test:
	uv run pytest

lint:
	uv run ruff check .

.PHONY: contract
contract:
	uv run python -m gawah_contract.export
