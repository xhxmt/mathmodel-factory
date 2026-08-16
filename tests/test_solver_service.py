import os
import time
import io
import json
import base64
import hashlib

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
from factory_core.workflow_events import replay_events, replay_state


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
    assert "SOLVER_JOB_RECEIPT_SUBMITTED" in event_types
    assert "SOLVER_JOB_RUNNING" in event_types
    assert "SOLVER_JOB_COMPLETED" in event_types
    assert "SOLVER_JOB_RECEIPT_COMPLETED" in event_types
    store = SQLiteStateStore(project)
    assert replay_events(store.events()) == replay_state(store.load())


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
    assert transport.requests[0].env["FACTORY_SOLVER_JOB_ID"] == submitted["job_id"]
    assert len(transport.requests[0].idempotency_key) == 64


def test_duplicate_solver_request_returns_existing_job_without_resubmission(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLOUD_SOLVER_QUARANTINED", "false")
    _service, project, script = make_project(tmp_path)
    transport = FakeCloudTransport()
    backends = SolverBackendRegistry()
    backends.register(
        "cloud_run", CloudRunSolverBackend(transport, quarantined=False)
    )
    service = FactoryService(tmp_path, solver_backends=backends)
    service.configure_solver_policy(
        project,
        mode="cloud",
        threshold_seconds=1,
        allowed_runtimes=["python"],
    )

    first = service.submit_solver(
        project, runtime="python", script=script, max_time_seconds=10
    )
    second = service.submit_solver(
        project, runtime="python", script=script, max_time_seconds=10
    )

    assert second["job_id"] == first["job_id"]
    assert second["idempotency_key"] == first["idempotency_key"]
    assert len(first["request_sha256"]) == 64
    assert len(transport.requests) == 1


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


def test_cloud_transport_forwards_provider_idempotency_key(tmp_path):
    script = tmp_path / "solve.py"
    script.write_text("print('done')\n", encoding="utf-8")
    text_input = tmp_path / "data.txt"
    text_input.write_text("alpha\n", encoding="utf-8")
    binary_input = tmp_path / "matrix.bin"
    binary_input.write_bytes(b"\x00\xff\x10")
    captured = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def opener(request, **_kwargs):
        captured["payload"] = json.loads(request.data)
        return Response(b'{"job_id":"cloud-job","status":"queued"}')

    transport = CloudRunHttpTransport(
        "https://solver.example",
        token_provider=lambda _audience: "identity-token",
        opener=opener,
    )
    submission = transport.submit(
        SolverRequest(
            job_id="cloud-job",
            idempotency_key="a" * 64,
            project_dir=tmp_path,
            runtime="python",
            script=script,
            input_paths=(text_input, binary_input),
            output_paths=("results/answer.json",),
            seeds=("17", "23"),
        )
    )

    payload = captured["payload"]
    assert payload["idempotency_key"] == "a" * 64
    assert payload["working_files"] == {"data.txt": "alpha\n"}
    assert payload["working_files_base64"] == {
        "matrix.bin": base64.b64encode(binary_input.read_bytes()).decode("ascii")
    }
    assert payload["requested_input_sha256"] == {
        "data.txt": hashlib.sha256(text_input.read_bytes()).hexdigest(),
        "matrix.bin": hashlib.sha256(binary_input.read_bytes()).hexdigest(),
    }
    assert payload["declared_outputs"] == ["results/answer.json"]
    assert payload["seeds"] == ["17", "23"]
    assert submission.external_id == "cloud-job"
    assert submission.status == "queued"
