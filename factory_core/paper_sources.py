from __future__ import annotations

from pathlib import Path


def _contained_regular_file(project: Path, candidate: Path) -> bool:
    if not candidate.is_file() or candidate.is_symlink():
        return False
    try:
        candidate.resolve(strict=True).relative_to(project)
    except (OSError, ValueError):
        return False
    return True


def discover_paper_sources(
    project_dir: str | Path, base_name: str | None = None
) -> tuple[Path, ...]:
    """Return every supported authored LaTeX source in precedence order.

    The root ``<base>_paper.tex`` layout remains the compatibility-first
    source.  ``paper/paper.tex`` is the canonical nested project layout.  Both
    are returned when both exist so audit and dirty tracking cannot silently
    ignore one of the authored sources.
    """

    project = Path(project_dir).resolve()
    base = base_name or project.name
    candidates = (
        project / f"{base}_paper.tex",
        project / "paper" / "paper.tex",
        *sorted(project.glob("*_paper.tex")),
    )
    return _unique_contained_files(project, candidates)


def primary_paper_source(
    project_dir: str | Path, base_name: str | None = None
) -> Path | None:
    sources = discover_paper_sources(project_dir, base_name)
    return sources[0] if sources else None


def discover_paper_pdfs(
    project_dir: str | Path, base_name: str | None = None
) -> tuple[Path, ...]:
    project = Path(project_dir).resolve()
    base = base_name or project.name
    candidates = (
        project / f"{base}_paper.pdf",
        project / "paper" / "paper.pdf",
        *sorted(project.glob("*_paper.pdf")),
    )
    return _unique_contained_files(project, candidates)


def _unique_contained_files(
    project: Path, candidates: tuple[Path, ...]
) -> tuple[Path, ...]:
    discovered: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if _contained_regular_file(project, candidate):
            discovered.append(candidate)
    return tuple(discovered)
