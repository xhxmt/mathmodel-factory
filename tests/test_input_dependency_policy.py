"""Phase 4: a change re-runs a Step only when a completed computation Step consumed it."""

from __future__ import annotations

import pytest

from factory_core.input_dependency_policy import (
    apply_to_record,
    classify_input,
    recompute_steps,
)

ALL_COMPUTATION = {5, 6, 7, 8}


@pytest.mark.parametrize(
    ("artifact", "owner", "kind", "consumer"),
    [
        # Documents: forward repair whatever their owner.
        ("symbol_table.md", 3, "document", None),
        ("model.md", 3, "document", None),
        ("problem/problem_plan.json", 1, "document", None),
        ("claim_registry.json", 3, "document", None),
        ("result1.xlsx", 4, "document", None),
        ("tables/main.tex", 9, "document", None),
        ("figures/sensitivity_tornado.pdf", 6, "document", None),
        ("@paper:demo_paper.tex:math", 8, "document", None),
        # Code: the computation Step that executes it.
        ("models/m1/03_solve.py", 3, "code", 5),
        ("scripts/step5/run.py", 4, "code", 5),
        ("models/m1/05_sensitivity.py", 5, "code", 6),
        ("models/m1/06_figures.py", 6, "code", 8),
        ("models/m1/07_bootstrap_convergence.py", 8, "code", None),
        # Data feeds the solve, wherever it sits under data/.
        ("data/final/instance.json", 4, "data", 5),
        ("data/external/rates.csv", 4, "data", 5),
        # Results: main results feed sensitivity, sensitivity feeds evaluation.
        ("results/canonical_results.json", 4, "result", 6),
        ("results/problem1/values.json", 4, "result", 6),
        ("results/sensitivity/summary.json", 5, "result", 7),
        ("results/derived_artifacts.json", 9, "result", None),
        # The solve's quick-look plots are what visualization redraws.
        ("results/problem1/plots.pdf", 4, "result", 8),
        # Machine evidence is never an input.
        (".factory/solver_receipts/job-1/completion.json", 4, "evidence", None),
        ("run_state/solver_jobs/job-1.json", 4, "evidence", None),
        ("models/m1/03_solve.py.log", 5, "evidence", None),
        ("results/problem1/solver.log", 4, "evidence", None),
        # Solver diagnostics filed under results/ are not sensitivity inputs.
        ("results/problem1/bound.json", 4, "evidence", None),
        ("results/problem1/convergence.json", 4, "evidence", None),
        ("results/problem1/cross_check.json", 4, "evidence", None),
        ("results/problem1/solver_evidence.json", 4, "evidence", None),
    ],
)
def test_classification(artifact, owner, kind, consumer):
    dependency = classify_input(artifact, baseline="abc123", v1_owner_stage=owner)
    assert (dependency.kind, dependency.consumer_step) == (kind, consumer)


def test_a_new_file_was_never_consumed():
    dependency = classify_input("models/m1/03_solve.py", baseline="MISSING", v1_owner_stage=3)
    assert (dependency.kind, dependency.consumer_step) == ("new_file", None)


def _start(*changes, completed=ALL_COMPUTATION):
    """Where the engine restarts: the earliest ``input_consumer_step`` that
    ``apply_to_record`` records on the change records (engine.py takes the min)."""
    records = [
        {"owner_stage": owner, "cause_artifact": artifact, "baseline_fingerprint": "abc123"}
        for artifact, owner in changes
    ]
    for record in records:
        apply_to_record(record, initiator_stage=8, completed_steps=completed, stage_of_step=lambda step: step)
    consumers = [record["input_consumer_step"] for record in records if "input_consumer_step" in record]
    return min(consumers) if consumers else None


def test_a_document_edit_in_revision_recomputes_nothing():
    assert _start(("symbol_table.md", 3), ("model.md", 3), ("evaluation.md", 5)) is None


def test_a_code_edit_in_revision_recomputes_from_the_solve():
    start = _start(("models/m1/03_solve.py", 3), ("symbol_table.md", 3))
    assert start == 5
    assert recompute_steps(start) == (5, 6, 7, 8)


def test_a_result_edit_in_revision_recomputes_from_sensitivity():
    start = _start(("results/canonical_results.json", 4))
    assert start == 6
    assert recompute_steps(start) == (6, 7, 8)


def test_the_solve_fixing_the_code_step_4_wrote_recomputes_nothing():
    # Step 5 is the consumer of model code, and it has not completed yet.
    assert _start(("models/m1/03_solve.py", 3), completed=set()) is None


def test_sensitivity_editing_solve_code_recomputes_the_solve():
    assert _start(("models/m1/03_solve.py", 3), completed={5}) == 5


@pytest.mark.parametrize(
    ("completed", "start", "steps"),
    [
        (ALL_COMPUTATION, 5, (5, 6, 7, 8)),  # the earliest consumer wins
        ({5, 6}, 5, (5, 6, 7, 8)),
        ({6}, 6, (6, 7, 8)),  # the solve has not run: only the result consumer counts
    ],
)
def test_the_earliest_completed_consumer_starts_the_recompute(completed, start, steps):
    found = _start(("models/m1/03_solve.py", 3), ("results/canonical_results.json", 4), completed=completed)
    assert found == start
    assert recompute_steps(found) == steps


@pytest.mark.parametrize("source", ["fallback", "explicit_fail_closed", "protected", "bespoke_recovery"])
def test_fail_closed_records_keep_their_owner(source):
    record = {
        "flag": "RESULT_DIRTY",
        "owner_stage": 4,
        "cause_artifact": "notes.md",
        "baseline_fingerprint": "abc",
        "classification_source": source,
    }
    assert apply_to_record(record, initiator_stage=8, completed_steps=ALL_COMPUTATION, stage_of_step=lambda s: s - 1) is None
    assert record["owner_stage"] == 4 and "v2_owner_rule" not in record


def test_an_upstream_document_becomes_a_forward_repair_and_a_consumed_input_its_consumer():
    stage_of = {5: 4, 6: 5, 7: 5, 8: 6}.get
    document = {"flag": "MODEL_DIRTY", "owner_stage": 3, "cause_artifact": "symbol_table.md",
                "baseline_fingerprint": "abc", "classification_source": "frozen_rule"}
    code = {"flag": "MODEL_DIRTY", "owner_stage": 3, "cause_artifact": "models/m1/03_solve.py",
            "baseline_fingerprint": "abc", "classification_source": "frozen_rule"}
    apply_to_record(document, initiator_stage=8, completed_steps=ALL_COMPUTATION, stage_of_step=stage_of)
    apply_to_record(code, initiator_stage=8, completed_steps=ALL_COMPUTATION, stage_of_step=stage_of)
    assert (document["owner_stage"], document["v2_owner_rule"]) == (8, "v2_input_forward_document")
    assert (code["owner_stage"], code["v2_owner_rule"], code["input_consumer_step"]) == (4, "v2_input_consumer_code", 5)
