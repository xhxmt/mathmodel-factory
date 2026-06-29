import pytest
import sys
import asyncio
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from web.backend.auth import WsTicketStore
from web.backend.auth import user_db, verify_password
from web.backend.config import Settings, validate_settings
from web.backend.ws import create_ws_router


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

    async def connect(self, websocket):
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
