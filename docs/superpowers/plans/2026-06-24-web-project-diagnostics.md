# Web Project Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为每个建模项目落地一套可解释的 Web 诊断层，让用户能在 30 秒内知道项目为何未前进、证据在哪、以及当前可执行的下一步动作。

**Architecture:** 诊断能力分三层实现。第一层在 `scripts/project_diagnostics.py` 中提供共享的快照/事件读写原语，并由 `scripts/runner_diagnostics.sh` 包装后供 `run_paper.sh` 调用。第二层在 `web/backend/diagnostics_service.py` 中消费 runner 诊断文件并做最少量 fallback 推断，再把摘要挂到项目列表和单项目诊断接口。第三层在 Vue 工作台新增诊断卡和列表徽章，动作统一落到既有的日志、产物和项目控制入口，不新建复杂前端状态机。

**Tech Stack:** Bash (`run_paper.sh`), Python 3, FastAPI, Vue 3 + Vite, pytest, Node.js, ripgrep.

---

## File Map

- Create: `scripts/project_diagnostics.py`
  - 共享的诊断快照/事件读写 helper，runner 直接通过 CLI 调用，backend 直接 import。
- Create: `scripts/runner_diagnostics.sh`
  - Bash 薄包装，隐藏 `python3 scripts/project_diagnostics.py ...` 的参数拼装。
- Create: `web/backend/diagnostics_service.py`
  - 诊断读取、fallback 推断、动作映射、列表摘要逻辑。
- Create: `web/frontend/src/components/DiagnosticsCard.vue`
  - 工作台顶部的诊断卡组件，展示主结论、依据、最近事件和动作按钮。
- Create: `web/frontend/src/lib/diagnostics.js`
  - 纯前端诊断文案与动作 label helper，避免把映射散落在组件里。
- Create: `tests/test_project_diagnostics.py`
  - 共享 helper 的读写与事件裁剪测试。
- Create: `tests/test_runner_diagnostics.py`
  - Bash wrapper 的烟雾测试，验证 shell 调用能产出正确的诊断文件。
- Create: `tests/test_web_diagnostics_backend.py`
  - backend 诊断服务的 runner 优先 / fallback / 动作映射测试。
- Modify: `run_paper.sh`
  - source wrapper，并在 stale lock、step start/complete、8.5 gate、consultation、verify failure、activity monitor 等关键分支写入诊断。
- Modify: `web/backend/app.py`
  - 引入诊断服务，扩展 `ProjectStatus`，新增 `/api/projects/{base_name}/diagnostics`，并把证据文件加入 artifact 列表。
- Modify: `web/frontend/src/lib/api.js`
  - 新增 `Projects.diagnostics()` helper。
- Modify: `web/frontend/src/components/ProjectCard.vue`
  - 渲染列表页诊断徽章。
- Modify: `web/frontend/src/components/ProjectWorkspace.vue`
  - 拉取 diagnostics、渲染 `DiagnosticsCard`、把动作路由到既有日志/产物/项目控制入口。
- Modify: `web/frontend/src/components/ArtifactBrowser.vue`
  - 暴露 `diagnostics` 分组，使 `runner.log`、`entry_gate.md`、`human_review.md` 等证据可点击。
- Modify: `web/README.md`
  - 记录新 API 和诊断交互方式。

---

### Task 1: 共享诊断 helper 与 CLI

**Files:**
- Create: `scripts/project_diagnostics.py`
- Test: `tests/test_project_diagnostics.py`

- [ ] **Step 1: 先写 helper 的失败测试**

```python
# tests/test_project_diagnostics.py
import json
from pathlib import Path

from project_diagnostics import (
    append_event,
    diagnostics_dir,
    load_recent_events,
    load_status,
    write_status,
)


def test_write_status_round_trips_snapshot(tmp_path):
    write_status(
        tmp_path,
        state="waiting",
        current_step=8,
        current_action="step8_5_gate_review",
        reason_code="AWAITING_STEP8_5",
        reason_summary="Step 8.5 未通过",
        suggested_actions=["open_entry_gate", "refresh_status"],
        evidence=[{"kind": "file", "path": "entry_gate.md"}],
        since=1700000000,
    )

    status = load_status(tmp_path)
    assert status["state"] == "waiting"
    assert status["current_step"] == 8
    assert status["reason_code"] == "AWAITING_STEP8_5"
    assert status["suggested_actions"] == ["open_entry_gate", "refresh_status"]


def test_load_status_returns_none_when_missing(tmp_path):
    assert load_status(tmp_path) is None
    assert diagnostics_dir(tmp_path) == tmp_path / "diagnostics"


def test_append_event_and_tail_recent_events(tmp_path):
    for idx in range(5):
        append_event(
            tmp_path,
            step=6,
            event_type="verification_failed",
            message=f"failure-{idx}",
            reason_code="VERIFY_OUTPUT_FAILED",
            files=["solve_log.md"],
            meta={"attempt": idx + 1},
        )

    events = load_recent_events(tmp_path, limit=2)
    assert [ev["message"] for ev in events] == ["failure-3", "failure-4"]
    assert events[-1]["meta"]["attempt"] == 5
```

- [ ] **Step 2: 跑测试，确认当前失败**

Run: `python3 -m pytest tests/test_project_diagnostics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'project_diagnostics'`

- [ ] **Step 3: 写最小共享 helper 与 CLI**

```python
#!/usr/bin/env python3
# scripts/project_diagnostics.py
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


VERSION = 1
STATUS_FILE = "status.json"
EVENTS_FILE = "events.jsonl"


def diagnostics_dir(project_dir: str | Path) -> Path:
    return Path(project_dir) / "diagnostics"


def _status_path(project_dir: str | Path) -> Path:
    return diagnostics_dir(project_dir) / STATUS_FILE


def _events_path(project_dir: str | Path) -> Path:
    return diagnostics_dir(project_dir) / EVENTS_FILE


def _ensure_dir(project_dir: str | Path) -> Path:
    path = diagnostics_dir(project_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def write_status(
    project_dir: str | Path,
    *,
    state: str,
    current_step: int,
    current_action: str,
    reason_code: str = "",
    reason_summary: str = "",
    suggested_actions: list[str] | None = None,
    evidence: list[dict] | None = None,
    since: int | None = None,
    last_event_at: int | None = None,
) -> dict:
    _ensure_dir(project_dir)
    now = int(time.time())
    payload = {
        "version": VERSION,
        "state": state,
        "current_step": current_step,
        "current_action": current_action,
        "reason_code": reason_code,
        "reason_summary": reason_summary,
        "since": since or now,
        "last_event_at": last_event_at or now,
        "suggested_actions": suggested_actions or [],
        "evidence": evidence or [],
    }
    _atomic_write_json(_status_path(project_dir), payload)
    return payload


def load_status(project_dir: str | Path) -> dict | None:
    path = _status_path(project_dir)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def append_event(
    project_dir: str | Path,
    *,
    step: int,
    event_type: str,
    message: str,
    reason_code: str = "",
    files: list[str] | None = None,
    meta: dict | None = None,
) -> dict:
    _ensure_dir(project_dir)
    event = {
        "ts": int(time.time()),
        "step": step,
        "type": event_type,
        "message": message,
        "reason_code": reason_code,
        "files": files or [],
        "meta": meta or {},
    }
    with _events_path(project_dir).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def load_recent_events(project_dir: str | Path, limit: int = 5) -> list[dict]:
    path = _events_path(project_dir)
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows[-limit:]


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    status_cmd = sub.add_parser("write-status")
    status_cmd.add_argument("project_dir")
    status_cmd.add_argument("--state", required=True)
    status_cmd.add_argument("--step", required=True, type=int)
    status_cmd.add_argument("--action", required=True)
    status_cmd.add_argument("--reason-code", default="")
    status_cmd.add_argument("--reason-summary", default="")
    status_cmd.add_argument("--since", type=int, default=None)
    status_cmd.add_argument("--suggested-action", action="append", default=[])
    status_cmd.add_argument("--evidence", action="append", default=[])

    event_cmd = sub.add_parser("append-event")
    event_cmd.add_argument("project_dir")
    event_cmd.add_argument("--step", required=True, type=int)
    event_cmd.add_argument("--type", required=True, dest="event_type")
    event_cmd.add_argument("--message", required=True)
    event_cmd.add_argument("--reason-code", default="")
    event_cmd.add_argument("--file", action="append", default=[])

    args = parser.parse_args()
    if args.cmd == "write-status":
        evidence = []
        for item in args.evidence:
            kind, value = item.split(":", 1)
            evidence.append({"kind": kind, "path": value} if kind == "file" else {"kind": kind, "value": value})
        payload = write_status(
            args.project_dir,
            state=args.state,
            current_step=args.step,
            current_action=args.action,
            reason_code=args.reason_code,
            reason_summary=args.reason_summary,
            suggested_actions=args.suggested_action,
            evidence=evidence,
            since=args.since,
        )
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    payload = append_event(
        args.project_dir,
        step=args.step,
        event_type=args.event_type,
        message=args.message,
        reason_code=args.reason_code,
        files=args.file,
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 重新跑 helper 测试，确认通过**

Run: `python3 -m pytest tests/test_project_diagnostics.py -v`
Expected: `3 passed`

- [ ] **Step 5: 做一条 CLI 烟雾验证**

Run:

```bash
tmp=$(mktemp -d)
python3 scripts/project_diagnostics.py write-status "$tmp" \
  --state waiting \
  --step 8 \
  --action step8_5_gate_review \
  --reason-code AWAITING_STEP8_5 \
  --reason-summary "Step 8.5 未通过" \
  --suggested-action open_entry_gate \
  --evidence file:entry_gate.md
python3 scripts/project_diagnostics.py append-event "$tmp" \
  --step 8 \
  --type gate_blocked \
  --message "Step 8.5 verdict is REVISE" \
  --reason-code AWAITING_STEP8_5 \
  --file entry_gate.md
cat "$tmp/diagnostics/status.json"
cat "$tmp/diagnostics/events.jsonl"
```

Expected:
- `status.json` 包含 `"reason_code": "AWAITING_STEP8_5"`
- `events.jsonl` 末行包含 `"type": "gate_blocked"`

- [ ] **Step 6: Commit**

```bash
git add scripts/project_diagnostics.py tests/test_project_diagnostics.py
git commit -m "feat: add shared project diagnostics helper"
```

---

### Task 2: Bash wrapper，避免把 CLI 参数散落进 runner

**Files:**
- Create: `scripts/runner_diagnostics.sh`
- Test: `tests/test_runner_diagnostics.py`

- [ ] **Step 1: 先写 shell wrapper 的失败测试**

```python
# tests/test_runner_diagnostics.py
import json
import subprocess
from pathlib import Path

from conftest import REPO_ROOT


def test_diag_status_shell_wrapper_writes_snapshot(tmp_path):
    cmd = f'''
set -euo pipefail
FACTORY="{REPO_ROOT}"
source "{REPO_ROOT}/scripts/runner_diagnostics.sh"
diag_status "{tmp_path}" waiting 8 step8_5_gate_review AWAITING_STEP8_5 "Step 8.5 未通过" "open_entry_gate,refresh_status" "file:entry_gate.md"
'''
    subprocess.run(["bash", "-lc", cmd], check=True)
    status = json.loads((tmp_path / "diagnostics" / "status.json").read_text(encoding="utf-8"))
    assert status["reason_code"] == "AWAITING_STEP8_5"
    assert status["suggested_actions"] == ["open_entry_gate", "refresh_status"]


def test_diag_event_shell_wrapper_appends_jsonl(tmp_path):
    cmd = f'''
set -euo pipefail
FACTORY="{REPO_ROOT}"
source "{REPO_ROOT}/scripts/runner_diagnostics.sh"
diag_event "{tmp_path}" 6 verification_failed VERIFY_OUTPUT_FAILED "Step 6 output invalid" "solve_log.md" "attempt=2"
'''
    subprocess.run(["bash", "-lc", cmd], check=True)
    rows = (tmp_path / "diagnostics" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    payload = json.loads(rows[-1])
    assert payload["type"] == "verification_failed"
    assert payload["reason_code"] == "VERIFY_OUTPUT_FAILED"
```

- [ ] **Step 2: 跑测试，确认当前失败**

Run: `python3 -m pytest tests/test_runner_diagnostics.py -v`
Expected: FAIL with `No such file or directory: scripts/runner_diagnostics.sh`

- [ ] **Step 3: 写 Bash wrapper**

```bash
#!/usr/bin/env bash
# scripts/runner_diagnostics.sh

diag_python() {
    python3 "${FACTORY:?FACTORY not set}/scripts/project_diagnostics.py" "$@"
}

diag_status() {
    local project="$1" state="$2" step="$3" action="$4" reason_code="$5" reason_summary="$6"
    local action_csv="${7:-}"
    local evidence="${8:-}"
    local -a cmd=(write-status "$project" --state "$state" --step "$step" --action "$action")
    [[ -n "$reason_code" ]] && cmd+=(--reason-code "$reason_code")
    [[ -n "$reason_summary" ]] && cmd+=(--reason-summary "$reason_summary")

    if [[ -n "$action_csv" ]]; then
        local IFS=','
        for item in $action_csv; do
            [[ -n "$item" ]] && cmd+=(--suggested-action "$item")
        done
    fi

    if [[ -n "$evidence" ]]; then
        local IFS=','
        for item in $evidence; do
            [[ -n "$item" ]] && cmd+=(--evidence "$item")
        done
    fi

    diag_python "${cmd[@]}" >/dev/null 2>&1 || true
}

diag_event() {
    local project="$1" step="$2" event_type="$3" reason_code="$4" message="$5"
    local file_path="${6:-}" meta="${7:-}"
    local -a cmd=(append-event "$project" --step "$step" --type "$event_type" --message "$message")
    [[ -n "$reason_code" ]] && cmd+=(--reason-code "$reason_code")
    [[ -n "$file_path" ]] && cmd+=(--file "$file_path")
    diag_python "${cmd[@]}" >/dev/null 2>&1 || true
}

export -f diag_python diag_status diag_event
```

- [ ] **Step 4: 重新跑 shell wrapper 测试**

Run: `python3 -m pytest tests/test_runner_diagnostics.py -v`
Expected: `2 passed`

- [ ] **Step 5: 做一条人工烟雾检查**

Run:

```bash
tmp=$(mktemp -d)
FACTORY="$PWD" bash -lc '
source "$FACTORY/scripts/runner_diagnostics.sh"
diag_status "'"$tmp"'" retrying 6 verification VERIFY_OUTPUT_FAILED "Step 6 output invalid" "open_runner_log,refresh_status" "file:solve_log.md"
diag_event "'"$tmp"'" 6 verification_failed VERIFY_OUTPUT_FAILED "Step 6 output invalid" "solve_log.md"
'
cat "$tmp/diagnostics/status.json"
cat "$tmp/diagnostics/events.jsonl"
```

Expected:
- `status.json` 中 `state` 为 `retrying`
- `events.jsonl` 中有一条 `verification_failed`

- [ ] **Step 6: Commit**

```bash
git add scripts/runner_diagnostics.sh tests/test_runner_diagnostics.py
git commit -m "feat: add runner diagnostics shell wrapper"
```

---

### Task 3: 在 `run_paper.sh` 关键控制流节点写诊断

**Files:**
- Modify: `run_paper.sh`
- Test: `tests/test_project_diagnostics.py`
- Test: `tests/test_runner_diagnostics.py`
- Test: `tests/test_run_paper_step8_5_infer.py`

- [ ] **Step 1: 先写一条针对 Step 8.5 诊断分支的回归断言**

```python
# tests/test_run_paper_step8_5_infer.py
import os
import subprocess
from pathlib import Path

from conftest import REPO_ROOT


def write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_infer_step_stays_at_8_with_step8_5_artifacts(tmp_path):
    project = tmp_path / "demo"
    write_file(project / "checkpoint.md", "- **Last completed step**: 8\n")
    write_file(project / "problem" / "problem_brief.md", "# brief\n")
    write_file(project / "visualization_log.md", "\n".join(["viz"] * 20) + "\n")
    write_file(project / "figures" / "demo.pdf", "fake\n")
    write_file(project / "reviewer_entry_map.md", "# map\n")
    write_file(project / "anchor_figure_plan.md", "# anchors\n")
    write_file(project / "entry_gate.md", "# gate\n\nVERDICT: REVISE\n")

    out = subprocess.run(
        [os.path.join(REPO_ROOT, "run_paper.sh"), "--infer-step", str(project)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "8"
```

- [ ] **Step 2: 先跑现有诊断相关测试，确认基线通过**

Run: `python3 -m pytest tests/test_project_diagnostics.py tests/test_runner_diagnostics.py tests/test_run_paper_step8_5_infer.py -v`
Expected: PASS（这是 runner 改动前的安全基线）

- [ ] **Step 3: 在 `run_paper.sh` source wrapper，并在关键分支写诊断**

```bash
# run_paper.sh
if [[ -f "$FACTORY/scripts/runner_diagnostics.sh" ]]; then
    # shellcheck disable=SC1091
    source "$FACTORY/scripts/runner_diagnostics.sh"
fi

diag_status "$PROJECT" running "$STEP" bootstrap runner_start "" "" ""

# stale lock reclaim
diag_event "$PROJECT" "${next_step:-0}" lock_stale_reclaimed LOCK_STALE_RECLAIMED \
    "Stale lock reclaimed: $stale_reason" "logs/runner.log"
diag_status "$PROJECT" waiting "${next_step:-0}" lock_recovery LOCK_STALE_RECLAIMED \
    "检测到陈旧锁并已回收" "open_runner_log,refresh_status" "file:logs/runner.log"

# every step attempt
diag_event "$PROJECT" "$NEXT" step_started "" "Step $NEXT started" "$STEP_LOG"
diag_status "$PROJECT" running "$NEXT" step_dispatch "" "" "" "file:$STEP_LOG"

# activity monitor no-progress branch
diag_status "$proj_dir" waiting "$step" activity_monitor NO_LOG_PROGRESS \
    "日志暂未增长，runner 仍在等待新活动信号" \
    "open_runner_log,refresh_status" "file:logs/runner.log"

# step 8.5 blocked
diag_event "$PROJECT" 8 gate_blocked AWAITING_STEP8_5 \
    "Step 8.5 verdict is not PASS" "entry_gate.md"
diag_status "$PROJECT" waiting 8 step8_5_gate_review AWAITING_STEP8_5 \
    "Step 8.5 未通过，等待补足 reviewer entry 材料" \
    "open_entry_gate,open_reviewer_entry_artifacts,refresh_status" \
    "file:entry_gate.md,file:reviewer_entry_map.md,file:anchor_figure_plan.md"

# consultation pending
diag_event "$PROJECT" "$step" consultation_requested CONSULTATION_PENDING \
    "Runner paused for human consultation" "consultation/${gate}_request.md"
diag_status "$PROJECT" waiting "$step" consultation_wait CONSULTATION_PENDING \
    "等待人工咨询回填" \
    "open_consultation_request,open_human_review,refresh_status" \
    "file:consultation/${gate}_request.md,file:human_review.md"

# verify failure -> retrying
diag_event "$PROJECT" "$NEXT" verification_failed VERIFY_OUTPUT_FAILED \
    "Step $NEXT output missing or invalid" "$STEP_LOG"
diag_status "$PROJECT" retrying "$NEXT" verification VERIFY_OUTPUT_FAILED \
    "Step $NEXT 产物校验未通过，等待重试" \
    "open_runner_log,refresh_status" "file:$STEP_LOG"

# verified / completed
diag_event "$PROJECT" "$NEXT" step_completed "" "Step $NEXT verified" "$STEP_LOG"
diag_status "$PROJECT" running "$NEXT" step_complete "" "" "" "file:$STEP_LOG"
if (( NEXT == 16 )); then
    diag_status "$PROJECT" completed 16 delivery_complete "" "项目已完成" "" "file:logs/runner.log"
fi
```

- [ ] **Step 4: 用语法检查和回归测试验证 runner 没被打坏**

Run:

```bash
bash -n run_paper.sh
python3 -m pytest tests/test_project_diagnostics.py tests/test_runner_diagnostics.py tests/test_run_paper_step8_5_infer.py -v
```

Expected:
- `bash -n run_paper.sh` 无输出并返回 0
- `pytest` 全部 PASS

- [ ] **Step 5: 用 grep 检查 5 个首批 reason code 都挂到了真实控制流**

Run:

```bash
rg -n "NO_LOG_PROGRESS|AWAITING_STEP8_5|CONSULTATION_PENDING|VERIFY_OUTPUT_FAILED|LOCK_STALE_RECLAIMED" run_paper.sh scripts/runner_diagnostics.sh
```

Expected:
- 5 个 code 全部命中
- `run_paper.sh` 至少命中 4 个控制流位置

- [ ] **Step 6: Commit**

```bash
git add run_paper.sh
git commit -m "feat: emit project diagnostics from runner"
```

---

### Task 4: backend 诊断服务、fallback 和 API

**Files:**
- Create: `web/backend/diagnostics_service.py`
- Modify: `web/backend/app.py`
- Test: `tests/test_web_diagnostics_backend.py`
- Test: `tests/test_web_step8_5_metadata.py`

- [ ] **Step 1: 先写 backend 诊断服务的失败测试**

```python
# tests/test_web_diagnostics_backend.py
from pathlib import Path

from web.backend.diagnostics_service import build_project_diagnostics, summarize_project_diagnostics


def write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_runner_status_beats_fallback(tmp_path):
    write_file(
        tmp_path / "diagnostics" / "status.json",
        """{
  "version": 1,
  "state": "waiting",
  "current_step": 8,
  "current_action": "step8_5_gate_review",
  "reason_code": "AWAITING_STEP8_5",
  "reason_summary": "Step 8.5 未通过",
  "since": 1700000000,
  "last_event_at": 1700000000,
  "suggested_actions": ["open_entry_gate"],
  "evidence": [{"kind": "file", "path": "entry_gate.md"}]
}
""",
    )
    diag = build_project_diagnostics(tmp_path, "demo", is_running=True, consultation_pending=False, consultation_gate=None)
    assert diag["source"] == "runner"
    assert diag["status"]["reason_code"] == "AWAITING_STEP8_5"


def test_fallback_detects_step8_5_gate_wait(tmp_path):
    write_file(tmp_path / ".heartbeat", "AWAITING_STEP8_5:8 1700000000\n")
    write_file(tmp_path / "entry_gate.md", "# gate\n\nVERDICT: REVISE\n")
    diag = build_project_diagnostics(tmp_path, "demo", is_running=False, consultation_pending=False, consultation_gate=None)
    assert diag["source"] == "fallback"
    assert diag["status"]["reason_code"] == "AWAITING_STEP8_5"


def test_summary_exposes_badge_and_priority(tmp_path):
    write_file(tmp_path / ".heartbeat", "CONSULT:6 1700000000\n")
    diag = build_project_diagnostics(tmp_path, "demo", is_running=False, consultation_pending=True, consultation_gate="dynamic")
    summary = summarize_project_diagnostics(diag)
    assert summary["diagnostic_badge"] == "等待人工"
    assert summary["diagnostic_priority"] == 1
```

- [ ] **Step 2: 跑测试，确认当前失败**

Run: `python3 -m pytest tests/test_web_diagnostics_backend.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'web.backend.diagnostics_service'`

- [ ] **Step 3: 写 backend 诊断服务**

```python
# web/backend/diagnostics_service.py
from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from project_diagnostics import load_recent_events, load_status


PRIORITY = {
    "CONSULTATION_PENDING": 1,
    "AWAITING_STEP8_5": 2,
    "VERIFY_OUTPUT_FAILED": 3,
    "NO_LOG_PROGRESS": 4,
    "LOCK_STALE_RECLAIMED": 5,
}

BADGES = {
    "CONSULTATION_PENDING": "等待人工",
    "AWAITING_STEP8_5": "等待 8.5 门禁",
    "VERIFY_OUTPUT_FAILED": "验证失败待重试",
    "NO_LOG_PROGRESS": "静默过久",
    "LOCK_STALE_RECLAIMED": "锁已回收",
}


def _fallback_status(project: Path, is_running: bool, consultation_pending: bool, consultation_gate: str | None) -> dict:
    heartbeat = (project / ".heartbeat").read_text(encoding="utf-8", errors="replace").strip() if (project / ".heartbeat").is_file() else ""
    if consultation_pending:
        return {
            "state": "waiting",
            "current_step": 0,
            "current_action": "consultation_wait",
            "reason_code": "CONSULTATION_PENDING",
            "reason_summary": "等待人工咨询回填",
            "suggested_actions": ["open_consultation_request", "open_human_review", "refresh_status"],
            "evidence": [{"kind": "file", "path": f"consultation/{consultation_gate or 'dynamic'}_request.md"}],
        }
    if heartbeat.startswith("AWAITING_STEP8_5:"):
        return {
            "state": "waiting",
            "current_step": 8,
            "current_action": "step8_5_gate_review",
            "reason_code": "AWAITING_STEP8_5",
            "reason_summary": "Step 8.5 未通过，等待补足 reviewer entry 材料",
            "suggested_actions": ["open_entry_gate", "open_reviewer_entry_artifacts", "refresh_status"],
            "evidence": [{"kind": "file", "path": "entry_gate.md"}],
        }
    if heartbeat.startswith("STUCK:"):
        return {
            "state": "retrying" if is_running else "waiting",
            "current_step": int(re.findall(r"\d+", heartbeat)[0]),
            "current_action": "verification",
            "reason_code": "VERIFY_OUTPUT_FAILED",
            "reason_summary": "最近一次产物校验未通过",
            "suggested_actions": ["open_runner_log", "refresh_status"],
            "evidence": [{"kind": "file", "path": "logs/runner.log"}],
        }
    return {
        "state": "running" if is_running else "unknown",
        "current_step": 0,
        "current_action": "fallback",
        "reason_code": "",
        "reason_summary": "",
        "suggested_actions": ["refresh_status"],
        "evidence": [],
    }


def build_project_diagnostics(project: Path, base_name: str, *, is_running: bool, consultation_pending: bool, consultation_gate: str | None) -> dict:
    status = load_status(project)
    events = load_recent_events(project, limit=5)
    if status:
        source = "runner"
    else:
        source = "fallback"
        status = _fallback_status(project, is_running, consultation_pending, consultation_gate)
    actions = [{"id": aid} for aid in status.get("suggested_actions", [])]
    return {"source": source, "status": status, "events": events, "actions": actions}


def summarize_project_diagnostics(diag: dict) -> dict:
    code = diag["status"].get("reason_code", "")
    return {
        "diagnostic_reason_code": code or None,
        "diagnostic_badge": BADGES.get(code),
        "diagnostic_priority": PRIORITY.get(code, 999),
    }
```

- [ ] **Step 4: 在 `app.py` 暴露新接口，并把摘要挂到项目列表**

```python
# web/backend/app.py
from diagnostics_service import build_project_diagnostics, summarize_project_diagnostics


class ProjectStatus(BaseModel):
    base_name: str
    status: str
    current_step: int
    total_steps: int = 16
    progress_percent: float
    last_updated: str
    is_running: bool
    pid: Optional[int] = None
    consultation_pending: bool = False
    consultation_gate: Optional[str] = None
    diagnostic_reason_code: Optional[str] = None
    diagnostic_badge: Optional[str] = None
    diagnostic_priority: int = 999


def get_project_status(project_path: Path, base_name: str) -> ProjectStatus:
    ...
    diag = build_project_diagnostics(
        project_path,
        base_name,
        is_running=is_running,
        consultation_pending=consult_pending,
        consultation_gate=consult_gate,
    )
    summary = summarize_project_diagnostics(diag)
    return ProjectStatus(
        ...,
        diagnostic_reason_code=summary["diagnostic_reason_code"],
        diagnostic_badge=summary["diagnostic_badge"],
        diagnostic_priority=summary["diagnostic_priority"],
    )


@app.get("/api/projects/{base_name}/diagnostics")
async def get_project_diagnostics(
    base_name: str,
    current_user: UserInfo = Depends(get_current_user),
):
    project = _resolve_project(base_name)
    status = get_project_status(project, base_name)
    return build_project_diagnostics(
        project,
        base_name,
        is_running=status.is_running,
        consultation_pending=status.consultation_pending,
        consultation_gate=status.consultation_gate,
    )
```

- [ ] **Step 5: 跑 backend 测试和既有 Step 8.5 元数据测试**

Run: `python3 -m pytest tests/test_web_diagnostics_backend.py tests/test_web_step8_5_metadata.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add web/backend/diagnostics_service.py web/backend/app.py tests/test_web_diagnostics_backend.py
git commit -m "feat: add backend project diagnostics service"
```

---

### Task 5: 把证据文件暴露给前端，并补 API helper

**Files:**
- Modify: `web/backend/app.py`
- Modify: `web/frontend/src/lib/api.js`
- Modify: `web/frontend/src/components/ArtifactBrowser.vue`
- Create: `web/frontend/src/lib/diagnostics.js`

- [ ] **Step 1: 先写一个纯前端 helper，用 Node 做失败验证**

```js
// web/frontend/src/lib/diagnostics.js
export const DIAGNOSTIC_ACTION_LABEL = {
  open_runner_log: '查看 runner.log',
  open_entry_gate: '查看 entry_gate.md',
  open_reviewer_entry_artifacts: '查看 8.5 入口材料',
  open_consultation_request: '查看咨询请求',
  open_human_review: '查看 human_review.md',
  open_failed_artifact: '查看失败产物',
  refresh_status: '刷新诊断',
  resume_project: '恢复运行',
}

export function badgeText(project) {
  return project.diagnostic_badge || ''
}

export function actionLabel(actionId) {
  return DIAGNOSTIC_ACTION_LABEL[actionId] || actionId
}
```

Run:

```bash
cd web/frontend
node --input-type=module <<'EOF'
import assert from 'node:assert/strict'
import { actionLabel, badgeText } from './src/lib/diagnostics.js'
assert.equal(actionLabel('open_runner_log'), '查看 runner.log')
assert.equal(badgeText({ diagnostic_badge: '等待人工' }), '等待人工')
EOF
```

Expected: FAIL with `Cannot find module './src/lib/diagnostics.js'`

- [ ] **Step 2: 创建 helper 并重新跑 Node 校验**

```js
// web/frontend/src/lib/diagnostics.js
export const DIAGNOSTIC_ACTION_LABEL = {
  open_runner_log: '查看 runner.log',
  open_entry_gate: '查看 entry_gate.md',
  open_reviewer_entry_artifacts: '查看 8.5 入口材料',
  open_consultation_request: '查看咨询请求',
  open_human_review: '查看 human_review.md',
  open_failed_artifact: '查看失败产物',
  refresh_status: '刷新诊断',
  resume_project: '恢复运行',
}

export function badgeText(project) {
  return project.diagnostic_badge || ''
}

export function actionLabel(actionId) {
  return DIAGNOSTIC_ACTION_LABEL[actionId] || actionId
}
```

Run:

```bash
cd web/frontend
node --input-type=module <<'EOF'
import assert from 'node:assert/strict'
import { actionLabel, badgeText } from './src/lib/diagnostics.js'
assert.equal(actionLabel('open_runner_log'), '查看 runner.log')
assert.equal(badgeText({ diagnostic_badge: '等待人工' }), '等待人工')
EOF
```

Expected: exit 0 with no output

- [ ] **Step 3: 在 backend artifact 列表中加入诊断证据文件**

```python
# web/backend/app.py
_ARTIFACT_GROUPS = {
    ...,
    "diagnostics": [
        "entry_gate.md",
        "reviewer_entry_map.md",
        "anchor_figure_plan.md",
        "human_review.md",
        "logs/runner.log",
        "diagnostics/status.json",
        "diagnostics/events.jsonl",
    ],
}
```

并在 `list_artifacts()` 的 group 顺序里确保 `diagnostics` 会被返回。

- [ ] **Step 4: 在前端 API 和 ArtifactBrowser 中接住 diagnostics**

```js
// web/frontend/src/lib/api.js
export const Projects = {
  ...,
  diagnostics: (b) => api.get(`/api/projects/${b}/diagnostics`).then((r) => r.data),
}
```

```js
// web/frontend/src/components/ArtifactBrowser.vue
const GROUP_META = {
  ...,
  diagnostics: { label: '诊断 · DIAGNOSTICS', icon: 'alert-triangle' },
}
const GROUP_ORDER = ['problem', 'method', 'model', 'solve', 'results', 'figures', 'evaluation', 'paper', 'diagnostics', 'code']
```

- [ ] **Step 5: 做前后端证据入口验证**

Run:

```bash
python3 -m pytest tests/test_web_diagnostics_backend.py tests/test_web_step8_5_metadata.py -v
cd web/frontend && npm run build
```

Expected:
- backend 测试 PASS
- `vite build` 成功

- [ ] **Step 6: Commit**

```bash
git add web/backend/app.py web/frontend/src/lib/api.js web/frontend/src/components/ArtifactBrowser.vue web/frontend/src/lib/diagnostics.js
git commit -m "feat: expose diagnostics evidence files to web ui"
```

---

### Task 6: 工作台诊断卡与列表徽章

**Files:**
- Create: `web/frontend/src/components/DiagnosticsCard.vue`
- Modify: `web/frontend/src/components/ProjectCard.vue`
- Modify: `web/frontend/src/components/ProjectWorkspace.vue`
- Modify: `web/frontend/src/App.vue`
- Modify: `web/frontend/src/lib/api.js`
- Modify: `web/frontend/src/components/ArtifactBrowser.vue`

- [ ] **Step 1: 先写诊断卡组件**

```vue
<!-- web/frontend/src/components/DiagnosticsCard.vue -->
<template>
  <section class="diag panel" :class="severityClass">
    <div class="diag-top">
      <div>
        <div class="diag-kicker mono">DIAGNOSTICS</div>
        <div class="diag-title">{{ title }}</div>
        <div class="diag-summary">{{ summary }}</div>
      </div>
      <div class="diag-actions">
        <button
          v-for="action in actions"
          :key="action.id"
          class="btn btn-sm btn-ghost"
          @click="$emit('action', action.id)"
        >
          {{ actionLabel(action.id) }}
        </button>
      </div>
    </div>

    <div v-if="events.length" class="diag-events">
      <div v-for="event in events" :key="`${event.ts}-${event.type}`" class="diag-event mono">
        {{ event.type }} · {{ event.message }}
      </div>
    </div>
  </section>
</template>

<script>
import { actionLabel } from '../lib/diagnostics.js'

export default {
  name: 'DiagnosticsCard',
  props: {
    diagnostics: { type: Object, required: true },
  },
  emits: ['action'],
  computed: {
    title() {
      return this.diagnostics?.status?.reason_summary || '当前无诊断阻塞'
    },
    summary() {
      return this.diagnostics?.status?.reason_code || 'runner 未报告诊断原因'
    },
    actions() {
      return this.diagnostics?.actions || []
    },
    events() {
      return this.diagnostics?.events || []
    },
    severityClass() {
      const code = this.diagnostics?.status?.reason_code
      return {
        'is-warn': code === 'NO_LOG_PROGRESS' || code === 'LOCK_STALE_RECLAIMED',
        'is-block': code === 'AWAITING_STEP8_5' || code === 'VERIFY_OUTPUT_FAILED' || code === 'CONSULTATION_PENDING',
      }
    },
  },
  methods: { actionLabel },
}
</script>
```

- [ ] **Step 2: 在列表卡片里增加诊断徽章**

```vue
<!-- web/frontend/src/components/ProjectCard.vue -->
<div class="c-top">
  <span class="dot" :class="dotClass"></span>
  <span class="c-name mono">{{ project.base_name }}</span>
  <span v-if="project.diagnostic_badge" class="tag diag-tag">{{ project.diagnostic_badge }}</span>
  <span class="spacer"></span>
  <span class="tag" :class="'st-' + project.status">{{ statusLabel }}</span>
</div>
```

```css
.diag-tag {
  color: var(--amber);
  border-color: var(--amber-line);
  background: var(--amber-dim);
}
```

- [ ] **Step 3: 在工作台中拉取 diagnostics，并把动作路由到既有入口**

```js
// web/frontend/src/components/ProjectWorkspace.vue
import DiagnosticsCard from './DiagnosticsCard.vue'
import { Projects, Models, relativeTime } from '../lib/api.js'

export default {
  components: { ..., DiagnosticsCard },
  data() {
    return {
      ...,
      diagnostics: null,
      diagnosticsLoading: false,
    }
  },
  mounted() {
    this.fetchSteps()
    this.fetchModels()
    this.fetchDiagnostics()
    ...
  },
  methods: {
    async fetchDiagnostics() {
      this.diagnosticsLoading = true
      try { this.diagnostics = await Projects.diagnostics(this.project.base_name) }
      finally { this.diagnosticsLoading = false }
    },
    onDiagnosticsAction(actionId) {
      if (actionId === 'refresh_status') {
        this.fetchDiagnostics()
        this.fetchSteps()
        this.$emit('refresh')
        return
      }
      if (actionId === 'resume_project') {
        this.act('resume')
        return
      }
      const evidenceMap = {
        open_runner_log: { path: 'logs/runner.log', type: 'text', name: 'runner.log' },
        open_entry_gate: { path: 'entry_gate.md', type: 'markdown', name: 'entry_gate.md' },
        open_reviewer_entry_artifacts: { path: 'reviewer_entry_map.md', type: 'markdown', name: 'reviewer_entry_map.md' },
        open_consultation_request: { path: `consultation/${this.project.consultation_gate || 'dynamic'}_request.md`, type: 'markdown', name: 'consultation request' },
        open_human_review: { path: 'human_review.md', type: 'markdown', name: 'human_review.md' },
        open_failed_artifact: { path: 'logs/runner.log', type: 'text', name: 'runner.log' },
      }
      const req = evidenceMap[actionId]
      if (req) this.requestFile(req)
    },
  },
}
```

并在 template 中把诊断卡插到日志区之前：

```vue
<DiagnosticsCard
  v-if="diagnostics && diagnostics.status && diagnostics.status.reason_code"
  class="rise"
  :diagnostics="diagnostics"
  @action="onDiagnosticsAction"
/>
```

- [ ] **Step 4: 构建前端，确认没有把现有 Dashboard 打坏**

Run: `cd web/frontend && npm run build`
Expected: `vite build` 成功，输出 `dist/`

- [ ] **Step 5: 手动 smoke 检查 3 个 UI 路径**

Run:

```bash
cd web
python3 dev.py
```

Expected:
- 列表页项目卡片在有诊断摘要时显示诊断徽章
- 工作台顶部出现诊断卡
- 点击 `查看 entry_gate.md` / `查看 runner.log` 会在右侧产物区打开对应证据

- [ ] **Step 6: Commit**

```bash
git add web/frontend/src/components/DiagnosticsCard.vue web/frontend/src/components/ProjectCard.vue web/frontend/src/components/ProjectWorkspace.vue web/frontend/src/lib/diagnostics.js
git commit -m "feat: add diagnostics card and project badges"
```

---

### Task 7: README、回归验证与收尾

**Files:**
- Modify: `web/README.md`
- Test: `tests/test_project_diagnostics.py`
- Test: `tests/test_runner_diagnostics.py`
- Test: `tests/test_web_diagnostics_backend.py`
- Test: `tests/test_web_step8_5_metadata.py`
- Test: `tests/test_run_paper_step8_5_infer.py`

- [ ] **Step 1: 更新 `web/README.md` 的 API 和功能说明**

```markdown
## 诊断能力

- 列表页会展示项目诊断徽章，例如“等待人工”“等待 8.5 门禁”“静默过久”。
- 工作台会展示诊断卡，包含主结论、最近关键事件和一键动作。
- 新增接口：`GET /api/projects/{base_name}/diagnostics`

返回示例：

```json
{
  "source": "runner",
  "status": {
    "reason_code": "AWAITING_STEP8_5",
    "reason_summary": "Step 8.5 未通过，等待补足 reviewer entry 材料"
  },
  "actions": [
    {"id": "open_entry_gate"},
    {"id": "refresh_status"}
  ]
}
```
```

- [ ] **Step 2: 跑完整的首批回归命令**

Run:

```bash
python3 -m pytest \
  tests/test_project_diagnostics.py \
  tests/test_runner_diagnostics.py \
  tests/test_web_diagnostics_backend.py \
  tests/test_web_step8_5_metadata.py \
  tests/test_run_paper_step8_5_infer.py -v
bash -n run_paper.sh
cd web/frontend && npm run build
```

Expected:
- 所有 pytest 通过
- `bash -n run_paper.sh` 返回 0
- `vite build` 成功

- [ ] **Step 3: 做一条 grep 覆盖检查，确认 spec 的 5 个首批 reason code 全部落地**

Run:

```bash
rg -n "NO_LOG_PROGRESS|AWAITING_STEP8_5|CONSULTATION_PENDING|VERIFY_OUTPUT_FAILED|LOCK_STALE_RECLAIMED" \
  scripts/project_diagnostics.py \
  scripts/runner_diagnostics.sh \
  web/backend/diagnostics_service.py \
  web/frontend/src/lib/diagnostics.js \
  web/frontend/src/components/DiagnosticsCard.vue
```

Expected:
- 5 个 code 均命中
- Bash、backend、frontend 至少各命中一次

- [ ] **Step 4: Commit**

```bash
git add web/README.md
git commit -m "docs: document web diagnostics workflow"
```

---

## Spec Coverage Check

- `diagnostics/status.json` / `diagnostics/events.jsonl`：Task 1、Task 2、Task 3
- runner 声明事实、backend 轻量 fallback：Task 3、Task 4
- `/api/projects/{base_name}/diagnostics`：Task 4
- 列表诊断徽章、工作台诊断卡：Task 6
- 一键动作映射：Task 4、Task 6
- 证据文件跳转：Task 5、Task 6
- README 与验证：Task 7

## Placeholder Scan

- 未使用 `TODO` / `TBD` / “implement later”
- 每个代码任务都给出了具体文件、代码片段、命令和预期结果
- 未引用未定义的动作名、reason code 或 helper 路径
