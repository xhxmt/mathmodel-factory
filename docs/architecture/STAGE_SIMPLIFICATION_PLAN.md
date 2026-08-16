# 10-Stage 编排合同与实施状态

> 状态：代码合同已实施；新项目默认使用 `stage_v1`，旧 native 项目保持
> `step_v2` 直到显式切换，Legacy 项目保持冻结适配路径。
>
> schema、调度、恢复、迁移、dirty flag、条件 Step 13、Final snapshot 冻结和
> 自动化验收均已落地。完成定义中的真实新项目无 override clean-room 运行仍是
> 独立的运营验收项；本文不会用模拟 lifecycle 或注入审计 receipt 冒充该结果。

## 1. 目标与非目标

目标是降低状态机和恢复路径的复杂度，同时保留现有验证、证据和兼容接口：

```text
用户层：8 个比赛阶段
调度层：10 个持久 Stage
验证层：Step 0–16 的 Step contract / checkpoint
        + Step 8.5 reviewer-entry gate / artifact contract
执行层：Stage 内的 Agent、确定性检查器和人工 Gate subtask
证据层：不可变 snapshot、receipt、fingerprint 与 release
```

本计划不是把 `STEPS.md` 中的 Step 直接删成十步，也不允许批量重写历史项目状态。
现有 Step ID、validator、审计 profile、artifact gate、恢复入口和 Legacy 兼容判断在迁移期内
都必须保持可用。

## 2. 概念边界

- **Stage**：调度、持久状态、重试和恢复的边界。
- **Step contract**：产物、validator、兼容和证据链的稳定边界。
- **Subtask**：一次 Agent、确定性检查、人工 Gate 或发布操作；可以在 Stage 内独立重试。
- **Artifact / Receipt**：证明某个输入快照完成了某项工作；Agent 自述不能替代 receipt。

合并后不得退回“整个 Stage 失败就全部重跑”。每个 Stage 必须持久化当前 subtask、所消费的
输入 fingerprint 和已完成的 Step checkpoint，从最后一个有效 checkpoint 恢复。

## 3. 当前 Stage 映射

| Stage | 原 Step contract | 持久子任务与退出条件 |
|---|---|---|
| 1 `UNDERSTAND` | Step 0 + Step 1 | 解析赛题并保存 Step 0 checkpoint；完成研究、候选流和 viability gate |
| 2 `MODEL_TOURNAMENT` | Step 2 + Step 3 | 并行 proposal/critique；Human Gate 1；持久化结构化选择后退出 |
| 3 `MODEL_CONTRACT` | Step 4 | 模型、符号、假设、quality contract 和 model audit 独立通过 |
| 4 `SOLVE` | Step 5 | canonical results、solver receipts、派生物和 results audit 通过 |
| 5 `VALIDATE_MODEL` | Step 6 + Step 7 | sensitivity、robustness、assumption update、`evaluation.md` 和复跑的 results audit 通过 |
| 6 `REVIEWER_ENTRY` | Step 8 + Step 8.5 | 生成图表与说明；生成阅卷入口三件套；`entry_gate.md` PASS 后退出 |
| 7 `DRAFT_AND_AUDIT` | Step 9 + Step 10 | 起草论文；独立运行 paper audit；定向修复并复验；保存双 fingerprint |
| 8 `REVIEW_AND_REVISE` | Step 11 + Step 12 + Step 13 | Reviewer/Reviser 有界循环；Step 13 是条件性退出 subtask；BLOCKING/MAJOR 清零或进入人工处理 |
| 9 `FINAL_PROSE` | Step 14 + Step 15 | 摘要、可选人工摘要覆盖、引用、表格、去模板化和最终文字机械检查；退出为 `CONTENT_READY` |
| 10 `FINALIZE` | Step 16 | Human Gate 2 转换守卫；清理后冻结输入；Final Audit、打包、验证、不可变 release、原子切换 `current.json` |

Step 4、Step 5 和 Step 16 继续独立：模型合同必须在高成本求解前失败关闭；正式求解需要独立
资源与 receipt 边界；发布是唯一可以改变对外 current release 的事务。

## 4. 关键合并合同

### 4.1 Step 0 + Step 1

Step 0 作为 `UNDERSTAND` 内的题面解析与归一化 checkpoint 保留。研究失败或外部资料暂不可用时，
恢复不得重新解析已由 fingerprint 证明未变化的题面。

### 4.2 Step 2 + Step 3

候选生成、批评和方法选择属于同一个 `MODEL_TOURNAMENT`，但 Human Gate 1 仍是持久等待状态。
选择决定继续写入 SQLite append-only decision；`method_decision.md` 与 `chosen_method.md` 仍是
可重建投影，不能反向覆盖权威选择。控制平面确定性生成 `chosen_method.md`，并在
`method_decision.md` 写入 request/decision/primary/auxiliary/subject/options 机器头；Step 3
validator 与 Step 4 prepare 共享同一验证器，重新核验当前候选 evidence fingerprint 和决定
receipt。`human_review.md` 即使被改写，也不参与权威选择解析。

### 4.3 Step 6 + Step 7

`evaluation.md` 必须消费本轮 sensitivity/robustness fingerprint。变化按责任边界分流：

- 只有 sensitivity、robustness 或 `evaluation.md` 变化：留在 Stage 5，重跑对应 subtask 和
  必要的 results audit；
- 从同一 canonical results 重新生成 derived artifact：可以留在 Stage 5，但 canonical、来源
  映射和 solver-provenance fingerprint 必须保持不变；
- canonical results、source mapping、solver evidence、adopted objective、采用值或决策变量变化：
  设置 `RESULT_DIRTY` 并重新打开 Stage 4 `SOLVE`。

已有 solver receipt 只证明它绑定的执行，没有授权 Stage 5 改写 Stage 4 的采用结果。

### 4.4 Step 8 + Step 8.5

Step 8.5 改为 `REVIEWER_ENTRY` 的 completion gate：

```text
生成/精修图表
  -> 生成 visualization_log
  -> 生成 reviewer_entry_map / anchor_figure_plan / entry_gate
  -> 校验 entry_gate
  -> Stage PASS
```

三件套在 Stage 退出后不得被后台投影或验证命令无条件重写。需要更新时必须重新打开
`REVIEWER_ENTRY`，形成新 fingerprint。

### 4.5 Step 9 + Step 10

合并的是调度节点，不是审计信任域。至少保留以下内部状态：

```text
draft_pending
draft_complete
audit_running
audit_failed
repairing
audit_passed
```

必须同时满足：

1. Step 9 写作 Agent 无权宣布 Step 10 PASS。
2. paper audit 仍由确定性检查器或独立上下文运行。
3. `draft_content_fingerprint` 与 `paper_audit_input_fingerprint` 必须分别持久化，audit receipt
   必须绑定两者及 `checker_contract_sha256`。重新计算的 fingerprint 与 receipt 中对应的
   同语义 fingerprint 任一不一致时，旧 PASS 失效；不得直接比较两种不同输入域的 hash。
4. 修复只针对审计指出的输入；修复后必须重新计算 hash 并复验。

### 4.6 Step 11 + Step 12 + 条件 Step 13

Stage 内运行 Reviewer 与 Reviser 两个独立角色：

```text
review -> issue ledger -> revise -> mechanical check -> review
```

只有未解决的 BLOCKING/MAJOR 为零才可退出。达到 Stage 的有界迭代上限后进入明确的人工处理或
失败状态，不得把未解决项自动降级，也不得删除 `PROTECTED` 问题。

Step 13 保留现有整数 Step contract，并作为 Stage 8 的条件性退出 subtask：

```text
review -> revise -> mechanical check
  -> MODEL_DIRTY OR MATH_DIRTY OR RESULT_DIRTY ? math preflight : skip receipt
  -> Stage PASS
```

如果 Stage 9 越权改变数学或结果语义，必须重新打开其责任 Stage，并在再次进入 Stage 9 前重新
完成 Stage 8 的条件性退出。Step 13 不成为第 11 个 Stage。

### 4.7 Step 14 + Step 15

`FINAL_PROSE` 的顺序固定为：

```text
abstract generation
  -> optional human abstract override / critic selection
  -> citation audit
  -> table formatting
  -> de-robotification
  -> final prose mechanical check
  -> CONTENT_READY
```

Step 14 的人工摘要是可选覆盖：不存在人工文本时 Agent/critic 仍可完成，不产生强制
`pending_action`。Human Gate 2 保持当前合同位置，作为 Stage 9 到 Stage 10 的
`content_freeze` 转换守卫；`CONTENT_READY` 只表示内容已完成，不表示人类已经批准冻结。

本 Stage 默认只允许修改摘要、叙事、引用、表格表现和格式。若修改 canonical results、模型公式、
约束、核心 numerical claim 或其来源映射，必须设置相应语义 dirty flag，并回到其责任 Stage；
不得在最终润色中静默改变科学内容。

## 5. 条件性数学预审与语义 dirty flag

Step 13 不按 mtime 或整个 `paper.tex` hash 决定是否运行。目标状态至少记录：

```text
MODEL_DIRTY
MATH_DIRTY
RESULT_DIRTY
PROSE_DIRTY
VISUAL_DIRTY
CITATION_DIRTY
FORMAT_DIRTY
```

触发 `CONDITIONAL_MATH_PREFLIGHT` 的条件是：

```text
MODEL_DIRTY OR MATH_DIRTY OR RESULT_DIRTY
```

只有 `PROSE_DIRTY`、`VISUAL_DIRTY`、`CITATION_DIRTY` 或 `FORMAT_DIRTY` 时可以跳过数学预审，
但必须写入机器可验证记录，例如：

```json
{
  "schema_version": "conditional-math-preflight-v1",
  "status": "SKIPPED_NO_MATH_SEMANTIC_CHANGE",
  "based_on_fingerprint": "<sha256>",
  "dirty_flags": ["FORMAT_DIRTY"],
  "step_contract": 13,
  "checker_contract_sha256": "<sha256>",
  "classifier_contract_sha256": "<sha256>"
}
```

dirty flag 不能只靠 Agent 自报。实现时应由受控写入范围、结构化修订声明和 validator 共同决定：

- `model.md`、`quality_contract.json`、模型代码或数学约束变化至少标记 `MODEL_DIRTY` 或
  `MATH_DIRTY`；
- `results/canonical_results.json`、结果来源、附件数值或 solver evidence 变化标记
  `RESULT_DIRTY`；
- 论文公式正文变化标记 `MATH_DIRTY`；
- 纯引用、样式、图像布局和普通叙事变化分别标记对应的非数学 flag；
- 无法可靠分类时失败关闭为数学/结果 dirty，而不是推定干净。

业务 owner 由唯一的 `factory-artifact-ownership-v1` registry 决定，覆盖问题合同、候选流、
方法选择投影、模型合同、采用结果、敏感性/评价、阅卷入口、论文审阅和最终文字产物。
dirty classifier、语义重开、Finalization snapshot 变化、Judge packet 缺件路由、Web 诊断和
final/submission input manifest 共同读取这张表；flag 的语义分类不能反向覆盖更早的业务 owner。
Stage 子任务永久失败时，也必须在同一失败事务中把 baseline delta 写入 dirty cause。

每个 dirty flag 必须持久化：

```text
flag
cause_revision
cause_artifact
baseline_fingerprint
classifier_contract_sha256
```

flag 只能由责任 Stage 针对当前 fingerprint 生成的成功 receipt 清除，Agent 不得直接 clear。
责任路由至少为：

- `MODEL_DIRTY` -> Stage 3；
- `RESULT_DIRTY` -> Stage 4；
- 论文数学表达产生的 `MATH_DIRTY` -> Stage 7/8，并执行条件 Step 13；
- `VISUAL_DIRTY` -> Stage 6 或产生该图的上游责任 subtask；
- `PROSE_DIRTY`、`CITATION_DIRTY`、`FORMAT_DIRTY` -> Stage 9。

下游 Stage 必须继承尚未清除的上游 dirty flag；改变分类器合同会使旧 skip receipt 失效。
Step 13 的 PASS 或 `SKIPPED_NO_MATH_SEMANTIC_CHANGE` 都不授权交付；最终快照仍必须通过
Step 16 的完整审计。

## 6. Finalize 不可变事务

Stage 9 完成 `CONTENT_READY` 后，Stage 9 -> Stage 10 转换守卫要求 SQLite 中已有 Human
Gate 2 / `content_freeze` 决策。Stage 10 随后先完成 final snapshot 之前的 housekeeping：

```text
CONTENT_READY
  -> Human Gate 2 / content_freeze transition guard
  -> cleanup rebuildable project intermediates
  -> build canonical final-input manifest
  -> final_snapshot_created
  -> freeze input set
  -> compile
  -> deterministic audit
  -> three-role Judge
  -> package
  -> verify package
  -> create immutable release
  -> atomic current.json switch
  -> DELIVERED
```

从 `final_snapshot_created` 到 `current.json` 切换完成，任何 Agent 都不得修改 Final Audit 输入。
此后的 cleanup 只能作用于 staging/release 临时空间，不能删除、重写或补生成 final-input
manifest 覆盖的项目文件。
实现必须在关键边界重算 fingerprint；一旦变化：

```text
abort finalization
  -> record FINALIZATION_ABORTED_SNAPSHOT_CHANGED
  -> reopen owning Stage
  -> create a new snapshot
  -> rerun FINALIZE
```

不得在原 snapshot 上就地修补并沿用旧 Judge 或 acceptance receipt。发布器只写 staging、不可变
release 目录和原子 current pointer；审计系统仍不得自行发布。

## 7. 持久化与兼容迁移

实施时采用扩展而非替换：

1. 保持 `STEP_CONTRACTS` 的 ID、validator 和 artifact contract 不变。
2. 新增 versioned Stage catalog，要求 Step 0–16 各自恰好映射到一个 Stage；Step 13 是
   Stage 8 的 conditional exit subtask。Step 8.5 保持 Stage 6 的非整数 completion gate，
   不新增 `active_step=8.5` 或任何 8.5 workflow-state ID。
3. SQLite 新字段/表必须通过 schema migration 增加；旧事件保持不可变，不回写历史。
   `active_stage`、`active_subtask` 和 Step compatibility cursor 必须在同一个 revision/SQLite
   transaction 中更新。每个 Step-backed subtask 携带 `source_step_id`；该 Step contract
   完成时立即更新 `last_completed_step`，Stage 未退出时 `active_step` 仍投影当前 subtask 的
   `source_step_id`。
4. Stage scheduler 是 Stage 项目的唯一调度权威；Step cursor 只是兼容投影。持久化
   `scheduler_generation` 与 `stage_catalog_version`，旧 Step scheduler 必须拒绝接管 Stage
   scheduler 项目，防止两个调度器根据同一 cursor 并行启动。
5. 所有 Step-backed subtask 继承原 `StepContract` 的 timeout、hang timeout、max attempts、
   max reopens 和 contest deadline policy；进入或重试 Stage 不得重置这些预算。Stage 8 的
   Reviewer/Reviser 科学循环预算与 infrastructure retry 分开计数。
6. 未迁移 native 项目的只读 Stage/subtask 投影必须联合使用 `last_completed_step`、
   `active_step`、`pending_action` 和 `status`，不能只看完成游标；只有显式迁移后才使用
   Stage 调度。
7. Legacy 项目继续由 Legacy Adapter 读取，不因本计划自动获得新状态或 `CURRENT_PASS`；Stage
   项目存在未清语义 dirty flag 时不得通过整体回滚到 Legacy 绕过责任 Stage 复验。
   所有 scheduler/control-mode 回滚共享同一 fail-closed guard：拒绝已开始的 attempt，复算
   `stage_cursor_input` baseline 与当前 manifest，拒绝 pending Finalization snapshot、未解决
   projection failure 或 Step-3 projection drift。
8. Web 的 8 个比赛阶段保持稳定，并始终从当前 subtask 的 `source_step_id` 投影，不从
   Stage ID 投影。例如 Stage 7 的 draft subtask 显示 Phase 5，audit subtask 显示 Phase 6。
   10-Stage 主要用于高级诊断、恢复和事件展示。

## 8. 分阶段实施状态

### R0：合同锁定（已完成）

- 增加 Stage catalog 与覆盖测试，但不改变调度路径。
- 固定 Step-to-Stage 映射、Step 13 conditional exit、Step 8.5 非整数 gate、Stage 内 subtask
  名称和 dirty flag 枚举。
- 固定 `source_step_id` -> Web phase 投影，确认八阶段与现状完全一致。
- 版本化 dirty classifier/clear receipt 和各 Step-backed subtask 的预算继承合同。
- 加入文档/代码 schema 版本一致性检查，清除 schema v4 的现役残留描述。

### R1：只读投影（已完成）

- CLI/Web 可以展示当前 Stage 和 subtask。
- 从 `last_completed_step`、`active_step`、`pending_action` 和 `status` 确定性派生，不写新的
  调度状态。
- Stage cursor 的运行中、等待、暂停、完成与恢复位置已有确定性回放测试；真实项目运营回放
  仍按第 10 节单独留证。

### R2：持久 Stage 状态（已完成）

- 增加 schema migration、`scheduler_generation`、Stage transition 和 subtask checkpoint；
  Stage/Step cursor/event 在同一个 revision transaction 中提交。
- 新项目默认使用 Stage scheduler；旧 native 项目保持 Step scheduler，直到显式激活。
- 验证 pause/resume/recovery/reopen/lease 的等价性。

### R3：逐组合并（已完成）

按风险从低到高启用：

1. Step 6 + 7；
2. Step 8 + 8.5；
3. Step 11 + 12；
4. Step 14 + 15；
5. Step 2 + 3；
6. Step 0 + 1；
7. Step 9 + 10。

各组合通过持久 subtask checkpoint 保留独立恢复位置，调度 generation 可显式回滚。
Step 13 归属 Stage 8，条件执行由 versioned dirty classifier、skip receipt 和 clear receipt
共同约束。

### R4：Finalize 冻结与默认切换（代码完成，运营验收待完成）

- 启用 `final_snapshot_created` 后的写入隔离与 fingerprint 监测。
- 通过故障注入、快照篡改、并发发布和恢复测试。
- 新项目代码默认值已经切换为 `stage_v1`；仍需把至少一个真实新项目的无 override
  clean-room Final Audit 与原子发布记录归档为运营验收证据。

## 9. 自动化验收条件

- Step 0–16 ID 和历史 artifact contract 未删除；Step 8.5 保持 reviewer-entry gate，不成为
  workflow-state ID。
- 10 个 Stage 覆盖 Step 0–16，没有重叠或遗漏；Step 13 恰好归属 Stage 8。
- 任一 Stage 能从最后一个有效 subtask/checkpoint 恢复，不必整段重跑。
- `active_stage`、`active_subtask`、`source_step_id`、Step cursor 和 event 在同一 revision
  transaction 中原子一致；旧 Step scheduler 不能接管 Stage scheduler 项目。
- 每个 Step-backed subtask 继承原 timeout、hang timeout、attempt/reopen 和 deadline 预算，
  Stage 重试不会补发预算。
- Step 9 写作 Agent 无法生成有效的 paper-audit PASS。
- paper-audit receipt 分别绑定 draft 与 audit-input fingerprint；只比较同语义 fingerprint。
- Step 13 对模型/数学/结果变化必跑；非数学语义跳过记录绑定 fingerprint、checker contract
  和 dirty-classifier contract。
- dirty flag 记录 cause/baseline/classifier，只有责任 Stage 的成功 receipt 可以清除。
- `FINAL_PROSE` 越权修改科学内容会置 dirty 并阻止进入 `CONTENT_READY`。
- `CONTENT_READY` 后由 Stage 9 -> 10 转换守卫取得 `content_freeze` 人工批准；Step 14 人工摘要
  保持 optional override，不改变无人值守语义。
- pre-finalize cleanup 在 `final_snapshot_created` 前完成；之后 cleanup 不得触碰 final-input
  manifest 覆盖的项目文件。
- `final_snapshot_created` 后修改任一审计输入都会中止发布；旧 receipt 不可复用。
- 原子发布失败时旧 `current.json` 保持有效，不出现 PDF/ZIP/manifest 混合版本。
- 旧 native 和 Legacy 项目无需重写即可读取、暂停、恢复或保持只读完成状态；运行中、等待人工、
  暂停和 interrupted 状态的 Stage 投影均正确。
- Web 用户层从 subtask `source_step_id` 稳定显示 8 个比赛阶段，高级视图可以同时解释
  Stage、Step 和 subtask。

上述代码合同由 Stage catalog、状态迁移、调度/恢复、dirty/finalization、审计、原子发布和
Web contract/build 聚焦测试覆盖。真实模型调用、比赛题输入、三角色 Judge 和最终发布的整链
运行属于下一节的运营验收，不能由 fake lifecycle 测试替代。

## 10. 完成定义

当前完成状态如下：

1. **完成**：Stage schema、调度器、恢复器和兼容迁移进入当前代码；
2. **完成**：自动化验收覆盖 Stage/Step 原子游标、恢复、dirty、冻结和发布失败关闭；
3. **完成**：`step_v2` 兼容投影、显式激活/回滚和固定 lifecycle 对照保持 Step 合同；
4. **待运营验收**：至少一个新建 `contest_core_v1` 项目完成无 override 的真实 Final Audit、
   原子发布和 clean-room replay，并归档 snapshot/release/event 证据；
5. **完成**：`STEPS.md`、`ORCHESTRATION_ENGINE.md`、README、Web 文档和 CHANGELOG 同步更新。

在第 4 项完成前，可以把 10 Stage 视为当前代码与新项目默认运行时合同，但不能声称已经完成
真实比赛项目的端到端生产验证。若该运行暴露 Blocker/Major，必须回滚对应项目的 scheduler
generation 或修复后重新从 clean-room 验收，不能对失败结果做 override 以满足本条。

## 11. 横向运行时收敛

Gate 原因、恢复状态、前端诊断、审计时间线、Human Decision、执行管线和持久 Job 身份已按
独立方案完成核心集成，不改变本文已实施的 10-Stage/Step 合同。Capability/Profile 扩展仍延后。
实现边界和仍待完成的 clean-room 运营验收见
[`RUNTIME_INFRASTRUCTURE_CONVERGENCE_PLAN.md`](RUNTIME_INFRASTRUCTURE_CONVERGENCE_PLAN.md)。
