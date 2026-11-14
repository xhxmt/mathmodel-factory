"""Stage-scheduler fixtures for tests of generic engine behaviour.

The engine only executes Stage-scheduler projects.  Tests that exercise one
Step's retries, failure classification, deadlines or leases register that Step
under test and let every other Step in the catalog succeed, start the project
just before the Step under test, and usually stop with ``run(max_steps=1)``.
"""
from __future__ import annotations

from factory_core.domain import (
    ExecutionResult,
    PrepareResult,
    RecoveryDecision,
    RecoveryDisposition,
    ValidationResult,
)
from factory_core.registry import StepDefinition, StepRegistry
from factory_core.stages import STAGE_SCHEDULER_GENERATION, stage_for_step
from factory_core.steps.catalog import STEP_CONTRACTS, contract_for


class _Succeeds:
    def prepare(self, _context):
        return PrepareResult.prepared()

    def execute(self, _context):
        return ExecutionResult.succeeded()

    def validate(self, _context):
        return ValidationResult.valid()

    def recover(self, _context, _error):
        return RecoveryDecision(RecoveryDisposition.RETRY)


class _ReviewerGateSucceeds(_Succeeds):
    """The Step 8.5 gate is fingerprinted on its artifacts, so it writes them."""

    def execute(self, context):
        _write_reviewer_entry_artifacts(context.project_dir)
        return ExecutionResult.succeeded()


def stage_registry_with(*definitions: StepDefinition) -> StepRegistry:
    """The full Stage catalog, with ``definitions`` replacing their Steps."""

    given = {definition.id: definition for definition in definitions}
    registry = StepRegistry()
    for contract in STEP_CONTRACTS:
        registry.register(
            given.get(contract.id)
            or StepDefinition(
                contract.id,
                contract.name,
                contract.timeout_seconds,
                contract.max_attempts,
                max_reopens=contract.max_reopens,
                step=_Succeeds(),
            )
        )
    for key, step_id, timeout, lifecycle in (
        ("reviewer_entry_gate", 8, 7_200, _ReviewerGateSucceeds()),
        ("conditional_math_preflight_skip", 13, 10_800, _Succeeds()),
        ("content_freeze_guard", 16, 21_600, _Succeeds()),
    ):
        registry.register_stage_subtask(
            key, StepDefinition(step_id, key, timeout, 1, max_reopens=0, step=lifecycle)
        )
    return registry


def _write_reviewer_entry_artifacts(project) -> None:
    for name in ("reviewer_entry_map.md", "anchor_figure_plan.md"):
        (project / name).write_text("# ready\n", encoding="utf-8")
    (project / "entry_gate.md").write_text("# gate\n\nVERDICT: PASS\n", encoding="utf-8")


def initialize_stage_project(store, *, last_completed_step: int = 0, **kwargs):
    """A Stage project whose next subtask is Step ``last_completed_step + 1``.

    A project seeded past Step 8 also gets the reviewer-entry artifacts its
    seeded gate checkpoint is fingerprinted on; otherwise the scheduler would
    invalidate that checkpoint and rewind to Step 8 before the Step under test.
    """

    state = store.initialize(
        project_id=kwargs.pop("project_id", "demo"),
        project_type=kwargs.pop("project_type", "modeling"),
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
        last_completed_step=last_completed_step,
        **kwargs,
    )
    if last_completed_step < 8:
        return state
    from scripts.step8_5_gate import collect_step8_5_state

    _write_reviewer_entry_artifacts(store.project_dir)
    state = store.transition(
        expected_revision=state.revision,
        event_type="TRUST_REVIEWER_GATE_FOR_TEST",
        changes={},
        stage_checkpoint={
            "stage_id": 6,
            "subtask": "reviewer_entry_gate",
            "source_step_id": 8,
            "completed_step_id": None,
            "input_fingerprint": "test",
            "output_fingerprint": "test",
            "receipt": {
                "schema_version": "factory-stage-checkpoint-v1",
                "validation": {"step8_5": collect_step8_5_state(store.project_dir)},
                "result": {},
            },
        },
    )
    # Seeded Stage 9 checkpoints carry no dirty-classifier identity, which the
    # scheduler reads as a changed classifier and rewinds to Step 13.
    from factory_core.engine import FactoryEngine

    classifier = FactoryEngine._classifier_sha(state)
    for checkpoint in store.stage_checkpoints():
        if checkpoint["stage_id"] != 9:
            continue
        state = store.transition(
            expected_revision=state.revision,
            event_type="BIND_SEEDED_CLASSIFIER_FOR_TEST",
            changes={},
            stage_checkpoint={
                "stage_id": 9,
                "subtask": checkpoint["subtask"],
                "source_step_id": checkpoint["source_step_id"],
                "completed_step_id": checkpoint["completed_step_id"],
                "input_fingerprint": "test",
                "output_fingerprint": "test",
                "receipt": {
                    "schema_version": "factory-stage-checkpoint-v1",
                    "classifier_contract_sha256": classifier,
                    "validation": {},
                    "result": {},
                },
            },
        )
    return state


def active_step_cursor(step_id: int) -> dict:
    """The atomic Stage cursor fields for an attempt of ``step_id``."""

    return {
        "active_step": step_id,
        "active_stage": stage_for_step(step_id).id,
        "active_subtask": contract_for(step_id).name,
        "source_step_id": step_id,
    }
