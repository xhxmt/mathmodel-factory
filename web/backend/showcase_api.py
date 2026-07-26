from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from .config import Settings
from .schemas import ShowcasePaper
from .showcase import ShowcasePaperNotFound, list_showcase_papers, resolve_showcase_paper


def create_showcase_router(settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.get("/api/showcase/papers", response_model=list[ShowcasePaper])
    async def get_showcase_papers():
        return list_showcase_papers(settings)

    @router.get("/api/showcase/papers/{base_name}/pdf")
    async def get_showcase_pdf(base_name: str, download: bool = False):
        try:
            paper = resolve_showcase_paper(settings, base_name)
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
                "Cache-Control": "public, max-age=3600",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router
