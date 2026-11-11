#!/usr/bin/env bash
# Full demo rehearsal, in video order. Starts the engine (8000), shopper (8001), Counsel (8002) and
# web (3000) (ENGINE_PORT, SHOPPER_PORT and WEB_PORT can be
# overridden; Counsel is pinned to 8002 because system3/run_demo_scenarios.py hardcodes it), then runs:
#   1. the REAL happy path (PayPal sandbox card route: one real $59.99 sandbox payment)
#   2. the attack set (every attack and Guardian case that does not need a second real payment)
#   3. system3/run_demo_scenarios.py (Counsel's simulated dispute cases)
#   4. the ledger tamper demo, on a COPY of the engine database
# then prints the URLs to open. Everything is stopped on exit.
#
#   bash scripts/rehearse.sh            (or: make rehearse)
#   REHEARSE_HOLD=0 bash scripts/rehearse.sh    do not wait for Enter before stopping
#
# Needs .env with the PayPal sandbox credentials and card, and GROQ_API_KEY, and the network.
# Random service keys per run (never printed); a fresh engine database; the shopper's merchant key
# persists in keys/system1-merchant. Counsel's database is fresh too, so nothing local is modified.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
ENGINE_PORT="${ENGINE_PORT:-8000}"; SHOPPER_PORT="${SHOPPER_PORT:-8001}"
COUNSEL_PORT=8002; WEB_PORT="${WEB_PORT:-3000}"
PY="$(uv run python -c 'import sys; print(sys.executable)')"
WORK="$(mktemp -d)"
PIDS=()
T0=$SECONDS
STEP_T=$SECONDS
FAILED=()

kill_tree() {
  local pid=$1 child
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do kill_tree "$child"; done
  kill "$pid" 2>/dev/null || true
}
cleanup() {
  echo; echo "stopping everything..."
  for pid in "${PIDS[@]:-}"; do [ -n "$pid" ] && kill_tree "$pid"; done
  sleep 1
  for pid in "${PIDS[@]:-}"; do [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null || true; done
  rm -rf "$WORK"
  echo "total rehearsal time: $((SECONDS - T0))s"
}
trap cleanup EXIT

step() { STEP_T=$SECONDS; printf '\n\033[1;36m=== %s ===\033[0m\n' "$1"; }
took() { echo "    -> $((SECONDS - STEP_T))s"; }
# run NAME CMD...: run a step, remember a failure but keep going so one miss shows the rest
run() {
  local name=$1; shift
  if "$@"; then took; else echo "!! step failed: $name"; FAILED+=("$name"); took; fi
}

for port in $ENGINE_PORT $SHOPPER_PORT $COUNSEL_PORT $WEB_PORT; do
  if "$PY" -c "import socket,sys; s=socket.socket(); sys.exit(0 if s.connect_ex(('127.0.0.1',$port))==0 else 1)"; then
    echo "port $port is already in use; stop that server first (lsof -i :$port)" >&2
    exit 2
  fi
done

export SYSTEM1_KEY="$(openssl rand -hex 16)"
export SYSTEM3_KEY="$(openssl rand -hex 16)"
export ENGINE_URL="http://127.0.0.1:$ENGINE_PORT" SHOPPER_URL="http://127.0.0.1:$SHOPPER_PORT"
export MERCHANT_SIGNING_KEY_PATH="${MERCHANT_SIGNING_KEY_PATH:-$ROOT/keys/system1-merchant}"
DB="$WORK/rehearsal.db"
ENGINE_KEYS="$WORK/engine-keys"
COUNSEL_UV=(uv run --no-project --with-requirements requirements.txt --with-editable ../contract)

wait_for() { # url name log
  for _ in $(seq 1 120); do
    curl -fsS "$1" >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  echo "$2 did not become healthy; last log lines:" >&2; tail -n 20 "$3" >&2 || true; exit 2
}

step "start: engine, shopper, Counsel, web"
# Engine from the repo root so .env supplies the sandbox credentials; everything else is explicit.
( cd "$ROOT" && exec env GAWAH_MOCK=0 GAWAH_ENV=dev GAWAH_FAKE_PAYPAL=0 RATE_LIMIT_PER_MINUTE=300 \
    "DATABASE_URL=sqlite:///$DB" "LEDGER_SIGNING_KEY_PATH=$ENGINE_KEYS" \
    "MANDATE_SIGNING_KEY_PATH=$ENGINE_KEYS" ENGINE_CORS_ORIGINS=http://127.0.0.1:$WEB_PORT \
    "$PY" -m uvicorn gawah_engine.main:app --host 127.0.0.1 --port "$ENGINE_PORT" ) >"$WORK/engine.log" 2>&1 &
PIDS+=($!)
( cd "$ROOT" && exec env PRODUCT_PROVIDER=demo ATTACK_SIMULATION_ENABLED=true \
    "$PY" -m uvicorn shopper.main:app --host 127.0.0.1 --port "$SHOPPER_PORT" ) >"$WORK/shopper.log" 2>&1 &
PIDS+=($!)
COUNSEL_ENV=("DATABASE_URL=sqlite:///$WORK/counsel.db" "SYSTEM2_BASE_URL=$ENGINE_URL"
  "SYSTEM2_KEY=$SYSTEM3_KEY" "SYSTEM3_KEY=$SYSTEM3_KEY" ENABLE_SANDBOX_RESOLUTION=false)
( cd "$ROOT/system3" && env "${COUNSEL_ENV[@]}" "${COUNSEL_UV[@]}" python -m alembic upgrade head ) \
    >"$WORK/counsel-migrate.log" 2>&1 || { tail -n 15 "$WORK/counsel-migrate.log" >&2; exit 2; }
( cd "$ROOT/system3" && exec env "${COUNSEL_ENV[@]}" "${COUNSEL_UV[@]}" python -m uvicorn \
    counsel.main:app --host 127.0.0.1 --port "$COUNSEL_PORT" ) >"$WORK/counsel.log" 2>&1 &
PIDS+=($!)
( cd "$ROOT/web" && exec env "SYSTEM3_API_URL=http://127.0.0.1:$COUNSEL_PORT" "SYSTEM3_KEY=$SYSTEM3_KEY" \
    npx next dev --webpack --hostname 127.0.0.1 --port "$WEB_PORT" ) >"$WORK/web.log" 2>&1 &
PIDS+=($!)
wait_for "$ENGINE_URL/health" engine "$WORK/engine.log"
wait_for "$SHOPPER_URL/health" shopper "$WORK/shopper.log"
wait_for "http://127.0.0.1:$COUNSEL_PORT/api/health" counsel "$WORK/counsel.log"
wait_for "http://127.0.0.1:$WEB_PORT/" web "$WORK/web.log"
curl -fsS "$ENGINE_URL/health" | "$PY" -c "import json,sys; h=json.load(sys.stdin); print('engine health: mock=%s env=%s card_configured=%s semantic=%s' % (h['mock'], h['env'], h['card_configured'], h['semantic']))"
took

step "warm the Guardian's semantic cache (real Groq) for this database"
run "warm cache" "$PY" scripts/warm_semantic_cache.py --database-url "sqlite:///$DB"

step "1/4 real happy path (PayPal sandbox)"
run "real happy path" "$PY" -m integration.e2e --phase happy

step "2/4 attack set"
run "attack set" "$PY" -m integration.e2e --phase attacks

step "3/4 Counsel demo scenarios (system3/run_demo_scenarios.py)"
run "counsel scenarios" bash -c "cd '$ROOT/system3' && env 'SYSTEM3_KEY=$SYSTEM3_KEY' ${COUNSEL_UV[*]} python run_demo_scenarios.py"

step "4/4 ledger tamper demo (on a COPY of the engine database)"
run "tamper demo" "$PY" scripts/tamper_demo.py --copy-of "$DB" --keys "$ENGINE_KEYS"
echo "original ledger, checked through the engine after the tamper demo:"
curl -fsS -H "X-Gawah-Key: $SYSTEM3_KEY" "$ENGINE_URL/v1/ledger/verify" | "$PY" -c "import json,sys; r=json.load(sys.stdin); print('  ok=%s, %s entries checked' % (r['ok'], r['checked']))"

step "open these"
cat <<URLS
  Dashboard (web)        http://127.0.0.1:$WEB_PORT/dashboard
  Mandates / Ledger      http://127.0.0.1:$WEB_PORT/mandates    http://127.0.0.1:$WEB_PORT/ledger
  Disputes / Evidence    http://127.0.0.1:$WEB_PORT/disputes    http://127.0.0.1:$WEB_PORT/evidence
  Engine health          $ENGINE_URL/health
  Shopper API docs       $SHOPPER_URL/docs
  Counsel API docs       http://127.0.0.1:$COUNSEL_PORT/docs
URLS
echo
if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "REHEARSAL: ${#FAILED[@]} step(s) failed: ${FAILED[*]}"; status=1
else
  echo "REHEARSAL: all steps passed"; status=0
fi
echo "(the servers stop when this script exits)"
if [ -t 0 ] && [ "${REHEARSE_HOLD:-1}" = "1" ]; then
  read -r -p "Press Enter to stop everything... " _
fi
exit "$status"
