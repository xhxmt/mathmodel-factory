from __future__ import annotations

import os
import shutil
import time
import uuid
import warnings
from pathlib import Path
from typing import Callable

from .domain import (
    InvalidTransition,
    RecoveryDisposition,
    RunnerBusy,
    StepError,
    StepContext,
    TERMINAL_STATUSES,
    WorkflowState,
    WorkflowStatus,
)
from .registry import StepDefinition, StepRegistry
from .storage import SQLiteStateStore


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
        if (
            state.runner_pid is not None
            and state.runner_pid != os.getpid()
            and self._pid_is_live(state.runner_pid)
        ):
            raise RunnerBusy(
                f"project {state.project_id} already has live runner {state.runner_pid}"
            )
        if state.status in TERMINAL_STATUSES:
            return state
        if state.status in {
            WorkflowStatus.AWAITING_SELECTION,
            WorkflowStatus.AWAITING_CONSULTATION,
            WorkflowStatus.PAUSED,
        }:
            return state
        if state.status in {WorkflowStatus.RUNNING, WorkflowStatus.RETRYING} and state.active_step is not None:
            state = self.recover()
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
        )
        completed_this_run = 0
        while True:
            definition = self.registry.next_after(state.last_completed_step)
            if definition is None:
                return self._transition(
                    expected_revision=state.revision,
                    event_type="PROJECT_COMPLETED",
                    changes={
                        "status": WorkflowStatus.COMPLETED,
                        "active_step": None,
                        "attempt": 0,
                        "runner_pid": None,
                        "runner_lease_id": None,
                        "heartbeat_at": None,
                    },
                )
            if max_steps is not None and completed_this_run >= max_steps:
                return self._transition(
                    expected_revision=state.revision,
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
            preview_context = StepContext(
                project_dir=self.project_dir,
                project_id=state.project_id,
                step_id=definition.id,
                attempt=attempt,
                timeout_seconds=definition.timeout_seconds,
                revision=state.revision,
            )
            prepared = definition.lifecycle.prepare(preview_context)
            if prepared.pending_action is not None:
                return self._await_action(
                    state,
                    prepared.pending_action.to_dict(),
                    reason=prepared.reason,
                    evidence=prepared.evidence,
                    event_type="STEP_PREPARE_AWAITING_ACTION",
                )
            if not prepared.ready:
                raise InvalidTransition(
                    f"step {definition.id} prepare returned neither ready nor pending action"
                )
            state = self._transition(
                expected_revision=state.revision,
                event_type="STEP_STARTED",
                changes={
                    "status": WorkflowStatus.RUNNING,
                    "active_step": definition.id,
                    "attempt": attempt,
                    "heartbeat_at": int(time.time()),
                },
                payload={"step_name": definition.name},
            )
            context = self._context(state, definition)
            result = definition.lifecycle.execute(context)
            current = self.store.load()
            if current.revision != state.revision:
                if current.status in {
                    WorkflowStatus.PAUSED,
                    WorkflowStatus.KILLED,
                    WorkflowStatus.AWAITING_SELECTION,
                    WorkflowStatus.AWAITING_CONSULTATION,
                }:
                    return current
                if current.active_step != definition.id:
                    raise InvalidTransition(
                        f"step {definition.id} lost ownership at revision {current.revision}"
                    )
                state = current
            if result.metadata.get("killed"):
                return self._transition(
                    expected_revision=state.revision,
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
                            "resume_after_step": resume_after,
                        },
                    )
                resume_after = self._validated_resume_target(resume_after, definition.id)
                state = self._transition(
                    expected_revision=state.revision,
                    event_type="STEP_REOPENED",
                    changes={
                        "status": WorkflowStatus.READY,
                        "last_completed_step": resume_after,
                        "active_step": None,
                        "attempt": 0,
                    },
                    payload={"source_step": definition.id, **result.metadata},
                )
                continue
            validation = definition.lifecycle.validate(context)
            if validation.pending_action is not None:
                return self._await_action(
                    state,
                    validation.pending_action.to_dict(),
                    reason=validation.reason,
                    evidence=validation.evidence,
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
                state = self._transition(
                    expected_revision=state.revision,
                    event_type="STEP_SUCCEEDED",
                    changes={
                        "status": WorkflowStatus.READY,
                        "last_completed_step": completed_step,
                        "active_step": None,
                        "attempt": 0,
                    },
                    payload={"evidence": validation.evidence, **result.metadata},
                )
                completed_this_run += 1
                continue
            permanent = result.error_class.startswith("PERMANENT")
            if permanent or attempt >= definition.max_attempts:
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
                        "error_class": result.error_class,
                        "reason": validation.reason,
                        "returncode": result.returncode,
                    },
                )
            state = self._transition(
                expected_revision=state.revision,
                event_type="RETRY_SCHEDULED",
                changes={"status": WorkflowStatus.RETRYING},
                payload={
                    "error_class": result.error_class,
                    "reason": validation.reason,
                    "delay_seconds": self._retry_delay(attempt),
                },
            )
            self._sleep(self._retry_delay(attempt))

    def recover(self) -> WorkflowState:
        state = self.store.load()
        if state.active_step is None:
            return state
        definition = self.registry.get(state.active_step)
        context = self._context(state, definition)
        decision = definition.lifecycle.recover(
            context,
            StepError(error_class="INTERRUPTED", reason="runner interrupted"),
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
        return self._transition(
            expected_revision=state.revision,
            event_type="RECOVERY_DECIDED",
            changes=changes,
            payload={
                "decision": decision_name,
                "source_step": definition.id,
                "reason": decision.reason,
                "evidence": decision.evidence,
                **decision.metadata,
            },
        )

    def _await_action(
        self,
        state: WorkflowState,
        action: dict,
        *,
        reason: str,
        evidence: tuple[str, ...],
        event_type: str = "AWAITING_ACTION",
    ) -> WorkflowState:
        awaiting_status = (
            WorkflowStatus.AWAITING_SELECTION
            if action["type"].endswith("selection")
            else WorkflowStatus.AWAITING_CONSULTATION
        )
        return self._transition(
            expected_revision=state.revision,
            event_type=event_type,
            changes={
                "status": awaiting_status,
                "pending_action": action,
                "runner_pid": None,
                "runner_lease_id": None,
                "heartbeat_at": None,
            },
            payload={"reason": reason, "evidence": evidence},
        )

    def pause(self, *, expected_revision: int) -> WorkflowState:
        return self._control_transition(expected_revision, "PAUSED", WorkflowStatus.PAUSED)

    def resume(self, *, expected_revision: int) -> WorkflowState:
        state = self.store.load()
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
        return self._transition(
            expected_revision=expected_revision,
            event_type="RESUMED",
            changes={
                "status": WorkflowStatus.READY,
                "active_step": None,
                "attempt": 0,
                "runner_pid": None,
                "runner_lease_id": None,
                "heartbeat_at": None,
            },
        )

    def kill(self, *, expected_revision: int) -> WorkflowState:
        return self._control_transition(expected_revision, "KILLED", WorkflowStatus.KILLED)

    def resolve_action(self, resolution: dict, *, expected_revision: int) -> WorkflowState:
        state = self.store.load()
        if state.pending_action is None:
            raise InvalidTransition("project has no pending action")
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
        return self._transition(
            expected_revision=expected_revision,
            event_type="ENGINE_DEACTIVATED",
            changes={"control_mode": "legacy"},
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

    def _context(self, state: WorkflowState, definition: StepDefinition) -> StepContext:
        return StepContext(
            project_dir=self.project_dir,
            project_id=state.project_id,
            step_id=definition.id,
            attempt=state.attempt,
            timeout_seconds=definition.timeout_seconds,
            revision=state.revision,
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

    @staticmethod
    def _pid_is_live(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
