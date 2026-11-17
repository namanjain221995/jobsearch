#!/usr/bin/env bash
# Browser tests against a self-contained demo stack: the demo API (synthetic data, no network, no AI cost)
# and a production build of the web app. Nothing here touches the real database or the real servers.
#
#   tests/e2e/run.sh                 everything (about 17 minutes)
#   tests/e2e/run.sh acceptance      the seven acceptance scenarios of the brief, for the candidate model (about 2 minutes)
#   tests/e2e/run.sh crawl           every page for every role at 1280 px and 390 px
#   tests/e2e/run.sh checks          the two check lists at 1280 px and 390 px
#
# Ports: E2E_API_PORT (default 8011) and E2E_WEB_PORT (default 3011). Both must be free.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
WHAT="${1:-all}"
API_PORT="${E2E_API_PORT:-8011}"
WEB_PORT="${E2E_WEB_PORT:-3011}"
PY="$ROOT/.venv/bin/python"
BASE="http://127.0.0.1:$WEB_PORT"
LOGS="$(mktemp -d -t jobfinder-e2e-XXXXXX)"
api_pid=""
web_pid=""

cleanup() {
  [ -n "$web_pid" ] && kill "$web_pid" 2>/dev/null
  [ -n "$api_pid" ] && kill "$api_pid" 2>/dev/null
  wait 2>/dev/null
}
trap cleanup EXIT

in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
for port in "$API_PORT" "$WEB_PORT"; do
  if in_use "$port"; then
    echo "Port $port is in use. Stop what runs there, or set E2E_API_PORT / E2E_WEB_PORT."
    exit 2
  fi
done

wait_for() {                       # wait_for <url> <what>
  for _ in $(seq 1 180); do
    curl -fsS -o /dev/null "$1" 2>/dev/null && return 0
    sleep 1
  done
  echo "No answer from $2 ($1). Logs: $LOGS"
  exit 1
}

fresh_api() {                      # (re)start the demo API with newly seeded data
  if [ -n "$api_pid" ]; then
    kill "$api_pid" 2>/dev/null
    wait "$api_pid" 2>/dev/null
  fi
  E2E_PUBLIC_BASE_URL="$BASE" "$PY" -m tests.e2e.server --reset --port "$API_PORT" >> "$LOGS/api.log" 2>&1 &
  api_pid=$!
  wait_for "http://127.0.0.1:$API_PORT/api/health" "the demo API"
  [ -n "$web_pid" ] && wait_for "$BASE/api/health" "the demo API through the web app"
}

echo "== building the web app against the demo API (logs in $LOGS)"
[ -d frontend/node_modules ] || (cd frontend && npm ci > "$LOGS/npm.log" 2>&1) || { tail -20 "$LOGS/npm.log"; exit 1; }
(cd frontend && API_ORIGIN="http://127.0.0.1:$API_PORT" NEXT_DIST_DIR=.next-e2e npm run build > "$LOGS/build.log" 2>&1) || { tail -40 "$LOGS/build.log"; exit 1; }

fresh_api
cd frontend
API_ORIGIN="http://127.0.0.1:$API_PORT" NEXT_DIST_DIR=.next-e2e PORT="$WEB_PORT" node scripts/start.mjs > "$LOGS/web.log" 2>&1 &
web_pid=$!
cd "$ROOT"
wait_for "$BASE/login" "the web app"
wait_for "$BASE/api/health" "the demo API through the web app"

failed=""
run() {                            # run <name> <command...>
  echo
  echo "== $1"
  shift
  "$@" || failed="$failed\n  $*"
}

if [ "$WHAT" = "all" ] || [ "$WHAT" = "acceptance" ]; then
  run "acceptance scenarios 1 to 7 (1280 px)" env E2E_BASE_URL="$BASE" "$PY" -m pytest tests/e2e/test_acceptance.py -q
fi
if [ "$WHAT" = "all" ] || [ "$WHAT" = "crawl" ]; then
  run "every page for every role, 1280 px" "$PY" tests/e2e/ui_crawl.py --base "$BASE" --w 1280 --h 900
  run "every page for every role, 390 px" "$PY" tests/e2e/ui_crawl.py --base "$BASE" --w 390 --h 844
fi
if [ "$WHAT" = "all" ] || [ "$WHAT" = "checks" ]; then
  for size in "1280 900" "390 844"; do
    set -- $size
    fresh_api
    run "check list, $1 px" "$PY" tests/e2e/ui_checks.py --base "$BASE" --w "$1" --h "$2"
    fresh_api
    run "second check list, $1 px" "$PY" tests/e2e/ui_checks_more.py --base "$BASE" --w "$1" --h "$2"
  done
fi

echo
if [ -n "$failed" ]; then
  printf "FAILED:%b\n" "$failed"
  echo "Server logs: $LOGS"
  exit 1
fi
echo "All browser tests passed."
