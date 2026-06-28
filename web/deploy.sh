#!/bin/bash
# Paper Factory Web Dashboard - 快速部署脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WEB_ROOT="/var/www/tfisher.de"

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

test_deployment() {
    echo ""
    echo -e "${GREEN}► 测试部署${NC}"

    # 测试后端
    if curl -sf http://127.0.0.1:8000/ > /dev/null; then
        echo -e "${GREEN}✓ 后端 API 响应正常${NC}"
    else
        echo -e "${RED}✗ 后端 API 无响应${NC}"
    fi

    # 测试前端
    if curl -sf https://tfisher.de/ > /dev/null; then
        echo -e "${GREEN}✓ 前端 HTTPS 访问正常${NC}"
    else
        echo -e "${YELLOW}! 前端访问测试失败（可能需要等待 nginx 重载）${NC}"
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
    sudo systemctl restart paper-factory-api
    sudo systemctl status paper-factory-api --no-pager
    exit 0
fi

deploy_frontend
deploy_backend
test_deployment
show_summary
