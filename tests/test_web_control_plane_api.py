import asyncio
import importlib
import os
import sys
import types


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
