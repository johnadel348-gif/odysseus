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

# Mobile networks drop connections constantly — be patient with retries.
PIP_FLAGS="--retries 8 --timeout 30"

python -m pip install $PIP_FLAGS --upgrade pip --quiet ||
  echo "[pocket] pip upgrade skipped (network) — bundled pip is fine, continuing…"

python -m pip install $PIP_FLAGS -r requirements.txt --quiet || {
  echo ""
  echo "[pocket] ✗ download failed — the connection dropped."
  echo "          Check your internet (Wi-Fi is steadier than mobile data),"
  echo "          then just run ./run.sh again — it resumes, nothing is lost."
  exit 1
}

# Keep Android from freezing the server while the screen is off (Termux only).
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock

exec python server.py
