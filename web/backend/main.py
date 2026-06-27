from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from .auth import WsTicketStore
from .config import load_settings, validate_settings
from .schemas import ProjectAction, UserInfo


settings = load_settings()
validate_settings(settings)
ticket_store = WsTicketStore(ttl_seconds=60)
project_actions = __import__("web.backend.project_actions", fromlist=["run_action"])

app = FastAPI(title="Paper Factory Dashboard", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


def _default_user() -> UserInfo:
    return UserInfo(username="admin", role="admin")


@app.post("/api/projects/{base_name}/action")
async def project_action(
    base_name: str,
    action: ProjectAction,
    current_user: UserInfo = Depends(_default_user),
):
    del current_user
    result = project_actions.run_action(settings.factory_root, action.action, base_name)
    if not result.ok:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=result.stderr or result.stdout or "project action failed",
        )
    return {"status": "ok", "action": action.action, "output": result.stdout}
