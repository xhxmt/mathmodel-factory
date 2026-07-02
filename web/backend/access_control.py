from __future__ import annotations

from collections.abc import Iterable

from fastapi import HTTPException, status

from .auth_store import AuthStore
from .config import Settings
from .schemas import ProjectStatus, UserInfo


def get_store(settings: Settings) -> AuthStore:
    store = AuthStore(settings.resolved_auth_db_file)
    store.initialize()
    return store


def require_admin(user: UserInfo) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="ADMIN_REQUIRED")


def require_active(user: UserInfo) -> None:
    if getattr(user, "status", "active") != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="USER_NOT_ACTIVE")


def can_manage_project(settings: Settings, user: UserInfo, base_name: str) -> bool:
    require_active(user)
    if user.role == "admin":
        return True
    return get_store(settings).user_can_access_project(user.username, base_name)


def require_project_access(settings: Settings, user: UserInfo, base_name: str) -> None:
    if not can_manage_project(settings, user, base_name):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")


def filter_visible_projects(
    settings: Settings,
    user: UserInfo,
    projects: Iterable[ProjectStatus],
) -> list[ProjectStatus]:
    require_active(user)
    project_list = list(projects)
    if user.role == "admin":
        return project_list
    allowed = set(get_store(settings).list_project_names_for_user(user.username))
    return [project for project in project_list if project.base_name in allowed]
