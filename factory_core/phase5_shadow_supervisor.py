"""Durable, default-disabled Phase-5 shadow supervisor.

The runtime owns only a standalone shadow SQLite file and synthetic observation
port.  It cannot signal a process, call a Solver/provider, dispatch an outbox
message, or alter workflow authority.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, replace
from enum import Enum
import errno
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from typing import Protocol

from .adapters.infrastructure.pause_policy import (
    PauseAction,
    PauseDecision,
    PauseMode,
    ProcessScopeKind,
    decide_pause_action,
)
from .canonical import canonical_bytes, canonical_sha256
from .fd_ownership import (
    OwnedDescriptor,
    RetryableCleanup,
    close_raw_descriptor_if_unowned,
    resilient_unlink_at,
    run_cleanup,
)


PHASE5_SHADOW_DEFAULT_ENABLED = False
PHASE5_SHADOW_SCHEMA = "phase5-durable-supervisor-shadow-v1"
PHASE5_SCOPE_BINDING_SCHEMA = "phase5-supervisor-scope-binding-v1"
PHASE5_RECEIPT_SCHEMA = "phase5-supervisor-transition-receipt-v1"
PHASE5_RUN_SCHEMA = "phase5-supervisor-full-shadow-run-v1"
PHASE5_OWNERSHIP_MARKER_SCHEMA = "phase5-shadow-store-ownership-marker-v2"
PHASE5_INTERNAL_STAGE_KEY_SCHEMA = "phase5-supervisor-internal-stage-key-v1"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,511}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_INTERNAL_KEY_PREFIX = "phase5-internal@"
_INTERNAL_KEY = re.compile(r"phase5-internal@[0-9a-f]{64}\Z")
_SQLITE_HEADER = b"SQLite format 3\x00"
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_OWNERSHIP_MARKER_SUFFIX = ".phase5-owner.json"
_RENAME_NOREPLACE = 1


def _raw_rename_noreplace(
    source_parent_fd: int,
    source_name: str,
    target_parent_fd: int,
    target_name: str,
) -> None:
    """Atomically rename without replacing an existing directory entry."""

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
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), source_name)


def _rename_noreplace(
    source_parent_fd: int,
    source_name: str,
    target_parent_fd: int,
    target_name: str,
) -> None:
    """Injectable wrapper around the non-replacing kernel primitive."""

    _raw_rename_noreplace(
        source_parent_fd, source_name, target_parent_fd, target_name
    )


def _rename_noreplace_supported() -> bool:
    return getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None) is not None


class Phase5SupervisorError(RuntimeError):
    """Base error for the isolated Phase-5 supervisor boundary."""


class Phase5SupervisorStoreError(Phase5SupervisorError):
    """Raised when persisted shadow bytes cannot be trusted."""


class Phase5SupervisorReplayConflict(Phase5SupervisorError):
    """Raised when an idempotency key is rebound to different bytes."""


class Phase5SupervisorFenceError(Phase5SupervisorError):
    """Raised for stale state or scope ownership."""


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise Phase5SupervisorError(f"{field} must be a canonical identifier")
    return value


def _caller_idempotency_key(value: object, field: str) -> str:
    key = _text(value, field)
    if key.startswith(_INTERNAL_KEY_PREFIX):
        raise Phase5SupervisorError(
            f"{field} uses the reserved Phase-5 internal namespace"
        )
    return key


def _persisted_idempotency_key(value: object, field: str) -> str:
    if isinstance(value, str) and _INTERNAL_KEY.fullmatch(value) is not None:
        return value
    return _caller_idempotency_key(value, field)


def _internal_stage_key(
    *, base_key: str, request_id: str, stage: str
) -> str:
    caller_key = _caller_idempotency_key(base_key, "request_idempotency_key")
    identifier = _text(request_id, "request_id")
    if stage not in {"checkpoint", "observation", "restart-recovery"}:
        raise Phase5SupervisorError("internal supervisor stage is unsupported")
    identity = {
        "schema_version": PHASE5_INTERNAL_STAGE_KEY_SCHEMA,
        "domain": "phase5-shadow-supervisor/internal-stage",
        "supervisor_schema_version": PHASE5_SHADOW_SCHEMA,
        "base_request_idempotency_key": caller_key,
        "request_id": identifier,
        "stage": stage,
    }
    return f"{_INTERNAL_KEY_PREFIX}{canonical_sha256(identity)}"


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise Phase5SupervisorError(f"{field} must be a lowercase SHA-256")
    return value


def _nonnegative(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise Phase5SupervisorError(f"{field} must be a nonnegative integer")
    return value


def _json(value: object) -> str:
    return canonical_bytes(value).decode("utf-8")


def _decode(value: object, digest: object, field: str) -> dict[str, object]:
    if not isinstance(value, str) or not isinstance(digest, str):
        raise Phase5SupervisorStoreError(f"{field} persisted identity is malformed")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise Phase5SupervisorStoreError(f"{field} JSON is malformed") from exc
    if not isinstance(decoded, dict):
        raise Phase5SupervisorStoreError(f"{field} must be an object")
    if _json(decoded) != value or canonical_sha256(decoded) != digest:
        raise Phase5SupervisorStoreError(f"{field} canonical bytes or hash differ")
    return decoded


@dataclass(frozen=True)
class SupervisorScopeBinding:
    workflow_id: str
    invocation_id: str
    attempt_id: str
    process_scope_id: str
    operation_identity_sha256: str
    scope_kind: ProcessScopeKind
    phase4_predecessor_state_sha256: str | None = None
    phase4_source_chain_binding_sha256: str | None = None

    def __post_init__(self) -> None:
        _text(self.workflow_id, "workflow_id")
        _text(self.invocation_id, "invocation_id")
        _text(self.attempt_id, "attempt_id")
        _text(self.process_scope_id, "process_scope_id")
        _sha(self.operation_identity_sha256, "operation_identity_sha256")
        if type(self.scope_kind) is not ProcessScopeKind:
            raise Phase5SupervisorError("scope_kind must be ProcessScopeKind")
        chain_values = (
            self.phase4_predecessor_state_sha256,
            self.phase4_source_chain_binding_sha256,
        )
        if any(value is None for value in chain_values):
            if any(value is not None for value in chain_values):
                raise Phase5SupervisorError(
                    "Phase-4 predecessor chain binding must be complete"
                )
        else:
            _sha(
                self.phase4_predecessor_state_sha256,
                "phase4_predecessor_state_sha256",
            )
            _sha(
                self.phase4_source_chain_binding_sha256,
                "phase4_source_chain_binding_sha256",
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PHASE5_SCOPE_BINDING_SCHEMA,
            "workflow_id": self.workflow_id,
            "invocation_id": self.invocation_id,
            "attempt_id": self.attempt_id,
            "process_scope_id": self.process_scope_id,
            "operation_identity_sha256": self.operation_identity_sha256,
            "scope_kind": self.scope_kind.value,
            "phase4_predecessor_state_sha256": (
                self.phase4_predecessor_state_sha256
            ),
            "phase4_source_chain_binding_sha256": (
                self.phase4_source_chain_binding_sha256
            ),
        }

    @property
    def binding_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


def _binding_from_dict(value: object) -> SupervisorScopeBinding:
    base_keys = {
        "schema_version",
        "workflow_id",
        "invocation_id",
        "attempt_id",
        "process_scope_id",
        "operation_identity_sha256",
        "scope_kind",
    }
    extended_keys = base_keys | {
        "phase4_predecessor_state_sha256",
        "phase4_source_chain_binding_sha256",
    }
    if not isinstance(value, dict) or set(value) not in {frozenset(base_keys), frozenset(extended_keys)}:
        raise Phase5SupervisorStoreError("scope binding is malformed")
    if value["schema_version"] != PHASE5_SCOPE_BINDING_SCHEMA:
        raise Phase5SupervisorStoreError("scope binding schema differs")
    try:
        return SupervisorScopeBinding(
            workflow_id=value["workflow_id"],
            invocation_id=value["invocation_id"],
            attempt_id=value["attempt_id"],
            process_scope_id=value["process_scope_id"],
            operation_identity_sha256=value["operation_identity_sha256"],
            scope_kind=ProcessScopeKind(value["scope_kind"]),
            phase4_predecessor_state_sha256=value.get(
                "phase4_predecessor_state_sha256"
            ),
            phase4_source_chain_binding_sha256=value.get(
                "phase4_source_chain_binding_sha256"
            ),
        )
    except (TypeError, ValueError, Phase5SupervisorError) as exc:
        raise Phase5SupervisorStoreError("scope binding is malformed") from exc


class SupervisorStatus(str, Enum):
    REQUESTED = "requested"
    EFFECT_CHECKPOINTED = "effect-checkpointed"
    RECONCILIATION_REQUIRED = "reconciliation-required"
    COMPLETED = "completed"


class SyntheticObservationOutcome(str, Enum):
    CONFIRMED_APPLIED = "confirmed-applied"
    CONFIRMED_NOT_REQUIRED = "confirmed-not-required"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SyntheticEffectObservation:
    observation_id: str
    outcome: SyntheticObservationOutcome
    observed_at: int
    evidence_sha256: str

    def __post_init__(self) -> None:
        _text(self.observation_id, "observation_id")
        if type(self.outcome) is not SyntheticObservationOutcome:
            raise Phase5SupervisorError(
                "outcome must be SyntheticObservationOutcome"
            )
        _nonnegative(self.observed_at, "observed_at")
        _sha(self.evidence_sha256, "evidence_sha256")

    def as_dict(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "outcome": self.outcome.value,
            "observed_at": self.observed_at,
            "evidence_sha256": self.evidence_sha256,
        }


class SyntheticEffectPort(Protocol):
    """Recording/observation boundary; it is not a process effect API."""

    def record_would_apply(
        self,
        *,
        request_id: str,
        binding: SupervisorScopeBinding,
        decision: PauseDecision,
    ) -> SyntheticEffectObservation:
        """Return one synthetic observation after the durable checkpoint."""


@dataclass(frozen=True)
class SupervisorState:
    request_id: str
    binding: SupervisorScopeBinding
    decision: PauseDecision
    status: SupervisorStatus
    transition_index: int
    last_observation: SyntheticEffectObservation | None = None

    def __post_init__(self) -> None:
        _text(self.request_id, "request_id")
        if type(self.binding) is not SupervisorScopeBinding:
            raise Phase5SupervisorError("binding must be SupervisorScopeBinding")
        if type(self.decision) is not PauseDecision:
            raise Phase5SupervisorError("decision must be PauseDecision")
        if self.decision.scope_kind is not self.binding.scope_kind:
            raise Phase5SupervisorError("pause decision is bound to another scope")
        if type(self.status) is not SupervisorStatus:
            raise Phase5SupervisorError("status must be SupervisorStatus")
        _nonnegative(self.transition_index, "transition_index")
        if self.status is SupervisorStatus.COMPLETED:
            if (
                type(self.last_observation) is not SyntheticEffectObservation
                or self.last_observation.outcome
                is SyntheticObservationOutcome.UNKNOWN
            ):
                raise Phase5SupervisorError("completed state needs a conclusive observation")
        elif (
            self.status is SupervisorStatus.RECONCILIATION_REQUIRED
            and self.last_observation is not None
            and self.last_observation.outcome is not SyntheticObservationOutcome.UNKNOWN
        ):
            raise Phase5SupervisorError("reconciliation state has a conclusive observation")
        elif self.status in {
            SupervisorStatus.REQUESTED,
            SupervisorStatus.EFFECT_CHECKPOINTED,
        } and self.last_observation is not None:
            raise Phase5SupervisorError("pre-observation state contains an observation")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PHASE5_SHADOW_SCHEMA,
            "request_id": self.request_id,
            "binding": self.binding.as_dict(),
            "binding_sha256": self.binding.binding_sha256,
            "decision": self.decision.as_dict(),
            "status": self.status.value,
            "transition_index": self.transition_index,
            "last_observation": (
                None if self.last_observation is None else self.last_observation.as_dict()
            ),
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
            "process_signal_performed": False,
            "provider_call_performed": False,
        }

    @property
    def state_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


def _observation_from_dict(value: object) -> SyntheticEffectObservation | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "observation_id",
        "outcome",
        "observed_at",
        "evidence_sha256",
    }:
        raise Phase5SupervisorStoreError("synthetic observation is malformed")
    try:
        return SyntheticEffectObservation(
            observation_id=value["observation_id"],
            outcome=SyntheticObservationOutcome(value["outcome"]),
            observed_at=value["observed_at"],
            evidence_sha256=value["evidence_sha256"],
        )
    except (TypeError, ValueError, Phase5SupervisorError) as exc:
        raise Phase5SupervisorStoreError("synthetic observation is malformed") from exc


def _state_from_dict(value: object) -> SupervisorState:
    if not isinstance(value, dict):
        raise Phase5SupervisorStoreError("supervisor state is malformed")
    expected = {
        "schema_version",
        "request_id",
        "binding",
        "binding_sha256",
        "decision",
        "status",
        "transition_index",
        "last_observation",
        "authoritative",
        "authority_transferred",
        "dispatch_performed",
        "process_signal_performed",
        "provider_call_performed",
    }
    if set(value) != expected or value["schema_version"] != PHASE5_SHADOW_SCHEMA:
        raise Phase5SupervisorStoreError("supervisor state schema differs")
    if any(
        value[field] is not False
        for field in (
            "authoritative",
            "authority_transferred",
            "dispatch_performed",
            "process_signal_performed",
            "provider_call_performed",
        )
    ):
        raise Phase5SupervisorStoreError("supervisor state claims a real side effect")
    binding = _binding_from_dict(value["binding"])
    if value["binding_sha256"] != binding.binding_sha256:
        raise Phase5SupervisorStoreError("supervisor scope binding hash differs")
    decision_value = value["decision"]
    if not isinstance(decision_value, dict):
        raise Phase5SupervisorStoreError("pause decision is malformed")
    try:
        decision = decide_pause_action(
            decision_value["mode"], decision_value["scope_kind"]
        )
        if decision.as_dict() != decision_value:
            raise Phase5SupervisorStoreError("pause decision does not recompile")
        return SupervisorState(
            request_id=value["request_id"],
            binding=binding,
            decision=decision,
            status=SupervisorStatus(value["status"]),
            transition_index=value["transition_index"],
            last_observation=_observation_from_dict(value["last_observation"]),
        )
    except (KeyError, TypeError, ValueError, Phase5SupervisorError) as exc:
        if isinstance(exc, Phase5SupervisorStoreError):
            raise
        raise Phase5SupervisorStoreError("supervisor state is malformed") from exc


def supervisor_state_from_dict(value: object) -> SupervisorState:
    """Public strict parser used by cross-store trusted-chain verification."""

    return _state_from_dict(value)


@dataclass(frozen=True)
class SupervisorReceipt:
    receipt_kind: str
    request_id: str
    request_idempotency_key: str
    request_sha256: str
    binding_sha256: str
    previous_state_sha256: str | None
    state_sha256: str
    transition_index: int
    occurred_at: int

    def __post_init__(self) -> None:
        if self.receipt_kind not in {
            "REQUESTED",
            "EFFECT_CHECKPOINTED",
            "RECONCILIATION_REQUIRED",
            "COMPLETED",
        }:
            raise Phase5SupervisorError("receipt kind is unsupported")
        _text(self.request_id, "request_id")
        _persisted_idempotency_key(
            self.request_idempotency_key, "request_idempotency_key"
        )
        _sha(self.request_sha256, "request_sha256")
        _sha(self.binding_sha256, "binding_sha256")
        if self.previous_state_sha256 is not None:
            _sha(self.previous_state_sha256, "previous_state_sha256")
        _sha(self.state_sha256, "state_sha256")
        _nonnegative(self.transition_index, "transition_index")
        _nonnegative(self.occurred_at, "occurred_at")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PHASE5_RECEIPT_SCHEMA,
            "receipt_kind": self.receipt_kind,
            "request_id": self.request_id,
            "request_idempotency_key": self.request_idempotency_key,
            "request_sha256": self.request_sha256,
            "binding_sha256": self.binding_sha256,
            "previous_state_sha256": self.previous_state_sha256,
            "state_sha256": self.state_sha256,
            "transition_index": self.transition_index,
            "occurred_at": self.occurred_at,
            "authoritative": False,
            "dispatch_performed": False,
        }

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


def _receipt_from_dict(value: object) -> SupervisorReceipt:
    if not isinstance(value, dict):
        raise Phase5SupervisorStoreError("supervisor receipt is malformed")
    expected = {
        "schema_version",
        "receipt_kind",
        "request_id",
        "request_idempotency_key",
        "request_sha256",
        "binding_sha256",
        "previous_state_sha256",
        "state_sha256",
        "transition_index",
        "occurred_at",
        "authoritative",
        "dispatch_performed",
    }
    if (
        set(value) != expected
        or value["schema_version"] != PHASE5_RECEIPT_SCHEMA
        or value["authoritative"] is not False
        or value["dispatch_performed"] is not False
    ):
        raise Phase5SupervisorStoreError("supervisor receipt schema differs")
    try:
        return SupervisorReceipt(
            receipt_kind=value["receipt_kind"],
            request_id=value["request_id"],
            request_idempotency_key=value["request_idempotency_key"],
            request_sha256=value["request_sha256"],
            binding_sha256=value["binding_sha256"],
            previous_state_sha256=value["previous_state_sha256"],
            state_sha256=value["state_sha256"],
            transition_index=value["transition_index"],
            occurred_at=value["occurred_at"],
        )
    except (TypeError, ValueError, Phase5SupervisorError) as exc:
        raise Phase5SupervisorStoreError("supervisor receipt is malformed") from exc


@dataclass(frozen=True)
class SupervisorCommitResult:
    state: SupervisorState
    receipt: SupervisorReceipt
    replayed: bool


def _phase5_failure_point(_stage: str) -> None:
    """Test-only transaction fault hook."""


def _phase5_initialization_failure_point(_stage: str) -> None:
    """Test seam for narrow creator/marker/connection ownership windows."""


_IMMUTABLE_TABLES = (
    "phase5_shadow_requests",
    "phase5_shadow_receipts",
    "phase5_shadow_idempotency",
)

_OWNED_TABLE_DEFINITIONS = (
    (
        "phase5_shadow_schema_state",
        "CREATE TABLE phase5_shadow_schema_state("
        "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
        "schema_version TEXT NOT NULL,"
        "ownership_marker_schema TEXT NOT NULL,"
        "allowed_objects_sha256 TEXT NOT NULL) STRICT",
    ),
    (
        "phase5_shadow_requests",
        "CREATE TABLE phase5_shadow_requests("
        "request_id TEXT PRIMARY KEY,"
        "request_idempotency_key TEXT NOT NULL UNIQUE,"
        "request_json TEXT NOT NULL,"
        "request_sha256 TEXT NOT NULL,"
        "binding_sha256 TEXT NOT NULL,"
        "created_at INTEGER NOT NULL) STRICT",
    ),
    (
        "phase5_shadow_current",
        "CREATE TABLE phase5_shadow_current("
        "request_id TEXT PRIMARY KEY REFERENCES phase5_shadow_requests(request_id),"
        "state_json TEXT NOT NULL,"
        "state_sha256 TEXT NOT NULL,"
        "updated_at INTEGER NOT NULL) STRICT",
    ),
    (
        "phase5_shadow_receipts",
        "CREATE TABLE phase5_shadow_receipts("
        "receipt_sha256 TEXT PRIMARY KEY,"
        "request_id TEXT NOT NULL REFERENCES phase5_shadow_requests(request_id),"
        "transition_index INTEGER NOT NULL,"
        "receipt_json TEXT NOT NULL,"
        "occurred_at INTEGER NOT NULL,"
        "UNIQUE(request_id, transition_index)) STRICT",
    ),
    (
        "phase5_shadow_idempotency",
        "CREATE TABLE phase5_shadow_idempotency("
        "request_idempotency_key TEXT PRIMARY KEY,"
        "request_sha256 TEXT NOT NULL,"
        "request_id TEXT NOT NULL,"
        "result_state_json TEXT NOT NULL,"
        "result_state_sha256 TEXT NOT NULL,"
        "result_receipt_json TEXT NOT NULL,"
        "result_receipt_sha256 TEXT NOT NULL,"
        "created_at INTEGER NOT NULL) STRICT",
    ),
)

_LEGACY_TABLE_STATEMENTS = (
    (
        "phase5_shadow_schema_state",
        """
    CREATE TABLE IF NOT EXISTS phase5_shadow_schema_state(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        schema_version TEXT NOT NULL
    ) STRICT
    """,
    ),
    (
        "phase5_shadow_requests",
        """
    CREATE TABLE IF NOT EXISTS phase5_shadow_requests(
        request_id TEXT PRIMARY KEY,
        request_idempotency_key TEXT NOT NULL UNIQUE,
        request_json TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        binding_sha256 TEXT NOT NULL,
        created_at INTEGER NOT NULL
    ) STRICT
    """,
    ),
    (
        "phase5_shadow_current",
        """
    CREATE TABLE IF NOT EXISTS phase5_shadow_current(
        request_id TEXT PRIMARY KEY REFERENCES phase5_shadow_requests(request_id),
        state_json TEXT NOT NULL,
        state_sha256 TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    ) STRICT
    """,
    ),
    (
        "phase5_shadow_receipts",
        """
    CREATE TABLE IF NOT EXISTS phase5_shadow_receipts(
        receipt_sha256 TEXT PRIMARY KEY,
        request_id TEXT NOT NULL REFERENCES phase5_shadow_requests(request_id),
        transition_index INTEGER NOT NULL,
        receipt_json TEXT NOT NULL,
        occurred_at INTEGER NOT NULL,
        UNIQUE(request_id, transition_index)
    ) STRICT
    """,
    ),
    (
        "phase5_shadow_idempotency",
        """
    CREATE TABLE IF NOT EXISTS phase5_shadow_idempotency(
        request_idempotency_key TEXT PRIMARY KEY,
        request_sha256 TEXT NOT NULL,
        request_id TEXT NOT NULL,
        result_state_json TEXT NOT NULL,
        result_state_sha256 TEXT NOT NULL,
        result_receipt_json TEXT NOT NULL,
        result_receipt_sha256 TEXT NOT NULL,
        created_at INTEGER NOT NULL
    ) STRICT
    """,
    ),
)


def _legacy_master_sql(statement: str) -> str:
    return statement.lstrip().replace(
        "CREATE TABLE IF NOT EXISTS ", "CREATE TABLE ", 1
    )


_LEGACY_TABLE_DEFINITIONS = tuple(
    (name, _legacy_master_sql(statement))
    for name, statement in _LEGACY_TABLE_STATEMENTS
)

_TRIGGER_DEFINITIONS = tuple(
    (
        f"{table}_immutable_{action.lower()}",
        table,
        f"CREATE TRIGGER {table}_immutable_{action.lower()} "
        f"BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, "
        f"'{table} is append-only'); END",
    )
    for table in _IMMUTABLE_TABLES
    for action in ("UPDATE", "DELETE")
)

# SQLite represents PRIMARY KEY and UNIQUE constraints on these rowid tables as
# persistent automatic indexes whose ``sqlite_master.sql`` value is NULL.  They
# are part of the owned schema profile just like explicitly-authored objects;
# the exact table definitions above make their canonical names deterministic.
_AUTO_INDEX_DEFINITIONS = (
    ("sqlite_autoindex_phase5_shadow_current_1", "phase5_shadow_current"),
    ("sqlite_autoindex_phase5_shadow_idempotency_1", "phase5_shadow_idempotency"),
    ("sqlite_autoindex_phase5_shadow_receipts_1", "phase5_shadow_receipts"),
    ("sqlite_autoindex_phase5_shadow_receipts_2", "phase5_shadow_receipts"),
    ("sqlite_autoindex_phase5_shadow_requests_1", "phase5_shadow_requests"),
    ("sqlite_autoindex_phase5_shadow_requests_2", "phase5_shadow_requests"),
)


def _schema_objects(
    tables: tuple[tuple[str, str], ...],
) -> tuple[dict[str, object], ...]:
    values = [
        {"type": "table", "name": name, "table_name": name, "sql": sql}
        for name, sql in tables
    ]
    values.extend(
        {
            "type": "trigger",
            "name": name,
            "table_name": table,
            "sql": sql,
        }
        for name, table, sql in _TRIGGER_DEFINITIONS
    )
    values.extend(
        {
            "type": "index",
            "name": name,
            "table_name": table,
            "sql": None,
        }
        for name, table in _AUTO_INDEX_DEFINITIONS
    )
    return tuple(sorted(values, key=lambda value: (value["type"], value["name"])))


_OWNED_SCHEMA_OBJECTS = _schema_objects(_OWNED_TABLE_DEFINITIONS)
_LEGACY_SCHEMA_OBJECTS = _schema_objects(_LEGACY_TABLE_DEFINITIONS)
_ALLOWED_OBJECTS = tuple(
    {
        "type": value["type"],
        "name": value["name"],
        "table_name": value["table_name"],
    }
    for value in _OWNED_SCHEMA_OBJECTS
)
_ALLOWED_OBJECTS_SHA256 = canonical_sha256(
    {
        "schema_version": "phase5-shadow-allowed-sqlite-objects-v1",
        "objects": _ALLOWED_OBJECTS,
    }
)
_OWNED_SCHEMA_STATE = {
    "singleton": 1,
    "schema_version": PHASE5_SHADOW_SCHEMA,
    "ownership_marker_schema": PHASE5_OWNERSHIP_MARKER_SCHEMA,
    "allowed_objects_sha256": _ALLOWED_OBJECTS_SHA256,
}
_LEGACY_SCHEMA_STATE = {
    "singleton": 1,
    "schema_version": PHASE5_SHADOW_SCHEMA,
}


def _schema_profile_sha256(
    objects: tuple[dict[str, object], ...], state: dict[str, object]
) -> str:
    return canonical_sha256(
        {
            "schema_version": "phase5-shadow-sqlite-profile-v1",
            "objects": objects,
            "schema_state": state,
        }
    )


_OWNED_SCHEMA_PROFILE_SHA256 = _schema_profile_sha256(
    _OWNED_SCHEMA_OBJECTS, _OWNED_SCHEMA_STATE
)
_LEGACY_SCHEMA_PROFILE_SHA256 = _schema_profile_sha256(
    _LEGACY_SCHEMA_OBJECTS, _LEGACY_SCHEMA_STATE
)


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
            int(value.st_dev),
            int(value.st_ino),
            int(value.st_size),
            int(value.st_mtime_ns),
            int(value.st_ctime_ns),
        )


def _identity(value: os.stat_result) -> _FileIdentity:
    return _FileIdentity.from_stat(value)


def _checked_open(
    path: Path,
    flags: int,
    *,
    label: str,
    owner: str,
) -> tuple[OwnedDescriptor, _FileIdentity]:
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise Phase5SupervisorStoreError(f"{label} is unavailable") from exc
    if not stat.S_ISREG(before.st_mode):
        raise Phase5SupervisorStoreError(f"{label} must be a regular file")
    lease: OwnedDescriptor | None = None
    try:
        open_flags = (
            flags
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        lease = OwnedDescriptor.from_opener(
            lambda: os.open(path, open_flags), owner=owner, label=label
        )
        descriptor = lease.fileno(owner)
        opened = os.fstat(descriptor)
        after = os.lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or int(opened.st_nlink) != 1
            or _identity(before) != _identity(opened)
            or _identity(opened) != _identity(after)
        ):
            raise Phase5SupervisorStoreError(f"{label} file identity changed")
        return lease, _identity(opened)
    except BaseException as primary:
        if lease is not None:
            run_cleanup(
                [(f"close {label}", lease.cleanup(owner))],
                primary=primary,
            )
        raise


def _descriptor_bytes(descriptor: int, *, label: str) -> bytes:
    value = os.pread(descriptor, 16385, 0)
    if len(value) > 16384:
        raise Phase5SupervisorStoreError(f"{label} is too large")
    return value


class _AnchoredConnection(sqlite3.Connection):
    """SQLite connection retaining the descriptors that proved ownership."""

    _database_lease: OwnedDescriptor | None = None
    _database_identity: _FileIdentity | None = None
    _schema_profile_sha256: str | None = None
    _marker_lease: OwnedDescriptor | None = None
    _marker_identity: _FileIdentity | None = None
    _marker_bytes: bytes | None = None
    _parent_lease: OwnedDescriptor | None = None
    _parent_identity: tuple[int, int] | None = None
    _sqlite_closed: bool = False

    @property
    def _database_fd(self) -> int | None:
        lease = self._database_lease
        if lease is None or lease.closed:
            return None
        return lease.fileno("phase5-connection-database")

    @property
    def _marker_fd(self) -> int | None:
        lease = self._marker_lease
        if lease is None or lease.closed:
            return None
        return lease.fileno("phase5-connection-marker")

    @property
    def _parent_fd(self) -> int | None:
        lease = self._parent_lease
        if lease is None or lease.closed:
            return None
        return lease.fileno("phase5-connection-parent")

    def _adopt_anchors(
        self,
        *,
        database: OwnedDescriptor,
        database_owner: str,
        marker: OwnedDescriptor | None,
        marker_owner: str,
        parent: OwnedDescriptor,
        parent_owner: str,
    ) -> None:
        if any(
            lease is not None
            for lease in (
                self._database_lease,
                self._marker_lease,
                self._parent_lease,
            )
        ):
            raise RuntimeError("Phase-5 connection already owns anchors")
        # Publish every shared lease before changing any owner token.  An
        # asynchronous failure during the sequence therefore leaves each
        # descriptor with either its old token or its connection token.
        self._database_lease = database
        self._marker_lease = marker
        self._parent_lease = parent
        database.transfer(
            owner=database_owner, new_owner="phase5-connection-database"
        )
        if marker is not None:
            marker.transfer(
                owner=marker_owner, new_owner="phase5-connection-marker"
            )
        parent.transfer(owner=parent_owner, new_owner="phase5-connection-parent")

    def close(self) -> None:
        def close_anchor(attribute: str, owner: str) -> None:
            lease = getattr(self, attribute)
            if lease is None:
                return
            try:
                run_cleanup(
                    [(f"close {attribute}", lease.cleanup(owner))]
                )
            finally:
                if lease.closed:
                    setattr(self, attribute, None)

        callbacks: list[tuple[str, object]] = []
        if self._parent_lease is not None:
            callbacks.append(
                (
                    "close Phase-5 connection parent anchor",
                    RetryableCleanup(
                        lambda: close_anchor(
                            "_parent_lease", "phase5-connection-parent"
                        )
                    ),
                )
            )
        if self._marker_lease is not None:
            callbacks.append(
                (
                    "close Phase-5 connection marker anchor",
                    RetryableCleanup(
                        lambda: close_anchor(
                            "_marker_lease", "phase5-connection-marker"
                        )
                    ),
                )
            )
        if self._database_lease is not None:
            callbacks.append(
                (
                    "close Phase-5 connection database anchor",
                    RetryableCleanup(
                        lambda: close_anchor(
                            "_database_lease", "phase5-connection-database"
                        )
                    ),
                )
            )
        try:
            super().close()
            self._sqlite_closed = True
        except BaseException as primary:
            run_cleanup(callbacks, primary=primary)
            raise
        run_cleanup(callbacks)

    @property
    def _resources_closed(self) -> bool:
        return self._sqlite_closed and all(
            lease is None or lease.closed
            for lease in (
                self._database_lease,
                self._marker_lease,
                self._parent_lease,
            )
        )


class Phase5SupervisorStore:
    """Standalone durable store for one or more shadow supervisor requests."""

    def __init__(self, database: str | Path) -> None:
        self._path = Path(database)
        if not self._path.is_absolute():
            raise Phase5SupervisorStoreError("shadow database path must be absolute")

    @property
    def path(self) -> Path:
        return self._path

    @property
    def _marker_path(self) -> Path:
        return Path(f"{self._path}{_OWNERSHIP_MARKER_SUFFIX}")

    def _validate_parent(self) -> None:
        parent = self._path.parent
        if not parent.is_dir():
            raise Phase5SupervisorStoreError("shadow database parent must exist")
        try:
            resolved = parent.resolve(strict=True)
        except OSError as exc:
            raise Phase5SupervisorStoreError("shadow database parent is unavailable") from exc
        if resolved != parent:
            raise Phase5SupervisorStoreError("shadow database parent cannot contain symlinks")
        if not Path("/proc/self/fd").is_dir():
            raise Phase5SupervisorStoreError("anchored SQLite descriptors are unavailable")

    def _open_parent_anchor(
        self, *, owner: str
    ) -> tuple[OwnedDescriptor, tuple[int, int]]:
        self._validate_parent()
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        lease: OwnedDescriptor | None = None
        try:
            try:
                lease = OwnedDescriptor.from_opener(
                    lambda: os.open(self._path.parent, flags),
                    owner=owner,
                    label="Phase-5 parent anchor",
                )
            except OSError as exc:
                raise Phase5SupervisorStoreError(
                    "shadow database parent anchor is unavailable"
                ) from exc
            descriptor = lease.fileno(owner)
            anchored = os.fstat(descriptor)
            named = os.stat(self._path.parent, follow_symlinks=False)
            if not stat.S_ISDIR(anchored.st_mode) or not stat.S_ISDIR(named.st_mode):
                raise Phase5SupervisorStoreError(
                    "shadow database parent must be a directory"
                )
            identity = (int(anchored.st_dev), int(anchored.st_ino))
            if identity != (int(named.st_dev), int(named.st_ino)):
                raise Phase5SupervisorStoreError(
                    "shadow database parent changed while acquiring anchor"
                )
            return lease, identity
        except BaseException as primary:
            if lease is not None:
                run_cleanup(
                    [
                        (
                            "close Phase-5 parent anchor",
                            lease.cleanup(owner),
                        )
                    ],
                    primary=primary,
                )
            raise

    def _assert_parent_matches(
        self, descriptor: int, expected: tuple[int, int]
    ) -> None:
        try:
            anchored = os.fstat(descriptor)
            named = os.stat(self._path.parent, follow_symlinks=False)
        except OSError as exc:
            raise Phase5SupervisorStoreError(
                "shadow database parent is unavailable"
            ) from exc
        if not stat.S_ISDIR(anchored.st_mode) or not stat.S_ISDIR(named.st_mode):
            raise Phase5SupervisorStoreError(
                "shadow database parent must remain a directory"
            )
        anchored_identity = (int(anchored.st_dev), int(anchored.st_ino))
        named_identity = (int(named.st_dev), int(named.st_ino))
        if anchored_identity != expected or named_identity != expected:
            raise Phase5SupervisorStoreError(
                "shadow database parent no longer names anchored inode"
            )

    def _assert_no_sqlite_sidecars(self) -> None:
        for suffix in _SQLITE_SIDECAR_SUFFIXES:
            if os.path.lexists(f"{self._path}{suffix}"):
                raise Phase5SupervisorStoreError(
                    f"shadow database ownership preflight rejects existing {suffix} sidecar"
                )

    @staticmethod
    def _fd_uri(descriptor: int, query: str) -> str:
        return f"file:/proc/self/fd/{descriptor}?{query}"

    @staticmethod
    def _same_inode(left: _FileIdentity, right: _FileIdentity) -> bool:
        return (left.device, left.inode) == (right.device, right.inode)

    def _assert_path_matches(
        self,
        path: Path,
        descriptor: int,
        *,
        label: str,
        expected: _FileIdentity | None = None,
        exact: bool,
    ) -> _FileIdentity:
        try:
            descriptor_stat = os.fstat(descriptor)
            path_stat = os.lstat(path)
        except OSError as exc:
            raise Phase5SupervisorStoreError(f"{label} is unavailable") from exc
        if (
            not stat.S_ISREG(descriptor_stat.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or int(descriptor_stat.st_nlink) != 1
            or int(path_stat.st_nlink) != 1
        ):
            raise Phase5SupervisorStoreError(f"{label} must be a single-link regular file")
        current = _identity(descriptor_stat)
        named = _identity(path_stat)
        if not self._same_inode(current, named):
            raise Phase5SupervisorStoreError(f"{label} no longer names anchored inode")
        if exact and current != named:
            raise Phase5SupervisorStoreError(f"{label} metadata changed during fence")
        if expected is not None:
            if exact and current != expected:
                raise Phase5SupervisorStoreError(f"{label} identity changed")
            if not exact and not self._same_inode(current, expected):
                raise Phase5SupervisorStoreError(f"{label} inode changed")
        return current

    @staticmethod
    def _database_path_sha256(path: Path) -> str:
        return canonical_sha256(
            {
                "schema_version": "phase5-shadow-database-path-v1",
                "absolute_path": str(path),
            }
        )

    def _marker_value(
        self,
        database_identity: _FileIdentity,
        parent_identity: tuple[int, int],
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "schema_version": PHASE5_OWNERSHIP_MARKER_SCHEMA,
            "store_schema_version": PHASE5_SHADOW_SCHEMA,
            "database_path_sha256": self._database_path_sha256(self._path),
            "database_device": database_identity.device,
            "database_inode": database_identity.inode,
            "parent_device": parent_identity[0],
            "parent_inode": parent_identity[1],
            "allowed_objects_sha256": _ALLOWED_OBJECTS_SHA256,
            "schema_profile_sha256": _OWNED_SCHEMA_PROFILE_SHA256,
        }
        return {**body, "marker_sha256": canonical_sha256(body)}

    def _verify_marker_bytes(
        self,
        value: bytes,
        database_identity: _FileIdentity,
        parent_identity: tuple[int, int],
    ) -> None:
        try:
            decoded = json.loads(value)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Phase5SupervisorStoreError("Phase-5 ownership marker is malformed") from exc
        if (
            not isinstance(decoded, dict)
            or set(decoded) != {
                "schema_version",
                "store_schema_version",
                "database_path_sha256",
                "database_device",
                "database_inode",
                "parent_device",
                "parent_inode",
                "allowed_objects_sha256",
                "schema_profile_sha256",
                "marker_sha256",
            }
            or canonical_bytes(decoded) != value
        ):
            raise Phase5SupervisorStoreError("Phase-5 ownership marker is not canonical")
        marker_sha256 = decoded.pop("marker_sha256")
        if (
            marker_sha256 != canonical_sha256(decoded)
            or decoded != {
                "schema_version": PHASE5_OWNERSHIP_MARKER_SCHEMA,
                "store_schema_version": PHASE5_SHADOW_SCHEMA,
                "database_path_sha256": self._database_path_sha256(self._path),
                "database_device": database_identity.device,
                "database_inode": database_identity.inode,
                "parent_device": parent_identity[0],
                "parent_inode": parent_identity[1],
                "allowed_objects_sha256": _ALLOWED_OBJECTS_SHA256,
                "schema_profile_sha256": _OWNED_SCHEMA_PROFILE_SHA256,
            }
        ):
            raise Phase5SupervisorStoreError("Phase-5 ownership marker differs")

    @staticmethod
    def _schema_inventory(
        connection: sqlite3.Connection,
    ) -> tuple[dict[str, object], ...]:
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

    @classmethod
    def _verify_schema(
        cls,
        connection: sqlite3.Connection,
        *,
        expected_profile: str | None = None,
    ) -> str:
        inventory = cls._schema_inventory(connection)
        if inventory == _OWNED_SCHEMA_OBJECTS:
            profile = _OWNED_SCHEMA_PROFILE_SHA256
            expected_state = _OWNED_SCHEMA_STATE
        elif inventory == _LEGACY_SCHEMA_OBJECTS:
            profile = _LEGACY_SCHEMA_PROFILE_SHA256
            expected_state = _LEGACY_SCHEMA_STATE
        else:
            raise Phase5SupervisorStoreError(
                "non-shadow SQLite schema objects or exact definitions differ"
            )
        if expected_profile is not None and profile != expected_profile:
            raise Phase5SupervisorStoreError("Phase-5 shadow schema profile changed")
        rows = connection.execute(
            "SELECT * FROM phase5_shadow_schema_state ORDER BY singleton"
        ).fetchall()
        if len(rows) != 1 or dict(rows[0]) != expected_state:
            raise Phase5SupervisorStoreError("Phase-5 ownership schema marker differs")
        return profile

    def _open_verified_anchor(
        self,
        *,
        owner_prefix: str,
    ) -> tuple[
        OwnedDescriptor,
        _FileIdentity,
        str,
        OwnedDescriptor | None,
        _FileIdentity | None,
        bytes | None,
        OwnedDescriptor,
        tuple[int, int],
    ]:
        database_owner = f"{owner_prefix}-database"
        marker_owner = f"{owner_prefix}-marker"
        parent_owner = f"{owner_prefix}-parent"
        parent: OwnedDescriptor | None = None
        parent_identity: tuple[int, int] | None = None
        database: OwnedDescriptor | None = None
        marker: OwnedDescriptor | None = None
        try:
            parent, parent_identity = self._open_parent_anchor(owner=parent_owner)
            self._assert_no_sqlite_sidecars()
            database, before = _checked_open(
                self._path,
                os.O_RDONLY,
                label="shadow database",
                owner=database_owner,
            )
            database_fd = database.fileno(database_owner)
            header = os.pread(database_fd, 100, 0)
            if (
                len(header) != 100
                or header[:16] != _SQLITE_HEADER
                or header[18] != 1
                or header[19] != 1
            ):
                raise Phase5SupervisorStoreError(
                    "shadow database raw rollback-journal header is invalid"
                )
            self._assert_path_matches(
                self._path,
                database_fd,
                label="shadow database",
                expected=before,
                exact=True,
            )
            marker_identity: _FileIdentity | None = None
            marker_bytes: bytes | None = None
            if os.path.lexists(self._marker_path):
                marker, marker_identity = _checked_open(
                    self._marker_path,
                    os.O_RDONLY,
                    label="Phase-5 ownership marker",
                    owner=marker_owner,
                )
                marker_fd = marker.fileno(marker_owner)
                marker_bytes = _descriptor_bytes(
                    marker_fd, label="Phase-5 ownership marker"
                )
            connection: sqlite3.Connection | None = None
            preflight_error: BaseException | None = None
            try:
                connection = sqlite3.connect(
                    self._fd_uri(database_fd, "mode=ro&immutable=1"),
                    uri=True,
                    timeout=0,
                    isolation_level=None,
                )
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                profile = self._verify_schema(connection)
            except BaseException as error:
                preflight_error = error
                raise
            finally:
                if connection is not None:
                    run_cleanup(
                        [
                            (
                                "close Phase-5 readonly preflight",
                                RetryableCleanup(connection.close),
                            )
                        ],
                        primary=preflight_error,
                    )
            if profile == _OWNED_SCHEMA_PROFILE_SHA256:
                if marker is None or marker_identity is None or marker_bytes is None:
                    raise Phase5SupervisorStoreError(
                        "Phase-5 ownership marker is unavailable"
                    )
                self._verify_marker_bytes(marker_bytes, before, parent_identity)
            elif marker is not None:
                raise Phase5SupervisorStoreError(
                    "legacy Phase-5 profile cannot claim a bound ownership marker"
                )
            after = self._assert_path_matches(
                self._path,
                database_fd,
                label="shadow database",
                expected=before,
                exact=True,
            )
            if marker is not None:
                marker_fd = marker.fileno(marker_owner)
                self._assert_path_matches(
                    self._marker_path,
                    marker_fd,
                    label="Phase-5 ownership marker",
                    expected=marker_identity,
                    exact=True,
                )
                if _descriptor_bytes(
                    marker_fd, label="Phase-5 ownership marker"
                ) != marker_bytes:
                    raise Phase5SupervisorStoreError(
                        "Phase-5 ownership marker changed during preflight"
                    )
            self._assert_no_sqlite_sidecars()
            self._assert_parent_matches(parent.fileno(parent_owner), parent_identity)
            return (
                database,
                after,
                profile,
                marker,
                marker_identity,
                marker_bytes,
                parent,
                parent_identity,
            )
        except BaseException as exc:
            callbacks = []
            if marker is not None:
                callbacks.append(
                    (
                        "close Phase-5 verified marker",
                        marker.cleanup(marker_owner),
                    )
                )
            if database is not None:
                callbacks.append(
                    (
                        "close Phase-5 verified database",
                        database.cleanup(database_owner),
                    )
                )
            if parent is not None:
                callbacks.append(
                    (
                        "close Phase-5 verified parent",
                        parent.cleanup(parent_owner),
                    )
                )
            run_cleanup(callbacks, primary=exc)
            if isinstance(exc, Phase5SupervisorError):
                raise
            if isinstance(exc, Exception):
                raise Phase5SupervisorStoreError(
                    "non-shadow Phase-5 ownership preflight failed"
                ) from exc
            raise

    def _owned_preflight(self) -> tuple[_FileIdentity, str]:
        database: OwnedDescriptor | None = None
        marker: OwnedDescriptor | None = None
        parent: OwnedDescriptor | None = None
        preflight_error: BaseException | None = None
        try:
            (
                database,
                identity,
                profile,
                marker,
                _,
                _,
                parent,
                _,
            ) = self._open_verified_anchor(owner_prefix="phase5-preflight")
            return identity, profile
        except BaseException as error:
            preflight_error = error
            raise
        finally:
            callbacks = []
            if marker is not None:
                callbacks.append(
                    (
                        "close Phase-5 preflight marker",
                        marker.cleanup("phase5-preflight-marker"),
                    )
                )
            if database is not None:
                callbacks.append(
                    (
                        "close Phase-5 preflight database",
                        database.cleanup("phase5-preflight-database"),
                    )
                )
            if parent is not None:
                callbacks.append(
                    (
                        "close Phase-5 preflight parent",
                        parent.cleanup("phase5-preflight-parent"),
                    )
                )
            run_cleanup(callbacks, primary=preflight_error)

    def _verify_connection_fence(
        self, connection: _AnchoredConnection, *, exact_database: bool
    ) -> None:
        if (
            connection._database_fd is None
            or connection._database_identity is None
            or connection._schema_profile_sha256 is None
            or connection._parent_fd is None
            or connection._parent_identity is None
        ):
            raise Phase5SupervisorStoreError("Phase-5 connection lacks ownership anchor")
        self._assert_parent_matches(
            connection._parent_fd, connection._parent_identity
        )
        current = self._assert_path_matches(
            self._path,
            connection._database_fd,
            label="shadow database",
            expected=connection._database_identity,
            exact=exact_database,
        )
        profile = connection._schema_profile_sha256
        if profile == _OWNED_SCHEMA_PROFILE_SHA256:
            if (
                connection._marker_fd is None
                or connection._marker_identity is None
                or connection._marker_bytes is None
            ):
                raise Phase5SupervisorStoreError("Phase-5 marker anchor is unavailable")
            self._assert_path_matches(
                self._marker_path,
                connection._marker_fd,
                label="Phase-5 ownership marker",
                expected=connection._marker_identity,
                exact=True,
            )
            marker_bytes = _descriptor_bytes(
                connection._marker_fd, label="Phase-5 ownership marker"
            )
            if marker_bytes != connection._marker_bytes:
                raise Phase5SupervisorStoreError("Phase-5 ownership marker changed")
            self._verify_marker_bytes(
                marker_bytes, current, connection._parent_identity
            )
        elif connection._marker_fd is not None:
            raise Phase5SupervisorStoreError("legacy Phase-5 profile acquired a marker")
        self._verify_schema(connection, expected_profile=profile)
        self._assert_parent_matches(
            connection._parent_fd, connection._parent_identity
        )

    def _verify_initialization_fence(
        self,
        *,
        parent_fd: int,
        parent_identity: tuple[int, int],
        database_fd: int,
        database_identity: _FileIdentity,
        marker_fd: int | None,
        marker_identity: _FileIdentity | None,
        marker_bytes: bytes | None,
        exact_database: bool,
    ) -> _FileIdentity:
        """Fence exclusive creation before the complete schema exists."""

        self._assert_parent_matches(parent_fd, parent_identity)
        current = self._assert_path_matches(
            self._path,
            database_fd,
            label="shadow database",
            expected=database_identity,
            exact=exact_database,
        )
        if marker_fd is None:
            if marker_identity is not None or marker_bytes is not None:
                raise Phase5SupervisorStoreError(
                    "Phase-5 initialization marker anchor is incomplete"
                )
        else:
            if marker_identity is None or marker_bytes is None:
                raise Phase5SupervisorStoreError(
                    "Phase-5 initialization marker anchor is unavailable"
                )
            self._assert_path_matches(
                self._marker_path,
                marker_fd,
                label="Phase-5 ownership marker",
                expected=marker_identity,
                exact=True,
            )
            if _descriptor_bytes(
                marker_fd, label="Phase-5 ownership marker"
            ) != marker_bytes:
                raise Phase5SupervisorStoreError(
                    "Phase-5 ownership marker changed during initialization"
                )
            self._verify_marker_bytes(marker_bytes, current, parent_identity)
        self._assert_parent_matches(parent_fd, parent_identity)
        return current

    def _connect(self, *, read_only: bool = False) -> _AnchoredConnection:
        database: OwnedDescriptor | None = None
        marker: OwnedDescriptor | None = None
        parent: OwnedDescriptor | None = None
        connection: _AnchoredConnection | None = None
        try:
            (
                database,
                identity,
                profile,
                marker,
                marker_identity,
                marker_bytes,
                parent,
                parent_identity,
            ) = self._open_verified_anchor(owner_prefix="phase5-connect")
            if profile == _LEGACY_SCHEMA_PROFILE_SHA256 and not read_only:
                raise Phase5SupervisorStoreError(
                    "markerless legacy Phase-5 stores are read-only"
                )
            self._assert_parent_matches(
                parent.fileno("phase5-connect-parent"), parent_identity
            )
            mode = "mode=ro&immutable=1" if read_only else "mode=rw"
            connection = sqlite3.connect(
                self._fd_uri(
                    database.fileno("phase5-connect-database"), mode
                ),
                uri=True,
                timeout=0 if read_only else 2,
                isolation_level=None if read_only else "",
                factory=_AnchoredConnection,
            )
            connection._adopt_anchors(
                database=database,
                database_owner="phase5-connect-database",
                marker=marker,
                marker_owner="phase5-connect-marker",
                parent=parent,
                parent_owner="phase5-connect-parent",
            )
            connection._database_identity = identity
            connection._schema_profile_sha256 = profile
            connection._marker_identity = marker_identity
            connection._marker_bytes = marker_bytes
            connection._parent_identity = parent_identity
            connection.row_factory = sqlite3.Row
            self._assert_no_sqlite_sidecars()
            if read_only:
                connection.execute("PRAGMA query_only=ON")
            else:
                connection.execute("PRAGMA foreign_keys=ON")
            self._verify_connection_fence(connection, exact_database=True)
            self._assert_no_sqlite_sidecars()
            return connection
        except BaseException as exc:
            if connection is not None:
                run_cleanup(
                    [
                        (
                            "close Phase-5 connection",
                            RetryableCleanup(connection.close),
                        )
                    ],
                    primary=exc,
                )
            callbacks = []
            if marker is not None:
                callbacks.append(
                    (
                        "close Phase-5 untransferred marker",
                        marker.cleanup("phase5-connect-marker"),
                    )
                )
            if database is not None:
                callbacks.append(
                    (
                        "close Phase-5 untransferred database",
                        database.cleanup("phase5-connect-database"),
                    )
                )
            if parent is not None:
                callbacks.append(
                    (
                        "close Phase-5 untransferred parent",
                        parent.cleanup("phase5-connect-parent"),
                    )
                )
            run_cleanup(callbacks, primary=exc)
            if isinstance(exc, Phase5SupervisorError):
                raise
            if isinstance(exc, Exception):
                raise Phase5SupervisorStoreError(
                    "Phase-5 anchored connection failed"
                ) from exc
            raise

    def _begin(self, connection: _AnchoredConnection, *, immediate: bool) -> None:
        self._assert_no_sqlite_sidecars()
        self._verify_connection_fence(connection, exact_database=True)
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        self._verify_connection_fence(connection, exact_database=True)

    def _commit_anchored(self, connection: _AnchoredConnection) -> None:
        self._verify_connection_fence(connection, exact_database=False)
        connection.commit()
        self._verify_connection_fence(connection, exact_database=False)
        self._assert_no_sqlite_sidecars()

    def _reconcile_initialization_outcome(
        self,
        *,
        parent_fd: int,
        parent_identity: tuple[int, int],
        database_fd: int,
        database_identity: _FileIdentity,
        marker_fd: int | None,
        marker_identity: _FileIdentity | None,
        marker_bytes: bytes | None,
        commit_attempted: bool,
    ) -> str:
        """Classify an exclusive Phase-5 creation without mutating it."""

        parent_stat = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or (int(parent_stat.st_dev), int(parent_stat.st_ino))
            != parent_identity
        ):
            raise Phase5SupervisorStoreError(
                "Phase-5 reconciliation parent anchor differs"
            )
        current = _identity(os.fstat(database_fd))
        named = _identity(
            os.stat(self._path.name, dir_fd=parent_fd, follow_symlinks=False)
        )
        expected_inode = (database_identity.device, database_identity.inode)
        if (
            (current.device, current.inode) != expected_inode
            or (named.device, named.inode) != expected_inode
        ):
            raise Phase5SupervisorStoreError(
                "Phase-5 reconciliation database identity differs"
            )
        if os.fstat(database_fd).st_size == 0:
            return "uncommitted"
        readonly: sqlite3.Connection | None = None
        readonly_error: BaseException | None = None
        try:
            readonly = sqlite3.connect(
                self._fd_uri(database_fd, "mode=ro&immutable=1"),
                uri=True,
                timeout=0,
                isolation_level=None,
            )
            readonly.row_factory = sqlite3.Row
            readonly.execute("PRAGMA query_only=ON")
            inventory = self._schema_inventory(readonly)
        except BaseException as error:
            readonly_error = error
            raise
        finally:
            if readonly is not None:
                run_cleanup(
                    [
                        (
                            "close Phase-5 reconciliation reader",
                            RetryableCleanup(readonly.close),
                        )
                    ],
                    primary=readonly_error,
                )
        if inventory == ():
            return "uncommitted"
        if not commit_attempted or inventory != _OWNED_SCHEMA_OBJECTS:
            return "ambiguous"
        if marker_fd is None or marker_identity is None or marker_bytes is None:
            return "ambiguous"
        fenced = self._verify_initialization_fence(
            parent_fd=parent_fd,
            parent_identity=parent_identity,
            database_fd=database_fd,
            database_identity=database_identity,
            marker_fd=marker_fd,
            marker_identity=marker_identity,
            marker_bytes=marker_bytes,
            exact_database=False,
        )
        fresh, profile = self._owned_preflight()
        if (
            not self._same_inode(current, fenced)
            or not self._same_inode(fresh, database_identity)
            or profile != _OWNED_SCHEMA_PROFILE_SHA256
        ):
            return "ambiguous"
        self._assert_no_sqlite_sidecars()
        os.fsync(parent_fd)
        return "committed"

    @staticmethod
    def _write_all(descriptor: int, value: bytes) -> None:
        offset = 0
        while offset < len(value):
            written = os.write(descriptor, value[offset:])
            if written <= 0:
                raise Phase5SupervisorStoreError("Phase-5 ownership marker write failed")
            offset += written

    @staticmethod
    def _cleanup_exclusive_entry(
        *,
        parent_fd: int,
        expected_parent: tuple[int, int],
        anchor_fd: int,
        name: str,
        expected: _FileIdentity,
    ) -> bool:
        """Quarantine and remove only the entry proved by this creation's anchor."""

        try:
            parent_stat = os.fstat(parent_fd)
            anchored_stat = os.fstat(anchor_fd)
            named_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise Phase5SupervisorStoreError(
                "exclusive Phase-5 cleanup ownership proof failed"
            ) from exc
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or (int(parent_stat.st_dev), int(parent_stat.st_ino))
            != expected_parent
            or not stat.S_ISREG(anchored_stat.st_mode)
            or not stat.S_ISREG(named_stat.st_mode)
            or int(anchored_stat.st_nlink) != 1
            or int(named_stat.st_nlink) != 1
        ):
            raise Phase5SupervisorStoreError(
                "exclusive Phase-5 cleanup refuses an invalid anchor"
            )
        anchored = _identity(anchored_stat)
        named = _identity(named_stat)
        expected_inode = (expected.device, expected.inode)
        if (
            (anchored.device, anchored.inode) != expected_inode
            or (named.device, named.inode) != expected_inode
        ):
            raise Phase5SupervisorStoreError(
                "exclusive Phase-5 cleanup refuses a replacement entry"
            )

        quarantine = (
            f".{name}.phase5-cleanup-{os.getpid()}-{secrets.token_hex(16)}"
        )

        def identity_if_present(entry: str) -> _FileIdentity | None:
            try:
                value = os.stat(entry, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if not stat.S_ISREG(value.st_mode) or int(value.st_nlink) != 1:
                raise Phase5SupervisorStoreError(
                    "exclusive Phase-5 cleanup recovery found an invalid entry"
                )
            return _identity(value)

        def finish_owned_cleanup() -> None:
            captured = identity_if_present(quarantine)
            original = identity_if_present(name)
            if captured is not None:
                if (captured.device, captured.inode) != expected_inode:
                    raise Phase5SupervisorStoreError(
                        "exclusive Phase-5 cleanup recovery found a replacement"
                    )
            elif original is not None:
                if (original.device, original.inode) != expected_inode:
                    raise Phase5SupervisorStoreError(
                        "exclusive Phase-5 cleanup recovery refuses a replacement"
                    )
                _raw_rename_noreplace(
                    parent_fd,
                    name,
                    parent_fd,
                    quarantine,
                )
                captured = identity_if_present(quarantine)
                if captured is None or (
                    captured.device,
                    captured.inode,
                ) != expected_inode:
                    raise Phase5SupervisorStoreError(
                        "exclusive Phase-5 cleanup recovery lost the owned inode"
                    )
            else:
                os.fsync(parent_fd)
                return
            resilient_unlink_at(parent_fd, quarantine)
            os.fsync(parent_fd)

        try:
            _rename_noreplace(
                parent_fd,
                name,
                parent_fd,
                quarantine,
            )
            captured_stat = os.stat(
                quarantine, dir_fd=parent_fd, follow_symlinks=False
            )
            captured = _identity(captured_stat)
            if (captured.device, captured.inode) != expected_inode:
                try:
                    _rename_noreplace(
                        parent_fd,
                        quarantine,
                        parent_fd,
                        name,
                    )
                finally:
                    os.fsync(parent_fd)
                raise Phase5SupervisorStoreError(
                    "exclusive Phase-5 cleanup captured a replacement entry"
                )
            resilient_unlink_at(parent_fd, quarantine)
        except BaseException as primary:
            run_cleanup(
                [("finish Phase-5 owned cleanup", finish_owned_cleanup)],
                primary=primary,
            )
            raise
        return True

    def _initialize_new(self) -> None:
        if not _rename_noreplace_supported():
            raise Phase5SupervisorStoreError(
                "safe exclusive cleanup requires Linux renameat2"
            )
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        database_raw = -1
        marker_raw = -1
        parent_fd = -1
        parent: OwnedDescriptor | None = None
        parent_identity: tuple[int, int] | None = None
        database_creator: OwnedDescriptor | None = None
        database_cleanup: OwnedDescriptor | None = None
        marker_creator: OwnedDescriptor | None = None
        marker_cleanup: OwnedDescriptor | None = None
        connection_parent: OwnedDescriptor | None = None
        connection: _AnchoredConnection | None = None
        database_identity: _FileIdentity | None = None
        marker_identity: _FileIdentity | None = None
        marker_bytes: bytes | None = None
        commit_attempted = False
        initialization_error: BaseException | None = None
        try:
            parent, parent_identity = self._open_parent_anchor(
                owner="phase5-initializer-parent"
            )
            parent_fd = parent.fileno("phase5-initializer-parent")
            self._assert_parent_matches(parent_fd, parent_identity)
            self._assert_no_sqlite_sidecars()
            if os.path.lexists(self._marker_path):
                raise Phase5SupervisorStoreError(
                    "Phase-5 ownership marker exists without an owned database"
                )
            database_raw = os.open(
                self._path.name,
                flags,
                0o600,
                dir_fd=parent_fd,
            )
            database_creator = OwnedDescriptor(
                database_raw,
                owner="phase5-database-creator",
                label="Phase-5 database creator",
            )
            database_raw = -1
            database_identity = self._assert_path_matches(
                self._path,
                database_creator.fileno("phase5-database-creator"),
                label="shadow database",
                exact=True,
            )
            _phase5_initialization_failure_point("after_database_create")
            if database_identity.size != 0:
                raise Phase5SupervisorStoreError(
                    "exclusive Phase-5 database is not empty"
                )
            _phase5_initialization_failure_point("before_database_cleanup_dup")
            database_cleanup = database_creator.duplicate(
                owner="phase5-database-creator",
                new_owner="phase5-database-cleanup",
                label="Phase-5 database cleanup anchor",
            )
            _phase5_initialization_failure_point("after_database_cleanup_dup")
            self._verify_initialization_fence(
                parent_fd=parent_fd,
                parent_identity=parent_identity,
                database_fd=database_creator.fileno("phase5-database-creator"),
                database_identity=database_identity,
                marker_fd=None,
                marker_identity=None,
                marker_bytes=None,
                exact_database=True,
            )
            _phase5_initialization_failure_point("before_marker_create")
            marker_raw = os.open(
                self._marker_path.name,
                flags,
                0o600,
                dir_fd=parent_fd,
            )
            marker_creator = OwnedDescriptor(
                marker_raw,
                owner="phase5-marker-creator",
                label="Phase-5 marker creator",
            )
            marker_raw = -1
            marker_identity = self._assert_path_matches(
                self._marker_path,
                marker_creator.fileno("phase5-marker-creator"),
                label="Phase-5 ownership marker",
                exact=True,
            )
            _phase5_initialization_failure_point("after_marker_create")
            _phase5_initialization_failure_point("before_marker_cleanup_dup")
            marker_cleanup = marker_creator.duplicate(
                owner="phase5-marker-creator",
                new_owner="phase5-marker-cleanup",
                label="Phase-5 marker cleanup anchor",
            )
            _phase5_initialization_failure_point("after_marker_cleanup_dup")
            marker_value = self._marker_value(database_identity, parent_identity)
            marker_bytes = canonical_bytes(marker_value)
            _phase5_initialization_failure_point("before_marker_write")
            self._write_all(
                marker_creator.fileno("phase5-marker-creator"), marker_bytes
            )
            os.fsync(marker_creator.fileno("phase5-marker-creator"))
            marker_identity = self._assert_path_matches(
                self._marker_path,
                marker_creator.fileno("phase5-marker-creator"),
                label="Phase-5 ownership marker",
                exact=True,
            )
            _phase5_initialization_failure_point("after_marker_write")
            self._verify_initialization_fence(
                parent_fd=parent_fd,
                parent_identity=parent_identity,
                database_fd=database_creator.fileno("phase5-database-creator"),
                database_identity=database_identity,
                marker_fd=marker_creator.fileno("phase5-marker-creator"),
                marker_identity=marker_identity,
                marker_bytes=marker_bytes,
                exact_database=True,
            )
            _phase5_initialization_failure_point("before_parent_dup")
            connection_parent = parent.duplicate(
                owner="phase5-initializer-parent",
                new_owner="phase5-connection-parent-pending",
                label="Phase-5 connection parent anchor",
            )
            _phase5_initialization_failure_point("after_parent_dup")
            _phase5_initialization_failure_point("before_connect")
            connection = sqlite3.connect(
                self._fd_uri(
                    database_creator.fileno("phase5-database-creator"),
                    "mode=rw",
                ),
                uri=True,
                timeout=2,
                factory=_AnchoredConnection,
            )
            _phase5_initialization_failure_point("after_connect_before_transfer")
            connection._adopt_anchors(
                database=database_creator,
                database_owner="phase5-database-creator",
                marker=marker_creator,
                marker_owner="phase5-marker-creator",
                parent=connection_parent,
                parent_owner="phase5-connection-parent-pending",
            )
            connection._database_identity = database_identity
            connection._schema_profile_sha256 = _OWNED_SCHEMA_PROFILE_SHA256
            connection._marker_identity = marker_identity
            connection._marker_bytes = marker_bytes
            connection._parent_identity = parent_identity
            _phase5_initialization_failure_point("after_connection_transfer")
            connection.row_factory = sqlite3.Row
            self._verify_initialization_fence(
                parent_fd=connection._parent_fd,
                parent_identity=parent_identity,
                database_fd=connection._database_fd,
                database_identity=database_identity,
                marker_fd=connection._marker_fd,
                marker_identity=marker_identity,
                marker_bytes=marker_bytes,
                exact_database=True,
            )
            self._assert_no_sqlite_sidecars()
            connection.execute("PRAGMA foreign_keys=ON")
            self._verify_initialization_fence(
                parent_fd=connection._parent_fd,
                parent_identity=parent_identity,
                database_fd=connection._database_fd,
                database_identity=database_identity,
                marker_fd=connection._marker_fd,
                marker_identity=marker_identity,
                marker_bytes=marker_bytes,
                exact_database=True,
            )
            connection.execute("BEGIN IMMEDIATE")
            self._verify_initialization_fence(
                parent_fd=connection._parent_fd,
                parent_identity=parent_identity,
                database_fd=connection._database_fd,
                database_identity=database_identity,
                marker_fd=connection._marker_fd,
                marker_identity=marker_identity,
                marker_bytes=marker_bytes,
                exact_database=False,
            )
            _phase5_initialization_failure_point("before_schema")
            for _, statement in _OWNED_TABLE_DEFINITIONS:
                connection.execute(statement)
            for _, _, statement in _TRIGGER_DEFINITIONS:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO phase5_shadow_schema_state VALUES(?,?,?,?)",
                tuple(_OWNED_SCHEMA_STATE.values()),
            )
            self._verify_schema(
                connection, expected_profile=_OWNED_SCHEMA_PROFILE_SHA256
            )
            self._verify_initialization_fence(
                parent_fd=connection._parent_fd,
                parent_identity=parent_identity,
                database_fd=connection._database_fd,
                database_identity=database_identity,
                marker_fd=connection._marker_fd,
                marker_identity=marker_identity,
                marker_bytes=marker_bytes,
                exact_database=False,
            )
            _phase5_initialization_failure_point("after_schema_before_commit")
            commit_attempted = True
            _phase5_initialization_failure_point("before_commit")
            self._commit_anchored(connection)
            _phase5_initialization_failure_point("before_parent_fsync")
            os.fsync(parent_fd)
            _phase5_initialization_failure_point("after_parent_fsync")
            _phase5_initialization_failure_point("after_commit")
            connection.close()
            if connection._resources_closed:
                connection = None
            _phase5_initialization_failure_point("before_final_preflight")
            post, profile = self._owned_preflight()
            if (
                not self._same_inode(post, database_identity)
                or profile != _OWNED_SCHEMA_PROFILE_SHA256
            ):
                raise Phase5SupervisorStoreError(
                    "initialized Phase-5 database identity differs"
                )
            _phase5_initialization_failure_point("after_final_preflight")
        except BaseException as exc:
            initialization_error = exc
            if connection is not None:
                def rollback_if_active() -> None:
                    if connection is not None and connection.in_transaction:
                        connection.rollback()

                run_cleanup(
                    [("rollback Phase-5 initialization", rollback_if_active)],
                    primary=exc,
                )
                run_cleanup(
                    [
                        (
                            "close Phase-5 initialization connection",
                            RetryableCleanup(connection.close),
                        )
                    ],
                    primary=exc,
                )
                if connection._resources_closed:
                    connection = None
            database_anchor_fd: int | None = None
            if database_cleanup is not None and not database_cleanup.closed:
                database_anchor_fd = database_cleanup.fileno(
                    "phase5-database-cleanup"
                )
            elif (
                database_creator is not None
                and not database_creator.closed
                and database_creator.owner == "phase5-database-creator"
            ):
                database_anchor_fd = database_creator.fileno(
                    "phase5-database-creator"
                )
            elif database_raw >= 0:
                database_anchor_fd = database_raw
            marker_anchor_fd: int | None = None
            if marker_cleanup is not None and not marker_cleanup.closed:
                marker_anchor_fd = marker_cleanup.fileno("phase5-marker-cleanup")
            elif (
                marker_creator is not None
                and not marker_creator.closed
                and marker_creator.owner == "phase5-marker-creator"
            ):
                marker_anchor_fd = marker_creator.fileno("phase5-marker-creator")
            elif marker_raw >= 0:
                marker_anchor_fd = marker_raw
            if database_identity is None and database_anchor_fd is not None:
                try:
                    database_stat = os.fstat(database_anchor_fd)
                    database_named_stat = os.stat(
                        self._path.name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    if (
                        not stat.S_ISREG(database_stat.st_mode)
                        or not stat.S_ISREG(database_named_stat.st_mode)
                        or int(database_stat.st_nlink) != 1
                        or int(database_named_stat.st_nlink) != 1
                        or (
                            int(database_stat.st_dev),
                            int(database_stat.st_ino),
                        )
                        != (
                            int(database_named_stat.st_dev),
                            int(database_named_stat.st_ino),
                        )
                    ):
                        raise Phase5SupervisorStoreError(
                            "Phase-5 cleanup probe database identity differs"
                        )
                    database_identity = _identity(database_stat)
                except BaseException as cleanup_probe_error:
                    def report_database_probe(
                        error: BaseException = cleanup_probe_error,
                    ) -> None:
                        raise error

                    run_cleanup(
                        [
                            (
                                "identify Phase-5 database for cleanup",
                                report_database_probe,
                            )
                        ],
                        primary=exc,
                    )
            if marker_identity is None and marker_anchor_fd is not None:
                try:
                    marker_stat = os.fstat(marker_anchor_fd)
                    marker_named_stat = os.stat(
                        self._marker_path.name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    if (
                        not stat.S_ISREG(marker_stat.st_mode)
                        or not stat.S_ISREG(marker_named_stat.st_mode)
                        or int(marker_stat.st_nlink) != 1
                        or int(marker_named_stat.st_nlink) != 1
                        or (int(marker_stat.st_dev), int(marker_stat.st_ino))
                        != (
                            int(marker_named_stat.st_dev),
                            int(marker_named_stat.st_ino),
                        )
                    ):
                        raise Phase5SupervisorStoreError(
                            "Phase-5 cleanup probe marker identity differs"
                        )
                    marker_identity = _identity(marker_stat)
                except BaseException as cleanup_probe_error:
                    def report_marker_probe(
                        error: BaseException = cleanup_probe_error,
                    ) -> None:
                        raise error

                    run_cleanup(
                        [
                            (
                                "identify Phase-5 marker for cleanup",
                                report_marker_probe,
                            )
                        ],
                        primary=exc,
                    )
            outcome = "unavailable"
            if database_identity is not None and database_anchor_fd is not None:
                try:
                    outcome = self._reconcile_initialization_outcome(
                        parent_fd=parent_fd,
                        parent_identity=parent_identity,
                        database_fd=database_anchor_fd,
                        database_identity=database_identity,
                        marker_fd=marker_anchor_fd,
                        marker_identity=marker_identity,
                        marker_bytes=marker_bytes,
                        commit_attempted=commit_attempted,
                    )
                except BaseException as reconciliation_error:
                    def report_reconciliation_error(
                        error: BaseException = reconciliation_error,
                    ) -> None:
                        raise error

                    run_cleanup(
                        [
                            (
                                "reconcile Phase-5 initialization outcome",
                                report_reconciliation_error,
                            )
                        ],
                        primary=exc,
                    )
                    outcome = "ambiguous"
            if outcome == "uncommitted":
                cleanup_callbacks = []
                if marker_identity is not None and marker_anchor_fd is not None:
                    cleanup_callbacks.append(
                        (
                            "remove uncommitted Phase-5 marker",
                            lambda: self._cleanup_exclusive_entry(
                                parent_fd=parent_fd,
                                expected_parent=parent_identity,
                                anchor_fd=marker_anchor_fd,
                                name=self._marker_path.name,
                                expected=marker_identity,
                            ),
                        )
                    )
                cleanup_callbacks.append(
                    (
                        "remove uncommitted Phase-5 database",
                        lambda: self._cleanup_exclusive_entry(
                            parent_fd=parent_fd,
                            expected_parent=parent_identity,
                            anchor_fd=database_anchor_fd,
                            name=self._path.name,
                            expected=database_identity,
                        ),
                    )
                )
                cleanup_callbacks.append(
                    ("fsync Phase-5 cleanup directory", lambda: os.fsync(parent_fd))
                )
                run_cleanup(cleanup_callbacks, primary=exc)
            elif outcome == "ambiguous":
                def report_ambiguous() -> None:
                    raise Phase5SupervisorStoreError(
                        "Phase-5 initialization outcome is ambiguous; "
                        "the owned entries were retained"
                    )

                run_cleanup(
                    [("reconcile Phase-5 initialization", report_ambiguous)],
                    primary=exc,
                )
            if isinstance(exc, Phase5SupervisorError):
                raise
            if isinstance(exc, Exception):
                raise Phase5SupervisorStoreError(
                    "exclusive Phase-5 database initialization failed"
                ) from exc
            raise
        finally:
            callbacks = []
            if connection is not None:
                callbacks.append(
                    (
                        "close Phase-5 connection",
                        RetryableCleanup(connection.close),
                    )
                )
            if connection_parent is not None:
                callbacks.append(
                    (
                        "close pending Phase-5 connection parent",
                        connection_parent.cleanup(
                            "phase5-connection-parent-pending"
                        ),
                    )
                )
            if marker_cleanup is not None:
                callbacks.append(
                    (
                        "close Phase-5 marker cleanup anchor",
                        marker_cleanup.cleanup("phase5-marker-cleanup"),
                    )
                )
            if database_cleanup is not None:
                callbacks.append(
                    (
                        "close Phase-5 database cleanup anchor",
                        database_cleanup.cleanup("phase5-database-cleanup"),
                    )
                )
            if marker_creator is not None:
                callbacks.append(
                    (
                        "close Phase-5 marker creator",
                        marker_creator.cleanup("phase5-marker-creator"),
                    )
                )
            if database_creator is not None:
                callbacks.append(
                    (
                        "close Phase-5 database creator",
                        database_creator.cleanup("phase5-database-creator"),
                    )
                )
            if marker_raw >= 0:
                callbacks.append(
                    (
                        "close raw Phase-5 marker descriptor",
                        lambda: close_raw_descriptor_if_unowned(
                            marker_raw, marker_creator
                        ),
                    )
                )
            if database_raw >= 0:
                callbacks.append(
                    (
                        "close raw Phase-5 database descriptor",
                        lambda: close_raw_descriptor_if_unowned(
                            database_raw, database_creator
                        ),
                    )
                )
            if parent is not None:
                callbacks.append(
                    (
                        "close Phase-5 initializer parent",
                        parent.cleanup("phase5-initializer-parent"),
                    )
                )
            run_cleanup(callbacks, primary=initialization_error)

    def initialize(self) -> None:
        self._validate_parent()
        if os.path.lexists(self._path):
            self._owned_preflight()
            return
        self._initialize_new()

    @staticmethod
    def _load_state_row(row: sqlite3.Row) -> SupervisorState:
        state = _state_from_dict(
            _decode(row["state_json"], row["state_sha256"], "supervisor state")
        )
        if state.state_sha256 != row["state_sha256"]:
            raise Phase5SupervisorStoreError("supervisor state identity differs")
        return state

    @staticmethod
    def _load_receipt_json(value: object, digest: object) -> SupervisorReceipt:
        receipt = _receipt_from_dict(_decode(value, digest, "supervisor receipt"))
        if receipt.receipt_sha256 != digest:
            raise Phase5SupervisorStoreError("supervisor receipt identity differs")
        return receipt

    def _replay(
        self,
        connection: sqlite3.Connection,
        *,
        request_idempotency_key: str,
        request_sha256: str,
    ) -> SupervisorCommitResult | None:
        row = connection.execute(
            "SELECT * FROM phase5_shadow_idempotency WHERE request_idempotency_key=?",
            (request_idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_sha256:
            raise Phase5SupervisorReplayConflict(
                "Phase-5 idempotency key is bound to different canonical bytes"
            )
        state = _state_from_dict(
            _decode(row["result_state_json"], row["result_state_sha256"], "replay state")
        )
        receipt = self._load_receipt_json(
            row["result_receipt_json"], row["result_receipt_sha256"]
        )
        persisted = connection.execute(
            "SELECT receipt_json FROM phase5_shadow_receipts WHERE receipt_sha256=?",
            (receipt.receipt_sha256,),
        ).fetchone()
        if persisted is None or persisted["receipt_json"] != row["result_receipt_json"]:
            raise Phase5SupervisorStoreError("replay receipt is unavailable")
        if state.state_sha256 != receipt.state_sha256:
            raise Phase5SupervisorStoreError("replay state binding differs")
        return SupervisorCommitResult(state, receipt, True)

    @staticmethod
    def _insert_result(
        connection: sqlite3.Connection,
        *,
        state: SupervisorState,
        receipt: SupervisorReceipt,
        request_idempotency_key: str,
        request_sha256: str,
        occurred_at: int,
    ) -> None:
        state_json = _json(state.as_dict())
        receipt_json = _json(receipt.as_dict())
        connection.execute(
            "INSERT INTO phase5_shadow_receipts VALUES(?,?,?,?,?)",
            (
                receipt.receipt_sha256,
                state.request_id,
                state.transition_index,
                receipt_json,
                occurred_at,
            ),
        )
        _phase5_failure_point("after_receipt")
        connection.execute(
            "INSERT INTO phase5_shadow_idempotency VALUES(?,?,?,?,?,?,?,?)",
            (
                request_idempotency_key,
                request_sha256,
                state.request_id,
                state_json,
                state.state_sha256,
                receipt_json,
                receipt.receipt_sha256,
                occurred_at,
            ),
        )
        _phase5_failure_point("after_idempotency")

    def request_pause(
        self,
        binding: SupervisorScopeBinding,
        mode: PauseMode | str,
        *,
        request_idempotency_key: str,
        occurred_at: int,
    ) -> SupervisorCommitResult:
        if type(binding) is not SupervisorScopeBinding:
            raise Phase5SupervisorError("binding must be SupervisorScopeBinding")
        key = _caller_idempotency_key(
            request_idempotency_key, "request_idempotency_key"
        )
        now = _nonnegative(occurred_at, "occurred_at")
        decision = decide_pause_action(mode, binding.scope_kind)
        request = {
            "schema_version": "phase5-supervisor-request-v1",
            "binding": binding.as_dict(),
            "binding_sha256": binding.binding_sha256,
            "decision": decision.as_dict(),
            "occurred_at": now,
        }
        request_sha = canonical_sha256(request)
        request_id = f"pause-{request_sha}"
        connection = self._connect()
        transaction_error: BaseException | None = None
        try:
            self._begin(connection, immediate=True)
            replay = self._replay(
                connection,
                request_idempotency_key=key,
                request_sha256=request_sha,
            )
            if replay is not None:
                self._commit_anchored(connection)
                return replay
            state = SupervisorState(
                request_id,
                binding,
                decision,
                SupervisorStatus.REQUESTED,
                0,
            )
            receipt = SupervisorReceipt(
                "REQUESTED",
                request_id,
                key,
                request_sha,
                binding.binding_sha256,
                None,
                state.state_sha256,
                0,
                now,
            )
            connection.execute(
                "INSERT INTO phase5_shadow_requests VALUES(?,?,?,?,?,?)",
                (request_id, key, _json(request), request_sha, binding.binding_sha256, now),
            )
            _phase5_failure_point("after_request")
            connection.execute(
                "INSERT INTO phase5_shadow_current VALUES(?,?,?,?)",
                (request_id, _json(state.as_dict()), state.state_sha256, now),
            )
            _phase5_failure_point("after_current")
            self._insert_result(
                connection,
                state=state,
                receipt=receipt,
                request_idempotency_key=key,
                request_sha256=request_sha,
                occurred_at=now,
            )
            self._commit_anchored(connection)
            return SupervisorCommitResult(state, receipt, False)
        except BaseException as primary:
            transaction_error = primary
            run_cleanup(
                [("rollback Phase-5 request transaction", connection.rollback)],
                primary=primary,
            )
            raise
        finally:
            run_cleanup(
                [
                    (
                        "close Phase-5 request connection",
                        RetryableCleanup(connection.close),
                    )
                ],
                primary=transaction_error,
            )

    def _current(
        self, connection: sqlite3.Connection, request_id: str
    ) -> SupervisorState:
        row = connection.execute(
            "SELECT * FROM phase5_shadow_current WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise Phase5SupervisorFenceError("supervisor request is unavailable")
        state = self._load_state_row(row)
        if state.request_id != request_id:
            raise Phase5SupervisorStoreError("current request identity differs")
        return state

    def load(self, request_id: str) -> SupervisorState:
        identifier = _text(request_id, "request_id")
        connection = self._connect(read_only=True)
        load_error: BaseException | None = None
        try:
            self._verify_schema(connection)
            return self._current(connection, identifier)
        except BaseException as error:
            load_error = error
            raise
        finally:
            run_cleanup(
                [
                    (
                        "close Phase-5 load connection",
                        RetryableCleanup(connection.close),
                    )
                ],
                primary=load_error,
            )

    def load_current_by_state_sha256(self, state_sha256: str) -> SupervisorState:
        """Return the unique exact Phase-5 request head bound by state hash."""

        digest = _sha(state_sha256, "state_sha256")
        connection = self._connect(read_only=True)
        load_error: BaseException | None = None
        try:
            self._verify_schema(connection)
            rows = connection.execute(
                "SELECT * FROM phase5_shadow_current WHERE state_sha256=?",
                (digest,),
            ).fetchall()
            if len(rows) != 1:
                raise Phase5SupervisorFenceError(
                    "supervisor current head is unavailable or ambiguous"
                )
            state = self._load_state_row(rows[0])
            if state.state_sha256 != digest:
                raise Phase5SupervisorStoreError(
                    "supervisor current-head identity differs"
                )
            return state
        except BaseException as error:
            load_error = error
            raise
        finally:
            run_cleanup(
                [("close Phase-5 current-head reader", RetryableCleanup(connection.close))],
                primary=load_error,
            )

    def load_current_for_operation(
        self,
        *,
        workflow_id: str,
        operation_identity_sha256: str,
    ) -> SupervisorState:
        """Resolve the sole typed current P5 head for a workflow/P4 operation."""

        workflow = _text(workflow_id, "workflow_id")
        operation = _sha(
            operation_identity_sha256, "operation_identity_sha256"
        )
        connection = self._connect(read_only=True)
        load_error: BaseException | None = None
        try:
            self._verify_schema(connection)
            rows = connection.execute(
                "SELECT * FROM phase5_shadow_current ORDER BY request_id"
            ).fetchall()
            matches = tuple(
                state
                for state in (self._load_state_row(row) for row in rows)
                if state.binding.workflow_id == workflow
                and state.binding.operation_identity_sha256 == operation
            )
            if len(matches) != 1:
                raise Phase5SupervisorFenceError(
                    "supervisor operation head is unavailable or ambiguous"
                )
            return matches[0]
        except BaseException as error:
            load_error = error
            raise
        finally:
            run_cleanup(
                [("close Phase-5 operation-head reader", RetryableCleanup(connection.close))],
                primary=load_error,
            )

    def _transition(
        self,
        request_id: str,
        *,
        event: str,
        expected_binding_sha256: str,
        request_idempotency_key: str,
        occurred_at: int,
        observation: SyntheticEffectObservation | None = None,
        _internal_idempotency: bool = False,
    ) -> SupervisorCommitResult:
        identifier = _text(request_id, "request_id")
        binding_sha = _sha(expected_binding_sha256, "expected_binding_sha256")
        if type(_internal_idempotency) is not bool:
            raise Phase5SupervisorError("internal idempotency selector must be boolean")
        key = (
            _persisted_idempotency_key(
                request_idempotency_key, "request_idempotency_key"
            )
            if _internal_idempotency
            else _caller_idempotency_key(
                request_idempotency_key, "request_idempotency_key"
            )
        )
        if _internal_idempotency and _INTERNAL_KEY.fullmatch(key) is None:
            raise Phase5SupervisorError(
                "internal transition requires a derived Phase-5 stage key"
            )
        now = _nonnegative(occurred_at, "occurred_at")
        if observation is not None and type(observation) is not SyntheticEffectObservation:
            raise Phase5SupervisorError("observation must be SyntheticEffectObservation")
        request = {
            "schema_version": "phase5-supervisor-transition-request-v1",
            "request_id": identifier,
            "event": event,
            "expected_binding_sha256": binding_sha,
            "observation": None if observation is None else observation.as_dict(),
            "occurred_at": now,
        }
        request_sha = canonical_sha256(request)
        connection = self._connect()
        transaction_error: BaseException | None = None
        try:
            self._begin(connection, immediate=True)
            replay = self._replay(
                connection,
                request_idempotency_key=key,
                request_sha256=request_sha,
            )
            if replay is not None:
                self._commit_anchored(connection)
                return replay
            current = self._current(connection, identifier)
            if current.binding.binding_sha256 != binding_sha:
                raise Phase5SupervisorFenceError("supervisor scope binding is stale")
            if event == "CHECKPOINT_EFFECT":
                if current.status is not SupervisorStatus.REQUESTED or observation is not None:
                    raise Phase5SupervisorFenceError("effect checkpoint transition is illegal")
                target = SupervisorStatus.EFFECT_CHECKPOINTED
                kind = "EFFECT_CHECKPOINTED"
                next_observation = None
            elif event == "RECOVER_UNCERTAIN":
                if (
                    current.status is not SupervisorStatus.EFFECT_CHECKPOINTED
                    or observation is not None
                ):
                    raise Phase5SupervisorFenceError("recovery transition is illegal")
                target = SupervisorStatus.RECONCILIATION_REQUIRED
                kind = "RECONCILIATION_REQUIRED"
                next_observation = None
            elif event == "RECORD_OBSERVATION":
                if current.status not in {
                    SupervisorStatus.EFFECT_CHECKPOINTED,
                    SupervisorStatus.RECONCILIATION_REQUIRED,
                } or observation is None:
                    raise Phase5SupervisorFenceError("observation transition is illegal")
                if observation.outcome is SyntheticObservationOutcome.UNKNOWN:
                    target = SupervisorStatus.RECONCILIATION_REQUIRED
                    kind = "RECONCILIATION_REQUIRED"
                else:
                    target = SupervisorStatus.COMPLETED
                    kind = "COMPLETED"
                next_observation = observation
            else:
                raise Phase5SupervisorError("unsupported supervisor event")
            updated = SupervisorState(
                current.request_id,
                current.binding,
                current.decision,
                target,
                current.transition_index + 1,
                next_observation,
            )
            receipt = SupervisorReceipt(
                kind,
                identifier,
                key,
                request_sha,
                binding_sha,
                current.state_sha256,
                updated.state_sha256,
                updated.transition_index,
                now,
            )
            changed = connection.execute(
                "UPDATE phase5_shadow_current SET state_json=?,state_sha256=?,updated_at=? "
                "WHERE request_id=? AND state_sha256=?",
                (
                    _json(updated.as_dict()),
                    updated.state_sha256,
                    now,
                    identifier,
                    current.state_sha256,
                ),
            )
            if changed.rowcount != 1:
                raise Phase5SupervisorFenceError("supervisor state CAS was lost")
            _phase5_failure_point("after_current")
            self._insert_result(
                connection,
                state=updated,
                receipt=receipt,
                request_idempotency_key=key,
                request_sha256=request_sha,
                occurred_at=now,
            )
            self._commit_anchored(connection)
            return SupervisorCommitResult(updated, receipt, False)
        except BaseException as primary:
            transaction_error = primary
            run_cleanup(
                [("rollback Phase-5 transition transaction", connection.rollback)],
                primary=primary,
            )
            raise
        finally:
            run_cleanup(
                [
                    (
                        "close Phase-5 transition connection",
                        RetryableCleanup(connection.close),
                    )
                ],
                primary=transaction_error,
            )

    def checkpoint_effect(self, request_id: str, **kwargs: object) -> SupervisorCommitResult:
        return self._transition(request_id, event="CHECKPOINT_EFFECT", **kwargs)

    def recover_uncertain(self, request_id: str, **kwargs: object) -> SupervisorCommitResult:
        return self._transition(request_id, event="RECOVER_UNCERTAIN", **kwargs)

    def record_observation(self, request_id: str, **kwargs: object) -> SupervisorCommitResult:
        return self._transition(request_id, event="RECORD_OBSERVATION", **kwargs)

    def table_counts(self) -> dict[str, int]:
        connection = self._connect(read_only=True)
        count_error: BaseException | None = None
        try:
            self._verify_schema(connection)
            return {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "phase5_shadow_requests",
                    "phase5_shadow_current",
                    "phase5_shadow_receipts",
                    "phase5_shadow_idempotency",
                )
            }
        except BaseException as error:
            count_error = error
            raise
        finally:
            run_cleanup(
                [
                    (
                        "close Phase-5 count connection",
                        RetryableCleanup(connection.close),
                    )
                ],
                primary=count_error,
            )


@dataclass(frozen=True)
class Phase5ShadowRun:
    schema_version: str
    enabled: bool
    authoritative: bool
    authority_transferred: bool
    dispatch_performed: bool
    process_signal_performed: bool
    provider_call_performed: bool
    effect_port_called: bool
    request_id: str | None
    final_state_sha256: str | None
    replayed: bool
    run_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "enabled": self.enabled,
            "authoritative": self.authoritative,
            "authority_transferred": self.authority_transferred,
            "dispatch_performed": self.dispatch_performed,
            "process_signal_performed": self.process_signal_performed,
            "provider_call_performed": self.provider_call_performed,
            "effect_port_called": self.effect_port_called,
            "request_id": self.request_id,
            "final_state_sha256": self.final_state_sha256,
            "replayed": self.replayed,
            "run_sha256": self.run_sha256,
        }


def _run_identity(run: Phase5ShadowRun) -> dict[str, object]:
    value = run.as_dict()
    value.pop("run_sha256")
    return value


def run_phase5_full_shadow(
    *,
    enabled: bool = PHASE5_SHADOW_DEFAULT_ENABLED,
    database: str | Path | None = None,
    binding: SupervisorScopeBinding | None = None,
    mode: PauseMode | str = PauseMode.PAUSE,
    request_idempotency_key: str = "phase5-disabled",
    occurred_at: int = 0,
    effect_port: SyntheticEffectPort | None = None,
) -> Phase5ShadowRun:
    """Run one fully synthetic supervisor cycle after explicit enablement."""

    if type(enabled) is not bool:
        raise Phase5SupervisorError("enabled must be a boolean")
    if not enabled:
        prototype = Phase5ShadowRun(
            PHASE5_RUN_SCHEMA,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            None,
            None,
            False,
            "0" * 64,
        )
        return replace(prototype, run_sha256=canonical_sha256(_run_identity(prototype)))
    if database is None or type(binding) is not SupervisorScopeBinding:
        raise Phase5SupervisorError("enabled supervisor requires database and binding")
    if effect_port is None:
        raise Phase5SupervisorError("enabled supervisor requires a synthetic effect port")
    base_key = _caller_idempotency_key(
        request_idempotency_key, "request_idempotency_key"
    )
    store = Phase5SupervisorStore(database)
    store.initialize()
    requested = store.request_pause(
        binding,
        mode,
        request_idempotency_key=base_key,
        occurred_at=occurred_at,
    )
    if requested.replayed:
        durable = store.load(requested.state.request_id)
        if durable.status in {
            SupervisorStatus.COMPLETED,
            SupervisorStatus.RECONCILIATION_REQUIRED,
        }:
            prototype = Phase5ShadowRun(
                PHASE5_RUN_SCHEMA,
                True,
                False,
                False,
                False,
                False,
                False,
                False,
                durable.request_id,
                durable.state_sha256,
                True,
                "0" * 64,
            )
            return replace(
                prototype, run_sha256=canonical_sha256(_run_identity(prototype))
            )
        if durable.status is SupervisorStatus.EFFECT_CHECKPOINTED:
            recovered = store._transition(
                durable.request_id,
                event="RECOVER_UNCERTAIN",
                expected_binding_sha256=binding.binding_sha256,
                request_idempotency_key=_internal_stage_key(
                    base_key=base_key,
                    request_id=durable.request_id,
                    stage="restart-recovery",
                ),
                occurred_at=occurred_at + 2,
                _internal_idempotency=True,
            )
            prototype = Phase5ShadowRun(
                PHASE5_RUN_SCHEMA,
                True,
                False,
                False,
                False,
                False,
                False,
                False,
                recovered.state.request_id,
                recovered.state.state_sha256,
                recovered.replayed,
                "0" * 64,
            )
            return replace(
                prototype, run_sha256=canonical_sha256(_run_identity(prototype))
            )
    checkpoint = store._transition(
        requested.state.request_id,
        event="CHECKPOINT_EFFECT",
        expected_binding_sha256=binding.binding_sha256,
        request_idempotency_key=_internal_stage_key(
            base_key=base_key,
            request_id=requested.state.request_id,
            stage="checkpoint",
        ),
        occurred_at=occurred_at + 1,
        _internal_idempotency=True,
    )
    observation = effect_port.record_would_apply(
        request_id=checkpoint.state.request_id,
        binding=binding,
        decision=checkpoint.state.decision,
    )
    completed = store._transition(
        checkpoint.state.request_id,
        event="RECORD_OBSERVATION",
        expected_binding_sha256=binding.binding_sha256,
        request_idempotency_key=_internal_stage_key(
            base_key=base_key,
            request_id=checkpoint.state.request_id,
            stage="observation",
        ),
        occurred_at=observation.observed_at,
        observation=observation,
        _internal_idempotency=True,
    )
    prototype = Phase5ShadowRun(
        PHASE5_RUN_SCHEMA,
        True,
        False,
        False,
        False,
        False,
        False,
        True,
        completed.state.request_id,
        completed.state.state_sha256,
        requested.replayed and checkpoint.replayed and completed.replayed,
        "0" * 64,
    )
    return replace(prototype, run_sha256=canonical_sha256(_run_identity(prototype)))


__all__ = [
    "PHASE5_SHADOW_DEFAULT_ENABLED",
    "Phase5ShadowRun",
    "Phase5SupervisorError",
    "Phase5SupervisorFenceError",
    "Phase5SupervisorReplayConflict",
    "Phase5SupervisorStore",
    "Phase5SupervisorStoreError",
    "SupervisorCommitResult",
    "SupervisorScopeBinding",
    "SupervisorState",
    "SupervisorStatus",
    "SyntheticEffectObservation",
    "SyntheticEffectPort",
    "SyntheticObservationOutcome",
    "run_phase5_full_shadow",
]
