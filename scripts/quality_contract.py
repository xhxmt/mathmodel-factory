#!/usr/bin/env python3
"""Declarative, project-specific quality claims and executable evidence."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    item_id: str = ""


@dataclass(frozen=True)
class EvidenceResult:
    claim_id: str
    evidence_type: str
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
    if data.get("version") != 1:
        raise ValueError("quality contract version must be 1")
    if not isinstance(data.get("claims", []), list):
        raise ValueError("quality contract claims must be a list")
    if not isinstance(data.get("anomaly_checks", []), list):
        raise ValueError("quality contract anomaly_checks must be a list")
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

        for item in evidence:
            argv_raw = item.get("argv") if isinstance(item, dict) else None
            evidence_type = str(item.get("type") or "command") if isinstance(item, dict) else "command"
            if not isinstance(argv_raw, list) or not argv_raw:
                finding = Finding(
                    code="INVALID_EVIDENCE_COMMAND",
                    item_id=claim_id,
                    message="evidence argv must be a nonempty JSON array",
                )
                (result.failures if severity == "hard" else result.warnings).append(finding)
                continue
            argv = _expanded_argv(argv_raw, project_dir, factory_root)
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
                    argv=argv,
                    returncode=completed.returncode,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                evidence_result = EvidenceResult(
                    claim_id=claim_id,
                    evidence_type=evidence_type,
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
