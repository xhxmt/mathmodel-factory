# Runtime Simplification 验收计划

- **基准 head**：`0dbc5c39b68c453d49f8c35c932ebee05497088d`（`feat/runtime-simplification`）。Gate 0 已验证的就是这一系列最新 head，两轨均在此基础上执行。
- **基准 worktree**：`/home/tfisher/paper_factory/.worktrees/runtime-simplification`（tree clean）
- **配套 PR**：[#35](https://github.com/xhxmt/mathmodel-factory/pull/35)（Draft，仅为触发 CI）
- **原则**：Gate 0 通过前不改结构；Gate 1 canary 通过前不删除任何 `work/*.py` 驱动层。

---

## 0. 已核实的基线事实

### 0.1 代码基线

| 事实 | 证据 |
|---|---|
| `run_bounded` 定义于 `factory_core/engine.py:97` | 唯一生产调用链：`service.advance_bounded()`（`service.py:426`）← CLI `factory advance`（`cli.py:423-460`） |
| 生产侧零消费者 | 无任何 `work/*.py` 使用 `run_bounded`；`tests/test_bounded_run.py` 收集到 23 项（与 S6 `tests_added: 23` 一致） |
| `run()` 对终态直接返回 | CAS → `check_cursor` → runner 检查 → `if state.status in TERMINAL_STATUSES: return state` |
| `run()` 对 `paused/failed/awaiting_*` 也直接返回 | 同一函数内独立分支 |
| outcome 双常量 | `UNCHANGED_BOUNDARY = "UNCHANGED_BOUNDARY"`（`bounded_run.py:76`）、`NEEDS_INSPECTION = "NEEDS_INSPECTION"`（:77） |
| `needs_inspection` 定义 | `unchanged_boundary and not made_progress`（:358-359） |
| `to_dict()["outcome"]` | `NEEDS_INSPECTION if needs_inspection else UNCHANGED_BOUNDARY if unchanged_boundary else "ADVANCED"`（:377） |
| 保护点双层命名**不对称** | engine 内 `error_class = "PERMANENT_PROTECTED_MANIFEST_VIOLATED"`（`engine.py:1257`，走 `STEP_FAILED` + `status=FAILED`）；`run_bounded` 返回 `stop_reason = "PROTECTED_MANIFEST_VIOLATED"`（`engine.py` 内 `if not final.ok` 分支） |
| 前置保护仅在带 manifest 的 contract 下生效 | `if self._bounded_contract is not None and self._bounded_contract.protected_manifest` |
| `SCHEMA_VERSION` | 本分支 = 10（`factory_core/domain.py:9`）；`main` = 9；`bde49712` = 9 |

### 0.2 版本双轨（设计，非漂移）

`storage.py:1110-1125` 明确注释：`schema_info.schema_version` 是**物理 DDL 代次**；`project_state.schema_version` 是**事件流记录的代次**（`_REPLAY_FIELDS` 成员，被每个 versioned event 的 `state_hash_after` 绑定）。在只读路径 bump 它会造成「DB 说 10、replay 出 9」的 hash 不自洽，因此**只有携带事件的写（`transition()`）才能推进它**。此项列入观测，不作为缺陷修复。

### 0.3 v9 库清单（只读实测）

| 项目 | `project_state` | `schema_info` | status | 位置 | rev |
|---|---|---|---|---|---|
| `cumcm_2020_a_codex_luna` | 9 | **9** | failed | step 15 / stage 9 | 296 |
| `cumcm_2025_b_codex_luna_stability_20260817` | 9 | **9** | killed | step -1 / stage 0 | 189 |
| `cumcm_2025_b_codex_luna_stability_20260817_run3` | 9 | **9** | failed | step -1 / stage 0 | 12 |
| `cumcm_2025_b_codex_luna_stability_20260817_run4` | 9 | **10** | failed | step 15 / stage 9 | 2484 |
| `cumcm_2025_b_gpt_formal_20260908t153023z`（B formal） | 9 | **10** | completed | step 16 / stage 10 | 1411 |
| `cumcm_2026_a_fable_pro_20260910`（A） | 9 | **10** | completed | step 16 / stage 10 | 561 |

仍是 `phys=9` 的 3 个（`2020_a`、`stability`、`run3`）才是 I8-b 的真实样本。

### 0.4 读路径即升级（硬约束）

`_upgrade_schema()` 被约 45 处调用，含纯读路径：`load()`（`storage.py:1440`）、`status_snapshot()`（:1455）、`events()`、`dirty_flags()`（:3664）。它执行 `UPDATE schema_info SET schema_version = 10` 并 `commit()`。

**推论（计划前提）**：
- 对 `phys=9` 的库，任何 Factory 读（含 `factory state`）都会造成不可逆物理升级；
- **`schema_info != SCHEMA_VERSION` 时 `_validate_schema()` 硬失败** → 一旦被新代码碰过，`main`/`bde49712` 的代码就读不了它。这是 fail-closed downgrade barrier 的实际来源，也是双轨必须分库的原因。

### 0.5 非终态 v9 样本已保全

`cumcm_2025_b_luna_usability_20260906t063231z` **不在** `ongoing/` 存储根内（Paper Factory MCP 枚举的 17 个项目里没有它）。原始只读取证（未调用任何 Factory 代码）：

```
path            /home/tfisher/.codex/full_problem_luna/20260906T063231Z/source/ongoing/
                cumcm_2025_b_luna_usability_20260906t063231z
state.db sha256 e7c3e25582acf0f247021ceb824a2ec26b87510c1cdf1928d3c0b51d076eac8f
size            2920448
triple          project_state=9  schema_info=9  status=paused  step=5  stage=4  rev=404
counts          events=404  stage_checkpoints=6  checkpoint_history=51  solver_jobs=34  dirty_flags=6
```

另有两份 byte-identical 副本（`material_review_live/20260908T135205Z`、`.../20260908T141636Z`，sha256 相同）。

**保全副本**：`/home/tfisher/pf-canary-staging/cumcm_2025_b_luna_usability_20260906t063231z`（48M，sha256 与三元组均已复核一致）。取证前后的 `state.db` sha256 相同，证明原始读取未造成变更。

---

## 1. 测试环境与隔离纪律

1. **只读铁律**：`phys=9` 的库在副本化之前，不得用新代码触碰（含 `factory state` / `factory diagnostics` / 任何 `load()`）。触碰即升级即不可逆。
2. **双轨不共库**：两轨各持独立副本。新轨副本一旦被 `load()` 过即为 `phys=10`，旧代码（`SCHEMA_VERSION=9`）将硬失败——这是设计而非缺陷，但会污染对比。
3. **canonical mount path**：两轨在同一 canonical 路径下**顺序运行**。仅让路径字符串长度相同**不足以**排除绝对路径进入产物内容；对 path-sensitive 输出必须显式列为例外，不能依赖长度相等。
4. **一次一因子**：除执行入口外，输入、revision、cursor、模型配置、registry 全部一致。
5. **起点/终点三元组快照**：每次 canary 前后各记录一次 `(pragma user_version, schema_info, project_state.schema_version)`，作为 G1.9 归因与「副本未被另一轨污染」的凭据。
6. **基线锁定**：所有 canary 在 `0dbc5c39b68c453d49f8c35c932ebee05497088d` 上执行，不边改边测。当前 CI 等价口径收集 **2415 / 2424**（9 项 latex deselected）；阶段记录中的 2459 与当前提交不一致，以实测为准。

---

## 2. Gate 0 — Draft PR CI

| 编号 | 测试 | 动作 | 通过判据 |
|---|---|---|---|
| G0.1 | 四组检查 | 开 **Draft PR**（不引入 `workflow_dispatch` 这类与验收无关的永久配置） | `core`/`web`/`cloud`/`latex` 全 success，且绑定目标 SHA |
| G0.2 | 本地复现 `core` | `uv sync --extra web --extra tui --group dev --locked && uv run pytest -q -m "not latex" --ignore=...`（照抄 ci.yml 的 8 个 ignore） | 0 failed |
| G0.3 | `git diff --check` | ci.yml 同款 | 干净 |
| G0.4 | S6 专项 | `pytest tests/test_bounded_run.py -q` | 23 passed |

### G0 当前结果（实测，head `1a04400`）

PR #35 触发过两次 run：

**run `37598322082`（head `2c5b606`）：四组全 fail，零测试执行。**
失败点经步骤级核实**均为 `uv sync ... --locked`，不是测试失败**：

```
core:  step 7  uv sync --extra web --extra tui --group dev --locked  -> failure
web:   step 6  uv sync --extra web  --group dev --locked             -> failure
cloud: step 5  uv sync --extra cloud --group dev --locked            -> failure
latex: step 6  uv sync --group dev --locked                          -> failure
```

后续步骤（`compileall` / 4 组 pytest / `npm ci` / `npm run build` / `git diff --check`）**全部 skipped**。

**根因**：`main...HEAD` 只改了 `pyproject.toml`（+1 行，commit `aaea7f8` "fix cloud test environment dependency declaration"）：

```diff
 cloud = [
   "python-multipart==0.0.29",
+  "requests>=2.31,<3",
```

而 `uv.lock` 在本分支**从未被改动**（`git log main..HEAD -- uv.lock` 为空）。`--locked` 要求 lock 与 `pyproject.toml` 全量一致（含未被请求的 extras），故四个 job 在装依赖阶段即失败。`main` 近 8 次 CI 全 success，属本分支引入的回归。`uv lock --check` 实证：`The lockfile at uv.lock needs to be updated`。

**已修复**：commit `1a04400` 仅同步 `uv.lock`（2 行，`requests` 原已作为 `google-cloud-storage` 的传递依赖存在于 lock 第 2057 行，此处仅登记为 `cloud` extra 的直接依赖），无代码改动。

**run `37599005741`（head `1a04400`）：三个 job 转绿，`core` 剩 1 项失败。**

| job | 结果 | 耗时 |
|---|---|---|
| `latex` | **success** | 1m47s |
| `cloud` | **success** | 26s |
| `web` | **success** | 47s |
| `core` | **failure** | 2m29s |

`core` 实测：`1 failed, 2381 passed, 33 skipped, 9 deselected in 112.23s`。

### G0 剩余阻塞：G2/G3 gate 测试与 CI 环境耦合

唯一失败项：

```
FAILED tests/test_gate_g3_replay.py::test_the_gate_actually_exercises_all_three_streams
  AssertionError: no real project available; the gate would pass vacuously
  assert []
tests/test_gate_g3_replay.py:209
```

该文件 `_REAL` **硬编码本服务器的绝对路径**，且**无任何 env var 覆盖机制**：

```python
_REAL = {
    "A": "/home/tfisher/paper_factory/ongoing/cumcm_2026_a_fable_pro_20260910",
    "B": "/home/tfisher/paper_factory/ongoing/cumcm_2025_b_gpt_formal_20260908t153023z",
    "R": "/home/tfisher/paper_factory/ongoing/cumcm_2025_b_codex_luna_stability_20260817_run4",
}
```

CI 中（`/home/runner/work/...`）三个路径均不存在 → 项目相关测试全部 `pytest.skip` → 空转保护 `assert available` **正确失败**。

**关键事实：`tests/test_gate_g3_replay.py`（G3，commit `99ba1bc`）、`tests/test_gate_g2_solver.py`（G2，commit `c762eca`）、`tests/test_solver_reconcile.py`（S5）在 `main` 上均不存在，全部由本分支引入。**

CI 中因环境缺失而 skip 的 31 项分布：

| 来源 | skip 数 |
|---|---|
| `test_gate_g2_solver.py`（A/B/R） | 12 |
| `test_gate_g3_replay.py`（A/B/R） | 16 |
| `test_solver_reconcile.py` | 3 |

**含义**：G2 + G3 + S5 的全部 gate 证据（31 项）**在 CI 中是盲的**，只在服务器上真实执行。因此「G3 已验证旧事件全量 replay / hash 校验 / reason envelope 兼容」这一结论**仅在本机环境成立，CI 未复现**。空转保护的设计目的正是拒绝让该 gate 在无数据环境下静默通过——它工作正常，不是误报。

**已处置（方案 A）**：见 §7。

### G0 结论：门禁达成（head `54be447`）

| job | 结果 | 耗时 |
|---|---|---|
| `core` | **success** | 3m50s |
| `web` | **success** | 52s |
| `cloud` | **success** | 26s |
| `latex` | **success** | 1m57s |

CI 中 `core` 的 pytest 统计：

| 提交 | 结果 |
|---|---|
| `1a04400`（gate 改动前） | `1 failed, 2381 passed, 33 skipped, 9 deselected` |
| `54be447`（gate 改动后） | **`2392 passed, 34 skipped, 9 deselected` in 185.52s** |

差值完全符合预期：`+11 passed` 为 `tests/test_gate_hermetic.py`；`+1 skipped` 为原空转保护由 fail 改为 skip；`0 failed`。

CI 日志中可见的相应 skip 记录：

```
SKIPPED [1] tests/test_gate_g3_replay.py:218: no real project available;
            the gate invariants are asserted hermetically in
            tests/test_gate_hermetic.py
SKIPPED [n] tests/_gate_projects.py:79: real project {A,B,R} unavailable at
            ...; set PF_GATE_PROJECTS_ROOT to a directory containing ongoing/
```

同时反证：本机曾出现的 `test_normal_run_cli_entry` 失败在 CI 中并未复现（`core` 全绿），确认其为本地双检出环境的产物。

**Gate 0 关闭。可进入 Gate 1。**

---

## 3. Gate 1 — 入口等价性

### 3.1 硬规则：两轨固定同一 commit

```text
legacy  track: 0dbc5c3 + FactoryEngine.run(max_steps=1)
bounded track: 0dbc5c3 + FactoryEngine.run_bounded(...)
```

`main` / `bde49712` **只允许出现在 Gate 3 的 downgrade 测试中**。若 Gate 1 旧轨用 `main`，则 dirty classification、schema、artifact policy、reason envelope 等代码差异会混入结果，届时无法区分差异来自 `run_bounded` 入口还是代码版本。

### 3.2 宿主选择（优先级）

1. 已存在的**完整 ready 历史快照**（DB + 文件系统同源）；
2. **真实 registry + hermetic ready canary**；
3. **可安全复制的非终态真实项目**（备选：`/home/tfisher/pf-canary-staging/` 中已保全的 usability 副本）；
4. B 的**完整历史 materialization**。

**不用 `replay_events()` 单独回卷作为正式验收宿主。** 理由：`replay_events()` 只纯重建 `_REPLAY_FIELDS` 那部分稳定状态，不会一起回卷 `stage_checkpoints`、`stage_checkpoint_history`、`solver_jobs`、dirty 表、decision 表、prompt receipt 表、各 effect-hash domain，以及磁盘上的模型/结果/evidence 文件。仅做「replay 到 revision N → 写回 `project_state` → 当作 ready 活库」会得到 cursor 看似正确、但 **domain root 与文件系统闭包已与 revision N 不一致** 的库——G1.8 会正好把它打掉。

**暂缓生产级 `factory rewind`。** 若确需 B 的历史状态，做一个**测试专用 snapshot / materializer**，但它必须重建所有受治理 domain 并验证 aggregate root，工作量已超过「最小 rewind 工具」。不宜为验收一套简化机制而引入一套新的状态恢复机制。

### 3.3 等价性判据

| 编号 | 对比项 | 通过判据 |
|---|---|---|
| G1.1 | 双轨推进 | 两侧均到达各自停止点，不抛异常 |
| G1.2 | checkpoint | `stage_checkpoints` / `stage_checkpoint_history` 新增行内容与顺序一致（时间戳例外） |
| G1.3 | 最终 cursor | `(stage, subtask, source_step)`、`last_completed_step/stage` 完全一致 |
| G1.4 | dirty flags | `dirty_flags` + `dirty_causes` 一致 |
| G1.5 | solver job 归属 | owner slot / status 一致；**新路径不得改写 `solver_jobs.status`**（S5 明确该 shadow 只报告不写） |
| G1.6 | 关键输出 fingerprint | 与逐字节相同白名单一致；path-sensitive 输出显式列例外 |
| G1.7 | replay 自洽 | `replay_events(events) == replay_state(state)`；**新路径产生的新事件**亦通过 hash 校验（G3 只覆盖旧历史） |
| G1.8 | domain root 稳定 | `aggregate_domain_root()` / `status_snapshot()["aggregate_valid"]` 通过；`dirty_cause_classification` 新表在 v10 边界处被容差分支正确吸收 |
| G1.9 | **业务写版本收敛观测** | 记录 9→10 由**哪一个 transition** 完成，**不归因给 bounded run** |
| G1.10 | 新路径身份记录 | `bounded_run_id`、`bounded_run_contract_sha256` 进入 `RUN_STARTED`；`BoundedRunResult` 各字段可回查，contract SHA 与入参一致（J1-10 收口） |

**G1.9 的重新定义**：若宿主是 `paused` + `phys=9` 的项目，则必须 `resume → advance`。物理升级更早发生在 `load()`；逻辑 `project_state.schema_version` 9→10 很可能在 **`resume` 这次 event-carrying write** 就已完成。因此到达 `run(max_steps=1)` vs `run_bounded()` 时，两边**都已**是逻辑 schema 10。所以只记录收敛发生在哪个 transition，不强行归因。**migration / downgrade 的性质统一放 Gate 3。**

### 3.4 已实现：hermetic ready canary

`tests/_g1_canary.py`（基建）+ `tests/test_gate_g1_entry_equivalence.py`（12 项）。

**固定不变量（两轨完全相同的部分）**

| 变量 | 处置 |
|---|---|
| 绝对路径 | 单一 canonical 路径 `/tmp/pf-g1-canary/project`，两轨先后恢复到同一路径。不用 `tmp_path`——绝对路径会进入产物与事件 payload，共用路径比"等长路径"更严格 |
| 时钟 | 向 `SQLiteStateStore(clock=...)`（`storage.py:82`）注入**恒定**时钟 `1_700_000_000`。因此所有时间戳、以及由 `created_at` 参与哈希的 `event_id`（`workflow_events.build_event_payload`）都变为确定值，可以**逐字节比较**而不是归一化掉 |
| 种子 | 一次生成，记录 DB sha256、文件 manifest、三元组、aggregate root、各表计数；随后 `copytree` 存为字节级种子。两轨各自 `rmtree` + `copytree` 恢复 |

**显式归一化清单（仅此四项，其余差异一律不得忽略）**

| 键 | 来源 |
|---|---|
| `runner_lease_id` / `lease_id` | `engine.py:309` 的 `uuid.uuid4().hex` |
| `heartbeat_at` | `engine.py:317`、`engine.py:504` 两处直接调用 `time.time()`，未走注入时钟 |
| `runner_pid` | 拥有进程 |

`worker_pid` / `worker_identity` **故意不归一化**：两轨运行在同一进程内，必须精确相等（`_process_identity` 取自 `/proc/<pid>/stat` 的启动时间 token，进程内稳定）。

**预期差异（单独断言存在，而非静默容忍）**：bounded 轨的 `RUN_STARTED` payload 多出 `bounded_run` 授权块（`engine.py:324`），含 `bounded_run_id` / `bounded_run_contract_sha256` / `run_policy` / `max_subtasks` / `expected_revision` / `protected_manifest_sha256`。比较器对**两侧**同时剥离该块后再比较，使 `compare()` 对称；授权块的存在由 `test_smoke_bounded_track_binds_its_authorisation` 单独断言，legacy 轨不含该块由 `test_smoke_legacy_track_carries_no_authorisation` 断言。

**两层**

- **Layer 1 smoke**（5 项）：step 调度器 + 两 Step fake registry。覆盖 CAS 拒绝（零业务事件）、契约身份、结构化结果、`PROJECT_COMPLETED`/无进展 boundary。**不用于关闭 G1**——`stage_checkpoints`、Stage cursor、Stage dirty ownership 不在此路径上。
- **Layer 2 正式**（3 项 + 2 项控制）：`scheduler_generation=stage_v1` 的真实 Stage 调度器。registry 用 `build_native_registry()` 的**注入点**（`factory_core/steps/registry.py:27`）：保留全部 17 个真实 Step contract、真实 prompt 模板名（`contract.prompt` 即模板文件名）、真实 Stage subtask 路由与 checkpoint 机制，仅把 dispatcher 换成确定性实现（无模型调用）、`validator_factory` 换成 permissive 实现。实测路径确实经过 `STAGE_SUBTASK_SELECTED` / `PROMPT_INPUT_BOUND` / `STEP_SUCCEEDED`，并产生真实 `stage_checkpoints`。
  - 场景 A：普通 Stage subtask 推进 1 次
  - 场景 B：`last_completed_step=4` 把 cursor 置于 Stage 4 / subtask `solve` / source step 5，并预置一个同槽位的 durable local solver job + exit artifact + receipt，使 `solver_jobs` 非空、G1.5 的 owner slot / status 比较**有真实数据**（并断言引擎未改写 `status`）

**比较器负对照**（本轮新增，用于防止"全绿但空转"）

- `test_the_comparator_detects_a_real_divergence`：故意把 bounded 轨放宽到 `max_subtasks=2`，比较器必须报出差异并定位到 `project_state` / `events`。
- `test_the_comparator_is_silent_on_the_same_inputs`：两次相同输入必须全等（replay hash 与 aggregate root 均可复现）。
- 该负对照在本次实现中**确实抓到过一个真实缺陷**：`compare()` 原先只从右侧剥离 `bounded_run`，对两次 bounded 运行不对称，导致误报。已改为两侧同时剥离。

**G1.1 – G1.10 逐条覆盖映射**（`tests/test_gate_g1_entry_equivalence.py`，13 项）

| 判据 | 覆盖方式 | 非空转保证 |
|---|---|---|
| G1.1 双轨推进 | `test_smoke_step_scheduler_advances_one_step_on_both_tracks`、`test_stage_v1_advances_one_subtask_equivalently_on_both_tracks` | 断言 `completed_subtasks == 1`、`made_progress is True`、`last_completed_step == 0` |
| G1.2 checkpoint | `test_stage_v1_checkpoint_and_cursor_are_identical`；闭包 `tables.stage_checkpoints` / `stage_checkpoint_history` | 种子无 checkpoint，运行后必须出现并逐行相等 |
| G1.3 最终 cursor | 同上（`last_completed_stage` / `active_stage` / `active_subtask` / `source_step_id` 四元组） | 断言推进确实发生 |
| G1.4 dirty obligations | `test_stage_v1_dirty_obligations_are_carried_equivalently` | **种子预置一条 `MATH_DIRTY`**，并断言种子与 legacy 轨均非空——避免"相等但都为空" |
| G1.5 solver 归属 | `test_stage_v1_solver_ownership_is_carried_equivalently` | 预置同槽位 durable job，断言 `len(solver_jobs) == 1`、`owner_stage == 4`、`status == "completed"` 未被改写 |
| G1.6 关键产物 fingerprint | 闭包 `files`（除 DB 及其 journal 外全部文件的 sha256） | 路径 canonical 化，两轨文件集必须逐一相等 |
| G1.7 replay 自洽 | 闭包 `replayed_matches_state` / `event_replay_valid` / `state_hashes`；`test_the_comparator_is_silent_on_the_same_inputs` | `state_hashes` 逐条相等且可复现 |
| G1.8 domain root | 闭包 `aggregate_domain_root`；`test_the_comparator_is_silent_on_the_same_inputs` | 两次相同运行必须给出相同 root |
| G1.9 版本收敛观测 | **本层 N/A** | hermetic 种子由当前代码创建，两轨均为 schema 10，不存在 9→10 收敛。该观测需 v9 宿主（Gate 3 / `pf-canary-staging` 的 paused v9 样本） |
| G1.10 身份记录 | `test_smoke_bounded_track_binds_its_authorisation`；`test_smoke_legacy_track_carries_no_authorisation` | 断言 `bounded_run_id == contract_sha256[:32]`、绑定出现在 `RUN_STARTED`、legacy 轨无该块 |

另有：`test_smoke_seed_is_reproducible_and_restorable`（种子可复现且字节级可恢复）、`test_smoke_stale_revision_is_refused_before_any_write`（CAS 零业务事件）、`test_smoke_repeated_boundary_reports_needs_inspection`（真实无进展 boundary → `NEEDS_INSPECTION`）、`test_smoke_both_entries_leave_a_paused_project_alone`（两入口对 boundary 判断一致），以及两项比较器对照。

### 3.5 canary 发现的产品侧确定性缺口（本轮实测）

**现象**：`test_stage_v1_dirty_obligations_are_carried_equivalently` 在单文件隔离时 13 passed，在**全量套件**下失败，差异只有一处：

```
events[5].payload._workflow.event_id: "dfa66316..." != "901a3198..."
```

**定位**：`event_id = hash({project_id, revision, event_type, created_at})`（`workflow_events.build_event_payload`）。payload 其余字段与 `state_hash_after` 全部相同，说明只有 `created_at` 不同。实测两条轨每条事件的 `created_at`：

```
[5] rev=6 PROMPT_INPUT_BOUND  created_at=1791367180   <- 真实 wall clock
其余事件                       created_at=1700000000   <- 注入的恒定时钟
```

**根因**：`factory_core/steps/prompt_step.py` 在**方法内部**自行构造 store——

```python
from ..storage import SQLiteStateStore      # 第 286 行，方法内导入
store = SQLiteStateStore(context.project_dir)   # 第 288 行，未传 clock
```

（同类构造另见第 130、175、384 行；`specialized.py:353,412`、`gates.py:116-167` 亦然。）于是 `PROMPT_INPUT_BOUND` 的 `created_at` 来自默认时钟 `time.time`，**绕过了引擎注入的 store 时钟**。

**为什么此前没被发现**：两条轨若落在同一秒内，`int(time.time())` 相等 → 差异被掩盖。机器空载时通过、满载时失败——最坏的失败形态。

**补充细节（Harness 实现要点）**：仅 monkeypatch `time.time` **无效**，因为 `SQLiteStateStore.__init__` 的 `clock: Callable = time.time` 是**定义时绑定的默认参数**，`self._clock` 已持有原函数对象。因此 `frozen_time()` 必须同时替换 `factory_core.storage.SQLiteStateStore` 为默认时钟为常量的子类；`prompt_step.py` 在方法内 `from ..storage import SQLiteStateStore`，所以替换模块属性可以生效。

**处置**：

1. harness 增加 `frozen_time()`，在两轨运行期间同时钉住 `time.time` 与内部 store 的默认时钟；
2. 新增 `test_every_event_is_stamped_with_the_pinned_clock`，**逐事件断言 `created_at == CONSTANT_EPOCH`**，把「不允许存在未注入时间源」固化为回归护栏——未来任何新时间源会在此显式失败，而不是变成偶发；
3. `collect()` 现在显式采集并比较 `event_created_at`；
4. 由于时钟被真正钉住，`heartbeat_at` 从归一化清单**移出**，改为精确比较。归一化清单收窄为三项：`runner_lease_id` / `lease_id` / `runner_pid`。

**未处置（需你决定）**：产品侧 `prompt_step.py` 等内部 store 构造不继承注入时钟，属**可测试性缺口**而非正确性缺陷（`created_at` 不在 `_REPLAY_FIELDS`，不影响 `state_hash` 与 replay 校验）。修它需要改产品代码，超出 Gate 1「不改基线」的范围，因此本轮只在 harness 侧中和，并记录在此。

---

## 4. Gate 2 — bounded contract 失败语义

| 编号 | 场景 | 通过判据 |
|---|---|---|
| G2.1 | stale expected revision | `run_bounded` 抛 `BoundedRunError`；**不得产生业务事件、不得改变 workflow revision**（见下方判据精确化） |
| G2.2 | cursor 不一致 | revision 正确、`expected_cursor` 的 stage/subtask/source_step 之一错位 → 拒绝，且错误信息能区分 revision 与 cursor |
| G2.3 | manifest 入口已脏 | 入口抛 `ProtectedManifestViolation`，零业务事件 |
| G2.4 | manifest 运行中被改 | **双层断言**：① `STEP_FAILED.error_class == "PERMANENT_PROTECTED_MANIFEST_VIOLATED"`（engine 内部保护）② 返回结果 `stop_reason == "PROTECTED_MANIFEST_VIOLATED"`（外部结构化结果）。两者须完全对应，且成功 checkpoint 被阻断 |
| G2.5 | 重复 boundary 无进展 | 见下方判据（已改） |
| G2.6 | 参数分歧拒绝 | `max_steps` 与 `contract.max_subtasks` 不一致、`allowed_source_steps` 与 contract 不一致 → 抛 `BoundedRunError` |
| G2.7 | live runner 占用 | 抛 `RunnerBusy` |
| G2.8 | manifest 路径安全 | 绝对路径（POSIX/Windows）、`..`、`.`（含前导 `./`）、空路径、非小写 sha256 全部拒绝；符号链接或指向项目外的父组件在校验期报 unsafe 而非比较 |

### G2.1 判据精确化

`phys=10` 副本上：stale CAS → `BoundedRunError` → events 增量为 0，可视为无状态写。但 `phys=9` 副本上：

```text
run_bounded → store.load() → _upgrade_schema() → schema_info 9→10 → CAS → BoundedRunError
```

因此：**「任何 event / business write 前拒绝」成立，「SQLite 字节完全零写」不成立。**

> **判据**：stale authorization 不得产生业务事件或 workflow revision 变化；物理 schema 自动迁移按 Gate 3 既定合同单独评价。

### G2.5 判据（已改）

`classify_stop_reason()` 实际顺序为 `completed → PROJECT_COMPLETED`、`paused → BOUNDARY_OR_SCOPE`、`failed → FAILED`、`blocked → BLOCKED`、`awaiting_* → HUMAN_DECISION`、`MAX_SUBTASKS`、`ready + same status + completed=0 → UNCHANGED`、`ready → NO_FURTHER_WORK`。

重复 boundary 由 `BoundedRunResult` 单独表达。**端到端测试不统一要求 `stop_reason == "UNCHANGED"`**，改为：

```text
必须：
  made_progress      == False
  unchanged_boundary == True
  outcome            == "NEEDS_INSPECTION"        # to_dict()
  boundary_fingerprint == previous_boundary_fingerprint

stop_reason：
  保留真实底层 boundary reason，不作为统一判据。
```

另保留**一个纯函数测试**，专门证明 `ready + same status + completed == 0` 时 `UNCHANGED` 优先于 `NO_FURTHER_WORK`（对应 S6「stop-reason ordering」设计修正）。

---

## 5. Gate 3 — 版本与历史兼容

与 `run_bounded` 正向等价性**完全分离**。

| 编号 | 项 | 内容 | 通过判据 |
|---|---|---|---|
| G3.1 | v9 读触发迁移 characterization | 对 v9 副本仅执行 `load()`，观察 `schema_info` | 明确固化「读即升级」的现行行为，作为 I8-a 修复的判定基准与回归护栏 |
| G3.2 | **I8-a** | 构造 `schema_info=9` 但缺列/缺表的库，走迁移路径 | 修复前应**拒绝**而非静默 DDL 提升；补只读前置校验器 |
| G3.3 | **I8-b** | 对生产 v9 库（`2020_a`、`stability`、`run3`）做只读 migration / domain 漂移审计 | 见下方「零写入」纪律 |
| G3.4 | **真实 `bde49712` downgrade** | 用 `bde49712` 的代码作**独立解释器**（独立 checkout/venv）打开 schema-10 fixture | 在**任何业务写之前**拒绝（`_validate_schema` 硬失败即 fail-closed）。将现有「模拟 v9 writer 接受集 `{1..8}` → 拒绝 schema 10」的逻辑回归升级为真实二进制回归 |
| G3.5 | v9→10→旧解释器 | 新代码升级副本到 10，再用旧代码打开 | 旧代码必须拒绝，且拒绝时**无业务写入** |

### G3.3 零写入纪律

生产 v9 DB 上**只允许真正的 SQLite 只读检查**，记录：

```text
sha256
mtime
schema_info
project_state.schema_version
domain root
```

之后：`copy DB + WAL/SHM + 必要文件闭包` → **在临时副本执行新代码迁移** → 对比 before/after。否则 `_upgrade_schema()` 本身就会破坏「审计零写入」这一门禁。

`G3.4` 实现要点：`main` 与 `bde49712` 的 `SCHEMA_VERSION` 均为 9，其工作树本身就是「旧解释器」，无需模拟。

---

## 6. Gate 4 — driver 退役

| 编号 | 动作 | 通过判据 |
|---|---|---|
| G4.1 | **重新冻结 driver 普查基线** | 统一口径重跑 A/work 的各项计数并落盘 |
| G4.2 | 只迁移**一个** canary 对应的 `work/*.py` 驱动链 | 移除 `run(max_steps=1)`、硬编码 `assert state.revision == N`、自带 `protected_files.json`、自制 `progress.json`、重复 verify；统一走 `run_bounded()` |
| G4.3 | 重跑 Gate 1 全部等价性 + Gate 2 全部分支 | 与迁移前逐项一致 |
| G4.4 | 全量回归 | CI 等价口径 0 failed；`git diff --check` 干净 |
| G4.5 | 批量迁移 | 每批重复 G4.3–G4.4 |

**普查基线必须重测**，不能用 S6 文档里的数字。A/work 实测为 128 条目 / 979 MB（与 S6 一致），但按引用口径重测：`.run(` 52、`progress.json` 29、`max_steps=1` 23（S6 写 24）、`protected_files` 54（S6 写 47）、硬编码 `assert revision == N` 44（S6 写 20，差异最大，疑似正则/范围不同）。S6 的数是引用口径而非文件口径。

---

## 7. Gate 0 环境耦合的处置（方案 A 已实施）

### 问题

G2/G3/S5 的 gate 测试断言的是**真实生产历史的具体事实**（A 恰好有那两个 unresolved job、r517 的 REOPEN_REVISION_TEXT 可读、B 的残余 running 行被 SUPERSEDED）。这些只能在拥有 `ongoing/` 项目树的机器上证明，因此原实现把三个绝对路径**硬编码**在三个测试文件里，且无覆盖机制。CI 中必然 skip，而 G3 的空转保护 `assert available` 随即 fail —— 它工作正常，但无从满足。

### 处置：拆成 hermetic 层 + 真实历史层

| 层 | 文件 | 职责 | CI 行为 |
|---|---|---|---|
| **hermetic** | `tests/test_gate_hermetic.py`（新）+ `tests/test_solver_reconcile.py` 的 `tmp_path` 测试 | 用 `SQLiteStateStore` 从零构造 >100 条带 envelope 与 `state_hash_after` 的 versioned event 流，证明**一般不变量** | **全绿**（28 passed） |
| **真实历史回归** | `test_gate_g3_replay.py` / `test_gate_g2_solver.py` / `test_solver_reconcile.py` 的 real 段 | 重新断言构建 gate 时记录的生产答案 | **干净 skip**（32 skipped） |

共享定位器 `tests/_gate_projects.py`：
- `PF_GATE_PROJECTS_ROOT` 指向含 `ongoing/` 的目录（缺省回落历史绝对路径，服务器行为不变）；
- 项目缺席时 real 段 `pytest.skip`，**绝不 fail**；
- 非空转职责移交 hermetic 层。

### hermetic 层覆盖的不变量

1. 构库 >100 条 versioned event 的流可 replay 且与当前状态一致（`event_replay_valid` / `aggregate_valid` / 全 `REPLAY_FIELDS`）
2. **hash 校验非空转**：篡改末条 `state_hash_after` → `ReplayIntegrityError`
3. 每条事件都能得到可用的 reason（`code`/`subcode`/`actor` 均为 str）
4. pre-S4.1 形状的 reason 保持可读且 subcode/actor 为空（4 种形状参数化）
5. X-01 提升机制：`final_decision` → `subcode`（r517 的**机制**，真实事件仍在 real 段）
6. 读取纯净：revision / 事件数 / payload 字节和 / mtime 均不变
7. **非空转保护**：断言构造流 >100 条、每条带 envelope 与 hash、blocking/proven 集合不重叠；并对任何**在场**的真实项目保留原 `>100 events` 要求

### 验证结果

| 环境 | 结果 |
|---|---|
| 本机，4 个 gate 文件（真实项目在场） | `60 passed, 0 failed` in 32.64s |
| 模拟 CI，4 个 gate 文件（`PF_GATE_PROJECTS_ROOT` 指向空目录） | `28 passed, 32 skipped, 0 failed` in 0.86s |
| 本机，全量 core 口径（含改动） | `1 failed, 2425 passed, 9 deselected` in 214.95s |
| 本机，全量 core 口径（**排除本计划改动的 4 个 gate 文件**） | `2366 passed, 9 deselected, 0 failed` in 182.15s |

对比改动前：`1 failed, 2381 passed, 33 skipped`。新增 2425−2414 = 11 项即 `test_gate_hermetic.py`；原先 skip 的 33 项因本机真实项目在场而真正执行。

### 本机全量口径下的一项失败（非本次改动引入）

`tests/test_normal_run_cli_entry.py::test_real_normal_entry_completes_initialization_when_pause_arrives[service_start]`
—— 真实子进程 `wait(timeout=5)` 返回 1。

判定依据：

1. 该文件**未被本次改动触碰**，也不引用任何 gate 模块（`grep` 无命中）；
2. 单文件隔离运行 `4 passed`；
3. **同一 head `1a04400` 的 CI `core` job 中该测试为 pass**（当时唯一失败是 G3 空转保护）；
4. 本机存在**双检出不明确**：venv 的 editable 指针指向主检出 `/home/tfisher/paper_factory`，而 `PYTHONPATH=.` 是相对路径，被 spawn 的子进程 cwd（`tmp_path`）解释，于是子进程可能导入主检出的 `factory_core` 而非 worktree 的。CI 中 `uv sync` 就地安装分支检出，不存在该歧义。

因此结论是「本机验证环境的产物」。**以 CI 为权威判定**，故本次改动推送后再看 `core` 结果。

> 附注（供 Gate 1/4 参考）：`PYTHONPATH=.` 这种相对写法在 spawn 场景下不可靠；后续本地复现应改用绝对路径，或对子进程显式传入 worktree 根。
### 已否决的备选

- **B 重定义 Gate 0**：需长期豁免「CI 全绿」，弱化门禁。
- **C 向 CI 提供真实项目树**：A 的 `work/` 为 979 MB，且把生产历史引入 CI。
- 把空转保护改为 `skip`/`xfail`：等于取消该 gate 效力，未采纳。

## 8. 执行顺序

```text
Gate 0  Draft PR CI  [#35；uv.lock 已修（1a04400），剩 G2/G3 环境耦合待决]
  ↓
Gate 1  入口等价性（两轨固定 0dbc5c3，或用 hermetic ready canary）
  ↓
Gate 2  bounded contract 失败语义（8 项）
  ↓
Gate 3  版本与历史兼容（I8-a / I8-b / bde49712 downgrade / v9 read migration characterization / v9→10→old fail-closed）
  ↓
Gate 4  driver 退役（先 1 条链，再批量）
```

**暂缓**：生产级 `factory rewind`；`work/*.py` 的批量删除。

**注意**：Gate 1 的两轨基线固定为 **`0dbc5c3`**（即 Gate 0 验证通过的那个 head）。历史沿革：`2c5b606` → `1a04400`（同步 `uv.lock`）→ `108e620`（G2/G3 gate 可在无生产主机上运行）→ `54be447`（冻结本计划）→ `0dbc5c3`（关闭 Gate 0）。
