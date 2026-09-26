"""Solver job reconciliation: three orthogonal questions (S5).

A single "is this job really done?" flag cannot be answered honestly, because
three independent questions get collapsed into it:

    execution_state      did the process finish, and how?
    evidence_state       is the two-stage receipt complete and valid?
    workflow_relevance   does the workflow still need this job?

A/B each have a residual row whose DB status says ``running`` while its exit
artifact says ``completed`` with returncode 0.  Treating the exit file as proof
of "done" would be wrong (the completion receipt is missing), and treating the DB
status as proof would be wrong too (it is merely stale).  Answering the three
questions separately lets both facts stand.

``workflow_relevance`` is the one that cannot always be answered.  A's residual
job has no owner slot at all (``owner_stage IS NULL`` and an
``stage-adhoc``/``subtask-adhoc`` attempt id), so nothing can be proven about it.
It is therefore reported as ``UNRESOLVED``, and ``UNRESOLVED`` blocks completion
exactly as ``REQUIRED`` does.  Only a positive proof - for example a later job
that succeeded in the *same* owner slot - permits a job to be ignored.  Guessing
"probably superseded" is what this module exists to avoid.

This is a **pure read**.  It never writes, never rewrites
``solver_jobs.status``, and never appends an event: reconciliation corrects the
effective view, not the history.  Persisting it is a later, deliberate step.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

SOLVER_RECONCILE_SCHEMA = "factory-solver-effective-state-v1"


class ExecutionState:
    RUNNING = "RUNNING"
    TERMINAL_SUCCESS = "TERMINAL_SUCCESS"
    TERMINAL_FAILURE = "TERMINAL_FAILURE"
    UNKNOWN = "UNKNOWN"


class EvidenceState:
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    INVALID = "INVALID"
    NOT_REQUIRED = "NOT_REQUIRED"


class WorkflowRelevance:
    REQUIRED = "REQUIRED"
    SUPERSEDED = "SUPERSEDED"
    ORPHANED = "ORPHANED"
    HISTORICAL = "HISTORICAL"
    ADVISORY = "ADVISORY"
    UNRESOLVED = "UNRESOLVED"


#: Only these two block a completion check.
BLOCKING_RELEVANCE = frozenset(
    {WorkflowRelevance.REQUIRED, WorkflowRelevance.UNRESOLVED}
)

#: Relevance values that require a positive proof before they may be used.
PROVEN_RELEVANCE = frozenset(
    {
        WorkflowRelevance.SUPERSEDED,
        WorkflowRelevance.ORPHANED,
        WorkflowRelevance.HISTORICAL,
        WorkflowRelevance.ADVISORY,
    }
)


@dataclass(frozen=True)
class SolverEffectiveState:
    job_id: str
    execution_state: str
    evidence_state: str
    workflow_relevance: str
    db_status: str
    exit_status: str | None = None
    exit_returncode: int | None = None
    receipt_ready: bool = False
    claim_limit: str | None = None
    reason: str = ""
    evidence_errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def blocks_completion(self) -> bool:
        return self.workflow_relevance in BLOCKING_RELEVANCE

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "execution_state": self.execution_state,
            "evidence_state": self.evidence_state,
            "workflow_relevance": self.workflow_relevance,
            "db_status": self.db_status,
            "exit_status": self.exit_status,
            "exit_returncode": self.exit_returncode,
            "receipt_ready": self.receipt_ready,
            "claim_limit": self.claim_limit,
            "reason": self.reason,
            "evidence_errors": list(self.evidence_errors),
            "blocks_completion": self.blocks_completion,
        }


def _exit_artifact(project: Path, job_id: str) -> dict | None:
    """Read the job's own exit record, if it has one."""

    import json

    path = Path(project) / ".factory" / "solver_jobs" / f"{job_id}.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _execution(job: dict, exit_record: dict | None) -> tuple[str, str]:
    """Answer only: did the process finish, and how?

    The exit artifact is the execution's own record, so it settles the question
    even when the database row is stale.  Without it, a ``running`` row cannot be
    distinguished from a live process - there is no per-job liveness signal - so
    the answer stays RUNNING rather than being guessed at.
    """

    if exit_record is not None:
        status = str(exit_record.get("status") or "").lower()
        returncode = exit_record.get("returncode")
        if status == "completed" and returncode == 0:
            return ExecutionState.TERMINAL_SUCCESS, "exit artifact reports completed/0"
        if status in {"completed", "failed"} or returncode is not None:
            return (
                ExecutionState.TERMINAL_FAILURE,
                f"exit artifact reports {status or 'failure'}/{returncode}",
            )
        return ExecutionState.UNKNOWN, f"exit artifact present but unreadable: {status!r}"

    db_status = str(job.get("status") or "").lower()
    if db_status == "running":
        return ExecutionState.RUNNING, "no exit artifact; db row says running"
    if db_status in {"completed", "failed"}:
        return (
            ExecutionState.UNKNOWN,
            f"db row says {db_status} but no exit artifact exists to confirm it",
        )
    return ExecutionState.UNKNOWN, f"unrecognised db status {db_status!r}"


def _evidence(
    project: Path, job: dict, events
) -> tuple[str, bool, str | None, tuple[str, ...]]:
    """Answer only: is the two-stage receipt complete and valid?

    Uses the same two helpers as ``cli.solver_evidence_payload`` - including its
    exact fail-closed shape (``receipt_ready=False``,
    ``claim_limit=LEGACY_JOB_METADATA_ONLY``) - rather than a second, parallel
    notion of "evidence complete", which would become a second authority.  The
    event stream is passed in because reading it per job is quadratic on a
    project with hundreds of jobs.
    """

    from scripts.solver_job_receipt import (
        ReceiptError,
        bind_event_stream,
        build_evidence,
        receipt_paths,
    )

    receipt_dir = project / ".factory" / "solver_receipts"
    submitted, completed = receipt_paths(receipt_dir, str(job["job_id"]))
    try:
        payload = bind_event_stream(
            build_evidence(
                project, submitted, completed if completed.is_file() else None
            ),
            events,
        )
    except (OSError, ReceiptError) as exc:
        ready = False
        errors = (f"MISSING_OR_INVALID_TWO_STAGE_RECEIPT: {exc}",)
        invalid = False
        claim_limit = "LEGACY_JOB_METADATA_ONLY"
        return (
            EvidenceState.INCOMPLETE if not invalid else EvidenceState.INVALID,
            ready,
            claim_limit,
            errors,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return EvidenceState.INVALID, False, None, (f"{type(exc).__name__}: {exc}",)

    ready = bool(payload.get("receipt_ready"))
    errors = tuple(str(e) for e in (payload.get("errors") or ()))
    claim_limit = payload.get("claim_limit")
    if ready:
        return EvidenceState.COMPLETE, True, claim_limit, errors

    # A malformed receipt is a different failure from an absent one: the former
    # must not be retried blindly, the latter is simply not yet produced.
    invalid = any("MISSING_OR_INVALID" not in e for e in errors)
    state = EvidenceState.INVALID if invalid else EvidenceState.INCOMPLETE
    return state, False, claim_limit, errors


def _relevance(
    job: dict,
    *,
    siblings: list[dict],
    project_state_active: tuple[int | None, str | None],
    project_cursor: tuple[int | None, str | None],
    execution_state: str,
) -> tuple[str, str]:
    """Answer only: does the workflow still need this job?

    A job may be ignored **only** on a positive proof.  Two are implemented, both
    read from committed state rather than inferred:

      SUPERSEDED  a later job (higher ``job_revision``) in the same owner slot
                  reached terminal success
      HISTORICAL  the job is terminal and the project's committed cursor has
                  already passed the stage that owned it
      ADVISORY    the job is terminal and the cursor has completed its own stage

    Anything else is UNRESOLVED - including a job with no owner slot at all,
    which is precisely the case where a guess would be tempting and wrong.
    """

    owner_stage = job.get("owner_stage")
    owner_subtask = job.get("owner_subtask")
    if owner_stage is None and owner_subtask is None:
        return (
            WorkflowRelevance.UNRESOLVED,
            "no owner slot recorded, so nothing can be proven about this job",
        )

    revision = int(job.get("job_revision") or 0)
    for other in siblings:
        if other.get("job_id") == job.get("job_id"):
            continue
        if (
            other.get("owner_stage") != owner_stage
            or other.get("owner_subtask") != owner_subtask
        ):
            continue
        if int(other.get("job_revision") or 0) <= revision:
            continue
        if str(other.get("status") or "").lower() != "completed":
            continue
        return (
            WorkflowRelevance.SUPERSEDED,
            f"later job {other.get('job_id')} succeeded in the same owner slot "
            f"(revision {other.get('job_revision')} > {revision})",
        )

    active_stage, active_subtask = project_state_active
    # Whether the job can still be *pending* is decided by the reconciled
    # execution state, not by the database row: a stale ``running`` row whose exit
    # artifact reports success is not pending work, and treating it as pending
    # would report REQUIRED for a job that has already finished.
    proven_terminal = execution_state in (
        ExecutionState.TERMINAL_SUCCESS,
        ExecutionState.TERMINAL_FAILURE,
    )
    is_terminal = proven_terminal
    if active_stage is not None and is_terminal is False and (
        int(owner_stage) == int(active_stage)
        and (active_subtask is None or owner_subtask == active_subtask)
    ):
        return (
            WorkflowRelevance.REQUIRED,
            f"owner slot is the project's active position (stage {active_stage}) "
            "and the job has not reached a terminal state",
        )

    # Positive proof of historicity: the project's own committed cursor has
    # already passed the stage that owned this job, so the job's contribution is
    # behind the workflow's progress rather than pending work.  This is evidence,
    # not a guess - it is read from project_state, which only advances through
    # committed checkpoints.
    completed_stage, _completed_subtask = project_cursor
    if (
        completed_stage is not None
        and completed_stage > int(owner_stage)
        and is_terminal is True
    ):
        return (
            WorkflowRelevance.HISTORICAL,
            f"terminal job whose owning stage {owner_stage} is behind the "
            f"committed cursor (last completed stage {completed_stage})",
        )
    if (
        completed_stage is not None
        and completed_stage == int(owner_stage)
        and is_terminal is True
    ):
        return (
            WorkflowRelevance.ADVISORY,
            f"terminal job in the stage the cursor has already completed "
            f"(stage {owner_stage}); it explains the run but is not pending work",
        )

    return (
        WorkflowRelevance.UNRESOLVED,
        "no positive proof of supersession, orphanhood or historicity",
    )


def db_status_is_terminal(job: dict) -> bool | None:
    """The database's own claim about termination, or None when it says nothing.

    Exposed for diagnostics only.  It is deliberately **not** used to decide
    whether a job is pending: a stale ``running`` row with a successful exit
    artifact is not pending work, and relevance now reads the reconciled
    execution state instead.
    """

    status = str(job.get("status") or "").lower()
    if status in {"completed", "failed"}:
        return True
    if status == "running":
        return False
    return None


def evaluate_solver_jobs(project_dir) -> list[SolverEffectiveState]:
    """Effective state for every solver job, in job order.  Pure read."""

    from .storage import SQLiteStateStore

    project = Path(project_dir).resolve()
    store = SQLiteStateStore(project)
    jobs = store.solver_jobs()
    events = store.events()          # read once, not once per job
    try:
        state = store.load()
        active = (state.active_stage, state.active_subtask)
        cursor = (state.last_completed_stage, None)
    except Exception:  # pragma: no cover - defensive
        active = (None, None)
        cursor = (None, None)
    return [
        evaluate_solver_job(
            project, job, siblings=jobs, events=events,
            project_state_active=active, project_cursor=cursor,
        )
        for job in jobs
    ]


def evaluate_solver_job(
    project_dir,
    job: dict,
    *,
    siblings: list[dict],
    events=None,
    project_state_active: tuple[int | None, str | None] = (None, None),
    project_cursor: tuple[int | None, str | None] = (None, None),
) -> SolverEffectiveState:
    """Effective state for one job.  Pure read: no writes, no events appended."""

    project = Path(project_dir).resolve()
    job_id = str(job.get("job_id"))
    if events is None:
        from .storage import SQLiteStateStore as _Store

        events = _Store(project).events()
    exit_record = _exit_artifact(project, job_id)
    execution, execution_reason = _execution(job, exit_record)
    evidence, ready, claim_limit, errors = _evidence(project, job, events)
    relevance, relevance_reason = _relevance(
        job, siblings=list(siblings), project_state_active=project_state_active,
        project_cursor=project_cursor, execution_state=execution,
    )
    exit_status = None
    exit_returncode = None
    if exit_record is not None:
        exit_status = str(exit_record.get("status") or "") or None
        exit_returncode = exit_record.get("returncode")

    return SolverEffectiveState(
        job_id=job_id,
        execution_state=execution,
        evidence_state=evidence,
        workflow_relevance=relevance,
        db_status=str(job.get("status") or ""),
        exit_status=exit_status,
        exit_returncode=(
            int(exit_returncode) if isinstance(exit_returncode, int) else None
        ),
        receipt_ready=ready,
        claim_limit=claim_limit,
        reason="; ".join(filter(None, (execution_reason, relevance_reason))),
        evidence_errors=errors,
    )


def completion_blockers(project_dir) -> list[SolverEffectiveState]:
    """Jobs that must stop a completion check from passing.

    Callers asking "may this project be completed?" use this and get both the
    jobs that are still required and the jobs whose relevance cannot be proven.
    """

    return [s for s in evaluate_solver_jobs(project_dir) if s.blocks_completion]