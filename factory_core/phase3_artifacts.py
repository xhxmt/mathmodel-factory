"""Typed, deterministic Phase-3 artifact shadow contracts.

This module is packaged so a future explicitly selected Authority route can use
the same values as tests.  Importing it has no side effects, and no active
Scheduler, CLI, Web, process, provider, model, or Solver path imports it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import errno
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Iterable

from .artifact_ownership import ArtifactOwnership
from .canonical import canonical_sha256
from .owner_compiler import (
    OwnerCompilation,
    OwnerDiagnosticCode,
    OwnerPriorityAuthorization,
    OwnerRuleContract,
    compile_owner_registry,
    resolve_owner,
)


ARTIFACT_REGISTRATION_SCHEMA = "artifact-registration-shadow-v1"
ARTIFACT_RECORD_SCHEMA = "authority-artifact-record-v1"
ARTIFACT_OWNER_COMPILATION_BINDING_SCHEMA = (
    "authority-artifact-owner-compilation-binding-v1"
)
ARTIFACT_OWNER_OPERATOR_CLAIM_SCHEMA = (
    "authority-artifact-owner-operator-claim-v1"
)
ARTIFACT_OWNER_OPERATOR_AUTHORIZATION_SCHEMA = (
    "authority-artifact-owner-operator-authorization-v2"
)
ARTIFACT_OWNER_OPERATOR_ISSUER_KIND = "AUTHORITY_PRODUCTION_CONTROL_RECEIPT"
ARTIFACT_MANIFEST_SCHEMA = "authority-artifact-manifest-v3"
ARTIFACT_REMOVAL_SCHEMA = "authority-artifact-removal-v1"
CHANGE_SET_SCHEMA = "authority-artifact-change-set-v3"
REOPEN_PLAN_SCHEMA = "authority-reopen-plan-v1"
BLOCKED_NO_REOPEN_DISPOSITION_SCHEMA = (
    "authority-blocked-no-reopen-disposition-v1"
)
PHASE3_PREVIOUS_HEAD_SCHEMA = "authority-phase3-previous-head-v1"
CHECKPOINT_ENTRY_SCHEMA = "authority-checkpoint-ledger-entry-v1"
CHECKPOINT_REATTESTATION_SCHEMA = "authority-checkpoint-reattestation-dry-run-v1"
PARITY_RECEIPT_SCHEMA = "authority-artifact-parity-receipt-v1"
PHASE3_MUTATION_SCHEMA = "authority-phase3-mutation-v3"
ARTIFACT_OCCURRENCE_SCHEMA = "authority-artifact-ledger-occurrence-v1"
CHECKPOINT_OCCURRENCE_SCHEMA = "authority-checkpoint-ledger-occurrence-v1"


class Phase3ContractError(ValueError):
    """Raised when a Phase-3 value cannot be proved safe and deterministic."""


class ArtifactRegistrationError(Phase3ContractError):
    """Raised when an artifact owner cannot be frozen safely."""


class ArtifactAvailability(str, Enum):
    RECORDED = "RECORDED"
    LEGACY_UNKNOWN = "legacy_unknown"
    REDACTED = "REDACTED"
    ERROR = "ERROR"


class ArtifactBlockerCode(str, Enum):
    INVALID_PATH = "INVALID_PATH"
    ROOT_UNSAFE = "ROOT_UNSAFE"
    MISSING = "MISSING"
    SYMLINK = "SYMLINK"
    NOT_REGULAR = "NOT_REGULAR"
    UNREADABLE = "UNREADABLE"
    CHANGED_DURING_READ = "CHANGED_DURING_READ"
    OWNER_RESOLUTION_BLOCKED = "OWNER_RESOLUTION_BLOCKED"


class ArtifactChangeKind(str, Enum):
    ADDED = "ADDED"
    RESOLVED = "RESOLVED"
    MODIFIED = "MODIFIED"
    REMOVED = "REMOVED"
    UNCHANGED = "UNCHANGED"
    BLOCKED = "BLOCKED"


class ArtifactOccurrenceKind(str, Enum):
    RECORD = "RECORD"
    BLOCKER = "BLOCKER"
    REMOVAL = "REMOVAL"


class Phase3PreviousHeadKind(str, Enum):
    BOOTSTRAP = "BOOTSTRAP"
    CONTINUATION = "CONTINUATION"


class DirtyDisposition(str, Enum):
    CLEAN = "CLEAN"
    DIRTY = "DIRTY"
    BLOCKED = "BLOCKED"


class OwnerPolicyDisposition(str, Enum):
    NOT_EVALUATED = "NOT_EVALUATED"
    UNCHANGED = "UNCHANGED"
    MIGRATION_REQUIRED = "MIGRATION_REQUIRED"


class OwnerPolicyMigrationRequired(Phase3ContractError):
    """Raised instead of silently rewriting a historical frozen owner."""

    classification = OwnerPolicyDisposition.MIGRATION_REQUIRED
    reason_code = "OWNER_POLICY_CHANGED"


class CheckpointState(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"


class CheckpointTransition(str, Enum):
    RECORDED_VALID = "RECORDED_VALID"
    RECORDED_INVALID = "RECORDED_INVALID"
    INVALIDATED = "INVALIDATED"
    REATTESTED_VALID = "REATTESTED_VALID"
    REATTESTED_INVALID = "REATTESTED_INVALID"


class ReattestationClassification(str, Enum):
    REUSED = "REUSED"
    REGENERATED = "REGENERATED"
    STILL_INVALID = "STILL_INVALID"


class ParityClassification(str, Enum):
    MATCH = "MATCH"
    EXPECTED_DIFFERENCE = "EXPECTED_DIFFERENCE"
    DIVERGENCE = "DIVERGENCE"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ArtifactRegistration:
    schema_version: str
    normalized_path: str
    owner_compilation_sha256: str
    matching_rule_ids: tuple[str, ...]
    owner_rule_id: str
    owner_id: str
    owner_stage: int
    semantic_domain: str
    dirty_flag: str
    final_input: bool
    submission_member: bool
    registration_sha256: str

    def as_dict(self) -> dict[str, object]:
        value = _registration_identity(self)
        value["registration_sha256"] = self.registration_sha256
        return value


@dataclass(frozen=True)
class ArtifactRecord:
    schema_version: str
    artifact_record_id: str
    artifact_type: str
    normalized_path: str
    content_sha256: str | None
    byte_length: int | None
    availability: ArtifactAvailability
    registration: ArtifactRegistration
    record_sha256: str

    def as_dict(self) -> dict[str, object]:
        value = _artifact_record_identity(self)
        value["artifact_record_id"] = self.artifact_record_id
        value["record_sha256"] = self.record_sha256
        return value


@dataclass(frozen=True)
class ArtifactOwnerCompilationBinding:
    schema_version: str
    registry: tuple[ArtifactOwnership, ...]
    priority_authorizations: tuple[OwnerPriorityAuthorization, ...]
    owner_compilation_sha256: str
    binding_sha256: str

    def as_dict(self) -> dict[str, object]:
        value = _artifact_owner_compilation_binding_identity(self)
        value["binding_sha256"] = self.binding_sha256
        return value


@dataclass(frozen=True)
class ArtifactOwnerOperatorClaim:
    schema_version: str
    workflow_id: str
    source_revision: int
    command_id: str
    normalized_path: str
    owner_compilation: ArtifactOwnerCompilationBinding
    owner_compilation_sha256: str
    owner_id: str
    owner_stage: int
    dirty_flag: str
    operator_subject: str
    reason_code: str
    claim_sha256: str

    def as_dict(self) -> dict[str, object]:
        value = _artifact_owner_operator_claim_identity(self)
        value["claim_sha256"] = self.claim_sha256
        return value


@dataclass(frozen=True)
class ArtifactOwnerOperatorAuthorization:
    schema_version: str
    workflow_id: str
    source_revision: int
    command_id: str
    normalized_path: str
    owner_compilation: ArtifactOwnerCompilationBinding
    owner_compilation_sha256: str
    owner_id: str
    owner_stage: int
    dirty_flag: str
    operator_subject: str
    reason_code: str
    claim_sha256: str
    issuer_kind: str
    issuer_writer_id: str
    issuer_writer_epoch: int
    issuer_receipt_id: str
    issuer_receipt_sha256: str
    authorization_sha256: str

    def as_dict(self) -> dict[str, object]:
        value = _artifact_owner_operator_authorization_identity(self)
        value["authorization_sha256"] = self.authorization_sha256
        return value


@dataclass(frozen=True)
class ArtifactBlocker:
    code: ArtifactBlockerCode
    normalized_path: str
    detail: str
    registration: ArtifactRegistration | None = None
    operator_authorization: ArtifactOwnerOperatorAuthorization | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "normalized_path": self.normalized_path,
            "detail": self.detail,
            "registration": (
                None if self.registration is None else self.registration.as_dict()
            ),
            "operator_authorization": (
                None
                if self.operator_authorization is None
                else self.operator_authorization.as_dict()
            ),
        }


@dataclass(frozen=True)
class ArtifactManifest:
    schema_version: str
    owner_compilation_sha256: str
    tracked_paths: tuple[str, ...]
    records: tuple[ArtifactRecord, ...]
    blockers: tuple[ArtifactBlocker, ...]
    manifest_sha256: str

    def as_dict(self) -> dict[str, object]:
        value = _artifact_manifest_identity(self)
        value["manifest_sha256"] = self.manifest_sha256
        return value


@dataclass(frozen=True)
class ArtifactChange:
    normalized_path: str
    kind: ArtifactChangeKind
    previous_record_sha256: str | None
    current_record_sha256: str | None
    previous_blocker_code: str | None = None
    removal_sha256: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "normalized_path": self.normalized_path,
            "kind": self.kind.value,
            "previous_record_sha256": self.previous_record_sha256,
            "current_record_sha256": self.current_record_sha256,
            "previous_blocker_code": self.previous_blocker_code,
            "removal_sha256": self.removal_sha256,
        }


@dataclass(frozen=True)
class ArtifactRemoval:
    schema_version: str
    normalized_path: str
    previous_record_sha256: str | None
    previous_blocker_code: str | None
    owner_compilation_sha256: str
    owner_id: str
    owner_stage: int
    dirty_flag: str
    reason_code: str
    removal_sha256: str

    def as_dict(self) -> dict[str, object]:
        value = _artifact_removal_identity(self)
        value["removal_sha256"] = self.removal_sha256
        return value


@dataclass(frozen=True)
class DirtyDecision:
    normalized_path: str
    disposition: DirtyDisposition
    dirty_flag: str | None
    owner_id: str | None
    owner_stage: int | None
    reason_code: str

    def as_dict(self) -> dict[str, object]:
        return {
            "normalized_path": self.normalized_path,
            "disposition": self.disposition.value,
            "dirty_flag": self.dirty_flag,
            "owner_id": self.owner_id,
            "owner_stage": self.owner_stage,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class ChangeSet:
    schema_version: str
    previous_manifest_sha256: str
    current_manifest_sha256: str
    removals: tuple[ArtifactRemoval, ...]
    changes: tuple[ArtifactChange, ...]
    dirty_decisions: tuple[DirtyDecision, ...]
    change_set_sha256: str

    def as_dict(self) -> dict[str, object]:
        value = _change_set_identity(self)
        value["change_set_sha256"] = self.change_set_sha256
        return value


@dataclass(frozen=True)
class ArtifactReadExpectation:
    normalized_path: str
    expected_artifact_record_id: str | None
    expected_record_sha256: str | None
    expected_blocker_code: str | None = None
    expected_occurrence_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "normalized_path": self.normalized_path,
            "expected_artifact_record_id": self.expected_artifact_record_id,
            "expected_record_sha256": self.expected_record_sha256,
            "expected_blocker_code": self.expected_blocker_code,
            "expected_occurrence_id": self.expected_occurrence_id,
        }


@dataclass(frozen=True)
class ReopenPlan:
    schema_version: str
    reopen_plan_id: str
    workflow_id: str
    source_revision: int
    target_scope: str
    target_owner_stage: int
    reason_code: str
    change_set_sha256: str
    read_set: tuple[ArtifactReadExpectation, ...]
    read_set_sha256: str
    plan_sha256: str

    def as_dict(self) -> dict[str, object]:
        value = _reopen_plan_identity(self)
        value["reopen_plan_id"] = self.reopen_plan_id
        value["plan_sha256"] = self.plan_sha256
        return value


@dataclass(frozen=True)
class BlockedNoReopenDisposition:
    schema_version: str
    disposition_id: str
    workflow_id: str
    source_revision: int
    target_scope: str
    target_owner_stage: int
    reason_code: str
    change_set_sha256: str
    read_set: tuple[ArtifactReadExpectation, ...]
    read_set_sha256: str
    disposition_sha256: str

    def as_dict(self) -> dict[str, object]:
        value = _blocked_no_reopen_disposition_identity(self)
        value["disposition_id"] = self.disposition_id
        value["disposition_sha256"] = self.disposition_sha256
        return value


@dataclass(frozen=True)
class CheckpointLedgerEntry:
    schema_version: str
    checkpoint_id: str
    checkpoint_key: str
    state: CheckpointState
    transition: CheckpointTransition
    owner_stage: int
    source_record_key: str
    input_manifest_sha256: str
    validation_sha256: str | None
    previous_checkpoint_id: str | None
    reason_code: str
    checkpoint_sha256: str
    previous_checkpoint_occurrence_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        value = _checkpoint_identity(self)
        value["checkpoint_id"] = self.checkpoint_id
        value["source_record_key"] = self.source_record_key
        value["checkpoint_sha256"] = self.checkpoint_sha256
        value["previous_checkpoint_occurrence_id"] = (
            self.previous_checkpoint_occurrence_id
        )
        return value


@dataclass(frozen=True)
class CheckpointReattestationReceipt:
    schema_version: str
    previous_checkpoint_id: str
    previous_manifest_sha256: str
    current_manifest_sha256: str
    regenerated_validation_sha256: str | None
    classification: ReattestationClassification
    would_write: bool
    receipt_sha256: str

    def as_dict(self) -> dict[str, object]:
        value = _reattestation_identity(self)
        value["receipt_sha256"] = self.receipt_sha256
        return value


@dataclass(frozen=True)
class ParityReceipt:
    schema_version: str
    subject: str
    v1_sha256: str | None
    shadow_sha256: str | None
    classification: ParityClassification
    difference_codes: tuple[str, ...]
    blocker_codes: tuple[str, ...]
    receipt_sha256: str

    def as_dict(self) -> dict[str, object]:
        value = _parity_identity(self)
        value["receipt_sha256"] = self.receipt_sha256
        return value


@dataclass(frozen=True)
class Phase3Mutation:
    schema_version: str
    artifact_records: tuple[ArtifactRecord, ...]
    checkpoint_entries: tuple[CheckpointLedgerEntry, ...]
    reopen_plan: ReopenPlan | None
    mutation_sha256: str
    previous_manifest: ArtifactManifest | None = None
    current_manifest: ArtifactManifest | None = None
    change_set: ChangeSet | None = None
    artifact_blockers: tuple[ArtifactBlocker, ...] = ()
    removals: tuple[ArtifactRemoval, ...] = ()
    blocked_disposition: BlockedNoReopenDisposition | None = None
    previous_head: Phase3PreviousHead | None = None

    def as_dict(self) -> dict[str, object]:
        value = _mutation_identity(self)
        value["mutation_sha256"] = self.mutation_sha256
        return value


@dataclass(frozen=True)
class Phase3PreviousHead:
    schema_version: str
    kind: Phase3PreviousHeadKind
    workflow_id: str
    source_revision: int
    previous_manifest_sha256: str
    previous_revision: int | None
    previous_command_id: str | None
    previous_mutation_sha256: str | None
    head_sha256: str

    def as_dict(self) -> dict[str, object]:
        value = _phase3_previous_head_identity(self)
        value["head_sha256"] = self.head_sha256
        return value


@dataclass(frozen=True)
class ArtifactLedgerOccurrence:
    schema_version: str
    occurrence_id: str
    workflow_id: str
    revision: int
    command_id: str
    mutation_sha256: str
    kind: ArtifactOccurrenceKind
    normalized_path: str
    semantic_sha256: str
    artifact_record: ArtifactRecord | None
    blocker: ArtifactBlocker | None
    removal: ArtifactRemoval | None

    def as_dict(self) -> dict[str, object]:
        value = _artifact_occurrence_identity(self)
        value["occurrence_id"] = self.occurrence_id
        return value


@dataclass(frozen=True)
class CheckpointLedgerOccurrence:
    schema_version: str
    occurrence_id: str
    workflow_id: str
    revision: int
    command_id: str
    mutation_sha256: str
    checkpoint_entry: CheckpointLedgerEntry

    def as_dict(self) -> dict[str, object]:
        value = _checkpoint_occurrence_identity(self)
        value["occurrence_id"] = self.occurrence_id
        return value


_UNSAFE_RESOLUTION_CODES = {
    OwnerDiagnosticCode.NO_OWNER,
    OwnerDiagnosticCode.MULTIPLE_MATCH,
    OwnerDiagnosticCode.SHADOWED,
    OwnerDiagnosticCode.UNREACHABLE,
    OwnerDiagnosticCode.UNANALYZABLE,
}


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise Phase3ContractError(f"{field} must be a non-empty string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise Phase3ContractError(f"{field} must contain valid UTF-8") from exc
    return value


def _identifier(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) > 160 or any(not (char.isalnum() or char in "._:-") for char in text):
        raise Phase3ContractError(f"{field} must be a bounded identifier")
    return text


def _require_sha256(value: object, field: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise Phase3ContractError(f"{field} must be lowercase SHA-256 hex")
    return value


def _positive(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise Phase3ContractError(f"{field} must be a positive integer")
    return value


def _nonnegative(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise Phase3ContractError(f"{field} must be a non-negative integer")
    return value


def _checkpoint_key_owner_stage(value: object) -> int:
    key = _identifier(value, "checkpoint_key")
    prefix = "phase3:stage"
    if not key.startswith(prefix) or "." not in key[len(prefix) :]:
        raise Phase3ContractError(
            "checkpoint_key must use phase3:stage<N>.<name> namespace"
        )
    stage_text, suffix = key[len(prefix) :].split(".", 1)
    if not stage_text.isdigit() or int(stage_text) < 1 or not suffix:
        raise Phase3ContractError(
            "checkpoint_key must use phase3:stage<N>.<name> namespace"
        )
    return int(stage_text)


def _normalize_registration_path(path: str) -> str:
    if type(path) is not str or not path:
        raise ArtifactRegistrationError("artifact path must be a non-empty string")
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise ArtifactRegistrationError("artifact path contains a control character")
    candidate = path.replace("\\", "/")
    raw_parts = candidate.split("/")
    pure = PurePosixPath(candidate)
    has_windows_drive = (
        len(candidate) >= 2 and candidate[0].isalpha() and candidate[1] == ":"
    )
    if (
        pure.is_absolute()
        or has_windows_drive
        or any(part in {"", ".", ".."} for part in raw_parts)
    ):
        raise ArtifactRegistrationError(
            "artifact path must be a normalized project-relative path"
        )
    normalized = pure.as_posix()
    if normalized != candidate:
        raise ArtifactRegistrationError(
            "artifact path must not depend on dot-segment normalization"
        )
    return normalized


def _invalid_path_sentinel(value: object) -> str:
    """Return a path-safe deterministic inventory key without serializing repr()."""

    if type(value) is not str:
        return "__phase3_invalid_path__/non_string"
    try:
        raw = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return "__phase3_invalid_path__/invalid_unicode"
    return "__phase3_invalid_path__/string-" + hashlib.sha256(raw).hexdigest()


def normalize_artifact_path(path: str) -> str:
    """Return the one accepted project-relative spelling for an artifact path."""

    return _normalize_registration_path(path)


def owner_compilation_semantic_sha256(compilation: OwnerCompilation) -> str:
    if type(compilation) is not OwnerCompilation:
        raise ArtifactRegistrationError("compilation must be OwnerCompilation")
    if type(compilation.rules) is not tuple or any(
        type(rule) is not OwnerRuleContract for rule in compilation.rules
    ):
        raise ArtifactRegistrationError("compilation rules must be frozen owner rules")
    return canonical_sha256(
        {
            "schema_version": compilation.schema_version,
            "mode": compilation.mode,
            "ownership_schema_version": compilation.ownership_schema_version,
            "rules": compilation.rules,
        }
    )


def _registration_identity(record: ArtifactRegistration) -> dict[str, object]:
    return {
        "schema_version": record.schema_version,
        "normalized_path": record.normalized_path,
        "owner_compilation_sha256": record.owner_compilation_sha256,
        "matching_rule_ids": record.matching_rule_ids,
        "owner_rule_id": record.owner_rule_id,
        "owner_id": record.owner_id,
        "owner_stage": record.owner_stage,
        "semantic_domain": record.semantic_domain,
        "dirty_flag": record.dirty_flag,
        "final_input": record.final_input,
        "submission_member": record.submission_member,
    }


def register_artifact_owner(
    compilation: OwnerCompilation, path: str
) -> ArtifactRegistration:
    normalized = _normalize_registration_path(path)
    compilation_sha256 = owner_compilation_semantic_sha256(compilation)
    resolution = resolve_owner(compilation, normalized)
    unsafe = sorted(
        {
            diagnostic.code.value
            for diagnostic in resolution.diagnostics
            if diagnostic.code in _UNSAFE_RESOLUTION_CODES
        }
    )
    if unsafe:
        raise ArtifactRegistrationError(
            "artifact registration rejected owner diagnostics: " + ", ".join(unsafe)
        )
    if not resolution.all_matches or resolution.resolved_rule_id is None:
        raise ArtifactRegistrationError("artifact registration has no frozen owner")
    winner = resolution.all_matches[0]
    if (
        resolution.resolved_rule_id != winner.rule_id
        or resolution.resolved_owner_id != winner.owner_id
        or resolution.resolved_owner_stage != winner.owner_stage
    ):
        raise ArtifactRegistrationError("artifact owner resolution is inconsistent")
    fields: dict[str, object] = {
        "schema_version": ARTIFACT_REGISTRATION_SCHEMA,
        "normalized_path": normalized,
        "owner_compilation_sha256": compilation_sha256,
        "matching_rule_ids": tuple(rule.rule_id for rule in resolution.all_matches),
        "owner_rule_id": winner.rule_id,
        "owner_id": winner.owner_id,
        "owner_stage": winner.owner_stage,
        "semantic_domain": winner.semantic_domain,
        "dirty_flag": winner.dirty_flag,
        "final_input": winner.final_input,
        "submission_member": winner.submission_member,
    }
    return validate_artifact_registration(
        ArtifactRegistration(**fields, registration_sha256=canonical_sha256(fields))
    )


def validate_artifact_registration(
    record: ArtifactRegistration,
    *,
    expected_compilation: OwnerCompilation | None = None,
) -> ArtifactRegistration:
    if type(record) is not ArtifactRegistration:
        raise ArtifactRegistrationError("record must be ArtifactRegistration")
    if record.schema_version != ARTIFACT_REGISTRATION_SCHEMA:
        raise ArtifactRegistrationError("unsupported artifact registration schema")
    if _normalize_registration_path(record.normalized_path) != record.normalized_path:
        raise ArtifactRegistrationError("record path is not normalized")
    _require_sha256(record.owner_compilation_sha256, "owner_compilation_sha256")
    _require_sha256(record.registration_sha256, "registration_sha256")
    if type(record.matching_rule_ids) is not tuple or not record.matching_rule_ids:
        raise ArtifactRegistrationError("matching_rule_ids must be a non-empty tuple")
    if any(type(rule_id) is not str or not rule_id for rule_id in record.matching_rule_ids):
        raise ArtifactRegistrationError("matching_rule_ids must contain strings")
    if len(set(record.matching_rule_ids)) != len(record.matching_rule_ids):
        raise ArtifactRegistrationError("matching_rule_ids must be unique")
    if record.owner_rule_id != record.matching_rule_ids[0]:
        raise ArtifactRegistrationError("owner_rule_id must be the first matching rule")
    if type(record.owner_stage) is not int or record.owner_stage < 1:
        raise ArtifactRegistrationError("owner_stage must be a positive integer")
    if record.owner_id != f"owner:stage:{record.owner_stage}":
        raise ArtifactRegistrationError("owner_id and owner_stage disagree")
    for field_name in ("owner_rule_id", "owner_id", "semantic_domain", "dirty_flag"):
        if type(getattr(record, field_name)) is not str or not getattr(record, field_name):
            raise ArtifactRegistrationError(f"{field_name} must be a non-empty string")
    if type(record.final_input) is not bool or type(record.submission_member) is not bool:
        raise ArtifactRegistrationError("artifact membership fields must be booleans")
    if record.registration_sha256 != canonical_sha256(_registration_identity(record)):
        raise ArtifactRegistrationError("artifact registration identity mismatch")
    if expected_compilation is not None:
        expected_sha256 = owner_compilation_semantic_sha256(expected_compilation)
        if record.owner_compilation_sha256 != expected_sha256:
            raise ArtifactRegistrationError("loaded owner compilation identity mismatch")
    return record


def _artifact_record_identity(record: ArtifactRecord) -> dict[str, object]:
    return {
        "schema_version": record.schema_version,
        "artifact_type": record.artifact_type,
        "normalized_path": record.normalized_path,
        "content_sha256": record.content_sha256,
        "byte_length": record.byte_length,
        "availability": record.availability.value,
        "registration": record.registration.as_dict(),
    }


def _artifact_ownership_as_dict(value: ArtifactOwnership) -> dict[str, object]:
    return {
        "pattern": value.pattern,
        "owner_stage": value.owner_stage,
        "semantic_domain": value.semantic_domain,
        "dirty_flag": value.dirty_flag,
        "final_input": value.final_input,
        "submission_member": value.submission_member,
    }


def _owner_priority_authorization_as_dict(
    value: OwnerPriorityAuthorization,
) -> dict[str, object]:
    return {
        "winner_pattern": value.winner_pattern,
        "winner_owner_stage": value.winner_owner_stage,
        "loser_pattern": value.loser_pattern,
        "loser_owner_stage": value.loser_owner_stage,
        "issue_id": value.issue_id,
        "rationale": value.rationale,
    }


def _artifact_owner_compilation_binding_identity(
    binding: ArtifactOwnerCompilationBinding,
) -> dict[str, object]:
    return {
        "schema_version": binding.schema_version,
        "registry": tuple(
            _artifact_ownership_as_dict(item) for item in binding.registry
        ),
        "priority_authorizations": tuple(
            _owner_priority_authorization_as_dict(item)
            for item in binding.priority_authorizations
        ),
        "owner_compilation_sha256": binding.owner_compilation_sha256,
    }


def _priority_authorization_sort_key(
    value: OwnerPriorityAuthorization,
) -> tuple[object, ...]:
    return (
        value.winner_pattern,
        value.winner_owner_stage,
        value.loser_pattern,
        value.loser_owner_stage,
        value.issue_id,
        value.rationale,
    )


def build_artifact_owner_compilation_binding(
    compilation: OwnerCompilation,
) -> ArtifactOwnerCompilationBinding:
    policy_sha256 = owner_compilation_semantic_sha256(compilation)
    registry = tuple(
        ArtifactOwnership(
            rule.pattern,
            rule.owner_stage,
            rule.semantic_domain,
            rule.dirty_flag,
            rule.final_input,
            rule.submission_member,
        )
        for rule in compilation.rules
    )
    priority_authorizations = tuple(
        sorted(
            {
                authorization
                for rule in compilation.rules
                for authorization in rule.priority_authorizations
            },
            key=_priority_authorization_sort_key,
        )
    )
    prototype = ArtifactOwnerCompilationBinding(
        ARTIFACT_OWNER_COMPILATION_BINDING_SCHEMA,
        registry,
        priority_authorizations,
        policy_sha256,
        "0" * 64,
    )
    return validate_artifact_owner_compilation_binding(
        replace(
            prototype,
            binding_sha256=canonical_sha256(
                _artifact_owner_compilation_binding_identity(prototype)
            ),
        )
    )


def validate_artifact_owner_compilation_binding(
    binding: ArtifactOwnerCompilationBinding,
) -> ArtifactOwnerCompilationBinding:
    if (
        type(binding) is not ArtifactOwnerCompilationBinding
        or binding.schema_version
        != ARTIFACT_OWNER_COMPILATION_BINDING_SCHEMA
    ):
        raise Phase3ContractError("unsupported artifact owner compilation binding")
    if type(binding.registry) is not tuple or any(
        type(item) is not ArtifactOwnership for item in binding.registry
    ):
        raise Phase3ContractError("owner compilation registry must be a frozen tuple")
    for item in binding.registry:
        _text(item.pattern, "owner compilation pattern")
        _positive(item.owner_stage, "owner compilation owner_stage")
        _identifier(item.semantic_domain, "owner compilation semantic_domain")
        _identifier(item.dirty_flag, "owner compilation dirty_flag")
        if type(item.final_input) is not bool or type(item.submission_member) is not bool:
            raise Phase3ContractError(
                "owner compilation membership values must be booleans"
            )
    if type(binding.priority_authorizations) is not tuple or any(
        type(item) is not OwnerPriorityAuthorization
        for item in binding.priority_authorizations
    ):
        raise Phase3ContractError(
            "owner priority authorizations must be a frozen tuple"
        )
    if tuple(
        sorted(binding.priority_authorizations, key=_priority_authorization_sort_key)
    ) != binding.priority_authorizations or len(set(binding.priority_authorizations)) != len(
        binding.priority_authorizations
    ):
        raise Phase3ContractError(
            "owner priority authorizations must be uniquely sorted"
        )
    for item in binding.priority_authorizations:
        _text(item.winner_pattern, "owner priority winner_pattern")
        _positive(item.winner_owner_stage, "owner priority winner_owner_stage")
        _text(item.loser_pattern, "owner priority loser_pattern")
        _positive(item.loser_owner_stage, "owner priority loser_owner_stage")
        _identifier(item.issue_id, "owner priority issue_id")
        _text(item.rationale, "owner priority rationale")
    _require_sha256(
        binding.owner_compilation_sha256,
        "owner compilation binding owner_compilation_sha256",
    )
    _require_sha256(binding.binding_sha256, "owner compilation binding_sha256")
    if binding.binding_sha256 != canonical_sha256(
        _artifact_owner_compilation_binding_identity(binding)
    ):
        raise Phase3ContractError("artifact owner compilation binding mismatch")
    compiled = compile_owner_registry(
        binding.registry,
        priority_authorizations=binding.priority_authorizations,
    )
    if owner_compilation_semantic_sha256(compiled) != (
        binding.owner_compilation_sha256
    ):
        raise Phase3ContractError("artifact owner compilation identity mismatch")
    return binding


def owner_compilation_from_binding(
    binding: ArtifactOwnerCompilationBinding,
) -> OwnerCompilation:
    validate_artifact_owner_compilation_binding(binding)
    return compile_owner_registry(
        binding.registry,
        priority_authorizations=binding.priority_authorizations,
    )


def _artifact_owner_operator_claim_identity(
    claim: ArtifactOwnerOperatorClaim,
) -> dict[str, object]:
    return {
        "schema_version": claim.schema_version,
        "workflow_id": claim.workflow_id,
        "source_revision": claim.source_revision,
        "command_id": claim.command_id,
        "normalized_path": claim.normalized_path,
        "owner_compilation": claim.owner_compilation.as_dict(),
        "owner_compilation_sha256": claim.owner_compilation_sha256,
        "owner_id": claim.owner_id,
        "owner_stage": claim.owner_stage,
        "dirty_flag": claim.dirty_flag,
        "operator_subject": claim.operator_subject,
        "reason_code": claim.reason_code,
    }


def build_artifact_owner_operator_claim(
    *,
    workflow_id: str,
    source_revision: int,
    command_id: str,
    normalized_path: str,
    owner_compilation: OwnerCompilation,
    owner_id: str,
    owner_stage: int,
    dirty_flag: str,
    operator_subject: str,
    reason_code: str,
) -> ArtifactOwnerOperatorClaim:
    binding = build_artifact_owner_compilation_binding(owner_compilation)
    prototype = ArtifactOwnerOperatorClaim(
        ARTIFACT_OWNER_OPERATOR_CLAIM_SCHEMA,
        _identifier(workflow_id, "workflow_id"),
        _positive(source_revision, "source_revision"),
        _identifier(command_id, "command_id"),
        _normalize_registration_path(normalized_path),
        binding,
        binding.owner_compilation_sha256,
        _identifier(owner_id, "owner_id"),
        _positive(owner_stage, "owner_stage"),
        _identifier(dirty_flag, "dirty_flag"),
        _identifier(operator_subject, "operator_subject"),
        _identifier(reason_code, "reason_code"),
        "0" * 64,
    )
    return validate_artifact_owner_operator_claim(
        replace(
            prototype,
            claim_sha256=canonical_sha256(
                _artifact_owner_operator_claim_identity(prototype)
            ),
        )
    )


def validate_artifact_owner_operator_claim(
    claim: ArtifactOwnerOperatorClaim,
) -> ArtifactOwnerOperatorClaim:
    if (
        type(claim) is not ArtifactOwnerOperatorClaim
        or claim.schema_version != ARTIFACT_OWNER_OPERATOR_CLAIM_SCHEMA
    ):
        raise Phase3ContractError("unsupported artifact owner operator claim")
    _identifier(claim.workflow_id, "claim.workflow_id")
    _positive(claim.source_revision, "claim.source_revision")
    _identifier(claim.command_id, "claim.command_id")
    if _normalize_registration_path(claim.normalized_path) != claim.normalized_path:
        raise Phase3ContractError("artifact owner operator claim path is not normalized")
    validate_artifact_owner_compilation_binding(claim.owner_compilation)
    _require_sha256(
        claim.owner_compilation_sha256,
        "claim.owner_compilation_sha256",
    )
    if claim.owner_compilation_sha256 != claim.owner_compilation.owner_compilation_sha256:
        raise Phase3ContractError("artifact owner operator claim policy differs")
    _identifier(claim.owner_id, "claim.owner_id")
    _positive(claim.owner_stage, "claim.owner_stage")
    if claim.owner_id != f"owner:stage:{claim.owner_stage}":
        raise Phase3ContractError("artifact owner operator claim owner identity differs")
    _identifier(claim.dirty_flag, "claim.dirty_flag")
    _identifier(claim.operator_subject, "claim.operator_subject")
    _identifier(claim.reason_code, "claim.reason_code")
    _require_sha256(claim.claim_sha256, "claim.claim_sha256")
    if claim.claim_sha256 != canonical_sha256(
        _artifact_owner_operator_claim_identity(claim)
    ):
        raise Phase3ContractError("artifact owner operator claim identity mismatch")
    return claim


def _artifact_owner_operator_authorization_identity(
    authorization: ArtifactOwnerOperatorAuthorization,
) -> dict[str, object]:
    return {
        "schema_version": authorization.schema_version,
        "workflow_id": authorization.workflow_id,
        "source_revision": authorization.source_revision,
        "command_id": authorization.command_id,
        "normalized_path": authorization.normalized_path,
        "owner_compilation": authorization.owner_compilation.as_dict(),
        "owner_compilation_sha256": authorization.owner_compilation_sha256,
        "owner_id": authorization.owner_id,
        "owner_stage": authorization.owner_stage,
        "dirty_flag": authorization.dirty_flag,
        "operator_subject": authorization.operator_subject,
        "reason_code": authorization.reason_code,
        "claim_sha256": authorization.claim_sha256,
        "issuer_kind": authorization.issuer_kind,
        "issuer_writer_id": authorization.issuer_writer_id,
        "issuer_writer_epoch": authorization.issuer_writer_epoch,
        "issuer_receipt_id": authorization.issuer_receipt_id,
        "issuer_receipt_sha256": authorization.issuer_receipt_sha256,
    }


def artifact_owner_operator_claim_from_authorization(
    authorization: ArtifactOwnerOperatorAuthorization,
) -> ArtifactOwnerOperatorClaim:
    return validate_artifact_owner_operator_claim(
        ArtifactOwnerOperatorClaim(
            ARTIFACT_OWNER_OPERATOR_CLAIM_SCHEMA,
            authorization.workflow_id,
            authorization.source_revision,
            authorization.command_id,
            authorization.normalized_path,
            authorization.owner_compilation,
            authorization.owner_compilation_sha256,
            authorization.owner_id,
            authorization.owner_stage,
            authorization.dirty_flag,
            authorization.operator_subject,
            authorization.reason_code,
            authorization.claim_sha256,
        )
    )


def build_artifact_owner_operator_authorization(
    claim: ArtifactOwnerOperatorClaim,
    *,
    issuer_writer_id: str,
    issuer_writer_epoch: int,
    issuer_receipt_id: str,
    issuer_receipt_sha256: str,
) -> ArtifactOwnerOperatorAuthorization:
    checked = validate_artifact_owner_operator_claim(claim)
    prototype = ArtifactOwnerOperatorAuthorization(
        ARTIFACT_OWNER_OPERATOR_AUTHORIZATION_SCHEMA,
        checked.workflow_id,
        checked.source_revision,
        checked.command_id,
        checked.normalized_path,
        checked.owner_compilation,
        checked.owner_compilation_sha256,
        checked.owner_id,
        checked.owner_stage,
        checked.dirty_flag,
        checked.operator_subject,
        checked.reason_code,
        checked.claim_sha256,
        ARTIFACT_OWNER_OPERATOR_ISSUER_KIND,
        _identifier(issuer_writer_id, "issuer_writer_id"),
        _positive(issuer_writer_epoch, "issuer_writer_epoch"),
        _identifier(issuer_receipt_id, "issuer_receipt_id"),
        str(_require_sha256(issuer_receipt_sha256, "issuer_receipt_sha256")),
        "0" * 64,
    )
    return validate_artifact_owner_operator_authorization(
        replace(
            prototype,
            authorization_sha256=canonical_sha256(
                _artifact_owner_operator_authorization_identity(prototype)
            ),
        )
    )


def validate_artifact_owner_operator_authorization(
    authorization: ArtifactOwnerOperatorAuthorization,
) -> ArtifactOwnerOperatorAuthorization:
    if (
        type(authorization) is not ArtifactOwnerOperatorAuthorization
        or authorization.schema_version
        != ARTIFACT_OWNER_OPERATOR_AUTHORIZATION_SCHEMA
    ):
        raise Phase3ContractError(
            "unsupported artifact owner operator authorization"
        )
    artifact_owner_operator_claim_from_authorization(authorization)
    if authorization.issuer_kind != ARTIFACT_OWNER_OPERATOR_ISSUER_KIND:
        raise Phase3ContractError("artifact owner operator issuer differs")
    _identifier(authorization.issuer_writer_id, "authorization.issuer_writer_id")
    _positive(authorization.issuer_writer_epoch, "authorization.issuer_writer_epoch")
    _identifier(authorization.issuer_receipt_id, "authorization.issuer_receipt_id")
    _require_sha256(
        authorization.issuer_receipt_sha256,
        "authorization.issuer_receipt_sha256",
    )
    _require_sha256(
        authorization.authorization_sha256,
        "authorization.authorization_sha256",
    )
    if authorization.authorization_sha256 != canonical_sha256(
        _artifact_owner_operator_authorization_identity(authorization)
    ):
        raise Phase3ContractError(
            "artifact owner operator authorization identity mismatch"
        )
    return authorization


def build_artifact_record(
    registration: ArtifactRegistration,
    *,
    content: bytes | None,
    availability: ArtifactAvailability = ArtifactAvailability.RECORDED,
    artifact_type: str = "PROJECT_FILE",
) -> ArtifactRecord:
    validate_artifact_registration(registration)
    if type(content) is not bytes and content is not None:
        raise Phase3ContractError("artifact content must be exact bytes or None")
    if type(availability) is not ArtifactAvailability:
        raise Phase3ContractError("artifact availability must be typed")
    if availability is ArtifactAvailability.RECORDED and content is None:
        raise Phase3ContractError("RECORDED artifact requires exact content bytes")
    if availability is not ArtifactAvailability.RECORDED and content is not None:
        raise Phase3ContractError("unavailable artifact cannot carry content bytes")
    prototype = ArtifactRecord(
        ARTIFACT_RECORD_SCHEMA,
        "pending",
        _identifier(artifact_type, "artifact_type"),
        registration.normalized_path,
        hashlib.sha256(content).hexdigest() if content is not None else None,
        len(content) if content is not None else None,
        availability,
        registration,
        "0" * 64,
    )
    digest = canonical_sha256(_artifact_record_identity(prototype))
    return validate_artifact_record(
        replace(
            prototype,
            artifact_record_id=f"artifact-{digest}",
            record_sha256=digest,
        )
    )


def validate_artifact_record(record: ArtifactRecord) -> ArtifactRecord:
    if type(record) is not ArtifactRecord:
        raise Phase3ContractError("artifact record must be ArtifactRecord")
    if record.schema_version != ARTIFACT_RECORD_SCHEMA:
        raise Phase3ContractError("unsupported artifact record schema")
    _identifier(record.artifact_record_id, "artifact_record_id")
    _identifier(record.artifact_type, "artifact_type")
    validate_artifact_registration(record.registration)
    if record.normalized_path != record.registration.normalized_path:
        raise Phase3ContractError("artifact record path and registration differ")
    if type(record.availability) is not ArtifactAvailability:
        raise Phase3ContractError("artifact availability must be typed")
    if record.availability is ArtifactAvailability.RECORDED:
        _require_sha256(record.content_sha256, "content_sha256")
        _nonnegative(record.byte_length, "byte_length")
    elif record.content_sha256 is not None or record.byte_length is not None:
        raise Phase3ContractError("unavailable artifact cannot carry content identity")
    _require_sha256(record.record_sha256, "record_sha256")
    expected = canonical_sha256(_artifact_record_identity(record))
    if record.record_sha256 != expected or record.artifact_record_id != f"artifact-{expected}":
        raise Phase3ContractError("artifact record identity mismatch")
    return record


def _safe_project_root(project_root: str | Path) -> Path:
    root = Path(project_root)
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise Phase3ContractError("project root is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise Phase3ContractError("project root must be a non-symlink directory")
    resolved = root.resolve(strict=True)
    if resolved != root.absolute():
        raise Phase3ContractError("project root must not traverse a symlink")
    return resolved


def _read_artifact_bytes(root: Path, normalized_path: str) -> bytes:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise OSError(errno.ENOTSUP, "safe descriptor-relative reads are unavailable")
    flags_dir = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    flags_file = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        current = os.open(root, flags_dir | nofollow)
        descriptors.append(current)
        parts = PurePosixPath(normalized_path).parts
        for part in parts[:-1]:
            try:
                next_descriptor = os.open(
                    part, flags_dir | nofollow, dir_fd=current
                )
            except OSError as exc:
                if exc.errno == errno.ENOTDIR:
                    try:
                        component = os.stat(
                            part,
                            dir_fd=current,
                            follow_symlinks=False,
                        )
                    except OSError:
                        raise exc
                    if stat.S_ISLNK(component.st_mode):
                        raise OSError(errno.ELOOP, "artifact path contains a symlink")
                    if not stat.S_ISDIR(component.st_mode):
                        raise OSError(
                            errno.EINVAL,
                            "artifact path component is not a directory",
                        )
                raise
            current = next_descriptor
            descriptors.append(current)
        file_descriptor = os.open(parts[-1], flags_file | nofollow, dir_fd=current)
        descriptors.append(file_descriptor)
        before = os.fstat(file_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError(errno.EINVAL, "artifact is not regular")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(file_descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise OSError(errno.ESTALE, "artifact changed during read")
        return b"".join(chunks)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _filesystem_blocker(
    path: str,
    exc: BaseException,
    *,
    registration: ArtifactRegistration,
) -> ArtifactBlocker:
    if isinstance(exc, FileNotFoundError):
        code = ArtifactBlockerCode.MISSING
    elif isinstance(exc, OSError) and exc.errno == errno.ELOOP:
        code = ArtifactBlockerCode.SYMLINK
    elif isinstance(exc, OSError) and exc.errno == errno.EINVAL:
        code = ArtifactBlockerCode.NOT_REGULAR
    elif isinstance(exc, OSError) and exc.errno == errno.ESTALE:
        code = ArtifactBlockerCode.CHANGED_DURING_READ
    else:
        code = ArtifactBlockerCode.UNREADABLE
    return ArtifactBlocker(
        code,
        path,
        code.value.lower(),
        registration=registration,
    )


def _artifact_manifest_identity(manifest: ArtifactManifest) -> dict[str, object]:
    return {
        "schema_version": manifest.schema_version,
        "owner_compilation_sha256": manifest.owner_compilation_sha256,
        "tracked_paths": manifest.tracked_paths,
        "records": tuple(record.as_dict() for record in manifest.records),
        "blockers": tuple(blocker.as_dict() for blocker in manifest.blockers),
    }


def validate_artifact_blocker(blocker: ArtifactBlocker) -> ArtifactBlocker:
    if type(blocker) is not ArtifactBlocker:
        raise Phase3ContractError("artifact blocker must be ArtifactBlocker")
    if type(blocker.code) is not ArtifactBlockerCode:
        raise Phase3ContractError("artifact blocker code must be typed")
    path = _text(blocker.normalized_path, "blocker.normalized_path")
    _identifier(blocker.detail, "blocker.detail")
    registration = blocker.registration
    operator_authorization = blocker.operator_authorization
    if blocker.code is ArtifactBlockerCode.ROOT_UNSAFE:
        if path != "__project_root__":
            raise Phase3ContractError("root blocker path is malformed")
        if registration is not None or operator_authorization is not None:
            raise Phase3ContractError(
                "root blocker cannot carry owner binding evidence"
            )
        return blocker
    if _normalize_registration_path(path) != path:
        raise Phase3ContractError("artifact blocker path is not normalized")
    if blocker.code is ArtifactBlockerCode.INVALID_PATH:
        if registration is not None or operator_authorization is not None:
            raise Phase3ContractError(
                "invalid-path blocker cannot carry owner binding evidence"
            )
        return blocker
    if blocker.code is ArtifactBlockerCode.OWNER_RESOLUTION_BLOCKED:
        if registration is not None:
            raise Phase3ContractError(
                "owner-resolution blocker cannot carry a frozen registration"
            )
        if operator_authorization is not None:
            validate_artifact_owner_operator_authorization(
                operator_authorization
            )
            if operator_authorization.normalized_path != path:
                raise Phase3ContractError(
                    "owner-resolution blocker authorization path differs"
                )
        return blocker
    if registration is None:
        raise Phase3ContractError(
            "artifact blocker requires a frozen owner binding"
        )
    validate_artifact_registration(registration)
    if registration.normalized_path != path:
        raise Phase3ContractError("artifact blocker registration path differs")
    if operator_authorization is not None:
        raise Phase3ContractError(
            "filesystem blocker cannot carry operator authorization"
        )
    return blocker


def build_artifact_manifest(
    *,
    owner_compilation_sha256: str,
    records: Iterable[ArtifactRecord],
    blockers: Iterable[ArtifactBlocker] = (),
    tracked_paths: Iterable[str] | None = None,
) -> ArtifactManifest:
    _require_sha256(owner_compilation_sha256, "owner_compilation_sha256")
    unsorted_records = tuple(records)
    unsorted_blockers = tuple(blockers)
    if any(type(record) is not ArtifactRecord for record in unsorted_records):
        raise Phase3ContractError("manifest records must be ArtifactRecord values")
    if any(type(blocker) is not ArtifactBlocker for blocker in unsorted_blockers):
        raise Phase3ContractError("manifest blockers must be typed")
    record_values = tuple(
        sorted(unsorted_records, key=lambda item: item.normalized_path)
    )
    blocker_values = tuple(
        sorted(
            unsorted_blockers,
            key=lambda item: (item.normalized_path, item.code.value),
        )
    )
    if tracked_paths is None:
        inventory_inputs = tuple(
            record.normalized_path for record in record_values
        ) + tuple(
            blocker.normalized_path
            for blocker in blocker_values
            if blocker.code is not ArtifactBlockerCode.ROOT_UNSAFE
        )
    else:
        inventory_inputs = tuple(tracked_paths)
    inventory = tuple(
        sorted(_normalize_registration_path(path) for path in inventory_inputs)
    )
    if len(inventory) != len(set(inventory)):
        raise Phase3ContractError("manifest tracked paths must be unique")
    for record in record_values:
        validate_artifact_record(record)
        if record.registration.owner_compilation_sha256 != owner_compilation_sha256:
            raise Phase3ContractError("manifest mixes owner compilation identities")
    if len({record.normalized_path for record in record_values}) != len(record_values):
        raise Phase3ContractError("manifest artifact paths must be unique")
    for blocker in blocker_values:
        validate_artifact_blocker(blocker)
        if blocker.registration is not None and (
            blocker.registration.owner_compilation_sha256 != owner_compilation_sha256
        ):
            raise Phase3ContractError("manifest blocker owner compilation differs")
        if blocker.operator_authorization is not None and (
            blocker.operator_authorization.owner_compilation_sha256
            != owner_compilation_sha256
        ):
            raise Phase3ContractError("manifest blocker owner compilation differs")
    prototype = ArtifactManifest(
        ARTIFACT_MANIFEST_SCHEMA,
        owner_compilation_sha256,
        inventory,
        record_values,
        blocker_values,
        "0" * 64,
    )
    return validate_artifact_manifest(
        replace(prototype, manifest_sha256=canonical_sha256(_artifact_manifest_identity(prototype)))
    )


def capture_artifact_manifest(
    project_root: str | Path,
    compilation: OwnerCompilation,
    paths: Iterable[str],
) -> ArtifactManifest:
    compilation_sha256 = owner_compilation_semantic_sha256(compilation)
    raw_paths = tuple(paths)
    normalized_inputs: list[str] = []
    readable_inputs: list[str] = []
    blockers: list[ArtifactBlocker] = []
    for raw_path in raw_paths:
        try:
            normalized = _normalize_registration_path(raw_path)
            normalized_inputs.append(normalized)
            readable_inputs.append(normalized)
        except ArtifactRegistrationError:
            sentinel = _invalid_path_sentinel(raw_path)
            normalized_inputs.append(sentinel)
            blockers.append(
                ArtifactBlocker(
                    ArtifactBlockerCode.INVALID_PATH,
                    sentinel,
                    "invalid_project_relative_path",
                )
            )
    if len(set(normalized_inputs)) != len(normalized_inputs):
        raise Phase3ContractError("manifest input paths must be unique after normalization")
    try:
        root = _safe_project_root(project_root)
    except Phase3ContractError:
        return build_artifact_manifest(
            owner_compilation_sha256=compilation_sha256,
            records=(),
            blockers=(
                ArtifactBlocker(
                    ArtifactBlockerCode.ROOT_UNSAFE, "__project_root__", "root_unsafe"
                ),
            ),
            tracked_paths=normalized_inputs,
        )
    records: list[ArtifactRecord] = []
    for normalized in sorted(readable_inputs):
        try:
            registration = register_artifact_owner(compilation, normalized)
        except ArtifactRegistrationError:
            blockers.append(
                ArtifactBlocker(
                    ArtifactBlockerCode.OWNER_RESOLUTION_BLOCKED,
                    normalized,
                    "owner_resolution_blocked",
                )
            )
            continue
        try:
            content = _read_artifact_bytes(root, normalized)
        except OSError as exc:
            blockers.append(
                _filesystem_blocker(normalized, exc, registration=registration)
            )
            continue
        records.append(build_artifact_record(registration, content=content))
    return build_artifact_manifest(
        owner_compilation_sha256=compilation_sha256,
        records=records,
        blockers=blockers,
        tracked_paths=normalized_inputs,
    )


def validate_artifact_manifest(manifest: ArtifactManifest) -> ArtifactManifest:
    if type(manifest) is not ArtifactManifest:
        raise Phase3ContractError("manifest must be ArtifactManifest")
    if manifest.schema_version != ARTIFACT_MANIFEST_SCHEMA:
        raise Phase3ContractError("unsupported artifact manifest schema")
    _require_sha256(manifest.owner_compilation_sha256, "owner_compilation_sha256")
    _require_sha256(manifest.manifest_sha256, "manifest_sha256")
    if (
        type(manifest.tracked_paths) is not tuple
        or type(manifest.records) is not tuple
        or type(manifest.blockers) is not tuple
    ):
        raise Phase3ContractError("manifest collections must be tuples")
    tracked_paths = tuple(
        _normalize_registration_path(path) for path in manifest.tracked_paths
    )
    if (
        tracked_paths != manifest.tracked_paths
        or tracked_paths != tuple(sorted(tracked_paths))
        or len(tracked_paths) != len(set(tracked_paths))
    ):
        raise Phase3ContractError(
            "manifest tracked paths must be normalized, sorted, and unique"
        )
    for record in manifest.records:
        validate_artifact_record(record)
        if record.registration.owner_compilation_sha256 != manifest.owner_compilation_sha256:
            raise Phase3ContractError("manifest owner compilation differs")
    if tuple(sorted(manifest.records, key=lambda item: item.normalized_path)) != manifest.records:
        raise Phase3ContractError("manifest records must be path sorted")
    if len({record.normalized_path for record in manifest.records}) != len(manifest.records):
        raise Phase3ContractError("manifest artifact paths must be unique")
    for blocker in manifest.blockers:
        validate_artifact_blocker(blocker)
        if blocker.registration is not None and (
            blocker.registration.owner_compilation_sha256
            != manifest.owner_compilation_sha256
        ):
            raise Phase3ContractError("manifest blocker owner compilation differs")
        if blocker.operator_authorization is not None and (
            blocker.operator_authorization.owner_compilation_sha256
            != manifest.owner_compilation_sha256
        ):
            raise Phase3ContractError("manifest blocker owner compilation differs")
    if tuple(
        sorted(
            manifest.blockers,
            key=lambda item: (item.normalized_path, item.code.value),
        )
    ) != manifest.blockers:
        raise Phase3ContractError("manifest blockers must be deterministically sorted")
    blocker_paths = {blocker.normalized_path for blocker in manifest.blockers}
    if len(blocker_paths) != len(manifest.blockers):
        raise Phase3ContractError("manifest blocker paths must be unique")
    record_paths = {record.normalized_path for record in manifest.records}
    if record_paths & blocker_paths:
        raise Phase3ContractError("manifest path cannot be both recorded and blocked")
    if not any(
        blocker.code is ArtifactBlockerCode.ROOT_UNSAFE
        for blocker in manifest.blockers
    ):
        covered_paths = record_paths | blocker_paths
        if covered_paths != set(manifest.tracked_paths):
            raise Phase3ContractError(
                "manifest tracked inventory must have one record or blocker per path"
            )
    if manifest.manifest_sha256 != canonical_sha256(_artifact_manifest_identity(manifest)):
        raise Phase3ContractError("artifact manifest identity mismatch")
    return manifest


def _change_set_identity(value: ChangeSet) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "previous_manifest_sha256": value.previous_manifest_sha256,
        "current_manifest_sha256": value.current_manifest_sha256,
        "removals": tuple(item.as_dict() for item in value.removals),
        "changes": tuple(item.as_dict() for item in value.changes),
        "dirty_decisions": tuple(item.as_dict() for item in value.dirty_decisions),
    }


def _artifact_removal_identity(value: ArtifactRemoval) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "normalized_path": value.normalized_path,
        "previous_record_sha256": value.previous_record_sha256,
        "previous_blocker_code": value.previous_blocker_code,
        "owner_compilation_sha256": value.owner_compilation_sha256,
        "owner_id": value.owner_id,
        "owner_stage": value.owner_stage,
        "dirty_flag": value.dirty_flag,
        "reason_code": value.reason_code,
    }


def build_artifact_removal(
    previous_manifest: ArtifactManifest,
    normalized_path: str,
    *,
    reason_code: str,
) -> ArtifactRemoval:
    """Create an explicit semantic untracking decision for one previous path."""

    validate_artifact_manifest(previous_manifest)
    path = _normalize_registration_path(normalized_path)
    previous_records = {
        record.normalized_path: record for record in previous_manifest.records
    }
    previous_blockers = {
        blocker.normalized_path: blocker for blocker in previous_manifest.blockers
    }
    record = previous_records.get(path)
    blocker = previous_blockers.get(path)
    if (record is None) == (blocker is None):
        raise Phase3ContractError(
            "artifact removal must close exactly one previous record or blocker"
        )
    if record is not None:
        registration = record.registration
        resolved_owner_id = registration.owner_id
        resolved_owner_stage = registration.owner_stage
        resolved_dirty_flag = registration.dirty_flag
    else:
        if blocker.code is ArtifactBlockerCode.OWNER_RESOLUTION_BLOCKED:
            authorization = blocker.operator_authorization
            if authorization is None:
                raise Phase3ContractError(
                    "blocked artifact removal requires typed operator authorization"
                )
            resolved_owner_id = authorization.owner_id
            resolved_owner_stage = authorization.owner_stage
            resolved_dirty_flag = authorization.dirty_flag
        elif blocker.registration is not None:
            resolved_owner_id = blocker.registration.owner_id
            resolved_owner_stage = blocker.registration.owner_stage
            resolved_dirty_flag = blocker.registration.dirty_flag
        else:
            raise Phase3ContractError(
                "blocked artifact removal requires a frozen owner binding"
            )
    prototype = ArtifactRemoval(
        ARTIFACT_REMOVAL_SCHEMA,
        path,
        record.record_sha256 if record is not None else None,
        blocker.code.value if blocker is not None else None,
        previous_manifest.owner_compilation_sha256,
        resolved_owner_id,
        resolved_owner_stage,
        resolved_dirty_flag,
        _identifier(reason_code, "removal reason_code"),
        "0" * 64,
    )
    return validate_artifact_removal(
        replace(
            prototype,
            removal_sha256=canonical_sha256(_artifact_removal_identity(prototype)),
        )
    )


def validate_artifact_removal(removal: ArtifactRemoval) -> ArtifactRemoval:
    if (
        type(removal) is not ArtifactRemoval
        or removal.schema_version != ARTIFACT_REMOVAL_SCHEMA
    ):
        raise Phase3ContractError("unsupported artifact removal")
    if _normalize_registration_path(removal.normalized_path) != removal.normalized_path:
        raise Phase3ContractError("artifact removal path is not normalized")
    previous_record = _require_sha256(
        removal.previous_record_sha256,
        "removal.previous_record_sha256",
        optional=True,
    )
    previous_blocker = removal.previous_blocker_code
    if (previous_record is None) == (previous_blocker is None):
        raise Phase3ContractError(
            "artifact removal must bind exactly one previous semantic identity"
        )
    if previous_blocker is not None:
        try:
            ArtifactBlockerCode(previous_blocker)
        except (TypeError, ValueError) as exc:
            raise Phase3ContractError(
                "artifact removal previous blocker code is invalid"
            ) from exc
    _require_sha256(
        removal.owner_compilation_sha256,
        "removal.owner_compilation_sha256",
    )
    _identifier(removal.owner_id, "removal.owner_id")
    _positive(removal.owner_stage, "removal.owner_stage")
    if removal.owner_id != f"owner:stage:{removal.owner_stage}":
        raise Phase3ContractError("artifact removal owner identity differs")
    _identifier(removal.dirty_flag, "removal.dirty_flag")
    _identifier(removal.reason_code, "removal.reason_code")
    _require_sha256(removal.removal_sha256, "removal.removal_sha256")
    if removal.removal_sha256 != canonical_sha256(
        _artifact_removal_identity(removal)
    ):
        raise Phase3ContractError("artifact removal identity mismatch")
    return removal


def _validate_artifact_change(change: ArtifactChange) -> ArtifactChange:
    if type(change) is not ArtifactChange:
        raise Phase3ContractError("artifact change must be ArtifactChange")
    if type(change.kind) is not ArtifactChangeKind:
        raise Phase3ContractError("artifact change kind must be typed")
    if change.kind is ArtifactChangeKind.BLOCKED:
        _text(change.normalized_path, "artifact change blocked path")
    elif _normalize_registration_path(change.normalized_path) != change.normalized_path:
        raise Phase3ContractError("artifact change path is not normalized")
    previous = _require_sha256(
        change.previous_record_sha256,
        "previous_record_sha256",
        optional=True,
    )
    current = _require_sha256(
        change.current_record_sha256,
        "current_record_sha256",
        optional=True,
    )
    previous_blocker = change.previous_blocker_code
    if previous_blocker is not None:
        try:
            ArtifactBlockerCode(previous_blocker)
        except (TypeError, ValueError) as exc:
            raise Phase3ContractError(
                "artifact change previous blocker code is invalid"
            ) from exc
    removal_sha = _require_sha256(
        change.removal_sha256, "removal_sha256", optional=True
    )
    shape_is_valid = {
        ArtifactChangeKind.ADDED: (
            previous is None and previous_blocker is None and current is not None
        ),
        ArtifactChangeKind.RESOLVED: (
            previous is None and previous_blocker is not None and current is not None
        ),
        ArtifactChangeKind.MODIFIED: (
            previous is not None and current is not None and previous != current
        ),
        ArtifactChangeKind.REMOVED: (
            (previous is not None) != (previous_blocker is not None)
            and current is None
            and removal_sha is not None
        ),
        ArtifactChangeKind.UNCHANGED: (
            previous is not None and previous == current
        ),
        ArtifactChangeKind.BLOCKED: current is None and removal_sha is None,
    }[change.kind]
    if change.kind is not ArtifactChangeKind.REMOVED and removal_sha is not None:
        shape_is_valid = False
    if not shape_is_valid:
        raise Phase3ContractError("artifact change kind and record identities differ")
    return change


def _validate_dirty_decision(decision: DirtyDecision) -> DirtyDecision:
    if type(decision) is not DirtyDecision:
        raise Phase3ContractError("dirty decision must be DirtyDecision")
    if type(decision.disposition) is not DirtyDisposition:
        raise Phase3ContractError("dirty decision disposition must be typed")
    if decision.disposition is DirtyDisposition.BLOCKED:
        _text(decision.normalized_path, "blocked dirty decision path")
    elif _normalize_registration_path(decision.normalized_path) != decision.normalized_path:
        raise Phase3ContractError("dirty decision path is not normalized")
    _identifier(decision.reason_code, "dirty decision reason_code")
    owner_fields = (
        decision.dirty_flag,
        decision.owner_id,
        decision.owner_stage,
    )
    if decision.disposition is DirtyDisposition.DIRTY:
        _identifier(decision.dirty_flag, "dirty_flag")
        _identifier(decision.owner_id, "owner_id")
        _positive(decision.owner_stage, "owner_stage")
        if decision.owner_id != f"owner:stage:{decision.owner_stage}":
            raise Phase3ContractError("dirty decision owner identity differs")
    elif owner_fields != (None, None, None):
        raise Phase3ContractError("non-dirty decision cannot carry an owner action")
    return decision


def validate_change_set(change_set: ChangeSet) -> ChangeSet:
    if type(change_set) is not ChangeSet or change_set.schema_version != CHANGE_SET_SCHEMA:
        raise Phase3ContractError("unsupported artifact change set")
    _require_sha256(
        change_set.previous_manifest_sha256, "previous_manifest_sha256"
    )
    _require_sha256(change_set.current_manifest_sha256, "current_manifest_sha256")
    _require_sha256(change_set.change_set_sha256, "change_set_sha256")
    if (
        type(change_set.removals) is not tuple
        or type(change_set.changes) is not tuple
        or type(change_set.dirty_decisions) is not tuple
    ):
        raise Phase3ContractError("change set collections must be tuples")
    for removal in change_set.removals:
        validate_artifact_removal(removal)
    for change in change_set.changes:
        _validate_artifact_change(change)
    for decision in change_set.dirty_decisions:
        _validate_dirty_decision(decision)
    change_paths = tuple(item.normalized_path for item in change_set.changes)
    decision_paths = tuple(item.normalized_path for item in change_set.dirty_decisions)
    if change_paths != tuple(sorted(change_paths)) or len(change_paths) != len(
        set(change_paths)
    ):
        raise Phase3ContractError("artifact changes must be path sorted and unique")
    if decision_paths != change_paths:
        raise Phase3ContractError("dirty decisions must align with artifact changes")
    removal_paths = tuple(item.normalized_path for item in change_set.removals)
    if (
        removal_paths != tuple(sorted(removal_paths))
        or len(removal_paths) != len(set(removal_paths))
    ):
        raise Phase3ContractError("artifact removals must be path sorted and unique")
    removal_by_path = {
        item.normalized_path: item for item in change_set.removals
    }
    for change in change_set.changes:
        removal = removal_by_path.get(change.normalized_path)
        if change.kind is ArtifactChangeKind.REMOVED:
            if removal is None or removal.removal_sha256 != change.removal_sha256:
                raise Phase3ContractError(
                    "removed artifact change lacks its typed removal"
                )
        elif removal is not None:
            raise Phase3ContractError(
                "artifact removal does not align with a removed change"
            )
    expected_dispositions = {
        ArtifactChangeKind.UNCHANGED: DirtyDisposition.CLEAN,
        ArtifactChangeKind.BLOCKED: DirtyDisposition.BLOCKED,
    }
    for change, decision in zip(change_set.changes, change_set.dirty_decisions):
        expected = expected_dispositions.get(change.kind, DirtyDisposition.DIRTY)
        if decision.disposition is not expected:
            raise Phase3ContractError("dirty decision and artifact change differ")
    if change_set.change_set_sha256 != canonical_sha256(
        _change_set_identity(change_set)
    ):
        raise Phase3ContractError("artifact change set identity mismatch")
    return change_set


def classify_owner_policy(
    previous: ArtifactManifest, current: ArtifactManifest
) -> OwnerPolicyDisposition:
    """Classify frozen-owner compatibility without mutating either manifest."""

    validate_artifact_manifest(previous)
    validate_artifact_manifest(current)
    if previous.owner_compilation_sha256 != current.owner_compilation_sha256:
        return OwnerPolicyDisposition.MIGRATION_REQUIRED
    previous_by_path = {record.normalized_path: record for record in previous.records}
    current_by_path = {record.normalized_path: record for record in current.records}
    if any(
        previous_by_path[path].registration != current_by_path[path].registration
        for path in set(previous_by_path) & set(current_by_path)
    ):
        return OwnerPolicyDisposition.MIGRATION_REQUIRED
    return OwnerPolicyDisposition.UNCHANGED


def compute_change_set(
    previous: ArtifactManifest,
    current: ArtifactManifest,
    *,
    removals: Iterable[ArtifactRemoval] = (),
) -> ChangeSet:
    if (
        classify_owner_policy(previous, current)
        is OwnerPolicyDisposition.MIGRATION_REQUIRED
    ):
        raise OwnerPolicyMigrationRequired(
            "owner compilation changed; explicit artifact owner migration is required"
        )
    previous_by_path = {record.normalized_path: record for record in previous.records}
    current_by_path = {record.normalized_path: record for record in current.records}
    previous_blocker_by_path = {
        blocker.normalized_path: blocker for blocker in previous.blockers
    }
    blocker_by_path = {blocker.normalized_path: blocker for blocker in current.blockers}
    removal_values = tuple(sorted(tuple(removals), key=lambda item: item.normalized_path))
    for removal in removal_values:
        validate_artifact_removal(removal)
        if removal.owner_compilation_sha256 != previous.owner_compilation_sha256:
            raise OwnerPolicyMigrationRequired(
                "artifact removal owner policy differs; explicit migration is required"
            )
    if len({item.normalized_path for item in removal_values}) != len(removal_values):
        raise Phase3ContractError("artifact removals must be path unique")
    removal_by_path = {item.normalized_path: item for item in removal_values}
    changes: list[ArtifactChange] = []
    decisions: list[DirtyDecision] = []
    all_paths = (
        set(previous.tracked_paths)
        | set(current.tracked_paths)
        | set(previous_by_path)
        | set(previous_blocker_by_path)
        | set(current_by_path)
        | set(blocker_by_path)
        | set(removal_by_path)
    )
    for path in sorted(all_paths):
        old = previous_by_path.get(path)
        old_blocker = previous_blocker_by_path.get(path)
        new = current_by_path.get(path)
        blocker = blocker_by_path.get(path)
        removal = removal_by_path.get(path)
        if removal is not None:
            if new is not None or blocker is not None or path in current.tracked_paths:
                raise Phase3ContractError(
                    "removed path cannot remain in the current tracked inventory"
                )
            if old is not None:
                if removal.previous_record_sha256 != old.record_sha256:
                    raise Phase3ContractError(
                        "artifact removal previous record identity differs"
                    )
            elif old_blocker is not None:
                if removal.previous_blocker_code != old_blocker.code.value:
                    raise Phase3ContractError(
                        "artifact removal previous blocker identity differs"
                    )
            else:
                raise Phase3ContractError(
                    "artifact removal does not close a previous tracked outcome"
                )
            changes.append(
                ArtifactChange(
                    path,
                    ArtifactChangeKind.REMOVED,
                    old.record_sha256 if old else None,
                    None,
                    old_blocker.code.value if old_blocker else None,
                    removal.removal_sha256,
                )
            )
            decisions.append(
                DirtyDecision(
                    path,
                    DirtyDisposition.DIRTY,
                    removal.dirty_flag,
                    removal.owner_id,
                    removal.owner_stage,
                    removal.reason_code,
                )
            )
            continue
        if blocker is not None:
            changes.append(
                ArtifactChange(
                    path,
                    ArtifactChangeKind.BLOCKED,
                    old.record_sha256 if old else None,
                    None,
                    old_blocker.code.value if old_blocker else None,
                )
            )
            decisions.append(
                DirtyDecision(path, DirtyDisposition.BLOCKED, None, None, None, blocker.code.value)
            )
            continue
        if old_blocker is not None and new is not None:
            kind = ArtifactChangeKind.RESOLVED
            owner = new.registration
            reason_code = "ARTIFACT_BLOCKER_RESOLVED"
        elif (old is not None or old_blocker is not None) and path not in current.tracked_paths:
            changes.append(
                ArtifactChange(
                    path,
                    ArtifactChangeKind.BLOCKED,
                    old.record_sha256 if old else None,
                    None,
                    old_blocker.code.value if old_blocker else None,
                )
            )
            decisions.append(
                DirtyDecision(
                    path,
                    DirtyDisposition.BLOCKED,
                    None,
                    None,
                    None,
                    (
                        "PREVIOUS_BLOCKER_OMITTED"
                        if old_blocker is not None
                        else "TRACKED_PATH_OMITTED"
                    ),
                )
            )
            continue
        elif old is not None and new is not None:
            if old.registration != new.registration:
                raise OwnerPolicyMigrationRequired(
                    f"frozen owner changed for {path}; explicit migration is required"
                )
            kind = (
                ArtifactChangeKind.UNCHANGED
                if old.record_sha256 == new.record_sha256
                else ArtifactChangeKind.MODIFIED
            )
            owner = new.registration
            reason_code = f"ARTIFACT_{kind.value}"
        elif new is not None:
            kind = ArtifactChangeKind.ADDED
            owner = new.registration
            reason_code = f"ARTIFACT_{kind.value}"
        else:
            # A tracked path without an outcome is a current scan blocker, not
            # proof of deletion.  capture_artifact_manifest normally prevents
            # this branch; retain the explicit fail-closed evidence for typed
            # manually constructed values.
            changes.append(
                ArtifactChange(
                    path,
                    ArtifactChangeKind.BLOCKED,
                    old.record_sha256 if old else None,
                    None,
                    old_blocker.code.value if old_blocker else None,
                )
            )
            decisions.append(
                DirtyDecision(
                    path,
                    DirtyDisposition.BLOCKED,
                    None,
                    None,
                    None,
                    "TRACKED_PATH_OUTCOME_MISSING",
                )
            )
            continue
        changes.append(
            ArtifactChange(
                path,
                kind,
                old.record_sha256 if old else None,
                new.record_sha256 if new else None,
                old_blocker.code.value if old_blocker else None,
            )
        )
        dirty = kind is not ArtifactChangeKind.UNCHANGED
        decisions.append(
            DirtyDecision(
                path,
                DirtyDisposition.DIRTY if dirty else DirtyDisposition.CLEAN,
                owner.dirty_flag if dirty else None,
                owner.owner_id if dirty else None,
                owner.owner_stage if dirty else None,
                reason_code,
            )
        )
    prototype = ChangeSet(
        CHANGE_SET_SCHEMA,
        previous.manifest_sha256,
        current.manifest_sha256,
        removal_values,
        tuple(changes),
        tuple(decisions),
        "0" * 64,
    )
    return validate_change_set(
        replace(
            prototype,
            change_set_sha256=canonical_sha256(_change_set_identity(prototype)),
        )
    )


def _reopen_plan_identity(value: ReopenPlan) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "workflow_id": value.workflow_id,
        "source_revision": value.source_revision,
        "target_scope": value.target_scope,
        "target_owner_stage": value.target_owner_stage,
        "reason_code": value.reason_code,
        "change_set_sha256": value.change_set_sha256,
        "read_set": tuple(item.as_dict() for item in value.read_set),
        "read_set_sha256": value.read_set_sha256,
    }


def _blocked_no_reopen_disposition_identity(
    value: BlockedNoReopenDisposition,
) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "workflow_id": value.workflow_id,
        "source_revision": value.source_revision,
        "target_scope": value.target_scope,
        "target_owner_stage": value.target_owner_stage,
        "reason_code": value.reason_code,
        "change_set_sha256": value.change_set_sha256,
        "read_set": tuple(item.as_dict() for item in value.read_set),
        "read_set_sha256": value.read_set_sha256,
    }


def _phase3_previous_head_identity(value: Phase3PreviousHead) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "kind": value.kind.value,
        "workflow_id": value.workflow_id,
        "source_revision": value.source_revision,
        "previous_manifest_sha256": value.previous_manifest_sha256,
        "previous_revision": value.previous_revision,
        "previous_command_id": value.previous_command_id,
        "previous_mutation_sha256": value.previous_mutation_sha256,
    }


def _read_set_identity(read_set: tuple[ArtifactReadExpectation, ...]) -> dict[str, object]:
    return {
        "schema": "authority-artifact-read-set-v1",
        "entries": tuple(item.as_dict() for item in read_set),
    }


def _build_read_set(
    *,
    previous_manifest: ArtifactManifest,
    relevant_paths: Iterable[str],
    previous_occurrence_ids: dict[str, str] | None = None,
) -> tuple[ArtifactReadExpectation, ...]:
    previous_by_path = {
        record.normalized_path: record for record in previous_manifest.records
    }
    previous_blockers = {
        blocker.normalized_path: blocker for blocker in previous_manifest.blockers
    }
    occurrence_ids = previous_occurrence_ids or {}
    if type(occurrence_ids) is not dict:
        raise Phase3ContractError("previous occurrence identities must be a mapping")
    for path, occurrence_id in occurrence_ids.items():
        if _normalize_registration_path(path) != path:
            raise Phase3ContractError("previous occurrence path is not normalized")
        _identifier(occurrence_id, "previous occurrence identity")
    return tuple(
        ArtifactReadExpectation(
            path,
            (
                previous_by_path[path].artifact_record_id
                if path in previous_by_path
                else None
            ),
            (
                previous_by_path[path].record_sha256
                if path in previous_by_path
                else None
            ),
            (
                previous_blockers[path].code.value
                if path in previous_blockers
                else None
            ),
            occurrence_ids.get(path),
        )
        for path in sorted({_normalize_registration_path(path) for path in relevant_paths})
    )


def _validate_phase3_read_set(
    read_set: tuple[ArtifactReadExpectation, ...],
    *,
    field_prefix: str,
) -> None:
    if type(read_set) is not tuple or not read_set:
        raise Phase3ContractError(f"{field_prefix} read_set must be a non-empty tuple")
    paths: list[str] = []
    for entry in read_set:
        if type(entry) is not ArtifactReadExpectation:
            raise Phase3ContractError(f"{field_prefix} read_set entries must be typed")
        normalized = _normalize_registration_path(entry.normalized_path)
        if normalized != entry.normalized_path:
            raise Phase3ContractError(f"{field_prefix} read_set path is not normalized")
        paths.append(normalized)
        record_present = entry.expected_artifact_record_id is not None
        blocker_present = entry.expected_blocker_code is not None
        if record_present != (entry.expected_record_sha256 is not None):
            raise Phase3ContractError(
                f"{field_prefix} read_set absence identity is incomplete"
            )
        if record_present and blocker_present:
            raise Phase3ContractError(
                f"{field_prefix} read_set cannot expect a record and blocker together"
            )
        if record_present:
            _identifier(
                entry.expected_artifact_record_id, "expected_artifact_record_id"
            )
            _require_sha256(entry.expected_record_sha256, "expected_record_sha256")
        if blocker_present:
            try:
                ArtifactBlockerCode(entry.expected_blocker_code)
            except (TypeError, ValueError) as exc:
                raise Phase3ContractError(
                    f"{field_prefix} read_set blocker identity is invalid"
                ) from exc
        if entry.expected_occurrence_id is not None:
            _identifier(entry.expected_occurrence_id, "expected_occurrence_id")
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise Phase3ContractError(
            f"{field_prefix} read_set paths must be sorted and unique"
        )


def _blocker_owner_fields(
    blocker: ArtifactBlocker,
    *,
    field_prefix: str,
) -> tuple[str, str, int, str]:
    validate_artifact_blocker(blocker)
    if blocker.registration is not None:
        registration = blocker.registration
        return (
            registration.owner_compilation_sha256,
            registration.owner_id,
            registration.owner_stage,
            registration.dirty_flag,
        )
    if blocker.operator_authorization is not None:
        authorization = blocker.operator_authorization
        return (
            authorization.owner_compilation_sha256,
            authorization.owner_id,
            authorization.owner_stage,
            authorization.dirty_flag,
        )
    raise Phase3ContractError(f"{field_prefix} blocker lacks a typed owner binding")


def build_reopen_plan(
    *, workflow_id: str, source_revision: int, change_set: ChangeSet,
    previous_manifest: ArtifactManifest,
    previous_occurrence_ids: dict[str, str] | None = None,
) -> ReopenPlan:
    validate_change_set(change_set)
    validate_artifact_manifest(previous_manifest)
    if change_set.previous_manifest_sha256 != previous_manifest.manifest_sha256:
        raise Phase3ContractError("reopen plan previous manifest differs")
    blocked = [
        item
        for item in change_set.dirty_decisions
        if item.disposition is DirtyDisposition.BLOCKED
    ]
    if blocked:
        raise Phase3ContractError("blocked change set cannot produce a reopen plan")
    dirty = [
        item
        for item in change_set.dirty_decisions
        if item.disposition is DirtyDisposition.DIRTY
    ]
    if not dirty:
        raise Phase3ContractError("clean change set does not require a reopen plan")
    read_set = _build_read_set(
        previous_manifest=previous_manifest,
        relevant_paths=(item.normalized_path for item in dirty),
        previous_occurrence_ids=previous_occurrence_ids,
    )
    read_set_sha256 = canonical_sha256(_read_set_identity(read_set))
    target = min(_positive(item.owner_stage, "owner_stage") for item in dirty)
    prototype = ReopenPlan(
        REOPEN_PLAN_SCHEMA,
        "pending",
        _identifier(workflow_id, "workflow_id"),
        _nonnegative(source_revision, "source_revision"),
        f"stage:{target}",
        target,
        "ARTIFACT_CHANGE_SET",
        str(_require_sha256(change_set.change_set_sha256, "change_set_sha256")),
        read_set,
        read_set_sha256,
        "0" * 64,
    )
    plan_sha = canonical_sha256(_reopen_plan_identity(prototype))
    return validate_reopen_plan(
        replace(prototype, reopen_plan_id=f"reopen-{plan_sha}", plan_sha256=plan_sha)
    )


def validate_reopen_plan(plan: ReopenPlan) -> ReopenPlan:
    if type(plan) is not ReopenPlan or plan.schema_version != REOPEN_PLAN_SCHEMA:
        raise Phase3ContractError("unsupported reopen plan")
    _identifier(plan.reopen_plan_id, "reopen_plan_id")
    _identifier(plan.workflow_id, "workflow_id")
    _nonnegative(plan.source_revision, "source_revision")
    _positive(plan.target_owner_stage, "target_owner_stage")
    if plan.target_scope != f"stage:{plan.target_owner_stage}":
        raise Phase3ContractError("reopen target scope and owner stage differ")
    _identifier(plan.reason_code, "reason_code")
    if plan.reason_code != "ARTIFACT_CHANGE_SET":
        raise Phase3ContractError("reopen reason code is unsupported")
    _require_sha256(plan.change_set_sha256, "change_set_sha256")
    _validate_phase3_read_set(plan.read_set, field_prefix="reopen")
    expected_read_set = canonical_sha256(_read_set_identity(plan.read_set))
    if plan.read_set_sha256 != expected_read_set:
        raise Phase3ContractError("reopen read_set identity mismatch")
    _require_sha256(plan.plan_sha256, "plan_sha256")
    expected_plan = canonical_sha256(_reopen_plan_identity(plan))
    if plan.plan_sha256 != expected_plan or plan.reopen_plan_id != f"reopen-{expected_plan}":
        raise Phase3ContractError("reopen plan identity mismatch")
    return plan


def build_blocked_no_reopen_disposition(
    *,
    workflow_id: str,
    source_revision: int,
    change_set: ChangeSet,
    previous_manifest: ArtifactManifest,
    current_manifest: ArtifactManifest,
    previous_occurrence_ids: dict[str, str] | None = None,
) -> BlockedNoReopenDisposition:
    validate_change_set(change_set)
    validate_artifact_manifest(previous_manifest)
    validate_artifact_manifest(current_manifest)
    if change_set.previous_manifest_sha256 != previous_manifest.manifest_sha256:
        raise Phase3ContractError(
            "blocked disposition previous manifest differs"
        )
    if change_set.current_manifest_sha256 != current_manifest.manifest_sha256:
        raise Phase3ContractError("blocked disposition current manifest differs")
    blocked = [
        item
        for item in change_set.dirty_decisions
        if item.disposition is DirtyDisposition.BLOCKED
    ]
    if not blocked:
        raise Phase3ContractError(
            "clean or dirty change set cannot produce a blocked disposition"
        )
    relevant = [
        item
        for item in change_set.dirty_decisions
        if item.disposition is not DirtyDisposition.CLEAN
    ]
    read_set = _build_read_set(
        previous_manifest=previous_manifest,
        relevant_paths=(item.normalized_path for item in relevant),
        previous_occurrence_ids=previous_occurrence_ids,
    )
    previous_records = {
        item.normalized_path: item for item in previous_manifest.records
    }
    previous_blockers = {
        item.normalized_path: item for item in previous_manifest.blockers
    }
    current_blockers = {
        item.normalized_path: item for item in current_manifest.blockers
    }
    owner_stages: list[int] = []
    for decision in relevant:
        if decision.disposition is DirtyDisposition.DIRTY:
            owner_stages.append(_positive(decision.owner_stage, "owner_stage"))
            continue
        path = decision.normalized_path
        current_blocker = current_blockers.get(path)
        if current_blocker is not None:
            owner_stages.append(
                _blocker_owner_fields(
                    current_blocker,
                    field_prefix="blocked disposition current",
                )[2]
            )
            continue
        previous_record = previous_records.get(path)
        if previous_record is not None:
            owner_stages.append(previous_record.registration.owner_stage)
            continue
        previous_blocker = previous_blockers.get(path)
        if previous_blocker is not None:
            owner_stages.append(
                _blocker_owner_fields(
                    previous_blocker,
                    field_prefix="blocked disposition previous",
                )[2]
            )
            continue
        raise Phase3ContractError(
            "blocked disposition path lacks a typed owner binding"
        )
    target = min(owner_stages)
    read_set_sha256 = canonical_sha256(_read_set_identity(read_set))
    prototype = BlockedNoReopenDisposition(
        BLOCKED_NO_REOPEN_DISPOSITION_SCHEMA,
        "pending",
        _identifier(workflow_id, "workflow_id"),
        _nonnegative(source_revision, "source_revision"),
        f"stage:{target}",
        target,
        "BLOCKED_NO_REOPEN",
        str(_require_sha256(change_set.change_set_sha256, "change_set_sha256")),
        read_set,
        read_set_sha256,
        "0" * 64,
    )
    disposition_sha = canonical_sha256(
        _blocked_no_reopen_disposition_identity(prototype)
    )
    return validate_blocked_no_reopen_disposition(
        replace(
            prototype,
            disposition_id=f"blocked-{disposition_sha}",
            disposition_sha256=disposition_sha,
        )
    )


def validate_blocked_no_reopen_disposition(
    disposition: BlockedNoReopenDisposition,
) -> BlockedNoReopenDisposition:
    if (
        type(disposition) is not BlockedNoReopenDisposition
        or disposition.schema_version != BLOCKED_NO_REOPEN_DISPOSITION_SCHEMA
    ):
        raise Phase3ContractError("unsupported blocked-no-reopen disposition")
    _identifier(disposition.disposition_id, "disposition_id")
    _identifier(disposition.workflow_id, "workflow_id")
    _nonnegative(disposition.source_revision, "source_revision")
    _positive(disposition.target_owner_stage, "target_owner_stage")
    if disposition.target_scope != f"stage:{disposition.target_owner_stage}":
        raise Phase3ContractError(
            "blocked-no-reopen target scope and owner stage differ"
        )
    _identifier(disposition.reason_code, "reason_code")
    if disposition.reason_code != "BLOCKED_NO_REOPEN":
        raise Phase3ContractError(
            "blocked-no-reopen reason code is unsupported"
        )
    _require_sha256(disposition.change_set_sha256, "change_set_sha256")
    _validate_phase3_read_set(disposition.read_set, field_prefix="blocked-no-reopen")
    expected_read_set = canonical_sha256(_read_set_identity(disposition.read_set))
    if disposition.read_set_sha256 != expected_read_set:
        raise Phase3ContractError(
            "blocked-no-reopen read_set identity mismatch"
        )
    _require_sha256(disposition.disposition_sha256, "disposition_sha256")
    expected = canonical_sha256(
        _blocked_no_reopen_disposition_identity(disposition)
    )
    if (
        disposition.disposition_sha256 != expected
        or disposition.disposition_id != f"blocked-{expected}"
    ):
        raise Phase3ContractError(
            "blocked-no-reopen disposition identity mismatch"
        )
    return disposition


def build_phase3_previous_head_bootstrap(
    *,
    workflow_id: str,
    source_revision: int,
    previous_manifest: ArtifactManifest,
) -> Phase3PreviousHead:
    validate_artifact_manifest(previous_manifest)
    prototype = Phase3PreviousHead(
        PHASE3_PREVIOUS_HEAD_SCHEMA,
        Phase3PreviousHeadKind.BOOTSTRAP,
        _identifier(workflow_id, "workflow_id"),
        _nonnegative(source_revision, "source_revision"),
        previous_manifest.manifest_sha256,
        None,
        None,
        None,
        "0" * 64,
    )
    return validate_phase3_previous_head(
        replace(
            prototype,
            head_sha256=canonical_sha256(_phase3_previous_head_identity(prototype)),
        )
    )


def build_phase3_previous_head_continuation(
    *,
    workflow_id: str,
    source_revision: int,
    previous_manifest: ArtifactManifest,
    previous_revision: int,
    previous_command_id: str,
    previous_mutation_sha256: str,
) -> Phase3PreviousHead:
    validate_artifact_manifest(previous_manifest)
    prototype = Phase3PreviousHead(
        PHASE3_PREVIOUS_HEAD_SCHEMA,
        Phase3PreviousHeadKind.CONTINUATION,
        _identifier(workflow_id, "workflow_id"),
        _nonnegative(source_revision, "source_revision"),
        previous_manifest.manifest_sha256,
        _positive(previous_revision, "previous_revision"),
        _identifier(previous_command_id, "previous_command_id"),
        str(_require_sha256(previous_mutation_sha256, "previous_mutation_sha256")),
        "0" * 64,
    )
    return validate_phase3_previous_head(
        replace(
            prototype,
            head_sha256=canonical_sha256(_phase3_previous_head_identity(prototype)),
        )
    )


def validate_phase3_previous_head(head: Phase3PreviousHead) -> Phase3PreviousHead:
    if type(head) is not Phase3PreviousHead or head.schema_version != PHASE3_PREVIOUS_HEAD_SCHEMA:
        raise Phase3ContractError("unsupported Phase-3 previous head")
    if type(head.kind) is not Phase3PreviousHeadKind:
        raise Phase3ContractError("Phase-3 previous head kind must be typed")
    _identifier(head.workflow_id, "workflow_id")
    _nonnegative(head.source_revision, "source_revision")
    _require_sha256(head.previous_manifest_sha256, "previous_manifest_sha256")
    if head.kind is Phase3PreviousHeadKind.BOOTSTRAP:
        if (
            head.previous_revision is not None
            or head.previous_command_id is not None
            or head.previous_mutation_sha256 is not None
        ):
            raise Phase3ContractError(
                "bootstrap Phase-3 previous head cannot bind a prior mutation"
            )
    else:
        _positive(head.previous_revision, "previous_revision")
        _identifier(head.previous_command_id, "previous_command_id")
        _require_sha256(head.previous_mutation_sha256, "previous_mutation_sha256")
    _require_sha256(head.head_sha256, "head_sha256")
    if head.head_sha256 != canonical_sha256(_phase3_previous_head_identity(head)):
        raise Phase3ContractError("Phase-3 previous head identity mismatch")
    return head


def _checkpoint_identity(value: CheckpointLedgerEntry) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "checkpoint_key": value.checkpoint_key,
        "state": value.state.value,
        "transition": value.transition.value,
        "owner_stage": value.owner_stage,
        "input_manifest_sha256": value.input_manifest_sha256,
        "validation_sha256": value.validation_sha256,
        "previous_checkpoint_id": value.previous_checkpoint_id,
        "reason_code": value.reason_code,
    }


def build_checkpoint_entry(
    *, checkpoint_key: str, owner_stage: int, input_manifest_sha256: str,
    state: CheckpointState, transition: CheckpointTransition,
    validation_sha256: str | None, previous_checkpoint_id: str | None = None,
    previous_checkpoint_occurrence_id: str | None = None,
    reason_code: str,
) -> CheckpointLedgerEntry:
    if type(state) is not CheckpointState or type(transition) is not CheckpointTransition:
        raise Phase3ContractError("checkpoint state and transition must be typed")
    key = _identifier(checkpoint_key, "checkpoint_key")
    stage = _positive(owner_stage, "owner_stage")
    if _checkpoint_key_owner_stage(key) != stage:
        raise Phase3ContractError("checkpoint key and owner stage differ")
    prototype = CheckpointLedgerEntry(
        CHECKPOINT_ENTRY_SCHEMA,
        "pending",
        key,
        state,
        transition,
        stage,
        "pending",
        str(_require_sha256(input_manifest_sha256, "input_manifest_sha256")),
        _require_sha256(validation_sha256, "validation_sha256", optional=True),
        previous_checkpoint_id,
        _identifier(reason_code, "reason_code"),
        "0" * 64,
        previous_checkpoint_occurrence_id,
    )
    digest = canonical_sha256(_checkpoint_identity(prototype))
    return validate_checkpoint_entry(
        replace(
            prototype,
            checkpoint_id=f"checkpoint-{digest}",
            source_record_key=f"phase3:checkpoint:{digest}",
            checkpoint_sha256=digest,
        )
    )


def validate_checkpoint_entry(entry: CheckpointLedgerEntry) -> CheckpointLedgerEntry:
    if type(entry) is not CheckpointLedgerEntry or entry.schema_version != CHECKPOINT_ENTRY_SCHEMA:
        raise Phase3ContractError("unsupported checkpoint ledger entry")
    _identifier(entry.checkpoint_id, "checkpoint_id")
    key_stage = _checkpoint_key_owner_stage(entry.checkpoint_key)
    _identifier(entry.source_record_key, "source_record_key")
    _identifier(entry.reason_code, "reason_code")
    _positive(entry.owner_stage, "owner_stage")
    if key_stage != entry.owner_stage:
        raise Phase3ContractError("checkpoint key and owner stage differ")
    _require_sha256(entry.input_manifest_sha256, "input_manifest_sha256")
    _require_sha256(entry.validation_sha256, "validation_sha256", optional=True)
    if (
        type(entry.state) is not CheckpointState
        or type(entry.transition) is not CheckpointTransition
    ):
        raise Phase3ContractError("checkpoint state and transition must be typed")
    initial = entry.transition in {
        CheckpointTransition.RECORDED_VALID,
        CheckpointTransition.RECORDED_INVALID,
    }
    if initial != (entry.previous_checkpoint_id is None):
        raise Phase3ContractError("checkpoint predecessor binding differs")
    if entry.previous_checkpoint_id is not None:
        _identifier(entry.previous_checkpoint_id, "previous_checkpoint_id")
    if entry.previous_checkpoint_occurrence_id is not None:
        _identifier(
            entry.previous_checkpoint_occurrence_id,
            "previous_checkpoint_occurrence_id",
        )
        if entry.previous_checkpoint_id is None:
            raise Phase3ContractError(
                "checkpoint occurrence predecessor lacks semantic predecessor"
            )
    expected_state = {
        CheckpointTransition.RECORDED_VALID: CheckpointState.VALID,
        CheckpointTransition.RECORDED_INVALID: CheckpointState.INVALID,
        CheckpointTransition.INVALIDATED: CheckpointState.INVALID,
        CheckpointTransition.REATTESTED_VALID: CheckpointState.VALID,
        CheckpointTransition.REATTESTED_INVALID: CheckpointState.INVALID,
    }[entry.transition]
    if entry.state is not expected_state:
        raise Phase3ContractError("checkpoint transition and state differ")
    if entry.state is CheckpointState.VALID and entry.validation_sha256 is None:
        raise Phase3ContractError("valid checkpoint requires validation identity")
    _require_sha256(entry.checkpoint_sha256, "checkpoint_sha256")
    expected = canonical_sha256(_checkpoint_identity(entry))
    if (
        entry.checkpoint_sha256 != expected
        or entry.checkpoint_id != f"checkpoint-{expected}"
        or entry.source_record_key != f"phase3:checkpoint:{expected}"
    ):
        raise Phase3ContractError("checkpoint ledger identity mismatch")
    return entry


def _reattestation_identity(value: CheckpointReattestationReceipt) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "previous_checkpoint_id": value.previous_checkpoint_id,
        "previous_manifest_sha256": value.previous_manifest_sha256,
        "current_manifest_sha256": value.current_manifest_sha256,
        "regenerated_validation_sha256": value.regenerated_validation_sha256,
        "classification": value.classification.value,
        "would_write": value.would_write,
    }


def validate_checkpoint_reattestation(
    receipt: CheckpointReattestationReceipt,
) -> CheckpointReattestationReceipt:
    if (
        type(receipt) is not CheckpointReattestationReceipt
        or receipt.schema_version != CHECKPOINT_REATTESTATION_SCHEMA
    ):
        raise Phase3ContractError("unsupported checkpoint re-attestation receipt")
    _identifier(receipt.previous_checkpoint_id, "previous_checkpoint_id")
    _require_sha256(receipt.previous_manifest_sha256, "previous_manifest_sha256")
    _require_sha256(receipt.current_manifest_sha256, "current_manifest_sha256")
    validation = _require_sha256(
        receipt.regenerated_validation_sha256,
        "regenerated_validation_sha256",
        optional=True,
    )
    if type(receipt.classification) is not ReattestationClassification:
        raise Phase3ContractError("re-attestation classification must be typed")
    if type(receipt.would_write) is not bool:
        raise Phase3ContractError("re-attestation write decision must be boolean")
    should_write = (
        receipt.classification is ReattestationClassification.REGENERATED
    )
    if receipt.would_write is not should_write:
        raise Phase3ContractError("re-attestation classification and write decision differ")
    if receipt.classification in {
        ReattestationClassification.REUSED,
        ReattestationClassification.REGENERATED,
    } and validation is None:
        raise Phase3ContractError("successful re-attestation requires validation identity")
    _require_sha256(receipt.receipt_sha256, "receipt_sha256")
    if receipt.receipt_sha256 != canonical_sha256(_reattestation_identity(receipt)):
        raise Phase3ContractError("checkpoint re-attestation identity mismatch")
    return receipt


def dry_run_checkpoint_reattestation(
    previous: CheckpointLedgerEntry,
    *, current_manifest_sha256: str, regenerated_valid: bool,
    regenerated_validation_sha256: str | None = None,
) -> CheckpointReattestationReceipt:
    validate_checkpoint_entry(previous)
    current_hash = str(_require_sha256(current_manifest_sha256, "current_manifest_sha256"))
    validation_hash = _require_sha256(
        regenerated_validation_sha256, "regenerated_validation_sha256", optional=True
    )
    if type(regenerated_valid) is not bool:
        raise Phase3ContractError("regenerated_valid must be a boolean")
    if previous.state is CheckpointState.VALID and previous.input_manifest_sha256 == current_hash:
        classification = ReattestationClassification.REUSED
        validation_hash = previous.validation_sha256
    elif regenerated_valid:
        if validation_hash is None:
            raise Phase3ContractError("regenerated valid checkpoint requires validation identity")
        classification = ReattestationClassification.REGENERATED
    else:
        classification = ReattestationClassification.STILL_INVALID
    prototype = CheckpointReattestationReceipt(
        CHECKPOINT_REATTESTATION_SCHEMA,
        previous.checkpoint_id,
        previous.input_manifest_sha256,
        current_hash,
        validation_hash,
        classification,
        classification is ReattestationClassification.REGENERATED,
        "0" * 64,
    )
    return validate_checkpoint_reattestation(
        replace(
            prototype,
            receipt_sha256=canonical_sha256(_reattestation_identity(prototype)),
        )
    )


def _parity_identity(value: ParityReceipt) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "subject": value.subject,
        "v1_sha256": value.v1_sha256,
        "shadow_sha256": value.shadow_sha256,
        "classification": value.classification.value,
        "difference_codes": value.difference_codes,
        "blocker_codes": value.blocker_codes,
    }


def validate_parity_receipt(receipt: ParityReceipt) -> ParityReceipt:
    if type(receipt) is not ParityReceipt or receipt.schema_version != PARITY_RECEIPT_SCHEMA:
        raise Phase3ContractError("unsupported artifact parity receipt")
    _identifier(receipt.subject, "subject")
    left = _require_sha256(receipt.v1_sha256, "v1_sha256", optional=True)
    right = _require_sha256(receipt.shadow_sha256, "shadow_sha256", optional=True)
    if type(receipt.classification) is not ParityClassification:
        raise Phase3ContractError("parity classification must be typed")
    if type(receipt.difference_codes) is not tuple or type(receipt.blocker_codes) is not tuple:
        raise Phase3ContractError("parity code collections must be tuples")
    differences = tuple(_identifier(item, "difference_code") for item in receipt.difference_codes)
    blockers = tuple(_identifier(item, "blocker_code") for item in receipt.blocker_codes)
    if differences != tuple(sorted(set(differences))):
        raise Phase3ContractError("parity difference codes must be sorted and unique")
    if blockers != tuple(sorted(set(blockers))):
        raise Phase3ContractError("parity blocker codes must be sorted and unique")
    if blockers or left is None or right is None:
        expected = ParityClassification.BLOCKED
    elif left == right:
        expected = ParityClassification.MATCH
        if differences:
            raise Phase3ContractError("matching parity cannot carry difference codes")
    elif differences:
        expected = ParityClassification.EXPECTED_DIFFERENCE
    else:
        expected = ParityClassification.DIVERGENCE
    if receipt.classification is not expected:
        raise Phase3ContractError("parity classification differs from evidence")
    _require_sha256(receipt.receipt_sha256, "receipt_sha256")
    if receipt.receipt_sha256 != canonical_sha256(_parity_identity(receipt)):
        raise Phase3ContractError("artifact parity receipt identity mismatch")
    return receipt


def build_parity_receipt(
    *, subject: str, v1_sha256: str | None, shadow_sha256: str | None,
    expected_difference_codes: Iterable[str] = (), blocker_codes: Iterable[str] = (),
) -> ParityReceipt:
    left = _require_sha256(v1_sha256, "v1_sha256", optional=True)
    right = _require_sha256(shadow_sha256, "shadow_sha256", optional=True)
    differences = tuple(
        sorted(
            {
                _identifier(item, "difference_code")
                for item in expected_difference_codes
            }
        )
    )
    blockers = tuple(sorted({_identifier(item, "blocker_code") for item in blocker_codes}))
    if blockers or left is None or right is None:
        classification = ParityClassification.BLOCKED
    elif left == right:
        classification = ParityClassification.MATCH
        differences = ()
    elif differences:
        classification = ParityClassification.EXPECTED_DIFFERENCE
    else:
        classification = ParityClassification.DIVERGENCE
    prototype = ParityReceipt(
        PARITY_RECEIPT_SCHEMA,
        _identifier(subject, "subject"),
        left,
        right,
        classification,
        differences,
        blockers,
        "0" * 64,
    )
    return validate_parity_receipt(
        replace(
            prototype,
            receipt_sha256=canonical_sha256(_parity_identity(prototype)),
        )
    )


def _mutation_identity(value: Phase3Mutation) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "previous_manifest": (
            value.previous_manifest.as_dict() if value.previous_manifest else None
        ),
        "current_manifest": (
            value.current_manifest.as_dict() if value.current_manifest else None
        ),
        "change_set": value.change_set.as_dict() if value.change_set else None,
        "artifact_records": tuple(item.as_dict() for item in value.artifact_records),
        "artifact_blockers": tuple(
            item.as_dict() for item in value.artifact_blockers
        ),
        "removals": tuple(item.as_dict() for item in value.removals),
        "checkpoint_entries": tuple(item.as_dict() for item in value.checkpoint_entries),
        "reopen_plan": value.reopen_plan.as_dict() if value.reopen_plan else None,
        "blocked_disposition": (
            value.blocked_disposition.as_dict()
            if value.blocked_disposition
            else None
        ),
        "previous_head": (
            value.previous_head.as_dict() if value.previous_head else None
        ),
    }


def build_phase3_mutation(
    *, artifact_records: Iterable[ArtifactRecord] = (),
    artifact_blockers: Iterable[ArtifactBlocker] = (),
    removals: Iterable[ArtifactRemoval] = (),
    checkpoint_entries: Iterable[CheckpointLedgerEntry] = (),
    reopen_plan: ReopenPlan | None = None,
    previous_manifest: ArtifactManifest | None = None,
    current_manifest: ArtifactManifest | None = None,
    change_set: ChangeSet | None = None,
    blocked_disposition: BlockedNoReopenDisposition | None = None,
    previous_head: Phase3PreviousHead | None = None,
) -> Phase3Mutation:
    unsorted_records = tuple(artifact_records)
    unsorted_blockers = tuple(artifact_blockers)
    unsorted_removals = tuple(removals)
    unsorted_checkpoints = tuple(checkpoint_entries)
    if any(type(item) is not ArtifactRecord for item in unsorted_records):
        raise Phase3ContractError("Phase-3 artifact records must be typed")
    if any(type(item) is not CheckpointLedgerEntry for item in unsorted_checkpoints):
        raise Phase3ContractError("Phase-3 checkpoint entries must be typed")
    if any(type(item) is not ArtifactBlocker for item in unsorted_blockers):
        raise Phase3ContractError("Phase-3 artifact blockers must be typed")
    if any(type(item) is not ArtifactRemoval for item in unsorted_removals):
        raise Phase3ContractError("Phase-3 artifact removals must be typed")
    records = tuple(
        sorted(
            unsorted_records,
            key=lambda item: (item.normalized_path, item.artifact_record_id),
        )
    )
    checkpoints = tuple(
        sorted(unsorted_checkpoints, key=lambda item: item.checkpoint_id)
    )
    blockers = tuple(
        sorted(
            unsorted_blockers,
            key=lambda item: (item.normalized_path, item.code.value),
        )
    )
    removal_values = tuple(
        sorted(unsorted_removals, key=lambda item: item.normalized_path)
    )
    prototype = Phase3Mutation(
        schema_version=PHASE3_MUTATION_SCHEMA,
        artifact_records=records,
        checkpoint_entries=checkpoints,
        reopen_plan=reopen_plan,
        mutation_sha256="0" * 64,
        previous_manifest=previous_manifest,
        current_manifest=current_manifest,
        change_set=change_set,
        artifact_blockers=blockers,
        removals=removal_values,
        blocked_disposition=blocked_disposition,
        previous_head=previous_head,
    )
    return validate_phase3_mutation(
        replace(prototype, mutation_sha256=canonical_sha256(_mutation_identity(prototype)))
    )


def validate_phase3_mutation(mutation: Phase3Mutation) -> Phase3Mutation:
    if type(mutation) is not Phase3Mutation or mutation.schema_version != PHASE3_MUTATION_SCHEMA:
        raise Phase3ContractError("unsupported Phase-3 mutation")
    if (
        type(mutation.artifact_records) is not tuple
        or type(mutation.artifact_blockers) is not tuple
        or type(mutation.removals) is not tuple
        or type(mutation.checkpoint_entries) is not tuple
    ):
        raise Phase3ContractError("Phase-3 mutation collections must be tuples")
    if (
        not mutation.artifact_records
        and not mutation.artifact_blockers
        and not mutation.removals
        and not mutation.checkpoint_entries
        and mutation.reopen_plan is None
        and mutation.blocked_disposition is None
        and mutation.previous_head is None
        and mutation.previous_manifest is None
        and mutation.current_manifest is None
        and mutation.change_set is None
    ):
        raise Phase3ContractError("Phase-3 mutation must contain at least one immutable record")
    for record in mutation.artifact_records:
        validate_artifact_record(record)
    for blocker in mutation.artifact_blockers:
        validate_artifact_blocker(blocker)
    for removal in mutation.removals:
        validate_artifact_removal(removal)
    for entry in mutation.checkpoint_entries:
        validate_checkpoint_entry(entry)
    if mutation.reopen_plan is not None:
        validate_reopen_plan(mutation.reopen_plan)
    if mutation.blocked_disposition is not None:
        validate_blocked_no_reopen_disposition(mutation.blocked_disposition)
    if mutation.previous_head is not None:
        validate_phase3_previous_head(mutation.previous_head)
    graph_values = (
        mutation.previous_manifest,
        mutation.current_manifest,
        mutation.change_set,
    )
    if any(value is not None for value in graph_values) and any(
        value is None for value in graph_values
    ):
        raise Phase3ContractError(
            "Phase-3 mutation graph must bind both manifests and the ChangeSet"
        )
    if mutation.reopen_plan is not None and mutation.blocked_disposition is not None:
        raise Phase3ContractError(
            "Phase-3 mutation cannot carry both a ReopenPlan and blocked disposition"
        )
    if (
        mutation.previous_head is not None
        and mutation.previous_manifest is None
    ):
        raise Phase3ContractError(
            "Phase-3 previous head requires the complete mutation graph"
        )
    if (
        mutation.blocked_disposition is not None
        and mutation.previous_manifest is None
    ):
        raise Phase3ContractError(
            "blocked Phase-3 disposition requires the complete mutation graph"
        )
    if tuple(
        sorted(
            mutation.artifact_records,
            key=lambda item: (item.normalized_path, item.artifact_record_id),
        )
    ) != mutation.artifact_records:
        raise Phase3ContractError("Phase-3 artifact records must be identity sorted")
    if tuple(
        sorted(
            mutation.artifact_blockers,
            key=lambda item: (item.normalized_path, item.code.value),
        )
    ) != mutation.artifact_blockers:
        raise Phase3ContractError("Phase-3 artifact blockers must be identity sorted")
    if tuple(
        sorted(mutation.removals, key=lambda item: item.normalized_path)
    ) != mutation.removals:
        raise Phase3ContractError("Phase-3 artifact removals must be path sorted")
    if tuple(
        sorted(mutation.checkpoint_entries, key=lambda item: item.checkpoint_id)
    ) != mutation.checkpoint_entries:
        raise Phase3ContractError("Phase-3 checkpoints must be identity sorted")
    if len(
        {item.artifact_record_id for item in mutation.artifact_records}
    ) != len(mutation.artifact_records):
        raise Phase3ContractError("Phase-3 artifact record identities must be unique")
    if len({item.normalized_path for item in mutation.artifact_records}) != len(
        mutation.artifact_records
    ):
        raise Phase3ContractError("Phase-3 artifact paths must be unique")
    if len({item.normalized_path for item in mutation.artifact_blockers}) != len(
        mutation.artifact_blockers
    ):
        raise Phase3ContractError("Phase-3 artifact blocker paths must be unique")
    if len({item.normalized_path for item in mutation.removals}) != len(
        mutation.removals
    ):
        raise Phase3ContractError("Phase-3 artifact removal paths must be unique")
    if len(
        {
            item.registration.owner_compilation_sha256
            for item in mutation.artifact_records
        }
    ) > 1:
        raise Phase3ContractError(
            "Phase-3 artifact records must share one frozen owner policy"
        )
    if len(
        {item.checkpoint_id for item in mutation.checkpoint_entries}
    ) != len(mutation.checkpoint_entries):
        raise Phase3ContractError("Phase-3 checkpoint identities must be unique")
    if len({item.checkpoint_key for item in mutation.checkpoint_entries}) != len(
        mutation.checkpoint_entries
    ):
        raise Phase3ContractError("Phase-3 checkpoint keys must be unique")
    if mutation.previous_manifest is not None:
        assert mutation.current_manifest is not None
        assert mutation.change_set is not None
        previous = validate_artifact_manifest(mutation.previous_manifest)
        current = validate_artifact_manifest(mutation.current_manifest)
        change_set = validate_change_set(mutation.change_set)
        if (
            change_set.previous_manifest_sha256 != previous.manifest_sha256
            or change_set.current_manifest_sha256 != current.manifest_sha256
        ):
            raise Phase3ContractError(
                "Phase-3 mutation manifest and ChangeSet identities differ"
            )
        recomputed = compute_change_set(
            previous,
            current,
            removals=mutation.removals,
        )
        if recomputed != change_set:
            raise Phase3ContractError(
                "Phase-3 mutation ChangeSet does not match its manifests"
            )
        if mutation.artifact_records != current.records:
            raise Phase3ContractError(
                "Phase-3 mutation records do not exactly match current manifest"
            )
        if mutation.artifact_blockers != current.blockers:
            raise Phase3ContractError(
                "Phase-3 mutation blockers do not exactly match current manifest"
            )
        if mutation.removals != change_set.removals:
            raise Phase3ContractError(
                "Phase-3 mutation removals do not exactly match ChangeSet"
            )
        if not mutation.checkpoint_entries:
            raise Phase3ContractError(
                "complete Phase-3 mutation requires a checkpoint occurrence"
            )
        if mutation.previous_head is None:
            raise Phase3ContractError(
                "complete Phase-3 mutation requires an explicit previous head"
            )
        if mutation.previous_head.previous_manifest_sha256 != previous.manifest_sha256:
            raise Phase3ContractError(
                "Phase-3 previous head previous manifest differs"
            )
        for checkpoint in mutation.checkpoint_entries:
            if checkpoint.input_manifest_sha256 != current.manifest_sha256:
                raise Phase3ContractError(
                    "Phase-3 checkpoint input manifest differs from current manifest"
                )
        blocked = tuple(
            item
            for item in change_set.dirty_decisions
            if item.disposition is DirtyDisposition.BLOCKED
        )
        dirty = tuple(
            item
            for item in change_set.dirty_decisions
            if item.disposition is DirtyDisposition.DIRTY
        )
        if blocked or dirty:
            control = mutation.reopen_plan or mutation.blocked_disposition
            if control is None:
                raise Phase3ContractError(
                    "complete Phase-3 mutation requires a reopen-ledger control record"
                )
            if (
                control.workflow_id != mutation.previous_head.workflow_id
                or control.source_revision != mutation.previous_head.source_revision
            ):
                raise Phase3ContractError(
                    "Phase-3 previous head source coordinate differs"
                )
        if blocked:
            if mutation.reopen_plan is not None:
                raise Phase3ContractError(
                    "blocked Phase-3 mutation cannot carry a ReopenPlan"
                )
            if mutation.blocked_disposition is None:
                raise Phase3ContractError(
                    "blocked Phase-3 mutation requires a blocked disposition"
                )
            expected_blocked = build_blocked_no_reopen_disposition(
                workflow_id=mutation.blocked_disposition.workflow_id,
                source_revision=mutation.blocked_disposition.source_revision,
                change_set=change_set,
                previous_manifest=previous,
                current_manifest=current,
                previous_occurrence_ids={
                    item.normalized_path: item.expected_occurrence_id
                    for item in mutation.blocked_disposition.read_set
                    if item.expected_occurrence_id is not None
                },
            )
            if mutation.blocked_disposition != expected_blocked:
                raise Phase3ContractError(
                    "Phase-3 blocked disposition differs from the mutation graph"
                )
            target = mutation.blocked_disposition.target_owner_stage
            for checkpoint in mutation.checkpoint_entries:
                if checkpoint.owner_stage != target:
                    raise Phase3ContractError(
                        "Phase-3 checkpoint owner differs from blocked disposition target"
                    )
        elif dirty:
            if mutation.reopen_plan is None:
                raise Phase3ContractError(
                    "dirty Phase-3 mutation requires its ReopenPlan"
                )
            if mutation.blocked_disposition is not None:
                raise Phase3ContractError(
                    "dirty Phase-3 mutation cannot carry a blocked disposition"
                )
            expected_plan = build_reopen_plan(
                workflow_id=mutation.reopen_plan.workflow_id,
                source_revision=mutation.reopen_plan.source_revision,
                change_set=change_set,
                previous_manifest=previous,
                previous_occurrence_ids={
                    item.normalized_path: item.expected_occurrence_id
                    for item in mutation.reopen_plan.read_set
                    if item.expected_occurrence_id is not None
                },
            )
            if mutation.reopen_plan != expected_plan:
                raise Phase3ContractError(
                    "Phase-3 ReopenPlan differs from the mutation graph"
                )
            target = mutation.reopen_plan.target_owner_stage
            for checkpoint in mutation.checkpoint_entries:
                if checkpoint.owner_stage != target:
                    raise Phase3ContractError(
                        "Phase-3 checkpoint owner differs from ReopenPlan target"
                    )
        elif mutation.reopen_plan is not None or mutation.blocked_disposition is not None:
            raise Phase3ContractError(
                "clean Phase-3 mutation cannot carry a reopen disposition"
            )
    _require_sha256(mutation.mutation_sha256, "mutation_sha256")
    if mutation.mutation_sha256 != canonical_sha256(_mutation_identity(mutation)):
        raise Phase3ContractError("Phase-3 mutation identity mismatch")
    return mutation


def artifact_blocker_semantic_sha256(blocker: ArtifactBlocker) -> str:
    validate_artifact_blocker(blocker)
    return canonical_sha256(
        {
            "schema": "authority-artifact-blocker-semantic-v1",
            "blocker": blocker.as_dict(),
        }
    )


def _artifact_occurrence_identity(
    value: ArtifactLedgerOccurrence,
) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "workflow_id": value.workflow_id,
        "revision": value.revision,
        "command_id": value.command_id,
        "mutation_sha256": value.mutation_sha256,
        "kind": value.kind.value,
        "normalized_path": value.normalized_path,
        "semantic_sha256": value.semantic_sha256,
        "artifact_record": (
            value.artifact_record.as_dict() if value.artifact_record else None
        ),
        "blocker": value.blocker.as_dict() if value.blocker else None,
        "removal": value.removal.as_dict() if value.removal else None,
    }


def build_artifact_occurrence(
    *,
    workflow_id: str,
    revision: int,
    command_id: str,
    mutation_sha256: str,
    artifact_record: ArtifactRecord | None = None,
    blocker: ArtifactBlocker | None = None,
    removal: ArtifactRemoval | None = None,
) -> ArtifactLedgerOccurrence:
    values = (artifact_record, blocker, removal)
    if sum(item is not None for item in values) != 1:
        raise Phase3ContractError(
            "artifact occurrence must carry exactly one semantic value"
        )
    if artifact_record is not None:
        validate_artifact_record(artifact_record)
        kind = ArtifactOccurrenceKind.RECORD
        path = artifact_record.normalized_path
        semantic_sha256 = artifact_record.record_sha256
    elif blocker is not None:
        validate_artifact_blocker(blocker)
        kind = ArtifactOccurrenceKind.BLOCKER
        path = blocker.normalized_path
        semantic_sha256 = artifact_blocker_semantic_sha256(blocker)
    else:
        assert removal is not None
        validate_artifact_removal(removal)
        kind = ArtifactOccurrenceKind.REMOVAL
        path = removal.normalized_path
        semantic_sha256 = removal.removal_sha256
    prototype = ArtifactLedgerOccurrence(
        ARTIFACT_OCCURRENCE_SCHEMA,
        "pending",
        _identifier(workflow_id, "workflow_id"),
        _positive(revision, "revision"),
        _identifier(command_id, "command_id"),
        str(_require_sha256(mutation_sha256, "mutation_sha256")),
        kind,
        path,
        semantic_sha256,
        artifact_record,
        blocker,
        removal,
    )
    digest = canonical_sha256(_artifact_occurrence_identity(prototype))
    return validate_artifact_occurrence(
        replace(
            prototype,
            occurrence_id=f"phase3-artifact-occurrence-{digest}",
        )
    )


def validate_artifact_occurrence(
    occurrence: ArtifactLedgerOccurrence,
) -> ArtifactLedgerOccurrence:
    if (
        type(occurrence) is not ArtifactLedgerOccurrence
        or occurrence.schema_version != ARTIFACT_OCCURRENCE_SCHEMA
    ):
        raise Phase3ContractError("unsupported artifact ledger occurrence")
    _identifier(occurrence.occurrence_id, "artifact occurrence_id")
    _identifier(occurrence.workflow_id, "workflow_id")
    _positive(occurrence.revision, "revision")
    _identifier(occurrence.command_id, "command_id")
    _require_sha256(occurrence.mutation_sha256, "mutation_sha256")
    _require_sha256(occurrence.semantic_sha256, "semantic_sha256")
    if type(occurrence.kind) is not ArtifactOccurrenceKind:
        raise Phase3ContractError("artifact occurrence kind must be typed")
    expected_kind = {
        ArtifactOccurrenceKind.RECORD: occurrence.artifact_record,
        ArtifactOccurrenceKind.BLOCKER: occurrence.blocker,
        ArtifactOccurrenceKind.REMOVAL: occurrence.removal,
    }[occurrence.kind]
    if expected_kind is None or sum(
        item is not None
        for item in (
            occurrence.artifact_record,
            occurrence.blocker,
            occurrence.removal,
        )
    ) != 1:
        raise Phase3ContractError("artifact occurrence typed payload differs")
    if occurrence.artifact_record is not None:
        validate_artifact_record(occurrence.artifact_record)
        path = occurrence.artifact_record.normalized_path
        semantic_sha256 = occurrence.artifact_record.record_sha256
    elif occurrence.blocker is not None:
        validate_artifact_blocker(occurrence.blocker)
        path = occurrence.blocker.normalized_path
        semantic_sha256 = artifact_blocker_semantic_sha256(occurrence.blocker)
    else:
        assert occurrence.removal is not None
        validate_artifact_removal(occurrence.removal)
        path = occurrence.removal.normalized_path
        semantic_sha256 = occurrence.removal.removal_sha256
    if occurrence.normalized_path != path or occurrence.semantic_sha256 != semantic_sha256:
        raise Phase3ContractError("artifact occurrence semantic identity differs")
    digest = canonical_sha256(_artifact_occurrence_identity(occurrence))
    if occurrence.occurrence_id != f"phase3-artifact-occurrence-{digest}":
        raise Phase3ContractError("artifact occurrence identity mismatch")
    return occurrence


def _checkpoint_occurrence_identity(
    value: CheckpointLedgerOccurrence,
) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "workflow_id": value.workflow_id,
        "revision": value.revision,
        "command_id": value.command_id,
        "mutation_sha256": value.mutation_sha256,
        "checkpoint_entry": value.checkpoint_entry.as_dict(),
    }


def build_checkpoint_occurrence(
    *,
    workflow_id: str,
    revision: int,
    command_id: str,
    mutation_sha256: str,
    checkpoint_entry: CheckpointLedgerEntry,
) -> CheckpointLedgerOccurrence:
    validate_checkpoint_entry(checkpoint_entry)
    prototype = CheckpointLedgerOccurrence(
        CHECKPOINT_OCCURRENCE_SCHEMA,
        "pending",
        _identifier(workflow_id, "workflow_id"),
        _positive(revision, "revision"),
        _identifier(command_id, "command_id"),
        str(_require_sha256(mutation_sha256, "mutation_sha256")),
        checkpoint_entry,
    )
    digest = canonical_sha256(_checkpoint_occurrence_identity(prototype))
    return validate_checkpoint_occurrence(
        replace(
            prototype,
            occurrence_id=f"phase3-checkpoint-occurrence-{digest}",
        )
    )


def validate_checkpoint_occurrence(
    occurrence: CheckpointLedgerOccurrence,
) -> CheckpointLedgerOccurrence:
    if (
        type(occurrence) is not CheckpointLedgerOccurrence
        or occurrence.schema_version != CHECKPOINT_OCCURRENCE_SCHEMA
    ):
        raise Phase3ContractError("unsupported checkpoint ledger occurrence")
    _identifier(occurrence.occurrence_id, "checkpoint occurrence_id")
    _identifier(occurrence.workflow_id, "workflow_id")
    _positive(occurrence.revision, "revision")
    _identifier(occurrence.command_id, "command_id")
    _require_sha256(occurrence.mutation_sha256, "mutation_sha256")
    validate_checkpoint_entry(occurrence.checkpoint_entry)
    digest = canonical_sha256(_checkpoint_occurrence_identity(occurrence))
    if occurrence.occurrence_id != f"phase3-checkpoint-occurrence-{digest}":
        raise Phase3ContractError("checkpoint occurrence identity mismatch")
    return occurrence


def _require_keys(value: object, expected: set[str], field: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise Phase3ContractError(f"{field} fields differ from the typed schema")
    return value


def artifact_registration_from_dict(value: object) -> ArtifactRegistration:
    data = _require_keys(value, set(ArtifactRegistration.__dataclass_fields__), "registration")
    if type(data["matching_rule_ids"]) not in {list, tuple}:
        raise Phase3ContractError("matching_rule_ids must be an array or tuple")
    return validate_artifact_registration(
        ArtifactRegistration(
            data["schema_version"], data["normalized_path"],
            data["owner_compilation_sha256"], tuple(data["matching_rule_ids"]),
            data["owner_rule_id"], data["owner_id"], data["owner_stage"],
            data["semantic_domain"], data["dirty_flag"], data["final_input"],
            data["submission_member"], data["registration_sha256"],
        )
    )


def artifact_record_from_dict(value: object) -> ArtifactRecord:
    data = _require_keys(value, set(ArtifactRecord.__dataclass_fields__), "artifact_record")
    if type(data["availability"]) is not str:
        raise Phase3ContractError("artifact availability is invalid")
    try:
        availability = ArtifactAvailability(data["availability"])
    except (TypeError, ValueError) as exc:
        raise Phase3ContractError("artifact availability is invalid") from exc
    return validate_artifact_record(
        ArtifactRecord(
            data["schema_version"], data["artifact_record_id"],
            data["artifact_type"], data["normalized_path"],
            data["content_sha256"], data["byte_length"], availability,
            artifact_registration_from_dict(data["registration"]), data["record_sha256"],
        )
    )


def artifact_owner_compilation_binding_from_dict(
    value: object,
) -> ArtifactOwnerCompilationBinding:
    data = _require_keys(
        value,
        set(ArtifactOwnerCompilationBinding.__dataclass_fields__),
        "artifact_owner_compilation_binding",
    )
    if type(data["registry"]) not in {list, tuple} or type(
        data["priority_authorizations"]
    ) not in {list, tuple}:
        raise Phase3ContractError("artifact owner compilation arrays are malformed")
    registry = []
    for value_item in data["registry"]:
        item = _require_keys(
            value_item,
            set(ArtifactOwnership.__dataclass_fields__),
            "artifact_ownership",
        )
        registry.append(
            ArtifactOwnership(
                item["pattern"],
                item["owner_stage"],
                item["semantic_domain"],
                item["dirty_flag"],
                item["final_input"],
                item["submission_member"],
            )
        )
    authorizations = []
    for value_item in data["priority_authorizations"]:
        item = _require_keys(
            value_item,
            set(OwnerPriorityAuthorization.__dataclass_fields__),
            "owner_priority_authorization",
        )
        authorizations.append(
            OwnerPriorityAuthorization(
                item["winner_pattern"],
                item["winner_owner_stage"],
                item["loser_pattern"],
                item["loser_owner_stage"],
                item["issue_id"],
                item["rationale"],
            )
        )
    return validate_artifact_owner_compilation_binding(
        ArtifactOwnerCompilationBinding(
            data["schema_version"],
            tuple(registry),
            tuple(authorizations),
            data["owner_compilation_sha256"],
            data["binding_sha256"],
        )
    )


def artifact_owner_operator_claim_from_dict(
    value: object,
) -> ArtifactOwnerOperatorClaim:
    data = _require_keys(
        value,
        set(ArtifactOwnerOperatorClaim.__dataclass_fields__),
        "artifact_owner_operator_claim",
    )
    return validate_artifact_owner_operator_claim(
        ArtifactOwnerOperatorClaim(
            data["schema_version"],
            data["workflow_id"],
            data["source_revision"],
            data["command_id"],
            data["normalized_path"],
            artifact_owner_compilation_binding_from_dict(
                data["owner_compilation"]
            ),
            data["owner_compilation_sha256"],
            data["owner_id"],
            data["owner_stage"],
            data["dirty_flag"],
            data["operator_subject"],
            data["reason_code"],
            data["claim_sha256"],
        )
    )


def artifact_owner_operator_authorization_from_dict(
    value: object,
) -> ArtifactOwnerOperatorAuthorization:
    data = _require_keys(
        value,
        set(ArtifactOwnerOperatorAuthorization.__dataclass_fields__),
        "artifact_owner_operator_authorization",
    )
    return validate_artifact_owner_operator_authorization(
        ArtifactOwnerOperatorAuthorization(
            data["schema_version"],
            data["workflow_id"],
            data["source_revision"],
            data["command_id"],
            data["normalized_path"],
            artifact_owner_compilation_binding_from_dict(
                data["owner_compilation"]
            ),
            data["owner_compilation_sha256"],
            data["owner_id"],
            data["owner_stage"],
            data["dirty_flag"],
            data["operator_subject"],
            data["reason_code"],
            data["claim_sha256"],
            data["issuer_kind"],
            data["issuer_writer_id"],
            data["issuer_writer_epoch"],
            data["issuer_receipt_id"],
            data["issuer_receipt_sha256"],
            data["authorization_sha256"],
        )
    )


def artifact_blocker_from_dict(value: object) -> ArtifactBlocker:
    data = _require_keys(value, set(ArtifactBlocker.__dataclass_fields__), "blocker")
    if type(data["code"]) is not str:
        raise Phase3ContractError("artifact blocker code is invalid")
    try:
        code = ArtifactBlockerCode(data["code"])
    except (TypeError, ValueError) as exc:
        raise Phase3ContractError("artifact blocker code is invalid") from exc
    return validate_artifact_blocker(
        ArtifactBlocker(
            code,
            data["normalized_path"],
            data["detail"],
            (
                artifact_registration_from_dict(data["registration"])
                if data["registration"] is not None
                else None
            ),
            (
                artifact_owner_operator_authorization_from_dict(
                    data["operator_authorization"]
                )
                if data["operator_authorization"] is not None
                else None
            ),
        )
    )


def artifact_manifest_from_dict(value: object) -> ArtifactManifest:
    data = _require_keys(value, set(ArtifactManifest.__dataclass_fields__), "manifest")
    if type(data["tracked_paths"]) not in {list, tuple}:
        raise Phase3ContractError("manifest tracked_paths must be an array or tuple")
    if type(data["records"]) not in {list, tuple} or type(data["blockers"]) not in {
        list,
        tuple,
    }:
        raise Phase3ContractError("manifest outcomes must be arrays or tuples")
    return validate_artifact_manifest(
        ArtifactManifest(
            data["schema_version"],
            data["owner_compilation_sha256"],
            tuple(data["tracked_paths"]),
            tuple(artifact_record_from_dict(item) for item in data["records"]),
            tuple(artifact_blocker_from_dict(item) for item in data["blockers"]),
            data["manifest_sha256"],
        )
    )


def artifact_removal_from_dict(value: object) -> ArtifactRemoval:
    data = _require_keys(value, set(ArtifactRemoval.__dataclass_fields__), "removal")
    return validate_artifact_removal(
        ArtifactRemoval(
            data["schema_version"],
            data["normalized_path"],
            data["previous_record_sha256"],
            data["previous_blocker_code"],
            data["owner_compilation_sha256"],
            data["owner_id"],
            data["owner_stage"],
            data["dirty_flag"],
            data["reason_code"],
            data["removal_sha256"],
        )
    )


def change_set_from_dict(value: object) -> ChangeSet:
    data = _require_keys(value, set(ChangeSet.__dataclass_fields__), "change_set")
    if any(
        type(data[field]) not in {list, tuple}
        for field in ("removals", "changes", "dirty_decisions")
    ):
        raise Phase3ContractError("change set collections must be arrays or tuples")
    changes = []
    for raw in data["changes"]:
        item = _require_keys(raw, set(ArtifactChange.__dataclass_fields__), "change")
        if type(item["kind"]) is not str:
            raise Phase3ContractError("artifact change kind is invalid")
        try:
            kind = ArtifactChangeKind(item["kind"])
        except (TypeError, ValueError) as exc:
            raise Phase3ContractError("artifact change kind is invalid") from exc
        changes.append(
            ArtifactChange(
                item["normalized_path"],
                kind,
                item["previous_record_sha256"],
                item["current_record_sha256"],
                item["previous_blocker_code"],
                item["removal_sha256"],
            )
        )
    decisions = []
    for raw in data["dirty_decisions"]:
        item = _require_keys(raw, set(DirtyDecision.__dataclass_fields__), "dirty_decision")
        if type(item["disposition"]) is not str:
            raise Phase3ContractError("dirty disposition is invalid")
        try:
            disposition = DirtyDisposition(item["disposition"])
        except (TypeError, ValueError) as exc:
            raise Phase3ContractError("dirty disposition is invalid") from exc
        decisions.append(
            DirtyDecision(
                item["normalized_path"],
                disposition,
                item["dirty_flag"],
                item["owner_id"],
                item["owner_stage"],
                item["reason_code"],
            )
        )
    return validate_change_set(
        ChangeSet(
            data["schema_version"],
            data["previous_manifest_sha256"],
            data["current_manifest_sha256"],
            tuple(artifact_removal_from_dict(item) for item in data["removals"]),
            tuple(changes),
            tuple(decisions),
            data["change_set_sha256"],
        )
    )


def checkpoint_entry_from_dict(value: object) -> CheckpointLedgerEntry:
    data = _require_keys(value, set(CheckpointLedgerEntry.__dataclass_fields__), "checkpoint")
    if type(data["state"]) is not str or type(data["transition"]) is not str:
        raise Phase3ContractError("checkpoint state or transition is invalid")
    try:
        state = CheckpointState(data["state"])
        transition = CheckpointTransition(data["transition"])
    except (TypeError, ValueError) as exc:
        raise Phase3ContractError("checkpoint state or transition is invalid") from exc
    return validate_checkpoint_entry(
        CheckpointLedgerEntry(
            data["schema_version"], data["checkpoint_id"],
            data["checkpoint_key"], state, transition, data["owner_stage"],
            data["source_record_key"], data["input_manifest_sha256"],
            data["validation_sha256"], data["previous_checkpoint_id"],
            data["reason_code"], data["checkpoint_sha256"],
            data["previous_checkpoint_occurrence_id"],
        )
    )


def reopen_plan_from_dict(value: object) -> ReopenPlan:
    data = _require_keys(value, set(ReopenPlan.__dataclass_fields__), "reopen_plan")
    raw_read_set = data["read_set"]
    if type(raw_read_set) not in {list, tuple}:
        raise Phase3ContractError("reopen read_set must be an array or tuple")
    entries = []
    for raw in raw_read_set:
        item = _require_keys(
            raw,
            set(ArtifactReadExpectation.__dataclass_fields__),
            "read_set_entry",
        )
        entries.append(
            ArtifactReadExpectation(
                item["normalized_path"], item["expected_artifact_record_id"],
                item["expected_record_sha256"],
                item["expected_blocker_code"],
                item["expected_occurrence_id"],
            )
        )
    return validate_reopen_plan(
        ReopenPlan(
            data["schema_version"], data["reopen_plan_id"],
            data["workflow_id"], data["source_revision"], data["target_scope"],
            data["target_owner_stage"], data["reason_code"],
            data["change_set_sha256"], tuple(entries), data["read_set_sha256"],
            data["plan_sha256"],
        )
    )


def blocked_no_reopen_disposition_from_dict(
    value: object,
) -> BlockedNoReopenDisposition:
    data = _require_keys(
        value,
        set(BlockedNoReopenDisposition.__dataclass_fields__),
        "blocked_no_reopen_disposition",
    )
    raw_read_set = data["read_set"]
    if type(raw_read_set) not in {list, tuple}:
        raise Phase3ContractError(
            "blocked-no-reopen read_set must be an array or tuple"
        )
    entries = []
    for raw in raw_read_set:
        item = _require_keys(
            raw,
            set(ArtifactReadExpectation.__dataclass_fields__),
            "blocked_no_reopen_read_set_entry",
        )
        entries.append(
            ArtifactReadExpectation(
                item["normalized_path"],
                item["expected_artifact_record_id"],
                item["expected_record_sha256"],
                item["expected_blocker_code"],
                item["expected_occurrence_id"],
            )
        )
    return validate_blocked_no_reopen_disposition(
        BlockedNoReopenDisposition(
            data["schema_version"],
            data["disposition_id"],
            data["workflow_id"],
            data["source_revision"],
            data["target_scope"],
            data["target_owner_stage"],
            data["reason_code"],
            data["change_set_sha256"],
            tuple(entries),
            data["read_set_sha256"],
            data["disposition_sha256"],
        )
    )


def phase3_previous_head_from_dict(value: object) -> Phase3PreviousHead:
    data = _require_keys(
        value,
        set(Phase3PreviousHead.__dataclass_fields__),
        "phase3_previous_head",
    )
    if type(data["kind"]) is not str:
        raise Phase3ContractError("Phase-3 previous head kind is invalid")
    try:
        kind = Phase3PreviousHeadKind(data["kind"])
    except (TypeError, ValueError) as exc:
        raise Phase3ContractError("Phase-3 previous head kind is invalid") from exc
    return validate_phase3_previous_head(
        Phase3PreviousHead(
            data["schema_version"],
            kind,
            data["workflow_id"],
            data["source_revision"],
            data["previous_manifest_sha256"],
            data["previous_revision"],
            data["previous_command_id"],
            data["previous_mutation_sha256"],
            data["head_sha256"],
        )
    )


def phase3_mutation_from_dict(value: object) -> Phase3Mutation:
    data = _require_keys(value, set(Phase3Mutation.__dataclass_fields__), "phase3_mutation")
    collection_fields = (
        "artifact_records",
        "artifact_blockers",
        "removals",
        "checkpoint_entries",
    )
    if any(type(data[field]) not in {list, tuple} for field in collection_fields):
        raise Phase3ContractError("Phase-3 mutation collections must be arrays or tuples")
    mutation = Phase3Mutation(
        data["schema_version"],
        tuple(artifact_record_from_dict(item) for item in data["artifact_records"]),
        tuple(checkpoint_entry_from_dict(item) for item in data["checkpoint_entries"]),
        (
            reopen_plan_from_dict(data["reopen_plan"])
            if data["reopen_plan"] is not None
            else None
        ),
        data["mutation_sha256"],
        (
            artifact_manifest_from_dict(data["previous_manifest"])
            if data["previous_manifest"] is not None
            else None
        ),
        (
            artifact_manifest_from_dict(data["current_manifest"])
            if data["current_manifest"] is not None
            else None
        ),
        (
            change_set_from_dict(data["change_set"])
            if data["change_set"] is not None
            else None
        ),
        tuple(artifact_blocker_from_dict(item) for item in data["artifact_blockers"]),
        tuple(artifact_removal_from_dict(item) for item in data["removals"]),
        (
            blocked_no_reopen_disposition_from_dict(data["blocked_disposition"])
            if data["blocked_disposition"] is not None
            else None
        ),
        (
            phase3_previous_head_from_dict(data["previous_head"])
            if data["previous_head"] is not None
            else None
        ),
    )
    return validate_phase3_mutation(mutation)


def artifact_occurrence_from_dict(value: object) -> ArtifactLedgerOccurrence:
    data = _require_keys(
        value, set(ArtifactLedgerOccurrence.__dataclass_fields__), "artifact_occurrence"
    )
    if type(data["kind"]) is not str:
        raise Phase3ContractError("artifact occurrence kind is invalid")
    try:
        kind = ArtifactOccurrenceKind(data["kind"])
    except (TypeError, ValueError) as exc:
        raise Phase3ContractError("artifact occurrence kind is invalid") from exc
    return validate_artifact_occurrence(
        ArtifactLedgerOccurrence(
            data["schema_version"],
            data["occurrence_id"],
            data["workflow_id"],
            data["revision"],
            data["command_id"],
            data["mutation_sha256"],
            kind,
            data["normalized_path"],
            data["semantic_sha256"],
            (
                artifact_record_from_dict(data["artifact_record"])
                if data["artifact_record"] is not None
                else None
            ),
            (
                artifact_blocker_from_dict(data["blocker"])
                if data["blocker"] is not None
                else None
            ),
            (
                artifact_removal_from_dict(data["removal"])
                if data["removal"] is not None
                else None
            ),
        )
    )


def checkpoint_occurrence_from_dict(value: object) -> CheckpointLedgerOccurrence:
    data = _require_keys(
        value,
        set(CheckpointLedgerOccurrence.__dataclass_fields__),
        "checkpoint_occurrence",
    )
    return validate_checkpoint_occurrence(
        CheckpointLedgerOccurrence(
            data["schema_version"],
            data["occurrence_id"],
            data["workflow_id"],
            data["revision"],
            data["command_id"],
            data["mutation_sha256"],
            checkpoint_entry_from_dict(data["checkpoint_entry"]),
        )
    )


__all__ = (
    "ARTIFACT_REGISTRATION_SCHEMA",
    "ARTIFACT_RECORD_SCHEMA",
    "ARTIFACT_OWNER_COMPILATION_BINDING_SCHEMA",
    "ARTIFACT_OWNER_OPERATOR_CLAIM_SCHEMA",
    "ARTIFACT_OWNER_OPERATOR_ISSUER_KIND",
    "ARTIFACT_MANIFEST_SCHEMA",
    "ARTIFACT_REMOVAL_SCHEMA",
    "CHANGE_SET_SCHEMA",
    "REOPEN_PLAN_SCHEMA",
    "CHECKPOINT_ENTRY_SCHEMA",
    "CHECKPOINT_REATTESTATION_SCHEMA",
    "PARITY_RECEIPT_SCHEMA",
    "PHASE3_MUTATION_SCHEMA",
    "ARTIFACT_OCCURRENCE_SCHEMA",
    "CHECKPOINT_OCCURRENCE_SCHEMA",
    "Phase3ContractError",
    "ArtifactRegistrationError",
    "OwnerPolicyMigrationRequired",
    "ArtifactAvailability",
    "ArtifactBlockerCode",
    "ArtifactChangeKind",
    "ArtifactOccurrenceKind",
    "Phase3PreviousHeadKind",
    "DirtyDisposition",
    "OwnerPolicyDisposition",
    "CheckpointState",
    "CheckpointTransition",
    "ReattestationClassification",
    "ParityClassification",
    "ARTIFACT_OWNER_OPERATOR_AUTHORIZATION_SCHEMA",
    "BLOCKED_NO_REOPEN_DISPOSITION_SCHEMA",
    "PHASE3_PREVIOUS_HEAD_SCHEMA",
    "ArtifactRegistration",
    "ArtifactRecord",
    "ArtifactOwnerCompilationBinding",
    "ArtifactOwnerOperatorClaim",
    "ArtifactOwnerOperatorAuthorization",
    "ArtifactBlocker",
    "ArtifactManifest",
    "ArtifactChange",
    "ArtifactRemoval",
    "DirtyDecision",
    "ChangeSet",
    "ArtifactReadExpectation",
    "ReopenPlan",
    "BlockedNoReopenDisposition",
    "CheckpointLedgerEntry",
    "CheckpointReattestationReceipt",
    "ParityReceipt",
    "Phase3Mutation",
    "Phase3PreviousHead",
    "ArtifactLedgerOccurrence",
    "CheckpointLedgerOccurrence",
    "normalize_artifact_path",
    "owner_compilation_semantic_sha256",
    "build_artifact_owner_compilation_binding",
    "validate_artifact_owner_compilation_binding",
    "owner_compilation_from_binding",
    "register_artifact_owner",
    "validate_artifact_registration",
    "build_artifact_owner_operator_claim",
    "validate_artifact_owner_operator_claim",
    "artifact_owner_operator_claim_from_authorization",
    "build_artifact_owner_operator_authorization",
    "build_artifact_record",
    "validate_artifact_record",
    "validate_artifact_owner_operator_authorization",
    "validate_artifact_blocker",
    "build_artifact_manifest",
    "capture_artifact_manifest",
    "validate_artifact_manifest",
    "build_artifact_removal",
    "validate_artifact_removal",
    "classify_owner_policy",
    "compute_change_set",
    "validate_change_set",
    "build_reopen_plan",
    "validate_reopen_plan",
    "build_blocked_no_reopen_disposition",
    "validate_blocked_no_reopen_disposition",
    "build_checkpoint_entry",
    "validate_checkpoint_entry",
    "dry_run_checkpoint_reattestation",
    "validate_checkpoint_reattestation",
    "build_parity_receipt",
    "validate_parity_receipt",
    "build_phase3_previous_head_bootstrap",
    "build_phase3_previous_head_continuation",
    "validate_phase3_previous_head",
    "build_phase3_mutation",
    "validate_phase3_mutation",
    "artifact_blocker_semantic_sha256",
    "build_artifact_occurrence",
    "validate_artifact_occurrence",
    "build_checkpoint_occurrence",
    "validate_checkpoint_occurrence",
    "artifact_registration_from_dict",
    "artifact_record_from_dict",
    "artifact_owner_compilation_binding_from_dict",
    "artifact_owner_operator_claim_from_dict",
    "artifact_owner_operator_authorization_from_dict",
    "artifact_blocker_from_dict",
    "artifact_manifest_from_dict",
    "artifact_removal_from_dict",
    "change_set_from_dict",
    "checkpoint_entry_from_dict",
    "reopen_plan_from_dict",
    "blocked_no_reopen_disposition_from_dict",
    "phase3_previous_head_from_dict",
    "phase3_mutation_from_dict",
    "artifact_occurrence_from_dict",
    "checkpoint_occurrence_from_dict",
)
