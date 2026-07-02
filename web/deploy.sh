#!/bin/bash
# Paper Factory Web Dashboard - 快速部署脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WEB_ROOT="/var/www/tfisher.de"
SERVICE_USER="${SERVICE_USER:-tfisher}"

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
if [ "$EUID" -ne 0 ] && [ "$1" != "backend-only" ]; then
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
    local sensitive_regex='^(MINERU_TOKEN|GEMINI_API_KEY|DEEPSEEK_API_KEY|JWT_SECRET|JWT_SECRET_KEY|ADMIN_PASSWORD)='
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
        "$PROJECT_ROOT/web/backend/start.sh" \
        "$PROJECT_ROOT/web/deploy.sh"; do
        bash -n "$script"
    done

    check_env_file "$PROJECT_ROOT/.env"
    check_env_file "$PROJECT_ROOT/web/.env"
    run_secret_loader_preflight

    if command -v systemctl >/dev/null 2>&1 && systemctl cat paper-factory-api >/dev/null 2>&1; then
        if ! systemctl cat paper-factory-api | grep -q "scripts/load_secrets.sh"; then
            echo -e "${RED}✗ paper-factory-api.service 未加载 Secret Manager loader${NC}"
            exit 1
        fi
    fi
    echo -e "${GREEN}✓ 部署预检通过${NC}"
}

deploy_frontend() {
    echo -e "${GREEN}► 步骤 1/4: 构建前端${NC}"
    cd "$PROJECT_ROOT/web/frontend"
    npm run build

    echo -e "${GREEN}► 步骤 2/4: 部署前端到 $WEB_ROOT${NC}"
    rm -rf "$WEB_ROOT"/*
    cp -r dist/* "$WEB_ROOT/"
    chown -R www-data:www-data "$WEB_ROOT"
    chmod -R 755 "$WEB_ROOT"

    echo -e "${GREEN}✓ 前端部署完成${NC}"
}

deploy_backend() {
    echo -e "${GREEN}► 步骤 3/4: 重启后端服务${NC}"
    systemctl restart paper-factory-api
    sleep 2

    echo -e "${GREEN}► 步骤 4/4: 检查服务状态${NC}"
    if systemctl is-active --quiet paper-factory-api; then
        echo -e "${GREEN}✓ 后端服务运行正常${NC}"
    else
        echo -e "${RED}✗ 后端服务启动失败${NC}"
        systemctl status paper-factory-api --no-pager
        exit 1
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

    # 测试后端
    if wait_for_http "http://127.0.0.1:8000/" 30; then
        echo -e "${GREEN}✓ 后端 API 响应正常${NC}"
    else
        echo -e "${RED}✗ 后端 API 无响应${NC}"
        failed=1
    fi

    # 测试前端
    if wait_for_http "https://tfisher.de/" 10; then
        echo -e "${GREEN}✓ 前端 HTTPS 访问正常${NC}"
    else
        echo -e "${YELLOW}! 前端访问测试失败（可能需要等待 nginx 重载）${NC}"
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
    echo "🔐 登录账号由 web/.env 中的管理员配置决定"
    echo ""
    echo "管理命令："
    echo "  查看日志：sudo journalctl -u paper-factory-api -f"
    echo "  重启服务：sudo systemctl restart paper-factory-api"
    echo "  服务状态：sudo systemctl status paper-factory-api"
    echo ""
}

# 主流程
if [ "$1" = "backend-only" ]; then
    echo "仅更新后端服务..."
    preflight
    sudo systemctl restart paper-factory-api
    sudo systemctl status paper-factory-api --no-pager
    exit 0
fi

preflight
deploy_frontend
deploy_backend
test_deployment
show_summary
