# 运行时简化 S0–S6 详细实施方案 — rev7.2（自包含完整版）

> **生成时间：** 2026-09-26
> **性质：** **自包含**实施级方案 + 0.5/0.6/**0.7**/**0.7.1**/**0.7.2** 执行记录。**本版不依赖任何早期版本。**
> 分支：`feat/runtime-simplification`（worktree `/home/tfisher/paper_factory/.worktrees/runtime-simplification`）
> 已完成：`aaea7f8`（0.6）→ `bbed84c`（0.7）→ `ed69234`（0.7.1）→ `5568d01`（0.7.2）→ `36733b8`（**S1-A**）→ `dda04ce`（**S1-C-prep**）→ **下一步 S1-D**

---

## rev7.2 修订记录（本版）

外部评审的第二轮意见（Major 1/2/3 + I2 补充 + I9）**全部处置完毕**。

| # | 修正 | 来源 |
|---|---|---|
| 1 | **§0.26 新增 0.7.2 完整记录**：物理 schema 版本与事件流代次分离；`event_replay_valid` 门禁 | Major 1 |
| 2 | **§0.26.2**：dirty-clear 内嵌 rebase 的 receipt 绑定进 transition payload；更正"唯一写入者"主张 | Major 2 |
| 3 | **§0.26.3**：`classification_contract_sha256()` 覆盖 5 个语义依赖文件（Q14 落地，**先于**任何真实项目产生 v10 cause） | Major 3 |
| 4 | **§0.26.4**：attested root 必须带 domain map | I2 补充 |
| 5 | **§0.26.5**：一个 `aggregate_valid` 拆成三个命名分量 | I9 |
| 6 | §S1.0 判据 5 纳入 0.7.2 门禁；S1 前四项待办登记 | 评审 |

**S1 启动前的四项待办（评审列为「证据不足」或后续能力）：**

| 项 | 内容 |
|---|---|
| I8-a | **只读 v9 前置校验器**：`_validate_schema()` 目前主要验版本号，一个"`schema_info=9` 但不完整"的库仍会被 DDL 迁移提升到 10 |
| I8-b | **6 个 state DB 的只读 migration / domain 漂移审计** |
| — | **真实旧版本 downgrade 回归**：用 `bde49712` 的代码作为独立解释器打开 schema-10 fixture |
| J1-10 | **bounded run contract identity**（`bounded_run_contract_sha256` / `bounded_run_id`），绑进 `RUN_STARTED` 与终止事件 |

---

## rev7.1 修订记录（本版）

外部评审对本方案的 10 项意见**全部处置完毕**；本版是处置后的状态。

| # | 修正 | 来源 |
|---|---|---|
| 1 | **§S1.3 拆出 `NON_BLOCKING_BY_POLICY`**，消除"双 False 准入条件"与"无 obligation 就 fail closed"的冲突；fail-closed 只覆盖"**声称有阻断责任却找不到**"的情形 | 评审：主张 C 仍未闭合 |
| 2 | **§0.25 新增 0.7.1 完整记录**：I7 解耦 + **被掩盖的既有缺陷**（见下）；I2 域单调性不变量；I5 入口 3 写 provenance；I6 数据库级回滚测试 | 评审 I2 / I5 / I6 / I7 |
| 3 | **§S1.4 #4 定案**：classifier identity 与 provenance identity **分开**；并加入"`policy_contract_sha256` 必须覆盖影响 provenance 判定的全部语义"的硬性条件 | 评审 I4：支持分开 |
| 4 | **§S6.2 扩到 9 项能力**（`protected_manifest` 入口+提交前各验一次；新增第 9 项 `made_progress`/`boundary_fingerprint` → `UNCHANGED_BOUNDARY`） | 评审 J1 |
| 5 | **§S6.4 成功指标改为 AST + 执行证据两层判定**，不再用字符串匹配 | 评审 J2 |
| 6 | Q14 定案；新增 Q15（0.7.1）、Q16（0.7.2） | — |

> **本版最重要的技术发现（§0.25.2）：** 移除自动 rebase 后暴露出一个**既有缺陷** ——
> `_upgrade_schema()` 里整段 **v8→v9 数据回填没有被源版本守卫**，任何迁移都会重跑它，
> 而它写入的表（`stage_checkpoint_history` 等）**本身就是 effect-hash domain**。
> 在 v9 上直接创建的项目（B、R）因此被补进 13 行，历史聚合校验 fail closed。
> **迁移末尾那条 rebase 事件一直在掩盖它**（它的哈希在所有变更之后计算）。
> 已用 `if current < 9:` 修好，迁移现在是纯 DDL。

---

## rev7 修订记录（相对上一版）

| # | 修正 | 原因 |
|---|---|---|
| 1 | **§0.17 重写**：由"三条直接 INSERT"改为**静态查询得到的写入入口表**（3 个入口 / 2 个文件）；更正"S3 退役目标"的错误归属 | 原文事实错误：`final_judge_projection.py` 与 `dirty_rebase.py` **只有 SELECT** |
| 2 | **§0.18 条件 8 限定范围**：只承诺**正常 transition 写入路径** | 原表述过宽 |
| 3 | **§0.18 schema-object 措辞**：明确"schema-object 变化仅有…"，并说明 rebase 可能另外追加 event | 避免被读成"迁移没有任何其他状态变化" |
| 4 | **§S1.4 #4 曾改为待定**（本版已由 I4 定案为"分开"） | 原决定会重新耦合两个身份 |
| 5 | 测试基线措辞："既有测试全部继续通过" | 0.7 确实改变了 schema/runtime 行为 |
| 6 | **恢复自包含**：全部章节完整展开，不依赖 rev6 | 否则外审包无法仅凭附件复核 |

---

## 0. 已验证事实

### 0.1 兜底机制的真实位置

`factory_core/dirty.py:404-420`：

```python
ownership = artifact_ownership(artifact)
if ownership is not None:
    remember(_change(DirtyFlag(ownership.dirty_flag), ownership.owner_stage, artifact, before, after))
else:
    # Unknown authored changes fail closed. Both flags are intentional:
    # the upstream result owner must re-attest, and the math preflight
    # cannot be skipped merely because classification was uncertain.
    remember(_change(DirtyFlag.MATH, 8, artifact, before, after))
    remember(_change(DirtyFlag.RESULT, 4, artifact, before, after))
```

`MATH_DIRTY(8) + RESULT_DIRTY(4)` 是"该产物没有任何 ownership 规则"的指纹。r414 的
`[MATH_DIRTY(8, judge_evidence.json), RESULT_DIRTY(4, judge_evidence.json)]` 正是这一对。

不走兜底的专门早退分支（`dirty.py:382-396`）：

| 前缀 | 结果 |
|---|---|
| `@protected:` | 恒 `MATH_DIRTY(8)`（`:383-385`），cause 记为完整键 |
| `@paper:<rel>:math` | `MATH_DIRTY(8)`（`:386-389`），cause 记为 `<rel>` |
| `@paper:<rel>:citation/prose/format` | `CITATION/PROSE/FORMAT_DIRTY(9)`（`:390-395`），cause 记为 `<rel>` |

另有 `dirty.py:397-403`：`.tex` 且存在 `@paper:<path>:<domain>` 键时，裸路径被转入
`paper_raw_changes`；`:422-425` 在"没有任何 `@paper:<path>:*` 键发生变化"时额外补 `FORMAT_DIRTY(9)`。

### 0.2 修复已存在，分了两代

| 模块 | 角色 |
|---|---|
| `artifact_ownership.py` | 冻结 v1 信任根（`factory-artifact-ownership-v1`，registry `:28-280`，**83 条**），被 `classifier_contract_sha256()` 哈希（`dirty.py:141-150`），**不得改字节** |
| `current_artifact_ownership.py` | 增量表（`factory-native-artifact-ownership-v3`，**5 条**，`:19-29`），已含 `judge_evidence.json` → Stage10/`FORMAT_DIRTY`、`models/reporting_scope/scope_review_manifest.json` → Stage10/`FORMAT_DIRTY` |
| `current_dirty.py` | v10 分类器；叠加增量表，**只对已登记路径剥掉兜底**（`:29-36`） |
| 运行入口 | `engine.py:13` 用 `current_dirty`；`submission_bundle.py:11`、`finalization.py:11` 用 `current_artifact_ownership` |

`artifact_ownership.py:24-27` 明写该表是 *"the single ownership table used for dirty routing,
finalization recovery, Judge missing-evidence routing, diagnostics, and final/submission input
collection"* —— **这正是 S1 要拆开的那一团概念。**

### 0.3 A 项目的量化面积

| 量 | 值 |
|---|---|
| `dirty_causes` 行数 | 491 |
| 合成 key 行（`@protected:`） | 7（有意分支，**非**兜底） |
| distinct `cause_artifact`（全部） | 199 |
| distinct 合成 key | 7 |
| **distinct 真实 artifact path** | **192** ← S1 判据的分母 |
| 有冻结规则的行 | 400 |
| 已被 v10 补上的行 | 10（`judge_evidence.json`） |
| **仍走兜底** | **74 行 / 21 个 distinct 路径** |

兜底行 flag 分布：`MATH_DIRTY(8)` 34、`RESULT_DIRTY(4)` 31、`CITATION/FORMAT/PROSE_DIRTY(9)` 9。

> **统计口径警告：** 若不剔除 `@protected:*` 合成键，会得到 28 个路径（偏大）；剔除后为 **21**。

### 0.4 `max_steps` 默认已是 `None`，tracked source 中无调用者传 1

| 位置 | 事实 |
|---|---|
| `engine.py:88` | `def run(self, *, max_steps: int | None = None, ...)` |
| `engine.py:183-186` | `completed_this_run = 0` → `while True:` ← **连续循环已存在** |
| `engine.py:198-210` | 唯一的切片开关 → `RUN_STOPPED` `payload={"reason": "max_steps"}` |
| `engine.py:211-215` | `allowed_source_steps` → `RUN_BOUNDARY_REACHED` + `status=PAUSED` ← **真正的授权边界** |
| `service.py:396,405` | `max_steps: int | None = None` → `engine.run(max_steps=max_steps)` |
| `cli.py:188` | `run.add_argument("--max-steps", type=int)` —— **无 default** |
| `cli.py:394` | 唯一把 `max_steps` 从外部传入的位置 |

**范围限定：** **GitHub tracked source 中**三处默认均为 `None`，且 tracked caller 中
**没有**固定传 `1` 的调用者。**项目目录内未跟踪的 `work/*.py` 不属于本断言范围** —— 那里有 24 处
`run(max_steps=1…)`，见 §0.19。

### 0.5 solver 证据已经是 fail-closed 的

`cli.py:57-92` `solver_evidence_payload()`（docstring: *"failing closed for old jobs"*）在缺双阶段
receipt 时返回 `receipt_ready: False`、`claim_limit: "LEGACY_JOB_METADATA_ONLY"`、
`errors: ["MISSING_OR_INVALID_TWO_STAGE_RECEIPT: …"]`。**S5 复用此语义，不新造状态。**

### 0.6 derived artifact 一致性检查已存在

`audit/incremental.py:424-431` → `scripts/verify_derived_artifacts.py` →
`derived_artifacts_verification.latest.json`；`scripts/submission_fingerprint.py:156`、
`scripts/quality_contract.py:207` 亦引用。**S1.4 复用。**

### 0.7 历史 classifier 契约身份 **21 个**（S2 暂缓依据）

| 项目 | `dirty_causes` 行 | distinct classifier sha |
|---|---|---|
| A 2026A | 491 | 3 |
| B 2025B formal | 1274 | 5 |
| R run4 | 2090 | 14 |
| 2025b stability | 201 | 1 |
| run3 | 7 | 1 |
| 2020A | 0 | 0 |

**并集 = 22 − 1（`1b42fe46…` 在 A 与 R 共享）= 21**，且**无一等于今日 frozen 或 current**。

### 0.8 S0 实测（基线）

| 项 | 值 |
|---|---|
| 远端 `main` | `bde49712ec0764c5ace31f17a7efb538440d536d` |
| S0 时本地 `HEAD` | `a3322367`（`feat/mcp-readonly-server`，**未推送**） |
| 关系 | main 是**严格祖先**，ahead 13 / behind 0，**零分歧** |
| S0 时 `SCHEMA_VERSION` | 9 |
| frozen / current classifier sha | `c451a9d0…` / `22d365b0…` |
| feature 分支工作树测试基线 | `9 failed, 1938 passed, 2 errors`（环境导致 + 1 条 feature 引入的 secret-scan 失败） |
| main 树 secret 扫描 | 858 文件 → 0 findings |

> S0 的测试基线是 **feature 分支专属**，不得跨分支套用。

### 0.9 effect-hash 兼容机制

`storage.py:1192-1231` 对每个 domain 做 `canonical_hash([dict(row) for row in rows])`；
`dirty_causes` 用 `SELECT *`。`status_snapshot()`（`storage.py:1462-1467`）：

```python
valid = (all(effects.get(k) == v for k, v in prior.items())
         if isinstance(prior, dict) and set(prior) != set(effects)   # 仅在域 key 集合不同时宽容
         else canonical_hash(effects) == expected["aggregate_root_hash_after"])
```

→ 给既有表**加列**会破坏历史 `aggregate_valid`（key 集合不变 → 严格比较）；
**新增 domain key** 不会（触发宽容分支）。

### 0.10 `iter_owned_artifacts` 的直接消费者是 **3 个**

`current_artifact_ownership.py:81-82` 对 `owner is None` 静默 `continue`：

| # | 位置 | 用途 |
|---|---|---|
| 1 | `finalization.py:57` | `final_input_only=True` → 收集 final input |
| 2 | `submission_bundle.py:141` | `submission_only=True` → 收集提交包成员 |
| 3 | `scripts/cleanup_project_artifacts.py:166` | `final_input_only=True` → 构建清理**受保护集合** |

cleanup 另外调用 `solver_declared_input_coverage()` 与 `submission_bundle_paths()` ——
这是**另外两种独立保护来源**，不属于该迭代器的消费者。

删除候选门槛仅 `{("data","intermediate"),("analysis","intermediate"),("replication","intermediate")}`
（`cleanup_policy.py:28-34`），21 条路径无一落入 → **机制性风险，非实际风险**。

### 0.11 ownership API 全量调用面 = **13 个调用点 / 9 个模块**

| # | 模块:行 | 调用 |
|---|---|---|
| 1 | `dirty.py:404` | `artifact_ownership`（**冻结模块**） |
| 2 | `finalization.py:57` | `iter_owned_artifacts` |
| 3 | `finalization.py:167` | `reopen_after_step_for_artifact` |
| 4 | `submission_bundle.py:141` | `iter_owned_artifacts` |
| 5 | `submission_bundle.py:168` | `artifact_ownership(...) is None`（**覆盖闸门**） |
| 6 | `submission_bundle.py:190` | 同（**覆盖闸门**） |
| 7 | `submission_bundle.py:217` | `artifact_ownership` |
| 8 | `steps/validators.py:509` | `reopen_after_step_for_artifact`（validator → 恢复目标） |
| 9 | `final_evidence_recovery.py:51` | `artifact_ownership` |
| 10 | `dirty_rebase.py:183` | `artifact_ownership` |
| 11 | `scripts/claim_graph.py:602` | `artifact_ownership` |
| 12 | `scripts/claim_graph.py:614` | `reopen_after_step_for_artifact` |
| 13 | `web/backend/diagnostics_service.py:245` | `artifact_owner_stage`（Web 诊断） |

### 0.12 `_upgrade_schema` 的版本处理

`storage.py:403-425`：`current == SCHEMA_VERSION` 时调 `ensure_*_schema` 并返回；
`if current not in {1,…,8}: raise`（**改前不含 9**）。`SCHEMA_VERSION` 定义在 `factory_core/domain.py`。

### 0.13 append-only 由数据库 trigger 强制

`storage.py` 建有两套 `CREATE TRIGGER`，覆盖 `events` / `workflow_decisions` /
`workflow_decision_requests` / `workflow_decision_instances` / `dirty_flag_clear_receipts` /
`stage_checkpoint_history` / **`dirty_causes`**，每条含 `BEFORE UPDATE` 与 `BEFORE DELETE`
→ `SELECT RAISE(ABORT, '… are append-only')`。新 side table 必须有同等 trigger。

### 0.14 需回改的上游文档 —— 已完成

`RUNTIME_SIMPLIFICATION_AUDIT_RESPONSE.md` 的 §2.5 与 §5 M1 原把 r414 归因为
"ownership 粒度过粗 / 需要建 dependency invalidation DAG"。**已修正**为
"未登记产物走 fail-closed 兜底（`dirty.py:415-420`）"；三问拆分保留为**目标设计**。

### 0.15 S0/0.5/0.6 执行结果

**0.5**：从 `bde49712` 建独立 worktree `feat/runtime-simplification`；
`allowed_paths` 修正（补 `current_dirty.py`、`scripts/cleanup_project_artifacts.py`、`domain.py`、`storage.py`）；
`dirty.py` 移入 `must_not_touch`；`classifier_contract_sha256` 与 S0 锚点**逐位一致**；
3 个参考文档复制进 worktree（**未跟踪**）+ SHA-256 + 环境事实入库。

**0.6**：安装 `cloud`/`web`/`tui` extras；`web/frontend` 执行 `npm ci`；
`requests` prerequisite commit（`aaea7f8`，**仅 1 行**）。

**分支正式基线 = `1757 passed / 0 failed / 0 collection errors`。**

**两处计划未预料的环境缺口：** 新 worktree 无 `node_modules`（13 个前端测试 `ERR_MODULE_NOT_FOUND`）；
新 venv 缺 `tui` extra（3 个收集错误）。
**并且必须为 worktree 建独立 venv** —— 主树 editable 用 `MetaPathFinder`
（`__editable___modeling_factory_2_0_0_finder.py`），优先级高于 `PYTHONPATH`，
复用会导入主树的 `scripts/`（`evidence_grounding.py`、`verify_provenance.py` 在两分支间不同）。

### 0.16 S6 的真实对象：项目内手写驱动脚本层

A 项目 `work/`（服务器侧，未跟踪）：

| 量 | 值 |
|---|---|
| `work/` 条目数 / 体积 | **128** / **979 MB** |
| 调用 `.run(` 的脚本 | **52** |
| `run(max_steps=1…)` 出现次数 | **24**（21 裸用 + 3 带 `allowed_source_steps`） |
| 自带硬编码 `assert state.revision==<N>` | **20** |
| 自带 `protected_files.json` + `def verify()` | **47** |
| 自带 `progress.json` 记账 | **29** |

样本骨架：读取 state → `assert revision/status/active_step/no-runner` → 自建受保护文件哈希校验
→ 写 `progress.json` → `run(max_steps=1)` → 再校验 → 再记账。

**结论：** 病根是**引擎缺少受支持的 bounded-advance/verify 入口**，逼出一次性脚本；
不是某个 operator wrapper。这些脚本**不在 git 内**，硬编码 revision 使其**一次性且静默腐烂**。

### 0.17 `dirty_causes` 的**写入入口表**（静态全仓库扫描，非手工列举）

扫描 `factory_core` / `apps` / `scripts` / `web` / `cloud` 下全部 `.py`，
匹配 `INSERT [OR …] INTO dirty_causes`（允许换行）：

| # | 位置 | 语句 | 性质 |
|---|---|---|---|
| 1 | `factory_core/storage.py:2908` | `INSERT INTO dirty_causes` | **正常 transition 写入路径** —— **已接 provenance（0.7）** |
| 2 | `factory_core/storage.py:789` | `INSERT OR IGNORE INTO dirty_causes` | **v8→v9 迁移**中从 `dirty_flags` 重建历史 cause —— 属历史重建，无 provenance（正确） |
| 3 | `factory_core/final_evidence_recovery.py:134` | `INSERT INTO dirty_causes` | bespoke 恢复路径**新建** cause —— 无 provenance |

**`final_judge_projection.py` 与 `dirty_rebase.py` 都只有 `SELECT`，不写 `dirty_causes`。**
`dirty_rebase.py` 写的是 `dirty_flags` 与 `dirty_classifier_rebases`。

**S3 退役目标只有 `final_evidence_recovery.py`（+ `final_judge_projection.py` 的 reclassification 分支）；
`dirty_rebase.py` 不是 S3 目标。**

### 0.18【0.7 实测】Schema 10 门禁 —— Q10 = Option A

```
SCHEMA_VERSION = 10                       factory_core/domain.py
迁移接受集合 {1,...,8} -> {1,...,9}        factory_core/storage.py
9 -> 10 的 schema-object 变化仅有：
    dirty_cause_classification side table
    两个 append-only trigger
    一个 _domain_effect_hashes() 新 domain key
旧 cause 不回填  ->  legacy_unrecorded
不改变任何旧表行形状
```

> **措辞说明：** 上表是 **schema-object 层**的变化。**现有 classifier rebase 机制可能在首次打开历史
> v9 项目时另外追加一条 rebase event 并更新 mutable projection**（`dirty_flags` 等）——
> 那不是 schema-object 变化，但确实是 migration-time state mutation，见 §0.21。

**14 项退出条件全部 PASS**（逐项证据见 `runtime_simplification_stage0_7.json`）：

| 条件 | 结果 |
|---|---|
| 1 `SCHEMA_VERSION == 10` | PASS |
| 2 `_upgrade_schema()` 接受 `9→10` | PASS |
| 3 只新增 side table + 2 trigger，无其他表/触发器变化 | PASS |
| 4 新表有独立 domain key | PASS |
| 5 升级后 `aggregate_valid == True` | PASS（hermetic + 真实历史库，见 §0.21） |
| 6 `dirty_causes` 列集合不变 | PASS |
| 7 旧 cause 只读作 `legacy_unrecorded`，不反推 | PASS |
| 8 **正常 transition 写入路径**中新建的 `dirty_causes` 与 `dirty_cause_classification` **同事务** | PASS（强制 provenance 失败 → cause 一并回滚） |
| 9 UPDATE / DELETE 被 trigger 拒绝 | PASS |
| 10 downgrade barrier —— **逻辑回归 PASS** | PASS（模拟 v9 writer 接受集 `{1..8}` → 拒绝 schema 10） |
| 11 迁移幂等 | PASS |
| 12 迁移失败整体回滚 | PASS（版本回 9、表与 trigger 均不存在） |
| 附加 1 首个 v10 event 后容忍窗口关闭 | PASS（篡改 side table → `aggregate_valid=False`） |
| 附加 2 推导覆盖分类器输出的每条 change | PASS（参数化 5 类形状） |

> **条件 10 的证据强度（诚实标注）：** 这是**版本判断逻辑的回归**，不是"真的用 `bde49712` 的旧实现
> 作为独立进程去打开 schema 10 库"。真正的 binary/code-version regression 列为**待补项**（§S6 前的门禁）。

> **条件 8 的范围限定：** 本判据只覆盖 §0.17 的**入口 1（正常 transition）**。
> 入口 2 是历史重建、入口 3 是 bespoke 恢复路径，两者都不写 provenance。

### 0.19【0.7 实测】为什么必须是 A 而不是 9→9

仓库既有的 `ensure_*_schema` 证明"9→9 增表"机械可行，但新表**同时要成为
`_domain_effect_hashes()` 的新 domain**。继续标记 schema 9 会留下**降级窗口**：旧 v9 代码看到数据库仍是 9，
会认为自己有资格写入，却不知道新的 classification table 与 effect-hash domain。
采用 A 后，旧 v9 代码看到 schema 10 会依版本检查**拒绝继续** → **fail-closed downgrade barrier**。

### 0.20【0.7 实测】`classification_source` 的推导位置：在 `dirty_classification.py`，**不在** `dirty.py`

| 模块 | 状态 |
|---|---|
| `factory_core/dirty.py` | **冻结字节**（Q11）。其字节被哈希进 `classifier_contract_sha256()` |
| `factory_core/artifact_ownership.py` | 冻结字节 |
| **`factory_core/dirty_classification.py`（新增）** | side table DDL + 2 trigger + **来源推导** + 读路径 |

推导方式：**镜像分类器的分支顺序**（不改 `DirtyChange`，不给 `dirty.py` 加字段）：

```
@protected: 键                  -> protected
@paper:<rel>:<domain> 域展开    -> paper_semantic（含 paper_raw_changes 的 FORMAT@9）
命中 ADDITIONAL_OWNERSHIP       -> current_rule
命中 frozen ARTIFACT_OWNERSHIP  -> frozen_rule
无任何规则（fail-closed 双标记） -> fallback
显式构造的 fail-closed 双标记    -> explicit_fail_closed
```

六个值构成闭集 `CLASSIFICATION_SOURCES`。side table 另记 `policy_schema` 与
`policy_contract_sha256`（= `dirty_classification.py` 的 sha）。

**两个身份目前是分开的：**

| 身份 | 回答的问题 | 现状 |
|---|---|---|
| `classifier_contract_sha256()` | 这个变化最终产生**什么 `DirtyChange`** | **0.7 未改**（未触碰 `dirty.py` / `artifact_ownership.py` / `current_dirty.py` / `paper_sources.py`） |
| `policy_contract_sha256`（side table 列） | 为什么把这个 `DirtyChange` 标成 `frozen_rule` / `current_rule` / `fallback` / … | 记录 `dirty_classification.py` 的 sha |

**由此产生的审计缺口：** 同一个 `classifier_contract_sha256` 之下，provenance 的推导逻辑可以不同。
**这是刻意留待外部评审 I4 决定的设计问题，不是已定案**（见 §S1-D）。

### 0.21【0.7 实测】A/B/R 真实历史库**副本**升级结果

方法：复制各项目 `.factory/state.db` 到 `/tmp`，用 v10 代码打开副本，检查副本。**原件未动。**

| 项 | A | B | R |
|---|---|---|---|
| `schema_version` | 9 → 10 | 9 → 10 | 9 → 10 |
| `aggregate_valid` | **true** | **true** | **true** |
| `dirty_causes` 列集合 | 不变（8 列同序） | 不变 | 不变 |
| `dirty_causes` 行数 | 491 → 491 | 1274 → 1274 | 2090 → 2090 |
| side table / triggers | 存在 / 2 | 存在 / 2 | 存在 / 2 |
| provenance 行数 | 0 | 0 | 0 |
| 旧 cause 读值 | `legacy_unrecorded` | `legacy_unrecorded` | `legacy_unrecorded` |
| 最新 event 含新 domain key | 是 | 是 | 是 |
| 新增 event | **恰 1 条 rebase，revision +1** | 同 | 同 |

> **rebase event 会写**：这些项目记录的 classifier 身份早于当前身份，
> `needs_rebase = old_hash != current_classifier` 成立（`dirty_rebase.py:244`）。
> 这是 migration-time state mutation，**不是** schema-object 变化，且会使 **completed 项目的 revision 增长**。
> 其影响（历史引用是否仍有效、是否存在"只读却触发迁移"的入口）列为外部评审 **I7** 的评审对象；
> 本方案在 S6 前**不**假定它无害。

### 0.22【0.7 实测】容忍窗口的精确语义

| 时点 | 行为 |
|---|---|
| 迁移完成、**尚未写入任何 v10 event** | 旧 event 的 domain key 集合较小 → 容忍分支 → **新 domain key 不受严格校验** |
| **首个 v10 event 之后** | key 集合相同 → 严格 `canonical_hash` → **新表任何非预期变化都会使 `aggregate_valid=False`** |

这是 `status_snapshot()` 的固有性质，也正是 pre-v10 兼容所依赖的机制。
附加测试断言的是"首个 v10 event **之后**必须严格失败"。**窗口内的残余风险列为外部评审 I2。**

### 0.23【0.7 实测】测试基线演进

| 阶段 | commit | 结果 | 说明 |
|---|---|---|---|
| 0.6（分支正式基线） | `aaea7f8` | **1757 passed / 0 failed / 0 errors** | 后续门禁的**比较基准** |
| 0.7 | `bbed84c` | **1783 passed / 0 failed / 0 errors** | +26（新测试模块）；既有测试全部继续通过 |
| **0.7.1** | 见 §0.25.7 | **1788 passed / 0 failed / 0 errors** | +5（0.7.1 的测试）；**零回归** —— 尽管改了迁移（影响 40 个调用点）、`status_snapshot()`、恢复路径与 CLI |
| **0.7.2** | `5568d01` | **1793 passed / 0 failed / 0 errors** | +5；**零回归**。另有 4 处既有测试因语义变化而调整（见 §0.26.6） |
| **S1-A** | `36733b8` | **2178 passed / 0 failed / 0 errors** | +385（新模块重度参数化）；**零回归**，且**零修改既有代码** |
| **S1-C-prep** | `dda04ce` | **2424 passed / 0 failed / 0 errors** | +246；**零回归**，15/15 集合组合实测一致 |

**门禁语义：** 后续阶段只与该分支基线比较；判据是"**不新增 failed**"，绝对条数随新增测试增长。

### 0.24 S1 的四个 `⚠` 路径仍需逐条核

`m1/m4_solver_evidence.json`、`model_source_map.json`、`results_values.tex`、`input_arrays.npz`
—— **逐条读生产者与消费者，不得按名字拍 `True/False`**（Q2 授权已足够，不需再决策）。

---

### 0.25【0.7.1 实测】读路径不再改写工作流状态 —— 含一个被掩盖的既有缺陷

外部评审 I7 的 verdict 是"当前行为不建议接受，应提前到 S1-A 之前处理"。已执行，**并发现真实原因比 I7 描述的更严重**。

#### 0.25.1 I7 的直接修复：schema DDL 与 classifier rebase 解耦

`_upgrade_schema()` 被 **40 个调用点**共用，其中大量是纯读入口（`load()`、`status_snapshot()`、
`events()`、`dirty_flags()`、`stage_checkpoints()`…）。原实现会在其中执行 classifier rebase，
从而**追加事件并 ++business revision**。

**为什么不能改成"下次业务写事务里 rebase"（评审的选项 b）：** `transition()` 在
`if row["revision"] != expected_revision: raise RevisionConflict` 处强制 revision CAS。
rebase 会 +1 revision，因此把它放进 `transition()` 会让**每一个**排队中的 transition 冲突。

**采纳评审的选项 a：**
- `_upgrade_schema()` → **纯 DDL + 版本号更新**，不 rebase、不写事件、不改 revision。
- classifier rebase 成为**显式维护动作**：`SQLiteStateStore.rebase_dirty_classifier(expected_revision=…)`
  （既有、带 CAS、已有测试覆盖）+ **新增 CLI 入口 `factory rebase-classifier <project_dir> [--expected-revision N]`**
  —— 此前该方法**没有任何生产调用者**，也没有 CLI 入口，所以能力实际不可达。

#### 0.25.2 【重要】被 rebase 事件长期掩盖的既有缺陷

移除自动 rebase 后，A/B/R 副本立刻出现差异（**B、R 的 `aggregate_valid` 变为 false，A 仍为 true**）。
诊断结果：

| 项目 | 迁移后被改动的 v9 domain | 表行数变化 |
|---|---|---|
| A | 无 | — |
| B | `checkpoint_history` | `stage_checkpoint_history` 49 → 62（**+13**） |
| R | `checkpoint_history` | `stage_checkpoint_history` 77 → 90（**+13**） |

**根因：** `_upgrade_schema()` 里有一整段 **v8→v9 时代的数据回填**（`storage.py` 原 742–1077 行，
49 条顶层语句），包括：

```
INSERT OR IGNORE INTO stage_checkpoint_history   (从 stage_checkpoints 回填)
INSERT OR IGNORE INTO dirty_causes               (从 dirty_flags 回填)
INSERT OR IGNORE INTO workflow_decision_requests
INSERT OR IGNORE INTO workflow_decision_instances
UPDATE project_state SET last_completed_stage=…
UPDATE solver_jobs SET request_sha256=…
```

这些表**本身都是 effect-hash domain**。而这段回填**没有被源版本守卫**，因此任何一次迁移
（包括 9→10）都会重跑它。**B 与 R 是在 v9 上直接创建的项目**，从未跑过 v8→v9 回填，
于是第一次 9→10 迁移就把这 13 行补了进去，`checkpoint_history` 域随之改变 ——
**而没有任何事件重新认证新哈希**，所以历史聚合校验 fail closed。

**为什么以前没被发现：** 迁移末尾的 classifier rebase 会写一条
`DIRTY_CLASSIFIER_REBASED` 事件，其 `effect_hashes_after` 是在**所有变更之后**计算的，
于是它顺带重新认证了被改动的域。**那条 rebase 事件一直在掩盖这个既有缺陷**；
0.7 移除自动 rebase 只是让它显形。

**修复（0.7.1）：** 把整段 v8→v9 数据回填用 **`if current < 9:`** 守卫
（`storage.py`，336 行 + 4 空格缩进）。9→10 迁移因此成为**纯 DDL**。

#### 0.25.3 修复后对 A/B/R 真实历史库副本的验证

| 项 | A | B | R |
|---|---|---|---|
| `schema_version` | 9 → 10 | 9 → 10 | 9 → 10 |
| `revision` | **561 → 561** | **1411 → 1411** | **2484 → 2484** |
| `events` 条数 | **561 → 561** | **1411 → 1411** | **2484 → 2484** |
| 被改动的表 | **无** | **无** | **无** |
| `aggregate_valid` | **true** | **true** | **true** |
| provenance 行数 | 0 | 0 | 0 |
| 旧 cause 读值 | `legacy_unrecorded` | 同 | 同 |

> **结论：** 迁移现在是 **DDL-only**：不改工作流状态、不写事件、不动 revision，
> 且历史聚合校验保持有效。I7 的核心风险（inspect 使 expected revision 失效、
> completed 项目 revision 增长）与 0.25.2 的隐藏缺陷**一并消除**。

#### 0.25.4 I2：effect domain generation 必须单调

评审 I2 指出："首个 v10 event 之后窗口永久关闭"**不是 verifier 自身保证的** ——
`status_snapshot()` 只看**最新一个**带 aggregate 的 event；若其后又出现一个缺少新 domain key 的
event，就会重新进入宽容分支。

**已实施硬不变量**（`status_snapshot()`，不新增权威源，只扫既有 event envelope）：

> **effect domain key 集合只能单调不减。** 一旦某个 event 记录了某个 domain key，
> 任何后续 event 缺少该 key 都使 `aggregate_valid = False`。

**测试：** `test_effect_domain_generation_must_be_monotonic`（构造
`v10 event → 缺少新 key 的 event`，断言 `aggregate_valid=False`）。
该测试特意让宽容分支本身**会通过**，以证明是单调性检查在起作用。

#### 0.25.5 I5：入口 3 现在也写 provenance

§0.17 的**入口 3**（`final_evidence_recovery.py:134`）此前不写 provenance，
于是 v10 时代新建的 cause 会与真正的历史 cause 一样读作 `legacy_unrecorded`，
把"provenance 契约存在之前的历史数据"与"当前实现绕过 provenance 写入口"混在一起。

**已修：** 该入口写入 `classification_source = "bespoke_recovery"`（新增枚举值）；
入口 2（§0.17 的 v8→v9 迁移重建）**保持不写** —— 它按定义就是历史重建，
读作 `legacy_unrecorded` 是正确的。

> 因此 `legacy_unrecorded` 的语义现在收紧为：**该 cause 创建于 provenance 契约之前，或来自历史迁移重建。**

#### 0.25.6 I6：同事务的数据库级证明

在 monkeypatch 证明之外，新增**数据库级**证明：
`test_provenance_insert_rejected_at_db_level_rolls_back_cause` —— 在
`dirty_cause_classification` 上临时装 `BEFORE INSERT … RAISE(ABORT)` trigger，
执行正常 transition，断言 **cause 与 provenance 都没有落库**。

> 评审明确：不必为"同一事务"做断电/fsync 测试（那属 durability 层）。
> 并发 CAS 覆盖已由既有 `test_concurrent_transitions_*` 系列承担，本阶段不重复。

#### 0.25.7 0.7.1 的改动清单

| 文件 | 改动 |
|---|---|
| `factory_core/storage.py` | `_upgrade_schema` 去掉 rebase/event/revision；v8→v9 数据回填加 `if current < 9:`；`status_snapshot` 加 domain 单调性 |
| `factory_core/cli.py` | 新增 `rebase-classifier` 子命令 |
| `factory_core/dirty_classification.py` | 枚举新增 `bespoke_recovery` |
| `factory_core/final_evidence_recovery.py` | 入口 3 写 provenance |
| `tests/test_dirty_cause_classification.py` | +5 测试（读路径不变更、显式 rebase CAS、domain 单调性、DB 级回滚、bespoke 来源） |

**测试：** `tests/test_dirty_cause_classification.py` **31 passed**（26 → 31）；
完整套件 **1788 passed / 0 failed / 0 collection errors**（见 §0.23）。

---



> **状态：未启动。**

### 0.26【0.7.2 实测】物理 schema 版本与「事件流记录的代次」分离；完整性分量命名

外部评审（Major 1/2/3 + I2 补充 + I9）verdict：**先加一个 0.7.2 再进 S1-A**。已执行。

#### 0.26.1 Major 1（**已核实成立**）—— 读触发的迁移让 replay 与 current 分叉

评审指出：`project_state.schema_version` **不是纯内部字段**，它属于
`workflow_events._REPLAY_FIELDS`（**第 18 行**，Tier A 可独立核对），
且 `_state_from_row` 会读它。因此 read-triggered 9→10 迁移会把
`project_state.schema_version` 写成 10，**而事件流没有对应 patch**。

**实测（A/B/R 真实库副本）：**

| 项目 | DB current | 事件流 replay | 修复前 `aggregate_valid` |
|---|---|---|---|
| A | 10 | **9** | **true**（漏检） |
| B | 10 | **9** | **true**（漏检） |
| R | 10 | **9** | **true**（漏检） |

> 0.7.1 的 `test_read_paths_do_not_mutate_revision_or_events` 只查 revision 与 event 数量，
> 因此漏掉了这个分叉 —— 评审的判断完全正确。

**修复（两个版本从此分离）：**

| 字段 | 语义 | 谁写 |
|---|---|---|
| `schema_info.schema_version` | **物理 DDL 代次** | `_upgrade_schema()` |
| `project_state.schema_version` | **事件流记录的代次** | **只有携带事件的写入**；`transition()` 收敛它，变化落进该事件的 `state_patch` |

**新门禁：** `status_snapshot()` 暴露 **`event_replay_valid`** ——
`replay_events(events)` 必须在**全部 `_REPLAY_FIELDS`** 上等于 `replay_state(current)`。

**修复后实测：** 三个库 `replay_diff == {}`、`event_replay_valid=True`、
`schema_info 9→10`、`project_state.schema_version 9→9`、revision 与 event 数不变、`aggregate_valid=True`。

#### 0.26.2 Major 2（**已核实成立**）—— rebase 并非只有一条写入路径

评审独立核对发现：`transition(clear_dirty_stage=…)` 内仍直接调用
`rebase_dirty_classifier_state()`（`storage.py` 内嵌于业务事务）。它是**合法的第二条路径**，
不会破坏"读路径无副作用"，但它**不产生 `DIRTY_CLASSIFIER_REBASED` 事件** ——
因此其 receipt 在事件流里**无处可查**。

**修复：** 把内嵌 rebase 的 receipt（`rebase_id` / `schema_version` / `obligation_count` /
`bound_by="dirty_clear_transaction"`）**绑定进该 transition 的 payload**；
并把测试名从"唯一写入者"更正为反映**两条路径**。

#### 0.26.3 Major 3（**已核实成立**）—— provenance 契约身份过窄

`classification_contract_sha256()` 此前只哈希 `schema name + dirty_classification.py 字节`，
而归因推导还依赖 `ADDITIONAL_OWNERSHIP`、冻结 registry 与 globstar matcher。

**修复：** 契约改为覆盖
`dirty_classification.py`（分支顺序与来源词表）、`dirty.py`（被镜像的分类器顺序）、
`artifact_ownership.py`（冻结 v1 表 + matcher）、`current_artifact_ownership.py`（增量表）、
`paper_sources.py`（管辖 `.tex` / `@paper:` 分支），**每个文件先写名字再写字节**。

**仍与 classifier identity 分离（Q14 保持）：** provenance-only 改动**不会**产生新的 classifier 身份。
**并且这是在任何真实项目产生 v10 cause 之前完成的** —— 评审要求的时序。

#### 0.26.4 I2 补充：attested root 必须带 domain map

原单调性循环对 `effect_hashes_after` 非 dict 的事件直接 `continue`。现在：
**携带 `aggregate_root_hash_after` 的事件必须同时携带 dict 形式的 `effect_hashes_after`**，
否则 `effect_domain_generation_valid=False`。

#### 0.26.5 I9：一个 `aggregate_valid` 拆成三个命名分量

| 分量 | 回答 |
|---|---|
| `latest_effect_attestation_valid` | 当前 domain 投影与**最近一次** aggregate attestation 一致 |
| `effect_domain_generation_valid` | effect-domain key 集合从未缩小，且每条 attestation 形式合法 |
| `event_replay_valid` | 重放事件流能复现数据库当前报告的状态 |

`aggregate_valid` = 三者合取；**三个分量各自仍可读**。
（旧实现下 `true` 可能只因"最后一次写入恰好重新计算过"。）

#### 0.26.6 测试

| 项 | 结果 |
|---|---|
| `tests/test_dirty_cause_classification.py` | **36 passed**（31 → 36） |
| 完整套件 | **1793 passed / 0 failed / 0 collection errors**（0.7.1 的 1788 + 5） |

**另有 4 处既有测试因语义变化而调整（非"顺带改测试"）：**

- `tests/test_factory_state_store.py` 有 **3 个测试**断言读触发迁移后
  `state.schema_version == SCHEMA_VERSION`。它们现在断言**物理版本**（新增
  `_physical_schema_version` 辅助函数）与**故意保持不变的**重放记录代次 —— 这正是 0.7.2 的语义。
- `tests/test_dirty_cause_classification.py` 的 `_rewind_to_v9` 改为**重放感知**：
  一个忠实的 v9 fixture 必须在事件的 `state_patch` 里也记录 9 并重算 `state_hash_after`，
  否则 fixture 自身就违反新的 replay 门禁。

#### 0.26.7 0.7.2 的改动清单

| 文件 | 改动 |
|---|---|
| `factory_core/storage.py` | 迁移只写 `schema_info`；`transition()` 收敛 `project_state.schema_version`；三个完整性分量 + replay 门禁；内嵌 rebase receipt 绑定 |
| `factory_core/workflow_events.py` | 导出 `REPLAY_FIELDS`（公开别名） |
| `factory_core/dirty_classification.py` | 契约覆盖 5 个文件 |
| `tests/test_dirty_cause_classification.py` | +5 测试；fixture 重放感知 |
| `tests/test_factory_state_store.py` | 3 个断言改为物理版本 + 重放代次 |

### 0.27【S1-A 已完成】artifact policy 层作为 compatibility façade

**提交：** `36733b8` —— **2 个新文件、563 行插入、零修改既有代码**
（这是"无行为变化"最强的可用证据）。

**新增 API：**

| 名称 | 作用 |
|---|---|
| `ArtifactPolicy` | `pattern` / `role` / `invalidation_mode` / `final_input` / `submission_member` / `ownership_rule`（`None` ⇒ **policy-only**）/ `blocker` |
| `ArtifactRole` | 闭集 12 值词表（**S1-B 才分配给已登记路径**） |
| `InvalidationMode` | `UPSTREAM_RECOMPUTE` / `EVIDENCE_SUFFICIENCY` / `PRESENTATION_ONLY` / `EXPLICIT_ONLY` / `FAIL_CLOSED` |
| `compatibility_policy(rule)` | 从一条 legacy 规则派生 policy，**逐字段原样复制** |
| `artifact_policy(path)` | 匹配语义与 `artifact_ownership` **完全一致** |
| `shadowed_ownership_rules()` | 检测 policy-only 条目**抢走** ownership 规则的路径（CI 不变量 3） |

**为何是 no-op：** ① `current_artifact_ownership.py` **未改**，`artifact_ownership()` 行为字节级不变；
② `NATIVE_POLICY` 为空 ⇒ 当前每条 policy 都是 ownership-backed；
③ compatibility policy 逐字段复制交付相关字段；④ `artifact_policy()` 返回 `None` 当且仅当
`artifact_ownership()` 返回 `None`。

**三条 CI 不变量：**

| # | 结果 |
|---|---|
| 1 | **PASS**，并**加强为同一性**（`is` 而非 `==`）：policy 携带的就是 legacy API 返回的那个对象，两个注册表无法对同一路径漂移 |
| 2 | **PASS** —— `pattern` / `semantic_domain` / 两个交付标志逐字段原样复制 |
| 3 | **PASS** —— `NATIVE_POLICY` 为空时是空真命题，因此**另有一条测试**给检测器喂一条故意遮蔽的 policy，证明它不是空转 |

**真实数据验证**（A 项目，只读）：192 个真实路径中 **171 个经两个 API 解析到同一个 ownership 对象**、
**21 个仍无规则**、**0 处不一致**；7 个 `@protected:`/`@paper:` 合成键正确地保持无规则。
**精确复现 S1 的基线分析（192 / 21）。**

**policy-only 机制：** `ownership_rule=None` 的条目**描述并分类**一个路径，但**不声明 Stage 归属**，
且**不会**出现在 `artifact_ownership()` 里。这正是 S1-B 登记那 21 个兜底路径的机制 ——
既不为 diagnostic / exploratory 文件**编造 Stage owner**，也不维护第二套 ownership 注册表。
`is_non_blocking` = `EXPLICIT_ONLY` + 两个交付标志均 False + 无 named blocker（**S1.3 的闭合**）；
`declares_blocker` 把这种**正向声明**与"消费者缺失"区分开。

**Q14 保持分离：** 有一条测试断言导入并使用本模块后
`classifier_contract_sha256()` **仍等于** `c451a9d0be64fabd…`。

**测试：** 1793 → **2178 passed / 0 failed / 0 collection errors**（+385，新模块在 118 条路径语料上重度参数化）。

---

### 0.28【S1-C-prep 已完成】所有 ownership 消费者迁到 policy 层（仍是 no-op）

**提交：** `dda04ce` —— 10 个文件、**+319 / −27**。

#### 0.28.1 为什么必须现在迁（而不是等 S1-B 之后）

收集点决定**什么会被交付**、**什么进终审输入**。它们此前问 `artifact_ownership()` 是否登记，
而该函数对"无 Stage 归属"的路径返回 `None`。因此一旦 **S1-B** 把那 21 条兜底路径登记为
policy-only 条目，`iter_owned_artifacts()` 会**跳过它们**，而 `submission_bundle` 的两个闸门
仍会判定它们"未覆盖"。

**在 S1-B 之后迁会把一次行为变化藏在一次分类变化里；现在迁则可作为 no-op 审查。**

#### 0.28.2 新增三个 policy-aware 等价件

| 名称 | 作用 |
|---|---|
| `iter_policy_artifacts()` | `iter_owned_artifacts` 的 policy 版孪生；**完全镜像其遍历与跳过顺序**，成员判定改由 `artifact_policy` 决定 |
| `policy_ownership_rule()` | 路径背后的 legacy 路由规则，或 `None`（policy-only 也为 `None`） |
| `reopen_after_step_for_policy_artifact()` | policy-aware 恢复目标 |

#### 0.28.3 迁移清单（13 个调用点 → 12 个已迁 + 2 个刻意保留）

| 文件 | 迁移内容 |
|---|---|
| `factory_core/finalization.py` | 迭代器（57）+ 恢复目标（167） |
| `factory_core/submission_bundle.py` | 迭代器（141）+ **两个覆盖闸门**（168、190）+ 成员查询（217） |
| `scripts/cleanup_project_artifacts.py` | 受保护集合迭代器（166） |
| `scripts/claim_graph.py` | 规则查询（602）+ resume step（614） |
| `factory_core/steps/validators.py` | 恢复目标（509） |
| `factory_core/dirty_rebase.py` | 规则查询（183） |
| `factory_core/final_evidence_recovery.py` | 规则查询（51） |
| `web/backend/diagnostics_service.py` | owner-stage 查询（245） |

**刻意不迁（并已由源码级测试守卫）：**

| 文件 | 原因 |
|---|---|
| `factory_core/dirty.py:404` | **冻结信任根**，其字节被哈希进 `classifier_contract_sha256` |
| `factory_core/dirty_classification.py:192` | **刻意镜像**冻结分类器的分支顺序 |

#### 0.28.4【新增实测】两处遗漏调用点都生产可达

计划最初写"四个消费者"，漏掉的两个都不可跳过：

| 调用点 | 可达性 |
|---|---|
| `scripts/claim_graph.py` | **可达且与门禁相关**：`claim_binding_issues` 被 `factory_core/steps/validators.py:203` 与 `scripts/judge_packet.py:957` 调用 |
| `web/backend/diagnostics_service.py` | **可达但只读**：经 `web/backend/project_api.py:25` |

#### 0.28.5【新增实测】顺带修掉一个真实缺陷

`web/backend/diagnostics_service.py` 此前从**冻结**模块导入 `artifact_owner_stage`（83 条规则），
而非 current（88 条），于是 Web 诊断 payload 与系统其余部分**不一致**：

| 路径 | frozen 报 | current 实际 |
|---|---|---|
| `judge_evidence.json` | `None` | **10** |
| `models/reporting_scope/scope_review_manifest.json` | **3** | **10** |
| `STEP5_RECEIPT.json` | `None` | **4** |
| `method_fit_suggestions.json` | `None` | **1** |

迁到 policy 层后经 current 注册表解析。**测试同时断言修正后的值与"冻结查询确实曾经不同"**，
以防该测试变成空真命题。

#### 0.28.6 等价性证据（最强形式：真实项目树）

| 项目 | 全部 | `final_input_only` | `submission_only` |
|---|---|---|---|
| A | 400 | 374 | 347 |
| B | 1146 | 1044 | 643 |
| R | 938 | 923 | 201 |

**5 种配置 × 3 个项目 = 15 种组合，全部集合完全相同。**
（B/R 是指向仓库外的符号链接；两个迭代器都在内部 `resolve()`，所以比对脚本也必须 resolve ——
这是本阶段抓到的一个**脚本 bug**，不是代码 bug。）

#### 0.28.7 测试

| 项 | 结果 |
|---|---|
| `tests/test_artifact_policy.py` | **631 passed**（385 → 631） |
| 完整套件 | **2424 passed / 0 failed / 0 collection errors**（2178 + 246） |

**新增 7 组测试**，其中最后一组是**源码级守卫**：扫描 `factory_core`/`apps`/`scripts`/`web`，
对所有四个 legacy 查询的调用报错，**除非该名称是由 `artifact_policy` 导入绑定的（含别名）**；
仅两个刻意保留的模块在允许名单内。这让"迁移完整性"变成可执行的断言，而不是一次性人工清点。

---

## S1 已观测 Artifact Policy 全覆盖 + fallback 遥测

> **S1-A（`36733b8`）与 S1-C-prep（`dda04ce`）已完成。** S1-D / S1-B-activate 未启动。

### S1.0 完成判据

| # | 判据 |
|---|---|
| 1 | A 的 **192 个真实 artifact path**：`observed_policy_gap = 0`（每条命中显式 policy，**不一定**命中 legacy ownership） |
| 2 | `accidental_fallback = 0`（兜底只允许由显式 `FAIL_CLOSED` policy 触发） |
| 3 | `explicit_fail_closed` 可以 > 0，但每条可在 policy 表定位 |
| 4 | §0.11 的 **13 个调用点 / 9 个模块**全部迁移并有 delta 断言 |
| 5 | §0.18/§0.22 的 aggregate 与 downgrade barrier 回归，**以及 §0.26 的三项完整性分量与 `event_replay_valid`**，继续通过 |
| 6 | §S1.4 的 4 条 CI 不变量通过（**不变量 1–3 已随 S1-A 生效**；#4 已由 I4 定案为「分开」，其 hash 扩展已在 0.7.2 落地） |
| 7 | §S1.3 的 fail-closed 兜底规则通过 |
| 8 | 相对 `aaea7f8` 分支基线**不新增 failed** |
| 9 | **provenance 覆盖**：§0.17 **入口 1** 产生的每个新 cause 都有非 `legacy_unrecorded` 的 `classification_source` |

### S1.1 四个 commit

| 序 | commit | 内容 | 行为变化 |
|---|---|---|---|
| 1 | **S1-A** ✅ `36733b8` | `ArtifactPolicy` + `artifact_policy()` + 从 legacy ownership 生成 compatibility policy | **无** |
| 2 | **S1-C-prep** ✅ `dda04ce` | 13 个调用点迁到 policy-aware collector（新增 `iter_policy_artifacts()`），**只消费 compatibility policy** | **无**（集合保持原样，15/15 组合已实测） |
| 3 | **S1-D** | 来源映射扩展到 policy 语义；**CI 不变量 #4 视 I4 结论**；replay 回归 | 新增遥测，历史 hash 不变 |
| 4 | **S1-B-activate** | 21 条 explicit policy；`current_dirty` 按 policy 分类 | **唯一改变业务分类语义的一步** |

四项仍必须**同 PR**。

### S1.2 `ArtifactPolicy` 与双 API

```python
ArtifactPolicy(pattern, role, invalidation_mode, final_input, submission_member, ownership_rule=None)
```

**role：** `PRODUCTION` / `DERIVED` / `EVIDENCE` / `EVIDENCE_INDEX` / `HISTORICAL_EVIDENCE` /
`OBSERVATION` / `DIAGNOSTIC` / `REPAIR_EVIDENCE` / `DERIVED_GENERATOR` / `DERIVED_PRESENTATION` /
`EXPLORATORY` / `DELIVERY`

**invalidation_mode：** `UPSTREAM_RECOMPUTE` / `EVIDENCE_SUFFICIENCY` / `PRESENTATION_ONLY` /
`EXPLICIT_ONLY` / `FAIL_CLOSED`

`artifact_policy()` 为新 API；`artifact_ownership()` **保持 legacy 语义**
（只对有 `ownership_rule` 的 policy 返回该规则；policy-only 返回 `None`）；
匹配顺序 native policy 优先于 frozen registry。

### S1.3 五种 `invalidation_mode` 的落点与阻断责任（闭合表）

`DirtyFlag` 只有 `MODEL` / `MATH` / `RESULT` / `PROSE` / `VISUAL` / `CITATION` / `FORMAT`，
**没有能表达 `EVIDENCE_SUFFICIENCY` 的状态**。**不新增第六种 flag**，拆到两条既有通道：

| invalidation_mode | 落点通道（既有对象） | 阻断责任 |
|---|---|---|
| `UPSTREAM_RECOMPUTE` | 现有 dirty routing（用 `ownership_rule` 的 legacy `dirty_flag` / `owner_stage`） | 现有 dirty → reopen |
| `PRESENTATION_ONLY` | 现有 downstream format/presentation obligation | 不产生 MODEL/RESULT/MATH rewind |
| `EVIDENCE_SUFFICIENCY` | **必须**使对应 **checkpoint / audit eligibility / candidate preflight** 失效，并在 policy 中**指明具体消费者** | 指定消费者 |
| `EXPLICIT_ONLY` | 文件自身变化**不自动**产生 upstream dirty | 见准入条件 |
| `FAIL_CLOSED` | 保持现有 `MATH@8 + RESULT@4` | 现有兜底 |

**每类 policy 的阻断语义（0.7.1 修正：原准入条件与 fail-closed 规则互相冲突，已闭合）**

| 类别 | 条件 | 语义 | finalization 行为 |
|---|---|---|---|
| **`NON_BLOCKING_BY_POLICY`** | `invalidation_mode = EXPLICIT_ONLY` **且** `final_input=false` **且** `submission_member=false` **且** 无 named blocker | 该产物自身变化**明确不需要任何阻断** —— 这是 policy 的**声明**，不是"找不到阻断者" | **不因这种变化 fail closed** |
| `BLOCKED_BY_NAMED_CONTRACT` | `invalidation_mode = EXPLICIT_ONLY` **且** policy 写出 named validator / finding / repair contract | 阻断责任由该既有合同承担 | 执行该 blocker |
| `EVIDENCE_SUFFICIENCY` | 必须指明具体 consumer（checkpoint / audit eligibility / candidate preflight） | 使该 consumer 失效 | 执行该 consumer |
| `UPSTREAM_RECOMPUTE` | — | 现有 dirty routing | 现有 dirty → reopen |
| `PRESENTATION_ONLY` | — | 现有 downstream format/presentation obligation | 不产生 MODEL/RESULT/MATH rewind |
| `FAIL_CLOSED` | — | 保持现有 `MATH@8 + RESULT@4` | 现有兜底 |

**fail-closed 兜底规则（修正后，只覆盖真正的缺口）：**

> 仅当 **policy 声称存在阻断责任，却找不到对应的 obligation / named consumer** 时，
> finalization 才必须 fail closed。**未知的 `invalidation_mode` 取值继续 fail closed。**
> `NON_BLOCKING_BY_POLICY` 是显式声明，**不属于**这一情形。

**为什么必须这样拆（评审指出）：** 原表述下，一个 `EXPLICIT_ONLY` + 双 False、
且没有 named blocker 的 diagnostic artifact 按准入条件合法，但按"没有 obligation 就 fail closed"的
硬规则又会在 finalization 被阻断 —— 于是"双 False"**没有提供 non-blocking 语义**，
只是把阻断从 upstream rewind 延后到了 finalization，S1 的简化收益被削弱。
拆成上表后**无需新增第六种 `DirtyFlag`**，也不会把 diagnostic / exploratory 文件重新变成延迟版 dirty obligation。

### S1.4 4 条 CI 不变量（S1 硬门禁）

| # | 不变量 | 状态 |
|---|---|---|
| 1 | 对所有 ownership-backed policy：`artifact_policy(path).ownership_rule == artifact_ownership(path)` | 生效 |
| 2 | compatibility policy 的 `owner_stage / dirty_flag / final_input / submission_member` **逐字段等于** legacy rule | 生效 |
| 3 | policy-only pattern **不允许遮蔽**既有 ownership rule，除非有显式 allowlist 与 overlap 测试 | 生效 |
| **4** | **classifier identity 与 provenance identity 分开** | **I4 已定案（原"合并"候选撤回）** |

**#4 的定案内容（外部评审 I4 verdict：支持分开）：**

| 身份 | 回答的问题 |
|---|---|
| `classifier_contract_sha256()` | **为什么产生这个 `DirtyChange`** |
| `policy_contract_sha256`（side table 列） | **为什么把这个 `DirtyChange` 的来源解释成 `current_rule` / `frozen_rule` / `fallback` 等** |

把纯 provenance 代码并入 classifier identity 会导致"只改审计解释逻辑也触发 classifier rebase"，
与本次架构拆分目标冲突。

**side table 现有列已足够**（无需再复制一份 classifier sha）：`cause_id` 可 JOIN 到
`dirty_causes.classifier_contract_sha256`，所以 `cause_id + classifier sha + policy sha` 三者齐备。

**但有一个硬性条件（评审强调）：** `policy_contract_sha256` **不能只是"文件名意义上的
`dirty_classification.py` 的 SHA"**，它必须覆盖**所有真正影响 provenance 判定的语义** ——
即 `ADDITIONAL_OWNERSHIP`、冻结 ownership registry、以及 pattern matcher（`artifact_pattern_matches`
及其 globstar 变体规则）。否则 `(classifier sha, policy sha)` 这一对**不足以唯一重建** provenance 判定。
**这是 S1-D 的验收项（取代原不变量 #4）。**

### S1.5 21 条路径（role 表达）

`step5_*` 与 `*_reuse_gap_record.md` → `DIAGNOSTIC` / `REPAIR_EVIDENCE` + `EXPLICIT_ONLY`（双 False）；
`m1/m4_solver_evidence.json` → `EVIDENCE` + `EVIDENCE_SUFFICIENCY`（`final_input=True` ⚠ / `submission_member=False`）；
`m1_solver_evidence_failed.json` → `HISTORICAL_EVIDENCE` + `EXPLICIT_ONLY`；
`model_source_map.json` → `EVIDENCE_INDEX` + `EVIDENCE_SUFFICIENCY`；
`paper/appendix_sources/06_figures.py` → `DERIVED_GENERATOR` + `PRESENTATION_ONLY`；
`paper/appendix_sources/pro01/**` → `EXPLORATORY` + `EXPLICIT_ONLY`（`input_arrays.npz` 双 False，Q3）；
`tables.tex` / `results_values.tex` → `DERIVED_PRESENTATION` + `PRESENTATION_ONLY`
（科学正确性走 §0.6 的派生校验链路：`canonical_result` → deterministic generator →
regenerated hash verification；**排版改动不拉回 Solve，同时不允许手改数字蒙混过关**）。

### S1.6 测试清单

3 个冻结兼容测试（`tests/test_rerun_current_ownership.py`：`test_native_extension_does_not_rewrite_frozen_trust_roots` /
`test_unknown_artifact_still_has_no_owner_and_keeps_fail_closed_dirty_flags` /
`test_existing_paths_have_exact_frozen_classifier_behavior`）
+ §0.18 的 7 条 schema/replay/tamper 测试
+ §S1.4 的 4 条不变量（#4 已定案为「分开」）
+ §S1.3 的 3 条：`test_policy_without_obligation_or_consumer_fails_closed` /
`test_explicit_only_requires_double_false_or_named_blocker` / `test_evidence_sufficiency_names_a_consumer`
+ 各调用点 delta 断言（含 `test_claim_graph_owner_delta_is_expected`、
`test_diagnostics_owner_stage_delta_is_expected`、`test_validator_reopen_target_delta_is_expected`）。

---

## S5 Solver 三维对账

### S5.1 三个正交维度 + 第六派生态

```
execution_state   : RUNNING | TERMINAL_SUCCESS | TERMINAL_FAILURE | UNKNOWN
                    （来源：PID/process identity、lease、exit file、backend status）
evidence_state    : COMPLETE | INCOMPLETE | INVALID | NOT_REQUIRED
                    （来源：submission/completion receipt、input/output closure、event binding；
                      复用 §0.5 的 fail-closed 语义，不新造状态）
workflow_relevance: REQUIRED | SUPERSEDED | ORPHANED | HISTORICAL | ADVISORY | UNRESOLVED
```

**完成条件：** `REQUIRED` 阻断；**`UNRESOLVED` 也阻断**；
只有能**正面证明** `SUPERSEDED / ORPHANED / HISTORICAL / ADVISORY` 才允许忽略。

A/B 两个残留 job 的现状：`execution_state = TERMINAL_SUCCESS`、`evidence_state = INCOMPLETE`、
`workflow_relevance = UNRESOLVED`（其 checkpoint receipt **未绑定 `job_id`**，无法正面证明）。

### S5.2 实施方式

`evaluate_solver_job(...) -> SolverEffectiveState`，**纯读取、不写库、初期不升 schema**；
先拿 A/B 两个真实异常回归；稳定后**才**让 runner preflight 写 reconciliation event。
**read path 全程只计算 effective state，不产生副作用。**
**只纠正"有效状态视图"，不改写历史 `solver_jobs.status`。**

### S5.3 未来 checkpoint receipt 应增加 durable job reference

新 checkpoint receipt 逐渐增加对所采用 **solver completion receipt / `job_id`** 的明确引用，
以逐步降低 `UNRESOLVED` 面积，且**无需**给历史 `solver_jobs` 加可变 relevance 字段。

### S5.4 测试

```
DB running + live PID                             -> execution RUNNING
DB running + dead PID + completed exit            -> execution TERMINAL
terminal + missing completion receipt             -> evidence INCOMPLETE
terminal + valid two-stage receipt                -> evidence COMPLETE
superseded historical job（正面证明）              -> 不阻断 Completed
required job + incomplete evidence                -> 阻断 Completed
无法证明 relevance 的 job                          -> UNRESOLVED，阻断 Completed
reconcile twice                                   -> same projection（幂等）
read API 调用                                      -> 无新事件、无写库
A/B 两个真实历史 job                               -> TERMINAL_SUCCESS + INCOMPLETE + UNRESOLVED
```

**禁止：** 不 `UPDATE solver_jobs SET status='completed'`。

---

## S4 原因与身份结构化

### S4.1 `reason` = `code` + `subcode` + `actor`

现状：`engine.py:1701` `PAUSED`、`:1740` `RESUMED` 的 `reason.message` 为空
（A 的 13 次 RESUMED、11 次 PAUSED 全空）；r517 的 `STEP_REOPENED` 语义在 payload 顶层 `final_decision`。

```json
{"reason": {"code": "WORK_REOPENED", "subcode": "REOPEN_REVISION_TEXT",
            "actor": "engine", "message": "", "evidence": []}}
```

```
PAUSED  / OPERATOR | DEADLINE | EXECUTION_SCOPE
RESUMED / OPERATOR | RETRY | AUTO_RECOVERY
WORK_REOPENED / REOPEN_REVISION_TEXT | INVALIDATED_RESULT
```

**canonical `code` 保持稳定；不为子类型新增顶层 event type；新字段为 additive extension，旧 event 仍可读。**
改动点：`engine.py:1701` / `:1740` / `:1130` 一带、`workflow_events.py:47`。

**测试：** `test_resumable_events_carry_reason_subcode_and_actor`；r517 类场景可从 `reason` 读出
`REOPEN_REVISION_TEXT`；旧事件（无 `subcode`）仍可被 reducer 解析。

### S4.2 prompt semantic identity

已证明的过绑定：`effective_prompt.py:55-85` 把跨 8 个项目共享的 `web/model_config.json`
**整文件哈希**并入 `model_config_sha256`。A 的 66 份 receipt 中整文件哈希 **11 个值**、
`resolved_assignment` 只 **2 个**；r249/r331 的漂移发生在有效选择未变时。

```
进 semantic identity：model id / reasoning effort / provider / tool availability /
                      关键模型参数 / 与当前 step 相关的 dispatch policy / 解析器语义（含默认与 fallback 规则）
                      -> semantic_prompt_hash, semantic_model_hash,
                         prompt_template_hash, researcher_instruction_hash
进 audit_context：其他项目配置 / UI 配置 / 无关 step 条目 /
                 完整 registry 的未消费部分 / 运行时间戳 -> audit_context_hash
```

**`dispatch_semantics_sha256` 必须覆盖解析器语义** —— 旧算法会 hash `model_dispatch_config.py`
与 dispatcher 源码；新算法若只 hash 最终 `resolved_assignment` 会**漏报**解析规则变化。

**必须保持：** 研究者指令、有效模型配置、审计政策的任何变化**仍必须**触发漂移。

**完成判据：** 用新算法回放 r249/r331，确认有效 step 配置未变时 `model_config` drift 消失；
**且**解析器默认/fallback 行为变化仍必须产生漂移。

**测试：** 无关项目 `step_N` 变化 → 不漂移；本 step `primary`/`dispatch_semantics` 变化 → 漂移；
`researcher_note` 变化 → 漂移；回放 r249/r331；v1 receipt 仍可读。

### S4.3 三层身份

| 身份 | 含义 |
|---|---|
| `candidate_id` | 科学内容与正式候选内容身份 |
| `audit_attempt_id` | `candidate` + audit contract + evaluator identity + audit policy |
| `release_id` | `candidate` + 被采用的 audit receipt + delivery/package contract |

实测支持：r549 与 r559 的 `input_fingerprint` **完全相同**（`7068d832…`），中间是 r556
`FINAL_AUDIT_CONTRACT_RETRY_PREPARED`；r550 证明发布阶段仍须检查终审后内容漂移。
**不把 `release_id == audit_snapshot` 写成永久 invariant。**

**测试：** `test_release_receipt_separates_three_identities`。

---

## S6 退役手写驱动脚本层（Q12：核心目标）

### S6.1 目标

> 退役"agent 为每次恢复或局部推进临时拼一个控制程序"的运行模式。
> `one_native_step_per_run` 只是这批脚本表现出的**一种症状**。

**不新增第二个 runner。** 在现有 `FactoryService + FactoryEngine.run()` 上增加
**受支持的 bounded-advance execution contract**。

### S6.2 最小能力（8 项）

| # | 能力 | 说明 |
|---|---|---|
| 1 | `expected_revision` | **CAS 式**拒绝过期控制脚本 |
| 2 | `expected_cursor` | 至少覆盖 stage / subtask / source step，防止 revision 匹配但**运行位置不匹配** |
| 3 | `allowed_source_steps` 或更细的 authorized subtasks | 继续复用**现有**授权边界 |
| 4 | 可选 `max_subtasks` | 人工调试或一次性受限恢复；**正常运行默认 advance until blocked** |
| 5 | `protected_manifest`（**入口 + 提交前各验一次**） | project-relative path + sha256，取代 47 份各写各的 `verify()`。**入口校验**回答"我开始执行时保护对象是否已符合调用者预期"；**提交前校验**回答"本次执行是否破坏了保护对象"。**只做终检无法区分"进入时已经脏"与"本次 runner 改脏"** |
| 6 | runner / lease 检查 | 继续由**现有 engine** 负责 |
| 7 | 统一结构化返回 | 起止 revision、实际完成 subtasks、停止原因、protection verification |
| 8 | **protected 终检时机** | 在 **subtask 执行结束、成功 checkpoint 提交之前** —— 使哈希漂移**阻止成功提交**，消除"checkpoint 已 PASS、随后 wrapper 才发现保护文件变了"的时间窗 |
| **9** | **`made_progress` + `boundary_fingerprint` → `UNCHANGED_BOUNDARY` / `NEEDS_INSPECTION`** | `boundary_fingerprint` 至少含 `revision / status / stage / subtask / source_step / blocked_reason / pending human request or solver dependency identity`。重复调用得到相同 fingerprint 且 `made_progress=false` 时，返回**明确的** `UNCHANGED_BOUNDARY`（或 `NEEDS_INSPECTION`）。**不持久化 `seen[key]`、不新增状态机** —— 它正好取代手写脚本里的 `Repeated unchanged native boundary requires inspection`（外部评审 J1 建议） |

### S6.3 结论收窄

> ~~核心 engine 几乎不用改。~~
>
> **现有连续 runner 与授权边界继续复用；S6 不增加新 runner，
> 但需要增加受支持的 bounded-advance execution contract，
> 并将 revision / cursor / protected-artifact precondition 纳入正式执行入口。**

保留（不变）：`max_steps` 作为调试/兼容开关；**不新增 `single_step_compat`**；
**不动 `allowed_source_steps`**；**不修改 A 项目的 `reuse_execution_contract.json`**（A 继续作为历史证据与回归样本）。

### S6.4 成功指标

```
新 fresh 项目                 : RUN_STOPPED(reason=max_steps) = 0
正常 reuse 项目（非兼容模式）   : RUN_STOPPED(reason=max_steps) = 0
RUN_BOUNDARY_REACHED          : 只在真实授权边界出现
human / solver / deadline     : 仍正确停机
```

**"退役手写控制脚本"的可测验收（外部评审 J2：不用字符串匹配）：**

1. **AST 层**：对新 canary 的 `work/**/*.py` 做 AST 扫描，**不允许项目控制脚本直接导入并调用
   `FactoryEngine.run`、`FactoryService.run` 或等价 workflow control API**。
   （字符串匹配 `engine.run(` 会漏掉 `FactoryService.run`、alias import、
   `getattr(engine, "run")` 等写法。）
2. **执行证据层**：所有受限推进都能从 **bounded advance 的结构化结果**或既有事件链证明
   是通过**正式入口**执行的。
3. **形态层**：`work/` 不再生成 `protected_files.json` + `progress.json` + `run_step*.py` 这一**组合模式**。

### S6.5 仍待查

**合同 / 模板生成侧**：谁为未来 reuse 项目生成 `one_native_step_per_run` 类约定，
谁在 prompt / 规范层要求逐步人工核验。**须在 S6 实施前定位**，否则只改代码不改动机，脚本仍会增殖。

---

## S3 退役历史 bespoke recovery

**目标：** `final_evidence_recovery.py`（`recover_final_evidence_config()`，docstring `:41-44`；
`:50-53` 硬编码要求 `judge_evidence.json` 的 `owner_stage==10 and dirty_flag=="FORMAT_DIRTY"`）
+ `final_judge_projection.py` 的 reclassification 分支。

> **范围澄清（见 §0.17）：** `dirty_rebase.py` **不是** S3 目标 —— 它不写 `dirty_causes`，
> 且承担正常的换代 rebase 职责，继续保留。

**流程（Q4：deprecate + guard）：**

```
1 deprecated -> 2 禁止新代码调用（RuntimeError 守卫 + CI 断言无新调用者）
-> 3 保留历史回归测试 -> 4 运行完整新项目周期 -> 5 删除代码，保留历史事件解释器
```

**与 S6 无关**（连续 runner 已存在）；独立成章，排流程末尾。
**门禁：** 不存在活跃项目处于该恢复窗口（`status='paused'`、无 runner、最后真实 checkpoint 是 Step15 `polish`）。

**§0.17 入口 3 的关系：** 该路径目前是唯一"新建 cause 但不写 provenance"的路径。
0.7 接受这一点（它是 S3 退役目标），但**若 S3 长期不执行**，该缺口会持续存在 —— 列为外部评审 I5。

---

## S2 暂缓（不实施 `OWNERSHIP_GENERATION_INDEX`）

历史契约身份 **21 个**（§0.7）；`SHA → generation label` 只是新增目录，**不消除**
frozen registry / additional registry / matching order / compatibility semantics。

**重新评估闸门：** ① S1 完成且连续运行稳定；② 不再需要新增 `ADDITIONAL_OWNERSHIP`；
③ 届时作为独立 **Classifier Contract Catalog**（静态、版本化、只读，**不得由运行时改 Python source**）。

---

## Canary 与分层 gate

| gate | 位置 | 内容 |
|---|---|---|
| **G1** | S1 后 | synthetic project，验证 classification / final input / submission / cleanup 四条 collection 路径 |
| **G2** | S5 后 | A/B 只读历史异常 + synthetic required / superseded / **unresolved** jobs |
| **G3** | S4.1 后 | 旧事件 replay + 新 reason envelope 兼容 |
| **4.5** | S6 后 | 完整 fresh canary `runtime_simplification_canary_01`（新建，不复用不改 A/B/R） |
| **5.5** | S4.2 后 | canary 回归，重点**误重跑** |
| **6.5** | S4.3 后 | canary 回归，重点 **audit / delivery** |

**4.5 的九项验证：** `RUN_STOPPED(max_steps)=0`、同一 runner 连续推进、human gate 停、
solver wait 停、`allowed_source_steps` 越界停、unknown artifact 仍 fail closed、
policy-only observation 不引发 upstream recompute、required solver evidence 不完整会阻断、
`PROJECT_COMPLETED` 满足新的 solver invariant。

---

## 实施顺序

| 顺序 | 步骤 | 状态 |
|---|---|---|
| 0 | S0 基线冻结 | ✅ |
| 0.5 | 修实施边界 + 干净 worktree + 资料同步 | ✅ |
| 0.6 | 环境冻结 → 分支基线 `1757 passed / 0 failed` | ✅ `aaea7f8` |
| 0.7 | schema 10 门禁（14/14 PASS） | ✅ `bbed84c` |
| **0.7.1** | **读路径不再改写工作流状态**（I7）+ 域单调性（I2）+ 入口 3 provenance（I5）+ DB 级回滚测试（I6）；**并修掉一个被 rebase 事件掩盖的既有缺陷** | ✅ `ed69234`（见 §0.25） |
| **0.7.2** | **物理 schema 版本与事件流代次分离** + `event_replay_valid`（Major 1）+ rebase receipt 绑定（Major 2）+ provenance 契约覆盖 5 文件（Major 3）+ attested root 必须带 domain map（I2）+ `aggregate_valid` 拆三个分量（I9） | ✅ `5568d01`（见 §0.26） |
| **1a** | **S1-A**：`ArtifactPolicy` 兼容 façade（零行为变化） | ✅ `36733b8`（见 §0.27） |
| **1b** | **S1-C-prep**：13 个调用点迁到 policy-aware collector | ✅ `dda04ce`（见 §0.28） |
| **1c** | S1-D：来源映射扩展 + replay 回归 | **下一步 / 未启动** |
| **1d** | S1-B-activate：21 条 explicit policy（唯一改变分类语义的一步） | 未启动 |
| G1 | synthetic project | |
| 2 | S5（shadow，含 `UNRESOLVED`，不写库） | |
| G2 | A/B 只读异常 + synthetic jobs | |
| 3 | S4.1 | |
| G3 | 旧事件 replay + reason envelope | |
| 4 | S6（bounded-advance contract + 退役手写驱动层） | |
| 4.5 | 完整 fresh canary | |
| 5 / 5.5 | S4.2 + canary 回归（误重跑） | |
| 6 / 6.5 | S4.3 + canary 回归（audit/delivery） | |
| 7 / 8 / 9 | S3 deprecate → 完整 fresh+reuse 周期 → S3 删除 | |
| 10 | S2 重评 | |

**S6 前的补充门禁（本版新增）：**
1. **真正的旧版本代码回归**：用 `bde49712` 的代码作为一个独立解释器环境，打开已升级到
   schema 10 的 fixture，断言在任何业务写入发生**之前**拒绝（目前只有版本判断逻辑的模拟回归）。
2. **migration-time rebase 的影响评估**（外部评审 I7 的结论落地）。

**跨阶段禁令：** 不新增 registry / 状态机 / **第二个 runner** / 权威源；不改写历史事件与
`dirty_causes` 行；**不改既有表行形状**；**不改 `factory_core/dirty.py` 与
`factory_core/artifact_ownership.py` 的字节**；不放宽 fail-closed 不变量；
不用 continuation override 冒充审核通过；不碰 `apps/mcp/**` 与那 12 条 secret finding；
不修改 A 的 `reuse_execution_contract.json`。

---

## 实施时最易走样的三处

1. **保持"policy role / invalidation / ownership"三者正交** —— 不要为让 192 个 path 全覆盖
   而给 evidence / diagnostic 文件补**虚假的 Stage owner**。
2. **三层身份保持分离** —— r549/r559 已证相同 `final_input_fingerprint` 可经历新 audit attempt；
   r550 证发布阶段仍须检查终审后内容漂移。
3. **S5 reconciliation 只纠正"有效状态视图"**，不得改写旧 `solver_jobs.status`。

---

## 决策登记

| # | 决定 | 状态 |
|---|---|---|
| Q1 | 不改 `dirty_causes` 行结构；append-only side table + 新 domain key + trigger | ✅ 已实施（0.7） |
| Q2 | 逐条核生产者/消费者；无法证明进交付的默认 `submission_member=false`；`final_input` 单独判定 | 已定 |
| Q3 | `input_arrays.npz` 默认不进 submission | 已定 |
| Q4 | S3 = deprecate + guard → 完整周期 → 删除 | 已定 |
| Q5 | 覆盖全部 21 条，目标 policy coverage 100% | 已定 |
| Q6 | submission 覆盖闸门迁到 `artifact_policy()`，与 S1 同 PR | 已定 |
| Q7 | 分支 = 从 `bde49712` 干净 worktree | ✅ 已实施 |
| Q8 | 环境 = 修环境 + `requests` prerequisite commit | ✅ 已实施（`aaea7f8`） |
| Q9 | `scripts/cleanup_project_artifacts.py` 纳入 S1-C | 已定 |
| Q10 | **= Option A**：`SCHEMA_VERSION = 10` + downgrade barrier | ✅ 已实施（0.7） |
| Q11 | `dirty.py` 冻结字节且不加新逻辑，但必须保留 | ✅ 已实施 |
| Q12 | S6 提升为核心目标：bounded-advance contract（不新增 runner） | 已定 |
| Q13 | 可开始 0.5 + 0.6，S1 暂不启动 | ✅ 已实施 |
| **Q14** | **classifier identity 与 provenance identity = 分开**（原"把 `dirty_classification.py` 并入 `classifier_contract_sha256()`"已撤回）。`policy_contract_sha256` 必须覆盖影响 provenance 判定的全部语义，否则 `(classifier sha, policy sha)` 不足以唯一重建 provenance | **已定案（I4）** |
| **Q15** | **0.7.1 = 读路径不改写工作流状态**（I7）+ 域单调性（I2）+ 入口 3 provenance（I5）+ DB 级回滚测试（I6） | **✅ 已实施**（`ed69234`） |
| **Q16** | **0.7.2 = 物理 schema 版本与事件流代次分离**（Major 1）+ rebase receipt 绑定（Major 2）+ provenance 契约覆盖 5 文件（Major 3）+ attested root 必须带 domain map（I2）+ `aggregate_valid` 拆三个分量（I9） | **✅ 已实施**（`5568d01`） |
| — | ⚠ 四条路径逐条读生产者/消费者，不拍布尔值 | 工作项（S1） |
| — | 上游文档 §2.5 / §5 M1 归因已修正 | ✅ |
| — | D 盘副本与 bundle 待刷新 | 待办 |

---

*本文件为自包含实施级方案 + 执行记录。0.5/0.6/0.7 的代码变更已在 `feat/runtime-simplification`
上提交（`aaea7f8`、`bbed84c`）；S1 未启动。*
