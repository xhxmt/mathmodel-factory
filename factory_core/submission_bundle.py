from __future__ import annotations

import hashlib
import json
import os
import stat
import zipfile
from pathlib import Path
from typing import Any, Iterable

from .paper_sources import require_safe_latex_dependencies


SUBMISSION_BUNDLE_SCHEMA = "submission-bundle-manifest-v1"

SKIP_DIR_NAMES = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    ".runner.lock",
    "runner_snapshots",
    "source.mineru",
}
SKIP_SUFFIXES = {
    ".aux",
    ".bbl",
    ".blg",
    ".log",
    ".out",
    ".toc",
    ".fls",
    ".fdb_latexmk",
    ".synctex.gz",
    ".pyc",
}
TOP_LEVEL_FILES = {
    "abstract_draft.md",
    "assumption_ledger.md",
    "audit_issue_ledger.md",
    "chosen_method.md",
    "citation_audit.md",
    "claim_registry.json",
    "code_review.md",
    "derobotification.md",
    "evaluation.md",
    "method_decision.md",
    "model.md",
    "modeling_guide.md",
    "quality_contract.json",
    "research_brief.md",
    "review_comments.md",
    "revision_summary.md",
    "sensitivity_report.md",
    "solve_log.md",
    "symbol_table.md",
    "viability_gate.md",
    "viable_streams.md",
    "visualization_log.md",
}
INCLUDE_DIRS = {
    "data/raw",
    "figures",
    "models",
    "problem",
    "results",
    "scripts",
    "style",
    "tables",
}


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_regular_file(project: Path, path: Path, *, label: str) -> Path:
    lexical = Path(os.path.abspath(path))
    try:
        relative = lexical.relative_to(project)
    except ValueError as exc:
        raise ValueError(f"{label} escapes project: {path}") from exc
    cursor = project
    for component in relative.parts:
        cursor = cursor / component
        if cursor.is_symlink():
            raise ValueError(f"{label} is or traverses a symlink: {relative.as_posix()}")
    if not lexical.is_file():
        raise ValueError(f"{label} is missing or not a regular file: {relative.as_posix()}")
    resolved = lexical.resolve(strict=True)
    try:
        resolved.relative_to(project)
    except ValueError as exc:
        raise ValueError(f"{label} resolves outside project: {relative.as_posix()}") from exc
    return resolved


def _should_skip(path: Path, relative: Path) -> bool:
    if relative.parts[:2] == ("paper", "archive"):
        return True
    if any(part in SKIP_DIR_NAMES for part in relative.parts):
        return True
    if path.name.startswith(".runner") or path.name in {
        ".heartbeat",
        ".killed",
        ".review_state.json",
    }:
        return True
    if any(path.name.endswith(suffix) for suffix in SKIP_SUFFIXES):
        return True
    return path.name.endswith("~") or path.name.startswith(".#")


def declared_delivery_files(project: Path) -> set[str]:
    contract = project / "problem" / "deliverables.json"
    if not contract.is_file():
        return set()
    contract = _validate_regular_file(project, contract, label="deliverables contract")
    try:
        value = json.loads(contract.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid deliverables contract: {exc}") from exc
    attachments = value.get("attachments") if isinstance(value, dict) else None
    if not isinstance(attachments, list):
        raise ValueError("deliverables attachments must be an array")
    declared: set[str] = set()
    for index, attachment in enumerate(attachments):
        relative = attachment.get("file") if isinstance(attachment, dict) else None
        if not isinstance(relative, str) or not relative.strip():
            raise ValueError(f"deliverables attachment {index} has no file")
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"deliverables attachment escapes project: {relative}")
        resolved = _validate_regular_file(
            project, project / candidate, label=f"deliverables attachment {index}"
        )
        declared.add(resolved.relative_to(project).as_posix())
    return declared


def _walk_allowed_directory(project: Path, root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"submission include root is not a regular directory: {root.relative_to(project)}")
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        kept: list[str] = []
        for name in sorted(directories):
            child = current_path / name
            relative = child.relative_to(project)
            if name in SKIP_DIR_NAMES:
                continue
            if child.is_symlink():
                raise ValueError(
                    f"submission include directory is a symlink: {relative.as_posix()}"
                )
            kept.append(name)
        directories[:] = kept
        for name in sorted(names):
            path = current_path / name
            relative = path.relative_to(project)
            if _should_skip(path, relative):
                continue
            yield _validate_regular_file(project, path, label="submission member")


def submission_bundle_paths(
    project_dir: str | Path,
    base: str | None = None,
    *,
    require_pdf: bool = True,
) -> tuple[Path, ...]:
    project = Path(project_dir).resolve()
    resolved_base = base or project.name
    graph = require_safe_latex_dependencies(project, resolved_base)
    selected: set[Path] = set(graph.files)

    pdf_candidate = project / f"{resolved_base}_paper.pdf"
    if require_pdf:
        selected.add(
            _validate_regular_file(project, pdf_candidate, label="final PDF")
        )
    for name in TOP_LEVEL_FILES:
        candidate = project / name
        if candidate.exists() or candidate.is_symlink():
            selected.add(_validate_regular_file(project, candidate, label="top-level submission file"))
    for candidate in sorted(project.glob("m*")):
        if candidate.suffix.lower() in {".md", ".json", ".csv"}:
            selected.add(_validate_regular_file(project, candidate, label="model stream artifact"))
    for relative in sorted(INCLUDE_DIRS):
        selected.update(_walk_allowed_directory(project, project / relative))
    for relative in declared_delivery_files(project):
        selected.add(
            _validate_regular_file(project, project / relative, label="declared deliverable")
        )
    return tuple(sorted(selected, key=lambda path: path.relative_to(project).as_posix()))


def submission_bundle_manifest(
    project_dir: str | Path,
    base: str | None = None,
    *,
    require_pdf: bool = True,
) -> dict[str, Any]:
    project = Path(project_dir).resolve()
    resolved_base = base or project.name
    members = [
        {
            "source_path": path.relative_to(project).as_posix(),
            "archive_path": path.relative_to(project).as_posix(),
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in submission_bundle_paths(
            project, resolved_base, require_pdf=require_pdf
        )
    ]
    identity = {
        "schema_version": SUBMISSION_BUNDLE_SCHEMA,
        "base": resolved_base,
        "pdf_required": require_pdf,
        "members": members,
    }
    return {**identity, "manifest_sha256": _canonical_hash(identity)}


def verify_zip_against_manifest(zip_path: str | Path, manifest: dict[str, Any]) -> None:
    expected_items = manifest.get("members")
    if not isinstance(expected_items, list):
        raise ValueError("submission bundle manifest members are invalid")
    expected = {str(item["archive_path"]): item for item in expected_items}
    if len(expected) != len(expected_items):
        raise ValueError("submission bundle manifest contains duplicate archive paths")
    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(names) != len(set(names)):
            raise ValueError("submission ZIP contains duplicate members")
        if set(names) != set(expected):
            extra = sorted(set(names) - set(expected))
            missing = sorted(set(expected) - set(names))
            raise ValueError(
                f"submission ZIP member mismatch: extra={extra}, missing={missing}"
            )
        for info in infos:
            relative = Path(info.filename)
            mode = (info.external_attr >> 16) & 0o170000
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or "\\" in info.filename
                or info.is_dir()
                or mode == stat.S_IFLNK
            ):
                raise ValueError(f"unsafe submission ZIP member: {info.filename}")
            digest = hashlib.sha256()
            size = 0
            with archive.open(info) as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
                    size += len(block)
            record = expected[info.filename]
            if size != record.get("size") or digest.hexdigest() != record.get("sha256"):
                raise ValueError(f"submission ZIP member content mismatch: {info.filename}")
