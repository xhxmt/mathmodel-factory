"""Durable, default-off Phase-4 full-shadow operation runtime.

The store is deliberately independent from the Authority and legacy databases.
It records only immutable launch intent, deterministic state transitions and
synthetic observations.  It has no process, provider, network or dispatch port.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, replace
import errno
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat

from .canonical import canonical_bytes, canonical_sha256
from .fd_ownership import (
    OwnedDescriptor,
    RetryableCleanup,
    close_raw_descriptor_if_unowned,
    resilient_unlink_at,
    run_cleanup,
)
from .durable_operation import (
    OPERATION_IDENTITY_SCHEMA,
    OPERATION_RECEIPT_SCHEMA,
    DurableOperation,
    DurableOperationIdentity,
    OperationEvent,
    OperationStatus,
    OperationTransitionReceipt,
    OperationType,
    transition_operation,
)


PHASE4_SHADOW_DEFAULT_ENABLED = False
PHASE4_SHADOW_SCHEMA_VERSION = "phase4-durable-shadow-store-v1"
PHASE4_SHADOW_RECEIPT_SCHEMA = "phase4-durable-shadow-commit-receipt-v1"
PHASE4_SHADOW_RUN_SCHEMA = "phase4-durable-full-shadow-run-v1"
PHASE4_SOURCE_CHAIN_BINDING_SCHEMA = "phase4-trusted-source-chain-binding-v1"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_OID = re.compile(r"[0-9a-f]{40}\Z")
_SQLITE_HEADER = b"SQLite format 3\x00"
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_EXPECTED_SCHEMA_DIGEST = (
    "81a8ad8ea68ee6b7211b29fcf4a3c298aaddbaf70aa16e26ec8699084ba1bd0f"
)
_EXPECTED_SCHEMA_OBJECT_NAMES = frozenset(
    {
        "phase4_shadow_current",
        "phase4_shadow_idempotency",
        "phase4_shadow_idempotency_immutable_delete",
        "phase4_shadow_idempotency_immutable_update",
        "phase4_shadow_outbox_intents",
        "phase4_shadow_outbox_intents_immutable_delete",
        "phase4_shadow_outbox_intents_immutable_update",
        "phase4_shadow_receipts",
        "phase4_shadow_receipts_immutable_delete",
        "phase4_shadow_receipts_immutable_update",
        "phase4_shadow_schema_state",
        "phase4_shadow_schema_state_immutable_delete",
        "phase4_shadow_schema_state_immutable_update",
        "sqlite_autoindex_phase4_shadow_current_1",
        "sqlite_autoindex_phase4_shadow_current_2",
        "sqlite_autoindex_phase4_shadow_idempotency_1",
        "sqlite_autoindex_phase4_shadow_outbox_intents_1",
        "sqlite_autoindex_phase4_shadow_outbox_intents_2",
        "sqlite_autoindex_phase4_shadow_outbox_intents_3",
        "sqlite_autoindex_phase4_shadow_receipts_1",
        "sqlite_autoindex_phase4_shadow_receipts_2",
        "sqlite_autoindex_phase4_shadow_receipts_3",
    }
)
_LEGACY_SCHEMA_DIGEST = (
    "242cd87efed94ae3b12d24340b57b7efc1d7673d0fcd6ce08c29d3db9287af18"
)
_LEGACY_SCHEMA_OBJECT_NAMES = _EXPECTED_SCHEMA_OBJECT_NAMES - {
    "phase4_shadow_schema_state_immutable_delete",
    "phase4_shadow_schema_state_immutable_update",
}
_BOUND_SCHEMA_PROFILE = "bound-v1"
_LEGACY_SCHEMA_PROFILE = "legacy-v1"
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

    def as_dict(self) -> dict[str, int]:
        return {
            "device": self.device,
            "inode": self.inode,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
        }


class _AnchoredConnection(sqlite3.Connection):
    """SQLite connection that owns the descriptor used to select its inode."""

    _anchor_lease: OwnedDescriptor | None = None
    _anchor_identity: _FileIdentity | None = None
    _schema_profile: str | None = None
    _sqlite_closed: bool = False

    @property
    def _anchor_fd(self) -> int | None:
        lease = self._anchor_lease
        if lease is None or lease.closed:
            return None
        return lease.fileno("phase4-connection")

    def _adopt_anchor(self, lease: OwnedDescriptor, *, owner: str) -> None:
        if self._anchor_lease is not None:
            raise RuntimeError("Phase-4 connection already owns an anchor")
        # Publish the shared lease first.  An asynchronous BaseException before
        # the token change leaves ``owner`` responsible; one after it leaves
        # the connection token responsible.  There is no ownerless window.
        self._anchor_lease = lease
        lease.transfer(owner=owner, new_owner="phase4-connection")

    def close(self) -> None:
        lease = self._anchor_lease
        try:
            super().close()
            self._sqlite_closed = True
        except BaseException as primary:
            if lease is not None:
                run_cleanup(
                    [
                        (
                            "close Phase-4 connection anchor",
                            lease.cleanup("phase4-connection"),
                        )
                    ],
                    primary=primary,
                )
                if lease.closed:
                    self._anchor_lease = None
            raise
        if lease is not None:
            try:
                run_cleanup(
                    [
                        (
                            "close Phase-4 connection anchor",
                            lease.cleanup("phase4-connection"),
                        )
                    ]
                )
            finally:
                if lease.closed:
                    self._anchor_lease = None

    @property
    def _resources_closed(self) -> bool:
        lease = self._anchor_lease
        return self._sqlite_closed and (lease is None or lease.closed)


class Phase4ShadowError(RuntimeError):
    """Base error for the durable Phase-4 shadow boundary."""


class Phase4ShadowStoreError(Phase4ShadowError):
    """Raised when the standalone store is unavailable or malformed."""


class Phase4ShadowIdempotencyConflict(Phase4ShadowError):
    """Raised when one request key is rebound to different canonical bytes."""


class Phase4ShadowFenceError(Phase4ShadowError):
    """Raised when operation, claim, lease or state CAS ownership is stale."""


@dataclass(frozen=True, slots=True)
class Phase4SourceChainBinding:
    """Immutable Phase-2/3 coordinate captured by the trusted P4 producer.

    The binding is optional for the historical standalone Phase-4 contract.
    A Phase-6 trusted-source chain, however, requires it and revalidates every
    field against the live Authority/Phase-3 readers before use.
    """

    project_id: str
    workflow_id: str
    authority_revision: int
    project_generation: str
    run_generation: str
    runtime_generation: str
    scheduler_generation: str
    contract_pin_set_sha256: str
    phase3_artifact_state_sha256: str
    selected_occurrence_id: str
    selected_occurrence_semantic_sha256: str
    phase3_current_graph_sha256: str
    authority_command_id: str
    authority_command_sha256: str
    authority_mutation_sha256: str
    authority_revision_snapshot_sha256: str
    authority_outbox_message_id: str
    authority_predecessor_event_sha256: str | None
    implementation_identity_sha256: str
    source_commit: str
    source_tree: str
    source_parent: str
    run_generation_request_sha256: str
    run_generation_creation_receipt_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "project_id", "workflow_id", "project_generation", "run_generation",
            "runtime_generation", "scheduler_generation", "selected_occurrence_id",
            "authority_command_id", "authority_outbox_message_id",
        ):
            value = _text(getattr(self, name), name)
            if name.endswith("generation") and value == "legacy_unknown":
                raise Phase4ShadowError("legacy_unknown generations are ineligible")
        _nonnegative(self.authority_revision, "authority_revision")
        if self.authority_revision < 1:
            raise Phase4ShadowError("authority_revision must be positive")
        for name in (
            "contract_pin_set_sha256", "phase3_artifact_state_sha256",
            "selected_occurrence_semantic_sha256", "phase3_current_graph_sha256",
            "authority_command_sha256", "authority_mutation_sha256",
            "authority_revision_snapshot_sha256", "implementation_identity_sha256",
            "run_generation_request_sha256",
            "run_generation_creation_receipt_sha256",
        ):
            _sha(getattr(self, name), name)
        for name in ("source_commit", "source_tree", "source_parent"):
            if _GIT_OID.fullmatch(getattr(self, name)) is None:
                raise Phase4ShadowError(f"{name} must be a concrete Git object ID")
        if self.authority_predecessor_event_sha256 is not None:
            _sha(
                self.authority_predecessor_event_sha256,
                "authority_predecessor_event_sha256",
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PHASE4_SOURCE_CHAIN_BINDING_SCHEMA,
            "project_id": self.project_id,
            "workflow_id": self.workflow_id,
            "authority_revision": self.authority_revision,
            "project_generation": self.project_generation,
            "run_generation": self.run_generation,
            "runtime_generation": self.runtime_generation,
            "scheduler_generation": self.scheduler_generation,
            "contract_pin_set_sha256": self.contract_pin_set_sha256,
            "phase3_artifact_state_sha256": self.phase3_artifact_state_sha256,
            "selected_occurrence_id": self.selected_occurrence_id,
            "selected_occurrence_semantic_sha256": (
                self.selected_occurrence_semantic_sha256
            ),
            "phase3_current_graph_sha256": self.phase3_current_graph_sha256,
            "authority_command_id": self.authority_command_id,
            "authority_command_sha256": self.authority_command_sha256,
            "authority_mutation_sha256": self.authority_mutation_sha256,
            "authority_revision_snapshot_sha256": (
                self.authority_revision_snapshot_sha256
            ),
            "authority_outbox_message_id": self.authority_outbox_message_id,
            "authority_predecessor_event_sha256": (
                self.authority_predecessor_event_sha256
            ),
            "implementation_identity_sha256": (
                self.implementation_identity_sha256
            ),
            "source_commit": self.source_commit,
            "source_tree": self.source_tree,
            "source_parent": self.source_parent,
            "run_generation_request_sha256": self.run_generation_request_sha256,
            "run_generation_creation_receipt_sha256": (
                self.run_generation_creation_receipt_sha256
            ),
        }

    @property
    def binding_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


def phase4_source_chain_binding_from_dict(
    value: object,
) -> Phase4SourceChainBinding:
    expected = {
        "schema_version", "project_id", "workflow_id", "authority_revision",
        "project_generation", "run_generation", "runtime_generation",
        "scheduler_generation", "contract_pin_set_sha256",
        "phase3_artifact_state_sha256", "selected_occurrence_id",
        "selected_occurrence_semantic_sha256", "phase3_current_graph_sha256",
        "authority_command_id", "authority_command_sha256",
        "authority_mutation_sha256", "authority_revision_snapshot_sha256",
        "authority_outbox_message_id", "authority_predecessor_event_sha256",
        "implementation_identity_sha256",
        "source_commit", "source_tree", "source_parent",
        "run_generation_request_sha256",
        "run_generation_creation_receipt_sha256",
    }
    if (
        type(value) is not dict
        or set(value) != expected
        or value.get("schema_version") != PHASE4_SOURCE_CHAIN_BINDING_SCHEMA
    ):
        raise Phase4ShadowStoreError("Phase-4 source-chain binding is malformed")
    try:
        return Phase4SourceChainBinding(
            **{name: value[name] for name in expected if name != "schema_version"}
        )
    except (TypeError, ValueError, Phase4ShadowError) as exc:
        raise Phase4ShadowStoreError(
            "Phase-4 source-chain binding does not revalidate"
        ) from exc


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise Phase4ShadowError(f"{field} must be a canonical identifier")
    return value


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise Phase4ShadowError(f"{field} must be a lowercase SHA-256")
    return value


def _nonnegative(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise Phase4ShadowError(f"{field} must be a nonnegative integer")
    return value


def _canonical_json(value: object) -> str:
    return canonical_bytes(value).decode("utf-8")


def _decode_canonical(value: object, digest: object, field: str) -> object:
    if not isinstance(value, str) or not isinstance(digest, str):
        raise Phase4ShadowStoreError(f"{field} canonical value is malformed")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise Phase4ShadowStoreError(f"{field} JSON is malformed") from exc
    if _canonical_json(decoded) != value or canonical_sha256(decoded) != digest:
        raise Phase4ShadowStoreError(f"{field} canonical bytes or hash differ")
    return decoded


def _identity_from_dict(value: object) -> DurableOperationIdentity:
    if not isinstance(value, dict):
        raise Phase4ShadowStoreError("operation identity is malformed")
    try:
        return DurableOperationIdentity(
            schema_version=value["schema_version"],
            operation_type=OperationType(value["operation_type"]),
            outbox_command_id=value["outbox_command_id"],
            invocation_id=value["invocation_id"],
            attempt_id=value["attempt_id"],
            process_scope_id=value["process_scope_id"],
            payload_sha256=value["payload_sha256"],
            idempotency_key=value["idempotency_key"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise Phase4ShadowStoreError("operation identity is malformed") from exc


def _transition_receipt_from_dict(value: object) -> OperationTransitionReceipt:
    if not isinstance(value, dict):
        raise Phase4ShadowStoreError("operation transition receipt is malformed")
    try:
        receipt = OperationTransitionReceipt(
            schema_version=value["schema_version"],
            operation_identity_sha256=value["operation_identity_sha256"],
            idempotency_key=value["idempotency_key"],
            transition_index=value["transition_index"],
            event=OperationEvent(value["event"]),
            previous_status=OperationStatus(value["previous_status"]),
            current_status=OperationStatus(value["current_status"]),
            claim_generation=value["claim_generation"],
            dispatch_nonce=value["dispatch_nonce"],
            reason_code=value["reason_code"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise Phase4ShadowStoreError("operation transition receipt is malformed") from exc
    if receipt.schema_version != OPERATION_RECEIPT_SCHEMA:
        raise Phase4ShadowStoreError("operation transition receipt schema differs")
    return receipt


@dataclass(frozen=True)
class Phase4RuntimeState:
    operation: DurableOperation
    claim_owner_id: str | None = None
    claim_owner_epoch: int | None = None
    lease_expires_at: int | None = None
    retry_count: int = 0
    source_chain_binding: Phase4SourceChainBinding | None = None

    def __post_init__(self) -> None:
        if type(self.operation) is not DurableOperation:
            raise Phase4ShadowError("operation must be DurableOperation")
        if (
            self.source_chain_binding is not None
            and type(self.source_chain_binding) is not Phase4SourceChainBinding
        ):
            raise Phase4ShadowError(
                "source_chain_binding must be Phase4SourceChainBinding"
            )
        if (
            self.source_chain_binding is not None
            and self.operation.identity.outbox_command_id
            != self.source_chain_binding.authority_outbox_message_id
        ):
            raise Phase4ShadowError(
                "operation command does not match Authority outbox predecessor"
            )
        _nonnegative(self.retry_count, "retry_count")
        claim_values = (self.claim_owner_id, self.claim_owner_epoch)
        if any(value is None for value in claim_values):
            if any(value is not None for value in claim_values):
                raise Phase4ShadowError("claim owner identity must be complete")
            if self.operation.status is not OperationStatus.PENDING:
                raise Phase4ShadowError("non-pending operation requires claim ownership")
            if self.lease_expires_at is not None:
                raise Phase4ShadowError("unclaimed operation cannot have a lease")
            return
        _text(self.claim_owner_id, "claim_owner_id")
        epoch = _nonnegative(self.claim_owner_epoch, "claim_owner_epoch")
        if epoch < 1:
            raise Phase4ShadowError("claim_owner_epoch must be positive")
        if self.operation.status is OperationStatus.PENDING:
            raise Phase4ShadowError("pending operation cannot have claim ownership")
        if self.operation.status is OperationStatus.CLAIMED:
            if self.lease_expires_at is None:
                raise Phase4ShadowError("claimed operation requires a lease")
            _nonnegative(self.lease_expires_at, "lease_expires_at")
        elif self.lease_expires_at is not None:
            raise Phase4ShadowError("post-checkpoint operation cannot retain a claim lease")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PHASE4_SHADOW_SCHEMA_VERSION,
            "identity": self.operation.identity.as_dict(),
            "status": self.operation.status.value,
            "claim_generation": self.operation.claim_generation,
            "dispatch_nonce": self.operation.dispatch_nonce,
            "transition_index": self.operation.transition_index,
            "claim_owner_id": self.claim_owner_id,
            "claim_owner_epoch": self.claim_owner_epoch,
            "lease_expires_at": self.lease_expires_at,
            "retry_count": self.retry_count,
            "source_chain_binding": (
                None
                if self.source_chain_binding is None
                else self.source_chain_binding.as_dict()
            ),
            "authoritative": False,
            "dispatch_performed": False,
        }

    @property
    def state_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


def _state_from_dict(value: object) -> Phase4RuntimeState:
    if not isinstance(value, dict):
        raise Phase4ShadowStoreError("operation state is malformed")
    try:
        if value["schema_version"] != PHASE4_SHADOW_SCHEMA_VERSION:
            raise Phase4ShadowStoreError("operation state schema differs")
        if value["authoritative"] is not False or value["dispatch_performed"] is not False:
            raise Phase4ShadowStoreError("operation state claims runtime authority")
        identity = _identity_from_dict(value["identity"])
        operation = DurableOperation(
            identity=identity,
            status=OperationStatus(value["status"]),
            claim_generation=value["claim_generation"],
            dispatch_nonce=value["dispatch_nonce"],
            transition_index=value["transition_index"],
        )
        return Phase4RuntimeState(
            operation=operation,
            claim_owner_id=value["claim_owner_id"],
            claim_owner_epoch=value["claim_owner_epoch"],
            lease_expires_at=value["lease_expires_at"],
            retry_count=value["retry_count"],
            source_chain_binding=(
                None
                if value.get("source_chain_binding") is None
                else phase4_source_chain_binding_from_dict(
                    value["source_chain_binding"]
                )
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise Phase4ShadowStoreError("operation state is malformed") from exc


def phase4_runtime_state_from_dict(value: object) -> Phase4RuntimeState:
    """Public strict parser used by cross-store trusted-chain verification."""

    return _state_from_dict(value)


@dataclass(frozen=True)
class Phase4CommitReceipt:
    schema_version: str
    receipt_kind: str
    operation_identity_sha256: str
    request_idempotency_key: str
    request_sha256: str
    transition_index: int
    state_sha256: str
    occurred_at: int
    transition_receipt: OperationTransitionReceipt | None
    authoritative: bool = False
    dispatch_performed: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != PHASE4_SHADOW_RECEIPT_SCHEMA:
            raise Phase4ShadowError("Phase-4 receipt schema is unsupported")
        if self.receipt_kind not in {"INTENT_RESERVED", "TRANSITION_APPLIED"}:
            raise Phase4ShadowError("Phase-4 receipt kind is unsupported")
        _sha(self.operation_identity_sha256, "operation_identity_sha256")
        _text(self.request_idempotency_key, "request_idempotency_key")
        _sha(self.request_sha256, "request_sha256")
        _nonnegative(self.transition_index, "transition_index")
        _sha(self.state_sha256, "state_sha256")
        _nonnegative(self.occurred_at, "occurred_at")
        if self.authoritative is not False or self.dispatch_performed is not False:
            raise Phase4ShadowError("Phase-4 shadow receipt cannot claim dispatch or authority")
        if self.receipt_kind == "INTENT_RESERVED":
            if self.transition_index != 0 or self.transition_receipt is not None:
                raise Phase4ShadowError("intent receipt shape differs")
        elif (
            type(self.transition_receipt) is not OperationTransitionReceipt
            or self.transition_receipt.transition_index != self.transition_index
            or self.transition_receipt.operation_identity_sha256
            != self.operation_identity_sha256
        ):
            raise Phase4ShadowError("transition receipt binding differs")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "receipt_kind": self.receipt_kind,
            "operation_identity_sha256": self.operation_identity_sha256,
            "request_idempotency_key": self.request_idempotency_key,
            "request_sha256": self.request_sha256,
            "transition_index": self.transition_index,
            "state_sha256": self.state_sha256,
            "occurred_at": self.occurred_at,
            "transition_receipt": (
                None if self.transition_receipt is None else self.transition_receipt.as_dict()
            ),
            "authoritative": self.authoritative,
            "dispatch_performed": self.dispatch_performed,
        }

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


def _receipt_from_dict(value: object) -> Phase4CommitReceipt:
    if not isinstance(value, dict):
        raise Phase4ShadowStoreError("Phase-4 receipt is malformed")
    try:
        transition_value = value["transition_receipt"]
        return Phase4CommitReceipt(
            schema_version=value["schema_version"],
            receipt_kind=value["receipt_kind"],
            operation_identity_sha256=value["operation_identity_sha256"],
            request_idempotency_key=value["request_idempotency_key"],
            request_sha256=value["request_sha256"],
            transition_index=value["transition_index"],
            state_sha256=value["state_sha256"],
            occurred_at=value["occurred_at"],
            transition_receipt=(
                None
                if transition_value is None
                else _transition_receipt_from_dict(transition_value)
            ),
            authoritative=value["authoritative"],
            dispatch_performed=value["dispatch_performed"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise Phase4ShadowStoreError("Phase-4 receipt is malformed") from exc


@dataclass(frozen=True)
class Phase4CommitResult:
    state: Phase4RuntimeState
    receipt: Phase4CommitReceipt
    replayed: bool


@dataclass(frozen=True)
class Phase4ShadowRun:
    schema_version: str
    enabled: bool
    authoritative: bool
    dispatch_performed: bool
    state_sha256: str | None
    receipt_sha256: str | None
    replayed: bool
    run_sha256: str


def _run_identity(value: Phase4ShadowRun) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "enabled": value.enabled,
        "authoritative": value.authoritative,
        "dispatch_performed": value.dispatch_performed,
        "state_sha256": value.state_sha256,
        "receipt_sha256": value.receipt_sha256,
        "replayed": value.replayed,
    }


def _phase4_failure_point(_stage: str) -> None:
    """Test seam for transaction crash-window verification."""


def _phase4_initialization_failure_point(_stage: str) -> None:
    """Test seam for every narrow initialization ownership window."""


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS phase4_shadow_schema_state (
        singleton INTEGER PRIMARY KEY CHECK (singleton=1),
        schema_version TEXT NOT NULL,
        store_instance_id TEXT NOT NULL,
        creation_binding_json TEXT NOT NULL,
        schema_digest_sha256 TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS phase4_shadow_outbox_intents (
        operation_identity_sha256 TEXT PRIMARY KEY,
        logical_idempotency_key TEXT NOT NULL UNIQUE,
        request_json TEXT NOT NULL,
        request_sha256 TEXT NOT NULL UNIQUE,
        identity_json TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS phase4_shadow_current (
        operation_identity_sha256 TEXT PRIMARY KEY,
        state_json TEXT NOT NULL,
        state_sha256 TEXT NOT NULL UNIQUE,
        updated_at INTEGER NOT NULL,
        FOREIGN KEY(operation_identity_sha256)
            REFERENCES phase4_shadow_outbox_intents(operation_identity_sha256)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS phase4_shadow_receipts (
        receipt_sha256 TEXT PRIMARY KEY,
        operation_identity_sha256 TEXT NOT NULL,
        transition_index INTEGER NOT NULL CHECK (transition_index >= 0),
        request_idempotency_key TEXT NOT NULL UNIQUE,
        request_sha256 TEXT NOT NULL,
        receipt_json TEXT NOT NULL,
        occurred_at INTEGER NOT NULL,
        UNIQUE(operation_identity_sha256, transition_index),
        FOREIGN KEY(operation_identity_sha256)
            REFERENCES phase4_shadow_outbox_intents(operation_identity_sha256)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS phase4_shadow_idempotency (
        request_idempotency_key TEXT PRIMARY KEY,
        operation_identity_sha256 TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        result_state_json TEXT NOT NULL,
        result_state_sha256 TEXT NOT NULL,
        result_receipt_json TEXT NOT NULL,
        result_receipt_sha256 TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        FOREIGN KEY(operation_identity_sha256)
            REFERENCES phase4_shadow_outbox_intents(operation_identity_sha256),
        FOREIGN KEY(result_receipt_sha256)
            REFERENCES phase4_shadow_receipts(receipt_sha256)
    )
    """,
)

_IMMUTABLE_TABLES = (
    "phase4_shadow_schema_state",
    "phase4_shadow_outbox_intents",
    "phase4_shadow_receipts",
    "phase4_shadow_idempotency",
)


class Phase4ShadowStore:
    """Standalone durable store; construction performs no filesystem access."""

    __slots__ = ("_path",)

    def __init__(self, database: str | Path) -> None:
        if not isinstance(database, (str, Path)) or not str(database):
            raise Phase4ShadowStoreError("an explicit shadow SQLite path is required")
        self._path = Path(database)
        if not self._path.is_absolute():
            raise Phase4ShadowStoreError("shadow SQLite path must be absolute")

    @property
    def path(self) -> Path:
        return self._path

    def _validate_parent(self) -> None:
        parent = self._path.parent
        if not parent.is_dir():
            raise Phase4ShadowStoreError("shadow SQLite parent must exist")
        try:
            resolved_parent = parent.resolve(strict=True)
        except OSError as exc:
            raise Phase4ShadowStoreError("shadow SQLite parent is unavailable") from exc
        if resolved_parent != parent:
            raise Phase4ShadowStoreError("shadow SQLite parent cannot contain symlinks")

    def _open_parent_anchor(self) -> tuple[OwnedDescriptor, tuple[int, int]]:
        """Open and verify the directory used for exclusive creation/cleanup."""

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
                    owner="phase4-initializer",
                    label="Phase-4 parent anchor",
                )
            except OSError as exc:
                raise Phase4ShadowStoreError(
                    "shadow SQLite parent anchor is unavailable"
                ) from exc
            fd = lease.fileno("phase4-initializer")
            anchored = os.fstat(fd)
            current = os.stat(self._path.parent, follow_symlinks=False)
            if not stat.S_ISDIR(anchored.st_mode) or not stat.S_ISDIR(current.st_mode):
                raise Phase4ShadowStoreError("shadow SQLite parent must be a directory")
            identity = (int(anchored.st_dev), int(anchored.st_ino))
            if identity != (int(current.st_dev), int(current.st_ino)):
                raise Phase4ShadowStoreError(
                    "shadow SQLite parent changed while acquiring anchor"
                )
            return lease, identity
        except BaseException as primary:
            if lease is not None:
                run_cleanup(
                    [
                        (
                            "close Phase-4 parent anchor",
                            lease.cleanup("phase4-initializer"),
                        )
                    ],
                    primary=primary,
                )
            raise

    def _assert_parent_anchor(
        self, fd: int, expected: tuple[int, int]
    ) -> tuple[int, int]:
        try:
            anchored = os.fstat(fd)
            named = os.stat(self._path.parent, follow_symlinks=False)
        except OSError as exc:
            raise Phase4ShadowStoreError(
                "shadow SQLite parent anchor is unavailable"
            ) from exc
        if not stat.S_ISDIR(anchored.st_mode) or not stat.S_ISDIR(named.st_mode):
            raise Phase4ShadowStoreError("shadow SQLite parent anchor is not a directory")
        anchored_identity = (int(anchored.st_dev), int(anchored.st_ino))
        named_identity = (int(named.st_dev), int(named.st_ino))
        if anchored_identity != expected or named_identity != expected:
            raise Phase4ShadowStoreError(
                "shadow SQLite parent no longer names anchored inode"
            )
        return anchored_identity

    @staticmethod
    def _entry_identity(parent_fd: int, name: str) -> _FileIdentity:
        try:
            value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise Phase4ShadowStoreError(
                "shadow SQLite anchored directory entry is unavailable"
            ) from exc
        if not stat.S_ISREG(value.st_mode):
            raise Phase4ShadowStoreError("shadow SQLite path must be a regular file")
        if value.st_nlink != 1:
            raise Phase4ShadowStoreError("shadow SQLite file must have exactly one link")
        return _FileIdentity.from_stat(value)

    def _cleanup_exclusive_entry(
        self,
        *,
        parent_fd: int,
        expected_parent: tuple[int, int],
        anchor_fd: int,
        expected: _FileIdentity,
    ) -> bool:
        """Quarantine and remove only this call's exclusively created inode."""

        try:
            parent_stat = os.fstat(parent_fd)
            if (
                not stat.S_ISDIR(parent_stat.st_mode)
                or (int(parent_stat.st_dev), int(parent_stat.st_ino))
                != expected_parent
            ):
                raise Phase4ShadowStoreError(
                    "exclusive cleanup parent anchor differs"
                )
            anchored = self._identity_from_fd(anchor_fd)
            named = self._entry_identity(parent_fd, self._path.name)
        except Phase4ShadowStoreError as exc:
            raise Phase4ShadowStoreError(
                "exclusive cleanup ownership proof failed"
            ) from exc
        if (
            (anchored.device, anchored.inode)
            != (expected.device, expected.inode)
            or (named.device, named.inode)
            != (expected.device, expected.inode)
        ):
            raise Phase4ShadowStoreError(
                "exclusive cleanup refuses an entry with a different inode"
            )

        quarantine = (
            f".{self._path.name}.phase4-cleanup-"
            f"{os.getpid()}-{secrets.token_hex(16)}"
        )

        def identity_if_present(name: str) -> _FileIdentity | None:
            try:
                value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if not stat.S_ISREG(value.st_mode) or int(value.st_nlink) != 1:
                raise Phase4ShadowStoreError(
                    "exclusive cleanup recovery found a non-regular entry"
                )
            return _FileIdentity.from_stat(value)

        def finish_owned_cleanup() -> None:
            captured = identity_if_present(quarantine)
            original = identity_if_present(self._path.name)
            expected_inode = (expected.device, expected.inode)
            if captured is not None:
                if (captured.device, captured.inode) != expected_inode:
                    raise Phase4ShadowStoreError(
                        "exclusive cleanup recovery found a replacement quarantine"
                    )
            elif original is not None:
                if (original.device, original.inode) != expected_inode:
                    raise Phase4ShadowStoreError(
                        "exclusive cleanup recovery refuses a replacement entry"
                    )
                _raw_rename_noreplace(
                    parent_fd,
                    self._path.name,
                    parent_fd,
                    quarantine,
                )
                captured = identity_if_present(quarantine)
                if captured is None or (
                    captured.device,
                    captured.inode,
                ) != expected_inode:
                    raise Phase4ShadowStoreError(
                        "exclusive cleanup recovery lost the owned inode"
                    )
            else:
                os.fsync(parent_fd)
                return
            resilient_unlink_at(parent_fd, quarantine)
            os.fsync(parent_fd)

        try:
            _rename_noreplace(
                parent_fd,
                self._path.name,
                parent_fd,
                quarantine,
            )
            captured = self._entry_identity(parent_fd, quarantine)
            if (
                captured.device,
                captured.inode,
            ) != (expected.device, expected.inode):
                try:
                    _rename_noreplace(
                        parent_fd,
                        quarantine,
                        parent_fd,
                        self._path.name,
                    )
                finally:
                    os.fsync(parent_fd)
                raise Phase4ShadowStoreError(
                    "exclusive cleanup captured a replacement entry"
                )
            resilient_unlink_at(parent_fd, quarantine)
            os.fsync(parent_fd)
        except BaseException as primary:
            run_cleanup(
                [("finish Phase-4 owned cleanup", finish_owned_cleanup)],
                primary=primary,
            )
            raise
        return True

    def _assert_no_sidecars(self) -> None:
        for suffix in _SIDECAR_SUFFIXES:
            sidecar = str(self._path) + suffix
            if os.path.lexists(sidecar):
                raise Phase4ShadowStoreError(
                    f"shadow SQLite ownership preflight rejects existing {suffix} sidecar"
                )

    @staticmethod
    def _identity_from_fd(fd: int) -> _FileIdentity:
        value = os.fstat(fd)
        if not stat.S_ISREG(value.st_mode):
            raise Phase4ShadowStoreError("shadow SQLite path must be a regular file")
        if value.st_nlink != 1:
            raise Phase4ShadowStoreError("shadow SQLite file must have exactly one link")
        return _FileIdentity.from_stat(value)

    def _path_identity(self) -> _FileIdentity:
        try:
            value = os.lstat(self._path)
        except OSError as exc:
            raise Phase4ShadowStoreError("shadow SQLite path is unavailable") from exc
        if not stat.S_ISREG(value.st_mode):
            raise Phase4ShadowStoreError("shadow SQLite path must be a regular file")
        if value.st_nlink != 1:
            raise Phase4ShadowStoreError("shadow SQLite path must have exactly one link")
        return _FileIdentity.from_stat(value)

    def _assert_path_matches_anchor(
        self,
        fd: int,
        *,
        expected: _FileIdentity | None = None,
        exact: bool,
    ) -> _FileIdentity:
        anchored = self._identity_from_fd(fd)
        path_identity = self._path_identity()
        if (path_identity.device, path_identity.inode) != (
            anchored.device,
            anchored.inode,
        ):
            raise Phase4ShadowStoreError("shadow SQLite path no longer names anchored inode")
        if exact and path_identity != anchored:
            raise Phase4ShadowStoreError("shadow SQLite path metadata changed during fence")
        if expected is not None:
            if exact and anchored != expected:
                raise Phase4ShadowStoreError(
                    "shadow SQLite anchored file identity changed"
                )
            if not exact and (
                anchored.device,
                anchored.inode,
            ) != (expected.device, expected.inode):
                raise Phase4ShadowStoreError(
                    "shadow SQLite anchored file inode changed"
                )
        return anchored

    @staticmethod
    def _fd_uri(fd: int, query: str) -> str:
        return f"file:/proc/self/fd/{fd}?{query}"

    @staticmethod
    def _schema_inventory(connection: sqlite3.Connection) -> tuple[dict[str, object], ...]:
        rows = connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            ORDER BY type, name
            """
        ).fetchall()
        return tuple(
            {
                "type": str(row[0]),
                "name": str(row[1]),
                "table": str(row[2]),
                "sql": None if row[3] is None else str(row[3]),
            }
            for row in rows
        )

    def _verify_schema(
        self,
        connection: sqlite3.Connection,
        current_identity: _FileIdentity,
        *,
        expected_profile: str | None = None,
    ) -> str:
        inventory = self._schema_inventory(connection)
        names = frozenset(str(item["name"]) for item in inventory)
        digest = canonical_sha256(inventory)
        if names == _EXPECTED_SCHEMA_OBJECT_NAMES and digest == _EXPECTED_SCHEMA_DIGEST:
            profile = _BOUND_SCHEMA_PROFILE
        elif (
            names == _LEGACY_SCHEMA_OBJECT_NAMES and digest == _LEGACY_SCHEMA_DIGEST
        ):
            profile = _LEGACY_SCHEMA_PROFILE
        else:
            raise Phase4ShadowStoreError(
                "non-shadow SQLite schema objects or schema digest differ"
            )
        if expected_profile is not None and profile != expected_profile:
            raise Phase4ShadowStoreError("shadow SQLite schema profile changed")

        if profile == _LEGACY_SCHEMA_PROFILE:
            rows = connection.execute(
                "SELECT singleton, schema_version "
                "FROM phase4_shadow_schema_state ORDER BY singleton"
            ).fetchall()
            if len(rows) != 1 or tuple(rows[0]) != (
                1,
                PHASE4_SHADOW_SCHEMA_VERSION,
            ):
                raise Phase4ShadowStoreError(
                    "non-shadow SQLite legacy ownership marker differs"
                )
            return profile

        rows = connection.execute(
            "SELECT * FROM phase4_shadow_schema_state ORDER BY singleton"
        ).fetchall()
        if len(rows) != 1:
            raise Phase4ShadowStoreError("non-shadow SQLite ownership marker is unavailable")
        row = rows[0]
        if (
            row["singleton"] != 1
            or row["schema_version"] != PHASE4_SHADOW_SCHEMA_VERSION
            or row["schema_digest_sha256"] != _EXPECTED_SCHEMA_DIGEST
        ):
            raise Phase4ShadowStoreError("shadow SQLite ownership marker differs")
        binding = _decode_canonical(
            row["creation_binding_json"], row["store_instance_id"], "store ownership"
        )
        if not isinstance(binding, dict) or set(binding) != {
            "schema_version",
            "absolute_path",
            "created_file_identity",
            "parent_device",
            "parent_inode",
        }:
            raise Phase4ShadowStoreError("shadow SQLite ownership binding is malformed")
        created = binding["created_file_identity"]
        if (
            binding["schema_version"] != "phase4-shadow-store-ownership-v1"
            or binding["absolute_path"] != str(self._path)
            or not isinstance(created, dict)
            or set(created) != {"device", "inode", "size", "mtime_ns", "ctime_ns"}
            or created["device"] != current_identity.device
            or created["inode"] != current_identity.inode
            or created["size"] != 0
        ):
            raise Phase4ShadowStoreError("shadow SQLite ownership binding differs")
        parent_stat = os.stat(self._path.parent, follow_symlinks=False)
        if (
            binding["parent_device"] != int(parent_stat.st_dev)
            or binding["parent_inode"] != int(parent_stat.st_ino)
        ):
            raise Phase4ShadowStoreError("shadow SQLite parent ownership binding differs")
        return profile

    def _open_verified_anchor(
        self, *, owner: str
    ) -> tuple[OwnedDescriptor, _FileIdentity, str]:
        self._validate_parent()
        self._assert_no_sidecars()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        lease: OwnedDescriptor | None = None
        try:
            try:
                lease = OwnedDescriptor.from_opener(
                    lambda: os.open(self._path, flags),
                    owner=owner,
                    label="Phase-4 verified database anchor",
                )
            except OSError as exc:
                raise Phase4ShadowStoreError(
                    "shadow SQLite file is unavailable"
                ) from exc
            fd = lease.fileno(owner)
            before = self._assert_path_matches_anchor(fd, exact=True)
            header = os.pread(fd, 100, 0)
            after_header = self._assert_path_matches_anchor(
                fd, expected=before, exact=True
            )
            if len(header) != 100 or header[:16] != _SQLITE_HEADER:
                raise Phase4ShadowStoreError("shadow SQLite raw header is invalid")
            if header[18] != 1 or header[19] != 1:
                raise Phase4ShadowStoreError(
                    "shadow SQLite must use rollback-journal header versions"
                )
            self._assert_no_sidecars()
            connection: sqlite3.Connection | None = None
            preflight_error: BaseException | None = None
            try:
                connection = sqlite3.connect(
                    self._fd_uri(fd, "mode=ro&immutable=1"),
                    uri=True,
                    timeout=2,
                )
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                profile = self._verify_schema(connection, after_header)
            except BaseException as error:
                preflight_error = error
                raise
            finally:
                if connection is not None:
                    run_cleanup(
                        [
                            (
                                "close Phase-4 readonly preflight",
                                RetryableCleanup(connection.close),
                            )
                        ],
                        primary=preflight_error,
                    )
            after = self._assert_path_matches_anchor(
                fd, expected=before, exact=True
            )
            self._assert_no_sidecars()
            return lease, after, profile
        except (OSError, sqlite3.Error) as exc:
            if lease is not None:
                run_cleanup(
                    [("close Phase-4 verified anchor", lease.cleanup(owner))],
                    primary=exc,
                )
            raise Phase4ShadowStoreError(
                "non-shadow SQLite ownership preflight failed"
            ) from exc
        except BaseException as primary:
            if lease is not None:
                run_cleanup(
                    [("close Phase-4 verified anchor", lease.cleanup(owner))],
                    primary=primary,
                )
            raise

    def _owned_preflight(self) -> tuple[_FileIdentity, str]:
        lease: OwnedDescriptor | None = None
        preflight_error: BaseException | None = None
        try:
            lease, identity, profile = self._open_verified_anchor(
                owner="phase4-preflight"
            )
            return identity, profile
        except BaseException as error:
            preflight_error = error
            raise
        finally:
            if lease is not None:
                run_cleanup(
                    [
                        (
                            "close Phase-4 preflight anchor",
                            lease.cleanup("phase4-preflight"),
                        )
                    ],
                    primary=preflight_error,
                )

    def _connect(self, *, read_only: bool = False) -> _AnchoredConnection:
        lease: OwnedDescriptor | None = None
        connection: _AnchoredConnection | None = None
        try:
            lease, before, profile = self._open_verified_anchor(
                owner="phase4-connect"
            )
            connection = sqlite3.connect(
                self._fd_uri(
                    lease.fileno("phase4-connect"),
                    "mode=ro&immutable=1" if read_only else "mode=rw",
                ),
                uri=True,
                timeout=0 if read_only else 2,
                factory=_AnchoredConnection,
            )
            connection._adopt_anchor(lease, owner="phase4-connect")
            connection._anchor_identity = before
            connection._schema_profile = profile
            after_open = self._assert_path_matches_anchor(
                connection._anchor_fd, expected=before, exact=True
            )
            self._assert_no_sidecars()
            connection.row_factory = sqlite3.Row
            if read_only:
                connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA foreign_keys=ON")
            self._verify_schema(
                connection, after_open, expected_profile=profile
            )
            self._assert_path_matches_anchor(
                connection._anchor_fd, expected=before, exact=True
            )
            self._assert_no_sidecars()
            return connection
        except BaseException as primary:
            if connection is not None:
                run_cleanup(
                    [
                        (
                            "close Phase-4 connection",
                            RetryableCleanup(connection.close),
                        )
                    ],
                    primary=primary,
                )
            if (
                lease is not None
                and not lease.closed
                and lease.owner == "phase4-connect"
            ):
                run_cleanup(
                    [
                        (
                            "close Phase-4 connect anchor",
                            lease.cleanup("phase4-connect"),
                        )
                    ],
                    primary=primary,
                )
            raise

    def _begin(self, connection: _AnchoredConnection, *, immediate: bool) -> None:
        if (
            not isinstance(connection, _AnchoredConnection)
            or connection._anchor_fd is None
            or connection._anchor_identity is None
            or connection._schema_profile is None
        ):
            raise Phase4ShadowStoreError("shadow SQLite connection lacks ownership anchor")
        self._validate_parent()
        self._assert_no_sidecars()
        before = self._assert_path_matches_anchor(
            connection._anchor_fd,
            expected=connection._anchor_identity,
            exact=True,
        )
        self._verify_schema(
            connection,
            before,
            expected_profile=connection._schema_profile,
        )
        self._assert_no_sidecars()
        self._assert_path_matches_anchor(
            connection._anchor_fd,
            expected=before,
            exact=True,
        )
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        self._assert_path_matches_anchor(
            connection._anchor_fd,
            expected=before,
            exact=True,
        )

    def _commit_anchored(self, connection: _AnchoredConnection) -> None:
        """Commit only while the requested path still names the anchored inode."""

        if (
            not isinstance(connection, _AnchoredConnection)
            or connection._anchor_fd is None
            or connection._anchor_identity is None
        ):
            raise Phase4ShadowStoreError("shadow SQLite connection lacks ownership anchor")
        self._assert_path_matches_anchor(
            connection._anchor_fd,
            expected=connection._anchor_identity,
            exact=False,
        )
        connection.commit()
        self._assert_path_matches_anchor(
            connection._anchor_fd,
            expected=connection._anchor_identity,
            exact=False,
        )
        self._assert_no_sidecars()

    def _reconcile_initialization_outcome(
        self,
        *,
        connection: _AnchoredConnection | None,
        parent_fd: int,
        parent_identity: tuple[int, int],
        cleanup_fd: int,
        created_identity: _FileIdentity,
        commit_attempted: bool,
    ) -> str:
        """Classify the created inode without mutating it.

        ``uncommitted`` is returned only when the retained inode proves that no
        schema escaped the transaction.  ``committed`` requires the complete
        bound schema and a fresh ownership preflight.  Anything else is
        ``ambiguous`` and is never unlinked by the cleanup path.
        """

        parent_stat = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or (int(parent_stat.st_dev), int(parent_stat.st_ino))
            != parent_identity
        ):
            raise Phase4ShadowStoreError(
                "Phase-4 reconciliation parent anchor differs"
            )
        anchored = self._identity_from_fd(cleanup_fd)
        named = self._entry_identity(parent_fd, self._path.name)
        expected_inode = (created_identity.device, created_identity.inode)
        if (
            (anchored.device, anchored.inode) != expected_inode
            or (named.device, named.inode) != expected_inode
        ):
            raise Phase4ShadowStoreError(
                "Phase-4 reconciliation entry identity differs"
            )
        if os.fstat(cleanup_fd).st_size == 0:
            return "uncommitted"
        in_transaction = (
            bool(connection.in_transaction) if connection is not None else False
        )
        readonly: sqlite3.Connection | None = None
        readonly_error: BaseException | None = None
        try:
            readonly = sqlite3.connect(
                self._fd_uri(cleanup_fd, "mode=ro&immutable=1"),
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
                            "close Phase-4 reconciliation reader",
                            RetryableCleanup(readonly.close),
                        )
                    ],
                    primary=readonly_error,
                )
        if not commit_attempted or in_transaction:
            return "uncommitted" if inventory == () else "ambiguous"
        if (
            frozenset(str(item["name"]) for item in inventory)
            != _EXPECTED_SCHEMA_OBJECT_NAMES
            or canonical_sha256(inventory) != _EXPECTED_SCHEMA_DIGEST
        ):
            return "ambiguous"
        self._assert_parent_anchor(parent_fd, parent_identity)
        fresh, profile = self._owned_preflight()
        if (
            (fresh.device, fresh.inode)
            != (created_identity.device, created_identity.inode)
            or profile != _BOUND_SCHEMA_PROFILE
            or (anchored.device, anchored.inode)
            != (fresh.device, fresh.inode)
        ):
            return "ambiguous"
        self._assert_no_sidecars()
        os.fsync(parent_fd)
        return "committed"

    def _initialize_new(self) -> None:
        if not _rename_noreplace_supported():
            raise Phase4ShadowStoreError(
                "safe exclusive cleanup requires Linux renameat2"
            )
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        creator_raw = -1
        parent_fd = -1
        parent_lease: OwnedDescriptor | None = None
        parent_identity: tuple[int, int] | None = None
        creator: OwnedDescriptor | None = None
        cleanup_anchor: OwnedDescriptor | None = None
        connection: _AnchoredConnection | None = None
        created_identity: _FileIdentity | None = None
        commit_attempted = False
        initialization_error: BaseException | None = None
        try:
            parent_lease, parent_identity = self._open_parent_anchor()
            parent_fd = parent_lease.fileno("phase4-initializer")
            self._assert_parent_anchor(parent_fd, parent_identity)
            self._assert_no_sidecars()
            try:
                creator_raw = os.open(
                    self._path.name,
                    flags,
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError as exc:
                raise Phase4ShadowStoreError(
                    "shadow SQLite path appeared during exclusive creation"
                ) from exc
            except OSError as exc:
                raise Phase4ShadowStoreError(
                    "shadow SQLite exclusive creation failed"
                ) from exc
            creator = OwnedDescriptor(
                creator_raw,
                owner="phase4-creator",
                label="Phase-4 creator anchor",
            )
            creator_raw = -1
            _phase4_initialization_failure_point("after_creator_open")
            creator_fd = creator.fileno("phase4-creator")
            created_identity = self._assert_path_matches_anchor(
                creator_fd, exact=True
            )
            anchored_entry = self._entry_identity(parent_fd, self._path.name)
            if anchored_entry != created_identity:
                raise Phase4ShadowStoreError(
                    "exclusive shadow SQLite directory entry identity differs"
                )
            if created_identity.size != 0:
                raise Phase4ShadowStoreError(
                    "exclusive shadow SQLite file is not empty"
                )
            _phase4_initialization_failure_point("before_cleanup_dup")
            cleanup_anchor = creator.duplicate(
                owner="phase4-creator",
                new_owner="phase4-cleanup",
                label="Phase-4 cleanup anchor",
            )
            _phase4_initialization_failure_point("after_cleanup_dup")
            binding = {
                "schema_version": "phase4-shadow-store-ownership-v1",
                "absolute_path": str(self._path),
                "created_file_identity": created_identity.as_dict(),
                "parent_device": parent_identity[0],
                "parent_inode": parent_identity[1],
            }
            store_instance_id = canonical_sha256(binding)
            self._assert_parent_anchor(parent_fd, parent_identity)
            self._assert_no_sidecars()
            _phase4_initialization_failure_point("before_connect")
            connection = sqlite3.connect(
                self._fd_uri(creator.fileno("phase4-creator"), "mode=rw"),
                uri=True,
                timeout=2,
                factory=_AnchoredConnection,
            )
            _phase4_initialization_failure_point("after_connect_before_transfer")
            connection._adopt_anchor(creator, owner="phase4-creator")
            connection._anchor_identity = created_identity
            connection._schema_profile = _BOUND_SCHEMA_PROFILE
            _phase4_initialization_failure_point("after_connection_transfer")
            connection.row_factory = sqlite3.Row
            self._assert_parent_anchor(parent_fd, parent_identity)
            self._assert_path_matches_anchor(
                connection._anchor_fd, expected=created_identity, exact=True
            )
            self._assert_no_sidecars()
            connection.execute("PRAGMA foreign_keys=ON")
            self._assert_path_matches_anchor(
                connection._anchor_fd, expected=created_identity, exact=True
            )
            connection.execute("BEGIN IMMEDIATE")
            self._assert_path_matches_anchor(
                connection._anchor_fd, expected=created_identity, exact=True
            )
            _phase4_initialization_failure_point("before_schema")
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
            inventory = self._schema_inventory(connection)
            if (
                frozenset(str(item["name"]) for item in inventory)
                != _EXPECTED_SCHEMA_OBJECT_NAMES
                or canonical_sha256(inventory) != _EXPECTED_SCHEMA_DIGEST
            ):
                raise Phase4ShadowStoreError(
                    "new Phase-4 schema objects or schema digest differ"
                )
            connection.execute(
                """
                INSERT INTO phase4_shadow_schema_state(
                    singleton, schema_version, store_instance_id,
                    creation_binding_json, schema_digest_sha256
                ) VALUES (1, ?, ?, ?, ?)
                """,
                (
                    PHASE4_SHADOW_SCHEMA_VERSION,
                    store_instance_id,
                    _canonical_json(binding),
                    _EXPECTED_SCHEMA_DIGEST,
                ),
            )
            _phase4_initialization_failure_point("after_schema_before_commit")
            commit_attempted = True
            _phase4_initialization_failure_point("before_commit")
            self._commit_anchored(connection)
            _phase4_initialization_failure_point("before_parent_fsync")
            os.fsync(parent_fd)
            _phase4_initialization_failure_point("after_parent_fsync")
            _phase4_initialization_failure_point("after_commit")
            connection.close()
            if connection._resources_closed:
                connection = None
            _phase4_initialization_failure_point("before_final_preflight")
            post, profile = self._owned_preflight()
            if (
                (post.device, post.inode)
                != (created_identity.device, created_identity.inode)
                or profile != _BOUND_SCHEMA_PROFILE
            ):
                raise Phase4ShadowStoreError(
                    "initialized shadow SQLite identity differs"
                )
            _phase4_initialization_failure_point("after_final_preflight")
        except BaseException as exc:
            initialization_error = exc
            if connection is not None:
                def rollback_if_active() -> None:
                    if connection is not None and connection.in_transaction:
                        connection.rollback()

                run_cleanup(
                    [("rollback Phase-4 initialization", rollback_if_active)],
                    primary=exc,
                )
            if connection is not None:
                run_cleanup(
                    [
                        (
                            "close Phase-4 initialization connection",
                            RetryableCleanup(connection.close),
                        )
                    ],
                    primary=exc,
                )
                if connection._resources_closed:
                    connection = None
            outcome = "unavailable"
            cleanup_fd: int | None = None
            if cleanup_anchor is not None and not cleanup_anchor.closed:
                cleanup_fd = cleanup_anchor.fileno("phase4-cleanup")
            elif (
                creator is not None
                and not creator.closed
                and creator.owner == "phase4-creator"
            ):
                cleanup_fd = creator.fileno("phase4-creator")
            elif creator_raw >= 0:
                cleanup_fd = creator_raw
            if created_identity is None and cleanup_fd is not None:
                try:
                    anchored_probe = self._identity_from_fd(cleanup_fd)
                    named_probe = self._entry_identity(
                        parent_fd, self._path.name
                    )
                    if (
                        anchored_probe.device,
                        anchored_probe.inode,
                    ) != (named_probe.device, named_probe.inode):
                        raise Phase4ShadowStoreError(
                            "Phase-4 cleanup probe entry identity differs"
                        )
                    created_identity = anchored_probe
                except BaseException as cleanup_probe_error:
                    def report_cleanup_probe(
                        error: BaseException = cleanup_probe_error,
                    ) -> None:
                        raise error

                    run_cleanup(
                        [
                            (
                                "identify Phase-4 database for cleanup",
                                report_cleanup_probe,
                            )
                        ],
                        primary=exc,
                    )
            if created_identity is not None and cleanup_fd is not None:
                try:
                    outcome = self._reconcile_initialization_outcome(
                        connection=None,
                        parent_fd=parent_fd,
                        parent_identity=parent_identity,
                        cleanup_fd=cleanup_fd,
                        created_identity=created_identity,
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
                                "reconcile Phase-4 initialization outcome",
                                report_reconciliation_error,
                            )
                        ],
                        primary=exc,
                    )
                    outcome = "ambiguous"
                if outcome == "uncommitted":
                    run_cleanup(
                        [
                            (
                                "remove uncommitted Phase-4 database",
                                lambda: self._cleanup_exclusive_entry(
                                    parent_fd=parent_fd,
                                    expected_parent=parent_identity,
                                    anchor_fd=cleanup_fd,
                                    expected=created_identity,
                                ),
                            )
                        ],
                        primary=exc,
                    )
                elif outcome == "ambiguous":
                    def report_ambiguous() -> None:
                        raise Phase4ShadowStoreError(
                            "Phase-4 initialization outcome is ambiguous; "
                            "the owned inode was retained"
                        )

                    run_cleanup(
                        [("reconcile Phase-4 initialization", report_ambiguous)],
                        primary=exc,
                    )
            if isinstance(exc, Phase4ShadowStoreError):
                raise
            if isinstance(exc, (OSError, sqlite3.Error)):
                raise Phase4ShadowStoreError(
                    "exclusive shadow SQLite initialization fence failed"
                ) from exc
            raise
        finally:
            callbacks = []
            if connection is not None:
                callbacks.append(
                    (
                        "close Phase-4 connection",
                        RetryableCleanup(connection.close),
                    )
                )
            if cleanup_anchor is not None:
                callbacks.append(
                    (
                        "close Phase-4 cleanup anchor",
                        cleanup_anchor.cleanup("phase4-cleanup"),
                    )
                )
            if creator is not None:
                callbacks.append(
                    (
                        "close Phase-4 creator anchor",
                        creator.cleanup("phase4-creator"),
                    )
                )
            if creator_raw >= 0:
                callbacks.append(
                    (
                        "close raw Phase-4 creator descriptor",
                        lambda: close_raw_descriptor_if_unowned(
                            creator_raw, creator
                        ),
                    )
                )
            if parent_lease is not None:
                callbacks.append(
                    (
                        "close Phase-4 parent anchor",
                        parent_lease.cleanup("phase4-initializer"),
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
    def _state_row(row: sqlite3.Row) -> Phase4RuntimeState:
        decoded = _decode_canonical(row["state_json"], row["state_sha256"], "state")
        state = _state_from_dict(decoded)
        if state.state_sha256 != row["state_sha256"]:
            raise Phase4ShadowStoreError("state identity differs")
        return state

    @staticmethod
    def _receipt_row(row: sqlite3.Row) -> Phase4CommitReceipt:
        decoded = _decode_canonical(
            row["result_receipt_json"], row["result_receipt_sha256"], "receipt"
        )
        receipt = _receipt_from_dict(decoded)
        if receipt.receipt_sha256 != row["result_receipt_sha256"]:
            raise Phase4ShadowStoreError("receipt identity differs")
        return receipt

    def _replay(
        self,
        connection: sqlite3.Connection,
        *,
        request_idempotency_key: str,
        request_sha256: str,
    ) -> Phase4CommitResult | None:
        row = connection.execute(
            "SELECT * FROM phase4_shadow_idempotency WHERE request_idempotency_key=?",
            (request_idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_sha256:
            raise Phase4ShadowIdempotencyConflict(
                "Phase-4 request idempotency key is bound to different bytes"
            )
        state_decoded = _decode_canonical(
            row["result_state_json"], row["result_state_sha256"], "replay state"
        )
        state = _state_from_dict(state_decoded)
        receipt = self._receipt_row(row)
        persisted = connection.execute(
            "SELECT receipt_json FROM phase4_shadow_receipts WHERE receipt_sha256=?",
            (receipt.receipt_sha256,),
        ).fetchone()
        if persisted is None or persisted["receipt_json"] != row["result_receipt_json"]:
            raise Phase4ShadowStoreError("idempotency result receipt is unavailable")
        if state.state_sha256 != receipt.state_sha256:
            raise Phase4ShadowStoreError("idempotency result state binding differs")
        return Phase4CommitResult(state, receipt, True)

    @staticmethod
    def _insert_result(
        connection: sqlite3.Connection,
        *,
        state: Phase4RuntimeState,
        receipt: Phase4CommitReceipt,
        request_idempotency_key: str,
        request_sha256: str,
        occurred_at: int,
    ) -> None:
        state_json = _canonical_json(state.as_dict())
        receipt_json = _canonical_json(receipt.as_dict())
        connection.execute(
            """
            INSERT INTO phase4_shadow_receipts(
                receipt_sha256, operation_identity_sha256, transition_index,
                request_idempotency_key, request_sha256, receipt_json, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt.receipt_sha256,
                receipt.operation_identity_sha256,
                receipt.transition_index,
                request_idempotency_key,
                request_sha256,
                receipt_json,
                occurred_at,
            ),
        )
        _phase4_failure_point("after_receipt")
        connection.execute(
            """
            INSERT INTO phase4_shadow_idempotency(
                request_idempotency_key, operation_identity_sha256, request_sha256,
                result_state_json, result_state_sha256, result_receipt_json,
                result_receipt_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_idempotency_key,
                receipt.operation_identity_sha256,
                request_sha256,
                state_json,
                state.state_sha256,
                receipt_json,
                receipt.receipt_sha256,
                occurred_at,
            ),
        )
        _phase4_failure_point("after_idempotency")

    def reserve_operation(
        self,
        identity: DurableOperationIdentity,
        *,
        occurred_at: int,
        source_chain_binding: Phase4SourceChainBinding | None = None,
    ) -> Phase4CommitResult:
        if type(identity) is not DurableOperationIdentity:
            raise Phase4ShadowError("identity must be DurableOperationIdentity")
        now = _nonnegative(occurred_at, "occurred_at")
        if (
            source_chain_binding is not None
            and type(source_chain_binding) is not Phase4SourceChainBinding
        ):
            raise Phase4ShadowError(
                "source_chain_binding must be Phase4SourceChainBinding"
            )
        request = {
            "schema_version": "phase4-shadow-launch-intent-request-v1",
            "operation_type": identity.operation_type.value,
            "invocation_id": identity.invocation_id,
            "attempt_id": identity.attempt_id,
            "process_scope_id": identity.process_scope_id,
            "payload_sha256": identity.payload_sha256,
            "logical_idempotency_key": identity.idempotency_key,
            "source_chain_binding": (
                None
                if source_chain_binding is None
                else source_chain_binding.as_dict()
            ),
        }
        request_sha = canonical_sha256(request)
        request_key = _text(identity.idempotency_key, "logical_idempotency_key")
        connection = self._connect()
        transaction_error: BaseException | None = None
        try:
            self._begin(connection, immediate=True)
            replay = self._replay(
                connection,
                request_idempotency_key=request_key,
                request_sha256=request_sha,
            )
            if replay is not None:
                self._commit_anchored(connection)
                return replay
            existing = connection.execute(
                """
                SELECT request_sha256 FROM phase4_shadow_outbox_intents
                WHERE logical_idempotency_key=?
                """,
                (request_key,),
            ).fetchone()
            if existing is not None:
                raise Phase4ShadowStoreError("launch intent lacks its atomic idempotency result")
            state = Phase4RuntimeState(
                DurableOperation(identity),
                source_chain_binding=source_chain_binding,
            )
            receipt = Phase4CommitReceipt(
                PHASE4_SHADOW_RECEIPT_SCHEMA,
                "INTENT_RESERVED",
                identity.identity_sha256,
                request_key,
                request_sha,
                0,
                state.state_sha256,
                now,
                None,
            )
            connection.execute(
                """
                INSERT INTO phase4_shadow_outbox_intents(
                    operation_identity_sha256, logical_idempotency_key,
                    request_json, request_sha256, identity_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.identity_sha256,
                    request_key,
                    _canonical_json(request),
                    request_sha,
                    _canonical_json(identity.as_dict()),
                    now,
                ),
            )
            _phase4_failure_point("after_intent")
            connection.execute(
                """
                INSERT INTO phase4_shadow_current(
                    operation_identity_sha256, state_json, state_sha256, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    identity.identity_sha256,
                    _canonical_json(state.as_dict()),
                    state.state_sha256,
                    now,
                ),
            )
            _phase4_failure_point("after_current")
            self._insert_result(
                connection,
                state=state,
                receipt=receipt,
                request_idempotency_key=request_key,
                request_sha256=request_sha,
                occurred_at=now,
            )
            self._commit_anchored(connection)
            return Phase4CommitResult(state, receipt, False)
        except BaseException as primary:
            transaction_error = primary
            run_cleanup(
                [("rollback Phase-4 reserve transaction", connection.rollback)],
                primary=primary,
            )
            raise
        finally:
            run_cleanup(
                [
                    (
                        "close Phase-4 reserve connection",
                        RetryableCleanup(connection.close),
                    )
                ],
                primary=transaction_error,
            )

    def _load_current(
        self, connection: sqlite3.Connection, operation_identity_sha256: str
    ) -> Phase4RuntimeState:
        row = connection.execute(
            "SELECT * FROM phase4_shadow_current WHERE operation_identity_sha256=?",
            (operation_identity_sha256,),
        ).fetchone()
        if row is None:
            raise Phase4ShadowFenceError("durable operation is unavailable")
        state = self._state_row(row)
        if state.operation.identity.identity_sha256 != operation_identity_sha256:
            raise Phase4ShadowStoreError("current operation identity differs")
        return state

    def load(self, operation_identity_sha256: str) -> Phase4RuntimeState:
        identity_sha = _sha(operation_identity_sha256, "operation_identity_sha256")
        connection = self._connect(read_only=True)
        load_error: BaseException | None = None
        try:
            self._begin(connection, immediate=False)
            state = self._load_current(connection, identity_sha)
            self._commit_anchored(connection)
            return state
        except BaseException as error:
            load_error = error
            raise
        finally:
            run_cleanup(
                [
                    (
                        "close Phase-4 load connection",
                        RetryableCleanup(connection.close),
                    )
                ],
                primary=load_error,
            )

    def load_current_by_state_sha256(self, state_sha256: str) -> Phase4RuntimeState:
        """Load the one exact current P4 head selected by its bound state hash."""

        digest = _sha(state_sha256, "state_sha256")
        connection = self._connect(read_only=True)
        error: BaseException | None = None
        try:
            row = connection.execute(
                "SELECT * FROM phase4_shadow_current WHERE state_sha256=?",
                (digest,),
            ).fetchone()
            if row is None:
                raise Phase4ShadowFenceError("durable operation current head is unavailable")
            state = self._state_row(row)
            if state.state_sha256 != digest:
                raise Phase4ShadowStoreError("current state identity differs")
            return state
        except BaseException as exc:
            error = exc
            raise
        finally:
            run_cleanup(
                [("close Phase-4 current-head reader", RetryableCleanup(connection.close))],
                primary=error,
            )

    def load_current_for_source_chain(
        self,
        *,
        workflow_id: str,
        source_chain_binding_sha256: str,
    ) -> Phase4RuntimeState:
        """Select the unique current P4 head for one trusted logical source.

        An operation identity supplied by a caller is not a current selector:
        an older successful operation remains loadable after another operation
        is reserved for the same Authority/Phase-3 source.  This reader parses
        every durable current row and rejects both absence and ambiguity.
        """

        workflow = _text(workflow_id, "workflow_id")
        binding_sha = _sha(
            source_chain_binding_sha256,
            "source_chain_binding_sha256",
        )
        connection = self._connect(read_only=True)
        error: BaseException | None = None
        try:
            self._begin(connection, immediate=False)
            rows = connection.execute(
                "SELECT * FROM phase4_shadow_current"
            ).fetchall()
            matches: list[Phase4RuntimeState] = []
            for row in rows:
                state = self._state_row(row)
                source = state.source_chain_binding
                if (
                    source is not None
                    and source.workflow_id == workflow
                    and source.binding_sha256 == binding_sha
                ):
                    matches.append(state)
            if len(matches) != 1:
                raise Phase4ShadowFenceError(
                    "trusted source does not have exactly one durable P4 current head"
                )
            self._commit_anchored(connection)
            return matches[0]
        except BaseException as exc:
            error = exc
            raise
        finally:
            run_cleanup(
                [
                    (
                        "close Phase-4 trusted-source current reader",
                        RetryableCleanup(connection.close),
                    )
                ],
                primary=error,
            )

    def load_state_by_sha256(self, state_sha256: str) -> Phase4RuntimeState:
        """Load a hash-bound historical/current state from immutable results."""

        digest = _sha(state_sha256, "state_sha256")
        connection = self._connect(read_only=True)
        error: BaseException | None = None
        try:
            rows = connection.execute(
                """
                SELECT result_state_json AS state_json,
                       result_state_sha256 AS state_sha256
                FROM phase4_shadow_idempotency
                WHERE result_state_sha256=?
                UNION
                SELECT state_json,state_sha256 FROM phase4_shadow_current
                WHERE state_sha256=?
                """,
                (digest, digest),
            ).fetchall()
            states = tuple(self._state_row(row) for row in rows)
            if len(states) != 1 or states[0].state_sha256 != digest:
                raise Phase4ShadowFenceError("durable operation state is unavailable")
            return states[0]
        except BaseException as exc:
            error = exc
            raise
        finally:
            run_cleanup(
                [("close Phase-4 state reader", RetryableCleanup(connection.close))],
                primary=error,
            )

    def claim_operation(
        self,
        operation_identity_sha256: str,
        *,
        request_idempotency_key: str,
        claim_owner_id: str,
        claim_owner_epoch: int,
        expected_claim_generation: int,
        occurred_at: int,
        lease_seconds: int,
    ) -> Phase4CommitResult:
        identity_sha = _sha(operation_identity_sha256, "operation_identity_sha256")
        request_key = _text(request_idempotency_key, "request_idempotency_key")
        owner = _text(claim_owner_id, "claim_owner_id")
        owner_epoch = _nonnegative(claim_owner_epoch, "claim_owner_epoch")
        expected = _nonnegative(expected_claim_generation, "expected_claim_generation")
        now = _nonnegative(occurred_at, "occurred_at")
        lease = _nonnegative(lease_seconds, "lease_seconds")
        if owner_epoch < 1 or lease < 1:
            raise Phase4ShadowError("claim owner epoch and lease must be positive")
        request = {
            "schema_version": "phase4-shadow-claim-request-v1",
            "operation_identity_sha256": identity_sha,
            "claim_owner_id": owner,
            "claim_owner_epoch": owner_epoch,
            "expected_claim_generation": expected,
            "occurred_at": now,
            "lease_seconds": lease,
        }
        request_sha = canonical_sha256(request)
        connection = self._connect()
        transaction_error: BaseException | None = None
        try:
            self._begin(connection, immediate=True)
            replay = self._replay(
                connection,
                request_idempotency_key=request_key,
                request_sha256=request_sha,
            )
            if replay is not None:
                self._commit_anchored(connection)
                return replay
            current = self._load_current(connection, identity_sha)
            if current.operation.claim_generation != expected:
                raise Phase4ShadowFenceError("claim generation is stale")
            if current.operation.status is OperationStatus.PENDING:
                event = OperationEvent.CLAIM
                reason = "LEASE_ACQUIRED"
                retry_count = current.retry_count
            elif current.operation.status is OperationStatus.CLAIMED:
                if current.lease_expires_at is None or now < current.lease_expires_at:
                    raise Phase4ShadowFenceError("claim lease has not expired")
                event = OperationEvent.RECLAIM_EXPIRED
                reason = "CLAIM_LEASE_EXPIRED"
                retry_count = current.retry_count + 1
            else:
                raise Phase4ShadowFenceError("operation is not claimable")
            operation, transition_receipt = transition_operation(
                current.operation,
                event,
                expected_claim_generation=expected,
                reason_code=reason,
            )
            updated = Phase4RuntimeState(
                operation,
                claim_owner_id=owner,
                claim_owner_epoch=owner_epoch,
                lease_expires_at=now + lease,
                retry_count=retry_count,
                source_chain_binding=current.source_chain_binding,
            )
            receipt = Phase4CommitReceipt(
                PHASE4_SHADOW_RECEIPT_SCHEMA,
                "TRANSITION_APPLIED",
                identity_sha,
                request_key,
                request_sha,
                operation.transition_index,
                updated.state_sha256,
                now,
                transition_receipt,
            )
            changed = connection.execute(
                """
                UPDATE phase4_shadow_current
                SET state_json=?, state_sha256=?, updated_at=?
                WHERE operation_identity_sha256=? AND state_sha256=?
                """,
                (
                    _canonical_json(updated.as_dict()),
                    updated.state_sha256,
                    now,
                    identity_sha,
                    current.state_sha256,
                ),
            )
            if changed.rowcount != 1:
                raise Phase4ShadowFenceError("operation state CAS was lost")
            _phase4_failure_point("after_current")
            self._insert_result(
                connection,
                state=updated,
                receipt=receipt,
                request_idempotency_key=request_key,
                request_sha256=request_sha,
                occurred_at=now,
            )
            self._commit_anchored(connection)
            return Phase4CommitResult(updated, receipt, False)
        except BaseException as primary:
            transaction_error = primary
            run_cleanup(
                [("rollback Phase-4 claim transaction", connection.rollback)],
                primary=primary,
            )
            raise
        finally:
            run_cleanup(
                [
                    (
                        "close Phase-4 claim connection",
                        RetryableCleanup(connection.close),
                    )
                ],
                primary=transaction_error,
            )

    def transition(
        self,
        operation_identity_sha256: str,
        event: OperationEvent | str,
        *,
        request_idempotency_key: str,
        expected_claim_generation: int,
        claim_owner_id: str,
        claim_owner_epoch: int,
        dispatch_nonce: str | None,
        reason_code: str,
        occurred_at: int,
    ) -> Phase4CommitResult:
        identity_sha = _sha(operation_identity_sha256, "operation_identity_sha256")
        request_key = _text(request_idempotency_key, "request_idempotency_key")
        owner = _text(claim_owner_id, "claim_owner_id")
        owner_epoch = _nonnegative(claim_owner_epoch, "claim_owner_epoch")
        expected = _nonnegative(expected_claim_generation, "expected_claim_generation")
        now = _nonnegative(occurred_at, "occurred_at")
        try:
            normalized_event = OperationEvent(event)
        except (TypeError, ValueError) as exc:
            raise Phase4ShadowError(f"unsupported operation event: {event!r}") from exc
        if owner_epoch < 1:
            raise Phase4ShadowError("claim_owner_epoch must be positive")
        if dispatch_nonce is not None:
            _text(dispatch_nonce, "dispatch_nonce")
        reason = _text(reason_code, "reason_code")
        request = {
            "schema_version": "phase4-shadow-transition-request-v1",
            "operation_identity_sha256": identity_sha,
            "event": normalized_event.value,
            "expected_claim_generation": expected,
            "claim_owner_id": owner,
            "claim_owner_epoch": owner_epoch,
            "dispatch_nonce": dispatch_nonce,
            "reason_code": reason,
            "occurred_at": now,
        }
        request_sha = canonical_sha256(request)
        connection = self._connect()
        transaction_error: BaseException | None = None
        try:
            self._begin(connection, immediate=True)
            replay = self._replay(
                connection,
                request_idempotency_key=request_key,
                request_sha256=request_sha,
            )
            if replay is not None:
                self._commit_anchored(connection)
                return replay
            current = self._load_current(connection, identity_sha)
            if current.operation.claim_generation != expected:
                raise Phase4ShadowFenceError("claim generation is stale")
            if current.claim_owner_id != owner or current.claim_owner_epoch != owner_epoch:
                raise Phase4ShadowFenceError("claim owner fence is stale")
            if (
                current.operation.status is OperationStatus.CLAIMED
                and current.lease_expires_at is not None
                and now >= current.lease_expires_at
            ):
                raise Phase4ShadowFenceError("claim lease expired before transition")
            operation, transition_receipt = transition_operation(
                current.operation,
                normalized_event,
                expected_claim_generation=expected,
                dispatch_nonce=dispatch_nonce,
                reason_code=reason,
            )
            updated = Phase4RuntimeState(
                operation,
                claim_owner_id=owner,
                claim_owner_epoch=owner_epoch,
                lease_expires_at=(
                    current.lease_expires_at
                    if operation.status is OperationStatus.CLAIMED
                    else None
                ),
                retry_count=current.retry_count,
                source_chain_binding=current.source_chain_binding,
            )
            receipt = Phase4CommitReceipt(
                PHASE4_SHADOW_RECEIPT_SCHEMA,
                "TRANSITION_APPLIED",
                identity_sha,
                request_key,
                request_sha,
                operation.transition_index,
                updated.state_sha256,
                now,
                transition_receipt,
            )
            changed = connection.execute(
                """
                UPDATE phase4_shadow_current
                SET state_json=?, state_sha256=?, updated_at=?
                WHERE operation_identity_sha256=? AND state_sha256=?
                """,
                (
                    _canonical_json(updated.as_dict()),
                    updated.state_sha256,
                    now,
                    identity_sha,
                    current.state_sha256,
                ),
            )
            if changed.rowcount != 1:
                raise Phase4ShadowFenceError("operation state CAS was lost")
            _phase4_failure_point("after_current")
            self._insert_result(
                connection,
                state=updated,
                receipt=receipt,
                request_idempotency_key=request_key,
                request_sha256=request_sha,
                occurred_at=now,
            )
            self._commit_anchored(connection)
            return Phase4CommitResult(updated, receipt, False)
        except BaseException as primary:
            transaction_error = primary
            run_cleanup(
                [("rollback Phase-4 transition transaction", connection.rollback)],
                primary=primary,
            )
            raise
        finally:
            run_cleanup(
                [
                    (
                        "close Phase-4 transition connection",
                        RetryableCleanup(connection.close),
                    )
                ],
                primary=transaction_error,
            )

    def table_counts(self) -> dict[str, int]:
        connection = self._connect()
        count_error: BaseException | None = None
        try:
            self._begin(connection, immediate=False)
            return {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "phase4_shadow_outbox_intents",
                    "phase4_shadow_current",
                    "phase4_shadow_receipts",
                    "phase4_shadow_idempotency",
                )
            }
        except BaseException as error:
            count_error = error
            raise
        finally:
            run_cleanup(
                [
                    (
                        "close Phase-4 count connection",
                        RetryableCleanup(connection.close),
                    )
                ],
                primary=count_error,
            )


def run_phase4_full_shadow(
    *,
    enabled: bool = PHASE4_SHADOW_DEFAULT_ENABLED,
    database: str | Path | None = None,
    identity: DurableOperationIdentity | None = None,
    occurred_at: int = 0,
) -> Phase4ShadowRun:
    """Reserve one durable synthetic launch intent after explicit enablement."""

    if type(enabled) is not bool:
        raise Phase4ShadowError("enabled must be a boolean")
    if not enabled:
        prototype = Phase4ShadowRun(
            PHASE4_SHADOW_RUN_SCHEMA,
            False,
            False,
            False,
            None,
            None,
            False,
            "0" * 64,
        )
        return replace(prototype, run_sha256=canonical_sha256(_run_identity(prototype)))
    if database is None or type(identity) is not DurableOperationIdentity:
        raise Phase4ShadowError("enabled Phase-4 shadow requires database and identity")
    store = Phase4ShadowStore(database)
    store.initialize()
    result = store.reserve_operation(identity, occurred_at=occurred_at)
    prototype = Phase4ShadowRun(
        PHASE4_SHADOW_RUN_SCHEMA,
        True,
        False,
        False,
        result.state.state_sha256,
        result.receipt.receipt_sha256,
        result.replayed,
        "0" * 64,
    )
    return replace(prototype, run_sha256=canonical_sha256(_run_identity(prototype)))
