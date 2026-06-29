from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, status

from .config import Settings
from .project_api import list_all_projects


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict) -> None:
        stale: list[WebSocket] = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                stale.append(connection)
        for connection in stale:
            self.disconnect(connection)


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
                for base_name, state in current_state.items():
                    if last_state.get(base_name) != state:
                        await manager.broadcast(
                            {"type": "project_updated", "project": base_name, "status": state}
                        )
                await manager.broadcast(
                    {"type": "status_update", "projects": [project.dict() for project in projects]}
                )
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

        await manager.connect(websocket)
        try:
            while True:
                await asyncio.sleep(2)
                projects = list_all_projects(settings)
                await websocket.send_json(
                    {"type": "status_update", "projects": [project.dict() for project in projects]}
                )
        except WebSocketDisconnect:
            manager.disconnect(websocket)
        except Exception:
            manager.disconnect(websocket)
            raise

    return router
