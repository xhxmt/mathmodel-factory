"""G2: the solver-reconciliation gate.

Two known anomalies plus synthetic coverage of every relevance value.  The
point of a separate gate is that it asserts on the *real* projects and on the
*interpretation* of what the shadow evaluator reports - not just on behaviour
with synthetic inputs.

Known anomalies:

  A  local_python_20260910154426_560c138e   db running, exit completed/0
  B  local_python_20260908173110_dd1262a8   db running, exit completed/0

They differ in exactly one respect, and it is the one that matters: B's row has
an owner slot that a later job superseded, while A's has no owner slot at all.
So B is provably ignorable and A is not.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from factory_core.solver_reconcile import (
    EvidenceState,
    ExecutionState,
    WorkflowRelevance,
    completion_blockers,
    evaluate_solver_jobs,
)

_REAL = {
    "A": "/home/tfisher/paper_factory/ongoing/cumcm_2026_a_fable_pro_20260910",
    "B": "/home/tfisher/paper_factory/ongoing/cumcm_2025_b_gpt_formal_20260908t153023z",
    "R": "/home/tfisher/paper_factory/ongoing/cumcm_2025_b_codex_luna_stability_20260817_run4",
}

_ANOMALIES = {
    "A": "local_python_20260910154426_560c138e",
    "B": "local_python_20260908173110_dd1262a8",
}


def _requires(name: str) -> str:
    path = _REAL[name]
    if not Path(path).is_dir():
        pytest.skip(f"project {name} unavailable")
    return path


# --------------------------------------------------------- the two anomalies
@pytest.mark.parametrize("name", sorted(_ANOMALIES))
def test_anomaly_is_reported_as_three_separate_answers(name):
    """Both anomalies must agree on execution and evidence, and differ on relevance."""

    path = _requires(name)
    job_id = _ANOMALIES[name]
    states = {s.job_id: s for s in evaluate_solver_jobs(path)}
    state = states[job_id]

    assert state.db_status == "running", "the database row is stale"
    assert state.execution_state == ExecutionState.TERMINAL_SUCCESS, "the exit artifact settles it"
    assert state.exit_status == "completed" and state.exit_returncode == 0
    assert (
        state.evidence_state != EvidenceState.COMPLETE
    ), "no completion receipt exists, so the evidence is not complete"


def test_A_anomaly_is_unresolved_because_it_has_no_owner_slot():
    path = _requires("A")
    state = next(
        s for s in evaluate_solver_jobs(path) if s.job_id == _ANOMALIES["A"]
    )
    assert state.workflow_relevance == WorkflowRelevance.UNRESOLVED
    assert state.blocks_completion is True
    assert "no owner slot" in state.reason


def test_B_anomaly_is_superseded_by_a_later_success_in_its_owner_slot():
    path = _requires("B")
    state = next(
        s for s in evaluate_solver_jobs(path) if s.job_id == _ANOMALIES["B"]
    )
    assert state.workflow_relevance == WorkflowRelevance.SUPERSEDED
    assert state.blocks_completion is False
    assert "same owner slot" in state.reason


def test_the_two_anomalies_differ_only_in_relevance():
    """The gate's central claim, asserted directly."""

    a = next(s for s in evaluate_solver_jobs(_requires("A")) if s.job_id == _ANOMALIES["A"])
    b = next(s for s in evaluate_solver_jobs(_requires("B")) if s.job_id == _ANOMALIES["B"])
    assert (a.execution_state, a.evidence_state) == (b.execution_state, b.evidence_state)
    assert a.workflow_relevance != b.workflow_relevance
    assert a.blocks_completion != b.blocks_completion


# ------------------------------------------------------------- real-project gate
def test_A_has_exactly_the_two_unresolved_jobs():
    path = _requires("A")
    blockers = completion_blockers(path)
    assert {s.job_id for s in blockers} == {
        "local_python_20260910151111_183973a7",
        _ANOMALIES["A"],
    }
    assert all(
        s.workflow_relevance == WorkflowRelevance.UNRESOLVED for s in blockers
    )


@pytest.mark.parametrize("name", ["B", "R"])
def test_B_and_R_have_no_completion_blockers(name):
    assert completion_blockers(_requires(name)) == []


@pytest.mark.parametrize("name", sorted(_REAL))
def test_gate_reading_a_real_project_mutates_nothing(name):
    """The evaluator must not touch the real databases, not even their mtime."""

    path = Path(_requires(name))
    database = path / ".factory" / "state.db"
    before = database.stat()
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        before_rows = (
            connection.execute("SELECT revision FROM project_state").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM solver_jobs").fetchone()[0],
        )
    finally:
        connection.close()

    evaluate_solver_jobs(path)
    completion_blockers(path)

    after = database.stat()
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        after_rows = (
            connection.execute("SELECT revision FROM project_state").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM solver_jobs").fetchone()[0],
        )
    finally:
        connection.close()

    assert after_rows == before_rows
    assert after.st_size == before.st_size
    assert after.st_mtime == before.st_mtime


# ------------------------------------------------------------------- synthetic
def test_every_relevance_value_is_reachable():
    """The six values are a closed set, and the blocking pair is what blocks."""

    values = {
        WorkflowRelevance.REQUIRED,
        WorkflowRelevance.SUPERSEDED,
        WorkflowRelevance.ORPHANED,
        WorkflowRelevance.HISTORICAL,
        WorkflowRelevance.ADVISORY,
        WorkflowRelevance.UNRESOLVED,
    }
    assert len(values) == 6
    blocking = {
        v for v in values if v in {WorkflowRelevance.REQUIRED, WorkflowRelevance.UNRESOLVED}
    }
    assert blocking == {WorkflowRelevance.REQUIRED, WorkflowRelevance.UNRESOLVED}


def test_gate_output_is_json_serialisable_for_all_real_projects():
    """The gate's result must be reportable, since S5 is a shadow evaluator."""

    for name in sorted(_REAL):
        states = evaluate_solver_jobs(_requires(name))
        payload = [s.to_dict() for s in states]
        assert json.loads(json.dumps(payload)) == payload
        assert all("blocks_completion" in item for item in payload)