"""Realtime push for the control plane.

Two properties are deliberate here.

**One scanner.**  ``ConnectionManager.projects_snapshot`` owns the only call to
``list_all_projects``.  It is TTL-cached behind a lock, so N connected clients
share one scan per cycle instead of each running its own full project walk.  The
previous design scanned once globally *and* once per connection, and the
per-connection work scaled with the number of clients.

**Explicit audiences.**  There is no unscoped ``broadcast``.  Every message names
its recipients -- :meth:`publish_public`, :meth:`publish_project`,
:meth:`publish_user` or :meth:`publish_admins` -- and each recipient's account is
re-read at send time, so a disabled account stops receiving on its existing
connection instead of continuing on the identity captured at handshake.
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from starlette.concurrency import run_in_threadpool

from .access_control import filter_visible_projects, get_store
from .config import Settings
from .project_api import list_all_projects
from .schemas import UserInfo

SNAPSHOT_TTL_SECONDS = 2.5


def _payload_username(payload: dict) -> str:
    return str(payload.get("sub", "") or "")


# Sentinel: the account could not be checked (auth store unavailable).  Distinct
# from ``None``, which means the account is gone.
_UNVERIFIED = object()


def _live_user(settings: Settings, username: str):
    """Current account row, ``None`` when it is gone, or :data:`_UNVERIFIED`."""

    if not username:
        return None
    try:
        stored = get_store(settings).get_user(username)
    except Exception:
        return _UNVERIFIED
    if stored is None:
        return None
    return UserInfo(username=stored.username, role=stored.role, status=stored.status)


def _project_audience(settings: Settings, base_name: str) -> set[str]:
    """Usernames holding access to ``base_name`` (admins are handled separately)."""

    store = get_store(settings)
    return {
        user.username
        for user in store.list_users()
        if store.user_can_access_project(user.username, base_name)
    }


class ConnectionManager:
    def __init__(self, *, snapshot_ttl_seconds: float = SNAPSHOT_TTL_SECONDS) -> None:
        self.active_connections: dict[WebSocket, dict] = {}
        self._snapshot: list = []
        self._snapshot_at = 0.0
        self._snapshot_ttl = snapshot_ttl_seconds
        self._snapshot_lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, payload: dict | None = None) -> None:
        await websocket.accept()
        self.active_connections[websocket] = payload or {}

    def disconnect(self, websocket: WebSocket) -> None:
        self.active_connections.pop(websocket, None)

    def connections(self) -> list[tuple[WebSocket, dict]]:
        return list(self.active_connections.items())

    async def projects_snapshot(self, settings: Settings) -> list:
        """The single shared project scan, refreshed at most once per TTL."""

        if self._snapshot and (time.monotonic() - self._snapshot_at) < self._snapshot_ttl:
            return self._snapshot
        async with self._snapshot_lock:
            if (
                self._snapshot
                and (time.monotonic() - self._snapshot_at) < self._snapshot_ttl
            ):
                return self._snapshot
            projects = await run_in_threadpool(list_all_projects, settings)
            self._snapshot = projects
            self._snapshot_at = time.monotonic()
            return projects

    async def revoke(self, websocket: WebSocket) -> None:
        """Close a connection whose account is no longer usable."""

        try:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        except Exception:
            pass
        self.disconnect(websocket)

    async def _send(self, websocket: WebSocket, message: dict) -> None:
        try:
            await websocket.send_json(message)
        except Exception:
            self.disconnect(websocket)

    async def _deliver(self, settings: Settings, message: dict, *, allow) -> None:
        cache: dict[str, object] = {}
        for websocket, payload in self.connections():
            username = _payload_username(payload)
            if username not in cache:
                cache[username] = await run_in_threadpool(
                    _live_user, settings, username
                )
            user = cache[username]
            if user is _UNVERIFIED:
                # Cannot confirm the account: withhold the message, keep the
                # connection (a transient auth-store failure is not a revocation).
                continue
            if user is None or getattr(user, "status", "active") != "active":
                await self.revoke(websocket)
                continue
            if allow(user, username):
                await self._send(websocket, message)

    async def publish_public(self, settings: Settings, message: dict) -> None:
        """Every active account."""

        await self._deliver(settings, message, allow=lambda user, name: True)

    async def publish_admins(self, settings: Settings, message: dict) -> None:
        await self._deliver(
            settings, message, allow=lambda user, name: user.role == "admin"
        )

    async def publish_user(
        self, settings: Settings, username: str, message: dict
    ) -> None:
        """One account, plus admins (who administer the request queues)."""

        await self._deliver(
            settings,
            message,
            allow=lambda user, name: name == username or user.role == "admin",
        )

    async def publish_project(
        self, settings: Settings, base_name: str, message: dict
    ) -> None:
        """Accounts that can access one project, plus admins."""

        audience = await run_in_threadpool(_project_audience, settings, base_name)
        await self._deliver(
            settings,
            message,
            allow=lambda user, name: user.role == "admin" or name in audience,
        )


def create_ws_router(settings: Settings, ticket_store, manager: ConnectionManager) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        ticket = websocket.query_params.get("ticket")
        if not ticket:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        payload = ticket_store.consume(ticket)
        if payload is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        await manager.connect(websocket, payload)
        username = _payload_username(payload)
        last_state: dict[str, dict] = {}
        try:
            while True:
                await asyncio.sleep(2)
                # Re-read the account every cycle: a ticket only constrains the
                # handshake, it cannot authorise the connection's lifetime.
                user = await run_in_threadpool(_live_user, settings, username)
                if user is _UNVERIFIED:
                    continue
                if user is None or getattr(user, "status", "active") != "active":
                    await manager.revoke(websocket)
                    return
                projects = await manager.projects_snapshot(settings)
                visible = await run_in_threadpool(
                    filter_visible_projects, settings, user, projects
                )
                await websocket.send_json(
                    {
                        "type": "status_update",
                        "projects": [project.dict() for project in visible],
                    }
                )
                current_state = {project.base_name: project.dict() for project in visible}
                for base_name, state in current_state.items():
                    if last_state.get(base_name) != state:
                        await websocket.send_json(
                            {
                                "type": "project_updated",
                                "project": base_name,
                                "status": state,
                            }
                        )
                last_state = current_state
        except WebSocketDisconnect:
            manager.disconnect(websocket)
        except RuntimeError as exc:
            manager.disconnect(websocket)
            if 'once a close message has been sent' not in str(exc):
                raise
        except Exception:
            manager.disconnect(websocket)
            raise

    return router
