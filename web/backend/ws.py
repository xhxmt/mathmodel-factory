from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, status

from .access_control import filter_visible_projects
from .config import Settings
from .project_api import list_all_projects
from .schemas import UserInfo


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: dict[WebSocket, dict] = {}

    async def connect(self, websocket: WebSocket, payload: dict | None = None) -> None:
        await websocket.accept()
        self.active_connections[websocket] = payload or {}

    def disconnect(self, websocket: WebSocket) -> None:
        self.active_connections.pop(websocket, None)

    async def broadcast(self, message: dict) -> None:
        stale: list[WebSocket] = []
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                stale.append(connection)
        for connection in stale:
            self.disconnect(connection)

    def connections(self) -> list[tuple[WebSocket, dict]]:
        return list(self.active_connections.items())


def _payload_user(payload: dict) -> UserInfo:
    return UserInfo(
        username=payload.get("sub", ""),
        role=payload.get("role", "user"),
        status=payload.get("status", "active"),
    )


def create_monitor_task(
    settings: Settings,
    manager: ConnectionManager,
) -> Callable[[], Awaitable[None]]:
    async def monitor_projects_task() -> None:
        last_state: dict[str, dict] = {}
        while True:
            try:
                projects = list_all_projects(settings)
                current_state = {project.base_name: project.dict() for project in projects}
                for websocket, payload in manager.connections():
                    user = _payload_user(payload)
                    visible = filter_visible_projects(settings, user, projects)
                    visible_names = {project.base_name for project in visible}
                    try:
                        await websocket.send_json(
                            {"type": "status_update", "projects": [project.dict() for project in visible]}
                        )
                        for base_name, state in current_state.items():
                            if base_name in visible_names and last_state.get(base_name) != state:
                                await websocket.send_json(
                                    {"type": "project_updated", "project": base_name, "status": state}
                                )
                    except Exception:
                        manager.disconnect(websocket)
                last_state = current_state
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(5)

    return monitor_projects_task


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
        user = _payload_user(payload)
        try:
            while True:
                await asyncio.sleep(2)
                projects = filter_visible_projects(settings, user, list_all_projects(settings))
                await websocket.send_json(
                    {"type": "status_update", "projects": [project.dict() for project in projects]}
                )
        except WebSocketDisconnect:
            manager.disconnect(websocket)
        except Exception:
            manager.disconnect(websocket)
            raise

    return router
