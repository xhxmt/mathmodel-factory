#!/usr/bin/env python3
"""Enrich external judge aggregates with structural and LLM-score sections."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


OBJECTIVE_SCHEMA = "objective-evidence-v1"


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _objective_summary(path: Path | None) -> tuple[dict[str, Any], list[str]]:
    """Load and bind the objective bundle without making it a score gate."""

    if path is None or not path.is_file():
        return {
            "schema_version": OBJECTIVE_SCHEMA,
            "status": "UNKNOWN",
            "valid": False,
            "bundle_sha256": None,
            "input_fingerprint": None,
            "summary": None,
            "metadata": {"reason": "missing"},
        }, ["objective_evidence_missing"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "schema_version": OBJECTIVE_SCHEMA,
            "status": "UNKNOWN",
            "valid": False,
            "bundle_sha256": None,
            "input_fingerprint": None,
            "summary": None,
            "metadata": {"reason": "invalid_json", "error": str(exc)},
        }, ["objective_evidence_invalid_json"]
    if not isinstance(payload, dict) or payload.get("schema_version") != OBJECTIVE_SCHEMA:
        return {
            "schema_version": OBJECTIVE_SCHEMA,
            "status": "UNKNOWN",
            "valid": False,
            "bundle_sha256": None,
            "input_fingerprint": None,
            "summary": None,
            "metadata": {"reason": "unsupported_schema"},
        }, ["objective_evidence_schema_mismatch"]
    declared = payload.get("bundle_sha256")
    unsigned = dict(payload)
    unsigned.pop("bundle_sha256", None)
    valid_hash = isinstance(declared, str) and declared == _canonical_hash(unsigned)
    input_fingerprint = payload.get("input_fingerprint")
    valid_input = isinstance(input_fingerprint, str) and len(input_fingerprint) == 64
    errors: list[str] = []
    if not valid_hash:
        errors.append("objective_evidence_bundle_hash_mismatch")
    if not valid_input:
        errors.append("objective_evidence_input_fingerprint_invalid")
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else None
    if summary is None:
        errors.append("objective_evidence_summary_missing")
    return {
        "schema_version": OBJECTIVE_SCHEMA,
        "status": summary.get("decision") if summary else "UNKNOWN",
        "valid": not errors,
        "bundle_sha256": declared if valid_hash else None,
        "input_fingerprint": input_fingerprint if valid_input else None,
        "summary": summary,
        "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        "decision_semantics": payload.get("decision_semantics"),
        "quality_verdict": payload.get("quality_verdict"),
        "path": str(path),
    }, errors


def failed_checks(precheck: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        check
        for check in precheck.get("checks", [])
        if not check.get("ok") and check.get("severity", "error") != "warning"
    ]


def verdict_distribution(verdicts: list[Any]) -> dict[str, int]:
    counts = Counter(str(verdict or "missing") for verdict in verdicts)
    return dict(sorted(counts.items()))


def score_spread(min_score: Any, max_score: Any) -> float | None:
    if isinstance(min_score, (int, float)) and isinstance(max_score, (int, float)):
        return round(max_score - min_score, 2)
    return None


def hard_roles_pass(aggregate: dict[str, Any], precheck: dict[str, Any] | None = None) -> bool:
    """Require explicit PASS from both non-averagable hard auditors."""
    roles = aggregate.get("roles")
    if isinstance(roles, dict):
        roles = [dict(value, role=key) if isinstance(value, dict) else {"role": key, "status": value}
                 for key, value in roles.items()]
    if isinstance(roles, list):
        by_role = {str(item.get("role")): item for item in roles if isinstance(item, dict)}
        return all(
            by_role.get(role, {}).get("status") == "PASS"
            and by_role.get(role, {}).get("verdict", "PASS") == "PASS"
            for role in ("math", "execution")
        )
    # The new aggregate schema may persist a compact hard-role map.
    compact = aggregate.get("hard_roles")
    if isinstance(compact, dict):
        return all(compact.get(role) == "PASS" for role in ("math", "execution"))
    role_statuses = aggregate.get("role_statuses")
    if isinstance(role_statuses, dict):
        return all(role_statuses.get(role) == "PASS" for role in ("math", "execution"))
    runs = aggregate.get("runs")
    if isinstance(runs, list) and runs:
        for run in runs:
            if not isinstance(run, dict):
                return False
            statuses = run.get("role_statuses")
            if not isinstance(statuses, dict) or any(
                statuses.get(role) != "PASS" for role in ("math", "execution")
            ):
                return False
        return True
    precheck_data = precheck or {}
    compact = precheck_data.get("hard_roles")
    if isinstance(compact, dict):
        return all(compact.get(role) == "PASS" for role in ("math", "execution"))
    return bool(precheck_data.get("hard_roles_pass") is True)


def calibration_identity_match(
    calibration: dict[str, Any], aggregate: dict[str, Any]
) -> tuple[bool, list[str]]:
    """Only accept a fresh, self-identifying calibration report."""
    reasons: list[str] = []
    contract = calibration.get("calibration_contract")
    identity = calibration.get("calibration_identity")
    if not isinstance(contract, dict) or contract.get("present") is not True:
        reasons.append("calibration_contract_missing")
    if not isinstance(identity, dict) or identity.get("fresh") is not True:
        reasons.append("calibration_report_stale")
    if not isinstance(identity, dict) or not identity.get("source_fingerprint"):
        reasons.append("calibration_source_fingerprint_missing")
    calibration_models = {str(value) for value in calibration.get("models", []) if isinstance(value, str)}
    aggregate_model = aggregate.get("model")
    if not aggregate_model and isinstance(aggregate.get("judge_config"), dict):
        aggregate_model = aggregate["judge_config"].get("model")
    expected_models = {str(value) for value in contract.get("models", []) if isinstance(value, str)} if isinstance(contract, dict) else set()
    if aggregate_model and calibration_models and str(aggregate_model) not in calibration_models:
        reasons.append("model_mismatch")
    if expected_models and calibration_models and not calibration_models <= expected_models:
        reasons.append("calibration_model_config_mismatch")
    aggregate_schema = aggregate.get("calibration_schema_version")
    contract_schema = contract.get("schema_version") if isinstance(contract, dict) else None
    if aggregate_schema is not None and contract_schema is not None and aggregate_schema != contract_schema:
        reasons.append("schema_mismatch")
    return not reasons, reasons


def enrich_aggregate(
    aggregate: dict[str, Any],
    *,
    precheck: dict[str, Any] | None,
    unmatched_numbers: str,
    inloop_total: str,
    objective_evidence: dict[str, Any] | None = None,
    objective_evidence_errors: list[str] | None = None,
    reliability_report: dict[str, Any] | None = None,
    calibration_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    precheck_data = precheck or {}
    precheck_passed = bool(precheck_data.get("passed"))
    # Packet construction writes an explicit eligibility decision onto the
    # aggregate.  A structural precheck may be older or less strict, so it is
    # only a fallback when the aggregate has no decision of its own.
    if "scoring_eligible" in aggregate:
        scoring_eligible = aggregate.get("scoring_eligible") is True
    else:
        scoring_eligible = precheck_data.get("scoring_eligible") is True
    llm_scored = int(aggregate.get("n_scored") or 0)
    min_recomputed = aggregate.get("min_recomputed")
    max_recomputed = aggregate.get("max_recomputed")
    calibration = calibration_report or {}
    reliability = calibration.get("score_reliability") if isinstance(calibration.get("score_reliability"), dict) else {}
    proxy = calibration.get("proxy_reliability") if isinstance(calibration.get("proxy_reliability"), dict) else {}
    runtime = calibration.get("runtime_score_reliability") if isinstance(calibration.get("runtime_score_reliability"), dict) else {}
    axis = calibration.get("axis_reliability") if isinstance(calibration.get("axis_reliability"), dict) else {}
    human_ready = bool(reliability.get("ready"))
    proxy_ready = bool(proxy.get("ready"))
    runtime_ready = bool(runtime.get("ready"))
    runtime_proxy_ready = runtime_ready and bool(runtime.get("proxy_ready"))
    runtime_human_ready = runtime_ready and bool(runtime.get("human_ready"))
    hard_pass = hard_roles_pass(aggregate, precheck_data)
    identity_match, identity_reasons = calibration_identity_match(calibration, aggregate)

    aggregate["structural"] = {
        "precheck_passed": precheck_passed,
        "scoring_eligible": scoring_eligible,
        "inferred_step": precheck_data.get("inferred_step"),
        "unmatched_numbers": unmatched_numbers,
        "blocking_evidence": failed_checks(precheck_data),
        "objective_evidence": objective_evidence or {
            "schema_version": OBJECTIVE_SCHEMA,
            "status": "UNKNOWN",
            "valid": False,
            "bundle_sha256": None,
            "input_fingerprint": None,
            "summary": None,
            "metadata": {"reason": "not_supplied"},
        },
        "objective_evidence_errors": list(objective_evidence_errors or []),
    }
    aggregate["objective_evidence"] = aggregate["structural"]["objective_evidence"]
    aggregate["reliability"] = reliability_report or {
        "schema": "judge-reliability-v1",
        "input_valid": False,
        "hard_gate_status": "INDETERMINATE",
        "overall": {
            "verdict": "INDETERMINATE",
            "reason": "not_supplied",
            "score_semantics": "UNCALIBRATED_DIAGNOSTIC_ONLY",
        },
    }
    aggregate["llm_score"] = {
        "samples_requested": aggregate.get("n"),
        "samples_scored": llm_scored,
        "median_recomputed": aggregate.get("median_recomputed"),
        "min_recomputed": min_recomputed,
        "max_recomputed": max_recomputed,
        "spread_recomputed": score_spread(min_recomputed, max_recomputed),
        "median_total": aggregate.get("median_total"),
        "verdict_distribution": verdict_distribution(aggregate.get("verdicts", [])),
    }
    aggregate["inloop_total"] = inloop_total
    aggregate["calibration"] = {
        "ready": runtime_ready,
        "runtime_ready": runtime_ready,
        "runtime_proxy_ready": runtime_proxy_ready,
        "runtime_human_ready": runtime_human_ready,
        "proxy_ready": proxy_ready,
        "axis_ready": bool(axis.get("ready")),
        "human_ready": human_ready,
        "award_prediction_ready": bool(calibration.get("award_prediction_ready")),
        "hard_roles_pass": hard_pass,
        "identity_match": identity_match,
        "identity_reasons": identity_reasons,
        "checks": runtime.get("checks", {}),
        "proxy_checks": proxy.get("checks", {}),
        "axis_checks": axis.get("checks", {}),
        "pairwise_accuracy": (calibration.get("pairwise") or {}).get("accuracy")
        if isinstance(calibration.get("pairwise"), dict)
        else None,
        "fatal_flaw_detection_rate": (calibration.get("fatal_flaw_detection") or {}).get("rate")
        if isinstance(calibration.get("fatal_flaw_detection"), dict)
        else None,
    }
    aggregate["precheck_passed"] = precheck_passed
    aggregate["unmatched_numbers"] = unmatched_numbers
    comparison_base = scoring_eligible and llm_scored > 0 and hard_pass and identity_match
    aggregate["comparison_ready_proxy"] = comparison_base and runtime_proxy_ready
    aggregate["comparison_ready_human"] = comparison_base and runtime_human_ready
    aggregate["comparison_ready"] = (
        aggregate["comparison_ready_proxy"] or aggregate["comparison_ready_human"]
    )
    # A hard correctness veto is never averaged away by a high paper score.
    if not hard_pass:
        aggregate["overall_score"] = None
        aggregate["median_recomputed"] = None
    return aggregate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("aggregate_json", help="Path to <base>_eval.json generated by parse_judge_score.py.")
    parser.add_argument("--precheck", required=True, help="Path to evaluate_modeling_project.py --json output.")
    parser.add_argument("--unmatched", required=True, help="UNMATCHED number count or NA.")
    parser.add_argument("--inloop", required=True, help="In-loop judge total or NA.")
    parser.add_argument(
        "--objective-evidence",
        help="Hash-bound objective-evidence-v1 bundle; invalid/missing is UNKNOWN.",
    )
    parser.add_argument(
        "--reliability-report",
        help="Optional judge-reliability-v1 report for repeatability diagnostics.",
    )
    parser.add_argument(
        "--calibration-report",
        help="Calibration report JSON; comparison_ready requires runtime_score_reliability.ready=true.",
    )
    args = parser.parse_args()

    aggregate_path = Path(args.aggregate_json)
    precheck_path = Path(args.precheck)
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    precheck = json.loads(precheck_path.read_text(encoding="utf-8")) if precheck_path.is_file() else None
    calibration_path = Path(args.calibration_report) if args.calibration_report else None
    calibration = (
        json.loads(calibration_path.read_text(encoding="utf-8"))
        if calibration_path and calibration_path.is_file()
        else None
    )
    objective, objective_errors = _objective_summary(
        Path(args.objective_evidence) if args.objective_evidence else None
    )
    reliability = None
    if args.reliability_report:
        try:
            reliability_value = json.loads(
                Path(args.reliability_report).read_text(encoding="utf-8")
            )
            if isinstance(reliability_value, dict):
                reliability = reliability_value
        except (OSError, json.JSONDecodeError):
            reliability = None
    enriched = enrich_aggregate(
        aggregate,
        precheck=precheck,
        unmatched_numbers=args.unmatched,
        inloop_total=args.inloop,
        objective_evidence=objective,
        objective_evidence_errors=objective_errors,
        reliability_report=reliability,
        calibration_report=calibration,
    )
    aggregate_path.write_text(json.dumps(enriched, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(enriched, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
