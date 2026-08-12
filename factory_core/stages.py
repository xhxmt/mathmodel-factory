from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .contest import phase_for_step
from .domain import WorkflowState, WorkflowStatus
from .steps.catalog import STEP_CONTRACTS, contract_for


STAGE_CATALOG_VERSION = "factory-stage-catalog-v1"
STAGE_SCHEDULER_GENERATION = "stage_v1"
STEP_SCHEDULER_GENERATION = "step_v2"


@dataclass(frozen=True)
class StageSubtaskContract:
    key: str
    source_step_id: int
    checkpoint_step_id: int | None
    kind: str = "step"
    conditional: bool = False

    @property
    def step_contract(self):
        return contract_for(self.source_step_id)


@dataclass(frozen=True)
class StageContract:
    id: int
    name: str
    subtasks: tuple[StageSubtaskContract, ...]


def _step(step_id: int, key: str | None = None, *, conditional: bool = False):
    contract = contract_for(step_id)
    return StageSubtaskContract(
        key=key or contract.name,
        source_step_id=step_id,
        checkpoint_step_id=step_id,
        conditional=conditional,
    )


STAGE_CONTRACTS: tuple[StageContract, ...] = (
    StageContract(1, "UNDERSTAND", (_step(0), _step(1))),
    StageContract(2, "MODEL_TOURNAMENT", (_step(2), _step(3))),
    StageContract(3, "MODEL_CONTRACT", (_step(4),)),
    StageContract(4, "SOLVE", (_step(5),)),
    StageContract(5, "VALIDATE_MODEL", (_step(6), _step(7))),
    StageContract(
        6,
        "REVIEWER_ENTRY",
        (
            _step(8),
            StageSubtaskContract(
                key="reviewer_entry_gate",
                source_step_id=8,
                checkpoint_step_id=None,
                kind="reviewer_entry_gate",
            ),
        ),
    ),
    StageContract(7, "DRAFT_AND_AUDIT", (_step(9), _step(10))),
    StageContract(
        8,
        "REVIEW_AND_REVISE",
        (_step(11), _step(12), _step(13, "conditional_math_preflight", conditional=True)),
    ),
    StageContract(9, "FINAL_PROSE", (_step(14), _step(15))),
    StageContract(
        10,
        "FINALIZE",
        (
            StageSubtaskContract(
                key="content_freeze_guard",
                source_step_id=16,
                checkpoint_step_id=None,
                kind="human_gate",
            ),
            _step(16),
        ),
    ),
)


_STAGE_BY_ID = {stage.id: stage for stage in STAGE_CONTRACTS}
_STAGE_BY_STEP = {
    subtask.checkpoint_step_id: stage
    for stage in STAGE_CONTRACTS
    for subtask in stage.subtasks
    if subtask.checkpoint_step_id is not None
}
_SUBTASK_BY_KEY = {
    subtask.key: (stage, subtask)
    for stage in STAGE_CONTRACTS
    for subtask in stage.subtasks
}


def validate_stage_catalog() -> None:
    stage_ids = [stage.id for stage in STAGE_CONTRACTS]
    if stage_ids != list(range(1, 11)):
        raise ValueError("Stage IDs must be contiguous from 1 through 10")
    step_ids = [
        subtask.checkpoint_step_id
        for stage in STAGE_CONTRACTS
        for subtask in stage.subtasks
        if subtask.checkpoint_step_id is not None
    ]
    expected = [contract.id for contract in STEP_CONTRACTS]
    if sorted(step_ids) != expected or len(step_ids) != len(set(step_ids)):
        raise ValueError("every Step 0-16 must map to exactly one Stage")
    if _STAGE_BY_STEP[13].id != 8:
        raise ValueError("Step 13 must remain the conditional exit of Stage 8")
    reviewer_gate = _SUBTASK_BY_KEY.get("reviewer_entry_gate")
    if reviewer_gate is None or reviewer_gate[0].id != 6:
        raise ValueError("Step 8.5 reviewer-entry gate must remain in Stage 6")


validate_stage_catalog()


def stage_for_id(stage_id: int) -> StageContract:
    try:
        return _STAGE_BY_ID[int(stage_id)]
    except KeyError as exc:
        raise KeyError(f"Stage {stage_id} is not defined") from exc


def stage_for_step(step_id: int) -> StageContract:
    try:
        return _STAGE_BY_STEP[int(step_id)]
    except KeyError as exc:
        raise KeyError(f"Step {step_id} is not mapped to a Stage") from exc


def subtask_for_key(key: str) -> tuple[StageContract, StageSubtaskContract]:
    try:
        return _SUBTASK_BY_KEY[str(key)]
    except KeyError as exc:
        raise KeyError(f"Stage subtask {key!r} is not defined") from exc


def completed_stage_for_step(step_id: int) -> int:
    """Return the last fully completed Stage implied by the Step cursor alone.

    Step 8 does not imply Stage 6 completion because the non-integer reviewer
    entry gate is still outstanding.
    """

    completed = 0
    for stage in STAGE_CONTRACTS:
        integer_steps = [
            subtask.checkpoint_step_id
            for subtask in stage.subtasks
            if subtask.checkpoint_step_id is not None
        ]
        has_noninteger_gate = any(
            subtask.checkpoint_step_id is None for subtask in stage.subtasks
        )
        if integer_steps and max(integer_steps) <= int(step_id) and not has_noninteger_gate:
            completed = stage.id
            continue
        if stage.id == 6 and int(step_id) >= 9:
            completed = stage.id
            continue
        if stage.id == 10 and int(step_id) >= 16:
            completed = stage.id
            continue
        break
    return completed


def initial_stage_checkpoints(last_completed_step: int) -> list[dict[str, Any]]:
    """Seed explicit Stage checkpoints from an approved compatibility cursor."""

    seeded: list[dict[str, Any]] = []
    for stage in STAGE_CONTRACTS:
        for subtask in stage.subtasks:
            complete = (
                subtask.checkpoint_step_id is not None
                and subtask.checkpoint_step_id <= int(last_completed_step)
            )
            if subtask.key == "reviewer_entry_gate":
                complete = int(last_completed_step) >= 9
            elif subtask.key == "content_freeze_guard":
                complete = int(last_completed_step) >= 16
            if complete:
                seeded.append(
                    {
                        "stage_id": stage.id,
                        "subtask": subtask.key,
                        "source_step_id": subtask.source_step_id,
                        "completed_step_id": subtask.checkpoint_step_id,
                    }
                )
    return seeded


def next_stage_subtask(
    *,
    completed_subtasks: Iterable[tuple[int, str]],
) -> tuple[StageContract, StageSubtaskContract] | None:
    completed = {(int(stage), str(subtask)) for stage, subtask in completed_subtasks}
    for stage in STAGE_CONTRACTS:
        for subtask in stage.subtasks:
            if (stage.id, subtask.key) not in completed:
                return stage, subtask
    return None


def _pending_projection(state: WorkflowState) -> tuple[int, str, int] | None:
    action = state.pending_action or {}
    gate = str(action.get("gate") or "")
    if gate == "step8_5":
        return 6, "reviewer_entry_gate", 8
    if gate == "step3":
        return 2, "method_selection", 3
    if gate in {"content_freeze", "delivery_freeze_override"}:
        return 10, "content_freeze_guard", 16
    return None


def projected_stage_cursor(state: WorkflowState) -> dict[str, Any]:
    """Project Stage diagnostics for both Stage and unmigrated Step schedulers."""

    if state.scheduler_generation == STAGE_SCHEDULER_GENERATION:
        source = state.source_step_id
        return {
            "stage_catalog_version": state.stage_catalog_version,
            "scheduler_generation": state.scheduler_generation,
            "last_completed_stage": state.last_completed_stage,
            "active_stage": state.active_stage,
            "active_stage_name": (
                stage_for_id(state.active_stage).name
                if state.active_stage is not None
                else None
            ),
            "active_subtask": state.active_subtask,
            "source_step_id": source,
        }

    pending = _pending_projection(state)
    if pending is not None:
        stage_id, subtask, source = pending
    else:
        if state.status is WorkflowStatus.COMPLETED or state.last_completed_step >= 16:
            return {
                "stage_catalog_version": STAGE_CATALOG_VERSION,
                "scheduler_generation": state.scheduler_generation,
                "last_completed_stage": 10,
                "active_stage": None,
                "active_stage_name": None,
                "active_subtask": None,
                "source_step_id": 16,
            }
        source = (
            state.active_step
            if state.active_step is not None
            else min(16, state.last_completed_step + 1)
        )
        stage = stage_for_step(source)
        stage_id = stage.id
        subtask = contract_for(source).name
    return {
        "stage_catalog_version": STAGE_CATALOG_VERSION,
        "scheduler_generation": state.scheduler_generation,
        "last_completed_stage": completed_stage_for_step(state.last_completed_step),
        "active_stage": stage_id,
        "active_stage_name": stage_for_id(stage_id).name,
        "active_subtask": subtask,
        "source_step_id": source,
    }


def stage_catalog_payload() -> dict[str, Any]:
    return {
        "schema_version": STAGE_CATALOG_VERSION,
        "scheduler_generation": STAGE_SCHEDULER_GENERATION,
        "stages": [
            {
                "id": stage.id,
                "name": stage.name,
                "subtasks": [
                    {
                        **asdict(subtask),
                        "contest_phase": phase_for_step(subtask.source_step_id).id,
                        "step_budget": {
                            "timeout_seconds": subtask.step_contract.timeout_seconds,
                            "hang_timeout_seconds": subtask.step_contract.hang_timeout_seconds,
                            "max_attempts": subtask.step_contract.max_attempts,
                            "max_reopens": subtask.step_contract.max_reopens,
                        },
                    }
                    for subtask in stage.subtasks
                ],
            }
            for stage in STAGE_CONTRACTS
        ],
    }
