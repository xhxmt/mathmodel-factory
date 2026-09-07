from __future__ import annotations

import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

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
    WorkflowState,
    WorkflowStatus,
)
from .execution_pipeline import StageExecutionPipeline, StageExecutionRequest
from .human_decisions import build_decision_request, validate_resolution
from .registry import StepDefinition, StepRegistry
from .storage import SQLiteStateStore
from .stages import (
    STAGE_CATALOG_VERSION,
    STAGE_SCHEDULER_GENERATION,
    STEP_SCHEDULER_GENERATION,
    completed_stage_for_step,
    next_stage_subtask,
    resume_after_step_for_stage,
    stage_for_id,
    subtask_for_key,
)
from .transitions import TransitionCoordinator


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
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.store = store or SQLiteStateStore(self.project_dir)
        self.registry = registry or StepRegistry()
        self._sleep = sleeper
        self._projector = projector
        self._pipeline = StageExecutionPipeline(self.store.now_epoch)
        self._transitions = TransitionCoordinator(
            self.project_dir, self.store, self._projector
        )

    def get_state(self) -> WorkflowState:
        return self.store.load()

    def run(self, *, max_steps: int | None = None) -> WorkflowState:
        state = self.store.load()
        if state.scheduler_generation not in {
            STEP_SCHEDULER_GENERATION,
            STAGE_SCHEDULER_GENERATION,
        }:
            raise InvalidTransition(
                f"unsupported scheduler generation: {state.scheduler_generation}"
            )
        stage_mode = state.scheduler_generation == STAGE_SCHEDULER_GENERATION
        if stage_mode and state.stage_catalog_version != STAGE_CATALOG_VERSION:
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
            payload={"lease_id": lease},
            expected_runner_pid=state.runner_pid,
            expected_runner_lease_id=state.runner_lease_id,
        )
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
            attempt = state.attempt + 1 if state.active_step == definition.id else 1
            if stage_mode and state.attempt >= definition.max_attempts:
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
                    definition.id == 16
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
                resume_after = self._validated_resume_target(resume_after, definition.id)
                reopen_changes = {
                    "status": WorkflowStatus.RUNNING,
                    "last_completed_step": resume_after,
                    "active_step": None,
                    "attempt": 0,
                }
                reopen_kwargs = {}
                if stage_task is not None:
                    reopen_changes.update(
                        last_completed_stage=completed_stage_for_step(resume_after),
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
                        WorkflowStatus.AWAITING_SELECTION,
                        WorkflowStatus.AWAITING_CONSULTATION,
                    }:
                        return state
                    continue
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
            permanent = error_class.startswith("PERMANENT")
            if permanent or attempt >= definition.max_attempts:
                terminal_error_class = self._terminal_error_class(
                    error_class, exhausted=not permanent
                )
                if terminal_error_class != error_class:
                    failure_metadata["exhausted_error_class"] = error_class
                terminal_payload = {
                    **failure_metadata,
                    "error_class": terminal_error_class,
                    "reason": validation.reason,
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
                    "reason": validation.reason,
                    "delay_seconds": retry_delay,
                },
            )
            self._sleep(retry_delay)

    def _select_stage_task(
        self, state: WorkflowState, lease: str
    ) -> tuple[WorkflowState, ScheduledStageTask | None]:
        state = self._invalidate_stale_stage_receipts(state, lease)
        completed = self.store.completed_stage_subtasks()
        selected = None
        if state.active_subtask is not None:
            stage, subtask = subtask_for_key(state.active_subtask)
            if state.active_stage != stage.id:
                raise InvalidTransition(
                    "persisted Stage and subtask do not belong to the same catalog entry"
                )
            selected = (stage, subtask)
        else:
            selected = next_stage_subtask(completed_subtasks=completed)
        if selected is None:
            return state, None
        stage, subtask = selected
        if subtask.key in {"reviewer_entry_gate", "content_freeze_guard"}:
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
                "stage_catalog_version": STAGE_CATALOG_VERSION,
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
        current_classifier = classifier_contract_sha256()
        classifier_stale = any(
            item.get("receipt", {}).get("classifier_contract_sha256")
            != current_classifier
            for item in stage9_checkpoints
        )
        result_metadata = (
            conditional.get("receipt", {}).get("result", {})
            if conditional is not None
            else {}
        )
        if classifier_stale or (
            result_metadata.get("conditional_math_preflight") == "skipped"
            and not stage9_checkpoints
        ):
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
                    "classifier_contract_sha256": classifier_contract_sha256(),
                },
                {
                    "flag": DirtyFlag.RESULT.value,
                    "owner_stage": 4,
                    "cause_artifact": "MISSING_STAGE_INPUT_BASELINE",
                    "baseline_fingerprint": "MISSING",
                    "current_fingerprint": output_fingerprint,
                    "classifier_contract_sha256": classifier_contract_sha256(),
                },
            ]
            return "MISSING", output_fingerprint, after, dirty_changes
        before = dict(baseline["manifest"])
        dirty_changes = []
        for change in classify_manifest_changes(before, after):
            record = {
                **change.to_dict(),
                "classifier_contract_sha256": classifier_contract_sha256(),
            }
            receipt_owner = self._solver_receipt_owner_stage(change.cause_artifact)
            if receipt_owner is not None:
                record["owner_stage"] = receipt_owner
            dirty_changes.append(record)
        return (
            str(baseline["input_fingerprint"]),
            output_fingerprint,
            after,
            dirty_changes,
        )

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
        upstream_owners = sorted(
            {
                int(item["owner_stage"])
                for item in dirty_changes
                if int(item["owner_stage"]) < task.stage_id
            }
        )
        if upstream_owners:
            semantic_owner_stage = upstream_owners[0]
            semantic_reopen_target = resume_after_step_for_stage(
                semantic_owner_stage
            )
            semantic_reason = (
                f"Stage {task.stage_id} changed content owned by "
                f"Stage {semantic_owner_stage}"
            )

        if semantic_reopen_target is not None:
            if not self._stage_semantic_reopen_allowed(task.stage_id):
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
                        "error_class": "PERMANENT_STAGE_SEMANTIC_REOPEN_EXHAUSTED",
                        "stage": task.stage_id,
                        "subtask": task.subtask,
                        "resume_after_step": semantic_reopen_target,
                        "semantic_owner_stage": semantic_owner_stage,
                        "reason": semantic_reason,
                        "classifier_contract_sha256": classifier_contract_sha256(),
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
                    "last_completed_stage": completed_stage_for_step(
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
                    "classifier_contract_sha256": classifier_contract_sha256(),
                },
                dirty_changes=dirty_changes,
                invalidate_checkpoints_after_step=semantic_reopen_target,
                subtask_baseline=None,
                event_step=task.source_step_id,
            )

        if task.stage_id == 8 and task.checkpoint_step_id == 13:
            from .audit.ledger import has_unresolved_critical

            if has_unresolved_critical(self.project_dir / "audit_issue_ledger.md"):
                if not self._reopen_allowed(self.registry.get(13)):
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
                        "source_step": 13,
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
        next_selected = next_stage_subtask(completed_subtasks=completed)
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
            "classifier_contract_sha256": classifier_contract_sha256(),
            "evidence": list(validation.evidence),
            "validation": validation_metadata,
            "result": result.metadata,
        }
        clear = None
        if stage_completed:
            clear = {
                "owner_stage": task.stage_id,
                "cleared_fingerprint": output_fingerprint,
                "classifier_contract_sha256": classifier_contract_sha256(),
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

    def _stage_semantic_reopen_allowed(self, stage_id: int) -> bool:
        current_classifier = classifier_contract_sha256()
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
        for event in events:
            if event.type != "STAGE_SEMANTIC_REOPENED":
                continue
            if int(event.payload.get("stage", -1)) != int(stage_id):
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
            count += 1
        return count < 2

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
            stage, subtask = subtask_for_key(state.active_subtask)
            if stage.id != state.active_stage or subtask.source_step_id != state.active_step:
                raise InvalidTransition("Stage recovery cursor is internally inconsistent")
            if subtask.key in {"reviewer_entry_gate", "content_freeze_guard"}:
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
                decision.resume_after_step, definition.id
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
                    last_completed_stage=completed_stage_for_step(resume_after),
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

    def pause(self, *, expected_revision: int) -> WorkflowState:
        return self._control_transition(expected_revision, "PAUSED", WorkflowStatus.PAUSED)

    def resume(self, *, expected_revision: int) -> WorkflowState:
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
        return self._transitions.resolve_human_decision(
            expected_revision=expected_revision,
            resolution=resolution,
            decision_record=decision_record,
        )

    def _control_transition(
        self, expected_revision: int, event: str, status: WorkflowStatus
    ) -> WorkflowState:
        return self._transition(
            expected_revision=expected_revision,
            event_type=event,
            changes={
                "status": status,
                "runner_pid": None,
                "runner_lease_id": None,
                "heartbeat_at": None,
            },
        )

    def deactivate(
        self,
        *,
        expected_revision: int,
        legacy_inferred_step: int | None = None,
    ) -> WorkflowState:
        state = self.store.load()
        if state.revision != expected_revision:
            raise RevisionConflict(
                f"expected revision {expected_revision}, found {state.revision}"
            )
        self.assert_semantically_clean_for_rollback(state=state)
        stage_history = state.scheduler_generation == STAGE_SCHEDULER_GENERATION or any(
            event.type in {
                "STAGE_SCHEDULER_ACTIVATED",
                "STAGE_SCHEDULER_ROLLED_BACK",
            }
            for event in self.store.events()
        )
        if stage_history:
            raise InvalidTransition(
                "Stage-scheduled projects may only roll back to step_v2; "
                "full Legacy deactivation is prohibited"
            )
        if legacy_inferred_step is None:
            raise InvalidTransition(
                "full Legacy deactivation requires a verified Legacy cursor"
            )
        if legacy_inferred_step != state.last_completed_step:
            raise InvalidTransition(
                "Legacy inference does not match the authoritative SQLite cursor: "
                f"legacy={legacy_inferred_step}, sqlite={state.last_completed_step}"
            )
        return self._transition(
            expected_revision=expected_revision,
            event_type="ENGINE_DEACTIVATED",
            changes={
                "control_mode": "legacy",
                "runtime_generation": "legacy_adapter",
                "scheduler_generation": STEP_SCHEDULER_GENERATION,
                "stage_catalog_version": None,
                "active_stage": None,
                "active_subtask": None,
                "source_step_id": state.active_step,
            },
        )

    def assert_semantically_clean_for_rollback(
        self,
        *,
        state: WorkflowState | None = None,
    ) -> None:
        """Fail closed before changing a Stage project's control authority."""

        current = state or self.store.load()
        if current.status in {
            WorkflowStatus.RUNNING,
            WorkflowStatus.RETRYING,
            WorkflowStatus.ARCHIVING,
        } or (
            current.runner_pid is not None
            and self._pid_is_live(current.runner_pid)
        ):
            raise InvalidTransition("cannot change scheduler authority while a runner is active")
        if current.pending_action is not None or current.status in {
            WorkflowStatus.AWAITING_SELECTION,
            WorkflowStatus.AWAITING_CONSULTATION,
        }:
            raise InvalidTransition(
                "cannot change scheduler authority while a human decision is pending"
            )
        open_requests = [
            request
            for request in self.store.decision_requests()
            if request.get("status") == "open"
        ]
        if open_requests:
            raise InvalidTransition(
                "cannot change scheduler authority with unfinished decision requests"
            )
        if current.attempt > 0:
            raise InvalidTransition(
                "cannot change scheduler authority after a workflow execution attempt started"
            )
        dirty = self.store.dirty_flags()
        if dirty:
            raise InvalidTransition(
                "cannot change scheduler authority while semantic dirty flags are unresolved"
            )
        baseline = self.store.stage_cursor_input()
        if baseline is not None:
            current_manifest = capture_artifact_manifest(self.project_dir)
            baseline_manifest = dict(baseline.get("manifest") or {})
            if manifest_fingerprint(current_manifest) != str(
                baseline.get("input_fingerprint") or ""
            ):
                changes = classify_manifest_changes(
                    baseline_manifest, current_manifest
                )
                artifacts = sorted(
                    {change.cause_artifact for change in changes}
                )
                raise InvalidTransition(
                    "cannot change scheduler authority because the Stage input "
                    "manifest drifted from its baseline"
                    + (f": {', '.join(artifacts[:5])}" if artifacts else "")
                )
        if self.store.projection_failures(pending_only=True):
            raise InvalidTransition(
                "cannot change scheduler authority with unresolved projection failures"
            )
        final_snapshot = (
            self.project_dir / ".factory" / "finalization" / "input_manifest.json"
        )
        finalization_pending = (
            current.active_stage == 10
            or current.source_step_id == 16
            or current.last_completed_step >= 15
        )
        if (
            final_snapshot.is_file()
            and finalization_pending
            and current.status is not WorkflowStatus.COMPLETED
        ):
            raise InvalidTransition(
                "cannot change scheduler authority while a Finalization snapshot is pending"
            )
        from .selection_projection import (
            step3_projection_required,
            verify_step3_projections,
        )

        if (
            bool(self.store.decision_history("step3"))
            and step3_projection_required(self.project_dir)
        ):
            projection = verify_step3_projections(self.project_dir)
            if not projection.valid:
                raise InvalidTransition(
                    "cannot change scheduler authority with unresolved Step 3 "
                    "projection drift: " + "; ".join(projection.errors)
                )
        from .consultation_projection import verify_consultation_projections

        consultation = verify_consultation_projections(self.project_dir)
        if not consultation.valid:
            raise InvalidTransition(
                "cannot change scheduler authority with unresolved consultation "
                "projection drift: " + "; ".join(consultation.errors)
            )

    def archive_completed(self, factory_root: str | Path) -> WorkflowState:
        root = Path(factory_root).resolve()
        state = self.store.load()
        if state.status not in {WorkflowStatus.ARCHIVING, WorkflowStatus.COMPLETED}:
            raise InvalidTransition("only completed projects can be archived")
        if state.status is WorkflowStatus.COMPLETED and self.project_dir.parent.name == "complete":
            return state

        from .phase9_delivery_fence import delivery_side_effect_commit_lease

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
        from .phase9_delivery_fence import delivery_side_effect_commit_lease

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
        )

    def _contest_deadline(self, definition: StepDefinition) -> int | None:
        payload = self.store.contest_policy()
        if payload is None:
            return None
        policy = ContestPolicy.from_dict(payload)
        return (
            policy.contest_deadline_at
            if definition.id == 16
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
        return effective_timeout(
            ContestPolicy.from_dict(payload),
            step_id=definition.id,
            step_timeout=configured,
            now=self.store.now_epoch(),
        )

    @staticmethod
    def _validated_resume_target(value: object, active_step: int) -> int:
        target = int(value)
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
    def _terminal_error_class(error_class: str, *, exhausted: bool) -> str:
        if exhausted and error_class.startswith("TRANSIENT_JUDGE"):
            return "PERMANENT_JUDGE_INFRASTRUCTURE"
        return error_class

    @staticmethod
    def _pid_is_live(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
