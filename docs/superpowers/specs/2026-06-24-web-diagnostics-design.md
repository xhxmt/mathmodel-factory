# 设计：`Web Project Diagnostics`

> 日期: 2026-06-24  
> 状态: 已讨论确认，待用户评审  
> 目标: 让单个项目在 Web 中出现“长时间无进展”或“门禁未满足”时，用户能在 30 秒内知道卡点原因、证据位置和下一步动作。

## 动机

当前 Web Dashboard 已具备：

- 项目列表与工作台；
- 基于 `WebSocket` 的状态刷新；
- `runner.log` / step log 查看；
- 咨询面板、Step 8.5 时间线、产物浏览。

但它仍然主要解决“能看到什么状态”，还没有解决“为什么现在没往前走”。

当前最痛的两类场景是：

1. **长时间无新日志**
   - 用户无法快速区分是正常慢、agent 仍在工作、外部依赖阻塞，还是 runner 已经卡住。
2. **流程没有显式报错，但门禁未满足**
   - 例如 Step 8.5 未通过、验证输出未达标、人工咨询未恢复。
   - 证据散落在 `.heartbeat`、`runner.log`、`entry_gate.md`、`consultation/` 等文件中，需要手工拼接。

现有系统其实已经有不少可用信号：

- `.heartbeat`
- `.runner.lock.info`
- `runner.log`
- `entry_gate.md`
- `consultation/*.md`
- `verify_step_output` 的失败路径
- Step 8.5 / Gate 2 / stale lock reclaim 等特殊分支

问题不在于“完全没有信号”，而在于这些信号没有被整理成一个稳定、可解释、可点击的诊断层。

## 方案比较

### 方案 A：纯 Web 推断型

- 只改 `web/backend` 和前端；
- 读取现有 `.heartbeat`、日志和产物文件后自行推断诊断结论。

优点：

- 改动最小；
- 不需要触碰 runner。

缺点：

- 诊断依据不稳定，很多结论只能猜；
- 难以可靠区分“慢”和“挂”；
- 随着特殊分支增多，后端推断逻辑会越来越脆弱。

### 方案 B：事件增强型

- 保留现有文件契约；
- runner 额外写结构化诊断快照和事件流；
- Web 读取这些诊断文件，并做少量兜底推断。

优点：

- 改动可控；
- 诊断结论有明确来源，不必大量猜测；
- 后续新增卡点时，只需新增 reason code 和事件写入点。

缺点：

- 需要同时修改 runner、backend、frontend。

### 方案 C：深契约型

- 为更多 step 引入标准化失败码、阻塞码和产物契约；
- 把诊断能力扩展成更完整的 observability 基础设施。

优点：

- 长期一致性最好；
- 扩展到全平台观测时基础更强。

缺点：

- 第一轮范围过大；
- 需要触达更多步骤和脚本，风险与成本都偏高。

**最终选择：方案 B。**

## 范围与非目标

### 范围

- 为每个项目增加 `diagnostics/` 目录；
- 让 `run_paper.sh` 在关键状态变化点写结构化诊断快照和事件；
- 为 Web 增加单项目诊断接口、列表摘要和工作台诊断卡；
- 覆盖第一批高价值 reason code：
  - `NO_LOG_PROGRESS`
  - `AWAITING_STEP8_5`
  - `CONSULTATION_PENDING`
  - `VERIFY_OUTPUT_FAILED`
  - `LOCK_STALE_RECLAIMED`

### 非目标

- 不接入第三方监控平台；
- 不做跨项目聚合大盘；
- 不设计完整 metrics / tracing 系统；
- 不在第一版里规范所有 step 的失败码；
- 不替用户自动执行危险恢复动作；
- 不改变现有 `ProjectStatus` 的基本语义；
- 不重构整个 pipeline 的产物契约。

## 核心设计

### 关键决定 1：诊断数据放在项目目录下的 `diagnostics/`

每个项目增加：

- `diagnostics/status.json`
- `diagnostics/events.jsonl`

原因：

- 与项目状态天然同域，便于归档、迁移和排障；
- 不需要引入额外数据库；
- 完成项目移入 `complete/` 后，诊断轨迹也可一并保留。

### 关键决定 2：runner 声明事实，Web 只做轻量聚合

职责边界如下：

- `run_paper.sh`
  - 负责写当前诊断状态和关键事件；
  - 明确声明“当前为什么没前进”。
- `web/backend`
  - 读取诊断文件；
  - 聚合成适合 UI 的结论和动作；
  - 仅在诊断文件缺失或损坏时做兜底推断。
- `web/frontend`
  - 展示当前诊断；
  - 提供跳转证据和操作入口；
  - 不自建复杂诊断状态机。

这样可以避免“runner 里发生了什么，Web 只能猜”的长期问题。

### 关键决定 3：项目状态与诊断状态分层

保留现有项目状态：

- `running`
- `paused`
- `awaiting_consultation`
- `completed`
- `ready`
- `setup`
- `killed`

新增独立诊断层回答：

- 为什么没前进；
- 证据在哪；
- 用户下一步能做什么。

也就是说，一个项目可以同时是：

- `status = running`
- `diagnostic_reason_code = NO_LOG_PROGRESS`

这比强行把所有问题塞进一个状态枚举更清晰。

## 诊断数据模型

### `diagnostics/status.json`

这是 UI 直接消费的当前快照。建议字段：

```json
{
  "version": 1,
  "state": "running",
  "current_step": 8,
  "current_action": "step8_5_gate_review",
  "reason_code": "AWAITING_STEP8_5",
  "reason_summary": "Step 8.5 未通过，等待补足 reviewer entry 入口材料",
  "since": 1782240000,
  "last_event_at": 1782240300,
  "evidence": [
    {"kind": "file", "path": "entry_gate.md"},
    {"kind": "heartbeat", "value": "AWAITING_STEP8_5:8 1782240300"}
  ],
  "suggested_actions": [
    "open_entry_gate",
    "open_reviewer_entry_artifacts",
    "refresh_status"
  ]
}
```

字段约束：

- `state`
  - `running | waiting | stalled | retrying | completed | failed | unknown`
- `current_step`
  - 当前诊断针对的 step 编号；
  - Step 8.5 仍然写为 `8`，通过 `reason_code` 和 `current_action` 表达其特殊性。
- `current_action`
  - 当前具体动作或阶段，例如 `step_dispatch`、`agent_run`、`verification`、`consultation_wait`、`step8_5_gate_review`
- `reason_code`
  - 当前最主要的解释性原因；
  - 第一版仅覆盖已确认的 5 类。
- `reason_summary`
  - 中文短句，直接给 UI 展示；
  - 避免前端再拼接复杂文案。
- `evidence`
  - 指向文件、heartbeat 或其他事件；
  - 用于“查看证据”动作。
- `suggested_actions`
  - 枚举式动作 id，由 backend 再映射成最终按钮。

### `diagnostics/events.jsonl`

这是追加式事件流，每行一条 JSON。建议字段：

```json
{"ts":1782240000,"step":8,"type":"step_started","message":"Step 8.5 gate review started","reason_code":"AWAITING_STEP8_5","files":["entry_gate.md"],"meta":{"attempt":1}}
```

第一版事件类型至少包括：

- `step_started`
- `step_completed`
- `agent_started`
- `agent_finished`
- `heartbeat_state_changed`
- `retry_scheduled`
- `verification_failed`
- `gate_blocked`
- `consultation_requested`
- `consultation_resolved`
- `lock_stale_reclaimed`

事件流主要用于：

- 工作台展示最近关键事件；
- 事后复盘；
- 在快照丢失时辅助 backend 做兜底判断。

## Runner 侧改动

### 写入点

`run_paper.sh` 需要在以下关键路径写诊断事件并更新快照：

1. 进入 step
2. step 成功完成
3. 进入 agent 执行
4. 超时 / hang kill / retry
5. `verify_step_output` 失败
6. Step 8.5 `VERDICT != PASS`
7. consultation 请求 / 等待 / 恢复
8. stale lock reclaim
9. 显式写入特殊 heartbeat 时
   - `ACTIVE:`
   - `STUCK:`
   - `CONSULT:`
   - `AWAITING_STEP8_5:`

### reason code 触发规则

#### `NO_LOG_PROGRESS`

用于“日志长时间无新增，但项目还没完成”的场景。

触发条件建议：

- runner 仍存活；
- heartbeat 仍在更新或最近更新过；
- 但 step 日志在阈值内无增长。

注意：

- 这只能判定为“无日志进展”，不能直接等同于“挂死”；
- 若 heartbeat 也过旧，则应改判为更重的 `stalled` 快照状态，而不是继续沿用该 code。

#### `AWAITING_STEP8_5`

触发条件：

- Step 8.5 执行后 `entry_gate.md` 的 `VERDICT` 不是 `PASS`；
- 或 runner 已写 `AWAITING_STEP8_5:` heartbeat。

快照应携带证据：

- `entry_gate.md`
- `reviewer_entry_map.md`
- `anchor_figure_plan.md`
- heartbeat 内容

#### `CONSULTATION_PENDING`

触发条件：

- `maybe_consult` 请求人工输入；
- 或 runner 写入 `CONSULT:` heartbeat；
- 或 `consultation/*.md` 已存在且 `STATUS` 仍非 `READY`。

#### `VERIFY_OUTPUT_FAILED`

触发条件：

- `verify_step_output` 返回非 0；
- 或 step 验证分支决定重试。

快照应尽量提供：

- 当前 step
- 失败的关键文件
- 最近一次 runner 提示

#### `LOCK_STALE_RECLAIMED`

触发条件：

- stale lock reclaim 分支命中并成功回收锁。

该 code 更接近“重要诊断事件”而非长期阻塞态：

- 事件流中必须记录；
- 快照可短暂显示，随后允许被新的更高优先级原因覆盖。

## Backend 设计

### 新增接口

新增：

- `GET /api/projects/{base_name}/diagnostics`

返回内容建议包括：

- 当前诊断快照
- 最近关键事件
- 可执行动作
- 证据文件
- 是否来自 runner 原生诊断还是 fallback 推断

建议返回结构：

```json
{
  "source": "runner",
  "status": { "...": "..." },
  "events": [ "...recent events..." ],
  "actions": [ "...ui actions..." ],
  "evidence_files": [ "...paths..." ]
}
```

### 现有接口的补充

`GET /api/projects`

- 增加轻量摘要字段，供列表页直接使用：
  - `diagnostic_badge`
  - `diagnostic_priority`
  - `diagnostic_reason_code`

`GET /api/projects/{base_name}/status`

- 保持兼容；
- 不承担完整诊断职责。

### 判定顺序

backend 判定顺序固定为：

1. 优先读取 `diagnostics/status.json`
2. 读取 `diagnostics/events.jsonl` 作为证据补充
3. 若诊断文件缺失或损坏，则做轻量 fallback
4. fallback 只做有限推断，不做重度猜测

fallback 可使用的信号：

- `.heartbeat`
- `entry_gate.md`
- `consultation/`
- `runner.log`
- `audit_issue_ledger.md`

### 动作映射

动作由 backend 生成，前端只渲染。

#### `NO_LOG_PROGRESS`

- `open_runner_log`
- `refresh_status`

若 PID 仍在运行：

- 不提供 `resume_project`

#### `AWAITING_STEP8_5`

- `open_entry_gate`
- `open_reviewer_entry_artifacts`
- `refresh_status`

若 gate 还未转为 `PASS`：

- 不提供 `resume_project`

#### `CONSULTATION_PENDING`

- `open_consultation_request`
- `open_human_review`
- `refresh_status`

若已 `READY` 但未恢复：

- 允许提供 `resume_project`

#### `VERIFY_OUTPUT_FAILED`

- `open_runner_log`
- `open_failed_artifact`
- `refresh_status`

若 runner 已退出且项目处于可恢复状态：

- 允许提供 `resume_project`

#### `LOCK_STALE_RECLAIMED`

- `open_runner_log`
- `refresh_status`

## Frontend 设计

### 列表页

项目列表新增诊断徽章，独立于现有 `status` 标签。

第一版徽章示例：

- `静默过久`
- `等待 8.5 门禁`
- `等待人工`
- `验证失败待重试`

要求：

- 诊断徽章优先级独立于项目状态；
- `running` 不能掩盖诊断问题；
- 列表页只显示简要结论，不展示完整证据。

### 工作台诊断卡

在 `ProjectWorkspace` 顶部增加诊断卡，优先于日志区。

结构：

1. 主结论
   - 例如：`当前阻塞：Step 8.5 未通过`
2. 依据摘要
   - 例如：`entry_gate.md = REVISE；heartbeat = AWAITING_STEP8_5:8；最近 12 分钟无 step 完成`
3. 最近关键事件
   - 仅显示最近 3-5 条
4. 一键动作
   - `查看证据`
   - `刷新诊断`
   - `恢复运行`
   - `打开咨询`

目标：

- 用户先看诊断卡，后看日志；
- 不再要求用户先自己翻 `runner.log` 才知道要找什么。

### 日志区增强

日志区上方增加执行上下文条，显示：

- `当前动作`
- `持续时长`
- `最近事件`

这样即使日志没增长，用户也能先知道：

- runner 是否仍活着；
- 当前是不是在等待某个门禁或人工动作；
- 现在是否只是“慢”，而不是“死”。

### 证据跳转

第一版不做复杂事件瀑布流，只做高价值证据跳转：

- 跳到 `runner.log`
- 跳到 `entry_gate.md`
- 跳到 `consultation/*.md`
- 跳到失败产物文件

这与用户当前的真实需求更一致：快速定位，而不是完整浏览所有事件。

## 状态与优先级

为避免同一项目同时命中多个原因时互相覆盖，诊断优先级建议如下：

1. `CONSULTATION_PENDING`
2. `AWAITING_STEP8_5`
3. `VERIFY_OUTPUT_FAILED`
4. `NO_LOG_PROGRESS`
5. `LOCK_STALE_RECLAIMED`

说明：

- 需要用户介入的状态优先于提示性状态；
- 门禁阻塞优先于“日志没动”的现象性状态；
- stale reclaim 是重要事件，但不应长期压住更当前的阻塞原因。

## 容错与降级

### 诊断文件缺失或损坏

处理策略：

- backend 自动降级到 fallback；
- UI 显示“诊断信息缺失，已使用兜底判断”；
- 不允许页面空白或报 500 后无替代信息。

### 事件文件过大

处理策略：

- backend 只读取最后 N 条关键事件；
- 不全量扫描历史。

### runner 写诊断失败

处理策略：

- 所有诊断写入必须是 best-effort；
- 不能影响主流水线继续执行；
- 写入失败最多记录到 `runner.log`。

### 未识别的 reason code

处理策略：

- UI 显示通用诊断卡；
- 原样展示 code、证据和日志入口；
- 不阻塞主页面渲染。

## 测试与验证

### 后端测试

- diagnostics 文件存在时的正常读取；
- diagnostics 缺失时的 fallback；
- diagnostics 损坏时的降级；
- 各 reason code 的动作映射；
- 列表摘要字段是否正确生成。

### runner 验证

至少验证以下路径确实写出事件和快照：

- step start
- Step 8.5 阻塞
- consultation 等待
- `verify_step_output` 失败
- stale lock reclaim

### 前端验证

- 列表页诊断徽章显示；
- 工作台诊断卡渲染；
- 诊断卡动作显隐是否符合后端返回；
- 证据跳转是否能打开正确日志或文件区域。

## 第一版完成标准

第一版完成后，用户打开任一项目，应能在 30 秒内回答：

1. 它现在是不是没往前走？
2. 如果没往前走，是慢、等门禁、等人工，还是验证没过？
3. 证据文件在哪？
4. 现在在 Web 上能直接点什么动作？

如果这四个问题仍需要用户自己翻目录和拼线索，则本设计视为未完成。
