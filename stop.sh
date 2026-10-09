#!/bin/sh
# agy/deck v3 — stop server
set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/server.pid"

if [ -f "$PID_FILE" ]; then
  PID="$(cat "$PID_FILE")"
  if [ -n "$PID" ] && ps -p "$PID" >/dev/null 2>&1; then
    echo ">> stop PID $PID …"
    kill "$PID" 2>/dev/null; sleep 2
    if ps -p "$PID" >/dev/null 2>&1; then kill -9 "$PID" 2>/dev/null; fi
    rm -f "$PID_FILE"; echo "OK berhenti."
    exit 0
  fi
  rm -f "$PID_FILE"
fi
# fallback: kill by pattern
PIDS="$(pgrep -f "server.main:app" 2>/dev/null || true)"
if [ -n "$PIDS" ]; then
  echo ">> stop by pattern: $PIDS"; kill $PIDS 2>/dev/null; sleep 1
  echo "OK berhenti."
else
  echo "tidak ada server berjalan."
fi
