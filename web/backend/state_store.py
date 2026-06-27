from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

TOTAL_STEPS = 16
ROOT = Path(__file__).resolve().parents[2]
PROJECT_DIAGNOSTICS_PATH = ROOT / "scripts" / "project_diagnostics.py"


def _load_status(project_path: Path) -> dict | None:
    spec = importlib.util.spec_from_file_location(
        "_project_diagnostics_for_state_store",
        PROJECT_DIAGNOSTICS_PATH,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load project diagnostics from {PROJECT_DIAGNOSTICS_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_status(project_path)


def _read_checkpoint_step(project_path: Path) -> int:
    checkpoint_file = project_path / "checkpoint.md"
    if not checkpoint_file.is_file():
        return 0
    content = checkpoint_file.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"Last completed step\*{0,2}\s*[:：]\s*(-?\d+)", content)
    if not match:
        return 0
    return max(0, int(match.group(1)))


def _read_pid(project_path: Path) -> int | None:
    pid_file = project_path / ".runner.pid"
    if not pid_file.is_file():
        return None
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, OSError, ProcessLookupError):
        return None


def _validate_pid(pid: int | None) -> int | None:
    if pid is None:
        return None
    try:
        pid = int(pid)
        os.kill(pid, 0)
        return pid
    except (ValueError, OSError, ProcessLookupError, TypeError):
        return None


def _read_consultation(project_path: Path) -> tuple[bool, str | None]:
    await_marker = project_path / ".awaiting_consultation"
    if await_marker.is_file():
        content = await_marker.read_text(encoding="utf-8", errors="replace")
        gate_match = re.search(r"GATE:([^\s]+)", content)
        gate = gate_match.group(1).strip() if gate_match else None
        return True, gate

    consult_dir = project_path / "consultation"
    if not consult_dir.is_dir():
        return False, None
    for req_file in sorted(consult_dir.glob("*_request.md")):
        return True, req_file.stem.replace("_request", "")
    return False, None


def _progress_percent(current_step: int) -> float:
    return round(min(100.0, max(0, current_step) / TOTAL_STEPS * 100), 1)


def _snapshot_timestamp(project_path: Path, snapshot: dict) -> int:
    for key in ("updated_at", "last_event_at", "since"):
        value = snapshot.get(key)
        if value is not None:
            return int(value)
    return int(project_path.stat().st_mtime)


def _from_snapshot(project_path: Path, base_name: str, snapshot: dict) -> dict:
    current_step = max(0, int(snapshot.get("current_step", 0)))
    pid = _validate_pid(snapshot.get("pid"))
    consultation_gate = snapshot.get("consultation_gate")
    display_status = snapshot.get("display_status") or snapshot.get("state", "unknown")
    return {
        "base_name": base_name,
        "status": snapshot.get("state", "unknown"),
        "display_status": display_status,
        "current_step": current_step,
        "total_steps": TOTAL_STEPS,
        "progress_percent": _progress_percent(current_step),
        "last_updated": _snapshot_timestamp(project_path, snapshot),
        "is_running": pid is not None,
        "pid": pid,
        "consultation_pending": snapshot.get("state") == "awaiting_consultation",
        "consultation_gate": consultation_gate,
        "reason_code": snapshot.get("reason_code", ""),
        "reason_summary": snapshot.get("reason_summary", ""),
        "suggested_actions": list(snapshot.get("suggested_actions", [])),
        "evidence": list(snapshot.get("evidence", [])),
    }


def _fallback_status(project_path: Path, base_name: str) -> dict:
    current_step = _read_checkpoint_step(project_path)
    pid = _read_pid(project_path)
    consultation_pending, consultation_gate = _read_consultation(project_path)

    if consultation_pending:
        return {
            "base_name": base_name,
            "status": "awaiting_consultation",
            "display_status": "等待咨询",
            "current_step": current_step,
            "total_steps": TOTAL_STEPS,
            "progress_percent": _progress_percent(current_step),
            "last_updated": int(project_path.stat().st_mtime),
            "is_running": False,
            "pid": None,
            "consultation_pending": True,
            "consultation_gate": consultation_gate,
            "reason_code": "CONSULTATION_PENDING",
            "reason_summary": "等待人工咨询回填",
            "suggested_actions": ["open_consultation_request", "open_human_review", "refresh_status"],
            "evidence": [{"kind": "file", "path": f"consultation/{consultation_gate or 'dynamic'}_request.md"}],
        }

    status = "running" if pid is not None else "completed" if current_step >= TOTAL_STEPS else "ready"
    return {
        "base_name": base_name,
        "status": status,
        "display_status": "运行中" if status == "running" else "已完成" if status == "completed" else "就绪",
        "current_step": current_step,
        "total_steps": TOTAL_STEPS,
        "progress_percent": _progress_percent(current_step),
        "last_updated": int(project_path.stat().st_mtime),
        "is_running": pid is not None,
        "pid": pid,
        "consultation_pending": False,
        "consultation_gate": None,
        "reason_code": "",
        "reason_summary": "",
        "suggested_actions": ["refresh_status"],
        "evidence": [],
    }


def read_runtime_status(project_path: str | Path, base_name: str) -> dict:
    project = Path(project_path)
    snapshot = _load_status(project)
    if snapshot:
        return _from_snapshot(project, base_name, snapshot)
    return _fallback_status(project, base_name)
