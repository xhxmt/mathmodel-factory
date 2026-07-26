from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .schemas import ShowcasePaper


_SAFE_BASE_NAME = re.compile(r"[A-Za-z0-9_-]+")
_CUMCM_NAME = re.compile(r"cumcm[_-]?(\d{4})[_-]?([abc])", re.IGNORECASE)


class ShowcasePaperNotFound(Exception):
    pass


def _is_within(path: Path, root: Path) -> bool:
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def _project_dir(settings: Settings, base_name: str) -> Path | None:
    if not _SAFE_BASE_NAME.fullmatch(base_name):
        return None
    project = settings.complete_dir / base_name
    if not project.is_dir() or not _is_within(project, settings.complete_dir):
        return None
    return project


def _showcase_pdf(settings: Settings, base_name: str) -> Path | None:
    project = _project_dir(settings, base_name)
    if project is None:
        return None

    packaged = settings.papers_dir / f"{base_name}_paper.pdf"
    candidates = [packaged, *sorted(project.glob("*_paper.pdf"))]
    for candidate in candidates:
        allowed_root = settings.papers_dir if candidate == packaged else project
        if candidate.is_file() and candidate.suffix.lower() == ".pdf" and _is_within(candidate, allowed_root):
            return candidate
    return None


def _paper_title(base_name: str) -> str:
    match = _CUMCM_NAME.search(base_name)
    if match:
        year, problem = match.groups()
        return f"{year} 全国大学生数学建模竞赛 {problem.upper()} 题论文"
    return f"{base_name.replace('_', ' ').replace('-', ' ')} · 示例论文"


def _updated_at(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def list_showcase_papers(settings: Settings) -> list[ShowcasePaper]:
    papers: list[ShowcasePaper] = []
    seen: set[str] = set()
    for base_name in settings.showcase_projects:
        if base_name in seen:
            continue
        seen.add(base_name)
        paper = _showcase_pdf(settings, base_name)
        if paper is None:
            continue
        papers.append(
            ShowcasePaper(
                base_name=base_name,
                title=_paper_title(base_name),
                collection="CUMCM · 完成论文",
                updated_at=_updated_at(paper),
                size_bytes=paper.stat().st_size,
                pdf_url=f"/api/showcase/papers/{base_name}/pdf",
            )
        )
    return papers


def resolve_showcase_paper(settings: Settings, base_name: str) -> Path:
    if base_name not in settings.showcase_projects:
        raise ShowcasePaperNotFound(base_name)
    paper = _showcase_pdf(settings, base_name)
    if paper is None:
        raise ShowcasePaperNotFound(base_name)
    return paper
