#!/usr/bin/env bash
# Odysseus Pocket launcher — works in Termux, Linux, and macOS.
# Creates a local venv on first run, installs the 3 dependencies, starts the app.
set -e
cd "$(dirname "$0")"

PY=python
command -v "$PY" >/dev/null 2>&1 || PY=python3

if [ ! -d .venv ]; then
  echo "[pocket] creating virtual environment (first run only)…"
  "$PY" -m venv .venv
fi

# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet

# Keep Android from freezing the server while the screen is off (Termux only).
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock

exec python server.py
