from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..adapters.infrastructure.commands import CommandRunner
from ..adapters.models.backends import build_model_backends
from ..adapters.models.dispatcher import ModelDispatcher
from ..registry import StepDefinition, StepRegistry
from .catalog import STEP_CONTRACTS
from .prompt_step import PromptStep
from .prompting import PromptRenderer
from .specialized import (
    DeliveryStep,
    JudgeStep,
    PaperDraftStep,
    ParallelProposalStep,
    PrecheckedPromptStep,
)
from .validators import validator_for


def build_native_registry(
    factory_root: str | Path,
    *,
    dispatcher: ModelDispatcher | None = None,
    renderer: PromptRenderer | None = None,
    runner: CommandRunner | None = None,
    validator_factory: Callable[[Path, int], object] | None = None,
    fingerprinter: Callable[[Path, str], str] | None = None,
) -> StepRegistry:
    root = Path(factory_root).resolve()
    renderer = renderer or PromptRenderer(root)
    dispatcher = dispatcher or ModelDispatcher(root, build_model_backends(root))
    runner = runner or CommandRunner()
    registry = StepRegistry()
    judge_step: JudgeStep | None = None
    for contract in STEP_CONTRACTS:
        validator = (
            validator_factory(root, contract.id)
            if validator_factory is not None
            else validator_for(root, contract.id)
        )
        prompt_step = (
            PromptStep(contract, renderer, dispatcher, validator)
            if contract.prompt is not None
            else None
        )
        if contract.implementation == "parallel_proposals":
            step = ParallelProposalStep(contract, renderer, dispatcher, validator)
        elif contract.implementation == "sensitivity":
            assert prompt_step is not None
            step = PrecheckedPromptStep(prompt_step, runner, root)
        elif contract.implementation == "paper_draft":
            assert prompt_step is not None
            step = PaperDraftStep(prompt_step)
        elif contract.implementation == "judge":
            judge_step = JudgeStep(contract, root, renderer, dispatcher, validator, runner)
            step = judge_step
        elif contract.implementation == "delivery":
            if judge_step is None:
                raise RuntimeError("judge Step must be registered before delivery Step")
            step = DeliveryStep(
                contract, root, judge_step, validator, runner, fingerprinter
            )
        else:
            if prompt_step is None:
                raise RuntimeError(f"Step {contract.id} has no native implementation")
            step = prompt_step
        registry.register(
            StepDefinition(
                id=contract.id,
                name=contract.name,
                timeout_seconds=contract.timeout_seconds,
                max_attempts=contract.max_attempts,
                max_reopens=contract.max_reopens,
                step=step,
            )
        )
    return registry
