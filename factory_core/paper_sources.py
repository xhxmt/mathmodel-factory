from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


LATEX_DEPENDENCY_SCHEMA = "factory-latex-dependency-graph-v1"
_DEPENDENCY_RE = re.compile(
    r"\\(?P<command>input|include|subfile|bibliography|addbibresource)"
    r"\s*(?:\[[^\]]*\]\s*)?\{(?P<target>[^{}]+)\}",
    re.IGNORECASE,
)
_SOURCE_COMMANDS = {"input", "include", "subfile"}


@dataclass(frozen=True)
class LatexDependencyEdge:
    source: Path
    target: Path | None
    command: str
    requested: str

    def to_dict(self, project: Path) -> dict[str, Any]:
        return {
            "source": self.source.relative_to(project).as_posix(),
            "target": (
                self.target.relative_to(project).as_posix()
                if self.target is not None
                else None
            ),
            "command": self.command,
            "requested": self.requested,
        }


@dataclass(frozen=True)
class LatexDependencyDiagnostic:
    code: str
    source: Path
    command: str
    requested: str
    message: str

    def to_dict(self, project: Path) -> dict[str, Any]:
        return {
            "code": self.code,
            "source": self.source.relative_to(project).as_posix(),
            "command": self.command,
            "requested": self.requested,
            "message": self.message,
        }


@dataclass(frozen=True)
class LatexDependencyGraph:
    project: Path
    roots: tuple[Path, ...]
    sources: tuple[Path, ...]
    bibliographies: tuple[Path, ...]
    edges: tuple[LatexDependencyEdge, ...]
    diagnostics: tuple[LatexDependencyDiagnostic, ...]

    @property
    def files(self) -> tuple[Path, ...]:
        return _unique_contained_files(
            self.project, (*self.sources, *self.bibliographies)
        )

    def manifest(self) -> dict[str, Any]:
        return {
            "schema_version": LATEX_DEPENDENCY_SCHEMA,
            "roots": [path.relative_to(self.project).as_posix() for path in self.roots],
            "sources": [
                path.relative_to(self.project).as_posix() for path in self.sources
            ],
            "bibliographies": [
                path.relative_to(self.project).as_posix()
                for path in self.bibliographies
            ],
            "edges": [edge.to_dict(self.project) for edge in self.edges],
            "diagnostics": [
                diagnostic.to_dict(self.project) for diagnostic in self.diagnostics
            ],
        }


def _contained_regular_file(project: Path, candidate: Path) -> bool:
    if not candidate.is_file() or candidate.is_symlink():
        return False
    try:
        lexical = Path(os.path.abspath(candidate))
        relative = lexical.relative_to(project)
    except ValueError:
        return False
    cursor = project
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return False
    try:
        candidate.resolve(strict=True).relative_to(project)
    except (OSError, ValueError):
        return False
    return True


def _discover_paper_roots(
    project: Path, base_name: str | None = None
) -> tuple[Path, ...]:
    base = base_name or project.name
    for candidate in (
        project / f"{base}_paper.tex",
        project / "paper" / "paper.tex",
        *sorted(project.glob("*_paper.tex")),
    ):
        discovered = _unique_contained_files(project, (candidate,))
        if discovered:
            return discovered
    return ()


def _strip_comments(text: str) -> str:
    return "\n".join(re.sub(r"(?<!\\)%.*$", "", line) for line in text.splitlines())


def _dependency_candidates(
    project: Path,
    source: Path,
    requested: str,
    *,
    suffix: str,
) -> tuple[Path, ...]:
    value = requested.strip().strip('"').strip("'")
    raw = Path(value)
    if raw.is_absolute() or not value or "\\" in value or "#" in value:
        return ()
    variants = (raw,) if raw.suffix else (raw, raw.with_suffix(suffix))
    candidates: list[Path] = []
    for base in (source.parent, project):
        for variant in variants:
            candidate = Path(os.path.abspath(base / variant))
            try:
                candidate.relative_to(project)
            except ValueError:
                continue
            if candidate not in candidates:
                candidates.append(candidate)
    return tuple(candidates)


def _resolve_dependency(
    project: Path,
    source: Path,
    requested: str,
    *,
    suffix: str,
) -> Path | None:
    for candidate in _dependency_candidates(
        project, source, requested, suffix=suffix
    ):
        if _contained_regular_file(project, candidate):
            return candidate.resolve(strict=True)
    return None


def resolve_latex_dependency_graph(
    project_dir: str | Path, base_name: str | None = None
) -> LatexDependencyGraph:
    """Resolve active LaTeX sources and bibliographies from supported roots.

    Only files reachable through ``input``, ``include``, ``subfile``,
    ``bibliography``, or ``addbibresource`` participate.  Archived or unreferenced
    drafts are deliberately excluded from the paper identity.
    """

    project = Path(project_dir).resolve()
    roots = _discover_paper_roots(project, base_name)
    sources: list[Path] = []
    bibliographies: list[Path] = []
    edges: list[LatexDependencyEdge] = []
    diagnostics: list[LatexDependencyDiagnostic] = []
    visited: set[Path] = set()
    visiting: set[Path] = set()

    def diagnostic(
        code: str, source: Path, command: str, requested: str, message: str
    ) -> None:
        diagnostics.append(
            LatexDependencyDiagnostic(code, source, command, requested, message)
        )

    def visit(source: Path) -> None:
        if source in visited:
            return
        visited.add(source)
        visiting.add(source)
        sources.append(source)
        try:
            text = _strip_comments(source.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            diagnostic("unreadable", source, "read", source.name, str(exc))
            visiting.discard(source)
            return
        for match in _DEPENDENCY_RE.finditer(text):
            command = match.group("command").lower()
            requested_values = (
                match.group("target").split(",")
                if command == "bibliography"
                else (match.group("target"),)
            )
            for requested_value in requested_values:
                requested = requested_value.strip()
                suffix = ".tex" if command in _SOURCE_COMMANDS else ".bib"
                target = _resolve_dependency(
                    project, source, requested, suffix=suffix
                )
                edges.append(LatexDependencyEdge(source, target, command, requested))
                if target is None:
                    code = (
                        "dynamic_dependency"
                        if "\\" in requested or "#" in requested
                        else "missing_dependency"
                    )
                    diagnostic(
                        code,
                        source,
                        command,
                        requested,
                        f"Could not resolve {command} dependency {requested!r}",
                    )
                    continue
                if command in _SOURCE_COMMANDS:
                    if target in visiting:
                        diagnostic(
                            "dependency_cycle",
                            source,
                            command,
                            requested,
                            "LaTeX dependency cycle detected",
                        )
                    else:
                        visit(target)
                elif target not in bibliographies:
                    bibliographies.append(target)
        visiting.discard(source)

    for root in roots:
        visit(root)
    return LatexDependencyGraph(
        project=project,
        roots=roots,
        sources=tuple(sources),
        bibliographies=tuple(bibliographies),
        edges=tuple(edges),
        diagnostics=tuple(diagnostics),
    )


def discover_paper_sources(
    project_dir: str | Path, base_name: str | None = None
) -> tuple[Path, ...]:
    """Return every active LaTeX source reachable from supported paper roots."""

    return resolve_latex_dependency_graph(project_dir, base_name).sources


def discover_paper_dependencies(
    project_dir: str | Path, base_name: str | None = None
) -> tuple[Path, ...]:
    """Return active LaTeX sources and bibliography files in manifest order."""

    return resolve_latex_dependency_graph(project_dir, base_name).files


def primary_paper_source(
    project_dir: str | Path, base_name: str | None = None
) -> Path | None:
    project = Path(project_dir).resolve()
    roots = _discover_paper_roots(project, base_name)
    return roots[0] if roots else None


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
        lexical = Path(os.path.abspath(candidate))
        resolved = lexical.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        if _contained_regular_file(project, lexical):
            discovered.append(resolved)
    return tuple(discovered)
