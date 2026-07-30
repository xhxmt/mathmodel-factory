from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..adapters.models.backends import ModelRequest
from ..adapters.models.dispatcher import ModelDispatcher
from ..domain import ExecutionResult, PrepareResult, RecoveryDecision, StepError
from .catalog import StepContract
from .gates import prepare_human_gates
from .prompting import PromptRenderer
from .validators import NativeArtifactValidator


@dataclass
class PromptStep:
    contract: StepContract
    renderer: PromptRenderer
    dispatcher: ModelDispatcher
    validator: NativeArtifactValidator

    def prepare(self, context) -> PrepareResult:
        gate = prepare_human_gates(context.project_dir, context.step_id)
        if not gate.ready:
            return gate
        return PrepareResult.prepared()

    def execute(self, context) -> ExecutionResult:
        assert self.contract.prompt is not None
        prompt = self.renderer.render(
            self.contract.prompt,
            context.project_dir,
            step_key=context.step_id,
            include_preamble=context.step_id != 0,
        )
        result = self.dispatcher.execute(
            ModelRequest(
                project_dir=context.project_dir,
                step_id=context.step_id,
                attempt=context.attempt,
                prompt=prompt,
                timeout_seconds=context.timeout_seconds,
                hang_timeout_seconds=self.contract.hang_timeout_seconds,
            ),
            step_key=context.step_id,
            defaults=self.contract.default_models,
        )
        validation = self.validator.validate(context)
        if validation.metadata.get("killed"):
            return ExecutionResult.succeeded(killed=True)
        return result

    def validate(self, context):
        return self.validator.validate(context)

    def recover(self, context, error: StepError) -> RecoveryDecision:
        return RecoveryDecision.from_validation(
            self.validator.validate(context), active_step=context.step_id
        )
