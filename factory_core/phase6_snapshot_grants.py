"""Verified Phase-6 snapshot and scoped-grant full-shadow store.

This module is deliberately isolated from the Authority repository, Web ACLs,
providers, processes and outbox implementations.  Its inputs are immutable
wire values supplied by callers and its only durable effect is an explicit,
caller-selected Phase-6 SQLite database.  Every public result is
non-authoritative and records that neither authority transfer nor dispatch
occurred.

The store uses a descriptor-anchored ownership protocol.  New files are
created with ``O_EXCL|O_NOFOLLOW``; existing files are verified through an
immutable read-only descriptor before an anchored read/write connection is
allowed.  Exact schema and canonical row verification is repeated before each
transaction so malformed or tampered state fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import ctypes
from enum import Enum
import errno
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
from typing import Iterable, Mapping

from .canonical import canonical_bytes, canonical_sha256
from .fd_ownership import (
    OwnedDescriptor,
    RetryableCleanup,
    close_raw_descriptor_if_unowned,
    resilient_unlink_at,
    run_cleanup,
)


PHASE6_SNAPSHOT_GRANTS_DEFAULT_ENABLED = False
PHASE6_STORE_SCHEMA = "phase6-verified-snapshot-grants-store-v1"
PHASE6_SOURCE_BINDING_SCHEMA = "phase6-authority-source-binding-v1"
PHASE6_SNAPSHOT_SCHEMA = "phase6-authority-bound-verified-shadow-snapshot-v1"
PHASE6_GRANT_SCHEMA = "phase6-shadow-scoped-grant-v1"
PHASE6_LIFECYCLE_SCHEMA = "phase6-shadow-grant-lifecycle-receipt-v1"
PHASE6_EVALUATION_SCHEMA = "phase6-shadow-grant-evaluation-receipt-v1"
PHASE6_ACCESS_PROOF_SCHEMA = "phase6-shadow-access-proof-v1"
PHASE6_RUN_SCHEMA = "phase6-snapshot-grants-full-shadow-run-v1"
PHASE6_WEB_SNAPSHOT_SCHEMA = "phase6-project-snapshot-web-v1"

_AUTHORITY_COORDINATE_SCHEMA = "authority-workflow-coordinate-v1"
_SOURCE_SNAPSHOT_COORDINATE_SCHEMA = "snapshot-coordinate-v0"
_SOURCE_COMPLETENESS = frozenset({"COMPLETE", "PARTIAL"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SQLITE_HEADER = b"SQLite format 3\x00"
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_INTERNAL_KEY_PREFIX = "phase6-internal@"
_CALLER_KEY_MAX = 256
_MAX_LOGICAL_INTEGER = 2**63 - 1
_CONNECTION_OWNER = "phase6-sqlite-connection"
_RENAME_NOREPLACE = 1

LOGGER = logging.getLogger(__name__)


def _deadline_check(deadline: object | None, stage: str) -> None:
    if deadline is None:
        return
    check = getattr(deadline, "check", None)
    if not callable(check):
        raise Phase6ContractError("deadline must expose check()")
    check(stage)


def _deadline_remaining(
    deadline: object | None, maximum_seconds: float, stage: str
) -> float:
    _deadline_check(deadline, stage)
    if deadline is None:
        return maximum_seconds
    remaining = getattr(deadline, "remaining_seconds", None)
    if not callable(remaining):
        raise Phase6ContractError("deadline must expose remaining_seconds()")
    value = remaining()
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value <= 0
    ):
        _deadline_check(deadline, f"{stage}_exhausted")
        raise Phase6StoreError("Phase-6 deadline has no remaining budget")
    return min(maximum_seconds, float(value))


def _rename_noreplace(
    source_parent_fd: int,
    source_name: str,
    target_parent_fd: int,
    target_name: str,
) -> None:
    """Atomically capture one directory entry without replacing another."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(
        source_parent_fd,
        os.fsencode(source_name),
        target_parent_fd,
        os.fsencode(target_name),
        _RENAME_NOREPLACE,
    ) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), source_name)


class Phase6Error(RuntimeError):
    """Base error for the Phase-6 full-shadow boundary."""


class Phase6ContractError(Phase6Error):
    """A caller supplied a value outside the frozen Phase-6 contract."""


class Phase6StoreError(Phase6Error):
    """The standalone Phase-6 store could not prove its identity/integrity."""


class Phase6IdempotencyConflict(Phase6Error):
    """An idempotency key is already bound to different canonical bytes."""


class Phase6SnapshotConflict(Phase6Error):
    """A snapshot revision, predecessor or chain identity conflicts."""


class Phase6GrantConflict(Phase6Error):
    """A grant lineage or lifecycle request conflicts."""


class Phase6SnapshotNotFound(Phase6Error):
    """No current verified snapshot exists for the requested project."""

    code = "PHASE6_SNAPSHOT_NOT_FOUND"


class Phase6SourceIneligible(Phase6Error):
    """The current snapshot is evidence-only and cannot drive a ready view."""

    code = "PHASE6_SOURCE_INELIGIBLE"


class Phase6SnapshotStale(Phase6Error):
    """The requested revision is not the current verified revision."""

    code = "PHASE6_SNAPSHOT_STALE"

    def __init__(self, message: str, *, server_revision: int) -> None:
        super().__init__(message)
        self.server_revision = _nonnegative(server_revision, "server_revision")


class SectionAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    EMPTY = "EMPTY"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"
    REDACTED = "REDACTED"


class GrantScope(str, Enum):
    SNAPSHOT_VIEW = "snapshot:view"
    SECTION_VIEW = "section:view"
    ACTION_CENTER_VIEW = "action-center:view"


class GrantStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class EvaluationDecision(str, Enum):
    ALLOWED_SHADOW = "ALLOWED_SHADOW"
    DENIED_SOURCE_INELIGIBLE = "DENIED_SOURCE_INELIGIBLE"
    DENIED_SNAPSHOT_STALE = "DENIED_SNAPSHOT_STALE"
    DENIED_SNAPSHOT_EXPIRED = "DENIED_SNAPSHOT_EXPIRED"
    DENIED_SUBJECT = "DENIED_SUBJECT"
    DENIED_SCOPE = "DENIED_SCOPE"
    DENIED_NOT_YET_VALID = "DENIED_NOT_YET_VALID"
    DENIED_GRANT_EXPIRED = "DENIED_GRANT_EXPIRED"
    DENIED_REVOKED = "DENIED_REVOKED"
    DENIED_TAMPER_OR_INCONSISTENT = "DENIED_TAMPER_OR_INCONSISTENT"


def _plain_text(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise Phase6ContractError(f"{field} must be a non-empty plain string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise Phase6ContractError(f"{field} must contain valid UTF-8") from exc
    return value


def _identifier(value: object, field: str) -> str:
    text = _plain_text(value, field)
    if _IDENTIFIER.fullmatch(text) is None:
        raise Phase6ContractError(f"{field} must be a canonical identifier")
    return text


def _sha(value: object, field: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise Phase6ContractError(f"{field} must be a lowercase SHA-256")
    return value


def _nonnegative(value: object, field: str) -> int:
    if type(value) is not int or value < 0 or value > _MAX_LOGICAL_INTEGER:
        raise Phase6ContractError(
            f"{field} must be a nonnegative signed 64-bit integer"
        )
    return value


def _positive(value: object, field: str) -> int:
    result = _nonnegative(value, field)
    if result == 0:
        raise Phase6ContractError(f"{field} must be positive")
    return result


def _caller_key(value: object, field: str = "idempotency_key") -> str:
    text = _plain_text(value, field)
    if len(text) > _CALLER_KEY_MAX:
        raise Phase6ContractError(f"{field} exceeds {_CALLER_KEY_MAX} characters")
    if text.startswith(_INTERNAL_KEY_PREFIX):
        raise Phase6ContractError(f"{field} uses the reserved Phase-6 namespace")
    return text


def _canonical_json(value: object) -> str:
    return canonical_bytes(value).decode("utf-8")


def _decode_canonical(value: object, digest: object, field: str) -> object:
    if type(value) is not str or type(digest) is not str:
        raise Phase6StoreError(f"{field} persisted canonical identity is malformed")
    try:
        raw = value.encode("utf-8", errors="strict")
        decoded = json.loads(value)
    except (UnicodeEncodeError, json.JSONDecodeError) as exc:
        raise Phase6StoreError(f"{field} persisted JSON is malformed") from exc
    if canonical_bytes(decoded) != raw or canonical_sha256(decoded) != digest:
        raise Phase6StoreError(f"{field} canonical bytes or SHA-256 differ")
    return decoded


def _decode_exact_json(value: object, field: str) -> object:
    """Decode canonical JSON whose domain identity excludes its digest field."""

    if type(value) is not str:
        raise Phase6StoreError(f"{field} persisted JSON is malformed")
    try:
        raw = value.encode("utf-8", errors="strict")
        decoded = json.loads(value)
    except (UnicodeEncodeError, json.JSONDecodeError) as exc:
        raise Phase6StoreError(f"{field} persisted JSON is malformed") from exc
    if canonical_bytes(decoded) != raw:
        raise Phase6StoreError(f"{field} persisted JSON is not canonical")
    return decoded


def _false_safety(value: Mapping[str, object], field: str) -> None:
    for name in ("authoritative", "authority_transferred", "dispatch_performed"):
        if value.get(name) is not False:
            raise Phase6StoreError(f"{field} claims authority or dispatch")


def _internal_key(*, base_key: str, object_id: str, stage: str) -> str:
    return f"{_INTERNAL_KEY_PREFIX}{canonical_sha256({
        'schema_version': 'phase6-internal-stage-key-v1',
        'base_key': _caller_key(base_key),
        'object_id': _sha(object_id, 'object_id'),
        'stage': _identifier(stage, 'stage'),
    })}"


_COORDINATE_KEYS = frozenset(
    {
        "schema",
        "workflow_id",
        "project_id",
        "project_generation",
        "run_generation",
        "runtime_generation",
        "scheduler_generation",
        "current_revision",
        "contract_pin_set_sha256",
        "authority_state",
        "source_fence_sha256",
        "switch_mode",
        "switch_epoch",
    }
)


def _validated_authority_coordinate(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _COORDINATE_KEYS:
        raise Phase6ContractError("authority_coordinate must contain the exact v1 keys")
    result = dict(value)
    if result["schema"] != _AUTHORITY_COORDINATE_SCHEMA:
        raise Phase6ContractError("authority coordinate schema is unsupported")
    for name in (
        "workflow_id",
        "project_id",
        "project_generation",
        "run_generation",
        "runtime_generation",
        "scheduler_generation",
        "authority_state",
        "switch_mode",
    ):
        _identifier(result[name], f"authority_coordinate.{name}")
        if name.endswith("generation") and result[name] == "legacy_unknown":
            raise Phase6ContractError("legacy_unknown generations are ineligible")
    _nonnegative(result["current_revision"], "authority_coordinate.current_revision")
    _nonnegative(result["switch_epoch"], "authority_coordinate.switch_epoch")
    _sha(result["source_fence_sha256"], "authority_coordinate.source_fence_sha256")
    if result["contract_pin_set_sha256"] is not None:
        _sha(
            result["contract_pin_set_sha256"],
            "authority_coordinate.contract_pin_set_sha256",
        )
    return result


_SOURCE_COORDINATE_KEYS = frozenset(
    {
        "schema_version",
        "project_id",
        "workflow_schema_version",
        "project_revision",
        "project_generation",
        "run_generation",
        "runtime_generation",
        "scheduler_generation",
        "recorded_contract_pin_set_sha256",
    }
)


def _validated_source_coordinate(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _SOURCE_COORDINATE_KEYS:
        raise Phase6ContractError("source_snapshot_coordinate must contain exact v0 keys")
    result = dict(value)
    if result["schema_version"] != _SOURCE_SNAPSHOT_COORDINATE_SCHEMA:
        raise Phase6ContractError("source snapshot coordinate schema is unsupported")
    _identifier(result["project_id"], "source_snapshot_coordinate.project_id")
    for field in ("workflow_schema_version", "project_revision"):
        _nonnegative(result[field], f"source_snapshot_coordinate.{field}")
    for field in (
        "project_generation",
        "run_generation",
        "runtime_generation",
        "scheduler_generation",
    ):
        if result[field] is None:
            if field not in {"project_generation", "run_generation"}:
                raise Phase6ContractError(f"source_snapshot_coordinate.{field} is required")
        else:
            _identifier(result[field], f"source_snapshot_coordinate.{field}")
    if result["recorded_contract_pin_set_sha256"] is not None:
        _sha(
            result["recorded_contract_pin_set_sha256"],
            "source_snapshot_coordinate.recorded_contract_pin_set_sha256",
        )
    return result


@dataclass(frozen=True)
class AuthoritySourceBinding:
    authority_coordinate: dict[str, object]
    authority_coordinate_sha256: str
    authority_revision_snapshot_sha256: str
    source_snapshot_schema: str
    source_snapshot_semantic_sha256: str
    source_snapshot_completeness: str
    source_snapshot_coordinate: dict[str, object]
    source_snapshot_coordinate_sha256: str
    phase3_artifact_state_sha256: str
    phase4_operation_state_sha256: str
    phase5_supervisor_state_sha256: str
    binding_sha256: str

    @property
    def eligible(self) -> bool:
        return (
            self.source_snapshot_completeness == "COMPLETE"
            and self.authority_coordinate["contract_pin_set_sha256"] is not None
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PHASE6_SOURCE_BINDING_SCHEMA,
            "authority_coordinate": dict(self.authority_coordinate),
            "authority_coordinate_sha256": self.authority_coordinate_sha256,
            "authority_revision_snapshot_sha256": self.authority_revision_snapshot_sha256,
            "source_snapshot_schema": self.source_snapshot_schema,
            "source_snapshot_semantic_sha256": self.source_snapshot_semantic_sha256,
            "source_snapshot_completeness": self.source_snapshot_completeness,
            "source_snapshot_coordinate": dict(self.source_snapshot_coordinate),
            "source_snapshot_coordinate_sha256": self.source_snapshot_coordinate_sha256,
            "phase3_artifact_state_sha256": self.phase3_artifact_state_sha256,
            "phase4_operation_state_sha256": self.phase4_operation_state_sha256,
            "phase5_supervisor_state_sha256": self.phase5_supervisor_state_sha256,
            "binding_sha256": self.binding_sha256,
        }


def build_authority_source_binding(
    *,
    authority_coordinate: dict[str, object],
    authority_coordinate_sha256: str,
    authority_revision_snapshot_sha256: str,
    authority_revision_through_revision: int,
    source_snapshot_schema: str,
    source_snapshot_semantic_sha256: str,
    source_snapshot_completeness: str,
    source_snapshot_coordinate: dict[str, object],
    phase3_artifact_state_sha256: str,
    phase4_operation_state_sha256: str,
    phase5_supervisor_state_sha256: str,
) -> AuthoritySourceBinding:
    """Validate and bind exact Phase3/4/5 and source-Authority identities."""

    coordinate = _validated_authority_coordinate(authority_coordinate)
    coordinate_sha = _sha(authority_coordinate_sha256, "authority_coordinate_sha256")
    if canonical_sha256(coordinate) != coordinate_sha:
        raise Phase6ContractError("authority coordinate SHA-256 differs")
    through = _nonnegative(
        authority_revision_through_revision,
        "authority_revision_through_revision",
    )
    if through != coordinate["current_revision"]:
        raise Phase6ContractError("Authority revision snapshot is cropped or ahead")
    source_coordinate = _validated_source_coordinate(source_snapshot_coordinate)
    source_coordinate_sha = canonical_sha256(source_coordinate)
    comparisons = {
        "project_id": "project_id",
        "project_generation": "project_generation",
        "run_generation": "run_generation",
        "runtime_generation": "runtime_generation",
        "scheduler_generation": "scheduler_generation",
        "current_revision": "project_revision",
        "contract_pin_set_sha256": "recorded_contract_pin_set_sha256",
    }
    for authority_field, source_field in comparisons.items():
        if coordinate[authority_field] != source_coordinate[source_field]:
            raise Phase6ContractError(
                f"source snapshot {source_field} differs from Authority coordinate"
            )
    schema = _identifier(source_snapshot_schema, "source_snapshot_schema")
    completeness = _plain_text(source_snapshot_completeness, "source_snapshot_completeness")
    if completeness not in _SOURCE_COMPLETENESS:
        raise Phase6ContractError("source snapshot completeness is unsupported")
    body: dict[str, object] = {
        "schema_version": PHASE6_SOURCE_BINDING_SCHEMA,
        "authority_coordinate": coordinate,
        "authority_coordinate_sha256": coordinate_sha,
        "authority_revision_snapshot_sha256": _sha(
            authority_revision_snapshot_sha256,
            "authority_revision_snapshot_sha256",
        ),
        "source_snapshot_schema": schema,
        "source_snapshot_semantic_sha256": _sha(
            source_snapshot_semantic_sha256,
            "source_snapshot_semantic_sha256",
        ),
        "source_snapshot_completeness": completeness,
        "source_snapshot_coordinate": source_coordinate,
        "source_snapshot_coordinate_sha256": source_coordinate_sha,
        "phase3_artifact_state_sha256": _sha(
            phase3_artifact_state_sha256, "phase3_artifact_state_sha256"
        ),
        "phase4_operation_state_sha256": _sha(
            phase4_operation_state_sha256, "phase4_operation_state_sha256"
        ),
        "phase5_supervisor_state_sha256": _sha(
            phase5_supervisor_state_sha256, "phase5_supervisor_state_sha256"
        ),
    }
    digest = canonical_sha256(body)
    return AuthoritySourceBinding(
        coordinate,
        coordinate_sha,
        body["authority_revision_snapshot_sha256"],
        schema,
        body["source_snapshot_semantic_sha256"],
        completeness,
        source_coordinate,
        source_coordinate_sha,
        body["phase3_artifact_state_sha256"],
        body["phase4_operation_state_sha256"],
        body["phase5_supervisor_state_sha256"],
        digest,
    )


def _source_binding_from_dict(value: object) -> AuthoritySourceBinding:
    if type(value) is not dict:
        raise Phase6StoreError("source binding is malformed")
    expected = {
        "schema_version",
        "authority_coordinate",
        "authority_coordinate_sha256",
        "authority_revision_snapshot_sha256",
        "source_snapshot_schema",
        "source_snapshot_semantic_sha256",
        "source_snapshot_completeness",
        "source_snapshot_coordinate",
        "source_snapshot_coordinate_sha256",
        "phase3_artifact_state_sha256",
        "phase4_operation_state_sha256",
        "phase5_supervisor_state_sha256",
        "binding_sha256",
    }
    if set(value) != expected or value.get("schema_version") != PHASE6_SOURCE_BINDING_SCHEMA:
        raise Phase6StoreError("source binding keys or schema differ")
    try:
        result = build_authority_source_binding(
            authority_coordinate=value["authority_coordinate"],
            authority_coordinate_sha256=value["authority_coordinate_sha256"],
            authority_revision_snapshot_sha256=value["authority_revision_snapshot_sha256"],
            authority_revision_through_revision=value["authority_coordinate"]["current_revision"],
            source_snapshot_schema=value["source_snapshot_schema"],
            source_snapshot_semantic_sha256=value["source_snapshot_semantic_sha256"],
            source_snapshot_completeness=value["source_snapshot_completeness"],
            source_snapshot_coordinate=value["source_snapshot_coordinate"],
            phase3_artifact_state_sha256=value["phase3_artifact_state_sha256"],
            phase4_operation_state_sha256=value["phase4_operation_state_sha256"],
            phase5_supervisor_state_sha256=value["phase5_supervisor_state_sha256"],
        )
    except (KeyError, TypeError, Phase6ContractError) as exc:
        raise Phase6StoreError("source binding does not revalidate") from exc
    if result.binding_sha256 != value["binding_sha256"]:
        raise Phase6StoreError("source binding SHA-256 differs")
    return result


@dataclass(frozen=True)
class VerifiedSection:
    section_id: str
    availability: SectionAvailability
    content_sha256: str
    source_section_schema: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.section_id, "section_id")
        if type(self.availability) is not SectionAvailability:
            raise Phase6ContractError("availability must be SectionAvailability")
        _sha(self.content_sha256, "content_sha256")
        if self.source_section_schema is not None:
            _identifier(self.source_section_schema, "source_section_schema")

    def as_dict(self) -> dict[str, object]:
        return {
            "section_id": self.section_id,
            "availability": self.availability.value,
            "content_sha256": self.content_sha256,
            "source_section_schema": self.source_section_schema,
        }


def _sections(values: Iterable[VerifiedSection]) -> tuple[VerifiedSection, ...]:
    if isinstance(values, (str, bytes, dict)):
        raise Phase6ContractError("sections must be an iterable of VerifiedSection")
    result = tuple(values)
    if any(type(item) is not VerifiedSection for item in result):
        raise Phase6ContractError("sections must contain only VerifiedSection")
    keys = tuple(item.section_id for item in result)
    if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
        raise Phase6ContractError("sections must have unique key-sorted section IDs")
    return result


def _section_from_dict(value: object) -> VerifiedSection:
    if type(value) is not dict or set(value) != {
        "section_id", "availability", "content_sha256", "source_section_schema"
    }:
        raise Phase6StoreError("snapshot section is malformed")
    try:
        return VerifiedSection(
            value["section_id"],
            SectionAvailability(value["availability"]),
            value["content_sha256"],
            value["source_section_schema"],
        )
    except (TypeError, ValueError, Phase6ContractError) as exc:
        raise Phase6StoreError("snapshot section does not revalidate") from exc


@dataclass(frozen=True)
class VerifiedShadowSnapshot:
    source_binding: AuthoritySourceBinding
    workflow_id: str
    project_id: str
    authority_coordinate_sha256: str
    snapshot_sequence: int
    previous_snapshot_id: str | None
    captured_at: int
    valid_until: int
    sections: tuple[VerifiedSection, ...]
    snapshot_id: str
    authoritative: bool = False
    authority_transferred: bool = False
    dispatch_performed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PHASE6_SNAPSHOT_SCHEMA,
            "source_binding": self.source_binding.as_dict(),
            "source_binding_sha256": self.source_binding.binding_sha256,
            "workflow_id": self.workflow_id,
            "project_id": self.project_id,
            "authority_coordinate_sha256": self.authority_coordinate_sha256,
            "snapshot_sequence": self.snapshot_sequence,
            "previous_snapshot_id": self.previous_snapshot_id,
            "captured_at": self.captured_at,
            "valid_until": self.valid_until,
            "sections": [item.as_dict() for item in self.sections],
            "authoritative": self.authoritative,
            "authority_transferred": self.authority_transferred,
            "dispatch_performed": self.dispatch_performed,
            "snapshot_id": self.snapshot_id,
        }


def _build_snapshot(
    *,
    source_binding: AuthoritySourceBinding,
    snapshot_sequence: int,
    previous_snapshot_id: str | None,
    captured_at: int,
    valid_until: int,
    sections: Iterable[VerifiedSection],
) -> VerifiedShadowSnapshot:
    if type(source_binding) is not AuthoritySourceBinding:
        raise Phase6ContractError("source_binding must be AuthoritySourceBinding")
    try:
        source_binding = _source_binding_from_dict(source_binding.as_dict())
    except Phase6StoreError as exc:
        raise Phase6ContractError("source_binding no longer revalidates") from exc
    sequence = _positive(snapshot_sequence, "snapshot_sequence")
    if previous_snapshot_id is None:
        if sequence != 1:
            raise Phase6ContractError("only snapshot sequence 1 may lack a predecessor")
    else:
        _sha(previous_snapshot_id, "previous_snapshot_id")
        if sequence == 1:
            raise Phase6ContractError("snapshot sequence 1 cannot name a predecessor")
    captured = _nonnegative(captured_at, "captured_at")
    until = _positive(valid_until, "valid_until")
    if until <= captured:
        raise Phase6ContractError("snapshot validity interval must be non-empty")
    checked_sections = _sections(sections)
    coordinate = source_binding.authority_coordinate
    body: dict[str, object] = {
        "schema_version": PHASE6_SNAPSHOT_SCHEMA,
        "source_binding": source_binding.as_dict(),
        "source_binding_sha256": source_binding.binding_sha256,
        "workflow_id": coordinate["workflow_id"],
        "project_id": coordinate["project_id"],
        "authority_coordinate_sha256": source_binding.authority_coordinate_sha256,
        "snapshot_sequence": sequence,
        "previous_snapshot_id": previous_snapshot_id,
        "captured_at": captured,
        "valid_until": until,
        "sections": [item.as_dict() for item in checked_sections],
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }
    return VerifiedShadowSnapshot(
        source_binding,
        str(coordinate["workflow_id"]),
        str(coordinate["project_id"]),
        source_binding.authority_coordinate_sha256,
        sequence,
        previous_snapshot_id,
        captured,
        until,
        checked_sections,
        canonical_sha256(body),
    )


def _snapshot_from_dict(value: object) -> VerifiedShadowSnapshot:
    if type(value) is not dict:
        raise Phase6StoreError("snapshot is malformed")
    expected = {
        "schema_version", "source_binding", "source_binding_sha256", "workflow_id",
        "project_id", "authority_coordinate_sha256", "snapshot_sequence",
        "previous_snapshot_id", "captured_at", "valid_until", "sections",
        "authoritative", "authority_transferred", "dispatch_performed", "snapshot_id",
    }
    if set(value) != expected or value.get("schema_version") != PHASE6_SNAPSHOT_SCHEMA:
        raise Phase6StoreError("snapshot keys or schema differ")
    _false_safety(value, "snapshot")
    try:
        binding = _source_binding_from_dict(value["source_binding"])
        if value["source_binding_sha256"] != binding.binding_sha256:
            raise Phase6StoreError("snapshot source binding SHA-256 differs")
        raw_sections = value["sections"]
        if type(raw_sections) is not list:
            raise Phase6StoreError("snapshot sections are malformed")
        snapshot = _build_snapshot(
            source_binding=binding,
            snapshot_sequence=value["snapshot_sequence"],
            previous_snapshot_id=value["previous_snapshot_id"],
            captured_at=value["captured_at"],
            valid_until=value["valid_until"],
            sections=tuple(_section_from_dict(item) for item in raw_sections),
        )
    except (KeyError, TypeError, Phase6ContractError) as exc:
        if isinstance(exc, Phase6StoreError):
            raise
        raise Phase6StoreError("snapshot does not revalidate") from exc
    for field in ("workflow_id", "project_id", "authority_coordinate_sha256", "snapshot_id"):
        if value[field] != getattr(snapshot, field):
            raise Phase6StoreError(f"snapshot {field} differs")
    return snapshot


@dataclass(frozen=True)
class ShadowScopedGrant:
    grant_sequence: int
    previous_grant_id: str | None
    snapshot_id: str
    authority_coordinate_sha256: str
    workflow_id: str
    project_id: str
    subject_type: str
    subject_id: str
    subject_generation: str
    scope: GrantScope
    scope_key: str | None
    issuer_id: str
    issuer_generation: str
    issuer_evidence_schema: str
    issuer_receipt_sha256: str
    issued_at: int
    not_before: int
    expires_at: int
    grant_id: str
    authoritative: bool = False
    authority_transferred: bool = False
    dispatch_performed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PHASE6_GRANT_SCHEMA,
            "grant_sequence": self.grant_sequence,
            "previous_grant_id": self.previous_grant_id,
            "snapshot_id": self.snapshot_id,
            "authority_coordinate_sha256": self.authority_coordinate_sha256,
            "workflow_id": self.workflow_id,
            "project_id": self.project_id,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "subject_generation": self.subject_generation,
            "scope": self.scope.value,
            "scope_key": self.scope_key,
            "issuer_id": self.issuer_id,
            "issuer_generation": self.issuer_generation,
            "issuer_evidence_schema": self.issuer_evidence_schema,
            "issuer_receipt_sha256": self.issuer_receipt_sha256,
            "issued_at": self.issued_at,
            "not_before": self.not_before,
            "expires_at": self.expires_at,
            "authoritative": self.authoritative,
            "authority_transferred": self.authority_transferred,
            "dispatch_performed": self.dispatch_performed,
            "grant_id": self.grant_id,
        }


def _build_grant(
    *,
    snapshot: VerifiedShadowSnapshot,
    grant_sequence: int,
    previous_grant_id: str | None,
    subject_type: str,
    subject_id: str,
    subject_generation: str,
    scope: GrantScope,
    scope_key: str | None,
    issuer_id: str,
    issuer_generation: str,
    issuer_evidence_schema: str,
    issuer_receipt_sha256: str,
    issued_at: int,
    not_before: int,
    expires_at: int,
) -> ShadowScopedGrant:
    if type(snapshot) is not VerifiedShadowSnapshot:
        raise Phase6ContractError("snapshot must be VerifiedShadowSnapshot")
    sequence = _positive(grant_sequence, "grant_sequence")
    if previous_grant_id is None:
        if sequence != 1:
            raise Phase6ContractError("only grant sequence 1 may lack a predecessor")
    else:
        _sha(previous_grant_id, "previous_grant_id")
        if sequence == 1:
            raise Phase6ContractError("grant sequence 1 cannot name a predecessor")
    if type(scope) is not GrantScope:
        raise Phase6ContractError("scope must be one closed GrantScope member")
    if scope is GrantScope.SECTION_VIEW:
        key = _identifier(scope_key, "scope_key")
        available = {
            item.section_id
            for item in snapshot.sections
            if item.availability is SectionAvailability.AVAILABLE
        }
        if key not in available:
            raise Phase6ContractError("section:view requires an exact AVAILABLE section")
    elif scope_key is not None:
        raise Phase6ContractError("only section:view accepts a scope_key")
    else:
        key = None
    issued = _nonnegative(issued_at, "issued_at")
    start = _nonnegative(not_before, "not_before")
    end = _positive(expires_at, "expires_at")
    if start < issued or end <= start:
        raise Phase6ContractError("grant validity interval is invalid")
    if (
        issued < snapshot.captured_at
        or issued >= snapshot.valid_until
        or end > snapshot.valid_until
    ):
        raise Phase6ContractError(
            "grant validity must remain within its verified snapshot"
        )
    body: dict[str, object] = {
        "schema_version": PHASE6_GRANT_SCHEMA,
        "grant_sequence": sequence,
        "previous_grant_id": previous_grant_id,
        "snapshot_id": snapshot.snapshot_id,
        "authority_coordinate_sha256": snapshot.authority_coordinate_sha256,
        "workflow_id": snapshot.workflow_id,
        "project_id": snapshot.project_id,
        "subject_type": _identifier(subject_type, "subject_type"),
        "subject_id": _identifier(subject_id, "subject_id"),
        "subject_generation": _identifier(subject_generation, "subject_generation"),
        "scope": scope.value,
        "scope_key": key,
        "issuer_id": _identifier(issuer_id, "issuer_id"),
        "issuer_generation": _identifier(issuer_generation, "issuer_generation"),
        "issuer_evidence_schema": _identifier(
            issuer_evidence_schema, "issuer_evidence_schema"
        ),
        "issuer_receipt_sha256": _sha(
            issuer_receipt_sha256, "issuer_receipt_sha256"
        ),
        "issued_at": issued,
        "not_before": start,
        "expires_at": end,
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }
    return ShadowScopedGrant(
        sequence,
        previous_grant_id,
        snapshot.snapshot_id,
        snapshot.authority_coordinate_sha256,
        snapshot.workflow_id,
        snapshot.project_id,
        body["subject_type"],
        body["subject_id"],
        body["subject_generation"],
        scope,
        key,
        body["issuer_id"],
        body["issuer_generation"],
        body["issuer_evidence_schema"],
        body["issuer_receipt_sha256"],
        issued,
        start,
        end,
        canonical_sha256(body),
    )


def _grant_from_dict(value: object, snapshot: VerifiedShadowSnapshot) -> ShadowScopedGrant:
    if type(value) is not dict:
        raise Phase6StoreError("grant is malformed")
    expected = {
        "schema_version", "grant_sequence", "previous_grant_id", "snapshot_id",
        "authority_coordinate_sha256", "workflow_id", "project_id", "subject_type",
        "subject_id", "subject_generation", "scope", "scope_key", "issuer_id",
        "issuer_generation", "issuer_evidence_schema", "issuer_receipt_sha256",
        "issued_at", "not_before", "expires_at", "authoritative",
        "authority_transferred", "dispatch_performed", "grant_id",
    }
    if set(value) != expected or value.get("schema_version") != PHASE6_GRANT_SCHEMA:
        raise Phase6StoreError("grant keys or schema differ")
    _false_safety(value, "grant")
    try:
        grant = _build_grant(
            snapshot=snapshot,
            grant_sequence=value["grant_sequence"],
            previous_grant_id=value["previous_grant_id"],
            subject_type=value["subject_type"],
            subject_id=value["subject_id"],
            subject_generation=value["subject_generation"],
            scope=GrantScope(value["scope"]),
            scope_key=value["scope_key"],
            issuer_id=value["issuer_id"],
            issuer_generation=value["issuer_generation"],
            issuer_evidence_schema=value["issuer_evidence_schema"],
            issuer_receipt_sha256=value["issuer_receipt_sha256"],
            issued_at=value["issued_at"],
            not_before=value["not_before"],
            expires_at=value["expires_at"],
        )
    except (KeyError, TypeError, ValueError, Phase6ContractError) as exc:
        raise Phase6StoreError("grant does not revalidate") from exc
    for field in (
        "snapshot_id", "authority_coordinate_sha256", "workflow_id", "project_id", "grant_id"
    ):
        if value[field] != getattr(grant, field):
            raise Phase6StoreError(f"grant {field} differs")
    return grant


@dataclass(frozen=True)
class GrantLifecycleReceipt:
    receipt_sequence: int
    previous_receipt_sha256: str | None
    grant_id: str
    grant_sequence: int
    snapshot_id: str
    authority_coordinate_sha256: str
    event: str
    before_status: str | None
    after_status: GrantStatus
    actor_id: str
    actor_generation: str
    reason_code: str
    effective_at: int
    request_schema: str
    request_sha256: str
    receipt_sha256: str
    authoritative: bool = False
    authority_transferred: bool = False
    dispatch_performed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PHASE6_LIFECYCLE_SCHEMA,
            "receipt_sequence": self.receipt_sequence,
            "previous_receipt_sha256": self.previous_receipt_sha256,
            "grant_id": self.grant_id,
            "grant_sequence": self.grant_sequence,
            "snapshot_id": self.snapshot_id,
            "authority_coordinate_sha256": self.authority_coordinate_sha256,
            "event": self.event,
            "before_status": self.before_status,
            "after_status": self.after_status.value,
            "actor_id": self.actor_id,
            "actor_generation": self.actor_generation,
            "reason_code": self.reason_code,
            "effective_at": self.effective_at,
            "request_schema": self.request_schema,
            "request_sha256": self.request_sha256,
            "authoritative": self.authoritative,
            "authority_transferred": self.authority_transferred,
            "dispatch_performed": self.dispatch_performed,
            "receipt_sha256": self.receipt_sha256,
        }


def _build_lifecycle(
    *,
    grant: ShadowScopedGrant,
    receipt_sequence: int,
    previous_receipt_sha256: str | None,
    event: str,
    before_status: GrantStatus | None,
    after_status: GrantStatus,
    actor_id: str,
    actor_generation: str,
    reason_code: str,
    effective_at: int,
    request_schema: str,
    request_sha256: str,
) -> GrantLifecycleReceipt:
    sequence = _positive(receipt_sequence, "receipt_sequence")
    if previous_receipt_sha256 is None:
        if sequence != 1 or event != "issued" or before_status is not None:
            raise Phase6ContractError("initial lifecycle receipt is malformed")
    else:
        _sha(previous_receipt_sha256, "previous_receipt_sha256")
        if sequence == 1:
            raise Phase6ContractError("later lifecycle receipt lacks a sequence")
    if event not in {"issued", "expired", "revoked"}:
        raise Phase6ContractError("lifecycle event is unsupported")
    if type(after_status) is not GrantStatus:
        raise Phase6ContractError("after_status must be GrantStatus")
    if before_status is not None and type(before_status) is not GrantStatus:
        raise Phase6ContractError("before_status must be GrantStatus or None")
    valid = (
        event == "issued" and before_status is None and after_status is GrantStatus.ACTIVE
    ) or (
        event == "expired" and before_status is GrantStatus.ACTIVE
        and after_status is GrantStatus.EXPIRED
    ) or (
        event == "revoked" and before_status in {GrantStatus.ACTIVE, GrantStatus.EXPIRED}
        and after_status is GrantStatus.REVOKED
    )
    if not valid:
        raise Phase6ContractError("lifecycle transition is invalid")
    body: dict[str, object] = {
        "schema_version": PHASE6_LIFECYCLE_SCHEMA,
        "receipt_sequence": sequence,
        "previous_receipt_sha256": previous_receipt_sha256,
        "grant_id": grant.grant_id,
        "grant_sequence": grant.grant_sequence,
        "snapshot_id": grant.snapshot_id,
        "authority_coordinate_sha256": grant.authority_coordinate_sha256,
        "event": event,
        "before_status": None if before_status is None else before_status.value,
        "after_status": after_status.value,
        "actor_id": _identifier(actor_id, "actor_id"),
        "actor_generation": _identifier(actor_generation, "actor_generation"),
        "reason_code": _identifier(reason_code, "reason_code"),
        "effective_at": _nonnegative(effective_at, "effective_at"),
        "request_schema": _identifier(request_schema, "request_schema"),
        "request_sha256": _sha(request_sha256, "request_sha256"),
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }
    return GrantLifecycleReceipt(
        sequence,
        previous_receipt_sha256,
        grant.grant_id,
        grant.grant_sequence,
        grant.snapshot_id,
        grant.authority_coordinate_sha256,
        event,
        None if before_status is None else before_status.value,
        after_status,
        body["actor_id"],
        body["actor_generation"],
        body["reason_code"],
        body["effective_at"],
        body["request_schema"],
        body["request_sha256"],
        canonical_sha256(body),
    )


@dataclass(frozen=True)
class GrantEvaluationReceipt:
    grant_id: str
    grant_sequence: int
    snapshot_id: str
    authority_coordinate_sha256: str
    observed_lifecycle_receipt_sha256: str
    observed_snapshot_head_id: str
    subject_type: str
    subject_id: str
    subject_generation: str
    requested_scope: GrantScope
    requested_scope_key: str | None
    evaluated_at: int
    decision: EvaluationDecision
    reason_code: str
    evaluation_id: str
    authoritative: bool = False
    authority_transferred: bool = False
    dispatch_performed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PHASE6_EVALUATION_SCHEMA,
            "evaluation_id": self.evaluation_id,
            "evaluation_sha256": self.evaluation_id,
            "grant_id": self.grant_id,
            "grant_sequence": self.grant_sequence,
            "snapshot_id": self.snapshot_id,
            "authority_coordinate_sha256": self.authority_coordinate_sha256,
            "observed_lifecycle_receipt_sha256": self.observed_lifecycle_receipt_sha256,
            "observed_snapshot_head_id": self.observed_snapshot_head_id,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "subject_generation": self.subject_generation,
            "requested_scope": self.requested_scope.value,
            "requested_scope_key": self.requested_scope_key,
            "evaluated_at": self.evaluated_at,
            "decision": self.decision.value,
            "reason_code": self.reason_code,
            "authoritative": self.authoritative,
            "authority_transferred": self.authority_transferred,
            "dispatch_performed": self.dispatch_performed,
        }


def _build_evaluation(
    *,
    grant: ShadowScopedGrant,
    lifecycle: GrantLifecycleReceipt,
    observed_snapshot_head_id: str,
    subject_type: str,
    subject_id: str,
    subject_generation: str,
    requested_scope: GrantScope,
    requested_scope_key: str | None,
    evaluated_at: int,
    decision: EvaluationDecision,
    reason_code: str,
) -> GrantEvaluationReceipt:
    if type(requested_scope) is not GrantScope:
        raise Phase6ContractError("requested_scope must be GrantScope")
    if requested_scope is GrantScope.SECTION_VIEW:
        key = _identifier(requested_scope_key, "requested_scope_key")
    elif requested_scope_key is not None:
        raise Phase6ContractError("only section:view accepts requested_scope_key")
    else:
        key = None
    if type(decision) is not EvaluationDecision:
        raise Phase6ContractError("decision must be EvaluationDecision")
    body: dict[str, object] = {
        "schema_version": PHASE6_EVALUATION_SCHEMA,
        "grant_id": grant.grant_id,
        "grant_sequence": grant.grant_sequence,
        "snapshot_id": grant.snapshot_id,
        "authority_coordinate_sha256": grant.authority_coordinate_sha256,
        "observed_lifecycle_receipt_sha256": lifecycle.receipt_sha256,
        "observed_snapshot_head_id": _sha(
            observed_snapshot_head_id, "observed_snapshot_head_id"
        ),
        "subject_type": _identifier(subject_type, "subject_type"),
        "subject_id": _identifier(subject_id, "subject_id"),
        "subject_generation": _identifier(subject_generation, "subject_generation"),
        "requested_scope": requested_scope.value,
        "requested_scope_key": key,
        "evaluated_at": _nonnegative(evaluated_at, "evaluated_at"),
        "decision": decision.value,
        "reason_code": _identifier(reason_code, "reason_code"),
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }
    identity = canonical_sha256(body)
    return GrantEvaluationReceipt(
        grant.grant_id,
        grant.grant_sequence,
        grant.snapshot_id,
        grant.authority_coordinate_sha256,
        lifecycle.receipt_sha256,
        body["observed_snapshot_head_id"],
        body["subject_type"],
        body["subject_id"],
        body["subject_generation"],
        requested_scope,
        key,
        body["evaluated_at"],
        decision,
        body["reason_code"],
        identity,
    )


@dataclass(frozen=True)
class ShadowAccessProof:
    source_binding: AuthoritySourceBinding
    snapshot: VerifiedShadowSnapshot
    grant: ShadowScopedGrant
    lifecycle_receipt: GrantLifecycleReceipt
    evaluation_receipt: GrantEvaluationReceipt
    requested_scope: GrantScope
    requested_scope_key: str | None
    evaluated_at: int
    proof_sha256: str
    shadow_allowed: bool = True
    authoritative: bool = False
    authority_transferred: bool = False
    dispatch_performed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PHASE6_ACCESS_PROOF_SCHEMA,
            "source_binding": self.source_binding.as_dict(),
            "snapshot": self.snapshot.as_dict(),
            "grant": self.grant.as_dict(),
            "lifecycle_receipt": self.lifecycle_receipt.as_dict(),
            "evaluation_receipt": self.evaluation_receipt.as_dict(),
            "requested_scope": self.requested_scope.value,
            "requested_scope_key": self.requested_scope_key,
            "evaluated_at": self.evaluated_at,
            "shadow_allowed": self.shadow_allowed,
            "authoritative": self.authoritative,
            "authority_transferred": self.authority_transferred,
            "dispatch_performed": self.dispatch_performed,
            "proof_sha256": self.proof_sha256,
        }


def _build_proof(
    snapshot: VerifiedShadowSnapshot,
    grant: ShadowScopedGrant,
    lifecycle: GrantLifecycleReceipt,
    evaluation: GrantEvaluationReceipt,
) -> ShadowAccessProof:
    if evaluation.decision is not EvaluationDecision.ALLOWED_SHADOW:
        raise Phase6StoreError("a denied evaluation cannot produce an access proof")
    body: dict[str, object] = {
        "schema_version": PHASE6_ACCESS_PROOF_SCHEMA,
        "source_binding": snapshot.source_binding.as_dict(),
        "snapshot": snapshot.as_dict(),
        "grant": grant.as_dict(),
        "lifecycle_receipt": lifecycle.as_dict(),
        "evaluation_receipt": evaluation.as_dict(),
        "requested_scope": evaluation.requested_scope.value,
        "requested_scope_key": evaluation.requested_scope_key,
        "evaluated_at": evaluation.evaluated_at,
        "shadow_allowed": True,
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }
    return ShadowAccessProof(
        snapshot.source_binding,
        snapshot,
        grant,
        lifecycle,
        evaluation,
        evaluation.requested_scope,
        evaluation.requested_scope_key,
        evaluation.evaluated_at,
        canonical_sha256(body),
    )


@dataclass(frozen=True)
class SnapshotCommitResult:
    snapshot: VerifiedShadowSnapshot
    replayed: bool


@dataclass(frozen=True)
class GrantCommitResult:
    grant: ShadowScopedGrant
    lifecycle_receipt: GrantLifecycleReceipt
    replayed: bool


@dataclass(frozen=True)
class LifecycleCommitResult:
    grant: ShadowScopedGrant
    lifecycle_receipt: GrantLifecycleReceipt
    replayed: bool


@dataclass(frozen=True)
class EvaluationResult:
    receipt: GrantEvaluationReceipt
    current: bool
    shadow_allowed: bool
    access_proof: ShadowAccessProof | None
    replayed: bool


def _phase6_failure_point(stage: str) -> None:
    """Deterministic no-op fault-injection seam used by adversarial tests."""


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "_FileIdentity":
        return cls(
            int(value.st_dev), int(value.st_ino), int(value.st_size),
            int(value.st_mtime_ns), int(value.st_ctime_ns),
        )


class _AnchoredConnection(sqlite3.Connection):
    _anchor: OwnedDescriptor | None = None
    _parent_anchor: OwnedDescriptor | None = None
    _identity: _FileIdentity | None = None
    _parent_identity: tuple[int, int] | None = None
    _commit_completed: bool = False
    _sqlite_closed: bool = False

    @property
    def anchor_fd(self) -> int:
        if self._anchor is None:
            raise Phase6StoreError("Phase-6 connection lacks an ownership anchor")
        return self._anchor.fileno(_CONNECTION_OWNER)

    def _adopt_anchors(
        self,
        *,
        database: OwnedDescriptor,
        database_owner: str,
        parent: OwnedDescriptor,
        parent_owner: str,
    ) -> None:
        if self._anchor is not None or self._parent_anchor is not None:
            raise Phase6StoreError("Phase-6 connection already owns anchors")
        # Publish both shared lease objects before changing either token.  A
        # BaseException at every subsequent line therefore leaves each lease
        # owned by either its old token or the connection token; the caller's
        # two cleanup paths can safely converge without an ownerless window.
        self._anchor = database
        self._parent_anchor = parent
        database.transfer(owner=database_owner, new_owner=_CONNECTION_OWNER)
        parent.transfer(owner=parent_owner, new_owner=_CONNECTION_OWNER)

    def _close_anchor_attribute(self, attribute: str) -> None:
        lease = getattr(self, attribute)
        if lease is None:
            return
        try:
            run_cleanup(
                [(f"close {attribute}", lease.cleanup(_CONNECTION_OWNER))]
            )
        finally:
            if lease.closed:
                setattr(self, attribute, None)

    def _anchor_cleanup_callbacks(self):
        callbacks = []
        if self._parent_anchor is not None:
            callbacks.append(
                (
                    "close Phase-6 parent anchor",
                    RetryableCleanup(
                        lambda: self._close_anchor_attribute("_parent_anchor")
                    ),
                )
            )
        if self._anchor is not None:
            callbacks.append(
                (
                    "close Phase-6 connection anchor",
                    RetryableCleanup(
                        lambda: self._close_anchor_attribute("_anchor")
                    ),
                )
            )
        return callbacks

    def close(self) -> None:
        try:
            callbacks = self._anchor_cleanup_callbacks()
            if not self._sqlite_closed:
                sqlite3.Connection.close(self)
                self._sqlite_closed = True
            run_cleanup(callbacks)
        except BaseException as primary:
            # sqlite3.Connection.close() is idempotent.  Retrying at the Python
            # object level reconciles an interrupt before its C call without
            # issuing a second OS close after a completed call.
            def reconcile_sqlite_close() -> None:
                if self._sqlite_closed:
                    return
                sqlite3.Connection.close(self)
                self._sqlite_closed = True

            run_cleanup(
                [
                    (
                        "reconcile interrupted Phase-6 SQLite close",
                        RetryableCleanup(reconcile_sqlite_close),
                    )
                ],
                primary=primary,
            )
            try:
                recovery_callbacks = self._anchor_cleanup_callbacks()
            except BaseException as builder_error:
                def report_builder(error: BaseException = builder_error) -> None:
                    raise error

                run_cleanup(
                    [("build Phase-6 anchor recovery", report_builder)],
                    primary=primary,
                )
            else:
                run_cleanup(recovery_callbacks, primary=primary)
            raise

    @property
    def _resources_closed(self) -> bool:
        return self._sqlite_closed and all(
            lease is None or lease.closed
            for lease in (self._anchor, self._parent_anchor)
        )

    def __del__(self) -> None:
        """Last-resort recovery for an interrupted connection publication."""

        if self._resources_closed:
            return
        try:
            self.close()
        except BaseException:
            try:
                LOGGER.exception(
                    "last-resort close failed for an unpublished Phase-6 connection"
                )
            except BaseException:
                # Interpreter teardown can make logging unavailable.  The
                # regular paths retain errors through ``run_cleanup``; this is
                # only the final guard for a value never published to a caller.
                pass


def _cleanup_failed_connection(
    connection: _AnchoredConnection,
    *,
    label: str,
    primary: BaseException,
) -> None:
    """Rollback if possible and close while preserving ``primary`` exactly."""

    def rollback_if_active() -> None:
        if connection.in_transaction:
            connection.rollback()

    run_cleanup(
        [
            (f"rollback {label}", rollback_if_active),
            (f"close {label}", RetryableCleanup(connection.close)),
        ],
        primary=primary,
    )


def _lifecycle_from_dict(
    value: object, grant: ShadowScopedGrant
) -> GrantLifecycleReceipt:
    if type(value) is not dict:
        raise Phase6StoreError("lifecycle receipt is malformed")
    expected = {
        "schema_version", "receipt_sequence", "previous_receipt_sha256", "grant_id",
        "grant_sequence", "snapshot_id", "authority_coordinate_sha256", "event",
        "before_status", "after_status", "actor_id", "actor_generation",
        "reason_code", "effective_at", "request_schema", "request_sha256",
        "authoritative", "authority_transferred", "dispatch_performed", "receipt_sha256",
    }
    if set(value) != expected or value.get("schema_version") != PHASE6_LIFECYCLE_SCHEMA:
        raise Phase6StoreError("lifecycle receipt keys or schema differ")
    _false_safety(value, "lifecycle receipt")
    try:
        before = None if value["before_status"] is None else GrantStatus(value["before_status"])
        result = _build_lifecycle(
            grant=grant,
            receipt_sequence=value["receipt_sequence"],
            previous_receipt_sha256=value["previous_receipt_sha256"],
            event=value["event"],
            before_status=before,
            after_status=GrantStatus(value["after_status"]),
            actor_id=value["actor_id"],
            actor_generation=value["actor_generation"],
            reason_code=value["reason_code"],
            effective_at=value["effective_at"],
            request_schema=value["request_schema"],
            request_sha256=value["request_sha256"],
        )
    except (KeyError, TypeError, ValueError, Phase6ContractError) as exc:
        raise Phase6StoreError("lifecycle receipt does not revalidate") from exc
    if result.receipt_sha256 != value["receipt_sha256"]:
        raise Phase6StoreError("lifecycle receipt SHA-256 differs")
    return result


def _evaluation_from_dict(
    value: object,
    grant: ShadowScopedGrant,
    lifecycle: GrantLifecycleReceipt,
) -> GrantEvaluationReceipt:
    if type(value) is not dict:
        raise Phase6StoreError("evaluation receipt is malformed")
    expected = {
        "schema_version", "evaluation_id", "evaluation_sha256", "grant_id",
        "grant_sequence", "snapshot_id", "authority_coordinate_sha256",
        "observed_lifecycle_receipt_sha256", "observed_snapshot_head_id",
        "subject_type", "subject_id", "subject_generation", "requested_scope",
        "requested_scope_key", "evaluated_at", "decision", "reason_code",
        "authoritative", "authority_transferred", "dispatch_performed",
    }
    if set(value) != expected or value.get("schema_version") != PHASE6_EVALUATION_SCHEMA:
        raise Phase6StoreError("evaluation receipt keys or schema differ")
    _false_safety(value, "evaluation receipt")
    try:
        result = _build_evaluation(
            grant=grant,
            lifecycle=lifecycle,
            observed_snapshot_head_id=value["observed_snapshot_head_id"],
            subject_type=value["subject_type"],
            subject_id=value["subject_id"],
            subject_generation=value["subject_generation"],
            requested_scope=GrantScope(value["requested_scope"]),
            requested_scope_key=value["requested_scope_key"],
            evaluated_at=value["evaluated_at"],
            decision=EvaluationDecision(value["decision"]),
            reason_code=value["reason_code"],
        )
    except (KeyError, TypeError, ValueError, Phase6ContractError) as exc:
        raise Phase6StoreError("evaluation receipt does not revalidate") from exc
    if value["evaluation_id"] != value["evaluation_sha256"] or result.evaluation_id != value["evaluation_id"]:
        raise Phase6StoreError("evaluation receipt SHA-256 differs")
    return result


def verify_shadow_access_proof(value: object) -> ShadowAccessProof:
    """Deeply revalidate one serialized, currently allowed shadow proof.

    This is the public deserialization boundary for later-phase CLI, Web and
    service callers.  It accepts either the typed value returned by
    :meth:`Phase6SnapshotGrantStore.evaluate_grant` or an exact JSON-safe
    mapping.  Every nested receipt is rebuilt through the same validators used
    by the durable store; no caller-supplied hash or ``shadow_allowed`` flag is
    trusted on its own.

    The proof describes the current state observed by its evaluation.  It is
    not a live authorization check against a database: a caller that needs to
    know whether a proof remains current must use
    :meth:`Phase6SnapshotGrantStore.verify_current_access_proof`.
    """

    if type(value) is ShadowAccessProof:
        raw: object = value.as_dict()
    elif isinstance(value, Mapping):
        try:
            raw = dict(value)
        except Exception as exc:
            raise Phase6ContractError(
                "access proof mapping cannot be read exactly"
            ) from exc
    else:
        raise Phase6ContractError(
            "access proof must be ShadowAccessProof or an exact JSON object"
        )
    try:
        wire = json.loads(canonical_bytes(raw).decode("utf-8"))
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise Phase6ContractError("access proof is outside canonical JSON") from exc
    if raw != wire or type(wire) is not dict:
        raise Phase6ContractError("access proof must contain exact JSON-safe values")

    expected = {
        "schema_version",
        "source_binding",
        "snapshot",
        "grant",
        "lifecycle_receipt",
        "evaluation_receipt",
        "requested_scope",
        "requested_scope_key",
        "evaluated_at",
        "shadow_allowed",
        "authoritative",
        "authority_transferred",
        "dispatch_performed",
        "proof_sha256",
    }
    if set(wire) != expected or wire.get("schema_version") != PHASE6_ACCESS_PROOF_SCHEMA:
        raise Phase6StoreError("access proof keys or schema differ")
    _false_safety(wire, "access proof")
    if wire["shadow_allowed"] is not True:
        raise Phase6StoreError("access proof must be currently shadow-allowed")

    source_binding = _source_binding_from_dict(wire["source_binding"])
    snapshot = _snapshot_from_dict(wire["snapshot"])
    if source_binding != snapshot.source_binding:
        raise Phase6StoreError("access proof source binding differs from snapshot")
    if not source_binding.eligible:
        raise Phase6StoreError("access proof source binding is ineligible")

    grant = _grant_from_dict(wire["grant"], snapshot)
    lifecycle = _lifecycle_from_dict(wire["lifecycle_receipt"], grant)
    evaluation = _evaluation_from_dict(
        wire["evaluation_receipt"], grant, lifecycle
    )

    issue_schema = "phase6-issue-shadow-scoped-grant-request-v1"
    issue_request = {
        "schema_version": issue_schema,
        "snapshot_id": grant.snapshot_id,
        "subject_type": grant.subject_type,
        "subject_id": grant.subject_id,
        "subject_generation": grant.subject_generation,
        "scope": grant.scope.value,
        "scope_key": grant.scope_key,
        "issuer_id": grant.issuer_id,
        "issuer_generation": grant.issuer_generation,
        "issuer_evidence_schema": grant.issuer_evidence_schema,
        "issuer_receipt_sha256": grant.issuer_receipt_sha256,
        "issued_at": grant.issued_at,
        "not_before": grant.not_before,
        "expires_at": grant.expires_at,
        "expected_previous_grant_id": grant.previous_grant_id,
    }
    if (
        lifecycle.after_status is not GrantStatus.ACTIVE
        or lifecycle.actor_id != grant.issuer_id
        or lifecycle.actor_generation != grant.issuer_generation
        or lifecycle.reason_code != "ISSUED_SHADOW"
        or lifecycle.effective_at != grant.issued_at
        or lifecycle.request_schema != issue_schema
        or lifecycle.request_sha256 != canonical_sha256(issue_request)
    ):
        raise Phase6StoreError("access proof lifecycle is not the active issue fact")

    if (
        evaluation.decision is not EvaluationDecision.ALLOWED_SHADOW
        or evaluation.reason_code != "EXACT_SHADOW_SCOPE_ALLOWED"
        or evaluation.observed_snapshot_head_id != snapshot.snapshot_id
        or evaluation.subject_type != grant.subject_type
        or evaluation.subject_id != grant.subject_id
        or evaluation.subject_generation != grant.subject_generation
        or evaluation.requested_scope is not grant.scope
        or evaluation.requested_scope_key != grant.scope_key
        or evaluation.evaluated_at < grant.not_before
        or evaluation.evaluated_at >= grant.expires_at
        or evaluation.evaluated_at >= snapshot.valid_until
    ):
        raise Phase6StoreError("access proof evaluation is not currently allowed")

    canonical = _build_proof(snapshot, grant, lifecycle, evaluation)
    if canonical.as_dict() != wire:
        raise Phase6StoreError("access proof canonical binding or SHA-256 differs")
    return canonical


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE phase6_shadow_schema_state (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        schema_version TEXT NOT NULL,
        store_instance_id TEXT NOT NULL,
        creation_binding_json TEXT NOT NULL,
        schema_digest_sha256 TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE phase6_shadow_snapshot_facts (
        snapshot_id TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        snapshot_sequence INTEGER NOT NULL,
        authority_revision INTEGER NOT NULL,
        snapshot_json TEXT NOT NULL,
        snapshot_sha256 TEXT NOT NULL,
        UNIQUE(workflow_id, project_id, snapshot_sequence)
    )
    """,
    """
    CREATE TABLE phase6_shadow_snapshot_current (
        workflow_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        snapshot_id TEXT NOT NULL,
        snapshot_sequence INTEGER NOT NULL,
        authority_revision INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY(workflow_id, project_id),
        FOREIGN KEY(snapshot_id) REFERENCES phase6_shadow_snapshot_facts(snapshot_id)
    )
    """,
    """
    CREATE TABLE phase6_shadow_grant_facts (
        grant_id TEXT PRIMARY KEY,
        snapshot_id TEXT NOT NULL,
        grant_sequence INTEGER NOT NULL,
        grant_chain_key TEXT NOT NULL,
        grant_json TEXT NOT NULL,
        grant_sha256 TEXT NOT NULL,
        UNIQUE(grant_chain_key, grant_sequence),
        FOREIGN KEY(snapshot_id) REFERENCES phase6_shadow_snapshot_facts(snapshot_id)
    )
    """,
    """
    CREATE TABLE phase6_shadow_grant_lifecycle_facts (
        receipt_sha256 TEXT PRIMARY KEY,
        grant_id TEXT NOT NULL,
        receipt_sequence INTEGER NOT NULL,
        receipt_json TEXT NOT NULL,
        effective_at INTEGER NOT NULL,
        UNIQUE(grant_id, receipt_sequence),
        FOREIGN KEY(grant_id) REFERENCES phase6_shadow_grant_facts(grant_id)
    )
    """,
    """
    CREATE TABLE phase6_shadow_grant_current (
        grant_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        receipt_sequence INTEGER NOT NULL,
        lifecycle_receipt_sha256 TEXT NOT NULL,
        last_evaluated_at INTEGER,
        updated_at INTEGER NOT NULL,
        FOREIGN KEY(grant_id) REFERENCES phase6_shadow_grant_facts(grant_id),
        FOREIGN KEY(lifecycle_receipt_sha256)
            REFERENCES phase6_shadow_grant_lifecycle_facts(receipt_sha256)
    )
    """,
    """
    CREATE TABLE phase6_shadow_evaluation_receipts (
        evaluation_id TEXT PRIMARY KEY,
        grant_id TEXT NOT NULL,
        evaluated_at INTEGER NOT NULL,
        evaluation_json TEXT NOT NULL,
        evaluation_sha256 TEXT NOT NULL,
        FOREIGN KEY(grant_id) REFERENCES phase6_shadow_grant_facts(grant_id)
    )
    """,
    """
    CREATE TABLE phase6_shadow_idempotency (
        idempotency_key TEXT PRIMARY KEY,
        request_domain TEXT NOT NULL,
        request_schema TEXT NOT NULL,
        request_json TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        result_kind TEXT NOT NULL,
        result_id TEXT NOT NULL,
        result_json TEXT NOT NULL,
        result_sha256 TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )
    """,
)

_IMMUTABLE_TABLES = (
    "phase6_shadow_schema_state",
    "phase6_shadow_snapshot_facts",
    "phase6_shadow_grant_facts",
    "phase6_shadow_grant_lifecycle_facts",
    "phase6_shadow_evaluation_receipts",
    "phase6_shadow_idempotency",
)

# Frozen after generating the schema above with the repository SQLite runtime.
# It is intentionally not derived by opening SQLite at import time.
_EXPECTED_SCHEMA_DIGEST = (
    "e770248470e333741e174e2cb5c0d414a7a3bca56dd50bd6fdc73f690af8fdd3"
)


def _schema_inventory(connection: sqlite3.Connection) -> tuple[dict[str, object], ...]:
    rows = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall()
    return tuple(
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table_name": str(row[2]),
            "sql": None if row[3] is None else str(row[3]),
        }
        for row in rows
    )


def _create_schema(connection: sqlite3.Connection) -> str:
    for statement in _SCHEMA_STATEMENTS:
        connection.execute(statement)
    for table in _IMMUTABLE_TABLES:
        connection.execute(
            f"""
            CREATE TRIGGER {table}_immutable_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} is append-only');
            END
            """
        )
        connection.execute(
            f"""
            CREATE TRIGGER {table}_immutable_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} is append-only');
            END
            """
        )
    return canonical_sha256(_schema_inventory(connection))


class Phase6SnapshotGrantStore:
    """Caller-path standalone durable store for Phase-6 shadow evidence."""

    __slots__ = ("_path",)

    def __init__(self, database: str | Path) -> None:
        if not isinstance(database, (str, Path)) or not str(database):
            raise Phase6StoreError("an explicit Phase-6 SQLite path is required")
        self._path = Path(database)
        if not self._path.is_absolute():
            raise Phase6StoreError("Phase-6 SQLite path must be absolute")

    @property
    def path(self) -> Path:
        return self._path

    def _validate_parent(self) -> None:
        parent = self._path.parent
        if not parent.is_dir():
            raise Phase6StoreError("Phase-6 SQLite parent must exist")
        try:
            resolved = parent.resolve(strict=True)
        except OSError as exc:
            raise Phase6StoreError("Phase-6 SQLite parent is unavailable") from exc
        if resolved != parent:
            raise Phase6StoreError("Phase-6 SQLite parent cannot contain symlinks")
        if not Path("/proc/self/fd").is_dir():
            raise Phase6StoreError("descriptor-anchored SQLite is unavailable")

    def _open_parent(self, owner: str) -> tuple[OwnedDescriptor, tuple[int, int]]:
        self._validate_parent()
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor: OwnedDescriptor | None = None
        try:
            try:
                descriptor = OwnedDescriptor.from_opener(
                    lambda: os.open(self._path.parent, flags),
                    owner=owner,
                    label="Phase-6 parent",
                )
            except OSError as exc:
                raise Phase6StoreError("Phase-6 parent anchor is unavailable") from exc
            anchored = os.fstat(descriptor.fileno(owner))
            named = os.stat(self._path.parent, follow_symlinks=False)
            if not stat.S_ISDIR(anchored.st_mode) or not stat.S_ISDIR(named.st_mode):
                raise Phase6StoreError("Phase-6 parent must remain a directory")
            identity = (int(anchored.st_dev), int(anchored.st_ino))
            if identity != (int(named.st_dev), int(named.st_ino)):
                raise Phase6StoreError("Phase-6 parent identity changed")
            return descriptor, identity
        except BaseException as primary:
            if descriptor is not None:
                run_cleanup(
                    [
                        (
                            "close Phase-6 parent after open failure",
                            descriptor.cleanup(owner),
                        )
                    ],
                    primary=primary,
                )
            raise

    def _assert_parent(
        self, descriptor: OwnedDescriptor, owner: str, expected: tuple[int, int]
    ) -> None:
        try:
            anchored = os.fstat(descriptor.fileno(owner))
            named = os.stat(self._path.parent, follow_symlinks=False)
        except OSError as exc:
            raise Phase6StoreError("Phase-6 parent fence is unavailable") from exc
        current = (int(anchored.st_dev), int(anchored.st_ino))
        current_name = (int(named.st_dev), int(named.st_ino))
        if (
            not stat.S_ISDIR(anchored.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or current != expected
            or current_name != expected
        ):
            raise Phase6StoreError("Phase-6 parent fence changed")

    def _assert_no_sidecars(self) -> None:
        for suffix in _SIDECAR_SUFFIXES:
            if os.path.lexists(f"{self._path}{suffix}"):
                raise Phase6StoreError(
                    f"Phase-6 ownership preflight rejects existing {suffix} sidecar"
                )

    @staticmethod
    def _fd_uri(descriptor: int, query: str) -> str:
        return f"file:/proc/self/fd/{descriptor}?{query}"

    @staticmethod
    def _identity_from_fd(descriptor: int) -> _FileIdentity:
        value = os.fstat(descriptor)
        if not stat.S_ISREG(value.st_mode) or int(value.st_nlink) != 1:
            raise Phase6StoreError("Phase-6 database must be a single-link regular file")
        if stat.S_IMODE(value.st_mode) != 0o600:
            raise Phase6StoreError("Phase-6 database mode must be exactly 0600")
        return _FileIdentity.from_stat(value)

    def _path_identity(self) -> _FileIdentity:
        try:
            value = os.lstat(self._path)
        except OSError as exc:
            raise Phase6StoreError("Phase-6 database path is unavailable") from exc
        if not stat.S_ISREG(value.st_mode) or int(value.st_nlink) != 1:
            raise Phase6StoreError("Phase-6 database must be a single-link regular file")
        if stat.S_IMODE(value.st_mode) != 0o600:
            raise Phase6StoreError("Phase-6 database mode must be exactly 0600")
        return _FileIdentity.from_stat(value)

    @staticmethod
    def _entry_identity(parent_fd: int, name: str) -> _FileIdentity:
        """Inspect one entry relative to the already verified parent inode."""

        try:
            value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise Phase6StoreError("Phase-6 database entry is unavailable") from exc
        if not stat.S_ISREG(value.st_mode) or int(value.st_nlink) != 1:
            raise Phase6StoreError("Phase-6 database must be a single-link regular file")
        if stat.S_IMODE(value.st_mode) != 0o600:
            raise Phase6StoreError("Phase-6 database mode must be exactly 0600")
        return _FileIdentity.from_stat(value)

    def _assert_path(
        self,
        descriptor: OwnedDescriptor,
        owner: str,
        *,
        expected: _FileIdentity | None = None,
        exact: bool,
    ) -> _FileIdentity:
        anchored = self._identity_from_fd(descriptor.fileno(owner))
        named = self._path_identity()
        if (anchored.device, anchored.inode) != (named.device, named.inode):
            raise Phase6StoreError("Phase-6 path no longer names the anchored inode")
        if exact and anchored != named:
            raise Phase6StoreError("Phase-6 path metadata changed during fence")
        if expected is not None:
            if exact and anchored != expected:
                raise Phase6StoreError("Phase-6 anchored identity changed")
            if not exact and (anchored.device, anchored.inode) != (
                expected.device,
                expected.inode,
            ):
                raise Phase6StoreError("Phase-6 anchored inode changed")
        return anchored

    def _creation_binding(
        self, identity: _FileIdentity, parent_identity: tuple[int, int]
    ) -> dict[str, object]:
        return {
            "schema_version": "phase6-store-ownership-binding-v1",
            "absolute_path_sha256": canonical_sha256(
                {
                    "schema_version": "phase6-store-absolute-path-v1",
                    "absolute_path": str(self._path),
                }
            ),
            "created_device": identity.device,
            "created_inode": identity.inode,
            "created_size": identity.size,
            "parent_device": parent_identity[0],
            "parent_inode": parent_identity[1],
        }

    def _verify_schema(
        self,
        connection: sqlite3.Connection,
        identity: _FileIdentity,
        parent_identity: tuple[int, int],
    ) -> None:
        digest = canonical_sha256(_schema_inventory(connection))
        if not _EXPECTED_SCHEMA_DIGEST or digest != _EXPECTED_SCHEMA_DIGEST:
            raise Phase6StoreError("Phase-6 exact schema profile differs")
        rows = connection.execute(
            "SELECT * FROM phase6_shadow_schema_state ORDER BY singleton"
        ).fetchall()
        if len(rows) != 1:
            raise Phase6StoreError("Phase-6 ownership marker is unavailable")
        row = rows[0]
        if (
            row["singleton"] != 1
            or row["schema_version"] != PHASE6_STORE_SCHEMA
            or row["schema_digest_sha256"] != _EXPECTED_SCHEMA_DIGEST
        ):
            raise Phase6StoreError("Phase-6 ownership marker differs")
        binding = _decode_canonical(
            row["creation_binding_json"], row["store_instance_id"], "ownership binding"
        )
        expected = self._creation_binding(
            _FileIdentity(identity.device, identity.inode, 0, 0, 0),
            parent_identity,
        )
        if type(binding) is not dict or set(binding) != set(expected):
            raise Phase6StoreError("Phase-6 ownership binding is malformed")
        for field in expected:
            if field not in {"created_size"} and binding[field] != expected[field]:
                raise Phase6StoreError("Phase-6 ownership binding differs")
        if binding["created_size"] != 0:
            raise Phase6StoreError("Phase-6 store was not bound at exclusive creation")

    def _open_verified(
        self,
        deadline: object | None = None,
    ) -> tuple[OwnedDescriptor, _FileIdentity, OwnedDescriptor, tuple[int, int]]:
        _deadline_check(deadline, "phase6_preflight_before")
        parent_owner = "phase6-preflight-parent"
        database_owner = "phase6-preflight-database"
        parent, parent_identity = self._open_parent(parent_owner)
        database: OwnedDescriptor | None = None
        readonly_connection: sqlite3.Connection | None = None
        try:
            try:
                database = OwnedDescriptor.from_opener(
                    lambda: os.open(
                        self._path,
                        os.O_RDONLY
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                    ),
                    owner=database_owner,
                    label="Phase-6 database",
                )
            except OSError as exc:
                raise Phase6StoreError("Phase-6 database is unavailable") from exc
            # Serialize every Phase-6 participant before immutable main-file
            # inspection.  Without this independent advisory lock, a second
            # verifier could ignore an in-flight rollback journal and observe
            # transient main-file pages as corruption.  The lock is retained
            # by the same owned descriptor through connection close.
            if deadline is None:
                fcntl.flock(database.fileno(database_owner), fcntl.LOCK_EX)
            else:
                while True:
                    try:
                        fcntl.flock(
                            database.fileno(database_owner),
                            fcntl.LOCK_EX | fcntl.LOCK_NB,
                        )
                        break
                    except BlockingIOError:
                        remaining = _deadline_remaining(
                            deadline, 0.01, "phase6_preflight_lock_wait"
                        )
                        time.sleep(remaining)
            self._assert_no_sidecars()
            before = self._assert_path(database, database_owner, exact=True)
            header = os.pread(database.fileno(database_owner), 100, 0)
            if (
                len(header) != 100
                or header[:16] != _SQLITE_HEADER
                or header[18] != 1
                or header[19] != 1
            ):
                raise Phase6StoreError("Phase-6 rollback-journal SQLite header is invalid")
            readonly_connection = sqlite3.connect(
                self._fd_uri(database.fileno(database_owner), "mode=ro&immutable=1"),
                uri=True,
                timeout=0,
            )
            preflight_error: BaseException | None = None
            try:
                readonly_connection.row_factory = sqlite3.Row
                readonly_connection.execute("PRAGMA query_only=ON")
                self._verify_schema(readonly_connection, before, parent_identity)
                self._verify_integrity(readonly_connection)
            except BaseException as error:
                preflight_error = error
                raise
            finally:
                run_cleanup(
                    [
                        (
                            "close Phase-6 readonly preflight",
                            RetryableCleanup(readonly_connection.close),
                        )
                    ],
                    primary=preflight_error,
                )
            self._assert_path(database, database_owner, expected=before, exact=True)
            self._assert_parent(parent, parent_owner, parent_identity)
            self._assert_no_sidecars()
            _deadline_check(deadline, "phase6_preflight_after")
            return database, before, parent, parent_identity
        except BaseException as primary:
            callbacks = []
            if readonly_connection is not None:
                callbacks.append(
                    (
                        "close interrupted Phase-6 readonly preflight",
                        RetryableCleanup(readonly_connection.close),
                    )
                )
            if database is not None:
                callbacks.append(
                    (
                        "close Phase-6 database preflight",
                        database.cleanup(database_owner),
                    )
                )
            callbacks.append(
                ("close Phase-6 parent preflight", parent.cleanup(parent_owner))
            )
            run_cleanup(callbacks, primary=primary)
            raise

    def _connect(self, deadline: object | None = None) -> _AnchoredConnection:
        database, identity, parent, parent_identity = self._open_verified(deadline)
        database_owner = database.owner
        parent_owner = parent.owner
        connection: _AnchoredConnection | None = None
        try:
            connection = sqlite3.connect(
                self._fd_uri(database.fileno(database_owner), "mode=rw"),
                uri=True,
                timeout=_deadline_remaining(
                    deadline, 5.0, "phase6_connect_before"
                ),
                factory=_AnchoredConnection,
            )
            connection._adopt_anchors(
                database=database,
                database_owner=database_owner,
                parent=parent,
                parent_owner=parent_owner,
            )
            connection._identity = identity
            connection._parent_identity = parent_identity
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            busy_timeout_ms = max(
                1,
                int(
                    _deadline_remaining(
                        deadline, 5.0, "phase6_connect_busy_timeout"
                    )
                    * 1000
                ),
            )
            connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            self._assert_connection(connection, exact=True)
            self._verify_schema(connection, identity, parent_identity)
            self._verify_integrity(connection)
            return connection
        except BaseException as primary:
            callbacks = []
            if connection is not None:
                callbacks.append(
                    (
                        "close Phase-6 failed connection",
                        RetryableCleanup(connection.close),
                    )
                )
            callbacks.extend(
                [
                    (
                        "close untransferred Phase-6 database",
                        database.cleanup(database_owner),
                    ),
                    (
                        "close untransferred Phase-6 parent",
                        parent.cleanup(parent_owner),
                    ),
                ]
            )
            run_cleanup(callbacks, primary=primary)
            raise

    def _assert_connection(
        self,
        connection: _AnchoredConnection,
        *,
        exact: bool,
        require_no_sidecars: bool = True,
    ) -> None:
        if (
            type(connection) is not _AnchoredConnection
            or connection._anchor is None
            or connection._parent_anchor is None
            or connection._identity is None
            or connection._parent_identity is None
        ):
            raise Phase6StoreError("Phase-6 connection ownership is incomplete")
        self._assert_path(
            connection._anchor,
            _CONNECTION_OWNER,
            expected=connection._identity,
            exact=exact,
        )
        self._assert_parent(
            connection._parent_anchor,
            _CONNECTION_OWNER,
            connection._parent_identity,
        )
        if require_no_sidecars:
            self._assert_no_sidecars()

    def _begin(self, connection: _AnchoredConnection, *, immediate: bool) -> None:
        self._assert_connection(connection, exact=True)
        self._verify_integrity(connection)
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        self._assert_connection(connection, exact=True)

    def _commit(self, connection: _AnchoredConnection) -> None:
        self._assert_connection(
            connection, exact=False, require_no_sidecars=False
        )
        try:
            connection.commit()
            connection._commit_completed = True
        except BaseException:
            # If an asynchronous exception lands after SQLite completed COMMIT,
            # ``in_transaction`` is false and ownership has already crossed the
            # durable boundary.  Record that state so cleanup cannot unlink it.
            if not connection.in_transaction:
                connection._commit_completed = True
            raise
        self._assert_connection(connection, exact=False)

    def _cleanup_created_entry(
        self,
        *,
        parent: OwnedDescriptor,
        parent_owner: str,
        parent_identity: tuple[int, int],
        cleanup_fd: int,
        expected: _FileIdentity,
    ) -> None:
        parent_fd = parent.fileno(parent_owner)
        anchored_parent = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(anchored_parent.st_mode)
            or (int(anchored_parent.st_dev), int(anchored_parent.st_ino))
            != parent_identity
        ):
            raise Phase6StoreError("Phase-6 cleanup parent anchor changed")
        anchored = self._identity_from_fd(cleanup_fd)
        try:
            named_stat = os.stat(
                self._path.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        except OSError as exc:
            raise Phase6StoreError(
                "failed Phase-6 database entry is unavailable"
            ) from exc
        if (
            not stat.S_ISREG(named_stat.st_mode)
            or int(named_stat.st_nlink) != 1
            or stat.S_IMODE(named_stat.st_mode) != 0o600
        ):
            raise Phase6StoreError(
                "refusing to clean a malformed Phase-6 database entry"
            )
        named = _FileIdentity.from_stat(named_stat)
        if (anchored.device, anchored.inode) != (expected.device, expected.inode) or (
            named.device,
            named.inode,
        ) != (expected.device, expected.inode):
            raise Phase6StoreError("refusing to clean a replaced Phase-6 database")

        def capture_and_unlink(
            name: str, *, expected_inode: tuple[int, int] | None
        ) -> None:
            try:
                observed_stat = os.stat(
                    name, dir_fd=parent_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                return
            if not stat.S_ISREG(observed_stat.st_mode):
                raise Phase6StoreError("refusing to clean a non-regular Phase-6 entry")
            observed_inode = (int(observed_stat.st_dev), int(observed_stat.st_ino))
            if expected_inode is not None and observed_inode != expected_inode:
                raise Phase6StoreError("refusing to clean a replaced Phase-6 entry")
            quarantine = (
                f".{self._path.name}.phase6-cleanup-"
                f"{os.getpid()}-{secrets.token_hex(16)}"
            )

            def inode_if_present(entry: str) -> tuple[int, int] | None:
                try:
                    value = os.stat(
                        entry, dir_fd=parent_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    return None
                if not stat.S_ISREG(value.st_mode) or int(value.st_nlink) != 1:
                    raise Phase6StoreError(
                        "Phase-6 cleanup recovery found a non-regular entry"
                    )
                return (int(value.st_dev), int(value.st_ino))

            def finish_owned_cleanup() -> None:
                captured_inode = inode_if_present(quarantine)
                original_inode = inode_if_present(name)
                if captured_inode is not None:
                    if captured_inode != observed_inode:
                        raise Phase6StoreError(
                            "Phase-6 cleanup recovery found a replacement quarantine"
                        )
                elif original_inode is not None:
                    if original_inode != observed_inode:
                        raise Phase6StoreError(
                            "Phase-6 cleanup recovery refuses a replacement entry"
                        )
                    _rename_noreplace(parent_fd, name, parent_fd, quarantine)
                    captured_inode = inode_if_present(quarantine)
                    if captured_inode != observed_inode:
                        raise Phase6StoreError(
                            "Phase-6 cleanup recovery lost the owned inode"
                        )
                else:
                    os.fsync(parent_fd)
                    return
                resilient_unlink_at(parent_fd, quarantine)
                os.fsync(parent_fd)

            try:
                _rename_noreplace(parent_fd, name, parent_fd, quarantine)
                captured_inode = inode_if_present(quarantine)
                if captured_inode != observed_inode:
                    raise Phase6StoreError(
                        "Phase-6 cleanup entry changed during capture"
                    )
                resilient_unlink_at(parent_fd, quarantine)
                os.fsync(parent_fd)
            except BaseException as primary:
                run_cleanup(
                    [("finish Phase-6 owned cleanup", finish_owned_cleanup)],
                    primary=primary,
                )
                raise

        run_cleanup(
            [
                *(
                    (
                        f"clean Phase-6 {suffix} sidecar",
                        lambda suffix=suffix: capture_and_unlink(
                            f"{self._path.name}{suffix}", expected_inode=None
                        ),
                    )
                    for suffix in _SIDECAR_SUFFIXES
                ),
                (
                    "clean Phase-6 database",
                    lambda: capture_and_unlink(
                        self._path.name,
                        expected_inode=(expected.device, expected.inode),
                    ),
                ),
                ("fsync Phase-6 cleanup parent", lambda: os.fsync(parent_fd)),
            ]
        )

    def _initialize_new(self) -> None:
        parent_owner = "phase6-initialize-parent"
        creator_owner = "phase6-initialize-creator"
        cleanup_owner = "phase6-initialize-cleanup"
        parent, parent_identity = self._open_parent(parent_owner)
        creator_raw = -1
        creator: OwnedDescriptor | None = None
        cleanup: OwnedDescriptor | None = None
        connection: _AnchoredConnection | None = None
        created: _FileIdentity | None = None
        committed = False
        try:
            self._assert_parent(parent, parent_owner, parent_identity)
            self._assert_no_sidecars()
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                creator_raw = os.open(
                    self._path.name,
                    flags,
                    0o600,
                    dir_fd=parent.fileno(parent_owner),
                )
                creator = OwnedDescriptor(
                    creator_raw,
                    owner=creator_owner,
                    label="Phase-6 creator",
                )
            except FileExistsError as exc:
                raise Phase6StoreError("Phase-6 path appeared during exclusive creation") from exc
            fcntl.flock(creator.fileno(creator_owner), fcntl.LOCK_EX)
            created = self._assert_path(creator, creator_owner, exact=True)
            anchored_entry = self._entry_identity(
                parent.fileno(parent_owner), self._path.name
            )
            if (created.device, created.inode) != (
                anchored_entry.device,
                anchored_entry.inode,
            ):
                raise Phase6StoreError(
                    "exclusive Phase-6 directory entry identity differs"
                )
            if created.size != 0:
                raise Phase6StoreError("new Phase-6 database is not empty")
            _phase6_failure_point("after_exclusive_create")
            cleanup = creator.duplicate(
                owner=creator_owner,
                new_owner=cleanup_owner,
                label="Phase-6 cleanup anchor",
            )
            _phase6_failure_point("after_cleanup_dup")
            connection = sqlite3.connect(
                self._fd_uri(creator.fileno(creator_owner), "mode=rw"),
                uri=True,
                timeout=5,
                factory=_AnchoredConnection,
            )
            _phase6_failure_point("after_sqlite_connect_before_transfer")
            connection._adopt_anchors(
                database=creator,
                database_owner=creator_owner,
                parent=parent,
                parent_owner=parent_owner,
            )
            connection._identity = created
            connection._parent_identity = parent_identity
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            digest = _create_schema(connection)
            if _EXPECTED_SCHEMA_DIGEST and digest != _EXPECTED_SCHEMA_DIGEST:
                raise Phase6StoreError("new Phase-6 schema profile differs")
            binding = self._creation_binding(created, parent_identity)
            connection.execute(
                """
                INSERT INTO phase6_shadow_schema_state(
                    singleton,schema_version,store_instance_id,
                    creation_binding_json,schema_digest_sha256
                ) VALUES(1,?,?,?,?)
                """,
                (
                    PHASE6_STORE_SCHEMA,
                    canonical_sha256(binding),
                    _canonical_json(binding),
                    digest,
                ),
            )
            _phase6_failure_point("before_initialize_commit")
            self._commit(connection)
            committed = connection._commit_completed
            _phase6_failure_point("before_parent_fsync")
            os.fsync(connection._parent_anchor.fileno(_CONNECTION_OWNER))
            _phase6_failure_point("after_parent_fsync")
            _phase6_failure_point("after_initialize_commit")
            connection.close()
            connection = None
        except BaseException as primary:
            if connection is not None:
                committed = connection._commit_completed
                _cleanup_failed_connection(
                    connection,
                    label="Phase-6 initialization connection",
                    primary=primary,
                )
            callbacks = []
            cleanup_fd: int | None = None
            if cleanup is not None and not cleanup.closed:
                cleanup_fd = cleanup.fileno(cleanup_owner)
            elif (
                creator is not None
                and not creator.closed
                and creator.owner == creator_owner
            ):
                cleanup_fd = creator.fileno(creator_owner)
            elif creator_raw >= 0:
                try:
                    self._identity_from_fd(creator_raw)
                    cleanup_fd = creator_raw
                except BaseException:
                    cleanup_fd = None
            if created is None and cleanup_fd is not None:
                try:
                    anchored_probe = self._identity_from_fd(cleanup_fd)
                    if parent.closed or parent.owner != parent_owner:
                        raise Phase6StoreError(
                            "failed Phase-6 creation lost its parent cleanup anchor"
                        )
                    named_probe = self._entry_identity(
                        parent.fileno(parent_owner), self._path.name
                    )
                    if anchored_probe != named_probe:
                        raise Phase6StoreError(
                            "failed Phase-6 exclusive creation identity changed"
                        )
                    created = anchored_probe
                except BaseException as probe_error:
                    def report_probe(error: BaseException = probe_error) -> None:
                        raise error

                    run_cleanup(
                        [("identify failed Phase-6 exclusive creation", report_probe)],
                        primary=primary,
                    )
            if not committed and created is not None and cleanup_fd is not None:
                # Reuse the original parent anchor before transfer; after a
                # connection-owned parent has closed, reacquire and reverify it.
                def cleanup_entry() -> None:
                    cleanup_parent_owner = "phase6-cleanup-parent"
                    reuse_parent = (
                        not parent.closed and parent.owner == parent_owner
                    )
                    if reuse_parent:
                        cleanup_parent = parent
                        cleanup_parent_owner = parent_owner
                        cleanup_parent_identity = parent_identity
                    else:
                        cleanup_parent, cleanup_parent_identity = self._open_parent(
                            cleanup_parent_owner
                        )
                    try:
                        self._cleanup_created_entry(
                            parent=cleanup_parent,
                            parent_owner=cleanup_parent_owner,
                            parent_identity=cleanup_parent_identity,
                            cleanup_fd=cleanup_fd,
                            expected=created,
                        )
                    except BaseException as cleanup_primary:
                        if not reuse_parent:
                            run_cleanup(
                                [
                                    (
                                        "close Phase-6 cleanup parent",
                                        cleanup_parent.cleanup(cleanup_parent_owner),
                                    )
                                ],
                                primary=cleanup_primary,
                            )
                        raise
                    else:
                        if not reuse_parent:
                            run_cleanup(
                                [
                                    (
                                        "close Phase-6 cleanup parent",
                                        cleanup_parent.cleanup(
                                            cleanup_parent_owner
                                        ),
                                    )
                                ]
                            )

                callbacks.append(("unlink failed Phase-6 exclusive creation", cleanup_entry))
            if cleanup is not None:
                callbacks.append(
                    ("close Phase-6 cleanup anchor", cleanup.cleanup(cleanup_owner))
                )
            if creator is not None:
                callbacks.append(
                    ("close Phase-6 creator", creator.cleanup(creator_owner))
                )
            if creator_raw >= 0:
                callbacks.append(
                    (
                        "close raw Phase-6 creator",
                        lambda: close_raw_descriptor_if_unowned(
                            creator_raw, creator
                        ),
                    )
                )
            callbacks.append(
                ("close Phase-6 initialize parent", parent.cleanup(parent_owner))
            )
            run_cleanup(callbacks, primary=primary)
            raise
        else:
            callbacks = []
            if cleanup is not None:
                callbacks.append(
                    ("close Phase-6 cleanup anchor", cleanup.cleanup(cleanup_owner))
                )
            if creator is not None:
                callbacks.append(
                    ("close Phase-6 creator", creator.cleanup(creator_owner))
                )
            if creator_raw >= 0:
                callbacks.append(
                    (
                        "close raw Phase-6 creator",
                        lambda: close_raw_descriptor_if_unowned(
                            creator_raw, creator
                        ),
                    )
                )
            callbacks.append(
                ("close Phase-6 initialize parent", parent.cleanup(parent_owner))
            )
            run_cleanup(callbacks)

    def initialize(self) -> None:
        self._validate_parent()
        if os.path.lexists(self._path):
            database, _, parent, _ = self._open_verified()
            run_cleanup(
                [
                    (
                        "close verified Phase-6 database",
                        database.cleanup(database.owner),
                    ),
                    (
                        "close verified Phase-6 parent",
                        parent.cleanup(parent.owner),
                    ),
                ]
            )
            return
        self._initialize_new()
        # Reopen through the full independent read-only preflight.
        database, _, parent, _ = self._open_verified()
        run_cleanup(
            [
                (
                    "close initialized Phase-6 database",
                    database.cleanup(database.owner),
                ),
                (
                    "close initialized Phase-6 parent",
                    parent.cleanup(parent.owner),
                ),
            ]
        )

    @staticmethod
    def _snapshot_row(row: sqlite3.Row) -> VerifiedShadowSnapshot:
        value = _decode_exact_json(row["snapshot_json"], "snapshot")
        snapshot = _snapshot_from_dict(value)
        if (
            row["snapshot_id"] != snapshot.snapshot_id
            or row["snapshot_sha256"] != snapshot.snapshot_id
            or row["workflow_id"] != snapshot.workflow_id
            or row["project_id"] != snapshot.project_id
            or row["snapshot_sequence"] != snapshot.snapshot_sequence
            or row["authority_revision"]
            != snapshot.source_binding.authority_coordinate["current_revision"]
        ):
            raise Phase6StoreError("snapshot row columns differ from canonical fact")
        return snapshot

    @staticmethod
    def _grant_chain_key(grant: ShadowScopedGrant) -> str:
        return canonical_sha256(
            {
                "schema_version": "phase6-shadow-grant-chain-key-v1",
                "workflow_id": grant.workflow_id,
                "project_id": grant.project_id,
                "subject_type": grant.subject_type,
                "subject_id": grant.subject_id,
                "subject_generation": grant.subject_generation,
                "scope": grant.scope.value,
                "scope_key": grant.scope_key,
            }
        )

    @staticmethod
    def _grant_row(
        row: sqlite3.Row, snapshots: Mapping[str, VerifiedShadowSnapshot]
    ) -> ShadowScopedGrant:
        snapshot = snapshots.get(str(row["snapshot_id"]))
        if snapshot is None:
            raise Phase6StoreError("grant refers to an unavailable snapshot")
        value = _decode_exact_json(row["grant_json"], "grant")
        grant = _grant_from_dict(value, snapshot)
        if (
            row["grant_id"] != grant.grant_id
            or row["grant_sha256"] != grant.grant_id
            or row["grant_sequence"] != grant.grant_sequence
            or row["grant_chain_key"] != Phase6SnapshotGrantStore._grant_chain_key(grant)
        ):
            raise Phase6StoreError("grant row columns differ from canonical fact")
        return grant

    @staticmethod
    def _lifecycle_row(
        row: sqlite3.Row, grants: Mapping[str, ShadowScopedGrant]
    ) -> GrantLifecycleReceipt:
        grant = grants.get(str(row["grant_id"]))
        if grant is None:
            raise Phase6StoreError("lifecycle receipt refers to an unavailable grant")
        value = _decode_exact_json(row["receipt_json"], "lifecycle receipt")
        receipt = _lifecycle_from_dict(value, grant)
        if (
            row["receipt_sha256"] != receipt.receipt_sha256
            or row["receipt_sequence"] != receipt.receipt_sequence
            or row["effective_at"] != receipt.effective_at
        ):
            raise Phase6StoreError("lifecycle row columns differ from canonical receipt")
        return receipt

    def _verify_integrity(self, connection: sqlite3.Connection) -> None:
        """Reconstruct every immutable chain and every current/reference edge."""

        snapshot_rows = connection.execute(
            "SELECT * FROM phase6_shadow_snapshot_facts "
            "ORDER BY workflow_id,project_id,snapshot_sequence"
        ).fetchall()
        snapshots: dict[str, VerifiedShadowSnapshot] = {}
        snapshot_chains: dict[tuple[str, str], list[VerifiedShadowSnapshot]] = {}
        for row in snapshot_rows:
            snapshot = self._snapshot_row(row)
            if snapshot.snapshot_id in snapshots:
                raise Phase6StoreError("duplicate snapshot identity")
            snapshots[snapshot.snapshot_id] = snapshot
            snapshot_chains.setdefault(
                (snapshot.workflow_id, snapshot.project_id), []
            ).append(snapshot)
        for chain in snapshot_chains.values():
            for index, snapshot in enumerate(chain):
                if snapshot.snapshot_sequence != index + 1:
                    raise Phase6StoreError("snapshot sequence is not contiguous")
                expected = None if index == 0 else chain[index - 1].snapshot_id
                if snapshot.previous_snapshot_id != expected:
                    raise Phase6StoreError("snapshot predecessor chain differs")
                if index and (
                    snapshot.source_binding.authority_coordinate["current_revision"]
                    <= chain[index - 1].source_binding.authority_coordinate["current_revision"]
                ):
                    raise Phase6StoreError("snapshot Authority revisions are not increasing")
        current_rows = connection.execute(
            "SELECT * FROM phase6_shadow_snapshot_current ORDER BY workflow_id,project_id"
        ).fetchall()
        if len(current_rows) != len(snapshot_chains):
            raise Phase6StoreError("snapshot current projection cardinality differs")
        for row in current_rows:
            chain = snapshot_chains.get((str(row["workflow_id"]), str(row["project_id"])))
            if not chain:
                raise Phase6StoreError("snapshot current projection has no fact chain")
            head = chain[-1]
            if (
                row["snapshot_id"] != head.snapshot_id
                or row["snapshot_sequence"] != head.snapshot_sequence
                or row["authority_revision"]
                != head.source_binding.authority_coordinate["current_revision"]
            ):
                raise Phase6StoreError("snapshot current projection differs")

        grant_rows = connection.execute(
            "SELECT * FROM phase6_shadow_grant_facts ORDER BY grant_chain_key,grant_sequence"
        ).fetchall()
        grants: dict[str, ShadowScopedGrant] = {}
        grant_chains: dict[str, list[ShadowScopedGrant]] = {}
        for row in grant_rows:
            grant = self._grant_row(row, snapshots)
            grants[grant.grant_id] = grant
            grant_chains.setdefault(str(row["grant_chain_key"]), []).append(grant)
        for chain in grant_chains.values():
            for index, grant in enumerate(chain):
                if grant.grant_sequence != index + 1:
                    raise Phase6StoreError("grant sequence is not contiguous")
                expected = None if index == 0 else chain[index - 1].grant_id
                if grant.previous_grant_id != expected:
                    raise Phase6StoreError("grant predecessor chain differs")

        lifecycle_rows = connection.execute(
            "SELECT * FROM phase6_shadow_grant_lifecycle_facts "
            "ORDER BY grant_id,receipt_sequence"
        ).fetchall()
        lifecycles: dict[str, GrantLifecycleReceipt] = {}
        lifecycle_chains: dict[str, list[GrantLifecycleReceipt]] = {}
        for row in lifecycle_rows:
            receipt = self._lifecycle_row(row, grants)
            lifecycles[receipt.receipt_sha256] = receipt
            lifecycle_chains.setdefault(receipt.grant_id, []).append(receipt)
        if set(lifecycle_chains) != set(grants):
            raise Phase6StoreError("grant lifecycle cardinality differs")
        for grant_id, chain in lifecycle_chains.items():
            for index, receipt in enumerate(chain):
                if receipt.receipt_sequence != index + 1:
                    raise Phase6StoreError("lifecycle receipt sequence differs")
                expected = None if index == 0 else chain[index - 1].receipt_sha256
                if receipt.previous_receipt_sha256 != expected:
                    raise Phase6StoreError("lifecycle predecessor chain differs")
                if index and receipt.before_status != chain[index - 1].after_status.value:
                    raise Phase6StoreError("lifecycle before status differs")
                if index and receipt.effective_at < chain[index - 1].effective_at:
                    raise Phase6StoreError("lifecycle logical time decreased")
        grant_current_rows = connection.execute(
            "SELECT * FROM phase6_shadow_grant_current ORDER BY grant_id"
        ).fetchall()
        if len(grant_current_rows) != len(grants):
            raise Phase6StoreError("grant current projection cardinality differs")
        for row in grant_current_rows:
            chain = lifecycle_chains.get(str(row["grant_id"]))
            if not chain:
                raise Phase6StoreError("grant current projection lacks lifecycle")
            head = chain[-1]
            if (
                row["status"] != head.after_status.value
                or row["receipt_sequence"] != head.receipt_sequence
                or row["lifecycle_receipt_sha256"] != head.receipt_sha256
            ):
                raise Phase6StoreError("grant current projection differs")
            last_evaluated = row["last_evaluated_at"]
            if last_evaluated is not None and (
                type(last_evaluated) is not int or last_evaluated < 0
            ):
                raise Phase6StoreError("grant evaluation time projection is malformed")

        evaluations: dict[str, GrantEvaluationReceipt] = {}
        evaluation_rows = connection.execute(
            "SELECT * FROM phase6_shadow_evaluation_receipts ORDER BY evaluation_id"
        ).fetchall()
        max_evaluated: dict[str, int] = {}
        for row in evaluation_rows:
            grant = grants.get(str(row["grant_id"]))
            if grant is None:
                raise Phase6StoreError("evaluation refers to an unavailable grant")
            value = _decode_exact_json(row["evaluation_json"], "evaluation receipt")
            if type(value) is not dict:
                raise Phase6StoreError("evaluation receipt is malformed")
            lifecycle = lifecycles.get(str(value.get("observed_lifecycle_receipt_sha256")))
            if lifecycle is None or lifecycle.grant_id != grant.grant_id:
                raise Phase6StoreError("evaluation observes an unavailable lifecycle receipt")
            receipt = _evaluation_from_dict(value, grant, lifecycle)
            if (
                row["evaluation_id"] != receipt.evaluation_id
                or row["evaluation_sha256"] != receipt.evaluation_id
                or row["evaluated_at"] != receipt.evaluated_at
            ):
                raise Phase6StoreError("evaluation row columns differ")
            evaluations[receipt.evaluation_id] = receipt
            max_evaluated[grant.grant_id] = max(
                max_evaluated.get(grant.grant_id, -1), receipt.evaluated_at
            )
        for row in grant_current_rows:
            expected_last = max_evaluated.get(str(row["grant_id"]))
            if row["last_evaluated_at"] != expected_last:
                raise Phase6StoreError("grant last evaluation projection differs")

        idempotency_rows = connection.execute(
            "SELECT * FROM phase6_shadow_idempotency ORDER BY idempotency_key"
        ).fetchall()
        decoded_idempotency: list[
            tuple[sqlite3.Row, dict[str, object], dict[str, object]]
        ] = []
        for row in idempotency_rows:
            request = _decode_canonical(
                row["request_json"], row["request_sha256"], "request"
            )
            result = _decode_canonical(
                row["result_json"],
                row["result_sha256"],
                "idempotency result",
            )
            if type(request) is not dict or type(result) is not dict:
                raise Phase6StoreError("idempotency request or result is malformed")
            if row["request_schema"] != request.get("schema_version"):
                raise Phase6StoreError("idempotency request schema differs")
            decoded_idempotency.append((row, request, result))

        expected_kind = {
            "snapshot": "snapshot",
            "grant": "grant",
            "lifecycle": "lifecycle",
            "evaluation": "evaluation",
        }
        for row, request, result in decoded_idempotency:
            key = row["idempotency_key"]
            domain = row["request_domain"]
            kind = row["result_kind"]
            result_id = row["result_id"]
            if (
                type(key) is not str
                or domain not in expected_kind
                or kind != expected_kind[domain]
                or type(result_id) is not str
            ):
                raise Phase6StoreError("idempotency domain, kind or key is malformed")
            try:
                _sha(result_id, "idempotency.result_id")
                _nonnegative(row["created_at"], "idempotency.created_at")
            except Phase6ContractError as exc:
                raise Phase6StoreError("idempotency columns are malformed") from exc

            expected_request: dict[str, object]
            expected_result: dict[str, object]
            expected_created_at: int
            if domain == "snapshot":
                target = snapshots.get(result_id)
                if target is None:
                    raise Phase6StoreError("idempotency snapshot target is unavailable")
                expected_request = {
                    "schema_version": "phase6-append-verified-snapshot-request-v1",
                    "source_binding": target.source_binding.as_dict(),
                    "sections": [section.as_dict() for section in target.sections],
                    "captured_at": target.captured_at,
                    "valid_until": target.valid_until,
                    "expected_previous_snapshot_id": target.previous_snapshot_id,
                }
                expected_result = target.as_dict()
                expected_created_at = target.captured_at
            elif domain == "grant":
                target = grants.get(result_id)
                if target is None:
                    raise Phase6StoreError("idempotency grant target is unavailable")
                chain = lifecycle_chains.get(target.grant_id)
                if not chain:
                    raise Phase6StoreError("grant idempotency lacks issuance lifecycle")
                issuance = chain[0]
                expected_request = {
                    "schema_version": "phase6-issue-shadow-scoped-grant-request-v1",
                    "snapshot_id": target.snapshot_id,
                    "subject_type": target.subject_type,
                    "subject_id": target.subject_id,
                    "subject_generation": target.subject_generation,
                    "scope": target.scope.value,
                    "scope_key": target.scope_key,
                    "issuer_id": target.issuer_id,
                    "issuer_generation": target.issuer_generation,
                    "issuer_evidence_schema": target.issuer_evidence_schema,
                    "issuer_receipt_sha256": target.issuer_receipt_sha256,
                    "issued_at": target.issued_at,
                    "not_before": target.not_before,
                    "expires_at": target.expires_at,
                    "expected_previous_grant_id": target.previous_grant_id,
                }
                expected_result = {
                    "schema_version": "phase6-grant-commit-result-v1",
                    "grant_id": target.grant_id,
                    "receipt_sha256": issuance.receipt_sha256,
                }
                expected_created_at = target.issued_at
                if (
                    issuance.request_schema != expected_request["schema_version"]
                    or issuance.request_sha256 != canonical_sha256(expected_request)
                ):
                    raise Phase6StoreError(
                        "grant issuance lifecycle request binding differs"
                    )
            elif domain == "lifecycle":
                target = lifecycles.get(result_id)
                if target is None:
                    raise Phase6StoreError("idempotency lifecycle target is unavailable")
                if target.event == "revoked":
                    expected_request = {
                        "schema_version": "phase6-revoke-shadow-grant-request-v1",
                        "grant_id": target.grant_id,
                        "actor_id": target.actor_id,
                        "actor_generation": target.actor_generation,
                        "reason_code": target.reason_code,
                        "effective_at": target.effective_at,
                    }
                elif target.event == "expired":
                    expected_request = dict(request)
                    if set(expected_request) != {
                        "schema_version",
                        "grant_id",
                        "evaluated_at",
                        "evaluation_request_sha256",
                    } or (
                        expected_request.get("schema_version")
                        != "phase6-materialize-shadow-grant-expiry-request-v1"
                        or expected_request.get("grant_id") != target.grant_id
                        or expected_request.get("evaluated_at") != target.effective_at
                    ):
                        raise Phase6StoreError("expiry idempotency request differs")
                    try:
                        _sha(
                            expected_request.get("evaluation_request_sha256"),
                            "evaluation_request_sha256",
                        )
                    except Phase6ContractError as exc:
                        raise Phase6StoreError(
                            "expiry evaluation request identity is malformed"
                        ) from exc
                else:
                    raise Phase6StoreError(
                        "issued lifecycle cannot have a lifecycle idempotency row"
                    )
                expected_result = target.as_dict()
                expected_created_at = target.effective_at
                if (
                    target.request_schema != expected_request["schema_version"]
                    or target.request_sha256 != canonical_sha256(expected_request)
                ):
                    raise Phase6StoreError("lifecycle request binding differs")
            else:
                target = evaluations.get(result_id)
                if target is None:
                    raise Phase6StoreError("idempotency evaluation target is unavailable")
                expected_request = {
                    "schema_version": "phase6-evaluate-shadow-grant-request-v1",
                    "grant_id": target.grant_id,
                    "subject_type": target.subject_type,
                    "subject_id": target.subject_id,
                    "subject_generation": target.subject_generation,
                    "requested_scope": target.requested_scope.value,
                    "requested_scope_key": target.requested_scope_key,
                    "evaluated_at": target.evaluated_at,
                }
                expected_result = target.as_dict()
                expected_created_at = target.evaluated_at

            if request != expected_request:
                raise Phase6StoreError("idempotency request differs from its target")
            if result != expected_result:
                raise Phase6StoreError("idempotency result differs from its target")
            if row["created_at"] != expected_created_at:
                raise Phase6StoreError("idempotency logical creation time differs")
            if key.startswith(_INTERNAL_KEY_PREFIX):
                if (
                    domain != "lifecycle"
                    or request.get("schema_version")
                    != "phase6-materialize-shadow-grant-expiry-request-v1"
                ):
                    raise Phase6StoreError("internal idempotency namespace is misused")
                evaluation_request_sha = request["evaluation_request_sha256"]
                matching_base = any(
                    candidate_row["request_domain"] == "evaluation"
                    and candidate_row["request_sha256"] == evaluation_request_sha
                    and _internal_key(
                        base_key=candidate_row["idempotency_key"],
                        object_id=result["grant_id"],
                        stage="expire",
                    )
                    == key
                    for candidate_row, _, _ in decoded_idempotency
                )
                if not matching_base:
                    raise Phase6StoreError("internal idempotency key binding differs")
            else:
                try:
                    _caller_key(key)
                except Phase6ContractError as exc:
                    raise Phase6StoreError("caller idempotency key is malformed") from exc

    @staticmethod
    def _load_snapshot(
        connection: sqlite3.Connection, snapshot_id: str
    ) -> VerifiedShadowSnapshot:
        row = connection.execute(
            "SELECT * FROM phase6_shadow_snapshot_facts WHERE snapshot_id=?",
            (_sha(snapshot_id, "snapshot_id"),),
        ).fetchone()
        if row is None:
            raise Phase6SnapshotNotFound("verified snapshot is unavailable")
        return Phase6SnapshotGrantStore._snapshot_row(row)

    @staticmethod
    def _load_grant(
        connection: sqlite3.Connection, grant_id: str
    ) -> ShadowScopedGrant:
        row = connection.execute(
            "SELECT * FROM phase6_shadow_grant_facts WHERE grant_id=?",
            (_sha(grant_id, "grant_id"),),
        ).fetchone()
        if row is None:
            raise Phase6GrantConflict("shadow grant is unavailable")
        snapshot = Phase6SnapshotGrantStore._load_snapshot(connection, str(row["snapshot_id"]))
        return Phase6SnapshotGrantStore._grant_row(row, {snapshot.snapshot_id: snapshot})

    @staticmethod
    def _load_lifecycle(
        connection: sqlite3.Connection, grant: ShadowScopedGrant
    ) -> GrantLifecycleReceipt:
        row = connection.execute(
            """
            SELECT f.* FROM phase6_shadow_grant_current c
            JOIN phase6_shadow_grant_lifecycle_facts f
              ON f.receipt_sha256=c.lifecycle_receipt_sha256
            WHERE c.grant_id=?
            """,
            (grant.grant_id,),
        ).fetchone()
        if row is None:
            raise Phase6StoreError("grant lifecycle head is unavailable")
        return Phase6SnapshotGrantStore._lifecycle_row(row, {grant.grant_id: grant})

    @staticmethod
    def _replay_row(
        connection: sqlite3.Connection,
        *,
        key: str,
        domain: str,
        request_sha256: str,
    ) -> sqlite3.Row | None:
        row = connection.execute(
            "SELECT * FROM phase6_shadow_idempotency WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if row["request_domain"] != domain or row["request_sha256"] != request_sha256:
            raise Phase6IdempotencyConflict(
                "Phase-6 idempotency key is bound to another domain or request"
            )
        _decode_canonical(row["request_json"], row["request_sha256"], "replay request")
        _decode_canonical(row["result_json"], row["result_sha256"], "replay result")
        return row

    @staticmethod
    def _insert_idempotency(
        connection: sqlite3.Connection,
        *,
        key: str,
        domain: str,
        request_schema: str,
        request: dict[str, object],
        result_kind: str,
        result_id: str,
        result: dict[str, object],
        created_at: int,
    ) -> None:
        request_json = _canonical_json(request)
        result_json = _canonical_json(result)
        connection.execute(
            """
            INSERT INTO phase6_shadow_idempotency(
                idempotency_key,request_domain,request_schema,request_json,
                request_sha256,result_kind,result_id,result_json,result_sha256,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                key,
                domain,
                request_schema,
                request_json,
                canonical_sha256(request),
                result_kind,
                result_id,
                result_json,
                canonical_sha256(result),
                created_at,
            ),
        )
        _phase6_failure_point(f"after_{domain}_idempotency")

    def append_snapshot(
        self,
        *,
        source_binding: AuthoritySourceBinding,
        sections: Iterable[VerifiedSection],
        captured_at: int,
        valid_until: int,
        expected_previous_snapshot_id: str | None,
        idempotency_key: str,
    ) -> SnapshotCommitResult:
        """Atomically append one immutable verified snapshot and advance its head."""

        key = _caller_key(idempotency_key)
        checked_sections = _sections(sections)
        request_schema = "phase6-append-verified-snapshot-request-v1"
        request = {
            "schema_version": request_schema,
            "source_binding": source_binding.as_dict(),
            "sections": [item.as_dict() for item in checked_sections],
            "captured_at": _nonnegative(captured_at, "captured_at"),
            "valid_until": _positive(valid_until, "valid_until"),
            "expected_previous_snapshot_id": expected_previous_snapshot_id,
        }
        if expected_previous_snapshot_id is not None:
            _sha(expected_previous_snapshot_id, "expected_previous_snapshot_id")
        request_sha = canonical_sha256(request)
        connection = self._connect()
        try:
            self._begin(connection, immediate=True)
            replay = self._replay_row(
                connection, key=key, domain="snapshot", request_sha256=request_sha
            )
            if replay is not None:
                snapshot = self._load_snapshot(connection, str(replay["result_id"]))
                self._commit(connection)
                result = SnapshotCommitResult(snapshot, True)
            else:
                coordinate = _source_binding_from_dict(
                    source_binding.as_dict()
                ).authority_coordinate
                current = connection.execute(
                    """
                    SELECT f.* FROM phase6_shadow_snapshot_current c
                    JOIN phase6_shadow_snapshot_facts f ON f.snapshot_id=c.snapshot_id
                    WHERE c.workflow_id=? AND c.project_id=?
                    """,
                    (coordinate["workflow_id"], coordinate["project_id"]),
                ).fetchone()
                previous: VerifiedShadowSnapshot | None = None
                if current is not None:
                    previous = self._snapshot_row(current)
                snapshot: VerifiedShadowSnapshot
                same_revision_replay = False
                if previous is not None:
                    previous_coordinate = previous.source_binding.authority_coordinate
                    for field in (
                        "workflow_id", "project_id", "project_generation", "run_generation",
                        "runtime_generation", "scheduler_generation", "source_fence_sha256",
                        "contract_pin_set_sha256",
                    ):
                        if previous_coordinate[field] != coordinate[field]:
                            raise Phase6SnapshotConflict(
                                f"snapshot chain {field} crossover is forbidden"
                            )
                    old_revision = int(previous_coordinate["current_revision"])
                    new_revision = int(coordinate["current_revision"])
                    if new_revision < old_revision:
                        raise Phase6SnapshotConflict("Authority revision decreased")
                    if request["captured_at"] < previous.captured_at:
                        raise Phase6SnapshotConflict(
                            "snapshot capture logical time decreased"
                        )
                    if new_revision == old_revision:
                        # A same-revision fact is never a new lineage element.
                        # Exact bytes can only replay the already stored fact;
                        # otherwise it is a conflict.
                        if (
                            expected_previous_snapshot_id
                            != previous.previous_snapshot_id
                        ):
                            raise Phase6SnapshotConflict(
                                "same-revision replay predecessor differs"
                            )
                        replay_candidate = _build_snapshot(
                            source_binding=source_binding,
                            snapshot_sequence=previous.snapshot_sequence,
                            previous_snapshot_id=previous.previous_snapshot_id,
                            captured_at=request["captured_at"],
                            valid_until=request["valid_until"],
                            sections=checked_sections,
                        )
                        if replay_candidate.snapshot_id != previous.snapshot_id:
                            raise Phase6SnapshotConflict(
                                "same Authority revision has different canonical snapshot bytes"
                            )
                        snapshot = previous
                        same_revision_replay = True
                if not same_revision_replay:
                    if (
                        None if previous is None else previous.snapshot_id
                    ) != expected_previous_snapshot_id:
                        raise Phase6SnapshotConflict(
                            "snapshot predecessor is not the current head"
                        )
                    sequence = 1 if previous is None else previous.snapshot_sequence + 1
                    snapshot = _build_snapshot(
                        source_binding=source_binding,
                        snapshot_sequence=sequence,
                        previous_snapshot_id=expected_previous_snapshot_id,
                        captured_at=request["captured_at"],
                        valid_until=request["valid_until"],
                        sections=checked_sections,
                    )
                existing = connection.execute(
                    "SELECT snapshot_json FROM phase6_shadow_snapshot_facts WHERE snapshot_id=?",
                    (snapshot.snapshot_id,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO phase6_shadow_snapshot_facts(
                            snapshot_id,workflow_id,project_id,snapshot_sequence,
                            authority_revision,snapshot_json,snapshot_sha256
                        ) VALUES(?,?,?,?,?,?,?)
                        """,
                        (
                            snapshot.snapshot_id,
                            snapshot.workflow_id,
                            snapshot.project_id,
                            snapshot.snapshot_sequence,
                            coordinate["current_revision"],
                            _canonical_json(snapshot.as_dict()),
                            snapshot.snapshot_id,
                        ),
                    )
                    _phase6_failure_point("after_snapshot_fact")
                    if previous is None:
                        connection.execute(
                            """
                            INSERT INTO phase6_shadow_snapshot_current(
                                workflow_id,project_id,snapshot_id,snapshot_sequence,
                                authority_revision,updated_at
                            ) VALUES(?,?,?,?,?,?)
                            """,
                            (
                                snapshot.workflow_id,
                                snapshot.project_id,
                                snapshot.snapshot_id,
                                snapshot.snapshot_sequence,
                                coordinate["current_revision"],
                                snapshot.captured_at,
                            ),
                        )
                    else:
                        cursor = connection.execute(
                            """
                            UPDATE phase6_shadow_snapshot_current
                            SET snapshot_id=?,snapshot_sequence=?,authority_revision=?,updated_at=?
                            WHERE workflow_id=? AND project_id=? AND snapshot_id=?
                            """,
                            (
                                snapshot.snapshot_id,
                                snapshot.snapshot_sequence,
                                coordinate["current_revision"],
                                snapshot.captured_at,
                                snapshot.workflow_id,
                                snapshot.project_id,
                                previous.snapshot_id,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise Phase6SnapshotConflict("snapshot current-head CAS failed")
                    _phase6_failure_point("after_snapshot_current")
                self._insert_idempotency(
                    connection,
                    key=key,
                    domain="snapshot",
                    request_schema=request_schema,
                    request=request,
                    result_kind="snapshot",
                    result_id=snapshot.snapshot_id,
                    result=snapshot.as_dict(),
                    created_at=snapshot.captured_at,
                )
                _phase6_failure_point("before_snapshot_commit")
                self._commit(connection)
                _phase6_failure_point("after_snapshot_commit")
                result = SnapshotCommitResult(snapshot, snapshot is previous)
            connection.close()
        except BaseException as primary:
            _cleanup_failed_connection(
                connection,
                label="Phase-6 snapshot append",
                primary=primary,
            )
            raise
        return result

    def load_snapshot(self, snapshot_id: str) -> VerifiedShadowSnapshot:
        connection = self._connect()
        try:
            self._begin(connection, immediate=False)
            snapshot = self._load_snapshot(connection, snapshot_id)
            self._commit(connection)
            connection.close()
        except BaseException as primary:
            _cleanup_failed_connection(
                connection,
                label="Phase-6 snapshot load",
                primary=primary,
            )
            raise
        return snapshot

    def current_snapshot(
        self, *, workflow_id: str, project_id: str
    ) -> VerifiedShadowSnapshot:
        workflow = _identifier(workflow_id, "workflow_id")
        project = _identifier(project_id, "project_id")
        connection = self._connect()
        try:
            self._begin(connection, immediate=False)
            row = connection.execute(
                """
                SELECT f.* FROM phase6_shadow_snapshot_current c
                JOIN phase6_shadow_snapshot_facts f ON f.snapshot_id=c.snapshot_id
                WHERE c.workflow_id=? AND c.project_id=?
                """,
                (workflow, project),
            ).fetchone()
            if row is None:
                raise Phase6SnapshotNotFound("current verified snapshot is unavailable")
            snapshot = self._snapshot_row(row)
            self._commit(connection)
            connection.close()
        except BaseException as primary:
            _cleanup_failed_connection(
                connection,
                label="Phase-6 current load",
                primary=primary,
            )
            raise
        return snapshot

    @staticmethod
    def _load_lifecycle_by_id(
        connection: sqlite3.Connection,
        grant: ShadowScopedGrant,
        receipt_sha256: str,
    ) -> GrantLifecycleReceipt:
        row = connection.execute(
            "SELECT * FROM phase6_shadow_grant_lifecycle_facts WHERE receipt_sha256=?",
            (_sha(receipt_sha256, "receipt_sha256"),),
        ).fetchone()
        if row is None:
            raise Phase6StoreError("lifecycle receipt is unavailable")
        return Phase6SnapshotGrantStore._lifecycle_row(row, {grant.grant_id: grant})

    def issue_grant(
        self,
        *,
        snapshot_id: str,
        subject_type: str,
        subject_id: str,
        subject_generation: str,
        scope: GrantScope,
        scope_key: str | None,
        issuer_id: str,
        issuer_generation: str,
        issuer_evidence_schema: str,
        issuer_receipt_sha256: str,
        issued_at: int,
        not_before: int,
        expires_at: int,
        expected_previous_grant_id: str | None,
        idempotency_key: str,
    ) -> GrantCommitResult:
        """Issue an immutable, closed-scope, current-snapshot-only shadow grant."""

        key = _caller_key(idempotency_key)
        if type(scope) is not GrantScope:
            raise Phase6ContractError("scope must be GrantScope")
        request_schema = "phase6-issue-shadow-scoped-grant-request-v1"
        request: dict[str, object] = {
            "schema_version": request_schema,
            "snapshot_id": _sha(snapshot_id, "snapshot_id"),
            "subject_type": _identifier(subject_type, "subject_type"),
            "subject_id": _identifier(subject_id, "subject_id"),
            "subject_generation": _identifier(subject_generation, "subject_generation"),
            "scope": scope.value,
            "scope_key": scope_key,
            "issuer_id": _identifier(issuer_id, "issuer_id"),
            "issuer_generation": _identifier(issuer_generation, "issuer_generation"),
            "issuer_evidence_schema": _identifier(
                issuer_evidence_schema, "issuer_evidence_schema"
            ),
            "issuer_receipt_sha256": _sha(
                issuer_receipt_sha256, "issuer_receipt_sha256"
            ),
            "issued_at": _nonnegative(issued_at, "issued_at"),
            "not_before": _nonnegative(not_before, "not_before"),
            "expires_at": _positive(expires_at, "expires_at"),
            "expected_previous_grant_id": expected_previous_grant_id,
        }
        if expected_previous_grant_id is not None:
            _sha(expected_previous_grant_id, "expected_previous_grant_id")
        if scope is GrantScope.SECTION_VIEW:
            _identifier(scope_key, "scope_key")
        elif scope_key is not None:
            raise Phase6ContractError("only section:view accepts scope_key")
        request_sha = canonical_sha256(request)
        connection = self._connect()
        try:
            self._begin(connection, immediate=True)
            replay = self._replay_row(
                connection, key=key, domain="grant", request_sha256=request_sha
            )
            if replay is not None:
                grant = self._load_grant(connection, str(replay["result_id"]))
                replay_value = _decode_canonical(
                    replay["result_json"], replay["result_sha256"], "grant replay"
                )
                if type(replay_value) is not dict:
                    raise Phase6StoreError("grant replay result is malformed")
                lifecycle = self._load_lifecycle_by_id(
                    connection, grant, str(replay_value.get("receipt_sha256"))
                )
                self._commit(connection)
                result = GrantCommitResult(grant, lifecycle, True)
            else:
                snapshot = self._load_snapshot(connection, request["snapshot_id"])
                if not snapshot.source_binding.eligible:
                    raise Phase6GrantConflict("ineligible source snapshot cannot issue a grant")
                current = connection.execute(
                    """
                    SELECT snapshot_id FROM phase6_shadow_snapshot_current
                    WHERE workflow_id=? AND project_id=?
                    """,
                    (snapshot.workflow_id, snapshot.project_id),
                ).fetchone()
                if current is None or current["snapshot_id"] != snapshot.snapshot_id:
                    raise Phase6GrantConflict("only the current snapshot may issue a grant")
                chain_body = {
                    "schema_version": "phase6-shadow-grant-chain-key-v1",
                    "workflow_id": snapshot.workflow_id,
                    "project_id": snapshot.project_id,
                    "subject_type": request["subject_type"],
                    "subject_id": request["subject_id"],
                    "subject_generation": request["subject_generation"],
                    "scope": scope.value,
                    "scope_key": scope_key,
                }
                chain_key = canonical_sha256(chain_body)
                previous_row = connection.execute(
                    """
                    SELECT * FROM phase6_shadow_grant_facts
                    WHERE grant_chain_key=? ORDER BY grant_sequence DESC LIMIT 1
                    """,
                    (chain_key,),
                ).fetchone()
                previous: ShadowScopedGrant | None = None
                if previous_row is not None:
                    previous_snapshot = self._load_snapshot(
                        connection, str(previous_row["snapshot_id"])
                    )
                    previous = self._grant_row(
                        previous_row,
                        {previous_snapshot.snapshot_id: previous_snapshot},
                    )
                if (None if previous is None else previous.grant_id) != expected_previous_grant_id:
                    raise Phase6GrantConflict("grant predecessor is not the chain head")
                if previous is not None and request["issued_at"] < previous.issued_at:
                    raise Phase6GrantConflict("grant issuance logical time decreased")
                grant = _build_grant(
                    snapshot=snapshot,
                    grant_sequence=1 if previous is None else previous.grant_sequence + 1,
                    previous_grant_id=expected_previous_grant_id,
                    subject_type=request["subject_type"],
                    subject_id=request["subject_id"],
                    subject_generation=request["subject_generation"],
                    scope=scope,
                    scope_key=scope_key,
                    issuer_id=request["issuer_id"],
                    issuer_generation=request["issuer_generation"],
                    issuer_evidence_schema=request["issuer_evidence_schema"],
                    issuer_receipt_sha256=request["issuer_receipt_sha256"],
                    issued_at=request["issued_at"],
                    not_before=request["not_before"],
                    expires_at=request["expires_at"],
                )
                lifecycle = _build_lifecycle(
                    grant=grant,
                    receipt_sequence=1,
                    previous_receipt_sha256=None,
                    event="issued",
                    before_status=None,
                    after_status=GrantStatus.ACTIVE,
                    actor_id=grant.issuer_id,
                    actor_generation=grant.issuer_generation,
                    reason_code="ISSUED_SHADOW",
                    effective_at=grant.issued_at,
                    request_schema=request_schema,
                    request_sha256=request_sha,
                )
                connection.execute(
                    """
                    INSERT INTO phase6_shadow_grant_facts(
                        grant_id,snapshot_id,grant_sequence,grant_chain_key,
                        grant_json,grant_sha256
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        grant.grant_id,
                        grant.snapshot_id,
                        grant.grant_sequence,
                        chain_key,
                        _canonical_json(grant.as_dict()),
                        grant.grant_id,
                    ),
                )
                _phase6_failure_point("after_grant_fact")
                connection.execute(
                    """
                    INSERT INTO phase6_shadow_grant_lifecycle_facts(
                        receipt_sha256,grant_id,receipt_sequence,receipt_json,effective_at
                    ) VALUES(?,?,?,?,?)
                    """,
                    (
                        lifecycle.receipt_sha256,
                        grant.grant_id,
                        lifecycle.receipt_sequence,
                        _canonical_json(lifecycle.as_dict()),
                        lifecycle.effective_at,
                    ),
                )
                _phase6_failure_point("after_grant_lifecycle")
                connection.execute(
                    """
                    INSERT INTO phase6_shadow_grant_current(
                        grant_id,status,receipt_sequence,lifecycle_receipt_sha256,
                        last_evaluated_at,updated_at
                    ) VALUES(?,?,?,?,NULL,?)
                    """,
                    (
                        grant.grant_id,
                        GrantStatus.ACTIVE.value,
                        1,
                        lifecycle.receipt_sha256,
                        lifecycle.effective_at,
                    ),
                )
                _phase6_failure_point("after_grant_current")
                result_value = {
                    "schema_version": "phase6-grant-commit-result-v1",
                    "grant_id": grant.grant_id,
                    "receipt_sha256": lifecycle.receipt_sha256,
                }
                self._insert_idempotency(
                    connection,
                    key=key,
                    domain="grant",
                    request_schema=request_schema,
                    request=request,
                    result_kind="grant",
                    result_id=grant.grant_id,
                    result=result_value,
                    created_at=grant.issued_at,
                )
                _phase6_failure_point("before_grant_commit")
                self._commit(connection)
                _phase6_failure_point("after_grant_commit")
                result = GrantCommitResult(grant, lifecycle, False)
            connection.close()
        except BaseException as primary:
            _cleanup_failed_connection(
                connection,
                label="Phase-6 grant issuance",
                primary=primary,
            )
            raise
        return result

    def revoke_grant(
        self,
        grant_id: str,
        *,
        actor_id: str,
        actor_generation: str,
        reason_code: str,
        effective_at: int,
        idempotency_key: str,
    ) -> LifecycleCommitResult:
        """Irreversibly revoke an active or expired grant."""

        key = _caller_key(idempotency_key)
        request_schema = "phase6-revoke-shadow-grant-request-v1"
        request: dict[str, object] = {
            "schema_version": request_schema,
            "grant_id": _sha(grant_id, "grant_id"),
            "actor_id": _identifier(actor_id, "actor_id"),
            "actor_generation": _identifier(actor_generation, "actor_generation"),
            "reason_code": _identifier(reason_code, "reason_code"),
            "effective_at": _nonnegative(effective_at, "effective_at"),
        }
        request_sha = canonical_sha256(request)
        connection = self._connect()
        try:
            self._begin(connection, immediate=True)
            replay = self._replay_row(
                connection, key=key, domain="lifecycle", request_sha256=request_sha
            )
            if replay is not None:
                grant = self._load_grant(connection, request["grant_id"])
                lifecycle = self._load_lifecycle_by_id(
                    connection, grant, str(replay["result_id"])
                )
                self._commit(connection)
                result = LifecycleCommitResult(grant, lifecycle, True)
            else:
                grant = self._load_grant(connection, request["grant_id"])
                current = self._load_lifecycle(connection, grant)
                if current.after_status is GrantStatus.REVOKED:
                    raise Phase6GrantConflict("grant is already revoked")
                if request["effective_at"] < current.effective_at:
                    raise Phase6GrantConflict("grant lifecycle logical time decreased")
                projected = connection.execute(
                    "SELECT last_evaluated_at FROM phase6_shadow_grant_current WHERE grant_id=?",
                    (grant.grant_id,),
                ).fetchone()
                if (
                    projected is None
                    or projected["last_evaluated_at"] is not None
                    and request["effective_at"] < projected["last_evaluated_at"]
                ):
                    raise Phase6GrantConflict(
                        "grant lifecycle time precedes its latest evaluation"
                    )
                lifecycle = _build_lifecycle(
                    grant=grant,
                    receipt_sequence=current.receipt_sequence + 1,
                    previous_receipt_sha256=current.receipt_sha256,
                    event="revoked",
                    before_status=current.after_status,
                    after_status=GrantStatus.REVOKED,
                    actor_id=request["actor_id"],
                    actor_generation=request["actor_generation"],
                    reason_code=request["reason_code"],
                    effective_at=request["effective_at"],
                    request_schema=request_schema,
                    request_sha256=request_sha,
                )
                connection.execute(
                    """
                    INSERT INTO phase6_shadow_grant_lifecycle_facts(
                        receipt_sha256,grant_id,receipt_sequence,receipt_json,effective_at
                    ) VALUES(?,?,?,?,?)
                    """,
                    (
                        lifecycle.receipt_sha256,
                        grant.grant_id,
                        lifecycle.receipt_sequence,
                        _canonical_json(lifecycle.as_dict()),
                        lifecycle.effective_at,
                    ),
                )
                _phase6_failure_point("after_revoke_lifecycle")
                cursor = connection.execute(
                    """
                    UPDATE phase6_shadow_grant_current
                    SET status=?,receipt_sequence=?,lifecycle_receipt_sha256=?,updated_at=?
                    WHERE grant_id=? AND lifecycle_receipt_sha256=?
                    """,
                    (
                        GrantStatus.REVOKED.value,
                        lifecycle.receipt_sequence,
                        lifecycle.receipt_sha256,
                        lifecycle.effective_at,
                        grant.grant_id,
                        current.receipt_sha256,
                    ),
                )
                if cursor.rowcount != 1:
                    raise Phase6GrantConflict("grant lifecycle CAS failed")
                _phase6_failure_point("after_revoke_current")
                self._insert_idempotency(
                    connection,
                    key=key,
                    domain="lifecycle",
                    request_schema=request_schema,
                    request=request,
                    result_kind="lifecycle",
                    result_id=lifecycle.receipt_sha256,
                    result=lifecycle.as_dict(),
                    created_at=lifecycle.effective_at,
                )
                _phase6_failure_point("before_revoke_commit")
                self._commit(connection)
                _phase6_failure_point("after_revoke_commit")
                result = LifecycleCommitResult(grant, lifecycle, False)
            connection.close()
        except BaseException as primary:
            _cleanup_failed_connection(
                connection,
                label="Phase-6 revocation",
                primary=primary,
            )
            raise
        return result

    @staticmethod
    def _load_evaluation(
        connection: sqlite3.Connection,
        evaluation_id: str,
    ) -> tuple[GrantEvaluationReceipt, ShadowScopedGrant, GrantLifecycleReceipt]:
        row = connection.execute(
            "SELECT * FROM phase6_shadow_evaluation_receipts WHERE evaluation_id=?",
            (_sha(evaluation_id, "evaluation_id"),),
        ).fetchone()
        if row is None:
            raise Phase6StoreError("evaluation receipt is unavailable")
        grant = Phase6SnapshotGrantStore._load_grant(connection, str(row["grant_id"]))
        value = _decode_exact_json(row["evaluation_json"], "evaluation receipt")
        if type(value) is not dict:
            raise Phase6StoreError("evaluation receipt is malformed")
        lifecycle = Phase6SnapshotGrantStore._load_lifecycle_by_id(
            connection,
            grant,
            str(value.get("observed_lifecycle_receipt_sha256")),
        )
        receipt = _evaluation_from_dict(value, grant, lifecycle)
        return receipt, grant, lifecycle

    @staticmethod
    def _current_evaluation_result(
        connection: sqlite3.Connection,
        *,
        receipt: GrantEvaluationReceipt,
        grant: ShadowScopedGrant,
        observed_lifecycle: GrantLifecycleReceipt,
        replayed: bool,
    ) -> EvaluationResult:
        snapshot = Phase6SnapshotGrantStore._load_snapshot(connection, grant.snapshot_id)
        current_snapshot = connection.execute(
            """
            SELECT snapshot_id FROM phase6_shadow_snapshot_current
            WHERE workflow_id=? AND project_id=?
            """,
            (grant.workflow_id, grant.project_id),
        ).fetchone()
        current_lifecycle = Phase6SnapshotGrantStore._load_lifecycle(connection, grant)
        current = bool(
            current_snapshot is not None
            and current_snapshot["snapshot_id"] == receipt.observed_snapshot_head_id
            and receipt.observed_snapshot_head_id == grant.snapshot_id
            and current_lifecycle.receipt_sha256
            == receipt.observed_lifecycle_receipt_sha256
            and observed_lifecycle.receipt_sha256
            == receipt.observed_lifecycle_receipt_sha256
        )
        allowed = current and receipt.decision is EvaluationDecision.ALLOWED_SHADOW
        proof = (
            _build_proof(snapshot, grant, current_lifecycle, receipt)
            if allowed
            else None
        )
        return EvaluationResult(receipt, current, allowed, proof, replayed)

    def evaluate_grant(
        self,
        grant_id: str,
        *,
        subject_type: str,
        subject_id: str,
        subject_generation: str,
        requested_scope: GrantScope,
        requested_scope_key: str | None,
        evaluated_at: int,
        idempotency_key: str,
    ) -> EvaluationResult:
        """Derive and durably record one closed-set shadow access decision."""

        key = _caller_key(idempotency_key)
        if type(requested_scope) is not GrantScope:
            raise Phase6ContractError("requested_scope must be GrantScope")
        request_schema = "phase6-evaluate-shadow-grant-request-v1"
        request: dict[str, object] = {
            "schema_version": request_schema,
            "grant_id": _sha(grant_id, "grant_id"),
            "subject_type": _identifier(subject_type, "subject_type"),
            "subject_id": _identifier(subject_id, "subject_id"),
            "subject_generation": _identifier(subject_generation, "subject_generation"),
            "requested_scope": requested_scope.value,
            "requested_scope_key": requested_scope_key,
            "evaluated_at": _nonnegative(evaluated_at, "evaluated_at"),
        }
        if requested_scope is GrantScope.SECTION_VIEW:
            _identifier(requested_scope_key, "requested_scope_key")
        elif requested_scope_key is not None:
            raise Phase6ContractError("only section:view accepts requested_scope_key")
        request_sha = canonical_sha256(request)
        connection = self._connect()
        try:
            self._begin(connection, immediate=True)
            replay = self._replay_row(
                connection, key=key, domain="evaluation", request_sha256=request_sha
            )
            if replay is not None:
                receipt, grant, observed = self._load_evaluation(
                    connection, str(replay["result_id"])
                )
                result = self._current_evaluation_result(
                    connection,
                    receipt=receipt,
                    grant=grant,
                    observed_lifecycle=observed,
                    replayed=True,
                )
                self._commit(connection)
            else:
                grant = self._load_grant(connection, request["grant_id"])
                snapshot = self._load_snapshot(connection, grant.snapshot_id)
                lifecycle = self._load_lifecycle(connection, grant)
                current_row = connection.execute(
                    "SELECT last_evaluated_at FROM phase6_shadow_grant_current WHERE grant_id=?",
                    (grant.grant_id,),
                ).fetchone()
                if current_row is None:
                    raise Phase6StoreError("grant current projection is unavailable")
                last_evaluated = current_row["last_evaluated_at"]
                if last_evaluated is not None and request["evaluated_at"] < last_evaluated:
                    raise Phase6GrantConflict("grant evaluation logical time decreased")
                snapshot_head = connection.execute(
                    """
                    SELECT snapshot_id FROM phase6_shadow_snapshot_current
                    WHERE workflow_id=? AND project_id=?
                    """,
                    (grant.workflow_id, grant.project_id),
                ).fetchone()
                if snapshot_head is None:
                    raise Phase6StoreError("snapshot current projection is unavailable")
                observed_head = str(snapshot_head["snapshot_id"])

                # Materialize the exclusive expiry boundary for the first
                # evaluation that observes it, regardless of whether that
                # request later fails the exact subject or scope comparison.
                # Revocation remains terminal and is never replaced by expiry.
                if (
                    lifecycle.after_status is GrantStatus.ACTIVE
                    and request["evaluated_at"] >= grant.expires_at
                ):
                    expiry_schema = "phase6-materialize-shadow-grant-expiry-request-v1"
                    expiry_request = {
                        "schema_version": expiry_schema,
                        "grant_id": grant.grant_id,
                        "evaluated_at": request["evaluated_at"],
                        "evaluation_request_sha256": request_sha,
                    }
                    expiry_request_sha = canonical_sha256(expiry_request)
                    lifecycle = _build_lifecycle(
                        grant=grant,
                        receipt_sequence=lifecycle.receipt_sequence + 1,
                        previous_receipt_sha256=lifecycle.receipt_sha256,
                        event="expired",
                        before_status=GrantStatus.ACTIVE,
                        after_status=GrantStatus.EXPIRED,
                        actor_id="phase6-evaluator",
                        actor_generation="v1",
                        reason_code="GRANT_EXPIRED",
                        effective_at=request["evaluated_at"],
                        request_schema=expiry_schema,
                        request_sha256=expiry_request_sha,
                    )
                    connection.execute(
                        """
                        INSERT INTO phase6_shadow_grant_lifecycle_facts(
                            receipt_sha256,grant_id,receipt_sequence,receipt_json,effective_at
                        ) VALUES(?,?,?,?,?)
                        """,
                        (
                            lifecycle.receipt_sha256,
                            grant.grant_id,
                            lifecycle.receipt_sequence,
                            _canonical_json(lifecycle.as_dict()),
                            lifecycle.effective_at,
                        ),
                    )
                    _phase6_failure_point("after_expiry_lifecycle")
                    cursor = connection.execute(
                        """
                        UPDATE phase6_shadow_grant_current
                        SET status=?,receipt_sequence=?,lifecycle_receipt_sha256=?,updated_at=?
                        WHERE grant_id=? AND status=?
                        """,
                        (
                            GrantStatus.EXPIRED.value,
                            lifecycle.receipt_sequence,
                            lifecycle.receipt_sha256,
                            lifecycle.effective_at,
                            grant.grant_id,
                            GrantStatus.ACTIVE.value,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise Phase6GrantConflict("grant expiry lifecycle CAS failed")
                    expiry_key = _internal_key(
                        base_key=key, object_id=grant.grant_id, stage="expire"
                    )
                    self._insert_idempotency(
                        connection,
                        key=expiry_key,
                        domain="lifecycle",
                        request_schema=expiry_schema,
                        request=expiry_request,
                        result_kind="lifecycle",
                        result_id=lifecycle.receipt_sha256,
                        result=lifecycle.as_dict(),
                        created_at=lifecycle.effective_at,
                    )
                    _phase6_failure_point("after_expiry_current")

                # Revocation is terminal and has precedence over every other
                # denial.  Other denial precedence is deterministic, while the
                # lifecycle expiry fact above remains independent of identity.
                if lifecycle.after_status is GrantStatus.REVOKED:
                    decision = EvaluationDecision.DENIED_REVOKED
                    reason = "GRANT_REVOKED"
                elif not snapshot.source_binding.eligible:
                    decision = EvaluationDecision.DENIED_SOURCE_INELIGIBLE
                    reason = "SOURCE_INELIGIBLE"
                elif observed_head != grant.snapshot_id:
                    decision = EvaluationDecision.DENIED_SNAPSHOT_STALE
                    reason = "SNAPSHOT_NOT_CURRENT_HEAD"
                elif request["evaluated_at"] >= snapshot.valid_until:
                    decision = EvaluationDecision.DENIED_SNAPSHOT_EXPIRED
                    reason = "SNAPSHOT_VALIDITY_EXPIRED"
                elif (
                    request["subject_type"] != grant.subject_type
                    or request["subject_id"] != grant.subject_id
                    or request["subject_generation"] != grant.subject_generation
                ):
                    decision = EvaluationDecision.DENIED_SUBJECT
                    reason = "SUBJECT_BINDING_DIFFERS"
                elif (
                    requested_scope is not grant.scope
                    or requested_scope_key != grant.scope_key
                ):
                    decision = EvaluationDecision.DENIED_SCOPE
                    reason = "CLOSED_SCOPE_DIFFERS"
                elif request["evaluated_at"] < grant.not_before:
                    decision = EvaluationDecision.DENIED_NOT_YET_VALID
                    reason = "GRANT_NOT_BEFORE_BOUNDARY"
                elif lifecycle.after_status is GrantStatus.EXPIRED:
                    decision = EvaluationDecision.DENIED_GRANT_EXPIRED
                    reason = "GRANT_VALIDITY_EXPIRED"
                else:
                    decision = EvaluationDecision.ALLOWED_SHADOW
                    reason = "EXACT_SHADOW_SCOPE_ALLOWED"
                receipt = _build_evaluation(
                    grant=grant,
                    lifecycle=lifecycle,
                    observed_snapshot_head_id=observed_head,
                    subject_type=request["subject_type"],
                    subject_id=request["subject_id"],
                    subject_generation=request["subject_generation"],
                    requested_scope=requested_scope,
                    requested_scope_key=requested_scope_key,
                    evaluated_at=request["evaluated_at"],
                    decision=decision,
                    reason_code=reason,
                )
                existing_evaluation = connection.execute(
                    "SELECT * FROM phase6_shadow_evaluation_receipts WHERE evaluation_id=?",
                    (receipt.evaluation_id,),
                ).fetchone()
                if existing_evaluation is None:
                    connection.execute(
                        """
                        INSERT INTO phase6_shadow_evaluation_receipts(
                            evaluation_id,grant_id,evaluated_at,evaluation_json,evaluation_sha256
                        ) VALUES(?,?,?,?,?)
                        """,
                        (
                            receipt.evaluation_id,
                            grant.grant_id,
                            receipt.evaluated_at,
                            _canonical_json(receipt.as_dict()),
                            receipt.evaluation_id,
                        ),
                    )
                    _phase6_failure_point("after_evaluation_receipt")
                else:
                    existing_value = _decode_exact_json(
                        existing_evaluation["evaluation_json"],
                        "existing evaluation receipt",
                    )
                    existing_receipt = _evaluation_from_dict(
                        existing_value, grant, lifecycle
                    )
                    if existing_receipt != receipt:
                        raise Phase6StoreError(
                            "evaluation identity collides with different bytes"
                        )
                cursor = connection.execute(
                    """
                    UPDATE phase6_shadow_grant_current
                    SET last_evaluated_at=?,updated_at=CASE WHEN updated_at>? THEN updated_at ELSE ? END
                    WHERE grant_id=?
                    """,
                    (
                        receipt.evaluated_at,
                        receipt.evaluated_at,
                        receipt.evaluated_at,
                        grant.grant_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise Phase6StoreError("grant evaluation projection update failed")
                _phase6_failure_point("after_evaluation_current")
                self._insert_idempotency(
                    connection,
                    key=key,
                    domain="evaluation",
                    request_schema=request_schema,
                    request=request,
                    result_kind="evaluation",
                    result_id=receipt.evaluation_id,
                    result=receipt.as_dict(),
                    created_at=receipt.evaluated_at,
                )
                _phase6_failure_point("before_evaluation_commit")
                self._commit(connection)
                _phase6_failure_point("after_evaluation_commit")
                result = self._current_evaluation_result(
                    connection,
                    receipt=receipt,
                    grant=grant,
                    observed_lifecycle=lifecycle,
                    replayed=False,
                )
            connection.close()
        except BaseException as primary:
            _cleanup_failed_connection(
                connection,
                label="Phase-6 evaluation",
                primary=primary,
            )
            raise
        return result

    def verify_current_access_proof(
        self, value: object, *, deadline: object | None = None
    ) -> ShadowAccessProof:
        """Revalidate an access proof against this store's current projections.

        Unlike :func:`verify_shadow_access_proof`, this boundary proves that
        the exact snapshot, grant, lifecycle and evaluation facts still exist
        in this store and that the observed snapshot and lifecycle remain the
        current heads.  The transaction is explicitly query-only and records
        no new evaluation, projection or idempotency fact.
        """

        _deadline_check(deadline, "phase6_current_proof_before")
        proof = verify_shadow_access_proof(value)
        connection = self._connect(deadline)
        try:
            connection.execute("PRAGMA query_only=ON")
            self._begin(connection, immediate=False)
            snapshot = self._load_snapshot(connection, proof.snapshot.snapshot_id)
            grant = self._load_grant(connection, proof.grant.grant_id)
            lifecycle = self._load_lifecycle(connection, grant)
            evaluation, evaluation_grant, observed_lifecycle = self._load_evaluation(
                connection,
                proof.evaluation_receipt.evaluation_id,
            )
            if (
                snapshot != proof.snapshot
                or grant != proof.grant
                or evaluation != proof.evaluation_receipt
                or evaluation_grant != proof.grant
                or observed_lifecycle != proof.lifecycle_receipt
            ):
                raise Phase6StoreError(
                    "access proof differs from exact persisted Phase-6 facts"
                )
            current = self._current_evaluation_result(
                connection,
                receipt=evaluation,
                grant=grant,
                observed_lifecycle=observed_lifecycle,
                replayed=True,
            )
            if (
                not current.current
                or not current.shadow_allowed
                or current.access_proof is None
                or current.access_proof != proof
                or lifecycle != proof.lifecycle_receipt
            ):
                raise Phase6StoreError(
                    "access proof is not current and shadow-allowed in this store"
                )
            self._commit(connection)
            connection.close()
        except BaseException as primary:
            _cleanup_failed_connection(
                connection,
                label="Phase-6 current access proof verification",
                primary=primary,
            )
            raise
        _deadline_check(deadline, "phase6_current_proof_after")
        return proof

    def load_grant(self, grant_id: str) -> LifecycleCommitResult:
        connection = self._connect()
        try:
            self._begin(connection, immediate=False)
            grant = self._load_grant(connection, grant_id)
            lifecycle = self._load_lifecycle(connection, grant)
            self._commit(connection)
            connection.close()
        except BaseException as primary:
            _cleanup_failed_connection(
                connection,
                label="Phase-6 grant load",
                primary=primary,
            )
            raise
        return LifecycleCommitResult(grant, lifecycle, False)


def load_project_snapshot_for_web(
    db_path: str | Path,
    project_id: str,
    expected_revision: int | None = None,
) -> dict[str, object]:
    """Return one current Phase-6 snapshot as a JSON-safe Web projection.

    Authentication and project ACL checks are intentionally outside this pure
    adapter and must run before it.  The adapter opens only the standalone
    Phase-6 database and never resolves a source/Authority database.
    """

    project = _identifier(project_id, "project_id")
    expected = (
        None
        if expected_revision is None
        else _nonnegative(expected_revision, "expected_revision")
    )
    store = Phase6SnapshotGrantStore(db_path)
    connection = store._connect()
    try:
        store._begin(connection, immediate=False)
        rows = connection.execute(
            """
            SELECT f.* FROM phase6_shadow_snapshot_current c
            JOIN phase6_shadow_snapshot_facts f ON f.snapshot_id=c.snapshot_id
            WHERE c.project_id=? ORDER BY c.workflow_id
            """,
            (project,),
        ).fetchall()
        if not rows:
            raise Phase6SnapshotNotFound(
                "current verified Phase-6 project snapshot is unavailable"
            )
        if len(rows) != 1:
            raise Phase6StoreError(
                "project identity maps to multiple Phase-6 workflow chains"
            )
        snapshot = store._snapshot_row(rows[0])
        if not snapshot.source_binding.eligible:
            raise Phase6SourceIneligible(
                "current Phase-6 source snapshot is not eligible for verified access"
            )
        revision = int(
            snapshot.source_binding.authority_coordinate["current_revision"]
        )
        if expected is not None and expected != revision:
            raise Phase6SnapshotStale(
                "requested Phase-6 project snapshot revision is stale",
                server_revision=revision,
            )
        coordinate: dict[str, object] = {
            "snapshot_id": snapshot.snapshot_id,
            "revision": revision,
        }
        sections = [
            {
                "key": section.section_id,
                "section_id": section.section_id,
                "availability": section.availability.value,
                "content_sha256": section.content_sha256,
                "source_section_schema": section.source_section_schema,
                "coordinate": dict(coordinate),
                "data": {
                    "availability": section.availability.value,
                    "content_sha256": section.content_sha256,
                    "source_section_schema": section.source_section_schema,
                },
            }
            for section in snapshot.sections
        ]
        result: dict[str, object] = {
            "schema_version": PHASE6_WEB_SNAPSHOT_SCHEMA,
            "state": "ready",
            "snapshot_id": snapshot.snapshot_id,
            "revision": revision,
            "coordinate": coordinate,
            "workflow_id": snapshot.workflow_id,
            "project_id": snapshot.project_id,
            "authority_coordinate": dict(
                snapshot.source_binding.authority_coordinate
            ),
            "source_binding_sha256": snapshot.source_binding.binding_sha256,
            "sections": sections,
            "actions": [],
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
        }
        store._commit(connection)
        connection.close()
    except BaseException as primary:
        _cleanup_failed_connection(
            connection,
            label="Phase-6 Web snapshot load",
            primary=primary,
        )
        raise
    return result


@dataclass(frozen=True)
class Phase6ShadowRun:
    schema_version: str
    enabled: bool
    store_verified: bool
    snapshot_count: int
    grant_count: int
    authoritative: bool
    authority_transferred: bool
    dispatch_performed: bool
    run_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "enabled": self.enabled,
            "store_verified": self.store_verified,
            "snapshot_count": self.snapshot_count,
            "grant_count": self.grant_count,
            "authoritative": self.authoritative,
            "authority_transferred": self.authority_transferred,
            "dispatch_performed": self.dispatch_performed,
            "run_sha256": self.run_sha256,
        }


def _run_identity(value: Phase6ShadowRun) -> dict[str, object]:
    result = value.as_dict()
    result.pop("run_sha256")
    return result


def run_phase6_snapshot_grants_shadow(
    *,
    enabled: bool = PHASE6_SNAPSHOT_GRANTS_DEFAULT_ENABLED,
    database: str | Path | None = None,
) -> Phase6ShadowRun:
    """Verify the isolated store only after explicit enablement.

    The disabled return occurs before path construction, resolution, lstat,
    SQLite, source reads, policy evaluation or logical-clock access.
    """

    if type(enabled) is not bool:
        raise Phase6ContractError("enabled must be a boolean")
    if not enabled:
        prototype = Phase6ShadowRun(
            PHASE6_RUN_SCHEMA,
            False,
            False,
            0,
            0,
            False,
            False,
            False,
            "0" * 64,
        )
        return replace(prototype, run_sha256=canonical_sha256(_run_identity(prototype)))
    if database is None:
        raise Phase6ContractError("enabled Phase-6 shadow run requires database")
    store = Phase6SnapshotGrantStore(database)
    store.initialize()
    connection = store._connect()
    try:
        store._begin(connection, immediate=False)
        snapshot_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM phase6_shadow_snapshot_facts"
            ).fetchone()[0]
        )
        grant_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM phase6_shadow_grant_facts"
            ).fetchone()[0]
        )
        store._commit(connection)
        connection.close()
    except BaseException as primary:
        _cleanup_failed_connection(
            connection,
            label="Phase-6 run",
            primary=primary,
        )
        raise
    prototype = Phase6ShadowRun(
        PHASE6_RUN_SCHEMA,
        True,
        True,
        snapshot_count,
        grant_count,
        False,
        False,
        False,
        "0" * 64,
    )
    return replace(prototype, run_sha256=canonical_sha256(_run_identity(prototype)))
