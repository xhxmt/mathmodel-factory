from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from .auth import get_current_user
from .auth_store import AuthStore
from .config import Settings
from .schemas import ShowcasePaper, UserInfo
from .showcase import (
    ShowcasePaperNotFound,
    list_completed_showcase_papers,
    list_showcase_papers,
    resolve_showcase_paper,
)


def create_showcase_router(settings: Settings, store: AuthStore) -> APIRouter:
    router = APIRouter()

    def guest_project_names() -> list[str]:
        return store.list_showcase_project_names("guest")

    def user_project_names(current_user: UserInfo) -> list[str]:
        if current_user.role == "admin":
            return [paper.base_name for paper in list_completed_showcase_papers(settings)]
        personal = store.list_showcase_project_names(
            store.showcase_user_audience(current_user.username)
        )
        return sorted(set(guest_project_names()) | set(personal))

    @router.get("/api/showcase/papers", response_model=list[ShowcasePaper])
    async def get_showcase_papers():
        return list_showcase_papers(settings, guest_project_names())

    @router.get("/api/showcase/user-papers", response_model=list[ShowcasePaper])
    async def get_user_showcase_papers(
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        return list_showcase_papers(
            settings,
            user_project_names(current_user),
            pdf_url_prefix="/api/showcase/user-papers",
        )

    @router.get("/api/showcase/papers/{base_name}/pdf")
    async def get_showcase_pdf(base_name: str, download: bool = False):
        try:
            paper = resolve_showcase_paper(settings, base_name, guest_project_names())
        except ShowcasePaperNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="SHOWCASE_PAPER_NOT_FOUND",
            ) from exc
        return FileResponse(
            str(paper),
            media_type="application/pdf",
            filename=paper.name if download else None,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/api/showcase/user-papers/{base_name}/pdf")
    async def get_user_showcase_pdf(
        base_name: str,
        download: bool = False,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        try:
            paper = resolve_showcase_paper(settings, base_name, user_project_names(current_user))
        except ShowcasePaperNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="SHOWCASE_PAPER_NOT_FOUND",
            ) from exc
        return FileResponse(
            str(paper),
            media_type="application/pdf",
            filename=paper.name if download else None,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router
