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


@dataclass(frozen=True)
class GatePolicy:
    gate: str
    stage_id: int | None
    subtask_key: str | None
    source_step_id: int | None
    kind: str
    authority: str
    condition: str
    producer: str
    binding: str
    gate_family: str = "exact"
    source_expression: str = ""
    compatibility_diagnostic: str | None = None
    projects_pending_action: bool = True


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


# Machine-readable descriptions of the Gate policies already enforced by the
# v1 Stage/Step runtime.  The existing pending-action projection shares this
# tuple, while the contract compiler consumes it read-only from outside runtime.
GATE_POLICIES: tuple[GatePolicy, ...] = (
    GatePolicy(
        gate="preflight",
        stage_id=1,
        subtask_key=None,
        source_step_id=1,
        kind="human_consultation",
        authority="project_workflow_decision",
        condition="consultation_enabled_and_no_current_immutable_decision",
        producer="factory_core.steps.gates._consultation_gate",
        binding="fixed_owner_stage_from_native_producer",
        projects_pending_action=False,
    ),
    GatePolicy(
        gate="step3",
        stage_id=2,
        subtask_key="method_selection",
        source_step_id=3,
        kind="human_selection",
        authority="project_workflow_decision",
        condition="contest_core_requires_current_bound_selection",
        producer="factory_core.steps.gates.prepare_human_gates",
        binding="fixed_stage_subtask",
    ),
    GatePolicy(
        gate="step4",
        stage_id=2,
        subtask_key=None,
        source_step_id=4,
        kind="human_consultation",
        authority="project_workflow_decision",
        condition="consultation_enabled_and_no_current_immutable_decision",
        producer="factory_core.steps.gates._consultation_gate",
        binding="fixed_owner_stage_from_native_producer",
        projects_pending_action=False,
    ),
    GatePolicy(
        gate="step8_5",
        stage_id=6,
        subtask_key="reviewer_entry_gate",
        source_step_id=8,
        kind="artifact_gate",
        authority="artifact_validator",
        condition="reviewer_entry_artifacts_and_pass_verdict_required",
        producer="factory_core.steps.validators.NativeArtifactValidator._step_9",
        binding="fixed_stage_subtask",
    ),
    GatePolicy(
        gate="conditional_math_preflight",
        stage_id=8,
        subtask_key="conditional_math_preflight",
        source_step_id=13,
        kind="conditional_gate",
        authority="dirty_classifier",
        condition="semantic_dirty_runs_math_preflight_otherwise_bound_skip_receipt",
        producer="factory_core.stages.next_stage_subtask",
        binding="fixed_stage_subtask",
        projects_pending_action=False,
    ),
    GatePolicy(
        gate="content_freeze",
        stage_id=10,
        subtask_key="content_freeze_guard",
        source_step_id=16,
        kind="human_approval",
        authority="project_workflow_decision",
        condition="contest_policy_requires_current_approved_decision",
        producer="factory_core.steps.gates.prepare_human_gates",
        binding="fixed_stage_subtask",
    ),
    GatePolicy(
        gate="delivery_freeze_override",
        stage_id=10,
        subtask_key="content_freeze_guard",
        source_step_id=16,
        kind="human_approval",
        authority="project_workflow_decision",
        condition="post_delivery_freeze_substantive_reopen_requires_approval",
        producer="factory_core.engine.FactoryEngine.run",
        binding="fixed_stage_subtask",
    ),
    GatePolicy(
        gate="dynamic",
        stage_id=None,
        subtask_key=None,
        source_step_id=None,
        kind="human_consultation",
        authority="project_workflow_decision",
        condition="consultation_REQUEST_exists_and_no_current_immutable_decision",
        producer="factory_core.steps.gates._consultation_gate",
        binding="active_stage_or_stage_1_fallback",
        projects_pending_action=False,
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
    for gate in GATE_POLICIES:
        if gate.projects_pending_action and (
            gate.stage_id is None
            or gate.subtask_key is None
            or gate.source_step_id is None
        ):
            raise ValueError(
                f"projected Gate {gate.gate!r} requires a fixed Stage binding"
            )
        if gate.subtask_key is None:
            continue
        stage, subtask = _SUBTASK_BY_KEY[gate.subtask_key]
        if stage.id != gate.stage_id or subtask.source_step_id != gate.source_step_id:
            raise ValueError(f"Gate {gate.gate!r} does not match the Stage catalog")


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


def resume_after_step_for_stage(stage_id: int) -> int:
    """Derive the Step cursor immediately before a Stage owns the workflow.

    Semantic reopen boundaries must follow the Stage catalog rather than a
    second hand-maintained Stage-to-Step mapping.  Stage 1 therefore resumes
    after Step -1, Stage 3 after Step 3, and so on.
    """

    stage = stage_for_id(stage_id)
    return min(subtask.source_step_id for subtask in stage.subtasks) - 1


def subtask_for_key(key: str) -> tuple[StageContract, StageSubtaskContract]:
    try:
        return _SUBTASK_BY_KEY[str(key)]
    except KeyError as exc:
        raise KeyError(f"Stage subtask {key!r} is not defined") from exc


def gate_policy(gate: str) -> GatePolicy:
    matches = tuple(policy for policy in GATE_POLICIES if policy.gate == str(gate))
    if len(matches) != 1:
        raise KeyError(f"Gate policy {gate!r} is not uniquely defined")
    return matches[0]


def native_consultation_policy_for_step(step_id: int) -> GatePolicy | None:
    matches = tuple(
        policy
        for policy in GATE_POLICIES
        if policy.kind == "human_consultation"
        and policy.source_step_id == int(step_id)
        and policy.binding == "fixed_owner_stage_from_native_producer"
    )
    if len(matches) > 1:
        raise ValueError(f"Step {step_id} has multiple fixed consultation Gates")
    return matches[0] if matches else None


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
    for policy in GATE_POLICIES:
        if policy.projects_pending_action and policy.gate == gate:
            assert policy.stage_id is not None
            assert policy.subtask_key is not None
            assert policy.source_step_id is not None
            return policy.stage_id, policy.subtask_key, policy.source_step_id
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
                catalog_for_state(state).stage_for_id(state.active_stage).name
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


# ---------------------------------------------------------------------------
# Stage catalog v2: the paper-segment reorder (optimization plan, phase 2).
#
# Step numbers keep their meaning; only the order and Stage membership of the
# paper segment change:
#
#   v1: ... 11 -> 12 -> 13 precheck -> 14 -> 15 -> [Gate 2] -> 16
#   v2: ... 11 -> 12 -> 14 -> 15 -> [Gate 2] -> 13 final Judge -> 16
#
# In v2 Step 13 is the one full three-role Judge of the post-Step-15 text,
# bound to the approved snapshot; Step 16 only accepts that judgment when the
# snapshot is unchanged.  Because Step 13 now runs after 14 and 15, step
# numbers no longer give the execution order, so every "after" question for a
# v2 project is answered by catalog position, never by comparing step ids.
# v1 keeps its original functions above byte for byte; existing projects stay
# on v1 and only new projects can be created on v2.
# ---------------------------------------------------------------------------

STAGE_CATALOG_VERSION_V2 = "factory-stage-catalog-v2"

FINAL_JUDGE_SUBTASK = "final_judge"

STAGE_CONTRACTS_V2: tuple[StageContract, ...] = (
    *STAGE_CONTRACTS[:7],
    StageContract(8, "REVIEW_AND_REVISE", (_step(11), _step(12))),
    StageContract(9, "FINAL_PROSE", (_step(14), _step(15))),
    StageContract(
        10,
        "JUDGE_AND_DELIVER",
        (
            StageSubtaskContract(
                key="content_freeze_guard",
                source_step_id=16,
                checkpoint_step_id=None,
                kind="human_gate",
            ),
            StageSubtaskContract(
                key=FINAL_JUDGE_SUBTASK,
                source_step_id=13,
                checkpoint_step_id=13,
                kind="final_judge",
            ),
            _step(16),
        ),
    ),
)

# v2 has no conditional math preflight: the one Judge is unconditional.
GATE_POLICIES_V2: tuple[GatePolicy, ...] = tuple(
    policy for policy in GATE_POLICIES if policy.gate != "conditional_math_preflight"
)


class StageCatalog:
    """One versioned Stage catalog; every ordering question uses its position.

    A subtask's *position* is its index in catalog order (Stage by Stage,
    subtask by subtask).  A Step's position is the position of the subtask
    whose ``checkpoint_step_id`` is that Step; Step ``-1`` (nothing completed)
    sits before everything.
    """

    def __init__(
        self,
        version: str,
        contracts: tuple[StageContract, ...],
        gate_policies: tuple[GatePolicy, ...],
    ) -> None:
        self.version = version
        self.contracts = contracts
        self.gate_policies = gate_policies
        self.order: tuple[tuple[StageContract, StageSubtaskContract], ...] = tuple(
            (stage, subtask) for stage in contracts for subtask in stage.subtasks
        )
        self._by_id = {stage.id: stage for stage in contracts}
        self._by_step = {
            subtask.checkpoint_step_id: stage
            for stage, subtask in self.order
            if subtask.checkpoint_step_id is not None
        }
        self._by_key = {subtask.key: (stage, subtask) for stage, subtask in self.order}
        self._step_position = {
            subtask.checkpoint_step_id: index
            for index, (_stage, subtask) in enumerate(self.order)
            if subtask.checkpoint_step_id is not None
        }
        self.validate()

    # -- lookups ---------------------------------------------------------
    def stage_for_id(self, stage_id: int) -> StageContract:
        try:
            return self._by_id[int(stage_id)]
        except KeyError as exc:
            raise KeyError(f"Stage {stage_id} is not defined") from exc

    def stage_for_step(self, step_id: int) -> StageContract:
        try:
            return self._by_step[int(step_id)]
        except KeyError as exc:
            raise KeyError(f"Step {step_id} is not mapped to a Stage") from exc

    def subtask_for_key(self, key: str) -> tuple[StageContract, StageSubtaskContract]:
        try:
            return self._by_key[str(key)]
        except KeyError as exc:
            raise KeyError(f"Stage subtask {key!r} is not defined") from exc

    def gate_policy(self, gate: str) -> GatePolicy:
        matches = tuple(p for p in self.gate_policies if p.gate == str(gate))
        if len(matches) != 1:
            raise KeyError(f"Gate policy {gate!r} is not uniquely defined")
        return matches[0]

    # -- ordering --------------------------------------------------------
    def step_position(self, step_id: int) -> int:
        if int(step_id) < 0:
            return -1
        try:
            return self._step_position[int(step_id)]
        except KeyError as exc:
            raise KeyError(f"Step {step_id} is not mapped to a Stage") from exc

    def subtasks_after_step(self, step_id: int) -> tuple[tuple[int, str], ...]:
        """Every (stage, subtask) positioned after ``step_id``'s checkpoint."""

        position = self.step_position(step_id)
        return tuple(
            (stage.id, subtask.key)
            for index, (stage, subtask) in enumerate(self.order)
            if index > position
        )

    def next_stage_subtask(
        self, *, completed_subtasks: Iterable[tuple[int, str]]
    ) -> tuple[StageContract, StageSubtaskContract] | None:
        completed = {(int(stage), str(subtask)) for stage, subtask in completed_subtasks}
        for stage, subtask in self.order:
            if (stage.id, subtask.key) not in completed:
                return stage, subtask
        return None

    def resume_after_step_for_stage(self, stage_id: int) -> int:
        """The checkpoint Step immediately before ``stage_id`` in catalog order."""

        stage = self.stage_for_id(stage_id)
        first = self.order.index((stage, stage.subtasks[0]))
        previous = [
            subtask.checkpoint_step_id
            for _stage, subtask in self.order[:first]
            if subtask.checkpoint_step_id is not None
        ]
        return previous[-1] if previous else -1

    def completed_stage_for_step(self, step_id: int) -> int:
        """The last fully completed Stage implied by a Step cursor alone.

        A gate subtask is complete only when a checkpoint Step positioned after
        it is, which is the v1 rule for the reviewer-entry and content-freeze
        gates expressed by position.
        """

        position = self.step_position(step_id)
        completed = 0
        for stage in self.contracts:
            last = self.order.index((stage, stage.subtasks[-1]))
            if last > position:
                break
            completed = stage.id
        return completed

    def initial_stage_checkpoints(self, last_completed_step: int) -> list[dict[str, Any]]:
        position = self.step_position(last_completed_step)
        return [
            {
                "stage_id": stage.id,
                "subtask": subtask.key,
                "source_step_id": subtask.source_step_id,
                "completed_step_id": subtask.checkpoint_step_id,
            }
            for index, (stage, subtask) in enumerate(self.order)
            if index <= position
        ]

    # -- contract --------------------------------------------------------
    def validate(self) -> None:
        if [stage.id for stage in self.contracts] != list(range(1, 11)):
            raise ValueError("Stage IDs must be contiguous from 1 through 10")
        step_ids = [
            subtask.checkpoint_step_id
            for _stage, subtask in self.order
            if subtask.checkpoint_step_id is not None
        ]
        expected = [contract.id for contract in STEP_CONTRACTS]
        if sorted(step_ids) != expected or len(step_ids) != len(set(step_ids)):
            raise ValueError("every Step 0-16 must map to exactly one Stage")
        if len(self._by_key) != len(self.order):
            raise ValueError("Stage subtask keys must be unique")
        reviewer_gate = self._by_key.get("reviewer_entry_gate")
        if reviewer_gate is None or reviewer_gate[0].id != 6:
            raise ValueError("Step 8.5 reviewer-entry gate must remain in Stage 6")
        guard = self._by_key.get("content_freeze_guard")
        if guard is None or self.order.index(guard) >= self.step_position(16):
            raise ValueError("the content-freeze guard must precede Step 16")
        for gate in self.gate_policies:
            if gate.projects_pending_action and (
                gate.stage_id is None
                or gate.subtask_key is None
                or gate.source_step_id is None
            ):
                raise ValueError(
                    f"projected Gate {gate.gate!r} requires a fixed Stage binding"
                )
            if gate.subtask_key is None:
                continue
            stage, subtask = self._by_key[gate.subtask_key]
            if stage.id != gate.stage_id or subtask.source_step_id != gate.source_step_id:
                raise ValueError(f"Gate {gate.gate!r} does not match the Stage catalog")
        if self.version == STAGE_CATALOG_VERSION_V2:
            judge = self._by_key.get(FINAL_JUDGE_SUBTASK)
            if judge is None or judge[0].id != 10 or judge[1].checkpoint_step_id != 13:
                raise ValueError("v2: Step 13 must be the final Judge of Stage 10")
            positions = (
                self.step_position(15),
                self.order.index(guard),
                self.step_position(13),
                self.step_position(16),
            )
            if positions != tuple(range(positions[0], positions[0] + 4)):
                raise ValueError("v2: order must be 15 -> content freeze -> 13 -> 16")

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.version,
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
                for stage in self.contracts
            ],
        }


class _StageCatalogV1(StageCatalog):
    """v1 answers through its original functions so its behaviour cannot drift."""

    def resume_after_step_for_stage(self, stage_id: int) -> int:
        return resume_after_step_for_stage(stage_id)

    def completed_stage_for_step(self, step_id: int) -> int:
        return completed_stage_for_step(step_id)

    def initial_stage_checkpoints(self, last_completed_step: int) -> list[dict[str, Any]]:
        return initial_stage_checkpoints(last_completed_step)

    def payload(self) -> dict[str, Any]:
        return stage_catalog_payload()


CATALOG_V1: StageCatalog = _StageCatalogV1(
    STAGE_CATALOG_VERSION, STAGE_CONTRACTS, GATE_POLICIES
)
CATALOG_V2: StageCatalog = StageCatalog(
    STAGE_CATALOG_VERSION_V2, STAGE_CONTRACTS_V2, GATE_POLICIES_V2
)
_CATALOGS = {catalog.version: catalog for catalog in (CATALOG_V1, CATALOG_V2)}
SUPPORTED_STAGE_CATALOG_VERSIONS = tuple(_CATALOGS)


def catalog_for_version(version: str | None) -> StageCatalog:
    """Resolve a persisted catalog version; a Step-scheduler project projects v1."""

    if version is None:
        return CATALOG_V1
    try:
        return _CATALOGS[str(version)]
    except KeyError as exc:
        raise ValueError(f"unsupported Stage catalog version: {version}") from exc


def catalog_for_state(state: WorkflowState) -> StageCatalog:
    return catalog_for_version(state.stage_catalog_version)
