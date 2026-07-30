from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from .domain import WorkflowState, WorkflowStatus


_STEP_RE = re.compile(r"(Last completed step\*{0,2}\s*[:：]\s*)-?\d+")


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(name, path)
        os.chmod(path, mode)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def _checkpoint(project: Path, state: WorkflowState) -> None:
    path = project / "checkpoint.md"
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    if _STEP_RE.search(text):
        text = _STEP_RE.sub(rf"\g<1>{state.last_completed_step}", text, count=1)
        _atomic_text(path, text)


def _heartbeat(project: Path, state: WorkflowState) -> None:
    path = project / ".heartbeat"
    if state.status is WorkflowStatus.RUNNING:
        step = state.active_step if state.active_step is not None else state.last_completed_step
        content = f"ACTIVE:{step} {state.updated_at}\n"
    elif state.status is WorkflowStatus.RETRYING:
        content = f"RETRYING:{state.active_step} {state.updated_at}\n"
    elif state.status is WorkflowStatus.AWAITING_SELECTION:
        content = f"AWAITING_SELECTION:{state.active_step} {state.updated_at}\n"
    elif state.status is WorkflowStatus.AWAITING_CONSULTATION:
        content = f"CONSULT:{state.active_step} {state.updated_at}\n"
    elif state.status is WorkflowStatus.FAILED:
        content = f"STUCK:{state.active_step} {state.updated_at}\n"
    elif state.status is WorkflowStatus.KILLED:
        content = f"KILLED:{state.active_step or 0} {state.updated_at}\n"
    elif state.status is WorkflowStatus.COMPLETED:
        content = f"{state.last_completed_step} {state.updated_at}\n"
    else:
        path.unlink(missing_ok=True)
        return
    _atomic_text(path, content)


def _markers(project: Path, state: WorkflowState) -> None:
    for name, active in (
        (".paused", state.status is WorkflowStatus.PAUSED),
        (".killed", state.status is WorkflowStatus.KILLED),
    ):
        path = project / name
        if active:
            path.touch()
        else:
            path.unlink(missing_ok=True)
    pid_path = project / ".runner.pid"
    if state.runner_pid is not None:
        _atomic_text(pid_path, f"{state.runner_pid}\n")
    else:
        pid_path.unlink(missing_ok=True)


def runtime_payload(state: WorkflowState) -> dict:
    current_step = state.active_step if state.active_step is not None else max(0, state.last_completed_step)
    action = state.pending_action or {}
    display = {
        WorkflowStatus.READY: "就绪",
        WorkflowStatus.RUNNING: "运行中",
        WorkflowStatus.RETRYING: "重试中",
        WorkflowStatus.AWAITING_SELECTION: "等待选方案",
        WorkflowStatus.AWAITING_CONSULTATION: "等待咨询",
        WorkflowStatus.PAUSED: "已暂停",
        WorkflowStatus.KILLED: "已终止",
        WorkflowStatus.FAILED: "失败",
        WorkflowStatus.COMPLETED: "已完成",
        WorkflowStatus.ARCHIVING: "归档中",
        WorkflowStatus.INTERRUPTED: "已中断",
    }[state.status]
    return {
        "version": 3,
        "state": state.status.value,
        "current_step": current_step,
        "current_action": action.get("type") or (
            "step_dispatch" if state.status is WorkflowStatus.RUNNING else "idle"
        ),
        "display_status": display,
        "consultation_gate": action.get("gate")
        if state.status is WorkflowStatus.AWAITING_CONSULTATION
        else None,
        "pid": state.runner_pid,
        "updated_at": state.updated_at,
        "reason_code": (
            "OPTION_SELECTION_PENDING"
            if state.status is WorkflowStatus.AWAITING_SELECTION
            else "CONSULTATION_PENDING"
            if state.status is WorkflowStatus.AWAITING_CONSULTATION
            else ""
        ),
        "reason_summary": display,
        "since": state.updated_at,
        "last_event_at": state.last_event_at,
        "suggested_actions": ["refresh_status"],
        "evidence": [
            {"kind": "database", "path": ".factory/state.db", "revision": state.revision}
        ],
        "revision": state.revision,
        "runtime_generation": state.runtime_generation,
        "last_completed_step": state.last_completed_step,
        "pending_action": state.pending_action,
    }


def write_compatibility_projections(project_dir: str | Path, state: WorkflowState) -> None:
    project = Path(project_dir)
    _checkpoint(project, state)
    _heartbeat(project, state)
    _markers(project, state)
    _atomic_text(
        project / "diagnostics" / "status.json",
        json.dumps(runtime_payload(state), ensure_ascii=False, indent=2) + "\n",
    )
