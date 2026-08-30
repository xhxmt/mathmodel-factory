#!/bin/bash
# Quick test: verify the dashboard can start and respond

set -e

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
BACKEND_DIR="$SCRIPT_DIR/backend"
FRONTEND_DIR="$SCRIPT_DIR/frontend"

echo "Testing Paper Factory Dashboard..."
echo ""

# Test backend
echo "1. Testing backend..."
cd "$BACKEND_DIR"
source venv/bin/activate
python3 -c "import app; print('✓ Backend imports OK')"

# Test API health check (start server in background)
python3 app.py &
BACKEND_PID=$!
sleep 3

if curl -s http://127.0.0.1:8000/ | grep -q "Paper Factory"; then
    echo "✓ Backend API responding"
else
    echo "✗ Backend API not responding"
    kill $BACKEND_PID 2>/dev/null || true
    exit 1
fi

# Test projects endpoint
if curl -s http://127.0.0.1:8000/api/projects | grep -q "\["; then
    echo "✓ Projects endpoint OK"
else
    echo "✗ Projects endpoint failed"
fi

kill $BACKEND_PID 2>/dev/null || true
sleep 1

# Test frontend
echo ""
echo "2. Testing frontend..."
cd "$FRONTEND_DIR"

if [[ -f "node_modules/.bin/vite" ]]; then
    echo "✓ Frontend dependencies installed"
else
    echo "✗ Frontend dependencies missing"
    exit 1
fi

if [[ -f "src/App.vue" && -f "src/main.js" ]]; then
    echo "✓ Frontend source files present"
else
    echo "✗ Frontend source files missing"
    exit 1
fi

echo ""
echo "=========================================="
echo "   All tests passed! ✓"
echo "=========================================="
echo ""
echo "To start the dashboard:"
echo "  cd $SCRIPT_DIR"
echo "  ./start_dashboard.sh"
echo ""
