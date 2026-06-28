from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from .auth import WsTicketStore, create_access_token, get_current_user, user_db, verify_password
from .cloud_api import create_cloud_router
from .config import load_settings, validate_settings
from .project_api import (
    _valid_model_step_key,
    _resolve_project,
    _runtime_to_project_status,
    create_project_router,
    get_steps,
    list_all_projects,
    load_model_config,
    load_model_registry,
)
from .schemas import LoginRequest, LoginResponse, ProjectAction, UserInfo, WsTicketResponse
from .ws import ConnectionManager, create_monitor_task, create_ws_router


settings = load_settings()
validate_settings(settings)
ticket_store = WsTicketStore(ttl_seconds=60)
manager = ConnectionManager()
monitor_projects_task = create_monitor_task(settings, manager)
project_actions = __import__("web.backend.project_actions", fromlist=["run_action"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    monitor_task = asyncio.create_task(monitor_projects_task())
    try:
        yield
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Paper Factory Dashboard", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.get("/")
async def root():
    return {"status": "Paper Factory Dashboard API", "version": "2.0.0"}


@app.post("/api/auth/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    users = user_db(settings)
    user = users.get(request.username)
    if not user or not verify_password(request.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )

    access_token = create_access_token(settings, user["username"], user["role"])
    return LoginResponse(access_token=access_token, username=user["username"])


@app.get("/api/auth/me", response_model=UserInfo)
async def get_me(current_user: UserInfo = Depends(get_current_user(settings))):
    return current_user


@app.post("/api/auth/logout")
async def logout(current_user: UserInfo = Depends(get_current_user(settings))):
    del current_user
    return {"status": "ok", "message": "Logged out successfully"}


@app.post("/api/auth/ws-ticket", response_model=WsTicketResponse)
async def issue_ws_ticket(current_user: UserInfo = Depends(get_current_user(settings))):
    ticket = ticket_store.issue({"sub": current_user.username, "role": current_user.role})
    return WsTicketResponse(ticket=ticket)


project_router = create_project_router(settings, ticket_store, manager)
cloud_router = create_cloud_router(settings)
ws_router = create_ws_router(settings, ticket_store, manager)

app.include_router(project_router)
app.include_router(cloud_router)
app.include_router(ws_router)


@app.post("/api/projects/{base_name}/action")
async def project_action(
    base_name: str,
    action: ProjectAction,
    current_user: UserInfo = Depends(get_current_user(settings)),
):
    del current_user
    result = project_actions.run_action(settings.factory_root, action.action, base_name)
    if not result.ok:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=result.stderr or result.stdout or "project action failed",
        )
    await manager.broadcast({"type": "project_action", "project": base_name, "action": action.action})
    return {"status": "ok", "action": action.action, "output": result.stdout}



def _router_endpoint(router, path: str):
    for route in getattr(router, "routes", []):
        if getattr(route, "path", None) == path:
            return route.endpoint
    return None


async def get_projects(current_user: UserInfo = Depends(get_current_user(settings))):
    del current_user
    return list_all_projects(settings)


async def get_single_project_status(
    base_name: str,
    current_user: UserInfo = Depends(get_current_user(settings)),
):
    del current_user
    state_store = __import__("web.backend.state_store", fromlist=["read_runtime_status"])
    return _runtime_to_project_status(
        state_store.read_runtime_status(_resolve_project(settings, base_name), base_name)
    )


async def get_models(current_user: UserInfo = Depends(get_current_user(settings))):
    del current_user
    return {
        "registry": load_model_registry(settings),
        "config": load_model_config(settings),
        "agentic_backends": sorted({"claude", "codex", "agy"}),
        "valid_backends": sorted({"claude", "codex", "agy", "openai", "gemini", "deepseek"}),
    }


# Re-export selected helpers for existing tests and compatibility shims.
get_checkpoint = _router_endpoint(project_router, "/api/projects/{base_name}/checkpoint")
get_recent_logs = _router_endpoint(project_router, "/api/projects/{base_name}/logs")
get_project_steps = _router_endpoint(project_router, "/api/projects/{base_name}/steps")
get_files = _router_endpoint(project_router, "/api/projects/{base_name}/files")
get_file_content = _router_endpoint(project_router, "/api/projects/{base_name}/file")
get_raw_file = _router_endpoint(project_router, "/api/projects/{base_name}/raw")
get_paper = _router_endpoint(project_router, "/api/projects/{base_name}/paper")
get_consultation = _router_endpoint(project_router, "/api/projects/{base_name}/consultation")
submit_consultation_answer = _router_endpoint(project_router, "/api/projects/{base_name}/consultation/answer")
upload_problem_file = _router_endpoint(project_router, "/api/upload/problem")
create_new_project = _router_endpoint(project_router, "/api/projects/new")
put_model_registry = _router_endpoint(project_router, "/api/models/registry")
put_model_config = _router_endpoint(project_router, "/api/models/config")
cloud_status = _router_endpoint(cloud_router, "/api/cloud/status")
cloud_config = _router_endpoint(cloud_router, "/api/cloud/config")
project_cloud_config = _router_endpoint(cloud_router, "/api/projects/{base_name}/cloud/config")
enable_cloud_solver = _router_endpoint(cloud_router, "/api/projects/{base_name}/cloud/enable")
disable_cloud_solver = _router_endpoint(cloud_router, "/api/projects/{base_name}/cloud/disable")
websocket_endpoint = _router_endpoint(ws_router, "/ws")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
