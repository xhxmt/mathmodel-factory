"""Phase 4: a change re-runs a Step only when a completed computation Step consumed it."""

from __future__ import annotations

import pytest

from factory_core.input_dependency_policy import (
    classify_input,
    recompute_start,
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
        # Data feeds the solve.
        ("data/final/instance.json", 4, "data", 5),
        # Results: main results feed sensitivity, sensitivity feeds evaluation.
        ("results/canonical_results.json", 4, "result", 6),
        ("results/problem1/values.json", 4, "result", 6),
        ("results/sensitivity/summary.json", 5, "result", 7),
        ("results/derived_artifacts.json", 9, "result", None),
        # Machine evidence is never an input.
        (".factory/solver_receipts/job-1/completion.json", 4, "evidence", None),
        ("run_state/solver_jobs/job-1.json", 4, "evidence", None),
        ("models/m1/03_solve.py.log", 5, "evidence", None),
        ("results/problem1/solver.log", 4, "evidence", None),
    ],
)
def test_classification(artifact, owner, kind, consumer):
    dependency = classify_input(artifact, baseline="abc123", v1_owner_stage=owner)
    assert (dependency.kind, dependency.consumer_step) == (kind, consumer)


def test_a_new_file_was_never_consumed():
    dependency = classify_input("models/m1/03_solve.py", baseline="MISSING", v1_owner_stage=3)
    assert (dependency.kind, dependency.consumer_step) == ("new_file", None)


def _start(*changes, completed=ALL_COMPUTATION):
    dependencies = [
        classify_input(artifact, baseline="abc123", v1_owner_stage=owner)
        for artifact, owner in changes
    ]
    return recompute_start(dependencies, completed_steps=completed)


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
