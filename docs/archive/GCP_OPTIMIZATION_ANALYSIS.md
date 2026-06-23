# GCP 服务优化方案分析

## 项目现状总结

**Paper Factory** 是一个本地多智能体数学建模竞赛论文生成工厂，包含：
- 16 步建模工作流（从题目解析到论文编译）
- 本地求解器执行（Python/Julia/MATLAB/R/Gurobi）
- Web Dashboard（Vue 3 + FastAPI）
- 初步的 Cloud Run 求解器 API（已实现但未广泛使用）
- 大量 LLM 智能体调用（Codex/Claude/GPT/Gemini）

**当前痛点：**
1. 本地求解器执行时间长（Step 5/6 经常超时）
2. 大文件存储混乱（`complete/` 125MB，`papers/` 44MB）
3. Web Dashboard 缺少持久化存储（内存注册表重启后丢失）
4. 日志和状态文件碎片化（`logs/` 1.3MB，`run_state/` 2.2MB）
5. 没有资源监控和成本控制
6. 缺少分布式任务调度

---

## 推荐 GCP 服务及优化方案

### 1. **Cloud Run** — 已部署，需扩展使用

**现状：** 已有 `cloud/solver_api.py` 和 `scripts/gcp_solver_client.sh`，但默认未启用。

**优化建议：**

#### 1.1 增强 Cloud Run Solver API
```yaml
优先级: P0 (立即改进)
收益: 将长时间求解任务卸载到云端，释放本地资源

改进点:
- ✅ 已实现: 基础 Python/Julia/MATLAB/R/Gurobi 支持
- 🔧 需优化: 添加并发限制和队列管理（当前单实例内存注册表）
- 🔧 需优化: 支持流式日志输出（当前只有完成后的 GCS 上传）
- 🔧 需优化: 添加成本估算（按执行时间收费的 Cloud Run）

实现步骤:
1. 启用 Firestore/Cloud Tasks 替换内存 job_registry
2. 添加 Cloud Logging 集成，实时推送日志到 Web Dashboard
3. 配置自动扩缩容 (min-instances=0, max-instances=10)
4. 在 solver_router.sh 中默认启用云端路由
```

**代码示例：**
```bash
# 在 .env 中设置
USE_CLOUD_SOLVER=true
CLOUD_THRESHOLD_TIME=180  # 3分钟以上的任务自动上云
```

#### 1.2 将 Web Dashboard 也迁移到 Cloud Run
```yaml
优先级: P1 (中期)
收益: 远程访问、持久化状态、更好的并发处理

当前问题:
- web/backend/app.py 使用内存状态（重启后丢失）
- 依赖本地文件系统（ongoing/, complete/, logs/）
- 需要手动启动 ./start_dashboard.sh

改进方案:
1. 容器化 Web Dashboard（Dockerfile 已有参考 cloud/Dockerfile）
2. 挂载 Cloud Storage FUSE 替换本地目录访问
3. 使用 Cloud SQL (PostgreSQL) 存储项目元数据
4. 配置 Cloud Load Balancing + HTTPS
```

---

### 2. **Cloud Storage** — 集中存储和版本管理

**现状：** 本地存储 125MB 完成项目 + 44MB 论文 + 碎片化日志。

**优化方案：**

#### 2.1 分层存储策略
```yaml
优先级: P0
收益: 节省本地磁盘，启用版本控制，降低成本

存储桶设计:
├── <project_id>-paper-factory-hot/       # Standard 存储类
│   ├── ongoing/{base_name}/              # 进行中的项目
│   ├── logs/{date}/{base_name}/          # 最近 30 天日志
│   └── uploads/{uuid}/                   # 用户上传的题目文件
├── <project_id>-paper-factory-cold/      # Nearline/Archive 存储类
│   ├── complete/{base_name}/             # 已完成项目（30天后自动迁移）
│   └── papers/{base_name}/               # 最终论文 PDF
└── <project_id>-solver-jobs/             # 已有（求解器临时文件）

生命周期策略:
- logs/: 30 天后删除
- ongoing/: 180 天后移至 Archive
- complete/: 立即移至 Nearline（低频访问）
- papers/: 永久保留，启用版本控制
```

**配置示例：**
```bash
# 创建存储桶和生命周期策略
gsutil mb -p level-night-476302-k0 -c STANDARD -l europe-west4 \
  gs://level-night-476302-k0-paper-factory-hot

gsutil lifecycle set lifecycle.json \
  gs://level-night-476302-k0-paper-factory-hot
```

`lifecycle.json`:
```json
{
  "rule": [
    {
      "action": {"type": "Delete"},
      "condition": {"age": 30, "matchesPrefix": ["logs/"]}
    },
    {
      "action": {"type": "SetStorageClass", "storageClass": "ARCHIVE"},
      "condition": {"age": 180, "matchesPrefix": ["ongoing/"]}
    },
    {
      "action": {"type": "SetStorageClass", "storageClass": "NEARLINE"},
      "condition": {"age": 1, "matchesPrefix": ["complete/"]}
    }
  ]
}
```

#### 2.2 Cloud Storage FUSE 挂载
```bash
# 在本地/Cloud Run 中挂载 GCS 为文件系统
gcsfuse level-night-476302-k0-paper-factory-hot /home/tfisher/paper_factory/ongoing
gcsfuse level-night-476302-k0-paper-factory-cold /home/tfisher/paper_factory/complete
```

---

### 3. **Firestore / Cloud SQL** — 元数据和状态管理

**现状：** 
- `run_state/process_registry` 文本文件追踪进程
- Web Dashboard 内存 job_registry 重启后丢失
- `checkpoint.md` 文件状态不权威

**优化方案：**

#### 3.1 Firestore 替换文件状态
```yaml
优先级: P1
收益: 实时同步、多客户端一致性、查询能力

数据模型:
collections/
├── projects/
│   └── {base_name}/
│       ├── status: string (running|paused|completed|failed)
│       ├── current_step: int (0-16)
│       ├── started_at: timestamp
│       ├── updated_at: timestamp
│       ├── checkpoints: map
│       └── consultation_gates: array
├── solver_jobs/
│   └── {job_id}/
│       ├── type: string
│       ├── status: string
│       ├── submitted_at: timestamp
│       ├── result_urls: array
│       └── metrics: map
└── audit_logs/
    └── {uuid}/
        ├── timestamp: timestamp
        ├── action: string
        ├── user: string
        └── details: map
```

**集成代码示例：**
```python
# web/backend/firestore_state.py
from google.cloud import firestore

db = firestore.Client()

def update_project_status(base_name: str, step: int, status: str):
    doc_ref = db.collection('projects').document(base_name)
    doc_ref.set({
        'current_step': step,
        'status': status,
        'updated_at': firestore.SERVER_TIMESTAMP
    }, merge=True)

def get_active_projects():
    return db.collection('projects').where('status', '==', 'running').stream()
```

---

### 4. **Cloud Tasks / Pub/Sub** — 异步任务队列

**现状：** `launch_agents.sh` 使用 bash 后台进程，难以管理和恢复。

**优化方案：**

#### 4.1 Cloud Tasks 替换本地进程管理
```yaml
优先级: P1
收益: 可靠的任务调度、自动重试、可观测性

架构:
┌─────────────────┐      ┌──────────────┐      ┌────────────────┐
│ Web Dashboard   │─────▶│ Cloud Tasks  │─────▶│ Worker Service │
│ (创建项目)      │      │ (队列)       │      │ (Cloud Run)    │
└─────────────────┘      └──────────────┘      └────────────────┘
                                │                        │
                                │                        ▼
                                │                ┌────────────────┐
                                └───────────────▶│ Firestore      │
                                                 │ (状态更新)     │
                                                 └────────────────┘

任务类型:
- step_execution: 执行单个 Step (1-16)
- solver_job: 提交求解器任务
- compilation: 编译 LaTeX
- consultation_reminder: 人工咨询提醒
```

**实现示例：**
```python
# 提交 Step 任务到 Cloud Tasks
from google.cloud import tasks_v2

client = tasks_v2.CloudTasksClient()
queue_path = client.queue_path(PROJECT_ID, REGION, 'paper-factory-steps')

task = {
    'http_request': {
        'http_method': tasks_v2.HttpMethod.POST,
        'url': f'{WORKER_URL}/execute_step',
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps({
            'base_name': 'cumcm2024b',
            'step': 5,
            'timeout': 3600
        }).encode()
    }
}

client.create_task(request={'parent': queue_path, 'task': task})
```

#### 4.2 Pub/Sub 实时事件流
```yaml
优先级: P2
收益: 解耦组件、实时通知、多订阅者

Topics:
- project.created
- project.step_completed
- project.failed
- solver.job_submitted
- solver.job_completed
- consultation.requested

订阅者:
1. Web Dashboard (WebSocket 推送)
2. Telegram Bot (通知)
3. Monitoring Service (指标采集)
4. Audit Log Writer (Firestore)
```

---

### 5. **Cloud Logging & Monitoring** — 可观测性

**现状：** 日志散落在 `logs/`, `run_state/`, 项目目录中，难以搜索和监控。

**优化方案：**

#### 5.1 结构化日志
```python
# 在所有 Python 脚本中使用 Cloud Logging
import google.cloud.logging

logging_client = google.cloud.logging.Client()
logging_client.setup_logging()

logger = logging.getLogger(__name__)
logger.info('Step 5 started', extra={
    'base_name': 'cumcm2024b',
    'step': 5,
    'solver_type': 'python',
    'expected_duration': 1800
})
```

#### 5.2 自定义指标和告警
```yaml
指标:
- paper_factory/project_duration (按 step 分组)
- paper_factory/solver_execution_time (按 type 分组)
- paper_factory/step_failure_rate
- paper_factory/consultation_response_time
- paper_factory/llm_token_usage (按 model 分组)

告警策略:
1. Step 5/6 执行超过 2 小时 → 发送 Slack/Email
2. 连续 3 个项目在 Step 13 失败 → 触发 on-call
3. 日志中出现 "BLOCKING" 但未标记 PROTECTED → 警告
4. Cloud Run 实例 CPU > 80% 持续 5 分钟 → 扩容
```

---

### 6. **Vertex AI** — LLM 调用优化

**现状：** 通过 Codex/Claude CLI 调用模型，缺少统一管理。

**优化方案：**

#### 6.1 Vertex AI Model Garden 统一接口
```yaml
优先级: P2
收益: 统一计费、缓存、速率限制、A/B 测试

支持的模型:
- Gemini 1.5 Pro/Flash (已支持 Gemini API)
- Claude 3.5 Sonnet (通过 Vertex AI)
- Llama 3.1 405B (fine-tuned for modeling)

优化点:
1. 启用 Prompt Caching (重复的 modeling_guide.md)
2. 批量推理（Step 2 并行建模提案）
3. 模型路由：简单步骤用 Flash，复杂步骤用 Pro
```

#### 6.2 Function Calling 替换 Bash 脚本
```python
# 用 Gemini Function Calling 替换 solver_submit.sh 手动解析
tools = [
    {
        'function_declarations': [{
            'name': 'submit_solver_job',
            'description': '提交求解器任务',
            'parameters': {
                'type': 'object',
                'properties': {
                    'solver_type': {'type': 'string', 'enum': ['python', 'julia', 'matlab']},
                    'script_path': {'type': 'string'},
                    'max_time': {'type': 'integer'}
                }
            }
        }]
    }
]
```

---

### 7. **Cloud Build** — CI/CD 和自动化

**现状：** 已有 `cloud/cloudbuild.yaml` 但未充分使用。

**优化方案：**

#### 7.1 扩展 CI/CD 流水线
```yaml
# cloudbuild.yaml
steps:
  # 1. 构建 Solver API
  - name: 'gcr.io/cloud-builders/docker'
    args: ['build', '-t', 'gcr.io/$PROJECT_ID/solver-api', './cloud']
  
  # 2. 构建 Web Dashboard
  - name: 'gcr.io/cloud-builders/docker'
    args: ['build', '-t', 'gcr.io/$PROJECT_ID/web-dashboard', './web']
  
  # 3. 运行测试
  - name: 'gcr.io/$PROJECT_ID/solver-api'
    entrypoint: 'pytest'
    args: ['tests/']
  
  # 4. 部署到 Cloud Run
  - name: 'gcr.io/cloud-builders/gcloud'
    args:
      - 'run'
      - 'deploy'
      - 'solver-api'
      - '--image=gcr.io/$PROJECT_ID/solver-api'
      - '--region=europe-west4'
      - '--platform=managed'
  
  # 5. 更新 Firestore 配置
  - name: 'gcr.io/cloud-builders/gcloud'
    entrypoint: 'python3'
    args: ['scripts/deploy_config.py', '--env=production']

triggers:
  - name: 'deploy-on-push'
    github:
      owner: 'xhxmt'
      name: 'paper-factory'
      push:
        branch: '^main$'
```

---

### 8. **Secret Manager** — 安全管理

**现状：** `.env` 文件包含敏感信息，未加密。

**优化方案：**

```bash
# 迁移敏感配置到 Secret Manager
echo -n "$MINERU_TOKEN" | gcloud secrets create mineru-token --data-file=-
echo -n "$ADMIN_PASSWORD" | gcloud secrets create dashboard-admin-password --data-file=-
echo -n "$JWT_SECRET" | gcloud secrets create jwt-secret --data-file=-

# 在 Cloud Run 中挂载 Secret
gcloud run deploy solver-api \
  --set-secrets=MINERU_TOKEN=mineru-token:latest,JWT_SECRET=jwt-secret:latest
```

---

## 实施路线图

### Phase 1: 基础设施迁移 (2-3 周)
```
✅ P0.1 启用 Cloud Storage 存储桶 + 生命周期策略
✅ P0.2 默认启用 Cloud Run Solver (修改 solver_router.sh)
✅ P0.3 集成 Firestore 替换内存状态
✅ P0.4 配置 Cloud Logging
✅ P0.5 迁移敏感配置到 Secret Manager
```

### Phase 2: 服务化改造 (3-4 周)
```
🔧 P1.1 Web Dashboard 容器化 + Cloud Run 部署
🔧 P1.2 Cloud Tasks 任务队列
🔧 P1.3 Pub/Sub 事件总线
🔧 P1.4 Cloud Build CI/CD 流水线
```

### Phase 3: 智能优化 (4-6 周)
```
📊 P2.1 Cloud Monitoring 自定义指标和告警
📊 P2.2 Vertex AI 统一 LLM 接口
📊 P2.3 BigQuery 数据仓库（历史项目分析）
📊 P2.4 Cloud Functions 定时清理任务
```

---

## 成本估算

基于当前使用量（假设每月处理 50 个项目）：

| 服务 | 月度成本 (USD) | 说明 |
|-----|---------------|------|
| Cloud Run (Solver API) | $20-50 | 2 vCPU, 4GB RAM, 平均 30 分钟/任务 |
| Cloud Storage | $5-10 | 200GB Standard + 500GB Nearline |
| Firestore | $5-10 | 50k 读/写操作/天 |
| Cloud Tasks | $1-2 | 10k 任务/月 |
| Cloud Logging | $10-20 | 50GB 日志/月 |
| Vertex AI (Gemini) | $50-100 | 500k tokens/天 (已有成本，迁移到 Vertex AI 可能更便宜) |
| **总计** | **$91-192** | vs 本地硬件折旧 + 电费 |

**节省项：**
- 减少本地计算资源闲置（夜间/周末）
- 按需扩容，避免过度配置
- 自动化运维，减少人工成本

---

## 风险和注意事项

### 技术风险
1. **网络延迟**: Cloud Run 冷启动 1-3 秒，影响短任务
   - **缓解**: 配置 min-instances=1 保持热实例
   
2. **GCS 挂载性能**: gcsfuse 比本地磁盘慢 2-5 倍
   - **缓解**: 热数据用本地 SSD，冷数据用 GCS

3. **Firestore 限制**: 每秒 10k 写入上限
   - **缓解**: 批量写入，避免高频更新

### 合规风险
1. **数据隐私**: 学生竞赛题目可能有版权
   - **缓解**: 启用 VPC-SC 限制数据流出

2. **成本超支**: Vertex AI Token 消耗不可控
   - **缓解**: 设置 Budget Alerts ($200/月)

---

## 下一步行动

### 立即可做（无需架构变更）：
1. ✅ 创建 GCS 存储桶并配置生命周期
2. ✅ 在 `.env` 中设置 `USE_CLOUD_SOLVER=true`
3. ✅ 测试一个长时间求解任务通过 Cloud Run 执行
4. ✅ 配置 Cloud Logging Python SDK

### 需要代码修改：
1. 🔧 实现 Firestore 状态管理层 (`web/backend/firestore_state.py`)
2. 🔧 修改 `run_paper.sh` 在每个 Step 后写入 Firestore
3. 🔧 添加 Cloud Storage FUSE 挂载到 `launch_agents.sh`

### 需要基础设施配置：
1. 📋 编写 Terraform/gcloud 脚本自动化创建资源
2. 📋 配置 IAM 角色和服务账号
3. 📋 设置 Cloud Monitoring Dashboard

---

**建议优先级排序：**
1. **P0**: Cloud Storage + Cloud Run Solver (立即收益)
2. **P1**: Firestore + Web Dashboard Cloud Run (架构基础)
3. **P2**: Monitoring + Vertex AI (长期优化)
