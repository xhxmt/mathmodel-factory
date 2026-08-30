"""M0.3 Project Snapshot V0: one narrow, read-only SQLite transaction.

The builder intentionally does not reuse ``SQLiteStateStore``. It observes one
database coordinate through one ``BEGIN`` transaction, exposes typed gaps
instead of invented empty collections, and never follows artifact references.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from urllib.parse import quote

from .canonical import CanonicalizationError, canonical_bytes, canonical_sha256
from .contract_pins import CONTRACT_PIN_SET_SCHEMA, ContractPinSetV1
from .dirty import DirtyFlag
from .domain import SCHEMA_VERSION, WorkflowEvent, WorkflowStatus
from .persisted_dirty_owner_policy import compile_persisted_dirty_owner_policy
from .stages import (
    GATE_POLICIES,
    STAGE_CATALOG_VERSION,
    STAGE_CONTRACTS,
    STAGE_SCHEDULER_GENERATION,
    STEP_SCHEDULER_GENERATION,
    completed_stage_for_step,
)
from .workflow_events import (
    ENVELOPE_KEY,
    EVENT_VERSION,
    REPLAY_STATE_VERSION,
    ReplayIntegrityError,
    canonical_event_type,
    canonical_hash as workflow_canonical_hash,
    replay_events,
    replay_state,
)


PROJECT_SNAPSHOT_V0_SCHEMA = "project-snapshot-v0-source-authorized-v3"
SNAPSHOT_BUILD_RESULT_SCHEMA = "project-snapshot-build-result-v0-source-authorized-v3"
SNAPSHOT_COORDINATE_SCHEMA = "snapshot-coordinate-v0"
SNAPSHOT_POLICY_SCHEMA = "snapshot-policy-v3"
SNAPSHOT_EVENT_ROW_POLICY_SCHEMA = "snapshot-event-row-policy-v1"
SNAPSHOT_EVENT_HEAD_CONTRACT_SCHEMA = "snapshot-event-head-contract-v3"
SNAPSHOT_IMMUTABLE_REF_CONTRACT_SCHEMA = "snapshot-immutable-ref-contract-v3"
SNAPSHOT_SOLVER_RECEIPT_CONTRACT_SCHEMA = "snapshot-solver-receipt-contract-v2"
EVENT_HEAD_FACT_TYPE = "events-source-authorized-v3"
SOLVER_RECEIPT_FACT_TYPE = "solver-receipt-source-authorized-v2"
SNAPSHOT_HISTORY_PAGE_LIMIT = 256
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class SnapshotV0ValidationError(ValueError):
    """Raised for malformed Snapshot V0 runtime values."""


class SnapshotAvailabilityV0(str, Enum):
    AVAILABLE = "AVAILABLE"
    ERROR = "ERROR"
    UNAVAILABLE_LEGACY_UNBOUND = "UNAVAILABLE_LEGACY_UNBOUND"
    REDACTED = "REDACTED"
    PAGED = "PAGED"


class SnapshotCompletenessV0(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


class SnapshotSectionIdV0(str, Enum):
    PROJECT_STATE_SCHEMA = "project_state_schema"
    EVENT_HEAD_CHAIN = "event_head_chain"
    PROJECTOR_STATE = "projector_state"
    STAGE_CURSOR_CHECKPOINTS = "stage_cursor_checkpoints"
    DIRTY_FACTS = "dirty_facts_causes_clear_rebase"
    PENDING_HUMAN = "pending_human_action_request"
    INVOCATIONS_SOLVER = "active_invocation_solver_summaries"
    TERMINAL_STATUS = "terminal_status"
    DELIVERY_AUTHORIZATION = "delivery_authorization"
    IMMUTABLE_REFS = "recorded_immutable_artifact_log_refs"
    CONFIG_POLICY = "config_policy"
    GENERATION_BINDING = "project_run_generation_binding"
    CONTRACT_PINS = "recorded_contract_pins"


class SnapshotErrorCodeV0(str, Enum):
    DB_NOT_FOUND_LEGACY_FILESYSTEM_ONLY = "DB_NOT_FOUND_LEGACY_FILESYSTEM_ONLY"
    DB_PATH_NOT_REGULAR = "DB_PATH_NOT_REGULAR"
    DB_PATH_SYMLINK = "DB_PATH_SYMLINK"
    DB_OPEN_FAILED = "DB_OPEN_FAILED"
    SQLITE_WAL_AUXILIARY_UNAVAILABLE = "SQLITE_WAL_AUXILIARY_UNAVAILABLE"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    PROJECT_STATE_INVALID = "PROJECT_STATE_INVALID"
    PROJECT_ID_MISMATCH = "PROJECT_ID_MISMATCH"
    COORDINATE_CHANGED = "COORDINATE_CHANGED"
    EVENT_CHAIN_INVALID = "EVENT_CHAIN_INVALID"
    REQUIRED_JSON_INVALID = "REQUIRED_JSON_INVALID"
    REQUIRED_HASH_INVALID = "REQUIRED_HASH_INVALID"
    FUTURE_ROW_REVISION = "FUTURE_ROW_REVISION"
    REQUIRED_DUPLICATE = "REQUIRED_DUPLICATE"
    SECTION_READ_FAILED = "SECTION_READ_FAILED"
    LEGACY_PROJECT_GENERATION_UNBOUND = "LEGACY_PROJECT_GENERATION_UNBOUND"
    LEGACY_RUN_GENERATION_UNBOUND = "LEGACY_RUN_GENERATION_UNBOUND"
    LEGACY_CONTRACT_PINS_UNBOUND = "LEGACY_CONTRACT_PINS_UNBOUND"
    LEGACY_DELIVERY_AUTHORIZATION_UNBOUND = "LEGACY_DELIVERY_AUTHORIZATION_UNBOUND"
    OPTIONAL_TABLE_UNAVAILABLE = "OPTIONAL_TABLE_UNAVAILABLE"
    PROJECT_STATE_CONTRACT_INVALID = "PROJECT_STATE_CONTRACT_INVALID"
    STAGE_CHECKPOINT_CONTRACT_INVALID = "STAGE_CHECKPOINT_CONTRACT_INVALID"
    DIRTY_FACT_CONTRACT_INVALID = "DIRTY_FACT_CONTRACT_INVALID"
    PENDING_REQUEST_CONTRACT_INVALID = "PENDING_REQUEST_CONTRACT_INVALID"
    PROJECTOR_CONTRACT_INVALID = "PROJECTOR_CONTRACT_INVALID"
    SOLVER_FACT_CONTRACT_INVALID = "SOLVER_FACT_CONTRACT_INVALID"
    IMMUTABLE_REF_CONTRACT_INVALID = "IMMUTABLE_REF_CONTRACT_INVALID"


class EventStepBindingModeV1(str, Enum):
    STEP_NONE = "STEP_NONE"
    STEP_SOURCE_CATALOG = "STEP_SOURCE_CATALOG"
    STEP_PAYLOAD_SOURCE = "STEP_PAYLOAD_SOURCE"
    STEP_PAYLOAD_STAGE_SUBTASK = "STEP_PAYLOAD_STAGE_SUBTASK"
    STEP_SUBJECT_SOURCE = "STEP_SUBJECT_SOURCE"
    STEP_RESULT_SOURCE = "STEP_RESULT_SOURCE"


class EventAttemptBindingModeV1(str, Enum):
    ATTEMPT_ZERO = "ATTEMPT_ZERO"
    ATTEMPT_SUBJECT = "ATTEMPT_SUBJECT"
    ATTEMPT_RESULT = "ATTEMPT_RESULT"


@dataclass(frozen=True)
class EventRowPolicyV1:
    event_type: str
    canonical_type: str
    payload_family: str
    required_payload_fields: tuple[str, ...]
    forbidden_payload_fields: tuple[str, ...]
    step_binding_mode: EventStepBindingModeV1
    attempt_binding_mode: EventAttemptBindingModeV1
    payload_source_step_field: str | None
    payload_attempt_field: str | None
    source_catalog_steps: tuple[int, ...]


@dataclass(frozen=True)
class SnapshotCoordinateV0:
    schema_version: str
    project_id: str
    workflow_schema_version: int
    project_revision: int
    project_generation: str | None
    run_generation: str | None
    runtime_generation: str
    scheduler_generation: str
    recorded_contract_pin_set_sha256: str | None


@dataclass(frozen=True)
class SnapshotFactV0:
    fact_type: str
    fact_key: str
    value_sha256: str
    entity_type: str | None
    entity_id: str | None
    entity_generation: int | None
    subject_type: str | None
    subject_id: str | None
    subject_sha256: str | None
    legacy_classifier_contract_sha256: str | None


@dataclass(frozen=True)
class SnapshotSectionV0:
    section_id: SnapshotSectionIdV0
    availability: SnapshotAvailabilityV0
    coordinate: SnapshotCoordinateV0
    facts: tuple[SnapshotFactV0, ...]
    error_code: SnapshotErrorCodeV0 | None
    gap_id: str | None
    policy_id: str | None
    page_cursor: str | None


@dataclass(frozen=True)
class ProjectSnapshotV0:
    schema_version: str
    coordinate: SnapshotCoordinateV0
    completeness: SnapshotCompletenessV0
    sections: tuple[SnapshotSectionV0, ...]
    contract_pins: ContractPinSetV1 | None
    authoritative: bool
    performed_workflow_side_effects: tuple[str, ...]
    application_initiated_write_operations: tuple[str, ...]


@dataclass(frozen=True)
class SnapshotPolicyV0:
    """Source-authorized domain values accepted from the legacy SQLite schema."""

    schema_version: str
    workflow_statuses: tuple[str, ...]
    runtime_generations: tuple[str, ...]
    scheduler_generations: tuple[str, ...]
    stage_catalog_version: str
    stage_subtasks: tuple[tuple[int, str, int, int | None, str], ...]
    dirty_flags: tuple[str, ...]
    gate_types: tuple[str, ...]
    decision_kinds: tuple[str, ...]
    checkpoint_receipt_schema: str
    dirty_clear_receipt_schema: str
    dirty_rebase_receipt_schema: str
    projector_versions: tuple[tuple[str, int], ...]
    solver_lifecycle_events: tuple[tuple[str, str], ...]
    solver_receipt_events: tuple[tuple[str, str], ...]
    solver_initial_statuses: tuple[str, ...]
    event_row_policy_schema: str
    event_row_policies: tuple[EventRowPolicyV1, ...]
    event_head_contract_schema: str
    immutable_ref_contract_schema: str
    solver_receipt_contract_schema: str
    supported_event_versions: tuple[int, ...]
    replay_state_version: int
    event_envelope_fields: tuple[str, ...]
    domain_effect_tables: tuple[tuple[str, str, str], ...]
    solver_job_id_pattern: str
    event_head_fact_type: str
    solver_receipt_fact_type: str

    @property
    def solver_statuses(self) -> tuple[str, ...]:
        return tuple(status for _event_type, status in self.solver_lifecycle_events)


def _compile_event_row_policies_v1() -> tuple[EventRowPolicyV1, ...]:
    """Compile the closed, source-owned event-row vocabulary and bindings."""

    def policy(
        event_type: str,
        payload_family: str,
        step_binding_mode: EventStepBindingModeV1,
        attempt_binding_mode: EventAttemptBindingModeV1,
        *,
        required: tuple[str, ...] = (),
        forbidden: tuple[str, ...] = (),
        source_step_field: str | None = None,
        payload_attempt_field: str | None = None,
        source_catalog_steps: tuple[int, ...] = (),
    ) -> EventRowPolicyV1:
        return EventRowPolicyV1(
            event_type=event_type,
            canonical_type=canonical_event_type(event_type),
            payload_family=payload_family,
            required_payload_fields=required,
            forbidden_payload_fields=forbidden,
            step_binding_mode=step_binding_mode,
            attempt_binding_mode=attempt_binding_mode,
            payload_source_step_field=source_step_field,
            payload_attempt_field=payload_attempt_field,
            source_catalog_steps=source_catalog_steps,
        )

    none_zero = (
        policy(
            event_type,
            family,
            EventStepBindingModeV1.STEP_NONE,
            EventAttemptBindingModeV1.ATTEMPT_ZERO,
            required=required,
        )
        for event_type, family, required in (
            ("PROJECT_CREATED", "project-created-v1", ()),
            ("PROJECT_IMPORTED", "project-imported-v1", ()),
            (
                "SOLVER_POLICY_CONFIGURED",
                "solver-policy-configured-v1",
                ("mode", "threshold_seconds", "allowed_runtimes"),
            ),
        )
    )
    lifecycle = (
        policy(
            event_type,
            f"solver-lifecycle-{status}-v1",
            EventStepBindingModeV1.STEP_NONE,
            EventAttemptBindingModeV1.ATTEMPT_ZERO,
            required=(
                "job_id",
                "backend",
                "runtime",
                "max_time_seconds",
                "idempotency_key",
                "owner_stage",
                "owner_subtask",
                "owner_revision",
                "attempt_id",
            )
            if event_type == "SOLVER_JOB_SUBMITTED"
            else ("job_id", "job_revision", "external_id", "failure"),
        )
        for event_type, status in (
            ("SOLVER_JOB_SUBMITTED", "submitted"),
            ("SOLVER_JOB_SUBMITTING", "submitting"),
            ("SOLVER_JOB_QUEUED", "queued"),
            ("SOLVER_JOB_RUNNING", "running"),
            ("SOLVER_JOB_CANCELLING", "cancelling"),
            ("SOLVER_JOB_COMPLETED", "completed"),
            ("SOLVER_JOB_FAILED", "failed"),
            ("SOLVER_JOB_TIMEOUT", "timeout"),
            ("SOLVER_JOB_CANCELLED", "cancelled"),
        )
    )
    receipts = (
        policy(
            event_type,
            f"solver-receipt-{stage}-v1",
            EventStepBindingModeV1.STEP_NONE,
            EventAttemptBindingModeV1.ATTEMPT_ZERO,
            required=(
                "job_id",
                "stage",
                "receipt_path",
                "receipt_sha256",
                "content_sha256",
                "request_sha256",
            ),
        )
        for event_type, stage in (
            ("SOLVER_JOB_RECEIPT_SUBMITTED", "submitted"),
            ("SOLVER_JOB_RECEIPT_COMPLETED", "completed"),
        )
    )
    simple_result = (
        policy(
            event_type,
            family,
            EventStepBindingModeV1.STEP_RESULT_SOURCE,
            EventAttemptBindingModeV1.ATTEMPT_RESULT,
            required=required,
        )
        for event_type, family, required in (
            ("WORKER_LAUNCHED", "worker-launched-v1", ("worker_pid", "log")),
            ("RUNNER_INTERRUPTED", "runner-interrupted-v1", ("reason",)),
            ("RUN_STARTED", "run-started-v1", ("lease_id",)),
            ("RUN_STOPPED", "run-stopped-v1", ("reason",)),
            ("STEP_STARTED", "step-started-v1", ("step_name",)),
            ("RETRY_SCHEDULED", "retry-scheduled-v1", ("error_class", "reason", "delay_seconds")),
            ("CONTEST_DEADLINE_EXHAUSTED", "contest-deadline-exhausted-v1", ("error_class", "reason")),
            ("PROJECT_COMPLETED", "project-completed-v1", ()),
            ("PAUSED", "project-paused-v1", ()),
            ("RESUMED", "project-resumed-v1", ()),
            ("KILLED", "project-killed-v1", ()),
            ("ENGINE_DEACTIVATED", "engine-deactivated-v1", ()),
            ("PROJECT_ARCHIVE_REQUESTED", "project-archive-requested-v1", ("destination",)),
            ("PROJECT_ARCHIVED", "project-archived-v1", ()),
            (
                "STAGE_SCHEDULER_ACTIVATED",
                "stage-scheduler-activated-v1",
                ("from_scheduler_generation", "to_scheduler_generation", "stage_catalog_version", "seeded_checkpoints"),
            ),
            (
                "STAGE_SCHEDULER_ROLLED_BACK",
                "stage-scheduler-rolled-back-v1",
                ("from_scheduler_generation", "to_scheduler_generation", "compatibility_step"),
            ),
        )
    )
    subject_events = (
        policy(
            event_type,
            family,
            EventStepBindingModeV1.STEP_SUBJECT_SOURCE,
            EventAttemptBindingModeV1.ATTEMPT_SUBJECT,
            required=required,
        )
        for event_type, family, required in (
            ("AWAITING_ACTION", "human-action-requested-v1", ("action", "pending_action")),
            ("STEP_PREPARE_AWAITING_ACTION", "step-prepare-human-action-v1", ("action", "pending_action")),
            ("DECISION_REQUEST_BUILD_FAILED", "decision-request-build-failed-v1", ("error_class", "exception_type")),
            ("ACTION_PROJECTION_FAILED", "action-projection-failed-v1", ("error_type",)),
            (
                "HUMAN_DECISION_REQUEST_SUPERSEDED",
                "human-decision-request-superseded-v1",
                ("gate", "superseded_request_id", "request_id", "generation", "action", "reason"),
            ),
            (
                "HUMAN_DECISION_RECORDED",
                "human-decision-recorded-v1",
                ("gate", "request_id", "decision_id", "generation", "resolution"),
            ),
            (
                "ACTION_RESOLVED",
                "human-action-resolved-v1",
                ("action_type", "gate", "request_id", "decision_id", "generation", "resolution"),
            ),
            (
                "WORK_REOPENED",
                "human-work-reopened-v1",
                ("action_type", "gate", "request_id", "decision_id", "generation", "resolution"),
            ),
        )
    )
    payload_source = (
        policy(
            event_type,
            family,
            EventStepBindingModeV1.STEP_PAYLOAD_SOURCE,
            EventAttemptBindingModeV1.ATTEMPT_RESULT,
            required=required,
            source_step_field=source_field,
            payload_attempt_field=payload_attempt_field,
        )
        for event_type, family, required, source_field, payload_attempt_field in (
            (
                "PROMPT_INPUT_BOUND",
                "prompt-input-bound-v1",
                ("schema_version", "receipt_id", "attempt_key", "source_step_id", "attempt"),
                "source_step_id",
                "attempt",
            ),
            (
                "STAGE_SUBTASK_SELECTED",
                "stage-subtask-selected-v1",
                ("stage", "subtask", "source_step", "stage_catalog_version"),
                "source_step",
                None,
            ),
            ("STEP_REOPENED", "step-reopened-v1", ("source_step",), "source_step", None),
            (
                "STAGE_SEMANTIC_REOPENED",
                "stage-semantic-reopened-v1",
                ("stage", "subtask", "source_step", "resume_after_step"),
                "source_step",
                None,
            ),
            (
                "RECOVERY_DECIDED",
                "recovery-decided-v1",
                ("decision", "source", "source_step"),
                "source_step",
                None,
            ),
        )
    )
    variants = (
        policy(
            "STEP_SUCCEEDED",
            "stage-step-succeeded-v1",
            EventStepBindingModeV1.STEP_PAYLOAD_SOURCE,
            EventAttemptBindingModeV1.ATTEMPT_RESULT,
            required=("stage", "subtask", "source_step", "evidence"),
            source_step_field="source_step",
        ),
        policy(
            "STEP_SUCCEEDED",
            "step-succeeded-v1",
            EventStepBindingModeV1.STEP_RESULT_SOURCE,
            EventAttemptBindingModeV1.ATTEMPT_RESULT,
            required=("evidence",),
            forbidden=("stage", "subtask", "source_step"),
        ),
        policy(
            "STEP_FAILED",
            "sourced-step-failed-v1",
            EventStepBindingModeV1.STEP_PAYLOAD_SOURCE,
            EventAttemptBindingModeV1.ATTEMPT_RESULT,
            required=("error_class", "source_step"),
            source_step_field="source_step",
        ),
        policy(
            "STEP_FAILED",
            "step-failed-v1",
            EventStepBindingModeV1.STEP_RESULT_SOURCE,
            EventAttemptBindingModeV1.ATTEMPT_RESULT,
            required=("error_class",),
            forbidden=("source_step",),
        ),
        policy(
            "STAGE_CHECKPOINT_INVALIDATED",
            "stage-checkpoint-invalidated-v1",
            EventStepBindingModeV1.STEP_PAYLOAD_STAGE_SUBTASK,
            EventAttemptBindingModeV1.ATTEMPT_RESULT,
            required=("stage", "subtask", "reason"),
        ),
        policy(
            "FINAL_SNAPSHOT_CREATED",
            "final-snapshot-created-v1",
            EventStepBindingModeV1.STEP_SOURCE_CATALOG,
            EventAttemptBindingModeV1.ATTEMPT_RESULT,
            required=("schema_version", "input_fingerprint", "manifest", "source_step"),
            source_catalog_steps=(16,),
        ),
        policy(
            "FINALIZATION_ABORTED_SNAPSHOT_CHANGED",
            "finalization-aborted-snapshot-changed-v1",
            EventStepBindingModeV1.STEP_SOURCE_CATALOG,
            EventAttemptBindingModeV1.ATTEMPT_RESULT,
            required=("schema_version", "input_fingerprint", "resume_after_step"),
            source_catalog_steps=(16,),
        ),
        policy(
            "DIRTY_CLASSIFIER_REBASED",
            "dirty-classifier-rebased-v1",
            EventStepBindingModeV1.STEP_NONE,
            EventAttemptBindingModeV1.ATTEMPT_RESULT,
            required=("schema_version", "rebase_id", "obligation_count"),
        ),
    )
    values = tuple((*none_zero, *lifecycle, *receipts, *simple_result, *subject_events, *payload_source, *variants))
    return tuple(
        sorted(
            values,
            key=lambda value: (value.event_type, value.payload_family),
        )
    )


@dataclass(frozen=True)
class SnapshotBuildResult:
    schema_version: str
    availability: SnapshotAvailabilityV0
    snapshot: ProjectSnapshotV0 | None
    error_code: SnapshotErrorCodeV0 | None
    error_context: str | None
    analysis_sql_trace: tuple[str, ...]
    authoritative: bool
    performed_workflow_side_effects: tuple[str, ...]
    application_initiated_write_operations: tuple[str, ...]


_EXPECTED_SECTION_ORDER = tuple(SnapshotSectionIdV0)
_JSON_COLUMNS = {
    "pending_action_json",
    "payload_json",
    "solver_runtimes_json",
    "argv_json",
    "result_refs_json",
    "failure_json",
    "decision_json",
    "request_json",
    "snapshot_json",
    "baseline_json",
    "receipt_json",
}
_REVISION_COLUMNS = {
    "revision",
    "updated_revision",
    "requested_revision",
    "selected_revision",
    "completed_revision",
    "cause_revision",
    "owner_revision",
    "through_revision",
}
_HASH_COLUMNS = {
    "request_sha256",
    "subject_fingerprint",
    "options_fingerprint",
    "evidence_manifest_sha256",
    "state_hash",
    "through_event_payload_sha256",
    "source_chain_root_sha256",
    "input_fingerprint",
    "output_fingerprint",
    "baseline_fingerprint",
    "current_fingerprint",
    "classifier_contract_sha256",
    "cleared_fingerprint",
    "new_classifier_sha256",
}


def compile_snapshot_policy_v0() -> SnapshotPolicyV0:
    """Rebuild the accepted SQLite-domain projection from frozen source contracts."""

    return SnapshotPolicyV0(
        schema_version=SNAPSHOT_POLICY_SCHEMA,
        workflow_statuses=tuple(member.value for member in WorkflowStatus),
        runtime_generations=("legacy_adapter", "native_v2"),
        scheduler_generations=(STAGE_SCHEDULER_GENERATION, STEP_SCHEDULER_GENERATION),
        stage_catalog_version=STAGE_CATALOG_VERSION,
        stage_subtasks=tuple(
            (
                stage.id,
                subtask.key,
                subtask.source_step_id,
                subtask.checkpoint_step_id,
                subtask.kind,
            )
            for stage in STAGE_CONTRACTS
            for subtask in stage.subtasks
        ),
        dirty_flags=tuple(member.value for member in DirtyFlag),
        gate_types=tuple(policy.gate for policy in GATE_POLICIES),
        decision_kinds=("selection", "approval", "consultation"),
        checkpoint_receipt_schema="factory-stage-checkpoint-v1",
        dirty_clear_receipt_schema="factory-dirty-clear-receipt-v1",
        dirty_rebase_receipt_schema="factory-dirty-classifier-rebase-v1",
        projector_versions=(("action-center", 1), ("workflow", 1)),
        solver_lifecycle_events=(
            ("SOLVER_JOB_SUBMITTED", "submitted"),
            ("SOLVER_JOB_SUBMITTING", "submitting"),
            ("SOLVER_JOB_QUEUED", "queued"),
            ("SOLVER_JOB_RUNNING", "running"),
            ("SOLVER_JOB_CANCELLING", "cancelling"),
            ("SOLVER_JOB_COMPLETED", "completed"),
            ("SOLVER_JOB_FAILED", "failed"),
            ("SOLVER_JOB_TIMEOUT", "timeout"),
            ("SOLVER_JOB_CANCELLED", "cancelled"),
        ),
        solver_receipt_events=(
            ("SOLVER_JOB_RECEIPT_SUBMITTED", "submitted"),
            ("SOLVER_JOB_RECEIPT_COMPLETED", "completed"),
        ),
        solver_initial_statuses=("submitting", "submitted"),
        event_row_policy_schema=SNAPSHOT_EVENT_ROW_POLICY_SCHEMA,
        event_row_policies=_compile_event_row_policies_v1(),
        event_head_contract_schema=SNAPSHOT_EVENT_HEAD_CONTRACT_SCHEMA,
        immutable_ref_contract_schema=SNAPSHOT_IMMUTABLE_REF_CONTRACT_SCHEMA,
        solver_receipt_contract_schema=SNAPSHOT_SOLVER_RECEIPT_CONTRACT_SCHEMA,
        supported_event_versions=(EVENT_VERSION,),
        replay_state_version=REPLAY_STATE_VERSION,
        event_envelope_fields=(
            "event_version",
            "event_id",
            "canonical_type",
            "replay_state_version",
            "state_patch_mode",
            "state_patch",
            "state_hash_before",
            "state_hash_after",
            "scheduler_generation",
            "coordinate_authority",
            "subject_stage_id",
            "subject_subtask",
            "subject_source_step_id",
            "result_stage_id",
            "result_subtask",
            "result_source_step_id",
            "stage_id",
            "subtask",
            "source_step_id",
            "request_id",
            "decision_id",
            "artifact_manifest_sha256",
            "effect_hashes_after",
            "aggregate_root_hash_after",
            "reason",
            "side_effect_refs",
        ),
        domain_effect_tables=(
            ("contest_policy", "contest_policy", "singleton"),
            ("project_config", "project_config", "singleton"),
            (
                "decision_requests",
                "workflow_decision_requests",
                "gate_type, generation",
            ),
            (
                "decision_instances",
                "workflow_decision_instances",
                "request_id",
            ),
            ("dirty_flags", "dirty_flags", "flag, owner_stage"),
            ("dirty_causes", "dirty_causes", "cause_revision, cause_id"),
            (
                "dirty_classifier_rebases",
                "dirty_classifier_rebases",
                "created_at, rebase_id",
            ),
            (
                "prompt_attempt_inputs",
                "prompt_attempt_inputs",
                "bound_revision, attempt_key",
            ),
            ("stage_checkpoints", "stage_checkpoints", "stage_id, subtask"),
            (
                "checkpoint_history",
                "stage_checkpoint_history",
                "completed_revision, checkpoint_id",
            ),
            ("solver_jobs", "solver_jobs", "job_id"),
            (
                "dirty_clear_receipts",
                "dirty_flag_clear_receipts",
                "revision, flag, owner_stage",
            ),
        ),
        solver_job_id_pattern=r"[A-Za-z0-9][A-Za-z0-9_:-]*\Z",
        event_head_fact_type=EVENT_HEAD_FACT_TYPE,
        solver_receipt_fact_type=SOLVER_RECEIPT_FACT_TYPE,
    )


def validate_snapshot_policy_v0(value: SnapshotPolicyV0) -> SnapshotPolicyV0:
    if type(value) is not SnapshotPolicyV0:
        raise SnapshotV0ValidationError("snapshot policy has an unsupported runtime type")
    for item in fields(SnapshotPolicyV0):
        _field(value, item.name, "snapshot_policy")
    if value != compile_snapshot_policy_v0():
        raise SnapshotV0ValidationError(
            "snapshot policy differs from the source-authorized projection"
        )
    return value


_SOURCE_POLICY = compile_snapshot_policy_v0()
_STAGE_SUBTASKS = {
    (stage_id, subtask): (source_step_id, completed_step_id, kind)
    for stage_id, subtask, source_step_id, completed_step_id, kind in _SOURCE_POLICY.stage_subtasks
}
_STAGE_IDS = frozenset(stage.id for stage in STAGE_CONTRACTS)
_STEP_IDS = frozenset(
    subtask.source_step_id for stage in STAGE_CONTRACTS for subtask in stage.subtasks
)
_PERSISTED_OWNER_POLICY = compile_persisted_dirty_owner_policy()
_SOLVER_RECEIPT_PATTERN = re.compile(
    _PERSISTED_OWNER_POLICY.receipt_path_pattern,
    _PERSISTED_OWNER_POLICY.receipt_path_regex_flags,
)
_SOLVER_JOB_ID_PATTERN = re.compile(_SOURCE_POLICY.solver_job_id_pattern)
_EVENT_ROW_POLICIES = _SOURCE_POLICY.event_row_policies


class _SnapshotBuildFailure(Exception):
    def __init__(self, code: SnapshotErrorCodeV0, context: str):
        self.code = code
        self.context = context
        super().__init__(f"{code.value}:{context}")


def _registered_enum(value: object, enum_type: type[Enum], path: str) -> None:
    if type(value) is not enum_type or not any(value is member for member in enum_type):
        raise SnapshotV0ValidationError(f"{path} is not a registered {enum_type.__name__} member")


def _field(value: object, name: str, path: str) -> object:
    try:
        return object.__getattribute__(value, name)
    except AttributeError as exc:
        raise SnapshotV0ValidationError(f"{path}.{name} is missing") from exc


def _text(value: object, path: str, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value):
        raise SnapshotV0ValidationError(f"{path} must be a plain string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SnapshotV0ValidationError(f"{path} must contain valid UTF-8 scalar values") from exc
    return value


def _optional_text(value: object, path: str, *, sha: bool = False) -> str | None:
    if value is None:
        return None
    result = _text(value, path)
    if sha and _SHA256_RE.fullmatch(result) is None:
        raise SnapshotV0ValidationError(f"{path} must be lowercase SHA-256")
    return result


def validate_snapshot_coordinate(value: SnapshotCoordinateV0) -> SnapshotCoordinateV0:
    if type(value) is not SnapshotCoordinateV0:
        raise SnapshotV0ValidationError("snapshot coordinate has an unsupported runtime type")
    for item in fields(SnapshotCoordinateV0):
        _field(value, item.name, "coordinate")
    if value.schema_version != SNAPSHOT_COORDINATE_SCHEMA:
        raise SnapshotV0ValidationError("snapshot coordinate schema is unsupported")
    _text(value.project_id, "coordinate.project_id")
    for name in ("workflow_schema_version", "project_revision"):
        number = getattr(value, name)
        if type(number) is not int or number < 0:
            raise SnapshotV0ValidationError(f"coordinate.{name} must be a non-negative plain integer")
    _optional_text(value.project_generation, "coordinate.project_generation")
    _optional_text(value.run_generation, "coordinate.run_generation")
    _text(value.runtime_generation, "coordinate.runtime_generation")
    _text(value.scheduler_generation, "coordinate.scheduler_generation")
    _optional_text(
        value.recorded_contract_pin_set_sha256,
        "coordinate.recorded_contract_pin_set_sha256",
        sha=True,
    )
    return value


def _validate_fact(value: SnapshotFactV0, path: str) -> SnapshotFactV0:
    if type(value) is not SnapshotFactV0:
        raise SnapshotV0ValidationError(f"{path} has an unsupported runtime type")
    for item in fields(SnapshotFactV0):
        _field(value, item.name, path)
    _text(value.fact_type, f"{path}.fact_type")
    _text(value.fact_key, f"{path}.fact_key")
    if _SHA256_RE.fullmatch(_text(value.value_sha256, f"{path}.value_sha256")) is None:
        raise SnapshotV0ValidationError(f"{path}.value_sha256 must be lowercase SHA-256")
    _optional_text(value.entity_type, f"{path}.entity_type")
    _optional_text(value.entity_id, f"{path}.entity_id")
    if value.entity_generation is not None and (
        type(value.entity_generation) is not int or value.entity_generation < 1
    ):
        raise SnapshotV0ValidationError(f"{path}.entity_generation is invalid")
    entity_values = (value.entity_type, value.entity_id, value.entity_generation)
    if any(item is None for item in entity_values) != all(
        item is None for item in entity_values
    ):
        raise SnapshotV0ValidationError(f"{path} has a partial entity binding")
    _optional_text(value.subject_type, f"{path}.subject_type")
    _optional_text(value.subject_id, f"{path}.subject_id")
    _optional_text(value.subject_sha256, f"{path}.subject_sha256", sha=True)
    subject_values = (value.subject_type, value.subject_id, value.subject_sha256)
    if any(item is None for item in subject_values) != all(
        item is None for item in subject_values
    ):
        raise SnapshotV0ValidationError(f"{path} has a partial subject binding")
    _optional_text(
        value.legacy_classifier_contract_sha256,
        f"{path}.legacy_classifier_contract_sha256",
        sha=True,
    )
    return value


def _validate_section(value: SnapshotSectionV0, path: str) -> SnapshotSectionV0:
    if type(value) is not SnapshotSectionV0:
        raise SnapshotV0ValidationError(f"{path} has an unsupported runtime type")
    for item in fields(SnapshotSectionV0):
        _field(value, item.name, path)
    _registered_enum(value.section_id, SnapshotSectionIdV0, f"{path}.section_id")
    _registered_enum(value.availability, SnapshotAvailabilityV0, f"{path}.availability")
    validate_snapshot_coordinate(value.coordinate)
    if type(value.facts) is not tuple:
        raise SnapshotV0ValidationError(f"{path}.facts must be an immutable tuple")
    keys: list[tuple[str, str]] = []
    for index, fact in enumerate(value.facts):
        checked = _validate_fact(fact, f"{path}.facts[{index}]")
        keys.append((checked.fact_type, checked.fact_key))
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise SnapshotV0ValidationError(f"{path}.facts must be uniquely canonical-sorted")
    if value.error_code is not None:
        _registered_enum(value.error_code, SnapshotErrorCodeV0, f"{path}.error_code")
    _optional_text(value.gap_id, f"{path}.gap_id")
    _optional_text(value.policy_id, f"{path}.policy_id")
    _optional_text(value.page_cursor, f"{path}.page_cursor")
    metadata = (value.error_code, value.gap_id, value.policy_id, value.page_cursor)
    if value.availability is SnapshotAvailabilityV0.AVAILABLE:
        if any(item is not None for item in metadata):
            raise SnapshotV0ValidationError(f"{path} AVAILABLE section must not carry status metadata")
    else:
        if value.facts:
            raise SnapshotV0ValidationError(f"{path} non-AVAILABLE section must not masquerade as facts")
        required_index = {
            SnapshotAvailabilityV0.ERROR: 0,
            SnapshotAvailabilityV0.UNAVAILABLE_LEGACY_UNBOUND: 1,
            SnapshotAvailabilityV0.REDACTED: 2,
            SnapshotAvailabilityV0.PAGED: 3,
        }[value.availability]
        if metadata[required_index] is None:
            raise SnapshotV0ValidationError(f"{path} lacks required typed status metadata")
    return value


def validate_project_snapshot_v0(value: ProjectSnapshotV0) -> ProjectSnapshotV0:
    if type(value) is not ProjectSnapshotV0:
        raise SnapshotV0ValidationError("project snapshot has an unsupported runtime type")
    for item in fields(ProjectSnapshotV0):
        _field(value, item.name, "snapshot")
    if value.schema_version != PROJECT_SNAPSHOT_V0_SCHEMA:
        raise SnapshotV0ValidationError("project snapshot schema is unsupported")
    validate_snapshot_coordinate(value.coordinate)
    _registered_enum(value.completeness, SnapshotCompletenessV0, "snapshot.completeness")
    if type(value.sections) is not tuple:
        raise SnapshotV0ValidationError("snapshot.sections must be an immutable tuple")
    checked_sections: list[SnapshotSectionV0] = []
    for index, section in enumerate(value.sections):
        checked = _validate_section(section, f"snapshot.sections[{index}]")
        checked_sections.append(checked)
        if checked.coordinate != value.coordinate:
            raise SnapshotV0ValidationError("snapshot sections do not share one coordinate")
    if tuple(section.section_id for section in checked_sections) != _EXPECTED_SECTION_ORDER:
        raise SnapshotV0ValidationError("snapshot sections are incomplete or out of canonical order")
    expected_completeness = (
        SnapshotCompletenessV0.COMPLETE
        if all(section.availability is SnapshotAvailabilityV0.AVAILABLE for section in value.sections)
        else SnapshotCompletenessV0.PARTIAL
    )
    if value.completeness is not expected_completeness:
        raise SnapshotV0ValidationError("snapshot completeness disagrees with section availability")
    if value.contract_pins is not None:
        if type(value.contract_pins) is not ContractPinSetV1:
            raise SnapshotV0ValidationError(
                "snapshot.contract_pins has an unsupported runtime type"
            )
        for item in fields(ContractPinSetV1):
            raw = _field(value.contract_pins, item.name, "snapshot.contract_pins")
            if item.name == "schema_version":
                if raw != CONTRACT_PIN_SET_SCHEMA:
                    raise SnapshotV0ValidationError(
                        "snapshot contract pin schema is unsupported"
                    )
            elif _SHA256_RE.fullmatch(
                _text(raw, f"snapshot.contract_pins.{item.name}")
            ) is None:
                raise SnapshotV0ValidationError(
                    f"snapshot.contract_pins.{item.name} must be lowercase SHA-256"
                )
    if value.coordinate.recorded_contract_pin_set_sha256 is None:
        if value.contract_pins is not None:
            raise SnapshotV0ValidationError(
                "snapshot contract pins lack a recorded coordinate identity"
            )
    elif value.contract_pins is None:
        raise SnapshotV0ValidationError(
            "snapshot recorded contract pin identity lacks recoverable pin values"
        )
    elif canonical_sha256(value.contract_pins) != value.coordinate.recorded_contract_pin_set_sha256:
        raise SnapshotV0ValidationError(
            "snapshot contract pin values do not match the recorded coordinate identity"
        )
    if value.completeness is SnapshotCompletenessV0.COMPLETE and value.contract_pins is None:
        raise SnapshotV0ValidationError(
            "COMPLETE snapshot must carry recoverable contract pin values"
        )
    if type(value.authoritative) is not bool or value.authoritative:
        raise SnapshotV0ValidationError("snapshot must be non-authoritative")
    for name in (
        "performed_workflow_side_effects",
        "application_initiated_write_operations",
    ):
        raw = getattr(value, name)
        if type(raw) is not tuple or raw:
            raise SnapshotV0ValidationError(f"snapshot {name} must be an empty tuple")
    return value


def validate_snapshot_build_result(value: SnapshotBuildResult) -> SnapshotBuildResult:
    if type(value) is not SnapshotBuildResult:
        raise SnapshotV0ValidationError("snapshot build result has an unsupported runtime type")
    for item in fields(SnapshotBuildResult):
        _field(value, item.name, "result")
    if value.schema_version != SNAPSHOT_BUILD_RESULT_SCHEMA:
        raise SnapshotV0ValidationError("snapshot build result schema is unsupported")
    _registered_enum(value.availability, SnapshotAvailabilityV0, "result.availability")
    if value.error_code is not None:
        _registered_enum(value.error_code, SnapshotErrorCodeV0, "result.error_code")
    _optional_text(value.error_context, "result.error_context")
    if type(value.analysis_sql_trace) is not tuple:
        raise SnapshotV0ValidationError("result.analysis_sql_trace must be an immutable tuple")
    for index, statement in enumerate(value.analysis_sql_trace):
        _text(statement, f"result.analysis_sql_trace[{index}]")
    if value.availability is SnapshotAvailabilityV0.AVAILABLE:
        if value.snapshot is None or value.error_code is not None or value.error_context is not None:
            raise SnapshotV0ValidationError("AVAILABLE build result has contradictory status fields")
        validate_project_snapshot_v0(value.snapshot)
    else:
        if value.snapshot is not None or value.error_code is None or value.error_context is None:
            raise SnapshotV0ValidationError("non-AVAILABLE build result has contradictory status fields")
    if type(value.authoritative) is not bool or value.authoritative:
        raise SnapshotV0ValidationError("snapshot build result must be non-authoritative")
    for name in (
        "performed_workflow_side_effects",
        "application_initiated_write_operations",
    ):
        raw = getattr(value, name)
        if type(raw) is not tuple or raw:
            raise SnapshotV0ValidationError(
                f"snapshot build result {name} must be an empty tuple"
            )
    return value


def project_snapshot_v0_semantic_bytes(value: ProjectSnapshotV0) -> bytes:
    snapshot = validate_project_snapshot_v0(value)
    try:
        return canonical_bytes(snapshot)
    except CanonicalizationError as exc:
        raise SnapshotV0ValidationError("project snapshot cannot be canonicalized") from exc


def project_snapshot_v0_semantic_sha256(value: ProjectSnapshotV0) -> str:
    snapshot = validate_project_snapshot_v0(value)
    try:
        return canonical_sha256(snapshot)
    except CanonicalizationError as exc:
        raise SnapshotV0ValidationError("project snapshot cannot be canonicalized") from exc


def project_snapshot_v0_analysis_bytes(value: ProjectSnapshotV0) -> bytes:
    # Snapshot facts carry no live source locators; semantic and analysis bytes
    # are presently equal under an explicitly separate public identity name.
    return project_snapshot_v0_semantic_bytes(value)


def project_snapshot_v0_analysis_sha256(value: ProjectSnapshotV0) -> str:
    snapshot = validate_project_snapshot_v0(value)
    try:
        return canonical_sha256(snapshot)
    except CanonicalizationError as exc:
        raise SnapshotV0ValidationError("project snapshot analysis cannot be canonicalized") from exc


def snapshot_build_result_semantic_bytes(value: SnapshotBuildResult) -> bytes:
    result = validate_snapshot_build_result(value)
    projection = (
        result.schema_version,
        result.availability,
        result.snapshot,
        result.error_code,
        result.error_context,
        result.authoritative,
        result.performed_workflow_side_effects,
        result.application_initiated_write_operations,
    )
    try:
        return canonical_bytes(projection)
    except CanonicalizationError as exc:
        raise SnapshotV0ValidationError("snapshot build result cannot be canonicalized") from exc


def snapshot_build_result_analysis_bytes(value: SnapshotBuildResult) -> bytes:
    result = validate_snapshot_build_result(value)
    try:
        return canonical_bytes(result)
    except CanonicalizationError as exc:
        raise SnapshotV0ValidationError("snapshot build result analysis cannot be canonicalized") from exc


def snapshot_build_result_semantic_sha256(value: SnapshotBuildResult) -> str:
    return hashlib.sha256(snapshot_build_result_semantic_bytes(value)).hexdigest()


def snapshot_build_result_analysis_sha256(value: SnapshotBuildResult) -> str:
    return hashlib.sha256(snapshot_build_result_analysis_bytes(value)).hexdigest()


def _unavailable_result(
    code: SnapshotErrorCodeV0,
    context: str,
    trace: tuple[str, ...] = (),
) -> SnapshotBuildResult:
    availability = (
        SnapshotAvailabilityV0.UNAVAILABLE_LEGACY_UNBOUND
        if code is SnapshotErrorCodeV0.DB_NOT_FOUND_LEGACY_FILESYSTEM_ONLY
        else SnapshotAvailabilityV0.ERROR
    )
    return validate_snapshot_build_result(
        SnapshotBuildResult(
            schema_version=SNAPSHOT_BUILD_RESULT_SCHEMA,
            availability=availability,
            snapshot=None,
            error_code=code,
            error_context=context,
            analysis_sql_trace=trace,
            authoritative=False,
            performed_workflow_side_effects=(),
            application_initiated_write_operations=(),
        )
    )


def _lstat_regular_db(path: Path) -> SnapshotBuildResult | None:
    if not path.is_absolute():
        return _unavailable_result(SnapshotErrorCodeV0.DB_PATH_NOT_REGULAR, "database path must be absolute")
    for component in (path,) + tuple(path.parents):
        try:
            component_details = os.lstat(component)
        except FileNotFoundError:
            if component == path:
                return _unavailable_result(
                    SnapshotErrorCodeV0.DB_NOT_FOUND_LEGACY_FILESYSTEM_ONLY,
                    "database file is absent",
                )
            return _unavailable_result(
                SnapshotErrorCodeV0.DB_OPEN_FAILED,
                "database parent path is unavailable",
            )
        except OSError:
            return _unavailable_result(SnapshotErrorCodeV0.DB_OPEN_FAILED, "database lstat failed")
        if stat.S_ISLNK(component_details.st_mode):
            return _unavailable_result(
                SnapshotErrorCodeV0.DB_PATH_SYMLINK,
                "database path or parent component is a symlink",
            )
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        return _unavailable_result(
            SnapshotErrorCodeV0.DB_NOT_FOUND_LEGACY_FILESYSTEM_ONLY,
            "database file is absent",
        )
    except OSError:
        return _unavailable_result(SnapshotErrorCodeV0.DB_OPEN_FAILED, "database lstat failed")
    if stat.S_ISLNK(details.st_mode):
        return _unavailable_result(SnapshotErrorCodeV0.DB_PATH_SYMLINK, "database path is a symlink")
    if not stat.S_ISREG(details.st_mode):
        return _unavailable_result(SnapshotErrorCodeV0.DB_PATH_NOT_REGULAR, "database path is not a regular file")
    return None


def _authorizer(action: int, arg1: str | None, arg2: str | None, _db: str | None, _trigger: str | None) -> int:
    denied = {
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_INDEX,
        sqlite3.SQLITE_CREATE_TEMP_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
        sqlite3.SQLITE_CREATE_TEMP_VIEW,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_CREATE_VIEW,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_INDEX,
        sqlite3.SQLITE_DROP_TEMP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_TRIGGER,
        sqlite3.SQLITE_DROP_TEMP_VIEW,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_DROP_VIEW,
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
        sqlite3.SQLITE_REINDEX,
        sqlite3.SQLITE_ANALYZE,
    }
    if action in denied:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA:
        if str(arg1 or "").lower() == "query_only" and str(arg2 or "").upper() in {"", "ON", "1"}:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _rows(connection: sqlite3.Connection, table: str, order: str) -> tuple[dict[str, object], ...]:
    if not _table_exists(connection, table):
        raise _SnapshotBuildFailure(
            SnapshotErrorCodeV0.SECTION_READ_FAILED,
            f"required table unavailable:{table}",
        )
    return tuple(dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}').fetchall())


def _validate_json_and_coordinate(
    rows: tuple[dict[str, object], ...],
    *,
    table: str,
    project_id: str,
    project_revision: int,
) -> None:
    seen_keys: set[tuple[object, ...]] = set()
    for row_index, row in enumerate(rows):
        for name, value in row.items():
            if name in _REVISION_COLUMNS and value is not None:
                if type(value) is not int or value < 0 or value > project_revision:
                    raise _SnapshotBuildFailure(
                        SnapshotErrorCodeV0.FUTURE_ROW_REVISION,
                        f"{table}.{name} at row {row_index}",
                    )
            if name in _HASH_COLUMNS and value is not None:
                migration_seed = (
                    table in {"stage_cursor_inputs", "stage_checkpoints", "stage_checkpoint_history"}
                    and name in {"input_fingerprint", "output_fingerprint"}
                    and value == "MIGRATION_SEED"
                )
                legacy_unbound = (
                    table == "workflow_decision_requests"
                    and name in {"subject_fingerprint", "options_fingerprint"}
                    and value == "LEGACY_UNBOUND"
                )
                if not (migration_seed or legacy_unbound) and (
                    type(value) is not str or _SHA256_RE.fullmatch(value) is None
                ):
                    raise _SnapshotBuildFailure(
                        SnapshotErrorCodeV0.REQUIRED_HASH_INVALID,
                        f"{table}.{name} at row {row_index}",
                    )
            if name in _JSON_COLUMNS and value is not None:
                if type(value) is not str:
                    raise _SnapshotBuildFailure(
                        SnapshotErrorCodeV0.REQUIRED_JSON_INVALID,
                        f"{table}.{name} at row {row_index}",
                    )
                try:
                    parsed = json.loads(value)
                except (TypeError, ValueError):
                    raise _SnapshotBuildFailure(
                        SnapshotErrorCodeV0.REQUIRED_JSON_INVALID,
                        f"{table}.{name} at row {row_index}",
                    ) from None
                stack = [parsed]
                while stack:
                    item = stack.pop()
                    if type(item) is dict:
                        for key, child in item.items():
                            if key == "project_id" and child != project_id:
                                raise _SnapshotBuildFailure(
                                    SnapshotErrorCodeV0.PROJECT_ID_MISMATCH,
                                    f"{table}.{name} embeds a foreign project",
                                )
                            stack.append(child)
                    elif type(item) is list:
                        stack.extend(item)
        key = tuple(row.get(name) for name in sorted(row) if name.endswith("_id") or name in {"revision", "flag", "owner_stage", "gate", "singleton", "projector_name", "subtask"})
        if key and key in seen_keys:
            raise _SnapshotBuildFailure(
                SnapshotErrorCodeV0.REQUIRED_DUPLICATE,
                f"{table} has a duplicate required identity",
            )
        seen_keys.add(key)


def _parsed_json_object(
    row: dict[str, object],
    column: str,
    *,
    code: SnapshotErrorCodeV0,
    context: str,
    allow_none: bool = False,
) -> dict[str, object] | None:
    raw = row.get(column)
    if raw is None and allow_none:
        return None
    if type(raw) is not str:
        raise _SnapshotBuildFailure(code, f"{context}.{column} must be JSON text")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        raise _SnapshotBuildFailure(code, f"{context}.{column} is invalid JSON") from None
    if type(parsed) is not dict:
        raise _SnapshotBuildFailure(code, f"{context}.{column} must encode an object")
    return parsed


def _storage_canonical_sha256(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise _SnapshotBuildFailure(
            SnapshotErrorCodeV0.REQUIRED_JSON_INVALID,
            "domain value is not canonical JSON",
        ) from None
    return hashlib.sha256(encoded).hexdigest()


def _validate_json_tree(value: object, *, context: str) -> None:
    code = SnapshotErrorCodeV0.EVENT_CHAIN_INVALID
    if value is None or type(value) in {bool, int, float}:
        return
    if type(value) is str:
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise _SnapshotBuildFailure(
                code, f"{context} contains a non-UTF-8 string"
            ) from None
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _validate_json_tree(item, context=f"{context}[{index}]")
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise _SnapshotBuildFailure(
                    code, f"{context} contains a non-string object key"
                )
            _validate_json_tree(key, context=f"{context}.key")
            _validate_json_tree(item, context=f"{context}.{key}")
        return
    raise _SnapshotBuildFailure(code, f"{context} has an unsupported JSON value")


def _project_relative_posix_path(
    value: object,
    *,
    context: str,
    code: SnapshotErrorCodeV0,
) -> str:
    if (
        type(value) is not str
        or not value
        or "\\" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise _SnapshotBuildFailure(code, f"{context} is not a safe project-relative POSIX path")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise _SnapshotBuildFailure(
            code, f"{context} is not a safe project-relative POSIX path"
        ) from None
    segments = value.split("/")
    if value.startswith("/") or any(segment in {"", ".", ".."} for segment in segments):
        raise _SnapshotBuildFailure(code, f"{context} is not a safe project-relative POSIX path")
    return value


def _select_event_row_policy_v1(
    event_type: str,
    payload: dict[str, object],
    *,
    revision: int,
) -> EventRowPolicyV1:
    candidates = tuple(
        policy
        for policy in _EVENT_ROW_POLICIES
        if policy.event_type == event_type
        and all(name in payload for name in policy.required_payload_fields)
        and all(name not in payload for name in policy.forbidden_payload_fields)
    )
    if len(candidates) != 1:
        detail = (
            "is not source-authorized"
            if not any(policy.event_type == event_type for policy in _EVENT_ROW_POLICIES)
            else "does not match exactly one source-authorized payload family"
        )
        raise _SnapshotBuildFailure(
            SnapshotErrorCodeV0.EVENT_CHAIN_INVALID,
            f"event revision {revision} type {event_type!r} {detail}",
        )
    return candidates[0]


def _validate_event_row_binding_v1(
    policy: EventRowPolicyV1,
    *,
    revision: int,
    step: object,
    attempt: object,
    payload: dict[str, object],
    subject_state: dict[str, object] | None,
    result_state: dict[str, object],
) -> None:
    code = SnapshotErrorCodeV0.EVENT_CHAIN_INVALID
    if step is not None and (type(step) is not int or step not in _STEP_IDS):
        raise _SnapshotBuildFailure(
            code, f"event revision {revision} Step is outside the source catalog"
        )
    if type(attempt) is not int or attempt < 0:
        raise _SnapshotBuildFailure(
            code, f"event revision {revision} attempt is invalid"
        )

    mode = policy.step_binding_mode
    if mode is EventStepBindingModeV1.STEP_NONE:
        expected_step = None
    elif mode is EventStepBindingModeV1.STEP_SOURCE_CATALOG:
        if step not in policy.source_catalog_steps:
            raise _SnapshotBuildFailure(
                code,
                f"event revision {revision} Step is not authorized for {policy.payload_family}",
            )
        expected_step = step
    elif mode is EventStepBindingModeV1.STEP_PAYLOAD_SOURCE:
        field = policy.payload_source_step_field
        expected_step = payload.get(field) if field is not None else None
        if type(expected_step) is not int or expected_step not in _STEP_IDS:
            raise _SnapshotBuildFailure(
                code,
                f"event revision {revision} payload source Step is outside the source catalog",
            )
    elif mode is EventStepBindingModeV1.STEP_PAYLOAD_STAGE_SUBTASK:
        stage = payload.get("stage")
        subtask = payload.get("subtask")
        binding = _STAGE_SUBTASKS.get((stage, subtask))
        if binding is None:
            raise _SnapshotBuildFailure(
                code,
                f"event revision {revision} payload Stage/subtask is not source-authorized",
            )
        expected_step = binding[0]
    elif mode is EventStepBindingModeV1.STEP_SUBJECT_SOURCE:
        if subject_state is None:
            raise _SnapshotBuildFailure(
                code, f"event revision {revision} lacks its subject Step binding"
            )
        expected_step = subject_state.get("source_step_id")
    elif mode is EventStepBindingModeV1.STEP_RESULT_SOURCE:
        expected_step = result_state.get("source_step_id")
    else:  # pragma: no cover - source projection makes this unreachable
        raise _SnapshotBuildFailure(code, f"event revision {revision} Step policy is unsupported")
    if step != expected_step:
        raise _SnapshotBuildFailure(
            code,
            f"event revision {revision} row Step contradicts {policy.payload_family}",
        )

    attempt_mode = policy.attempt_binding_mode
    if attempt_mode is EventAttemptBindingModeV1.ATTEMPT_ZERO:
        expected_attempt = 0
    elif attempt_mode is EventAttemptBindingModeV1.ATTEMPT_SUBJECT:
        if subject_state is None:
            raise _SnapshotBuildFailure(
                code, f"event revision {revision} lacks its subject attempt binding"
            )
        expected_attempt = subject_state.get("attempt")
    elif attempt_mode is EventAttemptBindingModeV1.ATTEMPT_RESULT:
        expected_attempt = result_state.get("attempt")
    else:  # pragma: no cover - source projection makes this unreachable
        raise _SnapshotBuildFailure(code, f"event revision {revision} attempt policy is unsupported")
    if attempt != expected_attempt:
        raise _SnapshotBuildFailure(
            code,
            f"event revision {revision} row attempt contradicts {policy.payload_family}",
        )
    if policy.payload_attempt_field is not None and (
        payload.get(policy.payload_attempt_field) != attempt
    ):
        raise _SnapshotBuildFailure(
            code,
            f"event revision {revision} payload attempt contradicts its row",
        )


def _solver_job_id(value: object, *, context: str) -> str:
    code = SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID
    if type(value) is not str or _SOLVER_JOB_ID_PATTERN.fullmatch(value) is None:
        raise _SnapshotBuildFailure(code, f"{context} is not a source-authorized Solver job ID")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise _SnapshotBuildFailure(
            code, f"{context} is not a source-authorized Solver job ID"
        ) from None
    return value


def _current_domain_effect_hashes(
    connection: sqlite3.Connection,
) -> dict[str, str]:
    values: dict[str, str] = {}
    for effect_name, table, order in _SOURCE_POLICY.domain_effect_tables:
        rows = _rows(connection, table, order)
        values[effect_name] = _storage_canonical_sha256(list(rows))
    return values


def _validate_domain_root(
    payload: dict[str, object],
    envelope: dict[str, object],
    *,
    context: str,
    expected_current: dict[str, str] | None,
) -> None:
    code = SnapshotErrorCodeV0.EVENT_CHAIN_INVALID
    top_effects = payload.get("effect_hashes_after")
    envelope_effects = envelope.get("effect_hashes_after")
    top_aggregate = payload.get("aggregate_root_hash_after")
    envelope_aggregate = envelope.get("aggregate_root_hash_after")
    if type(top_effects) is not dict or type(envelope_effects) is not dict:
        raise _SnapshotBuildFailure(code, f"{context} lacks an exact domain-effect map")
    expected_keys = {name for name, _table, _order in _SOURCE_POLICY.domain_effect_tables}
    if set(top_effects) != expected_keys or set(envelope_effects) != expected_keys:
        raise _SnapshotBuildFailure(code, f"{context} domain-effect keys are not source-authorized")
    for name, value in top_effects.items():
        if type(name) is not str or type(value) is not str or _SHA256_RE.fullmatch(value) is None:
            raise _SnapshotBuildFailure(code, f"{context} domain effect {name!r} is not SHA-256")
    if top_effects != envelope_effects:
        raise _SnapshotBuildFailure(code, f"{context} top-level and envelope domain roots differ")
    expected_aggregate = workflow_canonical_hash(top_effects)
    if (
        type(top_aggregate) is not str
        or _SHA256_RE.fullmatch(top_aggregate) is None
        or top_aggregate != expected_aggregate
        or envelope_aggregate != top_aggregate
    ):
        raise _SnapshotBuildFailure(code, f"{context} aggregate domain root is inconsistent")
    if expected_current is not None and top_effects != expected_current:
        raise _SnapshotBuildFailure(code, f"{context} head domain root differs from current business rows")


def _project_replay_state(project: dict[str, object]) -> dict[str, object]:
    values = {name: project.get(name) for name in replay_state({})}
    values["pending_action"] = _parsed_json_object(
        project,
        "pending_action_json",
        code=SnapshotErrorCodeV0.EVENT_CHAIN_INVALID,
        context="project_state",
        allow_none=True,
    )
    return replay_state(values)


def _validate_event_stream(
    connection: sqlite3.Connection,
    event_rows: tuple[dict[str, object], ...],
    project: dict[str, object],
) -> tuple[WorkflowEvent, ...]:
    code = SnapshotErrorCodeV0.EVENT_CHAIN_INVALID
    if not event_rows:
        if project.get("revision") != 0:
            raise _SnapshotBuildFailure(code, "nonzero project revision lacks an event stream")
        return ()
    replay_fields = tuple(replay_state({}))
    current_effects = _current_domain_effect_hashes(connection)
    prior_state: dict[str, object] | None = None
    events: list[WorkflowEvent] = []
    for index, row in enumerate(event_rows):
        revision = row.get("revision")
        event_type = row.get("type")
        created_at = row.get("created_at")
        step = row.get("step")
        attempt = row.get("attempt")
        if type(revision) is not int or revision < 1:
            raise _SnapshotBuildFailure(code, f"event row {index} revision is invalid")
        if type(event_type) is not str or not event_type:
            raise _SnapshotBuildFailure(code, f"event row {index} type is invalid")
        if type(created_at) is not int or created_at < 0:
            raise _SnapshotBuildFailure(code, f"event row {index} created_at is invalid")
        _validate_json_tree(event_type, context=f"events[{revision}].type")
        payload = _parsed_json_object(
            row,
            "payload_json",
            code=code,
            context=f"events[{revision}]",
        )
        assert payload is not None
        _validate_json_tree(payload, context=f"events[{revision}].payload")
        _storage_canonical_sha256(payload)
        row_policy = _select_event_row_policy_v1(
            event_type, payload, revision=revision
        )
        envelope = payload.get(ENVELOPE_KEY)
        if type(envelope) is not dict:
            raise _SnapshotBuildFailure(
                code, f"event revision {revision} is legacy-unbound without a workflow envelope"
            )
        if set(envelope) != set(_SOURCE_POLICY.event_envelope_fields):
            raise _SnapshotBuildFailure(code, f"event revision {revision} envelope schema is unsupported")
        event_version = envelope.get("event_version")
        if type(event_version) is not int or event_version not in _SOURCE_POLICY.supported_event_versions:
            raise _SnapshotBuildFailure(code, f"event revision {revision} version is unsupported")
        if envelope.get("replay_state_version") != _SOURCE_POLICY.replay_state_version:
            raise _SnapshotBuildFailure(code, f"event revision {revision} replay schema is unsupported")
        expected_event_id = workflow_canonical_hash(
            {
                "project_id": project.get("project_id"),
                "revision": revision,
                "event_type": event_type,
                "created_at": created_at,
            }
        )[:32]
        if envelope.get("event_id") != expected_event_id:
            raise _SnapshotBuildFailure(code, f"event revision {revision} identity is inconsistent")
        if (
            envelope.get("canonical_type") != row_policy.canonical_type
            or envelope.get("canonical_type") != canonical_event_type(event_type)
        ):
            raise _SnapshotBuildFailure(code, f"event revision {revision} canonical type is inconsistent")
        mode = envelope.get("state_patch_mode")
        patch = envelope.get("state_patch")
        if mode not in {"snapshot", "merge"} or type(patch) is not dict:
            raise _SnapshotBuildFailure(code, f"event revision {revision} state patch is malformed")
        if any(type(name) is not str or name not in replay_fields for name in patch):
            raise _SnapshotBuildFailure(code, f"event revision {revision} state patch has an unknown field")
        if mode == "snapshot":
            if set(patch) != set(replay_fields):
                raise _SnapshotBuildFailure(code, f"event revision {revision} snapshot patch is incomplete")
            after_state = dict(patch)
        else:
            if prior_state is None:
                raise _SnapshotBuildFailure(code, f"event revision {revision} merge lacks a prior snapshot")
            after_state = dict(prior_state)
            after_state.update(patch)
        expected_before = (
            workflow_canonical_hash(prior_state) if prior_state is not None else None
        )
        if envelope.get("state_hash_before") != expected_before:
            raise _SnapshotBuildFailure(code, f"event revision {revision} before-state hash is inconsistent")
        if envelope.get("state_hash_after") != workflow_canonical_hash(after_state):
            raise _SnapshotBuildFailure(code, f"event revision {revision} after-state hash is inconsistent")
        _validate_event_row_binding_v1(
            row_policy,
            revision=revision,
            step=step,
            attempt=attempt,
            payload=payload,
            subject_state=prior_state,
            result_state=after_state,
        )
        scheduler = (
            prior_state.get("scheduler_generation")
            if prior_state is not None
            else after_state.get("scheduler_generation")
        )
        if envelope.get("scheduler_generation") != scheduler:
            raise _SnapshotBuildFailure(code, f"event revision {revision} scheduler binding is inconsistent")
        expected_authority = "stage" if scheduler == STAGE_SCHEDULER_GENERATION else "step"
        if envelope.get("coordinate_authority") != expected_authority:
            raise _SnapshotBuildFailure(code, f"event revision {revision} coordinate authority is inconsistent")
        subject = prior_state or {}
        result_bindings = {
            "result_stage_id": after_state.get("active_stage"),
            "result_subtask": after_state.get("active_subtask"),
            "result_source_step_id": after_state.get("source_step_id"),
        }
        subject_bindings = {
            "subject_stage_id": subject.get("active_stage"),
            "subject_subtask": subject.get("active_subtask"),
            "subject_source_step_id": subject.get("source_step_id"),
        }
        alias_source = subject if prior_state is not None else after_state
        aliases = {
            "stage_id": alias_source.get("active_stage"),
            "subtask": alias_source.get("active_subtask"),
            "source_step_id": alias_source.get("source_step_id"),
        }
        for name, expected in {**subject_bindings, **result_bindings, **aliases}.items():
            if envelope.get(name) != expected:
                raise _SnapshotBuildFailure(code, f"event revision {revision} {name} is inconsistent")
        for name in ("request_id", "decision_id"):
            value = envelope.get(name)
            if value is not None and (type(value) is not str or not value):
                raise _SnapshotBuildFailure(code, f"event revision {revision} {name} is invalid")
        manifest = envelope.get("artifact_manifest_sha256")
        if manifest is not None and (
            type(manifest) is not str or _SHA256_RE.fullmatch(manifest) is None
        ):
            raise _SnapshotBuildFailure(code, f"event revision {revision} artifact manifest is invalid")
        reason = envelope.get("reason")
        if type(reason) is not dict or set(reason) != {
            "code",
            "message",
            "evidence",
            "recovery_target",
        }:
            raise _SnapshotBuildFailure(code, f"event revision {revision} reason is malformed")
        if type(reason.get("code")) is not str or type(reason.get("message")) is not str:
            raise _SnapshotBuildFailure(code, f"event revision {revision} reason text is malformed")
        if type(reason.get("evidence")) is not list or (
            reason.get("recovery_target") is not None
            and type(reason.get("recovery_target")) is not dict
        ):
            raise _SnapshotBuildFailure(code, f"event revision {revision} reason evidence is malformed")
        side_effect_refs = envelope.get("side_effect_refs")
        if type(side_effect_refs) is not list:
            raise _SnapshotBuildFailure(code, f"event revision {revision} side-effect refs are malformed")
        _validate_immutable_ref_value(
            side_effect_refs, context=f"events[{revision}]._workflow.side_effect_refs"
        )
        _validate_domain_root(
            payload,
            envelope,
            context=f"event revision {revision}",
            expected_current=current_effects if index == len(event_rows) - 1 else None,
        )
        event = WorkflowEvent(revision, event_type, created_at, step, attempt, payload)
        if type(event) is not WorkflowEvent:
            raise _SnapshotBuildFailure(code, f"event revision {revision} DTO is invalid")
        events.append(event)
        prior_state = after_state
    try:
        replayed = replay_events(tuple(events), verify_hashes=True)
    except ReplayIntegrityError as exc:
        raise _SnapshotBuildFailure(code, f"workflow event replay failed: {exc}") from None
    if replayed != _project_replay_state(project):
        raise _SnapshotBuildFailure(code, "event replay final state differs from project_state")
    return tuple(events)


def _validate_project_state_source(project: dict[str, object]) -> None:
    code = SnapshotErrorCodeV0.PROJECT_STATE_CONTRACT_INVALID
    if project.get("schema_version") != SCHEMA_VERSION:
        raise _SnapshotBuildFailure(code, "project state schema differs from schema_info")
    if project.get("status") not in _SOURCE_POLICY.workflow_statuses:
        raise _SnapshotBuildFailure(code, "project status is not source-authorized")
    runtime = project.get("runtime_generation")
    scheduler = project.get("scheduler_generation")
    if runtime not in _SOURCE_POLICY.runtime_generations:
        raise _SnapshotBuildFailure(code, "project runtime generation is not source-authorized")
    if scheduler not in _SOURCE_POLICY.scheduler_generations:
        raise _SnapshotBuildFailure(code, "project scheduler generation is not source-authorized")
    catalog = project.get("stage_catalog_version")
    if scheduler == STAGE_SCHEDULER_GENERATION:
        if runtime != "native_v2" or catalog != _SOURCE_POLICY.stage_catalog_version:
            raise _SnapshotBuildFailure(code, "Stage scheduler coordinate is inconsistent")
    elif catalog is not None:
        raise _SnapshotBuildFailure(code, "Step scheduler must not claim a Stage catalog")
    last_step = project.get("last_completed_step")
    last_stage = project.get("last_completed_stage")
    active_step = project.get("active_step")
    source_step = project.get("source_step_id")
    attempt = project.get("attempt")
    if type(last_step) is not int or last_step < -1 or last_step > max(_STEP_IDS):
        raise _SnapshotBuildFailure(code, "last completed Step is outside the source catalog")
    if type(last_stage) is not int or last_stage != completed_stage_for_step(last_step):
        raise _SnapshotBuildFailure(code, "last completed Stage contradicts the Step cursor")
    if active_step is not None and (type(active_step) is not int or active_step not in _STEP_IDS):
        raise _SnapshotBuildFailure(code, "active Step is outside the source catalog")
    if source_step is not None and (type(source_step) is not int or source_step not in _STEP_IDS):
        raise _SnapshotBuildFailure(code, "source Step is outside the source catalog")
    if type(attempt) is not int or attempt < 0:
        raise _SnapshotBuildFailure(code, "attempt is not a non-negative plain integer")
    active_stage = project.get("active_stage")
    active_subtask = project.get("active_subtask")
    if active_stage is None:
        if active_subtask is not None:
            raise _SnapshotBuildFailure(code, "active subtask lacks an active Stage")
    else:
        if type(active_stage) is not int or active_stage not in _STAGE_IDS:
            raise _SnapshotBuildFailure(code, "active Stage is outside the source catalog")
        if type(active_subtask) is not str:
            raise _SnapshotBuildFailure(code, "active Stage lacks a source-authorized subtask")
        binding = _STAGE_SUBTASKS.get((active_stage, active_subtask))
        if binding is None:
            raise _SnapshotBuildFailure(code, "active Stage/subtask binding is not source-authorized")
        expected_source = binding[0]
        if source_step != expected_source or active_step not in {None, expected_source}:
            raise _SnapshotBuildFailure(code, "active Stage cursor contradicts its source Step")
        if last_stage >= active_stage:
            raise _SnapshotBuildFailure(code, "active Stage is not after the completed Stage cursor")
    if scheduler == STEP_SCHEDULER_GENERATION and source_step not in {None, active_step}:
        raise _SnapshotBuildFailure(code, "Step scheduler source cursor contradicts active Step")


def _validate_stage_rows(
    rows_by_table: dict[str, tuple[dict[str, object], ...]],
    project: dict[str, object],
) -> None:
    code = SnapshotErrorCodeV0.STAGE_CHECKPOINT_CONTRACT_INVALID
    active_binding = (
        project.get("active_stage"),
        project.get("active_subtask"),
        project.get("source_step_id"),
    )
    cursor_rows = rows_by_table.get("stage_cursor_inputs", ())
    if len(cursor_rows) > 1:
        raise _SnapshotBuildFailure(code, "Stage cursor input is not a singleton")
    for table, rows in rows_by_table.items():
        for index, row in enumerate(rows):
            stage_id = row.get("stage_id")
            subtask = row.get("subtask")
            source_step = row.get("source_step_id")
            if type(stage_id) is not int or type(subtask) is not str:
                raise _SnapshotBuildFailure(code, f"{table} row {index} has an invalid Stage identity")
            binding = _STAGE_SUBTASKS.get((stage_id, subtask))
            if binding is None or source_step != binding[0]:
                raise _SnapshotBuildFailure(code, f"{table} row {index} contradicts the Stage catalog")
            if table == "stage_cursor_inputs":
                if (stage_id, subtask, source_step) != active_binding:
                    raise _SnapshotBuildFailure(code, "recorded Stage cursor differs from project state")
                baseline = _parsed_json_object(
                    row,
                    "baseline_json",
                    code=code,
                    context=table,
                )
                if baseline is None:  # pragma: no cover - required above
                    raise _SnapshotBuildFailure(code, "Stage baseline is unavailable")
                continue
            if row.get("completed_step_id") != binding[1]:
                raise _SnapshotBuildFailure(code, f"{table} row {index} has a mismatched completed Step")
            if (
                table == "stage_checkpoints"
                and binding[1] is not None
                and binding[1] > project.get("last_completed_step", -1)
            ):
                raise _SnapshotBuildFailure(
                    code,
                    f"{table} row {index} is ahead of the project Step cursor",
                )
            completed_revision = row.get("completed_revision")
            if type(completed_revision) is not int or completed_revision < 1:
                raise _SnapshotBuildFailure(
                    code,
                    f"{table} row {index} has an invalid completed revision",
                )
            if table == "stage_checkpoint_history":
                expected_checkpoint_id = _storage_canonical_sha256(
                    {
                        "stage_id": stage_id,
                        "subtask": subtask,
                        "revision": completed_revision,
                        "input": row.get("input_fingerprint"),
                        "output": row.get("output_fingerprint"),
                    }
                )[:32]
                if row.get("checkpoint_id") != expected_checkpoint_id:
                    raise _SnapshotBuildFailure(
                        code,
                        f"{table} row {index} has an unbound checkpoint identity",
                    )
            receipt = _parsed_json_object(
                row,
                "receipt_json",
                code=code,
                context=f"{table}[{index}]",
            )
            assert receipt is not None
            if receipt.get("schema_version") != _SOURCE_POLICY.checkpoint_receipt_schema:
                raise _SnapshotBuildFailure(code, f"{table} row {index} has an unsupported receipt schema")
            migration = receipt.get("source") == "compatibility_cursor_seed"
            if migration:
                if (
                    row.get("input_fingerprint") != "MIGRATION_SEED"
                    or row.get("output_fingerprint") != "MIGRATION_SEED"
                    or receipt.get("stage_id") != stage_id
                    or receipt.get("subtask") != subtask
                    or receipt.get("source_step_id") != source_step
                    or receipt.get("completed_step_id") != binding[1]
                ):
                    raise _SnapshotBuildFailure(code, "compatibility checkpoint fingerprints are inconsistent")
            else:
                if receipt.get("status") != "PASS":
                    raise _SnapshotBuildFailure(code, f"{table} row {index} is not a successful checkpoint")
                receipt_stage = receipt.get("stage", receipt.get("stage_id"))
                if (
                    receipt_stage != stage_id
                    or receipt.get("subtask") != subtask
                    or receipt.get("source_step_id") != source_step
                    or receipt.get("completed_step_id") != binding[1]
                    or receipt.get("input_fingerprint") != row.get("input_fingerprint")
                    or receipt.get("output_fingerprint") != row.get("output_fingerprint")
                ):
                    raise _SnapshotBuildFailure(code, f"{table} row {index} receipt contradicts its checkpoint")


def _validate_dirty_rows(
    rows_by_table: dict[str, tuple[dict[str, object], ...]],
    _context: object = None,
) -> None:
    code = SnapshotErrorCodeV0.DIRTY_FACT_CONTRACT_INVALID
    causes = rows_by_table.get("dirty_causes", ())
    active = rows_by_table.get("dirty_flags", ())
    for table in ("dirty_flags", "dirty_causes", "dirty_flag_clear_receipts"):
        for index, row in enumerate(rows_by_table.get(table, ())):
            if row.get("flag") not in _SOURCE_POLICY.dirty_flags:
                raise _SnapshotBuildFailure(code, f"{table} row {index} has an unknown dirty flag")
            if type(row.get("owner_stage")) is not int or row.get("owner_stage") not in _STAGE_IDS:
                raise _SnapshotBuildFailure(code, f"{table} row {index} has an unknown owner Stage")
    for index, row in enumerate(causes):
        expected_cause_id = _storage_canonical_sha256(
            {
                "revision": row.get("cause_revision"),
                "flag": row.get("flag"),
                "owner_stage": row.get("owner_stage"),
                "artifact": row.get("cause_artifact"),
                "baseline": row.get("baseline_fingerprint"),
                "current": row.get("current_fingerprint"),
            }
        )[:32]
        if row.get("cause_id") != expected_cause_id:
            raise _SnapshotBuildFailure(
                code, f"dirty cause row {index} has an unbound identity"
            )
    cause_identities = {
        (
            row.get("flag"),
            row.get("owner_stage"),
            row.get("cause_revision"),
            row.get("cause_artifact"),
            row.get("baseline_fingerprint"),
            row.get("current_fingerprint"),
            row.get("classifier_contract_sha256"),
        )
        for row in causes
    }
    rebase_authorized_active: set[tuple[object, ...]] = set()
    for index, row in enumerate(rows_by_table.get("dirty_classifier_rebases", ())):
        receipt = _parsed_json_object(
            row,
            "receipt_json",
            code=code,
            context=f"dirty_classifier_rebases[{index}]",
        )
        assert receipt is not None
        try:
            old_hashes = json.loads(str(row.get("old_classifier_sha256")))
        except (TypeError, ValueError):
            raise _SnapshotBuildFailure(code, "dirty rebase old classifier set is invalid") from None
        obligations = receipt.get("obligations")
        retired = receipt.get("retired_obligations")
        source_schema = row.get("source_schema_version")
        target_schema = row.get("target_schema_version")
        if (
            type(old_hashes) is not list
            or not old_hashes
            or any(type(item) is not str or _SHA256_RE.fullmatch(item) is None for item in old_hashes)
            or old_hashes != sorted(set(old_hashes))
            or type(obligations) is not list
            or type(retired) is not list
            or type(source_schema) is not int
            or source_schema < 1
            or target_schema != SCHEMA_VERSION
            or source_schema > target_schema
            or receipt.get("schema_version") != _SOURCE_POLICY.dirty_rebase_receipt_schema
            or receipt.get("rebase_id") != row.get("rebase_id")
            or receipt.get("source_schema_version") != source_schema
            or receipt.get("target_schema_version") != target_schema
            or receipt.get("old_classifier_sha256") != old_hashes
            or receipt.get("new_classifier_sha256") != row.get("new_classifier_sha256")
            or len(obligations) != row.get("obligation_count")
            or _storage_canonical_sha256(
                {key: value for key, value in receipt.items() if key != "rebase_id"}
            )
            != row.get("rebase_id")
        ):
            raise _SnapshotBuildFailure(code, "dirty rebase receipt contradicts its row")
        for obligation_index, obligation in enumerate(obligations):
            if type(obligation) is not dict:
                raise _SnapshotBuildFailure(
                    code,
                    f"dirty rebase obligation {obligation_index} is not an object",
                )
            source_causes = tuple(
                cause
                for cause in causes
                if cause.get("flag") == obligation.get("previous_flag")
                and cause.get("owner_stage") == obligation.get("previous_owner_stage")
                and cause.get("cause_revision") == obligation.get("cause_revision")
                and cause.get("cause_artifact") == obligation.get("cause_artifact")
                and cause.get("classifier_contract_sha256")
                == obligation.get("old_classifier_sha256")
            )
            if (
                len(source_causes) != 1
                or obligation.get("old_classifier_sha256") not in old_hashes
                or obligation.get("new_classifier_sha256")
                != row.get("new_classifier_sha256")
                or obligation.get("flag") not in _SOURCE_POLICY.dirty_flags
                or type(obligation.get("owner_stage")) is not int
                or obligation.get("owner_stage") not in _STAGE_IDS
            ):
                raise _SnapshotBuildFailure(
                    code, "dirty rebase obligation lacks its source-authorized cause"
                )
            cause = source_causes[0]
            rebase_authorized_active.add(
                (
                    obligation.get("flag"),
                    obligation.get("owner_stage"),
                    cause.get("cause_revision"),
                    cause.get("cause_artifact"),
                    cause.get("baseline_fingerprint"),
                    cause.get("current_fingerprint"),
                    obligation.get("new_classifier_sha256"),
                )
            )
    for row in active:
        identity = (
            row.get("flag"),
            row.get("owner_stage"),
            row.get("cause_revision"),
            row.get("cause_artifact"),
            row.get("baseline_fingerprint"),
            row.get("current_fingerprint"),
            row.get("classifier_contract_sha256"),
        )
        if identity not in cause_identities and identity not in rebase_authorized_active:
            raise _SnapshotBuildFailure(code, "active dirty fact lacks its immutable cause row")
    for index, row in enumerate(rows_by_table.get("dirty_flag_clear_receipts", ())):
        receipt = _parsed_json_object(
            row,
            "receipt_json",
            code=code,
            context=f"dirty_flag_clear_receipts[{index}]",
        )
        assert receipt is not None
        if (
            receipt.get("schema_version") != _SOURCE_POLICY.dirty_clear_receipt_schema
            or receipt.get("flag") != row.get("flag")
            or receipt.get("owner_stage") != row.get("owner_stage")
            or receipt.get("cleared_fingerprint") != row.get("cleared_fingerprint")
            or receipt.get("classifier_contract_sha256") != row.get("classifier_contract_sha256")
        ):
            raise _SnapshotBuildFailure(code, "dirty clear receipt contradicts its row")
        cause_revision = receipt.get("cause_revision")
        if type(cause_revision) is not int or cause_revision >= row.get("revision", -1):
            raise _SnapshotBuildFailure(code, "dirty clear receipt has an invalid cause revision")
        matching_causes = tuple(
            cause
            for cause in causes
            if cause.get("flag") == row.get("flag")
            and cause.get("owner_stage") == row.get("owner_stage")
            and cause.get("cause_revision") == cause_revision
            and cause.get("cause_artifact") == receipt.get("cause_artifact")
            and cause.get("classifier_contract_sha256")
            == row.get("classifier_contract_sha256")
        )
        if len(matching_causes) != 1:
            raise _SnapshotBuildFailure(
                code, "dirty clear receipt lacks its exact immutable cause"
            )
        success = receipt.get("success_receipt")
        if type(success) is not dict or (
            success.get("schema_version") != _SOURCE_POLICY.checkpoint_receipt_schema
            or success.get("status") != "PASS"
            or success.get("stage") != row.get("owner_stage")
            or success.get("output_fingerprint") != row.get("cleared_fingerprint")
        ):
            raise _SnapshotBuildFailure(code, "dirty clear lacks its successful Stage receipt")
        if any(
            active_row.get("flag") == row.get("flag")
            and active_row.get("owner_stage") == row.get("owner_stage")
            and active_row.get("cause_revision", -1) <= row.get("revision", -1)
            for active_row in active
        ):
            raise _SnapshotBuildFailure(code, "cleared dirty obligation is still active")


def _validate_pending_rows(
    rows_by_table: dict[str, tuple[dict[str, object], ...]],
    project: dict[str, object],
) -> None:
    code = SnapshotErrorCodeV0.PENDING_REQUEST_CONTRACT_INVALID
    requests = rows_by_table.get("workflow_decision_requests", ())
    request_by_id: dict[str, dict[str, object]] = {}
    allowed_status = {"open", "resolved", "rejected", "superseded"}
    for index, row in enumerate(requests):
        request = _parsed_json_object(
            row,
            "request_json",
            code=code,
            context=f"workflow_decision_requests[{index}]",
        )
        assert request is not None
        request_id = row.get("request_id")
        gate = row.get("gate_type")
        generation = row.get("generation")
        requested_revision = row.get("requested_revision")
        kind = row.get("kind")
        action_type = row.get("action_type")
        subject = row.get("subject_fingerprint")
        options = row.get("options_fingerprint")
        if type(request_id) is not str or not request_id or type(gate) is not str or not gate:
            raise _SnapshotBuildFailure(code, "decision request identity is invalid")
        if (
            type(generation) is not int
            or generation < 1
            or type(requested_revision) is not int
            or requested_revision < 1
            or kind not in _SOURCE_POLICY.decision_kinds
            or type(action_type) is not str
            or not action_type
        ):
            raise _SnapshotBuildFailure(code, "decision request domain values are invalid")
        if row.get("status") not in allowed_status:
            raise _SnapshotBuildFailure(code, "decision request status is not source-authorized")
        if gate not in _SOURCE_POLICY.gate_types:
            raise _SnapshotBuildFailure(code, "decision request gate is not source-authorized")
        normalized_action = action_type.lower()
        expected_kind = (
            "consultation"
            if "consult" in normalized_action
            else "approval"
            if (
                "approval" in normalized_action
                or "override" in normalized_action
                or gate.endswith("_approval")
                or gate in {"content_freeze", "delivery_freeze_override"}
            )
            else "selection"
        )
        if kind != expected_kind:
            raise _SnapshotBuildFailure(
                code, "decision request kind contradicts its action type"
            )
        expected_request_id = hashlib.sha256(
            (
                f"{project.get('project_id')}:{requested_revision}:{gate}:"
                f"{action_type}:{generation}:{subject}:{options}"
            ).encode("utf-8")
        ).hexdigest()[:24]
        if request_id != expected_request_id:
            raise _SnapshotBuildFailure(
                code, "decision request identity is not bound to its source fields"
            )
        bindings = {
            "request_id": request_id,
            "gate": gate,
            "generation": row.get("generation"),
            "kind": row.get("kind"),
            "type": row.get("action_type"),
            "requested_revision": row.get("requested_revision"),
            "subject_fingerprint": row.get("subject_fingerprint"),
            "options_fingerprint": row.get("options_fingerprint"),
        }
        if any(request.get(name) != expected for name, expected in bindings.items()):
            raise _SnapshotBuildFailure(code, "decision request JSON contradicts its columns")
        request_by_id[request_id] = row
    for index, row in enumerate(rows_by_table.get("workflow_decision_instances", ())):
        request = request_by_id.get(str(row.get("request_id")))
        decision = _parsed_json_object(
            row,
            "decision_json",
            code=code,
            context=f"workflow_decision_instances[{index}]",
        )
        if request is None or request.get("status") == "open":
            raise _SnapshotBuildFailure(code, "decision instance lacks a resolved request")
        if decision is None or decision.get("kind", row.get("kind")) != row.get("kind"):
            raise _SnapshotBuildFailure(code, "decision instance kind is inconsistent")
    pending = _parsed_json_object(
        project,
        "pending_action_json",
        code=code,
        context="project_state",
        allow_none=True,
    )
    open_rows = tuple(row for row in requests if row.get("status") == "open")
    if pending is None:
        if open_rows:
            raise _SnapshotBuildFailure(code, "open decision request lacks a pending project action")
        if project.get("status") in {"awaiting_selection", "awaiting_consultation"}:
            raise _SnapshotBuildFailure(
                code, "awaiting project state lacks a pending action"
            )
        return
    if project.get("status") not in {"awaiting_selection", "awaiting_consultation"}:
        raise _SnapshotBuildFailure(
            code, "pending action contradicts the project workflow status"
        )
    metadata = pending.get("metadata")
    request_identity = metadata.get("human_decision") if type(metadata) is dict else None
    if type(request_identity) is not dict:
        raise _SnapshotBuildFailure(code, "pending action lacks a bound decision request")
    request_id = request_identity.get("request_id")
    request = request_by_id.get(str(request_id))
    if request is None or request.get("status") != "open" or len(open_rows) != 1:
        raise _SnapshotBuildFailure(code, "pending action does not identify the unique open request")
    bindings = {
        "gate": request.get("gate_type"),
        "generation": request.get("generation"),
        "kind": request.get("kind"),
        "type": request.get("action_type"),
        "requested_revision": request.get("requested_revision"),
        "subject_fingerprint": request.get("subject_fingerprint"),
        "options_fingerprint": request.get("options_fingerprint"),
    }
    if pending.get("gate") != request.get("gate_type") or pending.get("type") != request.get("action_type"):
        raise _SnapshotBuildFailure(code, "pending action type or gate differs from its request")
    if any(request_identity.get(name) != expected for name, expected in bindings.items()):
        raise _SnapshotBuildFailure(code, "pending action request binding is inconsistent")


def _validate_projector_rows(
    rows_by_table: dict[str, tuple[dict[str, object], ...]],
    event_rows: tuple[dict[str, object], ...],
) -> None:
    code = SnapshotErrorCodeV0.PROJECTOR_CONTRACT_INVALID
    allowed = dict(_SOURCE_POLICY.projector_versions)
    events = {int(row["revision"]): row for row in event_rows}
    for index, row in enumerate(rows_by_table.get("projector_snapshots", ())):
        name = row.get("projector_name")
        version = row.get("projector_version")
        if type(name) is not str or allowed.get(name) != version:
            raise _SnapshotBuildFailure(code, f"projector row {index} has an unsupported name/version")
        snapshot = _parsed_json_object(
            row,
            "snapshot_json",
            code=code,
            context=f"projector_snapshots[{index}]",
        )
        assert snapshot is not None
        if row.get("state_hash") != _storage_canonical_sha256(snapshot):
            raise _SnapshotBuildFailure(code, f"projector row {index} state hash is unbound")
        through = row.get("through_revision")
        if type(through) is not int or through < 0:
            raise _SnapshotBuildFailure(code, f"projector row {index} revision is invalid")
        if through == 0:
            if any(row.get(name) is not None for name in ("through_event_id", "through_event_payload_sha256", "source_chain_root_sha256")):
                raise _SnapshotBuildFailure(code, "revision-zero projector claims an event binding")
            continue
        event = events.get(through)
        if event is None:
            raise _SnapshotBuildFailure(code, "projector references a missing event")
        payload = _parsed_json_object(
            event,
            "payload_json",
            code=code,
            context=f"events[{through}]",
        )
        assert payload is not None
        envelope = payload.get("_workflow")
        if type(envelope) is not dict or type(envelope.get("event_id")) is not str:
            raise _SnapshotBuildFailure(code, "projector source event lacks a versioned identity")
        payload_hash = _storage_canonical_sha256(payload)
        chain_root = _storage_canonical_sha256(
            [
                {
                    "revision": revision,
                    "sha256": _storage_canonical_sha256(
                        _parsed_json_object(
                            events[revision],
                            "payload_json",
                            code=code,
                            context=f"events[{revision}]",
                        )
                    ),
                }
                for revision in range(1, through + 1)
            ]
        )
        if (
            row.get("through_event_id") != envelope.get("event_id")
            or row.get("through_event_payload_sha256") != payload_hash
            or row.get("source_chain_root_sha256") != chain_root
        ):
            raise _SnapshotBuildFailure(code, "projector event-chain binding is inconsistent")
    for index, row in enumerate(rows_by_table.get("projection_failures", ())):
        if row.get("projector_name") not in allowed:
            raise _SnapshotBuildFailure(
                code, f"projection failure row {index} has an unsupported projector"
            )
        if row.get("status") not in {"pending", "resolved"}:
            raise _SnapshotBuildFailure(code, f"projection failure row {index} has an unsupported status")


def _validate_solver_rows(
    rows_by_table: dict[str, tuple[dict[str, object], ...]],
    event_rows: tuple[dict[str, object], ...],
) -> None:
    code = SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID
    lifecycle_events = dict(_SOURCE_POLICY.solver_lifecycle_events)
    receipt_events = dict(_SOURCE_POLICY.solver_receipt_events)
    lifecycle_statuses = frozenset(lifecycle_events.values())
    rows = rows_by_table.get("solver_jobs", ())
    rows_by_job: dict[str, dict[str, object]] = {}
    parsed_events: list[tuple[dict[str, object], str, dict[str, object]]] = []

    def plain_text(value: object, context: str, *, optional: bool = False) -> str | None:
        if value is None and optional:
            return None
        if type(value) is not str or not value or "\x00" in value:
            raise _SnapshotBuildFailure(code, f"{context} is not a non-empty plain string")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise _SnapshotBuildFailure(code, f"{context} is not valid UTF-8") from None
        return value

    def path_text(value: object, context: str, *, script: bool = False) -> str:
        text = plain_text(value, context)
        assert text is not None
        normalized = re.sub(r"\\", "/", text)
        if ".." in normalized.split("/") or (script and normalized.startswith("/")):
            raise _SnapshotBuildFailure(code, f"{context} is not a supported legacy row path")
        return text

    def canonical_sha(value: object, context: str) -> str:
        if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
            raise _SnapshotBuildFailure(code, f"{context} must be lowercase SHA-256")
        return value

    for index, row in enumerate(rows):
        job_id = _solver_job_id(
            row.get("job_id"), context=f"solver job row {index} identity"
        )
        if job_id in rows_by_job:
            raise _SnapshotBuildFailure(code, f"solver job {job_id} is duplicated")
        rows_by_job[job_id] = row
        plain_text(row.get("backend"), f"solver job {job_id} backend")
        plain_text(row.get("runtime"), f"solver job {job_id} runtime")
        path_text(row.get("script"), f"solver job {job_id} script", script=True)
        path_text(row.get("workdir"), f"solver job {job_id} workdir")
        plain_text(row.get("idempotency_key"), f"solver job {job_id} idempotency key", optional=True)
        plain_text(row.get("attempt_id"), f"solver job {job_id} attempt", optional=True)
        plain_text(row.get("external_id"), f"solver job {job_id} external id", optional=True)
        if row.get("request_sha256") is not None:
            canonical_sha(row.get("request_sha256"), f"solver job {job_id} request")
        if type(row.get("max_time_seconds")) is not int or int(row["max_time_seconds"]) <= 0:
            raise _SnapshotBuildFailure(code, f"solver job {job_id} timeout is invalid")
        for timestamp in ("requested_at", "started_at", "finished_at"):
            value = row.get(timestamp)
            if value is not None and (type(value) is not int or value < 0):
                raise _SnapshotBuildFailure(code, f"solver job {job_id} {timestamp} is invalid")
        raw_argv = row.get("argv_json")
        if type(raw_argv) is not str:
            raise _SnapshotBuildFailure(code, f"solver job {job_id} argv must be JSON text")
        try:
            argv = json.loads(raw_argv)
        except (TypeError, ValueError):
            raise _SnapshotBuildFailure(code, f"solver job {job_id} argv is invalid JSON") from None
        if type(argv) is not list:
            raise _SnapshotBuildFailure(code, f"solver job {job_id} argv must encode a list")
        for argument_index, argument in enumerate(argv):
            plain_text(argument, f"solver job {job_id} argv[{argument_index}]")
        result_refs = _parsed_json_object(
            row,
            "result_refs_json",
            code=code,
            context=f"solver_jobs[{index}]",
        )
        assert result_refs is not None
        if result_refs:
            raise _SnapshotBuildFailure(
                code,
                "solver result references are legacy-unbound because lifecycle events do not record them",
            )

    authorized_events = frozenset(lifecycle_events) | frozenset(receipt_events)
    for event in event_rows:
        event_type = event.get("type")
        if type(event_type) is not str or not event_type.startswith("SOLVER_JOB_"):
            continue
        if event_type not in authorized_events:
            raise _SnapshotBuildFailure(
                SnapshotErrorCodeV0.EVENT_CHAIN_INVALID,
                f"solver event {event_type} is not source-authorized",
            )
        payload = _parsed_json_object(
            event,
            "payload_json",
            code=code,
            context=f"events[{event.get('revision')}]",
        )
        assert payload is not None
        job_id = _solver_job_id(
            payload.get("job_id"), context=f"solver event {event_type} job identity"
        )
        if job_id not in rows_by_job:
            raise _SnapshotBuildFailure(
                code, f"solver event {event_type} references an unknown job"
            )
        parsed_events.append((event, event_type, payload))

    for index, row in enumerate(rows):
        job_id = str(row["job_id"])
        if type(row.get("job_revision")) is not int or row.get("job_revision", 0) < 1:
            raise _SnapshotBuildFailure(code, f"solver job row {index} has an invalid entity generation")
        if row.get("status") not in lifecycle_statuses:
            raise _SnapshotBuildFailure(code, f"solver job row {index} has an unsupported status")
        owner_stage = row.get("owner_stage")
        owner_subtask = row.get("owner_subtask")
        owner_revision = row.get("owner_revision")
        if owner_stage is None:
            if owner_subtask is not None or owner_revision is not None:
                raise _SnapshotBuildFailure(code, "solver owner fields are partially bound")
        else:
            if type(owner_stage) is not int or owner_stage not in _STAGE_IDS:
                raise _SnapshotBuildFailure(code, "solver owner Stage is not source-authorized")
            if owner_subtask is not None and (owner_stage, owner_subtask) not in _STAGE_SUBTASKS:
                raise _SnapshotBuildFailure(code, "solver owner subtask contradicts its Stage")
            if type(owner_revision) is not int or owner_revision < 0:
                raise _SnapshotBuildFailure(code, "solver owner revision is invalid")
        lifecycle: list[tuple[int, str, dict[str, object], int]] = []
        receipts: dict[str, dict[str, object]] = {}
        receipt_revisions: dict[str, int] = {}
        for event, event_type, payload in parsed_events:
            if payload.get("job_id") != job_id:
                continue
            lifecycle_status = lifecycle_events.get(event_type)
            receipt_stage = receipt_events.get(event_type)
            generation: object = payload.get("job_revision")
            if event_type == "SOLVER_JOB_SUBMITTED":
                generation = 1
                for field in (
                    "backend",
                    "runtime",
                    "max_time_seconds",
                    "idempotency_key",
                    "owner_stage",
                    "owner_subtask",
                    "owner_revision",
                    "attempt_id",
                ):
                    if payload.get(field) != row.get(field):
                        raise _SnapshotBuildFailure(
                            code, f"solver {field} contradicts its submission event"
                        )
            if lifecycle_status is not None:
                if type(generation) is not int or generation < 1:
                    raise _SnapshotBuildFailure(
                        code, "solver event generation is invalid"
                    )
                lifecycle.append(
                    (generation, lifecycle_status, payload, int(event["revision"]))
                )
            elif receipt_stage is not None:
                expected_keys = {
                    "_workflow",
                    "aggregate_root_hash_after",
                    "content_sha256",
                    "effect_hashes_after",
                    "job_id",
                    "receipt_path",
                    "receipt_sha256",
                    "request_sha256",
                    "stage",
                }
                if set(payload) != expected_keys:
                    raise _SnapshotBuildFailure(
                        code, f"solver receipt event {event_type} has an unsupported payload schema"
                    )
                if payload.get("stage") != receipt_stage:
                    raise _SnapshotBuildFailure(
                        code, f"solver receipt event {event_type} contradicts its stage"
                    )
                receipt_path = plain_text(
                    payload.get("receipt_path"), f"solver {receipt_stage} receipt path"
                )
                assert receipt_path is not None
                expected_receipt_path = (
                    f".factory/solver_receipts/{job_id}.{receipt_stage}.json"
                )
                _project_relative_posix_path(
                    receipt_path,
                    context=f"solver {receipt_stage} receipt path",
                    code=code,
                )
                match = _SOLVER_RECEIPT_PATTERN.fullmatch(receipt_path)
                if match is None or receipt_path != expected_receipt_path:
                    raise _SnapshotBuildFailure(
                        code, f"solver {receipt_stage} receipt path is not source-authorized"
                    )
                receipt = {
                    "job_id": job_id,
                    "stage": receipt_stage,
                    "receipt_path": receipt_path,
                    "receipt_sha256": canonical_sha(
                        payload.get("receipt_sha256"),
                        f"solver {receipt_stage} receipt",
                    ),
                    "content_sha256": canonical_sha(
                        payload.get("content_sha256"),
                        f"solver {receipt_stage} content",
                    ),
                    "request_sha256": canonical_sha(
                        payload.get("request_sha256"),
                        f"solver {receipt_stage} request",
                    ),
                }
                workflow_envelope = payload.get("_workflow")
                if (
                    type(workflow_envelope) is not dict
                    or workflow_envelope.get("canonical_type") != event_type
                    or type(workflow_envelope.get("side_effect_refs")) is not list
                    or workflow_envelope.get("side_effect_refs")
                    != [
                        {
                            "path": receipt_path,
                            "sha256": receipt["receipt_sha256"],
                            "kind": "receipt",
                        }
                    ]
                ):
                    raise _SnapshotBuildFailure(
                        code, f"solver receipt event {event_type} lacks its immutable event binding"
                    )
                prior = receipts.get(receipt_stage)
                if prior is not None and prior != receipt:
                    raise _SnapshotBuildFailure(
                        code, f"solver {receipt_stage} receipt has conflicting duplicates"
                    )
                receipts[receipt_stage] = receipt
                receipt_revisions.setdefault(receipt_stage, int(event["revision"]))
            if payload.get("request_sha256") is not None and row.get("request_sha256") not in {None, payload.get("request_sha256")}:
                raise _SnapshotBuildFailure(code, "solver request reference contradicts its event")
        if "completed" in receipts and "submitted" not in receipts:
            raise _SnapshotBuildFailure(code, "solver completed receipt lacks its submitted receipt")
        if (
            "completed" in receipt_revisions
            and receipt_revisions["completed"] <= receipt_revisions["submitted"]
        ):
            raise _SnapshotBuildFailure(code, "solver receipt event order is invalid")
        if "submitted" in receipts:
            submitted_request = receipts["submitted"]["request_sha256"]
            if submitted_request != row.get("request_sha256"):
                raise _SnapshotBuildFailure(code, "solver submitted receipt request contradicts its row")
            if (
                "completed" in receipts
                and receipts["completed"]["request_sha256"] != submitted_request
            ):
                raise _SnapshotBuildFailure(code, "solver completed receipt request contradicts submission")
        submission_revisions = tuple(
            revision
            for generation, status, _payload, revision in lifecycle
            if generation == 1 and status == "submitted"
        )
        if len(submission_revisions) != 1:
            raise _SnapshotBuildFailure(
                code, "solver job lacks one source-authorized submission event"
            )
        submission_revision = submission_revisions[0]
        if (
            "submitted" in receipt_revisions
            and receipt_revisions["submitted"] <= submission_revision
        ):
            raise _SnapshotBuildFailure(
                code, "solver submitted receipt does not follow its submission event"
            )
        terminal_statuses = {"completed", "failed", "timeout", "cancelled"}
        if "completed" in receipt_revisions:
            if row.get("status") not in terminal_statuses:
                raise _SnapshotBuildFailure(
                    code, "active solver job cannot publish a completed receipt"
                )
            terminal_revisions = tuple(
                revision
                for _generation, status, _payload, revision in lifecycle
                if status == row.get("status")
            )
            if (
                not terminal_revisions
                or receipt_revisions["completed"] <= terminal_revisions[-1]
            ):
                raise _SnapshotBuildFailure(
                    code, "solver completed receipt does not follow its terminal lifecycle event"
                )
        generations = tuple(
            generation for generation, _status, _payload, _revision in lifecycle
        )
        if generations != tuple(range(1, int(row["job_revision"]) + 1)):
            raise _SnapshotBuildFailure(code, "solver job generation differs from its event history")
        if int(row["job_revision"]) == 1:
            if row.get("status") not in _SOURCE_POLICY.solver_initial_statuses:
                raise _SnapshotBuildFailure(
                    code, "new solver job status contradicts its submission event"
                )
        elif lifecycle[-1][1] != row.get("status"):
            raise _SnapshotBuildFailure(
                code, "solver job status contradicts its latest lifecycle event"
            )
        latest_payload = lifecycle[-1][2]
        external_events = tuple(
            payload.get("external_id")
            for _generation, _status, payload, _revision in lifecycle
            if payload.get("external_id") is not None
        )
        if external_events and external_events[-1] != row.get("external_id"):
            raise _SnapshotBuildFailure(
                code, "solver external reference contradicts its latest non-empty event"
            )
        if row.get("external_id") is not None and not external_events:
            raise _SnapshotBuildFailure(code, "solver external reference has no lifecycle source")
        failure = _parsed_json_object(
            row,
            "failure_json",
            code=code,
            context=f"solver_jobs[{index}]",
            allow_none=True,
        )
        event_failure = latest_payload.get("failure") if lifecycle[-1][1] in terminal_statuses else None
        if event_failure is not None and type(event_failure) is not dict:
            raise _SnapshotBuildFailure(code, "solver terminal failure event is malformed")
        if failure != event_failure:
            raise _SnapshotBuildFailure(
                code, "solver failure reference contradicts its terminal lifecycle event"
            )


def _validate_immutable_ref_value(value: object, *, context: str) -> None:
    code = SnapshotErrorCodeV0.IMMUTABLE_REF_CONTRACT_INVALID
    if type(value) is list:
        for index, item in enumerate(value):
            _validate_immutable_ref_value(item, context=f"{context}[{index}]")
        return
    if type(value) is not dict:
        return
    for key, item in value.items():
        if type(key) is not str or not key:
            raise _SnapshotBuildFailure(code, f"{context} has an invalid immutable-ref key")
        normalized = key.lower()
        if normalized.endswith("_ref"):
            if type(item) is not dict:
                raise _SnapshotBuildFailure(code, f"{context}.{key} is not hash-pinned")
            locations = tuple(name for name in ("path", "url", "uri") if name in item)
            if (
                len(locations) != 1
                or type(item.get("sha256")) is not str
                or _SHA256_RE.fullmatch(str(item.get("sha256"))) is None
            ):
                raise _SnapshotBuildFailure(
                    code, f"{context}.{key} lacks one immutable location binding"
                )
        if normalized in {"path", "url", "uri"}:
            sha = value.get("sha256")
            if (
                type(item) is not str
                or not item
                or type(sha) is not str
                or _SHA256_RE.fullmatch(sha) is None
            ):
                raise _SnapshotBuildFailure(code, f"{context} contains an unpinned location")
            if normalized == "path":
                _project_relative_posix_path(
                    item,
                    context=f"{context}.{key}",
                    code=code,
                )
            else:
                try:
                    item.encode("utf-8", errors="strict")
                except UnicodeEncodeError:
                    raise _SnapshotBuildFailure(
                        code, f"{context}.{key} is not valid UTF-8"
                    ) from None
        _validate_immutable_ref_value(item, context=f"{context}.{key}")


def _validate_solver_result_refs(value: dict[str, object], *, context: str) -> None:
    code = SnapshotErrorCodeV0.IMMUTABLE_REF_CONTRACT_INVALID
    for key, item in value.items():
        if type(key) is not str or not key:
            raise _SnapshotBuildFailure(code, f"{context} has an invalid reference key")
        if type(item) is not dict:
            raise _SnapshotBuildFailure(code, f"{context}.{key} is an ordinary unpinned reference")
        locations = tuple(name for name in ("path", "url", "uri") if name in item)
        if (
            len(locations) != 1
            or type(item.get("sha256")) is not str
            or _SHA256_RE.fullmatch(str(item.get("sha256"))) is None
        ):
            raise _SnapshotBuildFailure(code, f"{context}.{key} is not hash-pinned")
        location = locations[0]
        location_value = item.get(location)
        if type(location_value) is not str or not location_value:
            raise _SnapshotBuildFailure(code, f"{context}.{key} has an invalid location")
        if location == "path":
            _project_relative_posix_path(
                location_value,
                context=f"{context}.{key}.path",
                code=code,
            )
        _validate_immutable_ref_value(item, context=f"{context}.{key}")


def _validate_immutable_rows(
    rows_by_table: dict[str, tuple[dict[str, object], ...]],
    _context: object = None,
) -> None:
    code = SnapshotErrorCodeV0.IMMUTABLE_REF_CONTRACT_INVALID
    for table, rows in rows_by_table.items():
        for index, row in enumerate(rows):
            for column in ("result_refs_json", "snapshot_json"):
                if row.get(column) is None:
                    continue
                parsed = _parsed_json_object(
                    row,
                    column,
                    code=code,
                    context=f"{table}[{index}]",
                )
                context = f"{table}[{index}].{column}"
                if column == "result_refs_json":
                    _validate_solver_result_refs(parsed, context=context)
                else:
                    _validate_immutable_ref_value(parsed, context=context)


def _fact(
    table: str,
    key: str,
    rows: tuple[dict[str, object], ...],
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    entity_generation: int | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    subject_sha256: str | None = None,
) -> SnapshotFactV0:
    legacy_values = {
        str(row["classifier_contract_sha256"])
        for row in rows
        if row.get("classifier_contract_sha256") is not None
    }
    legacy = next(iter(legacy_values)) if len(legacy_values) == 1 else None
    return SnapshotFactV0(
        fact_type=table,
        fact_key=key,
        value_sha256=canonical_sha256((table, rows)),
        entity_type=entity_type,
        entity_id=entity_id,
        entity_generation=entity_generation,
        subject_type=subject_type,
        subject_id=subject_id,
        subject_sha256=subject_sha256,
        legacy_classifier_contract_sha256=legacy,
    )


def _available_section(
    section_id: SnapshotSectionIdV0,
    coordinate: SnapshotCoordinateV0,
    facts: tuple[SnapshotFactV0, ...],
) -> SnapshotSectionV0:
    return SnapshotSectionV0(section_id, SnapshotAvailabilityV0.AVAILABLE, coordinate, tuple(sorted(facts, key=lambda item: (item.fact_type, item.fact_key))), None, None, None, None)


def _gap_section(
    section_id: SnapshotSectionIdV0,
    coordinate: SnapshotCoordinateV0,
    code: SnapshotErrorCodeV0,
    gap_id: str,
) -> SnapshotSectionV0:
    return SnapshotSectionV0(section_id, SnapshotAvailabilityV0.UNAVAILABLE_LEGACY_UNBOUND, coordinate, (), code, gap_id, None, None)


def _error_section(
    section_id: SnapshotSectionIdV0,
    coordinate: SnapshotCoordinateV0,
    code: SnapshotErrorCodeV0 = SnapshotErrorCodeV0.SECTION_READ_FAILED,
) -> SnapshotSectionV0:
    return SnapshotSectionV0(section_id, SnapshotAvailabilityV0.ERROR, coordinate, (), code, None, None, None)


def _paged_section(
    section_id: SnapshotSectionIdV0,
    coordinate: SnapshotCoordinateV0,
    page_cursor: str,
) -> SnapshotSectionV0:
    return SnapshotSectionV0(
        section_id,
        SnapshotAvailabilityV0.PAGED,
        coordinate,
        (),
        None,
        None,
        None,
        page_cursor,
    )


def _read_optional(
    connection: sqlite3.Connection,
    coordinate: SnapshotCoordinateV0,
    section_id: SnapshotSectionIdV0,
    tables: tuple[tuple[str, str], ...],
    *,
    validator=None,
    validator_context: object = None,
    derived_facts=None,
) -> SnapshotSectionV0:
    facts: list[SnapshotFactV0] = []
    rows_by_table: dict[str, tuple[dict[str, object], ...]] = {}
    try:
        for table, order in tables:
            if not _table_exists(connection, table):
                continue
            rows = _rows(connection, table, order)
            rows_by_table[table] = rows
            _validate_json_and_coordinate(
                rows,
                table=table,
                project_id=coordinate.project_id,
                project_revision=coordinate.project_revision,
            )
            facts.append(_fact(table, table, rows))
        if validator is not None:
            validator(rows_by_table, validator_context)
        if derived_facts is not None:
            facts.extend(derived_facts(rows_by_table, validator_context))
    except _SnapshotBuildFailure as exc:
        return _error_section(section_id, coordinate, exc.code)
    except sqlite3.Error:
        return _error_section(section_id, coordinate)
    if not facts:
        return _gap_section(
            section_id,
            coordinate,
            SnapshotErrorCodeV0.OPTIONAL_TABLE_UNAVAILABLE,
            f"legacy-table-unavailable:{section_id.value}",
        )
    return _available_section(section_id, coordinate, tuple(facts))


def _read_required_section(
    connection: sqlite3.Connection,
    coordinate: SnapshotCoordinateV0,
    section_id: SnapshotSectionIdV0,
    tables: tuple[tuple[str, str], ...],
    *,
    extra_facts: tuple[SnapshotFactV0, ...] = (),
    paged_tables: frozenset[str] = frozenset(),
    validator=None,
    validator_context: object = None,
    derived_facts=None,
) -> SnapshotSectionV0:
    facts: list[SnapshotFactV0] = list(extra_facts)
    rows_by_table: dict[str, tuple[dict[str, object], ...]] = {}
    for table, order in tables:
        rows = _rows(connection, table, order)
        rows_by_table[table] = rows
        _validate_json_and_coordinate(
            rows,
            table=table,
            project_id=coordinate.project_id,
            project_revision=coordinate.project_revision,
        )
        if table in paged_tables and len(rows) > SNAPSHOT_HISTORY_PAGE_LIMIT:
            return _paged_section(
                section_id,
                coordinate,
                canonical_sha256(
                    (
                        "snapshot-page-cursor-v0",
                        section_id.value,
                        table,
                        coordinate.project_revision,
                        SNAPSHOT_HISTORY_PAGE_LIMIT,
                        len(rows),
                    )
                ),
            )
        facts.append(_fact(table, table, rows))
    if validator is not None:
        validator(rows_by_table, validator_context)
    if derived_facts is not None:
        facts.extend(derived_facts(rows_by_table, validator_context))
    return _available_section(section_id, coordinate, tuple(facts))


def _stage_cursor_fact(project: dict[str, object]) -> SnapshotFactV0:
    source_step = project.get("source_step_id")
    if source_step is None:
        source_step = project.get("active_step")
    attempt = project.get("attempt")
    bound = type(source_step) is int and type(attempt) is int and attempt >= 1
    return _fact(
        "recorded_stage_cursor",
        "active",
        (
            {
                "active_stage": project.get("active_stage"),
                "active_subtask": project.get("active_subtask"),
                "active_step": project.get("active_step"),
                "source_step_id": project.get("source_step_id"),
                "attempt": attempt,
            },
        ),
        entity_type="workflow-step" if bound else None,
        entity_id=f"step:{source_step}" if bound else None,
        entity_generation=attempt if bound else None,
    )


def _pending_action_fact(project: dict[str, object]) -> SnapshotFactV0:
    pending = _parsed_json_object(
        project,
        "pending_action_json",
        code=SnapshotErrorCodeV0.PENDING_REQUEST_CONTRACT_INVALID,
        context="project_state",
        allow_none=True,
    )
    request = None
    if type(pending) is dict:
        metadata = pending.get("metadata")
        request = metadata.get("human_decision") if type(metadata) is dict else None
    subject_sha = request.get("subject_fingerprint") if type(request) is dict else None
    request_id = request.get("request_id") if type(request) is dict else None
    bound = (
        type(subject_sha) is str
        and _SHA256_RE.fullmatch(subject_sha) is not None
        and type(request_id) is str
        and bool(request_id)
    )
    return _fact(
        "recorded_pending_action",
        "active",
        ({"pending_action_json": project.get("pending_action_json")},),
        subject_type="pending-action" if bound else None,
        subject_id=request_id if bound else None,
        subject_sha256=subject_sha if bound else None,
    )


def _dirty_owner_facts(
    rows_by_table: dict[str, tuple[dict[str, object], ...]],
    _context: object,
) -> tuple[SnapshotFactV0, ...]:
    candidates = []
    for row in rows_by_table.get("dirty_flags", ()):
        artifact = row.get("cause_artifact")
        if type(artifact) is str and _SOLVER_RECEIPT_PATTERN.fullmatch(
            re.sub(r"\\", "/", artifact)
        ):
            candidates.append(row)
    if not candidates:
        return ()
    selected = max(
        candidates,
        key=lambda row: (
            int(row.get("cause_revision", 0)),
            str(row.get("cause_artifact", "")),
        ),
    )
    return (
        _fact(
            "solver_receipt_dirty_owner",
            "solver-receipt",
            (selected,),
            subject_type="solver-receipt",
            subject_id=str(selected["cause_artifact"]),
            subject_sha256=str(selected["current_fingerprint"]),
        ),
    )


def _solver_receipt_facts(
    rows_by_table: dict[str, tuple[dict[str, object], ...]],
    context: object,
) -> tuple[SnapshotFactV0, ...]:
    if type(context) is not tuple:
        return ()
    job_ids = {str(row["job_id"]) for row in rows_by_table.get("solver_jobs", ())}
    receipt_events = dict(_SOURCE_POLICY.solver_receipt_events)
    receipts: dict[tuple[str, str], dict[str, object]] = {}
    for event in context:
        if type(event) is not dict or event.get("type") not in receipt_events:
            continue
        payload = _parsed_json_object(
            event,
            "payload_json",
            code=SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID,
            context=f"events[{event.get('revision')}]",
        )
        assert payload is not None
        job_id = payload.get("job_id")
        stage = receipt_events[str(event["type"])]
        if job_id not in job_ids:
            continue
        receipt = {
            "job_id": job_id,
            "stage": stage,
            "receipt_ref": {
                "path": payload.get("receipt_path"),
                "sha256": payload.get("receipt_sha256"),
            },
            "content_sha256": payload.get("content_sha256"),
            "request_sha256": payload.get("request_sha256"),
        }
        _validate_immutable_ref_value(
            receipt, context=f"solver_receipt[{job_id}:{stage}]"
        )
        receipts[(str(job_id), stage)] = receipt
    return tuple(
        _fact(SOLVER_RECEIPT_FACT_TYPE, f"{job_id}:{stage}", (receipt,))
        for (job_id, stage), receipt in sorted(receipts.items())
    )


def _solver_snapshot_facts(
    rows_by_table: dict[str, tuple[dict[str, object], ...]],
    context: object,
) -> tuple[SnapshotFactV0, ...]:
    rows = rows_by_table.get("solver_jobs", ())
    bound: tuple[SnapshotFactV0, ...] = ()
    if len(rows) == 1:
        row = rows[0]
        bound = (
            _fact(
                "bound_solver_job",
                "bound",
                (row,),
                entity_type="solver-job",
                entity_id=str(row["job_id"]),
                entity_generation=int(row["job_revision"]),
            ),
        )
    return bound + _solver_receipt_facts(rows_by_table, context)


def _read_coordinate(connection: sqlite3.Connection, expected_project_id: str | None) -> tuple[SnapshotCoordinateV0, dict[str, object], tuple[dict[str, object], ...]]:
    schema_rows = _rows(connection, "schema_info", "singleton")
    if len(schema_rows) != 1 or schema_rows[0].get("singleton") != 1:
        raise _SnapshotBuildFailure(SnapshotErrorCodeV0.SCHEMA_INVALID, "schema_info must contain singleton=1")
    schema_version = schema_rows[0].get("schema_version")
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        raise _SnapshotBuildFailure(SnapshotErrorCodeV0.SCHEMA_INVALID, "workflow schema is not the current supported version")
    project_rows = _rows(connection, "project_state", "singleton")
    if len(project_rows) != 1 or project_rows[0].get("singleton") != 1:
        raise _SnapshotBuildFailure(SnapshotErrorCodeV0.PROJECT_STATE_INVALID, "project_state must contain exactly singleton=1")
    project = project_rows[0]
    project_id = project.get("project_id")
    revision = project.get("revision")
    if type(project_id) is not str or not project_id or type(revision) is not int or revision < 0:
        raise _SnapshotBuildFailure(SnapshotErrorCodeV0.PROJECT_STATE_INVALID, "project identity or revision is invalid")
    try:
        project_id.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise _SnapshotBuildFailure(SnapshotErrorCodeV0.PROJECT_STATE_INVALID, "project id is not valid UTF-8") from None
    if expected_project_id is not None and project_id != expected_project_id:
        raise _SnapshotBuildFailure(SnapshotErrorCodeV0.PROJECT_ID_MISMATCH, "database project id differs from requested project")
    runtime = project.get("runtime_generation")
    scheduler = project.get("scheduler_generation")
    if type(runtime) is not str or not runtime or type(scheduler) is not str or not scheduler:
        raise _SnapshotBuildFailure(SnapshotErrorCodeV0.PROJECT_STATE_INVALID, "runtime or scheduler generation is invalid")
    _validate_json_and_coordinate(project_rows, table="project_state", project_id=project_id, project_revision=revision)
    _validate_project_state_source(project)
    coordinate = SnapshotCoordinateV0(
        schema_version=SNAPSHOT_COORDINATE_SCHEMA,
        project_id=project_id,
        workflow_schema_version=schema_version,
        project_revision=revision,
        project_generation=None,
        run_generation=None,
        runtime_generation=runtime,
        scheduler_generation=scheduler,
        recorded_contract_pin_set_sha256=None,
    )
    return coordinate, project, schema_rows


def build_project_snapshot_v0(
    database_path: str | os.PathLike[str],
    *,
    expected_project_id: str | None = None,
) -> SnapshotBuildResult:
    """Build a non-authoritative snapshot from one read-only transaction."""

    if type(database_path) not in {str, Path}:
        return _unavailable_result(SnapshotErrorCodeV0.DB_PATH_NOT_REGULAR, "database path has an unsupported runtime type")
    if expected_project_id is not None:
        try:
            _text(expected_project_id, "expected_project_id")
        except SnapshotV0ValidationError:
            return _unavailable_result(SnapshotErrorCodeV0.PROJECT_ID_MISMATCH, "expected project id is invalid")
    path = Path(database_path)
    early = _lstat_regular_db(path)
    if early is not None:
        return early
    trace: list[str] = []
    connection: sqlite3.Connection | None = None
    try:
        uri = f"file:{quote(os.fspath(path), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.set_authorizer(_authorizer)
        connection.set_trace_callback(trace.append)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        coordinate, project, schema_rows = _read_coordinate(connection, expected_project_id)

        event_rows = _rows(connection, "events", "revision")
        _validate_json_and_coordinate(event_rows, table="events", project_id=coordinate.project_id, project_revision=coordinate.project_revision)
        revisions = tuple(row.get("revision") for row in event_rows)
        if coordinate.project_revision == 0:
            if revisions:
                raise _SnapshotBuildFailure(SnapshotErrorCodeV0.EVENT_CHAIN_INVALID, "revision zero must have no events")
        elif revisions != tuple(range(1, coordinate.project_revision + 1)):
            raise _SnapshotBuildFailure(SnapshotErrorCodeV0.EVENT_CHAIN_INVALID, "event revisions must be contiguous through project revision")
        required = (
            _fact("schema_info", "singleton:1", schema_rows),
            _fact("project_state", "project", (project,)),
        )
        sections: list[SnapshotSectionV0] = [
            _available_section(SnapshotSectionIdV0.PROJECT_STATE_SCHEMA, coordinate, required),
            _available_section(
                SnapshotSectionIdV0.EVENT_HEAD_CHAIN,
                coordinate,
                (_fact(EVENT_HEAD_FACT_TYPE, "project", event_rows),),
            ),
            _read_optional(
                connection,
                coordinate,
                SnapshotSectionIdV0.PROJECTOR_STATE,
                (("projector_snapshots", "projector_name"), ("projection_failures", "revision, projector_name")),
                validator=_validate_projector_rows,
                validator_context=event_rows,
            ),
            _read_required_section(
                connection,
                coordinate,
                SnapshotSectionIdV0.STAGE_CURSOR_CHECKPOINTS,
                (("stage_cursor_inputs", "singleton"), ("stage_checkpoints", "stage_id, subtask"), ("stage_checkpoint_history", "completed_revision, checkpoint_id")),
                extra_facts=(_stage_cursor_fact(project),),
                paged_tables=frozenset({"stage_checkpoint_history"}),
                validator=_validate_stage_rows,
                validator_context=project,
            ),
            _read_required_section(
                connection,
                coordinate,
                SnapshotSectionIdV0.DIRTY_FACTS,
                (("dirty_flags", "flag, owner_stage"), ("dirty_causes", "cause_revision, cause_id"), ("dirty_flag_clear_receipts", "revision, flag, owner_stage"), ("dirty_classifier_rebases", "created_at, rebase_id")),
                paged_tables=frozenset(
                    {"dirty_causes", "dirty_flag_clear_receipts", "dirty_classifier_rebases"}
                ),
                validator=_validate_dirty_rows,
                derived_facts=_dirty_owner_facts,
            ),
            _read_required_section(
                connection,
                coordinate,
                SnapshotSectionIdV0.PENDING_HUMAN,
                (("workflow_decisions", "gate"), ("workflow_decision_requests", "created_at, request_id"), ("workflow_decision_instances", "decided_at, decision_id")),
                extra_facts=(_pending_action_fact(project),),
                validator=_validate_pending_rows,
                validator_context=project,
            ),
            _read_required_section(
                connection,
                coordinate,
                SnapshotSectionIdV0.INVOCATIONS_SOLVER,
                (("solver_jobs", "requested_at, job_id"),),
                extra_facts=(
                    _fact(
                        "recorded_active_invocation",
                        "project_state",
                        (
                            {
                                "runner_pid": project.get("runner_pid"),
                                "runner_lease_id": project.get("runner_lease_id"),
                                "heartbeat_at": project.get("heartbeat_at"),
                                "status": project.get("status"),
                            },
                        ),
                    ),
                ),
                paged_tables=frozenset({"solver_jobs"}),
                validator=_validate_solver_rows,
                validator_context=event_rows,
                derived_facts=_solver_snapshot_facts,
            ),
            _available_section(
                SnapshotSectionIdV0.TERMINAL_STATUS,
                coordinate,
                (_fact("project_terminal_status", str(project.get("status")), (project,)),),
            ),
            _gap_section(
                SnapshotSectionIdV0.DELIVERY_AUTHORIZATION,
                coordinate,
                SnapshotErrorCodeV0.LEGACY_DELIVERY_AUTHORIZATION_UNBOUND,
                "legacy-database-has-no-delivery-authorization-generation-binding",
            ),
            _read_optional(
                connection,
                coordinate,
                SnapshotSectionIdV0.IMMUTABLE_REFS,
                (("solver_jobs", "requested_at, job_id"), ("projector_snapshots", "projector_name")),
                validator=_validate_immutable_rows,
                validator_context=event_rows,
                derived_facts=_solver_receipt_facts,
            ),
            _read_required_section(
                connection,
                coordinate,
                SnapshotSectionIdV0.CONFIG_POLICY,
                (("project_config", "singleton"), ("contest_policy", "singleton")),
            ),
            _gap_section(
                SnapshotSectionIdV0.GENERATION_BINDING,
                coordinate,
                SnapshotErrorCodeV0.LEGACY_PROJECT_GENERATION_UNBOUND,
                "legacy-database-has-no-project-or-run-generation-columns",
            ),
            _gap_section(
                SnapshotSectionIdV0.CONTRACT_PINS,
                coordinate,
                SnapshotErrorCodeV0.LEGACY_CONTRACT_PINS_UNBOUND,
                "legacy-database-has-no-recorded-m03-contract-pin-set",
            ),
        ]
        _validate_event_stream(connection, event_rows, project)
        final_coordinate, _final_project, _final_schema = _read_coordinate(connection, expected_project_id)
        if final_coordinate != coordinate:
            raise _SnapshotBuildFailure(SnapshotErrorCodeV0.COORDINATE_CHANGED, "coordinate changed within snapshot transaction")
        connection.execute("ROLLBACK")
        snapshot = validate_project_snapshot_v0(
            ProjectSnapshotV0(
                schema_version=PROJECT_SNAPSHOT_V0_SCHEMA,
                coordinate=coordinate,
                completeness=SnapshotCompletenessV0.PARTIAL,
                sections=tuple(sections),
                contract_pins=None,
                authoritative=False,
                performed_workflow_side_effects=(),
                application_initiated_write_operations=(),
            )
        )
        return validate_snapshot_build_result(
            SnapshotBuildResult(
                schema_version=SNAPSHOT_BUILD_RESULT_SCHEMA,
                availability=SnapshotAvailabilityV0.AVAILABLE,
                snapshot=snapshot,
                error_code=None,
                error_context=None,
                analysis_sql_trace=tuple(trace),
                authoritative=False,
                performed_workflow_side_effects=(),
                application_initiated_write_operations=(),
            )
        )
    except _SnapshotBuildFailure as exc:
        if connection is not None:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        return _unavailable_result(exc.code, exc.context, tuple(trace))
    except sqlite3.Error as exc:
        if connection is not None:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        extended_code = getattr(exc, "sqlite_errorcode", None)
        primary_code = (
            extended_code & 0xFF if type(extended_code) is int else None
        )
        auxiliary_failure = primary_code == sqlite3.SQLITE_READONLY or (
            primary_code == sqlite3.SQLITE_CANTOPEN
            and os.access(path, os.R_OK)
        )
        code = (
            SnapshotErrorCodeV0.SQLITE_WAL_AUXILIARY_UNAVAILABLE
            if auxiliary_failure
            else SnapshotErrorCodeV0.DB_OPEN_FAILED
        )
        context = (
            "SQLite runtime could not establish required WAL auxiliary files"
            if code is SnapshotErrorCodeV0.SQLITE_WAL_AUXILIARY_UNAVAILABLE
            else "read-only SQLite snapshot failed"
        )
        return _unavailable_result(code, context, tuple(trace))
    finally:
        if connection is not None:
            connection.close()
