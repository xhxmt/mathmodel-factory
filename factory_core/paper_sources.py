from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


LATEX_COMPILE_CONTRACT_SCHEMA = "factory-latex-compile-contract-v1"
LATEX_DEPENDENCY_SCHEMA = "factory-latex-dependency-graph-v2"
LATEX_EXPANDED_DOCUMENT_SCHEMA = "factory-latex-expanded-document-v1"

_DEPENDENCY_RE = re.compile(
    r"\\(?P<command>input|include|subfile|bibliography|addbibresource|"
    r"includegraphics|lstinputlisting|usepackage|RequirePackage|documentclass)"
    r"\s*(?:\[[^\]]*\]\s*)?\{(?P<target>[^{}]+)\}",
    re.IGNORECASE,
)
_SOURCE_COMMANDS = {"input", "include", "subfile"}
_BIBLIOGRAPHY_COMMANDS = {"bibliography", "addbibresource"}
_OPTIONAL_EXTERNAL_COMMANDS = {"usepackage", "requirepackage", "documentclass"}
_RESOURCE_SUFFIXES = {
    "includegraphics": ("", ".pdf", ".png", ".jpg", ".jpeg", ".eps"),
    "lstinputlisting": ("",),
    "usepackage": (".sty",),
    "requirepackage": (".sty",),
    "documentclass": (".cls",),
}
_GENERATED_INPUT_SUFFIXES = {
    ".aux",
    ".bbl",
    ".blg",
    ".fls",
    ".fdb_latexmk",
    ".log",
    ".out",
    ".toc",
    ".synctex.gz",
}


class LatexDependencyError(ValueError):
    """Raised when the paper input identity cannot be determined safely."""

    def __init__(self, diagnostics: Iterable["LatexDependencyDiagnostic"]):
        self.diagnostics = tuple(diagnostics)
        detail = "; ".join(
            f"{item.code}:{item.source.name}:{item.requested}"
            for item in self.diagnostics[:8]
        )
        super().__init__(f"unsafe LaTeX dependency graph: {detail}")


@dataclass(frozen=True)
class LatexCompileContract:
    project: Path
    working_directory: Path
    root_source: Path | None
    search_roots: tuple[Path, ...]
    engine: str
    job_name: str

    def manifest(self) -> dict[str, Any]:
        def relative(path: Path | None) -> str | None:
            if path is None:
                return None
            return path.relative_to(self.project).as_posix()

        return {
            "schema_version": LATEX_COMPILE_CONTRACT_SCHEMA,
            "working_directory": relative(self.working_directory),
            "root_source": relative(self.root_source),
            "search_roots": [relative(path) or "." for path in self.search_roots],
            "engine": self.engine,
            "job_name": self.job_name,
            "dependency_policy": "declared-project-inputs-equal-recorder-inputs",
        }


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
    contract: LatexCompileContract
    roots: tuple[Path, ...]
    sources: tuple[Path, ...]
    bibliographies: tuple[Path, ...]
    resources: tuple[Path, ...]
    edges: tuple[LatexDependencyEdge, ...]
    diagnostics: tuple[LatexDependencyDiagnostic, ...]

    @property
    def files(self) -> tuple[Path, ...]:
        return _unique_contained_files(
            self.project, (*self.sources, *self.bibliographies, *self.resources)
        )

    @property
    def declared_compile_inputs(self) -> tuple[Path, ...]:
        # BibTeX reads .bib files in a separate process, so they do not appear in
        # the TeX engine's .fls recorder output.
        return _unique_contained_files(self.project, (*self.sources, *self.resources))

    def manifest(self) -> dict[str, Any]:
        return {
            "schema_version": LATEX_DEPENDENCY_SCHEMA,
            "compile_contract": self.contract.manifest(),
            "roots": [path.relative_to(self.project).as_posix() for path in self.roots],
            "sources": [
                path.relative_to(self.project).as_posix() for path in self.sources
            ],
            "bibliographies": [
                path.relative_to(self.project).as_posix()
                for path in self.bibliographies
            ],
            "resources": [
                path.relative_to(self.project).as_posix() for path in self.resources
            ],
            "edges": [edge.to_dict(self.project) for edge in self.edges],
            "diagnostics": [
                diagnostic.to_dict(self.project) for diagnostic in self.diagnostics
            ],
        }


@dataclass(frozen=True)
class ExpandedLatexLine:
    source: Path
    source_line: int
    text: str


@dataclass(frozen=True)
class ExpandedLatexDocument:
    project: Path
    graph: LatexDependencyGraph
    lines: tuple[ExpandedLatexLine, ...]

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    def manifest(self) -> dict[str, Any]:
        records = [
            {
                "expanded_line": index,
                "source": line.source.relative_to(self.project).as_posix(),
                "source_line": line.source_line,
            }
            for index, line in enumerate(self.lines, start=1)
        ]
        identity = {
            "schema_version": LATEX_EXPANDED_DOCUMENT_SCHEMA,
            "dependency_graph": self.graph.manifest(),
            "line_map": records,
            "text_sha256": hashlib.sha256(self.text.encode("utf-8")).hexdigest(),
        }
        identity["manifest_sha256"] = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return identity


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


def _mask_comments(text: str) -> str:
    """Mask comments without changing offsets used for dependency expansion."""

    return "\n".join(
        re.sub(
            r"(?<!\\)%.*$",
            lambda match: " " * len(match.group(0)),
            line,
        )
        for line in text.split("\n")
    )


def _engine_for_source(source: Path | None) -> str:
    if source is None:
        return "pdflatex"
    try:
        text = source.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "pdflatex"
    if re.search(
        r"\\documentclass\s*(?:\[[^]]*\])?\s*\{(?:ctex|cumcmthesis|mcmthesis)",
        text,
    ) or r"\usepackage{xeCJK}" in text:
        return "xelatex"
    return "pdflatex"


def latex_compile_contract(
    project_dir: str | Path, base_name: str | None = None
) -> LatexCompileContract:
    project = Path(project_dir).resolve()
    roots = _discover_paper_roots(project, base_name)
    root = roots[0] if roots else None
    search_roots: list[Path] = []
    for candidate in ((root.parent if root is not None else project), project):
        if candidate not in search_roots:
            search_roots.append(candidate)
    base = base_name or project.name
    return LatexCompileContract(
        project=project,
        working_directory=project,
        root_source=root,
        search_roots=tuple(search_roots),
        engine=_engine_for_source(root),
        job_name=f"{base}_paper",
    )


def _dependency_candidates(
    contract: LatexCompileContract,
    requested: str,
    *,
    suffixes: tuple[str, ...],
) -> tuple[Path, ...]:
    value = requested.strip().strip('"').strip("'")
    raw = Path(value)
    if raw.is_absolute() or not value or "\\" in value or "#" in value:
        return ()
    variants: list[Path] = []
    if raw.suffix:
        variants.append(raw)
    else:
        variants.extend(raw if not suffix else raw.with_suffix(suffix) for suffix in suffixes)
    candidates: list[Path] = []
    for base in contract.search_roots:
        for variant in variants:
            candidate = Path(os.path.abspath(base / variant))
            try:
                candidate.relative_to(contract.project)
            except ValueError:
                continue
            if candidate not in candidates:
                candidates.append(candidate)
    return tuple(candidates)


def _resolve_dependency(
    contract: LatexCompileContract,
    requested: str,
    *,
    suffixes: tuple[str, ...],
) -> Path | None:
    for candidate in _dependency_candidates(contract, requested, suffixes=suffixes):
        if _contained_regular_file(contract.project, candidate):
            return candidate.resolve(strict=True)
    return None


def _command_suffixes(command: str) -> tuple[str, ...]:
    if command in _SOURCE_COMMANDS:
        return (".tex",)
    if command in _BIBLIOGRAPHY_COMMANDS:
        return (".bib",)
    return _RESOURCE_SUFFIXES.get(command, ("",))


def resolve_latex_dependency_graph(
    project_dir: str | Path, base_name: str | None = None
) -> LatexDependencyGraph:
    """Resolve project-local inputs using the same fixed search order as TeX.

    Nested commands never change the search root.  The main source directory is
    searched first, followed by the project working directory, matching
    ``compile_paper.sh``'s ``TEXINPUTS`` contract.
    """

    project = Path(project_dir).resolve()
    contract = latex_compile_contract(project, base_name)
    roots = (contract.root_source,) if contract.root_source is not None else ()
    sources: list[Path] = []
    bibliographies: list[Path] = []
    resources: list[Path] = []
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
            text = _mask_comments(
                source.read_text(encoding="utf-8", errors="replace")
            )
        except OSError as exc:
            diagnostic("unreadable", source, "read", source.name, str(exc))
            visiting.discard(source)
            return
        for match in _DEPENDENCY_RE.finditer(text):
            command = match.group("command").lower()
            requested_values = (
                match.group("target").split(",")
                if command in {"bibliography", "usepackage", "requirepackage"}
                else (match.group("target"),)
            )
            for requested_value in requested_values:
                requested = requested_value.strip()
                target = _resolve_dependency(
                    contract,
                    requested,
                    suffixes=_command_suffixes(command),
                )
                if command in _OPTIONAL_EXTERNAL_COMMANDS and target is None:
                    # System classes and packages are outside the project identity.
                    continue
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
                elif command in _BIBLIOGRAPHY_COMMANDS:
                    if target not in bibliographies:
                        bibliographies.append(target)
                elif target not in resources:
                    resources.append(target)
        visiting.discard(source)

    for root in roots:
        visit(root)
    return LatexDependencyGraph(
        project=project,
        contract=contract,
        roots=roots,
        sources=tuple(sources),
        bibliographies=tuple(bibliographies),
        resources=tuple(resources),
        edges=tuple(edges),
        diagnostics=tuple(diagnostics),
    )


def require_safe_latex_dependencies(
    project_dir: str | Path,
    base_name: str | None = None,
) -> LatexDependencyGraph:
    graph = resolve_latex_dependency_graph(project_dir, base_name)
    if not graph.roots:
        source = Path(project_dir).resolve()
        diagnostic = LatexDependencyDiagnostic(
            "missing_root", source, "document", "", "No active LaTeX root found"
        )
        raise LatexDependencyError((diagnostic,))
    if graph.diagnostics:
        raise LatexDependencyError(graph.diagnostics)
    return graph


def expand_latex_document(
    project_dir: str | Path, base_name: str | None = None
) -> ExpandedLatexDocument:
    """Expand input/include/subfile at command position with source locations."""

    graph = require_safe_latex_dependencies(project_dir, base_name)
    edge_targets: dict[tuple[Path, str, str], list[Path]] = {}
    for edge in graph.edges:
        if edge.target is not None and edge.command in _SOURCE_COMMANDS:
            edge_targets.setdefault(
                (edge.source, edge.command, edge.requested), []
            ).append(edge.target)
    edge_offsets: dict[tuple[Path, str, str], int] = {}
    expanded: list[ExpandedLatexLine] = []

    def emit(source: Path, source_line: int, text: str) -> None:
        # Empty structural fragments are not useful, but genuine blank lines
        # retain document ordering and stable expanded line coordinates.
        expanded.append(ExpandedLatexLine(source, source_line, text))

    def visit(source: Path) -> None:
        text = source.read_text(encoding="utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            masked = _mask_comments(line)
            cursor = 0
            matched_source_command = False
            for match in _DEPENDENCY_RE.finditer(masked):
                command = match.group("command").lower()
                if command not in _SOURCE_COMMANDS:
                    continue
                prefix = line[cursor : match.start()]
                if prefix:
                    emit(source, line_number, prefix)
                requested = match.group("target").strip()
                key = (source, command, requested)
                offset = edge_offsets.get(key, 0)
                targets = edge_targets.get(key, [])
                if offset >= len(targets):  # pragma: no cover - guarded graph invariant
                    raise LatexDependencyError(
                        (
                            LatexDependencyDiagnostic(
                                "expansion_mismatch",
                                source,
                                command,
                                requested,
                                "Dependency edge missing during expansion",
                            ),
                        )
                    )
                edge_offsets[key] = offset + 1
                visit(targets[offset])
                cursor = match.end()
                matched_source_command = True
            suffix = line[cursor:]
            if suffix or not matched_source_command:
                emit(source, line_number, suffix)
    visit(graph.roots[0])
    return ExpandedLatexDocument(graph.project, graph, tuple(expanded))


def observed_latex_inputs(
    project_dir: str | Path, fls_path: str | Path
) -> tuple[Path, ...]:
    """Return ordinary project inputs recorded by TeX's ``-recorder`` output."""

    project = Path(project_dir).resolve()
    fls = Path(fls_path)
    observed: list[Path] = []
    for raw_line in fls.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw_line.startswith("INPUT "):
            continue
        raw_path = raw_line[6:].strip()
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = project / candidate
        lexical = Path(os.path.abspath(candidate))
        try:
            lexical.relative_to(project)
        except ValueError:
            continue
        if any(
            lexical.name.endswith(suffix) for suffix in _GENERATED_INPUT_SUFFIXES
        ):
            continue
        if lexical.suffix.lower() in {".pdf"} and lexical.name.endswith("_paper.pdf"):
            continue
        if not _contained_regular_file(project, lexical):
            continue
        resolved = lexical.resolve(strict=True)
        if resolved not in observed:
            observed.append(resolved)
    return tuple(sorted(observed, key=lambda path: path.relative_to(project).as_posix()))


def verify_latex_recorder_inputs(
    project_dir: str | Path,
    base_name: str | None,
    fls_path: str | Path,
) -> dict[str, Any]:
    graph = require_safe_latex_dependencies(project_dir, base_name)
    project = graph.project
    declared = set(graph.declared_compile_inputs)
    observed = set(observed_latex_inputs(project, fls_path))
    undeclared = sorted(observed - declared, key=lambda path: path.relative_to(project).as_posix())
    unread = sorted(declared - observed, key=lambda path: path.relative_to(project).as_posix())
    payload = {
        "schema_version": "factory-latex-recorder-verification-v1",
        "compile_contract": graph.contract.manifest(),
        "dependency_manifest_sha256": hashlib.sha256(
            json.dumps(graph.manifest(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "declared_latex_inputs": [
            path.relative_to(project).as_posix() for path in sorted(declared)
        ],
        "observed_latex_inputs": [
            path.relative_to(project).as_posix() for path in sorted(observed)
        ],
        "undeclared_inputs": [path.relative_to(project).as_posix() for path in undeclared],
        "declared_but_unread": [path.relative_to(project).as_posix() for path in unread],
        "status": "PASS" if not undeclared and not unread else "FAIL",
    }
    if payload["status"] != "PASS":
        raise ValueError(
            "LaTeX recorder input mismatch: "
            f"undeclared={payload['undeclared_inputs']}, "
            f"unread={payload['declared_but_unread']}"
        )
    return payload


def discover_paper_sources(
    project_dir: str | Path, base_name: str | None = None
) -> tuple[Path, ...]:
    """Return every active LaTeX source reachable from supported paper roots."""

    return resolve_latex_dependency_graph(project_dir, base_name).sources


def discover_paper_dependencies(
    project_dir: str | Path, base_name: str | None = None
) -> tuple[Path, ...]:
    """Return all active, project-local LaTeX inputs in manifest order."""

    return resolve_latex_dependency_graph(project_dir, base_name).files


def primary_paper_source(
    project_dir: str | Path, base_name: str | None = None
) -> Path | None:
    return latex_compile_contract(project_dir, base_name).root_source


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
