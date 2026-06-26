# GCP Cloud Run 加速计算配置指南

## 概述

本指南说明如何在 GCP 平台上配置 Cloud Run 服务，将 Paper Factory 的计算密集型任务（Step 5 求解、Step 6 敏感性分析）从本地转移到云端并行执行，实现显著加速。

**预期效果：**
- Step 5 求解时间：从 90+ 分钟降至 15-20 分钟（5-6x 加速）
- Step 6 敏感性分析：从 40 分钟降至 8-10 分钟（4-5x 加速）
- 总项目时间：从 6 小时降至 3-4 小时

---

## 架构设计

### 当前架构（本地执行）

```
Paper Factory (本地)
├── run_paper.sh (主控)
└── solver_submit.sh (本地后台作业)
    ├── Python 求解器
    ├── Julia 求解器
    ├── MATLAB 求解器
    └── Gurobi/CPLEX
```

**瓶颈：**
- 单机 CPU 限制（Step 5 需要顺序执行 5 个问题）
- 本地内存限制（大规模 MILP 问题）
- 无法并行运行多个项目

### 目标架构（Cloud Run 混合）

```
Paper Factory (本地)
├── run_paper.sh (主控)
├── solver_router.sh (新增：路由层)
│   ├── 本地路径 → solver_submit.sh
│   └── 云端路径 → gcp_solver_client.sh
└── GCP Cloud Run (云端)
    ├── Solver API Service (HTTP endpoint)
    │   ├── POST /solve/python
    │   ├── POST /solve/julia
    │   ├── POST /solve/gurobi
    │   └── GET /jobs/{job_id}/status
    └── 容器镜像
        ├── Python 3.11 + NumPy/SciPy/Gurobi
        ├── Julia 1.10 + JuMP/Ipopt
        ├── Gurobi 11.0 (商业求解器)
        └── 作业管理器（异步执行 + 结果存储）
```

---

## 第一部分：GCP 平台配置（您需要完成的工作）

### 1. 启用 GCP 服务

在 GCP Console 或使用 `gcloud` CLI：

```bash
# 1.1 登录并设置项目
gcloud auth login
gcloud config set project YOUR_PROJECT_ID

# 1.2 启用必需的 API
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    storage.googleapis.com \
    artifactregistry.googleapis.com \
    secretmanager.googleapis.com
```

**验证：** 在 GCP Console 检查 "API 和服务" → "已启用的 API"，确认上述服务已启用。

---

### 2. 创建 Artifact Registry（存储容器镜像）

```bash
# 2.1 创建 Docker 仓库
gcloud artifacts repositories create solver-images \
    --repository-format=docker \
    --location=asia-east1 \
    --description="Paper Factory solver containers"

# 2.2 配置 Docker 认证
gcloud auth configure-docker asia-east1-docker.pkg.dev
```

**验证：**
```bash
gcloud artifacts repositories list --location=asia-east1
```

**预期输出：**
```
REPOSITORY      FORMAT  LOCATION     
solver-images   DOCKER  asia-east1
```

---

### 3. 创建 Cloud Storage 存储桶（存储作业输入/输出）

```bash
# 3.1 创建存储桶
gcloud storage buckets create gs://YOUR_PROJECT_ID-solver-jobs \
    --location=europe-west4 \
    --uniform-bucket-level-access

# 3.2 设置生命周期规则（自动清理 7 天前的文件）
cat > lifecycle.json <<EOF
{
  "lifecycle": {
    "rule": [
      {
        "action": {"type": "Delete"},
        "condition": {"age": 7}
      }
    ]
  }
}
EOF

gcloud storage buckets update gs://YOUR_PROJECT_ID-solver-jobs \
    --lifecycle-file=lifecycle.json
```

**验证：**
```bash
gcloud storage buckets list
# 应看到 gs://YOUR_PROJECT_ID-solver-jobs/
```

---

### 4. 配置 Gurobi 许可证（如果使用 Gurobi）

Gurobi 是商业求解器，需要许可证。有三种方式：

#### 方式 A：Web License Service (WLS)（推荐）

1. 在 [Gurobi 官网](https://www.gurobi.com/downloads/) 注册账号
2. 获取学术许可证（免费）或商业许可证
3. 生成 WLS 许可证并获取 `license-id` 和 `api-key`

```bash
# 4.1 创建 Secret（存储 Gurobi 许可证）
echo -n "YOUR_GUROBI_WLS_LICENSE_ID" | gcloud secrets create gurobi-wls-license \
    --data-file=- \
    --replication-policy="automatic"

echo -n "YOUR_GUROBI_WLS_API_KEY" | gcloud secrets create gurobi-wls-api-key \
    --data-file=- \
    --replication-policy="automatic"
```

#### 方式 B：浮动许可证服务器

如果您的机构有 Gurobi 浮动许可证服务器：

```bash
# 存储许可证服务器地址
echo -n "server1.example.com:port1,server2.example.com:port2" | \
    gcloud secrets create gurobi-license-server \
    --data-file=- \
    --replication-policy="automatic"
```

#### 方式 C：仅使用开源求解器（跳过 Gurobi）

如果不使用 Gurobi，可以依赖开源求解器：
- SCIP（混合整数规划）
- Ipopt（非线性优化）
- HiGHS（线性规划）

**验证：**
```bash
gcloud secrets list
# 应看到 gurobi-wls-license 和 gurobi-wls-api-key
```

---

### 5. 创建服务账号（Cloud Run 权限）

```bash
# 5.1 创建服务账号
gcloud iam service-accounts create solver-runner \
    --display-name="Paper Factory Solver Runner"

# 5.2 授予必要权限
PROJECT_ID=$(gcloud config get-value project)
SA_EMAIL="solver-runner@${PROJECT_ID}.iam.gserviceaccount.com"

# 读写 Cloud Storage
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/storage.objectAdmin"

# 读取 Secrets
gcloud secrets add-iam-policy-binding gurobi-wls-license \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor"

gcloud secrets add-iam-policy-binding gurobi-wls-api-key \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor"
```

**验证：**
```bash
gcloud iam service-accounts list | grep solver-runner
```

---

### 6. 配置防火墙规则（如果需要回调到本地）

如果您希望 Cloud Run 完成后推送通知到本地服务器（可选）：

```bash
# 6.1 获取 Cloud Run 出站 IP 范围（全球）
# 参考：https://cloud.google.com/run/docs/securing/ingress

# 6.2 在本地防火墙允许来自 Cloud Run 的 HTTPS 请求
# （具体命令取决于您的防火墙类型）
```

**注意：** 更推荐使用轮询模式（本地定期查询 Cloud Run 状态），无需开放防火墙。

---

### 7. 设置 Cloud Build 触发器（可选：自动构建）

如果希望每次代码更新自动重新构建容器：

```bash
# 7.1 连接 GitHub/GitLab 仓库
gcloud beta builds triggers create github \
    --repo-name=paper-factory \
    --repo-owner=YOUR_GITHUB_USERNAME \
    --branch-pattern="^main$" \
    --build-config=cloudbuild.yaml
```

**验证：**
```bash
gcloud builds triggers list
```

---

## 第二部分：本地代码集成（我们将完成的工作）

这部分由我来实现，您只需要提供上述 GCP 资源：

### 1. 容器镜像构建

我们将创建：
- `Dockerfile` - 包含 Python/Julia/Gurobi 的求解环境
- `cloudbuild.yaml` - Cloud Build 配置
- `solver_api.py` - FastAPI HTTP 服务

### 2. 本地客户端

我们将创建：
- `gcp_solver_client.sh` - 调用 Cloud Run API
- `solver_router.sh` - 本地/云端路由决策
- 修改 `run_paper.sh` 以支持云端求解器

### 3. 配置文件

我们将创建：
- `.env.gcp` - GCP 项目配置
- `gcp_config.json` - 求解器路由规则

---

## 第三部分：成本估算

### Cloud Run 定价（asia-east1 区域）

| 资源 | 单价 | Step 5 用量（5 问题） | 成本 |
|------|------|---------------------|------|
| CPU | $0.00002400/vCPU·秒 | 8 vCPU × 1200秒 | $0.23 |
| 内存 | $0.00000250/GB·秒 | 32 GB × 1200秒 | $0.10 |
| 请求数 | $0.40/百万请求 | 5 请求 | ~$0 |
| **单次运行总计** | | | **$0.33** |

### Cloud Storage 定价

| 资源 | 单价 | 用量 | 成本 |
|------|------|------|------|
| 存储（Standard） | $0.020/GB/月 | 1 GB × 7天 | $0.005 |
| 出站流量（欧洲内） | $0.00/GB | 500 MB | $0.00 |
| **单次运行总计** | | | **$0.07** |

### Gurobi 许可证

- 学术许可证：**免费**
- 商业单机许可证：$3,000-$12,000/年（已有则无额外成本）
- WLS 按需计费：$0.10-$0.50/CPU·小时（具体取决于合同）

### 总计

- **单次项目运行（有 Gurobi 学术许可）**: ~$0.34
- **月度使用（30 个项目）**: ~$10
- **年度使用（365 个项目）**: ~$124

**对比本地成本：**
- 本地服务器电费（300W × 6小时 × $0.12/kWh × 365天）: ~$80/年
- 本地服务器折旧（$2000 硬件 ÷ 5年）: $400/年
- **云端总成本更低，且无需维护**

---

## 第四部分：部署清单

### ✅ 您需要完成的任务（预计 30-45 分钟）

1. [ ] **启用 GCP 服务**（5 分钟）
   - 运行第 1 节的 `gcloud services enable` 命令
   - 验证 API 已启用

2. [ ] **创建 Artifact Registry**（5 分钟）
   - 运行第 2 节命令创建 Docker 仓库
   - 配置 Docker 认证

3. [ ] **创建 Cloud Storage 存储桶**（5 分钟）
   - 创建 `gs://YOUR_PROJECT_ID-solver-jobs`
   - 设置 7 天生命周期规则

4. [ ] **配置 Gurobi 许可证**（10-15 分钟）
   - 方式 A：注册 Gurobi 账号 → 获取 WLS 许可证 → 存入 Secret
   - 方式 B：记录您的浮动许可证服务器地址 → 存入 Secret
   - 方式 C：决定仅使用开源求解器（跳过此步骤）

5. [ ] **创建服务账号**（5 分钟）
   - 运行第 5 节命令创建 `solver-runner` 服务账号
   - 授予 Storage 和 Secret Manager 权限

6. [ ] **（可选）配置防火墙**（5 分钟）
   - 如果需要 Cloud Run 回调本地，开放 HTTPS 端口
   - 否则跳过，使用轮询模式

7. [ ] **提供配置信息**
   - 将以下信息提供给我：
     - GCP 项目 ID
     - 是否有 Gurobi 许可证（学术/商业/无）
     - 首选区域（推荐：asia-east1 香港 或 asia-northeast1 东京）
     - 本地是否可以开放端口接收回调（是/否）

### 🔧 我将完成的任务（预计 2-3 小时）

1. [ ] **构建容器镜像**
   - 编写 Dockerfile（Python/Julia/Gurobi 环境）
   - 编写 solver_api.py（FastAPI HTTP 服务）
   - 编写 cloudbuild.yaml（自动构建配置）
   - 构建并推送到 Artifact Registry

2. [ ] **部署 Cloud Run 服务**
   - 部署 solver-api 服务
   - 配置自动扩缩容（0-10 实例）
   - 配置超时和内存限制
   - 测试端点连通性

3. [ ] **本地客户端集成**
   - 实现 gcp_solver_client.sh（调用 Cloud Run）
   - 实现 solver_router.sh（路由决策）
   - 修改 run_paper.sh（云端求解器支持）
   - 编写配置文件和文档

4. [ ] **端到端测试**
   - 使用 mini_proj 测试云端求解
   - 验证结果一致性
   - 性能基准测试
   - 编写故障恢复测试

---

## 第五部分：快速开始命令（复制粘贴版）

**替换以下变量后执行：**
- `YOUR_PROJECT_ID` → 您的 GCP 项目 ID
- `YOUR_GUROBI_WLS_LICENSE_ID` → Gurobi WLS 许可证 ID（如果有）
- `YOUR_GUROBI_WLS_API_KEY` → Gurobi WLS API Key（如果有）

```bash
#!/bin/bash
# Paper Factory GCP 快速配置脚本

# ===== 配置变量（请修改） =====
PROJECT_ID="YOUR_PROJECT_ID"
REGION="europe-west4"
BUCKET="${PROJECT_ID}-solver-jobs"
GUROBI_LICENSE_ID="YOUR_GUROBI_WLS_LICENSE_ID"  # 留空如果不用 Gurobi
GUROBI_API_KEY="YOUR_GUROBI_WLS_API_KEY"        # 留空如果不用 Gurobi

# ===== 自动执行步骤 =====
set -euo pipefail

echo "🚀 开始配置 GCP Cloud Run 加速环境..."

# 1. 设置项目
echo "📋 设置项目: $PROJECT_ID"
gcloud config set project "$PROJECT_ID"

# 2. 启用 API
echo "🔌 启用必需的 GCP 服务..."
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    storage.googleapis.com \
    artifactregistry.googleapis.com \
    secretmanager.googleapis.com

# 3. 创建 Artifact Registry
echo "📦 创建容器镜像仓库..."
gcloud artifacts repositories create solver-images \
    --repository-format=docker \
    --location="$REGION" \
    --description="Paper Factory solver containers" || true

gcloud auth configure-docker "${REGION}-docker.pkg.dev"

# 4. 创建 Cloud Storage 存储桶
echo "🗄️  创建存储桶: gs://$BUCKET"
gcloud storage buckets create "gs://$BUCKET" \
    --location="$REGION" \
    --uniform-bucket-level-access || true

cat > /tmp/lifecycle.json <<EOF
{
  "lifecycle": {
    "rule": [
      {
        "action": {"type": "Delete"},
        "condition": {"age": 7}
      }
    ]
  }
}
EOF
gcloud storage buckets update "gs://$BUCKET" --lifecycle-file=/tmp/lifecycle.json

# 5. 配置 Gurobi 许可证（如果提供）
if [[ -n "$GUROBI_LICENSE_ID" && -n "$GUROBI_API_KEY" ]]; then
    echo "🔑 配置 Gurobi 许可证..."
    echo -n "$GUROBI_LICENSE_ID" | gcloud secrets create gurobi-wls-license \
        --data-file=- --replication-policy="automatic" || \
    echo -n "$GUROBI_LICENSE_ID" | gcloud secrets versions add gurobi-wls-license --data-file=-
    
    echo -n "$GUROBI_API_KEY" | gcloud secrets create gurobi-wls-api-key \
        --data-file=- --replication-policy="automatic" || \
    echo -n "$GUROBI_API_KEY" | gcloud secrets versions add gurobi-wls-api-key --data-file=-
else
    echo "⚠️  跳过 Gurobi 配置（将使用开源求解器）"
fi

# 6. 创建服务账号
echo "👤 创建服务账号..."
SA_EMAIL="solver-runner@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud iam service-accounts create solver-runner \
    --display-name="Paper Factory Solver Runner" || true

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/storage.objectAdmin"

if [[ -n "$GUROBI_LICENSE_ID" ]]; then
    gcloud secrets add-iam-policy-binding gurobi-wls-license \
        --member="serviceAccount:${SA_EMAIL}" \
        --role="roles/secretmanager.secretAccessor"
    
    gcloud secrets add-iam-policy-binding gurobi-wls-api-key \
        --member="serviceAccount:${SA_EMAIL}" \
        --role="roles/secretmanager.secretAccessor"
fi

# 7. 验证配置
echo ""
echo "✅ 配置完成！正在验证..."
echo ""
echo "📋 已启用的服务:"
gcloud services list --enabled | grep -E "run|build|storage|artifact|secret"

echo ""
echo "📦 Artifact Registry:"
gcloud artifacts repositories list --location="$REGION"

echo ""
echo "🗄️  Cloud Storage:"
gcloud storage buckets list | grep "$BUCKET"

echo ""
echo "🔑 Secrets:"
gcloud secrets list

echo ""
echo "👤 服务账号:"
gcloud iam service-accounts list | grep solver-runner

echo ""
echo "✅ 所有配置已完成！"
echo ""
echo "📝 请将以下信息提供给开发人员："
echo "   - 项目 ID: $PROJECT_ID"
echo "   - 区域: $REGION"
echo "   - 存储桶: gs://$BUCKET"
echo "   - Gurobi 许可证: $([ -n "$GUROBI_LICENSE_ID" ] && echo '已配置' || echo '未配置')"
echo ""
echo "🚀 下一步：开发人员将部署 Cloud Run 服务"
```

---

## 第六部分：故障排查

### 常见问题

**Q1: `gcloud` 命令未找到**
```bash
# 安装 Google Cloud SDK
curl https://sdk.cloud.google.com | bash
exec -l $SHELL
gcloud init
```

**Q2: 没有权限创建资源**
```bash
# 确认您的账号是项目所有者或编辑者
gcloud projects get-iam-policy YOUR_PROJECT_ID --flatten="bindings[].members" --filter="bindings.members:user:YOUR_EMAIL"
```

**Q3: Gurobi 许可证验证失败**
- 检查许可证是否过期：登录 [Gurobi Portal](https://portal.gurobi.com/)
- 检查 WLS 配额：确认剩余可用时间
- 使用开源求解器替代（SCIP/Ipopt）

**Q4: Cloud Run 冷启动慢**
- 配置最小实例数 ≥ 1（成本会增加）
- 使用 CPU always-on 配置
- 或接受 3-5 秒的冷启动延迟

---

## 联系与支持

完成配置后，请提供：
1. GCP 项目 ID
2. 选择的区域
3. 是否配置 Gurobi（学术/商业/无）
4. 任何错误消息或验证失败的步骤

我将根据您的配置完成代码集成和部署。

---

**文档版本**: 1.0  
**最后更新**: 2026-06-22  
**预计总配置时间**: 30-45 分钟（您的部分）+ 2-3 小时（代码集成）
