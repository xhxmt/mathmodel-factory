import os
import time
import io
import json
import base64
import hashlib

import pytest

from factory_core.adapters.solvers import (
    SolverRequest,
    SolverSubmission,
    build_solver_backends,
)
from factory_core.domain import InvalidTransition, RevisionConflict, WorkflowStatus
from factory_core.registry import SolverBackendRegistry
from factory_core.service import FactoryService
from factory_core.storage import SQLiteStateStore
from factory_core.workflow_events import replay_events, replay_state


class CountingBackend:
    """A solver backend that records every submission it receives."""

    name = "local"

    def __init__(self):
        self.requests = []
        self.cancelled = []

    def submit(self, request):
        self.requests.append(request)
        return SolverSubmission("external-123", result_refs={"result": "results/answer.json"})

    def status(self, _job):
        return "completed"

    def cancel(self, job):
        self.cancelled.append(job.get("external_id"))


def make_project(tmp_path):
    service = FactoryService(tmp_path)
    service.create_project("demo", "question", start=False)
    project = tmp_path / "ongoing/demo"
    script = project / "models/solve.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('done')\n", encoding="utf-8")
    return service, project, script


def test_solver_policy_is_revision_checked_and_evented(tmp_path):
    service, project, _script = make_project(tmp_path)
    before = service.inspect(project)

    policy = service.configure_solver_policy(
        project,
        allowed_runtimes=["python", "julia"],
        expected_revision=before.revision,
    )

    assert policy["revision"] == before.revision + 1
    assert policy["allowed_runtimes"] == ["julia", "python"]
    assert SQLiteStateStore(project).events()[-1].type == "SOLVER_POLICY_CONFIGURED"
    with pytest.raises(Exception, match="expected revision"):
        service.configure_solver_policy(
            project,
            expected_revision=before.revision,
        )


def test_local_solver_job_uses_sqlite_lifecycle(tmp_path):
    service, project, script = make_project(tmp_path)

    job = service.submit_solver(
        project, runtime="python", script=script, max_time_seconds=10
    )
    completed = service.wait_solver(project, job["job_id"], poll_seconds=0.02)

    assert job["backend"] == "local"
    assert completed["status"] == "completed"
    event_types = [event.type for event in SQLiteStateStore(project).events()]
    assert "SOLVER_JOB_SUBMITTED" in event_types
    assert "SOLVER_JOB_RECEIPT_SUBMITTED" in event_types
    assert "SOLVER_JOB_RUNNING" in event_types
    assert "SOLVER_JOB_COMPLETED" in event_types
    assert "SOLVER_JOB_RECEIPT_COMPLETED" in event_types
    assert completed["result_refs"]["stdout"] == (
        f"logs/solver_jobs/{job['job_id']}.stdout.log"
    )
    assert completed["result_refs"]["stderr"] == (
        f"logs/solver_jobs/{job['job_id']}.stderr.log"
    )
    assert (project / completed["result_refs"]["stdout"]).read_text(
        encoding="utf-8"
    ) == "done\n"
    assert (project / completed["result_refs"]["stderr"]).read_text(
        encoding="utf-8"
    ) == ""
    store = SQLiteStateStore(project)
    assert replay_events(store.events()) == replay_state(store.load())


def test_local_solver_logs_are_isolated_by_job_id(tmp_path):
    service, project, script = make_project(tmp_path)
    script.write_text(
        "import sys\nprint(sys.argv[1])\n",
        encoding="utf-8",
    )

    first = service.submit_solver(
        project,
        runtime="python",
        script=script,
        args=("first",),
        max_time_seconds=10,
    )
    first = service.wait_solver(project, first["job_id"], poll_seconds=0.02)
    second = service.submit_solver(
        project,
        runtime="python",
        script=script,
        args=("second",),
        max_time_seconds=10,
    )
    second = service.wait_solver(project, second["job_id"], poll_seconds=0.02)

    first_stdout = project / first["result_refs"]["stdout"]
    second_stdout = project / second["result_refs"]["stdout"]
    assert first_stdout != second_stdout
    assert first_stdout.read_text(encoding="utf-8") == "first\n"
    assert second_stdout.read_text(encoding="utf-8") == "second\n"


def test_duplicate_solver_request_returns_existing_job_without_resubmission(tmp_path):
    _service, project, script = make_project(tmp_path)
    backend = CountingBackend()
    backends = SolverBackendRegistry()
    backends.register("local", backend)
    service = FactoryService(tmp_path, solver_backends=backends)

    first = service.submit_solver(
        project, runtime="python", script=script, max_time_seconds=10
    )
    second = service.submit_solver(
        project, runtime="python", script=script, max_time_seconds=10
    )

    assert second["job_id"] == first["job_id"]
    assert second["idempotency_key"] == first["idempotency_key"]
    assert len(first["request_sha256"]) == 64
    assert len(backend.requests) == 1
    assert backend.requests[0].env["FACTORY_SOLVER_JOB_ID"] == first["job_id"]


def test_solver_jobs_always_run_locally_and_the_policy_has_no_cloud_mode(tmp_path):
    service, project, script = make_project(tmp_path)

    dry = service.submit_solver(
        project, runtime="python", script=script, max_time_seconds=7_200, dry_run=True
    )
    assert dry["backend"] == "local"
    assert set(service.solver_policy(project)) == {"allowed_runtimes", "updated_revision"}
    store = SQLiteStateStore(project)
    for mode in ("cloud", "auto"):
        with pytest.raises(ValueError, match="unsupported solver mode"):
            store.configure_solver_policy(
                expected_revision=store.load().revision,
                mode=mode,
                threshold_seconds=300,
                allowed_runtimes=["python"],
            )
    assert not (project / ".env.cloud").exists()


def test_v1_upgrade_keeps_legacy_runtime_while_adding_solver_tables(tmp_path):
    # The detailed v1 fixture lives in test_factory_state_store; this assertion
    # covers the Phase 3 tables after that compatibility upgrade path.
    service, project, _script = make_project(tmp_path)
    policy = SQLiteStateStore(project).solver_policy()
    assert policy["mode"] == "local"
    assert SQLiteStateStore(project).solver_jobs() == []


class RevisionRacingBackend:
    def __init__(self, store):
        self.store = store
        self.external_jobs = []

    def submit(self, request):
        self.external_jobs.append(request.job_id)
        current = self.store.load()
        self.store.transition(
            expected_revision=current.revision,
            event_type="CONCURRENT_PAUSE",
            changes={"status": WorkflowStatus.PAUSED},
        )
        return SolverSubmission("external-race-job")

    def status(self, _job):
        return "running"

    def cancel(self, _job):
        return None


def test_solver_confirmation_uses_job_revision_not_project_revision(tmp_path):
    _service, project, script = make_project(tmp_path)
    store = SQLiteStateStore(project)
    backend = RevisionRacingBackend(store)
    backends = SolverBackendRegistry()
    backends.register("local", backend)
    service = FactoryService(tmp_path, solver_backends=backends)

    job = service.submit_solver(project, runtime="python", script=script)

    assert backend.external_jobs == [job["job_id"]]
    assert job["status"] == "running"
    assert job["external_id"] == "external-race-job"
    assert job["job_revision"] == 2
    assert store.load().status is WorkflowStatus.PAUSED


def test_solver_job_revision_rejects_only_stale_job_writers(tmp_path):
    _service, project, script = make_project(tmp_path)
    store = SQLiteStateStore(project)
    state = store.load()
    store.create_solver_job(
        expected_revision=state.revision,
        record={
            "job_id": "job-revision-test",
            "backend": "local",
            "runtime": "python",
            "script": str(script.relative_to(project)),
            "workdir": str(script.parent),
            "argv": [],
            "max_time_seconds": 10,
            "status": "submitting",
        },
    )
    paused = store.transition(
        expected_revision=store.load().revision,
        event_type="CONCURRENT_PAUSE",
        changes={"status": WorkflowStatus.PAUSED},
    )

    store.update_solver_job(
        "job-revision-test",
        expected_job_revision=1,
        status="running",
        external_id="123",
    )

    with pytest.raises(RevisionConflict, match="expected solver job revision 1"):
        store.update_solver_job(
            "job-revision-test",
            expected_job_revision=1,
            status="failed",
        )
    assert store.load().status is WorkflowStatus.PAUSED
    assert paused.status is WorkflowStatus.PAUSED


def test_default_solver_builder_registers_only_the_local_backend(tmp_path):
    backends = build_solver_backends(tmp_path)

    assert backends.get("local").name == "local"
    with pytest.raises(KeyError):
        backends.get("cloud_run")


class CancelDuringSubmissionBackend:
    def __init__(self, store):
        self.store = store
        self.cancelled = []

    def submit(self, _request):
        job = self.store.solver_jobs()[0]
        self.store.update_solver_job(
            job["job_id"],
            expected_job_revision=job["job_revision"],
            status="cancelled",
        )
        return SolverSubmission("external-after-cancel")

    def status(self, _job):
        return "running"

    def cancel(self, job):
        self.cancelled.append(job["external_id"])


def test_cancel_during_submission_still_records_and_stops_external_job(tmp_path):
    _service, project, script = make_project(tmp_path)
    store = SQLiteStateStore(project)
    backend = CancelDuringSubmissionBackend(store)
    backends = SolverBackendRegistry()
    backends.register("local", backend)
    service = FactoryService(tmp_path, solver_backends=backends)

    job = service.submit_solver(project, runtime="python", script=script)

    assert job["status"] == "cancelled"
    assert job["external_id"] == "external-after-cancel"
    assert backend.cancelled == ["external-after-cancel"]


