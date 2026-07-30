from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .domain import (
    ExecutionResult,
    PrepareResult,
    RecoveryDecision,
    StepContext,
    StepError,
    ValidationResult,
)


class StepHandler(Protocol):
    def execute(self, context: StepContext) -> ExecutionResult: ...


class StepValidator(Protocol):
    def validate(self, context: StepContext) -> ValidationResult: ...


class Step(Protocol):
    def prepare(self, context: StepContext) -> PrepareResult: ...

    def execute(self, context: StepContext) -> ExecutionResult: ...

    def validate(self, context: StepContext) -> ValidationResult: ...

    def recover(self, context: StepContext, error: StepError) -> RecoveryDecision: ...


class ModelBackend(Protocol):
    def execute(self, request) -> ExecutionResult: ...


class SolverBackend(Protocol):
    def submit(self, request): ...

    def status(self, job): ...

    def cancel(self, job): ...


class ExecutionBackend(ModelBackend, Protocol):
    """Compatibility alias for the phase-1 generic backend contract."""


@dataclass(frozen=True)
class LegacyStepLifecycle:
    handler: StepHandler
    validator: StepValidator

    def prepare(self, context: StepContext) -> PrepareResult:
        return PrepareResult.prepared()

    def execute(self, context: StepContext) -> ExecutionResult:
        return self.handler.execute(context)

    def validate(self, context: StepContext) -> ValidationResult:
        return self.validator.validate(context)

    def recover(self, context: StepContext, error: StepError) -> RecoveryDecision:
        return RecoveryDecision.from_validation(
            self.validator.validate(context), active_step=context.step_id
        )


@dataclass(frozen=True)
class StepDefinition:
    id: int
    name: str
    timeout_seconds: int
    max_attempts: int
    # Compatibility definitions created before native contracts allowed one
    # validator-directed rewind. Native catalog entries always set this field
    # explicitly, including zero for Steps that cannot reopen.
    max_reopens: int = 1
    handler: StepHandler | None = None
    validator: StepValidator | None = None
    step: Step | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if self.max_reopens < 0:
            raise ValueError("max_reopens cannot be negative")
        if self.step is None and (self.handler is None or self.validator is None):
            raise ValueError("step or handler+validator must be provided")
        if self.step is not None and (self.handler is not None or self.validator is not None):
            raise ValueError("step cannot be combined with handler or validator")

    @property
    def lifecycle(self) -> Step:
        if self.step is not None:
            return self.step
        assert self.handler is not None and self.validator is not None
        return LegacyStepLifecycle(self.handler, self.validator)


class StepRegistry:
    def __init__(self) -> None:
        self._steps: dict[int, StepDefinition] = {}

    def register(self, definition: StepDefinition) -> None:
        if definition.id in self._steps:
            raise ValueError(f"step {definition.id} is already registered")
        self._steps[definition.id] = definition

    def get(self, step_id: int) -> StepDefinition:
        try:
            return self._steps[step_id]
        except KeyError as exc:
            raise KeyError(f"step {step_id} is not registered") from exc

    def next_after(self, completed_step: int) -> StepDefinition | None:
        for step_id in sorted(self._steps):
            if step_id > completed_step:
                return self._steps[step_id]
        return None

    def __iter__(self):
        return iter(self._steps[step_id] for step_id in sorted(self._steps))


class BackendRegistry:
    def __init__(self) -> None:
        self._backends: dict[str, ExecutionBackend] = {}

    def register(self, name: str, backend: ExecutionBackend) -> None:
        if name in self._backends:
            raise ValueError(f"backend {name!r} is already registered")
        self._backends[name] = backend

    def get(self, name: str) -> ExecutionBackend:
        try:
            return self._backends[name]
        except KeyError as exc:
            raise KeyError(f"backend {name!r} is not registered") from exc


class ModelBackendRegistry(BackendRegistry):
    pass


class SolverBackendRegistry:
    def __init__(self) -> None:
        self._backends: dict[str, SolverBackend] = {}

    def register(self, name: str, backend: SolverBackend) -> None:
        if name in self._backends:
            raise ValueError(f"backend {name!r} is already registered")
        self._backends[name] = backend

    def get(self, name: str) -> SolverBackend:
        try:
            return self._backends[name]
        except KeyError as exc:
            raise KeyError(f"backend {name!r} is not registered") from exc
