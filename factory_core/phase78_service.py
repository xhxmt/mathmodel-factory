"""Strict public service boundary for the enabled Phase 7+8 shadow pipeline.

The module defines one closed JSON request shape shared by CLI and Web.  It
does not create a path, database, CAS, spool, thread, or heavy Phase-7/8 import
until an explicitly enabled :class:`Phase78Settings` reaches a public method.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import importlib
import json
from pathlib import PurePosixPath
import re
import time
from typing import Mapping

from .canonical import canonical_bytes
from .phase78_config import Phase78Settings
from .phase78_deadline import TotalDeadline


PHASE78_PIPELINE_REQUEST_SCHEMA = "phase78-pipeline-request-v1"
PHASE78_SERVICE_RESULT_SCHEMA = "phase78-shadow-service-result-v1"
_ROLES = ("math", "execution", "paper")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}\Z")
_MAX_PACKET_BYTES = 2 * 1024 * 1024


class Phase78ServiceError(RuntimeError):
    code = "PHASE78_SHADOW_UNAVAILABLE"


class Phase78ServiceDisabled(Phase78ServiceError):
    code = "PHASE78_SHADOW_DISABLED"


class Phase78RequestError(Phase78ServiceError):
    code = "PHASE78_REQUEST_INVALID"


class Phase78ProjectMismatch(Phase78ServiceError):
    code = "PHASE78_CURRENT_HEAD_MISMATCH"


def _require_enabled(settings: Phase78Settings) -> None:
    if not isinstance(settings, Phase78Settings) or settings.enabled is not True:
        raise Phase78ServiceDisabled("Phase 7+8 shadow pipeline is disabled")


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise Phase78RequestError(f"{field} must be a canonical identifier")
    return value


def _integer(value: object, field: str) -> int:
    if type(value) is not int or value < 0 or value > 2**63 - 1:
        raise Phase78RequestError(f"{field} must be a nonnegative integer")
    return value


def _exact(value: object, fields: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise Phase78RequestError(f"{label} must be an object")
    if set(value) != fields:
        raise Phase78RequestError(
            f"{label} fields must be exactly {sorted(fields)!r}"
        )
    try:
        result = json.loads(canonical_bytes(dict(value)).decode("utf-8"))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise Phase78RequestError(f"{label} is outside canonical JSON") from exc
    if type(result) is not dict:
        raise Phase78RequestError(f"{label} must be an exact JSON object")
    return result


def _optional_sha(value: object, field: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise Phase78RequestError(f"{field} must be null or lowercase SHA-256")
    return value


def _packet_map(value: object, label: str) -> dict[str, bytes | None]:
    wire = _exact(value, set(_ROLES), label)
    result: dict[str, bytes | None] = {}
    for role in _ROLES:
        encoded = wire[role]
        if encoded is None:
            result[role] = None
            continue
        if type(encoded) is not str or len(encoded) > (_MAX_PACKET_BYTES * 4 // 3 + 8):
            raise Phase78RequestError(f"{label}.{role} is not bounded base64")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise Phase78RequestError(f"{label}.{role} is not strict base64") from exc
        if len(raw) > _MAX_PACKET_BYTES or base64.b64encode(raw).decode("ascii") != encoded:
            raise Phase78RequestError(f"{label}.{role} is not canonical base64")
        result[role] = raw
    return result


@dataclass(frozen=True, slots=True)
class ParsedPhase78Request:
    raw: dict[str, object]
    idempotency_key: str
    workflow_id: str
    phase3_artifact_state: dict[str, object]
    phase3_artifact_occurrence: dict[str, object]
    phase6_access_proof: dict[str, object]
    role_outputs: dict[str, bytes | None]
    manifests: dict[str, bytes | None]
    contexts: dict[str, bytes | None]
    grounding: dict[str, object]
    reference: dict[str, object]
    approval: dict[str, object]
    work: dict[str, object]


def parse_phase78_request(payload: Mapping[str, object]) -> ParsedPhase78Request:
    """Deeply validate the only accepted pipeline request representation."""

    outer = _exact(
        payload,
        {
            "schema_version",
            "idempotency_key",
            "workflow_id",
            "phase3_artifact_state",
            "phase3_artifact_occurrence",
            "phase6_access_proof",
            "grounding",
            "reference",
            "approval",
            "work",
        },
        "Phase 7+8 request",
    )
    if outer["schema_version"] != PHASE78_PIPELINE_REQUEST_SCHEMA:
        raise Phase78RequestError("Phase 7+8 request schema differs")
    key = _identifier(outer["idempotency_key"], "idempotency_key")
    workflow = _identifier(outer["workflow_id"], "workflow_id")
    state = _exact(
        outer["phase3_artifact_state"],
        {"schema", "workflow_id", "through_revision", "occurrences", "state_sha256"},
        "phase3_artifact_state",
    )
    occurrence = _exact(
        outer["phase3_artifact_occurrence"],
        {
            "schema_version", "occurrence_id", "workflow_id", "revision",
            "command_id", "mutation_sha256", "kind", "normalized_path",
            "semantic_sha256", "artifact_record", "blocker", "removal",
        },
        "phase3_artifact_occurrence",
    )
    proof = dict(outer["phase6_access_proof"]) if isinstance(
        outer["phase6_access_proof"], Mapping
    ) else None
    if proof is None:
        raise Phase78RequestError("phase6_access_proof must be an object")
    grounding = _exact(
        outer["grounding"],
        {"role_output_base64", "manifest_base64", "context_base64"},
        "grounding",
    )
    role_outputs = _packet_map(grounding["role_output_base64"], "role_output_base64")
    manifests = _packet_map(grounding["manifest_base64"], "manifest_base64")
    contexts = _packet_map(grounding["context_base64"], "context_base64")
    reference = _exact(
        outer["reference"],
        {
            "reference_id", "logical_id", "pdf_path", "bibliographic_metadata",
            "external_share_classification", "expected_current_binding_sha256",
        },
        "reference",
    )
    _identifier(reference["reference_id"], "reference.reference_id")
    _identifier(reference["logical_id"], "reference.logical_id")
    pdf_path = reference["pdf_path"]
    if type(pdf_path) is not str:
        raise Phase78RequestError("reference.pdf_path must be a logical path")
    logical_path = PurePosixPath(pdf_path)
    if (
        logical_path.is_absolute()
        or not logical_path.parts
        or any(part in {"", ".", ".."} for part in logical_path.parts)
        or "\\" in pdf_path
        or logical_path.suffix.lower() != ".pdf"
        or logical_path.as_posix() != pdf_path
    ):
        raise Phase78RequestError("reference.pdf_path must be canonical relative PDF path")
    if not isinstance(reference["bibliographic_metadata"], Mapping):
        raise Phase78RequestError("reference.bibliographic_metadata must be an object")
    _identifier(
        reference["external_share_classification"],
        "reference.external_share_classification",
    )
    _optional_sha(
        reference["expected_current_binding_sha256"],
        "reference.expected_current_binding_sha256",
    )
    approval = _exact(
        outer["approval"],
        {
            "approval_id", "issuer_id", "issuer_generation", "subject_id",
            "subject_generation", "logical_issued_at", "not_before", "expires_at",
            "data_egress_request", "successor_of",
            "expected_predecessor_event_sha256", "trusted_preflight_sha256",
            "expected_previous_decision_sha256",
        },
        "approval",
    )
    for name in (
        "approval_id", "issuer_id", "issuer_generation", "subject_id",
        "subject_generation",
    ):
        _identifier(approval[name], f"approval.{name}")
    for name in ("logical_issued_at", "not_before", "expires_at"):
        _integer(approval[name], f"approval.{name}")
    if not (
        approval["logical_issued_at"]
        <= approval["not_before"]
        < approval["expires_at"]
    ):
        raise Phase78RequestError("approval logical time interval is invalid")
    if (
        _optional_sha(
            approval["trusted_preflight_sha256"],
            "approval.trusted_preflight_sha256",
        )
        is None
    ):
        raise Phase78RequestError(
            "approval.trusted_preflight_sha256 must be a lowercase SHA-256"
        )
    successor = approval["successor_of"]
    if successor is not None:
        _identifier(successor, "approval.successor_of")
    _optional_sha(
        approval["expected_predecessor_event_sha256"],
        "approval.expected_predecessor_event_sha256",
    )
    _optional_sha(
        approval["expected_previous_decision_sha256"],
        "approval.expected_previous_decision_sha256",
    )
    if not isinstance(approval["data_egress_request"], Mapping):
        raise Phase78RequestError("approval.data_egress_request must be an object")
    work = _exact(
        outer["work"],
        {"submitted_at", "claimed_at", "checkpointed_at", "completed_at"},
        "work",
    )
    timeline = tuple(
        _integer(work[name], f"work.{name}")
        for name in ("submitted_at", "claimed_at", "checkpointed_at", "completed_at")
    )
    if tuple(sorted(timeline)) != timeline or len(set(timeline)) != 4:
        raise Phase78RequestError("work logical times must be strictly increasing")
    if state.get("workflow_id") != workflow or occurrence.get("workflow_id") != workflow:
        raise Phase78RequestError("workflow identity differs across the request")
    return ParsedPhase78Request(
        outer, key, workflow, state, occurrence, proof,
        role_outputs, manifests, contexts, grounding, reference, approval, work,
    )


def _deadline(settings: Phase78Settings, supplied: TotalDeadline | None) -> TotalDeadline:
    return supplied if supplied is not None else TotalDeadline(settings.deadline_ms)


def _trusted_logical_time() -> int:
    """Return service-owned logical time; request JSON cannot override it."""

    return int(time.time())


def _verify_caller(
    settings: Phase78Settings,
    request: ParsedPhase78Request,
    *,
    project_id: str,
    actor_id: str,
    deadline: TotalDeadline,
):
    current_module = importlib.import_module("factory_core.phase78_current")
    verifier = current_module.Phase78CurrentHeadVerifier(settings)
    facts = verifier.verify(
        phase3_artifact_state=request.phase3_artifact_state,
        phase3_artifact_occurrence=request.phase3_artifact_occurrence,
        phase6_access_proof=request.phase6_access_proof,
        deadline=deadline,
    )
    if facts.coordinate.project_id != _identifier(project_id, "project_id"):
        raise Phase78ProjectMismatch("request belongs to another project")
    actor = _identifier(actor_id, "actor_id")
    if (
        facts.access_proof.grant.subject_id != actor
        or request.approval["subject_id"] != actor
        or request.approval["subject_generation"]
        != facts.access_proof.grant.subject_generation
    ):
        raise Phase78ProjectMismatch("request subject differs from authenticated actor")
    return verifier, facts


def _verify_historical_caller(
    request: ParsedPhase78Request,
    *,
    project_id: str,
    actor_id: str,
) -> None:
    """Authorize a history read without pretending the captured proof is current."""

    authority = importlib.import_module("factory_core.authority_read_repository")
    artifacts = importlib.import_module("factory_core.phase3_artifacts")
    grants = importlib.import_module("factory_core.phase6_snapshot_grants")
    try:
        state = authority.authority_phase3_artifact_state_from_dict(
            request.phase3_artifact_state
        )
        occurrence = artifacts.artifact_occurrence_from_dict(
            request.phase3_artifact_occurrence
        )
        proof = grants.verify_shadow_access_proof(request.phase6_access_proof)
    except Exception as exc:
        raise Phase78ProjectMismatch("stored source identity does not revalidate") from exc
    coordinate = proof.source_binding.authority_coordinate
    if (
        coordinate["project_id"] != _identifier(project_id, "project_id")
        or coordinate["workflow_id"] != request.workflow_id
        or state.workflow_id != request.workflow_id
        or occurrence.workflow_id != request.workflow_id
        or proof.grant.subject_id != _identifier(actor_id, "actor_id")
    ):
        raise Phase78ProjectMismatch("stored request belongs to another caller/project")


def _wire(value: object) -> dict[str, object]:
    as_dict = getattr(value, "as_dict", None)
    result = as_dict() if callable(as_dict) else value
    if type(result) is not dict:
        raise Phase78ServiceError("Phase 7+8 adapter returned a non-object")
    return json.loads(canonical_bytes(result).decode("utf-8"))


def submit_phase78_request(
    settings: Phase78Settings,
    project_id: str,
    actor_id: str,
    payload: Mapping[str, object],
    *,
    deadline: TotalDeadline | None = None,
) -> dict[str, object]:
    _require_enabled(settings)
    request = parse_phase78_request(payload)
    total = _deadline(settings, deadline)
    _verify_caller(
        settings, request, project_id=project_id, actor_id=actor_id, deadline=total
    )
    scheduler_module = importlib.import_module("factory_core.phase78_scheduler")
    scheduled = scheduler_module.Phase78ShadowScheduler(
        settings, deadline=total
    ).submit(
        idempotency_key=request.idempotency_key,
        workflow_id=request.workflow_id,
        payload=request.raw,
        occurred_at=request.work["submitted_at"],
        deadline=total,
    )
    return {
        "schema_version": PHASE78_SERVICE_RESULT_SCHEMA,
        "project_id": project_id,
        "idempotency_key": request.idempotency_key,
        "outcome": "pending",
        "work": scheduled.view.as_dict(),
        "replayed": scheduled.replayed,
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
        "provider_call_performed": False,
        "outbox_dispatch_performed": False,
    }


def run_phase78_worker_once(
    settings: Phase78Settings,
    project_id: str,
    actor_id: str,
    payload: Mapping[str, object],
    *,
    deadline: TotalDeadline | None = None,
) -> dict[str, object]:
    _require_enabled(settings)
    request = parse_phase78_request(payload)
    total = _deadline(settings, deadline)
    verifier, _facts = _verify_caller(
        settings, request, project_id=project_id, actor_id=actor_id, deadline=total
    )
    scheduler_module = importlib.import_module("factory_core.phase78_scheduler")
    scheduler = scheduler_module.Phase78ShadowScheduler(settings, deadline=total)
    scheduler.submit(
        idempotency_key=request.idempotency_key,
        workflow_id=request.workflow_id,
        payload=request.raw,
        occurred_at=request.work["submitted_at"],
        deadline=total,
    )
    worker_module = importlib.import_module("factory_core.phase78_worker")
    result = worker_module.run_local_phase78_worker(
        settings=settings,
        scheduler=scheduler,
        request=request,
        project_id=project_id,
        actor_id=actor_id,
        current_verifier=verifier,
        deadline=total,
        trusted_evaluated_at=_trusted_logical_time(),
    )
    return _wire(result)


def load_phase78_status(
    settings: Phase78Settings,
    project_id: str,
    actor_id: str,
    idempotency_key: str,
    *,
    deadline: TotalDeadline | None = None,
) -> dict[str, object]:
    _require_enabled(settings)
    key = _identifier(idempotency_key, "idempotency_key")
    total = _deadline(settings, deadline)
    scheduler_module = importlib.import_module("factory_core.phase78_scheduler")
    scheduler = scheduler_module.Phase78ShadowScheduler(settings, deadline=total)
    view = scheduler.load(key, deadline=total)
    request = parse_phase78_request(view.job.payload)
    _verify_historical_caller(request, project_id=project_id, actor_id=actor_id)
    current_module = importlib.import_module("factory_core.phase78_current")
    verifier = current_module.Phase78CurrentHeadVerifier(settings)
    worker_module = importlib.import_module("factory_core.phase78_worker")
    result = worker_module.load_local_phase78_status(
        settings=settings,
        view=view,
        request=request,
        project_id=project_id,
        current_verifier=verifier,
        deadline=total,
        trusted_evaluated_at=_trusted_logical_time(),
    )
    return _wire(result)


def cancel_phase78_request(
    settings: Phase78Settings,
    project_id: str,
    actor_id: str,
    payload: Mapping[str, object],
    *,
    deadline: TotalDeadline | None = None,
) -> dict[str, object]:
    """Cancel local shadow work; callers never receive or supply a lease token."""

    _require_enabled(settings)
    request = _exact(
        payload,
        {
            "schema_version",
            "idempotency_key",
            "cancelled_at",
            "reason",
        },
        "work cancellation",
    )
    if request["schema_version"] != "phase78-work-cancel-request-v1":
        raise Phase78RequestError("work cancellation schema differs")
    key = _identifier(request["idempotency_key"], "idempotency_key")
    cancelled_at = _integer(request["cancelled_at"], "cancelled_at")
    if request["reason"] != "user_cancel":
        raise Phase78RequestError("public cancellation reason must be user_cancel")
    total = _deadline(settings, deadline)
    scheduler_module = importlib.import_module("factory_core.phase78_scheduler")
    scheduler = scheduler_module.Phase78ShadowScheduler(settings, deadline=total)
    view = scheduler.load(key, deadline=total)
    stored = parse_phase78_request(view.job.payload)
    _verify_historical_caller(stored, project_id=project_id, actor_id=actor_id)
    deadline_module = importlib.import_module("factory_core.phase78_deadline")
    result = scheduler.cancel(
        idempotency_key=key,
        occurred_at=cancelled_at,
        reason=deadline_module.Phase78CancellationReason.USER_CANCEL,
        deadline=total,
    )
    return {
        "schema_version": "phase78-shadow-cancel-result-v1",
        "project_id": project_id,
        "idempotency_key": key,
        "cancellation_reason": result.cancellation_reason,
        "work": result.view.as_dict(),
        "replayed": result.replayed,
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
        "provider_call_performed": False,
        "outbox_dispatch_performed": False,
    }


def revoke_phase78_approval(
    settings: Phase78Settings,
    project_id: str,
    actor_id: str,
    approval_id: str,
    payload: Mapping[str, object],
    *,
    deadline: TotalDeadline | None = None,
) -> dict[str, object]:
    _require_enabled(settings)
    request = _exact(
        payload,
        {
            "schema_version", "idempotency_key", "expected_event_sha256",
            "revoked_at", "reason_code",
        },
        "approval revocation",
    )
    if request["schema_version"] != "phase78-approval-revoke-request-v1":
        raise Phase78RequestError("approval revocation schema differs")
    key = _identifier(request["idempotency_key"], "idempotency_key")
    approval_identity = _identifier(approval_id, "approval_id")
    expected = _optional_sha(request["expected_event_sha256"], "expected_event_sha256")
    if expected is None:
        raise Phase78RequestError("expected_event_sha256 is required")
    revoked_at = _integer(request["revoked_at"], "revoked_at")
    reason = _identifier(request["reason_code"], "reason_code")
    total = _deadline(settings, deadline)
    worker_module = importlib.import_module("factory_core.phase78_worker")
    result = worker_module.revoke_local_phase78_approval(
        settings=settings,
        project_id=_identifier(project_id, "project_id"),
        actor_id=_identifier(actor_id, "actor_id"),
        idempotency_key=key,
        approval_id=approval_identity,
        expected_event_sha256=expected,
        revoked_at=revoked_at,
        reason_code=reason,
        deadline=total,
    )
    return _wire(result)


__all__ = [
    "PHASE78_PIPELINE_REQUEST_SCHEMA",
    "PHASE78_SERVICE_RESULT_SCHEMA",
    "ParsedPhase78Request",
    "Phase78ProjectMismatch",
    "Phase78RequestError",
    "Phase78ServiceDisabled",
    "Phase78ServiceError",
    "cancel_phase78_request",
    "load_phase78_status",
    "parse_phase78_request",
    "revoke_phase78_approval",
    "run_phase78_worker_once",
    "submit_phase78_request",
]
