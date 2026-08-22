#!/usr/bin/env bash
# Start tractor_beam. Usage: ./run.sh [--lan] [port]
set -euo pipefail
cd "$(dirname "$0")"

HOST=127.0.0.1
if [ "${1:-}" = "--lan" ]; then HOST=0.0.0.0; shift; fi
PORT="${1:-877}"

exec .venv/bin/uvicorn app:app --host "$HOST" --port "$PORT" --log-level warning
