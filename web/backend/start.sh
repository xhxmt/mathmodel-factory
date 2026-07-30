#!/bin/bash
# Start the Paper Factory Dashboard backend server

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
export PYTHONPATH="$SCRIPT_DIR/../..:${PYTHONPATH:-}"

# 🔒 Load secrets from GCP Secret Manager
if [[ -f "$SCRIPT_DIR/../../scripts/load_secrets.sh" ]]; then
    source "$SCRIPT_DIR/../../scripts/load_secrets.sh"
else
    echo "ERROR: missing $SCRIPT_DIR/../../scripts/load_secrets.sh" >&2
    exit 1
fi

PYTHON_BIN="$SCRIPT_DIR/../../.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "ERROR: locked environment missing. Run: uv sync --extra web --extra models --locked" >&2
    exit 1
fi

# Start server
echo "Starting Paper Factory Dashboard backend on http://127.0.0.1:8000"
exec "$PYTHON_BIN" -m web.backend.main
