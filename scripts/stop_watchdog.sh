#!/usr/bin/env bash
# Stop ONLY the watchdog process recorded in state/watchdog.pid (and only if it really is the watchdog).
# Never touches Hermes, Strata or llama.cpp.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIDFILE="$ROOT/state/watchdog.pid"
[ -f "$PIDFILE" ] || { echo "no pid file: watchdog not running"; exit 0; }
PID="$(cat "$PIDFILE")"
if ! kill -0 "$PID" 2>/dev/null; then echo "stale pid file removed"; rm -f "$PIDFILE"; exit 0; fi
if [ -r "/proc/$PID/cmdline" ] && ! tr '\0' ' ' < "/proc/$PID/cmdline" | grep -q "antiloop_watchdog"; then
  echo "pid $PID is not the watchdog; refusing to signal it" >&2; exit 1
fi
kill -TERM "$PID"
for _ in $(seq 1 50); do
  kill -0 "$PID" 2>/dev/null || { echo "watchdog stopped"; exit 0; }
  sleep 0.2
done
echo "watchdog (pid $PID) did not exit within 10 s; it was NOT force-killed" >&2
exit 1
