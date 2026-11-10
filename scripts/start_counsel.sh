#!/usr/bin/env bash
# Start Counsel (System 3 API) for a hosted deploy: apply the Alembic migrations, then serve.
# Migrations run on EVERY boot, so the schema exists even on a fresh ephemeral disk.
# Used by render.yaml. Run from the repo root; PORT defaults to 8002.
set -euo pipefail
cd "$(dirname "$0")/../system3"
python -m alembic upgrade head
exec python -m uvicorn counsel.main:app --host 0.0.0.0 --port "${PORT:-8002}"
