import asyncio
import importlib
import json
import os
import sys
import types
from pathlib import Path

import pytest

from project_diagnostics import write_status


def load_main_module(factory_root=None, auth_db_file=None):
    for module_name in [
        "web.backend.main",
        "web.backend.project_api",
        "web.backend.cloud_api",
        "web.backend.ws",
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

    os.environ["JWT_SECRET"] = "0123456789abcdef0123456789abcdef"
    os.environ["ADMIN_PASSWORD"] = "strong-password"
    if factory_root is None:
        os.environ.pop("FACTORY_ROOT", None)
    else:
        os.environ["FACTORY_ROOT"] = str(factory_root)
    auth_db = Path(auth_db_file) if auth_db_file else Path("/tmp") / f"paper_factory_web_control_plane_auth_{os.getpid()}.db"
    auth_db.unlink(missing_ok=True)
    os.environ["AUTH_DB_FILE"] = str(auth_db)

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
        "delete_user",
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
        "get_modeling_directions",
        "select_modeling_direction",
        "get_selection",
        "submit_selection_decision",
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
    user = mod.UserInfo(username="admin", role="admin", status="active")

    response = asyncio.run(mod.issue_ws_ticket(current_user=user))
    ticket = response.ticket if hasattr(response, "ticket") else response["ticket"]

    first = mod.ticket_store.consume(ticket)
    second = mod.ticket_store.consume(ticket)

    assert first == {"sub": "admin", "role": "admin", "status": "active"}
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


def test_recent_logs_prefers_newer_runner_log_over_stale_step_log(tmp_path):
    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    user = mod.UserInfo(username="admin", role="admin")
    logs_dir = tmp_path / "ongoing" / "demo" / "logs"
    logs_dir.mkdir(parents=True)
    stale_step = logs_dir / "step_setup_20260706_132022.log"
    runner_log = logs_dir / "runner.log"
    stale_step.write_text("old setup failure\n", encoding="utf-8")
    runner_log.write_text("current runner line\n", encoding="utf-8")
    os.utime(stale_step, (1000, 1000))
    os.utime(runner_log, (2000, 2000))

    response = asyncio.run(mod.get_recent_logs("demo", lines=20, current_user=user))

    assert response["file"] == "runner.log"
    assert "current runner line" in response["logs"]


def test_selection_endpoint_returns_pending_options(tmp_path):
    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    user = mod.UserInfo(username="admin", role="admin")
    project = tmp_path / "ongoing" / "demo"
    (project / "selection").mkdir(parents=True)
    (project / "selection" / "step3_options.json").write_text(
        json.dumps(
            {
                "gate": "step3",
                "available": True,
                "default_option_id": "m1",
                "options": [{"id": "m1", "rank": 1, "recommended_aux": "NONE"}],
            }
        ),
        encoding="utf-8",
    )

    response = asyncio.run(mod.get_selection("demo", current_user=user))

    assert response["default_option_id"] == "m1"


def test_selection_decision_writes_human_review(tmp_path, monkeypatch):
    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    user = mod.UserInfo(username="admin", role="admin")
    project = tmp_path / "ongoing" / "demo"
    (project / "selection").mkdir(parents=True)
    (project / "selection" / "step3_options.json").write_text(
        json.dumps(
            {
                "gate": "step3",
                "available": True,
                "default_option_id": "m1",
                "options": [
                    {
                        "id": "m1",
                        "rank": 1,
                        "title": "m1 - MILP",
                        "family": "MILP",
                        "recommended_aux": "NONE",
                        "scores": {},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    class DummyResult:
        ok = True
        stdout = "resumed"
        stderr = ""

    monkeypatch.setattr(mod.project_api, "run_action", lambda *a, **k: DummyResult())
    response = asyncio.run(
        mod.submit_selection_decision(
            "demo",
            mod.SelectionDecisionRequest(
                gate="step3",
                selected_option_id="m1",
                selected_aux_id="NONE",
                reason="ok",
            ),
            current_user=user,
        )
    )

    assert response["status"] == "ok"
    assert "## Step 3 decision:" in (project / "human_review.md").read_text(encoding="utf-8")


def _make_project(root, name):
    project = root / "ongoing" / name
    project.mkdir(parents=True)
    (project / "checkpoint.md").write_text("- **Last completed step**: 1\n", encoding="utf-8")
    write_status(
        project,
        state="running",
        current_step=2,
        current_action="agent_run",
        display_status="Running",
        updated_at=1700000000,
    )
    return project


def _install_auth_store(mod):
    settings = mod.settings
    mod.auth_store = mod.AuthStore(settings.resolved_auth_db_file)
    mod.auth_store.initialize()
    mod.auth_store.bootstrap_admin(settings.admin_password)
    mod.auth_store.register_user("alice", "alice password", "")
    mod.auth_store.approve_user("alice", actor="admin")
    mod.auth_store.register_user("bob", "bob password", "")
    mod.auth_store.approve_user("bob", actor="admin")
    return settings


def test_user_project_list_is_filtered_by_acl(tmp_path):
    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    _install_auth_store(mod)
    _make_project(tmp_path, "owned")
    _make_project(tmp_path, "other")
    mod.auth_store.grant_project_owner("owned", "alice", actor="admin")

    alice = mod.UserInfo(username="alice", role="user", status="active")
    projects = asyncio.run(mod.get_projects(current_user=alice))

    assert [project.base_name for project in projects] == ["owned"]


def test_non_owner_project_file_returns_404(tmp_path):
    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    _install_auth_store(mod)
    _make_project(tmp_path, "owned")
    _make_project(tmp_path, "other")
    mod.auth_store.grant_project_owner("owned", "alice", actor="admin")
    alice = mod.UserInfo(username="alice", role="user", status="active")

    with pytest.raises(mod.HTTPException) as excinfo:
        asyncio.run(mod.get_checkpoint("other", current_user=alice))

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "PROJECT_NOT_FOUND"


def test_admin_can_see_all_projects(tmp_path):
    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    _install_auth_store(mod)
    _make_project(tmp_path, "owned")
    _make_project(tmp_path, "other")
    admin = mod.UserInfo(username="admin", role="admin", status="active")

    projects = asyncio.run(mod.get_projects(current_user=admin))

    assert sorted(project.base_name for project in projects) == ["other", "owned"]


def test_model_registry_is_admin_only_but_owner_can_save_project_config(tmp_path):
    from web.backend.schemas import ModelConfigPayload, ModelRegistryPayload, StepAssignment

    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    _install_auth_store(mod)
    _make_project(tmp_path, "owned")
    mod.auth_store.grant_project_owner("owned", "alice", actor="admin")
    alice = mod.UserInfo(username="alice", role="user", status="active")

    with pytest.raises(mod.HTTPException) as registry_error:
        asyncio.run(mod.put_model_registry(ModelRegistryPayload(models=[]), current_user=alice))
    assert registry_error.value.status_code == 403

    payload = ModelConfigPayload(scope="owned", steps={"step_7": StepAssignment(primary="claude")})
    saved = asyncio.run(mod.put_model_config(payload, current_user=alice))
    assert saved["status"] == "ok"

    default_payload = ModelConfigPayload(scope="_default", steps={"step_7": StepAssignment(primary="claude")})
    with pytest.raises(mod.HTTPException) as default_error:
        asyncio.run(mod.put_model_config(default_payload, current_user=alice))
    assert default_error.value.status_code == 403


def test_user_new_project_requires_project_request(tmp_path):
    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    _install_auth_store(mod)
    problem = tmp_path / "problem.pdf"
    problem.write_text("problem", encoding="utf-8")
    alice = mod.UserInfo(username="alice", role="user", status="active")

    with pytest.raises(mod.HTTPException) as excinfo:
        asyncio.run(
            mod.create_new_project(
                mod.NewProjectRequest(base_name="alice_project", problem_path=str(problem)),
                current_user=alice,
            )
        )

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "PROJECT_APPROVAL_REQUIRED"


def test_user_can_submit_and_list_own_project_request(tmp_path):
    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    _install_auth_store(mod)
    problem = tmp_path / "problem.pdf"
    problem.write_text("problem", encoding="utf-8")
    alice = mod.UserInfo(username="alice", role="user", status="active")

    created = asyncio.run(
        mod.create_project_request(
            mod.ProjectRequestCreate(
                base_name="alice_project",
                problem_path=str(problem),
                no_start=False,
                consult=True,
            ),
            current_user=alice,
        )
    )
    listed = asyncio.run(mod.list_project_requests(current_user=alice))

    assert created.status == "pending"
    assert created.requester == "alice"
    assert [item.id for item in listed] == [created.id]


def test_admin_approves_project_request_launches_and_grants_acl(tmp_path, monkeypatch):
    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    _install_auth_store(mod)
    problem = tmp_path / "problem.pdf"
    problem.write_text("problem", encoding="utf-8")
    alice = mod.UserInfo(username="alice", role="user", status="active")
    admin = mod.UserInfo(username="admin", role="admin", status="active")
    created = asyncio.run(
        mod.create_project_request(
            mod.ProjectRequestCreate(
                base_name="alice_project",
                problem_path=str(problem),
                no_start=False,
                consult=False,
            ),
            current_user=alice,
        )
    )

    class DummyResult:
        returncode = 0
        stdout = "created alice_project"
        stderr = ""

    calls = []
    monkeypatch.setattr(
        mod.project_api,
        "run_project_launcher",
        lambda settings, request: calls.append(request.base_name) or DummyResult(),
    )
    approved = asyncio.run(
        mod.approve_project_request(
            created.id,
            mod.ProjectRequestDecision(note="ok"),
            current_user=admin,
        )
    )

    assert approved.status == "approved"
    assert calls == ["alice_project"]
    assert mod.auth_store.user_can_access_project("alice", "alice_project") is True


def test_admin_cannot_approve_project_request_after_it_is_processed(tmp_path, monkeypatch):
    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    _install_auth_store(mod)
    problem = tmp_path / "problem.pdf"
    problem.write_text("problem", encoding="utf-8")
    alice = mod.UserInfo(username="alice", role="user", status="active")
    admin = mod.UserInfo(username="admin", role="admin", status="active")
    created = asyncio.run(
        mod.create_project_request(
            mod.ProjectRequestCreate(
                base_name="alice_project",
                problem_path=str(problem),
                no_start=False,
                consult=False,
            ),
            current_user=alice,
        )
    )

    class DummyResult:
        returncode = 0
        stdout = "created alice_project"
        stderr = ""

    calls = []
    monkeypatch.setattr(
        mod.project_api,
        "run_project_launcher",
        lambda settings, request: calls.append(request.base_name) or DummyResult(),
    )

    asyncio.run(
        mod.approve_project_request(
            created.id,
            mod.ProjectRequestDecision(note="ok"),
            current_user=admin,
        )
    )

    with pytest.raises(mod.HTTPException) as excinfo:
        asyncio.run(
            mod.approve_project_request(
                created.id,
                mod.ProjectRequestDecision(note="again"),
                current_user=admin,
            )
        )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == "PROJECT_REQUEST_NOT_PENDING"
    assert calls == ["alice_project"]

    with pytest.raises(mod.HTTPException) as reject_error:
        asyncio.run(
            mod.reject_project_request(
                created.id,
                mod.ProjectRequestDecision(note="reject after approve"),
                current_user=admin,
            )
        )
    assert reject_error.value.status_code == 409
    assert reject_error.value.detail == "PROJECT_REQUEST_NOT_PENDING"


def test_failed_project_request_launch_marks_failed(tmp_path, monkeypatch):
    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    _install_auth_store(mod)
    problem = tmp_path / "problem.pdf"
    problem.write_text("problem", encoding="utf-8")
    alice = mod.UserInfo(username="alice", role="user", status="active")
    admin = mod.UserInfo(username="admin", role="admin", status="active")
    created = asyncio.run(
        mod.create_project_request(
            mod.ProjectRequestCreate(
                base_name="bad_project",
                problem_path=str(problem),
                no_start=False,
                consult=False,
            ),
            current_user=alice,
        )
    )

    class DummyResult:
        returncode = 2
        stdout = ""
        stderr = "launcher failed"

    monkeypatch.setattr(mod.project_api, "run_project_launcher", lambda settings, request: DummyResult())

    with pytest.raises(mod.HTTPException) as excinfo:
        asyncio.run(
            mod.approve_project_request(
                created.id,
                mod.ProjectRequestDecision(note="ok"),
                current_user=admin,
            )
        )

    assert excinfo.value.status_code == 500
    failed = mod.auth_store.require_project_request(created.id)
    assert failed.status == "failed"
    assert "launcher failed" in failed.failure_reason


def test_cloud_global_config_is_admin_only_and_project_config_requires_owner(tmp_path):
    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    _install_auth_store(mod)
    _make_project(tmp_path, "owned")
    mod.auth_store.grant_project_owner("owned", "alice", actor="admin")
    alice = mod.UserInfo(username="alice", role="user", status="active")
    bob = mod.UserInfo(username="bob", role="user", status="active")

    with pytest.raises(mod.HTTPException) as global_error:
        asyncio.run(mod.cloud_config(current_user=alice))
    assert global_error.value.status_code == 403

    owned = asyncio.run(mod.project_cloud_config("owned", current_user=alice))
    assert owned["enabled"] is False

    with pytest.raises(mod.HTTPException) as owner_error:
        asyncio.run(mod.project_cloud_config("owned", current_user=bob))
    assert owner_error.value.status_code == 404


class RecordingWebSocket:
    def __init__(self, ticket):
        self.query_params = {"ticket": ticket}
        self.accepted = False
        self.closed = None
        self.sent = []
        self._sends_before_stop = 1

    async def accept(self):
        self.accepted = True

    async def close(self, code=None):
        self.closed = code

    async def send_json(self, payload):
        self.sent.append(payload)
        self._sends_before_stop -= 1
        if self._sends_before_stop <= 0:
            raise RuntimeError("stop websocket test")


def test_websocket_filters_status_update_projects_by_ticket_user(tmp_path):
    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    _install_auth_store(mod)
    _make_project(tmp_path, "owned")
    _make_project(tmp_path, "other")
    mod.auth_store.grant_project_owner("owned", "alice", actor="admin")

    ticket = mod.ticket_store.issue({"sub": "alice", "role": "user", "status": "active"})
    websocket = RecordingWebSocket(ticket)

    with pytest.raises(RuntimeError, match="stop websocket test"):
        asyncio.run(mod.websocket_endpoint(websocket))

    assert websocket.accepted is True
    status_messages = [msg for msg in websocket.sent if msg.get("type") == "status_update"]
    assert status_messages
    assert [item["base_name"] for item in status_messages[0]["projects"]] == ["owned"]
