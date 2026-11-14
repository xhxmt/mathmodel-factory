# Step 调度代际退出评估（调用者与迁移清单）

> **状态（2026-11-14）：已完成。** 第 6 节的清单已全部执行：
> - `repair_operations.py` 已随技术续跑一起删除；
> - `storage.initialize` 的默认代际改为 `stage_v1`；
> - `FactoryEngine.run()` / `recover()` 遇到 `step_v2` 项目时直接以 `STAGE_SCHEDULER_REQUIRED` 拒绝，引擎里的 Step 执行分支已删除；
> - 原先以 Step 代际驱动引擎的测试改走 Stage 路径（`tests/_stage_engine.py`）。
>
> 读取历史 `step_v2` 状态、诊断，以及 `migrate scheduler-activate` 迁移命令都保留。下文是当时的评估原文。

**范围：** `main@996ae4ec`，静态调用链审阅。本文只回答一个问题：`FactoryEngine` 里
仍同时处理 Native Step 与 Stage 两条调度路径，现在能不能删掉 Step 分支？

**结论：不能。** Step 分派目前仍由三样东西支撑——一个未设防的默认值、一个绕过运行守卫的
文档化操作入口、以及直接构造引擎并以 Step 代际初始化存储的测试。它们是**活体代码**，不是
遗留死码。下文给出退出前必须处理的最小清单与顺序。

本文是**评估交付物**，不包含代码改动。

---

## 1. 代际的定义

| 位置 | 内容 |
|---|---|
| `factory_core/stages.py:13` | `STEP_SCHEDULER_GENERATION = "step_v2"` |
| `factory_core/stages.py` | `STAGE_SCHEDULER_GENERATION`（现役主线） |
| `factory_core/engine.py:92-99` | 同时接受两种代际并派生 `stage_mode` |
| `factory_core/engine.py:1852,1868` | `_commit_project_completed(..., stage_mode)` 按代际写不同的完成步 |

`engine.py` 共 **2098 行**，其中 `stage_mode` 出现 **10 处**（`:99,100,125,130,154,187,195,221,1852,1868`）。
这是全仓库唯一可能一次性削减数百行的地方，也是唯一需要"先出清单再动手"的地方。

**注意区分两件事：**

* Step 0–16 的**合同编号**（产物与验证职责）仍然有效，本文不建议改动。
* Step **调度代际**（谁在跑主循环）才是本评估的对象。

把前者当成后者的依据去删代码，会误伤产物合同。

---

## 2. 生产执行入口已经被守卫

`factory_core/service.py:720-727`：

```python
def _require_stage_runtime(state: WorkflowState) -> None:
    ...
    if state.scheduler_generation != STAGE_SCHEDULER_GENERATION:
        raise InvalidTransition(
            "STAGE_SCHEDULER_REQUIRED: explicitly run migrate scheduler-activate "
            "on a stopped Native project before starting it"
        )
```

调用点：`:332`、`:401`（`FactoryService.run`）、`:849`。

项目创建也一律以 Stage 初始化：

* `factory_core/service.py:305-310`（`create_project`，显式 `scheduler_generation=STAGE_SCHEDULER_GENERATION`）
* `factory_core/cli.py:313-317`（CLI 创建路径，同上）

**推论：** 经 `FactoryService.run()` 无法进入 `engine.py` 的 Step 执行分支。

---

## 3. 两个真实缺口

### 3.1 未设防的默认值

`factory_core/storage.py:1261`：

```python
def initialize(self, project_id, project_type, ...,
               scheduler_generation: str = STEP_SCHEDULER_GENERATION, ...):
```

**任何直接调用 `initialize()` 而不显式传代际的代码，都会造出一个 `step_v2` 项目。**

生产路径都已经显式传参，所以这不是当前的生产缺陷；但它是退出 Step 分派时**必须先关掉的
入口**，否则"删除 Step 执行"会与"仍然能造出 Step 项目"同时存在。

### 3.2 绕过守卫的文档化操作入口

`factory_core/repair_operations.py:20`：

```python
engine = FactoryEngine(args.project, registry=build_native_registry(root),
                       projector=write_compatibility_projections)
```

它**直接构造 `FactoryEngine`**，不经过 `_require_stage_runtime`，因此可以收到
`scheduler_generation == "step_v2"` 的项目并真正跑起 Step 分支。

该模块**没有代码调用者，但有文档**：
`docs/operations/RERUN_REPAIR_AND_TECHNICAL_CONTINUATION.md:28,37,59-60` 与 `STEPS.md:291`
把它描述为有界人工修复路线（`authorize-gate2` / `continue-gate2`）。

这是退出 Step 执行前**必须处理的第一项**：要么给它加上与 `run()` 相同的代际守卫，
要么证明它在实践中只用于已迁移项目。

---

## 4. 迁移合同（`scheduler-activate`）

`factory_core/service.py:733-800` `activate_stage_scheduler`：

* **前置条件**：项目为 `native_v2` engine；当前代际必须是 `step_v2`（已是 Stage 则直接返回）；
  runner 未运行且状态不在 RUNNING/RETRYING/ARCHIVING；`active_step is None or attempt == 0`
  （有中断 attempt 时要求先恢复）。
* **投影输入**：`projected_stage_cursor(state)` + `initial_stage_checkpoints(last_completed_step)`。
* **写入**：单个 `STAGE_SCHEDULER_ACTIVATED` 事件，携带
  `from/to_scheduler_generation`、`stage_catalog_version`、`seeded_checkpoints`，
  且 `stage_checkpoint_seed=seeds, replace_stage_checkpoints=True` —— 迁移是**原子且可审计**的。
* **回退已退休**：`factory_core/service.py:716-717` `rollback_stage_scheduler` 直接抛
  `InvalidTransition("Step scheduler rollback is retired")`。

CLI 入口：`factory_core/cli.py:536-541`。

**推论：** 迁移是单向的；退出 Step **执行**分派不会影响已迁移项目，但**必须保留**读取历史
`step_v2` 状态、诊断与迁移命令的能力。

---

## 5. 测试覆盖（不能凭主线文档删代码的证据）

| 测试 | 行 | 覆盖内容 |
|---|---|---|
| `tests/test_factory_engine.py` | `:63`、`:112`、`:140` | `store.initialize(project_id=..., project_type=...)` **不传代际** → 走默认 `step_v2` → **直接执行 Step 分支** |
| `tests/test_factory_service.py` | `:60-75` | `test_existing_native_project_requires_explicit_atomic_scheduler_migration` |
| `tests/test_stage_scheduler.py` | `:1131-1145` | `step_v2` 项目的 stage 游标投影 |
| `tests/test_factory_state_store.py` | `:420-432` | 默认代际为 `step_v2`、`stage_catalog_version is None` |

`test_factory_engine.py` 是决定性的：它**以 Step 代际构造引擎并断言执行结果**，说明 Step
分支有活体覆盖。若删除该分支，这些测试必须一并改写为 Stage 初始化。

---

## 6. 退出清单（按顺序）

1. **审计 `repair_operations.py`**：确认两个操作在生产中的项目来源；要么加代际守卫，
   要么在文档中限定为已迁移项目。
2. **关掉默认值**：把 `storage.initialize` 的 `scheduler_generation` 默认改为
   `STAGE_SCHEDULER_GENERATION`，或改为必填参数。同时处理显式传 `step_v2` 的地方
   （目前只有测试）。
3. **分离"读取"与"执行"**：保留历史 `step_v2` 状态的读取能力
   （`runtime_payload`、诊断、`migrate scheduler-activate`），只删除**执行**分支。
4. **删除 `engine.py` 的 Step 执行分支**：从 `:99` 的 `stage_mode` 派生开始，
   逐个处理 `:125/:130/:154/:187/:195/:221/:1852/:1868`；把纯状态读取留在边界适配层。
5. **改写直接构造引擎的测试**（`test_factory_engine.py` 等），改为 Stage 初始化，
   并补一条"对 `step_v2` 项目调用 `run()` 会被守卫拒绝"的回归。
6. **保留 Step 0–16 合同编号**，不做重编号。

## 7. 明确不做

* 不合并或重编号"八阶段 / 十 Stage / Step 0–16"三套编号。
* 不删除历史事件读取、诊断读取与 `scheduler-activate` 迁移命令。
* 不在没有调用者清单的情况下删除 `engine.py` 的分支。
* 不在同一次改动里既改默认代际又删执行分支——两者需要各自可回滚。
