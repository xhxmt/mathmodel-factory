# Interactive Upstream Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Phase 1 of the interactive upstream selection gate: after Step 2 has produced validated streams, Step 3 can pause for a ranked `PRIMARY/AUXILIARY` choice, expose the choices in Web, default to the top-ranked option after 30 minutes, and resume through the existing `human_review.md` Step 3 override path.

**Architecture:** Reuse the existing Step 1 `modeling_direction_service.py` pattern for a focused filesystem service, but keep Step 3 selection as a separate `selection_service.py` because it operates after Step 2 validation. Add a small runner-facing CLI helper under `scripts/selection_gate.py`, integrate it into `run_paper.sh` before Step 3, and expose Web endpoints plus a compact `SelectionPanel.vue` that follows the existing `ModelingDirectionPanel.vue` visual pattern.

**Tech Stack:** Bash runner orchestration, Python 3 filesystem services and pytest, FastAPI routes, Vue 3 + Vite, existing diagnostics/status JSON, existing `human_review.md` override prompts.

---

## File Map

- Create: `web/backend/selection_service.py`
  - Owns `selection/config.json`, Step 3 option building, decision writing, H2 replacement in `human_review.md`, and request payload reading.
- Create: `scripts/selection_gate.py`
  - Runner-facing CLI wrapper around `selection_service.py` for preparing options and applying timeout defaults.
- Modify: `run_paper.sh`
  - Adds `maybe_select_option step3 3 "方法主线选择"` before Step 3 dispatch.
- Modify: `scripts/runner_diagnostics.sh`
  - Documents `OPTION_SELECTION_PENDING`.
- Modify: `web/backend/state_store.py`
  - Adds fallback detection for pending selection and exposes selection fields in runtime status.
- Modify: `web/backend/schemas.py`
  - Adds `SelectionDecisionRequest` and extends `ProjectStatus` with optional selection fields.
- Modify: `web/backend/project_api.py`
  - Adds `GET /api/projects/{base}/selection` and `POST /api/projects/{base}/selection/decision`.
- Modify: `web/backend/main.py`
  - Adds route aliases for tests, matching the current route export style.
- Modify: `web/frontend/src/lib/contracts.js`
  - Normalizes `selection_pending`, `selection_gate`, and `selection_deadline`.
- Modify: `web/frontend/src/lib/api.js`
  - Adds `Projects.selection()` and `Projects.selectOption()`.
- Modify: `web/frontend/src/lib/status.js`
  - Adds `awaiting_selection` label.
- Modify: `web/frontend/src/lib/workspaceUi.js`
  - Adds a selection tab and helper state.
- Create: `web/frontend/src/components/SelectionPanel.vue`
  - Displays ranked options and submits Step 3 selection.
- Modify: `web/frontend/src/components/ProjectWorkspace.vue`
  - Renders `SelectionPanel` when selection is pending or selected.
- Modify: `web/frontend/src/components/ProjectCard.vue`
  - Shows "等待选方案" pending state and resume eligibility.
- Create: `tests/test_selection_service.py`
  - Unit tests for config parsing, Step 3 option building, decision writing, and human review mirroring.
- Create: `tests/test_selection_gate_cli.py`
  - CLI-level tests for prepare/default behavior without sleeping or launching.
- Modify: `tests/test_web_control_plane_api.py`
  - Adds route tests for selection request and decision submission.
- Modify: `tests/test_web_frontend_runtime_helpers.py`
  - Adds contract/status/workspace helper tests.
- Modify: `docs/superpowers/specs/2026-07-03-interactive-upstream-selection-design.md`
  - Keep it aligned if implementation names differ.

Implementation warning: the current worktree contains unrelated modified and untracked files from parallel work. Every commit command below uses exact paths; do not run `git add .`.

Execution note (2026-07-03): Phase 1 implementation has been applied in the
working tree through Task 5 documentation updates. The commit steps in this
plan were intentionally skipped because the user did not request commits and
the worktree contains unrelated parallel changes.

Follow-up execution note: `scripts/selection_gate.py` also exposes
`select-step3` so the human can choose `PRIMARY/AUXILIARY` directly from the
CLI, matching the CLI-first workflow preference. This command writes
`source=manual-cli`, mirrors the decision to `human_review.md`, and resumes
unless `--no-resume` is supplied.

---

### Task 1: Selection Service

**Files:**
- Create: `web/backend/selection_service.py`
- Test: `tests/test_selection_service.py`

- [ ] **Step 1: Write failing tests for Step 3 option generation and decision mirroring**

Create `tests/test_selection_service.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from web.backend.selection_service import (
    SelectionError,
    build_step3_options,
    read_selection_request,
    selection_enabled,
    write_selection_decision,
)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def seed_step2_streams(project: Path) -> None:
    write(project / "selection" / "config.json", '{"enabled": true, "gates": ["step3"], "timeout_minutes": 30}\n')
    write(project / "viable_streams.md", "## Stream m1:\nMILP stream\n\n## Stream m2:\nSA stream\n")
    write(project / "m1_critique.md", "VERDICT: VALIDATED\nMAJOR warnings: none\n")
    write(project / "m2_critique.md", "VERDICT: VALIDATED\nMAJOR warnings: runtime risk\n")
    write(project / "m3_critique.md", "VERDICT: ABANDONED\ninsufficient data\n")
    write(project / "m1_spec.md", "# m1\nmethod_library/optimization/milp.md\nCovers P1 P2 P3\n")
    write(project / "m2_spec.md", "# m2\nmethod_library/metaheuristic/simulated_annealing.md\nCovers P1 P2\n")
    write(project / "m1_demo_result.json", '{"status": "OPTIMAL", "runtime_seconds": 12}\n')
    write(project / "m2_demo_result.json", '{"status": "FEASIBLE", "runtime_seconds": 55}\n')


def test_selection_enabled_defaults_to_false(tmp_path):
    project = tmp_path / "project"
    assert selection_enabled(project, "step3") is False


def test_build_step3_options_ranks_validated_streams_and_writes_files(tmp_path):
    project = tmp_path / "project"
    seed_step2_streams(project)

    payload = build_step3_options(project, now_epoch=1000)

    assert payload["available"] is True
    assert payload["gate"] == "step3"
    assert payload["default_option_id"] == "m1"
    assert payload["default_aux_id"] == "m2"
    assert payload["deadline_epoch"] == 2800
    assert [item["id"] for item in payload["options"]] == ["m1", "m2"]
    assert payload["options"][0]["scores"]["correctness"] >= payload["options"][1]["scores"]["correctness"]
    assert (project / "selection" / "step3_options.json").is_file()
    assert (project / "selection" / "step3_request.md").is_file()


def test_write_selection_decision_rejects_unknown_option(tmp_path):
    project = tmp_path / "project"
    seed_step2_streams(project)
    build_step3_options(project, now_epoch=1000)

    with pytest.raises(SelectionError):
        write_selection_decision(
            project,
            gate="step3",
            selected_option_id="m9",
            selected_aux_id="",
            source="human",
            reason="bad id",
            now_epoch=1200,
        )


def test_write_selection_decision_records_json_and_step3_human_review(tmp_path):
    project = tmp_path / "project"
    seed_step2_streams(project)
    build_step3_options(project, now_epoch=1000)
    write(project / "human_review.md", "# 人工审核与介入记录\n\n## Other\nkeep\n")

    decision = write_selection_decision(
        project,
        gate="step3",
        selected_option_id="m2",
        selected_aux_id="m1",
        source="human",
        reason="Prefer heuristic contrast.",
        now_epoch=1200,
    )

    saved = read_json(project / "selection" / "step3_decision.json")
    review = (project / "human_review.md").read_text(encoding="utf-8")
    assert decision["selected_option_id"] == "m2"
    assert saved["selected_aux_id"] == "m1"
    assert "## Step 3 decision:" in review
    assert "PRIMARY: m2" in review
    assert "AUXILIARY: m1" in review
    assert "SOURCE: human" in review
    assert "## Other\nkeep" in review


def test_read_selection_request_reports_existing_decision(tmp_path):
    project = tmp_path / "project"
    seed_step2_streams(project)
    build_step3_options(project, now_epoch=1000)
    write_selection_decision(
        project,
        gate="step3",
        selected_option_id="m1",
        selected_aux_id="m2",
        source="auto-timeout",
        reason="deadline",
        now_epoch=2800,
    )

    payload = read_selection_request(project, gate="step3")

    assert payload["available"] is True
    assert payload["decision"]["source"] == "auto-timeout"
    assert payload["selected_option_id"] == "m1"
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
python3 -m pytest tests/test_selection_service.py -q
```

Expected: FAIL because `web.backend.selection_service` does not exist.

- [ ] **Step 3: Implement `web/backend/selection_service.py`**

Create `web/backend/selection_service.py` with these concrete interfaces:

```python
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any


STEP3_HEADING = "## Step 3 decision:"
VALID_VERDICT_RE = re.compile(r"^VERDICT:\s*(\S+)", re.M)
STREAM_RE = re.compile(r"m(\d+)")


class SelectionError(ValueError):
    pass


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default
    except Exception:
        return default


def selection_config(project_path: Path) -> dict[str, Any]:
    data = _load_json(project_path / "selection" / "config.json", {})
    return data if isinstance(data, dict) else {}


def selection_enabled(project_path: Path, gate: str) -> bool:
    cfg = selection_config(project_path)
    if cfg.get("enabled") is not True:
        return False
    gates = cfg.get("gates", [])
    return gate in gates if isinstance(gates, list) else False


def timeout_minutes(project_path: Path) -> int:
    raw = selection_config(project_path).get("timeout_minutes", 30)
    try:
        value = int(raw)
    except Exception:
        value = 30
    return max(1, min(value, 24 * 60))


def _first_verdict(path: Path) -> str:
    match = VALID_VERDICT_RE.search(_read_text(path).replace("\r\n", "\n"))
    return match.group(1).strip() if match else ""


def _demo_status(path: Path) -> str:
    data = _load_json(path, {})
    if isinstance(data, dict):
        return str(data.get("status") or data.get("solver_status") or "").upper()
    return ""


def _method_family(spec_text: str) -> str:
    match = re.search(r"method_library/([^/\s]+)/([^/\s]+)\.md", spec_text)
    if match:
        return match.group(2).replace("_", " ").upper()
    if "MILP" in spec_text.upper():
        return "MILP"
    if "PSO" in spec_text.upper():
        return "PSO"
    return "UNKNOWN"


def _stream_ids(project_path: Path) -> list[str]:
    ids = {
        match.group(0)
        for path in project_path.glob("m*_critique.md")
        for match in STREAM_RE.finditer(path.stem)
    }
    return sorted(ids, key=lambda item: int(item[1:]))


def _score_stream(project_path: Path, stream_id: str) -> dict[str, int]:
    spec_text = _read_text(project_path / f"{stream_id}_spec.md")
    critique_text = _read_text(project_path / f"{stream_id}_critique.md")
    demo_status = _demo_status(project_path / f"{stream_id}_demo_result.json")
    correctness = 90 if demo_status == "OPTIMAL" else 78 if demo_status in {"FEASIBLE", "SUCCESS"} else 65
    feasibility = 88 if demo_status in {"OPTIMAL", "FEASIBLE", "SUCCESS"} else 55
    coverage = min(100, 60 + 10 * len(set(re.findall(r"\bP[1-9]\b|问题[一二三四五六七八九十]", spec_text))))
    innovation = 70 + (8 if "robust" in spec_text.lower() or "鲁棒" in spec_text else 0)
    risk = 20 + 15 * len(re.findall(r"\bMAJOR\b|风险|warning", critique_text, re.I))
    return {
        "correctness": min(correctness, 100),
        "feasibility": min(feasibility, 100),
        "coverage": min(coverage, 100),
        "innovation": min(innovation, 100),
        "risk": min(risk, 100),
        "differentiation": 50,
    }


def _build_option(project_path: Path, stream_id: str) -> dict[str, Any] | None:
    if _first_verdict(project_path / f"{stream_id}_critique.md") != "VALIDATED":
        return None
    spec_text = _read_text(project_path / f"{stream_id}_spec.md")
    family = _method_family(spec_text)
    scores = _score_stream(project_path, stream_id)
    composite = (
        scores["correctness"] * 10_000
        + scores["feasibility"] * 100
        + scores["coverage"]
        - scores["risk"]
    )
    return {
        "id": stream_id,
        "rank": 0,
        "title": f"{stream_id} - {family}",
        "family": family,
        "validated": True,
        "scores": scores,
        "composite_score": composite,
        "summary": f"{stream_id} uses {family} as a validated modeling stream.",
        "why_high_ranked": [
            f"Demo status: {_demo_status(project_path / f'{stream_id}_demo_result.json') or 'unknown'}.",
            f"Critique verdict: VALIDATED.",
        ],
        "main_tradeoffs": _tradeoffs(project_path, stream_id),
        "subproblem_mapping": {},
        "evidence_files": [
            f"{stream_id}_spec.md",
            f"{stream_id}_critique.md",
            f"{stream_id}_demo_result.json",
        ],
        "aux_compatibility": [],
        "recommended_aux": "",
        "selection_payload": {"primary": stream_id, "auxiliary": "NONE"},
    }


def _tradeoffs(project_path: Path, stream_id: str) -> list[str]:
    text = _read_text(project_path / f"{stream_id}_critique.md")
    rows = [line.strip("- ").strip() for line in text.splitlines() if re.search(r"风险|warning|MAJOR", line, re.I)]
    return rows[:3] or ["No blocking critique issue recorded."]


def _rank_options(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    options.sort(
        key=lambda item: (
            -item["scores"]["correctness"],
            -item["scores"]["feasibility"],
            -item["scores"]["coverage"],
            -item["scores"]["innovation"],
            item["scores"]["risk"],
            item["id"],
        )
    )
    for rank, item in enumerate(options, 1):
        item["rank"] = rank
    for item in options:
        item["aux_compatibility"] = [
            other["id"] for other in options if other["id"] != item["id"] and other["family"] != item["family"]
        ]
        item["recommended_aux"] = item["aux_compatibility"][0] if item["aux_compatibility"] else "NONE"
        item["selection_payload"]["auxiliary"] = item["recommended_aux"]
    return options


def build_step3_options(project_path: Path, *, now_epoch: int | None = None) -> dict[str, Any]:
    now = int(time.time()) if now_epoch is None else int(now_epoch)
    options = [
        option for stream_id in _stream_ids(project_path)
        if (option := _build_option(project_path, stream_id)) is not None
    ]
    options = _rank_options(options)
    payload = {
        "schema_version": "1.0",
        "gate": "step3",
        "available": bool(options),
        "message": "" if options else "No VALIDATED Step 2 streams are available.",
        "created_epoch": now,
        "deadline_epoch": now + timeout_minutes(project_path) * 60,
        "default_option_id": options[0]["id"] if options else "",
        "default_aux_id": options[0]["recommended_aux"] if options else "NONE",
        "ranking_policy": "correctness_feasibility_first",
        "options": options,
    }
    _write_json_atomic(project_path / "selection" / "step3_options.json", payload)
    _write_text_atomic(project_path / "selection" / "step3_request.md", render_step3_request(payload))
    return payload


def render_step3_request(payload: dict[str, Any]) -> str:
    lines = ["# Step 3 方法主线选择", "", f"Default: `{payload.get('default_option_id', '')}`", ""]
    for item in payload.get("options", []):
        lines.extend([
            f"## #{item['rank']} {item['title']}",
            f"- 正确性: {item['scores']['correctness']}",
            f"- 可行性: {item['scores']['feasibility']}",
            f"- 推荐 AUX: {item.get('recommended_aux') or 'NONE'}",
            f"- 摘要: {item.get('summary', '')}",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def read_selection_request(project_path: Path, gate: str = "step3") -> dict[str, Any]:
    options = _load_json(project_path / "selection" / f"{gate}_options.json", {})
    decision = _load_json(project_path / "selection" / f"{gate}_decision.json", None)
    if not isinstance(options, dict):
        options = {}
    options["decision"] = decision if isinstance(decision, dict) else None
    options["selected_option_id"] = options["decision"].get("selected_option_id", "") if options["decision"] else ""
    return options


def write_selection_decision(
    project_path: Path,
    *,
    gate: str,
    selected_option_id: str,
    selected_aux_id: str = "",
    source: str,
    reason: str,
    now_epoch: int | None = None,
) -> dict[str, Any]:
    if gate != "step3":
        raise SelectionError(f"Unsupported selection gate: {gate}")
    payload = read_selection_request(project_path, gate)
    options = payload.get("options") or []
    option_ids = {str(item.get("id")) for item in options}
    if selected_option_id not in option_ids:
        raise SelectionError(f"Unknown option id: {selected_option_id}")
    if selected_aux_id and selected_aux_id != "NONE" and selected_aux_id not in option_ids:
        raise SelectionError(f"Unknown auxiliary id: {selected_aux_id}")
    selected = next(item for item in options if item.get("id") == selected_option_id)
    aux = selected_aux_id or selected.get("recommended_aux") or "NONE"
    now = int(time.time()) if now_epoch is None else int(now_epoch)
    decision = {
        "schema_version": "1.0",
        "gate": gate,
        "selected_option_id": selected_option_id,
        "selected_aux_id": aux,
        "source": source,
        "decided_epoch": now,
        "reason": reason,
        "mirrored_to_human_review": True,
    }
    _write_json_atomic(project_path / "selection" / "step3_decision.json", decision)
    mirror_step3_decision_to_human_review(project_path, selected, aux, decision)
    return decision


def mirror_step3_decision_to_human_review(
    project_path: Path,
    selected_option: dict[str, Any],
    selected_aux_id: str,
    decision: dict[str, Any],
) -> None:
    human_review = project_path / "human_review.md"
    existing = _read_text(human_review)
    section = (
        f"{STEP3_HEADING}\n\n"
        "STATUS: READY\n"
        f"SOURCE: {decision.get('source', '')}\n"
        f"PRIMARY: {selected_option.get('id', '')}\n"
        f"AUXILIARY: {selected_aux_id or 'NONE'}\n"
        f"Reason: {decision.get('reason', '')}\n"
        f"Selected title: {selected_option.get('title', '')}\n"
        f"Family: {selected_option.get('family', '')}\n"
    )
    pattern = re.compile(rf"^{re.escape(STEP3_HEADING)}\n.*?(?=^## |\Z)", re.M | re.S)
    if pattern.search(existing):
        updated = pattern.sub(section, existing).rstrip() + "\n"
    else:
        prefix = existing.rstrip()
        updated = f"{prefix}\n\n{section}" if prefix else f"# 人工审核与介入记录\n\n{section}"
    _write_text_atomic(human_review, updated)
```

- [ ] **Step 4: Run tests and verify they pass**

Run:

```bash
python3 -m pytest tests/test_selection_service.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit service**

```bash
git add web/backend/selection_service.py tests/test_selection_service.py
git commit -m "feat: add step3 selection service"
```

---

### Task 2: Runner CLI And Step 3 Gate

**Files:**
- Create: `scripts/selection_gate.py`
- Modify: `run_paper.sh`
- Modify: `scripts/runner_diagnostics.sh`
- Test: `tests/test_selection_gate_cli.py`
- Test: `tests/test_quality_gates_regression.py`

- [ ] **Step 1: Write failing CLI and runner tests**

Create `tests/test_selection_gate_cli.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

from tests.test_selection_service import seed_step2_streams
from scripts import selection_gate


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_prepare_step3_writes_options_and_returns_pending(tmp_path):
    project = tmp_path / "project"
    seed_step2_streams(project)

    code = selection_gate.main(["prepare-step3", str(project), "--now-epoch", "1000"])

    assert code == 10
    assert (project / "selection" / "step3_options.json").is_file()
    assert read_json(project / "selection" / "step3_options.json")["default_option_id"] == "m1"


def test_prepare_step3_returns_ready_when_decision_exists(tmp_path):
    project = tmp_path / "project"
    seed_step2_streams(project)
    selection_gate.main(["prepare-step3", str(project), "--now-epoch", "1000"])
    code = selection_gate.main(["default-step3", str(project), "--now-epoch", "2800", "--no-resume"])

    assert code == 0
    assert selection_gate.main(["prepare-step3", str(project), "--now-epoch", "2801"]) == 0
    assert read_json(project / "selection" / "step3_decision.json")["source"] == "auto-timeout"


def test_prepare_step3_returns_ready_when_selection_disabled(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    assert selection_gate.main(["prepare-step3", str(project)]) == 0
```

Add to `tests/test_quality_gates_regression.py`:

```python
def test_runner_invokes_step3_selection_before_step3_dispatch():
    runner = Path(REPO_ROOT) / "run_paper.sh"
    text = runner.read_text(encoding="utf-8")

    assert "maybe_select_option step3 3" in text
    assert text.index("maybe_select_option step3 3") < text.index("3)  run_step_3")
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
python3 -m pytest tests/test_selection_gate_cli.py tests/test_quality_gates_regression.py -q
```

Expected: FAIL because `scripts/selection_gate.py` and runner hook are missing.

- [ ] **Step 3: Implement `scripts/selection_gate.py`**

Create `scripts/selection_gate.py`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web.backend import selection_service


PENDING_EXIT = 10


def _resume(project: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [str(root / "launch_agents.sh"), "resume", project.name],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
    )


def prepare_step3(project: Path, now_epoch: int | None) -> int:
    if not selection_service.selection_enabled(project, "step3"):
        return 0
    if (project / "selection" / "step3_decision.json").is_file():
        return 0
    selection_service.build_step3_options(project, now_epoch=now_epoch)
    return PENDING_EXIT


def default_step3(project: Path, now_epoch: int | None, no_resume: bool) -> int:
    if (project / "selection" / "step3_decision.json").is_file():
        return 0
    payload = selection_service.read_selection_request(project, "step3")
    if not payload.get("options"):
        payload = selection_service.build_step3_options(project, now_epoch=now_epoch)
    option_id = str(payload.get("default_option_id") or "")
    aux_id = str(payload.get("default_aux_id") or "NONE")
    if not option_id:
        return 2
    selection_service.write_selection_decision(
        project,
        gate="step3",
        selected_option_id=option_id,
        selected_aux_id=aux_id,
        source="auto-timeout",
        reason="No human selection before the deadline; selected top-ranked option.",
        now_epoch=now_epoch,
    )
    if not no_resume:
        _resume(project)
    return 0


def wait_default_step3(project: Path, no_resume: bool) -> int:
    payload = selection_service.read_selection_request(project, "step3")
    deadline = int(payload.get("deadline_epoch") or 0)
    delay = max(0, deadline - int(time.time()))
    if delay:
        time.sleep(delay)
    return default_step3(project, now_epoch=None, no_resume=no_resume)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare-step3", "default-step3", "wait-default-step3"):
        p = sub.add_parser(name)
        p.add_argument("project")
        p.add_argument("--now-epoch", type=int, default=None)
        p.add_argument("--no-resume", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project = Path(args.project).resolve()
    if args.command == "prepare-step3":
        return prepare_step3(project, args.now_epoch)
    if args.command == "default-step3":
        return default_step3(project, args.now_epoch, args.no_resume)
    if args.command == "wait-default-step3":
        return wait_default_step3(project, args.no_resume)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Add runner hook**

Modify `run_paper.sh` near the consultation helpers or step dispatch helpers:

```bash
maybe_select_option() {
    local gate="$1" step="$2" title="$3"
    local script="$FACTORY/scripts/selection_gate.py"
    [[ -x "$script" || -f "$script" ]] || return 0
    set +e
    python3 "$script" "prepare-${gate}" "$PROJECT" >> "$PROJECT/logs/runner.log" 2>&1
    local ec=$?
    set -e
    if (( ec == 10 )); then
        log "   SELECTION[$gate]: awaiting human choice — $title"
        diag_event "$PROJECT" "$step" selection_requested OPTION_SELECTION_PENDING \
            "Runner paused for option selection" "selection/${gate}_request.md"
        diag_status "$PROJECT" awaiting_selection "$step" selection_wait OPTION_SELECTION_PENDING \
            "等待人工选择${title}" \
            "open_selection_request,open_selection_evidence,refresh_status" \
            "file:selection/${gate}_request.md,file:selection/${gate}_options.json"
        ( python3 "$script" "wait-default-${gate}" "$PROJECT" >> "$PROJECT/logs/selection_timeout_${gate}.log" 2>&1 & )
        log "   Runner exiting cleanly for selection. Resume after choosing or timeout."
        exit 0
    fi
    (( ec == 0 )) && return 0
    log "   SELECTION[$gate]: prepare failed (exit $ec)"
    return "$ec"
}
```

Add before the inner attempt loop dispatches Step 3:

```bash
if (( NEXT == 3 )); then
    maybe_select_option step3 3 "方法主线选择"
fi
```

Add `OPTION_SELECTION_PENDING` to the comment in `scripts/runner_diagnostics.sh`.

- [ ] **Step 5: Run tests**

Run:

```bash
python3 -m pytest tests/test_selection_gate_cli.py tests/test_quality_gates_regression.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit runner gate**

```bash
git add scripts/selection_gate.py run_paper.sh scripts/runner_diagnostics.sh tests/test_selection_gate_cli.py tests/test_quality_gates_regression.py
git commit -m "feat: pause step3 for interactive method selection"
```

---

### Task 3: Backend API And Project Status

**Files:**
- Modify: `web/backend/state_store.py`
- Modify: `web/backend/schemas.py`
- Modify: `web/backend/project_api.py`
- Modify: `web/backend/main.py`
- Test: `tests/test_web_control_plane_api.py`

- [ ] **Step 1: Add failing backend API tests**

Append to `tests/test_web_control_plane_api.py`:

```python
def test_selection_endpoint_returns_pending_options(client, demo_project):
    project = demo_project
    (project / "selection").mkdir(exist_ok=True)
    (project / "selection" / "step3_options.json").write_text(
        json.dumps(
            {
                "gate": "step3",
                "available": True,
                "default_option_id": "m1",
                "options": [{"id": "m1", "rank": 1, "recommended_aux": "NONE"}],
            }
        ),
        encoding="utf-8",
    )

    response = client.get(f"/api/projects/{project.name}/selection")

    assert response.status_code == 200
    assert response.json()["default_option_id"] == "m1"


def test_selection_decision_writes_human_review(client, demo_project):
    project = demo_project
    (project / "selection").mkdir(exist_ok=True)
    (project / "selection" / "step3_options.json").write_text(
        json.dumps(
            {
                "gate": "step3",
                "available": True,
                "default_option_id": "m1",
                "options": [
                    {
                        "id": "m1",
                        "rank": 1,
                        "title": "m1 - MILP",
                        "family": "MILP",
                        "recommended_aux": "NONE",
                        "scores": {},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    response = client.post(
        f"/api/projects/{project.name}/selection/decision",
        json={"gate": "step3", "selected_option_id": "m1", "selected_aux_id": "NONE", "reason": "ok"},
    )

    assert response.status_code == 200
    assert "## Step 3 decision:" in (project / "human_review.md").read_text(encoding="utf-8")
```

Use the existing test fixtures in that file. If the file does not expose `client`
and `demo_project` with these names, adapt only the fixture names, not the tested
behavior.

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
python3 -m pytest tests/test_web_control_plane_api.py -q
```

Expected: FAIL on missing selection endpoints or schemas.

- [ ] **Step 3: Extend schemas**

Modify `web/backend/schemas.py`:

```python
class SelectionDecisionRequest(BaseModel):
    gate: str = "step3"
    selected_option_id: str
    selected_aux_id: str = ""
    reason: str = ""
```

Extend `ProjectStatus`:

```python
    selection_pending: bool = False
    selection_gate: str | None = None
    selection_deadline: int | None = None
```

- [ ] **Step 4: Extend `state_store.py`**

Add a private reader:

```python
def _read_selection(project_path: Path) -> tuple[bool, str | None, int | None]:
    selection_dir = project_path / "selection"
    if not selection_dir.is_dir():
        return False, None, None
    for gate in ("step3", "step4"):
        options = selection_dir / f"{gate}_options.json"
        decision = selection_dir / f"{gate}_decision.json"
        if options.is_file() and not decision.is_file():
            try:
                data = json.loads(options.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            return True, gate, data.get("deadline_epoch")
    return False, None, None
```

Import `json`. In `_from_snapshot`, set:

```python
selection_pending = snapshot.get("state") == "awaiting_selection"
```

and include `selection_pending`, `selection_gate`, `selection_deadline`.

In `_fallback_status`, call `_read_selection()` before consultation and return
status `awaiting_selection` with reason code `OPTION_SELECTION_PENDING` when a
pending options file exists.

- [ ] **Step 5: Add API routes**

In `web/backend/project_api.py`, import `SelectionDecisionRequest` and
`write_selection_decision`, `read_selection_request`.

Add routes near the modeling direction routes:

```python
    @router.get("/api/projects/{base_name}/selection")
    async def get_selection(base_name: str, current_user: UserInfo = Depends(get_current_user(settings))):
        require_project_access(settings, current_user, base_name)
        project = _resolve_project(settings, base_name)
        payload = read_selection_request(project, "step3")
        if not payload:
            raise HTTPException(status_code=404, detail="No selection request")
        return payload

    @router.post("/api/projects/{base_name}/selection/decision")
    async def submit_selection_decision(
        base_name: str,
        decision: SelectionDecisionRequest,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        require_project_access(settings, current_user, base_name)
        project = _resolve_project(settings, base_name)
        try:
            saved = write_selection_decision(
                project,
                gate=decision.gate,
                selected_option_id=decision.selected_option_id.strip(),
                selected_aux_id=decision.selected_aux_id.strip(),
                source="human",
                reason=decision.reason.strip() or f"Selected by {current_user.username}",
            )
        except SelectionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await manager.broadcast({"type": "project_action", "project": base_name, "action": "select_option"})
        run_action(settings.factory_root, "resume", base_name)
        return {"status": "ok", "decision": saved}
```

Add route aliases in `web/backend/main.py`:

```python
get_selection = _router_endpoint(project_router, "/api/projects/{base_name}/selection")
submit_selection_decision = _router_endpoint(project_router, "/api/projects/{base_name}/selection/decision")
```

- [ ] **Step 6: Run backend tests**

Run:

```bash
python3 -m pytest tests/test_web_control_plane_api.py tests/test_selection_service.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit backend API**

```bash
git add web/backend/state_store.py web/backend/schemas.py web/backend/project_api.py web/backend/main.py tests/test_web_control_plane_api.py
git commit -m "feat: expose step3 selection through web api"
```

---

### Task 4: Frontend Selection Panel

**Files:**
- Modify: `web/frontend/src/lib/contracts.js`
- Modify: `web/frontend/src/lib/api.js`
- Modify: `web/frontend/src/lib/status.js`
- Modify: `web/frontend/src/lib/workspaceUi.js`
- Create: `web/frontend/src/components/SelectionPanel.vue`
- Modify: `web/frontend/src/components/ProjectWorkspace.vue`
- Modify: `web/frontend/src/components/ProjectCard.vue`
- Test: `tests/test_web_frontend_runtime_helpers.py`

- [ ] **Step 1: Add failing frontend helper tests**

Append to `tests/test_web_frontend_runtime_helpers.py`:

```python
def test_frontend_contracts_include_selection_fields():
    text = Path("web/frontend/src/lib/contracts.js").read_text(encoding="utf-8")

    assert "selection_pending" in text
    assert "selection_gate" in text
    assert "selection_deadline" in text


def test_frontend_api_exposes_selection_helpers():
    text = Path("web/frontend/src/lib/api.js").read_text(encoding="utf-8")

    assert "selection:" in text
    assert "selectOption:" in text
    assert "/selection/decision" in text


def test_workspace_has_selection_tab_and_panel():
    workspace = Path("web/frontend/src/components/ProjectWorkspace.vue").read_text(encoding="utf-8")
    tabs = Path("web/frontend/src/lib/workspaceUi.js").read_text(encoding="utf-8")

    assert "SelectionPanel" in workspace
    assert "selection_pending" in workspace
    assert "key: 'selection'" in tabs
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
python3 -m pytest tests/test_web_frontend_runtime_helpers.py -q
```

Expected: FAIL on missing selection fields/helpers/panel.

- [ ] **Step 3: Normalize selection fields and API**

Modify `normalizeProjectStatus()` in `contracts.js`:

```js
    selection_pending: Boolean(raw.selection_pending),
    selection_gate: stringOrNull(raw.selection_gate),
    selection_deadline: raw.selection_deadline === undefined || raw.selection_deadline === null || raw.selection_deadline === '' ? null : numberOr(raw.selection_deadline, null),
```

Add to `Projects` in `api.js`:

```js
  selection: (b) => api.get(`/api/projects/${b}/selection`).then((r) => r.data),
  selectOption: (b, payload) => api.post(`/api/projects/${b}/selection/decision`, payload).then((r) => r.data),
```

Add to `status.js`:

```js
    awaiting_selection: '等待选方案',
```

- [ ] **Step 4: Add workspace tab**

Change `workspaceTabs()` signature in `workspaceUi.js`:

```js
export function workspaceTabs({ consultationPending = false, selectionPending = false, diagnostics = null, cloudEnabled = false } = {}) {
```

Add tab:

```js
    { key: 'selection', label: '选方案', icon: 'git-branch', attention: selectionPending },
```

- [ ] **Step 5: Create `SelectionPanel.vue`**

Create a compact panel modeled after `ModelingDirectionPanel.vue`:

```vue
<template>
  <section class="sel-panel panel">
    <div class="sel-head">
      <div class="sel-title">
        <Icon name="git-branch" :size="15" />
        <span>{{ title }}</span>
        <span v-if="options.length" class="count mono">{{ options.length }}</span>
      </div>
      <button class="btn btn-icon btn-ghost btn-sm" @click="load" :disabled="loading" title="刷新">
        <Icon name="refresh" :size="13" :class="{ spin: loading }" />
      </button>
    </div>

    <div v-if="loading" class="empty">加载中…</div>
    <div v-else-if="!available" class="empty">{{ message || '暂无待选择方案' }}</div>
    <div v-else class="grid">
      <article v-for="option in options" :key="option.id" class="card" :class="{ selected: selectedOptionId === option.id }">
        <div class="top">
          <span class="rank mono">#{{ option.rank }}</span>
          <span class="family mono">{{ option.family || 'method' }}</span>
        </div>
        <h3>{{ option.title }}</h3>
        <p>{{ option.summary }}</p>
        <div class="score">
          <span>正确性</span><meter min="0" max="100" :value="score(option, 'correctness')"></meter><b class="mono">{{ score(option, 'correctness') }}</b>
        </div>
        <div class="score">
          <span>可行性</span><meter min="0" max="100" :value="score(option, 'feasibility')"></meter><b class="mono">{{ score(option, 'feasibility') }}</b>
        </div>
        <div class="meta mono">AUX: {{ option.recommended_aux || 'NONE' }}</div>
        <button class="btn btn-sm" :class="selectedOptionId === option.id ? 'btn-ghost' : 'btn-amber'" :disabled="submitting" @click="select(option)">
          <Icon name="check-circle" :size="13" /> {{ selectedOptionId === option.id ? '已选' : '选择' }}
        </button>
      </article>
    </div>
    <div v-if="error" class="error">{{ error }}</div>
  </section>
</template>

<script>
import { computed, onMounted, ref, watch } from 'vue'
import Icon from './Icon.vue'
import { Projects } from '../lib/api.js'
import { useToasts } from '../composables/useToasts.js'

export default {
  name: 'SelectionPanel',
  components: { Icon },
  props: { base: { type: String, required: true } },
  emits: ['changed'],
  setup(props, { emit }) {
    const toasts = useToasts()
    const loading = ref(false)
    const submitting = ref(false)
    const payload = ref({})
    const selectedOptionId = ref('')
    const error = ref('')
    const available = computed(() => Boolean(payload.value?.available))
    const options = computed(() => Array.isArray(payload.value?.options) ? payload.value.options : [])
    const message = computed(() => payload.value?.message || '')
    const title = computed(() => payload.value?.gate === 'step4' ? '建模口径选择' : '方法主线选择')

    function score(option, key) { return Number(option?.scores?.[key] || 0) }
    async function load() {
      if (!props.base) return
      loading.value = true
      error.value = ''
      try {
        payload.value = await Projects.selection(props.base)
        selectedOptionId.value = payload.value?.selected_option_id || payload.value?.default_option_id || ''
      } catch (err) {
        error.value = err.response?.data?.detail || '方案选择加载失败'
      } finally {
        loading.value = false
      }
    }
    async function select(option) {
      if (!option?.id || submitting.value) return
      submitting.value = true
      error.value = ''
      try {
        await Projects.selectOption(props.base, {
          gate: payload.value?.gate || 'step3',
          selected_option_id: option.id,
          selected_aux_id: option.recommended_aux || 'NONE',
          reason: `Selected ${option.id} from Web selection panel`,
        })
        selectedOptionId.value = option.id
        toasts.success(`${option.title} 已写入 Step 3 决策`, '方案选择')
        emit('changed')
      } catch (err) {
        error.value = err.response?.data?.detail || '方案选择保存失败'
      } finally {
        submitting.value = false
      }
    }
    watch(() => props.base, load)
    onMounted(load)
    return { loading, submitting, available, options, selectedOptionId, message, title, error, score, load, select }
  },
}
</script>

<style scoped>
.sel-panel { display: flex; flex-direction: column; gap: 12px; padding: 14px; }
.sel-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.sel-title { display: flex; align-items: center; gap: 8px; font-weight: 800; font-size: 13px; }
.count { padding: 2px 6px; border: 1px solid var(--line); border-radius: var(--r-sm); color: var(--ink-3); font-size: 10px; }
.grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.card { min-height: 235px; display: flex; flex-direction: column; gap: 9px; padding: 12px; border: 1px solid var(--line); border-radius: var(--r); background: var(--panel-2); }
.card.selected { border-color: var(--ok); background: var(--ok-dim); }
.top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.rank { color: var(--amber); font-weight: 900; font-size: 11px; }
.family, .meta { color: var(--ink-3); font-size: 10px; overflow-wrap: anywhere; }
h3 { margin: 0; font-size: 14px; line-height: 1.28; letter-spacing: 0; }
p { flex: 1; margin: 0; color: var(--ink-2); font-size: 11.5px; line-height: 1.45; }
.score { display: grid; grid-template-columns: 42px minmax(0, 1fr) 28px; align-items: center; gap: 8px; font-size: 11px; color: var(--ink-2); }
meter { width: 100%; height: 7px; }
.empty { min-height: 46px; padding: 10px 12px; border: 1px dashed var(--line-2); border-radius: var(--r); color: var(--ink-3); font-size: 12.5px; }
.error { padding: 9px 10px; border: 1px solid var(--bad); border-radius: var(--r-sm); background: var(--bad-dim); color: var(--bad); font-size: 12px; }
.spin { animation: spin 0.7s linear infinite; }
@media (max-width: 980px) { .grid { grid-template-columns: 1fr; } .card { min-height: auto; } }
</style>
```

- [ ] **Step 6: Integrate panel in `ProjectWorkspace.vue`**

Import `SelectionPanel`, add to components, and render:

```vue
<SelectionPanel
  v-if="project.selection_pending"
  class="rise"
  :base="project.base_name"
  @changed="onSelectionChanged"
/>
```

Add tab route:

```vue
<SelectionPanel
  v-else-if="activeTab === 'selection'"
  class="rise"
  :base="project.base_name"
  @changed="onSelectionChanged"
/>
```

Pass `selectionPending` into `workspaceTabs()`:

```js
selectionPending: props.project.selection_pending,
```

Add:

```js
function onSelectionChanged() {
  refresh()
  activeTab.value = 'overview'
}
```

- [ ] **Step 7: Update project card**

In `ProjectCard.vue`, treat pending class and message as:

```vue
<article class="card panel" :class="['ac-' + project.status, { pending: project.consultation_pending || project.selection_pending }]" ...>
```

Add a selection message:

```vue
<div v-if="project.selection_pending" class="c-consult">
  <Icon name="git-branch" :size="13" />
  <span>等待选方案 · {{ project.selection_gate || 'step3' }}</span>
</div>
```

Add resume status:

```js
canResume() { return ['paused', 'ready', 'awaiting_consultation', 'awaiting_selection'].includes(this.project.status) },
```

- [ ] **Step 8: Run frontend helper tests**

Run:

```bash
python3 -m pytest tests/test_web_frontend_runtime_helpers.py -q
```

Expected: PASS.

- [ ] **Step 9: Build frontend**

Run:

```bash
cd web/frontend && npm run build
```

Expected: build completes without Vue compile errors.

- [ ] **Step 10: Commit frontend**

```bash
git add web/frontend/src/lib/contracts.js web/frontend/src/lib/api.js web/frontend/src/lib/status.js web/frontend/src/lib/workspaceUi.js web/frontend/src/components/SelectionPanel.vue web/frontend/src/components/ProjectWorkspace.vue web/frontend/src/components/ProjectCard.vue tests/test_web_frontend_runtime_helpers.py
git commit -m "feat: add web selection panel for step3"
```

---

### Task 5: Documentation And Final Verification

**Files:**
- Modify: `README.md`
- Modify: `web/README.md`
- Modify: `docs/superpowers/specs/2026-07-03-interactive-upstream-selection-design.md`

- [ ] **Step 1: Update user-facing docs**

Add to `README.md` under Web Dashboard or workflow notes:

```markdown
- 交互式项目可启用上游方案选择门：Step 1 的“建模方向”用于早期方法偏好，Step 3 的“方法主线选择”用于在已验证候选流中选择 PRIMARY/AUXILIARY。无人值守项目默认不启用该选择门。
```

Add to `web/README.md` under 人工介入:

```markdown
### 上游方案选择

启用 `selection/config.json` 的项目会在 Step 3 前暂停，展示已验证候选流的正确性、可行性、覆盖率和风险。用户可选择 PRIMARY/AUXILIARY；若 30 分钟未选择，系统自动采用排名第 1 的候选并恢复运行。
```

- [ ] **Step 2: Run full focused verification**

Run:

```bash
python3 -m pytest tests/test_selection_service.py tests/test_selection_gate_cli.py tests/test_web_control_plane_api.py tests/test_web_frontend_runtime_helpers.py tests/test_quality_gates_regression.py -q
```

Expected: PASS.

Run:

```bash
cd web/frontend && npm run build
```

Expected: PASS.

- [ ] **Step 3: Check worktree**

Run:

```bash
git status --short
```

Expected: only intended selection-gate files plus pre-existing unrelated worktree changes remain.

- [ ] **Step 4: Commit docs**

```bash
git add README.md web/README.md docs/superpowers/specs/2026-07-03-interactive-upstream-selection-design.md
git commit -m "docs: describe interactive upstream selection"
```

---

## Self-Review

- Spec coverage: Phase 1 implements Step 3 only, preserves Step 1 modeling directions, writes `selection/step3_options.json`, `selection/step3_request.md`, `selection/step3_decision.json`, mirrors `human_review.md`, exposes Web choice, and supports timeout defaulting.
- Deliberate deferral: Step 4 selection remains Phase 2. This matches the spec rollout section and avoids mixing modeling口径 changes into the first implementation.
- Placeholder scan: The plan contains no `TBD`, `TODO`, or unspecified "add tests" steps. Every task names exact files, commands, and expected results.
- Type consistency: Backend field names use `selected_option_id`, `selected_aux_id`, `selection_pending`, `selection_gate`, and `selection_deadline` consistently across service, schema, API, and frontend.
