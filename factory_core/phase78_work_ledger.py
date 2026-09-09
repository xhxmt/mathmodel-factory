"""Durable, default-off Phase-7/8 local-shadow work ledger.

The request spool is the enqueue durability boundary.  Exact canonical job
bytes are published there before the audited :class:`Phase4ShadowStore`
reserves the matching operation.  On restart, every valid spool member is
replayed into Phase 4, so a process interruption between those two boundaries
cannot lose an acknowledged job.

This module has no provider, Authority, outbox or process-launch port.  Its
``checkpoint_local_worker`` transition records only a local shadow-worker
intent; every public state and result keeps authority and dispatch safety flags
false.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass, replace
from enum import Enum
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

from .canonical import canonical_bytes, canonical_sha256
from .durable_operation import (
    OperationEvent,
    OperationStatus,
    build_worker_launch_identity,
)
from .fd_ownership import OwnedDescriptor, resilient_unlink_at, run_cleanup
from .phase4_shadow_runtime import (
    Phase4CommitReceipt,
    Phase4RuntimeState,
    Phase4ShadowStore,
)


PHASE78_WORK_LEDGER_DEFAULT_ENABLED = False
PHASE78_WORK_JOB_SCHEMA = "phase78-local-shadow-work-job-v1"
PHASE78_WORK_CANCELLATION_SCHEMA = (
    "phase78-local-shadow-work-cancellation-receipt-v1"
)
PHASE78_WORK_RUN_SCHEMA = "phase78-local-shadow-work-ledger-run-v1"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FINAL_NAME = re.compile(r"request-([0-9a-f]{64})\.json\Z")
_TEMP_NAME = re.compile(r"\.phase78-request-[0-9]+-[0-9a-f]{32}\.tmp\Z")
_CANCELLATION_FINAL_NAME = re.compile(r"cancellation-([0-9a-f]{64})\.json\Z")
_CANCELLATION_TEMP_NAME = re.compile(
    r"\.phase78-cancellation-[0-9]+-[0-9a-f]{32}\.tmp\Z"
)
_CANCELLATION_REASONS = frozenset({"user_cancel", "shutdown", "superseded"})
_MAX_JOB_BYTES = 4 * 1024 * 1024
_MAX_CANCELLATION_BYTES = 64 * 1024
_RENAME_NOREPLACE = 1


class Phase78WorkLedgerError(RuntimeError):
    """Base error for the local Phase-7/8 work boundary."""

    code = "PHASE78_WORK_ERROR"


class Phase78WorkContractError(Phase78WorkLedgerError):
    """A caller supplied a value outside the closed work contract."""

    code = "PHASE78_REQUEST_INVALID"


class Phase78WorkStoreError(Phase78WorkLedgerError):
    """The request spool or its Phase-4 projection cannot be verified."""

    code = "PHASE78_WORK_STORE_INVALID"


class Phase78WorkIdempotencyConflict(Phase78WorkLedgerError):
    """One caller idempotency key was rebound to different job bytes."""

    code = "PHASE78_IDEMPOTENCY_CONFLICT"


class Phase78WorkNotFound(Phase78WorkLedgerError):
    """The requested durable local shadow job does not exist."""

    code = "PHASE78_WORK_NOT_FOUND"


class Phase78WorkDeadlineExceeded(Phase78WorkLedgerError):
    """The caller's shared deadline has no remaining budget."""

    code = "PHASE78_DEADLINE_EXCEEDED"


class Phase78WorkKind(str, Enum):
    PHASE7_GROUNDING = "phase7-grounding"
    PHASE8_MATERIALIZATION = "phase8-materialization"
    PHASE78_PIPELINE = "phase78-pipeline"


class Phase78ReconcileOutcome(str, Enum):
    ACTIVE = "active"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise Phase78WorkContractError(f"{field} must be a canonical identifier")
    return value


def _nonnegative(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise Phase78WorkContractError(f"{field} must be a nonnegative integer")
    return value


def _sha256(value: object, field: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise Phase78WorkContractError(f"{field} must be a lowercase SHA-256")
    return value


def _cancellation_reason(value: object) -> str:
    if type(value) is not str or value not in _CANCELLATION_REASONS:
        raise Phase78WorkContractError(
            "cancellation_reason must be user_cancel, shutdown or superseded"
        )
    return value


def _exact_json_object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise Phase78WorkContractError(f"{field} must be an exact JSON object")
    try:
        raw = dict(value)
        wire = json.loads(canonical_bytes(raw).decode("utf-8"))
    except (Exception, RecursionError) as exc:
        raise Phase78WorkContractError(f"{field} is outside canonical JSON") from exc
    if type(wire) is not dict or raw != wire:
        raise Phase78WorkContractError(f"{field} must contain exact JSON-safe values")
    return wire


def _check_deadline(deadline: object | None) -> float | None:
    if deadline is None:
        return None
    check = getattr(deadline, "check", None)
    if check is not None:
        if not callable(check):
            raise Phase78WorkContractError("deadline check must be callable")
        # Deadline-owned timeout/cancellation exceptions are part of the
        # public error contract and must retain their code/reason unchanged.
        check("phase78-work-ledger")
    remaining = getattr(deadline, "remaining_seconds", None)
    if not callable(remaining):
        raise Phase78WorkContractError(
            "deadline must expose callable remaining_seconds()"
        )
    value = remaining()
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise Phase78WorkContractError("deadline remaining budget must be finite seconds")
    result = float(value)
    if result <= 0:
        raise Phase78WorkDeadlineExceeded("Phase-7/8 work deadline exhausted")
    return result


def _job_identity(value: "Phase78WorkJob") -> dict[str, object]:
    return {
        "schema_version": PHASE78_WORK_JOB_SCHEMA,
        "caller_idempotency_key": value.caller_idempotency_key,
        "workflow_id": value.workflow_id,
        "work_kind": value.work_kind.value,
        "payload": value.payload,
        "authoritative": False,
        "authority_transferred": False,
        "provider_call_performed": False,
        "outbox_dispatch_performed": False,
    }


@dataclass(frozen=True)
class Phase78WorkJob:
    caller_idempotency_key: str
    workflow_id: str
    work_kind: Phase78WorkKind
    payload: dict[str, object]
    job_sha256: str
    authoritative: bool = False
    authority_transferred: bool = False
    provider_call_performed: bool = False
    outbox_dispatch_performed: bool = False

    def __post_init__(self) -> None:
        if any(
            value is not False
            for value in (
                self.authoritative,
                self.authority_transferred,
                self.provider_call_performed,
                self.outbox_dispatch_performed,
            )
        ):
            raise Phase78WorkContractError(
                "local shadow job cannot claim authority or dispatch"
            )

    def as_dict(self) -> dict[str, object]:
        value = _job_identity(self)
        value["job_sha256"] = self.job_sha256
        return value

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.as_dict())


def _cancellation_identity(
    value: "Phase78WorkCancellationReceipt",
) -> dict[str, object]:
    return {
        "schema_version": PHASE78_WORK_CANCELLATION_SCHEMA,
        "caller_idempotency_key": value.caller_idempotency_key,
        "job_sha256": value.job_sha256,
        "operation_identity_sha256": value.operation_identity_sha256,
        "claim_generation": value.claim_generation,
        "claim_owner_id": value.claim_owner_id,
        "claim_owner_epoch": value.claim_owner_epoch,
        "local_worker_nonce": value.local_worker_nonce,
        "request_idempotency_key": value.request_idempotency_key,
        "cancellation_reason": value.cancellation_reason,
        "occurred_at": value.occurred_at,
        "authoritative": False,
        "authority_transferred": False,
        "provider_call_performed": False,
        "outbox_dispatch_performed": False,
    }


@dataclass(frozen=True)
class Phase78WorkCancellationReceipt:
    """Immutable cancellation intent published before Phase-4 transitions.

    The receipt is the durable cancellation idempotency boundary.  Its exact
    bytes bind the work generation, private lease token, classified reason and
    logical occurrence time.  It is intentionally local-shadow metadata and
    grants no authority or dispatch capability.
    """

    caller_idempotency_key: str
    job_sha256: str
    operation_identity_sha256: str
    claim_generation: int
    claim_owner_id: str
    claim_owner_epoch: int
    local_worker_nonce: str
    request_idempotency_key: str
    cancellation_reason: str
    occurred_at: int
    receipt_sha256: str
    authoritative: bool = False
    authority_transferred: bool = False
    provider_call_performed: bool = False
    outbox_dispatch_performed: bool = False

    def __post_init__(self) -> None:
        if any(
            value is not False
            for value in (
                self.authoritative,
                self.authority_transferred,
                self.provider_call_performed,
                self.outbox_dispatch_performed,
            )
        ):
            raise Phase78WorkContractError(
                "local cancellation receipt cannot claim authority or dispatch"
            )

    def as_dict(self) -> dict[str, object]:
        value = _cancellation_identity(self)
        value["receipt_sha256"] = self.receipt_sha256
        return value

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.as_dict())

    def public_summary(self) -> dict[str, object]:
        """Return classified status without disclosing the private lease token."""

        return {
            "schema_version": "phase78-local-shadow-work-cancellation-summary-v1",
            "cancellation_reason": self.cancellation_reason,
            "occurred_at": self.occurred_at,
            "receipt_sha256": self.receipt_sha256,
            "authoritative": False,
            "authority_transferred": False,
            "provider_call_performed": False,
            "outbox_dispatch_performed": False,
        }


def build_phase78_work_cancellation_receipt(
    *,
    caller_idempotency_key: str,
    job_sha256: str,
    operation_identity_sha256: str,
    claim_generation: int,
    claim_owner_id: str,
    claim_owner_epoch: int,
    local_worker_nonce: str,
    request_idempotency_key: str,
    cancellation_reason: str,
    occurred_at: int,
) -> Phase78WorkCancellationReceipt:
    prototype = Phase78WorkCancellationReceipt(
        _identifier(caller_idempotency_key, "caller_idempotency_key"),
        _sha256(job_sha256, "job_sha256"),
        _sha256(operation_identity_sha256, "operation_identity_sha256"),
        _nonnegative(claim_generation, "claim_generation"),
        _identifier(claim_owner_id, "claim_owner_id"),
        _nonnegative(claim_owner_epoch, "claim_owner_epoch"),
        _identifier(local_worker_nonce, "local_worker_nonce"),
        _identifier(request_idempotency_key, "request_idempotency_key"),
        _cancellation_reason(cancellation_reason),
        _nonnegative(occurred_at, "occurred_at"),
        "0" * 64,
    )
    return replace(
        prototype,
        receipt_sha256=canonical_sha256(_cancellation_identity(prototype)),
    )


def build_phase78_work_job(
    *,
    caller_idempotency_key: str,
    workflow_id: str,
    work_kind: Phase78WorkKind,
    payload: Mapping[str, object],
) -> Phase78WorkJob:
    key = _identifier(caller_idempotency_key, "caller_idempotency_key")
    workflow = _identifier(workflow_id, "workflow_id")
    if type(work_kind) is not Phase78WorkKind:
        raise Phase78WorkContractError("work_kind must be Phase78WorkKind")
    payload_value = _exact_json_object(payload, "payload")
    prototype = Phase78WorkJob(key, workflow, work_kind, payload_value, "0" * 64)
    return replace(prototype, job_sha256=canonical_sha256(_job_identity(prototype)))


def phase78_work_job_from_bytes(value: bytes) -> Phase78WorkJob:
    if type(value) is not bytes or not value or len(value) > _MAX_JOB_BYTES:
        raise Phase78WorkStoreError("work job bytes are empty or exceed the size limit")
    try:
        decoded = json.loads(value.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Phase78WorkStoreError("work job is not canonical UTF-8 JSON") from exc
    expected = {
        "schema_version",
        "caller_idempotency_key",
        "workflow_id",
        "work_kind",
        "payload",
        "authoritative",
        "authority_transferred",
        "provider_call_performed",
        "outbox_dispatch_performed",
        "job_sha256",
    }
    if (
        type(decoded) is not dict
        or canonical_bytes(decoded) != value
        or set(decoded) != expected
        or decoded.get("schema_version") != PHASE78_WORK_JOB_SCHEMA
        or any(
            decoded.get(field) is not False
            for field in (
                "authoritative",
                "authority_transferred",
                "provider_call_performed",
                "outbox_dispatch_performed",
            )
        )
    ):
        raise Phase78WorkStoreError("work job fields, bytes or safety flags differ")
    try:
        job = build_phase78_work_job(
            caller_idempotency_key=decoded["caller_idempotency_key"],
            workflow_id=decoded["workflow_id"],
            work_kind=Phase78WorkKind(decoded["work_kind"]),
            payload=decoded["payload"],
        )
    except (KeyError, TypeError, ValueError, Phase78WorkContractError) as exc:
        raise Phase78WorkStoreError("work job does not revalidate") from exc
    try:
        supplied_sha256 = _sha256(decoded["job_sha256"], "job_sha256")
    except Phase78WorkContractError as exc:
        raise Phase78WorkStoreError("work job SHA-256 is malformed") from exc
    if job.as_dict() != decoded or job.job_sha256 != supplied_sha256:
        raise Phase78WorkStoreError("work job canonical SHA-256 differs")
    return job


def phase78_work_cancellation_receipt_from_bytes(
    value: bytes,
) -> Phase78WorkCancellationReceipt:
    if (
        type(value) is not bytes
        or not value
        or len(value) > _MAX_CANCELLATION_BYTES
    ):
        raise Phase78WorkStoreError(
            "cancellation receipt bytes are empty or exceed the size limit"
        )
    try:
        decoded = json.loads(value.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Phase78WorkStoreError(
            "cancellation receipt is not canonical UTF-8 JSON"
        ) from exc
    expected = {
        "schema_version",
        "caller_idempotency_key",
        "job_sha256",
        "operation_identity_sha256",
        "claim_generation",
        "claim_owner_id",
        "claim_owner_epoch",
        "local_worker_nonce",
        "request_idempotency_key",
        "cancellation_reason",
        "occurred_at",
        "receipt_sha256",
        "authoritative",
        "authority_transferred",
        "provider_call_performed",
        "outbox_dispatch_performed",
    }
    if (
        type(decoded) is not dict
        or canonical_bytes(decoded) != value
        or set(decoded) != expected
        or decoded.get("schema_version") != PHASE78_WORK_CANCELLATION_SCHEMA
        or any(
            decoded.get(field) is not False
            for field in (
                "authoritative",
                "authority_transferred",
                "provider_call_performed",
                "outbox_dispatch_performed",
            )
        )
    ):
        raise Phase78WorkStoreError(
            "cancellation receipt fields, bytes or safety flags differ"
        )
    try:
        receipt = build_phase78_work_cancellation_receipt(
            caller_idempotency_key=decoded["caller_idempotency_key"],
            job_sha256=decoded["job_sha256"],
            operation_identity_sha256=decoded["operation_identity_sha256"],
            claim_generation=decoded["claim_generation"],
            claim_owner_id=decoded["claim_owner_id"],
            claim_owner_epoch=decoded["claim_owner_epoch"],
            local_worker_nonce=decoded["local_worker_nonce"],
            request_idempotency_key=decoded["request_idempotency_key"],
            cancellation_reason=decoded["cancellation_reason"],
            occurred_at=decoded["occurred_at"],
        )
        supplied_sha256 = _sha256(
            decoded["receipt_sha256"], "receipt_sha256"
        )
    except (KeyError, TypeError, ValueError, Phase78WorkContractError) as exc:
        raise Phase78WorkStoreError(
            "cancellation receipt does not revalidate"
        ) from exc
    if receipt.as_dict() != decoded or receipt.receipt_sha256 != supplied_sha256:
        raise Phase78WorkStoreError(
            "cancellation receipt canonical SHA-256 differs"
        )
    return receipt


def _spool_key_sha256(caller_idempotency_key: str) -> str:
    return canonical_sha256(
        {
            "schema_version": "phase78-work-spool-key-v1",
            "caller_idempotency_key": _identifier(
                caller_idempotency_key, "caller_idempotency_key"
            ),
        }
    )


def _final_name(caller_idempotency_key: str) -> str:
    return f"request-{_spool_key_sha256(caller_idempotency_key)}.json"


def _cancellation_final_name(caller_idempotency_key: str) -> str:
    return f"cancellation-{_spool_key_sha256(caller_idempotency_key)}.json"


def _phase4_identity(job: Phase78WorkJob):
    key_sha = _spool_key_sha256(job.caller_idempotency_key)
    return build_worker_launch_identity(
        outbox_command_id=f"phase78-command-{key_sha}",
        invocation_id=f"phase78-invocation-{key_sha}",
        attempt_id="phase78-attempt-1",
        process_scope_id=f"phase78-scope-{key_sha}",
        payload_sha256=job.job_sha256,
    )


def _transition_key(job: Phase78WorkJob, base_key: str, stage: str) -> str:
    return "phase78-request-" + canonical_sha256(
        {
            "schema_version": "phase78-transition-idempotency-key-v1",
            "job_sha256": job.job_sha256,
            "base_key": _identifier(base_key, "request_idempotency_key"),
            "stage": _identifier(stage, "transition_stage"),
        }
    )


def _rename_noreplace(parent_fd: int, source: str, target: str) -> None:
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
        parent_fd,
        os.fsencode(source),
        parent_fd,
        os.fsencode(target),
        _RENAME_NOREPLACE,
    ) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), target)


@dataclass(frozen=True)
class Phase78WorkView:
    job: Phase78WorkJob
    state: Phase4RuntimeState
    cancellation: Phase78WorkCancellationReceipt | None = None

    @property
    def operation_identity_sha256(self) -> str:
        return self.state.operation.identity.identity_sha256

    @property
    def status(self) -> OperationStatus:
        return self.state.operation.status

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "phase78-local-shadow-work-view-v1",
            "job": self.job.as_dict(),
            "operation_identity_sha256": self.operation_identity_sha256,
            "status": self.status.value,
            "claim_generation": self.state.operation.claim_generation,
            "transition_index": self.state.operation.transition_index,
            "claim_owner_id": self.state.claim_owner_id,
            "claim_owner_epoch": self.state.claim_owner_epoch,
            "lease_expires_at": self.state.lease_expires_at,
            "retry_count": self.state.retry_count,
            "local_worker_launch_checkpointed": (
                self.state.operation.dispatch_nonce is not None
            ),
            "cancellation": (
                None
                if self.cancellation is None
                else self.cancellation.public_summary()
            ),
            "cancellation_reason": (
                None
                if self.cancellation is None
                else self.cancellation.cancellation_reason
            ),
            "authoritative": False,
            "authority_transferred": False,
            "provider_call_performed": False,
            "outbox_dispatch_performed": False,
        }


@dataclass(frozen=True)
class Phase78WorkCommitResult:
    view: Phase78WorkView
    receipt: Phase4CommitReceipt | None
    replayed: bool
    authoritative: bool = False
    authority_transferred: bool = False
    provider_call_performed: bool = False
    outbox_dispatch_performed: bool = False

    def __post_init__(self) -> None:
        if any(
            value is not False
            for value in (
                self.authoritative,
                self.authority_transferred,
                self.provider_call_performed,
                self.outbox_dispatch_performed,
            )
        ):
            raise Phase78WorkContractError(
                "local shadow result cannot claim authority or dispatch"
            )


@dataclass(frozen=True)
class Phase78WorkLedgerRun:
    schema_version: str
    enabled: bool
    ledger_verified: bool
    job_count: int
    authoritative: bool
    authority_transferred: bool
    provider_call_performed: bool
    outbox_dispatch_performed: bool
    run_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "enabled": self.enabled,
            "ledger_verified": self.ledger_verified,
            "job_count": self.job_count,
            "authoritative": self.authoritative,
            "authority_transferred": self.authority_transferred,
            "provider_call_performed": self.provider_call_performed,
            "outbox_dispatch_performed": self.outbox_dispatch_performed,
            "run_sha256": self.run_sha256,
        }


class Phase78WorkLedger:
    """Spool-backed composition over the audited Phase-4 state machine."""

    __slots__ = ("_database", "_spool", "_phase4", "_phase4_lock")

    def __init__(self, database: str | Path, request_spool: str | Path) -> None:
        if not isinstance(database, (str, Path)) or not str(database):
            raise Phase78WorkContractError("an explicit work database is required")
        if not isinstance(request_spool, (str, Path)) or not str(request_spool):
            raise Phase78WorkContractError("an explicit request spool is required")
        self._database = Path(database)
        self._spool = Path(request_spool)
        if not self._database.is_absolute() or not self._spool.is_absolute():
            raise Phase78WorkContractError("work database and spool must be absolute")
        if self._database == self._spool or self._spool in self._database.parents:
            raise Phase78WorkContractError("work database must be outside request spool")
        self._phase4 = Phase4ShadowStore(self._database)
        self._phase4_lock = threading.RLock()

    @property
    def database(self) -> Path:
        return self._database

    @property
    def request_spool(self) -> Path:
        return self._spool

    def _open_spool(self, *, create: bool) -> OwnedDescriptor:
        parent = self._spool.parent
        if not parent.is_dir():
            raise Phase78WorkStoreError("request spool parent must exist")
        try:
            if parent.resolve(strict=True) != parent:
                raise Phase78WorkStoreError("request spool parent contains symlinks")
        except OSError as exc:
            raise Phase78WorkStoreError("request spool parent is unavailable") from exc
        if create:
            try:
                os.mkdir(self._spool, mode=0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                raise Phase78WorkStoreError("request spool could not be created") from exc
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            lease = OwnedDescriptor.from_opener(
                lambda: os.open(self._spool, flags),
                owner="phase78-spool",
                label="Phase-7/8 request spool",
            )
        except OSError as exc:
            raise Phase78WorkStoreError("request spool is unavailable") from exc
        try:
            descriptor = lease.fileno("phase78-spool")
            anchored = os.fstat(descriptor)
            named = os.stat(self._spool, follow_symlinks=False)
            if (
                not stat.S_ISDIR(anchored.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or stat.S_IMODE(anchored.st_mode) != 0o700
                or stat.S_IMODE(named.st_mode) != 0o700
                or int(anchored.st_uid) != os.geteuid()
                or (int(anchored.st_dev), int(anchored.st_ino))
                != (int(named.st_dev), int(named.st_ino))
            ):
                raise Phase78WorkStoreError(
                    "request spool must be an owned anchored 0700 directory"
                )
            return lease
        except BaseException as primary:
            run_cleanup(
                [("close invalid Phase-7/8 request spool", lease.cleanup("phase78-spool"))],
                primary=primary,
            )
            raise

    @contextmanager
    def _phase4_guard(self, deadline: object | None = None):
        """Serialize Phase-4 exact-file preflight across ledger instances.

        Phase 4 intentionally fences the exact SQLite metadata before every
        connection.  A second writer changing that metadata during preflight
        is therefore rejected.  The private spool directory is already shared
        by every participant for this ledger, so its anchored descriptor is a
        natural advisory mutex without introducing a mutable lock file.
        """

        with self._phase4_lock:
            spool = self._open_spool(create=True)
            descriptor = spool.fileno("phase78-spool")
            locked = False
            primary: BaseException | None = None
            try:
                if deadline is None:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                    locked = True
                else:
                    while True:
                        remaining = _check_deadline(deadline)
                        try:
                            fcntl.flock(
                                descriptor,
                                fcntl.LOCK_EX | fcntl.LOCK_NB,
                            )
                            locked = True
                            break
                        except BlockingIOError:
                            time.sleep(min(0.01, remaining or 0.01))
                yield
            except BaseException as exc:
                primary = exc
                raise
            finally:
                callbacks = []
                if locked:
                    callbacks.append(
                        (
                            "unlock Phase-7/8 Phase-4 guard",
                            lambda: fcntl.flock(descriptor, fcntl.LOCK_UN),
                        )
                    )
                callbacks.append(
                    (
                        "close Phase-7/8 Phase-4 guard",
                        spool.cleanup("phase78-spool"),
                    )
                )
                run_cleanup(callbacks, primary=primary)

    @staticmethod
    def _entry_exists(parent_fd: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def _read_job(
        self,
        parent_fd: int,
        name: str,
        *,
        deadline: object | None = None,
    ) -> Phase78WorkJob:
        _check_deadline(deadline)
        owner = "phase78-spool-job"
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            lease = OwnedDescriptor.from_opener(
                lambda: os.open(name, flags, dir_fd=parent_fd),
                owner=owner,
                label="Phase-7/8 spooled job",
            )
        except OSError as exc:
            raise Phase78WorkStoreError("spooled job is unavailable") from exc
        primary: BaseException | None = None
        try:
            descriptor = lease.fileno(owner)
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or int(before.st_nlink) != 1
                or stat.S_IMODE(before.st_mode) != 0o600
                or int(before.st_uid) != os.geteuid()
                or int(before.st_size) <= 0
                or int(before.st_size) > _MAX_JOB_BYTES
            ):
                raise Phase78WorkStoreError(
                    "spooled job must be an owned single-link 0600 regular file"
                )
            chunks: list[bytes] = []
            remaining = _MAX_JOB_BYTES + 1
            while remaining:
                _check_deadline(deadline)
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(descriptor)
            if (
                len(raw) > _MAX_JOB_BYTES
                or (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                )
                != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )
            ):
                raise Phase78WorkStoreError("spooled job changed during read")
            return phase78_work_job_from_bytes(raw)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            run_cleanup(
                [("close Phase-7/8 spooled job", lease.cleanup(owner))],
                primary=primary,
            )

    def _read_cancellation(
        self,
        parent_fd: int,
        name: str,
        *,
        deadline: object | None = None,
    ) -> Phase78WorkCancellationReceipt:
        _check_deadline(deadline)
        owner = "phase78-spool-cancellation"
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            lease = OwnedDescriptor.from_opener(
                lambda: os.open(name, flags, dir_fd=parent_fd),
                owner=owner,
                label="Phase-7/8 cancellation receipt",
            )
        except OSError as exc:
            raise Phase78WorkStoreError(
                "cancellation receipt is unavailable"
            ) from exc
        primary: BaseException | None = None
        try:
            descriptor = lease.fileno(owner)
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or int(before.st_nlink) != 1
                or stat.S_IMODE(before.st_mode) != 0o600
                or int(before.st_uid) != os.geteuid()
                or int(before.st_size) <= 0
                or int(before.st_size) > _MAX_CANCELLATION_BYTES
            ):
                raise Phase78WorkStoreError(
                    "cancellation receipt must be an owned single-link 0600 regular file"
                )
            chunks: list[bytes] = []
            remaining = _MAX_CANCELLATION_BYTES + 1
            while remaining:
                _check_deadline(deadline)
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(descriptor)
            if (
                len(raw) > _MAX_CANCELLATION_BYTES
                or (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                )
                != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )
            ):
                raise Phase78WorkStoreError(
                    "cancellation receipt changed during read"
                )
            return phase78_work_cancellation_receipt_from_bytes(raw)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            run_cleanup(
                [("close Phase-7/8 cancellation receipt", lease.cleanup(owner))],
                primary=primary,
            )

    def _load_cancellation_for_job(
        self,
        job: Phase78WorkJob,
        *,
        deadline: object | None = None,
    ) -> Phase78WorkCancellationReceipt | None:
        spool = self._open_spool(create=True)
        primary: BaseException | None = None
        try:
            parent_fd = spool.fileno("phase78-spool")
            target = _cancellation_final_name(job.caller_idempotency_key)
            if not self._entry_exists(parent_fd, target):
                return None
            receipt = self._read_cancellation(
                parent_fd, target, deadline=deadline
            )
            if (
                receipt.caller_idempotency_key != job.caller_idempotency_key
                or receipt.job_sha256 != job.job_sha256
                or receipt.operation_identity_sha256
                != _phase4_identity(job).identity_sha256
            ):
                raise Phase78WorkStoreError(
                    "cancellation receipt and durable work identity differ"
                )
            return receipt
        except BaseException as exc:
            primary = exc
            raise
        finally:
            run_cleanup(
                [
                    (
                        "close cancellation receipt spool",
                        spool.cleanup("phase78-spool"),
                    )
                ],
                primary=primary,
            )

    def _publish_job(
        self,
        job: Phase78WorkJob,
        *,
        deadline: object | None = None,
    ) -> tuple[Phase78WorkJob, bool]:
        _check_deadline(deadline)
        spool = self._open_spool(create=True)
        parent_fd = spool.fileno("phase78-spool")
        target = _final_name(job.caller_idempotency_key)
        temp = f".phase78-request-{os.getpid()}-{secrets.token_hex(16)}.tmp"
        temp_lease: OwnedDescriptor | None = None
        primary: BaseException | None = None
        try:
            if self._entry_exists(parent_fd, target):
                existing = self._read_job(parent_fd, target, deadline=deadline)
                if existing.canonical_bytes != job.canonical_bytes:
                    raise Phase78WorkIdempotencyConflict(
                        "caller idempotency key is bound to different job bytes"
                    )
                return existing, True
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            temp_lease = OwnedDescriptor.from_opener(
                lambda: os.open(temp, flags, 0o600, dir_fd=parent_fd),
                owner="phase78-spool-writer",
                label="Phase-7/8 spool writer",
            )
            descriptor = temp_lease.fileno("phase78-spool-writer")
            os.fchmod(descriptor, 0o600)
            raw = job.canonical_bytes
            offset = 0
            while offset < len(raw):
                _check_deadline(deadline)
                written = os.write(descriptor, raw[offset:])
                if written <= 0:
                    raise Phase78WorkStoreError("spooled job write made no progress")
                offset += written
            os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            observed = b""
            while len(observed) < len(raw):
                _check_deadline(deadline)
                chunk = os.read(descriptor, len(raw) - len(observed))
                if not chunk:
                    break
                observed += chunk
            metadata = os.fstat(descriptor)
            if (
                observed != raw
                or not stat.S_ISREG(metadata.st_mode)
                or int(metadata.st_nlink) != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise Phase78WorkStoreError("spooled job re-read or identity differs")
            try:
                _rename_noreplace(parent_fd, temp, target)
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise Phase78WorkStoreError("spooled job publish failed") from exc
                existing = self._read_job(parent_fd, target, deadline=deadline)
                if existing.canonical_bytes != raw:
                    raise Phase78WorkIdempotencyConflict(
                        "caller idempotency key is bound to different job bytes"
                    )
                return existing, True
            os.fsync(parent_fd)
            published = self._read_job(parent_fd, target, deadline=deadline)
            if published.canonical_bytes != raw:
                raise Phase78WorkStoreError("published job bytes differ after rename")
            return published, False
        except BaseException as exc:
            primary = exc
            raise
        finally:
            callbacks = []
            if temp_lease is not None:
                callbacks.append(
                    (
                        "close Phase-7/8 spool writer",
                        temp_lease.cleanup("phase78-spool-writer"),
                    )
                )
            if self._entry_exists(parent_fd, temp):
                callbacks.append(
                    (
                        "unlink unpublished Phase-7/8 spool temp",
                        lambda: resilient_unlink_at(parent_fd, temp),
                    )
                )
                callbacks.append(("fsync Phase-7/8 spool temp cleanup", lambda: os.fsync(parent_fd)))
            callbacks.append(
                ("close Phase-7/8 request spool", spool.cleanup("phase78-spool"))
            )
            run_cleanup(callbacks, primary=primary)

    def _publish_cancellation(
        self,
        receipt: Phase78WorkCancellationReceipt,
        *,
        deadline: object | None = None,
    ) -> tuple[Phase78WorkCancellationReceipt, bool]:
        """Atomically publish the exact cancellation intent before transitions."""

        _check_deadline(deadline)
        spool = self._open_spool(create=True)
        parent_fd = spool.fileno("phase78-spool")
        target = _cancellation_final_name(receipt.caller_idempotency_key)
        temp = f".phase78-cancellation-{os.getpid()}-{secrets.token_hex(16)}.tmp"
        temp_lease: OwnedDescriptor | None = None
        primary: BaseException | None = None
        try:
            if self._entry_exists(parent_fd, target):
                existing = self._read_cancellation(
                    parent_fd, target, deadline=deadline
                )
                if existing.canonical_bytes != receipt.canonical_bytes:
                    raise Phase78WorkIdempotencyConflict(
                        "work cancellation request is bound to different bytes"
                    )
                return existing, True
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            temp_lease = OwnedDescriptor.from_opener(
                lambda: os.open(temp, flags, 0o600, dir_fd=parent_fd),
                owner="phase78-cancellation-writer",
                label="Phase-7/8 cancellation writer",
            )
            descriptor = temp_lease.fileno("phase78-cancellation-writer")
            os.fchmod(descriptor, 0o600)
            raw = receipt.canonical_bytes
            offset = 0
            while offset < len(raw):
                _check_deadline(deadline)
                written = os.write(descriptor, raw[offset:])
                if written <= 0:
                    raise Phase78WorkStoreError(
                        "cancellation receipt write made no progress"
                    )
                offset += written
            os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            observed = b""
            while len(observed) < len(raw):
                _check_deadline(deadline)
                chunk = os.read(descriptor, len(raw) - len(observed))
                if not chunk:
                    break
                observed += chunk
            metadata = os.fstat(descriptor)
            if (
                observed != raw
                or not stat.S_ISREG(metadata.st_mode)
                or int(metadata.st_nlink) != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise Phase78WorkStoreError(
                    "cancellation receipt re-read or identity differs"
                )
            try:
                _rename_noreplace(parent_fd, temp, target)
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise Phase78WorkStoreError(
                        "cancellation receipt publish failed"
                    ) from exc
                existing = self._read_cancellation(
                    parent_fd, target, deadline=deadline
                )
                if existing.canonical_bytes != raw:
                    raise Phase78WorkIdempotencyConflict(
                        "work cancellation request is bound to different bytes"
                    )
                return existing, True
            os.fsync(parent_fd)
            published = self._read_cancellation(
                parent_fd, target, deadline=deadline
            )
            if published.canonical_bytes != raw:
                raise Phase78WorkStoreError(
                    "published cancellation receipt differs after rename"
                )
            return published, False
        except BaseException as exc:
            primary = exc
            raise
        finally:
            callbacks = []
            if temp_lease is not None:
                callbacks.append(
                    (
                        "close Phase-7/8 cancellation writer",
                        temp_lease.cleanup("phase78-cancellation-writer"),
                    )
                )
            if self._entry_exists(parent_fd, temp):
                callbacks.append(
                    (
                        "unlink unpublished Phase-7/8 cancellation temp",
                        lambda: resilient_unlink_at(parent_fd, temp),
                    )
                )
                callbacks.append(
                    (
                        "fsync Phase-7/8 cancellation temp cleanup",
                        lambda: os.fsync(parent_fd),
                    )
                )
            callbacks.append(
                (
                    "close Phase-7/8 cancellation spool",
                    spool.cleanup("phase78-spool"),
                )
            )
            run_cleanup(callbacks, primary=primary)

    def _scan_jobs(
        self,
        *,
        deadline: object | None = None,
    ) -> tuple[Phase78WorkJob, ...]:
        _check_deadline(deadline)
        spool = self._open_spool(create=True)
        primary: BaseException | None = None
        try:
            parent_fd = spool.fileno("phase78-spool")
            names = sorted(os.listdir(parent_fd))
            for name in names:
                _check_deadline(deadline)
                if _TEMP_NAME.fullmatch(name) is None:
                    continue
                job = self._read_job(parent_fd, name, deadline=deadline)
                target = _final_name(job.caller_idempotency_key)
                try:
                    _rename_noreplace(parent_fd, name, target)
                except OSError as exc:
                    if exc.errno != errno.EEXIST:
                        raise Phase78WorkStoreError(
                            "orphan spool request could not be published"
                        ) from exc
                    existing = self._read_job(
                        parent_fd, target, deadline=deadline
                    )
                    if existing.canonical_bytes != job.canonical_bytes:
                        raise Phase78WorkIdempotencyConflict(
                            "orphan spool request conflicts with published job"
                        )
                    resilient_unlink_at(parent_fd, name)
                os.fsync(parent_fd)
            for name in sorted(os.listdir(parent_fd)):
                _check_deadline(deadline)
                if _CANCELLATION_TEMP_NAME.fullmatch(name) is None:
                    continue
                receipt = self._read_cancellation(
                    parent_fd, name, deadline=deadline
                )
                target = _cancellation_final_name(
                    receipt.caller_idempotency_key
                )
                try:
                    _rename_noreplace(parent_fd, name, target)
                except OSError as exc:
                    if exc.errno != errno.EEXIST:
                        raise Phase78WorkStoreError(
                            "orphan cancellation receipt could not be published"
                        ) from exc
                    existing = self._read_cancellation(
                        parent_fd, target, deadline=deadline
                    )
                    if existing.canonical_bytes != receipt.canonical_bytes:
                        raise Phase78WorkIdempotencyConflict(
                            "orphan cancellation receipt conflicts with published bytes"
                        )
                    resilient_unlink_at(parent_fd, name)
                os.fsync(parent_fd)
            jobs: list[Phase78WorkJob] = []
            cancellations: list[Phase78WorkCancellationReceipt] = []
            for name in sorted(os.listdir(parent_fd)):
                _check_deadline(deadline)
                match = _FINAL_NAME.fullmatch(name)
                cancellation_match = _CANCELLATION_FINAL_NAME.fullmatch(name)
                if match is None and cancellation_match is None:
                    raise Phase78WorkStoreError(
                        "request spool contains an unexpected entry"
                    )
                if cancellation_match is not None:
                    receipt = self._read_cancellation(
                        parent_fd, name, deadline=deadline
                    )
                    if name != _cancellation_final_name(
                        receipt.caller_idempotency_key
                    ):
                        raise Phase78WorkStoreError(
                            "cancellation filename and caller key differ"
                        )
                    cancellations.append(receipt)
                    continue
                job = self._read_job(parent_fd, name, deadline=deadline)
                if name != _final_name(job.caller_idempotency_key):
                    raise Phase78WorkStoreError(
                        "spooled job filename and caller key differ"
                    )
                jobs.append(job)
            keys = [item.caller_idempotency_key for item in jobs]
            if len(keys) != len(set(keys)):
                raise Phase78WorkStoreError("request spool contains duplicate caller keys")
            jobs_by_key = {item.caller_idempotency_key: item for item in jobs}
            cancellation_keys = [
                item.caller_idempotency_key for item in cancellations
            ]
            if len(cancellation_keys) != len(set(cancellation_keys)):
                raise Phase78WorkStoreError(
                    "request spool contains duplicate cancellation receipts"
                )
            for receipt in cancellations:
                job = jobs_by_key.get(receipt.caller_idempotency_key)
                if (
                    job is None
                    or receipt.job_sha256 != job.job_sha256
                    or receipt.operation_identity_sha256
                    != _phase4_identity(job).identity_sha256
                ):
                    raise Phase78WorkStoreError(
                        "cancellation receipt has no matching durable job"
                    )
            return tuple(sorted(jobs, key=lambda item: item.caller_idempotency_key))
        except BaseException as exc:
            primary = exc
            raise
        finally:
            run_cleanup(
                [("close scanned Phase-7/8 request spool", spool.cleanup("phase78-spool"))],
                primary=primary,
            )

    def _view(
        self,
        job: Phase78WorkJob,
        state: Phase4RuntimeState,
        *,
        deadline: object | None = None,
    ) -> Phase78WorkView:
        if state.operation.identity != _phase4_identity(job):
            raise Phase78WorkStoreError("spooled job and Phase-4 identity differ")
        if (
            state.as_dict()["authoritative"] is not False
            or state.as_dict()["dispatch_performed"] is not False
        ):
            raise Phase78WorkStoreError("Phase-4 work projection claims authority")
        cancellation = self._load_cancellation_for_job(
            job, deadline=deadline
        )
        if cancellation is not None and state.operation.status in {
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
        }:
            raise Phase78WorkStoreError(
                "terminal work conflicts with durable cancellation intent"
            )
        return Phase78WorkView(job, state, cancellation)

    def _commit(
        self,
        job: Phase78WorkJob,
        result,
        *,
        replayed: bool | None = None,
        deadline: object | None = None,
    ) -> Phase78WorkCommitResult:
        return Phase78WorkCommitResult(
            self._view(job, result.state, deadline=deadline),
            result.receipt,
            result.replayed if replayed is None else replayed,
        )

    def initialize(self, *, deadline: object | None = None) -> None:
        jobs = self._scan_jobs(deadline=deadline)
        with self._phase4_guard(deadline):
            _check_deadline(deadline)
            self._phase4.initialize()
            for job in jobs:
                _check_deadline(deadline)
                self._phase4.reserve_operation(_phase4_identity(job), occurred_at=0)

    def submit(
        self,
        *,
        caller_idempotency_key: str,
        workflow_id: str,
        work_kind: Phase78WorkKind,
        payload: Mapping[str, object],
        occurred_at: int,
        deadline: object | None = None,
    ) -> Phase78WorkCommitResult:
        _check_deadline(deadline)
        now = _nonnegative(occurred_at, "occurred_at")
        job = build_phase78_work_job(
            caller_idempotency_key=caller_idempotency_key,
            workflow_id=workflow_id,
            work_kind=work_kind,
            payload=payload,
        )
        with self._phase4_guard(deadline):
            # Publish and reserve under one cross-instance mutex.  Otherwise a
            # publisher can lose the Phase-4 race to a reader of its new spool
            # entry, causing both normal callers to report `replayed=false`.
            published, spool_replayed = self._publish_job(job, deadline=deadline)
            _check_deadline(deadline)
            result = self._phase4.reserve_operation(
                _phase4_identity(published), occurred_at=now
            )
        return self._commit(
            published,
            result,
            # A logical replay requires both durable boundaries to have seen
            # the request.  This also gives concurrent first-submit callers
            # one stable winner even if another thread reaches Phase 4 first.
            replayed=spool_replayed and result.replayed,
            deadline=deadline,
        )

    def _job_for_key(
        self,
        caller_idempotency_key: str,
        *,
        deadline: object | None = None,
    ) -> Phase78WorkJob:
        key = _identifier(caller_idempotency_key, "caller_idempotency_key")
        for job in self._scan_jobs(deadline=deadline):
            if job.caller_idempotency_key == key:
                return job
        raise Phase78WorkNotFound("spooled work job is unavailable")

    def load(
        self,
        caller_idempotency_key: str,
        *,
        deadline: object | None = None,
    ) -> Phase78WorkView:
        _check_deadline(deadline)
        job = self._job_for_key(caller_idempotency_key, deadline=deadline)
        _check_deadline(deadline)
        with self._phase4_guard(deadline):
            state = self._phase4.load(_phase4_identity(job).identity_sha256)
        return self._view(job, state, deadline=deadline)

    def list(
        self,
        *,
        deadline: object | None = None,
    ) -> tuple[Phase78WorkView, ...]:
        jobs = self._scan_jobs(deadline=deadline)
        with self._phase4_guard(deadline):
            return tuple(
                self._view(
                    job,
                    self._phase4.load(_phase4_identity(job).identity_sha256),
                    deadline=deadline,
                )
                for job in jobs
            )

    def claim(
        self,
        caller_idempotency_key: str,
        *,
        request_idempotency_key: str,
        claim_owner_id: str,
        claim_owner_epoch: int,
        expected_claim_generation: int,
        occurred_at: int,
        lease_seconds: int,
        deadline: object | None = None,
    ) -> Phase78WorkCommitResult:
        _check_deadline(deadline)
        job = self._job_for_key(caller_idempotency_key, deadline=deadline)
        _check_deadline(deadline)
        with self._phase4_guard(deadline):
            if self._load_cancellation_for_job(job, deadline=deadline) is not None:
                raise Phase78WorkContractError(
                    "work has a durable cancellation intent"
                )
            result = self._phase4.claim_operation(
                _phase4_identity(job).identity_sha256,
                request_idempotency_key=_transition_key(
                    job, request_idempotency_key, "claim"
                ),
                claim_owner_id=claim_owner_id,
                claim_owner_epoch=claim_owner_epoch,
                expected_claim_generation=expected_claim_generation,
                occurred_at=occurred_at,
                lease_seconds=lease_seconds,
            )
        return self._commit(job, result, deadline=deadline)

    def reclaim(
        self,
        caller_idempotency_key: str,
        *,
        request_idempotency_key: str,
        claim_owner_id: str,
        claim_owner_epoch: int,
        expected_claim_generation: int,
        occurred_at: int,
        lease_seconds: int,
        deadline: object | None = None,
    ) -> Phase78WorkCommitResult:
        current = self.load(caller_idempotency_key, deadline=deadline)
        if (
            current.status is not OperationStatus.CLAIMED
            or current.state.lease_expires_at is None
            or type(occurred_at) is not int
            or occurred_at < current.state.lease_expires_at
        ):
            raise Phase78WorkContractError("work lease is not reclaimable")
        return self.claim(
            caller_idempotency_key,
            request_idempotency_key=request_idempotency_key,
            claim_owner_id=claim_owner_id,
            claim_owner_epoch=claim_owner_epoch,
            expected_claim_generation=expected_claim_generation,
            occurred_at=occurred_at,
            lease_seconds=lease_seconds,
            deadline=deadline,
        )

    def _transition(
        self,
        caller_idempotency_key: str,
        event: OperationEvent,
        *,
        request_idempotency_key: str,
        transition_stage: str,
        expected_claim_generation: int,
        claim_owner_id: str,
        claim_owner_epoch: int,
        local_worker_nonce: str | None,
        reason_code: str,
        occurred_at: int,
        deadline: object | None,
        allow_cancellation_intent: bool = False,
    ) -> Phase78WorkCommitResult:
        _check_deadline(deadline)
        job = self._job_for_key(caller_idempotency_key, deadline=deadline)
        _check_deadline(deadline)
        with self._phase4_guard(deadline):
            if (
                not allow_cancellation_intent
                and self._load_cancellation_for_job(job, deadline=deadline)
                is not None
            ):
                raise Phase78WorkContractError(
                    "work has a durable cancellation intent"
                )
            result = self._phase4.transition(
                _phase4_identity(job).identity_sha256,
                event,
                request_idempotency_key=_transition_key(
                    job, request_idempotency_key, transition_stage
                ),
                expected_claim_generation=expected_claim_generation,
                claim_owner_id=claim_owner_id,
                claim_owner_epoch=claim_owner_epoch,
                dispatch_nonce=local_worker_nonce,
                reason_code=reason_code,
                occurred_at=occurred_at,
            )
        return self._commit(job, result, deadline=deadline)

    def checkpoint_local_worker(
        self,
        caller_idempotency_key: str,
        *,
        request_idempotency_key: str,
        expected_claim_generation: int,
        claim_owner_id: str,
        claim_owner_epoch: int,
        local_worker_nonce: str,
        reason_code: str,
        occurred_at: int,
        deadline: object | None = None,
    ) -> Phase78WorkCommitResult:
        return self._transition(
            caller_idempotency_key,
            OperationEvent.CHECKPOINT_DISPATCH,
            request_idempotency_key=request_idempotency_key,
            transition_stage="checkpoint-local-worker",
            expected_claim_generation=expected_claim_generation,
            claim_owner_id=claim_owner_id,
            claim_owner_epoch=claim_owner_epoch,
            local_worker_nonce=local_worker_nonce,
            reason_code=reason_code,
            occurred_at=occurred_at,
            deadline=deadline,
        )

    def complete(
        self,
        caller_idempotency_key: str,
        *,
        request_idempotency_key: str,
        expected_claim_generation: int,
        claim_owner_id: str,
        claim_owner_epoch: int,
        local_worker_nonce: str,
        reason_code: str,
        occurred_at: int,
        deadline: object | None = None,
    ) -> Phase78WorkCommitResult:
        return self._transition(
            caller_idempotency_key,
            OperationEvent.CONFIRM_SUCCEEDED,
            request_idempotency_key=request_idempotency_key,
            transition_stage="complete",
            expected_claim_generation=expected_claim_generation,
            claim_owner_id=claim_owner_id,
            claim_owner_epoch=claim_owner_epoch,
            local_worker_nonce=local_worker_nonce,
            reason_code=reason_code,
            occurred_at=occurred_at,
            deadline=deadline,
        )

    def fail(
        self,
        caller_idempotency_key: str,
        *,
        request_idempotency_key: str,
        expected_claim_generation: int,
        claim_owner_id: str,
        claim_owner_epoch: int,
        local_worker_nonce: str | None,
        reason_code: str,
        occurred_at: int,
        deadline: object | None = None,
    ) -> Phase78WorkCommitResult:
        return self._transition(
            caller_idempotency_key,
            OperationEvent.CONFIRM_FAILED,
            request_idempotency_key=request_idempotency_key,
            transition_stage="fail",
            expected_claim_generation=expected_claim_generation,
            claim_owner_id=claim_owner_id,
            claim_owner_epoch=claim_owner_epoch,
            local_worker_nonce=local_worker_nonce,
            reason_code=reason_code,
            occurred_at=occurred_at,
            deadline=deadline,
        )

    def cancel(
        self,
        caller_idempotency_key: str,
        *,
        request_idempotency_key: str,
        cancellation_reason: str,
        expected_claim_generation: int,
        claim_owner_id: str,
        claim_owner_epoch: int,
        local_worker_nonce: str,
        occurred_at: int,
        deadline: object | None = None,
    ) -> Phase78WorkCommitResult:
        """Durably bind one exact classified cancellation, then finish it.

        Publishing the immutable receipt is the cancellation linearization
        point.  A crash after that publish but before any Phase-4 transition is
        safe: readers and workers observe the intent, and an exact retry
        resumes the same transition sequence.  A different reason, timestamp,
        lease token or request key is an idempotency conflict.
        """

        _check_deadline(deadline)
        job = self._job_for_key(caller_idempotency_key, deadline=deadline)
        candidate = build_phase78_work_cancellation_receipt(
            caller_idempotency_key=job.caller_idempotency_key,
            job_sha256=job.job_sha256,
            operation_identity_sha256=_phase4_identity(job).identity_sha256,
            claim_generation=expected_claim_generation,
            claim_owner_id=claim_owner_id,
            claim_owner_epoch=claim_owner_epoch,
            local_worker_nonce=local_worker_nonce,
            request_idempotency_key=request_idempotency_key,
            cancellation_reason=cancellation_reason,
            occurred_at=occurred_at,
        )
        with self._phase4_guard(deadline):
            state = self._phase4.load(_phase4_identity(job).identity_sha256)
            existing = self._load_cancellation_for_job(
                job, deadline=deadline
            )
            if existing is None:
                if state.operation.status not in {
                    OperationStatus.DISPATCH_CHECKPOINTED,
                    OperationStatus.ACTIVE,
                    OperationStatus.CANCEL_REQUESTED,
                    OperationStatus.CANCEL_SIGNALLED,
                    # A pre-receipt development snapshot may already have
                    # reached CANCELLED.  Publishing the exact receipt here is
                    # a safe one-way compatibility closure.
                    OperationStatus.CANCELLED,
                }:
                    raise Phase78WorkContractError(
                        f"work cannot be cancelled from {state.operation.status.value}"
                    )
                if (
                    state.operation.claim_generation
                    != candidate.claim_generation
                    or state.claim_owner_id != candidate.claim_owner_id
                    or state.claim_owner_epoch != candidate.claim_owner_epoch
                    or state.operation.dispatch_nonce
                    != candidate.local_worker_nonce
                ):
                    raise Phase78WorkContractError(
                        "cancellation lease token is stale"
                    )
            published, receipt_replayed = self._publish_cancellation(
                candidate, deadline=deadline
            )
            if published.canonical_bytes != candidate.canonical_bytes:
                raise Phase78WorkIdempotencyConflict(
                    "work cancellation request is bound to different bytes"
                )

        last: Phase78WorkCommitResult | None = None
        transitions = {
            OperationStatus.DISPATCH_CHECKPOINTED: (
                OperationEvent.REQUEST_CANCEL,
                "cancel-request",
                "LOCAL_SHADOW_CANCEL_REQUESTED",
            ),
            OperationStatus.ACTIVE: (
                OperationEvent.REQUEST_CANCEL,
                "cancel-request",
                "LOCAL_SHADOW_CANCEL_REQUESTED",
            ),
            OperationStatus.CANCEL_REQUESTED: (
                OperationEvent.RECORD_CANCEL_SIGNAL,
                "cancel-signal",
                "LOCAL_SHADOW_CANCEL_SIGNAL_RECORDED",
            ),
            OperationStatus.CANCEL_SIGNALLED: (
                OperationEvent.CONFIRM_CANCELLED,
                "cancel-complete",
                "LOCAL_SHADOW_CANCELLED",
            ),
        }
        while True:
            _check_deadline(deadline)
            current = self.load(caller_idempotency_key, deadline=deadline)
            if current.status is OperationStatus.CANCELLED:
                if last is not None:
                    return replace(
                        last,
                        replayed=receipt_replayed or last.replayed,
                    )
                return Phase78WorkCommitResult(current, None, True)
            transition = transitions.get(current.status)
            if transition is None:
                raise Phase78WorkContractError(
                    f"work cannot be cancelled from {current.status.value}"
                )
            event, stage, reason = transition
            last = self._transition(
                caller_idempotency_key,
                event,
                request_idempotency_key=request_idempotency_key,
                transition_stage=stage,
                expected_claim_generation=expected_claim_generation,
                claim_owner_id=claim_owner_id,
                claim_owner_epoch=claim_owner_epoch,
                local_worker_nonce=local_worker_nonce,
                reason_code=reason,
                occurred_at=occurred_at,
                deadline=deadline,
                allow_cancellation_intent=True,
            )

    def reconcile(
        self,
        caller_idempotency_key: str,
        *,
        request_idempotency_key: str,
        expected_claim_generation: int,
        claim_owner_id: str,
        claim_owner_epoch: int,
        local_worker_nonce: str,
        occurred_at: int,
        outcome: Phase78ReconcileOutcome | None,
        deadline: object | None = None,
    ) -> Phase78WorkCommitResult:
        if outcome is not None and type(outcome) is not Phase78ReconcileOutcome:
            raise Phase78WorkContractError(
                "outcome must be Phase78ReconcileOutcome or None"
            )
        last: Phase78WorkCommitResult | None = None
        while True:
            _check_deadline(deadline)
            current = self.load(caller_idempotency_key, deadline=deadline)
            if outcome is not None and current.status.value == outcome.value:
                if last is not None:
                    return last
                return Phase78WorkCommitResult(current, None, True)
            if current.status is OperationStatus.DISPATCH_CHECKPOINTED:
                event = OperationEvent.MARK_DISPATCH_UNCERTAIN
                stage = "mark-local-worker-uncertain"
                reason = "LOCAL_SHADOW_WORKER_RESULT_UNCERTAIN"
            elif current.status in {
                OperationStatus.DISPATCH_UNCERTAIN,
                OperationStatus.ACTIVE,
                OperationStatus.CANCEL_REQUESTED,
                OperationStatus.CANCEL_SIGNALLED,
            }:
                event = OperationEvent.REQUIRE_RECONCILIATION
                stage = "require-local-reconciliation"
                reason = "LOCAL_SHADOW_RECONCILIATION_REQUIRED"
            elif current.status is OperationStatus.RECONCILIATION_REQUIRED:
                if outcome is None:
                    if last is not None:
                        return last
                    return Phase78WorkCommitResult(current, None, True)
                event = {
                    Phase78ReconcileOutcome.ACTIVE: OperationEvent.RECONCILE_ACTIVE,
                    Phase78ReconcileOutcome.SUCCEEDED: OperationEvent.RECONCILE_SUCCEEDED,
                    Phase78ReconcileOutcome.FAILED: OperationEvent.RECONCILE_FAILED,
                    Phase78ReconcileOutcome.CANCELLED: OperationEvent.RECONCILE_CANCELLED,
                }[outcome]
                stage = f"reconcile-{outcome.value}"
                reason = f"LOCAL_SHADOW_RECONCILED_{outcome.value.upper()}"
            else:
                raise Phase78WorkContractError(
                    f"work cannot be reconciled from {current.status.value}"
                )
            last = self._transition(
                caller_idempotency_key,
                event,
                request_idempotency_key=request_idempotency_key,
                transition_stage=stage,
                expected_claim_generation=expected_claim_generation,
                claim_owner_id=claim_owner_id,
                claim_owner_epoch=claim_owner_epoch,
                local_worker_nonce=local_worker_nonce,
                reason_code=reason,
                occurred_at=occurred_at,
                deadline=deadline,
            )
            if last.view.status is OperationStatus.RECONCILIATION_REQUIRED and outcome is None:
                return last
            if outcome is not None and last.view.status.value == outcome.value:
                return last


def run_phase78_work_ledger_shadow(
    *,
    enabled: bool = PHASE78_WORK_LEDGER_DEFAULT_ENABLED,
    database: str | Path | None = None,
    request_spool: str | Path | None = None,
) -> Phase78WorkLedgerRun:
    """Verify the work ledger only after explicit enablement."""

    if type(enabled) is not bool:
        raise Phase78WorkContractError("enabled must be a boolean")
    if not enabled:
        prototype = Phase78WorkLedgerRun(
            PHASE78_WORK_RUN_SCHEMA,
            False,
            False,
            0,
            False,
            False,
            False,
            False,
            "0" * 64,
        )
        body = prototype.as_dict()
        body.pop("run_sha256")
        return replace(prototype, run_sha256=canonical_sha256(body))
    if database is None or request_spool is None:
        raise Phase78WorkContractError(
            "enabled work ledger requires database and request spool"
        )
    ledger = Phase78WorkLedger(database, request_spool)
    ledger.initialize()
    prototype = Phase78WorkLedgerRun(
        PHASE78_WORK_RUN_SCHEMA,
        True,
        True,
        len(ledger.list()),
        False,
        False,
        False,
        False,
        "0" * 64,
    )
    body = prototype.as_dict()
    body.pop("run_sha256")
    return replace(prototype, run_sha256=canonical_sha256(body))
