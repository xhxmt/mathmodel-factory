# Cloud Solver Deployment Summary

## 已完成的工作

### 1. 容器构建文件 ✅
- **`cloud/Dockerfile`** - 包含 Python 3.11 + 开源求解器（HiGHS, SCIP, CyLP）
- **`cloud/requirements.txt`** - Python 依赖（FastAPI, scipy, numpy, pandas, etc.）
- **`cloud/solver_api.py`** - FastAPI HTTP 服务，提供 `/solve/{type}` 端点
- **`cloud/solver_runner.py`** - 求解器执行封装层
- **`cloud/cloudbuild.yaml`** - Cloud Build 自动构建和部署配置

### 2. 本地客户端脚本 ✅
- **`scripts/gcp_solver_client.sh`** - 调用 Cloud Run API 的客户端
- **`scripts/solver_router.sh`** - 本地/云端路由决策层
- **`.env.gcp`** - GCP 配置文件（已填入你的项目信息）
- **`.env.gcp.example`** - 配置模板

### 3. 部署脚本 ✅
- **`deploy_cloud_solver.sh`** - 一键部署脚本

## 下一步操作

### 步骤 4：完成服务账号配置（5 分钟）

你已经创建了 Artifact Registry 和 Storage，现在需要创建服务账号：

```bash
# 创建服务账号
gcloud iam service-accounts create solver-runner \
    --display-name="Paper Factory Solver Runner"

# 授予 Storage 权限
PROJECT_ID="level-night-476302-k0"
SA_EMAIL="solver-runner@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/storage.objectAdmin"
```

### 步骤 5：部署到 Cloud Run（10-15 分钟）

运行部署脚本：

```bash
cd /home/tfisher/paper_factory
./deploy_cloud_solver.sh
```

这个脚本会：
1. 检查先决条件（gcloud, docker）
2. 验证服务账号（如果不存在会自动创建）
3. 使用 Cloud Build 构建容器镜像（约 5-10 分钟）
4. 部署到 Cloud Run
5. 测试健康检查端点

### 步骤 6：测试云端求解器（5 分钟）

部署完成后，获取服务 URL：

```bash
gcloud run services describe solver-api \
    --region=europe-west4 \
    --format="value(status.url)"
```

测试健康检查：

```bash
SERVICE_URL="<上面命令输出的 URL>"
curl ${SERVICE_URL}/health
```

预期输出：
```json
{
  "status": "healthy",
  "timestamp": 1234567890.123,
  "active_jobs": 0,
  "total_jobs": 0,
  "system": {
    "cpu_percent": 5.2,
    "memory_percent": 12.3,
    "disk_percent": 8.1
  }
}
```

### 步骤 7：启用云端求解（可选）

编辑 `.env.gcp` 并启用云端路由：

```bash
# 修改这一行
USE_CLOUD_SOLVER=true
```

然后在启动项目时加载配置：

```bash
source .env.gcp
./launch_agents.sh new test_cloud /path/to/problem.pdf
```

## 架构说明

### 当前工作流
```
run_paper.sh 
  → solver_submit.sh (本地执行)
    → Python/Julia/MATLAB 脚本
```

### 云端增强工作流
```
run_paper.sh 
  → solver_router.sh (路由决策)
    ├─ 本地路径 → solver_submit.sh
    └─ 云端路径 → gcp_solver_client.sh
                   → Cloud Run API
                     → 容器化求解器
                       → 结果上传到 GCS
```

### 路由决策逻辑

`solver_router.sh` 根据以下条件决定是否使用云端：

1. **`USE_CLOUD_SOLVER=true`** 全局开关
2. **`max_time >= 300`** 任务时长 ≥ 5 分钟
3. **求解器类型在白名单中**（python, julia, matlab, R）

### 成本估算

按照 europe-west4 区域定价：

| 场景 | CPU·秒 | 内存·秒 | 估算成本 |
|------|--------|---------|---------|
| 单次 Step 5 求解（5 问题，平均 15 分钟） | 4 vCPU × 900s | 8 GB × 900s | $0.09 |
| 单个完整项目（Step 5+6，平均 25 分钟） | 4 vCPU × 1500s | 8 GB × 1500s | $0.15 |
| 月度 30 个项目 | | | $4.50 |

**对比本地成本：**
- 本地服务器电费（300W × 6h × 30 项目 × €0.12/kWh）≈ €6.50/月
- 云端更便宜且无需维护硬件

## 故障排查

### 问题：部署时找不到 solver-images 仓库

确认仓库位置：
```bash
gcloud artifacts repositories list --format="table(name,location)"
```

如果仓库在不同区域，修改 `.env.gcp` 中的 `GCP_REGION`。

### 问题：Cloud Build 权限不足

确保启用了必要的 API：
```bash
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com
```

### 问题：容器构建超时

Cloud Build 超时设置为 30 分钟。如果网络慢，可以增加：

编辑 `cloud/cloudbuild.yaml`：
```yaml
timeout: '3600s'  # 增加到 1 小时
```

### 问题：Cloud Run 冷启动慢

首次请求需要 3-5 秒启动容器。解决方案：

```bash
# 设置最小实例数（会增加成本）
gcloud run services update solver-api \
    --region=europe-west4 \
    --min-instances=1
```

成本影响：1 个实例 24/7 运行 ≈ €25/月

## 后续优化

### 1. 集成到 run_paper.sh（当前手动）

修改 `run_paper.sh` 中的 solver 调用：

```bash
# 现在
../../solver_submit.sh --type python --max-time 600 script.py

# 改为
../../solver_router.sh --type python --max-time 600 script.py
```

### 2. 并行化多问题求解

当前 Step 5 顺序执行 P1-P5。使用云端后可以并行：

```bash
# 同时提交 5 个任务到 Cloud Run
for i in {1..5}; do
    gcp_solver_client.sh --job-id "p${i}" --type python script_p${i}.py &
done
wait
```

预期加速：5x（如果任务独立）

### 3. 添加 Julia 支持

当前 Dockerfile 只安装了 Python。要支持 Julia：

编辑 `cloud/Dockerfile`，在 Python 依赖后添加：

```dockerfile
# Install Julia
RUN curl -fsSL https://install.julialang.org | sh -s -- -y
ENV PATH="/root/.juliaup/bin:${PATH}"
RUN julia -e 'using Pkg; Pkg.add(["JuMP", "Ipopt", "GLPK"])'
```

重新部署即可。

## 参考文档

- Cloud Run 文档：https://cloud.google.com/run/docs
- Cloud Build 文档：https://cloud.google.com/build/docs
- Artifact Registry：https://cloud.google.com/artifact-registry/docs
- 定价计算器：https://cloud.google.com/products/calculator

---

**创建时间**: 2026-06-22  
**项目 ID**: level-night-476302-k0  
**区域**: europe-west4  
**状态**: 代码就绪，等待部署
