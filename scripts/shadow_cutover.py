#!/usr/bin/env python3
"""Compare legacy/new judge decisions and emit an advisory cutover assessment.

The command never edits routing configuration or invokes production judges.
``cutover_ready`` means only that the explicitly configured capability and
shadow thresholds passed; an operator must still authorize any rollout.
"""

from __future__ import annotations

import argparse
import json
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
        decision_class,
        evaluator_identity,
        evaluator_fingerprint,
        file_sha256,
        tree_sha256,
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
        decision_class,
        evaluator_identity,
        evaluator_fingerprint,
        file_sha256,
        tree_sha256,
    )


SHADOW_SCHEMA = "judge-shadow-manifest-v1"
SHADOW_REPORT_SCHEMA = "judge-shadow-report-v1"
LOWER_BOUND_METRICS = {
    "sensitivity",
    "specificity",
    "precision",
    "evidence_grounding_rate",
}
UPPER_BOUND_METRICS = {
    "neutral_flip_rate",
    "position_bias_rate",
    "indeterminate_rate",
    "false_reopen_rate",
}
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


def _check_capability_thresholds(
    row: dict[str, Any], thresholds: dict[str, Any]
) -> list[dict[str, Any]]:
    configured = thresholds.get("capability")
    required = LOWER_BOUND_METRICS | UPPER_BOUND_METRICS
    if not isinstance(configured, dict) or set(configured) != required:
        raise CapabilityError(f"thresholds.capability must configure exactly {sorted(required)}")
    metrics = row.get("metrics")
    if not isinstance(metrics, dict):
        raise CapabilityError("capability matrix row has no metrics")
    checks: list[dict[str, Any]] = []
    for name in sorted(required):
        metric = metrics.get(name)
        if not isinstance(metric, dict):
            raise CapabilityError(f"missing capability metric: {name}")
        interval = metric.get("wilson_95")
        if not isinstance(interval, dict):
            raise CapabilityError(f"missing Wilson interval: {name}")
        if name in LOWER_BOUND_METRICS:
            threshold = _rate(configured[name], f"thresholds.capability.{name}")
            observed = interval.get("low")
            passed = isinstance(observed, (int, float)) and observed >= threshold
            comparison = "wilson_95.low >= threshold"
        else:
            threshold = _rate(configured[name], f"thresholds.capability.{name}")
            observed = interval.get("high")
            passed = isinstance(observed, (int, float)) and observed <= threshold
            comparison = "wilson_95.high <= threshold"
        checks.append(
            {
                "name": name,
                "observed": observed,
                "threshold": threshold,
                "comparison": comparison,
                "passed": passed,
            }
        )
    minimum = _integer(thresholds.get("minimum_test_cases"), "thresholds.minimum_test_cases")
    observed_cases = row.get("test_cases")
    checks.append(
        {
            "name": "minimum_test_cases",
            "observed": observed_cases,
            "threshold": minimum,
            "comparison": "observed >= threshold",
            "passed": isinstance(observed_cases, int) and observed_cases >= minimum,
        }
    )
    return checks


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


def evaluate_shadow(
    manifest: dict[str, Any], root: Path, capability_report: dict[str, Any], capability_path: Path
) -> dict[str, Any]:
    if manifest.get("schema") != SHADOW_SCHEMA:
        raise CapabilityError(f"shadow manifest schema must be {SHADOW_SCHEMA}")
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
    capability_checks = _check_capability_thresholds(row, thresholds)
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
    checks = capability_checks + shadow_checks
    ready = all(check["passed"] for check in checks)
    return {
        "schema": SHADOW_REPORT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "shadow_manifest_sha256": canonical_hash(manifest),
        "capability_report_sha256": expected_hash,
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
