"""Explicit, event-bound downstream evaluation after an unsuccessful Step 13.

This route preserves the source failure and never completes the production
workflow. Step 16 is analysis-only even when its scientific verdict is PASS.
"""
import os
import uuid

from .domain import InvalidTransition, WorkflowStatus
from .stages import stage_for_step


def authorize(engine, *, expected_revision, reason):
    state = engine.store.load()
    if not reason.strip() or not (state.active_step == 13 or state.last_completed_step == 12):
        raise InvalidTransition("explicit Step13 continuation requires its current source step and reason")
    return engine._transition(expected_revision=expected_revision,
        event_type="GATE2_CONTINUATION_AUTHORIZED", changes={}, event_step=13,
        payload={"reason": reason, "scope": "technical_steps_14_16", "analysis_only": True,
                 "quality_pass_authorized": False, "delivery_allowed": False})


def pending(engine):
    events = engine.store.events()
    consumed = {e.payload.get("authorization_revision") for e in events
                if e.type in {"GATE2_CONTINUATION_READY", "GATE2_CONTINUATION_CONSUMED"}}
    return next((e for e in reversed(events) if e.type == "GATE2_CONTINUATION_AUTHORIZED"
                 and e.revision not in consumed
                 and not any(later.revision > e.revision and later.step == 13
                             and later.type == "STEP_SUCCEEDED" for later in events)), None)


def stop_at_gate2(engine, state, lease, result, validation):
    if state.active_step != 13:
        return None
    grant = pending(engine)
    if validation is not None and validation.pending_action is not None:
        return None
    validation_metadata = validation.metadata if validation is not None else {}
    resume_after = result.metadata.get("resume_after_step", validation_metadata.get("resume_after_step"))
    failed = (result.returncode != 0 or result.metadata.get("resume_after_step") is not None
              or result.metadata.get("judge_completed") is False
              or (validation is not None and not validation.is_valid))
    if grant is None or not failed:
        return None
    source = engine._owned_transition(state, lease,
        event_type="STEP_REOPENED" if resume_after is not None else "STEP_FAILED",
        changes={}, event_step=13, payload={**result.metadata,
            "returncode": result.returncode, "error_class": result.error_class or validation_metadata.get("error_class", ""),
            "validation_metadata": validation_metadata,
            "requested_resume_after_step": resume_after,
            "validation_reason": validation.reason if validation else "",
            "technical_continuation_requested": True, "source_step": 13})
    return engine._owned_transition(source, lease, event_type="GATE2_CONTINUATION_READY",
        changes={"status": WorkflowStatus.PAUSED, "runner_pid": None,
                 "runner_lease_id": None, "heartbeat_at": None}, event_step=13,
        payload={"authorization_revision": grant.revision, "source_event_revision": source.revision,
                 "scope": "technical_steps_14_16", "analysis_only": True, "delivery_allowed": False})


def execute(engine, *, expected_revision):
    state = engine.store.load()
    if state.revision != expected_revision or state.status != WorkflowStatus.PAUSED:
        raise InvalidTransition("continuation requires the exact paused revision")
    if state.runner_pid is not None and engine._pid_is_live(state.runner_pid):
        raise InvalidTransition("continuation already has a live writer")
    events = engine.store.events()
    ready = next((e for e in reversed(events) if e.type == "GATE2_CONTINUATION_READY"), None)
    if ready is None:
        raise InvalidTransition("no event-bound continuation is ready")
    grant_revision = ready.payload["authorization_revision"]
    if any(e.type == "GATE2_CONTINUATION_CONSUMED" and e.payload.get("authorization_revision") == grant_revision
           for e in events):
        raise InvalidTransition("continuation authorization already consumed")
    source = next((e for e in events if e.revision == ready.payload["source_event_revision"]), None)
    if source is None or source.step != 13 or source.type not in {"STEP_FAILED", "STEP_REOPENED"}:
        raise InvalidTransition("continuation source failure is invalid")
    lease = uuid.uuid4().hex
    state = engine._transition(expected_revision=state.revision,
        event_type="GATE2_CONTINUATION_CONSUMED", changes={"status": WorkflowStatus.RUNNING,
            "runner_pid": os.getpid(), "runner_lease_id": lease},
        payload={"authorization_revision": grant_revision, "source_event_revision": source.revision,
                 "delivery_allowed": False}, event_step=13)
    for step_id in (14, 15, 16):
        definition = engine.registry.get(step_id)
        stage = stage_for_step(step_id)
        subtask = next(s for s in stage.subtasks if s.source_step_id == step_id and s.kind == "step")
        state = engine._owned_transition(state, lease, event_type="TECHNICAL_STEP_STARTED",
            changes={"active_step": step_id, "source_step_id": step_id, "active_stage": stage.id,
                     "active_subtask": subtask.key, "attempt": 1}, event_step=step_id,
            payload={"stage": stage.id, "subtask": subtask.key, "source_step": step_id,
                     "authorization_revision": grant_revision, "delivery_allowed": False})
        context = engine._context(state, definition)
        try:
            if step_id == 16:
                result = definition.lifecycle.execute_analysis(context)
                valid = result.returncode == 0
            else:
                from .execution_pipeline import StageExecutionRequest
                request = StageExecutionRequest(definition, context)
                prepared = engine._pipeline.prepare(request)
                if not prepared.ready or prepared.pending_action is not None:
                    raise InvalidTransition("technical continuation prepare gate: " + prepared.reason)
                outcome = engine._pipeline.run(request,
                    after_execute=lambda: engine._refresh_owned_state(lease, active_step=step_id))
                result = outcome.execution
                valid = result.returncode == 0 and outcome.validation is not None and outcome.validation.is_valid
            state = engine._refresh_owned_state(lease, active_step=step_id)
            state = engine._owned_transition(state, lease,
                event_type="TECHNICAL_STEP_VALIDATED" if valid else "TECHNICAL_STEP_FAILED",
                changes={}, event_step=step_id,
                payload={"stage": stage.id, "source_step": step_id, "returncode": result.returncode,
                         "error_class": result.error_class, "result": result.metadata,
                         "delivery_allowed": False, "quality_pass_fabricated": False})
            if not valid:
                break
        except Exception:
            state = engine._refresh_owned_state(lease, active_step=step_id)
            engine._owned_transition(state, lease, event_type="TECHNICAL_CONTINUATION_FAILED",
                changes={"status": WorkflowStatus.PAUSED, "runner_pid": None,
                         "runner_lease_id": None, "heartbeat_at": None},
                payload={"source_step": step_id, "delivery_allowed": False})
            raise
    return engine._owned_transition(state, lease, event_type="TECHNICAL_CONTINUATION_STOPPED",
        changes={"status": WorkflowStatus.PAUSED, "runner_pid": None,
                 "runner_lease_id": None, "heartbeat_at": None},
        payload={"authorization_revision": grant_revision, "delivery_allowed": False,
                 "production_complete": False, "source_step13_failure_preserved": True})
