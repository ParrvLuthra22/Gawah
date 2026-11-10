#!/usr/bin/env bash
# `make e2e`: start the engine and the shopper, run integration/e2e.py, stop both.
#
#   make e2e                      real engine (real Guardian, ledger, signing) + SIMULATED PayPal
#                                 (GAWAH_ENV=test) + the shopper with a SIMULATED Groq. No network,
#                                 no real keys; everything lives in a temp directory.
#   make e2e E2E_MODE=mock        the mock engine (fixtures). Only checks wire formats: the mock
#                                 blocks every merchant but demo-shoe-store, so nothing is proven.
#   make e2e E2E_LIVE_GROQ=1      the shopper uses the real Groq (needs GROQ_API_KEY): network.
#
# ENGINE_PORT / SHOPPER_PORT override 8000 / 8001. Keys are random per run and never printed.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
MODE="${E2E_MODE:-simulated}"
ENGINE_PORT="${ENGINE_PORT:-8000}"
SHOPPER_PORT="${SHOPPER_PORT:-8001}"
PY="$(uv run python -c 'import sys; print(sys.executable)')"
WORK="$(mktemp -d)"
PIDS=()

cleanup() {
  for pid in "${PIDS[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done
  for pid in "${PIDS[@]:-}"; do [ -n "$pid" ] && wait "$pid" 2>/dev/null || true; done
  rm -rf "$WORK"
}
trap cleanup EXIT

for port in "$ENGINE_PORT" "$SHOPPER_PORT"; do
  if "$PY" - "$port" <<'PYEOF'
import socket, sys
s = socket.socket()
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PYEOF
  then
    echo "port $port is already in use; stop that server or set ENGINE_PORT / SHOPPER_PORT" >&2
    exit 2
  fi
done

export SYSTEM1_KEY="$(openssl rand -hex 16)"
export SYSTEM3_KEY="$(openssl rand -hex 16)"
export ENGINE_URL="http://127.0.0.1:$ENGINE_PORT"
export SHOPPER_URL="http://127.0.0.1:$SHOPPER_PORT"
export MERCHANT_SIGNING_KEY_PATH="$WORK/merchant-keys"

wait_for() { # url name log
  for _ in $(seq 1 80); do
    curl -fsS "$1" >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  echo "$2 did not become healthy; last log lines:" >&2
  tail -n 20 "$3" >&2 || true
  exit 2
}

# The engine runs from the temp directory so it cannot read the repo's .env (real PayPal and Groq
# credentials): its configuration is exactly the variables below.
if [ "$MODE" = "mock" ]; then
  ENGINE_ENV=(GAWAH_MOCK=1)
  echo "engine: MOCK mode (fixtures; wire-format check only)"
else
  ENGINE_ENV=(GAWAH_MOCK=0 GAWAH_ENV=test GAWAH_FAKE_PAYPAL=1
    "DATABASE_URL=sqlite:///$WORK/e2e.db" "LEDGER_SIGNING_KEY_PATH=$WORK/engine-keys"
    "MANDATE_SIGNING_KEY_PATH=$WORK/engine-keys" RATE_LIMIT_PER_MINUTE=300)
  echo "engine: real Guardian and ledger, SIMULATED PayPal, no Groq key (keyword fallback)"
fi
# `exec` makes uvicorn replace the subshell, so the recorded pid is the server itself.
( cd "$WORK" && exec env "${ENGINE_ENV[@]}" "$PY" -m uvicorn gawah_engine.main:app \
    --host 127.0.0.1 --port "$ENGINE_PORT" ) >"$WORK/engine.log" 2>&1 &
PIDS+=($!)

if [ "${E2E_LIVE_GROQ:-0}" = "1" ]; then
  SHOPPER_APP="shopper.main:app"; echo "shopper: real Groq (network)"
  SHOPPER_ENV=(PRODUCT_PROVIDER=demo)
else
  SHOPPER_APP="integration.shopper_stub_app:app"; echo "shopper: SIMULATED Groq (no network)"
  SHOPPER_ENV=(GROQ_API_KEY=stub-not-used PRODUCT_PROVIDER=demo)
fi
env "${SHOPPER_ENV[@]}" ATTACK_SIMULATION_ENABLED=true "$PY" -m uvicorn "$SHOPPER_APP" \
    --host 127.0.0.1 --port "$SHOPPER_PORT" >"$WORK/shopper.log" 2>&1 &
PIDS+=($!)

wait_for "$ENGINE_URL/health" engine "$WORK/engine.log"
wait_for "$SHOPPER_URL/health" shopper "$WORK/shopper.log"

set +e
"$PY" -m integration.e2e
status=$?
set -e
if [ "$status" -ne 0 ]; then
  echo "--- engine log (tail) ---" >&2; tail -n 8 "$WORK/engine.log" >&2 || true
  echo "--- shopper log (tail) ---" >&2; tail -n 8 "$WORK/shopper.log" >&2 || true
fi
exit "$status"
