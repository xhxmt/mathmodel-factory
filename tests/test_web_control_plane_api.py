import asyncio
import importlib
import os
import sys
import types
from pathlib import Path

from project_diagnostics import write_status


def load_main_module():
    sys.modules.pop("web.backend.main", None)
    sys.modules.pop("fastapi", None)
    sys.modules.pop("fastapi.middleware.cors", None)
    sys.modules.pop("fastapi.responses", None)
    sys.modules.pop("fastapi.security", None)
    sys.modules.pop("pydantic", None)
    sys.modules.pop("dotenv", None)

    os.environ["JWT_SECRET"] = "0123456789abcdef0123456789abcdef"
    os.environ["ADMIN_PASSWORD"] = "strong-password"

    fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code=None, detail=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class DummyFastAPI:
        def __init__(self, *args, **kwargs):
            pass

        def add_middleware(self, *args, **kwargs):
            return None

        def include_router(self, *args, **kwargs):
            return None

        def get(self, *args, **kwargs):
            return lambda fn: fn

        def post(self, *args, **kwargs):
            return lambda fn: fn

        def put(self, *args, **kwargs):
            return lambda fn: fn

        def websocket(self, *args, **kwargs):
            return lambda fn: fn

    fastapi.FastAPI = DummyFastAPI
    fastapi.APIRouter = DummyFastAPI
    fastapi.HTTPException = HTTPException
    fastapi.Depends = lambda dep=None: dep
    fastapi.WebSocket = type("WebSocket", (), {})
    fastapi.WebSocketDisconnect = type("WebSocketDisconnect", (Exception,), {})
    fastapi.UploadFile = type("UploadFile", (), {})
    fastapi.File = lambda *a, **k: None
    fastapi.status = types.SimpleNamespace(
        HTTP_401_UNAUTHORIZED=401,
        HTTP_404_NOT_FOUND=404,
        HTTP_409_CONFLICT=409,
    )
    sys.modules["fastapi"] = fastapi

    cors = types.ModuleType("fastapi.middleware.cors")
    cors.CORSMiddleware = type("CORSMiddleware", (), {})
    sys.modules["fastapi.middleware.cors"] = cors

    responses = types.ModuleType("fastapi.responses")
    responses.FileResponse = type("FileResponse", (), {})
    sys.modules["fastapi.responses"] = responses

    security = types.ModuleType("fastapi.security")
    security.HTTPBearer = type("HTTPBearer", (), {"__call__": lambda self, *a, **k: None})
    security.HTTPAuthorizationCredentials = type("HTTPAuthorizationCredentials", (), {})
    sys.modules["fastapi.security"] = security

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

    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv

    return importlib.import_module("web.backend.main")


def test_project_action_raises_on_failed_control_command(monkeypatch):
    mod = load_main_module()

    class DummyResult:
        ok = False
        stdout = ""
        stderr = "cannot kill"

    monkeypatch.setattr(mod.project_actions, "run_action", lambda *a, **k: DummyResult())
    payload = mod.ProjectAction(action="kill")
    user = mod.UserInfo(username="admin", role="admin")

    try:
        asyncio.run(mod.project_action("demo", payload, current_user=user))
    except mod.HTTPException as exc:
        assert exc.status_code == 409
        assert "cannot kill" in exc.detail
    else:
        raise AssertionError("expected HTTPException")


def test_main_module_exposes_runtime_api_surface():
    mod = load_main_module()

    required = [
        "login",
        "get_me",
        "logout",
        "issue_ws_ticket",
        "upload_problem_file",
        "create_new_project",
        "get_projects",
        "get_single_project_status",
        "get_checkpoint",
        "get_recent_logs",
        "get_project_steps",
        "get_files",
        "get_file_content",
        "get_raw_file",
        "get_paper",
        "get_consultation",
        "submit_consultation_answer",
        "get_models",
        "put_model_registry",
        "put_model_config",
        "cloud_status",
        "cloud_config",
        "project_cloud_config",
        "enable_cloud_solver",
        "disable_cloud_solver",
        "websocket_endpoint",
    ]

    missing = [name for name in required if not hasattr(mod, name)]
    assert missing == []


def test_issue_ws_ticket_is_single_use():
    mod = load_main_module()
    user = mod.UserInfo(username="admin", role="admin")

    response = asyncio.run(mod.issue_ws_ticket(current_user=user))
    ticket = response.ticket if hasattr(response, "ticket") else response["ticket"]

    first = mod.ticket_store.consume(ticket)
    second = mod.ticket_store.consume(ticket)

    assert first == {"sub": "admin", "role": "admin"}
    assert second is None


def test_get_projects_formats_runtime_status_for_frontend(tmp_path, monkeypatch):
    mod = load_main_module()
    user = mod.UserInfo(username="admin", role="admin")

    project = tmp_path / "ongoing" / "demo"
    project.mkdir(parents=True)
    (project / "checkpoint.md").write_text("- **Last completed step**: 4\n", encoding="utf-8")
    write_status(
        project,
        state="running",
        current_step=5,
        current_action="agent_run",
        display_status="Running Step 5",
        updated_at=1700000000,
    )

    monkeypatch.setattr(
        mod,
        "settings",
        mod.settings.__class__(
            jwt_secret="0123456789abcdef0123456789abcdef",
            admin_password="strong-password",
            factory_root=tmp_path,
            jwt_hours=24,
        ),
    )

    payload = asyncio.run(mod.get_projects(current_user=user))
    assert len(payload) == 1

    first = payload[0]
    base_name = first.base_name if hasattr(first, "base_name") else first["base_name"]
    status = first.status if hasattr(first, "status") else first["status"]
    display_status = first.display_status if hasattr(first, "display_status") else first["display_status"]
    last_updated = first.last_updated if hasattr(first, "last_updated") else first["last_updated"]

    assert base_name == "demo"
    assert status == "running"
    assert display_status == "Running Step 5"
    assert isinstance(last_updated, str)


def test_project_cloud_config_reflects_enable_disable_state(tmp_path):
    mod = load_main_module()
    from web.backend import cloud_api

    settings = mod.settings.__class__(
        jwt_secret="0123456789abcdef0123456789abcdef",
        admin_password="strong-password",
        factory_root=tmp_path,
        jwt_hours=24,
        gcp_project_id="level-night-476302-k0",
        gcp_region="europe-west4",
        gcp_solver_service="solver-api",
    )
    project = settings.ongoing_dir / "demo"
    project.mkdir(parents=True)

    initial = cloud_api.project_cloud_config(settings, "demo")
    assert initial["enabled"] is False
    assert initial["env_file"] == str(project / ".env.cloud")

    cloud_api.set_project_cloud_enabled(settings, "demo", True)
    enabled = cloud_api.project_cloud_config(settings, "demo")
    assert enabled["enabled"] is True
    assert "USE_CLOUD_SOLVER=true" in (project / ".env.cloud").read_text(encoding="utf-8")

    cloud_api.set_project_cloud_enabled(settings, "demo", False)
    disabled = cloud_api.project_cloud_config(settings, "demo")
    assert disabled["enabled"] is False
    assert not (project / ".env.cloud").exists()


def test_project_cloud_config_rejects_unknown_project(tmp_path):
    mod = load_main_module()
    from web.backend import cloud_api

    settings = mod.settings.__class__(
        jwt_secret="0123456789abcdef0123456789abcdef",
        admin_password="strong-password",
        factory_root=tmp_path,
        jwt_hours=24,
    )

    try:
        cloud_api.set_project_cloud_enabled(settings, "missing", True)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 404
        assert "Project not found" in str(getattr(exc, "detail", ""))
    else:
        raise AssertionError("expected HTTPException")

    assert not (settings.ongoing_dir / "missing").exists()
