from __future__ import annotations

import json
import fcntl
import hashlib
import os
import re
import tempfile
from pathlib import Path

from .contest import phase_for_step
from .domain import WorkflowState, WorkflowStatus
from .stages import projected_stage_cursor


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
    source_step = (
        state.source_step_id
        if state.source_step_id is not None
        else state.active_step
    )
    if state.status is WorkflowStatus.RUNNING:
        step = source_step if source_step is not None else state.last_completed_step
        content = f"ACTIVE:{step} {state.updated_at}\n"
    elif state.status is WorkflowStatus.RETRYING:
        content = f"RETRYING:{source_step} {state.updated_at}\n"
    elif state.status is WorkflowStatus.AWAITING_SELECTION:
        content = f"AWAITING_SELECTION:{source_step} {state.updated_at}\n"
    elif state.status is WorkflowStatus.AWAITING_CONSULTATION:
        content = f"CONSULT:{source_step} {state.updated_at}\n"
    elif state.status is WorkflowStatus.FAILED:
        content = f"STUCK:{source_step} {state.updated_at}\n"
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


def runtime_payload(
    state: WorkflowState,
    *,
    contest_policy: dict | None = None,
    now_epoch: int | None = None,
) -> dict:
    stage_cursor = projected_stage_cursor(state)
    current_step = (
        stage_cursor["source_step_id"]
        if stage_cursor["source_step_id"] is not None
        else max(0, state.last_completed_step)
    )
    phase = phase_for_step(min(current_step, 16))
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
        "version": 5,
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
        "scheduler_generation": state.scheduler_generation,
        "stage_catalog_version": stage_cursor["stage_catalog_version"],
        "last_completed_stage": stage_cursor["last_completed_stage"],
        "active_stage": stage_cursor["active_stage"],
        "active_stage_name": stage_cursor["active_stage_name"],
        "active_subtask": stage_cursor["active_subtask"],
        "source_step_id": stage_cursor["source_step_id"],
        "last_completed_step": state.last_completed_step,
        "pending_action": state.pending_action,
        "contest_profile": contest_policy.get("profile") if contest_policy else None,
        "contest_phase": {
            "id": phase.id,
            "name": phase.name,
            "human_gate": phase.human_gate,
        },
        "contest_started_at": contest_policy.get("contest_started_at") if contest_policy else None,
        "contest_deadline_at": contest_policy.get("contest_deadline_at") if contest_policy else None,
        "content_freeze_at": contest_policy.get("content_freeze_at") if contest_policy else None,
        "delivery_freeze_at": contest_policy.get("delivery_freeze_at") if contest_policy else None,
        "delivery_reserve_seconds": contest_policy.get("delivery_reserve_seconds") if contest_policy else None,
        "remaining_seconds": max(
            0, int(contest_policy["contest_deadline_at"]) - int(now_epoch)
        ) if contest_policy and now_epoch is not None else None,
    }


AUDIT_FIELDS = (
    "execution_state", "recorded_workflow_state", "workflow_error", "evidence_validity", "evidence_errors",
    "scientific_verdict", "raw_scientific_verdict", "review_mode",
    "score_available", "official_score", "diagnostic_score", "delivery_allowed",
)


def authoritative_status(project: Path, snapshot: dict | None = None) -> dict:
    from .storage import SQLiteStateStore
    from .workflow_events import project_runtime_diagnostics
    snapshot = snapshot or SQLiteStateStore(project).status_snapshot()
    state, events = snapshot["state"], snapshot["events"]
    payload = runtime_payload(state, contest_policy=snapshot["contest_policy"],
                              now_epoch=snapshot["now_epoch"])
    projected = project_runtime_diagnostics(events, state)["status"]
    for key in ("current_action", "reason_code", "reason_summary", "suggested_actions", "evidence"):
        payload[key] = projected[key]
    payload.update(audit_status_fields(project, state, events))
    if payload["execution_state"] != state.status.value:
        payload.update(state=payload["execution_state"], display_status="已中断", pid=None,
                       reason_code="RUNNER_EXIT_UNVERIFIED",
                       reason_summary="Recorded worker is no longer live; descendant exit is unverified")
    return payload


def write_compatibility_projections(project_dir: str | Path, state: WorkflowState) -> dict:
    """Serialize writers; publish a manifest last so partial file sets are rejected."""
    project = Path(project_dir)
    from .storage import SQLiteStateStore
    store = SQLiteStateStore(project)
    lock_path = project / ".factory/projection.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if store.exists:
            snapshot = store.status_snapshot()
            state = snapshot["state"]
            payload = authoritative_status(project, snapshot)
        else:
            payload = runtime_payload(state) | audit_status_fields(project, state, [])
        _checkpoint(project, state)
        if payload["state"] == "interrupted" and state.status.value != "interrupted":
            from dataclasses import replace
            view_state = replace(state, status=WorkflowStatus.INTERRUPTED, runner_pid=None)
        else:
            view_state = state
        _heartbeat(project, view_state)
        _markers(project, view_state)
        files = {}
        for name in ("checkpoint.md", ".heartbeat", ".paused", ".killed", ".runner.pid"):
            path = project / name
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        payload["projection_files"] = files
        _atomic_text(project / "diagnostics/status.json",
                     json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return payload


def read_compatibility_projection(project_dir: str | Path) -> dict | None:
    project = Path(project_dir)
    try:
        path = project / "diagnostics/status.json"
        data = path.read_bytes()
        payload = json.loads(data)
        for name, expected in payload.get("projection_files", {}).items():
            if name not in {"checkpoint.md", ".heartbeat", ".paused", ".killed", ".runner.pid"}:
                return None
            member = project / name
            actual = hashlib.sha256(member.read_bytes()).hexdigest() if member.is_file() else None
            if actual != expected:
                return None
        if path.read_bytes() != data:
            return None
        return payload
    except (OSError, ValueError, TypeError):
        return None


def audit_status_fields(project: Path, state: WorkflowState, events) -> dict:
    """Current execution, evidence, scientific judgment and delivery are separate."""
    events = [event for event in events if event.revision <= state.revision]
    fields = {"execution_state": state.status.value, "recorded_workflow_state": state.status.value,
              "evidence_validity": "UNAVAILABLE",
              "evidence_errors": [], "review_mode": None,
              "scientific_verdict": "UNAVAILABLE", "raw_scientific_verdict": None,
              "workflow_error": None, "score_available": False, "official_score": None,
              "diagnostic_score": None, "delivery_allowed": False}
    if state.status in {WorkflowStatus.FAILED, WorkflowStatus.RETRYING, WorkflowStatus.INTERRUPTED}:
        for event in reversed(events):
            if event.type in {"STEP_STARTED", "WORKER_LAUNCHED", "RUN_RESUMED", "STEP_SUCCEEDED"}:
                break
            if event.payload.get("error_class"):
                fields["workflow_error"] = event.payload["error_class"]
                break
    if state.runner_pid and state.status in {WorkflowStatus.RUNNING, WorkflowStatus.RETRYING}:
        from .adapters.infrastructure.process import _process_identity
        current_identity = _process_identity(state.runner_pid)
        launch = next((e for e in reversed(events) if e.type == "WORKER_LAUNCHED"
                       and e.payload.get("worker_pid") == state.runner_pid), None)
        expected_identity = launch.payload.get("worker_identity") if launch else None
        if current_identity is None or (expected_identity and current_identity != expected_identity):
            fields.update(execution_state="interrupted", workflow_error="RUNNER_EXIT_UNVERIFIED")
    aggregate = project / "judge_outputs/aggregate.json"
    precheck = project / "judge_outputs/precheck.json"
    if not aggregate.is_file() and not precheck.is_file():
        return fields
    review_events = [e for e in events if e.step in {13, 16} and e.type in {
        "STEP_STARTED", "STEP_SUCCEEDED", "STAGE_SUBTASK_SUCCEEDED", "STEP_FAILED",
    }]
    mode = ("math_only" if review_events[-1].step == 13 else "final") if review_events else (
        "final" if aggregate.is_file() else "math_only")
    fields["review_mode"] = mode
    if not (aggregate if mode == "final" else precheck).is_file():
        return fields
    try:
        from scripts.submission_fingerprint import submission_fingerprint
        value = json.loads((aggregate if mode == "final" else precheck).read_text())
        if not isinstance(value, dict):
            raise ValueError("judge status must be an object")
        fields["raw_scientific_verdict"] = value.get("verdict")
        if mode == "final":
            from scripts.judgment_receipt import verify_receipt
            valid, errors = verify_receipt(project, expected_input_fingerprint=submission_fingerprint(project))
        else:
            from .judge_batch import verify, precheck_input_fingerprint
            binding = value["audit_binding"]
            response, metadata = verify(project, binding)
            if metadata.get("execution_step_id") != 13:
                raise ValueError("precheck is not a Step 13 call")
            verdict = re.search(r"(?m)^VERDICT:\s*(\S+)", response.decode())
            source = verdict.group(1) if verdict else "MISSING"
            mapped = {"PASS": "PRECHECK_PASS", "FAIL": "REOPEN_REVISION_MODEL"}.get(source, "INDETERMINATE_REVIEW")
            if value.get("source_verdict") != source or value.get("verdict") != mapped:
                raise ValueError("precheck verdict differs from sealed response")
            if value.get("input_fingerprint") != precheck_input_fingerprint(project):
                raise ValueError("precheck inputs changed")
            valid, errors = True, []
        step = 16 if mode == "final" else 13
        attempts = [e for e in events if e.step == step and e.type == "STEP_STARTED"]
        if attempts:
            # Artifacts from a preceding attempt cannot stand for the current call.
            current_results = [e for e in events if e.revision > attempts[-1].revision
                               and e.step == step and e.payload.get("audit_binding") == value.get("audit_binding")]
            if mode == "math_only" and not current_results:
                valid, errors = False, ["current precheck attempt has no committed result"]
            if mode == "final" and state.active_step == 16 and state.status in {
                WorkflowStatus.RUNNING, WorkflowStatus.RETRYING, WorkflowStatus.FAILED,
            }:
                valid, errors = False, ["current final audit has not completed"]
        fields["evidence_validity"] = "VALID" if valid else "INVALID"
        fields["evidence_errors"] = list(errors)
        if valid:
            fields["scientific_verdict"] = value.get("verdict", "UNAVAILABLE")
            if mode == "final" and value.get("score_available") is True:
                fields["score_available"] = True
                fields["diagnostic_score"] = value.get("overall_score")
            if mode == "final":
                from .phase9_delivery_fence import legacy_delivery_projection_allowed
                from scripts.submission_fingerprint import final_judge_is_current
                fields["delivery_allowed"] = (fields["execution_state"] != "interrupted"
                                               and legacy_delivery_projection_allowed(project)
                                               and final_judge_is_current(project))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        fields.update(evidence_validity="INVALID", evidence_errors=[str(exc)])
    return fields
