from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory_core.artifact_ownership import artifact_ownership
from factory_core.domain import (
    ExecutionResult,
    InvalidTransition,
    PrepareResult,
    RecoveryDecision,
    RecoveryDisposition,
    ValidationResult,
    WorkflowStatus,
)
from factory_core.dirty import (
    capture_artifact_manifest,
    classifier_contract_sha256,
    manifest_fingerprint,
    solver_receipt_job_id,
)
from factory_core.engine import FactoryEngine
from factory_core.registry import StepDefinition, StepRegistry
from factory_core.stages import (
    STAGE_CATALOG_VERSION,
    STAGE_CONTRACTS,
    STAGE_SCHEDULER_GENERATION,
    STEP_SCHEDULER_GENERATION,
    projected_stage_cursor,
    resume_after_step_for_stage,
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


class RecoverCompleteLifecycle(Lifecycle):
    def __init__(self, *, evidence=("recovered-artifact.md",), **metadata):
        super().__init__()
        self.evidence = tuple(evidence)
        self.metadata = metadata
        self.recover_calls = 0

    def recover(self, _context, _error):
        self.recover_calls += 1
        return RecoveryDecision(
            RecoveryDisposition.COMPLETE,
            evidence=self.evidence,
            metadata=self.metadata,
        )


class RecoverMutateThenFailLifecycle(Lifecycle):
    def recover(self, context, _error):
        problem = context.project_dir / "problem" / "problem_brief.md"
        problem.parent.mkdir(parents=True, exist_ok=True)
        problem.write_text("mutated during failed recovery\n", encoding="utf-8")
        return RecoveryDecision(RecoveryDisposition.FAIL, reason="cannot recover")


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


def interrupt_stage_task(store, project, *, stage, subtask, source_step):
    manifest = capture_artifact_manifest(project)
    state = store.load()
    return store.transition(
        expected_revision=state.revision,
        event_type="INTERRUPTED_AFTER_VALID_OUTPUT_FOR_TEST",
        changes={
            "status": WorkflowStatus.INTERRUPTED,
            "active_step": source_step,
            "active_stage": stage,
            "active_subtask": subtask,
            "source_step_id": source_step,
            "attempt": 1,
        },
        subtask_baseline={
            "stage_id": stage,
            "subtask": subtask,
            "source_step_id": source_step,
            "input_fingerprint": manifest_fingerprint(manifest),
            "manifest": manifest,
        },
        event_step=source_step,
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
    assert [resume_after_step_for_stage(stage) for stage in range(1, 11)] == [
        -1,
        1,
        3,
        4,
        5,
        7,
        8,
        10,
        13,
        15,
    ]
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


def test_stage_recovery_complete_promotes_checkpoint(tmp_path):
    artifact = tmp_path / "recovered-artifact.md"
    artifact.write_text("valid\n", encoding="utf-8")
    lifecycle = RecoverCompleteLifecycle(validation_marker="preserved")
    registry, _lifecycles = stage_registry(overrides={4: lifecycle})
    store = SQLiteStateStore(tmp_path)
    store.initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=3,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    interrupt_stage_task(
        store,
        tmp_path,
        stage=3,
        subtask="model_construction",
        source_step=4,
    )

    recovered = FactoryEngine(
        tmp_path, store=store, registry=registry
    ).recover()

    checkpoint = next(
        item
        for item in store.stage_checkpoints()
        if item["stage_id"] == 3 and item["subtask"] == "model_construction"
    )
    assert recovered.status is WorkflowStatus.READY
    assert recovered.last_completed_step == 4
    assert recovered.last_completed_stage == 3
    assert recovered.active_stage == 4
    assert checkpoint["receipt"]["validation"]["validation_marker"] == "preserved"
    assert checkpoint["receipt"]["result"]["recovered"] is True
    assert lifecycle.calls == []
    assert lifecycle.recover_calls == 1
    assert len(
        [
            event
            for event in store.events()
            if event.type == "STEP_SUCCEEDED"
            and event.payload.get("subtask") == "model_construction"
        ]
    ) == 1


def test_failed_stage_recovery_persists_manifest_delta(tmp_path):
    lifecycle = RecoverMutateThenFailLifecycle()
    registry, _lifecycles = stage_registry(overrides={4: lifecycle})
    store = SQLiteStateStore(tmp_path)
    store.initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=3,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    interrupt_stage_task(
        store,
        tmp_path,
        stage=3,
        subtask="model_construction",
        source_step=4,
    )

    recovered = FactoryEngine(
        tmp_path, store=store, registry=registry
    ).recover()

    assert recovered.status is WorkflowStatus.FAILED
    assert {(item["flag"], item["owner_stage"]) for item in store.dirty_flags()} == {
        ("MODEL_DIRTY", 1)
    }
    assert store.events()[-1].type == "RECOVERY_DECIDED"


def test_reviewer_entry_gate_complete_recovery(tmp_path):
    for name in ("reviewer_entry_map.md", "anchor_figure_plan.md"):
        (tmp_path / name).write_text("# ready\n", encoding="utf-8")
    (tmp_path / "entry_gate.md").write_text(
        "# gate\n\nVERDICT: PASS\n", encoding="utf-8"
    )
    lifecycle = RecoverCompleteLifecycle(evidence=("entry_gate.md",))
    registry, _lifecycles = stage_registry(gate=lifecycle)
    store = SQLiteStateStore(tmp_path)
    store.initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=8,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    interrupt_stage_task(
        store,
        tmp_path,
        stage=6,
        subtask="reviewer_entry_gate",
        source_step=8,
    )

    recovered = FactoryEngine(
        tmp_path, store=store, registry=registry
    ).recover()

    checkpoints = [
        item
        for item in store.stage_checkpoints()
        if item["stage_id"] == 6 and item["subtask"] == "reviewer_entry_gate"
    ]
    assert len(checkpoints) == 1
    assert recovered.last_completed_step == 8
    assert recovered.last_completed_stage == 6
    assert recovered.active_stage == 7
    assert recovered.active_step == 9
    assert checkpoints[0]["receipt"]["validation"]["step8_5"]["ready"] is True
    assert lifecycle.calls == []


def test_content_freeze_guard_complete_recovery(tmp_path):
    lifecycle = RecoverCompleteLifecycle(
        evidence=("recovered-artifact.md",),
        request_id="request-1",
        decision_id="decision-1",
    )
    (tmp_path / "recovered-artifact.md").write_text("approved\n", encoding="utf-8")
    registry, _lifecycles = stage_registry(content_guard=lifecycle)
    store = SQLiteStateStore(tmp_path)
    store.initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=15,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    interrupt_stage_task(
        store,
        tmp_path,
        stage=10,
        subtask="content_freeze_guard",
        source_step=16,
    )

    recovered = FactoryEngine(
        tmp_path, store=store, registry=registry
    ).recover()

    checkpoint = next(
        item
        for item in store.stage_checkpoints()
        if item["stage_id"] == 10 and item["subtask"] == "content_freeze_guard"
    )
    assert recovered.status is WorkflowStatus.READY
    assert recovered.last_completed_step == 15
    assert recovered.last_completed_stage == 9
    assert recovered.active_stage == 10
    assert recovered.active_subtask == "delivery"
    assert checkpoint["receipt"]["validation"]["request_id"] == "request-1"
    assert checkpoint["receipt"]["result"]["decision_id"] == "decision-1"
    assert lifecycle.calls == []


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
                "classifier_contract_sha256": classifier_contract_sha256(),
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


class RewriteProblemPlanOnce(Lifecycle):
    def __init__(self, *, also_results=False):
        super().__init__()
        self.also_results = also_results

    def execute(self, context):
        self.calls.append(context.attempt)
        if len(self.calls) == 1:
            plan = context.project_dir / "problem" / "problem_plan.json"
            plan.write_text('{"revision": 2}\n', encoding="utf-8")
            if self.also_results:
                results = context.project_dir / "results"
                results.mkdir(exist_ok=True)
                (results / "canonical_results.json").write_text(
                    '{"value": 2}\n', encoding="utf-8"
                )
        return ExecutionResult.succeeded()


class RewriteOwnedArtifactOnce(Lifecycle):
    def __init__(self, relative):
        super().__init__()
        self.relative = relative

    def execute(self, context):
        self.calls.append(context.attempt)
        path = context.project_dir / self.relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("changed by late Stage\n", encoding="utf-8")
        return ExecutionResult.succeeded()


class MutateCanonicalThenFail(Lifecycle):
    def execute(self, context):
        self.calls.append(context.attempt)
        path = context.project_dir / "results" / "canonical_results.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"value": 2}\n', encoding="utf-8")
        return ExecutionResult.failed("PERMANENT_TEST_FAILURE")

    def validate(self, _context):
        return ValidationResult.invalid("intentional failed mutation")


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


def _late_problem_plan_reopen(tmp_path, *, also_results=False):
    problem = tmp_path / "problem"
    problem.mkdir()
    (problem / "problem_plan.json").write_text(
        '{"revision": 1}\n', encoding="utf-8"
    )
    if also_results:
        results = tmp_path / "results"
        results.mkdir()
        (results / "canonical_results.json").write_text(
            '{"value": 1}\n', encoding="utf-8"
        )
    rewrite = RewriteProblemPlanOnce(also_results=also_results)
    registry, _lifecycles = stage_registry(overrides={14: rewrite})
    store = SQLiteStateStore(tmp_path)
    store.initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=13,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    trust_seeded_reviewer_gate(store, tmp_path)
    engine = FactoryEngine(tmp_path, store=store, registry=registry)
    state = engine.run(max_steps=1)
    reopen = next(
        event for event in store.events() if event.type == "STAGE_SEMANTIC_REOPENED"
    )
    return engine, store, state, reopen


def test_late_problem_plan_change_reopens_stage_1(tmp_path):
    _engine, _store, state, reopen = _late_problem_plan_reopen(tmp_path)

    assert reopen.payload["semantic_owner_stage"] == 1
    assert reopen.payload["resume_after_step"] == -1
    assert state.last_completed_step == 0
    assert state.last_completed_stage == 0
    assert state.active_stage == 1
    assert state.active_subtask == "research_and_viability"


def test_semantic_reopen_uses_earliest_dirty_owner(tmp_path):
    _engine, store, _state, reopen = _late_problem_plan_reopen(
        tmp_path, also_results=True
    )

    assert reopen.payload["dirty_owner_stages"] == [1, 4]
    assert reopen.payload["semantic_owner_stage"] == 1
    assert reopen.payload["resume_after_step"] == -1
    assert reopen.payload["classifier_contract_sha256"] == classifier_contract_sha256()
    assert {(item["flag"], item["owner_stage"]) for item in store.dirty_flags()} == {
        ("MODEL_DIRTY", 1),
        ("RESULT_DIRTY", 4),
    }


def test_semantic_reopen_budget_ignores_legacy_events_before_current_rebase():
    classifier = classifier_contract_sha256()
    events = [
        SimpleNamespace(
            revision=1,
            type="STAGE_SEMANTIC_REOPENED",
            payload={"stage": 4},
        ),
        SimpleNamespace(
            revision=2,
            type="STAGE_SEMANTIC_REOPENED",
            payload={"stage": 4},
        ),
        SimpleNamespace(
            revision=3,
            type="DIRTY_CLASSIFIER_REBASED",
            payload={"new_classifier_sha256": classifier},
        ),
    ]
    engine = object.__new__(FactoryEngine)
    engine.store = SimpleNamespace(events=lambda: events)

    assert engine._stage_semantic_reopen_allowed(4)

    for revision in (4, 5):
        events.append(
            SimpleNamespace(
                revision=revision,
                type="STAGE_SEMANTIC_REOPENED",
                payload={
                    "stage": 4,
                    "classifier_contract_sha256": classifier,
                },
            )
        )

    assert not engine._stage_semantic_reopen_allowed(4)


def test_stage_1_checkpoint_clears_problem_plan_dirty(tmp_path):
    engine, store, _state, _reopen = _late_problem_plan_reopen(tmp_path)

    completed_stage_1 = engine.run(max_steps=1)

    assert completed_stage_1.last_completed_step == 1
    assert completed_stage_1.last_completed_stage == 1
    assert store.dirty_flags() == []
    receipt = store.dirty_clear_receipts()[-1]
    assert receipt["flag"] == "MODEL_DIRTY"
    assert receipt["owner_stage"] == 1
    assert receipt["receipt"]["success_receipt"]["stage"] == 1


def test_semantic_reopen_invalidates_all_downstream_checkpoints(tmp_path):
    _engine, store, _state, _reopen = _late_problem_plan_reopen(tmp_path)

    checkpoints = store.stage_checkpoints()
    assert [(item["stage_id"], item["subtask"]) for item in checkpoints] == [
        (1, "problem_setup")
    ]


def _late_owned_artifact_reopen(tmp_path, relative, expected_owner, expected_resume):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("original\n", encoding="utf-8")
    rewrite = RewriteOwnedArtifactOnce(relative)
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

    assert reopen.payload["semantic_owner_stage"] == expected_owner
    assert reopen.payload["resume_after_step"] == expected_resume
    assert state.active_stage == expected_owner


def test_late_problem_brief_change_reopens_stage1(tmp_path):
    _late_owned_artifact_reopen(
        tmp_path, "problem/problem_brief.md", expected_owner=1, expected_resume=-1
    )


def test_late_viable_streams_change_reopens_stage1(tmp_path):
    _late_owned_artifact_reopen(
        tmp_path, "viable_streams.md", expected_owner=1, expected_resume=-1
    )


def test_late_method_decision_change_reopens_stage2(tmp_path):
    _late_owned_artifact_reopen(
        tmp_path, "method_decision.md", expected_owner=2, expected_resume=1
    )


def test_late_chosen_method_change_reopens_stage2(tmp_path):
    _late_owned_artifact_reopen(
        tmp_path, "chosen_method.md", expected_owner=2, expected_resume=1
    )


@pytest.mark.parametrize(
    ("relative", "kind", "owner", "final_input", "submission_member"),
    [
        (
            "models/m2_airy/08_generate_derived.log",
            "solver_runtime_log",
            5,
            False,
            False,
        ),
        ("models/m2_airy/05_sensitivity.py", "model_validation", 5, True, True),
        (
            "models/m1_fringe/05_peak_rule_sensitivity.py",
            "model_validation",
            5,
            True,
            True,
        ),
        ("models/m2_airy/06_figures.py", "visualization", 6, True, True),
        (
            "figures/sensitivity_tornado_thickness.pdf",
            "visualization",
            6,
            True,
            True,
        ),
        (
            "models/generate_derived.py",
            "format_generation",
            9,
            True,
            True,
        ),
        (
            "results/derived_artifacts.json",
            "format_manifest",
            9,
            True,
            True,
        ),
        (
            "number_verification.md",
            "revision_validation",
            8,
            True,
            True,
        ),
        (
            "models/m2_airy/07_bootstrap_convergence.py",
            "revision_validation",
            8,
            True,
            True,
        ),
        (
            "models/m2_airy/07_bootstrap_convergence.log",
            "revision_validation_log",
            8,
            False,
            False,
        ),
        (
            "models/m2_airy/08_block_length_extended.py",
            "revision_validation",
            8,
            True,
            True,
        ),
        (
            "results/problem3/bootstrap_convergence_sic.json",
            "revision_validation",
            8,
            True,
            True,
        ),
        (
            "results/problem3/block_length_extended.json",
            "revision_validation",
            8,
            True,
            True,
        ),
        ("models/m1_fringe/02_model.py.stub", "solver_scaffold", 4, True, True),
        ("scripts/step5/m2_sic_pso.py", "solver_implementation", 4, True, True),
        (
            "assumption_ledger.md",
            "revision_validation",
            8,
            True,
            True,
        ),
        ("models/m2_airy/02_model.py", "model_contract", 3, True, True),
        ("scripts/verify_spec_impl.py", "model_implementation", 3, True, True),
    ],
)
def test_solver_artifact_rules_precede_stage3_directory_fallbacks(
    relative, kind, owner, final_input, submission_member
):
    ownership = artifact_ownership(relative)

    assert ownership is not None
    assert ownership.owner_stage == owner
    assert ownership.semantic_domain == kind
    expected_dirty = {
        3: "MODEL_DIRTY",
        4: "RESULT_DIRTY",
        5: "RESULT_DIRTY",
        6: "VISUAL_DIRTY",
        7: "MATH_DIRTY",
        8: "MATH_DIRTY",
        9: "FORMAT_DIRTY",
    }[owner]
    assert ownership.dirty_flag == expected_dirty
    assert ownership.final_input is final_input
    assert ownership.submission_member is submission_member


def test_solver_receipt_dirty_owner_follows_durable_job_owner(tmp_path):
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(
        project_id="receipt-owner",
        project_type="modeling",
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    job_id = "local_python_sensitivity"
    store.create_solver_job(
        expected_revision=state.revision,
        record={
            "job_id": job_id,
            "owner_stage": 5,
            "owner_subtask": "sensitivity",
            "backend": "local",
            "runtime": "python",
            "script": "models/m2/05_sensitivity.py",
            "workdir": "models/m2",
            "argv": [],
            "max_time_seconds": 60,
            "status": "completed",
            "result_refs": {},
        },
    )
    receipt = f".factory/solver_receipts/{job_id}.completed.json"
    engine = FactoryEngine(tmp_path, store=store)
    assert solver_receipt_job_id(receipt) == job_id
    assert solver_receipt_job_id("results/problem1/values.json") is None
    assert engine._solver_receipt_owner_stage(receipt) == 5
    assert (
        engine._solver_receipt_owner_stage(
            ".factory/solver_receipts/unknown.completed.json"
        )
        is None
    )


def test_late_sensitivity_report_change_reopens_stage5(tmp_path):
    _late_owned_artifact_reopen(
        tmp_path, "sensitivity_report.md", expected_owner=5, expected_resume=5
    )


def test_late_number_verification_change_reopens_stage8(tmp_path):
    _late_owned_artifact_reopen(
        tmp_path, "number_verification.md", expected_owner=8, expected_resume=10
    )


def test_late_assumption_ledger_revision_reopens_stage8(tmp_path):
    _late_owned_artifact_reopen(
        tmp_path, "assumption_ledger.md", expected_owner=8, expected_resume=10
    )


def test_late_revision_bootstrap_evidence_change_reopens_stage8(tmp_path):
    _late_owned_artifact_reopen(
        tmp_path,
        "models/m2_airy/07_bootstrap_convergence.py",
        expected_owner=8,
        expected_resume=10,
    )


def test_late_revision_block_length_evidence_change_reopens_stage8(tmp_path):
    _late_owned_artifact_reopen(
        tmp_path,
        "models/m2_airy/08_block_length_extended.py",
        expected_owner=8,
        expected_resume=10,
    )


def test_late_shared_figure_renderer_change_reopens_stage6(tmp_path):
    _late_owned_artifact_reopen(
        tmp_path, "models/m2_airy/06_figures.py", expected_owner=6, expected_resume=7
    )


def test_late_entry_gate_change_reopens_stage6(tmp_path):
    _late_owned_artifact_reopen(
        tmp_path, "entry_gate.md", expected_owner=6, expected_resume=7
    )


def test_failed_stage_mutation_is_persisted_before_control_mode_change(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / "canonical_results.json").write_text(
        '{"value": 1}\n', encoding="utf-8"
    )
    failing = MutateCanonicalThenFail()
    registry, _lifecycles = stage_registry(overrides={14: failing})
    store = SQLiteStateStore(tmp_path)
    store.initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=13,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    trust_seeded_reviewer_gate(store, tmp_path)

    failed = FactoryEngine(tmp_path, store=store, registry=registry).run(max_steps=1)

    assert failed.status is WorkflowStatus.FAILED
    assert {(item["flag"], item["owner_stage"]) for item in store.dirty_flags()} == {
        ("RESULT_DIRTY", 4)
    }
    assert store.events()[-1].type == "STEP_FAILED"


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
    (tmp_path / f"{tmp_path.name}_paper.tex").write_text(
        "\\begin{document}ready\\end{document}\n", encoding="utf-8"
    )
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
    resolved = store.load()
    guarded = engine.run(max_steps=1)

    assert resolved.status is WorkflowStatus.READY
    assert guarded.last_completed_step == 15
    assert guarded.last_completed_stage == 9
    assert guarded.active_subtask == "delivery"


def test_human_request_build_failure_clears_runner_state(tmp_path, monkeypatch):
    registry, _lifecycles = stage_registry()
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id="demo", project_type="modeling")
    engine = FactoryEngine(tmp_path, store=store, registry=registry)

    def fail_request(**_kwargs):
        raise ValueError("fingerprint precondition failed")

    monkeypatch.setattr("factory_core.engine.build_decision_request", fail_request)
    failed = engine._await_action(
        state,
        {"type": "approval", "gate": "content_freeze"},
        reason="review required",
        evidence=(),
    )

    assert failed.status is WorkflowStatus.FAILED
    assert failed.runner_pid is None
    assert failed.runner_lease_id is None
    assert failed.pending_action is None
    event = store.events()[-1]
    assert event.type == "DECISION_REQUEST_BUILD_FAILED"
    assert event.payload["error_class"] == (
        "PERMANENT_DECISION_REQUEST_BUILD_FAILED"
    )


def test_successful_prompt_stage_checkpoint_rejects_missing_prompt_identity(tmp_path):
    from types import SimpleNamespace
    from factory_core.domain import ExecutionResult
    from factory_core.engine import FactoryEngine
    from factory_core.storage import SQLiteStateStore

    store = SQLiteStateStore(tmp_path)
    store.initialize(
        project_id="prompt-guard",
        project_type="modeling",
        scheduler_generation="stage_v1",
    )
    engine = FactoryEngine(tmp_path, store=store)
    task = SimpleNamespace(
        definition=SimpleNamespace(
            lifecycle=SimpleNamespace(requires_prompt_input_receipt=True)
        )
    )

    assert engine._prompt_input_receipt_valid(
        task, ExecutionResult.succeeded()
    ) is False


def test_successful_prompt_stage_checkpoint_accepts_bound_prompt_identity(tmp_path):
    from types import SimpleNamespace
    from factory_core.domain import ExecutionResult
    from factory_core.effective_prompt import build_effective_prompt_receipt
    from factory_core.engine import FactoryEngine
    from factory_core.storage import SQLiteStateStore

    store = SQLiteStateStore(tmp_path)
    state = store.initialize(
        project_id="prompt-guard-valid",
        project_type="modeling",
        scheduler_generation="stage_v1",
    )
    template = tmp_path / "prompt.txt"
    template.write_text("prompt\n", encoding="utf-8")
    receipt = build_effective_prompt_receipt(
        project_dir=tmp_path,
        factory_root=tmp_path,
        project_id="prompt-guard-valid",
        source_step_id=4,
        stage_id=3,
        subtask="model_construction",
        attempt=1,
        selected_revision=state.revision,
        prompt_template=template,
        prompt="effective prompt",
        researcher_note="",
    )
    store.bind_prompt_attempt_input(
        expected_revision=state.revision, receipt=receipt
    )
    task = SimpleNamespace(
        definition=SimpleNamespace(
            lifecycle=SimpleNamespace(requires_prompt_input_receipt=True)
        )
    )
    result = ExecutionResult.succeeded(
        prompt_input_schema="factory-effective-prompt-v1",
        prompt_input_receipt_id=receipt["receipt_id"],
        prompt_input_attempt_key=receipt["attempt_key"],
        effective_prompt_sha256=receipt["effective_prompt_sha256"],
        prompt_inputs_sha256=receipt["prompt_inputs_sha256"],
    )
    engine = FactoryEngine(tmp_path, store=store)

    assert engine._prompt_input_receipt_valid(task, result) is True


def test_engine_dispatches_prompt_only_after_durable_input_binding(tmp_path):
    import hashlib

    from factory_core.steps.prompt_step import PromptStep
    from factory_core.steps.prompting import PromptRenderer

    factory_root = tmp_path / "factory"
    project = factory_root / "ongoing" / "demo"
    prompt_dir = factory_root / "prompts"
    project.mkdir(parents=True)
    prompt_dir.mkdir(parents=True)
    prompt_dir.joinpath("step4_model_construction.txt").write_text(
        "Build the model for __BASE_NAME__.\n", encoding="utf-8"
    )

    store = SQLiteStateStore(project)
    store.initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=3,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    observed: dict[str, object] = {}

    class ReceiptAwareDispatcher:
        @staticmethod
        def execute(request, **_kwargs):
            events = store.events()
            receipts = store.prompt_attempt_inputs()
            assert events[-1].type == "PROMPT_INPUT_BOUND"
            assert len(receipts) == 1
            receipt = receipts[0]
            assert receipt["bound_revision"] == events[-1].revision
            assert receipt["effective_prompt_sha256"] == hashlib.sha256(
                request.prompt.encode("utf-8")
            ).hexdigest()
            observed["dispatch_revision"] = events[-1].revision
            observed["receipt_id"] = receipt["receipt_id"]
            return ExecutionResult.succeeded(model_id="test")

    class ValidPromptOutput:
        @staticmethod
        def validate(_context):
            return ValidationResult.valid("artifact")

    lifecycle = PromptStep(
        contract_for(4),
        PromptRenderer(factory_root),
        ReceiptAwareDispatcher(),
        ValidPromptOutput(),
    )
    registry, _lifecycles = stage_registry(overrides={4: lifecycle})

    state = FactoryEngine(project, store=store, registry=registry).run(max_steps=1)

    event_types = [event.type for event in store.events()]
    assert event_types.index("STEP_STARTED") < event_types.index(
        "PROMPT_INPUT_BOUND"
    ) < event_types.index("STEP_SUCCEEDED")
    prompt_bound = next(
        event for event in store.events() if event.type == "PROMPT_INPUT_BOUND"
    )
    assert observed["dispatch_revision"] == prompt_bound.revision
    assert observed["receipt_id"] == store.prompt_attempt_inputs()[0]["receipt_id"]
    assert state.status is WorkflowStatus.READY
    assert state.last_completed_step == 4
