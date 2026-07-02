import asyncio
import importlib
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from web.backend.auth import WsTicketStore, create_access_token, decode_token, get_current_user
from web.backend.auth import user_db, verify_password
from web.backend.auth_store import AuthStore
from web.backend.config import Settings, validate_settings
from web.backend.schemas import LoginRequest, RegisterRequest, UserDecisionRequest, UserInfo
from web.backend.ws import create_ws_router


def load_main_module(tmp_path: Path):
    sys.modules.pop("web.backend.main", None)
    os.environ["JWT_SECRET"] = "0123456789abcdef0123456789abcdef"
    os.environ["ADMIN_PASSWORD"] = "correct horse battery staple 42"
    os.environ["FACTORY_ROOT"] = str(tmp_path)
    os.environ["AUTH_DB_FILE"] = str(tmp_path / "web" / "auth.db")
    return importlib.import_module("web.backend.main")


def test_validate_settings_rejects_default_admin_password():
    settings = Settings(
        jwt_secret="0123456789abcdef0123456789abcdef",
        admin_password="admin123",
    )

    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        validate_settings(settings)


def test_validate_settings_accepts_strong_values():
    settings = Settings(
        jwt_secret="0123456789abcdef0123456789abcdef",
        admin_password="correct horse battery staple 42",
    )

    validate_settings(settings)


def test_ws_ticket_is_single_use():
    store = WsTicketStore(ttl_seconds=30)
    ticket = store.issue({"sub": "admin", "role": "admin"})

    first = store.consume(ticket)
    second = store.consume(ticket)

    assert first is not None
    assert first["sub"] == "admin"
    assert second is None


def test_user_db_hash_verifies_configured_password():
    settings = Settings(
        jwt_secret="0123456789abcdef0123456789abcdef",
        admin_password="correct horse battery staple 42",
    )

    user = user_db(settings)["admin"]

    assert verify_password("correct horse battery staple 42", user["password_hash"]) is True


class DummyWebSocket:
    def __init__(self, ticket=None):
        self.query_params = {}
        if ticket is not None:
            self.query_params["ticket"] = ticket
        self.accepted = False
        self.closed = None

    async def accept(self):
        self.accepted = True

    async def close(self, code=None):
        self.closed = code

    async def send_json(self, payload):
        raise RuntimeError("stop after auth gate")


class DummyManager:
    def __init__(self):
        self.connected = False

    async def connect(self, websocket, payload=None):
        del payload
        self.connected = True
        await websocket.accept()

    def disconnect(self, websocket):
        pass


def _ws_endpoint(router):
    for route in getattr(router, "routes", []):
        if getattr(route, "path", None) == "/ws":
            return route.endpoint
    raise AssertionError("missing /ws endpoint")


def test_websocket_rejects_missing_ticket():
    settings = Settings(
        jwt_secret="0123456789abcdef0123456789abcdef",
        admin_password="correct horse battery staple 42",
    )
    manager = DummyManager()
    router = create_ws_router(settings, WsTicketStore(ttl_seconds=30), manager)
    websocket = DummyWebSocket()

    asyncio.run(_ws_endpoint(router)(websocket))

    assert websocket.accepted is False
    assert manager.connected is False
    assert websocket.closed is not None


def test_current_user_loads_active_user_from_auth_store(tmp_path):
    settings = Settings(
        jwt_secret="0123456789abcdef0123456789abcdef",
        admin_password="correct horse battery staple 42",
        factory_root=tmp_path,
        auth_db_file=tmp_path / "web" / "auth.db",
    )
    store = AuthStore(settings.resolved_auth_db_file)
    store.initialize()
    store.bootstrap_admin(settings.admin_password)

    token = create_access_token(settings, "admin", "admin")
    payload = decode_token(settings, token)

    user = get_current_user(settings)(payload=payload)

    assert user.username == "admin"
    assert user.role == "admin"
    assert user.status == "active"


def test_current_user_rejects_disabled_token_user(tmp_path):
    settings = Settings(
        jwt_secret="0123456789abcdef0123456789abcdef",
        admin_password="correct horse battery staple 42",
        factory_root=tmp_path,
        auth_db_file=tmp_path / "web" / "auth.db",
    )
    store = AuthStore(settings.resolved_auth_db_file)
    store.initialize()
    store.bootstrap_admin(settings.admin_password)
    store.disable_user("admin", actor="admin")
    token = create_access_token(settings, "admin", "admin")
    payload = decode_token(settings, token)

    with pytest.raises(Exception) as excinfo:
        get_current_user(settings)(payload=payload)

    assert getattr(excinfo.value, "status_code", None) == 401


def test_register_login_and_admin_approval_routes(tmp_path):
    mod = load_main_module(tmp_path)

    registered = asyncio.run(
        mod.register_user(
            RegisterRequest(username="alice", password="alice password", display_name="Alice")
        )
    )
    assert registered.username == "alice"
    assert registered.status == "pending"

    with pytest.raises(mod.HTTPException) as pending_login:
        asyncio.run(mod.login(LoginRequest(username="alice", password="alice password")))
    assert pending_login.value.status_code == 403
    assert pending_login.value.detail == "USER_PENDING"

    admin = UserInfo(username="admin", role="admin", status="active")
    approved = asyncio.run(
        mod.approve_user(
            "alice",
            UserDecisionRequest(reason=""),
            current_user=admin,
        )
    )
    assert approved.status == "active"

    logged_in = asyncio.run(mod.login(LoginRequest(username="alice", password="alice password")))
    assert logged_in.username == "alice"
    assert logged_in.role == "user"
    assert logged_in.status == "active"
    assert logged_in.access_token


def test_admin_delete_user_route_removes_user(tmp_path):
    mod = load_main_module(tmp_path)

    asyncio.run(
        mod.register_user(
            RegisterRequest(username="alice", password="alice password", display_name="Alice")
        )
    )
    admin = UserInfo(username="admin", role="admin", status="active")

    result = asyncio.run(mod.delete_user("alice", current_user=admin))

    assert result == {"status": "ok", "username": "alice"}
    assert mod.auth_store.get_user("alice") is None


def test_admin_delete_user_route_rejects_builtin_admin(tmp_path):
    mod = load_main_module(tmp_path)
    admin = UserInfo(username="admin", role="admin", status="active")

    with pytest.raises(mod.HTTPException) as excinfo:
        asyncio.run(mod.delete_user("admin", current_user=admin))

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "ADMIN_USER_CANNOT_BE_DELETED"
    assert mod.auth_store.get_user("admin") is not None


def test_authenticated_json_body_routes_keep_flat_request_schema(tmp_path):
    mod = load_main_module(tmp_path)

    schema = mod.app.openapi()
    body_schema = schema["paths"]["/api/projects/new"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]

    assert body_schema == {"$ref": "#/components/schemas/NewProjectRequest"}
