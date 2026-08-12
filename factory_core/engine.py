from __future__ import annotations

import os
import shutil
import time
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .contest import ContestDeadlineExceeded, ContestPolicy, effective_timeout
from .deadline import deadline_scope, ensure_deadline
from .dirty import (
    DirtyFlag,
    capture_artifact_manifest,
    classifier_contract_sha256,
    classify_manifest_changes,
    manifest_fingerprint,
    semantic_flags,
)
from .domain import (
    InvalidTransition,
    PendingAction,
    RecoveryDisposition,
    RevisionConflict,
    RunnerBusy,
    RunnerLeaseLost,
    StepError,
    StepContext,
    TERMINAL_STATUSES,
    WorkflowState,
    WorkflowStatus,
)
from .registry import StepDefinition, StepRegistry
from .storage import SQLiteStateStore
from .stages import (
    STAGE_CATALOG_VERSION,
    STAGE_SCHEDULER_GENERATION,
    STEP_SCHEDULER_GENERATION,
    completed_stage_for_step,
    next_stage_subtask,
    stage_for_id,
    subtask_for_key,
)


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
                return self._owned_transition(
                    state,
                    lease,
                    event_type="PROJECT_COMPLETED",
                    changes={
                        "status": WorkflowStatus.COMPLETED,
                        "active_step": None,
                        "active_stage": None,
                        "active_subtask": None,
                        "source_step_id": None,
                        "last_completed_stage": 10 if stage_mode else state.last_completed_stage,
                        "attempt": 0,
                        "runner_pid": None,
                        "runner_lease_id": None,
                        "heartbeat_at": None,
                    },
                    subtask_baseline=None,
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
                        "error_class": "PERMANENT_ATTEMPT_BUDGET_EXHAUSTED",
                        "stage": stage_task.stage_id,
                        "subtask": stage_task.subtask,
                        "source_step": stage_task.source_step_id,
                    },
                )
            try:
                timeout_seconds = self._contest_timeout(definition)
            except ContestDeadlineExceeded as exc:
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
                    prepared = definition.lifecycle.prepare(preview_context)
                    ensure_deadline(now=self.store.now_epoch())
            except ContestDeadlineExceeded as exc:
                return self._deadline_failure(state, definition, exc, lease=lease)
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
                with deadline_scope(context.deadline_epoch):
                    result = definition.lifecycle.execute(context)
                    ensure_deadline(now=self.store.now_epoch())
            except ContestDeadlineExceeded as exc:
                return self._deadline_failure(state, definition, exc, lease=lease)
            state = self._refresh_owned_state(lease, active_step=definition.id)
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
            try:
                with deadline_scope(context.deadline_epoch):
                    validation = definition.lifecycle.validate(context)
                    ensure_deadline(now=self.store.now_epoch())
            except ContestDeadlineExceeded as exc:
                return self._deadline_failure(state, definition, exc, lease=lease)
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
                        **failure_metadata,
                        "error_class": terminal_error_class,
                        "reason": validation.reason,
                        "returncode": result.returncode,
                    },
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
                        **failure_metadata,
                        "error_class": "PERMANENT_CONTEST_DEADLINE",
                        "reason": budget_reason,
                        "step": definition.id,
                        "required_retry_delay_seconds": retry_delay,
                        "remaining_budget_seconds": retry_budget,
                    },
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

    def _complete_stage_task(
        self,
        state: WorkflowState,
        lease: str | None,
        task: ScheduledStageTask,
        *,
        validation,
        result,
    ) -> WorkflowState:
        baseline = self.store.stage_cursor_input()
        after = capture_artifact_manifest(self.project_dir)
        output_fingerprint = manifest_fingerprint(after)
        if baseline is None or (
            int(baseline["stage_id"]) != task.stage_id
            or str(baseline["subtask"]) != task.subtask
        ):
            before: dict[str, str] = {}
            dirty_changes = [
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
            input_fingerprint = "MISSING"
        else:
            before = dict(baseline["manifest"])
            input_fingerprint = str(baseline["input_fingerprint"])
            dirty_changes = [
                {
                    **change.to_dict(),
                    "classifier_contract_sha256": classifier_contract_sha256(),
                }
                for change in classify_manifest_changes(before, after)
            ]

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
        semantic_reason = ""
        if task.stage_id > 3 and DirtyFlag.MODEL.value in new_flags:
            semantic_reopen_target = 3
            semantic_reason = f"Stage {task.stage_id} changed the model contract"
        elif task.stage_id > 4 and DirtyFlag.RESULT.value in new_flags:
            semantic_reopen_target = 4
            semantic_reason = f"Stage {task.stage_id} changed canonical result semantics"
        elif task.stage_id > 6 and DirtyFlag.VISUAL.value in new_flags:
            semantic_reopen_target = 7
            semantic_reason = f"Stage {task.stage_id} changed reviewer-entry visuals"
        elif task.stage_id > 8 and DirtyFlag.MATH.value in new_flags:
            semantic_reopen_target = 10
            semantic_reason = f"Stage {task.stage_id} changed paper mathematics"
        elif task.stage_id > 9 and new_flags & {
            DirtyFlag.PROSE.value,
            DirtyFlag.CITATION.value,
            DirtyFlag.FORMAT.value,
        }:
            semantic_reopen_target = 13
            semantic_reason = "FINALIZE changed final-prose-owned content"

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
                        "reason": semantic_reason,
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
                    "reason": semantic_reason,
                    "dirty_flags": sorted(new_flags),
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
        count = sum(
            1
            for event in self.store.events()
            if event.type == "STAGE_SEMANTIC_REOPENED"
            and int(event.payload.get("stage", -1)) == int(stage_id)
        )
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
                expected_runner_pid=expected_runner_pid,
                expected_runner_lease_id=expected_runner_lease_id,
                enforce_lease=enforce_lease,
            )
        if decision.disposition is RecoveryDisposition.REOPEN:
            if not self._reopen_allowed(definition):
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
            status = (
                WorkflowStatus.AWAITING_SELECTION
                if action["type"].endswith("selection")
                else WorkflowStatus.AWAITING_CONSULTATION
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
                recovered = self._complete_stage_task(
                    state,
                    state.runner_lease_id if enforce_lease else None,
                    stage_task,
                    validation=type(
                        "RecoveredValidation",
                        (),
                        {"evidence": decision.evidence},
                    )(),
                    result=type(
                        "RecoveredResult",
                        (),
                        {"metadata": {"recovered": True, **decision.metadata}},
                    )(),
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
        awaiting_status = (
            WorkflowStatus.AWAITING_SELECTION
            if action["type"].endswith("selection")
            else WorkflowStatus.AWAITING_CONSULTATION
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
            payload={"reason": reason, "evidence": evidence},
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

    def kill(self, *, expected_revision: int) -> WorkflowState:
        return self._control_transition(expected_revision, "KILLED", WorkflowStatus.KILLED)

    def resolve_action(self, resolution: dict, *, expected_revision: int) -> WorkflowState:
        state = self.store.load()
        if state.pending_action is None:
            raise InvalidTransition("project has no pending action")
        pending_gate = str(state.pending_action.get("gate") or "")
        resolution_gate = str(resolution.get("gate") or "")
        if pending_gate and resolution_gate and pending_gate != resolution_gate:
            raise InvalidTransition(
                f"pending gate {pending_gate} cannot be resolved as {resolution_gate}"
            )
        return self._transition(
            expected_revision=expected_revision,
            event_type="ACTION_RESOLVED",
            changes={"status": WorkflowStatus.READY, "pending_action": None},
            payload={"action_type": state.pending_action.get("type"), "resolution": resolution},
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

    def deactivate(self, *, expected_revision: int) -> WorkflowState:
        state = self.store.load()
        if state.status in {WorkflowStatus.RUNNING, WorkflowStatus.RETRYING} or (
            state.runner_pid is not None and self._pid_is_live(state.runner_pid)
        ):
            raise InvalidTransition("cannot deactivate an active engine runner")
        if (
            state.scheduler_generation == STAGE_SCHEDULER_GENERATION
            and self.store.dirty_flags()
        ):
            raise InvalidTransition(
                "cannot deactivate Stage scheduling while semantic dirty flags are unresolved"
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

    def archive_completed(self, factory_root: str | Path) -> WorkflowState:
        root = Path(factory_root).resolve()
        state = self.store.load()
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
            return self._transition(
                expected_revision=state.revision,
                event_type="PROJECT_ARCHIVED",
                changes={"status": WorkflowStatus.COMPLETED, "storage_scope": "complete"},
            )
        if state.status is not WorkflowStatus.COMPLETED:
            raise InvalidTransition("only completed projects can be archived")
        if self.project_dir.parent.name == "complete":
            return state
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
        return self._transition(
            expected_revision=state.revision,
            event_type="PROJECT_ARCHIVED",
            changes={"status": WorkflowStatus.COMPLETED, "storage_scope": "complete"},
        )

    def _transition(self, **kwargs) -> WorkflowState:
        state = self.store.transition(**kwargs)
        if self._projector is not None:
            try:
                self._projector(self.project_dir, state)
            except OSError as exc:
                warnings.warn(
                    f"workflow state committed at revision {state.revision}, "
                    f"but compatibility projection failed: {exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )
        return state

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
