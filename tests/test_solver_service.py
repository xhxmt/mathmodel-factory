import os
import time

import pytest

from factory_core.adapters.solvers import CloudRunSolverBackend, SolverSubmission
from factory_core.domain import InvalidTransition
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
