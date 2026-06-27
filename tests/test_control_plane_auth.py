import pytest
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from web.backend.auth import WsTicketStore
from web.backend.config import Settings, validate_settings


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
