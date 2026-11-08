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


def _checkpoint_steps(store):
    return sorted(
        item["completed_step_id"] for item in store.stage_checkpoints()
        if item["completed_step_id"] is not None
    )


def test_v2_revision_model_code_change_recomputes_then_returns_to_revision(tmp_path):
    """Phase 4: changed solve code re-runs Steps 5-8 only, then revision resumes."""

    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "solve.py").write_text("x = 1\n", encoding="utf-8")
    revision = Lifecycle(writes={"models/solve.py": "x = 2\n"})
    registry, lifecycles = v2_registry(overrides={12: revision})
    store = v2_store(tmp_path, last_completed_step=11)
    engine = FactoryEngine(tmp_path, store=store, registry=registry)

    engine.run(max_steps=1)

    [reopen] = reopens(store)
    assert reopen.payload["rework_rule"] == "input_dependency"
    assert reopen.payload["recompute_steps"] == [5, 6, 7, 8]
    assert reopen.payload["return_subtask"] == "revision"
    assert reopen.payload["semantic_owner_stage"] == 4  # the solve, not model construction
    assert reopen.payload["resume_after_step"] == 4
    assert reopen.payload["recompute_reasons"] == ["models/solve.py: consumed by Step 5"]
    # Only the computation checkpoints went; the paper segment's stayed.  The
    # same run already re-ran the solve (the reopen is not a counted step).
    assert _checkpoint_steps(store) == [0, 1, 2, 3, 4, 5, 9, 10, 11]
    assert lifecycles[5].calls == [1]

    state = engine.run(max_steps=4)  # sensitivity, evaluation, visualization, revision

    for step in (5, 6, 7, 8):
        assert lifecycles[step].calls == [1], step
    for step in (4, 9, 10, 11):
        assert lifecycles[step].calls == [], step  # not re-run
    assert len(revision.calls) == 2  # revision runs again on the recomputed results
    assert len(reopens(store)) == 1
    assert state.last_completed_step == 12
    assert _checkpoint_steps(store) == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]


def test_v2_revision_result_change_recomputes_from_sensitivity(tmp_path):
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "canonical_results.json").write_text("{}\n", encoding="utf-8")
    revision = Lifecycle(writes={"results/canonical_results.json": '{"p1": 1}\n'})
    registry, lifecycles = v2_registry(overrides={12: revision})
    store = v2_store(tmp_path, last_completed_step=11)

    FactoryEngine(tmp_path, store=store, registry=registry).run(max_steps=1)

    [reopen] = reopens(store)
    assert reopen.payload["recompute_steps"] == [6, 7, 8]
    assert reopen.payload["return_subtask"] == "revision"
    assert reopen.payload["semantic_owner_stage"] == 5
    assert lifecycles[5].calls == []  # the solve did not consume the result
    assert lifecycles[6].calls == [1]
    # The solve keeps its checkpoint; sensitivity already re-ran in this run.
    assert _checkpoint_steps(store) == [0, 1, 2, 3, 4, 5, 6, 9, 10, 11]


def test_v2_revision_document_change_recomputes_nothing(tmp_path):
    for name in ("symbol_table.md", "model.md", "evaluation.md"):
        (tmp_path / name).write_text("# v1\n", encoding="utf-8")
    revision = Lifecycle(writes={name: "# v2\n" for name in ("symbol_table.md", "model.md", "evaluation.md")})
    registry, lifecycles = v2_registry(overrides={12: revision})
    store = v2_store(tmp_path, last_completed_step=11)

    state = FactoryEngine(tmp_path, store=store, registry=registry).run(max_steps=1)

    assert reopens(store) == []
    assert state.last_completed_step == 12
    assert all(lifecycles[step].calls == [] for step in range(4, 12))
    succeeded = [e for e in store.events() if e.type == "STEP_SUCCEEDED"][-1]
    assert succeeded.payload["subtask"] == "revision"  # forward repair, no recompute


def test_v2_evaluation_changing_main_results_recomputes_sensitivity_in_its_own_stage(tmp_path):
    # Steps 6 and 7 share Stage 5: the consumer is completed though not upstream.
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "canonical_results.json").write_text("{}\n", encoding="utf-8")
    evaluation = Lifecycle(writes={"results/canonical_results.json": '{"p1": 1}\n'})
    registry, lifecycles = v2_registry(overrides={7: evaluation})
    store = v2_store(tmp_path, last_completed_step=6)
    engine = FactoryEngine(tmp_path, store=store, registry=registry)

    engine.run(max_steps=1)

    [reopen] = reopens(store)
    assert reopen.payload["recompute_steps"] == [6]
    assert reopen.payload["return_subtask"] == "model_evaluation"
    engine.run(max_steps=2)
    assert lifecycles[6].calls == [1] and len(evaluation.calls) == 2
    assert lifecycles[5].calls == []


def test_v2_unregistered_file_keeps_the_fail_closed_rewind(tmp_path):
    # No ownership rule or policy covers it, so its role is unknown: the rule
    # does not guess "document" for it.
    (tmp_path / "peak_groups.csv").write_text("a\n", encoding="utf-8")
    revision = Lifecycle(writes={"peak_groups.csv": "b\n"})
    registry, _ = v2_registry(overrides={12: revision})
    store = v2_store(tmp_path, last_completed_step=11)

    FactoryEngine(tmp_path, store=store, registry=registry).run(max_steps=1)

    [reopen] = reopens(store)
    assert "rework_rule" not in reopen.payload
    assert reopen.payload["semantic_owner_stage"] == 4  # fail-closed RESULT@4


def test_v2_stream_aux_file_is_exploratory_and_rewinds_nothing(tmp_path):
    # A root m<N>_* file is a Step 2 stream's auxiliary output: the fallback
    # policy declares it exploratory and non-blocking (F10), so a later write
    # is neither a rewind nor a final input.
    (tmp_path / "m2_peak_groups.csv").write_text("a\n", encoding="utf-8")
    revision = Lifecycle(writes={"m2_peak_groups.csv": "b\n"})
    registry, _ = v2_registry(overrides={12: revision})
    store = v2_store(tmp_path, last_completed_step=11)

    FactoryEngine(tmp_path, store=store, registry=registry).run(max_steps=1)

    assert reopens(store) == []


def test_v1_project_keeps_the_ownership_rewind_for_the_same_code_edit(tmp_path):
    from factory_core.stages import STAGE_CATALOG_VERSION as V1

    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "solve.py").write_text("x = 1\n", encoding="utf-8")
    revision = Lifecycle(writes={"models/solve.py": "x = 2\n"})
    registry, _ = v2_registry(overrides={12: revision})
    registry.register_stage_subtask(
        "conditional_math_preflight_skip",
        StepDefinition(13, "conditional_math_preflight_skip", 10_800, 1, max_reopens=0, step=Lifecycle()),
    )
    store = SQLiteStateStore(tmp_path)
    store.initialize(project_id="demo", project_type="modeling", last_completed_step=11,
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
    assert reopen.payload["semantic_owner_stage"] == 3
    assert "rework_rule" not in reopen.payload


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


LEDGER_HEADER = (
    "| Issue ID | First Raised In | Category | Severity | Blocking Until | Status | Notes |\n"
    "|---|---|---|---|---|---|---|\n"
)


def write_ledger(project, review_rows, machine_rows=()):
    """An issue ledger shaped like the one acceptance A's Step 11 left."""

    def row(issue_id, category, severity, status):
        return f"| {issue_id} | review:step11 | {category} | {severity} | Step 12 | {status} | n |\n"

    (project / "audit_issue_ledger.md").write_text(
        "# Audit Issue Ledger\n\n## Incremental audit findings\n\n" + LEDGER_HEADER
        + "".join(row(*item) for item in machine_rows)
        + "\n## Step 11 constructive review issues\n\n" + LEDGER_HEADER
        + "".join(row(*item) for item in review_rows),
        encoding="utf-8",
    )


class ResolvingRevision(Lifecycle):
    """Step 12 that resolves every review row from its second run on."""

    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def execute(self, context):
        self.calls.append(context.attempt)
        if len(self.calls) >= 2:
            write_ledger(context.project_dir, [(i, c, s, "RESOLVED") for i, c, s, _ in self.rows])
        return ExecutionResult.succeeded()


def failures(store):
    return [event for event in store.events() if event.type == "STEP_FAILED"]


def test_v2_review_exit_ignores_machine_warning_rows(tmp_path):
    """F7: a MAJOR AUDIT-* row (the symbols warning) does not block the v2 exit."""

    write_ledger(tmp_path, [("S11-m1", "paper", "MINOR", "OPEN")],
                 machine_rows=[("AUDIT-PAPER-SYMBOLS", "paper", "MAJOR", "OPEN")])
    registry, _ = v2_registry()
    store = v2_store(tmp_path, last_completed_step=11)

    state = FactoryEngine(tmp_path, store=store, registry=registry).run(max_steps=1)

    assert failures(store) == [] and reopens(store) == []
    assert state.last_completed_step == 12
    assert state.active_subtask == "abstract"


def test_v2_review_exit_recomputes_for_results_issue_then_returns_to_revision(tmp_path):
    rows = [("S11-B4", "results", "BLOCKING", "OPEN"), ("S11-M9", "paper", "MAJOR", "OPEN")]
    write_ledger(tmp_path, rows)
    revision = ResolvingRevision(rows)
    registry, lifecycles = v2_registry(overrides={12: revision})
    store = v2_store(tmp_path, last_completed_step=11)

    state = FactoryEngine(tmp_path, store=store, registry=registry).run()

    [reopen] = reopens(store)
    assert reopen.payload["rework_rule"] == "review_issue"
    assert reopen.payload["recompute_steps"] == [5, 6, 7, 8]
    assert reopen.payload["return_subtask"] == "revision"
    assert reopen.payload["semantic_owner_stage"] == 4
    assert reopen.payload["review_issues"] == ["S11-B4 (results, BLOCKING)"]
    assert reopen.payload["recompute_reasons"] == [
        "audit_issue_ledger.md#S11-B4: open results BLOCKING review issue"
    ]
    for step in (5, 6, 7, 8):
        assert lifecycles[step].calls == [1], step
    assert lifecycles[11].calls == []  # the review is not re-run
    assert len(revision.calls) == 2
    assert failures(store) == []
    assert state.status is WorkflowStatus.COMPLETED


def test_v2_review_exit_fails_when_results_issue_survives_the_recompute(tmp_path):
    write_ledger(tmp_path, [("S11-B3", "model", "BLOCKING", "OPEN")])
    registry, lifecycles = v2_registry()
    store = v2_store(tmp_path, last_completed_step=11)

    state = FactoryEngine(tmp_path, store=store, registry=registry).run()

    assert len(reopens(store)) == 1
    assert lifecycles[12].calls == [1, 1]
    [failed] = failures(store)
    assert failed.payload["error_class"] == "PERMANENT_REVIEW_LOOP_EXHAUSTED"
    assert failed.payload["review_issues"] == ["S11-B3 (model, BLOCKING)"]
    assert "review recompute" in failed.payload["reason"]
    assert state.status is WorkflowStatus.FAILED


def test_v2_review_recompute_budget_restarts_after_retry_step(tmp_path):
    write_ledger(tmp_path, [("S11-B4", "results", "BLOCKING", "OPEN")])
    registry, lifecycles = v2_registry()
    store = v2_store(tmp_path, last_completed_step=11)
    engine = FactoryEngine(tmp_path, store=store, registry=registry)
    failed = engine.run()
    assert failed.status is WorkflowStatus.FAILED and len(reopens(store)) == 1

    engine.retry_step(expected_revision=failed.revision, step_id=12, note="fixed the rework prompt")
    state = engine.run()

    assert len(reopens(store)) == 2  # a second review recompute, from Step 5
    assert lifecycles[5].calls == [1, 1]
    assert state.status is WorkflowStatus.FAILED
    assert len(failures(store)) == 2


def _review_reopen(store, *, steps=(5, 6, 7, 8), issues=("S11-B4 (results, BLOCKING)",)):
    state = store.load()
    return store.transition(
        expected_revision=state.revision,
        event_type="STAGE_SEMANTIC_REOPENED",
        changes={},
        payload={
            "stage": 8, "subtask": "revision", "rework_rule": "review_issue",
            "recompute_steps": list(steps), "return_subtask": "revision",
            "review_issues": list(issues),
        },
    )


def test_v2_review_recompute_names_the_issues_in_the_recomputed_prompts(tmp_path):
    from pathlib import Path as _Path

    from factory_core.steps.prompting import PromptRenderer

    store = v2_store(tmp_path, last_completed_step=11)
    write_ledger(tmp_path, [("S11-B4", "results", "BLOCKING", "OPEN")])
    renderer = PromptRenderer(_Path(__file__).resolve().parents[1])
    render = lambda template, step: renderer.render(  # noqa: E731
        template, tmp_path, step_key=step, include_preamble=False, researcher_note=""
    )
    assert "【审稿返工" not in render("step6_sensitivity.txt", "6")

    _review_reopen(store)

    step6 = render("step6_sensitivity.txt", "6")
    assert step6.startswith("AGENT_KEY: step6\n\n【审稿返工 — 本次运行的原因】")
    assert "- S11-B4（results，BLOCKING）：n" in step6
    assert "【审稿返工" in render("step5_full_solve.txt", "5")
    assert "【审稿返工" not in render("step9_paper_draft.txt", "9")

    # Once the revision it returns to has run, the block is gone.
    state = store.load()
    store.transition(
        expected_revision=state.revision, event_type="STEP_SUCCEEDED", changes={},
        payload={"stage": 8, "subtask": "revision"},
    )
    assert "【审稿返工" not in render("step6_sensitivity.txt", "6")


def test_v2_review_recompute_block_skips_steps_it_does_not_recompute(tmp_path):
    from pathlib import Path as _Path

    from factory_core.steps.prompting import PromptRenderer

    store = v2_store(tmp_path, last_completed_step=11)
    write_ledger(tmp_path, [("S11-B4", "results", "RESOLVED", "OPEN")])
    _review_reopen(store, steps=(6, 7, 8))
    renderer = PromptRenderer(_Path(__file__).resolve().parents[1])

    assert "【审稿返工" not in renderer.render(
        "step5_full_solve.txt", tmp_path, step_key="5", include_preamble=False, researcher_note=""
    )
    step6 = renderer.render(
        "step6_sensitivity.txt", tmp_path, step_key="6", include_preamble=False, researcher_note=""
    )
    assert "- S11-B4：台账中已不是 OPEN 的 BLOCKING/MAJOR 行" in step6


def test_v2_review_exit_loops_the_review_once_for_paper_issues(tmp_path):
    write_ledger(tmp_path, [("S11-B5", "paper", "BLOCKING", "OPEN")])
    registry, lifecycles = v2_registry()
    store = v2_store(tmp_path, last_completed_step=11)

    state = FactoryEngine(tmp_path, store=store, registry=registry).run()

    assert reopens(store) == []
    [loop] = [e for e in store.events() if e.type == "STEP_REOPENED"]
    assert loop.payload["rework_rule"] == "review_loop"
    assert loop.payload["resume_after_step"] == 10
    assert loop.payload["review_issues"] == ["S11-B5 (paper, BLOCKING)"]
    assert lifecycles[11].calls == [1] and lifecycles[12].calls == [1, 1]
    [failed] = failures(store)
    assert "after the review loop" in failed.payload["reason"]
    assert state.status is WorkflowStatus.FAILED


def test_v2_review_exit_passes_once_a_paper_loop_resolves_the_issue(tmp_path):
    rows = [("S11-B5", "paper", "BLOCKING", "OPEN")]
    write_ledger(tmp_path, rows)
    registry, _ = v2_registry(overrides={12: ResolvingRevision(rows)})
    store = v2_store(tmp_path, last_completed_step=11)

    state = FactoryEngine(tmp_path, store=store, registry=registry).run()

    assert failures(store) == []
    assert state.status is WorkflowStatus.COMPLETED


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
    opted, _ = service.create_project(
        "v2_demo", "q", start=False, stage_catalog_version=STAGE_CATALOG_VERSION_V2
    )
    assert default.stage_catalog_version == STAGE_CATALOG_VERSION
    assert opted.stage_catalog_version == STAGE_CATALOG_VERSION_V2
    with pytest.raises(ValueError, match="unsupported Stage catalog"):
        service.create_project("bad", "q", stage_catalog_version="factory-stage-catalog-v7")
    # Rejected before anything is written, so the name stays usable.
    assert not (tmp_path / "ongoing" / "bad").exists()
    retried, _ = service.create_project("bad", "q", start=False)
    assert retried.project_id == "bad"


def test_cli_create_accepts_the_v2_catalog():
    from factory_core.cli import build_parser

    args = build_parser().parse_args(["create", "demo", "q", "--stage-catalog", "v2"])
    assert args.stage_catalog == "v2"
    assert build_parser().parse_args(["create", "demo", "q"]).stage_catalog == "v1"


# -- prompts and projections -----------------------------------------------

def test_v2_prompt_overlay_matches_every_template_exactly_once():
    from pathlib import Path as _Path

    from factory_core.steps.catalog_prompts import V2_PROMPT_OVERLAY

    prompts = _Path(__file__).resolve().parents[1] / "prompts"
    for template, pairs in V2_PROMPT_OVERLAY.items():
        text = (prompts / template).read_text(encoding="utf-8")
        for old, new in pairs:
            assert text.count(old) == 1, (template, old[:40])
            assert old != new


@pytest.mark.parametrize("template", [
    "step10_gate1_numerical.txt", "step11_constructive_review.txt",
    "step12_revision.txt", "step14_abstract.txt", "step15_polish.txt",
])
def test_v1_prompts_are_untouched_and_v2_prompts_drop_the_precheck(tmp_path, template):
    from pathlib import Path as _Path

    from factory_core.steps.catalog_prompts import V2_PROMPT_OVERLAY
    from factory_core.steps.prompting import PromptRenderer

    renderer = PromptRenderer(_Path(__file__).resolve().parents[1])
    v1_project = tmp_path / "v1"
    SQLiteStateStore(v1_project).initialize(
        project_id="v1", project_type="modeling", scheduler_generation=STAGE_SCHEDULER_GENERATION
    )
    v2_project = tmp_path / "v2"
    v2_store(v2_project)
    step = template.removeprefix("step").split("_")[0]
    render = lambda project: renderer.render(  # noqa: E731
        template, project, step_key=step, include_preamble=False, researcher_note=""
    )

    v1, v2 = render(v1_project), render(v2_project)
    for old, new in V2_PROMPT_OVERLAY[template]:
        assert old in v1 and new not in v1  # v1 keeps the template text verbatim
        assert old not in v2 and new in v2
    assert "PRECHECK_PASS" not in v2
    assert "Step 13 数学预审或" not in v2


def test_v2_contest_phase_moves_step13_to_final_audit():
    from factory_core.contest import CONTEST_CATALOG_V2, phase_for_step

    assert CONTEST_CATALOG_V2 == STAGE_CATALOG_VERSION_V2
    assert phase_for_step(13).name == "review_and_revision"
    assert phase_for_step(13, catalog_version=STAGE_CATALOG_VERSION_V2).name == "final_audit_and_delivery"
    assert phase_for_step(14, catalog_version=STAGE_CATALOG_VERSION_V2).name == "review_and_revision"
    payload = CATALOG_V2.payload()
    judge = next(s for stage in payload["stages"] for s in stage["subtasks"] if s["key"] == "final_judge")
    assert judge["contest_phase"] == 8
    assert payload["schema_version"] == STAGE_CATALOG_VERSION_V2


@pytest.mark.parametrize("catalog, expected_owner", [
    (STAGE_CATALOG_VERSION_V2, 10),
    (STAGE_CATALOG_VERSION, 8),
])
def test_dirty_rebase_moves_a_persisted_paper_math_cause_only_in_v2(tmp_path, catalog, expected_owner):
    from factory_core.dirty_rebase import rebase_dirty_classifier_state
    from factory_core.storage import SCHEMA_VERSION

    store = SQLiteStateStore(tmp_path)
    store.initialize(project_id="demo", project_type="modeling", last_completed_step=12,
                     scheduler_generation=STAGE_SCHEDULER_GENERATION, stage_catalog_version=catalog)
    state = store.load()
    # A cause written without the engine's v2 remap (e.g. by an older worker).
    store.transition(
        expected_revision=state.revision, event_type="TEST_PERSISTED_PAPER_MATH", changes={},
        dirty_changes=[{
            "flag": "MATH_DIRTY", "owner_stage": 8, "cause_artifact": "demo_paper.tex",
            "baseline_fingerprint": "a" * 64, "current_fingerprint": "b" * 64,
            "classifier_contract_sha256": "0" * 64,
        }],
    )
    with store._session() as connection:
        rebase_dirty_classifier_state(
            connection, source_schema_version=SCHEMA_VERSION, target_schema_version=SCHEMA_VERSION
        )

    keys = {(flag["flag"], int(flag["owner_stage"])) for flag in store.dirty_flags()}
    assert ("MATH_DIRTY", expected_owner) in keys
    assert len([k for k in keys if k[0] == "MATH_DIRTY"]) == 1


def test_remaining_step_count_follows_the_catalog_cursor():
    # v1 counts exactly as before (16 - step).
    for step in range(17):
        assert CATALOG_V1.checkpoint_steps_after(step_id=step) == 16 - step
    # v2: after Step 15 the Judge and delivery remain, also while the
    # content-freeze gate (source Step 16) is the active subtask.
    assert CATALOG_V2.checkpoint_steps_after(step_id=15) == 2
    assert CATALOG_V2.checkpoint_steps_after(subtask_key="content_freeze_guard") == 2
    assert CATALOG_V2.checkpoint_steps_after(subtask_key="final_judge") == 1
    assert CATALOG_V2.checkpoint_steps_after(step_id=12) == 4


def test_final_judge_budget_is_one_contract_for_display_deadline_and_execution(tmp_path):
    from pathlib import Path

    from factory_core.stages import FINAL_JUDGE_SUBTASK, FINAL_JUDGE_TIMEOUT_SECONDS
    from factory_core.steps.catalog import contract_for
    from factory_core.steps.registry import build_native_registry

    # The Judge runs the Step-16 final audit, so it has the final-audit budget.
    assert FINAL_JUDGE_TIMEOUT_SECONDS == contract_for(16).timeout_seconds
    shown = next(
        subtask["step_budget"]
        for stage in CATALOG_V2.payload()["stages"]
        for subtask in stage["subtasks"]
        if subtask["key"] == FINAL_JUDGE_SUBTASK
    )
    executed = build_native_registry(Path(__file__).resolve().parents[1]).stage_subtask(FINAL_JUDGE_SUBTASK)
    assert shown["timeout_seconds"] == executed.timeout_seconds == FINAL_JUDGE_TIMEOUT_SECONDS
    assert (shown["max_attempts"], shown["max_reopens"]) == (executed.max_attempts, executed.max_reopens)
    # The override is published only as the budget: v1 subtask fields and
    # budgets stay as they were.
    v1_judge = next(
        subtask for stage in CATALOG_V1.payload()["stages"] for subtask in stage["subtasks"]
        if subtask["source_step_id"] == 13 and subtask["kind"] != "human_gate"
    )
    assert "budget_timeout_seconds" not in v1_judge
    assert v1_judge["step_budget"]["timeout_seconds"] == contract_for(13).timeout_seconds

    # The deadline cap starts from the same executed budget.
    store = SQLiteStateStore(tmp_path)
    store.initialize(project_id="demo", project_type="modeling", last_completed_step=15,
                     scheduler_generation=STAGE_SCHEDULER_GENERATION,
                     stage_catalog_version=STAGE_CATALOG_VERSION_V2)
    engine = FactoryEngine(tmp_path, store=store, registry=StepRegistry())
    assert engine._contest_timeout(executed) == FINAL_JUDGE_TIMEOUT_SECONDS


def test_no_rendered_v2_prompt_describes_the_v1_precheck(tmp_path):
    """Every active prompt, rendered for a v2 project, drops the v1 handoff.

    v2 has no Step-13 math precheck and no "final audit after Step 15":
    Step 13 is that final audit.  Scanning the rendered text (not the overlay
    table) catches a template that mentions the precheck but has no overlay.
    """

    from pathlib import Path as _Path

    from factory_core.steps.prompting import PromptRenderer

    root = _Path(__file__).resolve().parents[1]
    renderer = PromptRenderer(root)
    project = tmp_path / "v2"
    v2_store(project)
    templates = sorted((root / "prompts").glob("step*.txt")) + sorted((root / "prompts" / "judges").glob("*.txt"))
    assert {"step10_gate1_numerical.txt", "step11_constructive_review.txt"} <= {path.name for path in templates}
    v1_phrases = ("PRECHECK", "预审", "Step 15 后的 `final` 审计")
    for path in templates:
        template = str(path.relative_to(root / "prompts"))
        step = path.stem.removeprefix("step").split("_")[0]
        text = renderer.render(template, project, step_key=step, include_preamble=True, researcher_note="")
        # The Step-12 overlay states the absence on purpose.
        text = text.replace("本项目没有 Step 13 数学预审", "")
        leftover = [phrase for phrase in v1_phrases if phrase in text]
        assert not leftover, (template, leftover)


def test_contest_phases_reject_an_unknown_catalog_version():
    from factory_core.contest import (
        CONTEST_CATALOG_V1,
        CONTEST_CATALOG_V2,
        contest_phases_for,
        phase_for_step,
    )

    assert (CONTEST_CATALOG_V1, CONTEST_CATALOG_V2) == (STAGE_CATALOG_VERSION, STAGE_CATALOG_VERSION_V2)
    assert phase_for_step(13).id == phase_for_step(13, catalog_version=STAGE_CATALOG_VERSION).id == 7
    assert phase_for_step(13, catalog_version=STAGE_CATALOG_VERSION_V2).id == 8
    with pytest.raises(ValueError, match="unsupported Stage catalog"):
        phase_for_step(13, catalog_version="factory-stage-catalog-v7")
    with pytest.raises(ValueError, match="unsupported Stage catalog"):
        contest_phases_for("factory-stage-catalog-v7")


def _judge_reopen(store, project, *, verdict="REOPEN_REVISION_MODEL", target=3, aggregate_verdict=None):
    import json as _json

    math = {"schema_version": "judge-hard-role-v2", "role": "math", "verdict": "FAIL", "fatal_flaws": 1,
            "evidence": [{"ref_id": "math-e1", "claim": "移动坐标方程多出平流项", "finding": "违反干基守恒",
                          "quote": "+\\xi R'/R", "severity": "fatal"},
                         {"ref_id": "math-e2", "claim": "链式法则正确", "severity": "support"}]}
    paper = {"role": "paper", "verdict": "REVISE",
             "issues": [{"ref_id": "paper-issue-1", "severity": "major", "finding": "表述有误",
                         "recommendation": "改写", "quote": "原句"},
                        {"ref_id": "paper-issue-9", "severity": "minor", "finding": "小问题"}]}
    aggregate = {"verdict": aggregate_verdict or verdict, "roles": [
        {"role": "math", "status": "FAIL", "text": "VERDICT: FAIL\n" + _json.dumps(math, ensure_ascii=False)},
        {"role": "execution", "status": "PASS", "text": "VERDICT: PASS\n{}"},
        {"role": "paper", "status": "REVISE", "text": "VERDICT: REVISE\n" + _json.dumps(paper, ensure_ascii=False)}]}
    (project / "judge_outputs").mkdir(exist_ok=True)
    (project / "judge_outputs" / "aggregate.json").write_text(_json.dumps(aggregate, ensure_ascii=False))
    state = store.load()
    return store.transition(
        expected_revision=state.revision, event_type="STEP_REOPENED", changes={},
        payload={"stage": 10, "subtask": "final_judge", "source_step": 13,
                 "final_decision": verdict, "resume_after_step": target},
    )


def _render(project, template, step):
    from pathlib import Path as _Path

    from factory_core.steps.prompting import PromptRenderer

    return PromptRenderer(_Path(__file__).resolve().parents[1]).render(
        template, project, step_key=step, include_preamble=False, researcher_note=""
    )


def test_v2_final_judge_reopen_names_its_findings_until_the_judge_runs_again(tmp_path):
    store = v2_store(tmp_path, last_completed_step=15)
    assert "【最终评审返工" not in _render(tmp_path, "step4_model_construction.txt", "4")

    _judge_reopen(store, tmp_path)

    step4 = _render(tmp_path, "step4_model_construction.txt", "4")
    assert "【最终评审返工 — 本次运行的原因】" in step4
    assert "- [math math-e1，fatal] 移动坐标方程多出平流项；违反干基守恒（论文引文：+\\xi R'/R）" in step4
    assert "- [paper paper-issue-1，major] 表述有误；改写（论文引文：原句）" in step4
    assert "math-e2" not in step4 and "paper-issue-9" not in step4
    for template, step in (("step5_full_solve.txt", "5"), ("step9_paper_draft.txt", "9"),
                           ("step12_revision.txt", "12"), ("step15_polish.txt", "15")):
        assert "【最终评审返工" in _render(tmp_path, template, step), template

    state = store.load()
    store.transition(expected_revision=state.revision, event_type="STEP_STARTED", changes={},
                     payload={"stage": 10, "subtask": "final_judge"})
    assert "【最终评审返工" not in _render(tmp_path, "step4_model_construction.txt", "4")


def test_v2_final_judge_text_reopen_skips_steps_before_its_target(tmp_path):
    store = v2_store(tmp_path, last_completed_step=15)
    _judge_reopen(store, tmp_path, verdict="REOPEN_REVISION_TEXT", target=8)
    assert "【最终评审返工" not in _render(tmp_path, "step5_full_solve.txt", "5")
    assert "【最终评审返工" in _render(tmp_path, "step9_paper_draft.txt", "9")


def test_v2_final_judge_block_falls_back_when_findings_do_not_match(tmp_path):
    store = v2_store(tmp_path, last_completed_step=15)
    _judge_reopen(store, tmp_path, aggregate_verdict="PASS")
    step4 = _render(tmp_path, "step4_model_construction.txt", "4")
    assert "【最终评审返工" in step4 and "judge_evaluation.md" in step4
    assert "math-e1" not in step4


def test_v2_review_budget_restarts_after_a_final_judge_reopen():
    from types import SimpleNamespace as E

    events = [
        E(revision=10, type="STEP_RETRY_AUTHORIZED", payload={"stage": 8, "subtask": "revision"}),
        E(revision=12, type="STAGE_SEMANTIC_REOPENED", payload={"rework_rule": "review_issue"}),
    ]
    assert FactoryEngine._review_budget_baseline(events) == 10
    events.append(E(revision=20, type="STEP_REOPENED",
                    payload={"subtask": "final_judge", "final_decision": "REOPEN_REVISION_MODEL"}))
    assert FactoryEngine._review_budget_baseline(events) == 20
    # Other reopens (a review loop, a Step 13 precheck) do not restart it.
    events.append(E(revision=30, type="STEP_REOPENED",
                    payload={"subtask": "revision", "rework_rule": "review_loop"}))
    events.append(E(revision=31, type="STEP_REOPENED",
                    payload={"subtask": "final_judge", "final_decision": "INDETERMINATE_REVIEW"}))
    assert FactoryEngine._review_budget_baseline(events) == 20
