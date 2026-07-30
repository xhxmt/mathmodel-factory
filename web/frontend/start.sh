#!/bin/bash
# Start the Paper Factory Dashboard frontend

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -d "node_modules" ]]; then
    echo "ERROR: dependencies missing. Run: npm ci" >&2
    exit 1
fi

# Start development server
echo "Starting Paper Factory Dashboard frontend on http://localhost:5173"
exec npm run dev
