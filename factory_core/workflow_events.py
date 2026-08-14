from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from typing import Any, Iterable, Mapping

from .domain import WorkflowEvent, WorkflowState, WorkflowStatus


EVENT_VERSION = 1
REPLAY_STATE_VERSION = 1
ENVELOPE_KEY = "_workflow"

_REPLAY_FIELDS = (
    "schema_version",
    "project_id",
    "project_type",
    "control_mode",
    "runtime_generation",
    "scheduler_generation",
    "stage_catalog_version",
    "status",
    "last_completed_step",
    "active_step",
    "last_completed_stage",
    "active_stage",
    "active_subtask",
    "source_step_id",
    "attempt",
    "revision",
    "pending_action",
    "storage_scope",
)

_CANONICAL_TYPES = {
    "STEP_PREPARE_AWAITING_ACTION": "HUMAN_DECISION_REQUESTED",
    "AWAITING_ACTION": "HUMAN_DECISION_REQUESTED",
    "ACTION_RESOLVED": "HUMAN_DECISION_RECORDED",
    "STEP_FAILED": "GATE_BLOCKED",
    "CONTEST_DEADLINE_EXHAUSTED": "GATE_BLOCKED",
    "RETRY_SCHEDULED": "RECOVERY_PLANNED",
    "RECOVERY_DECIDED": "RECOVERY_PLANNED",
    "STEP_REOPENED": "WORK_REOPENED",
    "STAGE_SEMANTIC_REOPENED": "WORK_REOPENED",
    "STAGE_CHECKPOINT_INVALIDATED": "CHECKPOINT_INVALIDATED",
}


class ReplayIntegrityError(ValueError):
    """Raised when an event stream cannot reproduce its recorded state hash."""


@dataclass(frozen=True)
class GateReason:
    code: str
    message: str = ""
    evidence: tuple[str, ...] = ()
    recovery_target: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        _json_value(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def replay_state(state: WorkflowState | Mapping[str, Any]) -> dict[str, Any]:
    raw: Mapping[str, Any]
    if isinstance(state, Mapping):
        raw = state
    else:
        raw = {field: getattr(state, field) for field in _REPLAY_FIELDS}
    return {field: _json_value(raw.get(field)) for field in _REPLAY_FIELDS}


def canonical_event_type(event_type: str) -> str:
    if event_type.startswith("SOLVER_JOB_"):
        return event_type
    return _CANONICAL_TYPES.get(event_type, event_type)


def normalize_reason(
    event_type: str, payload: Mapping[str, Any]
) -> GateReason:
    raw = payload.get("reason")
    if isinstance(raw, Mapping):
        code = str(
            raw.get("code")
            or payload.get("error_class")
            or payload.get("reason_code")
            or canonical_event_type(event_type)
        )
        message = str(raw.get("message") or "")
        evidence = tuple(raw.get("evidence") or payload.get("evidence") or ())
    else:
        code = str(
            payload.get("error_class")
            or payload.get("reason_code")
            or canonical_event_type(event_type)
        )
        message = str(raw or payload.get("message") or "")
        evidence = tuple(payload.get("evidence") or ())
    recovery_target = {
        key: payload[key]
        for key in ("resume_after_step", "stage", "subtask", "source_step")
        if payload.get(key) is not None
    }
    return GateReason(
        code=code,
        message=message,
        evidence=evidence,
        recovery_target=recovery_target or None,
    )


def build_event_payload(
    *,
    project_id: str,
    revision: int,
    event_type: str,
    created_at: int,
    payload: Mapping[str, Any] | None,
    before: WorkflowState | Mapping[str, Any] | None,
    after: WorkflowState | Mapping[str, Any],
    force_snapshot: bool = False,
) -> dict[str, Any]:
    """Add a versioned replay envelope without changing legacy payload fields."""

    safe_payload = dict(_json_value(payload or {}))
    before_state = replay_state(before) if before is not None else None
    after_state = replay_state(after)
    if force_snapshot or before_state is None:
        patch = after_state
        patch_mode = "snapshot"
    else:
        patch = {
            key: value
            for key, value in after_state.items()
            if before_state.get(key) != value
        }
        patch_mode = "merge"
    event_id_seed = {
        "project_id": project_id,
        "revision": revision,
        "event_type": event_type,
        "created_at": created_at,
    }
    side_effect_refs = list(
        safe_payload.get("artifact_refs")
        or safe_payload.get("side_effect_refs")
        or ()
    )
    if safe_payload.get("receipt_path") and safe_payload.get("receipt_sha256"):
        side_effect_refs.append(
            {
                "path": safe_payload["receipt_path"],
                "sha256": safe_payload["receipt_sha256"],
                "kind": "receipt",
            }
        )
    safe_payload[ENVELOPE_KEY] = {
        "event_version": EVENT_VERSION,
        "event_id": canonical_hash(event_id_seed)[:32],
        "canonical_type": canonical_event_type(event_type),
        "replay_state_version": REPLAY_STATE_VERSION,
        "state_patch_mode": patch_mode,
        "state_patch": patch,
        "state_hash_after": canonical_hash(after_state),
        "scheduler_generation": after_state.get("scheduler_generation"),
        "coordinate_authority": (
            "stage" if after_state.get("scheduler_generation") == "stage_v1" else "step"
        ),
        "stage_id": after_state.get("active_stage"),
        "subtask": after_state.get("active_subtask"),
        "source_step_id": after_state.get("source_step_id"),
        "reason": normalize_reason(event_type, safe_payload).to_dict(),
        "side_effect_refs": side_effect_refs,
    }
    return safe_payload


def replay_events(
    events: Iterable[WorkflowEvent], *, verify_hashes: bool = True
) -> dict[str, Any]:
    """Purely rebuild the stable workflow state from versioned events."""

    state: dict[str, Any] = {}
    saw_snapshot = False
    for event in events:
        envelope = event.payload.get(ENVELOPE_KEY)
        if not isinstance(envelope, dict):
            continue
        if int(envelope.get("event_version", 0)) != EVENT_VERSION:
            raise ReplayIntegrityError(
                f"unsupported workflow event version at revision {event.revision}"
            )
        mode = envelope.get("state_patch_mode")
        patch = envelope.get("state_patch")
        if not isinstance(patch, dict) or mode not in {"snapshot", "merge"}:
            raise ReplayIntegrityError(
                f"invalid state patch at revision {event.revision}"
            )
        if mode == "snapshot":
            state = dict(patch)
            saw_snapshot = True
        elif not saw_snapshot:
            raise ReplayIntegrityError(
                f"event stream has no replay snapshot before revision {event.revision}"
            )
        else:
            state.update(patch)
        expected = envelope.get("state_hash_after")
        if verify_hashes and expected != canonical_hash(state):
            raise ReplayIntegrityError(
                f"state hash mismatch at workflow revision {event.revision}"
            )
    if not saw_snapshot:
        raise ReplayIntegrityError("event stream contains no versioned replay snapshot")
    return state


def _reason(event: WorkflowEvent) -> dict[str, Any]:
    envelope = event.payload.get(ENVELOPE_KEY)
    if isinstance(envelope, dict) and isinstance(envelope.get("reason"), dict):
        return dict(envelope["reason"])
    raw = event.payload.get("reason")
    if isinstance(raw, dict):
        return dict(raw)
    code = event.payload.get("error_class") or event.payload.get("reason_code")
    return {
        "code": str(code or canonical_event_type(event.type)).lower(),
        "message": str(raw or event.payload.get("message") or ""),
        "evidence": list(event.payload.get("evidence") or ()),
    }


def project_action_center(events: Iterable[WorkflowEvent]) -> dict[str, Any]:
    """Project pending/resolved human actions without mutating workflow state."""

    active: dict[str, dict[str, Any]] = {}
    history: list[dict[str, Any]] = []
    for event in events:
        canonical = canonical_event_type(event.type)
        envelope = event.payload.get(ENVELOPE_KEY)
        if isinstance(envelope, dict):
            canonical = str(envelope.get("canonical_type") or canonical)
        if canonical == "HUMAN_DECISION_REQUESTED" or isinstance(
            event.payload.get("action"), dict
        ):
            action = event.payload.get("action") or event.payload.get("pending_action") or {}
            gate = str(action.get("gate") or event.payload.get("gate") or f"revision-{event.revision}")
            record = {
                "gate": gate,
                "request_id": action.get("request_id"),
                "kind": action.get("kind"),
                "action_type": action.get("type"),
                "requested_revision": event.revision,
                "reason": _reason(event),
            }
            active[gate] = record
            history.append({"status": "pending", **record})
        elif canonical == "HUMAN_DECISION_RECORDED":
            resolution = event.payload.get("resolution") or {}
            gate = str(resolution.get("gate") or event.payload.get("gate") or "")
            if gate:
                active.pop(gate, None)
            history.append(
                {
                    "status": "resolved",
                    "gate": gate or None,
                    "revision": event.revision,
                    "resolution": resolution,
                }
            )
    return {"pending": list(active.values()), "history": history}


def project_recovery_status(events: Iterable[WorkflowEvent]) -> dict[str, Any]:
    """Describe recorded recovery intent; never choose a recovery target."""

    latest: dict[str, Any] | None = None
    history: list[dict[str, Any]] = []
    for event in events:
        canonical = canonical_event_type(event.type)
        envelope = event.payload.get(ENVELOPE_KEY)
        if isinstance(envelope, dict):
            canonical = str(envelope.get("canonical_type") or canonical)
        if canonical not in {
            "GATE_BLOCKED",
            "RECOVERY_PLANNED",
            "WORK_REOPENED",
            "CHECKPOINT_INVALIDATED",
        }:
            continue
        record = {
            "revision": event.revision,
            "event_type": event.type,
            "canonical_type": canonical,
            "step": event.step,
            "stage": event.payload.get("stage"),
            "subtask": event.payload.get("subtask"),
            "decision": event.payload.get("decision"),
            "resume_after_step": event.payload.get("resume_after_step"),
            "reason": _reason(event),
        }
        latest = record
        history.append(record)
    return {"latest": latest, "history": history}


def project_audit_timeline(events: Iterable[WorkflowEvent]) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []
    for event in events:
        envelope = event.payload.get(ENVELOPE_KEY)
        canonical = canonical_event_type(event.type)
        event_id = None
        stage_id = event.payload.get("stage")
        subtask = event.payload.get("subtask")
        side_effect_refs: list[Any] = []
        if isinstance(envelope, dict):
            canonical = str(envelope.get("canonical_type") or canonical)
            event_id = envelope.get("event_id")
            stage_id = envelope.get("stage_id") or stage_id
            subtask = envelope.get("subtask") or subtask
            side_effect_refs = list(envelope.get("side_effect_refs") or ())
        timeline.append(
            {
                "event_id": event_id,
                "revision": event.revision,
                "created_at": event.created_at,
                "event_type": event.type,
                "canonical_type": canonical,
                "step": event.step,
                "attempt": event.attempt,
                "stage": stage_id,
                "subtask": subtask,
                "side_effect_refs": side_effect_refs,
                "reason": _reason(event),
                "ts": event.created_at,
                "type": canonical,
                "message": str(_reason(event).get("message") or event.type),
            }
        )
    return timeline


def project_runtime_diagnostics(
    events: Iterable[WorkflowEvent], state: WorkflowState
) -> dict[str, Any]:
    """Build the Native UI diagnostic contract from the event stream."""

    event_list = list(events)
    actions = project_action_center(event_list)
    recovery = project_recovery_status(event_list)
    latest_recovery = recovery["latest"] or {}
    pending = actions["pending"][-1] if actions["pending"] else None
    if pending is None and state.pending_action is not None:
        action = state.pending_action
        metadata = action.get("metadata") or {}
        request = metadata.get("human_decision") or {}
        pending = {
            "gate": action.get("gate"),
            "request_id": request.get("request_id"),
            "kind": request.get("kind") or (
                "consultation"
                if "consult" in str(action.get("type") or "").lower()
                else "selection"
            ),
            "action_type": action.get("type"),
            "requested_revision": request.get("requested_revision"),
            "reason": request.get("reason") or {
                "code": "pending_action",
                "message": "Human decision required",
                "evidence": [],
            },
        }
    reason_code = ""
    reason_summary = ""
    suggested = ["refresh_status"]
    evidence: list[dict[str, Any]] = []
    if pending is not None:
        gate = pending.get("gate")
        if gate in {"reviewer_entry_gate", "step8_5"}:
            reason_code = "AWAITING_STEP8_5"
            reason_summary = "Step 8.5 reviewer entry gate requires human review"
            suggested = ["open_entry_gate", "open_reviewer_entry_artifacts", "refresh_status"]
        elif pending.get("kind") == "consultation":
            reason_code = "CONSULTATION_PENDING"
            reason_summary = "A consultation answer is required"
            suggested = ["open_consultation_request", "open_human_review", "refresh_status"]
        else:
            reason_code = "HUMAN_DECISION_REQUIRED"
            reason_summary = str((pending.get("reason") or {}).get("message") or "Human decision required")
            suggested = ["open_human_review", "refresh_status"]
        evidence = [
            {"kind": "file", "path": path}
            for path in (pending.get("reason") or {}).get("evidence", [])
        ]
    elif latest_recovery and state.status in {
        WorkflowStatus.FAILED,
        WorkflowStatus.RETRYING,
        WorkflowStatus.INTERRUPTED,
    }:
        reason = latest_recovery.get("reason") or {}
        reason_code = str(reason.get("code") or latest_recovery["canonical_type"]).upper()
        reason_summary = str(reason.get("message") or "")
        evidence = [
            {"kind": "file", "path": path} for path in reason.get("evidence", [])
        ]
        if latest_recovery["canonical_type"] == "GATE_BLOCKED":
            suggested = ["open_gate_evidence", "open_audit_timeline", "refresh_status"]
    return {
        "status": {
            "version": 3,
            "state": state.status.value,
            "current_step": state.source_step_id or state.active_step or max(state.last_completed_step, 0),
            "current_stage": state.active_stage,
            "current_subtask": state.active_subtask,
            "current_action": (
                pending.get("action_type") if pending is not None else state.status.value
            ),
            "reason_code": reason_code,
            "reason_summary": reason_summary,
            "suggested_actions": suggested,
            "evidence": evidence,
            "workflow_revision": state.revision,
        },
        "events": project_audit_timeline(event_list)[-5:],
        "actions": [{"id": action_id} for action_id in suggested],
        "action_center": actions,
        "recovery": recovery,
    }
