#!/bin/bash
# Start the Paper Factory Dashboard frontend

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Check if node_modules exists
if [[ ! -d "node_modules" ]]; then
    echo "Installing dependencies..."
    npm install
fi

# Start development server
echo "Starting Paper Factory Dashboard frontend on http://localhost:5173"
npm run dev
