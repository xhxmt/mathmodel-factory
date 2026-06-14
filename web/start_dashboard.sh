#!/bin/bash
# Paper Factory Dashboard - One-click launcher
# Starts both backend and frontend servers

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FACTORY_ROOT="$SCRIPT_DIR/.."

echo "=========================================="
echo "   Paper Factory Dashboard Launcher"
echo "=========================================="
echo ""

# Function to cleanup on exit
cleanup() {
    echo ""
    echo "Shutting down servers..."
    if [[ -n "$BACKEND_PID" ]]; then
        kill $BACKEND_PID 2>/dev/null || true
    fi
    if [[ -n "$FRONTEND_PID" ]]; then
        kill $FRONTEND_PID 2>/dev/null || true
    fi
    exit 0
}

trap cleanup SIGINT SIGTERM

# Start backend
echo "Starting backend server..."
cd "$SCRIPT_DIR/backend"
./start.sh &
BACKEND_PID=$!
echo "Backend PID: $BACKEND_PID"
sleep 3

# Wait for backend to be ready
echo "Waiting for backend..."
for i in {1..10}; do
    if curl -s http://127.0.0.1:8000/ >/dev/null 2>&1; then
        echo "✓ Backend ready"
        break
    fi
    if [[ $i -eq 10 ]]; then
        echo "✗ Backend failed to start"
        cleanup
    fi
    sleep 1
done

# Start frontend
echo ""
echo "Starting frontend server..."
cd "$SCRIPT_DIR/frontend"
./start.sh &
FRONTEND_PID=$!
echo "Frontend PID: $FRONTEND_PID"

echo ""
echo "=========================================="
echo "   Dashboard is ready!"
echo "=========================================="
echo ""
echo "   Frontend: http://localhost:5173"
echo "   Backend:  http://localhost:8000"
echo ""
echo "Press Ctrl+C to stop both servers"
echo ""

# Wait for user interrupt
wait
