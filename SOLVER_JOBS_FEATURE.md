# Solver Jobs 面板功能与维护说明

## 概述

2026-08-06 实现并由提交 `5841aae` 合入 `main` 的三个前端功能增强：
1. **Solver 作业面板**（证据链可视化）
2. **统一人工收件箱**（已在 ProjectWorkspace 中实现）
3. **每步耗时/重试预算可视化**（已通过 diagnostics 部分实现）

## 一、Solver 作业面板

### 后端 API (`web/backend/solver_jobs_api.py`)

新增两个端点：

```python
GET /api/projects/{base_name}/solver-jobs
GET /api/projects/{base_name}/solver-jobs/{job_id}
```

#### 列表端点返回格式
```json
{
  "jobs": [
    {
      "job_id": "local_python_20260803152344_c7d700a3",
      "backend": "local",
      "runtime": "python",
      "status": "RUNNING|COMPLETED|FAILED|TIMEOUT",
      "script": "models/m1_dag_ode/05_sensitivity.py",
      "requested_at": 1785770624,
      "started_at": 1785770624,
      "finished_at": null,
      "duration_seconds": 235297,
      "has_submission_receipt": false,
      "has_completion_receipt": false,
      "legacy": false,
      "result_refs": {
        "exit": ".factory/solver_jobs/xxx.json",
        "stderr": "logs/xxx_stderr.log",
        "stdout": "models/xxx.log"
      }
    }
  ]
}
```

#### 详情端点返回格式（solver-job-evidence-v2）
```json
{
  "schema": "solver-job-evidence-v2",
  "job_id": "...",
  "backend": "local|cloud",
  "runtime": "python|julia|matlab|R|gurobi",
  "script": "/abs/path/to/script.py",
  "workdir": "/abs/path/to/workdir",
  "status": "RUNNING|COMPLETED|FAILED|TIMEOUT",
  "max_time_seconds": 900,
  "requested_at": 1785770624,
  "submission": {
    "receipt_path": "...",
    "digest": "sha256:...",
    "argv": ["python3", "script.py"],
    "inputs": [{"path": "data.json", "sha256": "..."}],
    "seeds": [20260804]
  },
  "completion": {
    "receipt_path": "...",
    "terminal_status": "SUCCESS",
    "outputs": [{"path": "results.json", "sha256": "..."}]
  },
  "receipt_ready": true,
  "errors": [],
  "claim_limit": "FULL_TWO_STAGE_RECEIPT|LEGACY_JOB_METADATA_ONLY"
}
```

**核心特性：**
- 支持 legacy jobs（只有状态文件，没有 two-stage receipt）
- 支持新 jobs（完整 submission + completion receipts）
- `receipt_ready=true` 证明提交的代码/输入身份和当前声明的输出哈希
- `errors` 数组记录缺失或无效的 receipt 文件

### 前端组件 (`web/frontend/src/components/SolverJobPanel.vue`)

**UI 结构：**
- 顶部统计栏：总数、运行中、成功、失败
- 过滤器：按状态、backend、runtime 过滤
- 作业列表：卡片式展示
  - 状态徽章（颜色编码）
  - Job ID（可点击展开详情）
  - 脚本路径
  - 时长、时间戳
  - Receipt 状态图标
  - 展开显示：submission/completion receipts、输入输出哈希、错误信息

**交互特性：**
- 自动刷新（15 秒）
- 可展开/折叠详情
- 点击路径复制到剪贴板
- Receipt 完整性指示器（✓ 表示 receipt_ready）

**视觉设计：**
- Aurora Glass 主题一致性
- 状态颜色：
  - RUNNING: amber-400（#fbbf24）
  - COMPLETED: emerald-400（#34d399）
  - FAILED: rose-400（#fb7185）
  - TIMEOUT: orange-400（#fb923c）
- 玻璃态卡片 + 柔和悬停效果
- 哈希值截断显示（前 8 位）

### 集成

1. **路由注册** (`web/backend/project_api.py`)
   ```python
   project_router.include_router(
       solver_jobs_router,
       prefix="/{base_name}/solver-jobs",
       tags=["solver-jobs"]
   )
   ```

2. **前端集成** (`web/frontend/src/components/ProjectWorkspace.vue`)
   - 新增 "Solver Jobs" tab
   - 位于常驻的产物页签之后、按需出现的选择/咨询/诊断页签之前
   - 使用 API 客户端自动处理认证

3. **API 客户端** (`web/frontend/src/lib/api.js`)
   ```javascript
   SolverJobs.list(baseName)
   SolverJobs.detail(baseName, jobId)
   ```

## 二、验证结果

2026-08-08 对当前 `main` 重新运行：

```bash
.venv/bin/python -m pytest -q \
  tests/test_web_solver_jobs_api.py \
  tests/test_repository_hygiene.py
```

结果为 `13 passed`；同日使用 `uv.lock` 的 Web/Cloud/Models/dev 全量环境复跑仓库测试，
结果为 `770 passed`。下列 API 示例来自 2026-08-06 的实现验收，只用于说明返回语义，
不代表当前生产部署状态。

### 测试环境
- 后端：`uv run python3 -m apps.web.backend.main`
- 前端：Vite dev server (port 5173)
- 测试项目：`cumcm_2020_a_codex_luna`

### 验证项
- ✅ 后端 API 返回正确的 job 列表
- ✅ 单个 job 详情包含完整 evidence-v2 结构
- ✅ Legacy jobs 正确标记为 `LEGACY_JOB_METADATA_ONLY`
- ✅ 错误信息正确报告缺失的 receipt 文件
- ✅ 前端组件正确渲染状态、统计和详情
- ✅ 自动刷新和展开/折叠交互正常
- ✅ Aurora Glass 视觉主题一致

### API 测试输出示例

```bash
# 列表端点
GET /api/projects/cumcm_2020_a_codex_luna/solver-jobs
返回 49KB 的作业列表

# 详情端点  
GET /api/projects/cumcm_2020_a_codex_luna/solver-jobs/local_python_20260801035612_a8817316
{
  "schema": "solver-job-evidence-v2",
  "status": "COMPLETED",
  "receipt_ready": false,
  "claim_limit": "LEGACY_JOB_METADATA_ONLY",
  "errors": ["MISSING_OR_INVALID_TWO_STAGE_RECEIPT: ..."]
}
```

## 三、价值与影响

### 审计链完整性
- **之前**：solver 作业证据只能通过 SSH 查看 `.factory/solver_jobs/` 目录
- **现在**：Web 界面直接展示所有作业、receipt 状态、输入输出哈希
- **影响**：审计人员可以验证"这个结果是否真的由声称的代码/数据产生"

### 调试效率
- 快速定位超时或失败的 solver 作业
- 直接查看 stdout/stderr 路径
- 识别 legacy vs. 新 receipt 系统的作业

### 与现有功能协同
- 与 DiagnosticsCard 的"建议操作"互补
- 与 LogConsole 的日志查看联动
- 为未来的"审计中心"功能奠定基础

## 四、后续改进方向

1. **日志内嵌查看**：点击 stdout/stderr 路径时弹出日志查看器
2. **作业重试**：为失败的作业提供"重试"按钮
3. **时间线视图**：将 solver jobs 整合到 revision 事件流时间线
4. **批量操作**：批量终止运行中的作业
5. **性能分析**：同一脚本多次运行的耗时对比图表

## 五、文件清单

### 新增文件
- `web/backend/solver_jobs_api.py`
- `web/frontend/src/components/SolverJobPanel.vue`
- `tests/test_web_solver_jobs_api.py`

### 修改文件
- `web/backend/project_api.py` (注册路由)
- `web/backend/main.py` (导入新模块)
- `web/frontend/src/components/ProjectWorkspace.vue` (新增 tab)
- `web/frontend/src/lib/api.js` (新增 API 方法)

## 六、维护注意事项

1. **Receipt 系统演进**：如果 `solver-job-evidence-v2` schema 升级，需同步更新 API 返回格式
2. **Legacy 迁移**：随着旧项目逐步迁移到新 receipt 系统，`legacy=false` 比例会上升
3. **云求解器集成**：当 `USE_CLOUD_SOLVER=true` 广泛使用时，需验证云端 receipt 的回传
4. **性能考虑**：大型项目可能有数百个 solver jobs，考虑分页或虚拟滚动

---

**实现日期**：2026-08-06

**合并状态**：已进入 `main`（`5841aae`）

**当前验证**：2026-08-08，聚焦测试 `13 passed`，锁定环境全量测试 `770 passed`

**部署边界**：本文不声明已部署或 live verified；生产状态以 `web/docs/deployment/DEPLOYMENT.md` 的现场核验为准
