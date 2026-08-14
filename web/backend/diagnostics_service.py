from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from project_diagnostics import load_recent_events, load_status
from factory_core.storage import SQLiteStateStore
from factory_core.workflow_events import (
    ENVELOPE_KEY,
    ReplayIntegrityError,
    project_audit_timeline,
    project_runtime_diagnostics,
    replay_events,
    replay_state,
)


PRIORITY = {
    "WORKFLOW_REPLAY_MISMATCH": 0,
    "CONSULTATION_PENDING": 1,
    "AWAITING_STEP8_5": 2,
    "VERIFY_OUTPUT_FAILED": 3,
    "NO_LOG_PROGRESS": 4,
    "LOCK_STALE_RECLAIMED": 5,
    "ORPHANED_DECISION_ARTIFACT": 2,
    "HUMAN_DECISION_REQUIRED": 1,
}

BADGES = {
    "WORKFLOW_REPLAY_MISMATCH": "事件重放异常",
    "CONSULTATION_PENDING": "等待人工",
    "AWAITING_STEP8_5": "等待 8.5 门禁",
    "VERIFY_OUTPUT_FAILED": "验证失败待重试",
    "NO_LOG_PROGRESS": "静默过久",
    "LOCK_STALE_RECLAIMED": "锁已回收",
    "ORPHANED_DECISION_ARTIFACT": "决策证据待对账",
    "HUMAN_DECISION_REQUIRED": "等待人工",
}


def _fallback_status(project: Path, is_running: bool, consultation_pending: bool, consultation_gate: str | None) -> dict:
    heartbeat = ""
    heartbeat_file = project / ".heartbeat"
    if heartbeat_file.is_file():
        heartbeat = heartbeat_file.read_text(encoding="utf-8", errors="replace").strip()

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
        step_match = re.search(r"AWAITING_STEP8_5:(\d+)", heartbeat)
        step = int(step_match.group(1)) if step_match else 8
        return {
            "state": "waiting",
            "current_step": step,
            "current_action": "step8_5_gate_review",
            "reason_code": "AWAITING_STEP8_5",
            "reason_summary": "Step 8.5 未通过，等待补足 reviewer entry 材料",
            "suggested_actions": ["open_entry_gate", "open_reviewer_entry_artifacts", "refresh_status"],
            "evidence": [{"kind": "file", "path": "entry_gate.md"}],
        }

    if heartbeat.startswith("STUCK:"):
        match = re.search(r"STUCK:(\d+)", heartbeat)
        step = int(match.group(1)) if match else 0
        return {
            "state": "retrying" if is_running else "waiting",
            "current_step": step,
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


def build_project_diagnostics(
    project: Path,
    base_name: str,
    *,
    is_running: bool,
    consultation_pending: bool,
    consultation_gate: str | None,
) -> dict:
    del base_name
    store = SQLiteStateStore(project)
    if store.exists:
        try:
            native_state = store.load()
            if native_state.control_mode == "engine":
                native_events = store.events()
                if any(
                    isinstance(event.payload.get(ENVELOPE_KEY), dict)
                    for event in native_events
                ):
                    try:
                        rebuilt = replay_events(native_events)
                        if rebuilt != replay_state(native_state):
                            raise ReplayIntegrityError(
                                "replayed state differs from authoritative state"
                            )
                    except ReplayIntegrityError as exc:
                        return {
                            "source": "workflow_events",
                            "status": {
                                "version": 3,
                                "state": "failed",
                                "current_step": native_state.source_step_id
                                or native_state.active_step
                                or max(native_state.last_completed_step, 0),
                                "current_action": "workflow_replay_audit",
                                "reason_code": "WORKFLOW_REPLAY_MISMATCH",
                                "reason_summary": str(exc),
                                "suggested_actions": [
                                    "open_audit_timeline",
                                    "refresh_status",
                                ],
                                "evidence": [
                                    {
                                        "kind": "database",
                                        "path": ".factory/state.db",
                                        "revision": native_state.revision,
                                    }
                                ],
                            },
                            "events": project_audit_timeline(native_events)[-5:],
                            "actions": [
                                {"id": "open_audit_timeline"},
                                {"id": "refresh_status"},
                            ],
                            "action_center": {"pending": [], "history": []},
                            "recovery": {"latest": None, "history": []},
                            "orphaned_artifacts": [],
                        }
                projected = project_runtime_diagnostics(native_events, native_state)
                orphaned = []
                for path in sorted((project / "selection").glob("*_decision.json")):
                    gate = path.name.removesuffix("_decision.json")
                    if store.decision(gate) is None:
                        orphaned.append(
                            {"gate": gate, "path": path.relative_to(project).as_posix()}
                        )
                pending = native_state.pending_action or {}
                pending_gate = str(pending.get("gate") or "")
                pending_metadata = pending.get("metadata") or {}
                human_request = pending_metadata.get("human_decision") or {}
                if (
                    pending_gate
                    and human_request.get("kind") == "consultation"
                    and store.decision(pending_gate) is None
                ):
                    from .consultation_service import gate_ready

                    if gate_ready(project / "human_review.md", pending_gate):
                        orphaned.append(
                            {"gate": pending_gate, "path": "human_review.md"}
                        )
                projected["orphaned_artifacts"] = orphaned
                if orphaned:
                    if not projected["status"].get("reason_code"):
                        projected["status"].update(
                            reason_code="ORPHANED_DECISION_ARTIFACT",
                            reason_summary=(
                                "A committed decision projection is not linked to the "
                                "workflow decision ledger"
                            ),
                        )
                    if not any(
                        action.get("id") == "retry_human_decision_commit"
                        for action in projected["actions"]
                    ):
                        projected["actions"].append(
                            {"id": "retry_human_decision_commit"}
                        )
                return {"source": "workflow_events", **projected}
        except (OSError, RuntimeError, ValueError):
            # A corrupt or unsupported Native database must not hide the legacy
            # diagnostic fallback used by operators during recovery.
            pass
    status = load_status(project)
    events = load_recent_events(project, limit=5)
    if status:
        source = "runner"
    else:
        source = "fallback"
        status = _fallback_status(project, is_running, consultation_pending, consultation_gate)
    actions = [{"id": action_id} for action_id in status.get("suggested_actions", [])]
    return {"source": source, "status": status, "events": events, "actions": actions}


def summarize_project_diagnostics(diag: dict) -> dict:
    code = diag["status"].get("reason_code", "")
    return {
        "diagnostic_reason_code": code or None,
        "diagnostic_badge": BADGES.get(code),
        "diagnostic_priority": PRIORITY.get(code, 999),
    }
