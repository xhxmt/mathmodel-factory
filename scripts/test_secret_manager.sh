#!/usr/bin/env bash
# Paper Factory - Secret Manager 集成测试脚本

set -euo pipefail

FACTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$FACTORY"

echo "=========================================="
echo "  Secret Manager 集成测试"
echo "=========================================="
echo ""

# 1. 检查脚本是否存在
echo "1. 检查必需文件..."
if [[ ! -f "scripts/load_secrets.sh" ]]; then
    echo "❌ scripts/load_secrets.sh 不存在"
    exit 1
fi
if [[ ! -f "scripts/setup_secret_manager.sh" ]]; then
    echo "❌ scripts/setup_secret_manager.sh 不存在"
    exit 1
fi
echo "✓ 必需文件存在"
echo ""

# 2. 检查 gcloud 配置
echo "2. 检查 GCP 配置..."
if ! gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null | grep -q "@"; then
    echo "❌ gcloud 未登录，请运行: gcloud auth login"
    exit 1
fi
PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
echo "✓ GCP 项目: $PROJECT_ID"
echo ""

# 3. 检查 Secret Manager API
echo "3. 检查 Secret Manager API..."
if gcloud services list --enabled --filter="name:secretmanager.googleapis.com" 2>/dev/null | grep -q secretmanager; then
    echo "✓ Secret Manager API 已启用"
else
    echo "⚠️  Secret Manager API 未启用"
    read -p "   是否现在启用? (y/n): " enable_api
    if [[ "$enable_api" == "y" ]]; then
        gcloud services enable secretmanager.googleapis.com --quiet
        echo "✓ Secret Manager API 已启用"
    else
        echo "❌ 跳过，无法继续"
        exit 1
    fi
fi
echo ""

# 4. 测试加载 secrets
echo "4. 测试加载 secrets..."
if source scripts/load_secrets.sh 2>&1 | grep -q "successfully"; then
    echo "✓ Secrets 加载成功"

    # 验证关键变量
    if [[ -n "${MINERU_TOKEN:-}" ]]; then
        echo "  ✓ MINERU_TOKEN: ${MINERU_TOKEN:0:20}..."
    else
        echo "  ⚠️  MINERU_TOKEN 未加载"
    fi

    if [[ -n "${GEMINI_API_KEY:-}" ]]; then
        echo "  ✓ GEMINI_API_KEY: ${GEMINI_API_KEY:0:20}..."
    else
        echo "  ⚠️  GEMINI_API_KEY 未加载"
    fi

    if [[ -n "${JWT_SECRET:-}" ]]; then
        echo "  ✓ JWT_SECRET: ${JWT_SECRET:0:20}..."
    else
        echo "  ⚠️  JWT_SECRET 未加载"
    fi
else
    echo "⚠️  Secrets 未创建或加载失败"
    echo ""
    echo "是否运行配置向导创建 Secrets?"
    read -p "(y/n): " run_setup
    if [[ "$run_setup" == "y" ]]; then
        ./scripts/setup_secret_manager.sh
        echo ""
        echo "配置完成，请重新运行此测试脚本"
        exit 0
    else
        echo "跳过配置"
    fi
fi
echo ""

# 5. 检查集成点
echo "5. 检查集成点..."
if grep -q "load_secrets.sh" launch_agents.sh; then
    echo "✓ launch_agents.sh 已集成"
else
    echo "❌ launch_agents.sh 未集成"
fi

if grep -q "load_secrets.sh" web/backend/start.sh; then
    echo "✓ web/backend/start.sh 已集成"
else
    echo "❌ web/backend/start.sh 未集成"
fi
echo ""

# 6. 测试 Web Dashboard 启动
echo "6. 测试 Web Dashboard..."
if curl -s http://127.0.0.1:8000/ >/dev/null 2>&1; then
    echo "✓ Web Dashboard 正在运行 (http://127.0.0.1:8000)"

    # 测试认证
    LOGIN_RESPONSE=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login \
        -H "Content-Type: application/json" \
        -d '{"username":"admin","password":"'"${ADMIN_PASSWORD:-admin123}"'"}' 2>/dev/null || echo "{}")

    if echo "$LOGIN_RESPONSE" | jq -e '.token' >/dev/null 2>&1; then
        echo "✓ 认证测试通过（JWT Secret 工作正常）"
    else
        echo "⚠️  认证测试失败，检查 JWT_SECRET 和 ADMIN_PASSWORD"
    fi
else
    echo "⚠️  Web Dashboard 未运行"
    echo "   启动命令: cd web && ./start_dashboard.sh"
fi
echo ""

# 7. 检查 Secret 列表
echo "7. GCP Secret Manager 状态..."
SECRET_COUNT=$(gcloud secrets list --format="value(name)" 2>/dev/null | wc -l)
if [[ $SECRET_COUNT -gt 0 ]]; then
    echo "✓ 已创建 $SECRET_COUNT 个 Secrets:"
    gcloud secrets list --format="table(name,createTime)" 2>/dev/null | head -10
else
    echo "⚠️  未找到任何 Secrets"
fi
echo ""

# 8. 安全建议
echo "=========================================="
echo "  安全建议"
echo "=========================================="
echo ""
if grep -qE "^(MINERU_TOKEN|GEMINI_API_KEY|DEEPSEEK_API_KEY|JWT_SECRET|ADMIN_PASSWORD)=" .env 2>/dev/null; then
    echo "⚠️  .env 文件仍包含明文密钥"
    echo ""
    echo "建议操作:"
    echo "  1. 备份已完成: .env.backup"
    echo "  2. 清理明文密钥:"
    echo "     mv .env.safe .env"
    echo "     mv web/.env.safe web/.env"
    echo ""
else
    echo "✓ .env 文件已清理，无明文密钥"
fi

echo "访问审计:"
echo "  gcloud logging read 'resource.type=\"secretmanager.googleapis.com/Secret\"' --limit=10"
echo ""

echo "=========================================="
echo "  测试完成"
echo "=========================================="
echo ""
