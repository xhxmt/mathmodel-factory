#!/bin/bash
# Paper Factory Web Dashboard - Final Verification
# 最终验证脚本，确保所有组件正常工作

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "╔════════════════════════════════════════════════════════╗"
echo "║   Paper Factory Dashboard - Final Verification        ║"
echo "╚════════════════════════════════════════════════════════╝"
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

pass() { echo -e "${GREEN}✓${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }
info() { echo -e "${YELLOW}ℹ${NC} $1"; }

# 1. Check directory structure
echo "1. Checking directory structure..."
[[ -d "backend" ]] || fail "backend/ directory missing"
[[ -d "frontend" ]] || fail "frontend/ directory missing"
[[ -f "backend/app.py" ]] || fail "backend/app.py missing"
[[ -f "frontend/src/App.vue" ]] || fail "frontend/src/App.vue missing"
pass "Directory structure OK"

# 2. Check backend
echo ""
echo "2. Checking backend..."
[[ -d "backend/venv" ]] || fail "Backend venv not created (run: cd backend && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt)"
[[ -f "backend/venv/bin/python3" ]] || fail "Python venv broken"

# Test import
cd backend
source venv/bin/activate
python3 -c "import app" 2>/dev/null || fail "Backend import failed"
pass "Backend imports OK"
cd ..

# 3. Check frontend
echo ""
echo "3. Checking frontend..."
[[ -d "frontend/node_modules" ]] || fail "Frontend node_modules missing (run: cd frontend && npm install)"
[[ -f "frontend/node_modules/.bin/vite" ]] || fail "Vite not installed"
pass "Frontend dependencies OK"

# 4. Check documentation
echo ""
echo "4. Checking documentation..."
[[ -f "README.md" ]] || fail "README.md missing"
[[ -f "INTERFACE_GUIDE.md" ]] || fail "INTERFACE_GUIDE.md missing"
[[ -f "QUICKSTART.txt" ]] || fail "QUICKSTART.txt missing"
[[ -f "PROJECT_SUMMARY.md" ]] || fail "PROJECT_SUMMARY.md missing"
pass "Documentation complete"

# 5. Check scripts
echo ""
echo "5. Checking scripts..."
[[ -x "start_dashboard.sh" ]] || fail "start_dashboard.sh not executable"
[[ -x "test_dashboard.sh" ]] || fail "test_dashboard.sh not executable"
[[ -x "backend/start.sh" ]] || fail "backend/start.sh not executable"
[[ -x "frontend/start.sh" ]] || fail "frontend/start.sh not executable"
pass "All scripts executable"

# 6. Test backend startup
echo ""
echo "6. Testing backend startup..."
cd backend
source venv/bin/activate
timeout 5 python3 app.py >/dev/null 2>&1 &
BACKEND_PID=$!
sleep 2

if curl -s http://127.0.0.1:8000/ | grep -q "Paper Factory"; then
    pass "Backend responds to HTTP"
else
    kill $BACKEND_PID 2>/dev/null || true
    fail "Backend not responding"
fi

if curl -s http://127.0.0.1:8000/api/projects | grep -q "\["; then
    pass "API endpoint OK"
else
    kill $BACKEND_PID 2>/dev/null || true
    fail "API endpoint failed"
fi

kill $BACKEND_PID 2>/dev/null || true
wait $BACKEND_PID 2>/dev/null || true
cd ..

# 7. Check demo projects
echo ""
echo "7. Checking demo projects..."
if ls ../ongoing/demo_* >/dev/null 2>&1 || ls ../complete/demo_* >/dev/null 2>&1; then
    DEMO_COUNT=$(find ../ongoing ../complete -maxdepth 1 -name "demo_*" -type d 2>/dev/null | wc -l)
    pass "Found $DEMO_COUNT demo project(s)"
else
    info "No demo projects (run: ./create_demo_projects.py)"
fi

# 8. Summary
echo ""
echo "╔════════════════════════════════════════════════════════╗"
echo "║              Verification Complete ✓                   ║"
echo "╚════════════════════════════════════════════════════════╝"
echo ""
echo "All checks passed! Ready to use."
echo ""
echo "📚 Quick Start:"
echo "   ./start_dashboard.sh"
echo ""
echo "📖 Documentation:"
echo "   cat QUICKSTART.txt"
echo "   cat README.md"
echo ""
echo "🧪 Create demo projects (for testing):"
echo "   ./create_demo_projects.py"
echo ""
echo "🌐 Access URL:"
echo "   http://localhost:5173"
echo ""
