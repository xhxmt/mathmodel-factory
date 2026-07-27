#!/usr/bin/env python3
"""Declarative, project-specific quality claims and executable evidence.

Evidence ``type`` describes what a command does; evidence ``level`` describes
how much independence it has from the project under test.  The distinction is
deliberate: a passing project-owned test is useful diagnostic evidence, but it
cannot by itself certify a hard claim.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


CONTRACT_VERSIONS = {1, 2}
EVIDENCE_LEVELS = frozenset(
    {"factory_oracle", "dual_impl", "project_test", "self_report"}
)
HARD_PASS_LEVELS = frozenset({"factory_oracle", "dual_impl"})
TRUSTED_FACTORY_ORACLE_FILES = frozenset(
    {
        "scripts/verify_deliverables.py",
        "scripts/verify_invariants.py",
        "scripts/verify_number_chain.py",
        "scripts/verify_provenance.py",
        "scripts/verify_spec_impl.py",
    }
)
TRUSTED_FACTORY_ORACLE_DIRS = ("scripts/domain_oracles",)


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    item_id: str = ""


@dataclass(frozen=True)
class EvidenceResult:
    claim_id: str
    evidence_type: str
    declared_level: str | None
    evidence_level: str
    hard_pass_eligible: bool
    qualification: str
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


@dataclass
class ContractResult:
    passed: bool
    failures: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    evidence_results: list[EvidenceResult] = field(default_factory=list)
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_contract(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("quality contract must be a JSON object")
    version = data.get("version")
    if version not in CONTRACT_VERSIONS:
        raise ValueError("quality contract version must be 1 or 2")
    if not isinstance(data.get("claims", []), list):
        raise ValueError("quality contract claims must be a list")
    if not isinstance(data.get("anomaly_checks", []), list):
        raise ValueError("quality contract anomaly_checks must be a list")
    seen_claim_ids: set[str] = set()
    for claim_index, claim in enumerate(data.get("claims", [])):
        if not isinstance(claim, dict):
            raise ValueError(f"quality contract claims[{claim_index}] must be an object")
        claim_id = claim.get("id")
        if not isinstance(claim_id, str) or not claim_id.strip():
            raise ValueError(f"quality contract claims[{claim_index}].id must be nonempty")
        if claim_id in seen_claim_ids:
            raise ValueError(f"duplicate quality contract claim id: {claim_id}")
        seen_claim_ids.add(claim_id)
        severity = claim.get("severity", "advisory")
        if severity not in {"hard", "advisory"}:
            raise ValueError(f"quality contract claim {claim_id} has invalid severity")
        evidence = claim.get("evidence", [])
        if not isinstance(evidence, list):
            raise ValueError(f"quality contract claim {claim_id} evidence must be a list")
        question_ids = claim.get("question_ids", [])
        if not isinstance(question_ids, list) or any(
            not isinstance(item, str) or not item.strip() for item in question_ids
        ):
            raise ValueError(f"quality contract claim {claim_id} question_ids must be strings")
        for evidence_index, item in enumerate(evidence):
            if not isinstance(item, dict):
                raise ValueError(
                    f"quality contract claim {claim_id} evidence[{evidence_index}] "
                    "must be an object"
                )
            level = item.get("level")
            if version == 2 and level is None:
                raise ValueError(
                    f"quality contract v2 claim {claim_id} evidence[{evidence_index}] "
                    "must declare level"
                )
            if level is not None and level not in EVIDENCE_LEVELS:
                raise ValueError(
                    f"quality contract claim {claim_id} evidence[{evidence_index}] "
                    f"has invalid level: {level}"
                )
    return data


def _expanded_argv(
    argv: list[Any], project_dir: Path, factory_root: Path
) -> list[str]:
    replacements = {
        "__PROJECT_PATH__": str(project_dir),
        "__FACTORY__": str(factory_root),
    }
    return [
        replacements.get(str(item), str(item))
        .replace("__PROJECT_PATH__", str(project_dir))
        .replace("__FACTORY__", str(factory_root))
        for item in argv
    ]


def _project_locator_path(locator: object, project_dir: Path) -> Path | None:
    """Resolve a ``path[::symbol][#anchor]`` locator inside the project."""

    if not isinstance(locator, str) or not locator.strip() or "\x00" in locator:
        return None
    raw = locator.split("::", 1)[0].split("#", 1)[0].strip()
    if not raw or "\\" in raw:
        return None
    candidate = Path(raw)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    try:
        resolved = (project_dir / candidate).resolve(strict=True)
        resolved.relative_to(project_dir)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _invoked_python_script(argv: list[str]) -> str | None:
    """Return the script Python actually executes, not an arbitrary argument."""

    if not argv:
        return None
    executable = Path(argv[0]).name.lower()
    if argv[0].lower().endswith(".py"):
        return argv[0]
    if not (executable.startswith("python") or executable.startswith("pypy")):
        return None
    index = 1
    options_with_values = {"-W", "-X"}
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return argv[index + 1] if index + 1 < len(argv) else None
        if token in {"-c", "-m"}:
            return None
        if token in options_with_values:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    return None


def _trusted_factory_oracle(argv: list[str], factory_root: Path) -> bool:
    """Require the invoked oracle script to be in a factory-owned allow-list."""

    token = _invoked_python_script(argv)
    if token is None or "\x00" in token:
        return False
    candidate = Path(token)
    if candidate.suffix.lower() != ".py":
        return False
    if not candidate.is_absolute():
        candidate = factory_root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(factory_root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return False
    if relative in TRUSTED_FACTORY_ORACLE_FILES:
        return True
    return any(
        relative == root or relative.startswith(root + "/")
        for root in TRUSTED_FACTORY_ORACLE_DIRS
    )


def _qualify_evidence_level(
    item: dict[str, Any],
    argv: list[str],
    project_dir: Path,
    factory_root: Path,
) -> tuple[str, bool, str]:
    declared = item.get("level")
    if declared is None:
        return "self_report", False, "legacy_missing_level"
    level = str(declared)
    if level == "factory_oracle":
        if _trusted_factory_oracle(argv, factory_root):
            return level, True, "trusted_factory_oracle"
        return "self_report", False, "factory_oracle_not_in_trusted_allowlist"
    if level == "dual_impl":
        implementations = item.get("implementations")
        if not isinstance(implementations, list):
            return "project_test", False, "dual_impl_requires_implementations"
        resolved = [
            _project_locator_path(locator, project_dir) for locator in implementations
        ]
        unique = {path for path in resolved if path is not None}
        if len(implementations) < 2 or len(unique) != len(implementations):
            return "project_test", False, "dual_impl_paths_not_distinct_and_safe"
        try:
            implementation_hashes = {
                hashlib.sha256(path.read_bytes()).hexdigest() for path in unique
            }
        except OSError:
            return "project_test", False, "dual_impl_files_unreadable"
        if len(implementation_hashes) < 2:
            return "project_test", False, "dual_impl_files_are_byte_identical"
        comparator_token = _invoked_python_script(argv)
        comparator = _project_locator_path(comparator_token, project_dir)
        if comparator is None or comparator in unique:
            return "project_test", False, "dual_impl_requires_distinct_project_comparator"
        return level, True, "distinct_project_implementations_registered"
    if level in EVIDENCE_LEVELS:
        return level, level in HARD_PASS_LEVELS, "declared_level"
    # ``load_contract`` prevents this for file-backed contracts.  Keep the
    # evaluator fail-closed for direct callers too.
    return "self_report", False, "invalid_declared_level"


def evaluate_contract(
    contract: dict[str, Any],
    project_dir: Path,
    *,
    factory_root: Path | None = None,
    timeout: int = 120,
) -> ContractResult:
    project_dir = Path(project_dir).resolve()
    factory_root = (factory_root or Path(__file__).resolve().parents[1]).resolve()
    result = ContractResult(passed=True)

    for claim in contract.get("claims", []):
        claim_id = str(claim.get("id") or "")
        severity = str(claim.get("severity") or "advisory").lower()
        evidence = claim.get("evidence") or []
        if severity == "hard" and not evidence:
            result.failures.append(
                Finding(
                    code="MISSING_INDEPENDENT_EVIDENCE",
                    item_id=claim_id,
                    message="hard claim has no executable independent evidence",
                )
            )
            continue

        configured_hard_pass_evidence = 0
        for item in evidence:
            argv_raw = item.get("argv") if isinstance(item, dict) else None
            evidence_type = (
                str(item.get("type") or "command")
                if isinstance(item, dict)
                else "command"
            )
            if not isinstance(argv_raw, list) or not argv_raw:
                finding = Finding(
                    code="INVALID_EVIDENCE_COMMAND",
                    item_id=claim_id,
                    message="evidence argv must be a nonempty JSON array",
                )
                (result.failures if severity == "hard" else result.warnings).append(finding)
                continue
            argv = _expanded_argv(argv_raw, project_dir, factory_root)
            evidence_level, hard_pass_eligible, qualification = _qualify_evidence_level(
                item, argv, project_dir, factory_root
            )
            if hard_pass_eligible:
                configured_hard_pass_evidence += 1
            declared_level = item.get("level")
            if declared_level != evidence_level:
                result.warnings.append(
                    Finding(
                        code="EVIDENCE_LEVEL_DOWNGRADED",
                        item_id=claim_id,
                        message=(
                            f"{evidence_type} evidence declared {declared_level or 'no level'}; "
                            f"effective level is {evidence_level} ({qualification})"
                        ),
                    )
                )
            try:
                completed = subprocess.run(
                    argv,
                    cwd=project_dir,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                evidence_result = EvidenceResult(
                    claim_id=claim_id,
                    evidence_type=evidence_type,
                    declared_level=str(declared_level) if declared_level is not None else None,
                    evidence_level=evidence_level,
                    hard_pass_eligible=hard_pass_eligible,
                    qualification=qualification,
                    argv=argv,
                    returncode=completed.returncode,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                evidence_result = EvidenceResult(
                    claim_id=claim_id,
                    evidence_type=evidence_type,
                    declared_level=str(declared_level) if declared_level is not None else None,
                    evidence_level=evidence_level,
                    hard_pass_eligible=hard_pass_eligible,
                    qualification=qualification,
                    argv=argv,
                    returncode=124,
                    stdout="",
                    stderr=str(exc),
                )
            result.evidence_results.append(evidence_result)
            if evidence_result.returncode != 0:
                finding = Finding(
                    code="EVIDENCE_FAILED",
                    item_id=claim_id,
                    message=(
                        f"{evidence_type} evidence exited "
                        f"{evidence_result.returncode}: {' '.join(argv)}"
                    ),
                )
                (result.failures if severity == "hard" else result.warnings).append(finding)

        if severity == "hard" and evidence and configured_hard_pass_evidence == 0:
            result.failures.append(
                Finding(
                    code="MISSING_TRUSTED_HARD_EVIDENCE",
                    item_id=claim_id,
                    message=(
                        "hard claim requires factory_oracle or structurally validated "
                        "dual_impl evidence; project_test/self_report cannot certify PASS"
                    ),
                )
            )

    for check in contract.get("anomaly_checks", []):
        check_id = str(check.get("id") or "")
        hard = check.get("hard") is True
        justification = str(check.get("justification") or "").strip()
        failed = str(check.get("status") or "unknown").lower() in {"fail", "failed"}
        if hard and not justification:
            result.failures.append(
                Finding(
                    code="UNJUSTIFIED_HARD_ANOMALY",
                    item_id=check_id,
                    message="hard anomaly rule lacks a problem-specific justification",
                )
            )
        elif failed and hard:
            result.failures.append(
                Finding(
                    code="HARD_ANOMALY_FAILED",
                    item_id=check_id,
                    message=str(check.get("detail") or "hard anomaly check failed"),
                )
            )
        elif failed:
            result.warnings.append(
                Finding(
                    code="ANOMALY_DETECTED",
                    item_id=check_id,
                    message=str(check.get("detail") or "advisory anomaly detected"),
                )
            )

    result.passed = not result.failures
    return result
