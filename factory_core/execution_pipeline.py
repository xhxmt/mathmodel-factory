from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .deadline import deadline_scope, ensure_deadline
from .domain import ExecutionResult, PrepareResult, StepContext, ValidationResult
from .registry import StepDefinition


@dataclass(frozen=True)
class StageExecutionRequest:
    definition: StepDefinition
    context: StepContext


@dataclass(frozen=True)
class StageOutcome:
    execution: ExecutionResult
    validation: ValidationResult | None
    workflow_events: tuple[dict, ...] = ()

    @property
    def killed(self) -> bool:
        return bool(
            self.execution.metadata.get("killed")
            or (self.validation and self.validation.metadata.get("killed"))
        )

    @property
    def resume_after_step(self) -> int | None:
        value = self.execution.metadata.get("resume_after_step")
        return int(value) if value is not None else None

    @property
    def disposition(self) -> str:
        if self.killed:
            return "killed"
        if self.resume_after_step is not None:
            return "reopen"
        if self.validation is None:
            return "executed"
        if self.validation.pending_action is not None:
            return "await"
        if self.execution.returncode == 0 and self.validation.is_valid:
            return "success"
        return "failed"


class StageExecutionPipeline:
    """Runs lifecycle code and returns values; it has no workflow state writer."""

    def __init__(self, now_epoch: Callable[[], int]) -> None:
        self._now_epoch = now_epoch

    def prepare(self, request: StageExecutionRequest) -> PrepareResult:
        with deadline_scope(request.context.deadline_epoch):
            prepared = request.definition.lifecycle.prepare(request.context)
            ensure_deadline(now=self._now_epoch())
        return prepared

    def run(
        self,
        request: StageExecutionRequest,
        *,
        after_execute: Callable[[], None] | None = None,
    ) -> StageOutcome:
        with deadline_scope(request.context.deadline_epoch):
            execution = request.definition.lifecycle.execute(request.context)
            ensure_deadline(now=self._now_epoch())
        metadata = dict(execution.metadata)
        raw_events = metadata.pop("_workflow_events", ())
        workflow_events = tuple(
            dict(item) for item in raw_events if isinstance(item, dict)
        )
        if raw_events:
            execution = ExecutionResult(
                returncode=execution.returncode,
                error_class=execution.error_class,
                metadata=metadata,
            )
        if after_execute is not None:
            after_execute()
        if execution.metadata.get("killed") or execution.metadata.get("resume_after_step") is not None:
            return StageOutcome(
                execution=execution,
                validation=None,
                workflow_events=workflow_events,
            )
        with deadline_scope(request.context.deadline_epoch):
            validation = request.definition.lifecycle.validate(request.context)
            ensure_deadline(now=self._now_epoch())
        return StageOutcome(
            execution=execution,
            validation=validation,
            workflow_events=workflow_events,
        )
