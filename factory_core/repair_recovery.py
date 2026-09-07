"""One explicit repair attempt bound to changed inputs and implementation bytes."""
import hashlib
from pathlib import Path

from .dirty import capture_artifact_manifest, manifest_fingerprint
from .domain import InvalidTransition, WorkflowStatus


def implementation_version():
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for directory in ("factory_core", "scripts"):
        for path in sorted((root / directory).rglob("*.py")):
            digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def authorize(engine, *, expected_revision, reason):
    state = engine.store.load()
    if state.status not in {WorkflowStatus.FAILED, WorkflowStatus.PAUSED} or state.active_step is None:
        raise InvalidTransition("repair recovery requires a stopped failed attempt")
    if state.runner_pid is not None and engine._pid_is_live(state.runner_pid):
        raise InvalidTransition("repair recovery requires no live runner")
    if not reason.strip():
        raise InvalidTransition("explicit repair reason is required")
    current = manifest_fingerprint(capture_artifact_manifest(engine.project_dir))
    baseline = engine.store.stage_cursor_input()
    prior_start = next((e for e in reversed(engine.store.events())
                        if e.type == "STEP_STARTED" and e.step == state.active_step), None)
    old_input = baseline.get("input_fingerprint") if baseline else (
        prior_start.payload.get("input_fingerprint") if prior_start else None)
    old_code = prior_start.payload.get("implementation_version") if prior_start else None
    code = implementation_version()
    if old_input is None:
        raise InvalidTransition("repair recovery requires an attested failed-attempt input baseline")
    if current == old_input and (old_code is None or old_code == code):
        raise InvalidTransition("unchanged input and implementation cannot renew an exhausted budget")
    from scripts.claim_graph import claim_binding_issues
    issues = claim_binding_issues(engine.project_dir)
    if issues:
        raise InvalidTransition("REPAIR_UPSTREAM_MISSING: " + str(issues))
    for event in engine.store.events():
        if (event.type == "REPAIR_RETRY_AUTHORIZED" and event.step == state.active_step
                and event.payload.get("input_fingerprint") == current
                and event.payload.get("implementation_version") == code):
            raise InvalidTransition("this repair version already has its bounded attempt")
    return engine._transition(expected_revision=expected_revision,
        event_type="REPAIR_RETRY_AUTHORIZED", changes={"status": WorkflowStatus.READY},
        event_step=state.active_step, payload={"reason": reason, "failed_attempt": state.attempt,
            "input_fingerprint": current, "implementation_version": code,
            "stage": state.active_stage, "subtask": state.active_subtask,
            "additional_attempts": 1, "normal_max_attempts_unchanged": True})


def usable(engine, state):
    for event in reversed(engine.store.events()):
        if event.type == "REPAIR_RETRY_AUTHORIZED" and event.step == state.active_step:
            p = event.payload
            if (p["failed_attempt"] == state.attempt and p["stage"] == state.active_stage
                    and p["subtask"] == state.active_subtask
                    and p["implementation_version"] == implementation_version()
                    and p["input_fingerprint"] == manifest_fingerprint(capture_artifact_manifest(engine.project_dir))):
                return event.revision
            break
    return None
