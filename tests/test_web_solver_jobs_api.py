"""Tests for solver jobs API endpoints."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import time
import types
from pathlib import Path

import pytest


def load_main_module(factory_root=None):
    """Load main module with proper environment setup and FastAPI mocks."""
    # Clear cached modules
    for module_name in [
        "web.backend.main",
        "web.backend.project_api",
        "web.backend.solver_jobs_api",
        "web.backend.auth",
        "web.backend.access_control",
        "web.backend.schemas",
    ]:
        sys.modules.pop(module_name, None)
    sys.modules.pop("fastapi", None)
    sys.modules.pop("fastapi.middleware.cors", None)
    sys.modules.pop("fastapi.responses", None)
    sys.modules.pop("fastapi.security", None)
    sys.modules.pop("pydantic", None)
    sys.modules.pop("dotenv", None)

    # Set environment
    os.environ["JWT_SECRET"] = "0123456789abcdef0123456789abcdef"
    os.environ["ADMIN_PASSWORD"] = "strong-password"
    if factory_root is None:
        os.environ.pop("FACTORY_ROOT", None)
    else:
        os.environ["FACTORY_ROOT"] = str(factory_root)

    auth_db = Path("/tmp") / f"test_solver_jobs_auth_{os.getpid()}.db"
    auth_db.unlink(missing_ok=True)
    os.environ["AUTH_DB_FILE"] = str(auth_db)

    # Mock FastAPI
    fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code=None, detail=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class DummyRoute:
        def __init__(self, path, endpoint, methods=None):
            self.path = path
            self.endpoint = endpoint
            self.methods = set(methods or [])

    class DummyFastAPI:
        def __init__(self, *args, **kwargs):
            self.routes = []

        def add_middleware(self, *args, **kwargs):
            return None

        def include_router(self, router, *args, **kwargs):
            self.routes.extend(getattr(router, "routes", []))
            return None

        def _route(self, path, methods, *args, **kwargs):
            def decorator(fn):
                self.routes.append(DummyRoute(path, fn, methods))
                return fn

            return decorator

        def get(self, path, *args, **kwargs):
            return self._route(path, {"GET"}, *args, **kwargs)

        def post(self, path, *args, **kwargs):
            return self._route(path, {"POST"}, *args, **kwargs)

        def put(self, path, *args, **kwargs):
            return self._route(path, {"PUT"}, *args, **kwargs)

        def delete(self, path, *args, **kwargs):
            return self._route(path, {"DELETE"}, *args, **kwargs)

        def websocket(self, path, *args, **kwargs):
            return self._route(path, set(), *args, **kwargs)

    fastapi.FastAPI = DummyFastAPI
    fastapi.APIRouter = DummyFastAPI
    fastapi.HTTPException = HTTPException
    fastapi.Depends = lambda dep=None: dep
    fastapi.WebSocket = type("WebSocket", (), {})
    fastapi.WebSocketDisconnect = type("WebSocketDisconnect", (Exception,), {})
    fastapi.UploadFile = type("UploadFile", (), {})
    fastapi.File = lambda *a, **k: None
    fastapi.status = types.SimpleNamespace(
        HTTP_400_BAD_REQUEST=400,
        HTTP_401_UNAUTHORIZED=401,
        HTTP_403_FORBIDDEN=403,
        HTTP_404_NOT_FOUND=404,
        HTTP_409_CONFLICT=409,
        HTTP_500_INTERNAL_SERVER_ERROR=500,
        HTTP_503_SERVICE_UNAVAILABLE=503,
    )
    sys.modules["fastapi"] = fastapi

    # Mock CORS
    cors = types.ModuleType("fastapi.middleware.cors")
    cors.CORSMiddleware = type("CORSMiddleware", (), {})
    sys.modules["fastapi.middleware.cors"] = cors

    # Mock responses
    responses = types.ModuleType("fastapi.responses")
    responses.FileResponse = type("FileResponse", (), {})
    sys.modules["fastapi.responses"] = responses

    # Mock security
    security = types.ModuleType("fastapi.security")
    security.HTTPBearer = type("HTTPBearer", (), {"__call__": lambda self, *a, **k: None})
    security.HTTPAuthorizationCredentials = type("HTTPAuthorizationCredentials", (), {})
    sys.modules["fastapi.security"] = security

    # Mock pydantic
    pydantic = types.ModuleType("pydantic")

    class BaseModel:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

        def dict(self):
            return self.__dict__.copy()

    pydantic.BaseModel = BaseModel
    pydantic.field_validator = lambda *args, **kwargs: (lambda fn: fn)
    sys.modules["pydantic"] = pydantic

    # Mock dotenv
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv

    # Import main module
    return importlib.import_module("web.backend.main")


def _make_project_with_solver_jobs(factory_root: Path, base: str, jobs: list[dict]) -> Path:
    """Create a project directory with native solver jobs in state.db."""
    from factory_core.service import FactoryService

    # Use FactoryService to create project with proper initialization
    service = FactoryService(factory_root)
    service.create_project(base, "test question", start=False)
    project = factory_root / "ongoing" / base

    # Initialize state.db with solver jobs
    from factory_core.storage import SQLiteStateStore

    store = SQLiteStateStore(project)

    # Get current revision via service.inspect
    state = service.inspect(project)
    revision = state.revision

    for job_data in jobs:
        record = {
            "job_id": job_data["job_id"],
            "backend": job_data.get("backend", "local"),
            "runtime": job_data.get("runtime", "python"),
            "script": job_data.get("script", "solve.py"),
            "workdir": job_data.get("workdir", "models/m1"),
            "argv": job_data.get("argv", []),
            "max_time_seconds": job_data.get("max_time_seconds", 600),
            "external_id": job_data.get("external_id"),
            "status": job_data.get("status", "submitted"),
            "started_at": job_data.get("started_at"),
            "finished_at": job_data.get("finished_at"),
        }

        state = store.create_solver_job(expected_revision=revision, record=record)
        revision = state.revision

    return project


def _make_legacy_meta(factory_root: Path, job_id: str, project_dir: str, **fields) -> Path:
    """Write a legacy solver job meta file."""
    legacy_dir = factory_root / "run_state" / "solver_jobs"
    legacy_dir.mkdir(parents=True, exist_ok=True)

    meta_path = legacy_dir / f"{job_id}.meta"
    lines = [f"project_dir={project_dir}"]
    for k, v in fields.items():
        lines.append(f"{k}={v}")

    meta_path.write_text("\n".join(lines), encoding="utf-8")
    return meta_path




def test_list_solver_jobs_native_only(tmp_path):
    """List endpoint returns native jobs with correct schema."""
    factory_root = tmp_path
    base = "test_native"
    now = int(time.time())

    jobs = [
        {
            "job_id": "local_python_123_abc",
            "backend": "local",
            "runtime": "python",
            "script": "models/m1/solve.py",
            "workdir": "models/m1",
            "status": "completed",
            "started_at": now - 100,
            "finished_at": now - 50,
        },
        {
            "job_id": "cloud_julia_124_def",
            "backend": "cloud_run",
            "runtime": "julia",
            "script": "models/m2/optimize.jl",
            "workdir": "models/m2",
            "status": "running",
            "started_at": now - 30,
        },
    ]

    project = _make_project_with_solver_jobs(factory_root, base, jobs)

    # Create receipts for first job
    receipt_dir = project / ".factory" / "solver_receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    (receipt_dir / "local_python_123_abc.submitted.json").write_text("{}", encoding="utf-8")
    (receipt_dir / "local_python_123_abc.completed.json").write_text("{}", encoding="utf-8")

    mod = load_main_module(factory_root)
    admin = mod.UserInfo(username="admin", role="admin", status="active")

    result = asyncio.run(mod.get_solver_jobs(base, admin))

    assert result["total"] == 2
    assert result["running"] == 1
    assert result["failed"] == 0
    assert len(result["jobs"]) == 2

    # Check first job
    job0 = result["jobs"][0]
    assert job0["job_id"] in ["local_python_123_abc", "cloud_julia_124_def"]
    assert job0["legacy"] is False
    assert job0["backend"] in ["local", "cloud_run"]
    if job0["job_id"] == "local_python_123_abc":
        assert job0["has_submission_receipt"] is True
        assert job0["has_completion_receipt"] is True
        assert job0["duration_seconds"] == 50




def test_list_solver_jobs_merge_legacy(tmp_path):
    """List endpoint merges native and legacy jobs."""
    factory_root = tmp_path
    base = "test_merge"
    now = int(time.time())

    # Create one native job
    native_jobs = [
        {
            "job_id": "local_python_200_native",
            "backend": "local",
            "runtime": "python",
            "script": "solve.py",
            "workdir": "models",
            "status": "completed",
            "started_at": now - 200,
            "finished_at": now - 150,
        }
    ]

    project = _make_project_with_solver_jobs(factory_root, base, native_jobs)

    # Create two legacy jobs
    _make_legacy_meta(
        factory_root,
        "local_python_300_legacy1",
        str(project),
        type="python",
        script="legacy1.py",
        started=str(now - 300),
    )
    _make_legacy_meta(
        factory_root,
        "cloud_julia_400_legacy2",
        str(project),
        type="julia",
        script="legacy2.jl",
        started=str(now - 400),
    )

    # Write exit file for legacy1
    exit_file = factory_root / "run_state" / "solver_jobs" / "local_python_300_legacy1.exit"
    exit_file.write_text("0", encoding="utf-8")

    mod = load_main_module(factory_root)
    admin = mod.UserInfo(username="admin", role="admin", status="active")

    result = asyncio.run(mod.get_solver_jobs(base, admin))

    assert result["total"] == 3
    assert len(result["jobs"]) == 3

    legacy_jobs = [j for j in result["jobs"] if j["legacy"]]
    assert len(legacy_jobs) == 2

    # Check legacy job with exit file
    legacy1 = next(j for j in legacy_jobs if "legacy1" in j["job_id"])
    assert legacy1["status"] == "COMPLETED"
    assert legacy1["backend"] == "local"
    assert legacy1["runtime"] == "python"


def test_list_solver_jobs_acl_denial(tmp_path):
    """Non-admin without project access gets 404."""
    factory_root = tmp_path
    base = "private_project"

    _make_project_with_solver_jobs(factory_root, base, [])

    mod = load_main_module(factory_root)
    alice = mod.UserInfo(username="alice", role="user", status="active")

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(mod.get_solver_jobs(base, alice))

    assert exc_info.value.status_code == 404
    assert "PROJECT_NOT_FOUND" in str(exc_info.value.detail)




def test_get_solver_job_evidence_receipt_missing(tmp_path):
    """Detail endpoint returns fail-closed envelope when receipts missing."""
    factory_root = tmp_path
    base = "test_no_receipt"
    now = int(time.time())

    jobs = [
        {
            "job_id": "local_python_500_norec",
            "backend": "local",
            "runtime": "python",
            "script": "solve.py",
            "workdir": "models",
            "status": "completed",
            "started_at": now - 100,
            "finished_at": now - 50,
        }
    ]

    _make_project_with_solver_jobs(factory_root, base, jobs)

    mod = load_main_module(factory_root)
    admin = mod.UserInfo(username="admin", role="admin", status="active")

    result = asyncio.run(mod.get_solver_job_detail(base, "local_python_500_norec", admin))

    # Fail-closed envelope
    assert result["receipt_ready"] is False
    assert result["claim_limit"] == "LEGACY_JOB_METADATA_ONLY"
    assert result["submission"] is None or "submission" in result


def test_get_solver_job_evidence_not_found(tmp_path):
    """Detail endpoint returns 404 for non-existent job."""
    factory_root = tmp_path
    base = "test_notfound"

    _make_project_with_solver_jobs(factory_root, base, [])

    mod = load_main_module(factory_root)
    admin = mod.UserInfo(username="admin", role="admin", status="active")

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(mod.get_solver_job_detail(base, "nonexistent_job", admin))

    assert exc_info.value.status_code == 404
    assert "SOLVER_JOB_NOT_FOUND" in str(exc_info.value.detail)


def test_legacy_jobs_filtered_by_project_dir(tmp_path):
    """Legacy jobs are filtered by project_dir field."""
    factory_root = tmp_path
    base = "filter_test"

    project1 = _make_project_with_solver_jobs(factory_root, base, [])
    project2 = factory_root / "ongoing" / "other_project"
    project2.mkdir(parents=True, exist_ok=True)

    # Create 3 legacy metas: 2 for project1, 1 for project2
    _make_legacy_meta(
        factory_root,
        "local_python_600_proj1a",
        str(project1),
        type="python",
        script="a.py",
        started=str(int(time.time()) - 100),
    )
    _make_legacy_meta(
        factory_root,
        "local_python_601_proj1b",
        str(project1),
        type="python",
        script="b.py",
        started=str(int(time.time()) - 200),
    )
    _make_legacy_meta(
        factory_root,
        "local_python_602_proj2",
        str(project2),
        type="python",
        script="c.py",
        started=str(int(time.time()) - 300),
    )

    mod = load_main_module(factory_root)
    admin = mod.UserInfo(username="admin", role="admin", status="active")

    result = asyncio.run(mod.get_solver_jobs(base, admin))

    assert result["total"] == 2
    job_ids = {j["job_id"] for j in result["jobs"]}
    assert "local_python_600_proj1a" in job_ids
    assert "local_python_601_proj1b" in job_ids
    assert "local_python_602_proj2" not in job_ids
