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

### 4.1 已实现：bounded contract 失败语义

`tests/test_gate_g2_failure_semantics.py`（23 项），复用 Gate 1 的 harness 与 hermetic Stage 种子。全部为**可执行断言**，无占位。

| 判据 | 承接测试 | 关键断言 |
|---|---|---|
| G2.1 stale revision | `test_a_stale_authorisation_is_refused_before_any_write` | 抛 `BoundedRunError`；`events` 与 `revision` 与运行前**逐项相等** |
| G2.2 cursor 不一致 | `test_a_matching_revision_with_the_wrong_position_is_refused` | 抛错且信息含 `cursor mismatch`，并**断言不含** `expected revision`（可区分） |
| G2.3 manifest 入口已脏 | `test_an_already_violated_manifest_is_refused_at_entry` | 抛 `ProtectedManifestViolation`（`already violated at entry`），零事件 |
| G2.4 manifest 运行中被改 | `test_a_manifest_broken_during_the_run_blocks_the_checkpoint` | **双层**：① `STEP_FAILED.payload.error_class == "PERMANENT_PROTECTED_MANIFEST_VIOLATED"` ② `outcome.stop_reason == "PROTECTED_MANIFEST_VIOLATED"`；另断言入口校验为 `ok=True`（脏是运行造成的）、`stage_checkpoints` 与 history 均为空、cursor 未越过被阻断的 subtask |
| G2.5 重复 boundary | `test_a_repeated_boundary_is_reported_and_needs_inspection` + `test_unchanged_takes_precedence_over_no_further_work` | 前者的四项判据（fingerprint 相同 / `made_progress False` / `unchanged_boundary True` / `outcome == NEEDS_INSPECTION`）**无条件**成立，并断言 `stop_reason` 保留为 `BOUNDARY_OR_SCOPE`；后者为纯函数测试，证明 `ready + same status + completed=0 → UNCHANGED` 优先于 `NO_FURTHER_WORK` |
| G2.6 参数分歧 | `test_max_steps_may_not_disagree_with_the_contract`、`test_allowed_source_steps_may_not_disagree_with_the_contract` | 抛 `BoundedRunError`，信息分别含 `max_steps disagrees` / `allowed_source_steps disagrees` |
| G2.7 live runner | `test_a_live_foreign_runner_is_not_taken_over` | 抛 `RunnerBusy`；原始行 `runner_pid` / `runner_lease_id` 未被改写，零事件 |
| G2.8 manifest 路径安全 | 8 项路径参数化 + 5 项 digest 参数化 + 符号链接 | 绝对路径（POSIX / UNC / 盘符）、`..`、嵌套 `..`、前导 `./`、内部 `.`、空路径、大写/长度错/非 hex 的 sha256 全部在**构造期**拒绝；直接符号链接与父组件符号链接解析到项目外，均在 `verify_protected_manifest` 报 `unsafe` 而非比较 |

**实现中修正的两处测试自身缺陷**（记在此处以备复查）：

1. **G2.7 最初用 PID 1 作为「存活的他人 runner」**，但 `_pid_is_live` 是 `os.kill(pid, 0)` 加 `except OSError: return False`；对属 root 的 pid 1，该调用抛 `PermissionError`（`OSError` 子类）→ 被判为**不存活** → 引擎按「runner 已中断」继续推进，`RunnerBusy` 根本不会触发，测试实际在跑真实步骤。已改用**本进程派生的同用户子进程**（`subprocess.Popen`），并在 `finally` 中回收。
2. **G2.7 最初用 `collect()` 断言 `runner_pid`**，而 `collect()` 按设计把它归一化为 `<normalised>`，断言必然失败。已改为读取原始 `store.load()`；归一化后仍用于「事件未变」的比较。

**G2.4 的可达性说明**：pre-commit 保护位于 `engine.py:1194 _complete_stage_task`，属 **Stage 路径**，因此必须用 `stage_v1` + `build_native_registry` 才能走到；两步 fake registry 走不到此处。断言的构造方式是由 hermetic dispatcher 的 `on_execute` 钩子在**步骤执行期间**改写受保护文件——这正是事后调用方自检无法覆盖的窗口。

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

### 5.1 已实现：版本与历史兼容

`tests/test_gate_g3_version_compat.py`（19 项）。

**关键设计：夹具是"生成"的，不是"借来"的。** `bde49712` 是最后一个 `SCHEMA_VERSION = 9` 的提交，因此用 `git archive bde49712 factory_core` 取出它的 `factory_core`，再用**同一个 venv**、`PYTHONPATH` 指向该树、**cwd 设为中立目录**运行，就得到一个真正的"旧解释器"。

> 踩过的坑：`python -c` 会把 **cwd 放在 `sys.path` 首位**，所以若从仓库 cwd 运行，`PYTHONPATH` 会被本分支的 `factory_core` 压过——实测第一次就拿到了 `SCHEMA_VERSION = 10`，等于在拿新代码断言新代码。测试里已用中立 cwd 并在 docstring 中说明。

因此 v9 库与真实 downgrade 都能在 **CI 中复现**，而不是只在有生产库的机器上 skip（CI 的 `core` job 用 `fetch-depth: 0`，历史可用）。

| 判据 | 承接测试 | 关键断言 |
|---|---|---|
| G3.1 读触发迁移 | `test_a_read_migrates_the_physical_schema_and_not_the_event_stream` | 实测三元组 `(user_version 0, schema_info 9→10, project_state 9→9)`；events 与 revision 不变；v10 侧表 `dirty_cause_classification` 由无变有 |
| G3.1 各读路径 | `test_every_read_path_triggers_the_migration`（`load` / `status_snapshot` / `events` / `dirty_flags` 参数化） | **四条读路径都会迁移**——所以任何 Factory 读都不能用来检查原始 v9 库 |
| G3.1 首次真实写入收敛 | `test_the_first_genuine_write_converges_the_state_generation` | 读之后 `project_state` 仍为 9；`transition()` 之后才变 10 |
| **G3.2 I8-a** | `test_a_v9_database_missing_a_required_table_is_refused_before_any_ddl`、`test_a_v9_database_missing_a_required_column_is_refused_before_any_ddl`、`test_the_v9_contract_matches_what_the_old_code_actually_creates`、`test_the_real_v9_production_databases_pass_the_precheck` | **已实现并收口**，见 §5.3 |
| **G3.3 I8-b** | `test_a_production_database_is_audited_read_only_and_migrated_in_a_copy`（6 库参数化） | 原始库只用原始 SQLite 读（sha256 / mtime / 三元组 / 事件数）；副本内迁移动；**复核原始库 sha256 与 mtime 均未变** |
| **G3.4 downgrade** | `test_the_old_code_refuses_a_schema_10_database` + `test_the_old_interpreter_is_really_the_old_schema_version` | 旧代码报 `unsupported workflow schema 10`；DB sha256 未变。后者是**防空转守卫**：若旧解释器哪天解析成 10，首个测试即失效 |
| **G3.5 v9→10→旧解释器** | `test_v9_migrated_then_opened_by_the_old_code_fails_closed` | 迁移后旧代码拒绝，且 sha256 与全部 raw 事实不变（fail-closed，零业务写） |
| 样本保全 | `test_the_preserved_v9_specimen_still_matches_its_recorded_digest`、`test_the_specimen_migrates_the_same_way_on_a_copy` | 样本 sha256 恒为 `e7c3e255…`；迁移只在副本内发生，且断言运行后样本 sha256 仍未变 |

### 5.2 Gate 3 实测发现：一个早于 versioned envelope 代次的生产库

I8-b 审计过程中，`event_replay_valid` 断言在 `cumcm_2020_a_codex_luna` 上失败。A/B 对照后确认**不是迁移造成的**，而是该库的事件流本身不带 versioned replay envelope：

```
ReplayIntegrityError: event stream contains no versioned replay snapshot
```

6 个生产库的 envelope 覆盖率实测：

| 库 | 物理版本 | 事件数 | 带 envelope | 首个 envelope revision |
|---|---|---|---|---|
| `cumcm_2020_a_codex_luna` | 9 | 296 | **0** | — |
| `stability_run2` | 9 | 189 | 189 | 1 |
| `stability_run3` | 9 | 12 | 12 | 1 |
| `stability_run4` | 10 | 2484 | 2484 | 1 |
| `formal_2025b` | 10 | 1411 | 1411 | 1 |
| `cumcm_2026_a` | 10 | 561 | 561 | 1 |

即：**恰好一个**生产库（296 条事件，0 条带 envelope）早于 versioned-event 代次，其余全部自 revision 1 起完整覆盖。因此 replay 与 aggregate-root 契约对该库**不适用**——这与"校验失败"是两回事，审计据**数据**分支而非一刀切：`enveloped == 0` 时断言 `event_replay_valid is False`（并确认流非空），`enveloped > 0` 时才断言其为 `True`。静默 skip 会把这一区分藏起来，故不采用。

**影响**：该库的审计链无法被 replay 校验（设计使然）。它不应作为任何"旧事件可全量 replay"结论的证据——G3 阶段的 replay 结论本就只基于 A/B/R（三者均完整带 envelope）。

### 5.3 I8-a 已实现：v9 → v10 前置结构合同

**范围**：只定义**一份** v9 → v10 的前置结构合同，不建立 v1–v10 的逐代 schema 清单。理由：`bde49712` 就是真实 v9 解释器，而 9 → 10 迁移按设计**只新增 `dirty_cause_classification` 这一侧表**，现有 v9 表结构不应被修补。因此当 `schema_info=9` 时，迁移前完全可以要求"这个库至少真的是一个完整 v9"。

**产品代码 diff（窄）**：`domain.py` +4 行（`SchemaPreconditionError(FactoryCoreError)`）、`storage.py` +122 行（两份常量 + 只读 validator + 调用点）。

```python
if current not in {1, 2, 3, 4, 5, 6, 7, 8, 9}:
    raise RuntimeError(f"unsupported workflow schema {current}; expected {SCHEMA_VERSION}")
# I8-a: generation 9 is the one generation where the upgrade boundary is a
# known, frozen contract, so a database claiming it is checked before any DDL
# runs.  Earlier generations keep the historical path.
if current == 9:
    self._validate_v9_pre_upgrade_schema(connection)
connection.execute("BEGIN IMMEDIATE")
```

`_validate_v9_pre_upgrade_schema` 只执行 `sqlite_master` 与 `PRAGMA table_info(...)`，**全程不写数据库**，且位于 `BEGIN IMMEDIATE` 之前——所以拒绝时文件保持字节不变。失败抛 `SchemaPreconditionError("schema 9 database is structurally incomplete: missing table X")`。

**范围边界**：
- `current == 9` → 走新预检；
- `current < 9` → 保持历史迁移路径（早年代次本来就允许迁移器建表补列）；
- `current == 10` → 继续走既有 `_validate_schema()`。

**两层判据**：
- 第一层：19 张表必须全部存在。该集合**由 `bde49712` 真实生成 fixture 的 `sqlite_master` 导出，不是凭记忆列**——覆盖你列的 16 张，另加 `project_config`、`dirty_classifier_rebases`、`prompt_attempt_inputs` 这三个由 helper 创建、参与既有 domain / receipt 语义的表。
- 第二层：仅 5 张表的关键列（`project_state` / `events` / `stage_checkpoint_history` / `dirty_causes` / `solver_jobs`）。**不冻结**每个 TEXT/INTEGER 类型、索引 SQL 与 nullable 属性——I8-a 要防的是"声称自己可以从 v9 升级、实际已缺失关键历史结构"的库；逐字节比对 `sqlite_master.sql` 只会增加无谓的兼容性风险。

**未采用**默认关闭的严格模式开关：这里涉及 schema 升级安全边界，默认关闭会让已确认的缺口继续存在，Gate 3 也就没有真正闭合。

**G3.2 已反转**（这是本次可复查的 diff）：

```text
旧：删 stage_checkpoint_history → load() → 静默重建 → schema_info=10 → 测试记录 gap
新：删 stage_checkpoint_history → load() → SchemaPreconditionError
    → schema_info 仍为 9 → DB sha256 不变 → events/revision 不变 → 该表未被重建
```

**防漂移 oracle**：`test_the_v9_contract_matches_what_the_old_code_actually_creates` 用 `bde49712` 现场生成 v9 fixture，断言 `V9_REQUIRED_TABLES` 与其真实表集合**完全相等**、`V9_REQUIRED_COLUMNS` 为真实列的子集，并断言 validator 接受它。由于 `bde49712` 不可变，这等价于把合同钉在**唯一产生过 v9 的那个实现**上——因此无需维护任何"历史 schema 文档"，只维护当前唯一存在升级边界的一份 v9 precondition。

**真实数据验证**：`test_the_real_v9_production_databases_pass_the_precheck` 对三个仍为 `phys=9` 的生产库（`cumcm_2020_a_codex_luna`、`stability_run2`、`stability_run3`）的**副本**直接调用 validator（只读、不迁移），全部通过；原始库仍只用原始 SQLite 读，事后复核 sha256 与 mtime 未变。

### 6.4 G4.5a：driver inventory v2 + 行为分族 + 迁移规则

artifact：`docs/architecture/runtime_simplification_driver_inventory_v2.json`。

#### 口径歧义已彻底消除（问题的根源是范围不同）

先前"20 对 44""47 对 54"的矛盾**不是漂移，而是两个不同总体**：整个 `work/` 的 212 个 python 文件，与其中真正调用 `.run(` 的文件。两个范围、两种口径全部并列记录：

| 指标 | 范围 A：212 个文件（files / refs） | 范围 B：52 个 `.run(` 文件（files / refs） |
|---|---|---|
| `run_call` | 52 / 65 | 52 / 65 |
| `max_steps=1` | 23 / 23 | 23 / 23 |
| `revision` 断言 | **48 / 52** | **12 / 12** |
| cursor 断言 | 45 / 50 | 21 / 24 |
| status 断言 | 67 / 80 | 22 / 28 |
| `protected_files` | **54 / 113** | **22 / 44** |
| `progress.json` | 29 / 35 | 23 / 28 |
| registry shim | 4 / 13 | 4 / 13 |
| solver 操作 | 28 / 40 | **2 / 2** |
| `allowed_steps` | 3 / 5 | 3 / 5 |

**此后引用任何数字都必须带范围与口径。**

#### 三次收敛：52 → 31 → **20 个真实驱动**

| 步骤 | 数量 | 排除掉什么 |
|---|---|---|
| 朴素 `\.run\(` 正则 | 52 | — |
| 排除备份/框架文件 | 31 | 15 个 `.before.py`/`.original.py` 备份、5 个 framework variant、1 个 framework copy |
| **精确工作流推进检测** | **25** | 再排除 **19 个 `subprocess.run(`**，以及 8 个 `self.run(`/`self.runner.run(`/`self.supervisor.run(`/`release_qN.run(` |
| **真实驱动（去掉 5 个备份）** | **20** | — |

**朴素正则会误报 32 个文件中的 60%**（52 里只有 25 是真推进，20 是真驱动）。退役集合是 **20**，不是 52——前两个数字分别高估了 160% 和 55%。两个 false positive 来源都已分类记录：

- `subprocess.run(` 类 19 个（如 `collect_status.py`：它只是轮询 solver 状态并打印 JSON，**根本不推进工作流**）；
- 普通方法调用 8 个。

| kind（精确集合内） | 数量 |
|---|---|
| `driver` | **20** |
| `backup_copy` | 5 |

#### 行为分族（仅 31 个真实驱动）

| 族 | 数量 | 迁移规则 |
|---|---|---|
| `advance_with_protection` | 7 | 追加 `protected_manifest`（摘要预先算好并冻进合同；**不再把 manifest 写盘**） |
| `recovery_boundary` | 6 | 追加 `previous_boundary_fingerprint`；手写循环改为**若干次显式 bounded 调用**；无进展 → `NEEDS_INSPECTION` 取代自定义停止条件 |
| `registry_limit` | 4 | 追加 `max_attempts_per_step` / `max_reopens_per_step`（已由第一条链证明） |
| `special_business` | 3 | 追加 `allowed_source_steps`；**最后迁移、逐条处理** |
| `pure_advance_wrapper` | **0** | **精确集合下为空**——其成员全是 `subprocess.run(` 误报 |
| `solver_evidence` | **0** | **为空**——其成员是 `specialized.before*.py` 框架备份 |

> **对原计划的两处修正**：你建议优先挑"有 solver/evidence 行为的"和"结构最简单的"各一条。精确检测后 **`solver_evidence` 与 `pure_advance_wrapper` 两族皆空**——solver 工作全在框架代码与非驱动脚本里（28 个文件触及 solver API，2 个是驱动且都是备份），而"最简单"的那条原本是 `subprocess.run(` 误报。**两族都不需要任何合同扩展。**

#### 删除判据（含实测结果）

一条驱动进入退役集合需同时满足：

1. legacy 轨与 bounded 轨语义等价（在版本控制内有测试）；
2. 手写 journal / manifest 已有合同字段承接；
3. **不被其他脚本引用**——见下方实测；
4. 其等价测试进入 CI；
5. 其硬编码状态是否腐烂已记录。**已腐烂的（如第一条）重点验证历史意图已被合同覆盖，不强行复活旧 revision。**

实测的判据 3：

- **被否决的方法**：把驱动的顶层 helper 名在整个 `work/` 里 grep。`save`/`verify`/`sha`/`run` 这类通用名会在无关文件里命中，误报 31 个里的 24 个。已作为**被否决的方法**记录，不作判据。
- **采用的方法**：按**文件路径**检测引用（覆盖按路径 import、`importlib` 载入、以及 `read_text()` 后改写源码三种形态）。结果：**31 个真实驱动中 9 个被其他脚本引用**。
- **新增发现——非驱动 patcher**：`activate_scope_alignment.py`、`prepare_direct_final.py`、`prepare_execution_assets.py`、`prepare_execution_closure.py`、`prepare_m6_native_revisit.py`、`prepare_readonly_output_recovery.py`、`prepare_step11_revisit.py`、`prepare_step12_m6.py` 共 **8 个非驱动脚本会改写驱动源码**（`read_text().replace()` 后写回）。

  **它们不在那 52 个之内**（从不调用 `.run(`），却必须先于/随同其目标一起处置——**退役单元是链，不是文件**。这正是"迁到第 8 个才发现"的那类隐藏耦合，也是本轮 inventory 最大的收获。

#### G4.5b 代表链选择（3–5 条）

| 目的 | 选中 | 理由 |
|---|---|---|
| 最简单 | `math_supplement_evidence_20260912/collect_status.py`（19L） | 该族最小值，验证纯机械替换 |
| 保护 | `continue_adopted_model.py`（48L）| `advance_with_protection` 代表 |
| 恢复/boundary | `run_final_workflow_resume.py`（40L） | 同时具备 `max_steps=1` + revision/cursor 断言 + protected manifest + journal，最完整 |
| **链（替代原 solver 族）** | `run_scope_alignment.py` + `activate_scope_alignment.py` | 验证"驱动 + patcher"链的整体退役 |
| ~~solver/evidence~~ | ~~不存在~~ | 真实驱动中该类为空 |

若这四类不再暴露合同缺口，即可认为合同表面稳定，进入 **G4.5c 批量退役**。

### 6.3 第一条链已迁移并证明等价

`tests/test_gate_g4_driver_migration.py`（5 项）。

**证明方式**：把驱动**自己的机制**与合同的机制放在同一份种子副本上并排跑。

```
legacy 轨：engine.run(max_steps=1) + 驱动原有的 BoundedRegistry shim
migrated 轨：engine.run_bounded(...) + 合同的 max_attempts_per_step / max_reopens_per_step
```

两轨各自从同一份字节级种子恢复，用 Gate 1 的 `compare()` 比较完整语义闭包。**legacy 轨忠实复现了驱动的 shim**——若拿裸 `run(max_steps=1)` 去比，比的是驱动从未做过的事。结果：除 `RUN_STARTED` 的授权块（迁移的目的本身）外**无任何差异**。

驱动的手写物 → 合同对应物：

| 驱动 | 迁移后 |
|---|---|
| `assert status=='ready' and active_step==5`（2 处） | `expected_revision` + `expected_cursor` → `BoundedRunError`，信息可读 |
| 自制 `protected_files.json` | `contract.protected_manifest` |
| 手写 `verify()`（运行前后各一次） | `entry_verification` / `final_verification` |
| 自制 `progress.json` | `BoundedRunResult` |
| 手写两轮循环 + 自定义停止条件 | 一次 bounded 调用 + `previous_boundary_fingerprint` |
| 私有 registry 子类 | 合同的两项上限 |
| 无条件 `protected_files_unchanged: True` | 由校验自动得出 |

**四项产物在迁移后全部不产生**（`progress.json`、`protected_files.json` 及其任意子路径版本）——已断言。

**结构化结果承接了驱动 journal 的全部信息**：`start_revision`/`end_revision`/`completed_subtasks`/`stop_reason`/`boundary_fingerprint`/两项 verification；且授权块（含 `max_attempts_per_step`、`max_reopens_per_step`）可从事件流回查。

**驱动原有的腐烂已被证实**：它断言 A 处于 `ready / active_step=5`，而 A 已完成（revision 561）。迁移后的形式把同一期望表达为**授权**——陈旧的授权会被拒绝并给出理由，而不是在脚本里抛 `AssertionError`。测试同时断言了 revision 与 cursor 两种拒绝路径。

### 6.5 G4.5b：代表链迁移结果与新发现的合同缺口

`tests/test_gate_g4_driver_migration.py` 由 5 项扩展到 11 项。

**已迁移并证明等价（2 条）**

| 代表 | 族 | 结果 |
|---|---|---|
| `run_step12_m6.py`（21L，最小真实驱动） | `advance_with_protection` | 与 legacy 轨语义闭包无差异；`assert state.revision == 306` 这一**已腐烂**的断言被授权取代（实测拒绝并给出实际 revision，零事件写入） |
| `continue_adopted_model.py`（48L） | `advance_with_protection` | 同上；其 `for step in (3,4)` 循环拆为两次独立 bounded 调用，均证明等价 |

**新发现的合同缺口（已处置：扩合同）：大文件按"身份"而非哈希保护**

`run_final_workflow_resume.py`（40L，`recovery_boundary` 族）除 sha256 manifest 外，还校验一个大文件的**身份**：

```python
large = json.loads((W / 'large_manifest_identity.json').read_text())
stat = (P / large['path']).stat()
assert stat.st_size == large['size'] and stat.st_mtime_ns == large['mtime_ns'], 'large number manifest drift'
```

而 `BoundedRunContract.protected_manifest` 是 `Mapping[str, str]`（相对路径 → 小写 sha256），`verify_protected_manifest` 只做 sha256 比较。**合同无法表达"按 (size, mtime_ns) 身份保护大文件、不哈希"**——这不是疏漏，是该驱动为避免对超大文件做完整 sha256 而做的刻意取舍。

按你的判断准则（"先判断它是否属于通用 runner 语义，再决定是否扩合同"），这**确实属于通用语义**：任何保护大产物的调用方都会遇到"哈希成本 vs 身份强度"的取舍。但扩合同需要设计决策，故**未擅自扩**，留待你定。三个方向：

**已按方案 1 实施**：合同新增第二类清单

```python
protected_identity: Mapping[str, tuple[int, int]] | None = None   # 路径 -> (size, mtime_ns)
```

- 与 `protected_manifest` **并列且同等强制**：入口校验与 checkpoint 提交前校验两处都检查；`ProtectedVerification` 新增 `identity_changed` 字段，使"内容变了"与"身份变了"**在结果里可区分**。
- 同一路径**不允许同时出现在两类清单里**（一个路径一种检查），构造期拒绝。
- 路径安全与数值校验（非负整数、拒绝 bool）与哈希清单一致；`protected_identity` 进入合同 SHA 与 `RUN_STARTED` 授权块。
- **弱保证已显式记录并被测试固化**：`(size, mtime_ns)` 相等**不证明内容相等**——测试 `test_identity_protection_is_weaker_than_hashing_and_says_so` 用"等长改写 + `os.utime` 复原 mtime"实际绕过了身份校验，而同一份文件被哈希校验发现。这正是调用方为不对超大文件做完整哈希而接受的取舍，也是两类检查必须分开报告的原因。
- `recovery_boundary` 代表 `run_final_workflow_resume.py` 已证明等价：其 `verify()` 的两类检查都成为合同字段，`entry_verification.checked == 2`；`while 11 <= active_step <= 15` 循环变为调用方侧的连续 bounded 调用。

### 6.6 `special_business` 族只读分析（最后一块拼图）

3 条驱动同构：`run_native_after_runtime_recovery.py`(44L)、`run_native_final16_recovered.py`(40L)、`run_native_polish_and_final.py`(47L)。

#### 逐项职责分类

| 驱动里的东西 | 归属 |
|---|---|
| `allowed_source_steps=scope`（`polish`→`{14,15}`、`final`→`{16}`） | ✅ **合同已有** `allowed_source_steps` |
| sha256 protected manifest | ✅ **合同已有** `protected_manifest` |
| 大文件 `(size, mtime_ns)` 身份 | ✅ **合同已有** `protected_identity`（本轮新增） |
| `assert state.revision == int(sys.argv[1])` | ✅ **合同已有** `expected_revision` |
| `status=='ready' and active_step==16` | ✅ **合同已有** `expected_cursor` |
| 无进展停止 `if state.revision == before.revision: break` | ✅ **合同已有** `made_progress` / `unchanged_boundary` |
| 步数上限停止 `if polish and last_completed_step>=15: break` | ✅ 调用方读取返回的 cursor 即可 |
| journal（`record` / `save`） | ✅ **合同已有** `BoundedRunResult` |
| **`checkpoint13` 的 receipt 断言 + 运行后不变性** | ❌ **合同无法表达** ← 唯一缺口 |
| `sys.executable == /usr/bin/python3`、`find_spec('openpyxl'|'numpy'|'scipy')` | 非 runner 语义（调用方环境前置检查） |
| `sys.argv[2] in {'polish','final'}` 模式选择 | 调用方 |
| `original_step13_judgment_before_final16.zip` 存在性 | 调用方 |

#### 唯一缺口：已提交 checkpoint 的不变性（已处置：扩合同）

驱动在运行前后各断言一次：

```python
checkpoint13 = next(c for c in store.stage_checkpoints() if c['source_step_id']==13)
assert checkpoint13['completed_revision'] == 397
assert checkpoint13['receipt']['result']['precheck_skipped'] is True
assert checkpoint13['receipt']['result']['judge_completed'] is False
# ... run ...
assert next(c for c in store.stage_checkpoints() if c['source_step_id']==13) == checkpoint13
```

**这不是冗余检查**：引擎确实有合法的 checkpoint 失效机制（`STAGE_CHECKPOINT_INVALIDATED`，`engine.py:976` 与 `:1039`，当上游产物变化时触发）。驱动是在断言"本次调用不会让 step 13 的已提交结论被改写"。

它与 `protected_manifest` **同类**（"不要动这些已提交的东西"），只是保护对象是 DB 行而非文件：

| 方案 | 说明 |
|---|---|
| **A（推荐）** | 合同新增 `protected_checkpoints: frozenset[int] \| None`（source_step_id 集合）：入口快照、提交前比对，与 manifest 同一套两阶段设计。优点是**在 commit 之前阻断**，而驱动只能在事后发现 |
| B | 不加合同字段：调用方用公开的 `store.stage_checkpoints()` 自行 before/after 比对。**事后**发现，提交已经发生 |
| C | 判为超出有界合同范围，这 3 条仅在引擎获得显式"checkpoint 不可变"保证后才退役（另一场设计讨论） |

**已按方案 A 实施**：

```python
protected_checkpoints: frozenset[int] | None = None   # 必须保持不变的 source_step_id 集合
```

- 入口**快照**（`snapshot_checkpoints`）并在 checkpoint 提交前**比对**（`verify_checkpoints`），与 manifest 同一套两阶段设计 —— 变化会在 commit 之前阻断，而驱动只能在事后发现。
- 新增独立的 `CheckpointVerification`（`ok` / `checked` / `changed` / `missing`，其中 `changed` 与 `missing` **互斥**）而不是塞进 `ProtectedVerification`：保护文件与保护已提交 checkpoint 是两回事，失败原因不同，其中一个是数据库行。
- `BoundedRunResult` 增加 `entry_checkpoints` / `final_checkpoints`；`stop_reason` 新增 `PROTECTED_CHECKPOINT_VIOLATED`（与 `PROTECTED_MANIFEST_VIOLATED` 并列，manifest 优先）。
- 预提交阻断使用 `error_class = "PERMANENT_PROTECTED_CHECKPOINT_VIOLATED"`，与 manifest 的 `PERMANENT_PROTECTED_MANIFEST_VIOLATED` 对称。
- **保护一个不存在的 checkpoint 在入口即拒绝**：`snapshot_checkpoints` 只报告存在的东西，所以请求本身必须单独检查——否则"保护了一个不存在的 step"会看起来像保护生效了。这一条是我在写测试时发现的实现漏洞，已修。
- 快照在 `run()` 里与 `_bounded_contract` **同一 `try/finally` 生命周期**启用与清除，因此 `run(contract=...)` 这条直接路径也受保护。

#### 链代表的结论：patcher 不是合同缺口

`activate_scope_alignment.py`（75L，被 `run_scope_alignment.py` 引用）的职责：

1. 备份并改写 `web/model_config.json`、`web/notes.json`（原子替换）
2. 用候选替换 `quality_contract.json`
3. 渲染 step-5 prompt 并断言其中含指定 note（**prompt 身份校验**）
4. **读取 `run_bounded_evidence_repair.py` 的源码文本，做 3 处字符串替换后另存为 `run_scope_alignment.py`**（先 `compile()` 校验）
5. 写 `activation_verification.json`

第 4 步是关键证据：`run_scope_alignment.py` 与 `run_bounded_evidence_repair.py` **逐字节同构**，仅 `W` 路径与 manifest 推导范围不同。**patcher 存在的原因正是当时没有受支持的方式表达"在这个作用域、带这套保护地推进一次"**——而这恰好就是 `run_bounded()`。

因此：

- 第 1–3、5 步是**产物/合同供给**，不属于 runner 语义，也不进合同；它们会作为普通维护脚本继续存在（第 3 步属 S4.2 prompt identity 范畴，本就不在本 goal 范围内）；
- **第 4 步随驱动层退役而消失**——它不是需要被合同承接的能力，而是**合同已经堵上的那个缺口的历史证据**。

#### 结论

**合同表面稳定**：4 个族的协议已全部归并完毕——`registry_limit`、`advance_with_protection`、`recovery_boundary`、`special_business` 的每一项手写关切都有合同字段或明确的"属调用方/属退役"归属。20 条真实驱动所代表的全部手写执行协议至此**归并完成**。

累计四项合同扩展，全部由真实驱动的真实需求驱动、且都只增强或只收紧语义：

| 扩展 | 触发它的真实驱动 | 语义方向 |
|---|---|---|
| `max_attempts_per_step` / `max_reopens_per_step` | `run_bounded_evidence_repair.py` 的 registry shim | 只收紧 |
| `protected_identity` | `run_final_workflow_resume.py` 的大文件身份 | 新增第二类检查 |
| `protected_checkpoints` | `run_native_*.py` 三条的 checkpoint 不变性 | 新增第二类保护 |

**下一步：G4.5c 批量退役。**

### 6.7 G4.5c 第 1 步：`work/` 备份与可复原性验证（已完成）

artifact：`runtime_simplification_g45c_work_backup.json`。备份位于仓库之外：`/home/tfisher/pf-g45c-backup/`。

| 项 | 值 |
|---|---|
| 条目 | 1565（1448 文件 / 117 目录 / 0 符号链接） |
| 文件字节 | 1,021,921,072（979 MiB） |
| `work.tar.zst` | 894,785,086 B（855 MiB） |
| `work.tar.zst` sha256 | `fb4acf0c174d8641e6a4838616fa70ffdee4f0e07176fc14b7540381a96f5a4f` |
| `work_manifest.json` sha256 | `919616cfff0dcb576970fb4e9b8a0343db35bf76845f7823152f3d2dca984b3d` |

**manifest 方法**：`os.walk(followlinks=False)` + 逐条目 `lstat`，每个常规文件算 sha256，记录 size 与 `mtime_ns`，符号链接记录 target，**不排除任何条目**。

**可复原性验证**：仅从归档（`zstd -dc | tar -x`）重建一棵临时树，逐文件重新哈希后与 manifest 比对：

```json
{"entries_expected": 1565, "entries_restored": 1565,
 "missing": [], "extra": [], "mismatched": [], "identical": true}
```

临时副本已删除——归档即备份。

**改写前的编译体检**：每一批改写前后都用分支解释器对 `work/` 下全部驱动做 `compile()` 检查，这样即使某个脚本再也不会被执行，语法破坏也能被当场发现。

**此刻 `work/` 仍未被改动**（本轮只有读取与归档）。

### 6.8 G4.5c 第 2 步：批次 1（`run_results_adoption.py`）已完成

批次记录：`runtime_simplification_g45c_batches.json`。

**为什么先选它**：它是 `registry_limit` 族里**唯一独立的**驱动（不被任何 patcher 引用），因此能在不触碰任何链的前提下，完整走通一次「改写 → 编译体检 → CI → 等价测试 → inventory 复查」的闭环。

| | 值 |
|---|---|
| 原件 | 53 行 / 2755 B / sha256 `7ae5a2f8…c9faba` |
| 迁移后 | 71 行 / sha256 `a22f310878e581f3…` |
| 替换内容 | `assert status/active_step/last_completed_step` → `expected_cursor`（**并补上原件没有的 revision CAS**）；`SingleAttemptRegistry` + shim 自检 → `max_attempts_per_step={5:1}` / `max_reopens_per_step={5:0}`；自写 `protected_files.json` + `verify_sources()` → `protected_manifest`（**不再写盘**）；两次 `progress.json` → `BoundedRunResult`；无条件 `protected_files_unchanged=True` → `final_verification.ok`；`engine.run(max_steps=1)` → `service.advance_bounded`（受支持的服务入口） |

**指标复查（单调下降，唯一上升项是预期替代物）**

| 指标 | scrub | before | after | Δ |
|---|---|---|---|---|
| `.run(` | code only | 52 | 51 | −1 |
| 精确工作流推进 | code only | 25 | 24 | −1 |
| `max_steps=1` | code only | 23 | 22 | −1 |
| registry shim | code only | 4 | 3 | −1 |
| `progress.json` | no comments | 35 | 33 | −2 |
| `protected_files` | no comments | 113 | 110 | −3 |
| `advance_bounded` | code only | 0 | 1 | **+1（预期替代物）** |

**编译体检**：`work/` 全部 212 个文件，改写前后 **0 语法错误**。

**一处测量方法的修正（值得记住）**：第一次复查报出 `registry_shim` 未下降、`protected_files` 上升——纯粹因为**迁移文件自己的说明注释**里写了 `class SingleAttemptRegistry(...)` 与 `protected_files.json`。指标必须对注释免疫。但两套 scrub 不能一刀切：`.run(`/`max_attempts=` 属**代码形状**（注释与字符串都抹掉），而 `progress.json`/`protected_files` 是**文件名、只存在于字符串里**（只抹注释）。最终采用两套 scrub，并把 before 值从**归档里取回的原件**上重新测得，保证前后同口径。

**判据 4 已满足**：`test_the_batch1_driver_shape_is_equivalent_after_migration` 与 `test_the_batch1_cursor_precondition_is_an_authorisation_not_an_assert` 已进入 CI，legacy 轨忠实复现了该驱动的 `SingleAttemptRegistry`。

**尚未改动**：任何被 patcher 引用的驱动（8 条链）与任何 patcher。

### 6.9 G4.5c 第 2 步：批次 2（5 条 `advance_with_protection`）已完成

`continue_adopted_model.py`、`run_paper_adoption.py`、`run_reviewer_entry.py`、`run_step11_review.py`、`run_step12_revision.py` 原地改写。它们形状同构（manifest + 单步推进 + 无 registry shim），且都不被 patcher 引用，因此共用一套 recipe 与等价测试模板。

**累计指标（batch 1 + 2）**

| 指标 | before | after | Δ |
|---|---|---|---|
| `.run(` | 52 | **46** | −6 |
| 精确工作流推进 | 25 | **19** | −6 |
| `max_steps=1` | 23 | **17** | −6 |
| registry shim | 4 | **3** | −1 |
| `progress.json` | 35 | **26** | −9 |
| `protected_files` | 113 | **110** | −3 |
| `advance_bounded` | 0 | **6** | **+6（每个迁移驱动各一个）** |

**唯一上升项是预期替代物**，其余全部下降；212 个文件 0 语法错误。

#### 本轮发现并修复的一处真实缺陷（重要）

**迁移后的驱动把 `expected_cursor` 取自刚刚读到的状态**：

```python
expected_revision=state.revision,
expected_cursor=(state.active_stage, state.active_subtask, state.source_step_id),
```

而这一读一回填，使 **revision CAS 恒真**，同时 `cursor_of()` 只返回 `(active_stage, active_subtask, source_step_id)`——**根本不含 `active_step`**。于是原件那条"下一步必须是第 N 步"的前置条件**被静默删除，而不是被搬进合同**。

**为什么此前看不出来**：CASS 用刚读的 revision，位置又用刚读的 cursor，两者都按构造满足；迁移看起来像是把前置条件表达成了授权。

**修复**：五个驱动**逐字保留原前置条件**作为显式守卫（对 `active_step` 或 `active_subtask` 加 `status` 的检查，拒绝时给出可读信息），合同则补上原件从来没有的 revision CAS。批次 1 的驱动同样补上了。

**回归测试**：`test_a_read_then_pin_cursor_silently_drops_a_step_expectation` 直接演示这个洞——read-then-pin 的合同会**推进一条原件本该拒绝的项目**——并断言真正起作用的是 revision CAS。

**严重性**：它会在**没有任何测试失败**的情况下从 5 个驱动上移除守卫，正是这一整轮工作要防的失败形态。

**测量方法的第三层修正**：文件名指标（`progress.json`/`protected_files`）第一次测出 `protected_files` **+1**——因为**模块 docstring 也是 STRING**，而"只抹注释、保留字符串"把迁移文件自己 docstring 里的 `protected_files.json` 保留了下来。现用 `ast` 定位 docstring 并抹除，普通字符串字面量仍保留。**指标必须对注释与 docstring 都免疫**，而文档不必为此缩水。

**尚未改动**：任何被 patcher 引用的驱动（8 条链）与 8 个 patcher。

**canary 保真边界（已记录）**

`build_native_registry` 下，hermetic canary 干净覆盖 step 0–8；**step 8.5 的 reviewer entry gate 需要真实门证据**（`entry_gate.md` 的 VERDICT 及两份配套 map），permissive validator 不产生它，故 ≥8 的种子会停在门处（实测 seed 8→8 failed，而 seed 3→4、4→5、6→7、7→8 均干净推进）。已写成断言测试，避免被误认成驱动差异；未来若需覆盖 8.5 及以后，fixture 需在该处生长。

**因此本轮未做**：`run_scope_alignment.py` + `activate_scope_alignment.py` 链。其 patcher 自身会写 `web/model_config.json`、`web/notes.json` 并改写驱动源码，属 `special_business` 量级，留到缺口决策之后。

**合同表面状态**：`registry_limit`（第一条链）、`advance_with_protection`（2 条）、`recovery_boundary`（1 条）**均已证明可表达且等价**；三项合同扩展全部由真实驱动的真实需求驱动，且都是**只收紧/只增强**语义。仅剩 `special_business`（3 条，含链形式的 patcher）未开始——**它是否还需要新能力，是合同表面能否宣布稳定的最后一块拼图**。

### 5.4 三道"广泛删旧层"门槛已全部关闭

| 门槛 | 状态 |
|---|---|
| I8-b（只读 migration / domain 漂移审计） | **已达成**（6 库审计 + 预版本化流的发现） |
| 真实 `bde49712` downgrade 回归 | **已达成**（真实旧解释器 + 防空转守卫） |
| I8-a（只读 v9 前置校验器） | **已达成**（§5.3） |

Gate 3 关闭。**下一步：冻结最后一份 pre-driver 基线，然后进入 Gate 4（driver 退役）。**## 6. Gate 4 — driver 退役

| 编号 | 动作 | 通过判据 |
|---|---|---|
| G4.1 | **重新冻结 driver 普查基线** | 统一口径重跑 A/work 的各项计数并落盘 |
| G4.2 | 只迁移**一个** canary 对应的 `work/*.py` 驱动链 | 移除 `run(max_steps=1)`、硬编码 `assert state.revision == N`、自带 `protected_files.json`、自制 `progress.json`、重复 verify；统一走 `run_bounded()` |
| G4.3 | 重跑 Gate 1 全部等价性 + Gate 2 全部分支 | 与迁移前逐项一致 |
| G4.4 | 全量回归 | CI 等价口径 0 failed；`git diff --check` 干净 |
| G4.5 | 批量迁移 | 每批重复 G4.3–G4.4 |

### 6.1 pre-driver 基线已冻结

`docs/architecture/runtime_simplification_pre_driver_baseline.json`（head `15f24bd`，Gate 0–3 全绿）。

**driver 普查实测**（A 的 `work/`，引用口径；S6 数字并列以便对照）：

| 指标 | S6 记录 | 本次实测 |
|---|---|---|
| `.run(` | 52 | **52** |
| `max_steps=1` | 24 | **23** |
| 硬编码 `assert revision == N` | 20 | **44** |
| `protected_files` 引用 | 47 | **54** |
| `progress.json` 引用 | 29 | **29** |
| `protected_files.json`（磁盘） | — | 14 |
| `progress.json`（磁盘） | — | 16 |
| `run_bounded` 引用 | 0 | **2** |
| `advance_bounded` 引用 | 0 | 0 |

`max_steps=1` 24→23、硬编码 assert 20→44、`protected_files` 47→54——**S6 的数字不能当删除清单**，以本表为准。

**第一条 driver 链已选定**并做了覆盖分析：`work/run_bounded_evidence_repair.py`（74 行）。它同时体现全部病态，且**名字已声称 `run_bounded` 而正文仍调用 `engine.run(max_steps=1)`**。

**已发现的合同覆盖缺口**（需决策，见 §6.2）。

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

### 6.2 Gate 4 已发现的合同覆盖缺口

第一条链的驱动用 `class BoundedRegistry(StepRegistry)` 覆写 `get()`，把 source step 5 的 `max_attempts` 压成 `before.attempt + 1`、`max_reopens=0`。

而 `BoundedRunContract` 的字段只有 `expected_revision` / `expected_cursor` / `allowed_source_steps` / `max_subtasks` / `protected_manifest` / `run_policy` / `actor` / `previous_boundary_fingerprint`——**没有 attempt 上限**。`max_attempts` 来自 `StepDefinition`（`engine.py:393`、`engine.py:766`）。

因此对这一条链而言，**纯替换会静默改变行为**：step 5 会按 registry 的真实 attempt 预算运行，而不是 `attempt + 1`。这正是"统一改成 `run_bounded()`"不能一概而论的地方，必须先决策。三个方向：

1. 给合同加 attempt / scope 上限（产品改动）；
2. 保留 registry shim，接受**部分迁移**；
3. 重新定性：把该 shim 读作对引擎重试策略的绕行，改为修引擎而非搬进合同。

**已处置（扩合同）**：`BoundedRunContract` 新增两项**只收紧不放宽**的上限，并纳入合同身份与 `RUN_STARTED` 事件载荷：

```python
max_attempts_per_step: Mapping[int, int] | None = None   # 每个 source step 的 attempt 上限
max_reopens_per_step:  Mapping[int, int] | None = None   # 每个 source step 的 reopen 上限
```

- 生效值取**合同上限与 registry 自身值的较小者**，因此合同**永远不能放宽**目录已授予的授权；`max_reopens` 一并加入，是因为同一条驱动同时对这两个旋钮做了限制，只补一半仍会留下 shim。
- 强制点：引擎在 bounded 推进期间换入 `ScopedRegistry` 包装（与 `_bounded_contract` 同一个 `try/finally`）。之所以不逐个去改约 11 处 definition 解析点，是因为那样下次新增一处就会漏掉；包装覆盖全部解析路径，且**仅在合同带了上限时才换**，无上限路径保持字节不变。
- `ScopedRegistry` 对未作用域的属性走 `__getattr__` 委托，registry 后续新增能力不会被静默丢掉。

**行为级验证**（不只是字段回填）：transient 失败的 step 在无上限时按 registry 的 `max_attempts=3` 重试 3 次并产生 `RETRY_SCHEDULED`；上限 `{1: 1}` 时**只调用 1 次且无重试事件**；上限 `{1: 99}`（比 registry 宽松）时行为与无上限完全一致（3 次 + 重试），证明"只收紧不放宽"是行为属性而非字段属性。

**另外**：该驱动的硬编码前置条件已经腐烂——它断言 A 处于 `status=ready, active_step=5`，而 A 现在是 `completed`（revision 561）。即驱动本身**早已不可运行**，这正是 S6 描述的"hard-coded revisions rot silently"。

**暂缓**：生产级 `factory rewind`；`work/*.py` 的批量删除。

**注意**：Gate 1 的两轨基线固定为 **`0dbc5c3`**（即 Gate 0 验证通过的那个 head）。历史沿革：`2c5b606` → `1a04400`（同步 `uv.lock`）→ `108e620`（G2/G3 gate 可在无生产主机上运行）→ `54be447`（冻结本计划）→ `0dbc5c3`（关闭 Gate 0）。
