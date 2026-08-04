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
import math
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


CONTRACT_VERSIONS = {1, 2, 3, 4}
EVIDENCE_LEVELS = frozenset(
    {"factory_oracle", "dual_impl", "project_test", "self_report"}
)
HARD_PASS_LEVELS = frozenset({"factory_oracle", "dual_impl"})
CONTINUOUS_TIME_PROOF_TYPES = frozenset(
    {"event_localization", "certified_error_bound", "interval_error_bound"}
)
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


@dataclass(frozen=True)
class CompetitivenessResult:
    check_id: str
    objective_sense: str
    objective: float | None
    bound_kind: str
    bound_value: float | None
    absolute_gap: float | None
    relative_gap: float | None
    ladder_levels: int
    plateau_interpretation: str
    plateau_observed: bool | None
    cross_check_families: list[str]
    passed: bool


@dataclass
class ContractResult:
    passed: bool
    failures: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    evidence_results: list[EvidenceResult] = field(default_factory=list)
    competitiveness_results: list[CompetitivenessResult] = field(default_factory=list)
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_contract(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("quality contract must be a JSON object")
    version = data.get("version")
    if version not in CONTRACT_VERSIONS:
        raise ValueError("quality contract version must be 1, 2, 3, or 4")
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
        constraint_domain = claim.get("constraint_domain")
        if version in {3, 4} and severity == "hard" and constraint_domain is None:
            raise ValueError(
                f"quality contract v{version} hard claim {claim_id} must declare constraint_domain"
            )
        if constraint_domain is not None and constraint_domain not in {
            "continuous_time",
            "discrete",
            "algebraic",
        }:
            raise ValueError(
                f"quality contract claim {claim_id} has invalid constraint_domain"
            )
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
            if version in {2, 3, 4} and level is None:
                raise ValueError(
                    f"quality contract v{version} claim {claim_id} evidence[{evidence_index}] "
                    "must declare level"
                )
            if level is not None and level not in EVIDENCE_LEVELS:
                raise ValueError(
                    f"quality contract claim {claim_id} evidence[{evidence_index}] "
                    f"has invalid level: {level}"
                )
        if version == 4 and severity == "hard":
            source = claim.get("source")
            implementations = claim.get("implementation")
            if not isinstance(source, str) or not source.strip():
                raise ValueError(
                    f"quality contract v4 hard claim {claim_id} must declare source"
                )
            if not isinstance(implementations, list) or not implementations or any(
                not isinstance(item, str) or not item.strip() for item in implementations
            ):
                raise ValueError(
                    f"quality contract v4 hard claim {claim_id} must declare implementation"
                )
    if version == 4:
        _validate_v4_contract(data)
    return data


def _relative_path_text(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be a nonempty project-relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(f"{context} must stay inside the project")
    return value


def _json_pointer_text(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError(f"{context} must be a JSON pointer")
    return value


def _positive_integer(value: Any, context: str, *, minimum: int = 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{context} must be an integer >= {minimum}")
    return value


def _nonnegative_number(value: Any, context: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError(f"{context} must be a finite nonnegative number")
    return float(value)


def _validate_v4_contract(data: dict[str, Any]) -> None:
    checks = data.get("competitiveness_checks")
    if not isinstance(checks, list):
        raise ValueError("quality contract v4 competitiveness_checks must be a list")
    derived = data.get("derived_artifacts")
    if not isinstance(derived, dict):
        raise ValueError("quality contract v4 derived_artifacts must be an object")
    _relative_path_text(
        derived.get("manifest"), "quality contract v4 derived_artifacts.manifest"
    )
    seen: set[str] = set()
    for index, check in enumerate(checks):
        context = f"quality contract v4 competitiveness_checks[{index}]"
        if not isinstance(check, dict):
            raise ValueError(f"{context} must be an object")
        check_id = check.get("id")
        if not isinstance(check_id, str) or not check_id.strip():
            raise ValueError(f"{context}.id must be nonempty")
        if check_id in seen:
            raise ValueError(f"duplicate competitiveness check id: {check_id}")
        seen.add(check_id)
        question_ids = check.get("question_ids")
        if not isinstance(question_ids, list) or not question_ids or any(
            not isinstance(item, str) or not item.strip() for item in question_ids
        ):
            raise ValueError(f"{context}.question_ids must be a nonempty string list")
        sense = check.get("objective_sense")
        if sense not in {"maximize", "minimize"}:
            raise ValueError(f"{context}.objective_sense must be maximize or minimize")
        result = check.get("result")
        bound = check.get("bound")
        ladder = check.get("ladder")
        cross_check = check.get("cross_check")
        for name, value in (
            ("result", result),
            ("bound", bound),
            ("ladder", ladder),
            ("cross_check", cross_check),
        ):
            if not isinstance(value, dict):
                raise ValueError(f"{context}.{name} must be an object")
        _relative_path_text(result.get("path"), f"{context}.result.path")
        _json_pointer_text(result.get("value_pointer"), f"{context}.result.value_pointer")
        expected_kind = "upper_bound" if sense == "maximize" else "lower_bound"
        if bound.get("kind") != expected_kind:
            raise ValueError(
                f"{context}.bound.kind must be {expected_kind} for {sense}"
            )
        _relative_path_text(bound.get("path"), f"{context}.bound.path")
        _json_pointer_text(bound.get("value_pointer"), f"{context}.bound.value_pointer")
        if not isinstance(bound.get("method"), str) or not bound["method"].strip():
            raise ValueError(f"{context}.bound.method must be nonempty")
        if not isinstance(bound.get("proof"), str) or not bound["proof"].strip():
            raise ValueError(f"{context}.bound.proof must be a locator")
        _relative_path_text(ladder.get("path"), f"{context}.ladder.path")
        _json_pointer_text(ladder.get("entries_pointer"), f"{context}.ladder.entries_pointer")
        for field_name in ("budget_key", "objective_key"):
            if not isinstance(ladder.get(field_name), str) or not ladder[field_name].strip():
                raise ValueError(f"{context}.ladder.{field_name} must be nonempty")
        _positive_integer(
            ladder.get("minimum_levels"), f"{context}.ladder.minimum_levels", minimum=2
        )
        plateau = ladder.get("plateau")
        if not isinstance(plateau, dict):
            raise ValueError(f"{context}.ladder.plateau must be an object")
        if plateau.get("interpretation") not in {"required_evidence", "diagnostic_only"}:
            raise ValueError(
                f"{context}.ladder.plateau.interpretation must be required_evidence or diagnostic_only"
            )
        _positive_integer(
            plateau.get("window"), f"{context}.ladder.plateau.window", minimum=2
        )
        _nonnegative_number(
            plateau.get("tolerance"), f"{context}.ladder.plateau.tolerance"
        )
        _json_pointer_text(
            plateau.get("explanation_pointer"),
            f"{context}.ladder.plateau.explanation_pointer",
        )
        _relative_path_text(cross_check.get("path"), f"{context}.cross_check.path")
        _json_pointer_text(
            cross_check.get("algorithms_pointer"),
            f"{context}.cross_check.algorithms_pointer",
        )
        if not isinstance(cross_check.get("family_key"), str) or not cross_check["family_key"].strip():
            raise ValueError(f"{context}.cross_check.family_key must be nonempty")
        _positive_integer(
            cross_check.get("minimum_families"),
            f"{context}.cross_check.minimum_families",
            minimum=2,
        )
        _json_pointer_text(
            cross_check.get("conclusion_pointer"),
            f"{context}.cross_check.conclusion_pointer",
        )
    for index, check in enumerate(data.get("anomaly_checks", [])):
        if not isinstance(check, dict):
            raise ValueError(f"quality contract anomaly_checks[{index}] must be an object")
        if check.get("hard") is True:
            proof = check.get("proof")
            if not isinstance(proof, str) or not proof.strip():
                raise ValueError(
                    f"quality contract v4 hard anomaly {check.get('id') or index} must declare proof"
                )


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


def _json_pointer(value: Any, pointer: str) -> Any:
    current = value
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            raise ValueError(f"JSON pointer does not exist: {pointer}")
    return current


def _read_project_json(project_dir: Path, relative: Any) -> dict[str, Any]:
    path = _project_locator_path(relative, project_dir)
    if path is None:
        raise ValueError(f"missing or unsafe project JSON: {relative}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid project JSON {relative}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"project JSON root must be an object: {relative}")
    return value


def _finite_value(value: Any, context: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{context} must be a finite number")
    return float(value)


def _evaluate_competitiveness(
    check: dict[str, Any], project_dir: Path
) -> tuple[CompetitivenessResult, list[Finding], list[Finding]]:
    check_id = str(check["id"])
    sense = str(check["objective_sense"])
    failures: list[Finding] = []
    warnings: list[Finding] = []
    objective: float | None = None
    bound_value: float | None = None
    absolute_gap: float | None = None
    relative_gap: float | None = None
    ladder_values: list[float] = []
    plateau_observed: bool | None = None
    families: list[str] = []
    bound_kind = str(check["bound"]["kind"])
    plateau = check["ladder"]["plateau"]
    interpretation = str(plateau["interpretation"])

    try:
        result_json = _read_project_json(project_dir, check["result"]["path"])
        objective = _finite_value(
            _json_pointer(result_json, check["result"]["value_pointer"]),
            f"{check_id}.objective",
        )
        bound_json = _read_project_json(project_dir, check["bound"]["path"])
        bound_value = _finite_value(
            _json_pointer(bound_json, check["bound"]["value_pointer"]),
            f"{check_id}.bound",
        )
    except ValueError as exc:
        failures.append(Finding("BOUND_EVIDENCE_INVALID", str(exc), check_id))

    if _project_locator_path(check["bound"]["proof"], project_dir) is None:
        failures.append(
            Finding(
                "BOUND_PROOF_MISSING",
                "declared relaxation-bound proof locator is missing or unsafe",
                check_id,
            )
        )

    ladder_levels = 0
    try:
        ladder_json = _read_project_json(project_dir, check["ladder"]["path"])
        entries = _json_pointer(ladder_json, check["ladder"]["entries_pointer"])
        if not isinstance(entries, list):
            raise ValueError("ladder entries must be an array")
        ladder_levels = len(entries)
        minimum_levels = int(check["ladder"]["minimum_levels"])
        if ladder_levels < minimum_levels:
            failures.append(
                Finding(
                    "INSUFFICIENT_BUDGET_LADDER",
                    f"ladder has {ladder_levels} levels; requires {minimum_levels}",
                    check_id,
                )
            )
        budgets: list[float] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ValueError(f"ladder[{index}] must be an object")
            budgets.append(
                _finite_value(entry.get(check["ladder"]["budget_key"]), f"ladder[{index}].budget")
            )
            ladder_values.append(
                _finite_value(
                    entry.get(check["ladder"]["objective_key"]),
                    f"ladder[{index}].objective",
                )
            )
        if any(right <= left for left, right in zip(budgets, budgets[1:])):
            failures.append(
                Finding(
                    "NON_INCREASING_BUDGET_LADDER",
                    "budget levels must be strictly increasing",
                    check_id,
                )
            )
        tolerance = float(plateau["tolerance"])
        if sense == "maximize":
            regressed = any(
                right + tolerance < left
                for left, right in zip(ladder_values, ladder_values[1:])
            )
        else:
            regressed = any(
                right - tolerance > left
                for left, right in zip(ladder_values, ladder_values[1:])
            )
        if regressed:
            failures.append(
                Finding(
                    "NON_MONOTONE_BUDGET_LADDER",
                    f"{sense} objective regresses as budget increases",
                    check_id,
                )
            )
        window = int(plateau["window"])
        if len(ladder_values) >= window:
            tail = ladder_values[-window:]
            plateau_observed = max(tail) - min(tail) <= tolerance
        else:
            plateau_observed = False
        explanation = _json_pointer(ladder_json, plateau["explanation_pointer"])
        explanation_ok = isinstance(explanation, str) and bool(explanation.strip())
        if interpretation == "required_evidence" and (
            not plateau_observed or not explanation_ok
        ):
            failures.append(
                Finding(
                    "PLATEAU_EVIDENCE_MISSING",
                    "required plateau was not established with a nonempty explanation",
                    check_id,
                )
            )
        elif interpretation == "diagnostic_only" and not plateau_observed:
            warnings.append(
                Finding(
                    "PLATEAU_NOT_ESTABLISHED_DIAGNOSTIC",
                    "budget ladder has not flattened; contract marks this diagnostic only",
                    check_id,
                )
            )
    except ValueError as exc:
        failures.append(Finding("LADDER_EVIDENCE_INVALID", str(exc), check_id))

    if objective is not None and bound_value is not None:
        feasible_values = [objective, *ladder_values]
        tolerance = float(plateau["tolerance"])
        bound_tolerance = 1e-9 * max(
            1.0, abs(bound_value), *(abs(value) for value in feasible_values)
        )
        invalid_bound = (
            bound_value + bound_tolerance < max(feasible_values)
            if sense == "maximize"
            else bound_value - bound_tolerance > min(feasible_values)
        )
        if invalid_bound:
            failures.append(
                Finding(
                    "INVALID_RELAXATION_BOUND",
                    f"{bound_kind} contradicts an observed feasible objective",
                    check_id,
                )
            )
        absolute_gap = (
            bound_value - objective if sense == "maximize" else objective - bound_value
        )
        relative_gap = absolute_gap / max(abs(bound_value), abs(objective), 1e-12)
        best_ladder = max(ladder_values) if sense == "maximize" else min(ladder_values)
        final_worse = (
            objective + tolerance < best_ladder
            if sense == "maximize"
            else objective - tolerance > best_ladder
        ) if ladder_values else False
        if final_worse:
            failures.append(
                Finding(
                    "FINAL_OBJECTIVE_WORSE_THAN_LADDER",
                    "canonical objective is worse than an observed ladder solution",
                    check_id,
                )
            )

    try:
        cross = _read_project_json(project_dir, check["cross_check"]["path"])
        algorithms = _json_pointer(cross, check["cross_check"]["algorithms_pointer"])
        conclusion = _json_pointer(cross, check["cross_check"]["conclusion_pointer"])
        if not isinstance(algorithms, list):
            raise ValueError("cross-check algorithms must be an array")
        family_key = check["cross_check"]["family_key"]
        families = sorted(
            {
                str(item[family_key]).strip()
                for item in algorithms
                if isinstance(item, dict)
                and isinstance(item.get(family_key), str)
                and item[family_key].strip()
            }
        )
        if len(families) < int(check["cross_check"]["minimum_families"]):
            raise ValueError("cross-check has too few distinct algorithm families")
        if not isinstance(conclusion, str) or not conclusion.strip():
            raise ValueError("cross-check conclusion must be nonempty")
    except ValueError as exc:
        failures.append(Finding("CROSS_CHECK_INVALID", str(exc), check_id))

    return (
        CompetitivenessResult(
            check_id=check_id,
            objective_sense=sense,
            objective=objective,
            bound_kind=bound_kind,
            bound_value=bound_value,
            absolute_gap=absolute_gap,
            relative_gap=relative_gap,
            ladder_levels=ladder_levels,
            plateau_interpretation=interpretation,
            plateau_observed=plateau_observed,
            cross_check_families=families,
            passed=not failures,
        ),
        failures,
        warnings,
    )


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
        if contract.get("version") == 4 and severity == "hard":
            if _project_locator_path(claim.get("source"), project_dir) is None:
                result.failures.append(
                    Finding(
                        code="HARD_CLAIM_SOURCE_MISSING",
                        item_id=claim_id,
                        message="hard claim source locator is missing or unsafe",
                    )
                )
            missing_implementations = [
                locator
                for locator in claim.get("implementation", [])
                if _project_locator_path(locator, project_dir) is None
            ]
            if missing_implementations:
                result.failures.append(
                    Finding(
                        code="HARD_CLAIM_IMPLEMENTATION_MISSING",
                        item_id=claim_id,
                        message=(
                            "hard claim implementation locators are missing or unsafe: "
                            + ", ".join(str(item) for item in missing_implementations)
                        ),
                    )
                )
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
        configured_continuous_time_evidence = 0
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
                if evidence_level == "dual_impl" or evidence_type in CONTINUOUS_TIME_PROOF_TYPES:
                    configured_continuous_time_evidence += 1
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
        if (
            severity == "hard"
            and claim.get("constraint_domain") == "continuous_time"
            and configured_continuous_time_evidence == 0
        ):
            result.failures.append(
                Finding(
                    code="MISSING_CONTINUOUS_TIME_CERTIFICATE",
                    item_id=claim_id,
                    message=(
                        "continuous-time hard claims require independent event localization, "
                        "a certified interval/error bound, or structurally validated dual_impl "
                        "evidence; rechecking the same sampled array cannot certify hard PASS"
                    ),
                )
            )

    for check in contract.get("anomaly_checks", []):
        check_id = str(check.get("id") or "")
        hard = check.get("hard") is True
        justification = str(check.get("justification") or "").strip()
        failed = str(check.get("status") or "unknown").lower() in {"fail", "failed"}
        if (
            contract.get("version") == 4
            and hard
            and _project_locator_path(check.get("proof"), project_dir) is None
        ):
            result.failures.append(
                Finding(
                    code="HARD_ANOMALY_PROOF_MISSING",
                    item_id=check_id,
                    message="hard anomaly proof locator is missing or unsafe",
                )
            )
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

    if contract.get("version") == 4:
        for check in contract.get("competitiveness_checks", []):
            item, failures, warnings = _evaluate_competitiveness(check, project_dir)
            result.competitiveness_results.append(item)
            result.failures.extend(failures)
            result.warnings.extend(warnings)

    result.passed = not result.failures
    return result
