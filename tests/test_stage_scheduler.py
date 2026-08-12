from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from factory_core.domain import (
    ExecutionResult,
    InvalidTransition,
    PrepareResult,
    RecoveryDecision,
    RecoveryDisposition,
    ValidationResult,
    WorkflowStatus,
)
from factory_core.engine import FactoryEngine
from factory_core.registry import StepDefinition, StepRegistry
from factory_core.stages import (
    STAGE_CATALOG_VERSION,
    STAGE_CONTRACTS,
    STAGE_SCHEDULER_GENERATION,
    STEP_SCHEDULER_GENERATION,
    projected_stage_cursor,
    stage_catalog_payload,
    stage_for_step,
)
from factory_core.steps.catalog import STEP_CONTRACTS, contract_for
from factory_core.steps.specialized import (
    ConditionalMathPreflightSkipStep,
    ContentFreezeGuardStep,
)
from factory_core.storage import SQLiteStateStore
from factory_core.contest import ContestPolicy
from scripts.step8_5_gate import collect_step8_5_state


@dataclass
class Lifecycle:
    calls: list[int] = field(default_factory=list)

    def prepare(self, _context):
        return PrepareResult.prepared()

    def execute(self, context):
        self.calls.append(context.attempt)
        return ExecutionResult.succeeded()

    def validate(self, _context):
        return ValidationResult.valid()

    def recover(self, _context, _error):
        return RecoveryDecision(RecoveryDisposition.RETRY)


class ReviewerGateLifecycle(Lifecycle):
    def execute(self, context):
        self.calls.append(context.attempt)
        for name in ("reviewer_entry_map.md", "anchor_figure_plan.md"):
            (context.project_dir / name).write_text("# ready\n", encoding="utf-8")
        (context.project_dir / "entry_gate.md").write_text(
            "# gate\n\nVERDICT: PASS\n", encoding="utf-8"
        )
        return ExecutionResult.succeeded()


def stage_registry(*, overrides=None, gate=None, skip=None, content_guard=None):
    overrides = overrides or {}
    registry = StepRegistry()
    lifecycles = {}
    for contract in STEP_CONTRACTS:
        lifecycle = overrides.get(contract.id) or Lifecycle()
        lifecycles[contract.id] = lifecycle
        registry.register(
            StepDefinition(
                contract.id,
                contract.name,
                contract.timeout_seconds,
                contract.max_attempts,
                max_reopens=contract.max_reopens,
                step=lifecycle,
            )
        )
    gate = gate or ReviewerGateLifecycle()
    registry.register_stage_subtask(
        "reviewer_entry_gate",
        StepDefinition(8, "reviewer_entry_gate", 7_200, 5, max_reopens=0, step=gate),
    )
    registry.register_stage_subtask(
        "conditional_math_preflight_skip",
        StepDefinition(13, "conditional_math_preflight_skip", 10_800, 1, max_reopens=0, step=skip or Lifecycle()),
    )
    registry.register_stage_subtask(
        "content_freeze_guard",
        StepDefinition(
            16,
            "content_freeze_guard",
            21_600,
            1,
            max_reopens=0,
            step=content_guard or Lifecycle(),
        ),
    )
    return registry, lifecycles


def trust_seeded_reviewer_gate(store, project):
    for name in ("reviewer_entry_map.md", "anchor_figure_plan.md"):
        (project / name).write_text("# ready\n", encoding="utf-8")
    (project / "entry_gate.md").write_text(
        "# gate\n\nVERDICT: PASS\n", encoding="utf-8"
    )
    current = collect_step8_5_state(project)
    state = store.load()
    return store.transition(
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
                "validation": {"step8_5": current},
                "result": {},
            },
        },
    )


def test_stage_catalog_covers_every_step_exactly_once_and_preserves_budgets():
    payload = stage_catalog_payload()

    assert len(STAGE_CONTRACTS) == 10
    mapped = [
        subtask.checkpoint_step_id
        for stage in STAGE_CONTRACTS
        for subtask in stage.subtasks
        if subtask.checkpoint_step_id is not None
    ]
    assert sorted(mapped) == list(range(17))
    assert len(mapped) == len(set(mapped))
    assert stage_for_step(13).id == 8
    reviewer_gate = next(
        subtask
        for stage in STAGE_CONTRACTS
        for subtask in stage.subtasks
        if subtask.key == "reviewer_entry_gate"
    )
    assert reviewer_gate.checkpoint_step_id is None
    assert reviewer_gate.source_step_id == 8
    for stage in payload["stages"]:
        for subtask in stage["subtasks"]:
            contract = contract_for(subtask["source_step_id"])
            assert subtask["step_budget"] == {
                "timeout_seconds": contract.timeout_seconds,
                "hang_timeout_seconds": contract.hang_timeout_seconds,
                "max_attempts": contract.max_attempts,
                "max_reopens": contract.max_reopens,
            }


def test_stage_scheduler_persists_all_subtask_checkpoints_and_atomic_cursor(tmp_path):
    registry, _lifecycles = stage_registry()
    store = SQLiteStateStore(tmp_path)
    store.initialize(
        project_id="demo",
        project_type="modeling",
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )

    state = FactoryEngine(tmp_path, store=store, registry=registry).run()

    assert state.status is WorkflowStatus.COMPLETED
    assert state.last_completed_step == 16
    assert state.last_completed_stage == 10
    assert len(store.stage_checkpoints()) == 19
    selected = [event for event in store.events() if event.type == "STAGE_SUBTASK_SELECTED"]
    assert selected
    for event in selected:
        assert event.step == event.payload["source_step"]
        assert event.payload["stage_catalog_version"] == STAGE_CATALOG_VERSION


def test_reviewer_entry_gate_is_resumable_without_advancing_step_cursor(tmp_path):
    gate = ReviewerGateLifecycle()
    registry, _lifecycles = stage_registry(gate=gate)
    store = SQLiteStateStore(tmp_path)
    store.initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=7,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    engine = FactoryEngine(tmp_path, store=store, registry=registry)

    after_step8 = engine.run(max_steps=1)

    assert after_step8.status is WorkflowStatus.READY
    assert after_step8.last_completed_step == 8
    assert after_step8.last_completed_stage == 5
    assert after_step8.active_stage == 6
    assert after_step8.active_subtask == "reviewer_entry_gate"
    assert after_step8.source_step_id == 8
    assert gate.calls == []

    after_gate = engine.run(max_steps=1)
    assert gate.calls == [1]
    assert after_gate.last_completed_step == 8
    assert after_gate.last_completed_stage == 6
    assert after_gate.active_stage == 7
    assert after_gate.source_step_id == 9


def test_conditional_step13_skips_only_when_no_semantic_dirty_flag(tmp_path):
    skip = Lifecycle()
    registry, lifecycles = stage_registry(skip=skip)
    store = SQLiteStateStore(tmp_path)
    store.initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=12,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    trust_seeded_reviewer_gate(store, tmp_path)

    state = FactoryEngine(tmp_path, store=store, registry=registry).run(max_steps=1)

    assert state.last_completed_step == 13
    assert skip.calls == [1]
    assert lifecycles[13].calls == []
    checkpoint = store.stage_checkpoints()[-1]
    assert checkpoint["subtask"] == "conditional_math_preflight"


def test_conditional_step13_runs_real_contract_for_math_dirty(tmp_path):
    skip = Lifecycle()
    registry, lifecycles = stage_registry(skip=skip)
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=12,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    state = trust_seeded_reviewer_gate(store, tmp_path)
    store.transition(
        expected_revision=state.revision,
        event_type="MATH_CHANGED_FOR_TEST",
        changes={},
        dirty_changes=[
            {
                "flag": "MATH_DIRTY",
                "owner_stage": 8,
                "cause_artifact": "demo_paper.tex",
                "baseline_fingerprint": "a" * 64,
                "current_fingerprint": "b" * 64,
                "classifier_contract_sha256": "c" * 64,
            }
        ],
    )

    completed = FactoryEngine(tmp_path, store=store, registry=registry).run(max_steps=1)

    assert completed.last_completed_step == 13
    assert lifecycles[13].calls == [1]
    assert skip.calls == []
    assert store.dirty_flags() == []
    assert store.dirty_clear_receipts()[0]["flag"] == "MATH_DIRTY"


class RewriteMathOnce(Lifecycle):
    def __init__(self, project):
        super().__init__()
        self.project = project

    def execute(self, context):
        self.calls.append(context.attempt)
        if len(self.calls) == 1:
            paper = self.project / "demo_paper.tex"
            paper.write_text(
                paper.read_text(encoding="utf-8").replace("x=1", "x=2"),
                encoding="utf-8",
            )
        return ExecutionResult.succeeded()


class RewriteProseOnce(Lifecycle):
    def execute(self, context):
        self.calls.append(context.attempt)
        paper = context.project_dir / "demo_paper.tex"
        paper.write_text(
            paper.read_text(encoding="utf-8").replace("alpha", "beta"),
            encoding="utf-8",
        )
        return ExecutionResult.succeeded()


class DeleteProtectedIssue(Lifecycle):
    def execute(self, context):
        self.calls.append(context.attempt)
        (context.project_dir / "audit_issue_ledger.md").write_text(
            "| Issue ID | Severity | Status | Notes |\n"
            "|---|---|---|---|\n",
            encoding="utf-8",
        )
        return ExecutionResult.succeeded()


def test_stage8_fails_closed_when_revision_deletes_protected_issue(tmp_path):
    ledger = tmp_path / "audit_issue_ledger.md"
    ledger.write_text(
        "| Issue ID | Severity | Status | Notes |\n"
        "|---|---|---|---|\n"
        "| P-1 | MAJOR | OPEN | PROTECTED: preserve mechanism |\n",
        encoding="utf-8",
    )
    deleter = DeleteProtectedIssue()
    registry, _lifecycles = stage_registry(overrides={12: deleter})
    store = SQLiteStateStore(tmp_path)
    store.initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=11,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    trust_seeded_reviewer_gate(store, tmp_path)

    failed = FactoryEngine(tmp_path, store=store, registry=registry).run()

    assert failed.status is WorkflowStatus.FAILED
    assert store.events()[-1].payload["error_class"] == (
        "PERMANENT_PROTECTED_ITEM_DELETED"
    )
    assert store.events()[-1].payload["protected_items"] == [
        "@protected:audit_issue_ledger.md:P-1"
    ]


def test_final_prose_math_change_reopens_stage8_and_invalidates_downstream(tmp_path):
    paper = tmp_path / "demo_paper.tex"
    paper.write_text("\\begin{document}\n$x=1$\n\\end{document}\n", encoding="utf-8")
    rewrite = RewriteMathOnce(tmp_path)
    registry, _lifecycles = stage_registry(overrides={14: rewrite})
    store = SQLiteStateStore(tmp_path)
    store.initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=13,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    trust_seeded_reviewer_gate(store, tmp_path)

    state = FactoryEngine(tmp_path, store=store, registry=registry).run(max_steps=1)

    reopen = next(
        event for event in store.events() if event.type == "STAGE_SEMANTIC_REOPENED"
    )
    assert reopen.payload["stage"] == 9
    assert reopen.payload["resume_after_step"] == 10
    assert "MATH_DIRTY" in reopen.payload["dirty_flags"]
    assert state.last_completed_step == 11
    assert state.active_stage == 8
    assert all(
        checkpoint["completed_step_id"] in {None} or checkpoint["completed_step_id"] <= 11
        for checkpoint in store.stage_checkpoints()
    )


def test_final_prose_change_does_not_stale_valid_conditional_skip(tmp_path):
    paper = tmp_path / "demo_paper.tex"
    paper.write_text(
        "\\begin{document}\n$x=1$ alpha\n\\end{document}\n", encoding="utf-8"
    )
    prose = RewriteProseOnce()
    registry, _lifecycles = stage_registry(
        overrides={14: prose},
        skip=ConditionalMathPreflightSkipStep(
            factory_root=Path(__file__).resolve().parents[1]
        ),
    )
    store = SQLiteStateStore(tmp_path)
    store.initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=12,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    trust_seeded_reviewer_gate(store, tmp_path)
    engine = FactoryEngine(tmp_path, store=store, registry=registry)

    skipped = engine.run(max_steps=1)
    after_prose = engine.run(max_steps=1)
    completed = engine.run(max_steps=1)

    assert skipped.last_completed_step == 13
    assert after_prose.last_completed_step == 14
    assert completed.last_completed_step == 15
    assert prose.calls == [1]
    assert not any(
        event.type == "STAGE_CHECKPOINT_INVALIDATED" for event in store.events()
    )


def test_unmigrated_step_projection_uses_pending_action_and_status(tmp_path):
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(
        project_id="old-native",
        project_type="modeling",
        last_completed_step=2,
        active_step=3,
        status=WorkflowStatus.AWAITING_SELECTION,
        pending_action={"type": "step3_selection", "gate": "step3"},
        scheduler_generation=STEP_SCHEDULER_GENERATION,
    )

    cursor = projected_stage_cursor(state)

    assert cursor["active_stage"] == 2
    assert cursor["active_subtask"] == "method_selection"
    assert cursor["source_step_id"] == 3


def test_stage_scheduler_rejects_wrong_catalog_version(tmp_path):
    registry, _lifecycles = stage_registry()
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(
        project_id="demo",
        project_type="modeling",
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    store.transition(
        expected_revision=state.revision,
        event_type="CORRUPT_FOR_TEST",
        changes={"stage_catalog_version": "unknown"},
    )

    with pytest.raises(InvalidTransition, match="unsupported Stage catalog"):
        FactoryEngine(tmp_path, store=store, registry=registry).run()


def test_content_freeze_is_a_persistent_guard_before_delivery(tmp_path):
    registry, _lifecycles = stage_registry(content_guard=ContentFreezeGuardStep())
    store = SQLiteStateStore(tmp_path)
    policy = ContestPolicy.default(started_at=store.now_epoch())
    store.initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=15,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
        contest_policy=policy.to_dict(),
    )
    trust_seeded_reviewer_gate(store, tmp_path)
    engine = FactoryEngine(tmp_path, store=store, registry=registry)

    awaiting = engine.run()

    assert awaiting.status is WorkflowStatus.AWAITING_SELECTION
    assert awaiting.pending_action["gate"] == "content_freeze"
    assert awaiting.active_stage == 10
    assert awaiting.active_subtask == "content_freeze_guard"
    assert awaiting.source_step_id == 16
    assert awaiting.last_completed_stage == 9

    store.record_decision("content_freeze", {"selected_option_id": "approve"})
    resolved = engine.resolve_action(
        {"gate": "content_freeze", "selected_option_id": "approve"},
        expected_revision=awaiting.revision,
    )
    guarded = engine.run(max_steps=1)

    assert resolved.status is WorkflowStatus.READY
    assert guarded.last_completed_step == 15
    assert guarded.last_completed_stage == 9
    assert guarded.active_subtask == "delivery"
