#!/usr/bin/env python3
"""Calibrate exact-runtime math/execution hard-gate capability.

R0a combines two bounded forms of evidence for every held-out mutation packet:
an oracle-backed capability observation and repeated decisions under the exact
same evaluator and packet identity.  It never calls a judge, changes routing,
or claims human/award validity.
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
        OBSERVATION_SCHEMA,
        PREPARED_SCHEMA,
        REPORT_SCHEMA as CAPABILITY_REPORT_SCHEMA,
        CapabilityError,
        _is_sha256,
        _read_json,
        _safe_relative,
        _write_json,
        canonical_hash,
        check_capability_thresholds,
        decision_class,
        evaluator_fingerprint,
        evaluator_identity,
        file_sha256,
        runtime_fingerprint,
        tree_sha256,
    )
    from scripts.judge_reliability import (
        DEFAULT_MIN_RUNS,
        INPUT_SCHEMA as RELIABILITY_INPUT_SCHEMA,
        ReliabilityError,
        aggregate_reliability,
    )
except ModuleNotFoundError:  # Direct execution from scripts/.
    from capability_harness import (  # type: ignore
        DECISIONS,
        OBSERVATION_SCHEMA,
        PREPARED_SCHEMA,
        REPORT_SCHEMA as CAPABILITY_REPORT_SCHEMA,
        CapabilityError,
        _is_sha256,
        _read_json,
        _safe_relative,
        _write_json,
        canonical_hash,
        check_capability_thresholds,
        decision_class,
        evaluator_fingerprint,
        evaluator_identity,
        file_sha256,
        runtime_fingerprint,
        tree_sha256,
    )
    from judge_reliability import (  # type: ignore
        DEFAULT_MIN_RUNS,
        INPUT_SCHEMA as RELIABILITY_INPUT_SCHEMA,
        ReliabilityError,
        aggregate_reliability,
    )


MANIFEST_SCHEMA = "judge-hard-gate-calibration-manifest-v1"
REPORT_SCHEMA = "judge-hard-gate-calibration-report-v1"
REQUIRED_HARD_ROLES = {"math", "execution"}
REQUIRED_CASE_KINDS = {"hard_defect", "neutral_transform"}
MINIMUM_R0A_RUNS = 5


def _required_text(mapping: dict[str, Any], field: str, context: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CapabilityError(f"{context}.{field} must be a non-empty string")
    return value


def _positive_integer(value: Any, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CapabilityError(f"{context} must be a positive integer")
    return value


def _pinned_file(
    value: dict[str, Any], path: Path, expected_hash: Any, label: str
) -> None:
    if _read_json(path) != value:
        raise CapabilityError(f"in-memory {label} differs from the pinned file")
    if not _is_sha256(expected_hash) or file_sha256(path) != expected_hash:
        raise CapabilityError(f"{label} is not pinned by the calibration manifest")


def _matrix_row(
    capability_report: dict[str, Any], role: str, fingerprint: str
) -> dict[str, Any]:
    matrix = capability_report.get("capability_matrix")
    if not isinstance(matrix, list):
        raise CapabilityError("capability report has no capability_matrix")
    rows = [
        row
        for row in matrix
        if isinstance(row, dict)
        and row.get("role") == role
        and row.get("evaluator_identity_fingerprint") == fingerprint
    ]
    if len(rows) != 1:
        raise CapabilityError(f"role {role} must match exactly one capability matrix row")
    row = rows[0]
    if (
        row.get("permitted_use") != "ROLE_ROUTING_AND_SHADOW_ELIGIBILITY_ONLY"
        or row.get("truth_claim") != "NONE"
    ):
        raise CapabilityError(f"capability matrix row for {role} overstates its permitted use")
    return row


def _normalise_hard_decision(decision: str) -> str:
    action = decision_class(decision)
    if action == "PASS":
        return "PASS"
    if action.startswith("BLOCK_"):
        return "FAIL"
    return "INDETERMINATE"


def _case_scope(cases: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        "project_ids": sorted({case["project_id"] for case in cases}),
        "problem_ids": sorted({case["problem_id"] for case in cases}),
        "mutation_families": sorted({case["mutation_family"] for case in cases}),
        "packet_sha256": sorted({case["packet_sha256"] for case in cases}),
    }


def evaluate_hard_gate_calibration(
    manifest: dict[str, Any],
    manifest_root: Path,
    prepared: dict[str, Any],
    prepared_path: Path,
    capability_report: dict[str, Any],
    capability_path: Path,
) -> dict[str, Any]:
    """Validate and combine capability plus repeatability evidence for R0a."""

    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise CapabilityError(f"calibration manifest schema must be {MANIFEST_SCHEMA}")
    _pinned_file(
        prepared,
        prepared_path,
        manifest.get("prepared_manifest_sha256"),
        "prepared manifest",
    )
    _pinned_file(
        capability_report,
        capability_path,
        manifest.get("capability_report_sha256"),
        "capability report",
    )
    if prepared.get("schema") != PREPARED_SCHEMA:
        raise CapabilityError(f"prepared manifest schema must be {PREPARED_SCHEMA}")
    if capability_report.get("schema") != CAPABILITY_REPORT_SCHEMA:
        raise CapabilityError(f"capability report schema must be {CAPABILITY_REPORT_SCHEMA}")
    if prepared.get("claim_limit") != "ORACLE_BACKED_MUTATION_CAPABILITY_ONLY":
        raise CapabilityError("prepared manifest lacks its bounded capability claim")
    if capability_report.get("claim_limit") != "ORACLE_BACKED_MUTATION_CAPABILITY_ONLY":
        raise CapabilityError("capability report lacks its bounded capability claim")
    if capability_report.get("prepared_manifest_sha256") != canonical_hash(prepared):
        raise CapabilityError("capability report does not bind the prepared manifest content")
    if prepared.get("holdout_audit", {}).get("passed") is not True:
        raise CapabilityError("prepared manifest holdout audit did not pass")
    if capability_report.get("holdout_audit", {}).get("passed") is not True:
        raise CapabilityError("capability report holdout audit did not pass")

    prepared_evaluator_raw = prepared.get("evaluator")
    capability_evaluator_raw = capability_report.get("evaluator")
    if not isinstance(prepared_evaluator_raw, dict) or not isinstance(capability_evaluator_raw, dict):
        raise CapabilityError("prepared/capability evaluator identities must be objects")
    evaluator = evaluator_identity(prepared_evaluator_raw)
    if evaluator_identity(capability_evaluator_raw) != evaluator:
        raise CapabilityError("prepared and capability evaluator identities differ")
    fingerprint = evaluator_fingerprint(evaluator)
    if prepared.get("evaluator_identity_fingerprint") != fingerprint:
        raise CapabilityError("prepared evaluator fingerprint mismatch")
    if capability_report.get("evaluator_identity_fingerprint") != fingerprint:
        raise CapabilityError("capability evaluator fingerprint mismatch")

    required_roles = manifest.get("required_roles")
    if (
        not isinstance(required_roles, list)
        or len(required_roles) != len(REQUIRED_HARD_ROLES)
        or set(required_roles) != REQUIRED_HARD_ROLES
    ):
        raise CapabilityError("required_roles must contain exactly math and execution")
    minimum_runs = _positive_integer(
        manifest.get("minimum_reliability_runs"), "minimum_reliability_runs"
    )
    if minimum_runs < max(DEFAULT_MIN_RUNS, MINIMUM_R0A_RUNS):
        raise CapabilityError(
            f"minimum_reliability_runs must be at least {MINIMUM_R0A_RUNS} for R0a"
        )
    thresholds = manifest.get("thresholds")
    if not isinstance(thresholds, dict):
        raise CapabilityError("thresholds must be an object")

    raw_cases = prepared.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise CapabilityError("prepared manifest has no cases")
    held_out: list[dict[str, Any]] = []
    prepared_root = prepared_path.parent.resolve()
    for index, case in enumerate(raw_cases):
        if not isinstance(case, dict):
            raise CapabilityError(f"prepared cases[{index}] must be an object")
        if case.get("split") != "test" or case.get("role") not in REQUIRED_HARD_ROLES:
            continue
        context = f"prepared cases[{index}]"
        for field in ("id", "project_id", "problem_id", "mutation_family", "role", "kind"):
            _required_text(case, field, context)
        if case["kind"] not in REQUIRED_CASE_KINDS:
            raise CapabilityError(f"{context}.kind is not a hard-gate calibration kind")
        if case.get("oracle_validation", {}).get("passed") is not True:
            raise CapabilityError(f"prepared case {case['id']} lacks a passed oracle receipt")
        packet = _safe_relative(prepared_root, case.get("packet_path"))
        if not packet.is_dir() or tree_sha256(packet) != case.get("packet_sha256"):
            raise CapabilityError(f"prepared packet hash mismatch for {case['id']}")
        expected_runtime = {**evaluator, "packet_sha256": case["packet_sha256"]}
        if case.get("runtime_identity") != expected_runtime:
            raise CapabilityError(f"prepared exact runtime identity mismatch for {case['id']}")
        if case.get("runtime_identity_fingerprint") != runtime_fingerprint(expected_runtime):
            raise CapabilityError(f"prepared runtime fingerprint mismatch for {case['id']}")
        held_out.append(case)
    if len({case["id"] for case in held_out}) != len(held_out):
        raise CapabilityError("held-out hard-gate case ids must be unique")
    for role in required_roles:
        kinds = {case["kind"] for case in held_out if case["role"] == role}
        if kinds != REQUIRED_CASE_KINDS:
            raise CapabilityError(
                f"role {role} must include held-out hard_defect and neutral_transform cases"
            )

    receipts_raw = capability_report.get("observation_receipts")
    if not isinstance(receipts_raw, list):
        raise CapabilityError("capability report lacks observation_receipts")
    receipts: dict[str, dict[str, Any]] = {}
    for receipt in receipts_raw:
        if not isinstance(receipt, dict):
            raise CapabilityError("capability observation receipt must be an object")
        case_id = _required_text(receipt, "case_id", "observation receipt")
        if case_id in receipts:
            raise CapabilityError(f"duplicate capability observation receipt: {case_id}")
        receipts[case_id] = receipt

    reliability_raw = manifest.get("reliability_cases")
    if not isinstance(reliability_raw, list):
        raise CapabilityError("reliability_cases must be an array")
    reliability_refs: dict[str, dict[str, Any]] = {}
    for index, reference in enumerate(reliability_raw):
        if not isinstance(reference, dict):
            raise CapabilityError(f"reliability_cases[{index}] must be an object")
        case_id = _required_text(reference, "case_id", f"reliability_cases[{index}]")
        if case_id in reliability_refs:
            raise CapabilityError(f"duplicate reliability case: {case_id}")
        reliability_refs[case_id] = reference
    expected_case_ids = {case["id"] for case in held_out}
    if set(reliability_refs) != expected_case_ids:
        raise CapabilityError(
            "reliability_cases must exactly cover every required held-out test case"
        )

    role_capability_checks: dict[str, list[dict[str, Any]]] = {}
    for role in required_roles:
        role_cases = [case for case in held_out if case["role"] == role]
        row = _matrix_row(capability_report, role, fingerprint)
        if row.get("test_cases") != len(role_cases) or row.get("scope") != _case_scope(role_cases):
            raise CapabilityError(f"capability matrix scope/count mismatch for role {role}")
        role_capability_checks[role] = check_capability_thresholds(row, thresholds)

    case_results: list[dict[str, Any]] = []
    for case in sorted(held_out, key=lambda value: value["id"]):
        case_id = case["id"]
        receipt = receipts.get(case_id)
        if receipt is None:
            raise CapabilityError(f"capability report lacks observation receipt for {case_id}")
        if receipt.get("runtime_identity_fingerprint") != case["runtime_identity_fingerprint"]:
            raise CapabilityError(f"capability observation runtime fingerprint mismatch for {case_id}")
        observation_path = _safe_relative(prepared_root, case.get("observation_path"))
        if not observation_path.is_file() or file_sha256(observation_path) != receipt.get("observation_sha256"):
            raise CapabilityError(f"capability observation hash mismatch for {case_id}")
        observation = _read_json(observation_path)
        if observation.get("schema") != OBSERVATION_SCHEMA:
            raise CapabilityError(f"capability observation schema mismatch for {case_id}")
        if observation.get("case_id") != case_id or observation.get("runtime_identity") != case["runtime_identity"]:
            raise CapabilityError(f"capability observation identity mismatch for {case_id}")
        observation_decision = observation.get("decision")
        if observation_decision not in DECISIONS:
            raise CapabilityError(f"capability observation decision is invalid for {case_id}")
        observation_verdict = _normalise_hard_decision(observation_decision)

        reference = reliability_refs[case_id]
        reliability_path = _safe_relative(manifest_root, reference.get("input_path"))
        expected_hash = reference.get("input_sha256")
        if not reliability_path.is_file() or not _is_sha256(expected_hash) or file_sha256(reliability_path) != expected_hash:
            raise CapabilityError(f"reliability input is not pinned for {case_id}")
        reliability_input = _read_json(reliability_path)
        if reliability_input.get("schema") != RELIABILITY_INPUT_SCHEMA:
            raise CapabilityError(f"reliability input schema mismatch for {case_id}")
        expected_packet_identity = {
            "packet_sha256": case["packet_sha256"],
            "condition_fingerprint": case["runtime_identity_fingerprint"],
        }
        if reliability_input.get("packet_identity") != expected_packet_identity:
            raise CapabilityError(f"reliability input does not bind exact runtime identity for {case_id}")
        if reliability_input.get("evaluator_identity") != evaluator:
            raise CapabilityError(f"reliability evaluator identity mismatch for {case_id}")
        if reliability_input.get("required_roles") != [case["role"]]:
            raise CapabilityError(f"reliability required role mismatch for {case_id}")
        roles_input = reliability_input.get("roles")
        if not isinstance(roles_input, dict) or set(roles_input) != {case["role"]}:
            raise CapabilityError(f"reliability input must contain only role {case['role']} for {case_id}")
        try:
            reliability_report = aggregate_reliability(
                reliability_input, min_runs=minimum_runs
            )
        except ReliabilityError as exc:
            raise CapabilityError(f"invalid reliability input for {case_id}: {exc}") from exc
        role_result = reliability_report.get("roles", {}).get(case["role"])
        if not isinstance(role_result, dict):
            raise CapabilityError(f"reliability report lacks role {case['role']} for {case_id}")
        repeat = role_result.get("repeat_reliability")
        if not isinstance(repeat, dict):
            raise CapabilityError(f"reliability report lacks repeat metrics for {case_id}")
        expected_verdict = "FAIL" if case["kind"] == "hard_defect" else "PASS"
        aggregate_verdict = role_result.get("verdict")
        checks = {
            "input_valid": reliability_report.get("input_valid") is True,
            "packet_bound": reliability_report.get("packet_binding", {}).get("status") == "VALID",
            "condition_bound": reliability_report.get("condition_binding", {}).get("status") == "BOUND",
            "complete_runs": (
                role_result.get("runs_requested", 0) >= minimum_runs
                and role_result.get("runs_valid") == role_result.get("runs_requested")
                and role_result.get("runs_invalid") == 0
            ),
            "repeatability_eligible": repeat.get("eligible") is True,
            "stable": repeat.get("stability") == "STABLE",
            "expected_aggregate_verdict": aggregate_verdict == expected_verdict,
            "expected_capability_observation": observation_verdict == expected_verdict,
            "observation_aggregate_consistent": observation_verdict == aggregate_verdict,
        }
        ready = all(checks.values())
        case_results.append(
            {
                "case_id": case_id,
                "role": case["role"],
                "kind": case["kind"],
                "packet_sha256": case["packet_sha256"],
                "runtime_identity_fingerprint": case["runtime_identity_fingerprint"],
                "capability_observation_decision": observation_decision,
                "capability_observation_verdict": observation_verdict,
                "aggregate_verdict": aggregate_verdict,
                "expected_verdict": expected_verdict,
                "stability": repeat.get("stability"),
                "runs_requested": role_result.get("runs_requested"),
                "runs_valid": role_result.get("runs_valid"),
                "reliability_input_sha256": expected_hash,
                "reliability_report_content_sha256": reliability_report.get("content_sha256"),
                "observation_aggregate_consistent": checks["observation_aggregate_consistent"],
                "checks": checks,
                "ready": ready,
            }
        )

    roles: dict[str, dict[str, Any]] = {}
    for role in required_roles:
        role_cases = [value for value in case_results if value["role"] == role]
        capability_checks = role_capability_checks[role]
        roles[role] = {
            "capability_threshold_checks": capability_checks,
            "case_ids": [value["case_id"] for value in role_cases],
            "hard_defect_cases": sum(value["kind"] == "hard_defect" for value in role_cases),
            "neutral_transform_cases": sum(value["kind"] == "neutral_transform" for value in role_cases),
            "ready": all(check["passed"] for check in capability_checks)
            and all(value["ready"] for value in role_cases),
        }
    hard_gate_ready = all(roles[role]["ready"] for role in required_roles)
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "calibration_manifest_sha256": canonical_hash(manifest),
        "prepared_manifest_sha256": manifest["prepared_manifest_sha256"],
        "capability_report_sha256": manifest["capability_report_sha256"],
        "evaluator": evaluator,
        "evaluator_identity_fingerprint": fingerprint,
        "required_roles": required_roles,
        "minimum_reliability_runs": minimum_runs,
        "thresholds": thresholds,
        "roles": roles,
        "cases": case_results,
        "hard_gate_ready": hard_gate_ready,
        "advisory_only": True,
        "automatic_switch_performed": False,
        "operator_authorization_required": True,
        "production_gate_activation_authorized": False,
        "claim_limit": "EXACT_RUNTIME_ORACLE_CAPABILITY_AND_REPEATABILITY_ONLY",
        "human_alignment": "UNAVAILABLE_WITHOUT_HUMAN_CALIBRATION",
        "award_prediction": "UNAVAILABLE_WITHOUT_HUMAN_CALIBRATION",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--prepared-manifest", required=True, type=Path)
    parser.add_argument("--capability-report", required=True, type=Path)
    parser.add_argument("--json-output", required=True, type=Path)
    parser.add_argument("--require-ready", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest_path = args.manifest.resolve()
        prepared_path = args.prepared_manifest.resolve()
        capability_path = args.capability_report.resolve()
        report = evaluate_hard_gate_calibration(
            _read_json(manifest_path),
            manifest_path.parent,
            _read_json(prepared_path),
            prepared_path,
            _read_json(capability_path),
            capability_path,
        )
        output = args.json_output.resolve()
        _write_json(output, report)
        print(output)
        if args.require_ready and not report["hard_gate_ready"]:
            return 3
        return 0
    except CapabilityError as exc:
        print(f"hard-gate calibration rejected input: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
