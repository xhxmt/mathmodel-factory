"""Pure Stage-v1 readiness projection and shadow scheduling for M0.2.

This module is intentionally outside the production engine import graph.  It
accepts one already-recorded read snapshot, validates the M0.1 workflow
contract at its boundary, and returns immutable values only.  It never opens a
path or database, reads a clock or random source, dispatches work, or mutates
authoritative workflow state.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
import re
from types import MappingProxyType, UnionType
from typing import Any, Mapping, Sequence, get_args, get_origin, get_type_hints

from .canonical import canonical_bytes, canonical_sha256
from .dirty import DirtyFlag
from .domain import WorkflowStatus
from .workflow_contract import (
    WorkflowContractBundle,
    validate_workflow_contract_bundle,
    workflow_contract_analysis_bytes,
    workflow_contract_analysis_sha256,
    workflow_contract_bytes,
    workflow_contract_sha256,
)


RECORDED_SNAPSHOT_SCHEMA = "stage-v1-recorded-read-snapshot-v1"
READINESS_INPUT_SCHEMA = "stage-v1-readiness-input-v1"
READINESS_RESULT_SCHEMA = "stage-v1-readiness-result-v1"
TRANSITION_PLAN_SCHEMA = "shadow-transition-plan-v1"
PARITY_VALUE_SCHEMA = "stage-v1-shadow-parity-value-v2"
PARITY_RECEIPT_SCHEMA = "stage-v1-shadow-parity-receipt-v2"
SHADOW_SCHEDULER_ENABLED_BY_DEFAULT = False

SUPPORTED_WORKFLOW_STATUSES = frozenset(status.value for status in WorkflowStatus)
SUPPORTED_DIRTY_FLAGS = frozenset(flag.value for flag in DirtyFlag)
SUPPORTED_ACTIVE_INVOCATION_STATES_BY_TYPE: Mapping[str, frozenset[str]] = (
    MappingProxyType(
        {
            "recovery": frozenset({"interrupted", "recovering"}),
            "solver": frozenset(
                {
                    "submitted",
                    "queued",
                    "submitting",
                    "running",
                    "cancelling",
                    "interrupted",
                }
            ),
        }
    )
)
SUPPORTED_ACTIVE_INVOCATION_TYPES = frozenset(
    SUPPORTED_ACTIVE_INVOCATION_STATES_BY_TYPE
)
SUPPORTED_DOMAIN_READINESS_STATES = frozenset({"READY", "WAITING", "BLOCKED"})
SUPPORTED_FACT_AVAILABILITY = frozenset(
    {"AVAILABLE", "UNAVAILABLE_LEGACY_UNBOUND"}
)
SUPPORTED_DELIVERY_CAPABILITIES = frozenset(
    {"contest-delivery-eligible", "technical-no-delivery"}
)
WORKFLOW_STATUS_PLAN_DISPOSITIONS: Mapping[str, str] = MappingProxyType(
    {
        "ready": "RUNNABLE",
        "running": "RUNNABLE",
        "retrying": "RUNNABLE",
        "awaiting_selection": "WAIT",
        "awaiting_consultation": "WAIT",
        "paused": "WAIT",
        "failed": "WAIT",
        "archiving": "WAIT",
        "interrupted": "WAIT",
        "completed": "TERMINAL_ONLY",
        "killed": "TERMINAL_ONLY",
    }
)
if frozenset(WORKFLOW_STATUS_PLAN_DISPOSITIONS) != SUPPORTED_WORKFLOW_STATUSES:
    raise RuntimeError("WorkflowStatus scheduling classification is not exhaustive")
_TERMINAL_WORKFLOW_STATUSES = frozenset({"completed", "failed", "killed"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class SnapshotValidationError(ValueError):
    """Raised when an allegedly recorded snapshot is incomplete or inconsistent."""


class TransitionAction(str, Enum):
    STAY = "STAY"
    ADVANCE = "ADVANCE"
    WAIT = "WAIT"
    DISPATCH = "DISPATCH"
    REOPEN = "REOPEN"
    TERMINATE = "TERMINATE"


class ReadinessState(str, Enum):
    READY = "READY"
    WAITING = "WAITING"
    BLOCKED = "BLOCKED"
    TERMINAL = "TERMINAL"


class ParityStatus(str, Enum):
    MATCH = "MATCH"
    EXPECTED_CORRECTION = "EXPECTED_CORRECTION"
    UNEXPLAINED_DIFFERENCE = "UNEXPLAINED_DIFFERENCE"
    V2_ERROR = "V2_ERROR"
    V1_UNREPRESENTABLE = "V1_UNREPRESENTABLE"


@dataclass(frozen=True)
class ContractIdentities:
    semantic_sha256: str
    semantic_bytes: int
    analysis_sha256: str
    analysis_bytes: int
    owner_resolution_mode: str


@dataclass(frozen=True)
class ScheduleCoordinate:
    stage_id: int
    subtask: str
    source_step_id: int


@dataclass(frozen=True)
class ScheduleEntry:
    coordinate: ScheduleCoordinate
    checkpoint_step_id: int | None
    conditional: bool
    kind: str


@dataclass(frozen=True)
class StageCursor:
    coordinate: ScheduleCoordinate
    active_step: int
    attempt: int


@dataclass(frozen=True)
class CheckpointHead:
    coordinate: ScheduleCoordinate
    checkpoint_step_id: int | None
    completed_revision: int
    receipt_sha256: str


@dataclass(frozen=True)
class DirtyRef:
    flag: str
    owner_stage: int
    cause_ref: str


@dataclass(frozen=True)
class PendingActionRef:
    action_type: str
    gate: str | None
    request_ref: str
    generation: int
    coordinate: ScheduleCoordinate | None


@dataclass(frozen=True)
class ActiveInvocationRef:
    invocation_id: str
    invocation_type: str
    state: str
    coordinate: ScheduleCoordinate | None


@dataclass(frozen=True)
class TerminalDeliveryFacts:
    is_terminal: bool
    terminal_reason: str | None
    delivery_capability: str
    delivery_allowed: bool


@dataclass(frozen=True)
class DomainReadinessFact:
    domain: str
    state: str
    action_hint: TransitionAction | None
    target: ScheduleCoordinate | None
    reason_code: str
    evidence_ref: str


@dataclass(frozen=True)
class ImmutableRef:
    kind: str
    ref: str
    sha256: str
    behavior_binding: bool


@dataclass(frozen=True)
class FactAvailability:
    fact: str
    availability: str
    required_for_plan: bool
    gap_id: str | None


@dataclass(frozen=True)
class ReadinessInput:
    schema_version: str
    contract: ContractIdentities
    project_id: str
    project_revision: int
    run_generation: str
    runtime_generation: str
    scheduler_generation: str
    workflow_status: str
    last_completed_step: int
    last_completed_stage: int
    attempt: int
    stage_cursor: StageCursor | None
    catalog: tuple[ScheduleEntry, ...]
    semantic_dirty_flags: tuple[str, ...]
    checkpoint_heads: tuple[CheckpointHead, ...]
    dirty_refs: tuple[DirtyRef, ...]
    pending_action_refs: tuple[PendingActionRef, ...]
    active_invocation_refs: tuple[ActiveInvocationRef, ...]
    terminal_delivery: TerminalDeliveryFacts
    domain_readiness: tuple[DomainReadinessFact, ...]
    immutable_refs: tuple[ImmutableRef, ...]
    availability: tuple[FactAvailability, ...]


@dataclass(frozen=True)
class ReadinessResult:
    schema_version: str
    contract: ContractIdentities
    project_revision: int
    run_generation: str
    state: ReadinessState
    reason_codes: tuple[str, ...]
    blockers: tuple[str, ...]
    target: ScheduleCoordinate | None
    input_semantic_sha256: str
    input_analysis_sha256: str


@dataclass(frozen=True)
class TransitionPlan:
    schema_version: str
    contract: ContractIdentities
    base_revision: int
    run_generation: str
    authoritative: bool
    action: TransitionAction
    current: ScheduleCoordinate | None
    target: ScheduleCoordinate | None
    execution_route: str | None
    reason_code: str
    readiness_semantic_sha256: str
    readiness_analysis_sha256: str
    read_set_sha256: str
    proposed_mutations: tuple[str, ...]
    performed_side_effects: tuple[str, ...]


@dataclass(frozen=True)
class SchedulerDecision:
    readiness_input: ReadinessInput
    readiness: ReadinessResult
    plan: TransitionPlan


@dataclass(frozen=True)
class ParityValue:
    schema_version: str = field(default=PARITY_VALUE_SCHEMA, init=False)
    action: TransitionAction
    coordinate: ScheduleCoordinate | None
    execution_route: str | None = None


@dataclass(frozen=True)
class ExpectedCorrection:
    issue_id: str
    fixture_id: str
    expected_v1: ParityValue
    expected_v2: ParityValue
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class ParityReceipt:
    schema_version: str
    contract: ContractIdentities
    fixture_id: str
    status: ParityStatus
    v1: ParityValue | None
    v2: ParityValue | None
    issue_id: str | None
    evidence_refs: tuple[str, ...]
    gap_ids: tuple[str, ...]
    readiness_semantic_sha256: str | None
    readiness_analysis_sha256: str | None
    plan_semantic_sha256: str | None
    plan_analysis_sha256: str | None
    error_code: str | None


def _validated_shadow_bundle(bundle: WorkflowContractBundle) -> WorkflowContractBundle:
    """Validate source-rooted workflow behavior before trusting the bundle."""

    validated = validate_workflow_contract_bundle(bundle)
    semantic_flags = tuple(validated.classifier.semantic_dirty_flags)
    if len(set(semantic_flags)) != len(semantic_flags):
        raise SnapshotValidationError(
            "workflow contract semantic dirty flags contain duplicates"
        )
    if any(flag not in SUPPORTED_DIRTY_FLAGS for flag in semantic_flags):
        raise SnapshotValidationError(
            "workflow contract semantic dirty flags are unsupported"
        )
    expected_dirty_flags = tuple(flag.value for flag in DirtyFlag)
    if validated.classifier.dirty_flags != expected_dirty_flags:
        raise SnapshotValidationError(
            "workflow contract dirty flags differ from shadow source order"
        )
    step13_operands = tuple(
        subtask.condition.operands
        for stage in validated.stages
        for subtask in stage.subtasks
        if stage.stage_id == 8
        and subtask.key == "conditional_math_preflight"
        and subtask.source_step_id == 13
    )
    if step13_operands != (semantic_flags,):
        raise SnapshotValidationError(
            "validated Step 13 operands differ from classifier semantic flags"
        )
    return validated


def _contract_identities(bundle: WorkflowContractBundle) -> ContractIdentities:
    validated = _validated_shadow_bundle(bundle)
    semantic_value = workflow_contract_bytes(validated)
    analysis_value = workflow_contract_analysis_bytes(validated)
    return ContractIdentities(
        semantic_sha256=workflow_contract_sha256(validated),
        semantic_bytes=len(semantic_value),
        analysis_sha256=workflow_contract_analysis_sha256(validated),
        analysis_bytes=len(analysis_value),
        owner_resolution_mode=validated.owner_compilation.mode,
    )


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if type(value) is not dict:
        raise SnapshotValidationError(f"{field} must be a plain mapping")
    if not all(type(key) is str for key in value):
        raise SnapshotValidationError(f"{field} keys must be strings")
    for key in value:
        _text(key, f"{field} key")
    return value


def _sequence(value: object, field: str) -> Sequence[object]:
    if type(value) not in {list, tuple}:
        raise SnapshotValidationError(f"{field} must be a plain list or tuple")
    return value


def _required(value: Mapping[str, object], field: str) -> object:
    if field not in value:
        raise SnapshotValidationError(f"snapshot is missing {field}")
    return value[field]


def _text(value: object, field: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value):
        raise SnapshotValidationError(f"{field} must be a non-empty string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SnapshotValidationError(
            f"{field} must contain valid UTF-8 scalar values"
        ) from exc
    return value


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _integer(value: object, field: str, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise SnapshotValidationError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise SnapshotValidationError(f"{field} must be >= {minimum}")
    return value


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise SnapshotValidationError(f"{field} must be a boolean")
    return value


def _choice(value: object, field: str, supported: frozenset[str]) -> str:
    selected = _text(value, field)
    if selected not in supported:
        raise SnapshotValidationError(f"{field} has unsupported value {selected!r}")
    return selected


def _sha256(value: object, field: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise SnapshotValidationError(
            f"{field} must be exactly 64 lowercase hexadecimal characters"
        )
    return value


def _optional_sha256(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _sha256(value, field)


def _validate_terminal_delivery_consistency(
    workflow_status: str,
    facts: TerminalDeliveryFacts,
) -> None:
    if facts.is_terminal and workflow_status not in _TERMINAL_WORKFLOW_STATUSES:
        raise SnapshotValidationError(
            "terminal_delivery.is_terminal conflicts with workflow_status"
        )
    if workflow_status in {"completed", "killed"} and not facts.is_terminal:
        raise SnapshotValidationError(
            "terminal workflow_status requires terminal_delivery.is_terminal"
        )
    if facts.is_terminal and facts.terminal_reason is None:
        raise SnapshotValidationError("terminal fact requires terminal_reason")
    if not facts.is_terminal and facts.terminal_reason is not None:
        raise SnapshotValidationError(
            "nonterminal fact cannot declare terminal_reason"
        )
    if facts.delivery_allowed and not facts.is_terminal:
        raise SnapshotValidationError(
            "delivery_allowed requires an explicit terminal fact"
        )
    if facts.delivery_allowed and workflow_status != "completed":
        raise SnapshotValidationError(
            "delivery_allowed requires completed workflow_status"
        )
    if (
        facts.delivery_allowed
        and facts.delivery_capability != "contest-delivery-eligible"
    ):
        raise SnapshotValidationError(
            "delivery_allowed conflicts with delivery_capability"
        )
    if (
        facts.delivery_capability == "technical-no-delivery"
        and facts.delivery_allowed
    ):
        raise SnapshotValidationError(
            "technical terminal cannot declare delivery permission"
        )


def _validate_domain_fact(field: str, fact: DomainReadinessFact) -> None:
    if fact.state in {"WAITING", "BLOCKED"} and fact.action_hint not in {
        None,
        TransitionAction.WAIT,
    }:
        raise SnapshotValidationError(
            f"{field} blocked/waiting state cannot request an active transition"
        )
    if fact.action_hint is TransitionAction.TERMINATE:
        raise SnapshotValidationError(
            f"{field} terminal action requires terminal_delivery facts"
        )
    if fact.state == "READY" and fact.action_hint is TransitionAction.WAIT:
        raise SnapshotValidationError(
            f"{field} ready state cannot request a waiting transition"
        )
    if fact.action_hint in {
        TransitionAction.ADVANCE,
        TransitionAction.DISPATCH,
        TransitionAction.REOPEN,
    } and fact.target is None:
        raise SnapshotValidationError(f"{field} active transition requires target")


def _validate_readiness_behavior_vocabulary(value: ReadinessInput) -> None:
    _choice(value.workflow_status, "workflow_status", SUPPORTED_WORKFLOW_STATUSES)
    for index, flag in enumerate(value.semantic_dirty_flags):
        _choice(
            flag,
            f"semantic_dirty_flags[{index}]",
            SUPPORTED_DIRTY_FLAGS,
        )
    for index, item in enumerate(value.dirty_refs):
        _choice(item.flag, f"dirty_refs[{index}].flag", SUPPORTED_DIRTY_FLAGS)
    for index, item in enumerate(value.active_invocation_refs):
        field = f"active_invocation_refs[{index}]"
        invocation_type = _choice(
            item.invocation_type,
            f"{field}.invocation_type",
            SUPPORTED_ACTIVE_INVOCATION_TYPES,
        )
        _choice(
            item.state,
            f"{field}.state",
            SUPPORTED_ACTIVE_INVOCATION_STATES_BY_TYPE[invocation_type],
        )
    for index, item in enumerate(value.domain_readiness):
        field = f"domain_readiness[{index}]"
        _choice(item.state, f"{field}.state", SUPPORTED_DOMAIN_READINESS_STATES)
        if item.action_hint is not None:
            _enum_member(item.action_hint, TransitionAction, f"{field}.action_hint")
        _validate_domain_fact(field, item)
    for index, item in enumerate(value.availability):
        _choice(
            item.availability,
            f"availability[{index}].availability",
            SUPPORTED_FACT_AVAILABILITY,
        )
    _choice(
        value.terminal_delivery.delivery_capability,
        "terminal_delivery.delivery_capability",
        SUPPORTED_DELIVERY_CAPABILITIES,
    )
    _validate_terminal_delivery_consistency(
        value.workflow_status, value.terminal_delivery
    )


def _coordinate(value: object, field: str) -> ScheduleCoordinate:
    item = _mapping(value, field)
    return ScheduleCoordinate(
        stage_id=_integer(_required(item, "stage_id"), f"{field}.stage_id", minimum=1),
        subtask=_text(_required(item, "subtask"), f"{field}.subtask"),
        source_step_id=_integer(
            _required(item, "source_step_id"), f"{field}.source_step_id", minimum=0
        ),
    )


def _optional_coordinate(value: object, field: str) -> ScheduleCoordinate | None:
    return None if value is None else _coordinate(value, field)


def _catalog(bundle: WorkflowContractBundle) -> tuple[ScheduleEntry, ...]:
    return tuple(
        ScheduleEntry(
            coordinate=ScheduleCoordinate(
                stage_id=stage.stage_id,
                subtask=subtask.key,
                source_step_id=subtask.source_step_id,
            ),
            checkpoint_step_id=subtask.checkpoint_step_id,
            conditional=subtask.condition.operator != "ALWAYS",
            kind=subtask.kind,
        )
        for stage in bundle.stages
        for subtask in stage.subtasks
    )


def _assert_known_coordinate(
    coordinate: ScheduleCoordinate | None,
    known: frozenset[ScheduleCoordinate],
    field: str,
) -> None:
    if coordinate is not None and coordinate not in known:
        raise SnapshotValidationError(f"{field} is outside the validated Stage catalog")


def _tuple(value: object, field: str) -> tuple[Any, ...]:
    if type(value) is not tuple:
        raise SnapshotValidationError(f"{field} must be an immutable tuple")
    return value


def _instance(value: object, expected: type, field: str) -> None:
    if type(value) is not expected:
        raise SnapshotValidationError(f"{field} has an unsupported runtime type")
    if is_dataclass(expected):
        for item in fields(expected):
            try:
                object.__getattribute__(value, item.name)
            except AttributeError as exc:
                raise SnapshotValidationError(
                    f"{field}.{item.name} is missing"
                ) from exc


def _enum_member(value: object, expected: type[Enum], field: str) -> None:
    """Require identity with a declared member, not merely an Enum runtime type."""

    if type(value) is not expected or not any(value is member for member in expected):
        raise SnapshotValidationError(f"{field} is not a registered enum member")


def _validate_runtime_annotation(
    value: object,
    expected: object,
    field: str,
) -> None:
    """Validate one frozen DTO field without authorizing its behavior value."""

    origin = get_origin(expected)
    arguments = get_args(expected)
    if origin is UnionType:
        if value is None and type(None) in arguments:
            return
        remaining = tuple(item for item in arguments if item is not type(None))
        if len(remaining) != 1:
            raise SnapshotValidationError(f"{field} has an unsupported runtime schema")
        _validate_runtime_annotation(value, remaining[0], field)
        return
    if origin is tuple:
        values = _tuple(value, field)
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            for index, item in enumerate(values):
                _validate_runtime_annotation(item, arguments[0], f"{field}[{index}]")
            return
        if len(values) != len(arguments):
            raise SnapshotValidationError(f"{field} has an unsupported tuple shape")
        for index, (item, annotation) in enumerate(zip(values, arguments, strict=True)):
            _validate_runtime_annotation(item, annotation, f"{field}[{index}]")
        return
    if expected is str:
        if type(value) is not str:
            raise SnapshotValidationError(f"{field} must be a string")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise SnapshotValidationError(
                f"{field} must contain valid UTF-8 scalar values"
            ) from exc
        return
    if expected is int:
        if type(value) is not int:
            raise SnapshotValidationError(f"{field} must be an integer")
        return
    if expected is bool:
        if type(value) is not bool:
            raise SnapshotValidationError(f"{field} must be a boolean")
        return
    if expected is type(None):
        if value is not None:
            raise SnapshotValidationError(f"{field} must be None")
        return
    if isinstance(expected, type) and issubclass(expected, Enum):
        _enum_member(value, expected, field)
        return
    if isinstance(expected, type) and is_dataclass(expected):
        _instance(value, expected, field)
        hints = get_type_hints(expected)
        for item in fields(expected):
            _validate_runtime_annotation(
                object.__getattribute__(value, item.name),
                hints[item.name],
                f"{field}.{item.name}",
            )
        return
    raise SnapshotValidationError(f"{field} has an unsupported runtime schema")


def _validate_shadow_runtime_structure(
    value: object,
    expected: type,
    field: str,
) -> None:
    _validate_runtime_annotation(value, expected, field)


def _validate_coordinate_value(value: object, field: str) -> ScheduleCoordinate:
    _instance(value, ScheduleCoordinate, field)
    coordinate = value
    assert type(coordinate) is ScheduleCoordinate
    _integer(coordinate.stage_id, f"{field}.stage_id", minimum=1)
    _text(coordinate.subtask, f"{field}.subtask")
    _integer(coordinate.source_step_id, f"{field}.source_step_id", minimum=0)
    return coordinate


def _validate_contract_identities_value(
    value: object,
    field: str,
) -> ContractIdentities:
    _instance(value, ContractIdentities, field)
    contract = value
    assert type(contract) is ContractIdentities
    _sha256(contract.semantic_sha256, f"{field}.semantic_sha256")
    _integer(contract.semantic_bytes, f"{field}.semantic_bytes", minimum=0)
    _sha256(contract.analysis_sha256, f"{field}.analysis_sha256")
    _integer(contract.analysis_bytes, f"{field}.analysis_bytes", minimum=0)
    _text(contract.owner_resolution_mode, f"{field}.owner_resolution_mode")
    return contract


def _validate_schedule_entry_value(value: object, field: str) -> ScheduleEntry:
    _instance(value, ScheduleEntry, field)
    entry = value
    assert type(entry) is ScheduleEntry
    _validate_coordinate_value(entry.coordinate, f"{field}.coordinate")
    if entry.checkpoint_step_id is not None:
        _integer(
            entry.checkpoint_step_id,
            f"{field}.checkpoint_step_id",
            minimum=0,
        )
    _boolean(entry.conditional, f"{field}.conditional")
    _text(entry.kind, f"{field}.kind")
    return entry


def _validate_readiness_input(
    bundle: WorkflowContractBundle,
    value: ReadinessInput,
) -> None:
    """Revalidate every behavior-bearing DTO field against trusted source facts."""

    _instance(value, ReadinessInput, "readiness_input")
    _text(value.schema_version, "schema_version")
    if value.schema_version != READINESS_INPUT_SCHEMA:
        raise SnapshotValidationError("unsupported readiness input schema")
    expected_contract = _contract_identities(bundle)
    _validate_contract_identities_value(value.contract, "contract")
    if value.contract != expected_contract:
        raise SnapshotValidationError(
            "readiness input contract identity does not match trusted bundle"
        )

    _text(value.project_id, "project_id")
    _integer(value.project_revision, "project_revision", minimum=0)
    _text(value.run_generation, "run_generation")
    _text(value.runtime_generation, "runtime_generation")
    _text(value.scheduler_generation, "scheduler_generation")
    if value.runtime_generation != bundle.runtime_generation:
        raise SnapshotValidationError(
            "readiness runtime generation does not match trusted bundle"
        )
    if value.scheduler_generation != bundle.scheduler_generation:
        raise SnapshotValidationError(
            "readiness scheduler generation does not match trusted bundle"
        )
    _choice(value.workflow_status, "workflow_status", SUPPORTED_WORKFLOW_STATUSES)

    trusted_catalog = _catalog(bundle)
    catalog = _tuple(value.catalog, "catalog")
    for index, entry in enumerate(catalog):
        _validate_schedule_entry_value(entry, f"catalog[{index}]")
    if catalog != trusted_catalog:
        raise SnapshotValidationError(
            "readiness catalog does not exactly match trusted workflow contract"
        )
    known = frozenset(item.coordinate for item in trusted_catalog)
    entry_by_coordinate = {item.coordinate: item for item in trusted_catalog}
    known_stages = frozenset(item.coordinate.stage_id for item in trusted_catalog)
    max_step = max(item.coordinate.source_step_id for item in trusted_catalog)
    max_stage = max(known_stages)
    last_completed_step = _integer(value.last_completed_step, "last_completed_step")
    if last_completed_step < -1 or last_completed_step > max_step:
        raise SnapshotValidationError("last_completed_step is outside the Stage catalog")
    last_completed_stage = _integer(
        value.last_completed_stage, "last_completed_stage", minimum=0
    )
    if last_completed_stage > max_stage:
        raise SnapshotValidationError("last_completed_stage is outside the Stage catalog")
    _integer(value.attempt, "attempt", minimum=0)

    if value.stage_cursor is not None:
        _instance(value.stage_cursor, StageCursor, "stage_cursor")
        cursor = value.stage_cursor
        coordinate = _validate_coordinate_value(cursor.coordinate, "stage_cursor.coordinate")
        _assert_known_coordinate(coordinate, known, "stage_cursor.coordinate")
        _integer(cursor.active_step, "stage_cursor.active_step", minimum=0)
        _integer(cursor.attempt, "stage_cursor.attempt", minimum=0)
        if cursor.active_step != coordinate.source_step_id:
            raise SnapshotValidationError("stage cursor active_step is not atomic")
        if cursor.attempt != value.attempt:
            raise SnapshotValidationError(
                "readiness attempt disagrees with atomic Stage cursor"
            )

    semantic_flags = _tuple(value.semantic_dirty_flags, "semantic_dirty_flags")
    for index, flag in enumerate(semantic_flags):
        _text(flag, f"semantic_dirty_flags[{index}]")
    if semantic_flags != tuple(bundle.classifier.semantic_dirty_flags):
        raise SnapshotValidationError(
            "readiness semantic dirty flags do not match trusted classifier"
        )

    checkpoints = _tuple(value.checkpoint_heads, "checkpoint_heads")
    checkpoint_coordinates = []
    for index, checkpoint in enumerate(checkpoints):
        field_name = f"checkpoint_heads[{index}]"
        _instance(checkpoint, CheckpointHead, field_name)
        coordinate = _validate_coordinate_value(
            checkpoint.coordinate, f"{field_name}.coordinate"
        )
        _assert_known_coordinate(coordinate, known, f"{field_name}.coordinate")
        expected_step = entry_by_coordinate[coordinate].checkpoint_step_id
        if checkpoint.checkpoint_step_id is not None:
            _integer(
                checkpoint.checkpoint_step_id,
                f"{field_name}.checkpoint_step_id",
                minimum=0,
            )
        if checkpoint.checkpoint_step_id != expected_step:
            raise SnapshotValidationError(
                f"{field_name}.checkpoint_step_id conflicts with trusted catalog"
            )
        revision = _integer(
            checkpoint.completed_revision,
            f"{field_name}.completed_revision",
            minimum=0,
        )
        if revision > value.project_revision:
            raise SnapshotValidationError(
                f"{field_name}.completed_revision exceeds project_revision"
            )
        _sha256(checkpoint.receipt_sha256, f"{field_name}.receipt_sha256")
        checkpoint_coordinates.append(coordinate)
    if len(set(checkpoint_coordinates)) != len(checkpoint_coordinates):
        raise SnapshotValidationError("checkpoint heads contain duplicate coordinates")
    if checkpoints != tuple(
        sorted(checkpoints, key=lambda item: (item.coordinate.stage_id, item.coordinate.subtask))
    ):
        raise SnapshotValidationError("checkpoint heads are not canonically ordered")

    dirty_refs = _tuple(value.dirty_refs, "dirty_refs")
    for index, item in enumerate(dirty_refs):
        field_name = f"dirty_refs[{index}]"
        _instance(item, DirtyRef, field_name)
        _choice(item.flag, f"{field_name}.flag", SUPPORTED_DIRTY_FLAGS)
        owner_stage = _integer(item.owner_stage, f"{field_name}.owner_stage", minimum=1)
        if owner_stage not in known_stages:
            raise SnapshotValidationError(
                f"{field_name}.owner_stage is outside the trusted Stage catalog"
            )
        _text(item.cause_ref, f"{field_name}.cause_ref")
    if dirty_refs != tuple(
        sorted(dirty_refs, key=lambda item: (item.flag, item.owner_stage, item.cause_ref))
    ):
        raise SnapshotValidationError("dirty refs are not canonically ordered")

    pending_refs = _tuple(value.pending_action_refs, "pending_action_refs")
    for index, item in enumerate(pending_refs):
        field_name = f"pending_action_refs[{index}]"
        _instance(item, PendingActionRef, field_name)
        _text(item.action_type, f"{field_name}.action_type")
        _optional_text(item.gate, f"{field_name}.gate")
        _text(item.request_ref, f"{field_name}.request_ref")
        _integer(item.generation, f"{field_name}.generation", minimum=0)
        if item.coordinate is not None:
            _validate_coordinate_value(item.coordinate, f"{field_name}.coordinate")
        _assert_known_coordinate(item.coordinate, known, f"{field_name}.coordinate")
    if pending_refs != tuple(
        sorted(pending_refs, key=lambda item: (item.generation, item.action_type, item.request_ref))
    ):
        raise SnapshotValidationError("pending action refs are not canonically ordered")

    invocation_refs = _tuple(value.active_invocation_refs, "active_invocation_refs")
    for index, item in enumerate(invocation_refs):
        field_name = f"active_invocation_refs[{index}]"
        _instance(item, ActiveInvocationRef, field_name)
        _text(item.invocation_id, f"{field_name}.invocation_id")
        invocation_type = _choice(
            item.invocation_type,
            f"{field_name}.invocation_type",
            SUPPORTED_ACTIVE_INVOCATION_TYPES,
        )
        _choice(
            item.state,
            f"{field_name}.state",
            SUPPORTED_ACTIVE_INVOCATION_STATES_BY_TYPE[invocation_type],
        )
        if item.coordinate is not None:
            _validate_coordinate_value(item.coordinate, f"{field_name}.coordinate")
        _assert_known_coordinate(item.coordinate, known, f"{field_name}.coordinate")
    if invocation_refs != tuple(
        sorted(invocation_refs, key=lambda item: (item.invocation_type, item.invocation_id))
    ):
        raise SnapshotValidationError("active invocation refs are not canonically ordered")

    _instance(value.terminal_delivery, TerminalDeliveryFacts, "terminal_delivery")
    terminal = value.terminal_delivery
    _boolean(terminal.is_terminal, "terminal_delivery.is_terminal")
    _optional_text(terminal.terminal_reason, "terminal_delivery.terminal_reason")
    _choice(
        terminal.delivery_capability,
        "terminal_delivery.delivery_capability",
        SUPPORTED_DELIVERY_CAPABILITIES,
    )
    _boolean(terminal.delivery_allowed, "terminal_delivery.delivery_allowed")

    domain_facts = _tuple(value.domain_readiness, "domain_readiness")
    for index, item in enumerate(domain_facts):
        field_name = f"domain_readiness[{index}]"
        _instance(item, DomainReadinessFact, field_name)
        _text(item.domain, f"{field_name}.domain")
        _choice(item.state, f"{field_name}.state", SUPPORTED_DOMAIN_READINESS_STATES)
        if item.action_hint is not None:
            _enum_member(
                item.action_hint,
                TransitionAction,
                f"{field_name}.action_hint",
            )
        if item.target is not None:
            _validate_coordinate_value(item.target, f"{field_name}.target")
        _assert_known_coordinate(item.target, known, f"{field_name}.target")
        _text(item.reason_code, f"{field_name}.reason_code")
        _text(item.evidence_ref, f"{field_name}.evidence_ref")
        _validate_domain_fact(field_name, item)
    if domain_facts != tuple(
        sorted(domain_facts, key=lambda item: (item.domain, item.reason_code))
    ):
        raise SnapshotValidationError("domain readiness facts are not canonically ordered")

    immutable_refs = _tuple(value.immutable_refs, "immutable_refs")
    for index, item in enumerate(immutable_refs):
        field_name = f"immutable_refs[{index}]"
        _instance(item, ImmutableRef, field_name)
        _text(item.kind, f"{field_name}.kind")
        _text(item.ref, f"{field_name}.ref")
        _sha256(item.sha256, f"{field_name}.sha256")
        _boolean(item.behavior_binding, f"{field_name}.behavior_binding")
    if immutable_refs != tuple(
        sorted(immutable_refs, key=lambda item: (item.kind, item.ref, item.sha256))
    ):
        raise SnapshotValidationError("immutable refs are not canonically ordered")

    availability = _tuple(value.availability, "availability")
    for index, item in enumerate(availability):
        field_name = f"availability[{index}]"
        _instance(item, FactAvailability, field_name)
        _text(item.fact, f"{field_name}.fact")
        selected = _choice(
            item.availability,
            f"{field_name}.availability",
            SUPPORTED_FACT_AVAILABILITY,
        )
        _boolean(item.required_for_plan, f"{field_name}.required_for_plan")
        _optional_text(item.gap_id, f"{field_name}.gap_id")
        if selected != "AVAILABLE" and item.gap_id is None:
            raise SnapshotValidationError(
                f"{field_name} unavailable fact requires an explicit gap_id"
            )
    if availability != tuple(
        sorted(availability, key=lambda item: (item.fact, item.availability))
    ):
        raise SnapshotValidationError("availability facts are not canonically ordered")

    _validate_readiness_behavior_vocabulary(value)


class StageV1ReadinessAdapter:
    """Defensively freeze one recorded snapshot into a validated input value."""

    def __init__(self, bundle: WorkflowContractBundle):
        self._bundle = _validated_shadow_bundle(bundle)
        self._contract = _contract_identities(self._bundle)
        self._catalog = _catalog(self._bundle)
        self._known_coordinates = frozenset(item.coordinate for item in self._catalog)
        self._entry_by_coordinate = {
            item.coordinate: item for item in self._catalog
        }
        self._known_stage_ids = frozenset(
            coordinate.stage_id for coordinate in self._known_coordinates
        )
        self._supported_dirty_flags = SUPPORTED_DIRTY_FLAGS

    def adapt(self, recorded_snapshot: Mapping[str, object]) -> ReadinessInput:
        snapshot = _mapping(recorded_snapshot, "recorded_snapshot")
        schema = _text(_required(snapshot, "schema_version"), "schema_version")
        if schema != RECORDED_SNAPSHOT_SCHEMA:
            raise SnapshotValidationError("unsupported recorded snapshot schema")

        project_revision = _integer(
            _required(snapshot, "project_revision"), "project_revision", minimum=0
        )
        workflow_status = _choice(
            _required(snapshot, "workflow_status"),
            "workflow_status",
            SUPPORTED_WORKFLOW_STATUSES,
        )

        cursor_value = _required(snapshot, "stage_cursor")
        cursor = None
        if cursor_value is not None:
            item = _mapping(cursor_value, "stage_cursor")
            cursor = StageCursor(
                coordinate=_coordinate(
                    _required(item, "coordinate"), "stage_cursor.coordinate"
                ),
                active_step=_integer(
                    _required(item, "active_step"), "stage_cursor.active_step", minimum=0
                ),
                attempt=_integer(
                    _required(item, "attempt"), "stage_cursor.attempt", minimum=0
                ),
            )
            _assert_known_coordinate(
                cursor.coordinate, self._known_coordinates, "stage_cursor.coordinate"
            )
            if cursor.active_step != cursor.coordinate.source_step_id:
                raise SnapshotValidationError("stage cursor active_step is not atomic")

        checkpoints = []
        for index, raw in enumerate(
            _sequence(_required(snapshot, "checkpoint_heads"), "checkpoint_heads")
        ):
            field = f"checkpoint_heads[{index}]"
            item = _mapping(raw, field)
            coordinate = _coordinate(_required(item, "coordinate"), f"{field}.coordinate")
            _assert_known_coordinate(coordinate, self._known_coordinates, field)
            completed_step_value = _required(item, "checkpoint_step_id")
            checkpoint_step_id = (
                None
                if completed_step_value is None
                else _integer(completed_step_value, f"{field}.checkpoint_step_id", minimum=0)
            )
            if checkpoint_step_id != self._entry_by_coordinate[coordinate].checkpoint_step_id:
                raise SnapshotValidationError(
                    f"{field}.checkpoint_step_id conflicts with the validated Stage catalog"
                )
            completed_revision = _integer(
                _required(item, "completed_revision"),
                f"{field}.completed_revision",
                minimum=0,
            )
            if completed_revision > project_revision:
                raise SnapshotValidationError(
                    f"{field}.completed_revision exceeds project_revision"
                )
            checkpoints.append(
                CheckpointHead(
                    coordinate=coordinate,
                    checkpoint_step_id=checkpoint_step_id,
                    completed_revision=completed_revision,
                    receipt_sha256=_sha256(
                        _required(item, "receipt_sha256"), f"{field}.receipt_sha256"
                    ),
                )
            )
        checkpoint_heads = tuple(
            sorted(checkpoints, key=lambda value: (value.coordinate.stage_id, value.coordinate.subtask))
        )
        if len({item.coordinate for item in checkpoint_heads}) != len(checkpoint_heads):
            raise SnapshotValidationError("checkpoint heads contain duplicate coordinates")

        dirty_refs = []
        for index, raw in enumerate(
            _sequence(_required(snapshot, "dirty_refs"), "dirty_refs")
        ):
            field = f"dirty_refs[{index}]"
            item = _mapping(raw, field)
            owner_stage = _integer(
                _required(item, "owner_stage"), f"{field}.owner_stage", minimum=1
            )
            if owner_stage not in self._known_stage_ids:
                raise SnapshotValidationError(
                    f"{field}.owner_stage is outside the validated Stage catalog"
                )
            dirty_refs.append(
                DirtyRef(
                    flag=_choice(
                        _required(item, "flag"),
                        f"{field}.flag",
                        self._supported_dirty_flags,
                    ),
                    owner_stage=owner_stage,
                    cause_ref=_text(_required(item, "cause_ref"), f"{field}.cause_ref"),
                )
            )

        pending_refs = []
        for index, raw in enumerate(
            _sequence(
                _required(snapshot, "pending_action_refs"), "pending_action_refs"
            )
        ):
            field = f"pending_action_refs[{index}]"
            item = _mapping(raw, field)
            coordinate = _optional_coordinate(
                _required(item, "coordinate"), f"{field}.coordinate"
            )
            _assert_known_coordinate(coordinate, self._known_coordinates, field)
            pending_refs.append(
                PendingActionRef(
                    action_type=_text(
                        _required(item, "action_type"), f"{field}.action_type"
                    ),
                    gate=_optional_text(_required(item, "gate"), f"{field}.gate"),
                    request_ref=_text(
                        _required(item, "request_ref"), f"{field}.request_ref"
                    ),
                    generation=_integer(
                        _required(item, "generation"), f"{field}.generation", minimum=0
                    ),
                    coordinate=coordinate,
                )
            )

        invocation_refs = []
        for index, raw in enumerate(
            _sequence(
                _required(snapshot, "active_invocation_refs"),
                "active_invocation_refs",
            )
        ):
            field = f"active_invocation_refs[{index}]"
            item = _mapping(raw, field)
            coordinate = _optional_coordinate(
                _required(item, "coordinate"), f"{field}.coordinate"
            )
            _assert_known_coordinate(coordinate, self._known_coordinates, field)
            invocation_type = _choice(
                _required(item, "invocation_type"),
                f"{field}.invocation_type",
                SUPPORTED_ACTIVE_INVOCATION_TYPES,
            )
            invocation_refs.append(
                ActiveInvocationRef(
                    invocation_id=_text(
                        _required(item, "invocation_id"), f"{field}.invocation_id"
                    ),
                    invocation_type=invocation_type,
                    state=_choice(
                        _required(item, "state"),
                        f"{field}.state",
                        SUPPORTED_ACTIVE_INVOCATION_STATES_BY_TYPE[invocation_type],
                    ),
                    coordinate=coordinate,
                )
            )

        terminal_raw = _mapping(
            _required(snapshot, "terminal_delivery"), "terminal_delivery"
        )
        terminal_delivery = TerminalDeliveryFacts(
            is_terminal=_boolean(
                _required(terminal_raw, "is_terminal"), "terminal_delivery.is_terminal"
            ),
            terminal_reason=_optional_text(
                _required(terminal_raw, "terminal_reason"),
                "terminal_delivery.terminal_reason",
            ),
            delivery_capability=_choice(
                _required(terminal_raw, "delivery_capability"),
                "terminal_delivery.delivery_capability",
                SUPPORTED_DELIVERY_CAPABILITIES,
            ),
            delivery_allowed=_boolean(
                _required(terminal_raw, "delivery_allowed"),
                "terminal_delivery.delivery_allowed",
            ),
        )
        _validate_terminal_delivery_consistency(workflow_status, terminal_delivery)

        domain_facts = []
        for index, raw in enumerate(
            _sequence(_required(snapshot, "domain_readiness"), "domain_readiness")
        ):
            field = f"domain_readiness[{index}]"
            item = _mapping(raw, field)
            action_value = _required(item, "action_hint")
            if action_value is None:
                action = None
            else:
                action_text = _text(action_value, f"{field}.action_hint")
                try:
                    action = TransitionAction(action_text)
                except ValueError as exc:
                    raise SnapshotValidationError(
                        f"{field}.action_hint is unsupported"
                    ) from exc
            target = _optional_coordinate(_required(item, "target"), f"{field}.target")
            _assert_known_coordinate(target, self._known_coordinates, field)
            fact = DomainReadinessFact(
                domain=_text(_required(item, "domain"), f"{field}.domain"),
                state=_choice(
                    _required(item, "state"),
                    f"{field}.state",
                    SUPPORTED_DOMAIN_READINESS_STATES,
                ),
                action_hint=action,
                target=target,
                reason_code=_text(
                    _required(item, "reason_code"), f"{field}.reason_code"
                ),
                evidence_ref=_text(
                    _required(item, "evidence_ref"), f"{field}.evidence_ref"
                ),
            )
            _validate_domain_fact(field, fact)
            domain_facts.append(fact)

        immutable_refs = []
        for index, raw in enumerate(
            _sequence(_required(snapshot, "immutable_refs"), "immutable_refs")
        ):
            field = f"immutable_refs[{index}]"
            item = _mapping(raw, field)
            immutable_refs.append(
                ImmutableRef(
                    kind=_text(_required(item, "kind"), f"{field}.kind"),
                    ref=_text(_required(item, "ref"), f"{field}.ref"),
                    sha256=_sha256(_required(item, "sha256"), f"{field}.sha256"),
                    behavior_binding=_boolean(
                        _required(item, "behavior_binding"),
                        f"{field}.behavior_binding",
                    ),
                )
            )

        availability = []
        for index, raw in enumerate(
            _sequence(_required(snapshot, "availability"), "availability")
        ):
            field = f"availability[{index}]"
            item = _mapping(raw, field)
            fact_availability = FactAvailability(
                fact=_text(_required(item, "fact"), f"{field}.fact"),
                availability=_choice(
                    _required(item, "availability"),
                    f"{field}.availability",
                    SUPPORTED_FACT_AVAILABILITY,
                ),
                required_for_plan=_boolean(
                    _required(item, "required_for_plan"),
                    f"{field}.required_for_plan",
                ),
                gap_id=_optional_text(_required(item, "gap_id"), f"{field}.gap_id"),
            )
            if (
                fact_availability.availability != "AVAILABLE"
                and fact_availability.gap_id is None
            ):
                raise SnapshotValidationError(
                    f"{field} unavailable fact requires an explicit gap_id"
                )
            availability.append(fact_availability)

        result = ReadinessInput(
            schema_version=READINESS_INPUT_SCHEMA,
            contract=self._contract,
            project_id=_text(_required(snapshot, "project_id"), "project_id"),
            project_revision=project_revision,
            run_generation=_text(
                _required(snapshot, "run_generation"), "run_generation"
            ),
            runtime_generation=_text(
                _required(snapshot, "runtime_generation"), "runtime_generation"
            ),
            scheduler_generation=_text(
                _required(snapshot, "scheduler_generation"), "scheduler_generation"
            ),
            workflow_status=workflow_status,
            last_completed_step=_integer(
                _required(snapshot, "last_completed_step"), "last_completed_step"
            ),
            last_completed_stage=_integer(
                _required(snapshot, "last_completed_stage"),
                "last_completed_stage",
                minimum=0,
            ),
            attempt=_integer(_required(snapshot, "attempt"), "attempt", minimum=0),
            stage_cursor=cursor,
            catalog=self._catalog,
            semantic_dirty_flags=tuple(self._bundle.classifier.semantic_dirty_flags),
            checkpoint_heads=checkpoint_heads,
            dirty_refs=tuple(
                sorted(dirty_refs, key=lambda value: (value.flag, value.owner_stage, value.cause_ref))
            ),
            pending_action_refs=tuple(
                sorted(pending_refs, key=lambda value: (value.generation, value.action_type, value.request_ref))
            ),
            active_invocation_refs=tuple(
                sorted(invocation_refs, key=lambda value: (value.invocation_type, value.invocation_id))
            ),
            terminal_delivery=terminal_delivery,
            domain_readiness=tuple(
                sorted(domain_facts, key=lambda value: (value.domain, value.reason_code))
            ),
            immutable_refs=tuple(
                sorted(immutable_refs, key=lambda value: (value.kind, value.ref, value.sha256))
            ),
            availability=tuple(
                sorted(availability, key=lambda value: (value.fact, value.availability))
            ),
        )
        _validate_readiness_input(self._bundle, result)
        return result


def _semantic_contract(contract: ContractIdentities) -> dict[str, object]:
    return {
        "semantic_sha256": contract.semantic_sha256,
        "semantic_bytes": contract.semantic_bytes,
        "owner_resolution_mode": contract.owner_resolution_mode,
    }


def _readiness_input_semantic_value(value: ReadinessInput) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "contract": _semantic_contract(value.contract),
        "project_id": value.project_id,
        "project_revision": value.project_revision,
        "run_generation": value.run_generation,
        "runtime_generation": value.runtime_generation,
        "scheduler_generation": value.scheduler_generation,
        "workflow_status": value.workflow_status,
        "last_completed_step": value.last_completed_step,
        "last_completed_stage": value.last_completed_stage,
        "attempt": value.attempt,
        "stage_cursor": value.stage_cursor,
        "catalog": value.catalog,
        "semantic_dirty_flags": value.semantic_dirty_flags,
        "checkpoint_heads": tuple(item.coordinate for item in value.checkpoint_heads),
        "dirty_refs": tuple(
            {"flag": item.flag, "owner_stage": item.owner_stage}
            for item in value.dirty_refs
        ),
        "pending_action_refs": value.pending_action_refs,
        "active_invocation_refs": value.active_invocation_refs,
        "terminal_delivery": value.terminal_delivery,
        "domain_readiness": tuple(
            {
                "domain": item.domain,
                "state": item.state,
                "action_hint": item.action_hint,
                "target": item.target,
                "reason_code": item.reason_code,
            }
            for item in value.domain_readiness
        ),
        "behavior_bound_immutable_refs": tuple(
            item for item in value.immutable_refs if item.behavior_binding
        ),
        "availability": tuple(
            {
                "fact": item.fact,
                "availability": item.availability,
                "required_for_plan": item.required_for_plan,
            }
            for item in value.availability
        ),
    }


def readiness_input_semantic_bytes(value: ReadinessInput) -> bytes:
    _validate_shadow_runtime_structure(value, ReadinessInput, "readiness_input")
    return canonical_bytes(_readiness_input_semantic_value(value))


def readiness_input_semantic_sha256(value: ReadinessInput) -> str:
    _validate_shadow_runtime_structure(value, ReadinessInput, "readiness_input")
    return canonical_sha256(_readiness_input_semantic_value(value))


def readiness_input_analysis_bytes(value: ReadinessInput) -> bytes:
    _validate_shadow_runtime_structure(value, ReadinessInput, "readiness_input")
    return canonical_bytes(value)


def readiness_input_analysis_sha256(value: ReadinessInput) -> str:
    _validate_shadow_runtime_structure(value, ReadinessInput, "readiness_input")
    return canonical_sha256(value)


def _readiness_result_semantic_value(value: ReadinessResult) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "contract": _semantic_contract(value.contract),
        "project_revision": value.project_revision,
        "run_generation": value.run_generation,
        "state": value.state,
        "reason_codes": value.reason_codes,
        "blockers": value.blockers,
        "target": value.target,
        "input_semantic_sha256": value.input_semantic_sha256,
    }


def readiness_result_semantic_bytes(value: ReadinessResult) -> bytes:
    _validate_shadow_runtime_structure(value, ReadinessResult, "readiness_result")
    return canonical_bytes(_readiness_result_semantic_value(value))


def readiness_result_semantic_sha256(value: ReadinessResult) -> str:
    _validate_shadow_runtime_structure(value, ReadinessResult, "readiness_result")
    return canonical_sha256(_readiness_result_semantic_value(value))


def readiness_result_analysis_bytes(value: ReadinessResult) -> bytes:
    _validate_shadow_runtime_structure(value, ReadinessResult, "readiness_result")
    return canonical_bytes(value)


def readiness_result_analysis_sha256(value: ReadinessResult) -> str:
    _validate_shadow_runtime_structure(value, ReadinessResult, "readiness_result")
    return canonical_sha256(value)


def _transition_plan_semantic_value(value: TransitionPlan) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "contract": _semantic_contract(value.contract),
        "base_revision": value.base_revision,
        "run_generation": value.run_generation,
        "authoritative": value.authoritative,
        "action": value.action,
        "current": value.current,
        "target": value.target,
        "execution_route": value.execution_route,
        "reason_code": value.reason_code,
        "readiness_semantic_sha256": value.readiness_semantic_sha256,
        "read_set_sha256": value.read_set_sha256,
        "proposed_mutations": value.proposed_mutations,
        "performed_side_effects": value.performed_side_effects,
    }


def transition_plan_semantic_bytes(value: TransitionPlan) -> bytes:
    _validate_shadow_runtime_structure(value, TransitionPlan, "transition_plan")
    return canonical_bytes(_transition_plan_semantic_value(value))


def transition_plan_semantic_sha256(value: TransitionPlan) -> str:
    _validate_shadow_runtime_structure(value, TransitionPlan, "transition_plan")
    return canonical_sha256(_transition_plan_semantic_value(value))


def transition_plan_analysis_bytes(value: TransitionPlan) -> bytes:
    _validate_shadow_runtime_structure(value, TransitionPlan, "transition_plan")
    return canonical_bytes(value)


def transition_plan_analysis_sha256(value: TransitionPlan) -> str:
    _validate_shadow_runtime_structure(value, TransitionPlan, "transition_plan")
    return canonical_sha256(value)


class SchedulerCore:
    """Pure shadow planner over one immutable :class:`ReadinessInput`."""

    _ACTIVE_INVOCATION_STATES = frozenset(
        state
        for states in SUPPORTED_ACTIVE_INVOCATION_STATES_BY_TYPE.values()
        for state in states
    )

    @classmethod
    def plan(
        cls,
        bundle: WorkflowContractBundle,
        value: ReadinessInput,
    ) -> SchedulerDecision:
        trusted_bundle = _validated_shadow_bundle(bundle)
        _validate_readiness_input(trusted_bundle, value)
        validated_step_ids = frozenset(step.step_id for step in trusted_bundle.steps)
        return cls._plan_validated(
            value,
            validated_step_ids=validated_step_ids,
        )

    @classmethod
    def _plan_validated(
        cls,
        value: ReadinessInput,
        *,
        validated_step_ids: frozenset[int],
    ) -> SchedulerDecision:
        workflow_disposition = WORKFLOW_STATUS_PLAN_DISPOSITIONS[
            value.workflow_status
        ]
        input_semantic = readiness_input_semantic_sha256(value)
        input_analysis = readiness_input_analysis_sha256(value)
        current = value.stage_cursor.coordinate if value.stage_cursor is not None else None

        unavailable = tuple(
            item.fact
            for item in value.availability
            if item.required_for_plan and item.availability != "AVAILABLE"
        )
        if unavailable:
            state = ReadinessState.BLOCKED
            blockers = unavailable
            reasons = ("REQUIRED_FACT_UNAVAILABLE",)
            action = TransitionAction.WAIT
            target = current
            route = None
            reason = "REQUIRED_FACT_UNAVAILABLE"
        elif value.terminal_delivery.is_terminal:
            state = ReadinessState.TERMINAL
            blockers = ()
            reasons = (value.terminal_delivery.terminal_reason or "RECORDED_TERMINAL",)
            action = TransitionAction.TERMINATE
            target = None
            route = None
            reason = reasons[0]
        elif value.pending_action_refs:
            pending = value.pending_action_refs[0]
            state = ReadinessState.WAITING
            blockers = (f"pending:{pending.action_type}:{pending.request_ref}",)
            reasons = ("PENDING_ACTION",)
            action = TransitionAction.WAIT
            target = pending.coordinate or current
            route = None
            reason = "PENDING_ACTION"
        else:
            active = tuple(
                item
                for item in value.active_invocation_refs
                if item.state in cls._ACTIVE_INVOCATION_STATES
            )
            blocking_domain = tuple(
                item
                for item in value.domain_readiness
                if item.state in {"BLOCKED", "WAITING"}
            )
            action_domains = tuple(
                item for item in value.domain_readiness if item.action_hint is not None
            )
            hinted_values = {
                (item.action_hint, item.target) for item in action_domains
            }
            if len(hinted_values) > 1:
                state = ReadinessState.BLOCKED
                blockers = tuple(
                    f"domain:{item.domain}:{item.reason_code}" for item in action_domains
                )
                reasons = ("AMBIGUOUS_DOMAIN_ACTIONS",)
                action = TransitionAction.WAIT
                target = current
                route = None
                reason = "AMBIGUOUS_DOMAIN_ACTIONS"
            elif active:
                invocation = active[0]
                state = ReadinessState.WAITING
                blockers = (f"invocation:{invocation.invocation_type}:{invocation.invocation_id}",)
                reasons = ("ACTIVE_INVOCATION",)
                action = TransitionAction.WAIT
                target = invocation.coordinate or current
                route = None
                reason = "ACTIVE_INVOCATION"
            elif blocking_domain:
                domain = blocking_domain[0]
                state = ReadinessState.WAITING
                blockers = (f"domain:{domain.domain}:{domain.state}",)
                reasons = (domain.reason_code,)
                action = domain.action_hint or TransitionAction.WAIT
                target = domain.target or current
                route = None
                reason = domain.reason_code
            elif action_domains:
                domain = action_domains[0]
                action = domain.action_hint or TransitionAction.STAY
                state = (
                    ReadinessState.TERMINAL
                    if action is TransitionAction.TERMINATE
                    else ReadinessState.READY
                )
                blockers = ()
                reasons = (domain.reason_code,)
                target = domain.target
                route = None
                reason = domain.reason_code
            elif workflow_disposition == "WAIT":
                state = ReadinessState.WAITING
                blockers = (f"workflow-status:{value.workflow_status}",)
                reasons = ("WORKFLOW_STATUS_NOT_RUNNABLE",)
                action = TransitionAction.WAIT
                target = current
                route = None
                reason = "WORKFLOW_STATUS_NOT_RUNNABLE"
            elif workflow_disposition == "TERMINAL_ONLY":
                raise SnapshotValidationError(
                    "terminal workflow status reached planning without terminal facts"
                )
            elif workflow_disposition != "RUNNABLE":
                raise SnapshotValidationError(
                    "workflow status has an unsupported planning disposition"
                )
            else:
                completed = {item.coordinate for item in value.checkpoint_heads}
                selected = None
                if current is not None:
                    selected = next(
                        (item for item in value.catalog if item.coordinate == current), None
                    )
                if selected is None:
                    selected = next(
                        (item for item in value.catalog if item.coordinate not in completed), None
                    )
                if selected is None:
                    state = ReadinessState.TERMINAL
                    blockers = ()
                    reasons = ("ALL_STAGE_SUBTASKS_COMPLETE",)
                    action = TransitionAction.TERMINATE
                    target = None
                    route = None
                    reason = "ALL_STAGE_SUBTASKS_COMPLETE"
                else:
                    state = ReadinessState.READY
                    blockers = ()
                    reasons = ("RECORDED_INPUT_READY",)
                    action = TransitionAction.DISPATCH
                    target = selected.coordinate
                    semantic_dirty = any(
                        item.flag in value.semantic_dirty_flags for item in value.dirty_refs
                    )
                    if selected.conditional and not semantic_dirty:
                        route = "stage-subtask:conditional_math_preflight_skip"
                        reason = "CONDITIONAL_STEP_13_BOUND_SKIP"
                    else:
                        if selected.coordinate.source_step_id not in validated_step_ids:
                            raise SnapshotValidationError(
                                "cannot construct an active route for an unvalidated Step"
                            )
                        route = f"step:{selected.coordinate.source_step_id}"
                        reason = "RECORDED_INPUT_READY"

        readiness = ReadinessResult(
            schema_version=READINESS_RESULT_SCHEMA,
            contract=value.contract,
            project_revision=value.project_revision,
            run_generation=value.run_generation,
            state=state,
            reason_codes=reasons,
            blockers=blockers,
            target=target,
            input_semantic_sha256=input_semantic,
            input_analysis_sha256=input_analysis,
        )
        plan = TransitionPlan(
            schema_version=TRANSITION_PLAN_SCHEMA,
            contract=value.contract,
            base_revision=value.project_revision,
            run_generation=value.run_generation,
            authoritative=False,
            action=action,
            current=current,
            target=target,
            execution_route=route,
            reason_code=reason,
            readiness_semantic_sha256=readiness_result_semantic_sha256(readiness),
            readiness_analysis_sha256=readiness_result_analysis_sha256(readiness),
            read_set_sha256=input_semantic,
            proposed_mutations=(),
            performed_side_effects=(),
        )
        return SchedulerDecision(
            readiness_input=value,
            readiness=readiness,
            plan=plan,
        )


def parity_value(plan: TransitionPlan) -> ParityValue:
    _validate_shadow_runtime_structure(plan, TransitionPlan, "plan")
    return ParityValue(
        action=plan.action,
        coordinate=plan.target,
        execution_route=plan.execution_route,
    )


def _validate_parity_value(
    value: ParityValue,
    known: frozenset[ScheduleCoordinate] | None,
    field_name: str,
) -> None:
    _instance(value, ParityValue, field_name)
    _text(value.schema_version, f"{field_name}.schema_version")
    if value.schema_version != PARITY_VALUE_SCHEMA:
        raise SnapshotValidationError(f"{field_name} has an unsupported schema")
    _enum_member(value.action, TransitionAction, f"{field_name}.action")
    if value.coordinate is not None:
        _validate_coordinate_value(value.coordinate, f"{field_name}.coordinate")
    if known is not None:
        _assert_known_coordinate(value.coordinate, known, f"{field_name}.coordinate")
    _optional_text(value.execution_route, f"{field_name}.execution_route")
    if value.action in {
        TransitionAction.ADVANCE,
        TransitionAction.DISPATCH,
        TransitionAction.REOPEN,
    } and value.coordinate is None:
        raise SnapshotValidationError(f"{field_name} active action requires coordinate")
    if value.action is TransitionAction.TERMINATE and value.coordinate is not None:
        raise SnapshotValidationError(f"{field_name} terminal action cannot have coordinate")


def _validate_scheduler_decision(
    bundle: WorkflowContractBundle,
    decision: SchedulerDecision,
) -> None:
    """Reject forged decisions before they can produce a parity receipt."""

    _instance(decision, SchedulerDecision, "decision")
    _validate_readiness_input(bundle, decision.readiness_input)
    expected_contract = _contract_identities(bundle)
    known = frozenset(item.coordinate for item in _catalog(bundle))

    _instance(decision.readiness, ReadinessResult, "decision.readiness")
    readiness = decision.readiness
    _text(readiness.schema_version, "readiness.schema_version")
    if readiness.schema_version != READINESS_RESULT_SCHEMA:
        raise SnapshotValidationError("unsupported readiness result schema")
    _validate_contract_identities_value(readiness.contract, "readiness.contract")
    if readiness.contract != expected_contract:
        raise SnapshotValidationError(
            "readiness result contract identity does not match trusted bundle"
        )
    _integer(readiness.project_revision, "readiness.project_revision", minimum=0)
    _text(readiness.run_generation, "readiness.run_generation")
    _enum_member(readiness.state, ReadinessState, "readiness.state")
    reason_codes = _tuple(readiness.reason_codes, "readiness.reason_codes")
    if not reason_codes:
        raise SnapshotValidationError("readiness.reason_codes cannot be empty")
    for index, item in enumerate(reason_codes):
        _text(item, f"readiness.reason_codes[{index}]")
    for index, item in enumerate(_tuple(readiness.blockers, "readiness.blockers")):
        _text(item, f"readiness.blockers[{index}]")
    if readiness.target is not None:
        _validate_coordinate_value(readiness.target, "readiness.target")
    _assert_known_coordinate(readiness.target, known, "readiness.target")
    input_semantic = readiness_input_semantic_sha256(decision.readiness_input)
    input_analysis = readiness_input_analysis_sha256(decision.readiness_input)
    _sha256(readiness.input_semantic_sha256, "readiness.input_semantic_sha256")
    _sha256(readiness.input_analysis_sha256, "readiness.input_analysis_sha256")
    if (
        readiness.project_revision != decision.readiness_input.project_revision
        or readiness.run_generation != decision.readiness_input.run_generation
        or readiness.input_semantic_sha256 != input_semantic
        or readiness.input_analysis_sha256 != input_analysis
    ):
        raise SnapshotValidationError(
            "readiness result does not exactly bind its validated input"
        )

    _instance(decision.plan, TransitionPlan, "decision.plan")
    plan = decision.plan
    _text(plan.schema_version, "plan.schema_version")
    if plan.schema_version != TRANSITION_PLAN_SCHEMA:
        raise SnapshotValidationError("unsupported transition plan schema")
    _validate_contract_identities_value(plan.contract, "plan.contract")
    if plan.contract != expected_contract:
        raise SnapshotValidationError(
            "transition plan contract identity does not match trusted bundle"
        )
    _integer(plan.base_revision, "plan.base_revision", minimum=0)
    _text(plan.run_generation, "plan.run_generation")
    _boolean(plan.authoritative, "plan.authoritative")
    if plan.authoritative:
        raise SnapshotValidationError("shadow transition plan cannot be authoritative")
    _enum_member(plan.action, TransitionAction, "plan.action")
    for field_name, coordinate in (("plan.current", plan.current), ("plan.target", plan.target)):
        if coordinate is not None:
            _validate_coordinate_value(coordinate, field_name)
        _assert_known_coordinate(coordinate, known, field_name)
    _optional_text(plan.execution_route, "plan.execution_route")
    _text(plan.reason_code, "plan.reason_code")
    _sha256(plan.readiness_semantic_sha256, "plan.readiness_semantic_sha256")
    _sha256(plan.readiness_analysis_sha256, "plan.readiness_analysis_sha256")
    _sha256(plan.read_set_sha256, "plan.read_set_sha256")
    proposed = _tuple(plan.proposed_mutations, "plan.proposed_mutations")
    performed = _tuple(plan.performed_side_effects, "plan.performed_side_effects")
    if proposed or performed:
        raise SnapshotValidationError(
            "shadow transition plan must contain zero mutations and side effects"
        )
    current = (
        decision.readiness_input.stage_cursor.coordinate
        if decision.readiness_input.stage_cursor is not None
        else None
    )
    if (
        plan.base_revision != decision.readiness_input.project_revision
        or plan.run_generation != decision.readiness_input.run_generation
        or plan.current != current
        or plan.target != readiness.target
        or plan.read_set_sha256 != input_semantic
        or plan.readiness_semantic_sha256
        != readiness_result_semantic_sha256(readiness)
        or plan.readiness_analysis_sha256
        != readiness_result_analysis_sha256(readiness)
    ):
        raise SnapshotValidationError(
            "transition plan does not exactly bind validated input and readiness"
        )

    expected = SchedulerCore._plan_validated(
        decision.readiness_input,
        validated_step_ids=frozenset(step.step_id for step in bundle.steps),
    )
    if decision != expected:
        raise SnapshotValidationError(
            "shadow decision differs from deterministic trusted-bundle planning"
        )


def build_parity_receipt(
    bundle: WorkflowContractBundle,
    *,
    fixture_id: str,
    decision: SchedulerDecision | None,
    v1: ParityValue | None,
    evidence_refs: Sequence[str],
    gap_ids: Sequence[str] = (),
    expected_correction: ExpectedCorrection | None = None,
    v2_error_code: str | None = None,
) -> ParityReceipt:
    """Compare one v1 observation with one shadow decision after validation.

    Unsupported/unverified bundles fail before any receipt is constructed.
    Broad correction allowlists are impossible: an expected correction must
    exactly bind the fixture and both compared values.
    """

    validated_bundle = _validated_shadow_bundle(bundle)
    contract = _contract_identities(validated_bundle)
    known = frozenset(item.coordinate for item in _catalog(validated_bundle))
    validated_v2_error_code = None
    if v2_error_code is not None:
        validated_v2_error_code = _text(v2_error_code, "v2_error_code")
        if decision is not None:
            raise SnapshotValidationError(
                "v2_error_code cannot accompany a shadow decision"
            )
    fixture = _text(fixture_id, "fixture_id")
    evidence_values = _sequence(evidence_refs, "evidence_refs")
    gap_values = _sequence(gap_ids, "gap_ids")
    evidence = tuple(
        sorted(
            _text(item, f"evidence_refs[{index}]")
            for index, item in enumerate(evidence_values)
        )
    )
    gaps = tuple(
        sorted(
            _text(item, f"gap_ids[{index}]")
            for index, item in enumerate(gap_values)
        )
    )
    if v1 is not None:
        _validate_parity_value(v1, known, "v1")
    if expected_correction is not None:
        _instance(expected_correction, ExpectedCorrection, "expected_correction")
        _text(expected_correction.issue_id, "expected_correction.issue_id")
        _text(expected_correction.fixture_id, "expected_correction.fixture_id")
        _validate_parity_value(
            expected_correction.expected_v1,
            known,
            "expected_correction.expected_v1",
        )
        _validate_parity_value(
            expected_correction.expected_v2,
            known,
            "expected_correction.expected_v2",
        )
        correction_evidence = _tuple(
            expected_correction.evidence_refs,
            "expected_correction.evidence_refs",
        )
        if not correction_evidence:
            raise SnapshotValidationError(
                "expected_correction.evidence_refs cannot be empty"
            )
        for index, item in enumerate(correction_evidence):
            _text(item, f"expected_correction.evidence_refs[{index}]")

    if decision is None:
        if validated_v2_error_code is not None:
            status = ParityStatus.V2_ERROR
            error_code = validated_v2_error_code
        elif v1 is None and gaps:
            status = ParityStatus.V1_UNREPRESENTABLE
            error_code = None
        else:
            raise ValueError("a missing shadow decision requires v2 error or v1 gap")
        return ParityReceipt(
            schema_version=PARITY_RECEIPT_SCHEMA,
            contract=contract,
            fixture_id=fixture,
            status=status,
            v1=v1,
            v2=None,
            issue_id=None,
            evidence_refs=evidence,
            gap_ids=gaps,
            readiness_semantic_sha256=None,
            readiness_analysis_sha256=None,
            plan_semantic_sha256=None,
            plan_analysis_sha256=None,
            error_code=error_code,
        )

    _validate_scheduler_decision(validated_bundle, decision)
    v2 = parity_value(decision.plan)
    _validate_parity_value(v2, known, "v2")
    issue_id = None
    error_code = None
    if v1 is None:
        if not gaps:
            raise ValueError("unrepresentable v1 observation requires an explicit gap")
        status = ParityStatus.V1_UNREPRESENTABLE
    elif v1 == v2:
        status = ParityStatus.MATCH
    elif expected_correction is not None:
        correction = expected_correction
        if (
            correction.fixture_id != fixture
            or correction.expected_v1 != v1
            or correction.expected_v2 != v2
            or not correction.issue_id
            or not correction.evidence_refs
        ):
            raise ValueError("expected correction does not exactly bind this comparison")
        status = ParityStatus.EXPECTED_CORRECTION
        issue_id = correction.issue_id
        evidence = tuple(sorted(set(evidence) | set(correction.evidence_refs)))
    else:
        status = ParityStatus.UNEXPLAINED_DIFFERENCE

    return ParityReceipt(
        schema_version=PARITY_RECEIPT_SCHEMA,
        contract=contract,
        fixture_id=fixture,
        status=status,
        v1=v1,
        v2=v2,
        issue_id=issue_id,
        evidence_refs=evidence,
        gap_ids=gaps,
        readiness_semantic_sha256=readiness_result_semantic_sha256(decision.readiness),
        readiness_analysis_sha256=readiness_result_analysis_sha256(decision.readiness),
        plan_semantic_sha256=transition_plan_semantic_sha256(decision.plan),
        plan_analysis_sha256=transition_plan_analysis_sha256(decision.plan),
        error_code=error_code,
    )


def _validate_parity_receipt_value(value: object) -> ParityReceipt:
    _instance(value, ParityReceipt, "parity_receipt")
    receipt = value
    assert type(receipt) is ParityReceipt
    _text(receipt.schema_version, "parity_receipt.schema_version")
    if receipt.schema_version != PARITY_RECEIPT_SCHEMA:
        raise SnapshotValidationError("unsupported parity receipt schema")
    _validate_contract_identities_value(receipt.contract, "parity_receipt.contract")
    _text(receipt.fixture_id, "parity_receipt.fixture_id")
    _enum_member(receipt.status, ParityStatus, "parity_receipt.status")
    if receipt.v1 is not None:
        _validate_parity_value(receipt.v1, None, "parity_receipt.v1")
    if receipt.v2 is not None:
        _validate_parity_value(receipt.v2, None, "parity_receipt.v2")
    _optional_text(receipt.issue_id, "parity_receipt.issue_id")
    for field_name, values in (
        ("parity_receipt.evidence_refs", receipt.evidence_refs),
        ("parity_receipt.gap_ids", receipt.gap_ids),
    ):
        for index, item in enumerate(_tuple(values, field_name)):
            _text(item, f"{field_name}[{index}]")
    for field_name, digest in (
        ("parity_receipt.readiness_semantic_sha256", receipt.readiness_semantic_sha256),
        ("parity_receipt.readiness_analysis_sha256", receipt.readiness_analysis_sha256),
        ("parity_receipt.plan_semantic_sha256", receipt.plan_semantic_sha256),
        ("parity_receipt.plan_analysis_sha256", receipt.plan_analysis_sha256),
    ):
        _optional_sha256(digest, field_name)
    _optional_text(receipt.error_code, "parity_receipt.error_code")
    return receipt


def parity_receipt_bytes(value: ParityReceipt) -> bytes:
    return canonical_bytes(_validate_parity_receipt_value(value))


def parity_receipt_sha256(value: ParityReceipt) -> str:
    return canonical_sha256(_validate_parity_receipt_value(value))
