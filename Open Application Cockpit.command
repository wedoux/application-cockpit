#!/bin/bash
# Double-click this file to start Application Cockpit and open it in your
# browser at http://127.0.0.1:8766. Leave this Terminal window open while
# you use it; close it (or Ctrl+C) when you're done.
cd "$(dirname "$0")" || exit 1

if [ -x ".venv/bin/python3" ]; then
  PY=".venv/bin/python3"
else
  PY="python3"
fi

echo "Starting Application Cockpit…  (close this window to stop it)"
exec "$PY" app.py
