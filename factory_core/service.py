from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .adapters.legacy import LegacyArtifactValidator, build_legacy_registry
from .domain import (
    FactoryCoreError,
    InvalidTransition,
    RevisionConflict,
    WorkflowState,
    WorkflowStatus,
)
from .engine import FactoryEngine
from .migration import LegacyInspector, MigrationReport, apply_migration
from .projections import runtime_payload, write_compatibility_projections
from .steps import build_native_registry
from .adapters.solvers import SolverRequest, build_solver_backends
from .registry import SolverBackendRegistry
from .storage import SQLiteStateStore


BASE_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class WorkerHandle:
    pid: int
    log_path: Path
    state: WorkflowState


class WorkerLauncher:
    """Launch the single engine worker entry point in its own process group."""

    def __init__(self, factory_root: Path, code_root: Path | None = None) -> None:
        self.factory_root = factory_root.resolve()
        self.code_root = (code_root or factory_root).resolve()

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
            updated = store.transition(
                expected_revision=revision,
                event_type="WORKER_LAUNCHED",
                changes={
                    "status": WorkflowStatus.RUNNING,
                    "runner_pid": process.pid,
                    "runner_lease_id": f"launch:{process.pid}",
                    "heartbeat_at": int(time.time()),
                },
                payload={"worker_pid": process.pid, "log": str(log_path.relative_to(project))},
            )
            write_compatibility_projections(project, updated)
            ready.write_text(str(process.pid) + "\n", encoding="ascii")
        except Exception:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (PermissionError, ProcessLookupError):
                pass
            raise
        return WorkerHandle(process.pid, log_path, updated)


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
        self.legacy_runner = self.code_root / "factory_core" / "adapters" / "legacy_runner.sh"
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
        if state.control_mode == "legacy":
            registry = build_legacy_registry(self.root, self.legacy_runner)
        elif state.control_mode != "engine":
            raise FactoryCoreError(f"unsupported control mode: {state.control_mode}")
        elif state.runtime_generation == "native_v2":
            registry = self._native_registry_factory(self.code_root)
        elif state.runtime_generation == "legacy_adapter":
            registry = build_legacy_registry(self.root, self.legacy_runner)
        else:
            raise FactoryCoreError(
                f"unsupported runtime generation: {state.runtime_generation}"
            )
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
    ) -> tuple[WorkflowState, WorkerHandle | None]:
        if not BASE_NAME_RE.fullmatch(base_name):
            raise ValueError("base_name must contain only letters, numbers, '_' or '-'")
        ongoing = self.root / "ongoing"
        complete = self.root / "complete"
        project = ongoing / base_name
        if project.exists() or (complete / base_name).exists():
            raise FileExistsError(f"project already exists: {base_name}")
        directories = (
            "style", "bib", "figures", "tables", "do/archive", "logs",
            "replication", "replication/temp", "data/raw", "data/intermediate",
            "data/final", "tmp", "scripts", "docs",
        )
        for relative in directories:
            (project / relative).mkdir(parents=True, exist_ok=True)
        copies = (
            (self.code_root / "resources/style/paper.sty", project / "style/paper.sty"),
            (self.code_root / "resources/bib/bibliography.bst", project / "bib/bibliography.bst"),
            (self.code_root / "resources/style/model_papers_style.json", project / "style/model_papers_style.json"),
            (self.code_root / "analysis_guide.md", project / "analysis_guide.md"),
            (self.code_root / "modeling_guide.md", project / "modeling_guide.md"),
        )
        for source, target in copies:
            if source.is_file():
                shutil.copy2(source, target)
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
        )
        write_compatibility_projections(project, state)
        worker = self.start(project) if start else None
        return (worker.state if worker is not None else state), worker

    def inspect(self, project: str | Path) -> WorkflowState:
        return SQLiteStateStore(self.resolve_project(project)).load()

    def status(self, project: str | Path) -> dict[str, Any]:
        return runtime_payload(self.inspect(project))

    def start(
        self,
        project: str | Path,
        *,
        expected_revision: int | None = None,
    ) -> WorkerHandle:
        resolved = self.resolve_project(project)
        state = SQLiteStateStore(resolved).load()
        self._assert_expected_revision(state, expected_revision)
        if state.runner_pid and self._pid_is_live(state.runner_pid):
            raise InvalidTransition(f"project already has live worker {state.runner_pid}")
        if (
            state.runner_pid is not None
            or state.status in {WorkflowStatus.RUNNING, WorkflowStatus.RETRYING}
        ):
            store = SQLiteStateStore(resolved)
            state = store.transition(
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
            write_compatibility_projections(resolved, state)
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
    ) -> WorkflowState:
        engine = self.engine(project)
        state = engine.run(max_steps=max_steps)
        if state.status is WorkflowStatus.COMPLETED:
            self._write_delivery_manifest(engine.project_dir)
            if archive and engine.project_dir.parent.name == "ongoing":
                state = engine.archive_completed(self.root)
        return state

    def pause(self, project: str | Path, *, expected_revision: int | None = None) -> WorkflowState:
        engine = self.engine(project)
        state = engine.get_state()
        revision = state.revision if expected_revision is None else expected_revision
        updated = engine.pause(expected_revision=revision)
        self._terminate_runner(state.runner_pid)
        return updated

    def resume(self, project: str | Path, *, expected_revision: int | None = None) -> WorkflowState:
        engine = self.engine(project)
        state = engine.get_state()
        self._assert_expected_revision(state, expected_revision)
        if state.pending_action is not None:
            state = self._resolve_pending_if_ready(engine.project_dir, state)
        return engine.resume(expected_revision=state.revision)

    def kill(self, project: str | Path, *, expected_revision: int | None = None) -> WorkflowState:
        engine = self.engine(project)
        state = engine.get_state()
        revision = state.revision if expected_revision is None else expected_revision
        updated = engine.kill(expected_revision=revision)
        self._terminate_runner(state.runner_pid)
        return updated

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
        return engine.resolve_action(resolution, expected_revision=revision)

    def resolve_and_start(
        self,
        project: str | Path,
        resolution: dict[str, Any],
        *,
        evidence_writer: Callable[[], None],
        expected_revision: int | None = None,
    ) -> tuple[WorkflowState, WorkerHandle]:
        """Accept a pending decision before projecting evidence and starting."""
        resolved_project = self.resolve_project(project)
        engine = self.engine(resolved_project)
        pending = engine.get_state()
        self._assert_expected_revision(pending, expected_revision)
        if pending.pending_action is None:
            raise InvalidTransition("project has no pending action")
        accepted = engine.resolve_action(
            resolution, expected_revision=pending.revision
        )
        try:
            evidence_writer()
        except Exception as exc:
            store = SQLiteStateStore(resolved_project)
            restored = store.transition(
                expected_revision=accepted.revision,
                event_type="ACTION_PROJECTION_FAILED",
                changes={
                    "status": pending.status,
                    "pending_action": pending.pending_action,
                },
                payload={"error_type": type(exc).__name__},
            )
            write_compatibility_projections(resolved_project, restored)
            raise
        return self.resume_and_start(
            resolved_project, expected_revision=accepted.revision
        )

    def archive(self, project: str | Path) -> WorkflowState:
        return self.engine(project).archive_completed(self.root)

    def inspect_migration(self, project: str | Path) -> MigrationReport:
        resolved = self.resolve_project(project)
        validator = LegacyArtifactValidator(self.root, self.legacy_runner)
        return LegacyInspector(infer_step=validator.infer_step).inspect(resolved)

    def apply_migration(
        self,
        project: str | Path,
        report: MigrationReport,
        *,
        expected_digest: str,
        runtime_generation: str = "native_v2",
    ) -> WorkflowState:
        resolved = self.resolve_project(project)
        state = apply_migration(
            resolved,
            report,
            expected_digest=expected_digest,
            runtime_generation=runtime_generation,
        )
        write_compatibility_projections(resolved, state)
        return state

    def rollback_migration(self, project: str | Path) -> WorkflowState:
        engine = self.engine(project)
        state = engine.get_state()
        return engine.deactivate(expected_revision=state.revision)

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
        updated = store.configure_solver_policy(
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
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        resolved = self.resolve_project(project)
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
        revision = state.revision if expected_revision is None else expected_revision
        job_id = f"{backend_name}_{runtime}_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
        store.create_solver_job(
            expected_revision=revision,
            record={
                "job_id": job_id,
                "backend": backend_name,
                "runtime": runtime,
                "script": str(script_path.relative_to(resolved)),
                "workdir": str(script_path.parent),
                "argv": list(args),
                "max_time_seconds": max_time_seconds,
                "status": "submitting",
            },
        )
        request = SolverRequest(
            job_id=job_id,
            project_dir=resolved,
            runtime=runtime,
            script=script_path,
            args=args,
            max_time_seconds=max_time_seconds,
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

    def solver_status(self, project: str | Path, job_id: str) -> dict[str, Any]:
        resolved = self.resolve_project(project)
        store = SQLiteStateStore(resolved)
        job = store.solver_job(job_id)
        if job["status"] in {"completed", "failed", "timeout", "cancelled"}:
            return job
        if job["status"] == "submitting":
            return job
        backend = self.solver_backends.get(job["backend"])
        observed = backend.status(job)
        if observed != job["status"]:
            try:
                store.update_solver_job(
                    job_id,
                    expected_job_revision=int(job["job_revision"]),
                    status=observed,
                )
            except RevisionConflict:
                return store.solver_job(job_id)
        return store.solver_job(job_id)

    def wait_solver(
        self,
        project: str | Path,
        job_id: str,
        *,
        poll_seconds: float = 1.0,
    ) -> dict[str, Any]:
        while True:
            job = self.solver_status(project, job_id)
            if job["status"] != "running":
                return job
            time.sleep(poll_seconds)

    def cancel_solver(self, project: str | Path, job_id: str) -> dict[str, Any]:
        resolved = self.resolve_project(project)
        store = SQLiteStateStore(resolved)
        job = store.solver_job(job_id)
        self.solver_backends.get(job["backend"]).cancel(job)
        try:
            store.update_solver_job(
                job_id,
                expected_job_revision=int(job["job_revision"]),
                status="cancelled",
            )
        except RevisionConflict:
            return store.solver_job(job_id)
        return store.solver_job(job_id)

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
        ready = False
        if action_type == "step3_selection":
            ready = (project / "selection" / f"{gate or 'step3'}_decision.json").is_file()
        elif action_type == "human_consultation":
            review = project / "human_review.md"
            if review.is_file():
                text = review.read_text(encoding="utf-8", errors="replace")
                ready = bool(
                    re.search(
                        rf"##\s+CONSULT\s+{re.escape(gate)}\b[\s\S]*?STATUS:\s*READY",
                        text,
                        re.IGNORECASE,
                    )
                ) if gate else "STATUS: READY" in text
        if not ready:
            return state
        return self.engine(project).resolve_action(
            {"source": "artifact_projection", "gate": gate},
            expected_revision=state.revision,
        )

    def _write_delivery_manifest(self, project: Path) -> None:
        papers_pdf = self.root / "papers" / f"{project.name}_paper.pdf"
        submission_zip = self.root / "papers" / f"{project.name}_submission.zip"
        if not papers_pdf.is_file() or not submission_zip.is_file():
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
                store.update_solver_job(
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
                store.update_solver_job(
                    job_id,
                    expected_job_revision=int(job["job_revision"]),
                    status="failed",
                    failure=failure,
                )
            except RevisionConflict:
                continue
            return store.solver_job(job_id)

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
    def _terminate_runner(pid: int | None) -> None:
        if not pid or pid == os.getpid():
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except (PermissionError, ProcessLookupError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (PermissionError, ProcessLookupError):
                pass


def wait_for_worker_ready(path: Path, *, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.is_file():
            path.unlink(missing_ok=True)
            return
        time.sleep(0.05)
    raise TimeoutError(f"worker launch handshake timed out: {path}")
