from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, Callable

from .domain import WorkflowState
from .storage import SQLiteStateStore


class TransitionCoordinator:
    """The workflow writer used by orchestration code.

    Stage execution returns outcomes; this coordinator alone commits workflow
    state and then refreshes compatibility projections.
    """

    def __init__(
        self,
        project_dir: Path,
        store: SQLiteStateStore,
        projector: Callable[[Path, WorkflowState], None] | None = None,
    ) -> None:
        self.project_dir = project_dir
        self.store = store
        self.projector = projector

    def transition(self, **kwargs: Any) -> WorkflowState:
        state = self.store.transition(**kwargs)
        self._project(state)
        return state

    def resolve_human_decision(self, **kwargs: Any) -> WorkflowState:
        state = self.store.resolve_human_decision(**kwargs)
        self._project(state)
        return state

    def configure_solver_policy(self, **kwargs: Any) -> WorkflowState:
        state = self.store.configure_solver_policy(**kwargs)
        self._project(state)
        return state

    def create_solver_job(self, **kwargs: Any) -> WorkflowState:
        state = self.store.create_solver_job(**kwargs)
        self._project(state)
        return state

    def update_solver_job(self, job_id: str, **kwargs: Any) -> WorkflowState:
        state = self.store.update_solver_job(job_id, **kwargs)
        self._project(state)
        return state

    def record_solver_receipt(self, job_id: str, **kwargs: Any) -> WorkflowState:
        state = self.store.record_solver_receipt(job_id, **kwargs)
        self._project(state)
        return state

    def _project(self, state: WorkflowState) -> None:
        if self.projector is not None:
            try:
                self.projector(self.project_dir, state)
            except OSError as exc:
                warnings.warn(
                    f"workflow state committed at revision {state.revision}, "
                    f"but compatibility projection failed: {exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )

    def relocate(self, project_dir: Path, store: SQLiteStateStore) -> None:
        self.project_dir = project_dir
        self.store = store
