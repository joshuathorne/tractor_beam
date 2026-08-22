#!/usr/bin/env bash
# Start tractor_beam. Usage: ./run.sh [--lan] [port]
set -euo pipefail
cd "$(dirname "$0")"

HOST=127.0.0.1
if [ "${1:-}" = "--lan" ]; then HOST=0.0.0.0; shift; fi
PORT="${1:-8877}"

# A venv bakes its absolute path into every script it installs, so it breaks
# if this directory is moved. Rebuild when it's missing or points at a python
# that no longer exists.
interp=""
[ -f .venv/bin/uvicorn ] && interp=$(sed -n '1s/^#!//p' .venv/bin/uvicorn)
if [ -z "$interp" ] || [ ! -x "$interp" ]; then
  echo "Building .venv (this takes a minute)..."
  rm -rf .venv
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

exec .venv/bin/uvicorn app:app --host "$HOST" --port "$PORT" --log-level warning
