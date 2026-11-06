"""Stage catalog v2: the paper-segment reorder (optimization plan, phase 2).

v2 runs ... 11 -> 12 -> 14 -> 15 -> [content freeze] -> 13 final Judge -> 16.
Step numbers keep their meaning, so every ordering decision for a v2 project
must use catalog position.  v1 projects keep their behaviour (covered by
test_stage_scheduler.py, which is unchanged).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from factory_core.contest import ContestPolicy
from factory_core.current_dirty import classifier_contract_sha256
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
    CATALOG_V1,
    CATALOG_V2,
    STAGE_CATALOG_VERSION,
    STAGE_CATALOG_VERSION_V2,
    STAGE_SCHEDULER_GENERATION,
    catalog_for_version,
)
from factory_core.steps.catalog import STEP_CONTRACTS
from factory_core.storage import SQLiteStateStore
from scripts.step8_5_gate import collect_step8_5_state


# -- catalog ---------------------------------------------------------------

def test_v2_order_moves_step13_after_content_freeze():
    order = [(stage.id, subtask.key, subtask.checkpoint_step_id) for stage, subtask in CATALOG_V2.order]
    assert order[-7:] == [
        (8, "constructive_review", 11),
        (8, "revision", 12),
        (9, "abstract", 14),
        (9, "polish", 15),
        (10, "content_freeze_guard", None),
        (10, "final_judge", 13),
        (10, "delivery", 16),
    ]
    # Stages 1-7 are the v1 Stages unchanged.
    assert CATALOG_V2.contracts[:7] == CATALOG_V1.contracts[:7]
    assert all(policy.gate != "conditional_math_preflight" for policy in CATALOG_V2.gate_policies)


def test_v2_ordering_is_positional_not_numeric():
    resume = {stage: CATALOG_V2.resume_after_step_for_stage(stage) for stage in range(1, 11)}
    assert resume == {1: -1, 2: 1, 3: 3, 4: 4, 5: 5, 6: 7, 7: 8, 8: 10, 9: 12, 10: 15}
    assert CATALOG_V2.subtasks_after_step(13) == ((10, "delivery"),)
    assert CATALOG_V2.subtasks_after_step(14)[0] == (9, "polish")
    assert (10, "final_judge") in CATALOG_V2.subtasks_after_step(15)
    # Step 15 does not complete Stage 10; Step 13 alone does not either.
    assert [CATALOG_V2.completed_stage_for_step(s) for s in (12, 15, 13, 16)] == [8, 9, 9, 10]


def test_v1_catalog_answers_are_unchanged():
    from factory_core import stages

    for step in [-1, *range(17)]:
        assert CATALOG_V1.completed_stage_for_step(step) == stages.completed_stage_for_step(step)
        assert CATALOG_V1.initial_stage_checkpoints(step) == stages.initial_stage_checkpoints(step)
    for stage in range(1, 11):
        assert CATALOG_V1.resume_after_step_for_stage(stage) == stages.resume_after_step_for_stage(stage)
    assert catalog_for_version(None) is CATALOG_V1
    assert catalog_for_version(STAGE_CATALOG_VERSION) is CATALOG_V1
    with pytest.raises(ValueError, match="unsupported Stage catalog"):
        catalog_for_version("factory-stage-catalog-v9")


# -- engine fixtures -------------------------------------------------------

@dataclass
class Lifecycle:
    calls: list[int] = field(default_factory=list)
    writes: dict[str, str] = field(default_factory=dict)
    result: dict = field(default_factory=dict)

    def prepare(self, _context):
        return PrepareResult.prepared()

    def execute(self, context):
        self.calls.append(context.attempt)
        if len(self.calls) == 1:
            for name, text in self.writes.items():
                path = context.project_dir / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
        return ExecutionResult.succeeded(**(self.result if len(self.calls) == 1 else {}))

    def validate(self, _context):
        return ValidationResult.valid()

    def recover(self, _context, _error):
        return RecoveryDecision(RecoveryDisposition.RETRY)


class ReviewerGate(Lifecycle):
    def execute(self, context):
        self.calls.append(context.attempt)
        write_reviewer_reports(context.project_dir)
        return ExecutionResult.succeeded()


def write_reviewer_reports(project):
    for name in ("reviewer_entry_map.md", "anchor_figure_plan.md"):
        (project / name).write_text("# ready\n", encoding="utf-8")
    (project / "entry_gate.md").write_text("# gate\n\nVERDICT: PASS\n", encoding="utf-8")


def v2_registry(*, overrides=None, final_judge=None, content_guard=None):
    overrides = overrides or {}
    registry = StepRegistry()
    lifecycles = {}
    for contract in STEP_CONTRACTS:
        lifecycle = overrides.get(contract.id) or Lifecycle()
        lifecycles[contract.id] = lifecycle
        registry.register(StepDefinition(
            contract.id, contract.name, contract.timeout_seconds, contract.max_attempts,
            max_reopens=contract.max_reopens, step=lifecycle,
        ))
    registry.register_stage_subtask(
        "reviewer_entry_gate",
        StepDefinition(8, "reviewer_entry_gate", 7_200, 5, max_reopens=0, step=ReviewerGate()),
    )
    lifecycles["final_judge"] = final_judge or Lifecycle()
    registry.register_stage_subtask(
        "final_judge",
        StepDefinition(13, "final_judge", 21_600, 2, max_reopens=1, step=lifecycles["final_judge"]),
    )
    registry.register_stage_subtask(
        "content_freeze_guard",
        StepDefinition(16, "content_freeze_guard", 21_600, 1, max_reopens=0,
                       step=content_guard or Lifecycle()),
    )
    return registry, lifecycles


def v2_store(project, *, last_completed_step=-1, **kwargs):
    store = SQLiteStateStore(project)
    store.initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=last_completed_step,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
        stage_catalog_version=STAGE_CATALOG_VERSION_V2,
        **kwargs,
    )
    if last_completed_step >= 9 or last_completed_step == 13:
        # Bind the seeded reviewer gate to the real Step 8.5 state.
        write_reviewer_reports(project)
        state = store.load()
        store.transition(
            expected_revision=state.revision,
            event_type="TRUST_REVIEWER_GATE_FOR_TEST",
            changes={},
            stage_checkpoint={
                "stage_id": 6, "subtask": "reviewer_entry_gate", "source_step_id": 8,
                "completed_step_id": None, "input_fingerprint": "test",
                "output_fingerprint": "test",
                "receipt": {"schema_version": "factory-stage-checkpoint-v1",
                            "validation": {"step8_5": collect_step8_5_state(project)},
                            "result": {}},
            },
        )
    return store


def succeeded_sequence(store, *, after_revision=0):
    return [
        (event.payload["stage"], event.payload["subtask"])
        for event in store.events()
        if event.type == "STEP_SUCCEEDED" and event.revision > after_revision
    ]


def reopens(store):
    return [event for event in store.events() if event.type == "STAGE_SEMANTIC_REOPENED"]


# -- engine ----------------------------------------------------------------

def test_v2_project_runs_the_reordered_paper_segment_to_completion(tmp_path):
    registry, lifecycles = v2_registry()
    store = v2_store(tmp_path)

    state = FactoryEngine(tmp_path, store=store, registry=registry).run()

    assert state.status is WorkflowStatus.COMPLETED
    assert state.last_completed_step == 16
    assert state.last_completed_stage == 10
    assert succeeded_sequence(store)[-7:] == [
        (8, "constructive_review"), (8, "revision"), (9, "abstract"), (9, "polish"),
        (10, "content_freeze_guard"), (10, "final_judge"), (10, "delivery"),
    ]
    assert all(
        event.payload["stage_catalog_version"] == STAGE_CATALOG_VERSION_V2
        for event in store.events() if event.type == "STAGE_SUBTASK_SELECTED"
    )
    # Every v2 subtask ran exactly once: no Step-13 checkpoint deleted 14/15.
    assert lifecycles["final_judge"].calls == [1]
    assert lifecycles[13].calls == []  # the v1 Step-13 precheck never runs
    assert lifecycles[14].calls == [1] and lifecycles[15].calls == [1]
    assert len(store.stage_checkpoints()) == 19


def test_v2_initialize_seeds_by_position(tmp_path):
    store = v2_store(tmp_path, last_completed_step=15)
    keys = {(c["stage_id"], c["subtask"]) for c in store.stage_checkpoints()}
    assert (9, "polish") in keys
    assert not keys & {(10, "content_freeze_guard"), (10, "final_judge"), (10, "delivery")}
    assert store.load().last_completed_stage == 9


def test_v2_invalidation_after_step13_keeps_stage9(tmp_path):
    store = v2_store(tmp_path, last_completed_step=13)
    state = store.load()
    store.transition(
        expected_revision=state.revision,
        event_type="TEST_INVALIDATE",
        changes={},
        invalidate_checkpoints_after_step=13,
    )
    keys = {(c["stage_id"], c["subtask"]) for c in store.stage_checkpoints()}
    assert {(9, "abstract"), (9, "polish"), (10, "content_freeze_guard"), (10, "final_judge")} <= keys
    assert (10, "delivery") not in keys


def test_v2_abstract_math_edit_does_not_reopen_stage8(tmp_path):
    paper = tmp_path / "demo_paper.tex"
    paper.write_text("\\begin{document}\n$x=1$\n\\end{document}\n", encoding="utf-8")
    abstract = Lifecycle(writes={"demo_paper.tex": "\\begin{document}\n$x=2$\n\\end{document}\n"})
    registry, _ = v2_registry(overrides={14: abstract})
    store = v2_store(tmp_path, last_completed_step=12)

    state = FactoryEngine(tmp_path, store=store, registry=registry).run(max_steps=1)

    assert reopens(store) == []
    assert state.last_completed_step == 14
    assert state.active_subtask == "polish"
    flags = store.dirty_flags()
    assert any(flag["owner_stage"] == 10 for flag in flags), flags


def test_v2_final_judge_projection_does_not_reopen_stage8(tmp_path):
    judge = Lifecycle(writes={"judge_evaluation.md": "VERDICT: PASS\n"})
    registry, _ = v2_registry(final_judge=judge)
    (tmp_path / "judge_evaluation.md").write_text("VERDICT: PRECHECK_PASS\n", encoding="utf-8")
    store = v2_store(tmp_path, last_completed_step=15)

    state = FactoryEngine(tmp_path, store=store, registry=registry).run()

    assert reopens(store) == []
    assert state.status is WorkflowStatus.COMPLETED


def test_v2_revision_document_edit_is_a_forward_repair(tmp_path):
    (tmp_path / "symbol_table.md").write_text("| x | a |\n", encoding="utf-8")
    (tmp_path / "reviewer_entry_map.md").write_text("# ready\n", encoding="utf-8")
    revision = Lifecycle(writes={
        "symbol_table.md": "| x | a | b |\n",
        "reviewer_entry_map.md": "# ready, clarified\n",
    })
    registry, _ = v2_registry(overrides={12: revision})
    store = v2_store(tmp_path, last_completed_step=11)

    state = FactoryEngine(tmp_path, store=store, registry=registry).run(max_steps=1)

    assert reopens(store) == []
    assert not any(e.type == "STAGE_CHECKPOINT_INVALIDATED" for e in store.events())
    assert state.last_completed_step == 12
    assert state.active_subtask == "abstract"


def test_v2_revision_model_code_change_still_reopens(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "solve.py").write_text("x = 1\n", encoding="utf-8")
    revision = Lifecycle(writes={"models/solve.py": "x = 2\n"})
    registry, _ = v2_registry(overrides={12: revision})
    store = v2_store(tmp_path, last_completed_step=11)

    FactoryEngine(tmp_path, store=store, registry=registry).run(max_steps=1)

    [reopen] = reopens(store)
    assert reopen.payload["semantic_owner_stage"] == 3
    assert reopen.payload["resume_after_step"] == 3


def test_v2_v1_project_keeps_v1_reopen_for_the_same_edit(tmp_path):
    # Control: the identical abstract math edit on a v1 project still reopens.
    from factory_core.stages import STAGE_CATALOG_VERSION as V1

    paper = tmp_path / "demo_paper.tex"
    paper.write_text("\\begin{document}\n$x=1$\n\\end{document}\n", encoding="utf-8")
    abstract = Lifecycle(writes={"demo_paper.tex": "\\begin{document}\n$x=2$\n\\end{document}\n"})
    registry, _ = v2_registry(overrides={14: abstract})
    registry.register_stage_subtask(
        "conditional_math_preflight_skip",
        StepDefinition(13, "conditional_math_preflight_skip", 10_800, 1, max_reopens=0, step=Lifecycle()),
    )
    store = SQLiteStateStore(tmp_path)
    store.initialize(project_id="demo", project_type="modeling", last_completed_step=13,
                     scheduler_generation=STAGE_SCHEDULER_GENERATION, stage_catalog_version=V1)
    write_reviewer_reports(tmp_path)
    state = store.load()
    store.transition(
        expected_revision=state.revision, event_type="TRUST_REVIEWER_GATE_FOR_TEST", changes={},
        stage_checkpoint={"stage_id": 6, "subtask": "reviewer_entry_gate", "source_step_id": 8,
                          "completed_step_id": None, "input_fingerprint": "t", "output_fingerprint": "t",
                          "receipt": {"schema_version": "factory-stage-checkpoint-v1",
                                      "validation": {"step8_5": collect_step8_5_state(tmp_path)},
                                      "result": {}}},
    )
    FactoryEngine(tmp_path, store=store, registry=registry).run(max_steps=1)
    [reopen] = reopens(store)
    assert reopen.payload["semantic_owner_stage"] == 8


def test_v2_judge_reopen_returns_to_revision(tmp_path):
    judge = Lifecycle(result={"resume_after_step": 11, "final_decision": "REOPEN_REVISION_TEXT"})
    registry, lifecycles = v2_registry(final_judge=judge)
    store = v2_store(tmp_path, last_completed_step=15)

    state = FactoryEngine(tmp_path, store=store, registry=registry).run()

    [reopened] = [e for e in store.events() if e.type == "STEP_REOPENED"]
    assert reopened.payload["resume_after_step"] == 11
    assert reopened.payload["subtask"] == "final_judge"
    assert state.status is WorkflowStatus.COMPLETED
    # Revision, final prose, the gate and the Judge all run again, in v2 order.
    assert succeeded_sequence(store, after_revision=reopened.revision) == [
        (8, "revision"), (9, "abstract"), (9, "polish"),
        (10, "content_freeze_guard"), (10, "final_judge"), (10, "delivery"),
    ]
    assert judge.calls == [1, 1]


def test_v2_resume_target_must_be_earlier_by_position():
    with pytest.raises(InvalidTransition):
        FactoryEngine._validated_resume_target(13, 14, CATALOG_V2)  # 13 runs after 14
    assert FactoryEngine._validated_resume_target(12, 13, CATALOG_V2) == 12
    assert FactoryEngine._validated_resume_target(15, 16, CATALOG_V2) == 15
    # v1 keeps the numeric rule.
    assert FactoryEngine._validated_resume_target(13, 14, CATALOG_V1) == 13


def test_v2_content_freeze_rejection_reopens_final_prose(tmp_path):
    from factory_core.steps.specialized import ContentFreezeGuardStep

    (tmp_path / f"{tmp_path.name}_paper.tex").write_text(
        "\\begin{document}ready\\end{document}\n", encoding="utf-8"
    )
    registry, _ = v2_registry(content_guard=ContentFreezeGuardStep())
    store = SQLiteStateStore(tmp_path)
    policy = ContestPolicy.default(started_at=store.now_epoch())
    store = v2_store(tmp_path, last_completed_step=15, contest_policy=policy.to_dict())
    engine = FactoryEngine(tmp_path, store=store, registry=registry)

    awaiting = engine.run()
    assert awaiting.pending_action["gate"] == "content_freeze"
    assert awaiting.active_subtask == "content_freeze_guard"

    store.record_decision("content_freeze", {"selected_option_id": "reject"})
    rejected = store.load()

    assert rejected.last_completed_step == 12
    assert rejected.last_completed_stage == 8
    keys = {(c["stage_id"], c["subtask"]) for c in store.stage_checkpoints()}
    assert (8, "revision") in keys
    assert not keys & {(9, "abstract"), (9, "polish")}


def test_v2_final_judge_runs_in_the_terminal_reserve(tmp_path):
    registry, _ = v2_registry()
    store = SQLiteStateStore(tmp_path)
    policy = ContestPolicy.default(started_at=store.now_epoch())
    store = v2_store(tmp_path, last_completed_step=15, contest_policy=policy.to_dict())
    engine = FactoryEngine(tmp_path, store=store, registry=registry)
    judge = registry.stage_subtask("final_judge")

    assert engine._contest_deadline(judge) == policy.contest_deadline_at
    assert engine._contest_deadline(registry.get(15)) == policy.content_freeze_at


def test_v2_classifier_identity_is_separate_and_v1_unchanged(tmp_path):
    v1_state = SQLiteStateStore(tmp_path / "a")
    v1_state.initialize(project_id="a", project_type="modeling",
                        scheduler_generation=STAGE_SCHEDULER_GENERATION)
    v2 = v2_store(tmp_path / "b")
    assert FactoryEngine._classifier_sha(v1_state.load()) == classifier_contract_sha256()
    assert FactoryEngine._classifier_sha(v2.load()) != classifier_contract_sha256()


def test_v2_has_no_step13_technical_continuation(tmp_path):
    from factory_core import technical_continuation

    registry, _ = v2_registry()
    store = v2_store(tmp_path, last_completed_step=12)
    engine = FactoryEngine(tmp_path, store=store, registry=registry)
    with pytest.raises(InvalidTransition, match="catalog v2"):
        technical_continuation.authorize(
            engine, expected_revision=store.load().revision, reason="test"
        )


# -- the v2 Step-13 lifecycle ----------------------------------------------

def test_final_judge_step_runs_the_full_final_audit_as_analysis(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from factory_core.audit import FinalAuditService
    from factory_core.domain import StepContext
    from factory_core.steps.specialized import FinalJudgeStep
    from test_audit_service import FakeValidator, PassingJudge, RecordingRunner, make_context

    root = tmp_path / "factory"
    project = root / "ongoing" / "demo"
    project.mkdir(parents=True)
    seen_steps = []

    class RecordingJudge(PassingJudge):
        def execute_prepared(self, context):
            seen_steps.append(context.step_id)
            return super().execute_prepared(context)

    current = {"id": "f" * 64}
    service = FinalAuditService(root, RecordingJudge(), FakeValidator(), RecordingRunner(),
                                fingerprinter=lambda _p, _b: current["id"])
    step = FinalJudgeStep(SimpleNamespace(final_audit_service=lambda: service))
    base = make_context(project)
    context = StepContext(project, project.name, 13, 1, 3_600, 0)
    assert base.project_dir == context.project_dir

    result = step.execute(context)

    assert result.returncode == 0
    assert result.metadata["analysis_only"] is True
    assert seen_steps == [16]  # the full Judge, not the v1 Step-13 precheck
    assert not (project / "judge_outputs/final_submission.sha256").exists()
    monkeypatch.setattr("scripts.judgment_receipt.verify_receipt",
                        lambda *_a, **_k: (True, []))
    assert step.validate(context).is_valid
    current["id"] = "0" * 64  # the content changed after the Judge
    assert not step.validate(context).is_valid


def test_new_projects_default_to_v1_and_v2_is_opt_in(tmp_path):
    from factory_core.service import FactoryService

    service = FactoryService(tmp_path)
    default, _ = service.create_project("default_demo", "q", start=False)
    reordered, _ = service.create_project(
        "v2_demo", "q", start=False, stage_catalog_version=STAGE_CATALOG_VERSION_V2
    )
    assert default.stage_catalog_version == STAGE_CATALOG_VERSION
    assert reordered.stage_catalog_version == STAGE_CATALOG_VERSION_V2
    with pytest.raises(ValueError, match="unsupported Stage catalog"):
        service.create_project("bad", "q", stage_catalog_version="factory-stage-catalog-v7")


def test_cli_create_accepts_the_v2_catalog():
    from factory_core.cli import build_parser

    args = build_parser().parse_args(["create", "demo", "q", "--stage-catalog", "v2"])
    assert args.stage_catalog == "v2"
    assert build_parser().parse_args(["create", "demo", "q"]).stage_catalog == "v1"
