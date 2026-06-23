# GCP 服务集成方案（基于现有服务器部署）

## 当前部署状态分析

### ✅ 已部署的 GCP 服务

| 服务 | 状态 | URL/资源 |
|------|------|----------|
| Cloud Run (Solver API) | ✅ 已部署 | `https://solver-api-144584367563.europe-west4.run.app` |
| Cloud Storage | ✅ 已创建 | `gs://level-night-476302-k0-solver-jobs/` |
| Cloud Logging | ✅ 已启用 | 默认日志收集 |
| Cloud Monitoring | ✅ 已启用 | 基础指标 |
| Cloud Pub/Sub | ✅ 已启用 | 未配置 |

### 🟡 本地服务器运行中

| 组件 | 状态 | 端口 |
|------|------|------|
| Web Backend (FastAPI) | ✅ 运行中 | `:8000` (uvicorn) |
| Web Frontend (Vue3) | ✅ 运行中 | `:5173` (Vite/Node) |
| Paper Factory Runner | ✅ 本地 bash | `launch_agents.sh` |
| Cloud Solver 路由 | 🔴 **未启用** | `USE_CLOUD_SOLVER=false` |

### 🔴 未使用的 GCP 服务

- Firestore (未启用)
- Cloud Tasks (未启用)
- Vertex AI (未配置)
- Secret Manager (未配置)
- Cloud Build CI/CD (有配置但未触发)

---

## 立即可启用的功能（无需重构）

### 1. **启用 Cloud Run Solver 自动路由** ⚡

**现状：** Cloud Run Solver API 已部署并健康运行，但 `solver_router.sh` 默认关闭。

**操作步骤：**

```bash
# 在项目根目录 .env 添加配置
cat >> /home/tfisher/paper_factory/.env <<'EOF'

# ==========================================
# Cloud Solver 配置
# ==========================================
USE_CLOUD_SOLVER=true
CLOUD_THRESHOLD_TIME=300        # 5分钟以上任务自动上云
CLOUD_SOLVER_TYPES=python,julia,matlab,R

# GCP 配置（已自动推断，可覆盖）
GCP_PROJECT_ID=level-night-476302-k0
GCP_REGION=europe-west4
GCP_SOLVER_SERVICE=solver-api
GCP_SOLVER_BUCKET=level-night-476302-k0-solver-jobs
EOF

# 测试云端求解器
cd /home/tfisher/paper_factory
./scripts/gcp_solver_client.sh --type python --max-time 60 --script <test_script.py>
```

**收益：**
- 长时间求解任务（Step 5/6）自动卸载到云端
- 本地资源释放，可并行处理多个项目
- 无需修改任何智能体提示词或工作流代码

**成本：** ~$0.02/小时（2 vCPU, 4GB RAM）

---

### 2. **Cloud Storage 自动备份和归档** 📦

**现状：** 本地 `complete/` 125MB，`papers/` 44MB 占用本地空间。

**操作步骤：**

```bash
# 创建归档存储桶
gsutil mb -p level-night-476302-k0 -c NEARLINE -l europe-west4 \
  gs://level-night-476302-k0-paper-archive

# 配置生命周期策略
cat > /tmp/archive_lifecycle.json <<'EOF'
{
  "lifecycle": {
    "rule": [
      {
        "action": {"type": "Delete"},
        "condition": {
          "age": 365,
          "matchesPrefix": ["logs/"]
        }
      }
    ]
  }
}
EOF
gsutil lifecycle set /tmp/archive_lifecycle.json gs://level-night-476302-k0-paper-archive

# 添加自动备份脚本
cat > /home/tfisher/paper_factory/scripts/auto_backup_gcs.sh <<'SCRIPT'
#!/bin/bash
# 自动备份完成项目到 GCS
set -euo pipefail

FACTORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUCKET="gs://level-night-476302-k0-paper-archive"

# 备份完成项目（增量，跳过已存在）
gsutil -m rsync -r -x ".*\.log$|.*\.tmp$" \
  "$FACTORY_ROOT/complete/" "$BUCKET/complete/"

# 备份最终论文
gsutil -m rsync -r \
  "$FACTORY_ROOT/papers/" "$BUCKET/papers/"

# 清理本地超过 30 天的完成项目
find "$FACTORY_ROOT/complete/" -maxdepth 1 -type d -mtime +30 -exec rm -rf {} \;

echo "Backup completed: $(date)"
SCRIPT
chmod +x /home/tfisher/paper_factory/scripts/auto_backup_gcs.sh

# 添加到 crontab（每天凌晨 2 点）
(crontab -l 2>/dev/null; echo "0 2 * * * /home/tfisher/paper_factory/scripts/auto_backup_gcs.sh >> /home/tfisher/paper_factory/logs/backup.log 2>&1") | crontab -
```

**收益：**
- 自动释放本地磁盘空间
- 保留历史项目，支持恢复
- 成本：~$2.50/月（100GB Nearline）

---

### 3. **Cloud Logging 结构化日志集成** 📊

**现状：** 日志散落在本地，难以搜索和告警。

**操作步骤：**

```bash
# 安装 Google Cloud Logging Python 客户端
cd /home/tfisher/paper_factory
source .venv/bin/activate
pip install google-cloud-logging

# 创建日志助手模块
cat > /home/tfisher/paper_factory/scripts/cloud_logger.py <<'PYTHON'
#!/usr/bin/env python3
"""
Cloud Logging 集成助手
在所有 Python 脚本中导入并使用
"""
import os
import logging
from google.cloud import logging as cloud_logging

# 只在启用时初始化
if os.getenv("USE_CLOUD_LOGGING", "false") == "true":
    client = cloud_logging.Client()
    client.setup_logging(log_level=logging.INFO)

def log_step_start(base_name: str, step: int):
    """记录 Step 开始"""
    logging.info(
        "Step started",
        extra={
            "base_name": base_name,
            "step": step,
            "event_type": "step_start"
        }
    )

def log_solver_job(job_id: str, solver_type: str, max_time: int):
    """记录求解器任务提交"""
    logging.info(
        "Solver job submitted",
        extra={
            "job_id": job_id,
            "solver_type": solver_type,
            "max_time": max_time,
            "event_type": "solver_submit"
        }
    )

def log_consultation_gate(base_name: str, gate: str):
    """记录人工咨询门禁"""
    logging.warning(
        "Consultation gate triggered",
        extra={
            "base_name": base_name,
            "gate": gate,
            "event_type": "consultation_requested"
        }
    )
PYTHON

# 启用 Cloud Logging
echo "USE_CLOUD_LOGGING=true" >> /home/tfisher/paper_factory/.env
```

**在现有脚本中集成：**

```bash
# 修改 run_paper.sh（在关键步骤添加日志）
# 在文件顶部添加：
# if [[ "${USE_CLOUD_LOGGING:-false}" == "true" ]]; then
#     python3 "$FACTORY/scripts/cloud_logger.py" step_start "$BASE_NAME" "$STEP"
# fi
```

**收益：**
- 集中查询所有日志（`gcloud logging read`）
- 按项目/步骤/错误类型过滤
- 设置自动告警（见下一节）

---

### 4. **Cloud Monitoring 自定义告警** 🚨

**现状：** 无主动监控，问题需人工发现。

**操作步骤：**

```bash
# 创建告警策略（Step 5/6 超时）
gcloud alpha monitoring policies create \
  --notification-channels=<YOUR_EMAIL_CHANNEL_ID> \
  --display-name="Paper Factory Step Timeout" \
  --condition-display-name="Step execution > 2 hours" \
  --condition-threshold-value=7200 \
  --condition-threshold-duration=300s \
  --condition-filter='resource.type="global" AND metric.type="logging.googleapis.com/user/step_duration"'

# 创建自定义指标（在 cloud_logger.py 中添加）
cat >> /home/tfisher/paper_factory/scripts/cloud_logger.py <<'PYTHON'

from google.cloud import monitoring_v3
import time

def record_step_duration(base_name: str, step: int, duration_seconds: float):
    """记录 Step 执行时长到 Cloud Monitoring"""
    if os.getenv("USE_CLOUD_LOGGING") != "true":
        return
    
    client = monitoring_v3.MetricServiceClient()
    project_name = f"projects/{os.getenv('GCP_PROJECT_ID', 'level-night-476302-k0')}"
    
    series = monitoring_v3.TimeSeries()
    series.metric.type = "custom.googleapis.com/paper_factory/step_duration"
    series.metric.labels["base_name"] = base_name
    series.metric.labels["step"] = str(step)
    
    now = time.time()
    point = monitoring_v3.Point()
    point.value.double_value = duration_seconds
    point.interval.end_time.seconds = int(now)
    series.points = [point]
    
    client.create_time_series(name=project_name, time_series=[series])
PYTHON
```

**告警场景：**
1. Step 5/6 执行超过 2 小时 → 邮件通知
2. 连续 3 个项目在 Step 13 失败 → 触发检查
3. Cloud Run Solver API 5xx 错误率 > 10% → 自动重试
4. 磁盘空间 > 80% → 触发自动备份

---

### 5. **Secret Manager 管理敏感信息** 🔐

**现状：** `.env` 包含明文 API Key，不安全。

**操作步骤：**

```bash
# 启用 Secret Manager API
gcloud services enable secretmanager.googleapis.com

# 迁移敏感配置
echo -n "$(grep MINERU_TOKEN /home/tfisher/paper_factory/.env | cut -d= -f2)" | \
  gcloud secrets create mineru-token --data-file=-

echo -n "$(grep GEMINI_API_KEY /home/tfisher/paper_factory/.env | cut -d= -f2)" | \
  gcloud secrets create gemini-api-key --data-file=-

echo -n "$(grep DEEPSEEK_API_KEY /home/tfisher/paper_factory/.env | cut -d= -f2)" | \
  gcloud secrets create deepseek-api-key --data-file=-

echo -n "$(grep JWT_SECRET /home/tfisher/paper_factory/web/.env | cut -d= -f2)" | \
  gcloud secrets create dashboard-jwt-secret --data-file=-

# 创建访问脚本
cat > /home/tfisher/paper_factory/scripts/load_secrets.sh <<'SCRIPT'
#!/bin/bash
# 从 Secret Manager 加载环境变量
export MINERU_TOKEN=$(gcloud secrets versions access latest --secret=mineru-token)
export GEMINI_API_KEY=$(gcloud secrets versions access latest --secret=gemini-api-key)
export DEEPSEEK_API_KEY=$(gcloud secrets versions access latest --secret=deepseek-api-key)
export JWT_SECRET=$(gcloud secrets versions access latest --secret=dashboard-jwt-secret)
SCRIPT
chmod +x /home/tfisher/paper_factory/scripts/load_secrets.sh

# 修改 launch_agents.sh（在顶部添加）
# source "$FACTORY/scripts/load_secrets.sh"
```

**收益：**
- 从 `.env` 删除敏感信息
- 统一密钥管理和轮换
- 审计日志（谁在何时访问了哪个密钥）

---

### 6. **Pub/Sub 实时事件流（可选）** 📡

**现状：** Web Dashboard 通过轮询获取状态更新。

**操作步骤：**

```bash
# 创建事件主题
gcloud pubsub topics create paper-factory-events

# 创建订阅（Web Dashboard 监听）
gcloud pubsub subscriptions create dashboard-events \
  --topic=paper-factory-events \
  --ack-deadline=60

# 在 run_paper.sh 中发布事件
cat > /home/tfisher/paper_factory/scripts/publish_event.sh <<'SCRIPT'
#!/bin/bash
# 发布事件到 Pub/Sub
EVENT_TYPE="$1"
BASE_NAME="$2"
METADATA="$3"

gcloud pubsub topics publish paper-factory-events \
  --message="{\"type\":\"$EVENT_TYPE\",\"base\":\"$BASE_NAME\",\"meta\":$METADATA}" \
  2>/dev/null || true  # 失败不影响主流程
SCRIPT
chmod +x /home/tfisher/paper_factory/scripts/publish_event.sh

# 集成到关键节点
# run_paper.sh 中每个 Step 完成后：
# ./scripts/publish_event.sh "step_completed" "$BASE_NAME" "{\"step\":$STEP}"
```

**Web Dashboard 订阅（修改 `web/backend/app.py`）：**

```python
from google.cloud import pubsub_v1
import json

# 后台任务监听 Pub/Sub
async def pubsub_listener():
    subscriber = pubsub_v1.SubscriberClient()
    subscription_path = subscriber.subscription_path(
        'level-night-476302-k0', 
        'dashboard-events'
    )
    
    def callback(message):
        event = json.loads(message.data.decode())
        asyncio.create_task(manager.broadcast({
            "type": "pubsub_event",
            "event": event
        }))
        message.ack()
    
    streaming_pull_future = subscriber.subscribe(subscription_path, callback)
    await streaming_pull_future
```

**收益：**
- 真正的实时推送（不是 3 秒轮询）
- 解耦架构（多个服务可订阅同一事件）
- 支持事件回放和审计

---

## 中期优化（需要代码调整）

### 7. **Firestore 持久化状态** 💾

**收益：** Web Dashboard 重启后不丢失项目状态。

**实施复杂度：** 中等（需修改 `web/backend/app.py`）

**关键修改点：**
1. 替换内存 `job_registry` 为 Firestore 查询
2. `launch_agents.sh` 写入项目启动时间到 Firestore
3. `run_paper.sh` 每个 Step 后更新 Firestore

**示例代码：**

```python
from google.cloud import firestore

db = firestore.Client()

# 替换内存注册表
def register_project(base_name: str, status: str):
    db.collection('projects').document(base_name).set({
        'status': status,
        'started_at': firestore.SERVER_TIMESTAMP,
        'current_step': 0
    })

def update_project_step(base_name: str, step: int):
    db.collection('projects').document(base_name).update({
        'current_step': step,
        'updated_at': firestore.SERVER_TIMESTAMP
    })
```

---

### 8. **Cloud Tasks 任务队列（高级）** 🎯

**收益：** 可靠的任务调度，支持重试和优先级。

**实施复杂度：** 高（需重构 `launch_agents.sh` 进程模型）

**适用场景：**
- 需要在多台服务器分布式运行项目
- 需要任务优先级（紧急项目插队）
- 需要自动重试失败的 Step

**暂时不推荐：** 当前单服务器部署，bash 进程管理已足够。

---

## 实施优先级和时间表

### 🔴 Phase 1: 立即启用（今天，0.5 天）

```bash
✅ 1.1 启用 Cloud Solver 自动路由（5分钟）
✅ 1.2 配置 Cloud Storage 自动备份（30分钟）
✅ 1.3 启用 Cloud Logging（30分钟）
✅ 1.4 迁移密钥到 Secret Manager（30分钟）
```

**预期收益：**
- 长时间求解任务自动上云
- 本地磁盘空间释放 50%+
- 密钥安全性提升

### 🟡 Phase 2: 监控和告警（本周，1 天）

```bash
⚠️ 2.1 配置 Cloud Monitoring 自定义指标（2小时）
⚠️ 2.2 设置告警策略（1小时）
⚠️ 2.3 集成 Pub/Sub 事件流（4小时）
```

**预期收益：**
- 主动发现问题（Step 超时、磁盘满）
- 实时事件推送（不再轮询）

### 🟢 Phase 3: 持久化改造（下周，2-3 天）

```bash
📊 3.1 Firestore 集成（1天）
📊 3.2 Web Dashboard 状态持久化（1天）
📊 3.3 历史项目查询界面（0.5天）
```

**预期收益：**
- Dashboard 重启不丢失状态
- 支持历史项目检索和对比

---

## 快速启动脚本

我已经为你准备了一键启用脚本：

```bash
# 保存为 /home/tfisher/paper_factory/scripts/enable_gcp_features.sh
#!/bin/bash
set -euo pipefail

echo "=========================================="
echo "  Paper Factory GCP 功能启用向导"
echo "=========================================="
echo ""

# 1. 启用 Cloud Solver
read -p "启用 Cloud Run Solver 自动路由? (y/n): " enable_solver
if [[ "$enable_solver" == "y" ]]; then
    echo "USE_CLOUD_SOLVER=true" >> .env
    echo "CLOUD_THRESHOLD_TIME=300" >> .env
    echo "✓ Cloud Solver 已启用"
fi

# 2. 配置自动备份
read -p "配置 Cloud Storage 自动备份? (y/n): " enable_backup
if [[ "$enable_backup" == "y" ]]; then
    gsutil mb -p level-night-476302-k0 -c NEARLINE -l europe-west4 \
      gs://level-night-476302-k0-paper-archive 2>/dev/null || echo "存储桶已存在"
    
    cat > scripts/auto_backup_gcs.sh <<'SCRIPT'
#!/bin/bash
FACTORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
gsutil -m rsync -r "$FACTORY_ROOT/complete/" gs://level-night-476302-k0-paper-archive/complete/
gsutil -m rsync -r "$FACTORY_ROOT/papers/" gs://level-night-476302-k0-paper-archive/papers/
find "$FACTORY_ROOT/complete/" -maxdepth 1 -type d -mtime +30 -exec rm -rf {} \;
SCRIPT
    chmod +x scripts/auto_backup_gcs.sh
    
    (crontab -l 2>/dev/null; echo "0 2 * * * $PWD/scripts/auto_backup_gcs.sh >> logs/backup.log 2>&1") | crontab -
    echo "✓ 自动备份已配置（每天 2:00 AM）"
fi

# 3. 启用 Cloud Logging
read -p "启用 Cloud Logging? (y/n): " enable_logging
if [[ "$enable_logging" == "y" ]]; then
    pip install google-cloud-logging
    echo "USE_CLOUD_LOGGING=true" >> .env
    echo "✓ Cloud Logging 已启用"
fi

# 4. 迁移密钥到 Secret Manager
read -p "迁移敏感密钥到 Secret Manager? (y/n): " enable_secrets
if [[ "$enable_secrets" == "y" ]]; then
    gcloud services enable secretmanager.googleapis.com
    
    grep MINERU_TOKEN .env | cut -d= -f2 | gcloud secrets create mineru-token --data-file=- 2>/dev/null || echo "mineru-token 已存在"
    grep GEMINI_API_KEY .env | cut -d= -f2 | gcloud secrets create gemini-api-key --data-file=- 2>/dev/null || echo "gemini-api-key 已存在"
    grep DEEPSEEK_API_KEY .env | cut -d= -f2 | gcloud secrets create deepseek-api-key --data-file=- 2>/dev/null || echo "deepseek-api-key 已存在"
    
    echo "✓ 密钥已迁移到 Secret Manager"
    echo "⚠️  建议从 .env 删除明文密钥"
fi

echo ""
echo "=========================================="
echo "  启用完成！"
echo "=========================================="
echo ""
echo "下一步："
echo "  1. 测试 Cloud Solver: ./scripts/gcp_solver_client.sh --type python --max-time 60 --script <test.py>"
echo "  2. 查看 Cloud Logging: gcloud logging read 'resource.type=global' --limit 50"
echo "  3. 监控备份任务: tail -f logs/backup.log"
echo ""
```

**使用方法：**

```bash
cd /home/tfisher/paper_factory
chmod +x scripts/enable_gcp_features.sh
./scripts/enable_gcp_features.sh
```

---

## 成本估算（月度）

基于当前使用模式（假设每月 50 个项目）：

| 服务 | 月度成本 | 说明 |
|-----|---------|------|
| Cloud Run (Solver) | $15-40 | 按实际执行时间，冷启动无费用 |
| Cloud Storage | $3-5 | 200GB 存储（100GB Standard + 100GB Nearline） |
| Cloud Logging | $5-10 | 50GB 日志/月（超过免费配额 50GB） |
| Cloud Monitoring | 免费 | 自定义指标在免费配额内 |
| Pub/Sub | $1-2 | 10k 消息/月 |
| Secret Manager | $0.36 | 6 个 Secret × $0.06/月 |
| **总计** | **$24-57** | vs 本地 24/7 运行成本 |

**节省来源：**
- 本地服务器可按需关机（夜间/周末）
- Cloud Run 按秒计费，闲时零成本
- 自动清理旧数据，避免磁盘扩容

---

## 下一步建议

### 🚀 立即可做（今天）：

```bash
# 1. 启用 Cloud Solver（5 分钟）
cd /home/tfisher/paper_factory
echo "USE_CLOUD_SOLVER=true" >> .env
echo "CLOUD_THRESHOLD_TIME=300" >> .env

# 测试
./scripts/gcp_solver_client.sh --type python --max-time 60 \
  --script <某个项目的求解脚本>

# 2. 配置自动备份（10 分钟）
./scripts/enable_gcp_features.sh  # 选择 y 启用备份

# 3. 验证 Cloud Run 健康状态
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  https://solver-api-144584367563.europe-west4.run.app/health | jq .
```

### 📋 本周可做：

1. 配置 Cloud Monitoring 告警（Step 超时通知）
2. 集成 Pub/Sub 实时事件（替换轮询）
3. 启用 Cloud Logging 结构化日志

### 📊 长期改进：

1. Firestore 持久化（重构 Web Dashboard）
2. Vertex AI 统一 LLM 调用（降低成本）
3. Cloud Tasks 分布式任务队列（多服务器扩展）

---

**需要我帮你执行哪个功能的启用？我可以生成具体的命令或修改代码。**
