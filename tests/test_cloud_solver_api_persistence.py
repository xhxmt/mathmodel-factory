import importlib
import json
import sys

class FakeBlob:
    def __init__(self, name):
        self.name = name
        self.content = None
        self.deleted = False

    def upload_from_filename(self, filename):
        with open(filename, "rb") as fh:
            data = fh.read()
        self.content = data.decode("utf-8", errors="replace")

    def upload_from_string(self, data, content_type=None):
        self.content = data

    def download_as_text(self):
        if self.content is None:
            raise FileNotFoundError(self.name)
        return self.content

    def exists(self):
        return self.content is not None and not self.deleted

    def delete(self):
        self.deleted = True


class FakeBucket:
    def __init__(self):
        self.blobs = {}

    def blob(self, name):
        self.blobs.setdefault(name, FakeBlob(name))
        return self.blobs[name]


class FakeStorageClient:
    def __init__(self):
        self.bucket_obj = FakeBucket()

    def bucket(self, name):
        return self.bucket_obj


def import_solver_api():
    sys.modules.pop("cloud.solver_api", None)
    return importlib.import_module("cloud.solver_api")


def test_solver_api_import_does_not_require_adc(monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    module = import_solver_api()

    assert module.app.title == "Paper Factory Solver API"
    assert module.storage_client is None


def test_job_store_persists_status_manifest_to_gcs(monkeypatch):
    module = import_solver_api()
    fake_client = FakeStorageClient()
    monkeypatch.setattr(module, "storage_client", fake_client)

    job = {
        "job_id": "job-1",
        "status": "queued",
        "submitted_at": 123.0,
        "started_at": None,
        "completed_at": None,
        "duration": None,
        "exit_code": None,
        "stdout_url": None,
        "stderr_url": None,
        "result_files": None,
        "error_message": None,
    }

    module.job_store.save(job)

    manifest = fake_client.bucket_obj.blobs["jobs/job-1/manifest.json"]
    payload = json.loads(manifest.content)
    assert payload["schema_version"] == 1
    assert payload["job"]["job_id"] == "job-1"
    assert payload["job"]["status"] == "queued"
    assert payload["job"]["gcs_prefix"] == "gs://level-night-476302-k0-solver-jobs/jobs/job-1/"
    assert payload["job"]["manifest_url"] == "gs://level-night-476302-k0-solver-jobs/jobs/job-1/manifest.json"


def test_status_recovers_job_from_persisted_manifest(monkeypatch):
    module = import_solver_api()
    fake_client = FakeStorageClient()
    monkeypatch.setattr(module, "storage_client", fake_client)

    module.job_store.save(
        {
            "job_id": "job-2",
            "status": "completed",
            "submitted_at": 123.0,
            "started_at": 124.0,
            "completed_at": 125.0,
            "duration": 1.0,
            "exit_code": 0,
            "stdout_url": "gs://bucket/jobs/job-2/stdout.log",
            "stderr_url": "gs://bucket/jobs/job-2/stderr.log",
            "result_files": ["gs://bucket/jobs/job-2/result.json"],
            "error_message": None,
        }
    )
    module.job_registry.clear()

    status = module.get_job_status("job-2")

    assert status.job_id == "job-2"
    assert status.status == "completed"
    assert status.result_files == ["gs://bucket/jobs/job-2/result.json"]


def test_solver_api_rejects_missing_solver_token_when_configured(monkeypatch):
    monkeypatch.setenv("SOLVER_API_TOKEN", "expected-token")
    module = import_solver_api()

    try:
        module.verify_solver_auth(x_solver_token=None)
    except module.HTTPException as exc:
        assert exc.status_code == 401
    else:
        raise AssertionError("missing solver token should be rejected")


def test_solver_api_fails_closed_when_solver_token_is_not_configured(monkeypatch):
    monkeypatch.delenv("SOLVER_API_TOKEN", raising=False)
    module = import_solver_api()

    try:
        module.verify_solver_auth(x_solver_token=None)
    except module.HTTPException as exc:
        assert exc.status_code == 503
        assert "unavailable" in exc.detail.lower()
    else:
        raise AssertionError("protected endpoints must fail closed without an application token")


def test_solver_api_accepts_configured_solver_token(monkeypatch):
    monkeypatch.setenv("SOLVER_API_TOKEN", "expected-token")
    module = import_solver_api()

    assert module.verify_solver_auth(x_solver_token="expected-token") is None


def test_solver_execution_is_quarantined_by_default(monkeypatch):
    monkeypatch.delenv("SOLVER_EXECUTION_ENABLED", raising=False)
    module = import_solver_api()

    try:
        module.require_solver_execution_enabled()
    except module.HTTPException as exc:
        assert exc.status_code == 503
        assert "quarantined" in exc.detail.lower()
    else:
        raise AssertionError("cloud execution must be quarantined by default")


def test_solver_execution_requires_explicit_enable(monkeypatch):
    monkeypatch.setenv("SOLVER_EXECUTION_ENABLED", "true")
    module = import_solver_api()

    assert module.require_solver_execution_enabled() is None


def test_health_reports_quarantine_without_exposing_auth_material(monkeypatch):
    monkeypatch.delenv("SOLVER_API_TOKEN", raising=False)
    monkeypatch.delenv("SOLVER_EXECUTION_ENABLED", raising=False)
    module = import_solver_api()

    payload = module.health_check()

    assert payload["status"] == "healthy"
    assert payload["execution_enabled"] is False
    assert payload["protected_endpoints_available"] is False
    assert "token" not in json.dumps(payload).lower()


def test_protected_routes_wire_fail_closed_dependencies(monkeypatch):
    module = import_solver_api()

    dependencies_by_route = {
        (route.path, method): {dependency.call for dependency in route.dependant.dependencies}
        for route in module.app.routes
        if hasattr(route, "dependant")
        for method in route.methods
    }

    protected_routes = (
        ("/solve/{solver_type}", "POST"),
        ("/jobs/{job_id}/status", "GET"),
        ("/jobs/{job_id}/output", "GET"),
        ("/jobs/{job_id}", "DELETE"),
        ("/jobs", "GET"),
    )

    for route_key in protected_routes:
        assert module.verify_solver_auth in dependencies_by_route[route_key]
    assert module.require_solver_execution_enabled in dependencies_by_route[
        ("/solve/{solver_type}", "POST")
    ]


def test_cloudbuild_does_not_deploy_public_solver():
    text = open("cloud/cloudbuild.yaml", encoding="utf-8").read()

    assert "--allow-unauthenticated" not in text
    assert "--no-allow-unauthenticated" in text
    assert "SOLVER_EXECUTION_ENABLED=false" in text
