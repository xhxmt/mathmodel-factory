from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .audit.persistence import atomic_write_json, utc_now
from .paper_sources import require_safe_latex_dependencies


BIBLIOGRAPHY_RECEIPT_SCHEMA = "bibliography-build-receipt-v1"
BIBLIOGRAPHY_RECEIPT_PATH = Path(
    "logs/compilation/bibliography_build_receipt.json"
)

_UNRESOLVED_PATTERNS = (
    re.compile(r"citation\s+[`'][^\n]+[`']\s+.*undefined", re.IGNORECASE),
    re.compile(r"there were undefined (?:references|citations)", re.IGNORECASE),
    re.compile(r"please\s+\(?(?:re)?run\)?\s+(?:biber|bibtex)", re.IGNORECASE),
    re.compile(r"empty bibliography", re.IGNORECASE),
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _record(project: Path, path: Path, *, required: bool = True) -> dict[str, Any]:
    lexical = path if path.is_absolute() else project / path
    try:
        resolved = lexical.resolve(strict=True)
        relative = resolved.relative_to(project).as_posix()
    except (OSError, ValueError):
        if required:
            raise ValueError(f"bibliography evidence is missing or unsafe: {path}")
        return {"path": str(path), "exists": False}
    if lexical.is_symlink() or not resolved.is_file():
        raise ValueError(f"bibliography evidence is not a regular file: {relative}")
    return {
        "path": relative,
        "exists": True,
        "size": resolved.stat().st_size,
        "sha256": _sha256(resolved),
    }


def _unresolved_messages(*paths: Path) -> list[str]:
    messages: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if any(pattern.search(line) for pattern in _UNRESOLVED_PATTERNS):
                normalized = line.strip()
                if normalized and normalized not in messages:
                    messages.append(normalized)
    return messages


def build_bibliography_receipt(
    project_dir: str | Path,
    base: str | None = None,
    *,
    backend: str,
    backend_version: str,
    backend_log: str | Path,
    final_log: str | Path,
) -> dict[str, Any]:
    project = Path(project_dir).resolve()
    resolved_base = base or project.name
    graph = require_safe_latex_dependencies(project, resolved_base)
    expected_backend = graph.contract.bibliography_backend
    if backend != expected_backend:
        raise ValueError(
            f"bibliography backend mismatch: expected {expected_backend}, got {backend}"
        )
    backend_log_path = Path(backend_log)
    final_log_path = Path(final_log)
    if not backend_log_path.is_absolute():
        backend_log_path = project / backend_log_path
    if not final_log_path.is_absolute():
        final_log_path = project / final_log_path
    unresolved = _unresolved_messages(backend_log_path, final_log_path)
    if unresolved:
        raise ValueError(
            "unresolved bibliography/reference diagnostics: " + "; ".join(unresolved[:8])
        )

    control_suffix = "bcf" if backend == "biber" else "aux"
    control_path = project / "logs" / "compilation" / f"pass1.{control_suffix}"
    bbl_path = project / f"{graph.contract.job_name}.bbl"
    backend_required = backend in {"bibtex", "biber"}
    if backend_required and not backend_version.strip():
        raise ValueError("bibliography backend version is missing")
    receipt: dict[str, Any] = {
        "schema_version": BIBLIOGRAPHY_RECEIPT_SCHEMA,
        "created_at": utc_now(),
        "base": resolved_base,
        "backend": backend,
        "backend_version": backend_version.strip() if backend_required else None,
        "dependency_manifest_sha256": _canonical_hash(graph.manifest()),
        "control_input": (
            _record(project, control_path) if backend_required else None
        ),
        "bib_input_records": [
            _record(project, path) for path in graph.bibliographies
        ],
        "bst_input_records": [
            _record(project, path) for path in graph.bibliography_styles
        ],
        "generated_bbl": _record(project, bbl_path) if backend_required else None,
        "backend_log": (
            _record(project, backend_log_path) if backend_required else None
        ),
        "final_log": _record(project, final_log_path),
        "warnings": [],
        "unresolved_citations": unresolved,
        "status": "PASS",
    }
    unsigned = dict(receipt)
    receipt["content_sha256"] = _canonical_hash(unsigned)
    atomic_write_json(project / BIBLIOGRAPHY_RECEIPT_PATH, receipt)
    return receipt


def verify_bibliography_receipt(
    project_dir: str | Path, base: str | None = None
) -> tuple[bool, list[str], dict[str, Any] | None]:
    project = Path(project_dir).resolve()
    resolved_base = base or project.name
    path = project / BIBLIOGRAPHY_RECEIPT_PATH
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, ["bibliography build receipt is missing or invalid"], None
    if not isinstance(receipt, dict):
        return False, ["bibliography build receipt root is invalid"], None
    errors: list[str] = []
    unsigned = dict(receipt)
    declared_hash = unsigned.pop("content_sha256", None)
    if declared_hash != _canonical_hash(unsigned):
        errors.append("bibliography receipt content hash mismatch")
    if receipt.get("schema_version") != BIBLIOGRAPHY_RECEIPT_SCHEMA:
        errors.append("bibliography receipt schema mismatch")
    if receipt.get("base") != resolved_base:
        errors.append("bibliography receipt base mismatch")
    try:
        graph = require_safe_latex_dependencies(project, resolved_base)
        if receipt.get("backend") != graph.contract.bibliography_backend:
            errors.append("bibliography backend changed")
        if receipt.get("dependency_manifest_sha256") != _canonical_hash(
            graph.manifest()
        ):
            errors.append("bibliography dependency manifest changed")
        expected_bib = [_record(project, item) for item in graph.bibliographies]
        expected_bst = [
            _record(project, item) for item in graph.bibliography_styles
        ]
        if receipt.get("bib_input_records") != expected_bib:
            errors.append("bibliography inputs changed")
        if receipt.get("bst_input_records") != expected_bst:
            errors.append("bibliography style inputs changed")
    except (OSError, ValueError) as exc:
        errors.append(f"bibliography dependency verification failed: {exc}")

    for field in ("control_input", "generated_bbl", "backend_log", "final_log"):
        record = receipt.get(field)
        if record is None:
            if field in {"control_input", "generated_bbl", "backend_log"} and receipt.get(
                "backend"
            ) == "none":
                continue
            errors.append(f"bibliography receipt {field} is missing")
            continue
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            errors.append(f"bibliography receipt {field} is invalid")
            continue
        try:
            current = _record(project, Path(str(record["path"])))
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
            continue
        if current != record:
            errors.append(f"bibliography evidence changed: {record['path']}")
    unresolved = _unresolved_messages(
        project / str((receipt.get("backend_log") or {}).get("path") or ""),
        project / str((receipt.get("final_log") or {}).get("path") or ""),
    )
    if unresolved or receipt.get("unresolved_citations") not in ([], None):
        errors.append("bibliography receipt contains unresolved citations")
    if receipt.get("status") != "PASS":
        errors.append("bibliography receipt status is not PASS")
    return not errors, errors, receipt


def bibliography_evidence_record(
    project_dir: str | Path, base: str | None = None
) -> dict[str, Any]:
    project = Path(project_dir).resolve()
    valid, errors, receipt = verify_bibliography_receipt(project, base)
    path = project / BIBLIOGRAPHY_RECEIPT_PATH
    return {
        "path": BIBLIOGRAPHY_RECEIPT_PATH.as_posix(),
        "exists": path.is_file(),
        "size": path.stat().st_size if path.is_file() else 0,
        "sha256": _sha256(path) if path.is_file() else None,
        "valid": valid,
        "errors": errors,
        "receipt_content_sha256": (
            receipt.get("content_sha256") if isinstance(receipt, dict) else None
        ),
        "backend": receipt.get("backend") if isinstance(receipt, dict) else None,
        "generated_bbl": (
            receipt.get("generated_bbl") if isinstance(receipt, dict) else None
        ),
    }
