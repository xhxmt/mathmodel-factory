import os
import time

import pytest

from factory_core.adapters.solvers import (
    CloudRunHttpTransport,
    CloudRunSolverBackend,
    SolverRequest,
    SolverSubmission,
    build_solver_backends,
)
from factory_core.domain import InvalidTransition, RevisionConflict, WorkflowStatus
from factory_core.registry import SolverBackendRegistry
from factory_core.service import FactoryService
from factory_core.storage import SQLiteStateStore


class FakeCloudTransport:
    def __init__(self):
        self.requests = []
        self.cancelled = []

    def submit(self, request):
        self.requests.append(request)
        return SolverSubmission("cloud-123", result_refs={"result": "results/cloud.json"})

    def status(self, external_id):
        assert external_id == "cloud-123"
        return "completed"

    def cancel(self, external_id):
        self.cancelled.append(external_id)


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
        mode="local",
        threshold_seconds=120,
        allowed_runtimes=["python", "julia"],
        expected_revision=before.revision,
    )

    assert policy["revision"] == before.revision + 1
    assert policy["allowed_runtimes"] == ["julia", "python"]
    assert SQLiteStateStore(project).events()[-1].type == "SOLVER_POLICY_CONFIGURED"
    with pytest.raises(Exception, match="expected revision"):
        service.configure_solver_policy(
            project,
            mode="local",
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
    assert "SOLVER_JOB_RUNNING" in event_types
    assert "SOLVER_JOB_COMPLETED" in event_types


def test_fake_cloud_and_local_share_solver_job_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUD_SOLVER_QUARANTINED", "false")
    _service, project, script = make_project(tmp_path)
    transport = FakeCloudTransport()
    backends = SolverBackendRegistry()
    from factory_core.adapters.solvers import LocalSolverBackend

    backends.register("local", LocalSolverBackend(os.getcwd()))
    backends.register("cloud_run", CloudRunSolverBackend(transport, quarantined=False))
    service = FactoryService(tmp_path, solver_backends=backends)
    service.configure_solver_policy(
        project,
        mode="auto",
        threshold_seconds=5,
        allowed_runtimes=["python"],
    )

    submitted = service.submit_solver(
        project, runtime="python", script=script, max_time_seconds=10
    )
    completed = service.solver_status(project, submitted["job_id"])

    assert submitted["backend"] == "cloud_run"
    assert completed["status"] == "completed"
    assert completed["external_id"] == "cloud-123"
    assert transport.requests[0].runtime == "python"


def test_cloud_policy_cannot_bypass_global_quarantine(tmp_path, monkeypatch):
    service, project, _script = make_project(tmp_path)
    monkeypatch.setenv("CLOUD_SOLVER_QUARANTINED", "true")

    with pytest.raises(InvalidTransition, match="quarantined"):
        service.configure_solver_policy(
            project,
            mode="auto",
            allowed_runtimes=["python"],
        )


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


def test_default_solver_builder_registers_both_backends(tmp_path):
    transport = FakeCloudTransport()
    backends = build_solver_backends(
        tmp_path, cloud_transport=transport, quarantined=False
    )

    assert backends.get("local").name == "local"
    assert backends.get("cloud_run").name == "cloud_run"


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


def test_cloud_transport_rejects_missing_https_url_before_auth(tmp_path):
    script = tmp_path / "solve.py"
    script.write_text("print('done')\n", encoding="utf-8")
    token_calls = []
    transport = CloudRunHttpTransport(
        "http://solver.example",
        token_provider=lambda audience: token_calls.append(audience) or "unused",
    )

    with pytest.raises(RuntimeError, match="must be an https URL"):
        transport.submit(
            SolverRequest(
                job_id="cloud-job",
                project_dir=tmp_path,
                runtime="python",
                script=script,
            )
        )

    assert token_calls == []
