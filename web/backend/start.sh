#!/bin/bash
# Start the Paper Factory Dashboard backend server

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 🔒 Load secrets from GCP Secret Manager
if [[ -f "$SCRIPT_DIR/../../scripts/load_secrets.sh" ]]; then
    source "$SCRIPT_DIR/../../scripts/load_secrets.sh" 2>/dev/null || echo "Warning: Failed to load secrets from Secret Manager" >&2
fi

# Check if virtual environment exists
if [[ ! -d "venv" ]]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install/update dependencies
echo "Installing dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

# Start server
echo "Starting Paper Factory Dashboard backend on http://127.0.0.1:8000"
python3 app.py
