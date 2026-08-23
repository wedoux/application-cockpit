#!/usr/bin/env bash
# Kill whatever holds the dev port, start app.py fresh, wait until it actually
# responds, and print the git sha it's serving.
#
# Exists because Flask's dev server (debug=False, no reloader) does not pick up
# code changes — a stale process kept answering requests with old code twice in
# this build's history and produced false test passes. Always use this instead
# of starting app.py by hand.

set -euo pipefail
cd "$(dirname "$0")"

PORT=8766
LOG=/tmp/cockpit_dev_server.log

echo "Stopping anything on port $PORT..."
PIDS=$(lsof -ti ":$PORT" 2>/dev/null || true)
if [ -n "$PIDS" ]; then
  echo "$PIDS" | xargs kill
  sleep 1
fi

echo "Starting app.py..."
.venv/bin/python3 app.py --no-open > "$LOG" 2>&1 &
SERVER_PID=$!

echo "Waiting for http://127.0.0.1:$PORT ..."
UP=0
for _ in $(seq 1 20); do
  if curl -s -o /dev/null "http://127.0.0.1:$PORT/"; then
    UP=1
    break
  fi
  sleep 0.5
done

if [ "$UP" -ne 1 ]; then
  echo "Server did not respond after 10s — check $LOG" >&2
  exit 1
fi

SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown (not a git checkout)")
DIRTY=""
if ! git diff --quiet -- . 2>/dev/null || ! git diff --cached --quiet -- . 2>/dev/null; then
  DIRTY=" + uncommitted changes in Job search tool/:"
fi

echo "Cockpit is live at http://127.0.0.1:$PORT (pid $SERVER_PID)"
echo "Serving git sha: $SHA$DIRTY"
if [ -n "$DIRTY" ]; then
  git status --short -- . 2>/dev/null | sed 's/^/  /'
fi
