from __future__ import annotations

import json
import os
import re
import sqlite3
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .domain import (
    FactoryCoreError,
    InvalidTransition,
    RevisionConflict,
    WorkflowState,
    WorkflowStatus,
)
from .contest import ContestPolicy
from .engine import FactoryEngine
from .human_decisions import validate_resolution
from .projections import runtime_payload, write_compatibility_projections
from .steps import build_native_registry
from .adapters.solvers import SolverRequest, build_solver_backends
from .registry import SolverBackendRegistry
from .storage import SQLiteStateStore
from .transitions import TransitionCoordinator
from .workflow_events import canonical_hash


SOLVER_TERMINAL_STATUSES = {
    "completed",
    "succeeded",
    "failed",
    "cancelled",
    "timeout",
    "timed_out",
}
from .workflow_events import project_runtime_diagnostics
from .stages import (
    STAGE_CATALOG_VERSION,
    STAGE_SCHEDULER_GENERATION,
    STEP_SCHEDULER_GENERATION,
    initial_stage_checkpoints,
    projected_stage_cursor,
)
from scripts.solver_job_receipt import (
    ReceiptError,
    build_completion_receipt,
    build_submission_receipt,
    file_sha256,
    read_receipt,
    receipt_paths,
    SUBMISSION_SCHEMA,
    COMPLETION_SCHEMA,
    write_receipt,
)


BASE_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")


def _workflow_coordinator(store: SQLiteStateStore) -> TransitionCoordinator:
    return TransitionCoordinator(
        store.project_dir, store, write_compatibility_projections
    )


@dataclass(frozen=True)
class WorkerHandle:
    pid: int
    log_path: Path
    state: WorkflowState


class WorkerLauncher:
    """Launch the single engine worker entry point in its own process group."""

    def __init__(self, factory_root: Path, code_root: Path | None = None, *, ready_timeout: float = 30) -> None:
        self.factory_root = factory_root.resolve()
        self.code_root = (code_root or factory_root).resolve()
        self.ready_timeout = ready_timeout

    def spawn(
        self, project: Path, *, expected_revision: int | None = None
    ) -> WorkerHandle:
        logs = project / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        log_path = logs / f"worker_{stamp}.log"
        ready = project / ".factory" / f"worker_ready_{os.getpid()}_{time.time_ns()}"
        ready.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("ab")
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "factory_core.cli",
                    "worker",
                    str(project),
                    "--ready-file",
                    str(ready),
                ],
                cwd=self.code_root,
                env={
                    **os.environ,
                    "FACTORY": str(self.factory_root),
                    "PYTHONPATH": os.pathsep.join(
                        filter(
                            None,
                            [str(self.code_root), os.environ.get("PYTHONPATH", "")],
                        )
                    ),
                },
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            handle.close()
        store = SQLiteStateStore(project)
        state = store.load()
        revision = state.revision if expected_revision is None else expected_revision
        try:
            from .adapters.infrastructure.process import _process_identity
            updated = _workflow_coordinator(store).transition(
                expected_revision=revision,
                event_type="WORKER_LAUNCHED",
                changes={
                    "status": WorkflowStatus.RUNNING,
                    "runner_pid": process.pid,
                    "runner_lease_id": f"launch:{process.pid}",
                    "heartbeat_at": int(time.time()),
                },
                payload={"worker_pid": process.pid, "worker_identity": _process_identity(process.pid),
                         "lease_id": f"launch:{process.pid}",
                         "log": str(log_path.relative_to(project))},
            )
            ready.write_text(str(process.pid) + "\n", encoding="ascii")
            acknowledged = ready.with_suffix(".ack")
            deadline = time.monotonic() + self.ready_timeout
            while not acknowledged.is_file():
                if process.poll() is not None:
                    raise RuntimeError(f"worker exited before initialization (exit {process.returncode})")
                if time.monotonic() >= deadline:
                    raise TimeoutError("worker initialization timed out")
                time.sleep(0.02)
            if acknowledged.read_text().strip() != str(process.pid):
                raise RuntimeError("worker readiness identity mismatch")
            acknowledged.unlink()
        except BaseException as exc:
            from .adapters.infrastructure.process import ProcessSupervisor, _process_identity
            descendants = {}
            ProcessSupervisor._terminate_tree(process, descendants, 0.5)
            exited = (_process_identity(process.pid) is None and
                      all(_process_identity(pid) != token for pid, token in descendants.items()))
            current = store.load()
            if current.runner_pid == process.pid:
                _workflow_coordinator(store).transition(
                    expected_revision=current.revision, event_type="WORKER_START_FAILED",
                    changes={"status": WorkflowStatus.FAILED if exited else WorkflowStatus.INTERRUPTED,
                             "runner_pid": None, "runner_lease_id": None, "heartbeat_at": None},
                    payload={"error_class": "WORKER_INITIALIZATION_FAILED" if exited else "WORKER_EXIT_UNVERIFIED",
                             "exception_type": type(exc).__name__, "exit_code": process.poll(),
                             "process_tree_exited": exited},
                )
            ready.unlink(missing_ok=True)
            raise
        return WorkerHandle(process.pid, log_path, store.load())


# Project template resources.  A missing required resource means the deployment
# is incomplete, so project creation reports the exact paths instead of
# producing a project that silently lacks its style or guidance files.
REQUIRED_PROJECT_RESOURCES = (
    ("resources/style/paper.sty", "style/paper.sty"),
    ("resources/bib/bibliography.bst", "bib/bibliography.bst"),
    ("resources/style/model_papers_style.json", "style/model_papers_style.json"),
    ("modeling_guide.md", "modeling_guide.md"),
)
# Legacy context, copied only when present.  README.md marks this file as
# historical reference that is not part of the executable workflow, so its
# absence is not a deployment error -- but it is no longer silently ambiguous.
OPTIONAL_PROJECT_RESOURCES = (
    ("analysis_guide.md", "analysis_guide.md"),
)


class FactoryService:
    def __init__(
        self,
        factory_root: str | Path,
        *,
        worker_launcher: WorkerLauncher | None = None,
        native_registry_factory: Callable[[Path], object] = build_native_registry,
        solver_backends: SolverBackendRegistry | None = None,
    ) -> None:
        self.root = Path(factory_root).resolve()
        self.code_root = Path(__file__).resolve().parents[1]
        self.worker_launcher = worker_launcher or WorkerLauncher(self.root, self.code_root)
        self._native_registry_factory = native_registry_factory
        self.solver_backends = solver_backends or build_solver_backends(self.code_root)

    def resolve_project(self, project: str | Path) -> Path:
        value = Path(project)
        if value.is_absolute() or value.parent != Path("."):
            candidate = value.resolve()
            if candidate.is_dir():
                return candidate
            raise FileNotFoundError(f"project directory not found: {candidate}")
        if not BASE_NAME_RE.fullmatch(value.name):
            raise ValueError(f"invalid project name: {value.name}")
        for root in (self.root / "ongoing", self.root / "complete"):
            candidate = root / value.name
            if candidate.is_dir():
                return candidate.resolve()
        raise FileNotFoundError(f"project not found: {value.name}")

    def engine(self, project: str | Path) -> FactoryEngine:
        resolved = self.resolve_project(project)
        state = SQLiteStateStore(resolved).load()
        if state.control_mode != "engine" or state.runtime_generation != "native_v2":
            raise InvalidTransition(
                "NATIVE_WORKFLOW_REQUIRED: only native_v2 engine projects are supported; "
                "historical runtimes are preserved in paper_new"
            )
        registry = self._native_registry_factory(self.code_root)
        return FactoryEngine(
            resolved,
            registry=registry,
            projector=write_compatibility_projections,
        )

    def create_project(
        self,
        base_name: str,
        research_question: str,
        *,
        consult: bool = False,
        start: bool = False,
        contest_deadline_at: int | None = None,
    ) -> tuple[WorkflowState, WorkerHandle | None]:
        if not BASE_NAME_RE.fullmatch(base_name):
            raise ValueError("base_name must contain only letters, numbers, '_' or '-'")
        ongoing = self.root / "ongoing"
        complete = self.root / "complete"
        project = ongoing / base_name
        if project.exists() or (complete / base_name).exists():
            raise FileExistsError(f"project already exists: {base_name}")
        # Check the template up front, before any directory or database is
        # created, so an incomplete deployment fails with the offending paths
        # rather than leaving a half-provisioned project behind.
        missing = [
            str(self.code_root / source)
            for source, _target in REQUIRED_PROJECT_RESOURCES
            if not (self.code_root / source).is_file()
        ]
        if missing:
            raise FileNotFoundError(
                "project template resources are missing: " + ", ".join(missing)
            )
        started_at = int(time.time())
        contest_policy = (
            ContestPolicy.default(started_at=started_at)
            if contest_deadline_at is None
            else ContestPolicy.for_deadline(
                started_at=started_at,
                deadline_at=int(contest_deadline_at),
            )
        )
        directories = (
            "style", "bib", "figures", "tables", "do/archive", "logs",
            "replication", "replication/temp", "data/raw", "data/intermediate",
            "data/final", "tmp", "scripts", "docs",
        )
        for relative in directories:
            (project / relative).mkdir(parents=True, exist_ok=True)
        for source_rel, target_rel in REQUIRED_PROJECT_RESOURCES:
            shutil.copy2(self.code_root / source_rel, project / target_rel)
        for source_rel, target_rel in OPTIONAL_PROJECT_RESOURCES:
            source = self.code_root / source_rel
            if source.is_file():
                shutil.copy2(source, project / target_rel)
        (project / "references.bib").touch()
        checkpoint = (
            "# Paper Skill Checkpoint\n\n"
            f"- **Base name**: {base_name}\n"
            f"- **Project path**: {project}\n"
            f"- **Research question**: {research_question}\n"
            "- **Last completed step**: -1\n"
            f"- **Timestamp**: {time.strftime('%Y-%m-%d %H:%M')}\n"
        )
        (project / "checkpoint.md").write_text(checkpoint, encoding="utf-8")
        if consult:
            consultation = project / "consultation"
            consultation.mkdir(parents=True, exist_ok=True)
            (consultation / "enabled").touch()
        state = SQLiteStateStore(project).initialize(
            project_id=base_name,
            project_type="modeling",
            runtime_generation="native_v2",
            scheduler_generation=STAGE_SCHEDULER_GENERATION,
            contest_policy=contest_policy.to_dict(),
        )
        write_compatibility_projections(project, state)
        worker = self.start(project) if start else None
        return (worker.state if worker is not None else state), worker

    def inspect(self, project: str | Path) -> WorkflowState:
        return SQLiteStateStore(self.resolve_project(project)).load()

    def status(self, project: str | Path) -> dict[str, Any]:
        resolved = self.resolve_project(project)
        store = SQLiteStateStore(resolved)
        return write_compatibility_projections(resolved, store.load())

    def start(
        self,
        project: str | Path,
        *,
        expected_revision: int | None = None,
    ) -> WorkerHandle:
        resolved = self.resolve_project(project)
        state = SQLiteStateStore(resolved).load()
        self._require_stage_runtime(state)
        self._assert_expected_revision(state, expected_revision)
        if state.runner_pid and self._pid_is_live(state.runner_pid):
            raise InvalidTransition(f"project already has live worker {state.runner_pid}")
        if (
            state.runner_pid is not None
            or state.status in {WorkflowStatus.RUNNING, WorkflowStatus.RETRYING}
        ):
            store = SQLiteStateStore(resolved)
            state = _workflow_coordinator(store).transition(
                expected_revision=state.revision,
                event_type="RUNNER_INTERRUPTED",
                changes={
                    "status": (
                        WorkflowStatus.INTERRUPTED
                        if state.active_step is not None
                        else WorkflowStatus.READY
                    ),
                    "runner_pid": None,
                    "runner_lease_id": None,
                    "heartbeat_at": None,
                },
                payload={"reason": "recorded runner is no longer live"},
            )
        if state.status in {
            WorkflowStatus.KILLED,
            WorkflowStatus.COMPLETED,
            WorkflowStatus.ARCHIVING,
        }:
            raise InvalidTransition(
                f"projects in {state.status.value} state cannot be started"
            )
        if state.status in {
            WorkflowStatus.PAUSED,
            WorkflowStatus.FAILED,
            WorkflowStatus.AWAITING_SELECTION,
            WorkflowStatus.AWAITING_CONSULTATION,
        } or (
            state.status is WorkflowStatus.INTERRUPTED
            and state.active_step is None
        ):
            state = self.resume(resolved, expected_revision=state.revision)
        return self.worker_launcher.spawn(
            resolved, expected_revision=state.revision
        )

    def resume_and_start(
        self,
        project: str | Path,
        *,
        expected_revision: int | None = None,
    ) -> tuple[WorkflowState, WorkerHandle]:
        """Resolve a satisfied human gate, resume, and launch one worker."""
        resolved = self.resolve_project(project)
        state = SQLiteStateStore(resolved).load()
        self._assert_expected_revision(state, expected_revision)
        resumed = self.resume(resolved, expected_revision=state.revision)
        worker = self.start(resolved, expected_revision=resumed.revision)
        return worker.state, worker

    def run(
        self,
        project: str | Path,
        *,
        max_steps: int | None = None,
        archive: bool = False,
        ready_file: Path | None = None,
    ) -> WorkflowState:
        engine = self.engine(project)
        self._require_stage_runtime(engine.store.load())
        if ready_file is not None:
            from .projections import _atomic_text
            _atomic_text(ready_file.with_suffix(".ack"), str(os.getpid()) + "\n")
        state = engine.run(max_steps=max_steps)
        if state.status is WorkflowStatus.COMPLETED:
            self._write_delivery_manifest(engine.project_dir)
            if archive and engine.project_dir.parent.name == "ongoing":
                state = engine.archive_completed(self.root)
        return state

    def advance_bounded(self, project: str | Path, contract):
        """Run one authorised, bounded advance and return its structured outcome.

        This is the supported entry point for what the hand-written ``work/*.py``
        drivers used to do: advance under an explicit scope and protection
        contract, and get back a report rather than writing a progress file.
        """

        from .bounded_run import BoundedRunContract

        if not isinstance(contract, BoundedRunContract):
            raise InvalidTransition("advance_bounded requires a BoundedRunContract")
        engine = self.engine(project)
        self._require_stage_runtime(engine.store.load())
        return engine.run_bounded(contract)

    def pause(
        self,
        project: str | Path,
        *,
        expected_revision: int | None = None,
        subcode: str = "OPERATOR",
        actor: str = "operator",
    ) -> WorkflowState:
        """Pause, recording why and who (S4.1).

        ``subcode`` and ``actor`` are required to be meaningful: a PAUSED event
        whose reason cannot be read is the failure S4.1 exists to remove.  The
        canonical ``code`` stays ``PAUSED``.
        """

        _require_reason_detail("PAUSED", subcode, actor)
        engine = self.engine(project)
        snapshot = engine.store.status_snapshot()
        state = snapshot["state"]
        revision = state.revision if expected_revision is None else expected_revision
        updated = engine.pause(
            expected_revision=revision, subcode=subcode, actor=actor
        )
        self._verify_runner_stop(engine.project_dir, snapshot)
        return updated

    def resume(
        self,
        project: str | Path,
        *,
        expected_revision: int | None = None,
        subcode: str = "OPERATOR",
        actor: str = "operator",
    ) -> WorkflowState:
        engine = self.engine(project)
        state = engine.get_state()
        self._assert_expected_revision(state, expected_revision)
        if state.pending_action is not None:
            state = self._resolve_pending_if_ready(engine.project_dir, state)
        from .consultation_projection import ensure_all_consultation_projections

        try:
            consultation = ensure_all_consultation_projections(
                engine.project_dir
            )
        except (OSError, ValueError) as exc:
            raise InvalidTransition(
                "consultation projection rebuild failed: " + str(exc)
            ) from exc
        if not consultation.valid:
            raise InvalidTransition(
                "consultation projection drift: "
                + "; ".join(consultation.errors)
            )
        _require_reason_detail("RESUMED", subcode, actor)
        return engine.resume(
            expected_revision=state.revision, subcode=subcode, actor=actor
        )

    def kill(self, project: str | Path, *, expected_revision: int | None = None) -> WorkflowState:
        engine = self.engine(project)
        snapshot = engine.store.status_snapshot()
        state = snapshot["state"]
        revision = state.revision if expected_revision is None else expected_revision
        updated = engine.kill(expected_revision=revision)
        self._verify_runner_stop(engine.project_dir, snapshot)
        return updated

    @staticmethod
    def _bind_consultation_staging(
        project: Path,
        pending_action: dict[str, Any],
        resolution: dict[str, Any],
        decision_record: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if pending_action.get("type") != "human_consultation":
            return resolution, decision_record
        from .artifacts import artifact_ref
        from .consultation_projection import (
            consultation_staging_path,
            read_staged_consultation_answer,
            stage_consultation_answer,
        )

        normalized = validate_resolution(pending_action, resolution)
        gate = str(normalized.get("gate") or pending_action.get("gate") or "")
        if gate in {"joint_modeling_candidates", "joint_modeling_risk"}:
            from .joint_modeling import validate_response

            validate_response(project, (pending_action.get("metadata") or {}).get("human_decision") or {},
                              str(normalized.get("answer") or ""), normalized.get("attestations") or {})
        request_id = str(normalized.get("request_id") or "")
        if not request_id:
            from .human_decisions import build_decision_request

            store = SQLiteStateStore(project)
            state = store.load()
            request = build_decision_request(
                project_id=state.project_id,
                project_dir=project,
                requested_revision=state.revision,
                generation=store.next_decision_generation(gate),
                action=pending_action,
                reason="compatibility request identity synthesized at resolution",
            )
            request_id = request.request_id
            normalized = {**normalized, "request_id": request_id}
        answer = str(normalized.get("answer") or "").strip()
        path = consultation_staging_path(project, request_id)
        if path.exists():
            staged = read_staged_consultation_answer(project, request_id, gate)
            if str(staged.get("answer") or "").strip() != answer:
                raise InvalidTransition(
                    "staged consultation answer does not match the resolution"
                )
        else:
            path = stage_consultation_answer(
                project_dir=project,
                request_id=request_id,
                gate=gate,
                answer=answer,
                step=(
                    int(pending_action.get("metadata", {}).get("step"))
                    if isinstance(pending_action.get("metadata"), dict)
                    and pending_action.get("metadata", {}).get("step") is not None
                    else None
                ),
            )
        reference = artifact_ref(project, path)
        record = dict(decision_record or {})
        refs = [
            dict(item)
            for item in record.get("artifact_refs") or ()
            if isinstance(item, dict)
        ]
        if not any(item.get("path") == reference["path"] for item in refs):
            refs.append(reference)
        record.update(
            schema_version=str(
                record.get("schema_version") or "human-decision-v1"
            ),
            gate=gate,
            kind="consultation",
            answer=answer,
            request_id=request_id,
            staging_receipt=reference["path"],
            artifact_refs=refs,
        )
        return normalized, record

    def resolve(
        self,
        project: str | Path,
        resolution: dict[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> WorkflowState:
        engine = self.engine(project)
        state = engine.get_state()
        revision = state.revision if expected_revision is None else expected_revision
        pending = dict(state.pending_action or {})
        resolution, decision_record = self._bind_consultation_staging(
            engine.project_dir, pending, resolution
        )
        updated = engine.resolve_action(
            resolution,
            expected_revision=revision,
            decision_record=decision_record,
        )
        if pending.get("type") == "human_consultation":
            self._rebuild_consultation_projection_or_record(
                engine.project_dir, str(pending.get("gate") or "")
            )
        return updated

    @staticmethod
    def _rebuild_consultation_projection_or_record(
        project: Path, gate: str
    ) -> None:
        from .consultation_projection import rebuild_consultation_projection

        store = SQLiteStateStore(project)
        projector_name = f"consultation:{gate}"
        try:
            rebuild_consultation_projection(project, gate)
        except Exception as exc:
            try:
                store.record_projection_failure(
                    revision=store.load().revision,
                    projector_name=projector_name,
                    error_type=type(exc).__name__,
                )
            except Exception:
                pass
            raise
        for failure in store.projection_failures(pending_only=True):
            if failure.get("projector_name") != projector_name:
                continue
            store.resolve_projection_failure(
                revision=int(failure["revision"]),
                projector_name=projector_name,
            )

    def supersede_pending_decision_request(
        self,
        project: str | Path,
        *,
        expected_revision: int,
        gate: str | None = None,
        reason: str = "Rebind the pending request to current project evidence",
    ) -> WorkflowState:
        resolved_project = self.resolve_project(project)
        return SQLiteStateStore(resolved_project).supersede_pending_decision_request(
            expected_revision=expected_revision,
            gate=gate,
            reason=reason,
        )

    def resolve_and_start(
        self,
        project: str | Path,
        resolution: dict[str, Any],
        *,
        evidence_writer: Callable[[], Any],
        expected_revision: int | None = None,
        artifact_first: bool = False,
    ) -> tuple[WorkflowState, WorkerHandle | None]:
        """Commit immutable evidence and a decision, then launch a worker."""

        resolved_project = self.resolve_project(project)
        engine = self.engine(resolved_project)
        pending = engine.get_state()
        self._assert_expected_revision(pending, expected_revision)
        if pending.pending_action is None:
            raise InvalidTransition("project has no pending action")
        pending_action = dict(pending.pending_action)
        gate = str(pending_action.get("gate") or "")
        artifact_first = artifact_first or bool(
            getattr(evidence_writer, "artifact_first", False)
        )
        if artifact_first:
            resolution = validate_resolution(pending_action, resolution)
            store = SQLiteStateStore(resolved_project)
            store.assert_pending_decision_current(gate)
            decision_record = evidence_writer()
            if (
                isinstance(decision_record, dict)
                and decision_record.get("kind") == "consultation"
                and "answer" not in resolution
            ):
                resolution = {
                    **resolution,
                    "answer": decision_record.get("answer"),
                }
            resolution, bound_record = self._bind_consultation_staging(
                resolved_project,
                pending_action,
                resolution,
                decision_record=(
                    decision_record
                    if isinstance(decision_record, dict)
                    else None
                ),
            )
            accepted = engine.resolve_action(
                resolution,
                expected_revision=pending.revision,
                decision_record=bound_record,
            )
            if accepted.pending_action is not None:
                return accepted, None
            if gate == "step3":
                from .selection_projection import rebuild_step3_projections

                rebuild_step3_projections(resolved_project)
            if pending_action.get("type") == "human_consultation":
                self._rebuild_consultation_projection_or_record(
                    resolved_project, gate
                )
            return self.resume_and_start(
                resolved_project, expected_revision=accepted.revision
            )

        resolution, staged_record = self._bind_consultation_staging(
            resolved_project, pending_action, resolution
        )
        accepted = engine.resolve_action(
            resolution,
            expected_revision=pending.revision,
            decision_record=staged_record,
        )
        if accepted.pending_action is not None:
            return accepted, None
        try:
            evidence_writer()
        except Exception as exc:
            store = SQLiteStateStore(resolved_project)
            _workflow_coordinator(store).transition(
                expected_revision=accepted.revision,
                event_type="ACTION_PROJECTION_FAILED",
                changes={
                    "status": pending.status,
                    "pending_action": pending.pending_action,
                },
                payload={"error_type": type(exc).__name__},
            )
            raise
        if gate == "step3":
            from .selection_projection import rebuild_step3_projections

            rebuild_step3_projections(resolved_project)
        if pending_action.get("type") == "human_consultation":
            self._rebuild_consultation_projection_or_record(
                resolved_project, gate
            )
        return self.resume_and_start(
            resolved_project, expected_revision=accepted.revision
        )

    def archive(self, project: str | Path) -> WorkflowState:
        return self.engine(project).archive_completed(self.root)


    def rollback_migration(self, project, *, expected_revision=None):
        raise InvalidTransition("Legacy workflow rollback is retired; use paper_new for historical recovery")

    def rollback_stage_scheduler(self, project, *, expected_revision=None):
        raise InvalidTransition("Step scheduler rollback is retired; the mainline uses Stage scheduling")

    @staticmethod
    def _require_stage_runtime(state: WorkflowState) -> None:
        if state.control_mode != "engine" or state.runtime_generation != "native_v2":
            raise InvalidTransition("NATIVE_WORKFLOW_REQUIRED: only native_v2 engine projects can run")
        if state.scheduler_generation != STAGE_SCHEDULER_GENERATION:
            raise InvalidTransition(
                "STAGE_SCHEDULER_REQUIRED: explicitly run migrate scheduler-activate "
                "on a stopped Native project before starting it"
            )

    def activate_stage_scheduler(
        self,
        project: str | Path,
        *,
        expected_revision: int | None = None,
    ) -> WorkflowState:
        resolved = self.resolve_project(project)
        store = SQLiteStateStore(resolved)
        state = store.load()
        self._assert_expected_revision(state, expected_revision)
        if state.scheduler_generation == STAGE_SCHEDULER_GENERATION:
            return state
        if state.scheduler_generation != STEP_SCHEDULER_GENERATION:
            raise InvalidTransition(
                f"unsupported scheduler generation: {state.scheduler_generation}"
            )
        if state.control_mode != "engine" or state.runtime_generation != "native_v2":
            raise InvalidTransition("only native engine projects can activate Stage scheduling")
        if state.runner_pid is not None or state.status in {
            WorkflowStatus.RUNNING,
            WorkflowStatus.RETRYING,
            WorkflowStatus.ARCHIVING,
        }:
            raise InvalidTransition("cannot change scheduler generation while a runner is active")
        if state.active_step is not None and state.attempt > 0:
            raise InvalidTransition(
                "recover the interrupted Step before activating Stage scheduling"
            )
        cursor = projected_stage_cursor(state)
        pending = state.pending_action is not None
        active_stage = cursor["active_stage"] if pending else None
        active_subtask = cursor["active_subtask"] if pending else None
        source_step_id = cursor["source_step_id"] if pending else None
        active_step = source_step_id if pending else None
        seeds = []
        for checkpoint in initial_stage_checkpoints(state.last_completed_step):
            seeds.append(
                {
                    **checkpoint,
                    "receipt": {
                        "schema_version": "factory-stage-checkpoint-v1",
                        "source": "explicit_scheduler_migration",
                        **checkpoint,
                    },
                }
            )
        updated = _workflow_coordinator(store).transition(
            expected_revision=state.revision,
            event_type="STAGE_SCHEDULER_ACTIVATED",
            changes={
                "scheduler_generation": STAGE_SCHEDULER_GENERATION,
                "stage_catalog_version": STAGE_CATALOG_VERSION,
                "last_completed_stage": cursor["last_completed_stage"],
                "active_stage": active_stage,
                "active_subtask": active_subtask,
                "source_step_id": source_step_id,
                "active_step": active_step,
                "attempt": 0,
            },
            payload={
                "from_scheduler_generation": STEP_SCHEDULER_GENERATION,
                "to_scheduler_generation": STAGE_SCHEDULER_GENERATION,
                "stage_catalog_version": STAGE_CATALOG_VERSION,
                "seeded_checkpoints": len(seeds),
            },
            stage_checkpoint_seed=seeds,
            replace_stage_checkpoints=True,
        )
        return updated


    def configure_solver_policy(
        self,
        project: str | Path,
        *,
        mode: str,
        threshold_seconds: int = 300,
        allowed_runtimes: list[str] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        resolved = self.resolve_project(project)
        if mode in {"cloud", "auto"} and self._cloud_quarantined():
            raise InvalidTransition("cloud solver execution is quarantined")
        store = SQLiteStateStore(resolved)
        state = store.load()
        revision = state.revision if expected_revision is None else expected_revision
        updated = _workflow_coordinator(store).configure_solver_policy(
            expected_revision=revision,
            mode=mode,
            threshold_seconds=threshold_seconds,
            allowed_runtimes=allowed_runtimes or ["python"],
        )
        write_compatibility_projections(resolved, updated)
        self._write_solver_policy_projection(resolved, store.solver_policy())
        return {"revision": updated.revision, **store.solver_policy()}

    def solver_policy(self, project: str | Path) -> dict[str, Any]:
        resolved = self.resolve_project(project)
        policy = SQLiteStateStore(resolved).solver_policy()
        return {
            **policy,
            "quarantined": self._cloud_quarantined(),
            "enabled": policy["mode"] in {"cloud", "auto"} and not self._cloud_quarantined(),
        }

    def submit_solver(
        self,
        project: str | Path,
        *,
        runtime: str,
        script: str | Path,
        args: tuple[str, ...] = (),
        max_time_seconds: int = 1_800,
        input_paths: tuple[str | Path, ...] = (),
        output_paths: tuple[str | Path, ...] = (),
        seeds: tuple[str | int, ...] = (),
        expected_revision: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        resolved = self.resolve_project(project)
        self._require_stage_runtime(SQLiteStateStore(resolved).load())
        script_path = Path(script)
        if not script_path.is_absolute():
            script_path = (Path.cwd() / script_path).resolve()
        else:
            script_path = script_path.resolve()
        try:
            script_path.relative_to(resolved)
        except ValueError as exc:
            raise ValueError("solver script must be inside the project") from exc
        if not script_path.is_file():
            raise FileNotFoundError(f"solver script not found: {script_path}")
        if max_time_seconds < 1 or max_time_seconds > 86_400:
            raise ValueError("max_time_seconds must be between 1 and 86400")
        store = SQLiteStateStore(resolved)
        policy = store.solver_policy()
        if runtime not in policy["allowed_runtimes"]:
            raise InvalidTransition(f"solver runtime is not allowed: {runtime}")
        backend_name = self._solver_backend_for(policy, runtime, max_time_seconds)
        backend = self.solver_backends.get(backend_name)
        state = store.load()
        if dry_run:
            self._assert_expected_revision(state, expected_revision)
            return {"dry_run": True, "backend": backend_name, "runtime": runtime,
                    "script": str(script_path), "max_time_seconds": max_time_seconds}
        revision = state.revision if expected_revision is None else expected_revision
        idempotency_key = self._solver_idempotency_key(
            resolved,
            state,
            backend=backend_name,
            runtime=runtime,
            script=script_path,
            args=args,
            max_time_seconds=max_time_seconds,
            input_paths=input_paths,
            output_paths=output_paths,
            seeds=seeds,
        )
        existing = store.solver_job_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing
        job_id = f"{backend_name}_{runtime}_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
        attempt_id = self._solver_attempt_id(state)
        try:
            _workflow_coordinator(store).create_solver_job(
                expected_revision=revision,
                record={
                    "job_id": job_id,
                    "idempotency_key": idempotency_key,
                    "owner_stage": state.active_stage,
                    "owner_subtask": state.active_subtask,
                    "owner_revision": state.revision,
                    "attempt_id": attempt_id,
                    "backend": backend_name,
                    "runtime": runtime,
                    "script": str(script_path.relative_to(resolved)),
                    "workdir": str(script_path.parent),
                    "argv": list(args),
                    "max_time_seconds": max_time_seconds,
                    "status": "submitting",
                },
            )
        except (sqlite3.IntegrityError, RevisionConflict):
            raced = store.solver_job_by_idempotency_key(idempotency_key)
            if raced is None:
                raise
            return raced
        created_job = store.solver_job(job_id)
        receipt_dir = resolved / ".factory" / "solver_receipts"
        submitted_path, _completed_path = receipt_paths(receipt_dir, job_id)
        try:
            submission_receipt = build_submission_receipt(
                project_dir=resolved,
                job_id=job_id,
                backend=backend_name,
                runtime=runtime,
                script=script_path,
                workdir=script_path.parent,
                argv=args,
                max_time_seconds=max_time_seconds,
                requested_at=int(created_job["requested_at"]),
                dependency_enforcement=("python-audit-open-v2"
                    if backend_name == "local" and runtime == "python" else "DECLARATION_ONLY"),
                input_paths=input_paths,
                output_paths=output_paths,
                seeds=seeds,
            )
            write_receipt(submitted_path, submission_receipt)
            _workflow_coordinator(store).record_solver_receipt(
                job_id,
                stage="submitted",
                receipt_path=submitted_path.relative_to(resolved).as_posix(),
                receipt_sha256=file_sha256(submitted_path),
                content_sha256=str(submission_receipt["content_sha256"]),
                request_sha256=str(submission_receipt["request_sha256"]),
            )
        except Exception as exc:
            self._record_solver_submission_failure(
                store,
                job_id,
                {
                    "type": type(exc).__name__,
                    "message": "solver submission receipt creation failed",
                },
            )
            raise
        request = SolverRequest(
            job_id=job_id,
            idempotency_key=idempotency_key,
            project_dir=resolved,
            runtime=runtime,
            script=script_path,
            args=args,
            max_time_seconds=max_time_seconds,
            env={"FACTORY_SOLVER_JOB_ID": job_id},
            input_paths=tuple(
                (
                    Path(value).resolve(strict=True)
                    if Path(value).is_absolute()
                    else (resolved / Path(value)).resolve(strict=True)
                )
                for value in input_paths
            ),
            output_paths=tuple(
                (
                    Path(value).resolve(strict=False).relative_to(resolved).as_posix()
                    if Path(value).is_absolute()
                    else (resolved / Path(value)).resolve(strict=False).relative_to(resolved).as_posix()
                )
                for value in output_paths
            ),
            seeds=tuple(str(seed) for seed in seeds),
            submission_receipt=submitted_path,
        )
        try:
            submission = backend.submit(request)
        except Exception as exc:
            self._record_solver_submission_failure(
                store,
                job_id,
                {
                    "type": type(exc).__name__,
                    "message": "solver backend submission failed",
                },
            )
            raise
        return self._record_solver_submission(
            store, backend, job_id, submission
        )

    @staticmethod
    def _solver_attempt_id(state: WorkflowState) -> str:
        return ":".join(
            (
                f"stage-{state.active_stage if state.active_stage is not None else 'adhoc'}",
                f"subtask-{state.active_subtask or 'adhoc'}",
                f"step-{state.source_step_id if state.source_step_id is not None else state.active_step}",
                f"attempt-{state.attempt}",
            )
        )

    @classmethod
    def _solver_idempotency_key(
        cls,
        project: Path,
        state: WorkflowState,
        *,
        backend: str,
        runtime: str,
        script: Path,
        args: tuple[str, ...],
        max_time_seconds: int,
        input_paths: tuple[str | Path, ...],
        output_paths: tuple[str | Path, ...],
        seeds: tuple[str | int, ...],
    ) -> str:
        root = project.resolve()

        def input_record(value: str | Path) -> dict[str, Any]:
            raw = Path(value)
            candidate = raw if raw.is_absolute() else root / raw
            resolved = candidate.resolve(strict=True)
            try:
                relative = resolved.relative_to(root).as_posix()
            except ValueError as exc:
                raise ValueError(f"solver input must be inside the project: {value}") from exc
            if not resolved.is_file():
                raise ValueError(f"solver input must be a regular file: {value}")
            return {"path": relative, "sha256": file_sha256(resolved)}

        def output_record(value: str | Path) -> str:
            raw = Path(value)
            candidate = raw if raw.is_absolute() else root / raw
            resolved = candidate.resolve(strict=False)
            try:
                return resolved.relative_to(root).as_posix()
            except ValueError as exc:
                raise ValueError(f"solver output must be inside the project: {value}") from exc

        body = {
            "schema": "factory-solver-idempotency-v1",
            "project_id": state.project_id,
            "attempt_id": cls._solver_attempt_id(state),
            "backend": backend,
            "runtime": runtime,
            "script": {
                "path": script.relative_to(root).as_posix(),
                "sha256": file_sha256(script),
            },
            "argv": list(args),
            "max_time_seconds": max_time_seconds,
            "inputs": [input_record(value) for value in input_paths],
            "outputs": [output_record(value) for value in output_paths],
            "seeds": [str(seed) for seed in seeds],
        }
        if backend == "local" and runtime == "python":
            body["schema"] = "factory-solver-idempotency-v3"
            body["dependency_enforcement"] = "python-audit-open-v2"
        return canonical_hash(body)

    def solver_status(self, project: str | Path, job_id: str) -> dict[str, Any]:
        resolved = self.resolve_project(project)
        store = SQLiteStateStore(resolved)
        job = store.solver_job(job_id)
        if job["status"] in SOLVER_TERMINAL_STATUSES:
            self._ensure_solver_completion_receipt(resolved, job)
            return job
        if job["status"] == "submitting":
            return self._reconcile_submitting_solver(resolved, store, job)
        backend = self.solver_backends.get(job["backend"])
        observed = backend.status(job)
        if observed != job["status"]:
            try:
                _workflow_coordinator(store).update_solver_job(
                    job_id,
                    expected_job_revision=int(job["job_revision"]),
                    status=observed,
                )
            except RevisionConflict:
                updated = store.solver_job(job_id)
                if updated["status"] in SOLVER_TERMINAL_STATUSES:
                    self._ensure_solver_completion_receipt(resolved, updated)
                return updated
        updated = store.solver_job(job_id)
        if updated["status"] in SOLVER_TERMINAL_STATUSES:
            self._ensure_solver_completion_receipt(resolved, updated)
        return updated

    def wait_solver(
        self,
        project: str | Path,
        job_id: str,
        *,
        poll_seconds: float = 1.0,
    ) -> dict[str, Any]:
        while True:
            job = self.solver_status(project, job_id)
            if job["status"] in SOLVER_TERMINAL_STATUSES:
                return job
            time.sleep(poll_seconds)

    def cancel_solver(self, project: str | Path, job_id: str) -> dict[str, Any]:
        resolved = self.resolve_project(project)
        store = SQLiteStateStore(resolved)
        job = store.solver_job(job_id)
        observed = self.solver_backends.get(job["backend"]).cancel(job)
        target_status = (
            str(observed)
            if isinstance(observed, str) and observed
            else "cancelling"
        )
        try:
            _workflow_coordinator(store).update_solver_job(
                job_id,
                expected_job_revision=int(job["job_revision"]),
                status=target_status,
            )
        except RevisionConflict:
            updated = store.solver_job(job_id)
        else:
            updated = store.solver_job(job_id)
        if updated["status"] in SOLVER_TERMINAL_STATUSES:
            self._ensure_solver_completion_receipt(resolved, updated)
        return updated

    def state_json(self, project: str | Path) -> str:
        payload = asdict(self.inspect(project))
        payload["status"] = payload["status"].value
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)

    def _resolve_pending_if_ready(
        self, project: Path, state: WorkflowState
    ) -> WorkflowState:
        pending = state.pending_action or {}
        gate = str(pending.get("gate") or "")
        action_type = pending.get("type")
        action_metadata = pending.get("metadata") or {}
        human_request = action_metadata.get("human_decision") or {}
        decision_kind = human_request.get("kind")
        ready = False
        resolution: dict[str, Any] = {
            "source": "artifact_projection",
            "gate": gate,
        }
        if decision_kind in {"selection", "approval"} or (
            action_type and action_type.endswith("selection")
        ):
            store = SQLiteStateStore(project)
            recorded = store.decision(gate or "step3")
            ready = recorded is not None
            if recorded is not None:
                resolution.update(
                    selected_option_id=(
                        recorded.get("selected_option_id")
                        or recorded.get("selected_primary")
                    ),
                    selected_aux_id=(
                        recorded.get("selected_aux_id")
                        or recorded.get("selected_auxiliary")
                        or ""
                    ),
                    approved=recorded.get("approved"),
                    request_id=recorded.get("request_id"),
                )
            if not ready and store.contest_policy() is None:
                ready = (
                    project / "selection" / f"{gate or 'step3'}_decision.json"
                ).is_file()
        elif decision_kind == "consultation" or action_type == "human_consultation":
            from .consultation_projection import extract_ready_consultation_answer

            answer = extract_ready_consultation_answer(project, gate) if gate else None
            ready = bool(answer)
            if answer:
                resolution["answer"] = answer
        if not ready:
            return state
        resolution, decision_record = self._bind_consultation_staging(
            project, dict(pending), resolution
        )
        updated = self.engine(project).resolve_action(
            resolution,
            expected_revision=state.revision,
            decision_record=decision_record,
        )
        if decision_kind == "consultation" or action_type == "human_consultation":
            self._rebuild_consultation_projection_or_record(project, gate)
        return updated

    def _write_delivery_manifest(self, project: Path) -> None:
        from .delivery.release import resolve_current_release

        release = resolve_current_release(
            self.root / "papers", project.name, project=project
        )
        if release is None:
            return
        from scripts.delivery_contract import write_delivery_manifest

        manifest = write_delivery_manifest(project, self.root)
        if manifest.get("status") not in {"CURRENT_PASS", "GATE2_OVERRIDE_DELIVERED"}:
            raise InvalidTransition("completed project failed final delivery evaluation")

    def _solver_backend_for(
        self, policy: dict[str, Any], runtime: str, max_time_seconds: int
    ) -> str:
        mode = policy["mode"]
        if mode == "local":
            return "local"
        cloud_eligible = (
            not self._cloud_quarantined()
            and runtime in policy["allowed_runtimes"]
            and max_time_seconds >= int(policy["threshold_seconds"])
        )
        if mode == "cloud":
            if not cloud_eligible:
                raise InvalidTransition("cloud solver policy cannot run this job")
            return "cloud_run"
        return "cloud_run" if cloud_eligible else "local"

    @staticmethod
    def _record_solver_submission(
        store: SQLiteStateStore,
        backend: object,
        job_id: str,
        submission,
    ) -> dict[str, Any]:
        """Persist an external ID even if a concurrent job control won the CAS."""
        while True:
            job = store.solver_job(job_id)
            if job["external_id"]:
                return job
            status = (
                submission.status
                if job["status"] == "submitting"
                else job["status"]
            )
            try:
                _workflow_coordinator(store).update_solver_job(
                    job_id,
                    expected_job_revision=int(job["job_revision"]),
                    status=status,
                    external_id=submission.external_id,
                    result_refs=submission.result_refs,
                )
            except RevisionConflict:
                continue
            updated = store.solver_job(job_id)
            if updated["status"] == "cancelled":
                backend.cancel(updated)
            return updated

    @staticmethod
    def _record_solver_submission_failure(
        store: SQLiteStateStore,
        job_id: str,
        failure: dict[str, Any],
    ) -> dict[str, Any]:
        while True:
            job = store.solver_job(job_id)
            if job["status"] != "submitting":
                return job
            try:
                _workflow_coordinator(store).update_solver_job(
                    job_id,
                    expected_job_revision=int(job["job_revision"]),
                    status="failed",
                    failure=failure,
                )
            except RevisionConflict:
                continue
        return store.solver_job(job_id)

    def _reconcile_submitting_solver(
        self,
        project: Path,
        store: SQLiteStateStore,
        job: dict[str, Any],
    ) -> dict[str, Any]:
        """Recover the crash window after provider acceptance but before local ack."""

        if job["backend"] == "cloud_run":
            backend = self.solver_backends.get(job["backend"])
            try:
                observed = backend.status({**job, "external_id": job["job_id"]})
            except Exception:
                return {**job, "reconciliation_status": "provider_lookup_pending"}
            _workflow_coordinator(store).update_solver_job(
                job["job_id"],
                expected_job_revision=int(job["job_revision"]),
                status=observed,
                external_id=job["job_id"],
            )
            return store.solver_job(job["job_id"])
        exit_path = project / ".factory" / "solver_jobs" / f"{job['job_id']}.json"
        if exit_path.is_file():
            try:
                observed = str(json.loads(exit_path.read_text(encoding="utf-8"))["status"])
            except (KeyError, OSError, json.JSONDecodeError):
                return {**job, "reconciliation_status": "local_receipt_invalid"}
            _workflow_coordinator(store).update_solver_job(
                job["job_id"],
                expected_job_revision=int(job["job_revision"]),
                status=observed,
                result_refs={"exit": exit_path.relative_to(project).as_posix()},
            )
            return store.solver_job(job["job_id"])
        return {**job, "reconciliation_status": "local_process_identity_unavailable"}

    @staticmethod
    def _ensure_solver_completion_receipt(project: Path, job: dict[str, Any]) -> None:
        receipt_dir = project / ".factory" / "solver_receipts"
        submitted_path, completed_path = receipt_paths(receipt_dir, str(job["job_id"]))
        if not submitted_path.is_file():
            return
        try:
            if completed_path.is_file():
                receipt = read_receipt(completed_path, COMPLETION_SCHEMA)
            else:
                receipt = build_completion_receipt(
                    project_dir=project,
                    submission_path=submitted_path,
                    status=str(job["status"]),
                    finished_at=int(job.get("finished_at") or time.time()),
                    result_refs=(job.get("result_refs") if isinstance(job.get("result_refs"), dict) else {}),
                )
                write_receipt(completed_path, receipt)
            submitted = read_receipt(submitted_path, SUBMISSION_SCHEMA)
            receipt_store = SQLiteStateStore(project)
            _workflow_coordinator(receipt_store).record_solver_receipt(
                str(job["job_id"]),
                stage="completed",
                receipt_path=completed_path.relative_to(project).as_posix(),
                receipt_sha256=file_sha256(completed_path),
                content_sha256=str(receipt["content_sha256"]),
                request_sha256=str(submitted["request_sha256"]),
            )
        except (OSError, ReceiptError, ValueError):
            # Job state remains queryable. Public evidence will fail closed and
            # expose the missing/invalid receipt instead of promoting metadata.
            return

    @staticmethod
    def _cloud_quarantined() -> bool:
        return os.getenv("CLOUD_SOLVER_QUARANTINED", "true").strip().lower() == "true"

    @staticmethod
    def _write_solver_policy_projection(project: Path, policy: dict[str, Any]) -> None:
        path = project / ".env.cloud"
        if policy["mode"] == "local":
            path.unlink(missing_ok=True)
            return
        path.write_text(
            "# Compatibility projection; SQLite project_config is authoritative.\n"
            "USE_CLOUD_SOLVER=true\n"
            f"CLOUD_THRESHOLD_TIME={policy['threshold_seconds']}\n"
            f"CLOUD_SOLVER_TYPES={','.join(policy['allowed_runtimes'])}\n",
            encoding="utf-8",
        )

    @staticmethod
    def _assert_expected_revision(
        state: WorkflowState, expected_revision: int | None
    ) -> None:
        if expected_revision is not None and state.revision != expected_revision:
            raise RevisionConflict(
                f"expected revision {expected_revision}, found {state.revision}"
            )

    @staticmethod
    def _pid_is_live(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (PermissionError, ProcessLookupError):
            return False

    @staticmethod
    def _recorded_runner_identity(snapshot: dict[str, Any]) -> str | None:
        state = snapshot["state"]
        if not snapshot["aggregate_valid"] or not state.runner_lease_id:
            return None
        for event in reversed(snapshot["events"]):
            if event.type not in {"WORKER_LAUNCHED", "RUN_STARTED"}:
                continue
            payload = event.payload
            identity = payload.get("worker_identity")
            if (payload.get("worker_pid") == state.runner_pid
                    and payload.get("lease_id") == state.runner_lease_id
                    and isinstance(identity, str) and identity):
                return identity
            return None  # Never fall back to an earlier owner's identity.
        return None

    def _verify_runner_stop(self, project: Path, snapshot: dict[str, Any]) -> None:
        pid = snapshot["state"].runner_pid
        try:
            self._terminate_runner(pid, self._recorded_runner_identity(snapshot))
        except (OSError, RuntimeError) as exc:
            store = SQLiteStateStore(project)
            state = store.load()
            _workflow_coordinator(store).transition(
                expected_revision=state.revision, event_type="RUNNER_EXIT_UNVERIFIED",
                changes={"status": WorkflowStatus.INTERRUPTED},
                payload={"error_class": "RUNNER_EXIT_UNVERIFIED", "worker_pid": pid,
                         "exception_type": type(exc).__name__, "process_tree_exited": False},
            )
            raise

    @staticmethod
    def _terminate_runner(pid: int | None, expected_identity: str | None) -> None:
        if not pid:
            return
        from .adapters.infrastructure.process import _descendants, _process_identity
        if not expected_identity:
            raise RuntimeError("runner ownership has no persisted launch identity and lease")
        identity = _process_identity(pid)
        if identity is None:
            return
        if identity != expected_identity:
            raise RuntimeError("runner identity differs from the persisted owner")
        if pid == os.getpid():
            raise RuntimeError("runner is the current controller; exit cannot be verified here")
        owned = {pid: expected_identity, **_descendants(pid)}
        for sig, grace in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 1.0)):
            for child, token in list(owned.items()):
                if _process_identity(child) != token:
                    continue
                owned.update(_descendants(child))
                try:
                    os.kill(child, sig)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                if all(_process_identity(child) != token for child, token in owned.items()):
                    return
                time.sleep(0.02)
        raise RuntimeError("runner exit not verified for all observed processes")


def wait_for_worker_ready(path: Path, *, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.is_file():
            path.unlink(missing_ok=True)
            return
        time.sleep(0.05)
    raise TimeoutError(f"worker launch handshake timed out: {path}")


# ---------------------------------------------------------------------------
# S4.1: structured reason vocabularies
#
# Closed on purpose: a free-form string would be no better than the empty reason
# message it replaces.  The canonical ``code`` stays the discriminator; these
# only add *why* and *who*.
# ---------------------------------------------------------------------------

PAUSED_SUBCODES = frozenset(
    {"OPERATOR", "DEADLINE", "EXECUTION_SCOPE", "FREEZE_BOUNDARY"}
)
RESUMED_SUBCODES = frozenset({"OPERATOR", "RETRY", "AUTO_RECOVERY"})
REASON_ACTORS = frozenset({"operator", "engine", "scheduler"})


def _require_reason_detail(event: str, subcode: str, actor: str) -> None:
    """Refuse an empty or unknown reason detail rather than recording a useless one."""

    allowed = PAUSED_SUBCODES if event == "PAUSED" else RESUMED_SUBCODES
    if subcode not in allowed:
        raise InvalidTransition(
            f"{event} subcode must be one of {sorted(allowed)}, got {subcode!r}"
        )
    if actor not in REASON_ACTORS:
        raise InvalidTransition(
            f"{event} actor must be one of {sorted(REASON_ACTORS)}, got {actor!r}"
        )
