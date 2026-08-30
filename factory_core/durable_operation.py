from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum


OPERATION_IDENTITY_SCHEMA = "durable-worker-operation-identity-v1"
OPERATION_RECEIPT_SCHEMA = "durable-operation-transition-receipt-v1"
_CANONICAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class DurableOperationError(ValueError):
    """Base error for invalid durable-operation contracts."""


class InvalidOperationIdentity(DurableOperationError):
    """Raised when operation identity is incomplete or non-canonical."""


class InvalidOperationTransition(DurableOperationError):
    """Raised when a state transition is stale, ambiguous or unsupported."""


class OperationType(str, Enum):
    WORKER_LAUNCH = "worker-launch"


class OperationStatus(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    DISPATCH_CHECKPOINTED = "dispatch-checkpointed"
    ACTIVE = "active"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DISPATCH_UNCERTAIN = "dispatch-uncertain"
    CANCEL_REQUESTED = "cancel-requested"
    CANCEL_SIGNALLED = "cancel-signalled"
    CANCELLED = "cancelled"
    RECONCILIATION_REQUIRED = "reconciliation-required"


class OperationEvent(str, Enum):
    CLAIM = "claim"
    RECLAIM_EXPIRED = "reclaim-expired"
    CHECKPOINT_DISPATCH = "checkpoint-dispatch"
    CONFIRM_ACTIVE = "confirm-active"
    CONFIRM_SUCCEEDED = "confirm-succeeded"
    CONFIRM_FAILED = "confirm-failed"
    MARK_DISPATCH_UNCERTAIN = "mark-dispatch-uncertain"
    REQUEST_CANCEL = "request-cancel"
    RECORD_CANCEL_SIGNAL = "record-cancel-signal"
    CONFIRM_CANCELLED = "confirm-cancelled"
    REQUIRE_RECONCILIATION = "require-reconciliation"
    RECONCILE_ACTIVE = "reconcile-active"
    RECONCILE_SUCCEEDED = "reconcile-succeeded"
    RECONCILE_FAILED = "reconcile-failed"
    RECONCILE_CANCELLED = "reconcile-cancelled"


def _canonical_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _CANONICAL_ID.fullmatch(value) is None:
        raise InvalidOperationIdentity(f"{field} must be a canonical operation identifier")
    return value


def _canonical_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise InvalidOperationIdentity(f"{field} must be a lowercase SHA-256")
    return value


def _canonical_json_sha256(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def derive_worker_launch_idempotency_key(
    *, invocation_id: str, attempt_id: str, payload_sha256: str
) -> str:
    """Derive the stable logical-operation key, independent of outbox row ID."""

    payload = {
        "schema": "worker-launch-idempotency-v1",
        "operation_type": OperationType.WORKER_LAUNCH.value,
        "invocation_id": _canonical_id(invocation_id, "invocation_id"),
        "attempt_id": _canonical_id(attempt_id, "attempt_id"),
        "payload_sha256": _canonical_sha256(payload_sha256, "payload_sha256"),
    }
    return "worker-launch-v1:" + _canonical_json_sha256(payload)


@dataclass(frozen=True)
class DurableOperationIdentity:
    schema_version: str
    operation_type: OperationType
    outbox_command_id: str
    invocation_id: str
    attempt_id: str
    process_scope_id: str
    payload_sha256: str
    idempotency_key: str

    def __post_init__(self) -> None:
        if self.schema_version != OPERATION_IDENTITY_SCHEMA:
            raise InvalidOperationIdentity("operation identity schema is unsupported")
        if self.operation_type is not OperationType.WORKER_LAUNCH:
            raise InvalidOperationIdentity("operation type is unsupported")
        _canonical_id(self.outbox_command_id, "outbox_command_id")
        _canonical_id(self.invocation_id, "invocation_id")
        _canonical_id(self.attempt_id, "attempt_id")
        _canonical_id(self.process_scope_id, "process_scope_id")
        _canonical_sha256(self.payload_sha256, "payload_sha256")
        expected = derive_worker_launch_idempotency_key(
            invocation_id=self.invocation_id,
            attempt_id=self.attempt_id,
            payload_sha256=self.payload_sha256,
        )
        if self.idempotency_key != expected:
            raise InvalidOperationIdentity(
                "idempotency_key does not match invocation, attempt and payload"
            )

    def as_dict(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "operation_type": self.operation_type.value,
            "outbox_command_id": self.outbox_command_id,
            "invocation_id": self.invocation_id,
            "attempt_id": self.attempt_id,
            "process_scope_id": self.process_scope_id,
            "payload_sha256": self.payload_sha256,
            "idempotency_key": self.idempotency_key,
        }

    @property
    def identity_sha256(self) -> str:
        return _canonical_json_sha256(self.as_dict())


def build_worker_launch_identity(
    *,
    outbox_command_id: str,
    invocation_id: str,
    attempt_id: str,
    process_scope_id: str,
    payload_sha256: str,
) -> DurableOperationIdentity:
    return DurableOperationIdentity(
        schema_version=OPERATION_IDENTITY_SCHEMA,
        operation_type=OperationType.WORKER_LAUNCH,
        outbox_command_id=outbox_command_id,
        invocation_id=invocation_id,
        attempt_id=attempt_id,
        process_scope_id=process_scope_id,
        payload_sha256=payload_sha256,
        idempotency_key=derive_worker_launch_idempotency_key(
            invocation_id=invocation_id,
            attempt_id=attempt_id,
            payload_sha256=payload_sha256,
        ),
    )


@dataclass(frozen=True)
class DurableOperation:
    identity: DurableOperationIdentity
    status: OperationStatus = OperationStatus.PENDING
    claim_generation: int = 0
    dispatch_nonce: str | None = None
    transition_index: int = 0

    def __post_init__(self) -> None:
        if type(self.identity) is not DurableOperationIdentity:
            raise InvalidOperationIdentity("identity must be DurableOperationIdentity")
        if not isinstance(self.status, OperationStatus):
            raise InvalidOperationTransition("status is unsupported")
        if not isinstance(self.claim_generation, int) or isinstance(
            self.claim_generation, bool
        ):
            raise InvalidOperationTransition("claim_generation must be an integer")
        if not isinstance(self.transition_index, int) or isinstance(
            self.transition_index, bool
        ):
            raise InvalidOperationTransition("transition_index must be an integer")
        if self.claim_generation < 0 or self.transition_index < 0:
            raise InvalidOperationTransition("operation counters cannot be negative")
        if self.status is OperationStatus.PENDING:
            if self.claim_generation != 0 or self.dispatch_nonce is not None:
                raise InvalidOperationTransition("pending operation has invalid claim state")
            return
        if self.claim_generation <= 0:
            raise InvalidOperationTransition("non-pending operation requires a claim")
        if self.status is OperationStatus.CLAIMED:
            if self.dispatch_nonce is not None:
                raise InvalidOperationTransition("claimed operation cannot have dispatch nonce")
            return
        if self.status is OperationStatus.FAILED and self.dispatch_nonce is None:
            return
        _canonical_id(self.dispatch_nonce, "dispatch_nonce")


@dataclass(frozen=True)
class OperationTransitionReceipt:
    schema_version: str
    operation_identity_sha256: str
    idempotency_key: str
    transition_index: int
    event: OperationEvent
    previous_status: OperationStatus
    current_status: OperationStatus
    claim_generation: int
    dispatch_nonce: str | None
    reason_code: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "operation_identity_sha256": self.operation_identity_sha256,
            "idempotency_key": self.idempotency_key,
            "transition_index": self.transition_index,
            "event": self.event.value,
            "previous_status": self.previous_status.value,
            "current_status": self.current_status.value,
            "claim_generation": self.claim_generation,
            "dispatch_nonce": self.dispatch_nonce,
            "reason_code": self.reason_code,
        }

    @property
    def receipt_sha256(self) -> str:
        return _canonical_json_sha256(self.as_dict())


_TRANSITIONS: dict[tuple[OperationStatus, OperationEvent], OperationStatus] = {
    (OperationStatus.PENDING, OperationEvent.CLAIM): OperationStatus.CLAIMED,
    (OperationStatus.CLAIMED, OperationEvent.RECLAIM_EXPIRED): OperationStatus.CLAIMED,
    (
        OperationStatus.CLAIMED,
        OperationEvent.CHECKPOINT_DISPATCH,
    ): OperationStatus.DISPATCH_CHECKPOINTED,
    (OperationStatus.CLAIMED, OperationEvent.CONFIRM_FAILED): OperationStatus.FAILED,
    (
        OperationStatus.DISPATCH_CHECKPOINTED,
        OperationEvent.CONFIRM_ACTIVE,
    ): OperationStatus.ACTIVE,
    (
        OperationStatus.DISPATCH_CHECKPOINTED,
        OperationEvent.CONFIRM_SUCCEEDED,
    ): OperationStatus.SUCCEEDED,
    (
        OperationStatus.DISPATCH_CHECKPOINTED,
        OperationEvent.CONFIRM_FAILED,
    ): OperationStatus.FAILED,
    (
        OperationStatus.DISPATCH_CHECKPOINTED,
        OperationEvent.MARK_DISPATCH_UNCERTAIN,
    ): OperationStatus.DISPATCH_UNCERTAIN,
    (
        OperationStatus.DISPATCH_CHECKPOINTED,
        OperationEvent.REQUEST_CANCEL,
    ): OperationStatus.CANCEL_REQUESTED,
    (OperationStatus.ACTIVE, OperationEvent.CONFIRM_SUCCEEDED): OperationStatus.SUCCEEDED,
    (OperationStatus.ACTIVE, OperationEvent.CONFIRM_FAILED): OperationStatus.FAILED,
    (OperationStatus.ACTIVE, OperationEvent.REQUEST_CANCEL): OperationStatus.CANCEL_REQUESTED,
    (
        OperationStatus.ACTIVE,
        OperationEvent.REQUIRE_RECONCILIATION,
    ): OperationStatus.RECONCILIATION_REQUIRED,
    (
        OperationStatus.DISPATCH_UNCERTAIN,
        OperationEvent.REQUIRE_RECONCILIATION,
    ): OperationStatus.RECONCILIATION_REQUIRED,
    (
        OperationStatus.CANCEL_REQUESTED,
        OperationEvent.RECORD_CANCEL_SIGNAL,
    ): OperationStatus.CANCEL_SIGNALLED,
    (
        OperationStatus.CANCEL_REQUESTED,
        OperationEvent.CONFIRM_CANCELLED,
    ): OperationStatus.CANCELLED,
    (
        OperationStatus.CANCEL_REQUESTED,
        OperationEvent.REQUIRE_RECONCILIATION,
    ): OperationStatus.RECONCILIATION_REQUIRED,
    (
        OperationStatus.CANCEL_SIGNALLED,
        OperationEvent.CONFIRM_CANCELLED,
    ): OperationStatus.CANCELLED,
    (
        OperationStatus.CANCEL_SIGNALLED,
        OperationEvent.REQUIRE_RECONCILIATION,
    ): OperationStatus.RECONCILIATION_REQUIRED,
    (
        OperationStatus.RECONCILIATION_REQUIRED,
        OperationEvent.RECONCILE_ACTIVE,
    ): OperationStatus.ACTIVE,
    (
        OperationStatus.RECONCILIATION_REQUIRED,
        OperationEvent.RECONCILE_SUCCEEDED,
    ): OperationStatus.SUCCEEDED,
    (
        OperationStatus.RECONCILIATION_REQUIRED,
        OperationEvent.RECONCILE_FAILED,
    ): OperationStatus.FAILED,
    (
        OperationStatus.RECONCILIATION_REQUIRED,
        OperationEvent.RECONCILE_CANCELLED,
    ): OperationStatus.CANCELLED,
}


def transition_operation(
    operation: DurableOperation,
    event: OperationEvent | str,
    *,
    expected_claim_generation: int,
    dispatch_nonce: str | None = None,
    reason_code: str,
) -> tuple[DurableOperation, OperationTransitionReceipt]:
    """Apply one fail-closed transition and return deterministic new state/receipt."""

    if type(operation) is not DurableOperation:
        raise InvalidOperationTransition("operation must be DurableOperation")
    try:
        normalized_event = OperationEvent(event)
    except (TypeError, ValueError) as exc:
        raise InvalidOperationTransition(f"unsupported operation event: {event!r}") from exc
    if not isinstance(expected_claim_generation, int) or isinstance(
        expected_claim_generation, bool
    ):
        raise InvalidOperationTransition("expected_claim_generation must be an integer")
    if expected_claim_generation != operation.claim_generation:
        raise InvalidOperationTransition("claim generation is stale")
    _canonical_id(reason_code, "reason_code")

    target = _TRANSITIONS.get((operation.status, normalized_event))
    if target is None:
        raise InvalidOperationTransition(
            f"event {normalized_event.value} is not allowed from {operation.status.value}"
        )

    next_claim_generation = operation.claim_generation
    next_dispatch_nonce = operation.dispatch_nonce
    if normalized_event in {OperationEvent.CLAIM, OperationEvent.RECLAIM_EXPIRED}:
        if dispatch_nonce is not None:
            raise InvalidOperationTransition("claim transition cannot carry dispatch nonce")
        next_claim_generation += 1
    elif normalized_event is OperationEvent.CHECKPOINT_DISPATCH:
        next_dispatch_nonce = _canonical_id(dispatch_nonce, "dispatch_nonce")
    elif operation.dispatch_nonce is None:
        if dispatch_nonce is not None:
            raise InvalidOperationTransition("pre-dispatch transition cannot carry dispatch nonce")
    elif dispatch_nonce != operation.dispatch_nonce:
        raise InvalidOperationTransition("dispatch nonce is stale or missing")

    updated = DurableOperation(
        identity=operation.identity,
        status=target,
        claim_generation=next_claim_generation,
        dispatch_nonce=next_dispatch_nonce,
        transition_index=operation.transition_index + 1,
    )
    receipt = OperationTransitionReceipt(
        schema_version=OPERATION_RECEIPT_SCHEMA,
        operation_identity_sha256=operation.identity.identity_sha256,
        idempotency_key=operation.identity.idempotency_key,
        transition_index=updated.transition_index,
        event=normalized_event,
        previous_status=operation.status,
        current_status=updated.status,
        claim_generation=updated.claim_generation,
        dispatch_nonce=updated.dispatch_nonce,
        reason_code=reason_code,
    )
    return updated, receipt
