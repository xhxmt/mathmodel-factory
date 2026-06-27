import secrets
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from .config import Settings
from .schemas import UserInfo


JWT_ALGORITHM = "HS256"

try:
    from fastapi import Depends, HTTPException, status
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
except ModuleNotFoundError:  # pragma: no cover - exercised by unit tests without fastapi installed
    class HTTPException(Exception):
        def __init__(self, status_code=None, detail=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class _Status:
        HTTP_401_UNAUTHORIZED = 401

    def Depends(dep=None):
        return dep

    class HTTPAuthorizationCredentials:
        def __init__(self, credentials: str = ""):
            self.credentials = credentials

    class HTTPBearer:
        def __call__(self, *args, **kwargs):
            return None

    status = _Status()

security = HTTPBearer()


def user_db(settings: Settings) -> dict[str, dict[str, Any]]:
    return {
        "admin": {
            "password_hash": bcrypt.hashpw(
                settings.admin_password.encode("utf-8"),
                bcrypt.gensalt(),
            ),
            "username": "admin",
            "role": "admin",
        }
    }


def verify_password(password: str, password_hash: bytes) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash)
    except (TypeError, ValueError):
        return False


def create_access_token(settings: Settings, username: str, role: str) -> str:
    expiration = datetime.now(timezone.utc) + timedelta(hours=settings.jwt_hours)
    payload = {"sub": username, "role": role, "exp": expiration}
    return jwt.encode(payload, settings.jwt_secret, algorithm=JWT_ALGORITHM)


def decode_token(settings: Settings, token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        ) from exc


def require_token(settings: Settings) -> Callable[[HTTPAuthorizationCredentials], dict[str, Any]]:
    def _verify_token(
        credentials: HTTPAuthorizationCredentials = Depends(security),
    ) -> dict[str, Any]:
        return decode_token(settings, credentials.credentials)

    return _verify_token


def get_current_user(settings: Settings) -> Callable[[dict[str, Any]], UserInfo]:
    verify = require_token(settings)

    def _get_current_user(payload: dict[str, Any] = Depends(verify)) -> UserInfo:
        return UserInfo(username=payload["sub"], role=payload["role"])

    return _get_current_user


class WsTicketStore:
    def __init__(self, ttl_seconds: int):
        self.ttl_seconds = ttl_seconds
        self._tickets: dict[str, tuple[float, dict[str, Any]]] = {}

    def issue(self, payload: dict[str, Any]) -> str:
        self._prune_expired()
        ticket = secrets.token_urlsafe(32)
        self._tickets[ticket] = (time.monotonic() + self.ttl_seconds, dict(payload))
        return ticket

    def consume(self, ticket: str) -> dict[str, Any] | None:
        self._prune_expired()
        entry = self._tickets.pop(ticket, None)
        if entry is None:
            return None

        expires_at, payload = entry
        if expires_at <= time.monotonic():
            return None

        return payload

    def _prune_expired(self) -> None:
        now = time.monotonic()
        expired = [
            ticket for ticket, (expires_at, _payload) in self._tickets.items()
            if expires_at <= now
        ]
        for ticket in expired:
            self._tickets.pop(ticket, None)
