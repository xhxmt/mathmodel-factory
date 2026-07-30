from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class StepContract:
    id: int
    name: str
    prompt: str | None
    timeout_seconds: int
    hang_timeout_seconds: int
    max_attempts: int
    default_models: tuple[str, ...]
    implementation: str = "prompt"
    max_reopens: int = 0


STEP_CONTRACTS: tuple[StepContract, ...] = (
    StepContract(0, "problem_setup", "step0_problem_parsing.txt", 3_600, 1_800, 1, ("codex",)),
    StepContract(1, "research_and_viability", "step1_research_viability.txt", 14_400, 3_600, 5, ("claude",)),
    StepContract(2, "parallel_model_proposals", None, 28_800, 3_600, 5, ("codex", "claude"), "parallel_proposals"),
    StepContract(3, "method_selection", "step3_method_selection.txt", 7_200, 1_800, 5, ("claude",)),
    StepContract(4, "model_construction", "step4_model_construction.txt", 14_400, 3_600, 5, ("claude", "codex")),
    StepContract(5, "solve", "step5_full_solve.txt", 14_400, 3_600, 5, ("codex", "claude")),
    StepContract(6, "sensitivity", "step6_sensitivity.txt", 10_800, 3_600, 3, ("codex", "claude"), "sensitivity"),
    StepContract(7, "model_evaluation", "step7_model_eval.txt", 7_200, 1_800, 5, ("claude", "codex")),
    StepContract(8, "visualization", "step8_visualization.txt", 10_800, 3_600, 5, ("claude", "codex")),
    StepContract(9, "paper_draft", "step9_paper_draft.txt", 14_400, 3_600, 5, ("claude", "codex"), "paper_draft"),
    StepContract(10, "numerical_gate", "step10_gate1_numerical.txt", 10_800, 3_600, 5, ("codex", "claude")),
    StepContract(11, "constructive_review", "step11_constructive_review.txt", 7_200, 1_800, 5, ("codex", "claude")),
    StepContract(12, "revision", "step12_revision.txt", 14_400, 3_600, 5, ("claude", "codex")),
    StepContract(13, "judge_gate", None, 10_800, 1_800, 2, ("deepseek-chat",), "judge", 1),
    StepContract(14, "abstract", "step14_abstract.txt", 7_200, 1_800, 5, ("claude", "codex")),
    StepContract(15, "polish", "step15_polish.txt", 10_800, 3_600, 5, ("codex", "claude")),
    StepContract(16, "delivery", None, 3_600, 1_800, 1, (), "delivery", 1),
)

_BY_ID = {contract.id: contract for contract in STEP_CONTRACTS}


def contract_for(step_id: int) -> StepContract:
    try:
        return _BY_ID[step_id]
    except KeyError as exc:
        raise KeyError(f"native Step {step_id} is not defined") from exc


def catalog_payload() -> dict[str, Any]:
    return {
        "schema_version": "factory-step-catalog-v1",
        "runtime_generation": "native_v2",
        "steps": [asdict(contract) for contract in STEP_CONTRACTS],
    }
