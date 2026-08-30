#!/usr/bin/env python3
"""Central path and file-type policy for frozen controller evidence payloads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class PayloadPolicyFinding:
    path: str
    rule: str


_DENIED_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".vite",
        "__pycache__",
        "dist",
        "node_modules",
    }
)
_DENIED_TOP_LEVEL = frozenset({"audit_artifacts"})
_DENIED_BASENAMES = frozenset({".phase6_patch_probe", "xhxmt.github.io"})
_DENIED_FILENAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        ".env.test",
    }
)
_DATABASE_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3"})
_DATABASE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _database_state_basename(basename: str) -> bool:
    return any(
        basename.endswith(database_suffix + sidecar_suffix)
        for database_suffix in _DATABASE_SUFFIXES
        for sidecar_suffix in ("", *_DATABASE_SIDECAR_SUFFIXES)
    )


def payload_path_finding(path: str) -> PayloadPolicyFinding | None:
    """Return a stable path-only denial without opening the referenced file."""

    # Inventories normally use POSIX separators, but the policy is also called
    # before archive canonicalization.  Normalize both path dialects here so a
    # Windows spelling cannot make a SQLite sidecar look like an extensionless
    # POSIX basename.  Policy comparisons are deliberately case-insensitive.
    normalized_path = path.replace("\\", "/")
    candidate = PurePosixPath(normalized_path)
    parts = candidate.parts
    folded_parts = tuple(part.casefold() for part in parts)
    basename = folded_parts[-1] if folded_parts else ""
    windows_drive_path = bool(
        parts
        and len(parts[0]) == 2
        and parts[0][0].isalpha()
        and parts[0][1] == ":"
    )
    if not path or candidate.is_absolute() or ".." in parts:
        return PayloadPolicyFinding(path=path, rule="unsafe_archive_path")
    if folded_parts and folded_parts[0] in _DENIED_TOP_LEVEL:
        return PayloadPolicyFinding(path=path, rule="generated_evidence_path")
    if any(part in _DENIED_PARTS for part in folded_parts):
        return PayloadPolicyFinding(path=path, rule="dependency_or_cache_path")
    if basename in _DENIED_BASENAMES:
        return PayloadPolicyFinding(path=path, rule="non_payload_path")
    if basename.endswith((".pyc", ".pyo")):
        return PayloadPolicyFinding(path=path, rule="dependency_or_cache_path")
    if basename.startswith(".put-"):
        return PayloadPolicyFinding(path=path, rule="cas_temporary_state")
    if basename in _DENIED_FILENAMES or (
        basename.startswith(".env.") and basename != ".env.example"
    ):
        return PayloadPolicyFinding(path=path, rule="environment_credential_file")
    if ".claude" in folded_parts and basename.endswith(".lock"):
        return PayloadPolicyFinding(path=path, rule="runtime_lock_state")
    if _database_state_basename(basename):
        return PayloadPolicyFinding(path=path, rule="database_state")
    if windows_drive_path:
        return PayloadPolicyFinding(path=path, rule="unsafe_archive_path")
    if "browser_state" in folded_parts or "playwright-report" in folded_parts:
        return PayloadPolicyFinding(path=path, rule="browser_runtime_state")
    return None


class PayloadPolicyEvaluationError(RuntimeError):
    """The central path policy rejected a path or could not evaluate it."""


def evaluate_payload_path(path: str) -> PayloadPolicyFinding | None:
    """Evaluate one path and turn policy implementation errors into rejection."""

    try:
        return payload_path_finding(path)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as error:
        raise PayloadPolicyEvaluationError(
            f"payload path policy failed closed for {path!r}"
        ) from error


def require_payload_path_allowed(path: str) -> None:
    """Reject a path before callers perform any payload filesystem I/O."""

    finding = evaluate_payload_path(path)
    if finding is not None:
        raise PayloadPolicyEvaluationError(
            "payload path rejected before file I/O: "
            f"{path!r} ({finding.rule})"
        )
