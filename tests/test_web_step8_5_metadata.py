from pathlib import Path
import importlib
import sys
import types


def write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def load_app_module():
    sys.modules.pop("web.backend.app", None)
    sys.modules.pop("pydantic", None)
    sys.modules.pop("dotenv", None)
    sys.modules.pop("fastapi", None)
    sys.modules.pop("fastapi.middleware.cors", None)
    sys.modules.pop("fastapi.responses", None)
    sys.modules.pop("fastapi.security", None)

    import os
    os.environ["JWT_SECRET"] = "test-secret"

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

        def get(self, *args, **kwargs):
            return lambda fn: fn

        def post(self, *args, **kwargs):
            return lambda fn: fn

        def put(self, *args, **kwargs):
            return lambda fn: fn

        def delete(self, *args, **kwargs):
            return lambda fn: fn

        def websocket(self, *args, **kwargs):
            return lambda fn: fn

    fastapi.FastAPI = DummyFastAPI
    fastapi.WebSocket = type("WebSocket", (), {})
    fastapi.WebSocketDisconnect = type("WebSocketDisconnect", (Exception,), {})
    fastapi.HTTPException = HTTPException
    fastapi.Depends = lambda dep=None: dep
    fastapi.status = types.SimpleNamespace(
        HTTP_401_UNAUTHORIZED=401,
        HTTP_404_NOT_FOUND=404,
    )
    fastapi.UploadFile = type("UploadFile", (), {})
    fastapi.File = lambda *args, **kwargs: None
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

    return importlib.import_module("web.backend.app")


def test_get_steps_exposes_editorial_gate(tmp_path):
    mod = load_app_module()

    write_file(tmp_path / "checkpoint.md", "- **Last completed step**: 8\n")
    write_file(tmp_path / "reviewer_entry_map.md", "# map\n")
    write_file(tmp_path / "anchor_figure_plan.md", "# anchors\n")
    write_file(tmp_path / "entry_gate.md", "# gate\n\nVERDICT: REVISE\n")

    data = mod.get_steps(tmp_path, "demo")
    assert data["current_step"] == 8
    assert data["editorial_gate"]["verdict"] == "REVISE"
    assert data["editorial_gate"]["ready"] is False


def test_valid_step_key_accepts_step_8_5():
    mod = load_app_module()

    assert mod._valid_model_step_key("step_8_5") is True
    assert mod._valid_model_step_key("step_9") is True
    assert mod._valid_model_step_key("step_nine") is False
