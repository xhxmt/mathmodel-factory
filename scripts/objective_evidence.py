#!/usr/bin/env python3
"""Small, independent evidence primitives for objective workflow checks.

This module intentionally does not import or modify any of the existing paper
checkers.  It gives callers one conservative representation for machine
observations while keeping three things separate:

* a deterministic observation (``PASS``/``FAIL``/``WARN``/``UNKNOWN``/``SKIP``),
* the strength and independence of the observation, and
* the policy used when observations are combined into a delivery decision.

The module is deliberately *not* an award predictor.  A heuristic finding can
be useful to an auditor, but it cannot become a hard veto merely because a
caller requested ``severity=hard_veto``.  Only an explicitly qualified
``factory_oracle`` or ``dual_impl`` finding with an exact contradiction is
allowed to remain a hard veto.

The public API is side-effect free except for ``run_safe_command``.  That
function is opt-in, never invokes a shell, requires an allow-listed root or
script, uses a bounded timeout, and returns a value rather than writing a
report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence


SCHEMA_VERSION = "objective-evidence-v1"

STATUS_VALUES = frozenset({"PASS", "FAIL", "WARN", "UNKNOWN", "SKIP"})
SEVERITY_VALUES = frozenset({"hard_veto", "soft_alert", "info"})
TRUST_LEVELS = frozenset(
    {
        "factory_oracle",
        "dual_impl",
        "project_test",
        "self_report",
        "heuristic",
        "infrastructure",
    }
)
APPLICABILITY_VALUES = frozenset({"applicable", "not_applicable", "unknown"})
HARD_TRUST_LEVELS = frozenset({"factory_oracle", "dual_impl"})

_SHA256_LENGTH = 64
_MISSING_REASONS = frozenset(
    {"missing", "not_run", "unavailable", "error", "command_error", "stale"}
)
_AMBIGUOUS_REASONS = frozenset({"ambiguous", "multiple_matches", "unclear"})
_DEFAULT_IGNORED_NAMES = frozenset(
    {".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache"}
)


class ObjectiveEvidenceError(ValueError):
    """Raised when an evidence object or path is not safe/valid."""


class UnsafeCommandError(ObjectiveEvidenceError):
    """Raised when an opt-in command is outside its explicit allow-list."""


def _canonical_json(value: Any) -> str:
    """Return a stable JSON representation suitable for hashing."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ObjectiveEvidenceError(f"value is not canonical JSON: {exc}") from exc


def canonical_hash(value: Any) -> str:
    """Hash a JSON-compatible value with no filesystem metadata involved."""

    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: str | Path) -> str:
    """Hash file contents, never mtime or inode metadata."""

    target = Path(path)
    if not target.is_file() or target.is_symlink():
        raise ObjectiveEvidenceError(f"not a regular file: {target}")
    digest = hashlib.sha256()
    try:
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ObjectiveEvidenceError(f"cannot read {target}: {exc}") from exc
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != _SHA256_LENGTH:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value == value.lower()


def _validate_serialized_relative_path(value: object, field: str = "evidence.path") -> str:
    """Validate the project-relative path form used in serialized evidence.

    ``evidence_ref`` performs containment checks while a root is available;
    deserialization has no root, so it must at least reject absolute paths and
    lexical traversal before a caller resolves the reference later.
    """

    if not isinstance(value, str) or not value.strip():
        raise ObjectiveEvidenceError(f"{field} must be non-empty")
    raw = value.strip()
    if "\x00" in raw or "\\" in raw:
        raise ObjectiveEvidenceError(f"{field} must be a project-relative POSIX path")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ObjectiveEvidenceError(
            f"{field} must not contain empty, dot, or traversal segments"
        )
    path = PurePosixPath(raw)
    if path.is_absolute():
        raise ObjectiveEvidenceError(f"{field} must not be absolute or contain traversal")
    return path.as_posix()


def _root_path(root: str | Path) -> Path:
    target = Path(root).expanduser().resolve()
    if not target.is_dir():
        raise ObjectiveEvidenceError(f"evidence root is not a directory: {target}")
    return target


def safe_relative_path(root: str | Path, path: str | Path) -> tuple[Path, str]:
    """Resolve ``path`` below ``root`` and return ``(absolute, POSIX rel)``.

    Absolute paths are accepted only when they resolve below ``root``.  This is
    useful for callers that receive a ``Path`` from a subprocess, while keeping
    serialized evidence project-relative.  Symlink escapes are rejected.
    """

    base = _root_path(root)
    raw = Path(path)
    if "\x00" in str(raw):
        raise ObjectiveEvidenceError("path contains NUL")
    if "\\" in str(raw):
        raise ObjectiveEvidenceError("path must use POSIX separators")
    if not raw.is_absolute() and ".." in raw.parts:
        raise ObjectiveEvidenceError("path contains traversal")
    candidate = raw if raw.is_absolute() else base / raw
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(base)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ObjectiveEvidenceError(f"path escapes evidence root: {path}") from exc
    relative = resolved.relative_to(base).as_posix()
    if relative in {"", "."}:
        relative = "."
    return resolved, relative


def _check_symlink_containment(root: Path, path: Path) -> None:
    """Reject a symlink whose target leaves ``root``."""

    if not path.is_symlink():
        return
    try:
        path.resolve(strict=False).relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ObjectiveEvidenceError(f"symlink escapes evidence root: {path}") from exc


def _tree_records(
    root: Path,
    target: Path,
    relative: str,
    *,
    ignored_names: frozenset[str],
    explicit: bool = False,
) -> list[dict[str, Any]]:
    """Collect deterministic records for one file/directory/missing path."""

    if not target.exists() and not target.is_symlink():
        return [{"path": relative, "exists": False, "kind": "missing", "sha256": None}]

    _check_symlink_containment(root, target)
    if target.is_symlink():
        # Do not follow links for the hash.  The target string is stable and
        # makes an external replacement visible to the caller.
        return [
            {
                "path": relative,
                "exists": True,
                "kind": "symlink",
                "target": os.readlink(target),
                "sha256": None,
            }
        ]
    if target.is_file():
        return [
            {
                "path": relative,
                "exists": True,
                "kind": "file",
                "bytes": target.stat().st_size,
                "sha256": sha256_file(target),
            }
        ]
    if not target.is_dir():
        return [{"path": relative, "exists": True, "kind": "other", "sha256": None}]

    records: list[dict[str, Any]] = []
    if explicit:
        records.append({"path": relative, "exists": True, "kind": "directory", "sha256": None})
    try:
        children = sorted(target.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise ObjectiveEvidenceError(f"cannot enumerate {target}: {exc}") from exc
    for child in children:
        if child.name in ignored_names:
            continue
        child_rel = child.relative_to(root).as_posix()
        records.extend(
            _tree_records(
                root,
                child,
                child_rel,
                ignored_names=ignored_names,
                explicit=True,
            )
        )
    return records


def fingerprint_paths(
    root: str | Path,
    paths: Iterable[str | Path] | None = None,
    *,
    ignored_names: Iterable[str] | None = None,
) -> str:
    """Return a deterministic SHA-256 fingerprint for selected project paths.

    Missing paths are intentionally represented in the record, so a checker
    cannot accidentally reuse a PASS fingerprint after an input disappears.
    Files are hashed by bytes; mtimes, permissions, and directory ordering are
    excluded.  By default common cache/VCS directories are omitted when the
    whole root is fingerprinted.
    """

    base = _root_path(root)
    ignored = frozenset(ignored_names or _DEFAULT_IGNORED_NAMES)
    selected = [Path(".")] if paths is None else [Path(item) for item in paths]
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in selected:
        target, relative = safe_relative_path(base, item)
        if relative in seen:
            continue
        seen.add(relative)
        records.extend(
            _tree_records(
                base,
                target,
                relative,
                ignored_names=ignored,
                explicit=relative == "." or target.is_dir(),
            )
        )
    records.sort(key=lambda item: (str(item.get("path")), str(item.get("kind"))))
    return canonical_hash({"root": ".", "records": records})


@dataclass(frozen=True)
class EvidenceRef:
    """A hash-bound source locator used by a finding."""

    path: str
    sha256: str | None
    locator: Any = ""

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "locator": self.locator}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvidenceRef":
        if not isinstance(value, Mapping):
            raise ObjectiveEvidenceError("evidence reference must be an object")
        path = _validate_serialized_relative_path(value.get("path"))
        digest = value.get("sha256")
        if digest is not None and not _is_sha256(digest):
            raise ObjectiveEvidenceError("evidence.sha256 must be lowercase SHA-256 or null")
        return cls(path=path, sha256=digest, locator=value.get("locator", ""))


def evidence_ref(
    root: str | Path,
    path: str | Path,
    locator: Any = "",
    *,
    allow_missing: bool = True,
) -> EvidenceRef:
    """Create a relative, content-hashed evidence reference."""

    base = _root_path(root)
    target, relative = safe_relative_path(base, path)
    if relative == ".":
        raise ObjectiveEvidenceError("evidence path must identify a project entry, not the root")
    if not target.exists() and not target.is_symlink():
        if allow_missing:
            return EvidenceRef(relative, None, locator)
        raise ObjectiveEvidenceError(f"evidence path is missing: {relative}")
    _check_symlink_containment(base, target)
    if target.is_file() and not target.is_symlink():
        return EvidenceRef(relative, sha256_file(target), locator)
    # Directories and symlinks are represented by the selected-tree fingerprint
    # rather than an ambiguous single-file digest.
    digest = fingerprint_paths(base, [relative])
    return EvidenceRef(relative, digest, locator)


def _coerce_evidence(
    root: str | Path | None,
    values: Iterable[EvidenceRef | Mapping[str, Any] | str | Path],
) -> tuple[EvidenceRef, ...]:
    result: list[EvidenceRef] = []
    for value in values:
        if isinstance(value, EvidenceRef):
            result.append(value)
        elif isinstance(value, Mapping):
            result.append(EvidenceRef.from_dict(value))
        elif root is not None:
            result.append(evidence_ref(root, value))
        else:
            raise ObjectiveEvidenceError("path evidence requires root")
    return tuple(result)


def _fallback_input_fingerprint(observed: Any, expected: Any) -> str:
    return canonical_hash({"observed": observed, "expected": expected, "source": "inline"})


def _policy_adjust(
    *,
    status: str,
    severity: str,
    trust_level: str,
    applicability: str,
    reason: str | None,
    exact_contradiction: bool,
) -> tuple[str, str, str | None]:
    """Apply the conservative hard-veto policy and return adjusted values."""

    if applicability == "not_applicable":
        return "SKIP", "info", "not_applicable"
    if applicability == "unknown":
        return "UNKNOWN", "soft_alert", "applicability_unknown"

    normalized_reason = (reason or "").strip().lower()
    if normalized_reason in _MISSING_REASONS:
        return "UNKNOWN", "soft_alert", normalized_reason
    if normalized_reason in _AMBIGUOUS_REASONS:
        return "UNKNOWN", "soft_alert", normalized_reason

    if status == "FAIL":
        hard_qualified = (
            severity == "hard_veto"
            and trust_level in HARD_TRUST_LEVELS
            and exact_contradiction is True
        )
        if hard_qualified:
            return "FAIL", "hard_veto", None
        # A heuristic/project-owned assertion remains visible, but cannot block
        # delivery.  Preserve a reason so callers can show the downgrade.
        return "WARN", "soft_alert", "unqualified_or_non_exact_contradiction"

    if status == "SKIP":
        return "SKIP", "info", normalized_reason or "skipped"
    if status == "UNKNOWN":
        return "UNKNOWN", "soft_alert" if severity != "info" else "info", normalized_reason or "unknown"
    if status == "WARN":
        return "WARN", "soft_alert" if severity == "hard_veto" else severity, normalized_reason or "warning"
    # PASS is an observation, not a claim that the paper is good.  A heuristic
    # PASS is retained for coverage accounting but is always informational.
    return "PASS", "info" if trust_level in {"heuristic", "self_report", "project_test"} else severity, None


@dataclass(frozen=True)
class Finding:
    """One machine observation in ``objective-evidence-v1``."""

    finding_id: str
    claim_id: str
    checker_id: str
    checker_version: str
    status: str
    severity: str
    trust_level: str
    applicability: str
    observed: Any
    expected: Any
    evidence: tuple[EvidenceRef, ...]
    input_fingerprint: str
    message: str = ""
    reason: str | None = None
    exact_contradiction: bool = False
    defect_cluster: str | None = None
    limitations: tuple[str, ...] = ()
    downgraded_from: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "claim_id": self.claim_id,
            "checker_id": self.checker_id,
            "checker_version": self.checker_version,
            "status": self.status,
            "severity": self.severity,
            "trust_level": self.trust_level,
            "applicability": self.applicability,
            "observed": self.observed,
            "expected": self.expected,
            "evidence": [item.to_dict() for item in self.evidence],
            "input_fingerprint": self.input_fingerprint,
            "message": self.message,
            "reason": self.reason,
            "exact_contradiction": self.exact_contradiction,
            "defect_cluster": self.defect_cluster,
            "limitations": list(self.limitations),
            "downgraded_from": self.downgraded_from,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Finding":
        if not isinstance(value, Mapping):
            raise ObjectiveEvidenceError("finding must be an object")
        required = {
            "finding_id",
            "claim_id",
            "checker_id",
            "checker_version",
            "status",
            "severity",
            "trust_level",
            "applicability",
            "observed",
            "expected",
            "evidence",
            "input_fingerprint",
        }
        missing = sorted(key for key in required if key not in value)
        if missing:
            raise ObjectiveEvidenceError(f"finding missing fields: {', '.join(missing)}")
        evidence = value.get("evidence")
        if not isinstance(evidence, list):
            raise ObjectiveEvidenceError("finding.evidence must be an array")
        if not _is_sha256(value.get("input_fingerprint")):
            raise ObjectiveEvidenceError("finding.input_fingerprint must be SHA-256")
        status = value.get("status")
        severity = value.get("severity")
        trust = value.get("trust_level")
        applicability = value.get("applicability")
        if status not in STATUS_VALUES or severity not in SEVERITY_VALUES:
            raise ObjectiveEvidenceError("finding status/severity is invalid")
        if trust not in TRUST_LEVELS or applicability not in APPLICABILITY_VALUES:
            raise ObjectiveEvidenceError("finding trust/applicability is invalid")
        return cls(
            finding_id=str(value["finding_id"]),
            claim_id=str(value["claim_id"]),
            checker_id=str(value["checker_id"]),
            checker_version=str(value["checker_version"]),
            status=str(status),
            severity=str(severity),
            trust_level=str(trust),
            applicability=str(applicability),
            observed=value.get("observed"),
            expected=value.get("expected"),
            evidence=tuple(EvidenceRef.from_dict(item) for item in evidence),
            input_fingerprint=str(value["input_fingerprint"]),
            message=str(value.get("message") or ""),
            reason=str(value["reason"]) if value.get("reason") is not None else None,
            exact_contradiction=bool(value.get("exact_contradiction", False)),
            defect_cluster=str(value["defect_cluster"]) if value.get("defect_cluster") else None,
            limitations=tuple(str(item) for item in (value.get("limitations") or [])),
            downgraded_from=value.get("downgraded_from"),
        )


def make_finding(
    *,
    finding_id: str,
    claim_id: str,
    checker_id: str,
    checker_version: str,
    status: str,
    severity: str = "info",
    trust_level: str = "heuristic",
    applicability: str = "applicable",
    observed: Any = None,
    expected: Any = None,
    root: str | Path | None = None,
    evidence: Iterable[EvidenceRef | Mapping[str, Any] | str | Path] = (),
    input_paths: Iterable[str | Path] | None = None,
    input_fingerprint: str | None = None,
    message: str = "",
    reason: str | None = None,
    exact_contradiction: bool = False,
    defect_cluster: str | None = None,
    limitations: Iterable[str] = (),
) -> Finding:
    """Construct a finding and enforce the conservative trust policy.

    ``status=FAIL, severity=hard_veto`` is intentionally not enough for a hard
    veto.  The caller must explicitly declare ``trust_level`` as
    ``factory_oracle`` or ``dual_impl`` and set ``exact_contradiction=True``.
    Missing/ambiguous/stale inputs become ``UNKNOWN``; legacy or heuristic
    failures become ``WARN``.
    """

    if not isinstance(finding_id, str) or not finding_id.strip():
        raise ObjectiveEvidenceError("finding_id must be non-empty")
    if not isinstance(claim_id, str) or not claim_id.strip():
        raise ObjectiveEvidenceError("claim_id must be non-empty")
    if status not in STATUS_VALUES:
        raise ObjectiveEvidenceError(f"invalid status: {status}")
    if severity not in SEVERITY_VALUES:
        raise ObjectiveEvidenceError(f"invalid severity: {severity}")
    if trust_level not in TRUST_LEVELS:
        raise ObjectiveEvidenceError(f"invalid trust_level: {trust_level}")
    if applicability not in APPLICABILITY_VALUES:
        raise ObjectiveEvidenceError(f"invalid applicability: {applicability}")

    refs = _coerce_evidence(root, evidence)
    if input_fingerprint is None:
        if root is not None:
            paths = list(input_paths) if input_paths is not None else [ref.path for ref in refs]
            input_fingerprint = fingerprint_paths(root, paths)
        else:
            input_fingerprint = _fallback_input_fingerprint(observed, expected)
    if not _is_sha256(input_fingerprint):
        raise ObjectiveEvidenceError("input_fingerprint must be lowercase SHA-256")

    adjusted_status, adjusted_severity, adjustment_reason = _policy_adjust(
        status=status,
        severity=severity,
        trust_level=trust_level,
        applicability=applicability,
        reason=reason,
        exact_contradiction=exact_contradiction,
    )
    downgraded = None
    final_reason = reason
    if (adjusted_status, adjusted_severity) != (status, severity):
        downgraded = {"status": status, "severity": severity}
        final_reason = adjustment_reason or reason
    return Finding(
        finding_id=finding_id.strip(),
        claim_id=claim_id.strip(),
        checker_id=checker_id.strip(),
        checker_version=checker_version.strip(),
        status=adjusted_status,
        severity=adjusted_severity,
        trust_level=trust_level,
        applicability=applicability,
        observed=observed,
        expected=expected,
        evidence=refs,
        input_fingerprint=input_fingerprint,
        message=message,
        reason=final_reason,
        exact_contradiction=bool(exact_contradiction),
        defect_cluster=defect_cluster,
        limitations=tuple(str(item) for item in limitations),
        downgraded_from=downgraded,
    )


def validate_finding(finding: Finding | Mapping[str, Any]) -> Finding:
    """Parse and validate a serialized finding without changing its policy."""

    parsed = finding if isinstance(finding, Finding) else Finding.from_dict(finding)
    if parsed.status == "FAIL" and parsed.severity == "hard_veto":
        if parsed.trust_level not in HARD_TRUST_LEVELS or not parsed.exact_contradiction:
            raise ObjectiveEvidenceError("unqualified hard_veto finding")
        if parsed.applicability != "applicable":
            raise ObjectiveEvidenceError("hard_veto finding is not applicable")
    return parsed


def _dedupe_findings(findings: Sequence[Finding]) -> list[Finding]:
    """Deduplicate exact repeats while preserving independent findings."""

    seen: set[str] = set()
    result: list[Finding] = []
    for finding in findings:
        key = canonical_hash(finding.to_dict())
        if key in seen:
            continue
        seen.add(key)
        result.append(finding)
    return result


def _summary(findings: Sequence[Finding]) -> dict[str, Any]:
    counts = {status: sum(item.status == status for item in findings) for status in STATUS_VALUES}
    severity_counts = {
        severity: sum(item.severity == severity for item in findings)
        for severity in SEVERITY_VALUES
    }
    hard_vetoes = [
        item.finding_id
        for item in findings
        if item.status == "FAIL" and item.severity == "hard_veto"
    ]
    unknown_required = [
        item.finding_id
        for item in findings
        if item.status == "UNKNOWN" and item.applicability == "applicable"
    ]
    if hard_vetoes:
        decision = "FAIL"
    elif unknown_required or not findings or all(item.status == "SKIP" for item in findings):
        decision = "INDETERMINATE"
    else:
        decision = "PASS"
    return {
        "decision": decision,
        "counts": counts,
        "severity_counts": severity_counts,
        "hard_vetoes": hard_vetoes,
        "unknown_required": unknown_required,
        "warning_findings": [item.finding_id for item in findings if item.status == "WARN"],
    }


@dataclass(frozen=True)
class EvidenceBundle:
    """Serializable collection of findings for one immutable input snapshot."""

    project_id: str
    input_fingerprint: str
    findings: tuple[Finding, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ObjectiveEvidenceError(f"unsupported schema: {self.schema_version}")
        if not self.project_id.strip():
            raise ObjectiveEvidenceError("project_id must be non-empty")
        if not _is_sha256(self.input_fingerprint):
            raise ObjectiveEvidenceError("bundle input_fingerprint must be SHA-256")
        for finding in self.findings:
            validate_finding(finding)

    @property
    def summary(self) -> dict[str, Any]:
        return _summary(self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "input_fingerprint": self.input_fingerprint,
            "findings": [item.to_dict() for item in self.findings],
            "summary": self.summary,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvidenceBundle":
        if not isinstance(value, Mapping):
            raise ObjectiveEvidenceError("bundle must be an object")
        if value.get("schema_version") != SCHEMA_VERSION:
            raise ObjectiveEvidenceError("unsupported bundle schema")
        raw_findings = value.get("findings")
        if not isinstance(raw_findings, list):
            raise ObjectiveEvidenceError("bundle.findings must be an array")
        return cls(
            project_id=str(value.get("project_id") or ""),
            input_fingerprint=str(value.get("input_fingerprint") or ""),
            findings=tuple(Finding.from_dict(item) for item in raw_findings),
            metadata=value.get("metadata") if isinstance(value.get("metadata"), Mapping) else {},
        )


def build_bundle(
    root: str | Path,
    findings: Iterable[Finding],
    *,
    project_id: str | None = None,
    input_paths: Iterable[str | Path] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> EvidenceBundle:
    """Build a bundle and hash the exact selected project inputs."""

    base = _root_path(root)
    selected = list(input_paths) if input_paths is not None else None
    bundle_fingerprint = fingerprint_paths(base, selected)
    parsed = tuple(_dedupe_findings([validate_finding(item) for item in findings]))
    return EvidenceBundle(
        project_id=project_id or base.name,
        input_fingerprint=bundle_fingerprint,
        findings=parsed,
        metadata=dict(metadata or {}),
    )


@dataclass(frozen=True)
class CommandResult:
    """Bounded result of an explicitly allow-listed command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    output_truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "output_truncated": self.output_truncated,
        }


def _decode_bounded(value: bytes, limit: int) -> str:
    """Decode at most ``limit`` bytes without returning a larger re-encoding."""

    text = value[:limit].decode("utf-8", errors="replace")
    # A replacement character can occupy more bytes than the partial UTF-8
    # sequence that produced it.  Trim until the serialized representation is
    # still within the caller's byte budget.
    while len(text.encode("utf-8")) > limit and text:
        text = text[:-1]
    return text


def _within(path: Path, roots: Sequence[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


_PYTHON_EXECUTABLES = frozenset(
    {
        "python",
        "python2",
        "python3",
        "python3.11",
        "python3.12",
        "python3.13",
        "pypy",
        "pypy3",
    }
)
_SHELL_EXECUTABLES = frozenset(
    {
        "sh",
        "bash",
        "dash",
        "zsh",
        "fish",
        "ksh",
        "csh",
        "tcsh",
        "ash",
        "cmd",
        "cmd.exe",
        "powershell",
        "pwsh",
    }
)


def _trusted_python_executable(value: str) -> bool:
    try:
        candidate = (
            Path(value).resolve(strict=True)
            if ("/" in value or value.startswith("."))
            else Path(shutil.which(value) or "").resolve(strict=True)
        )
    except (OSError, RuntimeError):
        return False
    trusted: set[Path] = set()
    for executable in (sys.executable, *(shutil.which(name) for name in _PYTHON_EXECUTABLES)):
        if not executable:
            continue
        try:
            trusted.add(Path(executable).resolve(strict=True))
        except (OSError, RuntimeError):
            continue
    return candidate.is_file() and candidate in trusted


def _trusted_script_from_argv(
    argv: Sequence[str],
    roots: Sequence[Path],
    scripts: Sequence[Path],
    *,
    cwd: Path | None = None,
) -> bool:
    if not argv:
        return False
    first = str(argv[0])
    first_name = Path(first).name.lower()
    # Shells are intentionally never accepted, even if a caller accidentally
    # places a shell binary under an allow-listed project directory.
    if first_name in _SHELL_EXECUTABLES:
        return False
    if first_name in _PYTHON_EXECUTABLES:
        # Python -c/-m can execute arbitrary code/modules and is deliberately
        # excluded.  A script argument must resolve below an allow-listed root.
        if not _trusted_python_executable(first):
            return False
        if len(argv) < 2 or str(argv[1]).startswith("-"):
            return False
        script = Path(argv[1])
        if not script.is_absolute() and cwd is not None:
            script = cwd / script
        try:
            resolved = script.resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        return resolved.is_file() and (
            _within(resolved, roots) or any(resolved == item for item in scripts)
        )
    try:
        executable = (
            Path(first).resolve(strict=True)
            if ("/" in first or first.startswith("."))
            else Path(shutil.which(first) or "").resolve(strict=True)
        )
    except (OSError, RuntimeError):
        return False
    return bool(str(executable)) and (_within(executable, roots) or any(executable == item for item in scripts))


def run_safe_command(
    argv: Sequence[str],
    *,
    cwd: str | Path,
    allowed_roots: Sequence[str | Path],
    allowed_scripts: Sequence[str | Path] = (),
    timeout: float = 30.0,
    max_output_bytes: int = 1_000_000,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run one explicitly allow-listed command without a shell.

    The caller must opt in with ``allowed_roots``.  Python ``-c``/``-m`` and
    shell interpreters are rejected, making accidental command injection much
    harder when an argv is assembled from project metadata.
    """

    if isinstance(argv, (str, bytes)) or not argv:
        raise UnsafeCommandError("argv must be a non-empty sequence, not a shell string")
    values = tuple(str(item) for item in argv)
    if any(not item or "\x00" in item for item in values):
        raise UnsafeCommandError("argv contains an empty token or NUL")
    if any(item in {"-c", "-m", "--command", "--eval"} for item in values[1:]):
        raise UnsafeCommandError("inline/module execution is not allowed")
    roots: list[Path] = []
    for item in allowed_roots:
        roots.append(_root_path(item))
    if not roots:
        raise UnsafeCommandError("at least one allowed root is required")
    scripts: list[Path] = []
    for item in allowed_scripts:
        path = Path(item).expanduser().resolve(strict=True)
        if not path.is_file() or not _within(path, roots):
            raise UnsafeCommandError(f"allowed script is outside roots: {item}")
        scripts.append(path)
    workdir = Path(cwd).expanduser().resolve(strict=True)
    if not workdir.is_dir() or not _within(workdir, roots):
        raise UnsafeCommandError("command cwd is outside allowed roots")
    if not _trusted_script_from_argv(values, roots, scripts, cwd=workdir):
        raise UnsafeCommandError("command executable/script is not allow-listed")
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or timeout <= 0
        or timeout > 900
    ):
        raise UnsafeCommandError("timeout must be in (0, 900]")
    if (
        not isinstance(max_output_bytes, int)
        or isinstance(max_output_bytes, bool)
        or max_output_bytes <= 0
        or max_output_bytes > 20_000_000
    ):
        raise UnsafeCommandError("max_output_bytes is outside the safe range")

    safe_env = {
        "PATH": os.environ.get("PATH", ""),
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONHASHSEED": "0",
    }
    if env:
        safe_env.update({str(key): str(value) for key, value in env.items()})
    try:
        completed = subprocess.run(
            list(values),
            cwd=workdir,
            env=safe_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            timeout=float(timeout),
            check=False,
        )
        stdout_bytes = completed.stdout or b""
        stderr_bytes = completed.stderr or b""
        truncated = len(stdout_bytes) > max_output_bytes or len(stderr_bytes) > max_output_bytes
        return CommandResult(
            argv=values,
            returncode=int(completed.returncode),
            stdout=_decode_bounded(stdout_bytes, max_output_bytes),
            stderr=_decode_bounded(stderr_bytes, max_output_bytes),
            output_truncated=truncated,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or b"")
        stderr = (exc.stderr or b"")
        if isinstance(stdout, str):
            stdout = stdout.encode()
        if isinstance(stderr, str):
            stderr = stderr.encode()
        return CommandResult(
            argv=values,
            returncode=124,
            stdout=_decode_bounded(stdout, max_output_bytes),
            stderr=_decode_bounded(stderr, max_output_bytes),
            timed_out=True,
            output_truncated=len(stdout) > max_output_bytes or len(stderr) > max_output_bytes,
        )
    except OSError as exc:
        return CommandResult(argv=values, returncode=127, stdout="", stderr=str(exc))


def command_finding(
    *,
    finding_id: str,
    claim_id: str,
    checker_id: str,
    checker_version: str,
    result: CommandResult,
    root: str | Path,
    input_paths: Iterable[str | Path],
    oracle_trusted: bool = True,
    exact_contradiction: bool = False,
    observed: Any = None,
    expected: Any = None,
    message: str = "",
) -> Finding:
    """Convert a command result to a conservative finding.

    A non-zero command is *not* automatically a mathematical contradiction;
    without an explicit parser assertion it becomes ``UNKNOWN``.  Callers that
    independently parsed an exact violated invariant may pass
    ``exact_contradiction=True`` and receive a hard-veto-eligible finding.
    """

    if result.ok:
        return make_finding(
            finding_id=finding_id,
            claim_id=claim_id,
            checker_id=checker_id,
            checker_version=checker_version,
            status="PASS",
            severity="info",
            trust_level="factory_oracle" if oracle_trusted else "infrastructure",
            root=root,
            input_paths=input_paths,
            observed=observed if observed is not None else result.stdout,
            expected=expected,
            message=message or "oracle command passed",
        )
    return make_finding(
        finding_id=finding_id,
        claim_id=claim_id,
        checker_id=checker_id,
        checker_version=checker_version,
        status="FAIL" if exact_contradiction else "UNKNOWN",
        severity="hard_veto" if exact_contradiction else "soft_alert",
        trust_level="factory_oracle" if oracle_trusted else "infrastructure",
        root=root,
        input_paths=input_paths,
        observed=observed if observed is not None else {"returncode": result.returncode, "stderr": result.stderr},
        expected=expected,
        exact_contradiction=exact_contradiction,
        reason=None if exact_contradiction else "command_error",
        message=message or "oracle command did not complete successfully",
    )


def snapshot_bundle(root: str | Path, paths: Iterable[str | Path] | None = None) -> EvidenceBundle:
    """Create a harmless, informational snapshot finding for CLI/diagnostics."""

    base = _root_path(root)
    selected = list(paths) if paths is not None else None
    fingerprint = fingerprint_paths(base, selected)
    finding = Finding(
        finding_id="snapshot.input_fingerprint",
        claim_id="snapshot",
        checker_id="objective_evidence.snapshot",
        checker_version=SCHEMA_VERSION,
        status="PASS",
        severity="info",
        trust_level="infrastructure",
        applicability="applicable",
        observed={"paths": selected if selected is not None else ["."]},
        expected={"input_fingerprint": fingerprint},
        evidence=(),
        input_fingerprint=fingerprint,
        message="deterministic input snapshot recorded",
    )
    return EvidenceBundle(project_id=base.name, input_fingerprint=fingerprint, findings=(finding,))


def _cli(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="project directory to fingerprint")
    parser.add_argument("--path", action="append", default=None, help="project-relative path (repeatable)")
    parser.add_argument("--json", action="store_true", help="print an objective-evidence-v1 snapshot")
    args = parser.parse_args(list(argv))
    try:
        bundle = snapshot_bundle(args.project, args.path)
    except ObjectiveEvidenceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(bundle.input_fingerprint)
    return 0


def main() -> int:
    return _cli(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
