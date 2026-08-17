from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from scripts.solver_job_receipt import (
    ReceiptError,
    SUBMISSION_SCHEMA,
    file_sha256,
    read_receipt,
)

SOLVER_INPUT_EXCLUSION_SCHEMA = "factory-solver-input-exclusion-v1"


@dataclass(frozen=True)
class SolverInputCoverage:
    included_paths: tuple[Path, ...]
    evidence_paths: tuple[Path, ...]
    excluded: tuple[dict[str, Any], ...]


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _regular_project_file(project: Path, relative: str, *, label: str) -> Path:
    pure = PurePosixPath(relative.replace("\\", "/"))
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise ValueError(f"{label} escapes project: {relative}")
    lexical = Path(os.path.abspath(project / Path(*pure.parts)))
    try:
        lexical.relative_to(project)
    except ValueError as exc:
        raise ValueError(f"{label} escapes project: {relative}") from exc
    cursor = project
    for component in pure.parts:
        cursor = cursor / component
        if cursor.is_symlink():
            raise ValueError(f"{label} is or traverses a symlink: {pure.as_posix()}")
    if not lexical.is_file():
        raise ValueError(f"{label} is missing or not a regular file: {pure.as_posix()}")
    resolved = lexical.resolve(strict=True)
    try:
        resolved.relative_to(project)
    except ValueError as exc:
        raise ValueError(f"{label} resolves outside project: {pure.as_posix()}") from exc
    return resolved


def solver_input_exclusion_path(
    project_dir: str | Path, *, relative_path: str, input_sha256: str
) -> Path:
    project = Path(project_dir).resolve()
    key = _canonical_hash(
        {
            "path": relative_path,
            "input_sha256": input_sha256,
            "scope": "final_input_and_submission",
        }
    )
    path = (
        project
        / ".factory"
        / "finalization"
        / "input_exclusions"
        / f"{key}.json"
    )
    cursor = project
    for component in path.relative_to(project).parts[:-1]:
        cursor = cursor / component
        if cursor.is_symlink():
            raise ValueError(
                "solver input exclusion path traverses a symlink"
            )
    if path.is_symlink():
        raise ValueError("solver input exclusion receipt must not be a symlink")
    return path


def _validate_exclusion_value(
    value: Mapping[str, Any], input_record: Mapping[str, Any]
) -> dict[str, Any]:
    normalized = dict(value)
    identity = {
        key: item
        for key, item in normalized.items()
        if key != "content_sha256"
    }
    relative = str(input_record.get("path") or "")
    if normalized.get("schema_version") != SOLVER_INPUT_EXCLUSION_SCHEMA:
        raise ValueError("solver input exclusion receipt schema is invalid")
    if normalized.get("content_sha256") != _canonical_hash(identity):
        raise ValueError("solver input exclusion receipt content hash mismatch")
    if str(normalized.get("path") or "") != relative:
        raise ValueError("solver input exclusion receipt path mismatch")
    if str(normalized.get("input_sha256") or "") != str(
        input_record.get("sha256") or ""
    ):
        raise ValueError("solver input exclusion receipt hash mismatch")
    if str(normalized.get("scope") or "") != "final_input_and_submission":
        raise ValueError("solver input exclusion receipt scope is invalid")
    if not str(normalized.get("reason") or "").strip():
        raise ValueError("solver input exclusion receipt requires a reason")
    return normalized


def _read_exclusion(
    project: Path, input_record: dict[str, Any]
) -> tuple[dict[str, Any], Path] | None:
    relative = str(input_record.get("path") or "")
    path = solver_input_exclusion_path(
        project,
        relative_path=relative,
        input_sha256=str(input_record.get("sha256") or ""),
    )
    if not path.exists():
        return None
    exclusion_path = _regular_project_file(
        project,
        path.relative_to(project).as_posix(),
        label="solver input exclusion receipt",
    )
    try:
        value = json.loads(exclusion_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid solver input exclusion receipt: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("solver input exclusion receipt root must be an object")
    return _validate_exclusion_value(value, input_record), exclusion_path


def solver_declared_input_coverage(project_dir: str | Path) -> SolverInputCoverage:
    project = Path(project_dir).resolve()
    root = project / ".factory" / "solver_receipts"
    if not root.exists():
        return SolverInputCoverage((), (), ())
    if root.is_symlink() or not root.is_dir():
        raise ValueError("solver receipt directory is unsafe")
    included: dict[str, Path] = {}
    evidence: dict[str, Path] = {}
    excluded: dict[str, dict[str, Any]] = {}
    for receipt_path in sorted(root.glob("*.submitted.json")):
        relative_receipt = receipt_path.relative_to(project).as_posix()
        safe_receipt = _regular_project_file(
            project, relative_receipt, label="solver submission receipt"
        )
        try:
            receipt = read_receipt(safe_receipt, SUBMISSION_SCHEMA)
        except (OSError, ReceiptError) as exc:
            raise ValueError(f"invalid solver submission receipt {relative_receipt}: {exc}") from exc
        evidence[relative_receipt] = safe_receipt
        inputs = receipt.get("inputs")
        if not isinstance(inputs, list):
            raise ValueError(f"solver submission receipt inputs are invalid: {relative_receipt}")
        for index, record in enumerate(inputs):
            if not isinstance(record, dict):
                raise ValueError(
                    f"solver input record {index} is invalid in {relative_receipt}"
                )
            relative = str(record.get("path") or "")
            path = _regular_project_file(
                project, relative, label=f"solver input {index} from {relative_receipt}"
            )
            if path.stat().st_size != int(record.get("size", -1)):
                raise ValueError(f"solver input size drift: {relative}")
            if file_sha256(path) != str(record.get("sha256") or ""):
                raise ValueError(f"solver input content drift: {relative}")
            prior = included.get(relative)
            if prior is not None and prior != path:
                raise ValueError(f"solver input identity conflict: {relative}")
            exclusion = _read_exclusion(project, record)
            if exclusion is not None:
                value, exclusion_path = exclusion
                excluded[relative] = value
                evidence[exclusion_path.relative_to(project).as_posix()] = exclusion_path
                included.pop(relative, None)
                continue
            if relative in excluded:
                raise ValueError(f"solver input has conflicting inclusion and exclusion: {relative}")
            included[relative] = path
    return SolverInputCoverage(
        tuple(included[key] for key in sorted(included)),
        tuple(evidence[key] for key in sorted(evidence)),
        tuple(excluded[key] for key in sorted(excluded)),
    )


def build_solver_input_exclusion_receipt(
    *,
    relative_path: str,
    input_sha256: str,
    reason: str,
    scope: str = "final_input_and_submission",
) -> dict[str, Any]:
    identity = {
        "schema_version": SOLVER_INPUT_EXCLUSION_SCHEMA,
        "path": relative_path,
        "input_sha256": input_sha256,
        "reason": reason.strip(),
        "scope": scope,
    }
    if not identity["reason"]:
        raise ValueError("solver input exclusion receipt requires a reason")
    if scope != "final_input_and_submission":
        raise ValueError("solver input exclusion receipt scope is invalid")
    return {**identity, "content_sha256": _canonical_hash(identity)}

def write_solver_input_exclusion_receipt(
    project_dir: str | Path, receipt: Mapping[str, Any]
) -> Path:
    """Persist one versioned exclusion using atomic no-overwrite semantics."""

    project = Path(project_dir).resolve()
    relative = str(receipt.get("path") or "")
    input_sha256 = str(receipt.get("input_sha256") or "")
    value = _validate_exclusion_value(
        receipt, {"path": relative, "sha256": input_sha256}
    )
    path = solver_input_exclusion_path(
        project, relative_path=relative, input_sha256=input_sha256
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    for cursor in (path.parent,):
        if cursor.is_symlink() or not cursor.is_dir():
            raise ValueError("solver input exclusion directory is unsafe")
    encoded = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file():
            raise ValueError("solver input exclusion receipt path is unsafe")
        if path.read_bytes() != encoded:
            raise ValueError(
                "immutable solver input exclusion receipt already differs"
            )
        return path
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            directory = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory = None
        if directory is not None:
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path
