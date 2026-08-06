#!/usr/bin/env python3
"""Compare legacy/new judge decisions and emit an advisory cutover assessment.

The command never edits routing configuration or invokes production judges.
``cutover_ready`` means only that the hash-bound R0a capability/repeatability
report and explicitly configured shadow thresholds passed; an operator must
still authorize any rollout.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts.capability_harness import (
        DECISIONS,
        REPORT_SCHEMA,
        CapabilityError,
        _is_sha256,
        _read_json,
        _safe_relative,
        _write_json,
        binomial_metric,
        canonical_hash,
        check_capability_thresholds,
        decision_class,
        evaluator_identity,
        evaluator_fingerprint,
        file_sha256,
        tree_sha256,
    )
    from scripts.hard_gate_calibration import (
        REPORT_SCHEMA as HARD_GATE_CALIBRATION_REPORT_SCHEMA,
    )
except ModuleNotFoundError:  # Direct execution from scripts/.
    from capability_harness import (  # type: ignore
        DECISIONS,
        REPORT_SCHEMA,
        CapabilityError,
        _is_sha256,
        _read_json,
        _safe_relative,
        _write_json,
        binomial_metric,
        canonical_hash,
        check_capability_thresholds,
        decision_class,
        evaluator_identity,
        evaluator_fingerprint,
        file_sha256,
        tree_sha256,
    )
    from hard_gate_calibration import (  # type: ignore
        REPORT_SCHEMA as HARD_GATE_CALIBRATION_REPORT_SCHEMA,
    )


LEGACY_SHADOW_SCHEMA = "judge-shadow-manifest-v1"
SHADOW_SCHEMA = "judge-shadow-manifest-v2"
SHADOW_REPORT_SCHEMA = "judge-shadow-report-v2"
SHADOW_THRESHOLDS = {
    "agreement_rate_min",
    "new_indeterminate_rate_max",
    "relaxation_rate_max",
}


def _required_text(mapping: dict[str, Any], field: str, context: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CapabilityError(f"{context}.{field} must be a non-empty string")
    return value


def _rate(value: Any, context: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= float(value) <= 1:
        raise CapabilityError(f"{context} must be between 0 and 1")
    return float(value)


def _integer(value: Any, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CapabilityError(f"{context} must be a positive integer")
    return value


def _blocked(decision: str) -> bool:
    return decision_class(decision).startswith("BLOCK_")


def _find_matrix_row(report: dict[str, Any], role: str, fingerprint: str) -> dict[str, Any]:
    matrix = report.get("capability_matrix")
    if not isinstance(matrix, list):
        raise CapabilityError("capability report has no capability_matrix")
    rows = [
        row for row in matrix
        if isinstance(row, dict)
        and row.get("role") == role
        and row.get("evaluator_identity_fingerprint") == fingerprint
    ]
    if len(rows) != 1:
        raise CapabilityError("target route must match exactly one capability matrix row")
    row = rows[0]
    if row.get("permitted_use") != "ROLE_ROUTING_AND_SHADOW_ELIGIBILITY_ONLY" or row.get("truth_claim") != "NONE":
        raise CapabilityError("capability matrix row overstates its permitted use")
    return row


def _shadow_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    raw_agreements = sum(row["legacy_decision"] == row["new_decision"] for row in rows)
    agreements = sum(
        decision_class(row["legacy_decision"]) == decision_class(row["new_decision"])
        for row in rows
    )
    new_indeterminate = sum(
        decision_class(row["new_decision"]) == "INDETERMINATE" for row in rows
    )
    relaxations = sum(
        _blocked(row["legacy_decision"]) and decision_class(row["new_decision"]) == "PASS"
        for row in rows
    )
    stricter = sum(
        decision_class(row["legacy_decision"]) == "PASS" and _blocked(row["new_decision"])
        for row in rows
    )
    return {
        "agreement_rate": binomial_metric(agreements, total),
        "disagreement_rate": binomial_metric(total - agreements, total),
        "raw_label_agreement_rate": binomial_metric(raw_agreements, total),
        "new_indeterminate_rate": binomial_metric(new_indeterminate, total),
        "relaxation_rate": binomial_metric(relaxations, total),
        "stricter_rate": binomial_metric(stricter, total),
    }


def _check_shadow_thresholds(
    metrics: dict[str, Any], count: int, thresholds: dict[str, Any]
) -> list[dict[str, Any]]:
    configured = thresholds.get("shadow")
    if not isinstance(configured, dict) or set(configured) != SHADOW_THRESHOLDS:
        raise CapabilityError(f"thresholds.shadow must configure exactly {sorted(SHADOW_THRESHOLDS)}")
    checks: list[dict[str, Any]] = []
    mapping = {
        "agreement_rate_min": ("agreement_rate", "low", ">="),
        "new_indeterminate_rate_max": ("new_indeterminate_rate", "high", "<="),
        "relaxation_rate_max": ("relaxation_rate", "high", "<="),
    }
    for threshold_name, (metric_name, bound, operator) in mapping.items():
        threshold = _rate(configured[threshold_name], f"thresholds.shadow.{threshold_name}")
        observed = metrics[metric_name]["wilson_95"][bound]
        passed = observed >= threshold if operator == ">=" else observed <= threshold
        checks.append(
            {
                "name": threshold_name,
                "observed": observed,
                "threshold": threshold,
                "comparison": f"wilson_95.{bound} {operator} threshold",
                "passed": passed,
            }
        )
    minimum = _integer(thresholds.get("minimum_shadow_cases"), "thresholds.minimum_shadow_cases")
    checks.append(
        {
            "name": "minimum_shadow_cases",
            "observed": count,
            "threshold": minimum,
            "comparison": "observed >= threshold",
            "passed": count >= minimum,
        }
    )
    return checks


def _hard_gate_calibration_check(
    manifest: dict[str, Any],
    root: Path,
    *,
    capability_report_sha256: str,
    identity: dict[str, str],
    fingerprint: str,
    role: str,
    thresholds: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any], str | None]:
    if manifest.get("schema") == LEGACY_SHADOW_SCHEMA:
        return (
            None,
            {
                "name": "r0a_hard_gate_calibration",
                "observed": "MISSING_LEGACY_CAPABILITY_ONLY_MANIFEST",
                "threshold": "HASH_BOUND_HARD_GATE_READY",
                "comparison": "v2 R0a report required",
                "passed": False,
            },
            None,
        )
    report_path = _safe_relative(root, manifest.get("hard_gate_calibration_report_path"))
    if not report_path.is_file():
        raise CapabilityError("hard-gate calibration report path must be a regular file")
    expected_hash = manifest.get("hard_gate_calibration_report_sha256")
    if not _is_sha256(expected_hash) or file_sha256(report_path) != expected_hash:
        raise CapabilityError("hard-gate calibration report is not pinned by the shadow manifest")
    report = _read_json(report_path)
    if report.get("schema") != HARD_GATE_CALIBRATION_REPORT_SCHEMA:
        raise CapabilityError("hard-gate calibration report schema mismatch")
    if report.get("claim_limit") != "EXACT_RUNTIME_ORACLE_CAPABILITY_AND_REPEATABILITY_ONLY":
        raise CapabilityError("hard-gate calibration report lacks its bounded claim limit")
    if report.get("automatic_switch_performed") is not False or report.get("operator_authorization_required") is not True:
        raise CapabilityError("hard-gate calibration report violates manual authorization policy")
    if report.get("capability_report_sha256") != capability_report_sha256:
        raise CapabilityError("hard-gate calibration and shadow capability reports differ")
    if report.get("evaluator") != identity or report.get("evaluator_identity_fingerprint") != fingerprint:
        raise CapabilityError("hard-gate calibration evaluator differs from the target route")
    required_roles = report.get("required_roles")
    if not isinstance(required_roles, list) or set(required_roles) != {"math", "execution"}:
        raise CapabilityError("hard-gate calibration does not cover both hard roles")
    role_result = report.get("roles", {}).get(role)
    if not isinstance(role_result, dict):
        raise CapabilityError("hard-gate calibration does not cover the target role")
    calibration_thresholds = report.get("thresholds")
    if not isinstance(calibration_thresholds, dict) or (
        calibration_thresholds.get("minimum_test_cases") != thresholds.get("minimum_test_cases")
        or calibration_thresholds.get("capability") != thresholds.get("capability")
    ):
        raise CapabilityError("shadow capability thresholds differ from the R0a calibration")
    ready = report.get("hard_gate_ready") is True and role_result.get("ready") is True
    return (
        report,
        {
            "name": "r0a_hard_gate_calibration",
            "observed": "READY" if ready else "NOT_READY",
            "threshold": "HASH_BOUND_HARD_GATE_READY",
            "comparison": "hard_gate_ready and target role ready",
            "passed": ready,
        },
        expected_hash,
    )


def evaluate_shadow(
    manifest: dict[str, Any], root: Path, capability_report: dict[str, Any], capability_path: Path
) -> dict[str, Any]:
    if manifest.get("schema") not in {LEGACY_SHADOW_SCHEMA, SHADOW_SCHEMA}:
        raise CapabilityError(
            f"shadow manifest schema must be {SHADOW_SCHEMA} (or legacy diagnostic {LEGACY_SHADOW_SCHEMA})"
        )
    if capability_report.get("schema") != REPORT_SCHEMA:
        raise CapabilityError(f"capability report schema must be {REPORT_SCHEMA}")
    if _read_json(capability_path) != capability_report:
        raise CapabilityError("in-memory capability report differs from the pinned file")
    if capability_report.get("claim_limit") != "ORACLE_BACKED_MUTATION_CAPABILITY_ONLY":
        raise CapabilityError("capability report lacks a bounded claim limit")
    expected_hash = manifest.get("capability_report_sha256")
    if not _is_sha256(expected_hash) or file_sha256(capability_path) != expected_hash:
        raise CapabilityError("capability report is not pinned by the shadow manifest")

    target = manifest.get("target_route")
    if not isinstance(target, dict):
        raise CapabilityError("target_route must be an object")
    role = _required_text(target, "role", "target_route")
    identity_value = target.get("evaluator")
    if not isinstance(identity_value, dict):
        raise CapabilityError("target_route.evaluator must be an object")
    identity = evaluator_identity(identity_value)
    fingerprint = evaluator_fingerprint(identity)
    if target.get("evaluator_identity_fingerprint") != fingerprint:
        raise CapabilityError("target route evaluator fingerprint mismatch")
    if capability_report.get("evaluator_identity_fingerprint") != fingerprint:
        raise CapabilityError("target route evaluator differs from capability report")
    row = _find_matrix_row(capability_report, role, fingerprint)

    raw_rows = manifest.get("cases")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise CapabilityError("shadow manifest needs at least one case")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_rows):
        if not isinstance(item, dict):
            raise CapabilityError(f"shadow cases[{index}] must be an object")
        case_id = _required_text(item, "id", f"shadow cases[{index}]")
        if case_id in seen:
            raise CapabilityError(f"duplicate shadow case id: {case_id}")
        seen.add(case_id)
        case_role = _required_text(item, "role", f"shadow cases[{index}]")
        project_id = _required_text(item, "project_id", f"shadow cases[{index}]")
        problem_id = _required_text(item, "problem_id", f"shadow cases[{index}]")
        if case_role != role:
            raise CapabilityError(f"shadow case {case_id} does not match target role")
        packet = _safe_relative(root, item.get("packet_path"))
        if not packet.is_dir():
            raise CapabilityError(f"shadow case {case_id} packet_path must be a directory")
        packet_hash = tree_sha256(packet)
        runtime = item.get("new_runtime_identity")
        if not isinstance(runtime, dict):
            raise CapabilityError(f"shadow case {case_id} lacks new_runtime_identity")
        expected_runtime = {**identity, "packet_sha256": packet_hash}
        if runtime != expected_runtime:
            raise CapabilityError(f"shadow case {case_id} runtime identity or packet hash mismatch")
        legacy = item.get("legacy_decision")
        new = item.get("new_decision")
        if legacy not in DECISIONS or new not in DECISIONS:
            raise CapabilityError(f"shadow case {case_id} has invalid decision")
        rows.append(
            {
                "id": case_id,
                "project_id": project_id,
                "problem_id": problem_id,
                "role": role,
                "packet_sha256": packet_hash,
                "legacy_decision": legacy,
                "new_decision": new,
                "legacy_action": decision_class(legacy),
                "new_action": decision_class(new),
            }
        )

    disjoint_axes = manifest.get("disjoint_from_capability_by")
    if not isinstance(disjoint_axes, list) or not disjoint_axes or any(
        axis not in {"project_id", "problem_id"} for axis in disjoint_axes
    ):
        raise CapabilityError("disjoint_from_capability_by must select project_id and/or problem_id")
    scope = row.get("scope") if isinstance(row.get("scope"), dict) else {}
    disjoint_audit: dict[str, list[str]] = {}
    for axis in disjoint_axes:
        capability_values = set(scope.get(f"{axis}s", []))
        shadow_values = {item[axis] for item in rows}
        overlap = sorted(capability_values & shadow_values)
        disjoint_audit[axis] = overlap
        if overlap:
            raise CapabilityError(f"shadow/capability leakage on {axis}: {', '.join(overlap)}")

    thresholds = manifest.get("thresholds")
    if not isinstance(thresholds, dict):
        raise CapabilityError("thresholds must be an object")
    capability_checks = check_capability_thresholds(row, thresholds)
    _hard_gate_report, hard_gate_check, hard_gate_hash = _hard_gate_calibration_check(
        manifest,
        root,
        capability_report_sha256=expected_hash,
        identity=identity,
        fingerprint=fingerprint,
        role=role,
        thresholds=thresholds,
    )
    metrics = _shadow_metrics(rows)
    shadow_checks = _check_shadow_thresholds(metrics, len(rows), thresholds)
    minimum_projects = _integer(
        thresholds.get("minimum_shadow_projects"), "thresholds.minimum_shadow_projects"
    )
    observed_projects = len({item["project_id"] for item in rows})
    shadow_checks.append(
        {
            "name": "minimum_shadow_projects",
            "observed": observed_projects,
            "threshold": minimum_projects,
            "comparison": "observed >= threshold",
            "passed": observed_projects >= minimum_projects,
        }
    )
    checks = capability_checks + [hard_gate_check] + shadow_checks
    ready = all(check["passed"] for check in checks)
    return {
        "schema": SHADOW_REPORT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "shadow_manifest_sha256": canonical_hash(manifest),
        "capability_report_sha256": expected_hash,
        "hard_gate_calibration_report_sha256": hard_gate_hash,
        "target_route": {
            "role": role,
            "evaluator_identity_fingerprint": fingerprint,
            "capability_scope": row["scope"],
        },
        "shadow_cases": rows,
        "shadow_metrics": metrics,
        "holdout_audit": {"axes": disjoint_axes, "overlap": disjoint_audit, "passed": True},
        "threshold_checks": checks,
        "cutover_ready": ready,
        "legacy_capability_only": manifest.get("schema") == LEGACY_SHADOW_SCHEMA,
        "advisory_only": True,
        "automatic_switch_performed": False,
        "operator_authorization_required": True,
        "claim_limit": "SHADOW_AGREEMENT_AND_ORACLE_CAPABILITY_NOT_HUMAN_TRUTH",
        "award_prediction": "UNAVAILABLE_WITHOUT_HUMAN_CALIBRATION",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--capability-report", required=True, type=Path)
    parser.add_argument("--json-output", required=True, type=Path)
    parser.add_argument("--require-ready", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest_path = args.manifest.resolve()
        capability_path = args.capability_report.resolve()
        report = evaluate_shadow(
            _read_json(manifest_path), manifest_path.parent, _read_json(capability_path), capability_path
        )
        output = args.json_output.resolve()
        _write_json(output, report)
        print(output)
        if args.require_ready and not report["cutover_ready"]:
            return 3
        return 0
    except CapabilityError as exc:
        print(f"shadow cutover rejected input: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
