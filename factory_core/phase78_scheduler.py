"""Explicit, local-only scheduler adapter for the Phase 7+8 shadow pipeline.

There is intentionally no background thread, process launcher, provider or
outbox in this module.  A caller explicitly submits a canonical request and an
explicit local worker claims it through the durable Phase-4-backed ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Mapping

from .durable_operation import OperationStatus
from .phase78_config import Phase78Settings
from .phase78_deadline import Phase78CancellationReason
from .phase78_work_ledger import (
    Phase78WorkCommitResult,
    Phase78WorkKind,
    Phase78WorkLedger,
    Phase78WorkView,
)


PHASE78_LOCAL_WORKER_ID = "phase78-local-shadow-worker"
PHASE78_LOCAL_WORKER_EPOCH = 1


class Phase78SchedulerError(RuntimeError):
    code = "PHASE78_SCHEDULER_ERROR"


class Phase78SchedulerStateError(Phase78SchedulerError):
    code = "PHASE78_SCHEDULER_STATE_INVALID"


class Phase78SchedulerTerminalConflict(Phase78SchedulerStateError):
    code = "PHASE78_WORK_TERMINAL"


def _require_enabled(settings: Phase78Settings) -> None:
    if not isinstance(settings, Phase78Settings) or settings.enabled is not True:
        raise Phase78SchedulerError("Phase 7+8 shadow scheduler is disabled")


def _ledger(settings: Phase78Settings) -> Phase78WorkLedger:
    _require_enabled(settings)
    return Phase78WorkLedger(
        settings.required_path("work_database"),
        settings.required_path("work_spool"),
    )


def local_worker_nonce(idempotency_key: str) -> str:
    return hashlib.sha256(
        ("phase78-local-shadow-worker:" + idempotency_key).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class Phase78Claim:
    view: Phase78WorkView
    replayed: bool


@dataclass(frozen=True, slots=True)
class Phase78Cancellation:
    view: Phase78WorkView
    cancellation_reason: str
    replayed: bool


class Phase78ShadowScheduler:
    """Synchronous facade over the durable request/claim/lease ledger."""

    def __init__(self, settings: Phase78Settings, *, deadline: object) -> None:
        _require_enabled(settings)
        self._settings = settings
        self._ledger = _ledger(settings)
        self._ledger.initialize(deadline=deadline)

    @property
    def ledger(self) -> Phase78WorkLedger:
        return self._ledger

    def submit(
        self,
        *,
        idempotency_key: str,
        workflow_id: str,
        payload: Mapping[str, object],
        occurred_at: int,
        deadline: object,
    ) -> Phase78WorkCommitResult:
        return self._ledger.submit(
            caller_idempotency_key=idempotency_key,
            workflow_id=workflow_id,
            work_kind=Phase78WorkKind.PHASE78_PIPELINE,
            payload=payload,
            occurred_at=occurred_at,
            deadline=deadline,
        )

    def load(self, idempotency_key: str, *, deadline: object) -> Phase78WorkView:
        return self._ledger.load(idempotency_key, deadline=deadline)

    def claim_and_checkpoint(
        self,
        *,
        idempotency_key: str,
        claimed_at: int,
        checkpointed_at: int,
        deadline: object,
    ) -> Phase78Claim:
        """Claim once, or resume the same deterministic local worker lease."""

        view = self._ledger.load(idempotency_key, deadline=deadline)
        if view.status is OperationStatus.PENDING:
            committed = self._ledger.claim(
                idempotency_key,
                request_idempotency_key=f"{idempotency_key}:claim",
                claim_owner_id=PHASE78_LOCAL_WORKER_ID,
                claim_owner_epoch=PHASE78_LOCAL_WORKER_EPOCH,
                expected_claim_generation=view.state.operation.claim_generation,
                occurred_at=claimed_at,
                lease_seconds=self._settings.lease_seconds,
                deadline=deadline,
            )
            view = committed.view
            replayed = committed.replayed
        elif view.status is OperationStatus.CLAIMED:
            if (
                view.state.claim_owner_id != PHASE78_LOCAL_WORKER_ID
                or view.state.claim_owner_epoch != PHASE78_LOCAL_WORKER_EPOCH
            ):
                raise Phase78SchedulerStateError(
                    "Phase 7+8 work is leased by another local worker generation"
                )
            replayed = True
        elif view.status in {
            OperationStatus.DISPATCH_CHECKPOINTED,
            OperationStatus.ACTIVE,
        }:
            if (
                view.state.claim_owner_id != PHASE78_LOCAL_WORKER_ID
                or view.state.claim_owner_epoch != PHASE78_LOCAL_WORKER_EPOCH
            ):
                raise Phase78SchedulerStateError(
                    "Phase 7+8 work belongs to another local worker generation"
                )
            return Phase78Claim(view, True)
        elif view.status is OperationStatus.SUCCEEDED:
            return Phase78Claim(view, True)
        else:
            raise Phase78SchedulerStateError(
                f"Phase 7+8 work cannot run from {view.status.value}"
            )

        generation = view.state.operation.claim_generation
        checkpoint = self._ledger.checkpoint_local_worker(
            idempotency_key,
            request_idempotency_key=f"{idempotency_key}:checkpoint",
            expected_claim_generation=generation,
            claim_owner_id=PHASE78_LOCAL_WORKER_ID,
            claim_owner_epoch=PHASE78_LOCAL_WORKER_EPOCH,
            local_worker_nonce=local_worker_nonce(idempotency_key),
            reason_code="LOCAL_PHASE78_SHADOW_WORKER_CHECKPOINTED",
            occurred_at=checkpointed_at,
            deadline=deadline,
        )
        return Phase78Claim(checkpoint.view, replayed and checkpoint.replayed)

    def cancel(
        self,
        *,
        idempotency_key: str,
        occurred_at: int,
        reason: Phase78CancellationReason,
        deadline: object,
    ) -> Phase78Cancellation:
        """Cancel pending/running local work without exposing its lease token."""

        if not isinstance(reason, Phase78CancellationReason):
            raise Phase78SchedulerStateError("cancellation reason is unsupported")
        current = self._ledger.load(idempotency_key, deadline=deadline)
        if current.status in {OperationStatus.SUCCEEDED, OperationStatus.FAILED}:
            raise Phase78SchedulerTerminalConflict(
                "terminal Phase 7+8 work cannot be cancelled"
            )
        if current.status in {OperationStatus.PENDING, OperationStatus.CLAIMED}:
            work = current.job.payload.get("work")
            if not isinstance(work, Mapping):
                raise Phase78SchedulerStateError(
                    "durable work timeline is unavailable"
                )
            claimed_at = work.get("claimed_at")
            checkpointed_at = work.get("checkpointed_at")
            if (
                type(claimed_at) is not int
                or type(checkpointed_at) is not int
                or checkpointed_at > occurred_at
            ):
                raise Phase78SchedulerStateError(
                    "cancellation precedes the durable work timeline"
                )
            current = self.claim_and_checkpoint(
                idempotency_key=idempotency_key,
                claimed_at=claimed_at,
                checkpointed_at=checkpointed_at,
                deadline=deadline,
            ).view
        if current.status not in {
            OperationStatus.DISPATCH_CHECKPOINTED,
            OperationStatus.ACTIVE,
            OperationStatus.CANCEL_REQUESTED,
            OperationStatus.CANCEL_SIGNALLED,
            OperationStatus.CANCELLED,
        }:
            raise Phase78SchedulerTerminalConflict(
                f"Phase 7+8 work cannot be cancelled from {current.status.value}"
            )
        committed = self._ledger.cancel(
            idempotency_key,
            request_idempotency_key=f"{idempotency_key}:cancel:{reason.value}",
            cancellation_reason=reason.value,
            expected_claim_generation=current.state.operation.claim_generation,
            claim_owner_id=PHASE78_LOCAL_WORKER_ID,
            claim_owner_epoch=PHASE78_LOCAL_WORKER_EPOCH,
            local_worker_nonce=local_worker_nonce(idempotency_key),
            occurred_at=occurred_at,
            deadline=deadline,
        )
        if committed.view.cancellation is None:
            raise Phase78SchedulerStateError(
                "durable cancellation receipt is unavailable"
            )
        return Phase78Cancellation(
            committed.view,
            committed.view.cancellation.cancellation_reason,
            committed.replayed,
        )


__all__ = [
    "PHASE78_LOCAL_WORKER_EPOCH",
    "PHASE78_LOCAL_WORKER_ID",
    "Phase78Claim",
    "Phase78Cancellation",
    "Phase78SchedulerError",
    "Phase78SchedulerStateError",
    "Phase78SchedulerTerminalConflict",
    "Phase78ShadowScheduler",
    "local_worker_nonce",
]
