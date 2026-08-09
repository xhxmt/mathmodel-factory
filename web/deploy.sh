#!/usr/bin/env bash
# Paper Factory Web Dashboard - 快速部署脚本

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WEB_ROOT="/var/www/tfisher.de"
SERVICE_USER="${SERVICE_USER:-tfisher}"
MODE="${1:-full}"
SERVICE_NAME="${SERVICE_NAME:-paper-factory-api.service}"

# shellcheck source=backend_service_health.sh
source "$SCRIPT_DIR/backend_service_health.sh"

echo "════════════════════════════════════════"
echo "Paper Factory Web Dashboard 部署脚本"
echo "════════════════════════════════════════"
echo ""

# 颜色定义
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# 检查权限
if [ "$EUID" -ne 0 ] && [ "$MODE" != "backend-only" ]; then
    echo -e "${YELLOW}警告：前端部署需要 sudo 权限${NC}"
    echo "使用方法："
    echo "  sudo $0           # 完整部署（前端+后端）"
    echo "  $0 backend-only   # 仅更新后端（无需 sudo）"
    exit 1
fi

preflight_secret_loader() {
    (
        cd "$PROJECT_ROOT"
        source "$PROJECT_ROOT/scripts/load_secrets.sh" >/dev/null
    )
}

run_secret_loader_preflight() {
    if [ "$EUID" -eq 0 ]; then
        sudo -u "$SERVICE_USER" -H bash -lc "cd '$PROJECT_ROOT' && source '$PROJECT_ROOT/scripts/load_secrets.sh' >/dev/null"
    else
        preflight_secret_loader
    fi
}

check_env_file() {
    local file="$1"
    local sensitive_regex='^(MINERU_TOKEN|GEMINI_API_KEY|DEEPSEEK_API_KEY|DASHSCOPE_API_KEY|JWT_SECRET|JWT_SECRET_KEY|ADMIN_PASSWORD|TELEGRAM_BOT_TOKEN)='
    if [ ! -f "$file" ]; then
        return 0
    fi

    if grep -Eq "$sensitive_regex" "$file"; then
        echo -e "${RED}✗ $file 仍包含敏感键，请迁移到 Secret Manager${NC}"
        exit 1
    fi

    if [ -n "$(find "$file" -perm /077 -print -quit)" ]; then
        echo -e "${RED}✗ $file 权限过宽，请设置为 600${NC}"
        exit 1
    fi
}

preflight() {
    echo -e "${GREEN}► 部署预检${NC}"
    local script
    for script in \
        "$PROJECT_ROOT/scripts/load_secrets.sh" \
        "$PROJECT_ROOT/launch_agents.sh" \
        "$PROJECT_ROOT/run_paper.sh" \
        "$PROJECT_ROOT/factory_core/adapters/legacy_runner.sh" \
        "$PROJECT_ROOT/web/backend/start.sh" \
        "$PROJECT_ROOT/web/deploy.sh"; do
        bash -n "$script"
    done

    for lock in \
        "$PROJECT_ROOT/pyproject.toml" \
        "$PROJECT_ROOT/uv.lock" \
        "$PROJECT_ROOT/web/backend/requirements.lock" \
        "$PROJECT_ROOT/cloud/requirements.lock" \
        "$PROJECT_ROOT/web/frontend/package-lock.json"; do
        [[ -s "$lock" ]] || { echo -e "${RED}✗ missing reproducible-build input: $lock${NC}"; exit 1; }
    done
    "$PROJECT_ROOT/.venv/bin/python" - <<'PY'
from factory_core.steps import build_native_registry

steps = list(build_native_registry("."))
assert [step.id for step in steps] == list(range(17)), "native registry is incomplete"
PY

    check_env_file "$PROJECT_ROOT/.env"
    check_env_file "$PROJECT_ROOT/web/.env"
    run_secret_loader_preflight

    if command -v systemctl >/dev/null 2>&1 && systemctl cat paper-factory-api >/dev/null 2>&1; then
        if ! systemctl cat paper-factory-api | grep -q "scripts/load_secrets.sh"; then
            echo -e "${RED}✗ paper-factory-api.service 未加载 Secret Manager loader${NC}"
            exit 1
        fi
        if ! systemctl cat paper-factory-api | grep -Fq "WorkingDirectory=$PROJECT_ROOT"; then
            echo -e "${RED}✗ paper-factory-api.service 工作目录不是 $PROJECT_ROOT${NC}"
            exit 1
        fi
        if ! systemctl cat paper-factory-api | grep -Fq "$PROJECT_ROOT/.venv/bin/uvicorn apps.web.backend.main:app"; then
            echo -e "${RED}✗ paper-factory-api.service 未使用锁定环境或稳定 ASGI 入口${NC}"
            exit 1
        fi
    fi
    echo -e "${GREEN}✓ 部署预检通过${NC}"
}

deploy_frontend() {
    echo -e "${GREEN}► 步骤 1/4: 构建前端${NC}"
    if [ "$EUID" -eq 0 ]; then
        sudo -u "$SERVICE_USER" -H bash -lc \
            "cd '$PROJECT_ROOT/web/frontend' && npm ci && npm run build"
    else
        cd "$PROJECT_ROOT/web/frontend"
        npm ci
        npm run build
    fi
    if [ ! -f "$PROJECT_ROOT/web/frontend/dist/index.html" ]; then
        echo -e "${RED}✗ 前端构建未生成 dist/index.html${NC}"
        exit 1
    fi

    echo -e "${GREEN}► 步骤 2/4: 部署前端到 $WEB_ROOT${NC}"
    rm -rf "$WEB_ROOT"/*
    cp -r "$PROJECT_ROOT/web/frontend/dist"/* "$WEB_ROOT/"
    chown -R www-data:www-data "$WEB_ROOT"
    chmod -R 755 "$WEB_ROOT"

    echo -e "${GREEN}✓ 前端部署完成${NC}"
}

deploy_backend() {
    echo -e "${GREEN}► 步骤 3/4: 重启后端服务${NC}"
    if [ "$EUID" -eq 0 ]; then
        systemctl restart "$SERVICE_NAME"
    else
        sudo systemctl restart "$SERVICE_NAME"
    fi
}

wait_for_http() {
    local url="$1"
    local attempts="${2:-30}"
    local attempt
    for attempt in $(seq 1 "$attempts"); do
        if curl -sf "$url" > /dev/null; then
            return 0
        fi
        sleep 1
    done
    return 1
}

test_deployment() {
    echo ""
    echo -e "${GREEN}► 测试部署${NC}"
    local failed=0

    # 先验证 systemd 所有权和稳定性，再接受 HTTP 结果。这样旧会话留下的
    # rogue listener 无法把失败的正式服务伪装成部署成功。
    local backend_snapshot
    if backend_snapshot="$(verify_backend_service_stable)"; then
        echo -e "${GREEN}✓ 后端由正式 unit 持有并通过稳定窗口: $backend_snapshot${NC}"
    else
        echo -e "${RED}✗ 后端 systemd/PID/cgroup/HTTP 验收失败${NC}"
        systemctl status "$SERVICE_NAME" --no-pager || true
        failed=1
    fi

    # 测试前端
    if wait_for_http "https://tfisher.de/" 30; then
        echo -e "${GREEN}✓ 前端 HTTPS 访问正常${NC}"
    else
        echo -e "${RED}✗ canonical HTTPS 访问失败${NC}"
        failed=1
    fi

    if [ "$MODE" != "backend-only" ]; then
        local source_hash deployed_hash
        source_hash="$(sha256sum "$PROJECT_ROOT/web/frontend/dist/index.html" | awk '{print $1}')"
        deployed_hash="$(sha256sum "$WEB_ROOT/index.html" | awk '{print $1}')"
        if [ "$source_hash" = "$deployed_hash" ]; then
            echo -e "${GREEN}✓ 前端部署指纹一致${NC}"
        else
            echo -e "${RED}✗ 前端 dist 与生产 index.html 指纹不一致${NC}"
            failed=1
        fi
    fi

    if [ "$failed" -ne 0 ]; then
        return 1
    fi
}

show_summary() {
    echo ""
    echo "════════════════════════════════════════"
    echo -e "${GREEN}✅ 部署完成！${NC}"
    echo "════════════════════════════════════════"
    echo ""
    echo "🌐 访问地址：https://tfisher.de"
    echo "🔐 管理员凭据由 GCP Secret Manager 注入"
    echo ""
    echo "管理命令："
    echo "  查看日志：sudo journalctl -u paper-factory-api -f"
    echo "  重启服务：sudo systemctl restart paper-factory-api"
    echo "  服务状态：sudo systemctl status paper-factory-api"
    echo ""
}

# 主流程
if [ "$MODE" = "backend-only" ]; then
    echo "仅更新后端服务..."
    preflight
    deploy_backend
    test_deployment
    show_summary
    exit 0
fi

preflight
deploy_frontend
deploy_backend
test_deployment
show_summary
