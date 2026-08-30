from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from functools import lru_cache
from typing import Iterable

from .artifact_ownership import (
    ARTIFACT_OWNERSHIP_REGISTRY,
    ARTIFACT_OWNERSHIP_SCHEMA,
    ArtifactOwnership,
)
from .canonical import (
    CANONICAL_JSON_SCHEMA,
    canonical_bytes,
    canonical_sha256,
)
from .contest import CONTEST_PHASES, ContestPhase
from .dirty import DIRTY_CLASSIFIER_SCHEMA, DirtyFlag, semantic_flags
from .domain import SCHEMA_VERSION
from .owner_compiler import (
    OWNER_COMPILER_MODE,
    OwnerDiagnostic,
    OwnerDiagnosticCode,
    OwnerCompilation,
    OwnerPriorityAuthorization,
    OwnerRuleContract,
    compile_owner_registry,
)
from .stages import (
    GATE_POLICIES,
    STAGE_CATALOG_VERSION,
    STAGE_CONTRACTS,
    STAGE_SCHEDULER_GENERATION,
    GatePolicy,
    StageContract,
)
from .steps.catalog import STEP_CONTRACTS, StepContract


WORKFLOW_CONTRACT_BUNDLE_SCHEMA = "workflow-contract-bundle-v1"
STEP_CATALOG_VERSION = "factory-step-catalog-v2"
RUNTIME_GENERATION = "native_v2"
CONTEST_PROFILE = "contest_core_v1"


@dataclass(frozen=True)
class ConditionContract:
    operator: str
    operands: tuple[str, ...]


@dataclass(frozen=True)
class BudgetContract:
    timeout_seconds: int
    hang_timeout_seconds: int
    max_attempts: int
    max_reopens: int


@dataclass(frozen=True)
class StepContractValue:
    step_id: int
    step_contract_id: str
    name: str
    prompt: str | None
    implementation: str
    contest_phase_id: int
    owner_id: str
    authority: str
    budget: BudgetContract
    default_models: tuple[str, ...]


@dataclass(frozen=True)
class StageSubtaskValue:
    subtask_id: str
    key: str
    source_step_id: int
    checkpoint_step_id: int | None
    kind: str
    contest_phase_id: int
    owner_id: str
    authority: str
    condition: ConditionContract
    budget: BudgetContract


@dataclass(frozen=True)
class StageContractValue:
    stage_id: int
    stage_contract_id: str
    name: str
    authority: str
    subtasks: tuple[StageSubtaskValue, ...]


@dataclass(frozen=True)
class GateContractValue:
    gate_id: str
    gate: str
    gate_family: str
    stage_id: int | None
    subtask_id: str | None
    source_step_id: int | None
    kind: str
    authority: str
    condition: ConditionContract
    producer: str
    binding: str
    source_expression: str
    compatibility_diagnostic: str | None
    projects_pending_action: bool


@dataclass(frozen=True)
class ContestPhaseValue:
    phase_id: int
    phase_contract_id: str
    name: str
    steps: tuple[int, ...]
    human_gate: str | None
    authority: str


@dataclass(frozen=True)
class ClassifierIdentity:
    classifier_id: str
    dirty_classifier_schema_version: str
    ownership_schema_version: str
    dirty_flags: tuple[str, ...]
    semantic_dirty_flags: tuple[str, ...]
    rule_identity_sha256: str


@dataclass(frozen=True)
class WorkflowContractBundle:
    schema_version: str
    canonicalization_schema_version: str
    workflow_state_schema_version: int
    runtime_generation: str
    scheduler_generation: str
    stage_catalog_version: str
    step_catalog_version: str
    contest_profile: str
    stages: tuple[StageContractValue, ...]
    steps: tuple[StepContractValue, ...]
    gates: tuple[GateContractValue, ...]
    contest_phases: tuple[ContestPhaseValue, ...]
    classifier: ClassifierIdentity
    owner_compilation: OwnerCompilation


class WorkflowContractValidationError(ValueError):
    """Raised when the compiled source facts do not form one valid bundle."""


def _require_runtime_type(value: object, expected: type, path: str) -> None:
    """Require one exact DTO type before any nested attribute is accessed.

    The workflow contract is a closed, immutable wire shape.  Subclasses could
    add mutable or non-canonical state while still satisfying ``isinstance``,
    so this boundary intentionally accepts the declared dataclass types only.
    """

    if type(value) is not expected:
        raise WorkflowContractValidationError(
            f"{path} has an unsupported runtime type"
        )
    if issubclass(expected, Enum):
        if not any(value is member for member in expected):
            raise WorkflowContractValidationError(
                f"{path} is not a registered enum member"
            )
        return
    if is_dataclass(expected):
        for item in fields(expected):
            try:
                object.__getattribute__(value, item.name)
            except AttributeError as exc:
                raise WorkflowContractValidationError(
                    f"{path}.{item.name} is missing"
                ) from exc


def _require_tuple(value: object, path: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise WorkflowContractValidationError(
            f"{path} must be an immutable tuple"
        )
    return value


def _require_string(value: object, path: str) -> None:
    if type(value) is not str:
        raise WorkflowContractValidationError(f"{path} must be a string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise WorkflowContractValidationError(
            f"{path} must contain valid UTF-8 scalar values"
        ) from exc


def _require_optional_string(value: object, path: str) -> None:
    if value is not None and type(value) is not str:
        raise WorkflowContractValidationError(
            f"{path} must be a string or None"
        )
    if value is not None:
        _require_string(value, path)


def _require_integer(value: object, path: str) -> None:
    if type(value) is not int:
        raise WorkflowContractValidationError(f"{path} must be an integer")


def _require_optional_integer(value: object, path: str) -> None:
    if value is not None and type(value) is not int:
        raise WorkflowContractValidationError(
            f"{path} must be an integer or None"
        )


def _require_boolean(value: object, path: str) -> None:
    if type(value) is not bool:
        raise WorkflowContractValidationError(f"{path} must be a boolean")


def _validate_string_tuple(value: object, path: str) -> None:
    for index, item in enumerate(_require_tuple(value, path)):
        _require_string(item, f"{path}[{index}]")


def _validate_integer_tuple(value: object, path: str) -> None:
    for index, item in enumerate(_require_tuple(value, path)):
        _require_integer(item, f"{path}[{index}]")


def _validate_budget_runtime(value: object, path: str) -> None:
    _require_runtime_type(value, BudgetContract, path)
    budget = value
    assert type(budget) is BudgetContract
    for field_name in (
        "timeout_seconds",
        "hang_timeout_seconds",
        "max_attempts",
        "max_reopens",
    ):
        _require_integer(getattr(budget, field_name), f"{path}.{field_name}")


def _validate_condition_runtime(value: object, path: str) -> None:
    _require_runtime_type(value, ConditionContract, path)
    condition = value
    assert type(condition) is ConditionContract
    _require_string(condition.operator, f"{path}.operator")
    _validate_string_tuple(condition.operands, f"{path}.operands")


def _validate_owner_authorization_runtime(value: object, path: str) -> None:
    _require_runtime_type(value, OwnerPriorityAuthorization, path)
    authorization = value
    assert type(authorization) is OwnerPriorityAuthorization
    for field_name in (
        "winner_pattern",
        "loser_pattern",
        "issue_id",
        "rationale",
    ):
        _require_string(
            getattr(authorization, field_name),
            f"{path}.{field_name}",
        )
    _require_integer(
        authorization.winner_owner_stage,
        f"{path}.winner_owner_stage",
    )
    _require_integer(
        authorization.loser_owner_stage,
        f"{path}.loser_owner_stage",
    )


def _validate_owner_rule_runtime(value: object, path: str) -> None:
    _require_runtime_type(value, OwnerRuleContract, path)
    rule = value
    assert type(rule) is OwnerRuleContract
    for field_name in (
        "rule_id",
        "pattern",
        "owner_id",
        "semantic_domain",
        "dirty_flag",
    ):
        _require_string(getattr(rule, field_name), f"{path}.{field_name}")
    _require_integer(rule.priority_index, f"{path}.priority_index")
    _require_integer(rule.owner_stage, f"{path}.owner_stage")
    _require_boolean(rule.final_input, f"{path}.final_input")
    _require_boolean(rule.submission_member, f"{path}.submission_member")
    for index, authorization in enumerate(
        _require_tuple(
            rule.priority_authorizations,
            f"{path}.priority_authorizations",
        )
    ):
        _validate_owner_authorization_runtime(
            authorization,
            f"{path}.priority_authorizations[{index}]",
        )


def _validate_owner_diagnostic_runtime(value: object, path: str) -> None:
    _require_runtime_type(value, OwnerDiagnostic, path)
    diagnostic = value
    assert type(diagnostic) is OwnerDiagnostic
    _require_runtime_type(diagnostic.code, OwnerDiagnosticCode, f"{path}.code")
    _validate_string_tuple(diagnostic.rule_ids, f"{path}.rule_ids")
    for field_name in (
        "path",
        "witness",
        "issue_id",
        "rationale",
    ):
        _require_optional_string(
            getattr(diagnostic, field_name),
            f"{path}.{field_name}",
        )
    _require_string(diagnostic.explanation, f"{path}.explanation")


def _validate_workflow_contract_runtime_structure(
    value: object,
) -> WorkflowContractBundle:
    """Validate the complete frozen DTO graph before semantic inspection.

    This function deliberately precedes all contract equality, collection
    traversal and canonical identity work.  Consequently malformed supplied
    values fail at the public contract boundary instead of leaking Python
    attribute or canonicalizer implementation errors.
    """

    _require_runtime_type(value, WorkflowContractBundle, "bundle")
    bundle = value
    assert type(bundle) is WorkflowContractBundle

    for field_name in (
        "schema_version",
        "canonicalization_schema_version",
        "runtime_generation",
        "scheduler_generation",
        "stage_catalog_version",
        "step_catalog_version",
        "contest_profile",
    ):
        _require_string(getattr(bundle, field_name), f"bundle.{field_name}")
    _require_integer(
        bundle.workflow_state_schema_version,
        "bundle.workflow_state_schema_version",
    )

    for stage_index, stage_value in enumerate(
        _require_tuple(bundle.stages, "bundle.stages")
    ):
        stage_path = f"bundle.stages[{stage_index}]"
        _require_runtime_type(stage_value, StageContractValue, stage_path)
        stage = stage_value
        assert type(stage) is StageContractValue
        _require_integer(stage.stage_id, f"{stage_path}.stage_id")
        for field_name in ("stage_contract_id", "name", "authority"):
            _require_string(getattr(stage, field_name), f"{stage_path}.{field_name}")
        for subtask_index, subtask_value in enumerate(
            _require_tuple(stage.subtasks, f"{stage_path}.subtasks")
        ):
            subtask_path = f"{stage_path}.subtasks[{subtask_index}]"
            _require_runtime_type(subtask_value, StageSubtaskValue, subtask_path)
            subtask = subtask_value
            assert type(subtask) is StageSubtaskValue
            for field_name in (
                "subtask_id",
                "key",
                "kind",
                "owner_id",
                "authority",
            ):
                _require_string(
                    getattr(subtask, field_name),
                    f"{subtask_path}.{field_name}",
                )
            _require_integer(
                subtask.source_step_id,
                f"{subtask_path}.source_step_id",
            )
            _require_optional_integer(
                subtask.checkpoint_step_id,
                f"{subtask_path}.checkpoint_step_id",
            )
            _require_integer(
                subtask.contest_phase_id,
                f"{subtask_path}.contest_phase_id",
            )
            _validate_condition_runtime(
                subtask.condition,
                f"{subtask_path}.condition",
            )
            _validate_budget_runtime(
                subtask.budget,
                f"{subtask_path}.budget",
            )

    for step_index, step_value in enumerate(
        _require_tuple(bundle.steps, "bundle.steps")
    ):
        step_path = f"bundle.steps[{step_index}]"
        _require_runtime_type(step_value, StepContractValue, step_path)
        step = step_value
        assert type(step) is StepContractValue
        _require_integer(step.step_id, f"{step_path}.step_id")
        _require_integer(
            step.contest_phase_id,
            f"{step_path}.contest_phase_id",
        )
        for field_name in (
            "step_contract_id",
            "name",
            "implementation",
            "owner_id",
            "authority",
        ):
            _require_string(getattr(step, field_name), f"{step_path}.{field_name}")
        _require_optional_string(step.prompt, f"{step_path}.prompt")
        _validate_budget_runtime(step.budget, f"{step_path}.budget")
        _validate_string_tuple(
            step.default_models,
            f"{step_path}.default_models",
        )

    for gate_index, gate_value in enumerate(
        _require_tuple(bundle.gates, "bundle.gates")
    ):
        gate_path = f"bundle.gates[{gate_index}]"
        _require_runtime_type(gate_value, GateContractValue, gate_path)
        gate = gate_value
        assert type(gate) is GateContractValue
        for field_name in (
            "gate_id",
            "gate",
            "gate_family",
            "kind",
            "authority",
            "producer",
            "binding",
            "source_expression",
        ):
            _require_string(getattr(gate, field_name), f"{gate_path}.{field_name}")
        _require_optional_integer(gate.stage_id, f"{gate_path}.stage_id")
        _require_optional_string(gate.subtask_id, f"{gate_path}.subtask_id")
        _require_optional_integer(
            gate.source_step_id,
            f"{gate_path}.source_step_id",
        )
        _validate_condition_runtime(gate.condition, f"{gate_path}.condition")
        _require_optional_string(
            gate.compatibility_diagnostic,
            f"{gate_path}.compatibility_diagnostic",
        )
        _require_boolean(
            gate.projects_pending_action,
            f"{gate_path}.projects_pending_action",
        )

    for phase_index, phase_value in enumerate(
        _require_tuple(bundle.contest_phases, "bundle.contest_phases")
    ):
        phase_path = f"bundle.contest_phases[{phase_index}]"
        _require_runtime_type(phase_value, ContestPhaseValue, phase_path)
        phase = phase_value
        assert type(phase) is ContestPhaseValue
        _require_integer(phase.phase_id, f"{phase_path}.phase_id")
        for field_name in ("phase_contract_id", "name", "authority"):
            _require_string(getattr(phase, field_name), f"{phase_path}.{field_name}")
        _validate_integer_tuple(phase.steps, f"{phase_path}.steps")
        _require_optional_string(phase.human_gate, f"{phase_path}.human_gate")

    _require_runtime_type(bundle.classifier, ClassifierIdentity, "bundle.classifier")
    classifier = bundle.classifier
    assert type(classifier) is ClassifierIdentity
    for field_name in (
        "classifier_id",
        "dirty_classifier_schema_version",
        "ownership_schema_version",
        "rule_identity_sha256",
    ):
        _require_string(
            getattr(classifier, field_name),
            f"bundle.classifier.{field_name}",
        )
    _validate_string_tuple(
        classifier.dirty_flags,
        "bundle.classifier.dirty_flags",
    )
    _validate_string_tuple(
        classifier.semantic_dirty_flags,
        "bundle.classifier.semantic_dirty_flags",
    )

    _require_runtime_type(
        bundle.owner_compilation,
        OwnerCompilation,
        "bundle.owner_compilation",
    )
    owner = bundle.owner_compilation
    assert type(owner) is OwnerCompilation
    for field_name in ("schema_version", "mode", "ownership_schema_version"):
        _require_string(
            getattr(owner, field_name),
            f"bundle.owner_compilation.{field_name}",
        )
    for rule_index, rule in enumerate(
        _require_tuple(owner.rules, "bundle.owner_compilation.rules")
    ):
        _validate_owner_rule_runtime(
            rule,
            f"bundle.owner_compilation.rules[{rule_index}]",
        )
    for diagnostic_index, diagnostic in enumerate(
        _require_tuple(
            owner.diagnostics,
            "bundle.owner_compilation.diagnostics",
        )
    ):
        _validate_owner_diagnostic_runtime(
            diagnostic,
            f"bundle.owner_compilation.diagnostics[{diagnostic_index}]",
        )
    return bundle


def _budget(contract: StepContract) -> BudgetContract:
    return BudgetContract(
        timeout_seconds=int(contract.timeout_seconds),
        hang_timeout_seconds=int(contract.hang_timeout_seconds),
        max_attempts=int(contract.max_attempts),
        max_reopens=int(contract.max_reopens),
    )


def _phase_by_step(phases: tuple[ContestPhase, ...]) -> dict[int, ContestPhase]:
    return {step_id: phase for phase in phases for step_id in phase.steps}


def _stage_by_step(stages: tuple[StageContract, ...]) -> dict[int, StageContract]:
    return {
        subtask.checkpoint_step_id: stage
        for stage in stages
        for subtask in stage.subtasks
        if subtask.checkpoint_step_id is not None
    }


def _condition_for_subtask(key: str, conditional: bool) -> ConditionContract:
    if conditional:
        return ConditionContract(
            operator="ANY",
            operands=_semantic_dirty_flags(),
        )
    return ConditionContract(operator="ALWAYS", operands=())


def _condition_for_gate(policy: GatePolicy) -> ConditionContract:
    if policy.gate == "conditional_math_preflight":
        return ConditionContract(
            operator="ANY",
            operands=_semantic_dirty_flags(),
        )
    return ConditionContract(operator="POLICY", operands=(policy.condition,))


def _semantic_dirty_flags() -> tuple[str, ...]:
    """Derive the condition domain through the current classifier truth path."""

    records = tuple({"flag": flag.value} for flag in DirtyFlag)
    selected = semantic_flags(records)
    return tuple(flag.value for flag in DirtyFlag if flag.value in selected)


def _compile_step_contract_values(
    step_contracts: tuple[StepContract, ...],
    stage_contracts: tuple[StageContract, ...],
    contest_phases: tuple[ContestPhase, ...],
) -> tuple[StepContractValue, ...]:
    """Compile the behavior-bearing Step catalog from source contracts.

    ``prompt`` remains an analysis-only source locator.  Validation compares
    the remaining fields explicitly so a prompt relocation does not become a
    behavior change while a supplied Step value cannot authorize itself.
    """

    phases_by_step = _phase_by_step(contest_phases)
    stages_by_step = _stage_by_step(stage_contracts)
    return tuple(
        StepContractValue(
            step_id=int(step.id),
            step_contract_id=f"step:{int(step.id)}",
            name=str(step.name),
            prompt=step.prompt,
            implementation=str(step.implementation),
            contest_phase_id=int(phases_by_step[step.id].id),
            owner_id=f"owner:stage:{int(stages_by_step[step.id].id)}",
            authority="step_validation_evidence",
            budget=_budget(step),
            default_models=tuple(step.default_models),
        )
        for step in step_contracts
    )


def _compile_gate_contract_values(
    gate_policies: tuple[GatePolicy, ...],
) -> tuple[GateContractValue, ...]:
    """Compile Gate behavior while retaining ``producer`` as analysis-only."""

    return tuple(
        GateContractValue(
            gate_id=(
                f"gate:{policy.gate}"
                if policy.gate_family == "exact"
                else f"gate-family:{policy.gate}"
            ),
            gate=str(policy.gate),
            gate_family=str(policy.gate_family),
            stage_id=(int(policy.stage_id) if policy.stage_id is not None else None),
            subtask_id=(
                f"subtask:stage:{int(policy.stage_id)}:{policy.subtask_key}"
                if policy.stage_id is not None and policy.subtask_key is not None
                else None
            ),
            source_step_id=(
                int(policy.source_step_id)
                if policy.source_step_id is not None
                else None
            ),
            kind=str(policy.kind),
            authority=str(policy.authority),
            condition=_condition_for_gate(policy),
            producer=str(policy.producer),
            binding=str(policy.binding),
            source_expression=str(policy.source_expression),
            compatibility_diagnostic=policy.compatibility_diagnostic,
            projects_pending_action=bool(policy.projects_pending_action),
        )
        for policy in gate_policies
    )


def _compile_contest_phase_values(
    contest_phases: tuple[ContestPhase, ...],
) -> tuple[ContestPhaseValue, ...]:
    return tuple(
        ContestPhaseValue(
            phase_id=int(phase.id),
            phase_contract_id=f"contest-phase:{int(phase.id)}",
            name=str(phase.name),
            steps=tuple(int(step_id) for step_id in phase.steps),
            human_gate=phase.human_gate,
            authority="contest_phase_projection",
        )
        for phase in contest_phases
    )


def _compile_classifier_projection(
    owner_compilation: OwnerCompilation,
) -> ClassifierIdentity:
    """Compile classifier behavior from current source classifier truth."""

    return ClassifierIdentity(
        classifier_id=f"classifier:{DIRTY_CLASSIFIER_SCHEMA}",
        dirty_classifier_schema_version=DIRTY_CLASSIFIER_SCHEMA,
        ownership_schema_version=ARTIFACT_OWNERSHIP_SCHEMA,
        dirty_flags=tuple(flag.value for flag in DirtyFlag),
        semantic_dirty_flags=_semantic_dirty_flags(),
        rule_identity_sha256=canonical_sha256(
            tuple(rule.rule_id for rule in owner_compilation.rules)
        ),
    )


@lru_cache(maxsize=1)
def _compile_source_owner_behavior() -> OwnerCompilation:
    """Compile the immutable source owner truth independently of supplied DTOs."""

    return compile_owner_registry(ARTIFACT_OWNERSHIP_REGISTRY)


def _compile_stage_contract_values(
    stage_contracts: tuple[StageContract, ...],
    step_contracts: tuple[StepContract, ...],
    contest_phases: tuple[ContestPhase, ...],
) -> tuple[StageContractValue, ...]:
    """Compile the behavior-bearing Stage catalog from source contracts.

    This is shared by bundle construction and trust-root validation so a
    caller-supplied bundle cannot become the source of truth for its own Stage
    projection.
    """

    steps_by_id = {step.id: step for step in step_contracts}
    phases_by_step = _phase_by_step(contest_phases)
    return tuple(
        StageContractValue(
            stage_id=int(stage.id),
            stage_contract_id=f"stage:{int(stage.id)}",
            name=str(stage.name),
            authority="stage_scheduler",
            subtasks=tuple(
                StageSubtaskValue(
                    subtask_id=f"subtask:stage:{int(stage.id)}:{subtask.key}",
                    key=str(subtask.key),
                    source_step_id=int(subtask.source_step_id),
                    checkpoint_step_id=(
                        int(subtask.checkpoint_step_id)
                        if subtask.checkpoint_step_id is not None
                        else None
                    ),
                    kind=str(subtask.kind),
                    contest_phase_id=int(phases_by_step[subtask.source_step_id].id),
                    owner_id=f"owner:stage:{int(stage.id)}",
                    authority="stage_scheduler",
                    condition=_condition_for_subtask(
                        subtask.key, bool(subtask.conditional)
                    ),
                    budget=_budget(steps_by_id[subtask.source_step_id]),
                )
                for subtask in stage.subtasks
            ),
        )
        for stage in stage_contracts
    )


def _validate_stage_conditions(stages: tuple[StageContractValue, ...]) -> None:
    """Validate the one versioned conditional Stage-v1 behavior contract."""

    conditional: list[tuple[StageContractValue, StageSubtaskValue]] = []
    for stage in stages:
        for subtask in stage.subtasks:
            if subtask.condition.operator == "ALWAYS":
                if subtask.condition.operands:
                    raise WorkflowContractValidationError(
                        "ALWAYS Stage subtask conditions must have empty operands"
                    )
                continue
            conditional.append((stage, subtask))

    if len(conditional) != 1:
        raise WorkflowContractValidationError(
            "Stage catalog must contain exactly one non-ALWAYS subtask"
        )
    stage, subtask = conditional[0]
    if (
        stage.stage_id != 8
        or subtask.key != "conditional_math_preflight"
        or subtask.source_step_id != 13
    ):
        raise WorkflowContractValidationError(
            "the only conditional Stage subtask must be Stage 8 Step 13"
        )
    if subtask.condition.operator != "ANY":
        raise WorkflowContractValidationError(
            "conditional_math_preflight must use the ANY operator"
        )
    operands = subtask.condition.operands
    if len(set(operands)) != len(operands):
        raise WorkflowContractValidationError(
            "conditional_math_preflight operands contain duplicates"
        )
    expected_operands = _semantic_dirty_flags()
    if any(operand not in expected_operands for operand in operands):
        raise WorkflowContractValidationError(
            "conditional_math_preflight contains an unknown semantic operand"
        )
    if operands != expected_operands:
        raise WorkflowContractValidationError(
            "conditional_math_preflight operands must exactly match source semantics"
        )


def compile_workflow_contract_bundle(
    *,
    stage_contracts: Iterable[StageContract] = STAGE_CONTRACTS,
    step_contracts: Iterable[StepContract] = STEP_CONTRACTS,
    gate_policies: Iterable[GatePolicy] = GATE_POLICIES,
    contest_phases: Iterable[ContestPhase] = CONTEST_PHASES,
    owner_registry: Iterable[ArtifactOwnership] = ARTIFACT_OWNERSHIP_REGISTRY,
) -> WorkflowContractBundle:
    """Compile immutable source constants; no runtime or filesystem reads occur."""

    source_stages = tuple(stage_contracts)
    source_steps = tuple(step_contracts)
    source_gates = tuple(gate_policies)
    source_phases = tuple(contest_phases)
    owner_compilation = compile_owner_registry(owner_registry)

    stages = _compile_stage_contract_values(
        source_stages,
        source_steps,
        source_phases,
    )

    steps = _compile_step_contract_values(
        source_steps,
        source_stages,
        source_phases,
    )
    gates = _compile_gate_contract_values(source_gates)
    phases = _compile_contest_phase_values(source_phases)
    classifier = _compile_classifier_projection(owner_compilation)

    bundle = WorkflowContractBundle(
        schema_version=WORKFLOW_CONTRACT_BUNDLE_SCHEMA,
        canonicalization_schema_version=CANONICAL_JSON_SCHEMA,
        workflow_state_schema_version=int(SCHEMA_VERSION),
        runtime_generation=RUNTIME_GENERATION,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
        stage_catalog_version=STAGE_CATALOG_VERSION,
        step_catalog_version=STEP_CATALOG_VERSION,
        contest_profile=CONTEST_PROFILE,
        stages=stages,
        steps=steps,
        gates=gates,
        contest_phases=phases,
        classifier=classifier,
        owner_compilation=owner_compilation,
    )
    return validate_workflow_contract_bundle(bundle)


def _validate_step_behavior_projection(
    supplied: tuple[StepContractValue, ...],
    expected: tuple[StepContractValue, ...],
) -> None:
    if len(supplied) != len(expected):
        raise WorkflowContractValidationError(
            "Step catalog behavior projection has an unexpected length"
        )
    behavior_fields = (
        "step_id",
        "step_contract_id",
        "name",
        "implementation",
        "contest_phase_id",
        "owner_id",
        "authority",
        "default_models",
    )
    budget_fields = (
        "timeout_seconds",
        "hang_timeout_seconds",
        "max_attempts",
        "max_reopens",
    )
    for index, (actual, source) in enumerate(zip(supplied, expected, strict=True)):
        for field_name in behavior_fields:
            if getattr(actual, field_name) != getattr(source, field_name):
                raise WorkflowContractValidationError(
                    f"Step catalog behavior drift at index {index} field {field_name}"
                )
        for field_name in budget_fields:
            if getattr(actual.budget, field_name) != getattr(
                source.budget, field_name
            ):
                raise WorkflowContractValidationError(
                    "Step catalog behavior drift at index "
                    f"{index} field budget.{field_name}"
                )


def _validate_gate_behavior_projection(
    supplied: tuple[GateContractValue, ...],
    expected: tuple[GateContractValue, ...],
) -> None:
    if len(supplied) != len(expected):
        raise WorkflowContractValidationError(
            "Gate behavior projection has an unexpected length"
        )
    behavior_fields = (
        "gate_id",
        "gate",
        "gate_family",
        "stage_id",
        "subtask_id",
        "source_step_id",
        "kind",
        "authority",
        "binding",
        "source_expression",
        "compatibility_diagnostic",
        "projects_pending_action",
    )
    for index, (actual, source) in enumerate(zip(supplied, expected, strict=True)):
        for field_name in behavior_fields:
            if getattr(actual, field_name) != getattr(source, field_name):
                raise WorkflowContractValidationError(
                    f"Gate behavior drift at index {index} field {field_name}"
                )
        for field_name in ("operator", "operands"):
            if getattr(actual.condition, field_name) != getattr(
                source.condition, field_name
            ):
                raise WorkflowContractValidationError(
                    "Gate behavior drift at index "
                    f"{index} field condition.{field_name}"
                )


def _validate_contest_phase_behavior_projection(
    supplied: tuple[ContestPhaseValue, ...],
    expected: tuple[ContestPhaseValue, ...],
) -> None:
    if len(supplied) != len(expected):
        raise WorkflowContractValidationError(
            "ContestPhase behavior projection has an unexpected length"
        )
    behavior_fields = (
        "phase_id",
        "phase_contract_id",
        "name",
        "steps",
        "human_gate",
        "authority",
    )
    for index, (actual, source) in enumerate(zip(supplied, expected, strict=True)):
        for field_name in behavior_fields:
            if getattr(actual, field_name) != getattr(source, field_name):
                raise WorkflowContractValidationError(
                    "ContestPhase behavior drift at index "
                    f"{index} field {field_name}"
                )


def _validate_owner_behavior_projection(
    supplied: OwnerCompilation,
    expected: OwnerCompilation,
) -> None:
    if supplied.mode != expected.mode:
        raise WorkflowContractValidationError("unsupported owner resolution mode")
    if supplied.ownership_schema_version != expected.ownership_schema_version:
        raise WorkflowContractValidationError("owner ownership schema identity drift")
    if len(supplied.rules) != len(expected.rules):
        raise WorkflowContractValidationError(
            "owner behavior rule projection has an unexpected length"
        )
    behavior_fields = (
        "rule_id",
        "priority_index",
        "pattern",
        "owner_id",
        "owner_stage",
        "semantic_domain",
        "dirty_flag",
        "final_input",
        "submission_member",
        "priority_authorizations",
    )
    for index, (actual, source) in enumerate(
        zip(supplied.rules, expected.rules, strict=True)
    ):
        for field_name in behavior_fields:
            if getattr(actual, field_name) != getattr(source, field_name):
                raise WorkflowContractValidationError(
                    f"owner behavior rule drift at index {index} field {field_name}"
                )


def _validate_classifier_projection(
    supplied: ClassifierIdentity,
    expected: ClassifierIdentity,
) -> None:
    fields = (
        "classifier_id",
        "dirty_classifier_schema_version",
        "ownership_schema_version",
        "dirty_flags",
        "semantic_dirty_flags",
        "rule_identity_sha256",
    )
    for field_name in fields:
        if getattr(supplied, field_name) != getattr(expected, field_name):
            raise WorkflowContractValidationError(
                f"classifier behavior drift field {field_name}"
            )


def _validate_stage_step_links(
    stages: tuple[StageContractValue, ...],
    steps: tuple[StepContractValue, ...],
) -> None:
    steps_by_id = {step.step_id: step for step in steps}
    for stage in stages:
        for subtask in stage.subtasks:
            step = steps_by_id[subtask.source_step_id]
            prefix = (
                f"Stage {stage.stage_id} subtask {subtask.key!r} and "
                f"Step {step.step_id}"
            )
            if subtask.contest_phase_id != step.contest_phase_id:
                raise WorkflowContractValidationError(
                    f"{prefix} contest_phase_id mismatch"
                )
            if subtask.owner_id != step.owner_id:
                raise WorkflowContractValidationError(f"{prefix} owner_id mismatch")
            for field_name in (
                "timeout_seconds",
                "hang_timeout_seconds",
                "max_attempts",
                "max_reopens",
            ):
                if getattr(subtask.budget, field_name) != getattr(
                    step.budget, field_name
                ):
                    raise WorkflowContractValidationError(
                        f"{prefix} budget.{field_name} mismatch"
                    )


def _validate_contest_phase_step_links(
    phases: tuple[ContestPhaseValue, ...],
    steps: tuple[StepContractValue, ...],
) -> None:
    phase_ids = tuple(phase.phase_id for phase in phases)
    if len(phase_ids) != len(set(phase_ids)):
        raise WorkflowContractValidationError("ContestPhase IDs must be unique")
    steps_by_id = {step.step_id: step for step in steps}
    occurrences: dict[int, list[int]] = {step_id: [] for step_id in steps_by_id}
    for phase in phases:
        for step_id in phase.steps:
            if step_id not in steps_by_id:
                raise WorkflowContractValidationError(
                    f"ContestPhase {phase.phase_id} references unknown Step {step_id}"
                )
            occurrences[step_id].append(phase.phase_id)
            if steps_by_id[step_id].contest_phase_id != phase.phase_id:
                raise WorkflowContractValidationError(
                    f"ContestPhase {phase.phase_id} and Step {step_id} phase mismatch"
                )
    for step_id, mapped_phases in occurrences.items():
        if len(mapped_phases) != 1:
            raise WorkflowContractValidationError(
                f"ContestPhase mapping must contain Step {step_id} exactly once"
            )


def _validate_classifier_step13_binding(
    stages: tuple[StageContractValue, ...],
    classifier: ClassifierIdentity,
) -> None:
    step13 = tuple(
        subtask
        for stage in stages
        for subtask in stage.subtasks
        if stage.stage_id == 8
        and subtask.key == "conditional_math_preflight"
        and subtask.source_step_id == 13
    )
    if len(step13) != 1:
        raise WorkflowContractValidationError(
            "validated classifier requires one Stage 8 Step 13 condition"
        )
    if step13[0].condition.operands != classifier.semantic_dirty_flags:
        raise WorkflowContractValidationError(
            "Step 13 condition operands must equal validated classifier semantic flags"
        )


def validate_workflow_contract_bundle(
    bundle: WorkflowContractBundle,
) -> WorkflowContractBundle:
    bundle = _validate_workflow_contract_runtime_structure(bundle)
    if bundle.schema_version != WORKFLOW_CONTRACT_BUNDLE_SCHEMA:
        raise WorkflowContractValidationError("unexpected bundle schema_version")
    if bundle.canonicalization_schema_version != CANONICAL_JSON_SCHEMA:
        raise WorkflowContractValidationError("canonicalization schema identity drift")
    if bundle.workflow_state_schema_version != SCHEMA_VERSION:
        raise WorkflowContractValidationError("workflow state schema identity drift")
    if bundle.runtime_generation != RUNTIME_GENERATION:
        raise WorkflowContractValidationError("runtime generation identity drift")
    if bundle.scheduler_generation != STAGE_SCHEDULER_GENERATION:
        raise WorkflowContractValidationError("scheduler generation identity drift")
    if bundle.stage_catalog_version != STAGE_CATALOG_VERSION:
        raise WorkflowContractValidationError("unsupported stage_catalog_version")
    if bundle.step_catalog_version != STEP_CATALOG_VERSION:
        raise WorkflowContractValidationError("step catalog identity drift")
    if bundle.contest_profile != CONTEST_PROFILE:
        raise WorkflowContractValidationError("contest profile identity drift")
    if bundle.owner_compilation.mode != OWNER_COMPILER_MODE:
        raise WorkflowContractValidationError("unsupported owner resolution mode")
    if [stage.stage_id for stage in bundle.stages] != list(range(1, 11)):
        raise WorkflowContractValidationError("Stage IDs must remain ordered 1 through 10")
    if [step.step_id for step in bundle.steps] != list(range(17)):
        raise WorkflowContractValidationError("Step IDs must remain ordered 0 through 16")

    step_ids = tuple(step.step_id for step in bundle.steps)
    if len(step_ids) != len(set(step_ids)):
        raise WorkflowContractValidationError("Step IDs must be unique")
    validated_step_ids = frozenset(step_ids)
    coordinates: list[tuple[int, str, int]] = []
    subtask_ids: list[str] = []
    stage_key_sets: list[tuple[str, ...]] = []
    for stage in bundle.stages:
        stage_keys: list[str] = []
        for subtask in stage.subtasks:
            if subtask.source_step_id not in validated_step_ids:
                raise WorkflowContractValidationError(
                    "Stage subtask source_step_id does not resolve to a unique Step"
                )
            coordinates.append(
                (stage.stage_id, subtask.key, subtask.source_step_id)
            )
            stage_keys.append(subtask.key)
            subtask_ids.append(subtask.subtask_id)
        stage_key_sets.append(tuple(stage_keys))
    if len(coordinates) != len(set(coordinates)):
        raise WorkflowContractValidationError(
            "Stage ScheduleCoordinate values must be unique"
        )
    if any(len(keys) != len(set(keys)) for keys in stage_key_sets):
        raise WorkflowContractValidationError(
            "Stage subtask keys must be unique within each Stage"
        )
    if len(subtask_ids) != len(set(subtask_ids)):
        raise WorkflowContractValidationError("Stage subtask IDs must be globally unique")

    mapped = tuple(
        subtask.checkpoint_step_id
        for stage in bundle.stages
        for subtask in stage.subtasks
        if subtask.checkpoint_step_id is not None
    )
    if sorted(mapped) != list(range(17)) or len(mapped) != len(set(mapped)):
        raise WorkflowContractValidationError(
            "every integer Step must map to exactly one Stage"
        )
    for stage in bundle.stages:
        for subtask in stage.subtasks:
            if (
                subtask.kind == "step"
                and subtask.checkpoint_step_id != subtask.source_step_id
            ):
                raise WorkflowContractValidationError(
                    "Step-backed Stage subtask source/checkpoint mapping drift"
                )

    _validate_stage_conditions(bundle.stages)
    reviewer_entry = tuple(
        subtask
        for stage in bundle.stages
        for subtask in stage.subtasks
        if subtask.key == "reviewer_entry_gate"
    )
    if (
        len(reviewer_entry) != 1
        or reviewer_entry[0].source_step_id != 8
        or reviewer_entry[0].checkpoint_step_id is not None
    ):
        raise WorkflowContractValidationError("Step 8.5 gate contract drift")
    non_human_no_checkpoint = tuple(
        subtask
        for stage in bundle.stages
        for subtask in stage.subtasks
        if subtask.checkpoint_step_id is None and subtask.kind != "human_gate"
    )
    if non_human_no_checkpoint != reviewer_entry:
        raise WorkflowContractValidationError(
            "reviewer_entry_gate must remain the unique non-Human no-checkpoint subtask"
        )
    expected_stages = _compile_stage_contract_values(
        STAGE_CONTRACTS,
        STEP_CONTRACTS,
        CONTEST_PHASES,
    )
    if bundle.stages != expected_stages:
        raise WorkflowContractValidationError(
            "Stage catalog behavior projection differs from source contracts"
        )
    _validate_stage_step_links(bundle.stages, bundle.steps)
    expected_steps = _compile_step_contract_values(
        STEP_CONTRACTS,
        STAGE_CONTRACTS,
        CONTEST_PHASES,
    )
    _validate_step_behavior_projection(bundle.steps, expected_steps)
    _validate_contest_phase_step_links(bundle.contest_phases, bundle.steps)
    expected_phases = _compile_contest_phase_values(CONTEST_PHASES)
    _validate_contest_phase_behavior_projection(
        bundle.contest_phases,
        expected_phases,
    )
    if len({rule.rule_id for rule in bundle.owner_compilation.rules}) != len(
        bundle.owner_compilation.rules
    ):
        raise WorkflowContractValidationError("owner rule IDs are not unique")
    if len({gate.gate_id for gate in bundle.gates}) != len(bundle.gates):
        raise WorkflowContractValidationError("Gate IDs are not unique")
    exact_gates = {gate.gate for gate in bundle.gates if gate.gate_family == "exact"}
    human_gates = {
        phase.human_gate
        for phase in bundle.contest_phases
        if phase.human_gate is not None
    }
    if not human_gates <= exact_gates:
        raise WorkflowContractValidationError("contest Human Gate is not inventoried")
    dynamic = tuple(gate for gate in bundle.gates if gate.gate == "dynamic")
    if (
        len(dynamic) != 1
        or dynamic[0].stage_id is not None
        or dynamic[0].source_step_id is not None
    ):
        raise WorkflowContractValidationError("dynamic Gate must retain dynamic binding")
    legacy = tuple(
        gate for gate in bundle.gates if gate.gate_family == "legacy_arbitrary"
    )
    if (
        len(legacy) != 1
        or legacy[0].compatibility_diagnostic != "UNANALYZABLE"
        or legacy[0].stage_id is not None
    ):
        raise WorkflowContractValidationError(
            "legacy arbitrary Gate family must remain explicitly unanalyzable"
        )
    expected_gates = _compile_gate_contract_values(GATE_POLICIES)
    _validate_gate_behavior_projection(bundle.gates, expected_gates)
    expected_owner = _compile_source_owner_behavior()
    _validate_owner_behavior_projection(bundle.owner_compilation, expected_owner)
    expected_classifier = _compile_classifier_projection(expected_owner)
    _validate_classifier_projection(bundle.classifier, expected_classifier)
    _validate_classifier_step13_binding(bundle.stages, bundle.classifier)
    return bundle


def workflow_contract_bytes(bundle: WorkflowContractBundle) -> bytes:
    """Return the canonical semantic identity of one compiled bundle.

    The compiled bundle deliberately carries analysis evidence as well as the
    workflow contract.  Witnesses, diagnostic prose, compiler schema version,
    and Python source locations are useful review material, but none changes
    the workflow semantics.  Keep those fields in
    :func:`workflow_contract_analysis_bytes` and exclude them from the primary
    identity so an analyzer refactor cannot fabricate contract drift.
    """

    structured = _validate_workflow_contract_runtime_structure(bundle)
    return canonical_bytes(_workflow_contract_semantic_value(structured))


def workflow_contract_sha256(bundle: WorkflowContractBundle) -> str:
    structured = _validate_workflow_contract_runtime_structure(bundle)
    return canonical_sha256(_workflow_contract_semantic_value(structured))


def workflow_contract_analysis_bytes(bundle: WorkflowContractBundle) -> bytes:
    """Return the complete bundle, including deterministic analysis evidence."""

    return canonical_bytes(_validate_workflow_contract_runtime_structure(bundle))


def workflow_contract_analysis_sha256(bundle: WorkflowContractBundle) -> str:
    return canonical_sha256(_validate_workflow_contract_runtime_structure(bundle))


def _workflow_contract_semantic_value(
    bundle: WorkflowContractBundle,
) -> dict[str, object]:
    """Project only behavior-bearing fields into the primary identity.

    ``StepContractValue.prompt`` and ``GateContractValue.producer`` are source
    locators.  Owner diagnostics contain witnesses and explanatory text, while
    the owner compiler schema describes the current analyzer implementation.
    The semantic projection retains dispatch behavior, conditions, patterns,
    ordered rules, the owner resolution mode, exact priority authorizations,
    and the explicit fail-closed compatibility diagnostic code.
    """

    return {
        "schema_version": bundle.schema_version,
        "canonicalization_schema_version": bundle.canonicalization_schema_version,
        "workflow_state_schema_version": bundle.workflow_state_schema_version,
        "runtime_generation": bundle.runtime_generation,
        "scheduler_generation": bundle.scheduler_generation,
        "stage_catalog_version": bundle.stage_catalog_version,
        "step_catalog_version": bundle.step_catalog_version,
        "contest_profile": bundle.contest_profile,
        "stages": bundle.stages,
        "steps": tuple(
            {
                "step_id": step.step_id,
                "step_contract_id": step.step_contract_id,
                "name": step.name,
                "implementation": step.implementation,
                "contest_phase_id": step.contest_phase_id,
                "owner_id": step.owner_id,
                "authority": step.authority,
                "budget": step.budget,
                "default_models": step.default_models,
            }
            for step in bundle.steps
        ),
        "gates": tuple(
            {
                "gate_id": gate.gate_id,
                "gate": gate.gate,
                "gate_family": gate.gate_family,
                "stage_id": gate.stage_id,
                "subtask_id": gate.subtask_id,
                "source_step_id": gate.source_step_id,
                "kind": gate.kind,
                "authority": gate.authority,
                "condition": gate.condition,
                "binding": gate.binding,
                "source_expression": gate.source_expression,
                "compatibility_diagnostic": gate.compatibility_diagnostic,
                "projects_pending_action": gate.projects_pending_action,
            }
            for gate in bundle.gates
        ),
        "contest_phases": bundle.contest_phases,
        "classifier": bundle.classifier,
        "owner_compilation": {
            "mode": bundle.owner_compilation.mode,
            "ownership_schema_version": (
                bundle.owner_compilation.ownership_schema_version
            ),
            "rules": bundle.owner_compilation.rules,
        },
    }
