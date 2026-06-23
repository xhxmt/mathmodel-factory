#!/usr/bin/env bash
# Paper Factory GCP Secret Manager 配置向导
# 一键迁移敏感配置到 Secret Manager

set -euo pipefail

FACTORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$FACTORY_ROOT"

echo "=========================================="
echo "  GCP Secret Manager 配置向导"
echo "=========================================="
echo ""

# 检查 gcloud 是否已登录
if ! gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null | grep -q "@"; then
    echo "❌ 错误: 请先运行 'gcloud auth login' 登录 GCP"
    exit 1
fi

PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
echo "当前 GCP 项目: $PROJECT_ID"
echo ""

# 启用 Secret Manager API
echo "启用 Secret Manager API..."
gcloud services enable secretmanager.googleapis.com --quiet
echo "✓ Secret Manager API 已启用"
echo ""

# 函数: 创建或更新 Secret
create_or_update_secret() {
    local secret_name="$1"
    local secret_value="$2"

    if [[ -z "$secret_value" ]]; then
        echo "⚠️  跳过 $secret_name (值为空)"
        return
    fi

    # 检查 Secret 是否已存在
    if gcloud secrets describe "$secret_name" >/dev/null 2>&1; then
        echo -n "  更新 $secret_name..."
        echo -n "$secret_value" | gcloud secrets versions add "$secret_name" --data-file=- 2>/dev/null
        echo " ✓"
    else
        echo -n "  创建 $secret_name..."
        echo -n "$secret_value" | gcloud secrets create "$secret_name" --data-file=- 2>/dev/null
        echo " ✓"
    fi
}

# 从 .env 文件读取配置
echo "从配置文件读取敏感信息..."
echo ""

MINERU_TOKEN=$(grep -E "^MINERU_TOKEN=" .env 2>/dev/null | cut -d= -f2- || echo "")
GEMINI_API_KEY=$(grep -E "^GEMINI_API_KEY=" .env 2>/dev/null | cut -d= -f2- || echo "")
DEEPSEEK_API_KEY=$(grep -E "^DEEPSEEK_API_KEY=" .env 2>/dev/null | cut -d= -f2- || echo "")
JWT_SECRET=$(grep -E "^JWT_SECRET=" web/.env 2>/dev/null | cut -d= -f2- || echo "")
ADMIN_PASSWORD=$(grep -E "^ADMIN_PASSWORD=" web/.env 2>/dev/null | cut -d= -f2- || echo "")

# 创建 Secrets
echo "开始创建/更新 Secrets..."
create_or_update_secret "mineru-token" "$MINERU_TOKEN"
create_or_update_secret "gemini-api-key" "$GEMINI_API_KEY"
create_or_update_secret "deepseek-api-key" "$DEEPSEEK_API_KEY"
create_or_update_secret "dashboard-jwt-secret" "$JWT_SECRET"
create_or_update_secret "dashboard-admin-password" "$ADMIN_PASSWORD"

echo ""
echo "=========================================="
echo "  配置完成！"
echo "=========================================="
echo ""

# 列出所有 Secrets
echo "已创建的 Secrets:"
gcloud secrets list --format="table(name,createTime)"
echo ""

# 测试访问
echo "测试 Secret 访问..."
if source scripts/load_secrets.sh 2>&1 | grep -q "successfully"; then
    echo "✓ Secret 访问测试通过"
else
    echo "⚠️  Secret 访问测试失败，请检查权限"
fi
echo ""

# 备份原始 .env
echo "备份原始配置文件..."
cp .env .env.backup
cp web/.env web/.env.backup
echo "✓ 备份已保存: .env.backup, web/.env.backup"
echo ""

# 生成清理后的 .env（移除敏感信息）
echo "生成清理后的配置文件..."
cat > .env.safe <<'EOF'
# Paper Factory Configuration (Secrets moved to GCP Secret Manager)
# 敏感信息已迁移到 GCP Secret Manager
# 使用方法: source scripts/load_secrets.sh

# ==========================================
# Secret Manager Secrets (不要在此填写真实值)
# ==========================================
# MINERU_TOKEN=<从 Secret Manager 加载>
# GEMINI_API_KEY=<从 Secret Manager 加载>
# DEEPSEEK_API_KEY=<从 Secret Manager 加载>

# ==========================================
# Cloud Solver 配置
# ==========================================
USE_CLOUD_SOLVER=false
CLOUD_THRESHOLD_TIME=300
CLOUD_SOLVER_TYPES=python,julia,matlab,R

GCP_PROJECT_ID=level-night-476302-k0
GCP_REGION=europe-west4
GCP_SOLVER_SERVICE=solver-api
GCP_SOLVER_BUCKET=level-night-476302-k0-solver-jobs

# ==========================================
# Cloud Logging
# ==========================================
USE_CLOUD_LOGGING=false
EOF

cat > web/.env.safe <<'EOF'
# Web Dashboard Configuration (Secrets moved to GCP Secret Manager)

# ==========================================
# Secret Manager Secrets (不要在此填写真实值)
# ==========================================
# JWT_SECRET=<从 Secret Manager 加载>
# ADMIN_PASSWORD=<从 Secret Manager 加载>

# ==========================================
# Server Configuration
# ==========================================
API_HOST=0.0.0.0
API_PORT=8000
CORS_ORIGINS=http://localhost:5173,http://localhost:3000

# ==========================================
# Logging
# ==========================================
LOG_LEVEL=INFO
LOG_FILE=api.log
ERROR_LOG_FILE=api.error.log
EOF

echo "✓ 清理后的配置: .env.safe, web/.env.safe"
echo ""

echo "=========================================="
echo "  下一步操作"
echo "=========================================="
echo ""
echo "1. 修改 launch_agents.sh 在启动时加载 Secrets:"
echo "   在文件开头添加: source \"\$FACTORY/scripts/load_secrets.sh\""
echo ""
echo "2. 修改 web/backend/start.sh 在启动前加载 Secrets:"
echo "   在启动 uvicorn 前添加: source ../../scripts/load_secrets.sh"
echo ""
echo "3. (可选) 删除原始 .env 中的敏感信息:"
echo "   mv .env.safe .env"
echo "   mv web/.env.safe web/.env"
echo ""
echo "4. 测试 Secret 加载:"
echo "   source scripts/load_secrets.sh"
echo "   echo \$MINERU_TOKEN  # 应显示 token 值"
echo ""

# 生成集成补丁
cat > /tmp/secret_manager_integration.patch <<'EOF'
--- launch_agents.sh.orig
+++ launch_agents.sh
@@ -5,6 +5,9 @@

 FACTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

+# Load secrets from GCP Secret Manager
+source "$FACTORY/scripts/load_secrets.sh"
+
 # Runtime directories
 ONGOING="$FACTORY/ongoing"
 COMPLETE="$FACTORY/complete"
EOF

echo "如需自动集成，参考补丁: /tmp/secret_manager_integration.patch"
echo ""
