#!/bin/bash
# Double-click to launch. Prefer Python 3 from PATH, then /usr/bin/python3.
cd "$(dirname "$0")" || exit 1
PY="$(command -v python3 2>/dev/null)"
[ -x "$PY" ] || PY=/usr/bin/python3
if [ ! -x "$PY" ]; then
  echo "Python 3 was not found. Install Python 3 or the Xcode Command Line Tools:"
  echo "xcode-select --install"
  read -r -p "Press Return to close this window…"
  exit 1
fi
exec "$PY" server.py --open
