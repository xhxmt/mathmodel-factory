# 设计：交互式上游候选方案选择门

> 日期: 2026-07-03  
> 状态: Phase 1 已实现并完成聚焦验证  
> 目标: 在交互式项目中，把 Step 3 方法主线选择和 Step 4 建模口径选择显式展示给用户；候选项按正确性/可行性优先排序，30 分钟无人工选择时自动采用第 1 名并继续运行。

## 实现记录

2026-07-03 已落地 Phase 1 的 Step 3 方法主线选择门：

- `web/backend/selection_service.py` 负责读取 `selection/config.json`、生成 `selection/step3_options.json` 和 `selection/step3_request.md`、写入 `selection/step3_decision.json`，并把结果镜像到 `human_review.md` 的 `## Step 3 decision:`。
- `scripts/selection_gate.py` 提供 `prepare-step3`、`select-step3`、`default-step3`、`wait-default-step3`；人工 CLI 选择和 timeout default 逻辑都合并在这个 helper 内，没有单独创建 `selection_timeout.py`。
- `run_paper.sh` 在 Step 3 dispatch 前调用 `maybe_select_option step3 3 "方法主线选择"`；无 `selection/config.json` 时行为不变。
- Web API 已提供 `GET /api/projects/{base_name}/selection` 和 `POST /api/projects/{base_name}/selection/decision`，项目状态新增 `selection_pending`、`selection_gate`、`selection_deadline`。
- Dashboard 已新增 `SelectionPanel.vue` 和 `awaiting_selection` 状态展示；selection tab 只在 `selection_pending` 时出现。
- Step 4/5 选择仍保留为 Phase 2。

## 背景

当前 Modeling Factory 的上游内容生成主要由 Step 2/3/4/5 决定：

- Step 2 生成多条建模流；
- Step 3 从 `VERDICT: VALIDATED` 的流中选 `PRIMARY` 和可选 `AUXILIARY`；
- Step 4 把主流程规格提升为完整模型；
- Step 5 执行完整求解。

这些步骤决定论文的模型骨架、证据链、图表主题和结论口径。只在 Step 14/15 做摘要和文字润色，不能可靠地产生真正差异化的论文。用户希望在上游候选产生后，通过 Web 控制台选择方案，同时保留无人值守运行的默认行为。

## 用户确认的约束

- 第一版只用于人工介入的交互式运行，不改变无人值守基线。
- 先做 Step 3 方法主线选择，再做 Step 4/5 建模与求解口径选择。
- 等待人工选择时设置 30 分钟时间窗。
- 超时后自动采用排名第 1 的候选项。
- 候选项默认按正确性/可行性优先排序。
- 前端第一版保持 CLI/现有 dashboard 风格，不需要额外视觉原型。

## 现有能力

已有机制可以复用：

- 当前 Web 已经有 `ModelingDirectionPanel.vue`、`web/backend/modeling_direction_service.py` 和 `/api/projects/{base}/modeling-directions`。这套能力发生在 Step 1 之前或 Step 1 期间，基于 `problem/method_retrieval.md` 和方法库索引给出早期建模方向，并写入 `human_review.md` 的 `## Step 1 modeling directions:` 段。
- `prompts/step1_research_viability.txt` 已经会读取 `## Step 1 modeling directions:`，并要求 Step 1 至少生成一条承接该方向的候选流，同时保留不同方法族备选。
- `human_review.md` 中的 `## Step 3 decision:` 已被 Step 3 prompt 识别为最高优先级人工 override。
- `human_review.md` 中的 `## Step 4 decision:` 已被 Step 4 prompt 识别为最高优先级人工 override。
- Web dashboard 已有 consultation 面板、项目状态轮询、诊断面板、artifact browser 和 resume action。
- `run_paper.sh` 已采用 clean exit 的人工等待模式，避免 runner 长时间占锁。
- 最新修改把共享状态判断抽到 `scripts/workflow_state.py`，把 model config 解析抽到 `scripts/model_dispatch_config.py`。选择门实现也应沿用“小 Python helper + shell orchestration”的风格。

因此本设计不替代 Step 1 modeling directions。两者分工如下：

- `modeling directions`：早期方向偏好，影响 Step 1 如何生成候选流；
- `selection gate`：候选流已经由 Step 2 验证后，选择 Step 3 的 `PRIMARY/AUXILIARY`，后续再扩展到 Step 4 建模口径。

## 方案比较

### 方案 A：复用 consultation 面板和 request/answer 结构

优点：

- 改动少；
- 后端和前端已有处理路径；
- 用户已经熟悉。

缺点：

- consultation 是开放式问答，不适合展示结构化候选、排名、倒计时和默认选项；
- 容易把“选择一个已排序候选”混成“咨询外部模型”。

### 方案 B：新增结构化 selection gate

优点：

- 明确表达这是候选项选择；
- 可以保存结构化候选、分数、证据文件、默认选择和最终选择来源；
- Web 可以直接渲染卡片、排序、倒计时；
- 仍可把最终结果镜像写入 `human_review.md`，复用现有 Step 3/4 prompt override。

缺点：

- 需要新增文件协议、后端接口、前端面板和 timeout helper。

### 方案 C：runner 阻塞等待 30 分钟

优点：

- 表面实现最少。

缺点：

- 与现有 clean-exit consultation 模式冲突；
- 容易被活动监控视为异常；
- 长时间占用 runner lock，不利于恢复和诊断。

**最终选择：方案 B。**

## 范围

### Phase 1 范围

- 新增 Step 3 方法主线选择门。
- 对显式启用交互式选择的项目生效。
- 生成结构化候选 `selection/step3_options.json` 和人类可读摘要 `selection/step3_request.md`。
- Web 展示候选、证据、默认 AUX 推荐和倒计时。第一版应复用 `ModelingDirectionPanel.vue` 的紧凑卡片风格，但另建 `SelectionPanel.vue`，避免把 Step 1 早期方向和 Step 3 最终方法主线混淆。
- 提交后写 `selection/step3_decision.json`，并镜像写入 `human_review.md` 的 `## Step 3 decision:` 段。
- 30 分钟无人工选择时自动采用第 1 名，并写明来源为 `auto-timeout`。

### Phase 2 范围

- 新增 Step 4 建模口径选择门。
- 生成 `selection/step4_options.json`、`selection/step4_request.md`、`selection/step4_decision.json`。
- 提交后镜像写入 `human_review.md` 的 `## Step 4 decision:` 段。

### 非目标

- 不改变无人值守项目默认行为。
- 不让 Web 直接生成 `chosen_method.md` 或 `model.md`。
- 不把 Step 5 求解策略选择放进第一版。
- 不把“差异化优先”作为默认排序；差异化只作为展示字段和后续 tie-break 参考。
- 不用该机制规避查重。目标是让上游方案选择更透明，并保留真正不同的模型路线。

## 文件协议

项目目录新增：

```text
selection/
  config.json
  step3_options.json
  step3_request.md
  step3_decision.json
  step4_options.json
  step4_request.md
  step4_decision.json
```

### `selection/config.json`

```json
{
  "enabled": true,
  "gates": ["step3", "step4"],
  "timeout_minutes": 30,
  "default_policy": "top_ranked"
}
```

Absent config means selection gates are disabled.

### `selection/step3_options.json`

Each option represents one validated Step 2 stream:

```json
{
  "schema_version": "1.0",
  "gate": "step3",
  "created_at": "2026-07-03T00:00:00Z",
  "deadline_at": "2026-07-03T00:30:00Z",
  "default_option_id": "m3",
  "default_aux_id": "m2",
  "ranking_policy": "correctness_feasibility_first",
  "options": [
    {
      "id": "m3",
      "rank": 1,
      "title": "m3 - MILP + heuristic repair",
      "family": "MILP",
      "validated": true,
      "scores": {
        "correctness": 5,
        "feasibility": 5,
        "coverage": 5,
        "innovation": 4,
        "risk": 2,
        "differentiation": 3
      },
      "composite_score": 125,
      "summary": "One-sentence technical summary.",
      "why_high_ranked": ["Demo solved OPTIMAL.", "Covers every sub-question."],
      "main_tradeoffs": ["Runtime risk concentrates in P4."],
      "subproblem_mapping": {
        "P1": "SPRT module",
        "P2": "MILP decision module"
      },
      "evidence_files": ["m3_spec.md", "m3_critique.md", "m3_demo_result.json"],
      "aux_compatibility": ["m2"],
      "recommended_aux": "m2",
      "selection_payload": {
        "primary": "m3",
        "auxiliary": "m2"
      }
    }
  ]
}
```

Sorting rule:

1. `correctness` descending;
2. `feasibility` descending;
3. `coverage` descending;
4. `innovation` descending;
5. `risk` ascending.

### `selection/step3_decision.json`

```json
{
  "schema_version": "1.0",
  "gate": "step3",
  "selected_option_id": "m3",
  "selected_aux_id": "m2",
  "source": "human",
  "decided_at": "2026-07-03T00:12:00Z",
  "reason": "Selected the highest-ranked option.",
  "mirrored_to_human_review": true
}
```

`source` is one of:

- `human`;
- `auto-timeout`;
- `manual-cli`.

### `selection/step4_options.json`

Each option represents a modeling口径包 under the selected primary method:

```json
{
  "schema_version": "1.0",
  "gate": "step4",
  "base_method": "m3",
  "default_option_id": "m3_variant_a",
  "options": [
    {
      "id": "m3_variant_a",
      "rank": 1,
      "title": "Robust objective + staged solve",
      "variant_type": "objective_formulation",
      "scores": {
        "correctness": 5,
        "feasibility": 4,
        "robustness": 5,
        "interpretability": 4,
        "runtime_risk": 2
      },
      "summary": "Use a robust objective and staged solver path.",
      "assumption_deltas": ["Treat demand bounds as interval uncertainty."],
      "objective_deltas": ["Optimize worst-case adjusted profit."],
      "solver_deltas": ["Use relaxation warm start before exact solve."],
      "expected_output_difference": [
        "Sensitivity section centers on uncertainty budget.",
        "Main table reports robust and nominal objective side by side."
      ],
      "risks": ["May produce more conservative headline result."],
      "evidence_files": ["chosen_method.md", "m3_spec.md"]
    }
  ]
}
```

## Runner Design

Add a selection gate primitive analogous to consultation:

```bash
maybe_select_option <gate> <step> <title>
```

Responsibilities:

- Check `selection/config.json`.
- If disabled, return immediately.
- If `selection/<gate>_decision.json` exists, return immediately.
- If options do not exist, run the gate-specific ranking prompt or helper.
- Write diagnostics state `awaiting_selection`.
- Start the timeout helper.
- Cleanly exit the runner with status 0.

Step placement:

- Before Step 3 dispatch: `maybe_select_option step3 3 "方法主线选择"`.
- Before Step 4 dispatch: `maybe_select_option step4 4 "建模口径选择"`.

Step 3 and Step 4 then continue through existing prompt override mechanisms.

## Timeout Helper

Timeout defaulting is implemented inside `scripts/selection_gate.py` rather
than a separate `selection_timeout.py`.

Responsibilities:

- Read `selection/<gate>_options.json`.
- Sleep until `deadline_epoch`.
- Exit if `selection/<gate>_decision.json` already exists.
- Otherwise write a decision using `default_option_id` and `source=auto-timeout`.
- Mirror the decision into `human_review.md`.
- Resume the project with `launch_agents.sh resume <base>`.

The helper must be idempotent. Multiple helper processes may exist after
retries; only the first process that successfully creates the decision file may
resume the project.

Manual CLI choice is handled by:

```bash
python3 scripts/selection_gate.py select-step3 ongoing/<base_name> \
  --primary m2 --aux m1 --reason "Prefer heuristic contrast"
```

This writes `source=manual-cli`, mirrors the Step 3 override into
`human_review.md`, and resumes the project unless `--no-resume` is supplied.

## Backend Design

Add `web/backend/selection_service.py`.

Core functions:

- `read_selection_request(project_path, gate="step3")`;
- `write_selection_decision(project_path, gate, selected_option_id, selected_aux_id, source, reason, now_epoch=None)`;
- `mirror_step3_decision_to_human_review(...)`;

Implementation should reuse patterns from `web/backend/modeling_direction_service.py`:

- focused filesystem service with no FastAPI dependency;
- deterministic ranking helpers;
- human-review section replacement through a scoped H2 heading regex;
- tests built directly against the service module.

Add API endpoints:

- `GET /api/projects/{base_name}/selection`;
- `POST /api/projects/{base_name}/selection/decision`.

The response should include:

- current gate;
- options;
- deadline;
- default option;
- existing decision, if present;
- evidence files.

## Frontend Design

Add `SelectionPanel.vue` beside `ConsultationPanel.vue`.

`ModelingDirectionPanel.vue` stays in the overview for Step 0/1. `SelectionPanel.vue`
appears when `selection_pending` is true. The first implementation keeps the
tab hidden for ordinary projects to avoid adding an empty browser surface.

Display:

- title: `Step 3 方法主线选择` or `Step 4 建模口径选择`;
- countdown until default selection;
- option cards sorted by rank;
- score strip for correctness, feasibility, coverage, innovation, and risk;
- evidence-file buttons using the existing artifact opening path;
- primary and auxiliary selectors for Step 3;
- single variant selector for Step 4.

Actions:

- `查看证据`;
- `选为 PRIMARY`;
- `提交并恢复运行`.

Project status:

- Add `awaiting_selection` status label as `等待选方案`.
- Add `selection_pending`, `selection_gate`, and `selection_deadline` fields to the project status schema.
- `workspaceTabs()` should mark the selection tab as attention when `selection_pending` is true.

## Diagnostics

Use reason code:

```text
OPTION_SELECTION_PENDING
```

Suggested actions:

- `open_selection_request`;
- `open_selection_evidence`;
- `refresh_status`.

This should be separate from consultation reason codes so users can distinguish
structured choice from open-ended advice.

## Interaction With Delivery Contract

The selection gate is upstream of Step 16 and does not change the current
delivery contract. It should not affect:

- `scripts/workflow_state.py` Gate 2 / Step 8.5 / Step 16 predicates;
- `scripts/delivery_contract.py` manifest classification;
- `scripts/audit_complete_projects.py` legacy/current audit.

If selection is enabled, the final `delivery_manifest.json` should remain a
delivery artifact manifest, not a full decision log. Decision provenance lives
in `selection/*_decision.json` and `human_review.md`.

## Test Plan

Unit tests:

- parse valid and missing `selection/config.json`;
- pick default option by sorting policy;
- write human and timeout decisions idempotently;
- mirror Step 3 decisions into `human_review.md`;
- mirror Step 4 decisions into `human_review.md`;
- backend rejects unknown option IDs.

Runner regression tests:

- disabled selection config leaves Step 3 behavior unchanged;
- enabled Step 3 gate exits cleanly with awaiting status;
- existing decision lets Step 3 continue;
- timeout-created decision lets Step 3 continue.

Frontend tests can stay light in Phase 1:

- API normalizer handles `selection_pending`;
- `SelectionPanel` renders options and emits selected decision payload.

## Rollout

Phase 1 should implement only Step 3. Step 4 selection should wait until the
Step 3 path is stable in real projects. Step 5 solver-strategy selection remains
out of scope until Step 4 variants show a real need.

The implementation should preserve the current default behavior: projects that
do not contain `selection/config.json` must run byte-for-byte through the old
Step 3 path except for harmless logging.
