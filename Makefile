.PHONY: install dev-engine dev-web test lint

install:
	uv sync
	cd web && npm install

dev-engine:
	uv run uvicorn gawah_engine.main:app --reload --app-dir engine --port 8000

dev-web:
	cd web && npm run dev

test:
	uv run pytest

lint:
	uv run ruff check .
