# Secret Manager 配置指南

## 概述

Secret Manager 是 GCP 的密钥管理服务，用于安全存储 API 密钥、密码、证书等敏感信息。

**优势：**
- ✅ 集中管理，无需在代码/配置文件中硬编码
- ✅ 版本控制和轮换（保留历史版本）
- ✅ 访问审计（谁在何时访问了哪个密钥）
- ✅ 细粒度权限控制（IAM）
- ✅ 加密存储（自动加密）

**成本：** 
- 前 6 个 Secret 免费
- 之后 $0.06/Secret/月
- 访问操作：前 10,000 次免费，之后 $0.03/10,000 次

---

## 快速开始

### 一键配置（推荐）

```bash
cd /home/tfisher/paper_factory

# 运行配置向导
./scripts/setup_secret_manager.sh
```

这个脚本会自动：
1. 启用 Secret Manager API
2. 从 `.env` 和 `web/.env` 读取敏感信息
3. 创建/更新所有 Secrets
4. 备份原始配置文件
5. 生成清理后的配置模板
6. 测试 Secret 访问

---

## 手动配置步骤

### 1. 启用 API

```bash
gcloud services enable secretmanager.googleapis.com
```

### 2. 创建 Secrets

```bash
cd /home/tfisher/paper_factory

# 从 .env 文件提取并创建
echo -n "$(grep MINERU_TOKEN .env | cut -d= -f2)" | \
  gcloud secrets create mineru-token --data-file=-

echo -n "$(grep GEMINI_API_KEY .env | cut -d= -f2)" | \
  gcloud secrets create gemini-api-key --data-file=-

echo -n "$(grep DEEPSEEK_API_KEY .env | cut -d= -f2)" | \
  gcloud secrets create deepseek-api-key --data-file=-

echo -n "$(grep JWT_SECRET web/.env | cut -d= -f2)" | \
  gcloud secrets create dashboard-jwt-secret --data-file=-

echo -n "$(grep ADMIN_PASSWORD web/.env | cut -d= -f2)" | \
  gcloud secrets create dashboard-admin-password --data-file=-
```

### 3. 验证创建

```bash
# 列出所有 Secrets
gcloud secrets list

# 查看特定 Secret 的元数据（不显示值）
gcloud secrets describe mineru-token

# 访问 Secret 值（测试）
gcloud secrets versions access latest --secret=mineru-token
```

---

## 在应用中使用

### 方式 1: Shell 脚本加载（已实现）

```bash
# 在 launch_agents.sh 开头添加
source "$FACTORY/scripts/load_secrets.sh"

# 在 web/backend/start.sh 开头添加
source ../../scripts/load_secrets.sh
```

`load_secrets.sh` 会将 Secrets 导出为环境变量：
```bash
export MINERU_TOKEN=$(gcloud secrets versions access latest --secret=mineru-token)
export GEMINI_API_KEY=$(gcloud secrets versions access latest --secret=gemini-api-key)
export DEEPSEEK_API_KEY=$(gcloud secrets versions access latest --secret=deepseek-api-key)
export JWT_SECRET=$(gcloud secrets versions access latest --secret=dashboard-jwt-secret)
export ADMIN_PASSWORD=$(gcloud secrets versions access latest --secret=dashboard-admin-password)
```

### 方式 2: Python 直接访问

```python
# 在 web/backend/app.py 中
from google.cloud import secretmanager

def get_secret(secret_id: str, project_id: str = "level-night-476302-k0") -> str:
    """从 Secret Manager 获取密钥"""
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")

# 使用示例
JWT_SECRET = os.getenv("JWT_SECRET") or get_secret("dashboard-jwt-secret")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD") or get_secret("dashboard-admin-password")
```

需要安装依赖：
```bash
pip install google-cloud-secret-manager
```

### 方式 3: Cloud Run 环境变量挂载

如果 Web Dashboard 部署到 Cloud Run：

```bash
gcloud run deploy web-dashboard \
  --image=gcr.io/level-night-476302-k0/web-dashboard \
  --set-secrets=JWT_SECRET=dashboard-jwt-secret:latest,ADMIN_PASSWORD=dashboard-admin-password:latest
```

Secret 会自动注入为环境变量，无需代码修改。

---

## 完整集成示例

### 修改 `launch_agents.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

FACTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 🔒 加载 GCP Secrets
if [[ -f "$FACTORY/scripts/load_secrets.sh" ]]; then
    source "$FACTORY/scripts/load_secrets.sh"
fi

# 原有代码继续...
```

### 修改 `web/backend/start.sh`

```bash
#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 🔒 加载 GCP Secrets
if [[ -f "$SCRIPT_DIR/../../scripts/load_secrets.sh" ]]; then
    source "$SCRIPT_DIR/../../scripts/load_secrets.sh"
fi

# 激活虚拟环境
source "$SCRIPT_DIR/venv/bin/activate"

# 启动 FastAPI
exec uvicorn app:app --host 0.0.0.0 --port 8000
```

### 清理 `.env` 文件

迁移后，从 `.env` 删除明文密钥：

```bash
# .env (清理后)
# Secrets moved to GCP Secret Manager
# Load with: source scripts/load_secrets.sh

# ==========================================
# Cloud Solver 配置
# ==========================================
USE_CLOUD_SOLVER=true
CLOUD_THRESHOLD_TIME=300

# ==========================================
# GCP 配置
# ==========================================
GCP_PROJECT_ID=level-night-476302-k0
GCP_REGION=europe-west4
```

---

## Secret 管理操作

### 更新 Secret 值

```bash
# 添加新版本（保留旧版本）
echo -n "new-api-key-value" | \
  gcloud secrets versions add gemini-api-key --data-file=-

# 列出所有版本
gcloud secrets versions list gemini-api-key

# 访问特定版本
gcloud secrets versions access 2 --secret=gemini-api-key
```

### 轮换密钥

```bash
# 1. 生成新密钥
NEW_JWT_SECRET=$(openssl rand -hex 32)

# 2. 添加到 Secret Manager
echo -n "$NEW_JWT_SECRET" | \
  gcloud secrets versions add dashboard-jwt-secret --data-file=-

# 3. 重启应用（自动加载最新版本）
# 旧版本保留，支持回滚
```

### 删除 Secret

```bash
# 删除整个 Secret（谨慎操作）
gcloud secrets delete mineru-token

# 禁用特定版本（不删除）
gcloud secrets versions disable 1 --secret=gemini-api-key
```

### 访问控制

```bash
# 授予特定用户访问权限
gcloud secrets add-iam-policy-binding dashboard-jwt-secret \
  --member="user:developer@example.com" \
  --role="roles/secretmanager.secretAccessor"

# 授予 Cloud Run 服务账号访问权限
gcloud secrets add-iam-policy-binding dashboard-jwt-secret \
  --member="serviceAccount:144584367563-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

---

## 审计和监控

### 查看访问日志

```bash
# 查看最近 50 次 Secret 访问
gcloud logging read 'resource.type="secretmanager.googleapis.com/Secret"' \
  --limit=50 \
  --format=json

# 按 Secret 过滤
gcloud logging read 'resource.type="secretmanager.googleapis.com/Secret" AND protoPayload.resourceName:"mineru-token"' \
  --limit=10
```

### 设置告警

```bash
# 创建告警：当有人访问敏感 Secret 时通知
gcloud alpha monitoring policies create \
  --notification-channels=<YOUR_CHANNEL_ID> \
  --display-name="Secret Access Alert" \
  --condition-display-name="Sensitive secret accessed" \
  --condition-filter='resource.type="secretmanager.googleapis.com/Secret" AND protoPayload.methodName="AccessSecretVersion"'
```

---

## 常见问题

### Q: 如何测试 Secret 是否正确加载？

```bash
source scripts/load_secrets.sh
echo "MINERU_TOKEN: ${MINERU_TOKEN:0:20}..."  # 只显示前 20 字符
echo "JWT_SECRET: ${JWT_SECRET:0:20}..."
```

### Q: Cloud Run 部署时如何使用 Secrets？

两种方式：
1. **环境变量挂载**（推荐）：
   ```bash
   gcloud run deploy --set-secrets=JWT_SECRET=dashboard-jwt-secret:latest
   ```

2. **代码直接访问**：
   ```python
   from google.cloud import secretmanager
   client = secretmanager.SecretManagerServiceClient()
   ```

### Q: 本地开发时如何避免频繁调用 Secret Manager API？

```bash
# 方案 1: 缓存到本地 .env（不提交到 Git）
source scripts/load_secrets.sh
env | grep -E "MINERU|GEMINI|DEEPSEEK|JWT|ADMIN" > .env.local

# 方案 2: 使用 direnv（自动加载）
echo "source scripts/load_secrets.sh" > .envrc
direnv allow
```

### Q: 如果 Secret 泄露怎么办？

```bash
# 1. 立即轮换
echo -n "new-secure-key" | gcloud secrets versions add <secret-name> --data-file=-

# 2. 禁用旧版本
gcloud secrets versions disable <VERSION> --secret=<secret-name>

# 3. 检查访问日志
gcloud logging read 'protoPayload.resourceName:"<secret-name>"' --limit=100

# 4. 撤销可疑用户权限
gcloud secrets remove-iam-policy-binding <secret-name> \
  --member="user:suspicious@example.com" \
  --role="roles/secretmanager.secretAccessor"
```

---

## 成本优化

### 避免频繁访问

```python
# ❌ 错误：每次请求都读取
@app.get("/api/data")
def get_data():
    token = get_secret("mineru-token")  # 每次请求 $0.000003
    ...

# ✅ 正确：启动时缓存
MINERU_TOKEN = os.getenv("MINERU_TOKEN") or get_secret("mineru-token")

@app.get("/api/data")
def get_data():
    # 使用缓存的 MINERU_TOKEN
    ...
```

### 使用环境变量注入

Cloud Run 的 `--set-secrets` 不收取 API 调用费用，只收取存储费用。

---

## 验证清单

配置完成后，验证以下内容：

- [ ] `gcloud secrets list` 显示 5 个 Secrets
- [ ] `source scripts/load_secrets.sh` 无错误输出
- [ ] `echo $MINERU_TOKEN` 显示正确的 token
- [ ] `.env.backup` 和 `web/.env.backup` 已创建
- [ ] `launch_agents.sh` 开头添加了 `source` 命令
- [ ] `web/backend/start.sh` 开头添加了 `source` 命令
- [ ] 重启 Web Dashboard 后仍可正常登录
- [ ] 创建新项目时 MinerU 解析正常工作

---

## 下一步

配置完成后，可以：
1. 从 `.env` 文件删除明文密钥（保留 `.env.backup`）
2. 更新 `.gitignore` 确保 `.env.backup` 不被提交
3. 设置 Secret 访问告警（监控异常访问）
4. 配置定期密钥轮换（如 JWT_SECRET 每 90 天）

---

**需要帮助？**
- 官方文档：https://cloud.google.com/secret-manager/docs
- 定价计算器：https://cloud.google.com/products/calculator
- 支持：gcloud support cases create
