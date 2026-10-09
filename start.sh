#!/bin/sh
# agy/deck v3 — start server
# Usage: ./start.sh  |  PORT=3000 ./start.sh  |  HOST=0.0.0.0 ./start.sh
set -u
PORT="${PORT:-8080}"
HOST="${HOST:-127.0.0.1}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/server.pid"
LOG="$SCRIPT_DIR/server.log"

if [ -f "$PID_FILE" ]; then
  PID="$(cat "$PID_FILE")"
  if [ -n "$PID" ] && ps -p "$PID" >/dev/null 2>&1; then
    echo "sudah berjalan (PID $PID) — http://$HOST:$PORT"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

# python with venv preference
if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
  PY="$SCRIPT_DIR/.venv/bin/python"
else
  PY="python3"
fi

# ensure deps
if ! "$PY" -c "import fastapi, uvicorn" 2>/dev/null; then
  echo ">> install dependensi (fastapi, uvicorn)…"
  "$PY" -m pip install -r "$SCRIPT_DIR/requirements.txt" || {
    echo "gagal install. coba manual: $PY -m pip install -r requirements.txt"; exit 1; }
fi

echo ">> start agy/deck di $HOST:$PORT …"
cd "$SCRIPT_DIR" || exit 1
# pass-through env (env wins over .env values)
ENV_PASSTHROUGH=""
for v in WORKSPACE_DIR AGY_MOCK AGY_YOLO AGY_TURN_TIMEOUT AGY_DEFAULT_MODEL GEMINI_API_KEY AGY_MODEL_PROVIDER; do
  val="$(eval "echo \${$v:-}")"
  if [ -n "$val" ]; then ENV_PASSTHROUGH="$ENV_PASSTHROUGH $v=$val"; fi
done
# shellcheck disable=SC2086
PORT="$PORT" HOST="$HOST" env $ENV_PASSTHROUGH setsid "$PY" -m uvicorn server.main:app \
  --host "$HOST" --port "$PORT" --log-level warning >"$LOG" 2>&1 &
echo $! > "$PID_FILE"
sleep 2
if ps -p "$(cat "$PID_FILE")" >/dev/null 2>&1; then
  echo "== OK (PID $(cat "$PID_FILE")) =="
  echo "   http://$HOST:$PORT"
  echo "   token ada di server log / file .auth_token"
  grep -a "Auth token" "$LOG" | tail -1
else
  echo "!! gagal start. lihat $LOG"; tail -20 "$LOG"; rm -f "$PID_FILE"; exit 1
fi
