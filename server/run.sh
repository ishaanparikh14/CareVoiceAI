#!/usr/bin/env bash
# run.sh — Launch the CareVoice AI FastAPI server on Linux / macOS
#
# Usage:
#   chmod +x run.sh
#   ./run.sh                        # default: host=0.0.0.0 port=8000 with reload
#   PORT=9000 ./run.sh              # override port via env var
#   RELOAD=0 ./run.sh               # disable hot-reload for production
#
# Prerequisites:
#   pip install -r requirements.txt

set -euo pipefail

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
LOG_LEVEL="${LOG_LEVEL:-info}"
RELOAD="${RELOAD:-1}"

# Always run from the directory containing this script so relative paths
# in config.py (BASE_DIR = Path(__file__).parent) resolve correctly.
cd "$(dirname "$0")"

echo ""
echo "  CareVoice AI — FastAPI Gateway"
echo "  http://${HOST}:${PORT}/docs    (Swagger UI)"
echo "  http://${HOST}:${PORT}/health  (health check)"
echo ""

RELOAD_FLAG=""
if [ "$RELOAD" = "1" ]; then
    RELOAD_FLAG="--reload"
fi

# Prefer the project virtualenv if present.
PYTHON="python"
if [ -x "../.venv/bin/python" ]; then
    PYTHON="../.venv/bin/python"
fi

exec "$PYTHON" -m uvicorn main:app \
    --host      "$HOST"      \
    --port      "$PORT"      \
    --log-level "$LOG_LEVEL" \
    $RELOAD_FLAG
