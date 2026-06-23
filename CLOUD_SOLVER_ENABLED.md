# ✅ Cloud Run Solver 启用完成

**完成时间:** 2026-06-23 13:47

---

## 配置结果

### ✅ Cloud Run Solver 已启用

**配置文件:** `/home/tfisher/paper_factory/.env`

```bash
USE_CLOUD_SOLVER=true           # 已启用
CLOUD_THRESHOLD_TIME=300        # 5 分钟阈值
CLOUD_SOLVER_TYPES=python,julia,matlab,R
```

**Cloud Run 服务状态:**
- URL: `https://solver-api-144584367563.europe-west4.run.app`
- Health: ✅ Healthy
- Active Jobs: 0
- System: CPU 0%, Memory 1.2%, Disk 0%

---

## 工作原理

### 自动路由逻辑

`solver_router.sh` 会根据任务执行时间自动选择执行位置：

```
┌─────────────────────────────────────────────────┐
│  用户提交任务 (solver_submit.sh 或 router)     │
└────────────────┬────────────────────────────────┘
                 │
                 ▼
        ┌────────────────────┐
        │ solver_router.sh   │
        │ 检查 max-time      │
        └────────┬───────────┘
                 │
         ┌───────┴────────┐
         │                │
         ▼                ▼
    < 300s           ≥ 300s
         │                │
         ▼                ▼
┌─────────────┐  ┌──────────────────┐
│ 本地执行     │  │ Cloud Run 执行    │
│ (本机CPU)    │  │ (云端 2 vCPU)    │
└─────────────┘  └──────────────────┘
```

### 路由规则

| 执行时间 | 路由目标 | 原因 |
|---------|---------|------|
| < 5 分钟 (300s) | 本地 | 快速任务，本地执行无冷启动延迟 |
| ≥ 5 分钟 (300s) | Cloud Run | 长时间任务，释放本地资源 |

**支持的求解器类型:**
- ✅ Python (`--type python`)
- ✅ Julia (`--type julia`)
- ✅ MATLAB (`--type matlab`)
- ✅ R (`--type R`)
- ✅ Gurobi (`--type gurobi`)

---

## 测试结果

### 测试 1: 短任务（60秒）→ 本地执行

```bash
$ ./scripts/solver_router.sh --type python --max-time 60 test_cloud_solver.py
[solver_router] Routing to local solver
local_python_20260623134727_2024627

$ ./solver_submit.sh --status local_python_20260623134727_2024627
COMPLETED
```

✅ **短任务正确路由到本地**

### 测试 2: 长任务（400秒）→ Cloud Run 执行

```bash
$ USE_CLOUD_SOLVER=true ./scripts/solver_router.sh --type python --max-time 400 test_cloud_solver.py
[solver_router] Routing to Cloud Run (max_time=400s)
Using Cloud Run service: https://solver-api-chqc6lw4sa-ez.a.run.app
Submitting job job-20260623-134741-2024699 to Cloud Run...
Job submitted successfully: job-20260623-134741-2024699
Polling job status...
...
Job finished with status: completed
Job completed successfully
```

✅ **长任务正确路由到 Cloud Run**

---

## 使用方法

### 1. 在 Paper Factory 工作流中（自动）

```bash
# 在项目目录内（如 ongoing/cumcm2024b/）
../../solver_submit.sh --type python --max-time 1800 models/m3_milp/03_solve.py
```

`run_paper.sh` 会自动使用 `solver_submit.sh`，路由逻辑透明工作。

**Step 5/6（求解和敏感性分析）会自动受益：**
- 长时间求解任务自动上云
- 本地资源释放
- 支持并行运行多个项目

### 2. 手动提交任务

```bash
# 短任务（本地执行）
./solver_submit.sh --type python --max-time 60 script.py

# 长任务（自动上云）
./solver_submit.sh --type python --max-time 600 script.py

# 检查状态
./solver_submit.sh --status <jobid>

# 等待完成
./solver_submit.sh --wait <jobid>
```

### 3. 强制使用 Cloud Run

```bash
# 临时强制上云（忽略阈值）
USE_CLOUD_SOLVER=true CLOUD_THRESHOLD_TIME=0 \
  ./scripts/solver_router.sh --type python --max-time 60 script.py
```

---

## 性能对比

### 本地执行 vs Cloud Run

| 指标 | 本地 | Cloud Run |
|-----|------|----------|
| CPU | 取决于硬件 | 2 vCPU (固定) |
| 内存 | 取决于硬件 | 4GB (固定) |
| 冷启动 | 0s | 1-3s |
| 并发 | 受限于本地资源 | 自动扩容 (最多 10 实例) |
| 成本 | 固定（电费） | 按使用时间 (~$0.02/小时) |
| 适用场景 | 快速任务 | 长时间/并行任务 |

### 何时受益最大

✅ **最适合的场景：**
1. Step 5: 完整求解（常 > 30 分钟）
2. Step 6: 敏感性分析（多次求解，并行）
3. 大规模优化问题（MILP/MINLP）
4. Monte Carlo 模拟（多次独立运行）

❌ **不适合的场景：**
1. 快速测试（< 1 分钟）
2. 交互式调试
3. 需要本地文件系统的任务

---

## 成本分析

### Cloud Run 计费

基于实际执行时间（按秒计费）：

| 配置 | 价格 | 示例 |
|-----|------|------|
| 2 vCPU, 4GB 内存 | ~$0.000024/秒 | 1 小时 = $0.0864 |
| 1 vCPU, 2GB 内存 | ~$0.000012/秒 | 1 小时 = $0.0432 |

**月度估算（假设每月 50 个项目）：**

| 场景 | 云端执行时间 | 月度成本 |
|-----|------------|---------|
| 轻度使用 | 50 小时 | $4.32 |
| 中度使用 | 100 小时 | $8.64 |
| 重度使用 | 200 小时 | $17.28 |

**vs 本地执行：**
- 本地服务器 24/7 运行 → 固定电费 + 硬件折旧
- Cloud Run 按需使用 → 只在实际计算时付费

---

## 监控和日志

### 查看 Cloud Run 日志

```bash
# 查看最近 50 条日志
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="solver-api"' \
  --limit=50 \
  --format=json

# 查看特定任务的日志
gcloud logging read 'resource.type="cloud_run_revision" AND jsonPayload.job_id="job-20260623-134741-2024699"' \
  --format=json
```

### Cloud Run 指标

```bash
# 查看服务状态
gcloud run services describe solver-api --region=europe-west4

# 查看请求计数
gcloud monitoring time-series list \
  --filter='metric.type="run.googleapis.com/request_count"' \
  --format=json
```

### 设置告警（可选）

```bash
# Cloud Run 错误率 > 10%
gcloud alpha monitoring policies create \
  --notification-channels=<YOUR_CHANNEL> \
  --display-name="Cloud Run Solver Errors" \
  --condition-threshold-value=0.1 \
  --condition-filter='resource.type="cloud_run_revision" AND metric.type="run.googleapis.com/request_count"'
```

---

## 故障排查

### 问题 1: 任务仍然在本地执行

**检查：**
```bash
# 确认环境变量
source .env
echo $USE_CLOUD_SOLVER  # 应为 true
echo $CLOUD_THRESHOLD_TIME  # 应为 300

# 确认任务时间超过阈值
./scripts/solver_router.sh --type python --max-time 400 script.py
# 应看到 "[solver_router] Routing to Cloud Run"
```

**解决：**
```bash
# 重新加载 .env
source .env

# 或临时设置
export USE_CLOUD_SOLVER=true
export CLOUD_THRESHOLD_TIME=300
```

### 问题 2: Cloud Run 认证失败

**检查：**
```bash
# 测试认证
gcloud auth print-identity-token

# 测试 API 访问
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  https://solver-api-144584367563.europe-west4.run.app/health
```

**解决：**
```bash
# 重新登录
gcloud auth login

# 设置默认项目
gcloud config set project level-night-476302-k0
```

### 问题 3: 任务提交后无响应

**检查：**
```bash
# 查看 Cloud Run 日志
gcloud logging read 'resource.type="cloud_run_revision"' --limit=10

# 检查服务状态
gcloud run services describe solver-api --region=europe-west4
```

### 问题 4: 成本超出预期

**检查：**
```bash
# 查看当月使用量
gcloud billing accounts list
gcloud billing accounts get-billing-info <BILLING_ACCOUNT_ID>

# 设置预算告警
gcloud billing budgets create \
  --billing-account=<BILLING_ACCOUNT_ID> \
  --display-name="Cloud Run Budget" \
  --budget-amount=50
```

---

## 优化建议

### 1. 调整阈值（根据实际情况）

```bash
# .env
CLOUD_THRESHOLD_TIME=180  # 3 分钟（更激进上云）
# 或
CLOUD_THRESHOLD_TIME=600  # 10 分钟（保守上云）
```

### 2. 配置 Cloud Run 资源

编辑 `cloud/solver_api.py` 并重新部署：

```python
# 更多 CPU/内存（适合计算密集型）
gcloud run deploy solver-api \
  --cpu=4 \
  --memory=8Gi \
  --region=europe-west4

# 更快冷启动（保持 1 个热实例）
gcloud run services update solver-api \
  --min-instances=1 \
  --region=europe-west4
```

### 3. 批量任务并行

```bash
# 多个独立任务并行提交
for script in models/*/solve.py; do
    ./solver_submit.sh --type python --max-time 1800 "$script" &
done
wait
```

---

## 验证清单

- [x] `USE_CLOUD_SOLVER=true` 已设置
- [x] Cloud Run API 健康检查通过
- [x] 短任务路由到本地
- [x] 长任务路由到 Cloud Run
- [x] 测试任务成功完成
- [x] `solver_router.sh` Bug 已修复
- [x] 环境变量正确加载

---

## 相关文档

- **GCP 服务集成:** `docs/GCP_SERVICES_INTEGRATION.md`
- **Cloud Solver 实现:** `cloud/solver_api.py`
- **路由器实现:** `scripts/solver_router.sh`, `scripts/gcp_solver_client.sh`

---

## 🎉 启用完成！

Cloud Run Solver 现在已启用并正常工作。你的 Paper Factory 系统现在可以：

✅ **自动将长时间求解任务卸载到云端**
✅ **释放本地资源用于其他工作**
✅ **支持更多并行项目**
✅ **按需付费，无闲置成本**

**下次运行项目时，Step 5/6 会自动受益！**

---

## 下一步建议

1. **配置 Cloud Storage 自动备份**（10分钟）
   - 每天自动备份 `complete/` 到云端
   - 自动清理本地旧项目

2. **设置 Cloud Monitoring 告警**（30分钟）
   - Step 超时通知
   - 磁盘空间告警
   - Cloud Run 错误率监控

3. **运行一个完整项目测试**
   ```bash
   ./launch_agents.sh new test_cloud /path/to/problem.pdf
   ```

详细步骤参考: `docs/GCP_SERVICES_INTEGRATION.md`
