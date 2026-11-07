#!/usr/bin/env bash
# Start the watchdog in the background (WSL / Linux / macOS). No global installs: uses ./.venv (stdlib only).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python3}"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
mkdir -p state logs
if [ -f state/watchdog.pid ] && kill -0 "$(cat state/watchdog.pid)" 2>/dev/null; then
  echo "watchdog already running (pid $(cat state/watchdog.pid))"; exit 0
fi
nohup .venv/bin/python -m antiloop_watchdog "$@" >> logs/stdout.log 2>&1 &
URL="${WATCHDOG_URL:-http://127.0.0.1:8791}"
for _ in $(seq 1 50); do
  if .venv/bin/python tools/healthcheck.py --url "$URL" >/dev/null 2>&1; then
    echo "watchdog is up at $URL (pid $(cat state/watchdog.pid 2>/dev/null || echo '?'))"
    echo "API token file: $ROOT/state/token"
    exit 0
  fi
  sleep 0.2
done
echo "watchdog did not become healthy; see $ROOT/logs/stdout.log" >&2
exit 1
