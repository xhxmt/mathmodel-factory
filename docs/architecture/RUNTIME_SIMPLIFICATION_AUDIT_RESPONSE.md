# 运行时简化审计 — 独立复核与修订方案

> **交付对象：** 提出《服务器运行数据与架构方案审计》的审计方与项目所有者
> **生成时间：** 2026-09-26
> **性质：** 对上一轮审计报告的**逐条响应 + 独立复验**，并给出修订后的方案
> **复核方式：** 只读。未修改、未提交、未推送仓库任何内容；唯一新增文件即本文件
> **编号规则：** `V-*` 复验确认、`X-*` 复验更正、`S-*` 复验强化（比原报告更强的结论）、`U-*` 未验证项
> **本文件取代的是"方案"，不取代被审计报告本身** —— 被审计报告的原文结论保持可读，本文件只标注哪些成立、哪些需要收窄

---

## 0. 复核方法与证据边界

### 0.1 复核方式

上一轮报告基于只读插件读取服务器数据。本次复核**没有直接采信报告的任何数字**，而是回到同一批原始数据独立重算。所用手段：

| 手段 | 具体方式 |
|---|---|
| 项目发现 | 复现插件发现逻辑（`apps/mcp/workspace.py:21` `STORAGE_ROOTS = ("ongoing", "complete")` + `SAFE_PROJECT_NAME`），按同名规则重新枚举 |
| 事件与作业计数 | Python `sqlite3` 以 `file:<db>?mode=ro` URI 只读打开各项目 `.factory/state.db`，用 SQL 直接聚合 |
| 载荷级结论 | 解析 `events.payload_json` 与 `stage_checkpoint_history.receipt_json`，读取 `_workflow` 信封、`reason`、`recovery_target`、`state_patch` |
| 文件级结论 | 直接 `stat` / 读取文件字节数与前缀 |
| 源码级结论 | 读取 `factory_core/effective_prompt.py`、`apps/mcp/workspace.py` 等被引用位置的原文 |

**所有可复现命令与 SQL 见附录 A。**

### 0.2 样本与真实路径

三个样本的 `ongoing/` 入口**不都是项目真实目录**，这一点对"能否用当前数据认证部署代码"很关键：

| 简称 | 项目目录名 | 真实解析路径 | 是否符号链接 |
|---|---|---|---|
| **R** | `cumcm_2025_b_codex_luna_stability_20260817_run4` | `/home/tfisher/paper_factory/.worktrees/cumcm-2025b-stability-run2-4d2eb32/ongoing/…/run4` | 是（**git worktree**） |
| **B** | `cumcm_2025_b_gpt_formal_20260908t153023z` | `/home/tfisher/.codex/formal_2025b/20260908T153023Z/source/ongoing/…` | 是（**仓库外运行根**） |
| **A** | `cumcm_2026_a_fable_pro_20260910` | `/home/tfisher/paper_factory/ongoing/cumcm_2026_a_fable_pro_20260910` | 否（仓库内真实目录） |

三个样本的数据库当前标识一致：`schema_version=9`、`runtime_generation=native_v2`、`scheduler_generation=stage_v1`、`stage_catalog_version=factory-stage-catalog-v1`。

### 0.3 明确未取得实现级证据

以下项目**本次没有取得证据**，因此本文件不把它们写成已通过（与附录 B 对应）：

- **`U-01` 部署引擎的确定 commit。** 本仓库当前 `HEAD` 为 `a3322367128b8c54b3246377577bf8f43319cc47`（2026-09-24T13:40:45Z，分支 `feat/mcp-readonly-server`），且工作区**存在未提交改动**（`apps/mcp/*`、`tests/*`、`deploy/*`）。仓库当前状态本身就说明"当前 HEAD"不能代替"当时跑出这些数据的引擎版本"。插件版本（1.0.0）与引擎版本是两个东西。
- **`U-02` runner 的事务边界与执行权入口实现。** 事件里存在 `expected_revision`、`effect_hashes_after`、`state_hash_before/after` 等结构，但**结构存在不等于提交边界与并发锁实现正确**。
- **`U-03` `scripts/verify_numbers.py` 及其消费者。** 本次只读了 manifest 的产物与其前 16 KB，没有读生成器与消费者源码，因此**无法证明**该 manifest 是否进入 final audit / candidate fingerprint 的完整消费链。
- **`U-04` 发布原件的字节闭包。** 未对 release 目录内 PDF / ZIP / manifest 做哈希闭合校验（只读了目录清单与部分收据 JSON）。
- **`U-05` 分类器的完整依赖声明。** 本次只验证了具体哪些 `cause_artifact` 出现在 `dirty_causes`，未读取 classifier 的完整规则集。

---

## 1. 对报告 B 节（运行数据）的逐项复验

### 1.1 项目与样本边界 —— `V-01` 完全成立

复现插件的发现逻辑后重新枚举：

| 存储根 | 目录项 | 通过 `SAFE_PROJECT_NAME` 且为目录 | 有 `.factory/state.db` |
|---|---|---|---|
| `ongoing/` | 6 | 6 | **6** |
| `complete/` | 12 | 11 | 0 |
| 合计 | 18 | **17** | **6** |

`complete/_validation_index.json` 是一个文件、且首字符 `_` 不满足 `^[A-Za-z0-9]`，被两条规则同时排除。

> **结论：** 报告"项目总数 17、有 state.db 的项目 6"**精确成立**，且排除原因可复现。

### 1.2 三项目计数总表 —— `V-02` 全部精确成立

| 指标 | R：run4 | B：2025 B formal | A：2026 A |
|---|---|---|---|
| 当前状态 | `failed` | `completed` | `completed` |
| revision | 2,484 | 1,411 | 561 |
| 事件总数 | 2,484 | 1,411 | 561 |
| solver jobs | 361 | 202 | 14 |
| 已完成 solver jobs | 335 | 178 | 8 |
| 失败 solver jobs | 26 | 23 | 5 |
| 数据库仍为 `running` 的作业 | **0** | **1** | **1** |
| `RUN_STARTED` | 85 | 36 | 93 |
| `RUN_STOPPED` | 19 | 2 | 61 |
| `STEP_STARTED` | 121 | 88 | 92 |
| `STEP_SUCCEEDED` | 71 | 45 | 65 |
| `STEP_FAILED` | 24 | 12 | 12 |
| `RECOVERY_DECIDED` | 55 | 6 | 19 |
| `STAGE_SEMANTIC_REOPENED` | 13 | 9 | 11 |
| content freeze 代数 | 4 | 4 | 6 |
| `FINAL_SNAPSHOT_CREATED` | 4 | 10 | 5 |
| `PROJECT_COMPLETED` | 0 | 2 | 1 |
| 当前 dirty flag 条数 | 3 | 1 | 1 |

补充验证（报告 B.3 的项目）：A 的 `RESUMED=13`、`PAUSED=11`、`RUN_BOUNDARY_REACHED=2`、`STEP_REOPENED=2`，全部吻合。

**一处口径说明：** 表中"revision"与"事件总数"在三个样本里**恰好相等**（2484/1411/561），因为在 schema v9 下 `events.revision` 就是主键、即事件即版本计数。报告把两者并列在同一格写作"revision／事件总数"，容易让读者以为是两个独立量；实际是同一个量。

### 1.3 两处占比换算 —— `V-03` 成立但需要补口径

- **A：`RUN_STARTED + RUN_STOPPED = 154`，占 561 的 27.45%** ✅ 精确。
- **R：solver 相关事件 1,805 条，占 2,484 的 72.66%** ✅ 精确，但报告写作"提交、提交收据、运行、终态和完成收据"，实际构成是 **6 类**：`SOLVER_JOB_SUBMITTED` 361 + `SOLVER_JOB_RUNNING` 361 + `SOLVER_JOB_RECEIPT_SUBMITTED` 361 + `SOLVER_JOB_RECEIPT_COMPLETED` 361 + `SOLVER_JOB_COMPLETED` 335 + `SOLVER_JOB_FAILED` 26 = 1,805。

> 报告此处"终态"实际含 completed 与 failed 两类，读者若按字面只加 5 类会得到 1,779 而对不上。**建议修订版把 6 类显式列出。**

### 1.4 B 节复验结论

`V-01`~`V-03` 之外**未发现任何数字性错误**。B 节可以原样采信，只需补 1.2/1.3 两处口径说明。

---

## 2. 对报告 C 节（九个机制）的复验

### 2.1 九机制总表

报告的九机制总表在**方向上是可复验的**，以下逐行给出独立结论（"作者原始设计动机"仍不可核实，报告的自我限定正确）：

| 机制 | 报告判断 | 复验结论 |
|---|---|---|
| `one_native_step_per_run` | 保留为显式策略，不作所有项目默认 | `V-04` 成立：A 的 61 次 `RUN_STOPPED` **全部** `reason.code=RUN_STOPPED`、`message="max_steps"`，无一例外 |
| semantic reopen | 保留失效语义，替换过粗的目标选择 | `V-05` 成立，见 2.2 |
| dirty classifier | 保留，增加产物角色与阻断级别 | `S-01` 强化：分类器已在读 manifest 形状路径（见 2.8） |
| prompt input binding | 必须保留，缩小到实际有效输入 | `S-02` **强化为已证明**，见 2.7 |
| content freeze | 改为明确候选内容的审批 | `V-06` 成立，见 2.3 |
| final audit | 保留，细分失败类型与恢复目标 | `V-06` 成立，见 2.3 |
| delivery | 保留 —— 是安全收益 | `V-06` 成立，见 2.3 |
| numbers manifest | 收缩职责并分层存储 | `V-07` + `S-03`，见 2.8 |
| human gate | 保留两个正常 gate + 独立例外授权 | `V-08` 成立 + `X-01` 更正，见 2.6 |

### 2.2 Semantic Reopen：11 次逐条复核 —— `V-05` 全部成立

报告 C.2 的 11 行表**逐条吻合**。下表为独立复核结果（阶段映射按 `recovery_target.resume_after_step` 与 `state_patch.last_completed_stage` 反推）：

| revision | 主体（subject） | 回退目标 | 该 revision 的 `dirty_causes` 关键项 | 复核 |
|---|---|---|---|---|
| 51 | Stage 2 / `parallel_model_proposals` | Stage 1（`resume_after=-1`） | `MODEL_DIRTY(1, viability_gate.md)`, `MODEL_DIRTY(1, viable_streams.md)` | ✅ |
| 129 | Stage 4 / `solve` | Stage 3（`resume_after=3`） | `MODEL_DIRTY(3, models/m1_m4_adoption_step5/03_adopt_results.py)`, `MODEL_DIRTY(3, models/reporting_scope/*)`, `MODEL_DIRTY(3, quality_contract.json)` | ✅ |
| 143 | Stage 5 / `sensitivity` | Stage 4（`resume_after=4`） | 唯一 Stage 4 归属项：`RESULT_DIRTY(4, solve_log.md)` | ✅ |
| 199 | Stage 7 / `numerical_gate` | Stage 3（`resume_after=3`） | `MODEL_DIRTY(3, models/published_delivery/generate.py)`, `MODEL_DIRTY(3, models/reporting_scope/scope_review_manifest.json)`, `MODEL_DIRTY(3, quality_contract.json)` | ✅ |
| 253 | Stage 8 / `revision` | Stage 3（`resume_after=3`） | `MODEL_DIRTY(3, symbol_table.md)` | ✅ |
| 310 | Stage 8 / `revision` | Stage 3（`resume_after=3`） | `MODEL_DIRTY(3, models/reporting_scope/scope_review_manifest.json)` | ✅ |
| **414** | Stage 10 / `content_freeze_guard` | **Stage 4**（`resume_after=4`） | **`MATH_DIRTY(8, judge_evidence.json)`, `RESULT_DIRTY(4, judge_evidence.json)`** | ✅ |
| 433 | Stage 10 / `delivery` | Stage 9（`resume_after=13`） | `FORMAT_DIRTY(9, paper.tex)`, `FORMAT_DIRTY(10, judge_evidence.json)`, `PROSE_DIRTY(9, paper.tex)` | ✅ |
| 455 | Stage 10 / `delivery` | Stage 9（`resume_after=13`） | `FORMAT_DIRTY(10, judge_evidence.json)`, `FORMAT_DIRTY(10, scope_review_manifest.json)` | ✅ |
| 482 | Stage 10 / `delivery` | Stage 10（`resume_after=15`） | `FORMAT_DIRTY(10, judge_evidence.json)` | ✅ |
| 495 | Stage 10 / `delivery` | Stage 9（`resume_after=13`） | `CITATION_DIRTY/FORMAT_DIRTY/PROSE_DIRTY(9, paper.tex)`, `FORMAT_DIRTY(10, judge_evidence.json)`, `FORMAT_DIRTY(9, results/validation_supplement_20260912/report.json)` | ✅ |

**r414 是报告最有力的单个证据，复验完全支持：**

```
r414  type=STAGE_SEMANTIC_REOPENED  subject_stage=10  subject_subtask=content_freeze_guard
      reason.message = "Stage 10 changed content owned by Stage 4"
      recovery_target = {stage:10, subtask:content_freeze_guard, resume_after_step:4}
      state_patch     = {last_completed_stage:3, last_completed_step:4}
      dirty_causes@414 = [MATH_DIRTY(owner 8, judge_evidence.json),
                          RESULT_DIRTY(owner 4, judge_evidence.json)]
```

即：**一个证据文件变化 → 直接退到 Stage 4 求解**。报告的三层拆分（写入权限 / 变化分类 / 依赖失效）在此处成立：`judge_evidence.json` 是 Stage 10 在冻结守卫中写入的观察性证据，它的变化不应默认推导出"必须重算 canonical results"。

**同时确认报告的克制：** 报告明确写"不能直接据此宣布 r414 无须任何工作"。这一点在数据上也是对的 —— 事件没有记录新证据的**内容**，因此无法判定新证据是否推翻了结果来源。**应删除的是"证据文件变化 → 计算回退"的默认捷径，不是证据检查本身。**

### 2.3 Finalization：冻结—快照—审计—交付链 —— `V-06` 全部成立

**六代冻结的请求 revision 逐一对上：**

| 代数 | 请求 revision | 批准 | 冻结主题（decision 原文摘要） |
|---|---|---|---|
| 1 | **410** | 411 | 既有授权继续同一冻结候选；215 个受保护文件未变 |
| 2 | **446** | 447 | 三处已记录的行文更正；冻结 subject `bddf8604…` 仅供真实终审 |
| 3 | **468** | 469 | 冻结 subject `115aa367…` 仅供真实终审 |
| 4 | **485** | 486 | 冻结 subject `ab58fd0f…`；214 个科学与论文来源身份保留 |
| 5 | **508** | 509 | 用户授权直接接收并推进到最终交付 |
| 6 | **541** | 542 | 仅附录文件描述拆段；科学结果与局限未变 |

**`FINAL_SNAPSHOT_CREATED` 只在 5 个 revision 出现：r476、r493、r516、r549、r559**（6 代冻结 / 5 个快照，与报告 C.3 的叙述一致）。

**两条计数必须分开 —— 报告此项成立，且复核后更明确：**

- Step 16 的 `STEP_FAILED` 恰好 **6 条**：r477、r481、r494、r550、r551、r555。
- r517 的终审 FAIL **不在这 6 条里**：它是 `STEP_REOPENED`，其 `final_decision = "REOPEN_REVISION_TEXT"`（见 `X-01`）。

**r550 是保护生效而非应删除的障碍 —— 成立：**

```
r549  FINAL_SNAPSHOT_CREATED   input_fingerprint = 7068d832aaf258b1300a36cd1b946e48614328cde86360a17df4603f265731a7
r550  STEP_FAILED              reason.code = PERMANENT_ATOMIC_DELIVERY
                               delivery_error = "project content changed after Final Audit"
                               audit_snapshot = 7bb15f928f1279ff17b17d9c0447155559da71c046c9d60b0679b1a6d78fbc28
```

而 `.factory/audits/7bb15f…/attempts/` 内的审计文件记录 `status=PASS`、`delivery_allowed=true`、`override=false`。

> 即：该次流程**没有**把终审后已变化的可变工作区发布出去。这是安全收益。正确简化是"从不可变候选包发布"，错误简化是"忽略漂移、复用旧审计结果"。

**r549 与 r559 给出一个报告没写、但直接支持其 §G.4 的实测事实：**

```
r549  FINAL_SNAPSHOT_CREATED   input_fingerprint = 7068d832aaf258b1300a36cd1b946e48614328cde86360a17df4603f265731a7
r556  FINAL_AUDIT_CONTRACT_RETRY_PREPARED
r559  FINAL_SNAPSHOT_CREATED   input_fingerprint = 7068d832aaf258b1300a36cd1b946e48614328cde86360a17df4603f265731a7   ← 完全相同
r560  STEP_SUCCEEDED
```

r549 与 r559 的**候选内容身份完全相同**，中间只发生了一次"评估合同修复后的重审"。这**实证**了报告 §G.4 的区分：*"实现修复、验收政策未变：可以对同一内容候选重新审计"* —— 内容没变就不该产生新候选，只应增加 audit attempt。报告说"不能独立认证该变更仅属于实现修复"（因缺源码差分）是诚实的；但**候选内容身份未变**这一点现在可以独立认证。

**r560 交付收据的完整绑定（复验）：**

```
release_id            = dc871538f9648af575e8ab3132b5676db8714114c1342a23d264d30e2ec7642b
audit_snapshot        = dc871538…（同一个值）
audit_status          = PASS
final_decision        = PASS
final_input_fingerprint = 7068d832aaf258b1300a36cd1b946e48614328cde86360a17df4603f265731a7
final_input_manifest  = .factory/finalization/input_manifest.json
gate2_delivery_override = false
published_pdf         = papers/releases/cumcm_2026_a_fable_pro_20260910/dc871538…/paper.pdf
```

审计文件 `.factory/audits/dc871538…/attempts/20260912T072224.958288Z.json`：`status=PASS`、`delivery_allowed=true`、`override=false`、`judge_completed=true`、`judge_verdict=PASS`、`final_acceptance_receipt=judge_outputs/final_acceptance_receipt.json`。

> **一个必须写清的身份区分（报告尚未明确）：** r560 里存在**两个不同的身份值** ——
> `final_input_fingerprint`（≈ 候选内容身份，r549/r559/r560 三者相同）与 `audit_snapshot`（= `release_id`）。
> 修订版候选合同必须把这两个层次区分开，否则"发布对象与验收对象一致"会被误做成"两个哈希必须相等"。

`FINALIZATION_ABORTED_SNAPSHOT_CHANGED` 的正确样本在 **B / r1399**（`V-09` 成立）：

```
r1399  type=FINALIZATION_ABORTED_SNAPSHOT_CHANGED  schema_version="factory-finalization-abort-v1"
       changed_paths = ["<approval-receipts>"]
       resume_after_step = 3
       reason.code = FINALIZATION_ABORTED_SNAPSHOT_CHANGED   reason.message = ""（空）
```

> 报告的判断成立：审批变化需要重新检查授权，但不应仅凭审批收据变化就默认需要重建模型。
> 注意 `changed_paths` 用的是**占位符** `"<approval-receipts>"` 而非具体路径 —— 这本身就是"审批收据没有独立内容身份"的线索，与 §G.2 的"审批不进入候选自身身份、但必须可检查"是一致的。

### 2.4 Completed：三个事实的拆开 —— `V-10` 全部成立

| 项目 | 数据库事实（复验） | 交叉读取（复验） | 结论 |
|---|---|---|---|
| A | `completed`，仍有 `MATH_DIRTY` owner **Stage 8**，`cause_revision=555`，`cause_artifact=judge_evaluation.md` | r560 又把 `judge_evaluation.md` 作为 generated projection 绑定进交付收据 | ✅ 报告成立 |
| A | `local_python_20260910154426_560c138e`：DB `status='running'`、`finished_at=NULL`、`owner_stage=NULL`、`owner_subtask=NULL` | `.factory/solver_jobs/local_python_20260910154426_560c138e.json` = `{"status":"completed","returncode":0,"finished_at":1789055336}` | ✅ 成立 |
| B | `local_python_20260908173110_dd1262a8`：DB `status='running'`、`finished_at=NULL`、**`owner_stage=4`、`owner_subtask='solve'`** | 同名 JSON = `{"status":"completed","returncode":0,"finished_at":1788888671}` | ✅ 成立，**且确认带 owner** |
| A | `projection_failures`：(r61, `write_compatibility_projections`, `OperationalError`, `pending`, `resolved_at=NULL`) 与 (r183, 同) | —— | ✅ 成立 |

> **报告"同类问题不只发生在无 owner 的临时作业"这一点得到精确证实**：B 的记录 `owner_stage=4`、`owner_subtask='solve'`，是正式 Stage 4 求解作业。
> 报告同时正确地区分："数据库 running 记录不等于当前仍有进程运行"（本次未做进程存活检查），"不能仅凭退出文件就跳过 completion receipt 的输入输出绑定验证"。

**9 月 20 日的工作区失败不能抹掉历史 PASS —— 精确成立：**

```
A 的 PROJECT_COMPLETED：revision 561，created_at = 2026-09-12T07:31:56Z
.factory/audits/latest.json：
    created_at   = 2026-09-20T11:47:00+00:00
    status       = FAIL
    decision     = CONTENT_FREEZE_EVIDENCE_INVALID
    error_class  = PERMANENT_CONTENT_FREEZE_RECEIPT
    snapshot_id  = ac34ed627befb767024e55e1144c140e771424cd9bc4ad773173074f785c4b5c
    snapshot_error = "alias canonical content is not complete: models/A-S-01/code/common.py"
    returncode   = 2
```

`.factory/audits/latest.json` 是**可变索引**（A 项目里它已被 9/20 的结果覆盖），而 `dc871538…/attempts/20260912T072224.958288Z.json` 是**绑定候选 ID 的不可变收据**。两者必须同时保留、且语义不同。

### 2.5 三层拆分仍是正确的目标设计，但 r414 的**根因**不是「ownership 粒度过粗」

> **⚠ 本小节的归因已在后续复核中修正。** 原文把 r414 归因为「同一套 ownership 规则把三种不同变化
> 压成了同一种动作」。**读完分类器源码后确认该归因不成立。**

**已核实的根因：** `judge_evidence.json` 与 `models/reporting_scope/scope_review_manifest.json`
当时**没有任何 ownership 规则**，因而落入 `factory_core/dirty.py:415-420` 的 **fail-closed 兜底分支**——
该分支**无条件**同时产生两个 flag：

```python
else:
    # Unknown authored changes fail closed. Both flags are intentional: ...
    remember(_change(DirtyFlag.MATH, 8, artifact, before, after))
    remember(_change(DirtyFlag.RESULT, 4, artifact, before, after))
```

r414 的 `[MATH_DIRTY(8, judge_evidence.json), RESULT_DIRTY(4, judge_evidence.json)]` **正是这一对的指纹**。
所以问题不是「规则判断过粗」，而是**「根本没有规则」**。同理，r310 的 `scope_review_manifest.json` 与
r143 的部分 `MATH_DIRTY(8)` 也来自兜底，而非 Stage 归属判断。

**该洞已被部分补上：** `factory_core/current_artifact_ownership.py` 的 `ADDITIONAL_OWNERSHIP`（5 条）
已登记这两个路径（均归 Stage 10 / `FORMAT_DIRTY`），并由 v10 分类器
`current_dirty.classify_manifest_changes()` **只对已登记路径剥掉兜底**。
但 A 的 **192 个真实 artifact path 中仍有 21 个（74 行 dirty 义务）落在兜底**。

**三层拆分仍然是正确的目标设计**（报告把问题拆成三问是对的，只是它给 r414 安错了原因）：

1. **谁有权修改这个产物？** → 写入权限（writer ownership）
2. **这个产物的哪些语义发生变化？** → 变化分类（semantic role）
3. **哪些既有结果与验收结论因此失效？** → 依赖关系（dependency / invalidation）

**注意一个反例：** `@protected:*` 键（A 中有 7 条）走的是 `dirty.py:383-385` 的**专门早退分支**，
恒记 `MATH_DIRTY(8)`，**不属于兜底**。统计兜底面积时若不剔除这些合成键，会得到偏大的数字
（曾据此得到 28 个路径，剔除后为 21 个）。

### 2.6 两处我要更正报告

#### `X-01` r517 的证据位置需要更正（结论不变，但依据要换）

报告写"r517 返回 `REOPEN_REVISION_TEXT`"、B.3 写"r517 是最终审核要求文本修订"。

**复验：** r517 的 `_workflow.reason.message` 是**空字符串**，`reason.code` 是笼统的 `WORK_REOPENED`。`REOPEN_REVISION_TEXT` 出现在 **payload 顶层字段** `final_decision`：

```
r517  STEP_REOPENED   _workflow.reason = {code:"WORK_REOPENED", message:"", ...}
                      final_decision   = "REOPEN_REVISION_TEXT"
                      recovery_target  = {stage:10, subtask:"delivery", resume_after_step:11}
```

**这不削弱报告的实质结论，反而扩大报告自己的论点：** 报告在 B.3 只指出 `RESUMED`/`PAUSED` 缺少具体 `reason.message`；实际这个缺陷**同样存在于 `STEP_REOPENED`** —— 真正的决策语义落在 `_workflow` 信封之外的另一个字段里。**修订版应把"reason 结构化"从"仅控制事件"扩大到"所有可恢复事件"，并要求 `reason.code` 至少能区分 `REOPEN_REVISION_TEXT` 这类子类型。**

> 修正后的表述："r517 的终审 FAIL 以 `STEP_REOPENED` 表达，其决策语义记录在 payload 的 `final_decision` 字段（`REOPEN_REVISION_TEXT`），而不在 `_workflow.reason` 中；因此它既不在 Step 16 的 6 条 `STEP_FAILED` 内，也不能从 `reason.code` 单独判读。"

#### `X-02` Gate2 例外授权有两个代次，且性质不同

报告写"A／r528 的 Step13 checkpoint 同时具有 status=PASS、precheck_skipped=true、judge_completed=false、delivery_allowed=false、gate2_continuation_override=true"。**r528 完全吻合。**

但 `stage_checkpoint_history` 里**不止一次**这种记录：

| checkpoint | completed_revision | status | precheck_skipped | judge_completed | judge_verdict | delivery_allowed | gate2 字段 |
|---|---|---|---|---|---|---|---|
| `1bd6a7a6…` | 397 | PASS | true | false | `INDETERMINATE_REVIEW` | false | `gate2_delivery_override=true`（**含交付豁免**）+ `gate2_continuation_override=true` |
| `6dc76c05…` | 528 | PASS | true | false | `PASS` | false | 仅 `gate2_continuation_override=true` |

> **这比报告的观察更精确：** r397 同时存在 `gate2_delivery_override` 与 `gate2_continuation_override`，r528 只有后者。即"是否允许继续推进"与"是否放宽交付"是**两个独立开关**，且在冻结第 3 代前后发生了收缩（r528 收回了交付豁免）。
> 报告的结论"例外继续授权必须单独建模，不能统一压成一个 PASS 布尔值"成立，并且**应当区分 `continuation` 与 `delivery` 两个豁免维度**，而不是一个。

### 2.7 一处我要强化报告：prompt 过绑定不再是"待证明"，已被证据证明

报告 C.6 写：

> "这是绑定范围可能过宽的直接线索……**但是否已经产生这种误触发，仍需完整 composer 和配置解析入口证明。**"

本次复核**取得了该证明**，分三步：

**第一步 — 机制（源码）：** `factory_core/effective_prompt.py:55-85`

```python
def model_config_identity(factory_root, project_id, step_id):
    records = [
        _file_identity(web / "model_config.json", factory_root),        # 整文件哈希
        _file_identity(web / "model_registry.json", factory_root),      # 整文件哈希
        _file_identity(factory_root / "factory_core/adapters/models/dispatcher.py", ...),
        _file_identity(factory_root / "scripts/model_dispatch_config.py", ...),
    ]
    assignment = get_step_model_ids(web / "model_config.json", project_id, step_id)  # 按 (项目, step) 解析
    identity = {"project_id":…, "step_id":…, "resolved_assignment":list(assignment or ()), "records":records}
    return canonical_hash(identity), identity
```

即**同一个 `model_config_sha256` 里同时含"整文件身份"与"该 step 的有效选择"**，报告所引的 4 个文件路径逐字对上。

**第二步 — 该文件是跨项目共享的单文件：** `web/model_config.json` 顶层键是 **8 个项目名**（`cumcm_2025_b`、`cumcm_2025_a_v2`、`cumcm_2025_a_current_pass`、`_default`、`cumcm_2025_a_rerun_0706`、`cumcm_2020_a_codex_luna`、`cumcm_2025_b_gpt_formal_20260908t153023z`、`cumcm_2026_a_fable_pro_20260910`），每个项目下按 `step_N` 给 `primary`/`fallback`。**项目 A 只占其中一个 key。**

**第三步 — 在 A 的 66 份持久化 receipt 上直接统计：**

| 量 | 不同取值数 |
|---|---|
| `web/model_config.json` **整文件** sha256 | **11** |
| `resolved_assignment`（真正影响该 step 的量） | **2** —— `[]` 与 `['codex','']` |
| `web/model_registry.json` sha256 | 1（恒定） |
| `factory_core/adapters/models/dispatcher.py` sha256 | 1（恒定） |
| `scripts/model_dispatch_config.py` sha256 | 1（恒定） |

且**按 step 分组后，每个 step 的 `resolved_assignment` 都只有 1 个取值**（step 12 的 2 个里有一个是空 `[]`）。

两次 `model_config_sha256` 漂移事件的明细：

| 事件 | step | 该 step 的 `resolved_assignment` | `model_config.json` 整文件哈希变化 |
|---|---|---|---|
| r331 | step 7 | 全程 `['codex','']` | `443359e8…` → `48c865b2…` → `08b98bcb…` → `e56e4f71…`（4 个值） |
| r249 | step 12 | 除一次空 `[]` 外全程 `['codex','']` | `08b98bcb…` → `e56e4f71…` |

两次事件的 `prompt_input_errors` 分别为（r249、r331 相同）：

```
["effective prompt input drift: researcher_note_sha256",
 "effective prompt input drift: model_config_sha256",
 "effective prompt input drift: effective_prompt_sha256",
 "effective prompt input drift: prompt_inputs_sha256"]   →  decision = "retry_incomplete_step"
```

> **`S-02` 结论：** 在 r249 / r331 两次恢复中，**受影响 step 的有效模型选择没有变化**，而 `model_config_sha256` 发生了变化。其唯一可见来源是共享文件 `web/model_config.json` 的整文件字节变化 —— 而该文件**包含另外 7 个项目的条目**。因此"别的项目的 step 配置被改动 → 本项目该 step 的有效输入被判定为漂移 → 触发 `retry_incomplete_step`"这条路径**不是理论推测，而是已由持久化 receipt 证明的过绑定**。
> **修订版要求：** `model_config_identity` 的语义身份应以 `(project_id, step_id, resolved_assignment, 该 step 实际消费的模型条目)` 为边界；整文件哈希降级为**审计上下文**（记入 receipt，但不参与漂移判定）。报告"不能把被送入模型、改变任务约束、改变工具权限或改变证据解释的信息移出有效输入"的限定仍然成立。

**其余 C.6 数据全部吻合（`V-11`）：** A 的 19 次 `RECOVERY_DECIDED` 精确分解为 5 + 8 + 1 + 5：

| 类别 | 条数 | 复核证据 |
|---|---|---|
| 已有有效产物，恢复晋级 | **5** | `decision="promote_valid_stage_subtask"`（r18、r57、r164、r349、r379） |
| effective prompt input changed | **8** | r93、r109、r125、r184、r195、r249、r259、r331 |
| 持久化 prompt receipt 缺失 | **1** | `"persistent effective prompt input receipt is missing"` |
| 合同/检查问题 | **5** | `"fewer than two validated modeling streams"`×1、`"Step 16 delivery contract invalid"`×2、`"Step 13 judge verdict missing or invalid"`×1、`TRANSIENT_INCREMENTAL_AUDIT`×1 |

并且 **8 次 prompt 漂移全部包含 `researcher_note_sha256`** ✅，**只有 r249 / r331 另外包含 `model_config_sha256`** ✅。

报告引用的 `r28` "少于两个经过验证的建模流"也确认存在 ✅。

### 2.8 numbers manifest：机制比报告描述的更硬 —— `V-07` + `S-03`

**字节数精确成立：**

```
numbers_manifest.json = 1,247,685,597 bytes
claim_registry.json   = 23,669 bytes
```

**报告的前缀清单 —— 一项不可复现（`X-03`）：**

前 16,000 字节中逐项计数：

| 报告声称的前缀内容 | 实测 |
|---|---|
| `runtime_numeric_display_tokens` | 出现 12 次 ✅ |
| `started_at` | 出现 1 次 ✅ |
| `commands[].seconds` | 存在（见下）✅ |
| `commands[].exit_code` | 存在（见下）✅ |
| 工作簿比较计数与差异样例 | `workbook` 出现 **57** 次 ✅ |
| **"网格节点数量"** | **`grid` 出现 0 次** ❌ |

**`S-03` 报告描述的机制不准确，实测机制更严重。** `"commands"` 作为**独立 JSON key 出现 0 次**。真实结构是：

```json
{
  "generated_by": "scripts/verify_numbers.py",
  "step": "Step 10 Gate 1",
  "sources": {
    "results/validation_supplement_20260912/report.json": {
      "boundary_ranges.air_moisture_min": {"value": 0.01963, "checksum": "20b6ab6a", "type": "float"},
      "validation.Q4.runtime_numeric_display_tokens.scipy[1]": {"value": 1.0, "checksum": "0a1794b6", "type": "float"}
    },
    "results/A-S-01-server/verification.json": {
      "started_at":            {"value": 1789053123.880562, "checksum": "049de41e", "type": "float"},
      "commands[0].seconds":   {"value": 40.55244183540344, "checksum": "5816e382", "type": "float"},
      "commands[0].exit_code": {"value": 0, "checksum": "cfcd2084", "type": "int"},
      "commands[1].seconds":   { … }
    }
  }
}
```

> **即：整份文件是一个 `来源文件 → 点号路径数字 → {value, checksum, type}` 的扁平登记表。运行元数据（`started_at`、命令耗时、退出码）与科学数值处在同一个 key 空间、同一种记录结构里，没有层级区分。** 这比报告"包含运行元数据数字"的描述更能说明为什么职责必须拆分 —— 问题不是"混进来了"，而是**它们与科学主张在数据结构上不可区分**。

**报告关于 dirty 的限定成立，并可收紧（`V-12`）：**

| 报告结论 | 复核 |
|---|---|
| A 的 `dirty_causes` 中精确路径 `numbers_manifest.json` 为 0 条 | ✅ 成立（`%numbers_manifest%` 也为 0） |
| 0 条不能证明分类器从不读取它 | ✅ 成立，且可**收紧为正面证据**：分类器**确实**在处理 manifest 形状路径 —— A 的 `dirty_causes` 中出现 `models/reporting_scope/scope_review_manifest.json` 与 `paper/appendix_sources/pro01/manifest.json` |
| 是否进入 final audit / candidate fingerprint 的完整消费链，证据不足 | ✅ 保持为 `U-03` |

A 的 `dirty_causes` 总量为 491 条，`judge_evaluation.md` 作为 cause 出现在 r481/r494/r550/r551/r555（`MATH_DIRTY`, owner 8），另有 r560 的 `FORMAT_DIRTY(10, judge_evaluation.md)` —— 与当前 `dirty_flags` 中 `cause_revision=555` 一致。

**关于 claim registry：** 报告建议"优先扩展现有 `claim_registry.json` 而不是新建竞争权威"，复核支持 —— 该文件确实存在且已达 23,669 字节（含 questions / claims / required roles / artifact 绑定，未做结构级审阅）。**但在扩展前必须先确认它与 manifest 的消费关系**（`U-03` 未闭合）。

### 2.9 一个小而有用的补充：release 目录把 manifest 膨胀带进了交付物

A 的两个 release 目录内容一致（各 8 项）：

| 文件 | dc871538… | 7bb15f… |
|---|---|---|
| `paper.pdf` | 997,662 B | 997,674 B |
| `submission.zip` | **1,298,229,369 B** | 1,298,229,414 B |
| `approval_content_freeze_*.json` | 1,845 B | 1,845 B |
| `audit_result.json` | 807 B | 807 B |
| `audit_snapshot.json` | 441,126 B | 440,658 B |
| `bibliography_build_evidence.json` | 761 B | 761 B |
| `delivery_manifest.json` | 1,973 B | 1,973 B |
| `final_audit_receipt.json` | 2,450 B | 2,450 B |

> `submission.zip` ≈ 1.298 GB —— 数量级与 1.25 GB 的 `numbers_manifest.json` 一致。**这说明 numbers manifest 的成本不止是"生成慢"，而是直接进入了对外交付包的体积。** 报告没有提出这一点，但它显著加强了 §M5 的优先级。
> **限定：** 本次未打开 ZIP 核对内部清单（`U-04`），因此这是体积相关性证据，不是包内容证明。

---

## 3. 对报告 D 节七项方案的逐条响应

以下是我**独立给出**的判定（与报告结论大体一致，但在依据与风险上更具体）。"报告原判"列仅用于对照。

| # | 方案 | 报告原判 | 我的判定 | 复验依据 / 必须修改的地方 |
|---|---|---|---|---|
| 1 | `advance_until_blocked`（连续 runner） | 修改后实施 | **修改后实施**，但**不是第一优先** | 依据 `V-04`（61/61 全为 max_steps）。**必须先做 M0 的完成态对账与原因结构化**，否则连续 runner 会把 `RUN_STOPPED`/`RESUMED`/`PAUSED` 现有的"原因不可细分"问题放大成"恢复原因不可追溯"。必须保留：授权范围（r423/r519 的 `allowed_source_steps=[16]`）、human gate、solver wait、deadline、人工暂停 |
| 2 | dependency invalidation DAG | 修改后实施 | **修改后实施**，**第一步是"停止捷径"而不是"建 DAG"** | 依据 2.2 / 2.5。**最小改动**：让"观察/证据文件变化"不再默认产生 `RESULT_DIRTY` + 计算回退（r414）。写入权限、变化分类、失效依赖三段分开。先 shadow 对照，不直接清掉旧 dirty |
| 3 | immutable final candidate | 修改后实施 | **修改后实施** | 依据 2.3 `V-06`。收敛已有 `FINAL_SNAPSHOT_CREATED` / `input_manifest` / `release_id`，**并先写清 `final_input_fingerprint` 与 `audit_snapshot` 是两个层次**（报告未明确）。区分 delivery plan 与 delivery receipt（报告 C.3 的"不能把 missing delivery manifest 当作根因"成立） |
| 4 | first-class execution modes | 修改后实施 | **修改后实施** | 模式应产出**任务计划 + 权限边界**，不复制四套 FSM。复用必须保留来源与证据强度（报告的限定正确）。**A 上已经存在这个问题**：`r28` 的 "fewer than two validated modeling streams" 出现在复用项目里 |
| 5 | numbers manifest 分层／分片 | 建议实施 | **建议实施，且优先级应提到前面** | 依据 2.8 `S-03` + 2.9：科学数值与运行元数据在同一 key 空间不可区分；manifest 体积已进入交付包（`submission.zip` ≈1.298 GB）。先扩展现有 `claim_registry.json`，但**必须先闭合 `U-03`** |
| 6 | semantic / execution-context hash | 修改后实施 | **修改后实施 —— 本项已有最硬的证据** | 依据 2.7 `S-02`：过绑定已由 receipt 数据证明，不是推断。修复边界明确：`(project_id, step_id, resolved_assignment, 实际消费的模型条目)`。研究者指令、有效模型配置、审计政策**不能**移出语义身份 |
| 7 | 四 Phase 外部投影 | 建议实施（仅外部） | **建议实施，且可以最先做** | 依据 §4.1：四 Phase 可由既有坐标纯投影得到，不需要新 cursor，不触碰内部状态机。风险最低、见效最快 |

**同意报告"不建议加入的额外复杂度"清单，并补充一条：**

- ❌ 四套模式各一套状态机
- ❌ 强制 Parquet
- ❌ 默认 Merkle tree
- ❌ 第二套候选状态权威
- ❌ 为清理历史而重写旧事件
- ❌ **（补充）在未闭合 `U-03` 前，把 numbers manifest 拆成新权威** —— 会同时产生两套数字权威，且无法判定哪套进入了 final audit

---

## 4. 修订版目标状态机与候选合同

### 4.1 保留既有内部坐标 —— `V-13` 精确成立

A 的 `stage_checkpoints` 当前 **19 行**（"当前选择表"），映射与报告 E.1 **逐行一致**：

| Stage | subtask | `source_step_id` | 已完成 revision |
|---|---|---|---|
| 1 `UNDERSTAND` | `problem_setup` | 0 | 56 |
| 1 | `research_and_viability` | 1 | 61 |
| 2 `MODEL_TOURNAMENT` | `parallel_model_proposals` | 2 | 65 |
| 2 | `method_selection` | 3 | 71 |
| 3 `MODEL_CONTRACT` | `model_construction` | 4 | 314 |
| 4 `SOLVE` | `solve` | 5 | 319 |
| 5 `VALIDATE_MODEL` | `sensitivity` | 6 | 324 |
| 5 | `model_evaluation` | 7 | 335 |
| 6 `REVIEWER_ENTRY` | `visualization` | 8 | 340 |
| 6 | `reviewer_entry_gate` | 8 | 348 |
| 7 `DRAFT_AND_AUDIT` | `paper_draft` | 9 | 353 |
| 7 | `numerical_gate` | 10 | 358 |
| 8 `REVIEW_AND_REVISE` | `constructive_review` | 11 | 388 |
| 8 | `revision` | 12 | 524 |
| 8 | `conditional_math_preflight` | 13 | 528 |
| 9 `FINAL_PROSE` | `abstract` | 14 | 533 |
| 9 | `polish` | 15 | 538 |
| 10 `FINALIZE` | `content_freeze_guard` | 16 | 545 |
| 10 | `delivery` | 16 | 560 |

四 Phase 可由既有坐标**纯投影**得到，**不新增可与 Stage cursor 独立写入的第二个 cursor**：

```
DISCOVER          ← Stage 1–2
BUILD             ← Stage 3–5
WRITE_AND_VERIFY  ← Stage 6–8
FINALIZE          ← Stage 9–10
```

**报告对 `stage_checkpoints` 性质的判断需要保留并写清：** 这是一张"当前选择"表（`PRIMARY KEY(stage_id, subtask)`，可被覆盖）；真正不可变的是 `stage_checkpoint_history`（A 有 65 条）。**修订版不应因为名字里有 "checkpoint" 就假定 `stage_checkpoints` 每一行不可变**，也不应把历史收据的保护需求错加到这张表上。

### 4.2 生命周期与模式正交

建议只有**一套**运行生命周期，等待原因放进 `blocked_reason`：

```
顶层状态：READY / RUNNING / BLOCKED / REPAIR_REQUIRED / COMPLETED / CANCELLED
blocked_reason：HUMAN_DECISION / SOLVER_RESULT / RETRY_BACKOFF /
                EXECUTION_SCOPE_BOUNDARY / FREEZE_BOUNDARY / NEEDS_INTERVENTION
```

依据：`r423`/`r519` 的 `allowed_source_steps=[16]` + `state_patch.status="paused"` 证明 **`EXECUTION_SCOPE_BOUNDARY` 是真实存在的等待原因**，当前被压进 `RUN_BOUNDARY_REACHED` + `paused` 里；它既不是失败也不是普通暂停。

`fresh / reuse / repair / finalization_only` 是**计划与权限配置**，不是四套生命周期。

### 4.3 runner 的持久化单位是 subtask，不是 run

```
长时间计算 / LLM 调用：在数据库事务之外
产物写入：不可变、可校验的位置

单个数据库事务：
    校验 expected_revision 与有效执行权
    校验输入身份仍然匹配
    写入完成事件
    写入/引用不可变 checkpoint receipt
    更新 cursor 与依赖失效状态
    写入必要的持久化副作用意图
提交
```

现有结构（`expected_revision`、`effect_hashes_after`、`state_hash_before/after`、`stage_checkpoint_history`）**表明该结构存在**，但**提交边界与并发锁的正确性属于 `U-02`，本次不认证**。外部 solver 与文件发布必须靠持久化意图 + 幂等键 + 完成收据 + 崩溃后对账，不能靠"都在事务里"一句话解决。`local_python_2026…` 的 DB/文件终态不一致（2.4）正是这一层的现成反例。

### 4.4 完成条件（对当前待完成候选）

必须同时满足：

1. 没有 blocking invalidation
2. 没有未解决的必需 human action
3. 没有未对账的必需 solver job
4. 没有仍能改写该候选的执行者
5. `candidate_id` 已确定且候选内容完整
6. freeze approval 绑定该 candidate
7. final audit 对该 candidate 为 PASS
8. final audit **已实际完成**，而非 continuation override
9. delivery receipt 对该 candidate 有效
10. 没有未解决的发布事务／副作用

**三条禁令（复验支持）：**

- 不能要求"历史所有失败作业都变成成功"（R 有 26 条 `SOLVER_JOB_FAILED`，B 有 23 条，均为历史事实）。
- 不能用删除 `judge_evaluation.md` 的 dirty 记录来冒充不变量成立（A 当前 `MATH_DIRTY(owner 8, cause r555)` 是真实义务）。
- 不能用 continuation override 冒充"审核已通过"（依据 `X-02`：`judge_completed=false` 而 `gate2_continuation_override=true` 的记录确实存在）。

### 4.5 Final Candidate 合同 —— 与现有 snapshot/release 收敛

```json
{
  "identity_schema": "factory-candidate-v1",
  "project_id": "<project>",
  "candidate_id": "<sha256 of canonical identity payload>",
  "generation": 7,
  "created_revision": 600,
  "identity": {
    "canonical_result_root": "<sha256>",
    "claims_manifest_hash": "<sha256>",
    "paper_hash": "<sha256>",
    "attachment_root": "<sha256>",
    "evidence_root": "<sha256>",
    "source_manifest_hash": "<sha256>",
    "quality_contract_hash": "<sha256>",
    "scientific_scope_hash": "<sha256>",
    "delivery_plan_hash": "<sha256>"
  },
  "attestations": {
    "freeze_decision_id": null,
    "audit_receipt_id": null,
    "delivery_receipt_id": null
  }
}
```

**映射到本仓库已存在的对象（这是"收敛"，不是"新建"）：**

| 合同字段 | 本仓库现有对应物 | 复验证据 |
|---|---|---|
| 候选内容身份 | `final_input_fingerprint` + `.factory/finalization/input_manifest.json` | r549/r559/r560 = `7068d832…` |
| 冻结 | `content_freeze` decision request/instance | 6 代，请求 revision 410/446/468/485/508/541 |
| 审计 | `.factory/audits/<snapshot_id>/attempts/<ts>.json` | `dc871538…/attempts/20260912T072224.958288Z.json` |
| 交付 | `delivery_manifest.json` + release 目录 + `papers/<project>/current.json` | 2 个 release 目录，8 个文件 |
| 发布指针 | `papers/<project>/current.json` | 已存在 |

**两个必须在合同里分开、报告未明确的身份：**

- `final_input_fingerprint`（候选**内容**身份）—— r549/r559 相同 ⇒ 内容未变
- `audit_snapshot` / `release_id`（**审计—发布**身份）—— `dc871538…`

> 若把两者当成同一个值，"内容未变但评估器修复"就无法表达（会强制产生新候选，正是 §G.3 想避免的）；若把它们混成一个"发布哈希"，r550 那种"终审后内容漂移"就无法检测。

### 4.6 审计与交付如何绑定（含实测支持）

**Final Audit receipt 至少绑定：** `candidate_id`、实际读取的候选输入 root、`quality_contract_hash`、评估器实现／有效配置身份、verdict、审计**是否完成**、claim coverage、attempt identity、证据与发现。

依据 `V-06`：r549 与 r559 的候选内容身份相同而重审发生，说明"评估器代码修复"可以在**同一内容候选**上增加 attempt。而 `r556 FINAL_AUDIT_CONTRACT_RETRY_PREPARED` 就是该动作的现成事件。

**Delivery receipt 至少绑定：** `candidate_id`、freeze approval 身份、被采用的 final audit receipt 身份、实际发布文件 root、实际 ZIP/PDF hashes、发布目标与操作身份、发布完成状态。

依据 `V-06`：r550 的 `PERMANENT_ATOMIC_DELIVERY` + `"project content changed after Final Audit"` 证明当前实现**已经**在做内容漂移检查。发布应拆成"持久化发布意图 → 发布/核对不可变包 → 完成对账"。

---

## 5. 迁移计划 M0–M6（修订版）

> 与报告 H 节相比，主要变化：**M0 扩容**（加入原因结构化与 `X-01` 的修复）、**M1 最小化**（第一步只做"停捷径"）、**M5 前置条件**（先闭合 `U-03`）、**M6 提前**（可与 M0 并行）。
> **顺序原则不变：** 先确保新 runner 不会放大现有状态歧义，再上连续 runner。

| 阶段 | 组件与实施方式 | 兼容策略 | 必须测试 | 退出条件 | 回滚／停止条件 |
|---|---|---|---|---|---|
| **M0** 固定版本、观测、完成态对账、**原因结构化** | 绑定部署源码 commit；给 `pause/resume/stop/reopen` **所有可恢复事件**补 `reason.code` 子类型（`X-01`）与执行主体；加入 required-job reconciliation、候选完成检查、投影健康状态；**分离 `final_input_fingerprint` 与 `audit_snapshot` 两个身份（`V-06`）** | 只读报告／shadow，不自动改写历史完成结论 | A/B 两个作业终态不一致；A 的 `MATH_DIRTY(cause r555)`；9/20 工作区失败不得覆盖历史 release；`REOPEN_REVISION_TEXT` 必须能从 `reason` 读出 | 每个 `completed` 异常可解释、可定位；待对账与真运行可区分；两个身份层次在收据中可分别引用 | 需要靠删除失败记录或伪造收据才能"修复"时停止 |
| **M1** typed artifacts 与 invalidation shadow（**最小第一步：先把兜底面积压到 0，不是建 DAG**） | **根因是「未登记产物走 fail-closed 兜底」**（`dirty.py:415-420`），不是「依赖图缺失」。第一刀：为 A 已观测的 21 个路径补 explicit policy（`ArtifactPolicy`），使兜底只由显式 `FAIL_CLOSED` 触发；`@protected:` / `@paper:` 走专门早退分支，不属此列 | 旧 classifier 与 hash 保留（`dirty.py` / `artifact_ownership.py` **字节冻结**）；新行为全部进 `current_dirty.py` + policy 层 | r143/r310/r414 不再由兜底产生 `MATH@8 + RESULT@4`；真实模型与证据失效仍必须阻断；`r423/r519` 范围保护不受影响 | 对 A 的 192 个真实 path 重跑：`observed_policy_gap = 0` 且 `accidental_fallback = 0` | 出现 false negative、候选被错误放行、旧义务丢失 |
| **M2** 连续 runner（`advance_until_blocked`） | 明确 subtask 提交单位、有效执行权、外部作业幂等、预算与范围检查 | 新项目开关启用；在途 attempt 保持旧语义；单步模式仍可显式使用 | 崩溃切点、重复回调、重复提交、失效执行者提交、r423/r519 范围保护、human/solver wait | 无重复业务副作用；新旧调度业务终态等价；每次恢复可解释 | 越权执行、双重提交、checkpoint 与事件不一致、恢复误复用 |
| **M3** 统一候选与发布身份 | 收敛 `FINAL_SNAPSHOT_CREATED` / `input_manifest` / `release`；新增明确 delivery **plan**（区别于 receipt）；候选 preflight；终审产物放在候选外 | 旧 release ID 原样保留；不能把不同 hash 方案冒充同一身份 | r550 的终审后漂移；B/r1399 审批变化（`changed_paths=["<approval-receipts>"]`）；发布后但 DB 未完成时崩溃；工作区后续变化 | 审批、审计、发布均能追到同一候选；正式发布不依赖当前可变工作区 | 任一收据无绑定、发布字节不匹配、旧授权被误复用 → 停止发布 |
| **M4** 运行模式成为正式计划输入 | `fresh/reuse/repair/finalization_only` 生成任务计划与权限边界；明确采用既有结果的收据语义 | 老项目必须由可验证合同导入，不能靠"目录已有文件"猜测模式 | A 的已选 m1/m4 不再强制重赛；复用没有额外轨迹求解；修复超范围必须停；`r28` 的 "fewer than two validated modeling streams" 不再出现在复用路径 | 能证明任务只执行授权缺口，未伪造 fresh solve 成功 | 未经授权重算、历史结果证据强度被提升、模式转换隐含放宽权限 |
| **M5** claims 与数值数据分层（**前置：闭合 `U-03`**） | 扩展现有 `claim_registry.json`；数字证据索引与大数据分离；**先把运行元数据从科学数值的同一 key 空间分出**（`S-03`）；旧 manifest 双读对照 | 旧审计仍可定位旧格式；新候选明确采用新 schema | 必需 claim 的值、单位、来源、显示规则；完整工作簿交付检查；词法数字不冒充科学证据；`scripts/verify_numbers.py` 的实际消费者已读 | 必需 claims 覆盖无损；I/O 与生成时间得到实际测量；交付包体积回落 | 关键 claim 无来源、完整性检查被漏掉、显示结果或来源绑定变化、出现两套数字权威 |
| **M6** 四 Phase 外部投影与兼容层（**可与 M0 并行**） | UI/API 使用四 Phase；内部旧坐标保持可读；逐步停止新增不必要的旧写入形式 | 历史 event reducer／兼容 reader 保留，不重写历史 | R/B/A 固定历史 replay；跨完成再打开；Phase 与 cursor 一致 | 外部不再需要手动维护第二套流程状态 | 投影成为第二权威、旧历史无法解释、完成态或恢复目标出现歧义 |

**两种验收必须区分（报告此项正确）：**

- **固定历史事件 replay：** 应保持对应版本的重建结果与校验关系。
- **新旧 runner 对同一任务执行：** 比较业务终态、产物身份、证据与权限语义；**不要求**事件条数、时间戳、整条事件链 hash 相同。减少运行边界事件后，新运行的事件链本来就会不同。

---

## 6. 可以删除、降级与必须保留（修订）

| 类别 | 判断 | 复验依据 |
|---|---|---|
| **可以直接删除** | 本轮**没有**充分证据认定某段生产代码可无条件直接删除。尤其不能直接删 recovery、dirty、freeze 或历史事件 reader | `U-02`/`U-05` 未闭合 |
| **可在替代机制验收后移除的默认行为** | ① 所有项目强制单步退出（`V-04`）② 仅凭"较晚 Stage 改了较早 owner 文件"自动回退（2.5）③ 把重建观察报告等同科学输入改变（r414）④ **把共享 `model_config.json` 的整文件哈希计入语义身份（`S-02`）** | ③④ 为本文件新增 |
| **可以变成 compatibility layer** | 旧 Step/Stage 外部接口、旧事件名称映射、旧 manifest reader、旧 checkpoint 坐标转换 | —— |
| **可以变成 derived projection** | 四 Phase 显示、Markdown 进度、`latest.json`、当前 checkpoint 选择视图、报告展示状态 | `latest.json` 已被 9/20 覆盖，证明它只能是索引 |
| **必须保留** | 事件审计链、历史 receipt、输入输出身份、求解来源绑定、授权范围（`allowed_source_steps`）、依赖失效、最终验收、发布对账与历史重建、**两个身份层次（内容身份 vs 审计—发布身份）** | `V-06` |

**关于 `stage_checkpoints`：** 它是"当前选择"表，不应仅因为名字叫 checkpoint 就假定每一行不可变。真正需要保护的是 `stage_checkpoint_history` 里不可变的历史完成证据及其引用关系。

**关于 `latest.json`：** 可以继续存在，但只能是索引，不能替代绑定候选 ID 的历史审计收据（依据 2.4 的覆盖事实）。

---

## 7. 最小目标：我最建议先交付的三个可验证结果

不是"删掉几个 Stage"，而是：

1. **`judge_evaluation.md`、`judge_evidence.json` 等观察／证据文件的变化，不再未经合同分析就触发模型或计算重跑。**
   *可验证：* r143 / r310 / r414 三个历史样本在新的分类器下不再自动选择 solve；同时真实模型与证据失效仍必须阻断。
   *复验基础：* 2.2、2.5。

2. **一个成功交付版本能够独立回答"发布了哪个候选、谁批准、哪个审计通过、实际发布了哪些字节"，而不依赖可变的 `latest.json` 或当前工作区。**
   *可验证：* 只用 `release_id` 就能定位冻结批准、audit attempt、发布清单与 PDF/ZIP 哈希；且能在 `latest.json` 被覆盖后仍然成立。
   *复验基础：* 2.3、2.4、4.5。

3. **runner 可以连续推进，但遇到人工决策、solver 等待、授权范围、冻结边界和真实失败时，仍以明确、可恢复的方式停下。**
   *可验证：* r423 / r519 的 `allowed_source_steps=[16]` 在新 runner 下仍然阻断；`freeze` 与 `solver wait` 仍是显式等待原因；每次恢复都能从事件读出原因（含 `REOPEN_REVISION_TEXT` 这类子类型）。
   *复验基础：* 2.2、`X-01`、4.2。

**"可以简化为"：** 一个权威事件聚合、一套持续推进的调度循环、一张带版本的产物依赖图，以及一条不可变候选的审批—审计—发布链。

**内部保留旧 Stage/Step 作为历史坐标，不需要继续让它们决定每次进程启停；保留细粒度 subtask，不需要再让每一类报告变化都回到某个"大阶段"。**

---

## 附录 A：只读复核 SQL、计数表与复现命令

> 全部命令只读。`sqlite3` CLI 在本机不存在，故用 Python `sqlite3`；以 `file:<db>?mode=ro` URI 打开以杜绝写入。

### A.1 环境

```bash
cd /home/tfisher/paper_factory
python3 -c "import sqlite3; print(sqlite3.sqlite_version)"   # 3.46.1
readlink -f ongoing/cumcm_2025_b_codex_luna_stability_20260817_run4
readlink -f ongoing/cumcm_2025_b_gpt_formal_20260908t153023z
```

### A.2 项目发现（复现 17 / 6）

```python
import os, re
SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
n = 0
for root in ("ongoing", "complete"):
    for e in sorted(os.listdir(root)):
        p = os.path.join(root, e)
        if e.startswith(".") or not SAFE.match(e) or not os.path.isdir(p):
            continue
        n += 1
        print(root, e, os.path.exists(os.path.join(p, ".factory", "state.db")))
print("projects:", n)   # 17
```

### A.3 计数与状态（对应 §1.2）

```python
import sqlite3
BASE = "/home/tfisher/paper_factory/ongoing"
PROJ = {"R": "cumcm_2025_b_codex_luna_stability_20260817_run4",
        "B": "cumcm_2025_b_gpt_formal_20260908t153023z",
        "A": "cumcm_2026_a_fable_pro_20260910"}
TYPES = ["RUN_STARTED","RUN_STOPPED","STEP_STARTED","STEP_SUCCEEDED","STEP_FAILED",
         "RECOVERY_DECIDED","STAGE_SEMANTIC_REOPENED","FINAL_SNAPSHOT_CREATED",
         "PROJECT_COMPLETED","RESUMED","PAUSED","RUN_BOUNDARY_REACHED","STEP_REOPENED"]
for k, v in PROJ.items():
    c = sqlite3.connect(f"file:{BASE}/{v}/.factory/state.db?mode=ro", uri=True)
    print(k, list(c.execute(
        "select status,schema_version,runtime_generation,scheduler_generation,"
        "stage_catalog_version,revision from project_state"))[0])
    print("  events", list(c.execute("select count(*) from events"))[0][0],
          {t: list(c.execute("select count(*) from events where type=?", (t,)))[0][0] for t in TYPES})
    print("  solver", list(c.execute("select status,count(*) from solver_jobs group by status")),
          "| running:", list(c.execute(
              "select job_id,owner_stage,owner_subtask from solver_jobs where status='running'")))
    print("  dirty", list(c.execute("select flag,owner_stage,cause_revision,cause_artifact from dirty_flags")))
```

### A.4 语义回退与其 dirty cause（对应 §2.2）

```python
import json, sqlite3
c = sqlite3.connect("file:.factory/state.db?mode=ro", uri=True)   # 在 A 项目目录内
for rev, step, pj in c.execute(
        "select revision,step,payload_json from events "
        "where type='STAGE_SEMANTIC_REOPENED' order by revision"):
    w = json.loads(pj)["_workflow"]
    rt = (w.get("reason") or {}).get("recovery_target") or {}
    print(f"r{rev} subj_stage={w.get('subject_stage_id')} "
          f"subj_step={w.get('subject_source_step_id')} "
          f"resume_after={rt.get('resume_after_step')} "
          f"msg={(w.get('reason') or {}).get('message')!r}")
    print("   patch:", w.get("state_patch"))
    print("   causes:", list(c.execute(
        "select flag,owner_stage,cause_artifact from dirty_causes "
        "where cause_revision=? order by flag", (rev,))))
```

### A.5 可恢复事件的原因完备性（对应 `X-01`、§2.2）

```python
for t in ("RESUMED", "PAUSED", "STEP_REOPENED"):
    print(t, [(rev, (json.loads(pj)["_workflow"].get("reason") or {}))
              for rev, pj in c.execute(
                  "select revision,payload_json from events where type=? order by revision", (t,))])
# r517 的决策语义在 payload 顶层：
print([ (r, json.loads(p)["final_decision"])
        for r, p in c.execute("select revision,payload_json from events where revision=517") ])
```

### A.6 交付链身份（对应 §2.3、§4.5）

```python
for rev in (476, 493, 516, 549, 550, 556, 559, 560, 561):
    for r, t, pj in c.execute("select revision,type,payload_json from events where revision=?", (rev,)):
        p = json.loads(pj)
        print(r, t, {k: p[k] for k in
              ("input_fingerprint","audit_snapshot","audit_status","final_decision",
               "final_input_fingerprint","delivery_error","release_id","gate2_delivery_override")
              if k in p})
```

### A.7 Gate 与例外授权（对应 `X-02`、§2.4）

```python
print(list(c.execute("select gate_type,generation,status,requested_revision "
                     "from workflow_decision_requests order by gate_type,generation")))
print("workflow_decisions rows:", list(c.execute("select gate from workflow_decisions")))
for cid, st, sub, crev, rj in c.execute(
        "select checkpoint_id,stage_id,subtask,completed_revision,receipt_json "
        "from stage_checkpoint_history order by completed_revision"):
    if "gate2" in rj:
        r = json.loads(rj)
        print(cid[:12], "stage", st, sub, "rev", crev, r.get("status"), r.get("validation"))
```

### A.8 prompt 过绑定（对应 §2.7 `S-02`）

```python
import collections
rows = list(c.execute("select source_step_id,selected_revision,receipt_json "
                      "from prompt_attempt_inputs order by selected_revision"))
per_step = collections.defaultdict(list)
for step, rev, rj in rows:
    mi = json.loads(rj)["model_config_identity"]
    cid = {r["path"]: r["sha256"] for r in mi["records"]}["web/model_config.json"]
    per_step[step].append((rev, cid, tuple(mi["resolved_assignment"])))
for step, lst in sorted(per_step.items()):
    print(step, "receipts", len(lst),
          "distinct model_config.json", len({x[1] for x in lst}),
          "distinct resolved_assignment", len({x[2] for x in lst}))
```

### A.9 numbers manifest 结构（对应 §2.8）

```bash
stat -c '%s %n' numbers_manifest.json claim_registry.json
head -c 16000 numbers_manifest.json > /tmp/nm_prefix.txt
grep -o -e runtime_numeric_display_tokens -e started_at -e workbook -e grid /tmp/nm_prefix.txt | sort | uniq -c
grep -c '"commands"' /tmp/nm_prefix.txt     # 0：commands 是点号 key，不是独立 key
sed -n '1,600p' /tmp/nm_prefix.txt | grep -n -e 'commands\[' -e started_at -e exit_code
```

### A.10 作业终态不一致（对应 §2.4）

```bash
python3 -c "
import sqlite3; c=sqlite3.connect('file:.factory/state.db?mode=ro',uri=True)
print(list(c.execute(\"select job_id,job_revision,owner_stage,owner_subtask,status,finished_at \"
                     \"from solver_jobs where status='running'\")))"
cat .factory/solver_jobs/local_python_20260910154426_560c138e.json
cat .factory/solver_jobs/local_python_20260908173110_dd1262a8.json
```

### A.11 审计索引 vs 不可变收据（对应 §2.4）

```bash
cat .factory/audits/latest.json
cat .factory/audits/dc871538f9648af575e8ab3132b5676db8714114c1342a23d264d30e2ec7642b/attempts/20260912T072224.958288Z.json
cat .factory/audits/7bb15f928f1279ff17b17d9c0447155559da71c046c9d60b0679b1a6d78fbc28/attempts/*.json
```

### A.12 计数表（本次复验值，供 diff）

| 项目 | status | revision | events | solver_total | solver_completed | solver_failed | solver_running | dirty |
|---|---|---|---|---|---|---|---|---|
| R | failed | 2484 | 2484 | 361 | 335 | 26 | 0 | 3 |
| B | completed | 1411 | 1411 | 202 | 178 | 23 | 1 | 1 |
| A | completed | 561 | 561 | 14 | 8 | 5 | 1 | 1 |

（与上一轮报告完全一致；本次复核结束时再次查询，三个 revision 仍为 2484 / 1411 / 561。）

---

## 附录 B：本次未验证项

| 编号 | 未验证内容 | 为什么重要 | 需要什么才能闭合 |
|---|---|---|---|
| `U-01` | 部署引擎的确定 commit | 插件版本 ≠ 引擎版本；仓库当前 `HEAD` 有未提交改动 | 部署清单 / 镜像摘要 / 服务单元中的版本钉住 |
| `U-02` | runner 的事务边界与执行权入口实现 | 事务原子性、完整 replay 的结论都依赖它 | `factory_core/engine.py` 与 storage 的提交入口源码 + 并发测试 |
| `U-03` | `scripts/verify_numbers.py` 及其消费者 | 决定 numbers manifest 是否进入 final audit / candidate fingerprint 链 | 生成器与全部消费者源码；claim_registry 与 manifest 的关系 |
| `U-04` | 发布原件的字节闭包 | release 目录内 PDF/ZIP/manifest 的实际内容与哈希未闭包核对 | 读取发布文件字节 + 对 ZIP 内部清单校验 |
| `U-05` | 分类器的完整依赖声明 | "哪些路径会触发哪类 dirty"目前只能从 `dirty_causes` 反推 | classifier 规则集与 `classifier_contract_sha256` 对应实现 |
| `U-06` | 进程存活状态 | "DB running"与"进程真在跑"未做交叉检查 | `runner_pid` / lease 与实际进程表比对 |
| `U-07` | 评估器合同修复的具体内容 | r556 的"实现修复"性质未取得源码差分 | `r556` 前后评估器实现差分 |

**本文件没有把上述任何一项写成已通过。**

---

*本文件为只读复核的产物。除本文件外，本次未修改、未提交、未推送仓库任何内容。*
