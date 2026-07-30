from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 4


class FactoryCoreError(RuntimeError):
    pass


class StateNotInitialized(FactoryCoreError):
    pass


class RevisionConflict(FactoryCoreError):
    pass


class InvalidTransition(FactoryCoreError):
    pass


class RunnerBusy(FactoryCoreError):
    pass


class RunnerLeaseLost(FactoryCoreError):
    """Raised when a worker no longer owns the project's runner lease."""


class MigrationConflict(FactoryCoreError):
    pass


class WorkflowStatus(str, Enum):
    READY = "ready"
    RUNNING = "running"
    RETRYING = "retrying"
    AWAITING_SELECTION = "awaiting_selection"
    AWAITING_CONSULTATION = "awaiting_consultation"
    PAUSED = "paused"
    KILLED = "killed"
    FAILED = "failed"
    COMPLETED = "completed"
    ARCHIVING = "archiving"
    INTERRUPTED = "interrupted"


TERMINAL_STATUSES = {WorkflowStatus.KILLED, WorkflowStatus.COMPLETED}


@dataclass(frozen=True)
class WorkflowState:
    schema_version: int
    project_id: str
    project_type: str
    control_mode: str
    runtime_generation: str
    status: WorkflowStatus
    last_completed_step: int
    active_step: int | None
    attempt: int
    revision: int
    pending_action: dict[str, Any] | None
    runner_pid: int | None
    runner_lease_id: str | None
    heartbeat_at: int | None
    storage_scope: str
    created_at: int
    updated_at: int
    last_event_at: int


@dataclass(frozen=True)
class WorkflowEvent:
    revision: int
    type: str
    created_at: int
    step: int | None
    attempt: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class PendingAction:
    type: str
    gate: str | None = None
    deadline_epoch: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "gate": self.gate,
            "deadline_epoch": self.deadline_epoch,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class PrepareResult:
    ready: bool = True
    pending_action: PendingAction | None = None
    reason: str = ""
    evidence: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def prepared(cls, *evidence: str, **metadata: Any) -> "PrepareResult":
        return cls(evidence=tuple(evidence), metadata=metadata)

    @classmethod
    def awaiting(
        cls, action: PendingAction, *evidence: str, reason: str = ""
    ) -> "PrepareResult":
        return cls(
            ready=False,
            pending_action=action,
            reason=reason,
            evidence=tuple(evidence),
        )


@dataclass(frozen=True)
class StepError:
    error_class: str
    reason: str = ""
    returncode: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionResult:
    returncode: int
    error_class: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def succeeded(cls, **metadata: Any) -> "ExecutionResult":
        return cls(returncode=0, metadata=metadata)

    @classmethod
    def failed(cls, error_class: str, returncode: int = 1, **metadata: Any) -> "ExecutionResult":
        return cls(returncode=returncode, error_class=error_class, metadata=metadata)


@dataclass(frozen=True)
class ValidationResult:
    is_valid: bool
    reason: str = ""
    pending_action: PendingAction | None = None
    evidence: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def valid(
        cls, *evidence: str, metadata: dict[str, Any] | None = None
    ) -> "ValidationResult":
        return cls(is_valid=True, evidence=tuple(evidence), metadata=metadata or {})

    @classmethod
    def invalid(
        cls,
        reason: str,
        *evidence: str,
        metadata: dict[str, Any] | None = None,
    ) -> "ValidationResult":
        return cls(
            is_valid=False,
            reason=reason,
            evidence=tuple(evidence),
            metadata=metadata or {},
        )

    @classmethod
    def awaiting(cls, action: PendingAction, *evidence: str) -> "ValidationResult":
        return cls(is_valid=False, pending_action=action, evidence=tuple(evidence))


class RecoveryDisposition(str, Enum):
    COMPLETE = "complete"
    RETRY = "retry"
    AWAIT = "await"
    REOPEN = "reopen"
    FAIL = "fail"


@dataclass(frozen=True)
class RecoveryDecision:
    disposition: RecoveryDisposition
    reason: str = ""
    evidence: tuple[str, ...] = ()
    pending_action: PendingAction | None = None
    completed_through_step: int | None = None
    resume_after_step: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_validation(
        cls, validation: ValidationResult, *, active_step: int
    ) -> "RecoveryDecision":
        resume_after = validation.metadata.get("resume_after_step")
        if resume_after is not None:
            return cls(
                disposition=RecoveryDisposition.REOPEN,
                reason=validation.reason,
                evidence=validation.evidence,
                resume_after_step=int(resume_after),
                metadata=validation.metadata,
            )
        if validation.pending_action is not None:
            return cls(
                disposition=RecoveryDisposition.AWAIT,
                reason=validation.reason,
                evidence=validation.evidence,
                pending_action=validation.pending_action,
                metadata=validation.metadata,
            )
        if validation.is_valid:
            return cls(
                disposition=RecoveryDisposition.COMPLETE,
                reason=validation.reason,
                evidence=validation.evidence,
                completed_through_step=int(
                    validation.metadata.get("completed_through_step", active_step)
                ),
                metadata=validation.metadata,
            )
        return cls(
            disposition=RecoveryDisposition.RETRY,
            reason=validation.reason,
            evidence=validation.evidence,
            metadata=validation.metadata,
        )


@dataclass(frozen=True)
class StepContext:
    project_dir: Path
    project_id: str
    step_id: int
    attempt: int
    timeout_seconds: int
    revision: int
