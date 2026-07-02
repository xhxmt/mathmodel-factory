from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from .auth import WsTicketStore, create_access_token, get_current_user
from .auth_store import AuthStore, InvalidUsername, UserExists, UserNotFound
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
from .schemas import (
    LoginRequest,
    LoginResponse,
    ProjectAction,
    ProjectRequestCreate,
    ProjectRequestDecision,
    RegisterRequest,
    UserDecisionRequest,
    UserInfo,
    UserResponse,
    NewProjectRequest,
    WsTicketResponse,
)
from .ws import ConnectionManager, create_monitor_task, create_ws_router


settings = load_settings()
validate_settings(settings)
auth_store = AuthStore(settings.resolved_auth_db_file)
auth_store.initialize()
auth_store.bootstrap_admin(settings.admin_password)
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


def _user_response(user) -> UserResponse:
    return UserResponse(
        username=user.username,
        role=user.role,
        status=user.status,
        display_name=user.display_name or "",
        created_at=user.created_at,
        approved_at=user.approved_at,
        approved_by=user.approved_by,
        rejected_at=user.rejected_at,
        rejected_by=user.rejected_by,
        rejection_reason=user.rejection_reason,
    )


def _require_admin(current_user: UserInfo) -> None:
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="ADMIN_REQUIRED")


@app.post("/api/auth/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    user = auth_store.get_user(request.username)
    if not user or not auth_store.verify_user_password(request.username, request.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )
    if user.status == "pending":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="USER_PENDING")
    if user.status == "rejected":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="USER_REJECTED")
    if user.status == "disabled":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="USER_DISABLED")

    access_token = create_access_token(settings, user.username, user.role, user.status)
    return LoginResponse(
        access_token=access_token,
        username=user.username,
        role=user.role,
        status=user.status,
    )


@app.post("/api/auth/register", response_model=UserResponse)
async def register_user(request: RegisterRequest):
    try:
        user = auth_store.register_user(
            request.username,
            request.password,
            request.display_name,
        )
    except UserExists as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="USER_EXISTS") from exc
    except InvalidUsername as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="INVALID_USERNAME") from exc
    return _user_response(user)


@app.get("/api/auth/me", response_model=UserInfo)
async def get_me(current_user: UserInfo = Depends(get_current_user(settings))):
    return current_user


@app.post("/api/auth/logout")
async def logout(current_user: UserInfo = Depends(get_current_user(settings))):
    del current_user
    return {"status": "ok", "message": "Logged out successfully"}


@app.post("/api/auth/ws-ticket", response_model=WsTicketResponse)
async def issue_ws_ticket(current_user: UserInfo = Depends(get_current_user(settings))):
    ticket = ticket_store.issue(
        {
            "sub": current_user.username,
            "role": current_user.role,
            "status": current_user.status,
        }
    )
    return WsTicketResponse(ticket=ticket)


@app.get("/api/admin/users", response_model=list[UserResponse])
async def list_users(current_user: UserInfo = Depends(get_current_user(settings))):
    _require_admin(current_user)
    return [_user_response(user) for user in auth_store.list_users()]


@app.post("/api/admin/users/{username}/approve", response_model=UserResponse)
async def approve_user(
    username: str,
    decision: UserDecisionRequest = UserDecisionRequest(),
    current_user: UserInfo = Depends(get_current_user(settings)),
):
    del decision
    _require_admin(current_user)
    try:
        user = auth_store.approve_user(username, actor=current_user.username)
    except UserNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="USER_NOT_FOUND") from exc
    return _user_response(user)


@app.post("/api/admin/users/{username}/reject", response_model=UserResponse)
async def reject_user(
    username: str,
    decision: UserDecisionRequest,
    current_user: UserInfo = Depends(get_current_user(settings)),
):
    _require_admin(current_user)
    try:
        user = auth_store.reject_user(
            username,
            actor=current_user.username,
            reason=decision.reason,
        )
    except UserNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="USER_NOT_FOUND") from exc
    return _user_response(user)


@app.post("/api/admin/users/{username}/disable", response_model=UserResponse)
async def disable_user(
    username: str,
    decision: UserDecisionRequest = UserDecisionRequest(),
    current_user: UserInfo = Depends(get_current_user(settings)),
):
    del decision
    _require_admin(current_user)
    try:
        user = auth_store.disable_user(username, actor=current_user.username)
    except UserNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="USER_NOT_FOUND") from exc
    return _user_response(user)


@app.delete("/api/admin/users/{username}")
async def delete_user(
    username: str,
    current_user: UserInfo = Depends(get_current_user(settings)),
):
    _require_admin(current_user)
    try:
        auth_store.delete_user(username, actor=current_user.username)
    except UserNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="USER_NOT_FOUND") from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ADMIN_USER_CANNOT_BE_DELETED",
        ) from exc
    return {"status": "ok", "username": username}


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
    from .access_control import require_project_access

    require_project_access(settings, current_user, base_name)
    result = project_actions.run_action(settings.factory_root, action.action, base_name)
    if not result.ok:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=result.stderr or result.stdout or "project action failed",
        )
    await manager.broadcast({"type": "project_action", "project": base_name, "action": action.action})
    return {"status": "ok", "action": action.action, "output": result.stdout}



def _router_endpoint(router, path: str, method: str | None = None):
    for route in getattr(router, "routes", []):
        if getattr(route, "path", None) != path:
            continue
        if method is not None:
            methods = getattr(route, "methods", None)
            if methods is not None and method.upper() not in methods:
                continue
        return route.endpoint
    return None


async def get_projects(current_user: UserInfo = Depends(get_current_user(settings))):
    from .access_control import filter_visible_projects

    return filter_visible_projects(settings, current_user, list_all_projects(settings))


async def get_single_project_status(
    base_name: str,
    current_user: UserInfo = Depends(get_current_user(settings)),
):
    from .access_control import require_project_access

    require_project_access(settings, current_user, base_name)
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
get_modeling_directions = _router_endpoint(project_router, "/api/projects/{base_name}/modeling-directions")
select_modeling_direction = _router_endpoint(project_router, "/api/projects/{base_name}/modeling-directions/selection")
upload_problem_file = _router_endpoint(project_router, "/api/upload/problem")
create_new_project = _router_endpoint(project_router, "/api/projects/new")
list_project_requests = _router_endpoint(project_router, "/api/project-requests", "GET")
create_project_request = _router_endpoint(project_router, "/api/project-requests", "POST")
approve_project_request = _router_endpoint(project_router, "/api/admin/project-requests/{request_id}/approve")
reject_project_request = _router_endpoint(project_router, "/api/admin/project-requests/{request_id}/reject")
put_model_registry = _router_endpoint(project_router, "/api/models/registry")
put_model_config = _router_endpoint(project_router, "/api/models/config")
cloud_status = _router_endpoint(cloud_router, "/api/cloud/status")
cloud_config = _router_endpoint(cloud_router, "/api/cloud/config")
project_cloud_config = _router_endpoint(cloud_router, "/api/projects/{base_name}/cloud/config")
enable_cloud_solver = _router_endpoint(cloud_router, "/api/projects/{base_name}/cloud/enable")
disable_cloud_solver = _router_endpoint(cloud_router, "/api/projects/{base_name}/cloud/disable")
websocket_endpoint = _router_endpoint(ws_router, "/ws")
project_api = __import__("web.backend.project_api", fromlist=["run_project_launcher"])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
