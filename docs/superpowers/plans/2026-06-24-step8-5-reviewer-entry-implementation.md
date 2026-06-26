# Step 8.5 Reviewer Entry Design Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不重编号既有 16 个主步骤的前提下，把 `Step 8.5 Reviewer Entry Design` 作为 `Step 9` 之前的编辑 gate 落地，并让 runner、prompts、Web 时间线和模型配置都能正确消费它。

**Architecture:** 实现分两层：第一层用一个可测试的 Python helper `scripts/step8_5_gate.py` 统一解析 `reviewer_entry_map.md` / `anchor_figure_plan.md` / `entry_gate.md` 的存在性和 verdict；第二层在 `run_paper.sh` 里把 Step 8.5 作为 `Step 9` 前置 gate 执行，保持整数主循环和 `infer-step` 兼容。Web 端不修改 `current_step` 的整数语义，而是在 `stepsData` 中暴露一个虚拟 `8.5` 节点并在时间线中渲染。

**Tech Stack:** Bash (`run_paper.sh`), Python 3.13, FastAPI, Vue 3 + Vite, pytest, ripgrep.

**Execution note:** 这份计划假定在独立 worktree 中执行，避免和当前脏工作区的无关改动互相污染。

---

### Task 1: 可测试的 Step 8.5 gate 解析器

**Files:**
- Create: `scripts/step8_5_gate.py`
- Create: `tests/test_step8_5_gate.py`

- [ ] **Step 1: 先写 gate 解析器的失败测试**

```python
# tests/test_step8_5_gate.py
from pathlib import Path


def write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_missing_gate_files_returns_missing(tmp_path):
    from step8_5_gate import collect_step8_5_state

    state = collect_step8_5_state(tmp_path)
    assert state["status"] == "missing"
    assert state["verdict"] is None
    assert state["ready"] is False
    assert state["artifacts_complete"] is False


def test_revise_gate_is_not_ready(tmp_path):
    from step8_5_gate import collect_step8_5_state

    write_file(tmp_path / "reviewer_entry_map.md", "# map\n")
    write_file(tmp_path / "anchor_figure_plan.md", "# anchors\n")
    write_file(tmp_path / "entry_gate.md", "# Step 8.5 Entry Gate\n\nVERDICT: REVISE\n")

    state = collect_step8_5_state(tmp_path)
    assert state["status"] == "revise"
    assert state["verdict"] == "REVISE"
    assert state["ready"] is False
    assert state["artifacts_complete"] is True


def test_pass_gate_is_ready(tmp_path):
    from step8_5_gate import collect_step8_5_state

    write_file(tmp_path / "reviewer_entry_map.md", "# map\n")
    write_file(tmp_path / "anchor_figure_plan.md", "# anchors\n")
    write_file(tmp_path / "entry_gate.md", "# Step 8.5 Entry Gate\n\nVERDICT: PASS\n")

    state = collect_step8_5_state(tmp_path)
    assert state["status"] == "pass"
    assert state["verdict"] == "PASS"
    assert state["ready"] is True
    assert state["artifacts_complete"] is True
```

- [ ] **Step 2: 跑测试，确认当前失败**

Run: `python3 -m pytest tests/test_step8_5_gate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'step8_5_gate'`

- [ ] **Step 3: 写最小 gate 解析器实现**

```python
#!/usr/bin/env python3
# scripts/step8_5_gate.py
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


VERDICT_RE = re.compile(r"(?m)^VERDICT:\s*(PASS|REVISE)\s*$")
REQUIRED_FILES = (
    "reviewer_entry_map.md",
    "anchor_figure_plan.md",
    "entry_gate.md",
)


def collect_step8_5_state(project_dir: str | Path) -> dict:
    project = Path(project_dir)
    files = {name: project / name for name in REQUIRED_FILES}
    existing = {name: path.is_file() and path.stat().st_size > 0 for name, path in files.items()}
    artifacts_complete = all(existing.values())
    verdict = None
    if existing["entry_gate.md"]:
        text = files["entry_gate.md"].read_text(encoding="utf-8", errors="replace")
        match = VERDICT_RE.search(text)
        verdict = match.group(1) if match else None

    if not artifacts_complete:
        status = "missing"
    elif verdict == "PASS":
        status = "pass"
    elif verdict == "REVISE":
        status = "revise"
    else:
        status = "invalid"

    return {
        "status": status,
        "verdict": verdict,
        "ready": verdict == "PASS" and artifacts_complete,
        "artifacts_complete": artifacts_complete,
        "files": {name: str(path) for name, path in files.items()},
        "present": existing,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--verdict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    state = collect_step8_5_state(args.project_dir)
    if args.verdict:
        print(state["verdict"] or "")
    elif args.json:
        print(json.dumps(state, ensure_ascii=False))
    else:
        print(state["status"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 重新跑测试，确认通过**

Run: `python3 -m pytest tests/test_step8_5_gate.py -v`
Expected: `3 passed`

- [ ] **Step 5: 做一条 CLI 烟雾检查**

Run:

```bash
tmp=$(mktemp -d)
printf '# map\n' > "$tmp/reviewer_entry_map.md"
printf '# anchors\n' > "$tmp/anchor_figure_plan.md"
printf '# gate\n\nVERDICT: PASS\n' > "$tmp/entry_gate.md"
python3 scripts/step8_5_gate.py "$tmp" --verdict
python3 scripts/step8_5_gate.py "$tmp" --json
```

Expected:
- 第一条命令输出 `PASS`
- 第二条命令输出包含 `"status": "pass"` 和 `"ready": true`

- [ ] **Step 6: Commit**

```bash
git add scripts/step8_5_gate.py tests/test_step8_5_gate.py
git commit -m "feat: add step 8.5 gate parser"
```

---

### Task 2: 工作流契约与 Step 8.5 prompt

**Files:**
- Create: `prompts/step8_5_reviewer_entry.txt`
- Modify: `STEPS.md`
- Modify: `README.md`

- [ ] **Step 1: 先写 Step 8.5 prompt 文件**

```text
你是 Modeling Factory 的 Step 8.5 Reviewer Entry Design agent。Step 8 已经完成图表精修，Step 9 尚未开始论文起稿。本步骤的任务不是写整篇论文，而是为 CUMCM 评委前 3 分钟的阅读路径定义统一入口。

你必须产出：
1. reviewer_entry_map.md
2. anchor_figure_plan.md
3. entry_gate.md

你必须逐问给出：
- 方法主线句
- 最终交付句
- 可信度句
- 摘要压缩版
- 正文首段承接版

禁止事项：
- 不改数值
- 不改模型
- 不重画图
- 不直接写 paper.tex
- 不写 m1/m2 / workflow / runner / cache / fallback 等内部工程词
```

- [ ] **Step 2: 在 `STEPS.md` 插入 Step 8.5 章节，并保留“16 个主步骤 + 1 个辅助 gate”的表述**

```markdown
### Step 8.5: Reviewer Entry Design

Produce:
- `reviewer_entry_map.md`
- `anchor_figure_plan.md`
- `entry_gate.md`

This is an editorial gate inserted between Step 8 and Step 9. It does not renumber the main 16-step workflow. Its job is to align abstract skeleton, visual anchors, and section-opening paragraphs before paper drafting begins.
```

并在 Step 9 小节中补一行：

```markdown
Pull content from: `reviewer_entry_map.md`, `anchor_figure_plan.md`, `entry_gate.md`, `problem/`, `model.md`, `symbol_table.md`, `assumption_ledger.md`, `results/`, `sensitivity_report.md`, `evaluation.md`, `visualization_log.md`.
```

- [ ] **Step 3: 在 `README.md` 的工作流概览中补充 Step 8.5**

```markdown
- Step 8.5：阅卷入口设计。为每个子问题定义评委入口三句式、主图/主表锚点和正文首段承接提纲。
```

- [ ] **Step 4: 用 grep 做契约检查**

Run:

```bash
rg -n "Step 8.5|reviewer_entry_map|anchor_figure_plan|entry_gate" STEPS.md README.md prompts/step8_5_reviewer_entry.txt
```

Expected:
- 三个文件都命中 `Step 8.5`
- `STEPS.md` 和 prompt 同时出现 3 个新产物文件名

- [ ] **Step 5: Commit**

```bash
git add STEPS.md README.md prompts/step8_5_reviewer_entry.txt
git commit -m "docs: add step 8.5 workflow contract and prompt"
```

---

### Task 3: runner 集成 Step 8.5，并保持整数主循环

**Files:**
- Modify: `run_paper.sh`
- Modify: `scripts/evaluate_modeling_project.py`
- Test: `tests/test_step8_5_gate.py`

- [ ] **Step 1: 先写 runner 集成前的契约说明注释**

在 `run_paper.sh` 的 Step 8 / 9 相邻区域上方加注释，明确：

```bash
# Step 8.5 is implemented as a pre-Step-9 editorial gate.
# We do not renumber the integer main loop. Instead, Step 9 refuses to draft
# the paper until reviewer_entry_map.md + anchor_figure_plan.md + entry_gate.md
# exist and entry_gate.md says VERDICT: PASS.
```

- [ ] **Step 2: 扩展 `dispatch_step`，支持单独的模型配置键**

把签名从：

```bash
dispatch_step() {
    local prompt_file="$1" timeout="$2" hang="$3" default_fn="$4"
```

改成：

```bash
dispatch_step() {
    local prompt_file="$1" timeout="$2" hang="$3" default_fn="$4"
    local step_key="${5:-$NEXT}"
    local ids primary fallback
    ids="$(get_step_model_ids "$step_key")"
```

这样 Step 8.5 可以单独读取 `step_8_5` 覆盖，而不抢占 `step_9`。

- [ ] **Step 3: 加 Step 8.5 helper 和执行函数**

在 `run_paper.sh` 的 Step 7/8/9 区域新增：

```bash
step8_5_verdict() {
    python3 "$FACTORY/scripts/step8_5_gate.py" "$PROJECT" --verdict 2>/dev/null || true
}

step8_5_passed() {
    [[ "$(step8_5_verdict)" == "PASS" ]]
}

run_step_8_5() {
    dispatch_step step8_5_reviewer_entry.txt 7200 1800 run_claude_then_codex 8_5
}
```

- [ ] **Step 4: 把 Step 8.5 挂到 `run_step_9()` 前置逻辑**

把当前的：

```bash
run_step_9()  { dispatch_step step9_paper_draft.txt 14400 3600 run_claude_then_codex; }
```

改成：

```bash
run_step_9() {
    if ! step8_5_passed; then
        log "   Step 9 preflight: running Step 8.5 Reviewer Entry Design"
        run_step_8_5 || return $?
        local verdict
        verdict="$(step8_5_verdict)"
        if [[ "$verdict" != "PASS" ]]; then
            log "   Step 8.5 verdict ${verdict:-<missing>} — stop before paper draft"
            return 42
        fi
    fi
    dispatch_step step9_paper_draft.txt 14400 3600 run_claude_then_codex
}
```

- [ ] **Step 5: 在主循环里把 Step 8.5 的 `REVISE` 当作“停在 Step 8 和 Step 9 之间”的显式停止**

在 case dispatch 之后、`verify_step "$NEXT"` 之前保存退出码：

```bash
STEP_RC=$?
set -e
```

然后插入：

```bash
if (( NEXT == 9 )) && (( STEP_RC == 42 )); then
    log "   Step 8.5 requires revision — leaving checkpoint at Step 8"
    _set_checkpoint_step 8
    exit 1
fi
```

不要让它走普通 retry 路径，否则会把编辑 gate 当成瞬时失败重试。

- [ ] **Step 6: 让 `scripts/evaluate_modeling_project.py` 接受新契约**

在必需 artifact 列表里追加：

```python
for name in [
    "reviewer_entry_map.md",
    "anchor_figure_plan.md",
    "entry_gate.md",
    # existing artifacts...
]:
```

并新增一条检查：

```python
entry_gate = project / "entry_gate.md"
gate_text = read_text(entry_gate) if entry_gate.is_file() else ""
ev.add(
    "entry_gate_verdict",
    "VERDICT: PASS" in gate_text,
    "entry_gate PASS" if "VERDICT: PASS" in gate_text else "entry_gate missing or not PASS",
)
```

- [ ] **Step 7: 跑自动测试 + 两条 runner 烟雾检查**

Run:

```bash
python3 -m pytest tests/test_step8_5_gate.py -v
tmp=$(mktemp -d)
mkdir -p "$tmp/demo/figures"
printf '%s\n' '- **Last completed step**: 8' > "$tmp/demo/checkpoint.md"
printf '# viz\n' > "$tmp/demo/visualization_log.md"
printf 'fake' > "$tmp/demo/figures/demo.pdf"
./run_paper.sh --infer-step "$tmp/demo"
python3 scripts/step8_5_gate.py "$tmp/demo" --json
```

Expected:
- pytest 继续 PASS
- `--infer-step` 输出 `8`
- gate JSON 输出 `"status": "missing"`

- [ ] **Step 8: Commit**

```bash
git add run_paper.sh scripts/evaluate_modeling_project.py
git commit -m "feat: gate paper drafting on step 8.5 reviewer entry"
```

---

### Task 4: 让 Step 8 / 9 / 11 / 13 / 14 prompts 消费新产物

**Files:**
- Modify: `prompts/step8_visualization.txt`
- Modify: `prompts/step9_paper_draft.txt`
- Modify: `prompts/step11_constructive_review.txt`
- Modify: `prompts/step13_gate2_judge.txt`
- Modify: `prompts/step14_abstract.txt`

- [ ] **Step 1: 先在 Step 8 prompt 里补“候选主锚点”职责**

在 `prompts/step8_visualization.txt` 的 `visualization_log.md` 说明区补一列提示：

```text
- 若某问已有明显主图 / 主表候选，在备注中标明“候选主锚点”，供 Step 8.5 直接消费。
```

- [ ] **Step 2: 把 Step 9 prompt 改成强制读取 Step 8.5 三文件**

在“必读资料”区新增：

```text
7. `__PROJECT_PATH__/reviewer_entry_map.md` —— 每问正文首段和摘要骨架的第一来源。
8. `__PROJECT_PATH__/anchor_figure_plan.md` —— 每问主图 / 主表锚点的第一来源。
9. `__PROJECT_PATH__/entry_gate.md` —— 若 verdict 不是 PASS，本步骤不得继续。
```

并在执行节奏里插入：

```text
1. 先检查 entry_gate.md 必须为 VERDICT: PASS。
2. 每问先用 reviewer_entry_map.md 的“正文首段承接版”起段，再展开公式和过程。
3. 主图 / 主表优先按 anchor_figure_plan.md 摆放，而不是自由挑选。
```

- [ ] **Step 3: 给 Step 11 / Step 13 加“入口一致性”检查**

Step 11 增加：

```text
- reviewer_entry_map.md 的每问交付是否真的被 Step 9 写进了正文首段。
- anchor_figure_plan.md 的主锚点是否真的被放到评委优先看到的位置。
```

Step 13 增加：

```text
- 评委只看摘要素材、主图锚点和正文首段时，三者是否仍是同一套叙事。
```

- [ ] **Step 4: 改 Step 14 的来源优先级**

把来源顺序改成：

```text
1. human_review.md
2. reviewer_entry_map.md
3. judge_evaluation.md
```

并在执行节奏中明确：

```text
优先压缩 reviewer_entry_map.md 的“摘要压缩版”和“总体主线”，judge_evaluation.md 只用于补强亮点和评委视角。
```

- [ ] **Step 5: 跑 prompt 关键字检查**

Run:

```bash
rg -n "reviewer_entry_map|anchor_figure_plan|entry_gate|候选主锚点|来源优先级" \
  prompts/step8_visualization.txt \
  prompts/step9_paper_draft.txt \
  prompts/step11_constructive_review.txt \
  prompts/step13_gate2_judge.txt \
  prompts/step14_abstract.txt
```

Expected:
- 五个 prompt 都至少命中新引入的一个 Step 8.5 关键词
- `step9_paper_draft.txt` 和 `step14_abstract.txt` 都命中 `reviewer_entry_map`

- [ ] **Step 6: Commit**

```bash
git add \
  prompts/step8_visualization.txt \
  prompts/step9_paper_draft.txt \
  prompts/step11_constructive_review.txt \
  prompts/step13_gate2_judge.txt \
  prompts/step14_abstract.txt
git commit -m "feat: wire step 8.5 artifacts into paper prompts"
```

---

### Task 5: 后端状态、artifact API 和模型配置键支持 `step_8_5`

**Files:**
- Modify: `web/backend/app.py`
- Create: `tests/test_web_step8_5_metadata.py`

- [ ] **Step 1: 先写后端元数据测试**

```python
# tests/test_web_step8_5_metadata.py
from pathlib import Path


def write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_get_steps_exposes_editorial_gate(tmp_path):
    from web.backend.app import get_steps

    write_file(tmp_path / "checkpoint.md", "- **Last completed step**: 8\n")
    write_file(tmp_path / "reviewer_entry_map.md", "# map\n")
    write_file(tmp_path / "anchor_figure_plan.md", "# anchors\n")
    write_file(tmp_path / "entry_gate.md", "# gate\n\nVERDICT: REVISE\n")

    data = get_steps(tmp_path, "demo")
    assert data["current_step"] == 8
    assert data["editorial_gate"]["verdict"] == "REVISE"
    assert data["editorial_gate"]["ready"] is False


def test_valid_step_key_accepts_step_8_5():
    from web.backend.app import _valid_model_step_key

    assert _valid_model_step_key("step_8_5") is True
    assert _valid_model_step_key("step_9") is True
    assert _valid_model_step_key("step_nine") is False
```

- [ ] **Step 2: 跑测试，确认当前失败**

Run: `python3 -m pytest tests/test_web_step8_5_metadata.py -v`
Expected: FAIL with missing `_valid_model_step_key` and missing `editorial_gate`

- [ ] **Step 3: 在后端加 editorial gate 元数据和 step key 校验 helper**

在 `web/backend/app.py` 增加：

```python
def _valid_model_step_key(step_key: str) -> bool:
    return bool(re.fullmatch(r"step_(?:\d+|8_5)", step_key))


def _read_editorial_gate(project: Path) -> dict:
    files = ["reviewer_entry_map.md", "anchor_figure_plan.md", "entry_gate.md"]
    artifacts = []
    for rel in files:
        p = project / rel
        if p.is_file():
            artifacts.append(_meta(project, p, "step"))

    verdict = None
    entry_gate = project / "entry_gate.md"
    if entry_gate.is_file():
        m = re.search(r"(?m)^VERDICT:\s*(PASS|REVISE)\s*$", entry_gate.read_text(errors="ignore"))
        if m:
            verdict = m.group(1)

    return {
        "key": "8_5",
        "verdict": verdict,
        "ready": verdict == "PASS",
        "artifacts": artifacts,
        "present": len(artifacts) == 3,
    }
```

把 `get_steps()` 返回值扩成：

```python
editorial_gate = _read_editorial_gate(project)
return {
    "current_step": current,
    "steps": steps,
    "editorial_gate": editorial_gate,
    "verdict": verdict,
    "open_issues": open_issues,
    "paper_available": paper is not None,
}
```

并把 `put_model_config()` 的校验从：

```python
if not re.fullmatch(r"step_\d+", step_key):
```

改成：

```python
if not _valid_model_step_key(step_key):
```

- [ ] **Step 4: 重新跑测试，确认通过**

Run: `python3 -m pytest tests/test_web_step8_5_metadata.py -v`
Expected: `2 passed`

- [ ] **Step 5: 做一个 API 级别烟雾验证**

Run:

```bash
python3 -m pytest tests/test_web_step8_5_metadata.py -v
python3 - <<'PY'
from web.backend.app import _valid_model_step_key
print(_valid_model_step_key("step_8_5"))
print(_valid_model_step_key("step_14"))
PY
```

Expected:
- pytest PASS
- Python 片段输出两行 `True`

- [ ] **Step 6: Commit**

```bash
git add web/backend/app.py tests/test_web_step8_5_metadata.py
git commit -m "feat: expose step 8.5 metadata in dashboard backend"
```

---

### Task 6: 前端时间线、详情页和模型管理显示虚拟 Step 8.5

**Files:**
- Modify: `web/frontend/src/lib/steps.js`
- Modify: `web/frontend/src/components/PipelineTimeline.vue`
- Modify: `web/frontend/src/components/ProjectWorkspace.vue`
- Modify: `web/frontend/src/components/ModelManager.vue`

- [ ] **Step 1: 在 `steps.js` 增加虚拟步骤定义和配置键 helper**

```js
export const EDITORIAL_GATE_STEP = {
  key: '8_5',
  index: 8.5,
  kind: 'gate',
  name: '阅卷入口设计',
  en: 'Reviewer Entry',
  icon: 'eye',
}

export function stepConfigKey(step) {
  if (step && step.key) return `step_${step.key}`
  return `step_${step.index}`
}
```

并在 `STEP_MODEL_META` 里追加：

```js
'8_5': { overridable: true, apiOk: false, default: 'Claude → Codex' },
```

同时把 `stepModelMeta(index)` 改成支持字符串键：

```js
export function stepModelMeta(index) {
  return STEP_MODEL_META[index] || STEP_MODEL_META[String(index)] || { overridable: true, apiOk: false, default: '' }
}
```

- [ ] **Step 2: 让 `PipelineTimeline.vue` 在 Step 8 和 Step 9 之间插入虚拟节点**

把当前直接使用 `STEPS` 的方式改成：

```js
import { STEPS, EDITORIAL_GATE_STEP, stepModelMeta, stepConfigKey } from '../lib/steps.js'

computed: {
  timelineSteps() {
    return [...STEPS.slice(0, 9), EDITORIAL_GATE_STEP, ...STEPS.slice(9)]
  },
  sel() { return this.timelineSteps[this.selectedIndex] || this.timelineSteps[0] },
  selArtifacts() {
    if (this.sel.key === '8_5') return this.stepsData?.editorial_gate?.artifacts || []
    return this.stepsData?.steps?.[this.sel.index]?.artifacts || []
  },
}
```

并在 `state(s)` 里 special-case `8_5`：

```js
if (s.key === '8_5') {
  const gate = this.stepsData?.editorial_gate
  if (!gate) return 'pending'
  if (gate.ready) return 'done'
  if (this.currentStep >= 8) return this.awaiting ? 'attention' : 'live'
  return 'pending'
}
```

- [ ] **Step 3: 让 `ProjectWorkspace.vue` 在 Step 8 之后显示 `STEP 8.5 / 16`**

把 `stepLabel()` 改成：

```js
stepLabel() {
  const c = this.project.current_step
  const gate = this.stepsData?.editorial_gate
  if (c >= 16) return 'STEP 16 / 16 · 已完成'
  if (c === 8 && gate && !gate.ready) return 'STEP 8.5 / 16 · 阅卷入口设计'
  const active = stepByIndex(Math.min(16, c + 1))
  return `STEP ${Math.max(0, c + 1)} / 16 · ${active ? active.name : ''}`
}
```

- [ ] **Step 4: 让 `ModelManager.vue` 和时间线赋值都使用 `step_8_5` 键**

在 `ModelManager.vue` 中，overridable steps 也插入 `EDITORIAL_GATE_STEP`：

```js
computed: {
  overridableSteps() {
    return [...STEPS.filter((s) => stepModelMeta(s.index).overridable).slice(0, 9),
      EDITORIAL_GATE_STEP,
      ...STEPS.filter((s) => stepModelMeta(s.index).overridable).slice(9)]
  },
}
```

保存默认模型时，不再硬拼 `step_${index}`，而是：

```js
import { stepConfigKey } from '../lib/steps.js'
const key = stepConfigKey(step)
```

`PipelineTimeline.vue` 里的 `selAssign` 和 emit 逻辑也一样改用 `stepConfigKey(this.sel)`。

- [ ] **Step 5: 跑前端构建验证**

Run: `npm --prefix web/frontend run build`
Expected: `vite build` 成功，无类型/语法错误

- [ ] **Step 6: Commit**

```bash
git add \
  web/frontend/src/lib/steps.js \
  web/frontend/src/components/PipelineTimeline.vue \
  web/frontend/src/components/ProjectWorkspace.vue \
  web/frontend/src/components/ModelManager.vue
git commit -m "feat: show virtual step 8.5 in dashboard timeline"
```

---

### Task 7: 文档收尾与全链路验证

**Files:**
- Modify: `web/README.md`
- Modify: `docs/superpowers/specs/2026-06-24-step8-5-reviewer-entry-design.md` (only if implementation diverged)

- [ ] **Step 1: 更新 Web 文档中的模型配置和步骤说明**

在 `web/README.md` 的模型配置段补一条：

```markdown
- `step_8_5`：Step 8.5 Reviewer Entry Design，默认建议使用 agentic 模型（Claude / Codex）。
```

在功能说明里补一条：

```markdown
- 工作台时间线会在 Step 8 和 Step 9 之间显示一个虚拟的 `8.5` 节点，用于提示“阅卷入口设计”是否完成。
```

- [ ] **Step 2: 运行完整验证矩阵**

Run:

```bash
python3 -m pytest \
  tests/test_step8_5_gate.py \
  tests/test_web_step8_5_metadata.py \
  tests/test_render_and_cli.py \
  tests/test_collect_all.py -v

python3 -m compileall scripts/step8_5_gate.py web/backend/app.py

npm --prefix web/frontend run build

rg -n "Step 8.5|reviewer_entry_map|anchor_figure_plan|entry_gate|step_8_5" \
  STEPS.md README.md web/README.md run_paper.sh prompts web/backend/app.py web/frontend/src
```

Expected:
- pytest 全绿
- `compileall` 无报错
- 前端构建成功
- grep 命中 runner、prompts、backend、frontend、docs 五个区域

- [ ] **Step 3: 做一次手工项目烟雾验证**

Run:

```bash
tmp=$(mktemp -d)
proj="$tmp/demo"
mkdir -p "$proj/figures"
cat > "$proj/checkpoint.md" <<'EOF'
# Project Checkpoint
- **Last completed step**: 8
EOF
printf '# viz\n' > "$proj/visualization_log.md"
printf 'fake' > "$proj/figures/solve_p1_demo.pdf"
printf '# map\n' > "$proj/reviewer_entry_map.md"
printf '# anchors\n' > "$proj/anchor_figure_plan.md"
printf '# gate\n\nVERDICT: REVISE\n' > "$proj/entry_gate.md"
./run_paper.sh --infer-step "$proj"
python3 scripts/step8_5_gate.py "$proj" --json
```

Expected:
- `--infer-step` 仍输出 `8`
- helper JSON 输出 `status=revise`
- 这证明 runner 的主步骤编号保持兼容，而 Step 8.5 状态由 gate 文件单独表达

- [ ] **Step 4: Commit**

```bash
git add web/README.md
git commit -m "docs: document step 8.5 dashboard behavior"
```

---

## Self-Review

### Spec coverage

- `Step 8.5` 新增 prompt 与三文件产物：Task 2
- 不重编号后续主步骤：Task 3（整数主循环 + Step 9 前置 gate）
- `entry_gate.md` 明确 `PASS / REVISE`：Task 1 + Task 3
- Step 9 / 11 / 13 / 14 消费新产物：Task 4
- Dashboard 显示虚拟 `8.5` 节点：Task 5 + Task 6
- 模型配置支持 `step_8_5`：Task 5 + Task 6
- 文档与 README 更新：Task 2 + Task 7

### Placeholder scan

- 计划中没有 `TODO` / `TBD` / “适当处理” / “写测试” 这类空描述。
- 每个代码任务都附了实际代码片段。
- 每个验证步骤都附了精确命令和预期结果。

### Type consistency

- Step 8.5 的 runner 键统一为 `step_8_5`
- gate 文件统一为 `reviewer_entry_map.md` / `anchor_figure_plan.md` / `entry_gate.md`
- `editorial_gate` 是 backend → frontend 的统一 JSON 字段名
- `VERDICT: PASS | REVISE` 是 helper、runner、backend 和前端共同依赖的唯一 verdict 语义

