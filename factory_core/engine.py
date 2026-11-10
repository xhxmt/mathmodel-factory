from __future__ import annotations

import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from .adapters.infrastructure.process import _process_identity
from .contest import ContestDeadlineExceeded, ContestPolicy, effective_timeout
from .deadline import deadline_scope, ensure_deadline
from .current_dirty import (
    DirtyFlag,
    capture_artifact_manifest,
    classifier_contract_sha256,
    classify_manifest_changes,
    manifest_fingerprint,
    semantic_flags,
    solver_receipt_job_id,
)
from .bounded_run import (
    BoundedRunContract,
    BoundedRunError,
    BoundedRunResult,
    CheckpointVerification,
    ProtectedCheckpointViolation,
    ProtectedManifestViolation,
    ScopedRegistry,
    check_cursor,
    snapshot_checkpoints,
    verify_checkpoints,
    verify_protected_manifest,
)
from .dirty_classification import classification_sources, source_for
from .liveness import pid_is_live
from .domain import (
    ExecutionResult,
    InvalidTransition,
    PendingAction,
    RecoveryDisposition,
    RevisionConflict,
    RunnerBusy,
    RunnerLeaseLost,
    StepError,
    StepContext,
    TERMINAL_STATUSES,
    ValidationResult,
    WorkflowEvent,
    WorkflowState,
    WorkflowStatus,
)
from .execution_pipeline import StageExecutionPipeline, StageExecutionRequest
from .human_decisions import build_decision_request, validate_resolution
from .registry import StepDefinition, StepRegistry
from .storage import SQLiteStateStore
from .stages import (
    STAGE_CATALOG_VERSION,
    STAGE_CATALOG_VERSION_V2,
    FINAL_JUDGE_SUBTASK,
    STAGE_SCHEDULER_GENERATION,
    STEP_SCHEDULER_GENERATION,
    SUPPORTED_STAGE_CATALOG_VERSIONS,
    StageCatalog,
    catalog_for_version,
    completed_stage_for_step,
    stage_for_id,
)
from .transitions import TransitionCoordinator


# A model process that failed (non-zero exit, timeout, unusable route).  The
# validator then reports only what is missing, so the failure reason must
# name the backend first.
BACKEND_FAILURE_ERROR_CLASSES = frozenset(
    {
        "TRANSIENT_MODEL_BACKEND",
        "TRANSIENT_TIMEOUT",
        "PERMANENT_BACKEND_UNAVAILABLE",
        "PERMANENT_MODEL_UNSUPPORTED",
        "PERMANENT_MODEL_CONFIG",
    }
)


# Failures that say a check or harness produced no verdict at all.  They are
# not evidence about the content, so they never consume one of the Step's
# content attempts (``max_attempts``).  Each class has its own budget of
# INFRASTRUCTURE_RETRY_BUDGET retries per Step attempt sequence (an audit
# crash does not spend the backend's retries) and maps to the terminal class
# recorded once that budget is spent - so an exhausted harness never reads
# as a content failure.
NON_CONTENT_ERROR_CLASSES: dict[str, str] = {
    "TRANSIENT_INCREMENTAL_AUDIT": "PERMANENT_AUDIT_INFRASTRUCTURE",
    # The model process exited non-zero (an upstream error, a dropped
    # stream, "high demand"): the Step's content was never attempted.
    # TRANSIENT_TIMEOUT is deliberately absent - a model that runs out of
    # time on a hard Step has attempted the content.
    "TRANSIENT_MODEL_BACKEND": "PERMANENT_MODEL_BACKEND_INFRASTRUCTURE",
    # Phase 3c: a Judge that produced no usable verdict (indeterminate review,
    # a malformed or missing role output, a packet, provenance or aggregation
    # failure) has not judged the content, so it retries the Judge's
    # infrastructure instead of spending one of the Step's content attempts.
    # The classes share one budget: they all end as PERMANENT_JUDGE_INFRASTRUCTURE.
    "TRANSIENT_JUDGE_INDETERMINATE": "PERMANENT_JUDGE_INFRASTRUCTURE",
    "TRANSIENT_JUDGE_ROLE": "PERMANENT_JUDGE_INFRASTRUCTURE",
    "TRANSIENT_JUDGE_PACKET": "PERMANENT_JUDGE_INFRASTRUCTURE",
    "TRANSIENT_JUDGE_INFRASTRUCTURE": "PERMANENT_JUDGE_INFRASTRUCTURE",
    "TRANSIENT_JUDGE_PROVENANCE": "PERMANENT_JUDGE_INFRASTRUCTURE",
    "TRANSIENT_JUDGE_AGGREGATION": "PERMANENT_JUDGE_INFRASTRUCTURE",
    # The Judge finished but the deterministic decision router
    # (scripts/judge_decision_router.py) failed: unreadable aggregate or
    # visual-gate input, a timeout or a crash.  No verdict was routed, so the
    # content was not judged.  TRANSIENT_JUDGE_INCOMPLETE stays a content
    # attempt on purpose: a PASS without a completed review and without a
    # delivery override is an authorization/workflow state, which a retry
    # would only reproduce.
    "TRANSIENT_JUDGE_ROUTING": "PERMANENT_JUDGE_INFRASTRUCTURE",
}
INFRASTRUCTURE_RETRY_BUDGET = 3

# Semantic-reopen budget (optimization plan, phase 3a; decision 4 taken on
# 2026-11-06).  Under the current classifier identity an initiating Stage may
# rewind an earlier Stage at most STAGE_SEMANTIC_REOPEN_LIMIT times, and one
# run at most RUN_SEMANTIC_REOPEN_LIMIT times in total.  Either limit being
# spent pauses the run with an explanation instead of failing it; an operator
# either reverts the upstream-owned edits and resumes (the change set is
# rechecked through recovery, without re-running the model) or grants one more
# reopen (``SEMANTIC_REOPEN_GRANTED``).
STAGE_SEMANTIC_REOPEN_LIMIT = 2
RUN_SEMANTIC_REOPEN_LIMIT = 5
SEMANTIC_REOPEN_PAUSE_SUBCODE = "SEMANTIC_REOPEN_BUDGET"
SEMANTIC_REOPEN_GRANTED = "SEMANTIC_REOPEN_GRANTED"
# Phase 3b: the one formal way to retry a failed Step with a fresh attempt
# budget, replacing direct database edits and one-off authorization events.
STEP_RETRY_AUTHORIZED = "STEP_RETRY_AUTHORIZED"
# Stage catalog v2 review exit (acceptance A, F6/F7).  v2 leaves Stage 8 at
# Step 12 with no Judge in between, so the open review issues (rows outside
# the machine-maintained audit section) are routed here by their Category:
# results/model issues recompute the computation Steps 5-8 and return to the
# revision, at most V2_REVIEW_RECOMPUTE_LIMIT times; the rest re-run Steps
# 11-12 at most V2_REVIEW_LOOP_LIMIT times.  Then the run fails as before.
# Both counts restart after an operator ``retry-step`` of the revision.
REVIEW_UPSTREAM_CATEGORIES = frozenset({"results", "model"})
V2_REVIEW_RECOMPUTE_LIMIT = 1
V2_REVIEW_LOOP_LIMIT = 1
REVIEW_ISSUE_REWORK_RULE = "review_issue"


@dataclass(frozen=True)
class ScheduledStageTask:
    stage_id: int
    stage_name: str
    subtask: str
    source_step_id: int
    checkpoint_step_id: int | None
    definition: StepDefinition


class FactoryEngine:
    def __init__(
        self,
        project_dir: str | Path,
        *,
        store: SQLiteStateStore | None = None,
        registry: StepRegistry | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        projector: Callable[[Path, WorkflowState], None] | None = None,
        dispatch_preflight: Callable[[StepDefinition], dict | None] | None = None,
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        #: Phase 3d: called with the Step definition before ``STEP_STARTED``; a report
        #: (not ``None``) pauses the run without consuming an attempt.
        self._dispatch_preflight = dispatch_preflight
        self.store = store or SQLiteStateStore(self.project_dir)
        self.registry = registry or StepRegistry()
        self._sleep = sleeper
        self._projector = projector
        self._pipeline = StageExecutionPipeline(self.store.now_epoch)
        self._transitions = TransitionCoordinator(
            self.project_dir, self.store, self._projector
        )
        #: The bounded-run contract this invocation is executing under, if any.
        #: Held on the instance so the pre-commit protection check can see it,
        #: and cleared in run()'s finally so it cannot leak between invocations.
        self._bounded_contract: BoundedRunContract | None = None
        #: Committed checkpoints the running contract asked to protect, frozen
        #: at entry so the pre-commit check can compare against them.
        self._protected_checkpoints: dict[int, tuple[str, ...]] = {}

    def run_bounded(self, contract: BoundedRunContract) -> "BoundedRunResult":
        """Execute one authorised, bounded advance and report it structurally.

        This is the supported replacement for the hand-written ``work/*.py``
        drivers: it verifies the protected manifest at entry, runs the existing
        continuous runner under the contract, verifies the manifest again, and
        returns a structured outcome instead of leaving the caller to journal its
        own progress file and guess at the stop reason.

        It adds no second runner - ``run`` is the same loop, invoked under a
        contract.
        """

        from .bounded_run import (
            BoundedRunResult,
            boundary_fingerprint,
            classify_stop_reason,
            reconcile_contract_violation,
        )

        start_state = self.store.load()
        if contract.protected_manifest or contract.protected_identity:
            entry = verify_protected_manifest(
                self.project_dir,
                contract.protected_manifest,
                contract.protected_identity,
            )
            if not entry.ok:
                # Refuse before writing anything: the caller's own expectation is
                # already violated, so advancing would build on a broken premise.
                raise ProtectedManifestViolation(
                    "protected manifest is already violated at entry: " + entry.describe()
                )
        else:
            entry = verify_protected_manifest(self.project_dir, {}, {})

        # The protected checkpoints are snapshotted here and compared again after
        # the run.  A caller naming a source step that has no committed checkpoint
        # is refused up front: there would be nothing to protect.
        checkpoint_snapshot: dict[int, str] = {}
        entry_checkpoints = CheckpointVerification(ok=True)
        if contract.protected_checkpoints:
            checkpoint_snapshot = snapshot_checkpoints(
                self.store.stage_checkpoints(), contract.protected_checkpoints
            )
            # snapshot_checkpoints only reports what exists, so the request itself
            # has to be checked: naming a source step with no committed checkpoint
            # would otherwise protect nothing while looking like protection.
            absent = sorted(
                set(contract.protected_checkpoints) - set(checkpoint_snapshot)
            )
            if absent:
                raise ProtectedCheckpointViolation(
                    "protected checkpoint is absent at entry for source step(s): "
                    + ", ".join(str(step) for step in absent)
                )
            entry_checkpoints = verify_checkpoints(
                self.store.stage_checkpoints(), checkpoint_snapshot
            )

        start_revision = int(start_state.revision)
        previous_status = str(
            getattr(start_state.status, "value", start_state.status)
        )
        end_state = self.run(contract=contract)

        succeeded = [
            event
            for event in self.store.events(since_revision=start_revision)
            if event.type == "STEP_SUCCEEDED"
        ]
        completed = len(succeeded)
        final = verify_protected_manifest(
            self.project_dir, contract.protected_manifest, contract.protected_identity
        )
        final_checkpoints = (
            verify_checkpoints(self.store.stage_checkpoints(), checkpoint_snapshot)
            if checkpoint_snapshot
            else CheckpointVerification(ok=True)
        )
        blocked_reason = ""
        pending = getattr(end_state, "pending_action", None)
        if pending is not None:
            blocked_reason = str(
                getattr(pending, "type", None)
                or getattr(pending, "kind", None)
                or "PENDING_ACTION"
            )
        stop_reason = classify_stop_reason(
            end_state,
            previous_status=previous_status,
            completed=completed,
            bounded=contract.max_subtasks,
        )
        if not final.ok:
            stop_reason = "PROTECTED_MANIFEST_VIOLATED"
        elif not final_checkpoints.ok:
            # a distinct reason: this is a database row, not a protected file
            stop_reason = "PROTECTED_CHECKPOINT_VIOLATED"
        fingerprint = boundary_fingerprint(
            end_state, blocked_reason=blocked_reason
        )
        made_progress = int(end_state.revision) > start_revision
        violation = reconcile_contract_violation(
            contract, end_state, start_revision=start_revision, completed=completed
        )
        return BoundedRunResult(
            run_id=contract.run_id,
            contract_sha256=contract.contract_sha256,
            run_policy=contract.run_policy,
            actor=contract.actor,
            start_revision=start_revision,
            end_revision=int(end_state.revision),
            status=str(getattr(end_state.status, "value", end_state.status)),
            stop_reason=stop_reason,
            completed_subtasks=completed,
            made_progress=made_progress,
            boundary_fingerprint=fingerprint,
            unchanged_boundary=(
                contract.previous_boundary_fingerprint is not None
                and contract.previous_boundary_fingerprint == fingerprint
                and not made_progress
            ),
            entry_verification=entry,
            final_verification=final,
            entry_checkpoints=entry_checkpoints,
            final_checkpoints=final_checkpoints,
            blocked_reason=blocked_reason,
            contract_violation=violation,
        )

    def get_state(self) -> WorkflowState:
        return self.store.load()

    def run(
        self, *, max_steps: int | None = None,
        allowed_source_steps: frozenset[int] | None = None,
        contract: "BoundedRunContract | None" = None,
    ) -> WorkflowState:
        """Advance the project.

        ``contract`` is optional and additive: when given, it pins the revision
        and cursor this invocation was authorised against, records the
        authorisation in ``RUN_STARTED``, and makes the protected manifest part of
        the pre-commit check.  Callers that pass nothing keep the old behaviour.
        """

        if contract is not None:
            if contract.allowed_source_steps is not None:
                # The contract is the authorisation; the explicit kwarg is the
                # lower-level form of the same thing and must not disagree.
                if (
                    allowed_source_steps is not None
                    and frozenset(allowed_source_steps) != contract.allowed_source_steps
                ):
                    raise BoundedRunError(
                        "allowed_source_steps disagrees with the bounded run contract"
                    )
                allowed_source_steps = contract.allowed_source_steps
            if contract.max_subtasks is not None:
                if max_steps is not None and max_steps != contract.max_subtasks:
                    raise BoundedRunError(
                        "max_steps disagrees with the bounded run contract"
                    )
                max_steps = contract.max_subtasks
        state = self.store.load()
        if contract is not None:
            # Compare-and-swap first: a stale authorisation must be refused before
            # any event is written, not discovered afterwards.
            if int(state.revision) != int(contract.expected_revision):
                raise BoundedRunError(
                    f"expected revision {contract.expected_revision}, found {state.revision}"
                )
            check_cursor(state, contract.expected_cursor)
        if state.scheduler_generation not in {
            STEP_SCHEDULER_GENERATION,
            STAGE_SCHEDULER_GENERATION,
        }:
            raise InvalidTransition(
                f"unsupported scheduler generation: {state.scheduler_generation}"
            )
        stage_mode = state.scheduler_generation == STAGE_SCHEDULER_GENERATION
        if stage_mode and state.stage_catalog_version not in SUPPORTED_STAGE_CATALOG_VERSIONS:
            raise InvalidTransition(
                "Stage scheduler project has an unsupported Stage catalog version"
            )
        runner_is_live = state.runner_pid is not None and self._pid_is_live(
            state.runner_pid
        )
        if runner_is_live and state.runner_pid != os.getpid():
            raise RunnerBusy(
                f"project {state.project_id} already has live runner {state.runner_pid}"
            )
        # Arm the contract before anything below can write.  This has to happen
        # here rather than just around the advance loop, because the recovery
        # branch further down reaches _complete_stage_task through its COMPLETE
        # disposition - the same commit path the advance loop uses - and all three
        # of these attributes are read by that path:
        #
        #   _bounded_contract      the pre-commit manifest/identity check
        #   _protected_checkpoints the pre-commit committed-checkpoint check
        #   the scoped registry    the Step ceilings recovery's reopen decision reads
        #
        # Arming them only for the loop would let a recovered commit land and be
        # reported afterwards, which is the opposite of a pre-commit check.
        previous_registry = self.registry
        previous_contract = self._bounded_contract
        previous_checkpoints = self._protected_checkpoints
        self._bounded_contract = contract
        if contract is not None and contract.protected_checkpoints:
            self._protected_checkpoints = snapshot_checkpoints(
                self.store.stage_checkpoints(), contract.protected_checkpoints
            )
        # A contract may tighten the Step ceilings the engine reads from a
        # StepDefinition.  Swapping the registry is how that reaches every
        # resolution path, including recovery's; the swap is skipped entirely when
        # the contract carries no ceiling, so an unscoped run is unchanged.
        if contract is not None and (
            contract.max_attempts_per_step or contract.max_reopens_per_step
        ):
            self.registry = ScopedRegistry(previous_registry, contract)
        try:
            if state.status in TERMINAL_STATUSES:
                return state
            if state.status in {
                WorkflowStatus.AWAITING_SELECTION,
                WorkflowStatus.AWAITING_CONSULTATION,
                WorkflowStatus.PAUSED,
                WorkflowStatus.FAILED,
            }:
                return state

            # An already exhausted step schedule must not write RUN_STARTED before
            # the production-state completion boundary is classified.  This path
            # is also used by service.run() for a migrated Step-16 project.
            if (
                not stage_mode
                and state.active_step is None
                and state.runner_pid is None
                and self.registry.next_after(state.last_completed_step) is None
            ):
                return self._commit_project_completed(state, stage_mode=False)

            if state.runner_pid is not None and not runner_is_live:
                state = self._transition(
                    expected_revision=state.revision,
                    event_type="RUNNER_INTERRUPTED",
                    changes={
                        "status": WorkflowStatus.INTERRUPTED,
                        "runner_pid": None,
                        "runner_lease_id": None,
                        "heartbeat_at": None,
                    },
                    payload={"reason": "recorded runner is no longer live"},
                )
            elif (
                state.status in {WorkflowStatus.RUNNING, WorkflowStatus.RETRYING}
                and state.runner_pid is None
            ):
                state = self._transition(
                    expected_revision=state.revision,
                    event_type="RUNNER_INTERRUPTED",
                    changes={"status": WorkflowStatus.INTERRUPTED},
                    payload={"reason": "active run has no recorded runner"},
                )
            if state.active_step is not None and (not stage_mode or state.attempt > 0):
                enforce_recovery_lease = state.runner_pid == os.getpid()
                state = self.recover(
                    expected_runner_pid=state.runner_pid,
                    expected_runner_lease_id=state.runner_lease_id,
                    enforce_lease=enforce_recovery_lease,
                )
                if state.status in {
                    WorkflowStatus.AWAITING_SELECTION,
                    WorkflowStatus.AWAITING_CONSULTATION,
                    WorkflowStatus.FAILED,
                    WorkflowStatus.PAUSED,
                    WorkflowStatus.KILLED,
                    WorkflowStatus.COMPLETED,
                }:
                    return state
            lease = uuid.uuid4().hex
            state = self._transition(
                expected_revision=state.revision,
                event_type="RUN_STARTED",
                changes={
                    "status": WorkflowStatus.RUNNING,
                    "runner_pid": os.getpid(),
                    "runner_lease_id": lease,
                    "heartbeat_at": int(time.time()),
                },
                payload={
                    "lease_id": lease,
                    "worker_pid": os.getpid(),
                    "worker_identity": _process_identity(os.getpid()),
                    **(
                        {"bounded_run": contract.event_payload()}
                        if contract is not None
                        else {}
                    ),
                },
                expected_runner_pid=state.runner_pid,
                expected_runner_lease_id=state.runner_lease_id,
            )
            return self._advance_loop(
                state,
                lease=lease,
                stage_mode=stage_mode,
                max_steps=max_steps,
                allowed_source_steps=allowed_source_steps,
            )

        finally:
            self.registry = previous_registry
            self._bounded_contract = previous_contract
            self._protected_checkpoints = previous_checkpoints

    def _advance_loop(
        self,
        state: WorkflowState,
        *,
        lease: str,
        stage_mode: bool,
        max_steps: int | None,
        allowed_source_steps: frozenset[int] | None,
    ) -> WorkflowState:
        completed_this_run = 0
        while True:
            stage_task: ScheduledStageTask | None = None
            if stage_mode:
                state, stage_task = self._select_stage_task(state, lease)
                definition = stage_task.definition if stage_task is not None else None
            else:
                definition = self.registry.next_after(state.last_completed_step)
            if definition is None:
                return self._commit_project_completed(
                    state,
                    stage_mode=stage_mode,
                    runner_lease=lease,
                )
            if max_steps is not None and completed_this_run >= max_steps:
                return self._owned_transition(
                    state,
                    lease,
                    event_type="RUN_STOPPED",
                    changes={
                        "status": WorkflowStatus.READY,
                        "runner_pid": None,
                        "runner_lease_id": None,
                        "heartbeat_at": None,
                    },
                    payload={"reason": "max_steps"},
                )
            if allowed_source_steps is not None and definition.id not in allowed_source_steps:
                return self._owned_transition(
                    state, lease, event_type="RUN_BOUNDARY_REACHED",
                    changes={"status": WorkflowStatus.PAUSED, "runner_pid": None,
                             "runner_lease_id": None, "heartbeat_at": None},
                    payload={"reason": "selected source step outside requested run scope",
                             "selected_step": definition.id,
                             "allowed_source_steps": sorted(allowed_source_steps)},
                )
            attempt = state.attempt + 1 if state.active_step == definition.id else 1
            content_attempts_used = (
                state.attempt - self._non_content_retries(definition.id)
                if state.active_step == definition.id
                else 0
            )
            if stage_mode and content_attempts_used >= definition.max_attempts:
                assert stage_task is not None
                return self._fail_stage_task(
                    state,
                    lease,
                    stage_task,
                    event_type="STEP_FAILED",
                    payload={
                        "error_class": "PERMANENT_ATTEMPT_BUDGET_EXHAUSTED",
                        "stage": stage_task.stage_id,
                        "subtask": stage_task.subtask,
                        "source_step": stage_task.source_step_id,
                    },
                )
            try:
                timeout_seconds = self._contest_timeout(definition)
            except ContestDeadlineExceeded as exc:
                if stage_task is not None:
                    return self._fail_stage_task(
                        state,
                        lease,
                        stage_task,
                        event_type="CONTEST_DEADLINE_EXHAUSTED",
                        payload={
                            "error_class": "PERMANENT_CONTEST_DEADLINE",
                            "reason": str(exc),
                            "step": definition.id,
                        },
                    )
                return self._owned_transition(
                    state,
                    lease,
                    event_type="CONTEST_DEADLINE_EXHAUSTED",
                    changes={
                        "status": WorkflowStatus.FAILED,
                        "runner_pid": None,
                        "runner_lease_id": None,
                        "heartbeat_at": None,
                    },
                    payload={
                        "error_class": "PERMANENT_CONTEST_DEADLINE",
                        "reason": str(exc),
                        "step": definition.id,
                    },
                )
            preview_context = StepContext(
                project_dir=self.project_dir,
                project_id=state.project_id,
                step_id=definition.id,
                attempt=attempt,
                timeout_seconds=timeout_seconds,
                revision=state.revision,
                deadline_epoch=self._contest_deadline(definition),
                clock=self.store.clock,
            )
            try:
                with deadline_scope(preview_context.deadline_epoch):
                    prepared = self._pipeline.prepare(
                        StageExecutionRequest(definition, preview_context)
                    )
            except ContestDeadlineExceeded as exc:
                return self._deadline_failure(
                    state,
                    definition,
                    exc,
                    lease=lease,
                    stage_task=stage_task,
                )
            if prepared.pending_action is not None:
                return self._await_action(
                    state,
                    prepared.pending_action.to_dict(),
                    reason=prepared.reason,
                    evidence=prepared.evidence,
                    event_type="STEP_PREPARE_AWAITING_ACTION",
                    lease=lease,
                )
            if not prepared.ready:
                if stage_task is not None:
                    return self._fail_stage_task(
                        state,
                        lease,
                        stage_task,
                        event_type="STEP_FAILED",
                        payload={
                            "error_class": "PERMANENT_INVALID_PREPARE_RESULT",
                            "reason": prepared.reason,
                        },
                    )
                return self._owned_transition(
                    state,
                    lease,
                    event_type="STEP_FAILED",
                    changes={
                        "status": WorkflowStatus.FAILED,
                        "runner_pid": None,
                        "runner_lease_id": None,
                        "heartbeat_at": None,
                    },
                    payload={
                        "error_class": "PERMANENT_INVALID_PREPARE_RESULT",
                        "reason": prepared.reason,
                    },
                )
            blocked = (
                self._dispatch_preflight(definition)
                if self._dispatch_preflight is not None
                else None
            )
            if blocked is not None:
                # No STEP_STARTED, so no attempt is spent; the Stage cursor
                # stays, and a resume after the provider or disk is fixed
                # dispatches the same Step.
                return self._owned_transition(
                    state,
                    lease,
                    event_type="PAUSED",
                    changes={
                        "status": WorkflowStatus.PAUSED,
                        "runner_pid": None,
                        "runner_lease_id": None,
                        "heartbeat_at": None,
                    },
                    payload={
                        "reason": {
                            "code": "PAUSED",
                            "subcode": "PROVIDER_UNAVAILABLE",
                            "actor": "engine",
                            "message": str(blocked.get("message") or "dispatch preflight failed"),
                            "evidence": [],
                        },
                        "stage": stage_task.stage_id if stage_task is not None else None,
                        "subtask": stage_task.subtask if stage_task is not None else None,
                        "source_step": definition.id,
                        "preflight": blocked,
                    },
                )
            state = self._owned_transition(
                state,
                lease,
                event_type="STEP_STARTED",
                changes={
                    "status": WorkflowStatus.RUNNING,
                    "active_step": definition.id,
                    "attempt": attempt,
                    "heartbeat_at": int(time.time()),
                },
                payload={
                    "step_name": definition.name,
                    "configured_timeout_seconds": definition.timeout_seconds,
                    "effective_timeout_seconds": timeout_seconds,
                    **(
                        {
                            "stage": stage_task.stage_id,
                            "stage_name": stage_task.stage_name,
                            "subtask": stage_task.subtask,
                            "source_step": stage_task.source_step_id,
                        }
                        if stage_task is not None
                        else {}
                    ),
                },
            )
            context = self._context(
                state, definition, timeout_seconds=timeout_seconds
            )
            try:
                outcome = self._pipeline.run(
                    StageExecutionRequest(definition, context),
                    after_execute=lambda: self._refresh_owned_state(
                        lease, active_step=definition.id
                    ),
                )
            except ContestDeadlineExceeded as exc:
                return self._deadline_failure(
                    state,
                    definition,
                    exc,
                    lease=lease,
                    stage_task=stage_task,
                )
            result = outcome.execution
            state = self._refresh_owned_state(lease, active_step=definition.id)
            for side_effect in outcome.workflow_events:
                side_effect_type = str(side_effect.get("type") or "")
                if not side_effect_type:
                    raise InvalidTransition("pipeline workflow event is missing a type")
                state = self._owned_transition(
                    state,
                    lease,
                    event_type=side_effect_type,
                    changes={},
                    payload=dict(side_effect.get("payload") or {}),
                    event_step=side_effect.get("step", definition.id),
                )
            if result.metadata.get("killed"):
                return self._owned_transition(
                    state,
                    lease,
                    event_type="KILLED",
                    changes={
                        "status": WorkflowStatus.KILLED,
                        "runner_pid": None,
                        "runner_lease_id": None,
                        "heartbeat_at": None,
                    },
                    payload=result.metadata,
                )
            resume_after = result.metadata.get("resume_after_step")
            if (continuation := self._stop_at_gate2(state, lease, result, outcome.validation)) is not None:
                return continuation
            if resume_after is not None:
                policy_payload = self.store.contest_policy()
                if (
                    (
                        definition.id == 16
                        or (
                            stage_task is not None
                            and stage_task.subtask == FINAL_JUDGE_SUBTASK
                        )
                    )
                    and policy_payload is not None
                    and self.store.now_epoch()
                    >= int(policy_payload["delivery_freeze_at"])
                    and self.store.decision("delivery_freeze_override") is None
                ):
                    from web.backend.selection_service import (
                        build_delivery_freeze_override_options,
                    )

                    build_delivery_freeze_override_options(
                        self.project_dir,
                        resume_after_step=int(resume_after),
                        now_epoch=self.store.now_epoch(),
                    )
                    return self._await_action(
                        state,
                        PendingAction(
                            type="delivery_freeze_override_selection",
                            gate="delivery_freeze_override",
                            metadata={"resume_after_step": int(resume_after)},
                        ).to_dict(),
                        reason=(
                            "delivery freeze requires human override before "
                            "reopening substantive work"
                        ),
                        evidence=(
                            str(
                                self.project_dir.joinpath(
                                    "selection/delivery_freeze_override_options.json"
                                ).relative_to(self.project_dir)
                            ),
                        ),
                        lease=lease,
                    )
                if not self._reopen_allowed(definition):
                    if stage_task is not None:
                        return self._fail_stage_task(
                            state,
                            lease,
                            stage_task,
                            event_type="STEP_FAILED",
                            payload={
                                "error_class": "PERMANENT_REOPEN_BUDGET_EXHAUSTED",
                                "source_step": definition.id,
                                "resume_after_step": resume_after,
                            },
                        )
                    return self._owned_transition(
                        state,
                        lease,
                        event_type="STEP_FAILED",
                        changes={
                            "status": WorkflowStatus.FAILED,
                            "runner_pid": None,
                            "runner_lease_id": None,
                            "heartbeat_at": None,
                        },
                        payload={
                            "error_class": "PERMANENT_REOPEN_BUDGET_EXHAUSTED",
                            "source_step": definition.id,
                            "resume_after_step": resume_after,
                        },
                    )
                resume_after = self._validated_resume_target(
                    resume_after, definition.id, self._catalog(state)
                )
                reopen_changes = {
                    "status": WorkflowStatus.RUNNING,
                    "last_completed_step": resume_after,
                    "active_step": None,
                    "attempt": 0,
                }
                reopen_kwargs = {}
                if stage_task is not None:
                    reopen_changes.update(
                        last_completed_stage=self._catalog(state).completed_stage_for_step(resume_after),
                        active_stage=None,
                        active_subtask=None,
                        source_step_id=None,
                    )
                    reopen_kwargs = {
                        "invalidate_checkpoints_after_step": resume_after,
                        "subtask_baseline": None,
                    }
                state = self._owned_transition(
                    state,
                    lease,
                    event_type="STEP_REOPENED",
                    changes=reopen_changes,
                    payload={
                        "source_step": definition.id,
                        **(
                            {
                                "stage": stage_task.stage_id,
                                "subtask": stage_task.subtask,
                            }
                            if stage_task is not None
                            else {}
                        ),
                        **result.metadata,
                    },
                    **reopen_kwargs,
                )
                continue
            validation = outcome.validation
            if validation is None:
                raise InvalidTransition(
                    f"step {definition.id} returned no validation outcome"
                )
            state = self._refresh_owned_state(lease, active_step=definition.id)
            if validation.pending_action is not None:
                return self._await_action(
                    state,
                    validation.pending_action.to_dict(),
                    reason=validation.reason,
                    evidence=validation.evidence,
                    lease=lease,
                )
            if validation.metadata.get("killed"):
                return self._owned_transition(
                    state,
                    lease,
                    event_type="KILLED",
                    changes={
                        "status": WorkflowStatus.KILLED,
                        "runner_pid": None,
                        "runner_lease_id": None,
                        "heartbeat_at": None,
                    },
                    payload={**validation.metadata, **result.metadata},
                )
            if result.returncode == 0 and validation.is_valid:
                completed_step = int(
                    result.metadata.get(
                        "completed_through_step",
                        validation.metadata.get("completed_through_step", definition.id),
                    )
                )
                if completed_step < definition.id:
                    raise InvalidTransition(
                        f"step {definition.id} cannot complete through earlier step {completed_step}"
                    )
                if stage_task is not None:
                    if (
                        stage_task.checkpoint_step_id is not None
                        and completed_step != stage_task.checkpoint_step_id
                    ):
                        raise InvalidTransition(
                            "Stage scheduler subtasks cannot fast-forward the Step cursor"
                        )
                    state = self._complete_stage_task(
                        state,
                        lease,
                        stage_task,
                        validation=validation,
                        result=result,
                    )
                    if self.store.events()[-1].type == "STEP_SUCCEEDED":
                        completed_this_run += 1
                    if state.status in {
                        WorkflowStatus.FAILED,
                        WorkflowStatus.PAUSED,
                        WorkflowStatus.AWAITING_SELECTION,
                        WorkflowStatus.AWAITING_CONSULTATION,
                    }:
                        return state
                    continue
                # The Step-scheduler path commits Step success directly, without
                # going through _complete_stage_task, so it has to enforce the
                # contract's protection itself.  run_bounded() accepts a Step
                # project as readily as a Stage one; a check on only the Stage
                # path would let a contract on a Step project be a no-op.
                violation = self._contract_protection_violation()
                if violation is not None:
                    return self._owned_transition(
                        state,
                        lease,
                        event_type="STEP_FAILED",
                        changes={
                            "status": WorkflowStatus.FAILED,
                            "runner_pid": None,
                            "runner_lease_id": None,
                            "heartbeat_at": None,
                        },
                        payload={**violation, "source_step": completed_step},
                    )
                state = self._owned_transition(
                    state,
                    lease,
                    event_type="STEP_SUCCEEDED",
                    changes={
                        "status": WorkflowStatus.RUNNING,
                        "last_completed_step": completed_step,
                        "active_step": None,
                        "attempt": 0,
                    },
                    payload={"evidence": validation.evidence, **result.metadata},
                )
                completed_this_run += 1
                continue
            failure_metadata = {**validation.metadata, **result.metadata}
            missing_artifacts = self._missing_evidence(validation.evidence)
            if missing_artifacts and "missing_artifacts" not in failure_metadata:
                failure_metadata["missing_artifacts"] = missing_artifacts
            error_class = (
                result.error_class
                or str(validation.metadata.get("error_class") or "")
                or (
                    "TRANSIENT_ARTIFACT_MISSING"
                    if failure_metadata.get("missing_artifacts")
                    else "TRANSIENT_ARTIFACT_INVALID"
                )
            )
            failure_reason = self._failure_reason(result, validation.reason)
            if failure_reason != validation.reason:
                failure_metadata["validation_reason"] = validation.reason
            permanent = error_class.startswith("PERMANENT")
            non_content = error_class in NON_CONTENT_ERROR_CLASSES
            earlier_non_content = self._non_content_retries(definition.id)
            if non_content:
                # Spends the infrastructure budget of this class's terminal
                # class (shared by every class that ends the same way), never
                # a content attempt.
                same_class = self._non_content_retries(
                    definition.id, terminal_class=NON_CONTENT_ERROR_CLASSES[error_class]
                )
                exhausted = same_class + 1 > INFRASTRUCTURE_RETRY_BUDGET
                failure_metadata["infrastructure_retries"] = same_class + 1
                failure_metadata["infrastructure_retry_budget"] = INFRASTRUCTURE_RETRY_BUDGET
            else:
                exhausted = attempt - earlier_non_content >= definition.max_attempts
            failure_metadata["content_attempts_used"] = attempt - earlier_non_content - int(non_content)
            if permanent or exhausted:
                terminal_error_class = self._terminal_error_class(
                    error_class, exhausted=not permanent
                )
                if terminal_error_class != error_class:
                    failure_metadata["exhausted_error_class"] = error_class
                terminal_payload = {
                    **failure_metadata,
                    "error_class": terminal_error_class,
                    "reason": failure_reason,
                    "returncode": result.returncode,
                }
                if stage_task is not None:
                    return self._fail_stage_task(
                        state,
                        lease,
                        stage_task,
                        event_type="STEP_FAILED",
                        payload=terminal_payload,
                    )
                return self._owned_transition(
                    state,
                    lease,
                    event_type="STEP_FAILED",
                    changes={
                        "status": WorkflowStatus.FAILED,
                        "runner_pid": None,
                        "runner_lease_id": None,
                        "heartbeat_at": None,
                    },
                    payload=terminal_payload,
                )
            retry_delay = self._retry_delay(attempt)
            try:
                retry_budget = self._contest_timeout(
                    definition, step_timeout=retry_delay
                )
            except ContestDeadlineExceeded as exc:
                retry_budget = 0
                budget_reason = str(exc)
            else:
                budget_reason = "insufficient time for retry delay"
            if retry_budget < retry_delay:
                deadline_payload = {
                    **failure_metadata,
                    "error_class": "PERMANENT_CONTEST_DEADLINE",
                    "reason": budget_reason,
                    "step": definition.id,
                    "required_retry_delay_seconds": retry_delay,
                    "remaining_budget_seconds": retry_budget,
                }
                if stage_task is not None:
                    return self._fail_stage_task(
                        state,
                        lease,
                        stage_task,
                        event_type="CONTEST_DEADLINE_EXHAUSTED",
                        payload=deadline_payload,
                    )
                return self._owned_transition(
                    state,
                    lease,
                    event_type="CONTEST_DEADLINE_EXHAUSTED",
                    changes={
                        "status": WorkflowStatus.FAILED,
                        "runner_pid": None,
                        "runner_lease_id": None,
                        "heartbeat_at": None,
                    },
                    payload=deadline_payload,
                )
            state = self._owned_transition(
                state,
                lease,
                event_type="RETRY_SCHEDULED",
                changes={"status": WorkflowStatus.RETRYING},
                payload={
                    **failure_metadata,
                    "error_class": error_class,
                    "reason": failure_reason,
                    "delay_seconds": retry_delay,
                },
            )
            self._sleep(retry_delay)

    def _select_stage_task(
        self, state: WorkflowState, lease: str
    ) -> tuple[WorkflowState, ScheduledStageTask | None]:
        state = self._invalidate_stale_stage_receipts(state, lease)
        completed = self.store.completed_stage_subtasks()
        catalog = self._catalog(state)
        selected = None
        if state.active_subtask is not None:
            stage, subtask = catalog.subtask_for_key(state.active_subtask)
            if state.active_stage != stage.id:
                raise InvalidTransition(
                    "persisted Stage and subtask do not belong to the same catalog entry"
                )
            selected = (stage, subtask)
        else:
            selected = catalog.next_stage_subtask(completed_subtasks=completed)
        if selected is None:
            return state, None
        stage, subtask = selected
        if subtask.kind != "step":
            # Gate and final-Judge subtasks have their own registered lifecycle.
            definition = self.registry.stage_subtask(subtask.key)
        elif subtask.conditional:
            if semantic_flags(self.store.dirty_flags()):
                definition = self.registry.get(subtask.source_step_id)
            else:
                definition = self.registry.stage_subtask(
                    "conditional_math_preflight_skip"
                )
        else:
            definition = self.registry.get(subtask.source_step_id)
        task = ScheduledStageTask(
            stage_id=stage.id,
            stage_name=stage.name,
            subtask=subtask.key,
            source_step_id=subtask.source_step_id,
            checkpoint_step_id=subtask.checkpoint_step_id,
            definition=definition,
        )
        baseline = self.store.stage_cursor_input()
        baseline_matches = (
            baseline is not None
            and int(baseline["stage_id"]) == stage.id
            and str(baseline["subtask"]) == subtask.key
            and int(baseline["source_step_id"]) == subtask.source_step_id
        )
        cursor_matches = (
            state.active_stage == stage.id
            and state.active_subtask == subtask.key
            and state.source_step_id == subtask.source_step_id
            and state.active_step == subtask.source_step_id
        )
        if cursor_matches and baseline_matches:
            return state, task
        manifest = capture_artifact_manifest(self.project_dir)
        state = self._owned_transition(
            state,
            lease,
            event_type="STAGE_SUBTASK_SELECTED",
            changes={
                "status": WorkflowStatus.RUNNING,
                "active_stage": stage.id,
                "active_subtask": subtask.key,
                "source_step_id": subtask.source_step_id,
                "active_step": subtask.source_step_id,
                "attempt": 0,
            },
            payload={
                "stage": stage.id,
                "stage_name": stage.name,
                "subtask": subtask.key,
                "source_step": subtask.source_step_id,
                "checkpoint_step": subtask.checkpoint_step_id,
                "stage_catalog_version": catalog.version,
            },
            subtask_baseline={
                "stage_id": stage.id,
                "subtask": subtask.key,
                "source_step_id": subtask.source_step_id,
                "input_fingerprint": manifest_fingerprint(manifest),
                "manifest": manifest,
            },
            event_step=subtask.source_step_id,
        )
        return state, task

    def _invalidate_stale_stage_receipts(
        self, state: WorkflowState, lease: str
    ) -> WorkflowState:
        checkpoints = {
            (int(item["stage_id"]), str(item["subtask"])): item
            for item in self.store.stage_checkpoints()
        }
        reviewer = checkpoints.get((6, "reviewer_entry_gate"))
        v2 = state.stage_catalog_version == STAGE_CATALOG_VERSION_V2
        if v2 and any(
            item.get("completed_step_id") == 9 for item in checkpoints.values()
        ):
            # v2 validates the reviewer-entry gate once, at Step 9 entry.  Later
            # edits to its reports are forward repairs (stage_catalog_v2_policy)
            # and must not rewind the paper to Step 8.
            reviewer = None
        if reviewer is not None:
            from scripts.step8_5_gate import collect_step8_5_state

            current = collect_step8_5_state(self.project_dir)
            bound = reviewer.get("receipt", {}).get("validation", {}).get("step8_5")
            reviewer_current = (
                isinstance(bound, dict)
                and current.get("ready") is True
                and bound.get("input_fingerprint") == current.get("input_fingerprint")
                and bound.get("artifact_fingerprint")
                == current.get("artifact_fingerprint")
            )
            if not reviewer_current:
                return self._owned_transition(
                    state,
                    lease,
                    event_type="STAGE_CHECKPOINT_INVALIDATED",
                    changes={
                        "status": WorkflowStatus.RUNNING,
                        "last_completed_step": min(state.last_completed_step, 8),
                        "last_completed_stage": min(state.last_completed_stage, 5),
                        "active_step": None,
                        "active_stage": None,
                        "active_subtask": None,
                        "source_step_id": None,
                        "attempt": 0,
                    },
                    payload={
                        "stage": 6,
                        "subtask": "reviewer_entry_gate",
                        "reason": "reviewer-entry fingerprint changed",
                    },
                    invalidate_checkpoints_after_step=8,
                    subtask_baseline=None,
                    event_step=8,
                )

        conditional = checkpoints.get((8, "conditional_math_preflight"))
        stage9_checkpoints = [
            item for (stage_id, _subtask), item in checkpoints.items() if stage_id == 9
        ]
        current_classifier = self._classifier_sha(state)
        classifier_stale = any(
            item.get("receipt", {}).get("classifier_contract_sha256")
            != current_classifier
            for item in stage9_checkpoints
        )
        if classifier_stale:
            from .final_evidence_recovery import classifier_equivalent_for_restored_checkpoints

            classifier_stale = not classifier_equivalent_for_restored_checkpoints(
                self.store, stage9_checkpoints, current_classifier
            )
        result_metadata = (
            conditional.get("receipt", {}).get("result", {})
            if conditional is not None
            else {}
        )
        if v2 and classifier_stale:
            return self._owned_transition(
                state,
                lease,
                event_type="STAGE_CHECKPOINT_INVALIDATED",
                changes={
                    "status": WorkflowStatus.RUNNING,
                    "last_completed_step": min(state.last_completed_step, 12),
                    "last_completed_stage": min(state.last_completed_stage, 8),
                    "active_step": None,
                    "active_stage": None,
                    "active_subtask": None,
                    "source_step_id": None,
                    "attempt": 0,
                },
                payload={
                    "stage": 9,
                    "subtask": "abstract",
                    "reason": "dirty-classifier contract changed",
                },
                invalidate_checkpoints_after_step=12,
                subtask_baseline=None,
                event_step=14,
            )
        if not v2 and (classifier_stale or (
            result_metadata.get("conditional_math_preflight") == "skipped"
            and not stage9_checkpoints
        )):
            definition = self.registry.stage_subtask(
                "conditional_math_preflight_skip"
            )
            context = StepContext(
                project_dir=self.project_dir,
                project_id=state.project_id,
                step_id=13,
                attempt=1,
                timeout_seconds=definition.timeout_seconds,
                revision=state.revision,
                deadline_epoch=None,
                clock=self.store.clock,
            )
            validation = definition.lifecycle.validate(context)
            if classifier_stale or not validation.is_valid:
                return self._owned_transition(
                    state,
                    lease,
                    event_type="STAGE_CHECKPOINT_INVALIDATED",
                    changes={
                        "status": WorkflowStatus.RUNNING,
                        "last_completed_step": min(state.last_completed_step, 12),
                        "last_completed_stage": min(state.last_completed_stage, 7),
                        "active_step": None,
                        "active_stage": None,
                        "active_subtask": None,
                        "source_step_id": None,
                        "attempt": 0,
                    },
                    payload={
                        "stage": 8,
                        "subtask": "conditional_math_preflight",
                        "reason": (
                            "dirty-classifier contract changed"
                            if classifier_stale
                            else "conditional math-preflight skip receipt is stale"
                        ),
                    },
                    invalidate_checkpoints_after_step=12,
                    subtask_baseline=None,
                    event_step=13,
                )
        return state

    # These methods participate in the frozen persisted-owner symbol manifest;
    # keep their source-span coordinates stable when editing earlier code.
    def _stage_manifest_delta(
        self,
        task: ScheduledStageTask,
    ) -> tuple[str, str, dict[str, str], list[dict[str, object]]]:
        baseline = self.store.stage_cursor_input()
        after = capture_artifact_manifest(self.project_dir)
        output_fingerprint = manifest_fingerprint(after)
        state = self.store.load()
        classifier_sha = self._classifier_sha(state)
        if baseline is None or (
            int(baseline["stage_id"]) != task.stage_id
            or str(baseline["subtask"]) != task.subtask
            or int(baseline["source_step_id"]) != task.source_step_id
        ):
            dirty_changes: list[dict[str, object]] = [
                {
                    "flag": DirtyFlag.MATH.value,
                    "owner_stage": 8,
                    "cause_artifact": "MISSING_STAGE_INPUT_BASELINE",
                    "baseline_fingerprint": "MISSING",
                    "current_fingerprint": output_fingerprint,
                    "classifier_contract_sha256": classifier_sha,
                    # Not derived from a rule: constructed deliberately to fail
                    # closed when the stage baseline is absent.
                    "classification_source": "explicit_fail_closed",
                },
                {
                    "flag": DirtyFlag.RESULT.value,
                    "owner_stage": 4,
                    "cause_artifact": "MISSING_STAGE_INPUT_BASELINE",
                    "baseline_fingerprint": "MISSING",
                    "current_fingerprint": output_fingerprint,
                    "classifier_contract_sha256": classifier_sha,
                    "classification_source": "explicit_fail_closed",
                },
            ]
            return "MISSING", output_fingerprint, after, dirty_changes
        before = dict(baseline["manifest"])
        dirty_changes = []
        # Schema v10 provenance: derived outside the frozen classifier module so
        # that dirty.py's bytes (part of the frozen classifier contract identity)
        # stay untouched.
        sources = classification_sources(before, after)
        v2 = state.stage_catalog_version == STAGE_CATALOG_VERSION_V2
        if v2:
            completed_computation_steps = self._completed_computation_steps(state)
        for change in classify_manifest_changes(before, after):
            record = {
                **change.to_dict(),
                "classifier_contract_sha256": classifier_sha,
                "classification_source": source_for(
                    sources, change.flag.value, change.cause_artifact
                ),
            }
            receipt_owner = self._solver_receipt_owner_stage(change.cause_artifact)
            if receipt_owner is not None:
                record["owner_stage"] = receipt_owner
            if v2:
                from .input_dependency_policy import apply_to_record
                from .stage_catalog_v2_policy import v2_owner_stage

                owner, rule = v2_owner_stage(record, initiator_stage=task.stage_id)
                if rule is not None:
                    record["v1_owner_stage"] = record["owner_stage"]
                    record["v2_owner_rule"] = rule
                    record["owner_stage"] = owner
                    record["classification_source"] = "stage_catalog_v2"
                # Phase 4: rework by input dependency, not by ownership.
                apply_to_record(
                    record,
                    initiator_stage=task.stage_id,
                    completed_steps=completed_computation_steps,
                    stage_of_step=lambda step: self._catalog(state).stage_for_step(step).id,
                )
            dirty_changes.append(record)
        return (
            str(baseline["input_fingerprint"]),
            output_fingerprint,
            after,
            dirty_changes,
        )

    def _input_recompute_plan(
        self, state: WorkflowState, dirty_changes: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Which computation subtasks re-run for consumed inputs (phase 4).

        The earliest completed computation Step that consumed a changed input
        and the completed computation Steps after it lose their checkpoints;
        every other checkpoint stays, so the scheduler re-runs them and then
        returns to the first unfinished subtask, the initiating one.
        """

        causes = [item for item in dirty_changes if item.get("input_consumer_step") is not None]
        if not causes:
            return None
        start = min(int(item["input_consumer_step"]) for item in causes)
        return self._computation_recompute(
            state,
            start,
            causes,
            sorted(
                f"{item.get('cause_artifact')}: consumed by Step {item['input_consumer_step']}"
                for item in causes
            ),
        )

    def _computation_recompute(
        self,
        state: WorkflowState,
        start: int,
        causes: list[dict[str, Any]],
        reasons: list[str],
    ) -> dict[str, Any]:
        from .input_dependency_policy import recompute_steps

        completed = self._completed_computation_steps(state)
        steps = [step for step in recompute_steps(start) if step in completed]
        catalog = self._catalog(state)
        subtasks = [
            (stage.id, subtask.key)
            for stage, subtask in catalog.order
            if subtask.kind == "step" and subtask.source_step_id in steps
        ]
        first = next(
            index
            for index, (_stage, subtask) in enumerate(catalog.order)
            if subtask.kind == "step" and subtask.source_step_id == start
        )
        previous = [
            subtask.checkpoint_step_id
            for _stage, subtask in catalog.order[:first]
            if subtask.checkpoint_step_id is not None
        ]
        return {
            "start_step": start,
            "steps": steps,
            "subtasks": subtasks,
            "owner_stage": catalog.stage_for_step(start).id,
            "resume_after_step": previous[-1] if previous else -1,
            "causes": causes,
            "reasons": reasons,
        }

    def _is_v2_review_exit(self, state: WorkflowState, task: ScheduledStageTask) -> bool:
        return (
            state.stage_catalog_version == STAGE_CATALOG_VERSION_V2
            and task.stage_id == 8
            and task.checkpoint_step_id == 12
        )

    def _open_review_issues(self, validation: ValidationResult) -> list[Any]:
        """Open BLOCKING/MAJOR review rows, unless continuation is authorized."""

        from .audit.ledger import open_review_issues

        if validation.metadata.get("gate2_continuation_override") is True:
            return []
        return [
            issue
            for issue in open_review_issues(
                self.project_dir / "audit_issue_ledger.md", {"BLOCKING", "MAJOR"}
            )
            if not issue.deferred_past_review
        ]

    def _deferred_review_issues(self) -> list[str]:
        """Open rows the v2 review exit leaves to a human or to Step 14/15 (F16)."""

        from .audit.ledger import open_review_issues

        return [
            f"{issue.issue_id} ({issue.category or 'uncategorized'}, {issue.severity}; "
            f"Blocking Until: {issue.blocking_until})"
            for issue in open_review_issues(
                self.project_dir / "audit_issue_ledger.md", {"BLOCKING", "MAJOR"}
            )
            if issue.deferred_past_review
        ]

    @staticmethod
    def _review_issue_summary(issues: list[Any]) -> list[str]:
        return [
            f"{issue.issue_id} ({issue.category or 'uncategorized'}, {issue.severity})"
            for issue in issues
        ]

    def _review_recompute_plan(
        self, state: WorkflowState, validation: ValidationResult
    ) -> dict[str, Any] | None:
        """Recompute Steps 5-8 for open results/model review issues (v2 exit)."""

        from .input_dependency_policy import COMPUTATION_STEPS

        upstream = [
            issue
            for issue in self._open_review_issues(validation)
            if issue.category in REVIEW_UPSTREAM_CATEGORIES
        ]
        if not upstream:
            return None
        events = self.store.events()
        since = self._review_budget_baseline(events)
        used = sum(
            1
            for event in events
            if event.revision > since
            and event.type == "STAGE_SEMANTIC_REOPENED"
            and event.payload.get("rework_rule") == REVIEW_ISSUE_REWORK_RULE
        )
        if used >= V2_REVIEW_RECOMPUTE_LIMIT:
            return None
        start = COMPUTATION_STEPS[0]
        if start not in self._completed_computation_steps(state):
            return None
        owner_stage = self._catalog(state).stage_for_step(start).id
        plan = self._computation_recompute(
            state,
            start,
            [
                {
                    "flag": "RESULT_DIRTY",
                    "owner_stage": owner_stage,
                    "cause_artifact": f"audit_issue_ledger.md#{issue.issue_id}",
                }
                for issue in upstream
            ],
            [
                f"audit_issue_ledger.md#{issue.issue_id}: open {issue.category} "
                f"{issue.severity} review issue"
                for issue in upstream
            ],
        )
        plan["rule"] = REVIEW_ISSUE_REWORK_RULE
        plan["review_issues"] = self._review_issue_summary(upstream)
        return plan

    @staticmethod
    def _review_budget_baseline(events: list[Any]) -> int:
        """The latest restart of the v2 review budgets, or 0.

        The review recompute and review loop are counted after it. Two events
        restart them: an operator ``retry-step`` of the revision (like the
        attempt count, an explicit retry of the exit Step starts them afresh),
        and a final-Judge ``REOPEN_REVISION_*`` reopen, whose repair cycle runs
        a new Step 11 review that must not inherit the first pass's spent
        budget (acceptance A, F15).
        """

        return max(
            (
                event.revision
                for event in events
                if (
                    event.type == STEP_RETRY_AUTHORIZED
                    and event.payload.get("stage") == 8
                    and event.payload.get("subtask") == "revision"
                )
                or (
                    event.type == "STEP_REOPENED"
                    and event.payload.get("subtask") == "final_judge"
                    and str(event.payload.get("final_decision", "")).startswith("REOPEN_REVISION")
                )
            ),
            default=0,
        )

    def _v2_review_exit(
        self,
        state: WorkflowState,
        lease: str | None,
        task: ScheduledStageTask,
        validation: ValidationResult,
        dirty_changes: list[dict[str, Any]],
    ) -> WorkflowState | None:
        """Leave Stage 8 in catalog v2, or loop the review once, or fail.

        Runs after the recompute route, so open results/model issues here mean
        that route is spent. The audit section's machine rows never block.
        """

        issues = self._open_review_issues(validation)
        if not issues:
            return None
        summary = self._review_issue_summary(issues)
        upstream = [issue for issue in issues if issue.category in REVIEW_UPSTREAM_CATEGORIES]
        events = self.store.events()
        since = self._review_budget_baseline(events)
        loops_used = sum(
            1
            for event in events
            if event.revision > since
            and event.type == "STEP_REOPENED"
            and event.payload.get("stage") == 8
            and event.payload.get("subtask") == task.subtask
        )
        if not upstream and loops_used < V2_REVIEW_LOOP_LIMIT:
            return self._stage_transition(
                state,
                lease,
                event_type="STEP_REOPENED",
                changes={
                    "status": WorkflowStatus.RUNNING,
                    "last_completed_step": 10,
                    "last_completed_stage": 7,
                    "active_step": None,
                    "active_stage": None,
                    "active_subtask": None,
                    "source_step_id": None,
                    "attempt": 0,
                },
                payload={
                    "source_step": task.source_step_id,
                    "stage": 8,
                    "subtask": task.subtask,
                    "resume_after_step": 10,
                    "rework_rule": "review_loop",
                    "review_issues": summary,
                    "deferred_review_issues": self._deferred_review_issues(),
                    "reason": "unresolved BLOCKING or MAJOR review issue remains",
                },
                dirty_changes=dirty_changes,
                invalidate_checkpoints_after_step=10,
                subtask_baseline=None,
                event_step=task.source_step_id,
            )
        return self._stage_transition(
            state,
            lease,
            event_type="STEP_FAILED",
            changes={
                "status": WorkflowStatus.FAILED,
                "runner_pid": None,
                "runner_lease_id": None,
                "heartbeat_at": None,
            },
            payload={
                "error_class": "PERMANENT_REVIEW_LOOP_EXHAUSTED",
                "stage": 8,
                "subtask": task.subtask,
                "review_issues": summary,
                "deferred_review_issues": self._deferred_review_issues(),
                "reason": (
                    "unresolved results/model review issue remains after the "
                    "review recompute"
                    if upstream
                    else "unresolved BLOCKING or MAJOR review issue remains after the review loop"
                ),
            },
            dirty_changes=dirty_changes,
            event_step=task.source_step_id,
        )

    def _completed_computation_steps(self, state: WorkflowState) -> set[int]:
        """The computation Steps (5-8) whose Stage checkpoint is present."""

        from .input_dependency_policy import COMPUTATION_STEPS

        completed = self.store.completed_stage_subtasks()
        return {
            subtask.source_step_id
            for stage, subtask in self._catalog(state).order
            if subtask.kind == "step"
            and subtask.source_step_id in COMPUTATION_STEPS
            and (stage.id, subtask.key) in completed
        }

    def _solver_receipt_owner_stage(self, artifact: str) -> int | None:
        """Resolve shared receipt infrastructure to its durable job owner."""

        job_id = solver_receipt_job_id(artifact)
        if job_id is None:
            return None
        try:
            job = self.store.solver_job(job_id)
        except KeyError:
            return None
        owner_stage = job.get("owner_stage")
        return int(owner_stage) if owner_stage is not None else None

    def _fail_stage_task(
        self,
        state: WorkflowState,
        lease: str | None,
        task: ScheduledStageTask,
        *,
        event_type: str,
        payload: dict[str, object],
        expected_runner_pid: int | None = None,
        expected_runner_lease_id: str | None = None,
        enforce_lease: bool = False,
    ) -> WorkflowState:
        """Fail one Stage subtask while atomically persisting its file delta."""

        _input, _output, _after, dirty_changes = self._stage_manifest_delta(task)
        transition_guards = (
            self._lease_expectations(
                expected_runner_pid,
                expected_runner_lease_id,
                enforce_lease,
            )
            if lease is None
            else {}
        )
        return self._stage_transition(
            state,
            lease,
            event_type=event_type,
            changes={
                "status": WorkflowStatus.FAILED,
                "runner_pid": None,
                "runner_lease_id": None,
                "heartbeat_at": None,
            },
            payload=payload,
            dirty_changes=dirty_changes,
            event_step=task.source_step_id,
            **transition_guards,
        )

    def _prompt_input_receipt_valid(
        self, task: ScheduledStageTask, result: ExecutionResult
    ) -> bool:
        if not getattr(
            task.definition.lifecycle,
            "requires_prompt_input_receipt",
            False,
        ):
            return True
        if result.metadata.get("prompt_input_schema") != (
            "factory-effective-prompt-v1"
        ):
            return False
        receipt_id = str(
            result.metadata.get("prompt_input_receipt_id") or ""
        )
        attempt_key_value = str(
            result.metadata.get("prompt_input_attempt_key") or ""
        )
        if not receipt_id or not attempt_key_value:
            return False
        stored = self.store.prompt_attempt_input(attempt_key_value)
        return bool(
            stored is not None
            and stored.get("receipt_id") == receipt_id
            and stored.get("effective_prompt_sha256")
            == result.metadata.get("effective_prompt_sha256")
            and stored.get("prompt_inputs_sha256")
            == result.metadata.get("prompt_inputs_sha256")
        )

    def _contract_protection_violation(self) -> dict[str, Any] | None:
        """The contract's pre-commit protection check, as a payload or ``None``.

        Called immediately before a Step-success commit on **every** path.  There
        are two: the Stage path commits through :meth:`_complete_stage_task`, and
        the Step path commits directly in ``_advance_loop``.  Only the first used
        to check, so a contract on a Step-scheduler project promised protection
        that never ran.  ``run_bounded`` accepts both, so both have to enforce it.

        Returns the violation's payload rather than raising, because each path
        needs to add its own context (a Stage task adds its stage and subtask) and
        commit the same terminal failure the rest of the engine uses.
        """

        contract = self._bounded_contract
        if contract is None:
            return None
        if contract.protected_manifest or contract.protected_identity:
            verification = verify_protected_manifest(
                self.project_dir,
                contract.protected_manifest,
                contract.protected_identity,
            )
            if not verification.ok:
                return {
                    "error_class": "PERMANENT_PROTECTED_MANIFEST_VIOLATED",
                    "protected_manifest": verification.to_dict(),
                    "bounded_run": contract.event_payload(),
                }
        if self._protected_checkpoints:
            checkpoints = verify_checkpoints(
                self.store.stage_checkpoints(), self._protected_checkpoints
            )
            if not checkpoints.ok:
                return {
                    "error_class": "PERMANENT_PROTECTED_CHECKPOINT_VIOLATED",
                    "protected_checkpoints": checkpoints.to_dict(),
                    "bounded_run": contract.event_payload(),
                }
        return None

    def _complete_stage_task(
        self,
        state: WorkflowState,
        lease: str | None,
        task: ScheduledStageTask,
        *,
        validation,
        result,
    ) -> WorkflowState:
        input_fingerprint, output_fingerprint, after, dirty_changes = (
            self._stage_manifest_delta(task)
        )

        if not self._prompt_input_receipt_valid(task, result):
            receipt_id = str(
                result.metadata.get("prompt_input_receipt_id") or ""
            )
            attempt_key_value = str(
                result.metadata.get("prompt_input_attempt_key") or ""
            )
            return self._stage_transition(
                state,
                lease,
                event_type="STEP_FAILED",
                changes={
                    "status": WorkflowStatus.FAILED,
                    "runner_pid": None,
                    "runner_lease_id": None,
                    "heartbeat_at": None,
                },
                payload={
                    "error_class": "PERMANENT_PROMPT_INPUT_RECEIPT_MISSING",
                    "stage": task.stage_id,
                    "subtask": task.subtask,
                    "source_step": task.source_step_id,
                    "prompt_input_receipt_id": receipt_id,
                    "prompt_input_attempt_key": attempt_key_value,
                },
                dirty_changes=dirty_changes,
                event_step=task.source_step_id,
            )

        # S6: the contract's protected manifest and committed checkpoints are
        # verified here, immediately before a successful checkpoint can commit, so
        # a violation blocks the commit instead of being discovered by the caller
        # afterwards.  The entry check answers a different question (was the state
        # already as expected), so both are needed.
        violation = self._contract_protection_violation()
        if violation is not None:
            return self._stage_transition(
                state,
                lease,
                event_type="STEP_FAILED",
                changes={
                    "status": WorkflowStatus.FAILED,
                    "runner_pid": None,
                    "runner_lease_id": None,
                    "heartbeat_at": None,
                },
                payload={
                    **violation,
                    "stage": task.stage_id,
                    "subtask": task.subtask,
                    "source_step": task.source_step_id,
                },
                dirty_changes=dirty_changes,
                event_step=task.source_step_id,
            )

        from .final_judge_projection import classify_delivery_report

        dirty_changes = classify_delivery_report(self.project_dir, task, dirty_changes)
        new_flags = {str(item["flag"]) for item in dirty_changes}
        protected_deleted = [
            item["cause_artifact"]
            for item in dirty_changes
            if str(item["cause_artifact"]).startswith("@protected:")
            and item["current_fingerprint"] == "MISSING"
        ]
        if protected_deleted:
            return self._stage_transition(
                state,
                lease,
                event_type="STEP_FAILED",
                changes={
                    "status": WorkflowStatus.FAILED,
                    "runner_pid": None,
                    "runner_lease_id": None,
                    "heartbeat_at": None,
                },
                payload={
                    "error_class": "PERMANENT_PROTECTED_ITEM_DELETED",
                    "stage": task.stage_id,
                    "subtask": task.subtask,
                    "protected_items": protected_deleted,
                },
                dirty_changes=dirty_changes,
                event_step=task.source_step_id,
            )
        semantic_reopen_target: int | None = None
        semantic_owner_stage: int | None = None
        semantic_reason = ""
        recompute: dict[str, Any] | None = None
        # Phase 4 (Stage catalog v2): a change a completed computation Step
        # consumed is re-owned by that Step's Stage but re-runs only the
        # computation Steps (a recompute), not everything after its owner.
        reopen_causes = [
            item
            for item in dirty_changes
            if int(item["owner_stage"]) < task.stage_id
            and not str(item.get("v2_owner_rule", "")).startswith("v2_input_consumer")
        ]
        upstream_owners = sorted({int(item["owner_stage"]) for item in reopen_causes})
        if upstream_owners:
            semantic_owner_stage = upstream_owners[0]
            semantic_reopen_target = self._catalog(state).resume_after_step_for_stage(
                semantic_owner_stage
            )
            semantic_reason = (
                f"Stage {task.stage_id} changed content owned by "
                f"Stage {semantic_owner_stage}"
            )
        elif state.stage_catalog_version == STAGE_CATALOG_VERSION_V2:
            recompute = self._input_recompute_plan(state, dirty_changes)
            if recompute is None and self._is_v2_review_exit(state, task):
                recompute = self._review_recompute_plan(state, validation)
            if recompute is not None:
                semantic_owner_stage = recompute["owner_stage"]
                semantic_reopen_target = recompute["resume_after_step"]
                reopen_causes = recompute["causes"]
                steps = ", ".join(str(step) for step in recompute["steps"])
                if recompute.get("rule") == REVIEW_ISSUE_REWORK_RULE:
                    semantic_reason = (
                        f"Stage {task.stage_id} has open results/model review "
                        f"issues; recompute Steps {steps}"
                    )
                else:
                    semantic_reason = (
                        f"Stage {task.stage_id} changed an input of completed "
                        f"Step {recompute['start_step']}; recompute Steps {steps}"
                    )

        if semantic_reopen_target is not None:
            budget = self._semantic_reopen_budget(task.stage_id, state)
            if not budget["allowed"]:
                # The Stage cursor stays on this subtask, so a later resume
                # rechecks the same change set through recovery.
                return self._stage_transition(
                    state,
                    lease,
                    event_type="PAUSED",
                    changes={
                        "status": WorkflowStatus.PAUSED,
                        "runner_pid": None,
                        "runner_lease_id": None,
                        "heartbeat_at": None,
                    },
                    payload={
                        "reason": {
                            "code": "PAUSED",
                            "subcode": SEMANTIC_REOPEN_PAUSE_SUBCODE,
                            "actor": "engine",
                            "message": (
                                f"{semantic_reason}; the semantic reopen budget "
                                f"({budget['exhausted']} limit) is spent"
                            ),
                            "evidence": sorted(
                                {
                                    str(item["cause_artifact"])
                                    for item in reopen_causes
                                    if item.get("cause_artifact")
                                }
                            ),
                        },
                        "stage": task.stage_id,
                        "subtask": task.subtask,
                        "source_step": task.source_step_id,
                        "resume_after_step": semantic_reopen_target,
                        "semantic_owner_stage": semantic_owner_stage,
                        "dirty_owner_stages": upstream_owners,
                        "classifier_contract_sha256": self._classifier_sha(state),
                        "reopen_budget": {
                            **{key: value for key, value in budget.items() if key != "allowed"},
                            "causes": [
                                {
                                    "flag": str(item["flag"]),
                                    "owner_stage": int(item["owner_stage"]),
                                    "cause_artifact": str(item.get("cause_artifact") or ""),
                                }
                                for item in reopen_causes
                            ],
                            "options": [
                                "revert_upstream_edits_and_resume",
                                "grant_semantic_reopen",
                            ],
                        },
                    },
                    dirty_changes=dirty_changes,
                    event_step=task.source_step_id,
                )
            return self._stage_transition(
                state,
                lease,
                event_type="STAGE_SEMANTIC_REOPENED",
                changes={
                    "status": WorkflowStatus.RUNNING,
                    "last_completed_step": semantic_reopen_target,
                    "last_completed_stage": self._catalog(state).completed_stage_for_step(
                        semantic_reopen_target
                    ),
                    "active_step": None,
                    "active_stage": None,
                    "active_subtask": None,
                    "source_step_id": None,
                    "attempt": 0,
                },
                payload={
                    "source_step": task.source_step_id,
                    "stage": task.stage_id,
                    "subtask": task.subtask,
                    "resume_after_step": semantic_reopen_target,
                    "semantic_owner_stage": semantic_owner_stage,
                    "dirty_owner_stages": upstream_owners,
                    "reason": semantic_reason,
                    "dirty_flags": sorted(new_flags),
                    "classifier_contract_sha256": self._classifier_sha(state),
                    **(
                        {
                            "rework_rule": recompute.get("rule", "input_dependency"),
                            "recompute_steps": recompute["steps"],
                            "return_subtask": task.subtask,
                            "recompute_reasons": recompute["reasons"],
                            **(
                                {"review_issues": recompute["review_issues"]}
                                if "review_issues" in recompute
                                else {}
                            ),
                        }
                        if recompute is not None
                        else {}
                    ),
                },
                dirty_changes=dirty_changes,
                **(
                    {"invalidate_stage_subtasks": recompute["subtasks"]}
                    if recompute is not None
                    else {"invalidate_checkpoints_after_step": semantic_reopen_target}
                ),
                subtask_baseline=None,
                event_step=task.source_step_id,
            )

        stage8_exit_step = (
            12 if state.stage_catalog_version == STAGE_CATALOG_VERSION_V2 else 13
        )
        if self._is_v2_review_exit(state, task):
            exit_transition = self._v2_review_exit(state, lease, task, validation, dirty_changes)
            if exit_transition is not None:
                return exit_transition
        elif task.stage_id == 8 and task.checkpoint_step_id == stage8_exit_step:
            from .audit.ledger import has_unresolved_critical

            # Step 13 continuation is explicitly authorized by the control-plane
            # provider in its validator. Keep open issues and the judge verdict;
            # this authorization never releases the final delivery gate.
            if (
                has_unresolved_critical(self.project_dir / "audit_issue_ledger.md")
                and validation.metadata.get("gate2_continuation_override") is not True
            ):
                if not self._reopen_allowed(self.registry.get(stage8_exit_step)):
                    return self._stage_transition(
                        state,
                        lease,
                        event_type="STEP_FAILED",
                        changes={
                            "status": WorkflowStatus.FAILED,
                            "runner_pid": None,
                            "runner_lease_id": None,
                            "heartbeat_at": None,
                        },
                        payload={
                            "error_class": "PERMANENT_REVIEW_LOOP_EXHAUSTED",
                            "stage": 8,
                            "subtask": task.subtask,
                            "reason": "unresolved BLOCKING or MAJOR issue remains",
                        },
                        dirty_changes=dirty_changes,
                        event_step=task.source_step_id,
                    )
                return self._stage_transition(
                    state,
                    lease,
                    event_type="STEP_REOPENED",
                    changes={
                        "status": WorkflowStatus.RUNNING,
                        "last_completed_step": 10,
                        "last_completed_stage": 7,
                        "active_step": None,
                        "active_stage": None,
                        "active_subtask": None,
                        "source_step_id": None,
                        "attempt": 0,
                    },
                    payload={
                        "source_step": task.source_step_id,
                        "stage": 8,
                        "subtask": task.subtask,
                        "resume_after_step": 10,
                        "reason": "unresolved BLOCKING or MAJOR issue remains",
                    },
                    dirty_changes=dirty_changes,
                    invalidate_checkpoints_after_step=10,
                    subtask_baseline=None,
                    event_step=task.source_step_id,
                )

        completed = self.store.completed_stage_subtasks()
        completed.add((task.stage_id, task.subtask))
        next_selected = self._catalog(state).next_stage_subtask(
            completed_subtasks=completed
        )
        stage_completed = next_selected is None or next_selected[0].id != task.stage_id
        next_baseline = None
        changes = {
            "status": WorkflowStatus.RUNNING,
            "last_completed_step": (
                task.checkpoint_step_id
                if task.checkpoint_step_id is not None
                else state.last_completed_step
            ),
            "last_completed_stage": (
                task.stage_id if stage_completed else state.last_completed_stage
            ),
            "attempt": 0,
        }
        if next_selected is None:
            changes.update(
                active_step=None,
                active_stage=None,
                active_subtask=None,
                source_step_id=None,
            )
        else:
            next_stage, next_subtask = next_selected
            changes.update(
                active_step=next_subtask.source_step_id,
                active_stage=next_stage.id,
                active_subtask=next_subtask.key,
                source_step_id=next_subtask.source_step_id,
            )
            next_baseline = {
                "stage_id": next_stage.id,
                "subtask": next_subtask.key,
                "source_step_id": next_subtask.source_step_id,
                "input_fingerprint": output_fingerprint,
                "manifest": after,
            }
        validation_metadata = dict(validation.metadata)
        projection_receipts = [
            item["generated_projection_receipt"] for item in dirty_changes
            if "generated_projection_receipt" in item
        ]
        if projection_receipts:
            validation_metadata["generated_projection_receipts"] = projection_receipts
        if task.subtask == "reviewer_entry_gate":
            from scripts.step8_5_gate import collect_step8_5_state

            validation_metadata["step8_5"] = collect_step8_5_state(
                self.project_dir
            )
        checkpoint_receipt = {
            "schema_version": "factory-stage-checkpoint-v1",
            "status": "PASS",
            "stage": task.stage_id,
            "stage_name": task.stage_name,
            "subtask": task.subtask,
            "source_step_id": task.source_step_id,
            "completed_step_id": task.checkpoint_step_id,
            "input_fingerprint": input_fingerprint,
            "output_fingerprint": output_fingerprint,
            "classifier_contract_sha256": self._classifier_sha(state),
            "evidence": list(validation.evidence),
            "validation": validation_metadata,
            "result": result.metadata,
        }
        clear = None
        if stage_completed:
            clear = {
                "owner_stage": task.stage_id,
                "cleared_fingerprint": output_fingerprint,
                "classifier_contract_sha256": self._classifier_sha(state),
                "success_receipt": checkpoint_receipt,
            }
        return self._stage_transition(
            state,
            lease,
            event_type="STEP_SUCCEEDED",
            changes=changes,
            payload={
                "stage": task.stage_id,
                "stage_name": task.stage_name,
                "subtask": task.subtask,
                "source_step": task.source_step_id,
                "stage_completed": stage_completed,
                "input_fingerprint": input_fingerprint,
                "output_fingerprint": output_fingerprint,
                "dirty_flags": sorted(new_flags),
                "evidence": validation.evidence,
                **result.metadata,
            },
            stage_checkpoint={
                "stage_id": task.stage_id,
                "subtask": task.subtask,
                "source_step_id": task.source_step_id,
                "completed_step_id": task.checkpoint_step_id,
                "input_fingerprint": input_fingerprint,
                "output_fingerprint": output_fingerprint,
                "receipt": checkpoint_receipt,
            },
            dirty_changes=dirty_changes,
            clear_dirty_stage=clear,
            subtask_baseline=next_baseline,
            event_step=task.source_step_id,
        )

    def _catalog(self, state: WorkflowState) -> StageCatalog:
        return catalog_for_version(state.stage_catalog_version)

    @staticmethod
    def _classifier_sha(state: WorkflowState) -> str:
        """The classifier identity this project's dirty decisions are bound to."""

        base = classifier_contract_sha256()
        if state.stage_catalog_version == STAGE_CATALOG_VERSION_V2:
            from .stage_catalog_v2_policy import v2_classifier_contract_sha256

            return v2_classifier_contract_sha256(base)
        return base

    def _stage_semantic_reopen_allowed(
        self, stage_id: int, state: WorkflowState | None = None
    ) -> bool:
        return bool(self._semantic_reopen_budget(stage_id, state)["allowed"])

    def _semantic_reopen_budget(
        self, stage_id: int, state: WorkflowState | None = None
    ) -> dict:
        """How much of the semantic-reopen budget the current classifier used.

        Counts ``STAGE_SEMANTIC_REOPENED`` events decided under the current
        classifier identity, per initiating Stage and for the whole run, and
        the operator grants recorded under the same identity.  A grant names
        the Stage it was given for and allows exactly one more reopen: it
        raises that Stage's limit and the run limit by one, and the reopen it
        allows then counts like any other.
        """

        current_classifier = (
            self._classifier_sha(state) if state is not None else classifier_contract_sha256()
        )
        events = self.store.events()
        legacy_boundary = max(
            (
                event.revision
                for event in events
                if event.type == "DIRTY_CLASSIFIER_REBASED"
                and str(event.payload.get("new_classifier_sha256", ""))
                == current_classifier
            ),
            default=0,
        )
        count = 0
        run_count = 0
        stage_grants = 0
        run_grants = 0
        history: list[dict] = []
        for event in events:
            if event.type == SEMANTIC_REOPEN_GRANTED:
                # A grant is bound to the classifier it was given under, like
                # the reopens it allows: a classifier change resets both.
                if str(event.payload.get("classifier_contract_sha256", "")) != current_classifier:
                    continue
                run_grants += 1
                if int(event.payload.get("stage", -1)) == int(stage_id):
                    stage_grants += 1
                continue
            if event.type != "STAGE_SEMANTIC_REOPENED":
                continue
            event_classifier = event.payload.get("classifier_contract_sha256")
            if event_classifier is None:
                # Legacy events did not record their classifier identity.  A
                # later explicit rebase to the current contract is the audit
                # boundary proving that older ownership decisions are stale.
                if event.revision <= legacy_boundary:
                    continue
            elif str(event_classifier) != current_classifier:
                continue
            run_count += 1
            history.append(
                {
                    "revision": event.revision,
                    "stage": int(event.payload.get("stage", -1)),
                    "owner_stage": event.payload.get("semantic_owner_stage"),
                    "resume_after_step": event.payload.get("resume_after_step"),
                }
            )
            if int(event.payload.get("stage", -1)) == int(stage_id):
                count += 1
        stage_limit = STAGE_SEMANTIC_REOPEN_LIMIT + stage_grants
        run_limit = RUN_SEMANTIC_REOPEN_LIMIT + run_grants
        exhausted = (
            "stage" if count >= stage_limit else "run" if run_count >= run_limit else None
        )
        return {
            "allowed": exhausted is None,
            "exhausted": exhausted,
            "stage": int(stage_id),
            "stage_count": count,
            "stage_limit": stage_limit,
            "run_count": run_count,
            "run_limit": run_limit,
            "grants": run_grants,
            "history": history,
        }

    def recover(
        self,
        *,
        expected_runner_pid: int | None = None,
        expected_runner_lease_id: str | None = None,
        enforce_lease: bool = False,
    ) -> WorkflowState:
        state = self.store.load()
        if state.active_step is None:
            return state
        stage_task: ScheduledStageTask | None = None
        if state.scheduler_generation == STAGE_SCHEDULER_GENERATION:
            if state.active_stage is None or state.active_subtask is None:
                raise InvalidTransition(
                    "Stage scheduler recovery requires an atomic Stage/subtask cursor"
                )
            stage, subtask = self._catalog(state).subtask_for_key(state.active_subtask)
            if stage.id != state.active_stage or subtask.source_step_id != state.active_step:
                raise InvalidTransition("Stage recovery cursor is internally inconsistent")
            if subtask.kind != "step":
                definition = self.registry.stage_subtask(subtask.key)
            elif subtask.conditional and not semantic_flags(self.store.dirty_flags()):
                definition = self.registry.stage_subtask(
                    "conditional_math_preflight_skip"
                )
            else:
                definition = self.registry.get(subtask.source_step_id)
            stage_task = ScheduledStageTask(
                stage.id,
                stage.name,
                subtask.key,
                subtask.source_step_id,
                subtask.checkpoint_step_id,
                definition,
            )
        else:
            definition = self.registry.get(state.active_step)
        try:
            timeout_seconds = self._contest_timeout(definition)
            context = self._context(
                state, definition, timeout_seconds=timeout_seconds
            )
            with deadline_scope(context.deadline_epoch):
                decision = definition.lifecycle.recover(
                    context,
                    StepError(error_class="INTERRUPTED", reason="runner interrupted"),
                )
                ensure_deadline(now=self.store.now_epoch())
        except ContestDeadlineExceeded as exc:
            return self._deadline_failure(
                state,
                definition,
                exc,
                stage_task=stage_task,
                expected_runner_pid=expected_runner_pid,
                expected_runner_lease_id=expected_runner_lease_id,
                enforce_lease=enforce_lease,
            )
        decision_event_payload = {}
        if decision.disposition is RecoveryDisposition.REOPEN:
            if not self._reopen_allowed(definition):
                if stage_task is not None:
                    return self._fail_stage_task(
                        state,
                        None,
                        stage_task,
                        event_type="STEP_FAILED",
                        payload={
                            "error_class": "PERMANENT_REOPEN_BUDGET_EXHAUSTED",
                            "source_step": definition.id,
                        },
                        expected_runner_pid=expected_runner_pid,
                        expected_runner_lease_id=expected_runner_lease_id,
                        enforce_lease=enforce_lease,
                    )
                return self._transition(
                    expected_revision=state.revision,
                    event_type="STEP_FAILED",
                    changes={
                        "status": WorkflowStatus.FAILED,
                        "runner_pid": None,
                        "runner_lease_id": None,
                        "heartbeat_at": None,
                    },
                    payload={
                        "error_class": "PERMANENT_REOPEN_BUDGET_EXHAUSTED",
                        "source_step": definition.id,
                    },
                    **self._lease_expectations(
                        expected_runner_pid,
                        expected_runner_lease_id,
                        enforce_lease,
                    ),
                )
            resume_after = self._validated_resume_target(
                decision.resume_after_step, definition.id, self._catalog(state)
            )
            changes = {
                "status": WorkflowStatus.READY,
                "last_completed_step": resume_after,
                "active_step": None,
                "attempt": 0,
                "runner_pid": None,
                "runner_lease_id": None,
                "heartbeat_at": None,
            }
            if stage_task is not None:
                changes.update(
                    last_completed_stage=self._catalog(state).completed_stage_for_step(resume_after),
                    active_stage=None,
                    active_subtask=None,
                    source_step_id=None,
                )
            decision_name = "resume_from_reopen"
        elif decision.disposition is RecoveryDisposition.AWAIT:
            if decision.pending_action is None:
                raise InvalidTransition("await recovery decision is missing pending action")
            action = decision.pending_action.to_dict()
            request = build_decision_request(
                project_id=state.project_id,
                project_dir=self.project_dir,
                requested_revision=state.revision + 1,
                generation=self.store.next_decision_generation(
                    str(action.get("gate") or action.get("type") or "human_decision")
                ),
                action=action,
                reason=decision.reason,
                evidence=decision.evidence,
            )
            action_metadata = dict(action.get("metadata") or {})
            action_metadata["human_decision"] = request.to_dict()
            action["metadata"] = action_metadata
            decision_event_payload = {
                "action": request.to_dict(),
                "pending_action": action,
            }
            status = (
                WorkflowStatus.AWAITING_CONSULTATION
                if request.kind.value == "consultation"
                else WorkflowStatus.AWAITING_SELECTION
            )
            changes = {
                "status": status,
                "pending_action": action,
                "runner_pid": None,
                "runner_lease_id": None,
                "heartbeat_at": None,
            }
            decision_name = "await_pending_action"
        elif decision.disposition is RecoveryDisposition.COMPLETE:
            completed_step = int(decision.completed_through_step or definition.id)
            if completed_step < definition.id:
                raise InvalidTransition(
                    f"step {definition.id} cannot recover through earlier step {completed_step}"
                )
            if stage_task is not None:
                validation = ValidationResult.valid(
                    *decision.evidence,
                    metadata=decision.metadata,
                )
                result = ExecutionResult.succeeded(
                    **{**decision.metadata, "recovered": True}
                )
                recovered = self._complete_stage_task(
                    state,
                    state.runner_lease_id if enforce_lease else None,
                    stage_task,
                    validation=validation,
                    result=result,
                )
                if recovered.status is WorkflowStatus.RUNNING:
                    return self._transition(
                        expected_revision=recovered.revision,
                        event_type="RECOVERY_DECIDED",
                        changes={
                            "status": WorkflowStatus.READY,
                            "runner_pid": None,
                            "runner_lease_id": None,
                            "heartbeat_at": None,
                        },
                        payload={
                            "decision": "promote_valid_stage_subtask",
                            "source": "recovery",
                            "source_step": definition.id,
                            "stage": stage_task.stage_id,
                            "subtask": stage_task.subtask,
                            "reason": decision.reason,
                            "evidence": decision.evidence,
                            **decision.metadata,
                        },
                    )
                return recovered
            changes = {
                "status": WorkflowStatus.READY,
                "last_completed_step": completed_step,
                "active_step": None,
                "attempt": 0,
                "runner_pid": None,
                "runner_lease_id": None,
                "heartbeat_at": None,
            }
            decision_name = "promote_valid_artifacts"
        elif decision.disposition is RecoveryDisposition.RETRY:
            changes = {
                "status": WorkflowStatus.RETRYING,
                "runner_pid": None,
                "runner_lease_id": None,
                "heartbeat_at": None,
            }
            decision_name = "retry_incomplete_step"
        else:
            if stage_task is not None:
                return self._fail_stage_task(
                    state,
                    None,
                    stage_task,
                    event_type="RECOVERY_DECIDED",
                    payload={
                        "decision": "fail_recovery",
                        "source": "recovery",
                        "source_step": definition.id,
                        "reason": decision.reason,
                        "evidence": decision.evidence,
                        **decision.metadata,
                    },
                    expected_runner_pid=expected_runner_pid,
                    expected_runner_lease_id=expected_runner_lease_id,
                    enforce_lease=enforce_lease,
                )
            changes = {
                "status": WorkflowStatus.FAILED,
                "runner_pid": None,
                "runner_lease_id": None,
                "heartbeat_at": None,
            }
            decision_name = "fail_recovery"
        event_type = (
            "STEP_REOPENED"
            if decision.disposition is RecoveryDisposition.REOPEN
            else "RECOVERY_DECIDED"
        )
        stage_recovery_kwargs = {}
        if stage_task is not None and decision.disposition is RecoveryDisposition.REOPEN:
            stage_recovery_kwargs = {
                "invalidate_checkpoints_after_step": changes["last_completed_step"],
                "subtask_baseline": None,
            }
        return self._transition(
            expected_revision=state.revision,
            event_type=event_type,
            changes=changes,
            payload={
                "decision": decision_name,
                "source": "recovery",
                "source_step": definition.id,
                "reason": decision.reason,
                "evidence": decision.evidence,
                **decision.metadata,
                **decision_event_payload,
            },
            **self._lease_expectations(
                expected_runner_pid,
                expected_runner_lease_id,
                enforce_lease,
            ),
            **stage_recovery_kwargs,
        )

    def _await_action(
        self,
        state: WorkflowState,
        action: dict,
        *,
        reason: str,
        evidence: tuple[str, ...],
        event_type: str = "AWAITING_ACTION",
        lease: str | None = None,
    ) -> WorkflowState:
        try:
            request = build_decision_request(
                project_id=state.project_id,
                project_dir=self.project_dir,
                requested_revision=state.revision + 1,
                generation=self.store.next_decision_generation(
                    str(action.get("gate") or action.get("type") or "human_decision")
                ),
                action=action,
                reason=reason,
                evidence=evidence,
            )
        except Exception as exc:
            transition = self._transition if lease is None else self._owned_transition
            args = () if lease is None else (state, lease)
            kwargs = {"expected_revision": state.revision} if lease is None else {}
            return transition(
                *args,
                event_type="DECISION_REQUEST_BUILD_FAILED",
                changes={
                    "status": WorkflowStatus.FAILED,
                    "pending_action": None,
                    "runner_pid": None,
                    "runner_lease_id": None,
                    "heartbeat_at": None,
                },
                payload={
                    "error_class": "PERMANENT_DECISION_REQUEST_BUILD_FAILED",
                    "exception_type": type(exc).__name__,
                    "reason": str(exc),
                    "gate": str(action.get("gate") or ""),
                    "action_type": str(action.get("type") or ""),
                    "evidence": evidence,
                },
                **kwargs,
            )
        action = dict(action)
        metadata = dict(action.get("metadata") or {})
        metadata["human_decision"] = request.to_dict()
        action["metadata"] = metadata
        awaiting_status = (
            WorkflowStatus.AWAITING_CONSULTATION
            if request.kind.value == "consultation"
            else WorkflowStatus.AWAITING_SELECTION
        )
        transition = self._transition if lease is None else self._owned_transition
        args = () if lease is None else (state, lease)
        kwargs = {"expected_revision": state.revision} if lease is None else {}
        return transition(
            *args,
            event_type=event_type,
            changes={
                "status": awaiting_status,
                "pending_action": action,
                "runner_pid": None,
                "runner_lease_id": None,
                "heartbeat_at": None,
            },
            payload={
                "reason": request.reason,
                "evidence": evidence,
                "action": request.to_dict(),
                "pending_action": action,
            },
            **kwargs,
        )

    def semantic_reopen_pause(self, state: WorkflowState | None = None) -> WorkflowEvent | None:
        """The budget pause the project is currently stopped at, if any.

        It is the latest ``PAUSED`` event, provided the engine (not an
        operator) recorded it for a spent semantic-reopen budget and no later
        event has changed the status.
        """

        from .workflow_events import current_engine_pause

        state = state or self.store.load()
        if state.status is not WorkflowStatus.PAUSED:
            return None
        return current_engine_pause(
            self.store.events(), {SEMANTIC_REOPEN_PAUSE_SUBCODE}
        )

    def grant_semantic_reopen(
        self,
        *,
        expected_revision: int,
        note: str,
        actor: str = "operator",
    ) -> WorkflowState:
        """Allow the one reopen a budget pause stopped (phase 3a).

        Recorded as ``SEMANTIC_REOPEN_GRANTED`` for the paused Stage under the
        current classifier identity; the project stays paused, and the next
        resume rechecks the change set through recovery, which then performs
        the reopen.  One grant per pause.
        """

        state = self.store.load()
        if not str(note or "").strip():
            raise InvalidTransition("a semantic reopen grant needs a reason")
        pause = self.semantic_reopen_pause(state)
        if pause is None:
            raise InvalidTransition(
                "the project is not paused at a spent semantic reopen budget"
            )
        if any(
            event.type == SEMANTIC_REOPEN_GRANTED and event.revision > pause.revision
            for event in self.store.events()
        ):
            raise InvalidTransition("this budget pause already has a grant")
        return self._transition(
            expected_revision=expected_revision,
            event_type=SEMANTIC_REOPEN_GRANTED,
            changes={},
            payload={
                "stage": int(pause.payload["stage"]),
                "subtask": pause.payload.get("subtask"),
                "paused_revision": pause.revision,
                "resume_after_step": pause.payload.get("resume_after_step"),
                "classifier_contract_sha256": self._classifier_sha(state),
                "reason": {
                    "code": SEMANTIC_REOPEN_GRANTED,
                    "subcode": "OPERATOR",
                    "actor": actor,
                    "message": str(note).strip(),
                },
            },
        )

    def retry_step(
        self,
        *,
        expected_revision: int,
        step_id: int,
        note: str,
        actor: str = "operator",
    ) -> WorkflowState:
        """Retry the failed Step at the cursor with a fresh attempt budget (3b).

        Only a ``FAILED`` project whose cursor is on ``step_id`` qualifies; the
        Stage cursor is kept and the attempt count reset, so the next run
        starts that Step's attempt 1 instead of recovering the failed attempt.
        Nothing else changes: dirty state, checkpoints, the contest deadline
        and every other budget still apply.
        """

        state = self.store.load()
        if not str(note or "").strip():
            raise InvalidTransition("a Step retry needs a reason")
        if state.status is not WorkflowStatus.FAILED:
            raise InvalidTransition(
                f"only a failed project can retry a Step (status {state.status.value})"
            )
        if state.pending_action is not None:
            raise InvalidTransition("pending action must be resolved before a retry")
        if state.active_step is None or int(state.active_step) != int(step_id):
            raise InvalidTransition(
                f"the failed cursor is on Step {state.active_step}, not Step {step_id}"
            )
        failure = next(
            (event for event in reversed(self.store.events()) if event.type == "STEP_FAILED"),
            None,
        )
        return self._transition(
            expected_revision=expected_revision,
            event_type=STEP_RETRY_AUTHORIZED,
            changes={
                "status": WorkflowStatus.READY,
                "attempt": 0,
                "runner_pid": None,
                "runner_lease_id": None,
                "heartbeat_at": None,
            },
            payload={
                "stage": state.active_stage,
                "subtask": state.active_subtask,
                "source_step": int(step_id),
                "previous_attempt": state.attempt,
                "failed_revision": failure.revision if failure is not None else None,
                "failed_error_class": (
                    failure.payload.get("error_class") if failure is not None else None
                ),
                "reason": {
                    "code": STEP_RETRY_AUTHORIZED,
                    "subcode": "OPERATOR",
                    "actor": actor,
                    "message": str(note).strip(),
                },
            },
        )

    def pause(
        self,
        *,
        expected_revision: int,
        subcode: str = "OPERATOR",
        actor: str = "operator",
    ) -> WorkflowState:
        return self._control_transition(
            expected_revision,
            "PAUSED",
            WorkflowStatus.PAUSED,
            subcode=subcode,
            actor=actor,
        )

    def resume(
        self,
        *,
        expected_revision: int,
        subcode: str = "OPERATOR",
        actor: str = "operator",
    ) -> WorkflowState:
        state = self.store.load()
        if state.runner_pid is not None and self._pid_is_live(state.runner_pid):
            raise InvalidTransition(
                f"project already has live worker {state.runner_pid}"
            )
        if state.status is WorkflowStatus.KILLED:
            raise InvalidTransition("killed projects cannot be resumed")
        if state.status not in {
            WorkflowStatus.READY,
            WorkflowStatus.PAUSED,
            WorkflowStatus.FAILED,
            WorkflowStatus.INTERRUPTED,
            WorkflowStatus.AWAITING_SELECTION,
            WorkflowStatus.AWAITING_CONSULTATION,
        }:
            raise InvalidTransition(f"projects in {state.status.value} state cannot be resumed")
        if state.pending_action is not None:
            raise InvalidTransition("pending action must be resolved before resume")
        preserve_interrupted_step = (
            state.status is WorkflowStatus.INTERRUPTED
            and state.active_step is not None
        )
        preserve_stage_subtask = (
            state.scheduler_generation == STAGE_SCHEDULER_GENERATION
            and state.active_subtask is not None
        )
        changes = {
            "status": WorkflowStatus.READY,
            "runner_pid": None,
            "runner_lease_id": None,
            "heartbeat_at": None,
        }
        if not preserve_interrupted_step and not preserve_stage_subtask:
            changes.update(active_step=None, attempt=0)
        return self._transition(
            expected_revision=expected_revision,
            event_type="RESUMED",
            changes=changes,
            payload={"reason": {"code": "RESUMED", "subcode": subcode, "actor": actor}},
        )

    def _stop_at_gate2(self, state, lease, result, validation):
        from .technical_continuation import stop_at_gate2
        return stop_at_gate2(self, state, lease, result, validation)

    def kill(self, *, expected_revision: int) -> WorkflowState:
        return self._control_transition(expected_revision, "KILLED", WorkflowStatus.KILLED)

    def resolve_action(
        self,
        resolution: dict,
        *,
        expected_revision: int,
        decision_record: dict | None = None,
    ) -> WorkflowState:
        state = self.store.load()
        if state.pending_action is None:
            raise InvalidTransition("project has no pending action")
        resolution = validate_resolution(state.pending_action, resolution)
        resolution = self._content_freeze_acknowledgements(state, resolution)
        return self._transitions.resolve_human_decision(
            expected_revision=expected_revision,
            resolution=resolution,
            decision_record=decision_record,
        )

    def _content_freeze_acknowledgements(
        self, state: WorkflowState, resolution: dict
    ) -> dict:
        """Bind the open human-owned ledger rows to a content-freeze approval.

        A BLOCKING row whose resolver is a human or Step 14/15
        (``ledger.deferred_blocking_issues``) passes the v2 Stage 8 exit for
        Gate 2 (F16); the final audit refuses any open BLOCKING row (F17).
        The approval therefore names the rows it takes on in
        ``acknowledged_human_issues``; the immutable decision carries them and
        the final audit stops counting exactly those rows.  In catalog v2 an
        approval that leaves such a row unacknowledged is refused here, while
        the decision can still be changed, instead of failing Step 13 later.
        """

        raw = resolution.get("acknowledged_human_issues")
        if resolution.get("gate") != "content_freeze":
            if raw is not None:
                raise InvalidTransition(
                    "acknowledged_human_issues is a content-freeze field"
                )
            return resolution
        from .audit.ledger import deferred_blocking_issues

        if raw is not None and (
            not isinstance(raw, list)
            or not all(isinstance(value, str) and value.strip() for value in raw)
        ):
            raise InvalidTransition(
                "acknowledged_human_issues must be a list of ledger issue ids"
            )
        acknowledged = sorted({value.strip() for value in raw or []})
        if acknowledged and state.stage_catalog_version != STAGE_CATALOG_VERSION_V2:
            # Only the v2 Stage 8 exit lets a human-owned BLOCKING row reach
            # Gate 2; v1 keeps its delivery rules unchanged.
            raise InvalidTransition(
                "acknowledged_human_issues is a Stage catalog v2 field"
            )
        if acknowledged and resolution.get("approved") is not True:
            raise InvalidTransition(
                "only a content-freeze approval can acknowledge ledger rows"
            )
        deferred = sorted(
            issue.issue_id
            for issue in deferred_blocking_issues(
                self.project_dir / "audit_issue_ledger.md"
            )
        )
        unknown = sorted(set(acknowledged) - set(deferred))
        if unknown:
            raise InvalidTransition(
                "acknowledged_human_issues names rows that are not open BLOCKING rows "
                f"owned by a human or Step 14/15: {', '.join(unknown)} "
                f"(acknowledgeable: {', '.join(deferred) or 'none'})"
            )
        missing = sorted(set(deferred) - set(acknowledged))
        if (
            missing
            and resolution.get("approved") is True
            and state.stage_catalog_version == STAGE_CATALOG_VERSION_V2
        ):
            raise InvalidTransition(
                "content-freeze approval must acknowledge the open BLOCKING ledger rows "
                f"that only a human or Step 14/15 resolves: {', '.join(missing)}; "
                "list them in acknowledged_human_issues, or close them in "
                "audit_issue_ledger.md before approving"
            )
        if raw is None:
            return resolution
        return {**resolution, "acknowledged_human_issues": acknowledged}

    def _control_transition(
        self,
        expected_revision: int,
        event: str,
        status: WorkflowStatus,
        *,
        subcode: str = "",
        actor: str = "",
    ) -> WorkflowState:
        """A control transition, with a structured reason (S4.1).

        ``subcode`` and ``actor`` record *why* and *who* without adding a new
        top-level event type: the canonical ``code`` stays the discriminator.
        """

        return self._transition(
            expected_revision=expected_revision,
            event_type=event,
            changes={
                "status": status,
                "runner_pid": None,
                "runner_lease_id": None,
                "heartbeat_at": None,
            },
            payload={"reason": {"code": event, "subcode": subcode, "actor": actor}},
        )

    def deactivate(self, *, expected_revision: int, legacy_inferred_step: int | None = None) -> WorkflowState:
        raise InvalidTransition("Legacy workflow deactivation is retired; use paper_new for historical recovery")

    def archive_completed(self, factory_root: str | Path) -> WorkflowState:
        root = Path(factory_root).resolve()
        state = self.store.load()
        if state.status not in {WorkflowStatus.ARCHIVING, WorkflowStatus.COMPLETED}:
            raise InvalidTransition("only completed projects can be archived")
        if state.status is WorkflowStatus.COMPLETED and self.project_dir.parent.name == "complete":
            return state

        from .native_boundary import delivery_side_effect_commit_lease

        with delivery_side_effect_commit_lease(
            self.project_dir,
            operation="archive",
        ):
            return self._archive_completed_locked(root, state)

    def _archive_completed_locked(
        self, root: Path, state: WorkflowState
    ) -> WorkflowState:
        if state.status is WorkflowStatus.ARCHIVING:
            if self.project_dir.parent.name == "complete":
                return self._transition(
                    expected_revision=state.revision,
                    event_type="PROJECT_ARCHIVED",
                    changes={"status": WorkflowStatus.COMPLETED, "storage_scope": "complete"},
                )
            destination = root / "complete" / self.project_dir.name
            if destination.exists():
                raise InvalidTransition(f"archive destination already exists: {destination}")
            self.store.prepare_for_move()
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(self.project_dir), str(destination))
            self.project_dir = destination
            self.store = SQLiteStateStore(destination)
            self._pipeline = StageExecutionPipeline(self.store.now_epoch)
            self._transitions.relocate(destination, self.store)
            return self._transition(
                expected_revision=state.revision,
                event_type="PROJECT_ARCHIVED",
                changes={"status": WorkflowStatus.COMPLETED, "storage_scope": "complete"},
            )
        destination = root / "complete" / self.project_dir.name
        if destination.exists():
            raise InvalidTransition(f"archive destination already exists: {destination}")
        state = self._transition(
            expected_revision=state.revision,
            event_type="PROJECT_ARCHIVE_REQUESTED",
            changes={"status": WorkflowStatus.ARCHIVING},
            payload={"destination": str(destination)},
        )
        self.store.prepare_for_move()
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.project_dir), str(destination))
        self.project_dir = destination
        self.store = SQLiteStateStore(destination)
        self._pipeline = StageExecutionPipeline(self.store.now_epoch)
        self._transitions.relocate(destination, self.store)
        return self._transition(
            expected_revision=state.revision,
            event_type="PROJECT_ARCHIVED",
            changes={"status": WorkflowStatus.COMPLETED, "storage_scope": "complete"},
        )

    def _commit_project_completed(
        self,
        state: WorkflowState,
        *,
        stage_mode: bool,
        runner_lease: str | None = None,
    ) -> WorkflowState:
        from .native_boundary import delivery_side_effect_commit_lease

        with delivery_side_effect_commit_lease(
            self.project_dir,
            operation="completion",
        ):
            changes = {
                "status": WorkflowStatus.COMPLETED,
                "active_step": None,
                "active_stage": None,
                "active_subtask": None,
                "source_step_id": None,
                "last_completed_stage": (
                    10 if stage_mode else state.last_completed_stage
                ),
                "attempt": 0,
                "runner_pid": None,
                "runner_lease_id": None,
                "heartbeat_at": None,
            }
            if runner_lease is None:
                return self._transition(
                    expected_revision=state.revision,
                    event_type="PROJECT_COMPLETED",
                    changes=changes,
                    subtask_baseline=None,
                )
            return self._owned_transition(
                state,
                runner_lease,
                event_type="PROJECT_COMPLETED",
                changes=changes,
                subtask_baseline=None,
            )

    def _transition(self, **kwargs) -> WorkflowState:
        return self._transitions.transition(**kwargs)

    def _owned_transition(
        self,
        state: WorkflowState,
        lease: str,
        **kwargs,
    ) -> WorkflowState:
        """Commit a worker event only while PID and lease still match atomically."""
        while True:
            try:
                return self._transition(
                    expected_revision=state.revision,
                    expected_runner_pid=os.getpid(),
                    expected_runner_lease_id=lease,
                    **kwargs,
                )
            except RevisionConflict:
                state = self._refresh_owned_state(lease)

    def _stage_transition(
        self,
        state: WorkflowState,
        lease: str | None,
        **kwargs,
    ) -> WorkflowState:
        if lease is not None:
            return self._owned_transition(state, lease, **kwargs)
        return self._transition(
            expected_revision=state.revision,
            **kwargs,
        )

    def _refresh_owned_state(
        self, lease: str, *, active_step: int | None = None
    ) -> WorkflowState:
        state = self.store.load()
        if state.runner_pid != os.getpid() or state.runner_lease_id != lease:
            raise RunnerLeaseLost(
                f"runner lease lost at revision {state.revision}"
            )
        if active_step is not None and state.active_step != active_step:
            raise RunnerLeaseLost(
                f"step {active_step} lost runner ownership at revision {state.revision}"
            )
        return state

    @staticmethod
    def _lease_expectations(
        runner_pid: int | None,
        runner_lease_id: str | None,
        enforce: bool,
    ) -> dict:
        if not enforce:
            return {}
        return {
            "expected_runner_pid": runner_pid,
            "expected_runner_lease_id": runner_lease_id,
        }

    def _context(
        self,
        state: WorkflowState,
        definition: StepDefinition,
        *,
        timeout_seconds: int | None = None,
    ) -> StepContext:
        return StepContext(
            project_dir=self.project_dir,
            project_id=state.project_id,
            step_id=definition.id,
            attempt=state.attempt,
            timeout_seconds=(
                definition.timeout_seconds
                if timeout_seconds is None
                else timeout_seconds
            ),
            revision=state.revision,
            deadline_epoch=self._contest_deadline(definition),
            clock=self.store.clock,
        )

    def _contest_deadline(self, definition: StepDefinition) -> int | None:
        payload = self.store.contest_policy()
        if payload is None:
            return None
        policy = ContestPolicy.from_dict(payload)
        return (
            policy.contest_deadline_at
            if definition.id == 16 or definition.name == FINAL_JUDGE_SUBTASK
            else policy.content_freeze_at
        )

    def _deadline_failure(
        self,
        state: WorkflowState,
        definition: StepDefinition,
        exc: ContestDeadlineExceeded,
        *,
        lease: str | None = None,
        stage_task: ScheduledStageTask | None = None,
        expected_runner_pid: int | None = None,
        expected_runner_lease_id: str | None = None,
        enforce_lease: bool = False,
    ) -> WorkflowState:
        changes = {
            "status": WorkflowStatus.FAILED,
            "runner_pid": None,
            "runner_lease_id": None,
            "heartbeat_at": None,
        }
        payload = {
            "error_class": "PERMANENT_CONTEST_DEADLINE",
            "reason": str(exc),
            "step": definition.id,
        }
        if stage_task is not None:
            return self._fail_stage_task(
                state,
                lease,
                stage_task,
                event_type="CONTEST_DEADLINE_EXHAUSTED",
                payload=payload,
            )
        if lease is not None:
            return self._owned_transition(
                state,
                lease,
                event_type="CONTEST_DEADLINE_EXHAUSTED",
                changes=changes,
                payload=payload,
            )
        return self._transition(
            expected_revision=state.revision,
            event_type="CONTEST_DEADLINE_EXHAUSTED",
            changes=changes,
            payload=payload,
            **self._lease_expectations(
                expected_runner_pid,
                expected_runner_lease_id,
                enforce_lease,
            ),
        )

    def _contest_timeout(
        self,
        definition: StepDefinition,
        *,
        step_timeout: int | None = None,
    ) -> int:
        payload = self.store.contest_policy()
        configured = definition.timeout_seconds if step_timeout is None else step_timeout
        if payload is None:
            return configured
        # The v2 final Judge (Step 13) runs after the content-freeze approval,
        # inside the terminal reserve that v1 gave to Step 16, which ran the
        # same Judge there; it is bounded by the contest deadline like Step 16.
        deadline_step = 16 if definition.name == FINAL_JUDGE_SUBTASK else definition.id
        return effective_timeout(
            ContestPolicy.from_dict(payload),
            step_id=deadline_step,
            step_timeout=configured,
            now=self.store.now_epoch(),
        )

    @staticmethod
    def _validated_resume_target(
        value: object, active_step: int, catalog: StageCatalog | None = None
    ) -> int:
        target = int(value)
        if catalog is not None and catalog.version == STAGE_CATALOG_VERSION_V2:
            # v2 runs Step 13 after 14 and 15: "earlier" is catalog position.
            try:
                earlier = catalog.step_position(target) < catalog.step_position(active_step)
            except KeyError:
                earlier = False
            if target < -1 or not earlier:
                raise InvalidTransition(
                    f"step {active_step} cannot reopen after invalid step {target}"
                )
            return target
        if target < -1 or target >= active_step:
            raise InvalidTransition(
                f"step {active_step} cannot reopen after invalid step {target}"
            )
        return target

    def _reopen_allowed(self, definition: StepDefinition) -> bool:
        if definition.max_reopens <= 0:
            return False
        count = 0
        for event in self.store.events():
            if event.type != "STEP_REOPENED":
                continue
            source = event.payload.get("source_step")
            if source is None or int(source) == definition.id:
                count += 1
        return count < definition.max_reopens

    @staticmethod
    def _retry_delay(attempt: int) -> int:
        return (30, 60, 120, 300, 600)[min(max(attempt - 1, 0), 4)]

    def _missing_evidence(self, evidence: tuple[str, ...]) -> list[str]:
        missing: list[str] = []
        for relative in evidence:
            path = Path(relative)
            if path.is_absolute() or "::" in relative or "#" in relative:
                continue
            if not (self.project_dir / path).is_file():
                missing.append(relative)
        return missing

    @staticmethod
    def _failure_reason(result: ExecutionResult, validation_reason: str) -> str:
        """The reason a failure event records.

        When the model backend itself failed, the validator's message only
        describes the consequence (an artifact the model never wrote), so it
        used to read as a content or gate refusal.  The event then names the
        backend failure first and keeps the validator's message after it (and
        in ``validation_reason``).  Every other failure keeps the validator's
        message, which is the precise one for audits, judges and gates.
        """

        if result.error_class not in BACKEND_FAILURE_ERROR_CLASSES:
            return validation_reason
        metadata = result.metadata
        own = str(metadata.get("reason") or "").strip()
        backend = str(metadata.get("model_id") or metadata.get("backend") or "model backend")
        duration = metadata.get("duration_seconds")
        # Dispatcher-level config failures (unknown or quarantined model,
        # missing credential, launch error) never ran a process; their
        # returncode is synthetic and must not be reported as an exit.
        ran = metadata.get("process_pid") is not None or isinstance(duration, (int, float))
        if own:
            head = f"{result.error_class} from {backend}: {own}"
        elif metadata.get("missing_key_env"):
            head = f"{result.error_class}: {backend} not started, {metadata['missing_key_env']} is not set"
        elif ran and not metadata.get("launch_error"):
            head = f"{result.error_class}: {backend} exited with returncode {result.returncode}"
            if isinstance(duration, (int, float)):
                head += f" after {float(duration):.1f} s"
        else:
            head = f"{result.error_class}: {backend} failed before running"
        return f"{head}; then {validation_reason}" if validation_reason else head

    @staticmethod
    def _terminal_error_class(error_class: str, *, exhausted: bool) -> str:
        if exhausted and error_class.startswith("TRANSIENT_JUDGE"):
            return "PERMANENT_JUDGE_INFRASTRUCTURE"
        if exhausted and error_class in NON_CONTENT_ERROR_CLASSES:
            return NON_CONTENT_ERROR_CLASSES[error_class]
        return error_class

    def _non_content_retries(
        self,
        step_id: int,
        error_class: str | None = None,
        *,
        terminal_class: str | None = None,
    ) -> int:
        """Non-content retries in the current attempt sequence of ``step_id``.

        Derived from the event stream rather than a state field, so replay
        stays exact and no member is added to the replayed state: a sequence
        starts at the latest Stage-subtask selection or the Step's latest
        ``STEP_STARTED`` with attempt 1, whichever is later, and every
        ``RETRY_SCHEDULED`` of a class in :data:`NON_CONTENT_ERROR_CLASSES`
        since then is counted (only those of ``error_class``, or of a class
        ending as ``terminal_class``, when given).
        """

        count = 0
        for event in reversed(self.store.events()):
            if event.type == "STAGE_SUBTASK_SELECTED":
                break
            if event.step != step_id:
                continue
            recorded = str(event.payload.get("error_class") or "")
            if event.type == "RETRY_SCHEDULED" and recorded in NON_CONTENT_ERROR_CLASSES and (
                error_class is None or recorded == error_class
            ) and (
                terminal_class is None or NON_CONTENT_ERROR_CLASSES[recorded] == terminal_class
            ):
                count += 1
            elif event.type == "STEP_STARTED" and event.attempt <= 1:
                break
        return count

    @staticmethod
    def _pid_is_live(pid: int) -> bool:
        """Whether the recorded runner still exists.

        Delegates to :func:`factory_core.liveness.pid_is_live`, which is the one
        implementation: ``engine`` and ``service`` had drifted apart, and both read
        EPERM as death, which would let a second runner start on a project another
        user's runner is still advancing.
        """

        return pid_is_live(pid)
