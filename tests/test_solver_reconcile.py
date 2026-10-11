"""S5: solver reconciliation across three orthogonal questions.

The point of the split is that one "is this job done?" flag cannot answer all of:

    execution_state      did the process finish, and how?
    evidence_state       is the two-stage receipt complete and valid?
    workflow_relevance   does the workflow still need this job?

A stale ``running`` database row with a ``completed`` exit artifact is the case
that forces the split: the execution did finish, the evidence is missing, and the
relevance may or may not be provable.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from factory_core.solver_reconcile import (
    BLOCKING_RELEVANCE,
    PROVEN_RELEVANCE,
    EvidenceState,
    ExecutionState,
    SolverEffectiveState,
    WorkflowRelevance,
    completion_blockers,
    evaluate_solver_job,
    evaluate_solver_jobs,
)
from factory_core.storage import SQLiteStateStore

import _gate_projects

_JOB = {
    "job_id": "job-under-test",
    "job_revision": 2,
    "owner_stage": 4,
    "owner_subtask": "solve",
    "owner_revision": 10,
    "attempt_id": "stage-4:subtask-solve:step-5:attempt-1",
    "backend": "local",
    "runtime": "python",
    "script": "solve.py",
    "workdir": ".",
    "argv_json": "[]",
    "max_time_seconds": 60,
    "external_id": None,
    "status": "running",
    "requested_at": 1,
    "started_at": 1,
    "finished_at": None,
    "result_refs_json": "[]",
    "failure_json": None,
}


def _project(tmp_path) -> Path:
    root = tmp_path / "s5"
    root.mkdir()
    SQLiteStateStore(root).initialize(project_id="s5", project_type="modeling")
    return root


def _write_exit(root: Path, job_id: str, *, status: str, returncode: int) -> None:
    directory = root / ".factory" / "solver_jobs"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{job_id}.json").write_text(
        json.dumps({"status": status, "returncode": returncode, "finished_at": 2}),
        encoding="utf-8",
    )


def _eval(
    root, job, *, siblings=(), active=(None, None), cursor=(None, None),
    sibling_execution=None,
):
    return evaluate_solver_job(
        root, job, siblings=list(siblings),
        project_state_active=active, project_cursor=cursor,
        sibling_execution=sibling_execution,
    )


# --------------------------------------------------------- the three questions
def test_stale_running_row_with_completed_exit_splits_the_answers(tmp_path):
    """The exact A/B case: execution settled, evidence missing, relevance separate."""

    root = _project(tmp_path)
    _write_exit(root, _JOB["job_id"], status="completed", returncode=0)

    state = _eval(root, _JOB, cursor=(9, None))

    assert state.db_status == "running"
    assert state.execution_state == ExecutionState.TERMINAL_SUCCESS
    assert state.evidence_state != EvidenceState.COMPLETE, "no receipt exists yet"
    assert state.exit_status == "completed" and state.exit_returncode == 0


def test_running_row_without_exit_stays_running_not_guessed(tmp_path):
    root = _project(tmp_path)
    state = _eval(root, _JOB)
    assert state.execution_state == ExecutionState.RUNNING
    assert "no exit artifact" in state.reason


def test_exit_failure_is_terminal_failure(tmp_path):
    root = _project(tmp_path)
    _write_exit(root, _JOB["job_id"], status="failed", returncode=2)
    state = _eval(root, _JOB, cursor=(9, None))
    assert state.execution_state == ExecutionState.TERMINAL_FAILURE
    assert state.exit_returncode == 2


def test_db_terminal_without_exit_is_unknown_not_trusted(tmp_path):
    """A database claim with no execution artifact is not evidence."""

    root = _project(tmp_path)
    job = {**_JOB, "status": "completed"}
    state = _eval(root, job)
    assert state.execution_state == ExecutionState.UNKNOWN
    assert "no exit artifact" in state.reason


# ------------------------------------------------------------------- relevance
def test_ownerless_job_is_unresolved_and_blocks(tmp_path):
    """No owner slot means nothing can be proven - the case a guess would ruin."""

    root = _project(tmp_path)
    job = {
        **_JOB,
        "owner_stage": None,
        "owner_subtask": None,
        "attempt_id": "stage-adhoc:subtask-adhoc:step-None:attempt-0",
    }
    _write_exit(root, job["job_id"], status="completed", returncode=0)
    state = _eval(root, job, cursor=(9, None))

    assert state.workflow_relevance == WorkflowRelevance.UNRESOLVED
    assert state.blocks_completion is True


def test_superseded_requires_a_later_success_in_the_same_slot(tmp_path):
    """A later job that really finished supersedes this one."""

    root = _project(tmp_path)
    later = {**_JOB, "job_id": "later", "job_revision": 3, "status": "completed"}
    _write_exit(root, "later", status="completed", returncode=0)
    state = _eval(root, _JOB, siblings=[_JOB, later], cursor=(3, None))
    assert state.workflow_relevance == WorkflowRelevance.SUPERSEDED
    assert state.blocks_completion is False
    assert "later" in state.reason
    assert "exit artifact" in state.reason


def test_a_completed_row_without_an_exit_artifact_proves_nothing(tmp_path):
    """The row is a claim; only the execution's own record settles it.

    This is the hole the module's own ``_execution`` already refuses to fall into:
    it answers UNKNOWN with "db row says completed but no exit artifact exists to
    confirm it".  The supersede proof used to accept that same row, so one proof
    contradicted the other and a job could be dropped on a status column alone.
    """

    root = _project(tmp_path)
    later = {**_JOB, "job_id": "later", "job_revision": 3, "status": "completed"}
    # deliberately no exit artifact for "later"
    state = _eval(root, _JOB, siblings=[_JOB, later], cursor=(3, None))

    assert state.workflow_relevance == WorkflowRelevance.UNRESOLVED
    assert state.blocks_completion is True
    assert "no owner slot" not in state.reason


def test_a_superseding_failure_does_not_prove_supersession(tmp_path):
    """Terminal is not enough - it has to be terminal success."""

    root = _project(tmp_path)
    later = {**_JOB, "job_id": "later", "job_revision": 3, "status": "completed"}
    _write_exit(root, "later", status="failed", returncode=2)
    state = _eval(root, _JOB, siblings=[_JOB, later], cursor=(3, None))

    assert state.workflow_relevance == WorkflowRelevance.UNRESOLVED


def test_a_supplied_sibling_map_is_honoured(tmp_path):
    """The batch entry point resolves the map once and passes it down.

    Pinned here directly so the parameter cannot be dropped without a failure.
    ``evaluate_solver_jobs`` builds it from every job's exit artifact in one pass,
    which is why the real-history assertions in ``test_gate_g2_solver`` - B's
    stale rows staying SUPERSEDED - still hold.
    """

    from factory_core.solver_reconcile import ExecutionState

    root = _project(tmp_path)
    later = {**_JOB, "job_id": "later", "job_revision": 3, "status": "completed"}
    state = _eval(
        root, _JOB, siblings=[_JOB, later], cursor=(3, None),
        sibling_execution={"later": ExecutionState.TERMINAL_SUCCESS},
    )

    assert state.workflow_relevance == WorkflowRelevance.SUPERSEDED
    assert state.blocks_completion is False


def test_a_supplied_map_that_is_not_success_still_refuses(tmp_path):
    """Supplying the map does not weaken the proof."""

    from factory_core.solver_reconcile import ExecutionState

    root = _project(tmp_path)
    later = {**_JOB, "job_id": "later", "job_revision": 3, "status": "completed"}
    _write_exit(root, "later", status="completed", returncode=0)
    state = _eval(
        root, _JOB, siblings=[_JOB, later], cursor=(3, None),
        sibling_execution={"later": ExecutionState.UNKNOWN},
    )

    assert state.workflow_relevance == WorkflowRelevance.UNRESOLVED


def test_earlier_or_failed_sibling_does_not_prove_supersession(tmp_path):
    root = _project(tmp_path)
    earlier = {**_JOB, "job_id": "earlier", "job_revision": 1, "status": "completed"}
    failed_later = {**_JOB, "job_id": "failed-later", "job_revision": 3, "status": "failed"}

    for sibling in (earlier, failed_later):
        state = _eval(root, _JOB, siblings=[_JOB, sibling], cursor=(3, None))
        assert state.workflow_relevance != WorkflowRelevance.SUPERSEDED, sibling["job_id"]


def test_historicity_comes_from_the_committed_cursor(tmp_path):
    """Historicity needs *both* the cursor proof and a proven terminal execution.

    The database's ``completed`` claim is not enough on its own - that is the
    point of the split - so the exit artifact is required here too.
    """

    root = _project(tmp_path)
    job = {**_JOB, "status": "completed"}
    _write_exit(root, job["job_id"], status="completed", returncode=0)
    state = _eval(root, job, cursor=(9, None))
    assert state.execution_state == ExecutionState.TERMINAL_SUCCESS
    assert state.workflow_relevance == WorkflowRelevance.HISTORICAL
    assert state.blocks_completion is False


def test_db_completed_without_execution_evidence_proves_nothing(tmp_path):
    """Contrast with the test above: the same row, no exit artifact, no proof."""

    root = _project(tmp_path)
    job = {**_JOB, "status": "completed"}
    state = _eval(root, job, cursor=(9, None))
    assert state.execution_state == ExecutionState.UNKNOWN
    assert state.workflow_relevance == WorkflowRelevance.UNRESOLVED
    assert state.blocks_completion is True


def test_advisory_when_the_cursor_completed_the_owning_stage(tmp_path):
    root = _project(tmp_path)
    job = {**_JOB, "status": "completed"}
    _write_exit(root, job["job_id"], status="completed", returncode=0)
    state = _eval(root, job, cursor=(4, None))
    assert state.workflow_relevance == WorkflowRelevance.ADVISORY
    assert state.blocks_completion is False


def test_required_when_the_job_is_the_active_position_and_not_terminal(tmp_path):
    root = _project(tmp_path)
    state = _eval(root, _JOB, active=(4, "solve"))
    assert state.workflow_relevance == WorkflowRelevance.REQUIRED
    assert state.blocks_completion is True


def test_a_terminal_job_in_the_active_slot_is_not_required(tmp_path):
    root = _project(tmp_path)
    _write_exit(root, _JOB["job_id"], status="completed", returncode=0)
    state = _eval(root, _JOB, active=(4, "solve"), cursor=(3, None))
    assert state.workflow_relevance != WorkflowRelevance.REQUIRED


def test_blocking_and_proven_sets_do_not_overlap():
    assert BLOCKING_RELEVANCE & PROVEN_RELEVANCE == frozenset()
    assert BLOCKING_RELEVANCE == {
        WorkflowRelevance.REQUIRED, WorkflowRelevance.UNRESOLVED
    }


# ------------------------------------------------------------------- pure read
def test_reconciliation_never_writes(tmp_path):
    """No event, no revision move, and solver_jobs.status is never rewritten."""

    root = _project(tmp_path)
    _write_exit(root, _JOB["job_id"], status="completed", returncode=0)
    database = root / ".factory" / "state.db"

    def snapshot():
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            return (
                connection.execute("SELECT revision FROM project_state").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM solver_jobs").fetchone()[0],
            )
        finally:
            connection.close()

    before = snapshot()
    for _ in range(3):
        evaluate_solver_jobs(root)
        completion_blockers(root)
    assert snapshot() == before


def test_evaluation_is_deterministic(tmp_path):
    root = _project(tmp_path)
    _write_exit(root, _JOB["job_id"], status="completed", returncode=0)
    first = [s.to_dict() for s in evaluate_solver_jobs(root)]
    second = [s.to_dict() for s in evaluate_solver_jobs(root)]
    assert first == second


def test_effective_state_serialises_without_dataclass_junk(tmp_path):
    root = _project(tmp_path)
    state = _eval(root, _JOB)
    payload = state.to_dict()
    assert set(payload) == {
        "job_id", "execution_state", "evidence_state", "workflow_relevance",
        "db_status", "exit_status", "exit_returncode", "receipt_ready",
        "claim_limit", "reason", "evidence_errors", "blocks_completion",
    }
    assert json.loads(json.dumps(payload)) == payload


# -------------------------------------------------- real-history regression
#: The rule-level coverage above is hermetic (``tmp_path``); this layer re-asserts
#: the answers recorded on the machine that produced the projects, and skips where
#: the trees are absent.  Point it elsewhere with ``PF_GATE_PROJECTS_ROOT``.
@pytest.mark.parametrize("name", sorted(_gate_projects.PROJECTS))
def test_real_history_regression(tmp_path, name):
    """The real projects must land on the answers established when S5 was built.

    A: two ownerless jobs whose relevance cannot be proven -> UNRESOLVED.
    B: the residual stale ``running`` row is provably SUPERSEDED -> no blocker.
    R: every job is behind the committed cursor -> no blocker.
    """

    # a copy: evaluate_solver_jobs opens a store, and any read path migrates a
    # generation-9 database in place
    states = evaluate_solver_jobs(_gate_projects.snapshot(name, tmp_path))
    blockers = [s for s in states if s.blocks_completion]
    assert states, name

    if name == "A":
        assert len(blockers) == 2, [s.job_id for s in blockers]
        assert all(
            s.workflow_relevance == WorkflowRelevance.UNRESOLVED for s in blockers
        )
        stale = next(
            s for s in states if s.job_id == "local_python_20260910154426_560c138e"
        )
        assert stale.db_status == "running"
        assert stale.execution_state == ExecutionState.TERMINAL_SUCCESS
        assert stale.evidence_state != EvidenceState.COMPLETE
    elif name == "B":
        assert blockers == [], [s.job_id for s in blockers]
        residual = next(
            s for s in states if s.job_id == "local_python_20260908173110_dd1262a8"
        )
        assert residual.db_status == "running"
        assert residual.execution_state == ExecutionState.TERMINAL_SUCCESS
        assert residual.workflow_relevance == WorkflowRelevance.SUPERSEDED
    else:
        assert blockers == [], [s.job_id for s in blockers]