"""M0.3 pure CommandEnvelope and structured read-set CAS validator.

This module validates a proposal against explicitly supplied current facts. It
does not read SQLite, files, clocks, environment, processes, or networks, and a
positive result is only a non-authoritative shadow-validation outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
import re

from .canonical import CanonicalizationError, canonical_bytes, canonical_sha256
from .contract_pins import (
    CONTRACT_PIN_SET_SCHEMA,
    ContractPinSetV1,
    contract_pin_set_sha256,
    validate_contract_pin_set,
)
from .project_snapshot_v0 import (
    EVENT_HEAD_FACT_TYPE,
    ProjectSnapshotV0,
    SnapshotAvailabilityV0,
    SnapshotCompletenessV0,
    SnapshotFactV0,
    SnapshotSectionIdV0,
    SnapshotV0ValidationError,
    validate_project_snapshot_v0,
)
from .workflow_contract_v2 import WorkflowContractBundleV2


COMMAND_ENVELOPE_SCHEMA = "command-envelope-v1"
READ_SET_SCHEMA = "command-read-set-v1"
CURRENT_FACTS_SCHEMA = "command-current-facts-v3"
COMMAND_SCOPE_POLICY_SCHEMA = "command-scope-policy-v1"
COMMAND_CAS_DECISION_SCHEMA = "command-cas-decision-v3"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class CommandEnvelopeValidationError(ValueError):
    """Raised for malformed Command/CAS runtime values."""


class CommandType(str, Enum):
    SHADOW_ADVANCE = "SHADOW_ADVANCE"
    SHADOW_RETRY = "SHADOW_RETRY"
    SHADOW_APPLY_DECISION = "SHADOW_APPLY_DECISION"
    SHADOW_RECORD_SOLVER_FACT = "SHADOW_RECORD_SOLVER_FACT"
    UNSUPPORTED = "UNSUPPORTED"


class ActorType(str, Enum):
    HUMAN = "HUMAN"
    SERVICE = "SERVICE"
    TEST_FIXTURE = "TEST_FIXTURE"


class FactType(str, Enum):
    PROJECT_STATE = "project_state"
    EVENT_HEAD = "event_head"
    STAGE_CURSOR = "stage_cursor"
    DIRTY_OWNER = "dirty_owner"
    PENDING_ACTION = "pending_action"
    SOLVER_JOB = "solver_job"
    DELIVERY_AUTHORIZATION = "delivery_authorization"


class FactScopeBindingV1(str, Enum):
    NONE = "NONE"
    ENTITY = "ENTITY"
    SUBJECT = "SUBJECT"
    ENTITY_AND_SUBJECT = "ENTITY_AND_SUBJECT"


class CommandCASRejectionCode(str, Enum):
    UNSUPPORTED_COMMAND = "UNSUPPORTED_COMMAND"
    SNAPSHOT_INCOMPLETE = "SNAPSHOT_INCOMPLETE"
    PROJECT_ID_MISMATCH = "PROJECT_ID_MISMATCH"
    PROJECT_GENERATION_MISMATCH = "PROJECT_GENERATION_MISMATCH"
    PROJECT_REVISION_MISMATCH = "PROJECT_REVISION_MISMATCH"
    RUNTIME_GENERATION_MISMATCH = "RUNTIME_GENERATION_MISMATCH"
    SCHEDULER_GENERATION_MISMATCH = "SCHEDULER_GENERATION_MISMATCH"
    RUN_GENERATION_MISMATCH = "RUN_GENERATION_MISMATCH"
    ENTITY_SCOPE_MISMATCH = "ENTITY_SCOPE_MISMATCH"
    SUBJECT_SCOPE_MISMATCH = "SUBJECT_SCOPE_MISMATCH"
    READ_SET_MISMATCH = "READ_SET_MISMATCH"
    REQUIRED_FACT_UNAVAILABLE = "REQUIRED_FACT_UNAVAILABLE"
    PAYLOAD_MISMATCH = "PAYLOAD_MISMATCH"
    SEMANTIC_PIN_MISMATCH = "SEMANTIC_PIN_MISMATCH"
    IMPLEMENTATION_PIN_MISMATCH = "IMPLEMENTATION_PIN_MISMATCH"
    RUNTIME_PIN_MISMATCH = "RUNTIME_PIN_MISMATCH"
    PERSISTED_OWNER_PIN_MISMATCH = "PERSISTED_OWNER_PIN_MISMATCH"
    ENTITY_GENERATION_MISMATCH = "ENTITY_GENERATION_MISMATCH"
    SUBJECT_FINGERPRINT_MISMATCH = "SUBJECT_FINGERPRINT_MISMATCH"


@dataclass(frozen=True)
class ProjectGenerationBindingV1:
    project_id: str
    project_generation: str
    project_revision: int


@dataclass(frozen=True)
class RunGenerationBindingV1:
    runtime_generation: str
    scheduler_generation: str
    run_generation: str


@dataclass(frozen=True)
class NoEntityScopeV1:
    schema_version: str = "no-entity-scope-v1"


@dataclass(frozen=True)
class BoundEntityScopeV1:
    entity_type: str
    entity_id: str
    entity_generation: int


@dataclass(frozen=True)
class NoSubjectScopeV1:
    schema_version: str = "no-subject-scope-v1"


@dataclass(frozen=True)
class BoundSubjectScopeV1:
    subject_type: str
    subject_id: str
    subject_sha256: str


@dataclass(frozen=True)
class ActorRefV1:
    actor_type: ActorType
    actor_id: str


@dataclass(frozen=True)
class NoPayloadV1:
    schema_version: str = "no-payload-v1"


@dataclass(frozen=True)
class PayloadBindingV1:
    payload_schema: str
    payload_sha256: str


@dataclass(frozen=True)
class PayloadFieldV1:
    key: str
    value: str | int | bool | None


@dataclass(frozen=True)
class ActualPayloadV1:
    payload_schema: str
    fields: tuple[PayloadFieldV1, ...]


@dataclass(frozen=True)
class ReadSetEntryV1:
    fact_type: FactType
    fact_key: str
    value_sha256: str
    entity_generation: int | None
    subject_sha256: str | None


@dataclass(frozen=True)
class ReadSetV1:
    schema_version: str
    entries: tuple[ReadSetEntryV1, ...]
    entries_sha256: str


@dataclass(frozen=True)
class CurrentFactV1:
    fact_type: FactType
    fact_key: str
    availability: SnapshotAvailabilityV0
    value_sha256: str | None
    entity_generation: int | None
    subject_sha256: str | None
    status_detail: str | None


@dataclass(frozen=True)
class CurrentFactsV1:
    schema_version: str
    snapshot_completeness: SnapshotCompletenessV0
    project_binding: ProjectGenerationBindingV1
    run_binding: RunGenerationBindingV1
    entity_scope: NoEntityScopeV1 | BoundEntityScopeV1
    subject_scope: NoSubjectScopeV1 | BoundSubjectScopeV1
    contract_pins: ContractPinSetV1 | None
    facts: tuple[CurrentFactV1, ...]


@dataclass(frozen=True)
class FactRequirementV1:
    fact_type: FactType
    fact_key: str
    scope_binding: FactScopeBindingV1


@dataclass(frozen=True)
class SnapshotFactProjectionV1:
    section_id: SnapshotSectionIdV0
    source_fact_type: str
    source_fact_key: str
    target_fact_type: FactType
    target_fact_key: str


@dataclass(frozen=True)
class CommandScopePolicyV1:
    schema_version: str
    command_type: CommandType
    supported: bool
    entity_scope: str
    subject_scope: str
    required_facts: tuple[FactRequirementV1, ...]
    payload_schema: str | None
    requires_persisted_dirty_owner_pins: bool
    authority: str


@dataclass(frozen=True)
class CommandEnvelopeV1:
    schema_version: str
    command_id: str
    command_type: CommandType
    project_binding: ProjectGenerationBindingV1
    run_binding: RunGenerationBindingV1
    entity_scope: NoEntityScopeV1 | BoundEntityScopeV1
    subject_scope: NoSubjectScopeV1 | BoundSubjectScopeV1
    actor: ActorRefV1
    payload_binding: NoPayloadV1 | PayloadBindingV1
    read_set: ReadSetV1
    contract_pins: ContractPinSetV1


@dataclass(frozen=True)
class CommandCASRejectionV1:
    code: CommandCASRejectionCode
    fact_key: str
    expected_sha256: str | None
    actual_sha256: str | None


@dataclass(frozen=True)
class CommandCASDecisionV1:
    schema_version: str
    accepted_for_shadow_validation: bool
    rejections: tuple[CommandCASRejectionV1, ...]
    authoritative: bool
    proposed_mutations: tuple[str, ...]
    performed_side_effects: tuple[str, ...]


_ENTITY_TYPES = (NoEntityScopeV1, BoundEntityScopeV1)
_SUBJECT_TYPES = (NoSubjectScopeV1, BoundSubjectScopeV1)
_PAYLOAD_TYPES = (NoPayloadV1, PayloadBindingV1)
def _field(value: object, name: str, path: str) -> object:
    try:
        return object.__getattribute__(value, name)
    except AttributeError as exc:
        raise CommandEnvelopeValidationError(f"{path}.{name} is missing") from exc


def _text(value: object, path: str, *, sha: bool = False) -> str:
    if type(value) is not str or not value:
        raise CommandEnvelopeValidationError(f"{path} must be a non-empty plain string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise CommandEnvelopeValidationError(f"{path} must contain valid UTF-8 scalar values") from exc
    if sha and _SHA256_RE.fullmatch(value) is None:
        raise CommandEnvelopeValidationError(f"{path} must be lowercase SHA-256")
    return value


def _optional_text(value: object, path: str, *, sha: bool = False) -> str | None:
    if value is None:
        return None
    return _text(value, path, sha=sha)


def _integer(value: object, path: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise CommandEnvelopeValidationError(f"{path} must be a plain integer >= {minimum}")
    return value


def _enum(value: object, enum_type: type[Enum], path: str) -> None:
    if type(value) is not enum_type or not any(value is member for member in enum_type):
        raise CommandEnvelopeValidationError(f"{path} is not a registered {enum_type.__name__} member")


def _exact(value: object, expected: type, path: str) -> None:
    if type(value) is not expected:
        raise CommandEnvelopeValidationError(f"{path} has an unsupported runtime type")
    for item in fields(expected):
        _field(value, item.name, path)


def _validate_project_binding(value: ProjectGenerationBindingV1, path: str) -> None:
    _exact(value, ProjectGenerationBindingV1, path)
    _text(value.project_id, f"{path}.project_id")
    _text(value.project_generation, f"{path}.project_generation")
    _integer(value.project_revision, f"{path}.project_revision")


def _validate_run_binding(value: RunGenerationBindingV1, path: str) -> None:
    _exact(value, RunGenerationBindingV1, path)
    _text(value.runtime_generation, f"{path}.runtime_generation")
    _text(value.scheduler_generation, f"{path}.scheduler_generation")
    _text(value.run_generation, f"{path}.run_generation")


def _validate_entity_scope(value: object, path: str) -> None:
    if type(value) is NoEntityScopeV1:
        _exact(value, NoEntityScopeV1, path)
        if value.schema_version != "no-entity-scope-v1":
            raise CommandEnvelopeValidationError(f"{path} schema is unsupported")
        return
    if type(value) is BoundEntityScopeV1:
        _exact(value, BoundEntityScopeV1, path)
        _text(value.entity_type, f"{path}.entity_type")
        _text(value.entity_id, f"{path}.entity_id")
        _integer(value.entity_generation, f"{path}.entity_generation", minimum=1)
        return
    raise CommandEnvelopeValidationError(f"{path} has an unsupported entity sum type")


def _validate_subject_scope(value: object, path: str) -> None:
    if type(value) is NoSubjectScopeV1:
        _exact(value, NoSubjectScopeV1, path)
        if value.schema_version != "no-subject-scope-v1":
            raise CommandEnvelopeValidationError(f"{path} schema is unsupported")
        return
    if type(value) is BoundSubjectScopeV1:
        _exact(value, BoundSubjectScopeV1, path)
        _text(value.subject_type, f"{path}.subject_type")
        _text(value.subject_id, f"{path}.subject_id")
        _text(value.subject_sha256, f"{path}.subject_sha256", sha=True)
        return
    raise CommandEnvelopeValidationError(f"{path} has an unsupported subject sum type")


def _validate_payload_binding(value: object, path: str) -> None:
    if type(value) is NoPayloadV1:
        _exact(value, NoPayloadV1, path)
        if value.schema_version != "no-payload-v1":
            raise CommandEnvelopeValidationError(f"{path} schema is unsupported")
        return
    if type(value) is PayloadBindingV1:
        _exact(value, PayloadBindingV1, path)
        _text(value.payload_schema, f"{path}.payload_schema")
        _text(value.payload_sha256, f"{path}.payload_sha256", sha=True)
        return
    raise CommandEnvelopeValidationError(f"{path} has an unsupported payload sum type")


def validate_actual_payload(value: NoPayloadV1 | ActualPayloadV1) -> NoPayloadV1 | ActualPayloadV1:
    if type(value) is NoPayloadV1:
        _validate_payload_binding(value, "actual_payload")
        return value
    _exact(value, ActualPayloadV1, "actual_payload")
    _text(value.payload_schema, "actual_payload.payload_schema")
    if type(value.fields) is not tuple:
        raise CommandEnvelopeValidationError("actual_payload.fields must be an immutable tuple")
    keys: list[str] = []
    for index, item in enumerate(value.fields):
        path = f"actual_payload.fields[{index}]"
        _exact(item, PayloadFieldV1, path)
        keys.append(_text(item.key, f"{path}.key"))
        if item.value is not None and type(item.value) not in {str, int, bool}:
            raise CommandEnvelopeValidationError(f"{path}.value has an unsupported scalar type")
        if type(item.value) is str:
            _text(item.value, f"{path}.value")
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise CommandEnvelopeValidationError("actual_payload.fields must be uniquely canonical-sorted")
    return value


def actual_payload_sha256(value: NoPayloadV1 | ActualPayloadV1) -> str:
    payload = validate_actual_payload(value)
    try:
        return canonical_sha256(payload)
    except CanonicalizationError as exc:
        raise CommandEnvelopeValidationError("actual payload cannot be canonicalized") from exc


def actual_payload_bytes(value: NoPayloadV1 | ActualPayloadV1) -> bytes:
    payload = validate_actual_payload(value)
    try:
        return canonical_bytes(payload)
    except CanonicalizationError as exc:
        raise CommandEnvelopeValidationError("actual payload cannot be canonicalized") from exc


def _validate_read_entry(value: ReadSetEntryV1, path: str) -> None:
    _exact(value, ReadSetEntryV1, path)
    _enum(value.fact_type, FactType, f"{path}.fact_type")
    _text(value.fact_key, f"{path}.fact_key")
    _text(value.value_sha256, f"{path}.value_sha256", sha=True)
    if value.entity_generation is not None:
        _integer(value.entity_generation, f"{path}.entity_generation", minimum=1)
    _optional_text(value.subject_sha256, f"{path}.subject_sha256", sha=True)


def _entries_hash(entries: tuple[ReadSetEntryV1, ...]) -> str:
    try:
        return canonical_sha256(entries)
    except CanonicalizationError as exc:
        raise CommandEnvelopeValidationError("read-set entries cannot be canonicalized") from exc


def validate_read_set(value: ReadSetV1) -> ReadSetV1:
    _exact(value, ReadSetV1, "read_set")
    if value.schema_version != READ_SET_SCHEMA:
        raise CommandEnvelopeValidationError("read-set schema is unsupported")
    if type(value.entries) is not tuple:
        raise CommandEnvelopeValidationError("read_set.entries must be an immutable tuple")
    keys: list[tuple[str, str]] = []
    for index, entry in enumerate(value.entries):
        _validate_read_entry(entry, f"read_set.entries[{index}]")
        keys.append((entry.fact_type.value, entry.fact_key))
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise CommandEnvelopeValidationError("read-set entries must be uniquely canonical-sorted")
    if _text(value.entries_sha256, "read_set.entries_sha256", sha=True) != _entries_hash(value.entries):
        raise CommandEnvelopeValidationError("read-set entries hash mismatch")
    return value


def read_set_bytes(value: ReadSetV1) -> bytes:
    read_set = validate_read_set(value)
    try:
        return canonical_bytes(read_set)
    except CanonicalizationError as exc:
        raise CommandEnvelopeValidationError("read set cannot be canonicalized") from exc


def read_set_sha256(value: ReadSetV1) -> str:
    read_set = validate_read_set(value)
    try:
        return canonical_sha256(read_set)
    except CanonicalizationError as exc:
        raise CommandEnvelopeValidationError("read set cannot be canonicalized") from exc


def compile_read_set(entries: tuple[ReadSetEntryV1, ...]) -> ReadSetV1:
    if type(entries) is not tuple:
        raise CommandEnvelopeValidationError("read-set source entries must be an immutable tuple")
    ordered = tuple(sorted(entries, key=lambda item: (item.fact_type.value, item.fact_key)))
    return validate_read_set(ReadSetV1(READ_SET_SCHEMA, ordered, _entries_hash(ordered)))


def _validate_current_fact(value: CurrentFactV1, path: str) -> None:
    _exact(value, CurrentFactV1, path)
    _enum(value.fact_type, FactType, f"{path}.fact_type")
    _text(value.fact_key, f"{path}.fact_key")
    _enum(value.availability, SnapshotAvailabilityV0, f"{path}.availability")
    _optional_text(value.value_sha256, f"{path}.value_sha256", sha=True)
    if value.entity_generation is not None:
        _integer(value.entity_generation, f"{path}.entity_generation", minimum=1)
    _optional_text(value.subject_sha256, f"{path}.subject_sha256", sha=True)
    _optional_text(value.status_detail, f"{path}.status_detail")
    if value.availability is SnapshotAvailabilityV0.AVAILABLE:
        if value.value_sha256 is None or value.status_detail is not None:
            raise CommandEnvelopeValidationError(f"{path} AVAILABLE fact has contradictory status")
    elif value.value_sha256 is not None or value.status_detail is None:
        raise CommandEnvelopeValidationError(f"{path} unavailable fact has contradictory status")


def validate_current_facts(value: CurrentFactsV1) -> CurrentFactsV1:
    _exact(value, CurrentFactsV1, "current_facts")
    if value.schema_version != CURRENT_FACTS_SCHEMA:
        raise CommandEnvelopeValidationError("current facts schema is unsupported")
    _enum(value.snapshot_completeness, SnapshotCompletenessV0, "current_facts.snapshot_completeness")
    _validate_project_binding(value.project_binding, "current_facts.project_binding")
    _validate_run_binding(value.run_binding, "current_facts.run_binding")
    _validate_entity_scope(value.entity_scope, "current_facts.entity_scope")
    _validate_subject_scope(value.subject_scope, "current_facts.subject_scope")
    if value.contract_pins is not None:
        if type(value.contract_pins) is not ContractPinSetV1:
            raise CommandEnvelopeValidationError("current_facts.contract_pins has an unsupported runtime type")
        for item in fields(ContractPinSetV1):
            raw = _field(value.contract_pins, item.name, "current_facts.contract_pins")
            _text(raw, f"current_facts.contract_pins.{item.name}", sha=item.name != "schema_version")
        if value.contract_pins.schema_version != CONTRACT_PIN_SET_SCHEMA:
            raise CommandEnvelopeValidationError(
                "current_facts.contract_pins schema is unsupported"
            )
    if type(value.facts) is not tuple:
        raise CommandEnvelopeValidationError("current_facts.facts must be an immutable tuple")
    keys: list[tuple[str, str]] = []
    for index, fact in enumerate(value.facts):
        _validate_current_fact(fact, f"current_facts.facts[{index}]")
        keys.append((fact.fact_type.value, fact.fact_key))
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise CommandEnvelopeValidationError("current facts must be uniquely canonical-sorted")
    return value


def current_facts_bytes(value: CurrentFactsV1) -> bytes:
    current = validate_current_facts(value)
    try:
        return canonical_bytes(current)
    except CanonicalizationError as exc:
        raise CommandEnvelopeValidationError("current facts cannot be canonicalized") from exc


def current_facts_sha256(value: CurrentFactsV1) -> str:
    current = validate_current_facts(value)
    try:
        return canonical_sha256(current)
    except CanonicalizationError as exc:
        raise CommandEnvelopeValidationError("current facts cannot be canonicalized") from exc


def compile_command_scope_policy(command_type: CommandType) -> CommandScopePolicyV1:
    _enum(command_type, CommandType, "command_type")
    common = dict(
        schema_version=COMMAND_SCOPE_POLICY_SCHEMA,
        command_type=command_type,
        authority="m03-shadow-command-policy-source-projection",
    )
    if command_type is CommandType.SHADOW_ADVANCE:
        return CommandScopePolicyV1(
            **common,
            supported=True,
            entity_scope="none",
            subject_scope="none",
            required_facts=(
                FactRequirementV1(FactType.EVENT_HEAD, "project", FactScopeBindingV1.NONE),
                FactRequirementV1(FactType.PROJECT_STATE, "project", FactScopeBindingV1.NONE),
                FactRequirementV1(FactType.STAGE_CURSOR, "active", FactScopeBindingV1.NONE),
            ),
            payload_schema=None,
            requires_persisted_dirty_owner_pins=False,
        )
    if command_type is CommandType.SHADOW_RETRY:
        return CommandScopePolicyV1(
            **common,
            supported=True,
            entity_scope="required:workflow-step",
            subject_scope="none",
            required_facts=(
                FactRequirementV1(FactType.EVENT_HEAD, "project", FactScopeBindingV1.NONE),
                FactRequirementV1(FactType.PROJECT_STATE, "project", FactScopeBindingV1.NONE),
                FactRequirementV1(FactType.STAGE_CURSOR, "active", FactScopeBindingV1.ENTITY),
            ),
            payload_schema="shadow-retry-payload-v1",
            requires_persisted_dirty_owner_pins=False,
        )
    if command_type is CommandType.SHADOW_APPLY_DECISION:
        return CommandScopePolicyV1(
            **common,
            supported=True,
            entity_scope="none",
            subject_scope="required:pending-action",
            required_facts=(
                FactRequirementV1(FactType.EVENT_HEAD, "project", FactScopeBindingV1.NONE),
                FactRequirementV1(FactType.PENDING_ACTION, "active", FactScopeBindingV1.SUBJECT),
                FactRequirementV1(FactType.PROJECT_STATE, "project", FactScopeBindingV1.NONE),
            ),
            payload_schema="shadow-human-decision-payload-v1",
            requires_persisted_dirty_owner_pins=False,
        )
    if command_type is CommandType.SHADOW_RECORD_SOLVER_FACT:
        return CommandScopePolicyV1(
            **common,
            supported=True,
            entity_scope="required:solver-job",
            subject_scope="required:solver-receipt",
            required_facts=(
                FactRequirementV1(FactType.DIRTY_OWNER, "solver-receipt", FactScopeBindingV1.SUBJECT),
                FactRequirementV1(FactType.EVENT_HEAD, "project", FactScopeBindingV1.NONE),
                FactRequirementV1(FactType.PROJECT_STATE, "project", FactScopeBindingV1.NONE),
                FactRequirementV1(FactType.SOLVER_JOB, "bound", FactScopeBindingV1.ENTITY),
            ),
            payload_schema="shadow-solver-fact-payload-v1",
            requires_persisted_dirty_owner_pins=True,
        )
    return CommandScopePolicyV1(
        **common,
        supported=False,
        entity_scope="none",
        subject_scope="none",
        required_facts=(),
        payload_schema=None,
        requires_persisted_dirty_owner_pins=False,
    )


def validate_command_scope_policy(value: CommandScopePolicyV1) -> CommandScopePolicyV1:
    _exact(value, CommandScopePolicyV1, "policy")
    if value.schema_version != COMMAND_SCOPE_POLICY_SCHEMA:
        raise CommandEnvelopeValidationError("command scope policy schema is unsupported")
    _enum(value.command_type, CommandType, "policy.command_type")
    if type(value.supported) is not bool or type(value.requires_persisted_dirty_owner_pins) is not bool:
        raise CommandEnvelopeValidationError("command scope policy booleans must be plain bool")
    _text(value.entity_scope, "policy.entity_scope")
    _text(value.subject_scope, "policy.subject_scope")
    _optional_text(value.payload_schema, "policy.payload_schema")
    _text(value.authority, "policy.authority")
    if type(value.required_facts) is not tuple:
        raise CommandEnvelopeValidationError("policy.required_facts must be an immutable tuple")
    keys: list[tuple[str, str]] = []
    for index, requirement in enumerate(value.required_facts):
        path = f"policy.required_facts[{index}]"
        _exact(requirement, FactRequirementV1, path)
        _enum(requirement.fact_type, FactType, f"{path}.fact_type")
        _text(requirement.fact_key, f"{path}.fact_key")
        _enum(requirement.scope_binding, FactScopeBindingV1, f"{path}.scope_binding")
        keys.append((requirement.fact_type.value, requirement.fact_key))
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise CommandEnvelopeValidationError("policy required facts must be uniquely canonical-sorted")
    expected = compile_command_scope_policy(value.command_type)
    if value != expected:
        raise CommandEnvelopeValidationError("command scope policy differs from source-authorized projection")
    return value


def validate_command_envelope_structure(value: CommandEnvelopeV1) -> CommandEnvelopeV1:
    _exact(value, CommandEnvelopeV1, "envelope")
    if value.schema_version != COMMAND_ENVELOPE_SCHEMA:
        raise CommandEnvelopeValidationError("command envelope schema is unsupported")
    _text(value.command_id, "envelope.command_id")
    _enum(value.command_type, CommandType, "envelope.command_type")
    _validate_project_binding(value.project_binding, "envelope.project_binding")
    _validate_run_binding(value.run_binding, "envelope.run_binding")
    _validate_entity_scope(value.entity_scope, "envelope.entity_scope")
    _validate_subject_scope(value.subject_scope, "envelope.subject_scope")
    _exact(value.actor, ActorRefV1, "envelope.actor")
    _enum(value.actor.actor_type, ActorType, "envelope.actor.actor_type")
    _text(value.actor.actor_id, "envelope.actor.actor_id")
    _validate_payload_binding(value.payload_binding, "envelope.payload_binding")
    validate_read_set(value.read_set)
    if type(value.contract_pins) is not ContractPinSetV1:
        raise CommandEnvelopeValidationError("envelope.contract_pins has an unsupported runtime type")
    for item in fields(ContractPinSetV1):
        raw = _field(value.contract_pins, item.name, "envelope.contract_pins")
        _text(raw, f"envelope.contract_pins.{item.name}", sha=item.name != "schema_version")
    return value


def command_envelope_bytes(value: CommandEnvelopeV1) -> bytes:
    envelope = validate_command_envelope_structure(value)
    try:
        return canonical_bytes(envelope)
    except CanonicalizationError as exc:
        raise CommandEnvelopeValidationError("command envelope cannot be canonicalized") from exc


def command_envelope_sha256(value: CommandEnvelopeV1) -> str:
    envelope = validate_command_envelope_structure(value)
    try:
        return canonical_sha256(envelope)
    except CanonicalizationError as exc:
        raise CommandEnvelopeValidationError("command envelope cannot be canonicalized") from exc


def _reject(
    rejections: list[CommandCASRejectionV1],
    code: CommandCASRejectionCode,
    key: str,
    expected: str | None = None,
    actual: str | None = None,
) -> None:
    rejections.append(CommandCASRejectionV1(code, key, expected, actual))


def _scope_matches(policy_value: str, value: object, bound_type: str) -> bool:
    if policy_value == "none":
        return type(value) in {NoEntityScopeV1, NoSubjectScopeV1}
    required = policy_value.removeprefix("required:")
    if type(value) is BoundEntityScopeV1:
        return required == value.entity_type
    if type(value) is BoundSubjectScopeV1:
        return required == value.subject_type
    return required == bound_type


def _pin_groups(pin_set: ContractPinSetV1) -> tuple[tuple[str, str], ...]:
    return tuple(
        (item.name, getattr(pin_set, item.name))
        for item in fields(ContractPinSetV1)
        if item.name != "schema_version"
    )


def validate_command_cas(
    envelope: CommandEnvelopeV1,
    actual_payload: NoPayloadV1 | ActualPayloadV1,
    current_facts: CurrentFactsV1,
    trusted_policy: CommandScopePolicyV1,
    trusted_workflow: WorkflowContractBundleV2,
) -> CommandCASDecisionV1:
    """Evaluate CAS using only explicit, already-recorded immutable values."""

    proposal = validate_command_envelope_structure(envelope)
    payload = validate_actual_payload(actual_payload)
    current = validate_current_facts(current_facts)
    policy = validate_command_scope_policy(trusted_policy)
    if policy.command_type is not proposal.command_type:
        raise CommandEnvelopeValidationError("trusted policy command type differs from envelope")
    validate_contract_pin_set(proposal.contract_pins, trusted_workflow)
    rejections: list[CommandCASRejectionV1] = []
    if not policy.supported:
        _reject(rejections, CommandCASRejectionCode.UNSUPPORTED_COMMAND, proposal.command_type.value)
    if current.snapshot_completeness is not SnapshotCompletenessV0.COMPLETE:
        _reject(rejections, CommandCASRejectionCode.SNAPSHOT_INCOMPLETE, "snapshot")
    binding_pairs = (
        (CommandCASRejectionCode.PROJECT_ID_MISMATCH, "project_id", proposal.project_binding.project_id, current.project_binding.project_id),
        (CommandCASRejectionCode.PROJECT_GENERATION_MISMATCH, "project_generation", proposal.project_binding.project_generation, current.project_binding.project_generation),
        (CommandCASRejectionCode.PROJECT_REVISION_MISMATCH, "project_revision", str(proposal.project_binding.project_revision), str(current.project_binding.project_revision)),
        (CommandCASRejectionCode.RUNTIME_GENERATION_MISMATCH, "runtime_generation", proposal.run_binding.runtime_generation, current.run_binding.runtime_generation),
        (CommandCASRejectionCode.SCHEDULER_GENERATION_MISMATCH, "scheduler_generation", proposal.run_binding.scheduler_generation, current.run_binding.scheduler_generation),
        (CommandCASRejectionCode.RUN_GENERATION_MISMATCH, "run_generation", proposal.run_binding.run_generation, current.run_binding.run_generation),
    )
    for code, key, expected, actual in binding_pairs:
        if expected != actual:
            _reject(rejections, code, key, expected, actual)
    if not _scope_matches(policy.entity_scope, proposal.entity_scope, "") or proposal.entity_scope != current.entity_scope:
        _reject(rejections, CommandCASRejectionCode.ENTITY_SCOPE_MISMATCH, "entity_scope")
    if not _scope_matches(policy.subject_scope, proposal.subject_scope, "") or proposal.subject_scope != current.subject_scope:
        _reject(rejections, CommandCASRejectionCode.SUBJECT_SCOPE_MISMATCH, "subject_scope")
    if current.contract_pins is None:
        _reject(rejections, CommandCASRejectionCode.SEMANTIC_PIN_MISMATCH, "contract_pins_unavailable")
    else:
        expected_pins = dict(_pin_groups(proposal.contract_pins))
        actual_pins = dict(_pin_groups(current.contract_pins))
        semantic = {
            "workflow_contract_semantic_sha256",
            "scheduler_contract_semantic_sha256",
            "artifact_ownership_recording_semantic_sha256",
            "dirty_classifier_semantic_sha256",
            "persisted_dirty_owner_policy_semantic_sha256",
        }
        implementation = {
            "dirty_classifier_operational_implementation_sha256",
            "persisted_dirty_owner_policy_implementation_sha256",
        }
        for name in sorted(expected_pins):
            if expected_pins[name] == actual_pins[name]:
                continue
            if name.startswith("persisted_dirty_owner") and not (
                policy.requires_persisted_dirty_owner_pins
            ):
                continue
            if name == "runtime_contract_sha256":
                code = CommandCASRejectionCode.RUNTIME_PIN_MISMATCH
            elif name.startswith("persisted_dirty_owner") and policy.requires_persisted_dirty_owner_pins:
                code = CommandCASRejectionCode.PERSISTED_OWNER_PIN_MISMATCH
            elif name in semantic:
                code = CommandCASRejectionCode.SEMANTIC_PIN_MISMATCH
            elif name in implementation:
                code = CommandCASRejectionCode.IMPLEMENTATION_PIN_MISMATCH
            else:
                continue
            _reject(rejections, code, name, expected_pins[name], actual_pins[name])
    current_by_key = {(fact.fact_type, fact.fact_key): fact for fact in current.facts}
    required_keys = tuple((item.fact_type, item.fact_key) for item in policy.required_facts)
    rebuilt: list[ReadSetEntryV1] = []
    for requirement in policy.required_facts:
        fact_type = requirement.fact_type
        fact_key = requirement.fact_key
        fact = current_by_key.get((fact_type, fact_key))
        if fact is None or fact.availability is not SnapshotAvailabilityV0.AVAILABLE:
            _reject(rejections, CommandCASRejectionCode.REQUIRED_FACT_UNAVAILABLE, f"{fact_type.value}:{fact_key}")
            continue
        assert fact.value_sha256 is not None
        if requirement.scope_binding in {
            FactScopeBindingV1.ENTITY,
            FactScopeBindingV1.ENTITY_AND_SUBJECT,
        }:
            expected_generation = (
                current.entity_scope.entity_generation
                if type(current.entity_scope) is BoundEntityScopeV1
                else None
            )
            if fact.entity_generation != expected_generation:
                _reject(
                    rejections,
                    CommandCASRejectionCode.ENTITY_GENERATION_MISMATCH,
                    f"{fact_type.value}:{fact_key}",
                    str(expected_generation) if expected_generation is not None else None,
                    str(fact.entity_generation) if fact.entity_generation is not None else None,
                )
        if requirement.scope_binding in {
            FactScopeBindingV1.SUBJECT,
            FactScopeBindingV1.ENTITY_AND_SUBJECT,
        }:
            expected_subject = (
                current.subject_scope.subject_sha256
                if type(current.subject_scope) is BoundSubjectScopeV1
                else None
            )
            if fact.subject_sha256 != expected_subject:
                _reject(
                    rejections,
                    CommandCASRejectionCode.SUBJECT_FINGERPRINT_MISMATCH,
                    f"{fact_type.value}:{fact_key}",
                    expected_subject,
                    fact.subject_sha256,
                )
        rebuilt.append(
            ReadSetEntryV1(fact_type, fact_key, fact.value_sha256, fact.entity_generation, fact.subject_sha256)
        )
    expected_read_set = compile_read_set(tuple(rebuilt))
    if proposal.read_set != expected_read_set or tuple(
        (entry.fact_type, entry.fact_key) for entry in proposal.read_set.entries
    ) != required_keys:
        _reject(
            rejections,
            CommandCASRejectionCode.READ_SET_MISMATCH,
            "read_set",
            proposal.read_set.entries_sha256,
            expected_read_set.entries_sha256,
        )
    actual_hash = actual_payload_sha256(payload)
    if policy.payload_schema is None:
        payload_matches = type(proposal.payload_binding) is NoPayloadV1 and type(payload) is NoPayloadV1
    else:
        payload_matches = (
            type(proposal.payload_binding) is PayloadBindingV1
            and type(payload) is ActualPayloadV1
            and proposal.payload_binding.payload_schema == policy.payload_schema
            and payload.payload_schema == policy.payload_schema
            and proposal.payload_binding.payload_sha256 == actual_hash
        )
    if not payload_matches:
        expected_hash = proposal.payload_binding.payload_sha256 if type(proposal.payload_binding) is PayloadBindingV1 else None
        _reject(rejections, CommandCASRejectionCode.PAYLOAD_MISMATCH, "payload", expected_hash, actual_hash)
    ordered = tuple(sorted(set(rejections), key=lambda item: (item.code.value, item.fact_key, item.expected_sha256 or "", item.actual_sha256 or "")))
    return validate_command_cas_decision(
        CommandCASDecisionV1(
            schema_version=COMMAND_CAS_DECISION_SCHEMA,
            accepted_for_shadow_validation=not ordered,
            rejections=ordered,
            authoritative=False,
            proposed_mutations=(),
            performed_side_effects=(),
        )
    )


def validate_command_cas_decision(value: CommandCASDecisionV1) -> CommandCASDecisionV1:
    _exact(value, CommandCASDecisionV1, "decision")
    if value.schema_version != COMMAND_CAS_DECISION_SCHEMA:
        raise CommandEnvelopeValidationError("CAS decision schema is unsupported")
    if type(value.accepted_for_shadow_validation) is not bool:
        raise CommandEnvelopeValidationError("CAS accepted flag must be plain bool")
    if type(value.rejections) is not tuple:
        raise CommandEnvelopeValidationError("CAS rejections must be an immutable tuple")
    prior: tuple[str, str, str, str] | None = None
    for index, item in enumerate(value.rejections):
        path = f"decision.rejections[{index}]"
        _exact(item, CommandCASRejectionV1, path)
        _enum(item.code, CommandCASRejectionCode, f"{path}.code")
        _text(item.fact_key, f"{path}.fact_key")
        _optional_text(item.expected_sha256, f"{path}.expected_sha256")
        _optional_text(item.actual_sha256, f"{path}.actual_sha256")
        key = (item.code.value, item.fact_key, item.expected_sha256 or "", item.actual_sha256 or "")
        if prior is not None and key <= prior:
            raise CommandEnvelopeValidationError("CAS rejections must be unique and stable-sorted")
        prior = key
    if value.accepted_for_shadow_validation == bool(value.rejections):
        raise CommandEnvelopeValidationError("CAS accepted flag contradicts rejection set")
    if type(value.authoritative) is not bool or value.authoritative:
        raise CommandEnvelopeValidationError("CAS decision must be non-authoritative")
    for name in ("proposed_mutations", "performed_side_effects"):
        raw = getattr(value, name)
        if type(raw) is not tuple or raw:
            raise CommandEnvelopeValidationError(f"CAS decision {name} must be an empty tuple")
    return value


def command_cas_decision_bytes(value: CommandCASDecisionV1) -> bytes:
    decision = validate_command_cas_decision(value)
    try:
        return canonical_bytes(decision)
    except CanonicalizationError as exc:
        raise CommandEnvelopeValidationError("CAS decision cannot be canonicalized") from exc


def command_cas_decision_sha256(value: CommandCASDecisionV1) -> str:
    decision = validate_command_cas_decision(value)
    try:
        return canonical_sha256(decision)
    except CanonicalizationError as exc:
        raise CommandEnvelopeValidationError("CAS decision cannot be canonicalized") from exc


def compile_snapshot_fact_projection_v1() -> tuple[SnapshotFactProjectionV1, ...]:
    """Rebuild the only Snapshot V0 to CAS fact vocabulary mapping."""

    return (
        SnapshotFactProjectionV1(
            SnapshotSectionIdV0.PROJECT_STATE_SCHEMA,
            "project_state",
            "project",
            FactType.PROJECT_STATE,
            "project",
        ),
        SnapshotFactProjectionV1(
            SnapshotSectionIdV0.EVENT_HEAD_CHAIN,
            EVENT_HEAD_FACT_TYPE,
            "project",
            FactType.EVENT_HEAD,
            "project",
        ),
        SnapshotFactProjectionV1(
            SnapshotSectionIdV0.STAGE_CURSOR_CHECKPOINTS,
            "recorded_stage_cursor",
            "active",
            FactType.STAGE_CURSOR,
            "active",
        ),
        SnapshotFactProjectionV1(
            SnapshotSectionIdV0.PENDING_HUMAN,
            "recorded_pending_action",
            "active",
            FactType.PENDING_ACTION,
            "active",
        ),
        SnapshotFactProjectionV1(
            SnapshotSectionIdV0.DIRTY_FACTS,
            "solver_receipt_dirty_owner",
            "solver-receipt",
            FactType.DIRTY_OWNER,
            "solver-receipt",
        ),
        SnapshotFactProjectionV1(
            SnapshotSectionIdV0.INVOCATIONS_SOLVER,
            "bound_solver_job",
            "bound",
            FactType.SOLVER_JOB,
            "bound",
        ),
    )


def validate_snapshot_fact_projection_v1(
    value: tuple[SnapshotFactProjectionV1, ...],
) -> tuple[SnapshotFactProjectionV1, ...]:
    if type(value) is not tuple:
        raise CommandEnvelopeValidationError(
            "Snapshot fact projection must be an immutable tuple"
        )
    source_keys: list[tuple[str, str, str]] = []
    target_keys: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        if type(item) is not SnapshotFactProjectionV1:
            raise CommandEnvelopeValidationError(
                f"snapshot fact projection[{index}] has an unsupported runtime type"
            )
        _enum(item.section_id, SnapshotSectionIdV0, f"snapshot_projection[{index}].section_id")
        _text(item.source_fact_type, f"snapshot_projection[{index}].source_fact_type")
        _text(item.source_fact_key, f"snapshot_projection[{index}].source_fact_key")
        _enum(item.target_fact_type, FactType, f"snapshot_projection[{index}].target_fact_type")
        _text(item.target_fact_key, f"snapshot_projection[{index}].target_fact_key")
        source_keys.append(
            (item.section_id.value, item.source_fact_type, item.source_fact_key)
        )
        target_keys.append((item.target_fact_type.value, item.target_fact_key))
    if len(source_keys) != len(set(source_keys)) or len(target_keys) != len(set(target_keys)):
        raise CommandEnvelopeValidationError(
            "source-authorized Snapshot fact projection contains a duplicate mapping"
        )
    if value != compile_snapshot_fact_projection_v1():
        raise CommandEnvelopeValidationError(
            "Snapshot fact projection differs from the source-authorized mapping"
        )
    return value


def _bound_scopes_from_snapshot_facts(
    requirements: tuple[FactRequirementV1, ...],
    projected: dict[tuple[FactType, str], SnapshotFactV0],
) -> tuple[NoEntityScopeV1 | BoundEntityScopeV1, NoSubjectScopeV1 | BoundSubjectScopeV1]:
    entity_values = {
        (fact.entity_type, fact.entity_id, fact.entity_generation)
        for requirement in requirements
        if requirement.scope_binding
        in {FactScopeBindingV1.ENTITY, FactScopeBindingV1.ENTITY_AND_SUBJECT}
        for fact in (projected[(requirement.fact_type, requirement.fact_key)],)
    }
    subject_values = {
        (fact.subject_type, fact.subject_id, fact.subject_sha256)
        for requirement in requirements
        if requirement.scope_binding
        in {FactScopeBindingV1.SUBJECT, FactScopeBindingV1.ENTITY_AND_SUBJECT}
        for fact in (projected[(requirement.fact_type, requirement.fact_key)],)
    }
    if entity_values:
        if len(entity_values) != 1 or any(item is None for item in next(iter(entity_values))):
            raise CommandEnvelopeValidationError(
                "Snapshot entity-bound facts do not identify one complete entity scope"
            )
        entity_type, entity_id, entity_generation = next(iter(entity_values))
        assert entity_type is not None and entity_id is not None and entity_generation is not None
        entity_scope: NoEntityScopeV1 | BoundEntityScopeV1 = BoundEntityScopeV1(
            entity_type,
            entity_id,
            entity_generation,
        )
    else:
        entity_scope = NoEntityScopeV1()
    if subject_values:
        if len(subject_values) != 1 or any(item is None for item in next(iter(subject_values))):
            raise CommandEnvelopeValidationError(
                "Snapshot subject-bound facts do not identify one complete subject scope"
            )
        subject_type, subject_id, subject_sha256 = next(iter(subject_values))
        assert subject_type is not None and subject_id is not None and subject_sha256 is not None
        subject_scope: NoSubjectScopeV1 | BoundSubjectScopeV1 = BoundSubjectScopeV1(
            subject_type,
            subject_id,
            subject_sha256,
        )
    else:
        subject_scope = NoSubjectScopeV1()
    return entity_scope, subject_scope


def current_facts_from_snapshot(
    snapshot: ProjectSnapshotV0,
    command_type: CommandType,
    trusted_workflow: WorkflowContractBundleV2,
) -> CurrentFactsV1:
    """Project one source-authorized Snapshot fact vocabulary for a command policy."""

    try:
        value = validate_project_snapshot_v0(snapshot)
    except SnapshotV0ValidationError as exc:
        raise CommandEnvelopeValidationError(f"Snapshot structure is invalid: {exc}") from exc
    _enum(command_type, CommandType, "command_type")
    policy = validate_command_scope_policy(compile_command_scope_policy(command_type))
    if not policy.supported:
        raise CommandEnvelopeValidationError("unsupported command has no Snapshot fact projection")
    if value.coordinate.project_generation is None or value.coordinate.run_generation is None:
        raise CommandEnvelopeValidationError(
            "legacy PARTIAL snapshot has no project/run generation binding"
        )
    if value.contract_pins is None or value.coordinate.recorded_contract_pin_set_sha256 is None:
        raise CommandEnvelopeValidationError(
            "Snapshot has no recoverable source-authorized contract pin set"
        )
    try:
        validate_contract_pin_set(value.contract_pins, trusted_workflow)
        recorded_pin_sha = contract_pin_set_sha256(value.contract_pins, trusted_workflow)
    except Exception as exc:
        raise CommandEnvelopeValidationError(
            "Snapshot contract pins are not source-authorized"
        ) from exc
    if recorded_pin_sha != value.coordinate.recorded_contract_pin_set_sha256:
        raise CommandEnvelopeValidationError(
            "Snapshot coordinate differs from its recoverable contract pin set"
        )
    sections = {section.section_id: section for section in value.sections}
    mappings = {
        (item.target_fact_type, item.target_fact_key): item
        for item in validate_snapshot_fact_projection_v1(
            compile_snapshot_fact_projection_v1()
        )
    }
    facts: list[CurrentFactV1] = []
    projected_sources: dict[tuple[FactType, str], SnapshotFactV0] = {}
    for requirement in policy.required_facts:
        target = (requirement.fact_type, requirement.fact_key)
        mapping = mappings.get(target)
        if mapping is None:
            raise CommandEnvelopeValidationError(
                f"command policy fact lacks a source Snapshot mapping: {requirement.fact_type.value}:{requirement.fact_key}"
            )
        section = sections[mapping.section_id]
        if section.availability is not SnapshotAvailabilityV0.AVAILABLE:
            detail = section.gap_id or section.policy_id or section.page_cursor or (
                section.error_code.value if section.error_code is not None else "unavailable"
            )
            facts.append(
                CurrentFactV1(
                    requirement.fact_type,
                    requirement.fact_key,
                    section.availability,
                    None,
                    None,
                    None,
                    detail,
                )
            )
            continue
        matches = tuple(
            fact
            for fact in section.facts
            if fact.fact_type == mapping.source_fact_type
            and fact.fact_key == mapping.source_fact_key
        )
        if len(matches) != 1:
            raise CommandEnvelopeValidationError(
                f"Snapshot source fact projection is not unique: {mapping.source_fact_type}:{mapping.source_fact_key}"
            )
        source = matches[0]
        projected_sources[target] = source
        facts.append(
            CurrentFactV1(
                requirement.fact_type,
                requirement.fact_key,
                SnapshotAvailabilityV0.AVAILABLE,
                source.value_sha256,
                source.entity_generation,
                source.subject_sha256,
                None,
            )
        )
    if len(projected_sources) != len(policy.required_facts):
        # A PARTIAL Snapshot can still become CurrentFacts only when none of the
        # missing facts owns the command scope; CAS will reject their availability.
        bound_missing = any(
            requirement.scope_binding is not FactScopeBindingV1.NONE
            and (requirement.fact_type, requirement.fact_key) not in projected_sources
            for requirement in policy.required_facts
        )
        if bound_missing:
            raise CommandEnvelopeValidationError(
                "Snapshot unavailable fact prevents reconstruction of the required command scope"
            )
    entity_scope, subject_scope = _bound_scopes_from_snapshot_facts(
        tuple(
            requirement
            for requirement in policy.required_facts
            if (requirement.fact_type, requirement.fact_key) in projected_sources
        ),
        projected_sources,
    )
    return validate_current_facts(
        CurrentFactsV1(
            schema_version=CURRENT_FACTS_SCHEMA,
            snapshot_completeness=value.completeness,
            project_binding=ProjectGenerationBindingV1(
                value.coordinate.project_id,
                value.coordinate.project_generation,
                value.coordinate.project_revision,
            ),
            run_binding=RunGenerationBindingV1(
                value.coordinate.runtime_generation,
                value.coordinate.scheduler_generation,
                value.coordinate.run_generation,
            ),
            entity_scope=entity_scope,
            subject_scope=subject_scope,
            contract_pins=value.contract_pins,
            facts=tuple(sorted(facts, key=lambda item: (item.fact_type.value, item.fact_key))),
        )
    )
