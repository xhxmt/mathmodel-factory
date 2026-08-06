#!/usr/bin/env python3
"""Run a hash-bound, advisory-only shadow portfolio evaluation.

R3 compares multiple candidates only after R0a/R0b identity and hard-pass
checks. It records what a selector would have recommended; it never performs
a production switch and never treats Gate 2 as a selector label.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from scripts.capability_harness import (
        _is_sha256,
        _read_json,
        _write_json,
        binomial_metric,
        canonical_hash,
        file_sha256,
    )
    from scripts.hard_gate_calibration import REPORT_SCHEMA as R0A_REPORT_SCHEMA
    from scripts.selector_calibration import REPORT_SCHEMA as R0B_REPORT_SCHEMA
except ModuleNotFoundError:  # Direct execution from scripts/.
    from capability_harness import (  # type: ignore
        _is_sha256,
        _read_json,
        _write_json,
        binomial_metric,
        canonical_hash,
        file_sha256,
    )
    from hard_gate_calibration import REPORT_SCHEMA as R0A_REPORT_SCHEMA  # type: ignore
    from selector_calibration import REPORT_SCHEMA as R0B_REPORT_SCHEMA  # type: ignore


MANIFEST_SCHEMA = "shadow-portfolio-manifest-v1"
REPORT_SCHEMA = "shadow-portfolio-report-v1"
REQUIRED_THRESHOLDS = {
    "minimum_candidates",
    "minimum_projects",
    "minimum_pair_decisions",
    "selector_coverage_min",
    "tie_rate_max",
    "mainline_disagreement_rate_max",
    "budget_ratio_max",
    "minimum_adjudications",
    "adjudication_win_rate_min",
    "regret_rate_max",
}


class PortfolioError(ValueError):
    """Raised when the shadow portfolio contract is invalid."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PortfolioError(f"{field} must be a non-empty string")
    return value


def _hash(value: Any, field: str) -> str:
    if not _is_sha256(value):
        raise PortfolioError(f"{field} must be a lowercase SHA-256")
    return value


def _positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PortfolioError(f"{field} must be a positive integer")
    return value


def _finite_positive(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PortfolioError(f"{field} must be a finite positive number")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise PortfolioError(f"{field} must be a finite positive number")
    return value


def _rate(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PortfolioError(f"{field} must be between 0 and 1")
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise PortfolioError(f"{field} must be between 0 and 1")
    return value


def _pinned_report(root: Path, reference: Any, expected_schema: str, label: str) -> tuple[dict[str, Any], Path, str]:
    if not isinstance(reference, dict):
        raise PortfolioError(f"{label} must be an object with path and sha256")
    path_value = reference.get("path")
    if not isinstance(path_value, str) or not path_value:
        raise PortfolioError(f"{label}.path must be a relative string")
    path = Path(path_value)
    if path.is_absolute() or ".." in path.parts:
        raise PortfolioError(f"{label}.path must stay below the manifest root")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise PortfolioError(f"{label}.path escapes the manifest root") from exc
    if not resolved.is_file():
        raise PortfolioError(f"{label}.path is not a regular file")
    expected_hash = _hash(reference.get("sha256"), f"{label}.sha256")
    actual_hash = file_sha256(resolved)
    if actual_hash != expected_hash:
        raise PortfolioError(f"{label} is not pinned by its sha256")
    report = _read_json(resolved)
    if report.get("schema") != expected_schema:
        raise PortfolioError(f"{label} schema mismatch")
    return report, resolved, actual_hash


def _thresholds(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != REQUIRED_THRESHOLDS:
        raise PortfolioError(f"thresholds must configure exactly {sorted(REQUIRED_THRESHOLDS)}")
    result = {
        "minimum_candidates": _positive_int(raw["minimum_candidates"], "thresholds.minimum_candidates"),
        "minimum_projects": _positive_int(raw["minimum_projects"], "thresholds.minimum_projects"),
        "minimum_pair_decisions": _positive_int(raw["minimum_pair_decisions"], "thresholds.minimum_pair_decisions"),
        "selector_coverage_min": _rate(raw["selector_coverage_min"], "thresholds.selector_coverage_min"),
        "tie_rate_max": _rate(raw["tie_rate_max"], "thresholds.tie_rate_max"),
        "mainline_disagreement_rate_max": _rate(raw["mainline_disagreement_rate_max"], "thresholds.mainline_disagreement_rate_max"),
        "budget_ratio_max": _finite_positive(raw["budget_ratio_max"], "thresholds.budget_ratio_max"),
        "minimum_adjudications": _positive_int(raw["minimum_adjudications"], "thresholds.minimum_adjudications"),
        "adjudication_win_rate_min": _rate(raw["adjudication_win_rate_min"], "thresholds.adjudication_win_rate_min"),
        "regret_rate_max": _rate(raw["regret_rate_max"], "thresholds.regret_rate_max"),
    }
    if result["budget_ratio_max"] < 1:
        raise PortfolioError("thresholds.budget_ratio_max must be at least 1")
    return result


def _candidate(value: Any, candidate_id: str, r0a_fp: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PortfolioError(f"candidate {candidate_id} must be an object")
    required = {
        "candidate_id",
        "problem_identity",
        "family_id",
        "project_id",
        "method_stream_sha256",
        "code_sha256",
        "solver_receipt_sha256",
        "canonical_result_sha256",
        "packet_sha256",
        "pdf_sha256",
        "budget",
        "seed",
        "hard_gate_identity_fingerprint",
        "hard_gate_decisions",
        "r1_r2_hard_pass",
    }
    if set(value) != required:
        raise PortfolioError(f"candidate {candidate_id} has an incomplete immutable evidence contract")
    if value.get("candidate_id") != candidate_id:
        raise PortfolioError(f"candidate id key mismatch: {candidate_id}")
    result = {
        "candidate_id": _text(candidate_id, "candidate_id"),
        "problem_identity": _text(value["problem_identity"], f"candidate {candidate_id}.problem_identity"),
        "family_id": _text(value["family_id"], f"candidate {candidate_id}.family_id"),
        "project_id": _text(value["project_id"], f"candidate {candidate_id}.project_id"),
        "method_stream_sha256": _hash(value["method_stream_sha256"], f"candidate {candidate_id}.method_stream_sha256"),
        "code_sha256": _hash(value["code_sha256"], f"candidate {candidate_id}.code_sha256"),
        "solver_receipt_sha256": _hash(value["solver_receipt_sha256"], f"candidate {candidate_id}.solver_receipt_sha256"),
        "canonical_result_sha256": _hash(value["canonical_result_sha256"], f"candidate {candidate_id}.canonical_result_sha256"),
        "packet_sha256": _hash(value["packet_sha256"], f"candidate {candidate_id}.packet_sha256"),
        "pdf_sha256": _hash(value["pdf_sha256"], f"candidate {candidate_id}.pdf_sha256"),
        "budget": _finite_positive(value["budget"], f"candidate {candidate_id}.budget"),
        "seed": _text(value["seed"], f"candidate {candidate_id}.seed"),
        "hard_gate_identity_fingerprint": _hash(value["hard_gate_identity_fingerprint"], f"candidate {candidate_id}.hard_gate_identity_fingerprint"),
        "hard_gate_decisions": value["hard_gate_decisions"],
        "r1_r2_hard_pass": value["r1_r2_hard_pass"],
    }
    if result["hard_gate_identity_fingerprint"] != r0a_fp:
        raise PortfolioError(f"candidate {candidate_id} has a different R0a identity")
    decisions = result["hard_gate_decisions"]
    if not isinstance(decisions, dict) or set(decisions) != {"math", "execution"} or any(decisions[role] not in {"PASS", "FAIL", "INDETERMINATE"} for role in decisions):
        raise PortfolioError(f"candidate {candidate_id}.hard_gate_decisions is invalid")
    if not isinstance(result["r1_r2_hard_pass"], bool):
        raise PortfolioError(f"candidate {candidate_id}.r1_r2_hard_pass must be boolean")
    result["hard_pass"] = result["r1_r2_hard_pass"] and all(value == "PASS" for value in decisions.values())
    return result


def _pair_decision(value: Any, candidates: dict[str, dict[str, Any]], tie_band: float, selector_fp: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PortfolioError("portfolio pair decision must be an object")
    required = {"pair_id", "candidate_a", "candidate_b", "problem_identity", "margin", "decision", "mainline_candidate_id", "selector_identity_fingerprint", "r1_conflicts"}
    if set(value) != required:
        raise PortfolioError("portfolio pair decision has an incomplete evidence contract")
    pair_id = _text(value["pair_id"], "pair decision.pair_id")
    a_id = _text(value["candidate_a"], f"pair {pair_id}.candidate_a")
    b_id = _text(value["candidate_b"], f"pair {pair_id}.candidate_b")
    if a_id == b_id or a_id not in candidates or b_id not in candidates:
        raise PortfolioError(f"pair {pair_id} references unknown or duplicate candidates")
    a, b = candidates[a_id], candidates[b_id]
    if a["problem_identity"] != b["problem_identity"] or value["problem_identity"] != a["problem_identity"]:
        raise PortfolioError(f"pair {pair_id} candidates must share problem_identity")
    if value["selector_identity_fingerprint"] != selector_fp:
        raise PortfolioError(f"pair {pair_id} selector identity drift")
    if value["mainline_candidate_id"] not in {a_id, b_id}:
        raise PortfolioError(f"pair {pair_id}.mainline_candidate_id must be one candidate")
    conflicts = value["r1_conflicts"]
    if not isinstance(conflicts, list) or any(not isinstance(item, str) or not item.strip() for item in conflicts):
        raise PortfolioError(f"pair {pair_id}.r1_conflicts must be a string array")
    budget_ratio = max(a["budget"], b["budget"]) / min(a["budget"], b["budget"])
    admitted = a["hard_pass"] and b["hard_pass"]
    if admitted:
        margin = value["margin"]
        if isinstance(margin, bool) or not isinstance(margin, (int, float)) or not math.isfinite(float(margin)):
            raise PortfolioError(f"pair {pair_id}.margin must be finite for admitted candidates")
        margin = float(margin)
        expected = "TIE" if abs(margin) <= tie_band else ("A" if margin > 0 else "B")
        if value["decision"] != expected:
            raise PortfolioError(f"pair {pair_id}.decision does not follow the bound TIE rule")
    else:
        if value["margin"] is not None or value["decision"] is not None:
            raise PortfolioError(
                f"pair {pair_id} hard-fail candidates must not enter the selector"
            )
        margin = None
    effective = "HARD_GATE_BLOCKED" if not admitted else "R1_VETO" if conflicts else value["decision"]
    return {
        "pair_id": pair_id,
        "candidate_a": a_id,
        "candidate_b": b_id,
        "problem_identity": a["problem_identity"],
        "margin": margin,
        "selector_decision": value["decision"],
        "mainline_candidate_id": value["mainline_candidate_id"],
        "r1_conflicts": conflicts,
        "budget_ratio": budget_ratio,
        "hard_pass_admitted": admitted,
        "effective_decision": effective,
    }


def _rate_from(count: int, total: int) -> dict[str, Any]:
    return binomial_metric(count, total)


def _adjudications(raw: Any, pairs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, list):
        raise PortfolioError("adjudications must be a separate array")
    pair_map = {pair["pair_id"]: pair for pair in pairs}
    result: dict[str, dict[str, Any]] = {}
    for value in raw:
        if not isinstance(value, dict):
            raise PortfolioError("adjudication must be an object")
        pair_id = _text(value.get("pair_id"), "adjudication.pair_id")
        if pair_id in result or pair_id not in pair_map:
            raise PortfolioError(f"duplicate or unknown adjudication pair: {pair_id}")
        pair = pair_map[pair_id]
        winner = value.get("winner_candidate_id")
        if winner not in {pair["candidate_a"], pair["candidate_b"], "TIE"}:
            raise PortfolioError(f"adjudication {pair_id} winner is not in the pair")
        source = _text(value.get("source"), f"adjudication {pair_id}.source")
        method = _text(value.get("method"), f"adjudication {pair_id}.method")
        if any(token in source.lower() or token in method.lower() for token in ("gate2", "selector")):
            raise PortfolioError(f"adjudication {pair_id} must be independent of Gate 2 and selector output")
        if value.get("blind") is not True or value.get("selector_blinded") is not True:
            raise PortfolioError(f"adjudication {pair_id} must be independently blind")
        result[pair_id] = {
            "pair_id": pair_id,
            "winner_candidate_id": winner,
            "source": source,
            "adjudicated_at": _text(value.get("adjudicated_at"), f"adjudication {pair_id}.adjudicated_at"),
            "method": method,
            "blind": True,
            "selector_blinded": True,
        }
    return result


def evaluate_shadow_portfolio(
    manifest: dict[str, Any],
    manifest_root: Path,
    r0a_report: dict[str, Any] | None = None,
    r0a_path: Path | None = None,
    selector_report: dict[str, Any] | None = None,
    selector_path: Path | None = None,
) -> dict[str, Any]:
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise PortfolioError(f"manifest schema must be {MANIFEST_SCHEMA}")
    _text(manifest.get("run_id"), "run_id")
    if manifest.get("frozen") is not True:
        raise PortfolioError("portfolio manifest must be frozen")
    thresholds = _thresholds(manifest.get("thresholds"))
    pinned_r0a, pinned_r0a_path, r0a_hash = _pinned_report(
        manifest_root, manifest.get("r0a_report"), R0A_REPORT_SCHEMA, "r0a_report"
    )
    if r0a_report is not None and r0a_report != pinned_r0a:
        raise PortfolioError("in-memory r0a_report differs from its pinned file")
    if r0a_path is not None and r0a_path.resolve() != pinned_r0a_path:
        raise PortfolioError("in-memory r0a_report path differs from its pinned file")
    r0a_report, r0a_path = pinned_r0a, pinned_r0a_path
    pinned_selector, pinned_selector_path, selector_hash = _pinned_report(
        manifest_root,
        manifest.get("selector_report"),
        R0B_REPORT_SCHEMA,
        "selector_report",
    )
    if selector_report is not None and selector_report != pinned_selector:
        raise PortfolioError("in-memory selector_report differs from its pinned file")
    if selector_path is not None and selector_path.resolve() != pinned_selector_path:
        raise PortfolioError("in-memory selector_report path differs from its pinned file")
    selector_report, selector_path = pinned_selector, pinned_selector_path
    if r0a_report.get("hard_gate_ready") is not True:
        r0a_ready = False
    else:
        r0a_ready = True
    if selector_report.get("comparison_ready_human") is not True:
        selector_ready = False
    else:
        selector_ready = True
    if (
        r0a_report.get("automatic_switch_performed") is not False
        or r0a_report.get("operator_authorization_required") is not True
        or r0a_report.get("claim_limit")
        != "EXACT_RUNTIME_ORACLE_CAPABILITY_AND_REPEATABILITY_ONLY"
    ):
        raise PortfolioError("R0a report violates advisory-only governance")
    if (
        selector_report.get("advisory_only") is not True
        or selector_report.get("automatic_switch_performed") is not False
        or selector_report.get("operator_authorization_required") is not True
        or selector_report.get("production_selection_authorized") is not False
        or selector_report.get("claim_limit")
        != "BLIND_PAIRWISE_SELECTOR_CALIBRATION_ONLY"
    ):
        raise PortfolioError("R0b report violates advisory-only governance")
    _hash(selector_report.get("holdout_hash"), "selector holdout_hash")
    r0a_fp = _hash(r0a_report.get("evaluator_identity_fingerprint"), "r0a evaluator_identity_fingerprint")
    selector_fp = _hash(selector_report.get("evaluator_identity_fingerprint"), "selector evaluator_identity_fingerprint")
    if selector_report.get("hard_gate_identity_fingerprint") != r0a_fp:
        raise PortfolioError("R0b report does not bind the exact R0a identity")
    expected_selector_fp = _hash(manifest.get("selector_identity_fingerprint"), "selector_identity_fingerprint")
    if expected_selector_fp != selector_fp:
        raise PortfolioError("portfolio selector identity differs from R0b")
    gate2_isolation = manifest.get("gate2_isolation")
    required_isolation = {
        "gate2_evaluator_identity_fingerprint",
        "receipt_sha256",
        "selector_recommendation_hidden",
        "candidate_scores_hidden",
        "rejected_candidate_identity_hidden",
    }
    if not isinstance(gate2_isolation, dict) or set(gate2_isolation) != required_isolation:
        raise PortfolioError(
            f"gate2_isolation must configure exactly {sorted(required_isolation)}"
        )
    gate2_fp = _hash(
        gate2_isolation["gate2_evaluator_identity_fingerprint"],
        "gate2_isolation.gate2_evaluator_identity_fingerprint",
    )
    if gate2_fp == selector_fp:
        raise PortfolioError("Gate 2 evaluator must differ from the selector evaluator")
    isolation_receipt_hash = _hash(
        gate2_isolation["receipt_sha256"], "gate2_isolation.receipt_sha256"
    )
    for field in (
        "selector_recommendation_hidden",
        "candidate_scores_hidden",
        "rejected_candidate_identity_hidden",
    ):
        if gate2_isolation[field] is not True:
            raise PortfolioError(f"gate2_isolation.{field} must be true")
    hard_gate_fp = r0a_fp
    raw_candidates = manifest.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise PortfolioError("candidates must be a non-empty array")
    candidates: dict[str, dict[str, Any]] = {}
    for raw in raw_candidates:
        candidate_id = _text(raw.get("candidate_id") if isinstance(raw, dict) else None, "candidate.candidate_id")
        if candidate_id in candidates:
            raise PortfolioError(f"duplicate candidate: {candidate_id}")
        candidates[candidate_id] = _candidate(raw, candidate_id, hard_gate_fp)
    selector_families = selector_report.get("split_families")
    if not isinstance(selector_families, dict):
        raise PortfolioError("R0b report has no dev/holdout family scope")
    calibration_families: set[str] = set()
    for split in ("dev", "holdout"):
        values = selector_families.get(split)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise PortfolioError("R0b split_families is malformed")
        calibration_families.update(values)
    family_overlap = calibration_families.intersection(
        candidate["family_id"] for candidate in candidates.values()
    )
    if family_overlap:
        raise PortfolioError(f"R3 cohort leaks R0b calibration families: {sorted(family_overlap)}")
    pair_values = manifest.get("pair_decisions")
    if not isinstance(pair_values, list):
        raise PortfolioError("pair_decisions must be an array")
    pairs: list[dict[str, Any]] = []
    pair_ids: set[str] = set()
    tie_band = selector_report.get("tie_band")
    if isinstance(tie_band, bool) or not isinstance(tie_band, (int, float)) or not math.isfinite(float(tie_band)) or float(tie_band) <= 0:
        raise PortfolioError("R0b report has no valid tie_band")
    for raw in pair_values:
        pair = _pair_decision(raw, candidates, float(tie_band), selector_fp)
        if pair["pair_id"] in pair_ids:
            raise PortfolioError(f"duplicate pair decision: {pair['pair_id']}")
        pair_ids.add(pair["pair_id"])
        pairs.append(pair)
    adjudications = _adjudications(manifest.get("adjudications"), pairs)
    projects = {candidate["project_id"] for candidate in candidates.values()}
    hard_pass_count = sum(candidate["hard_pass"] for candidate in candidates.values())
    admitted_pairs = [pair for pair in pairs if pair["hard_pass_admitted"]]
    directional = [pair for pair in admitted_pairs if pair["effective_decision"] in {"A", "B"}]
    ties = [pair for pair in admitted_pairs if pair["effective_decision"] == "TIE"]
    conflicts = [pair for pair in admitted_pairs if pair["r1_conflicts"]]
    disagreements = [
        pair for pair in directional
        if pair["mainline_candidate_id"] != (pair["candidate_a"] if pair["effective_decision"] == "A" else pair["candidate_b"])
    ]
    budget_imbalanced = [pair for pair in admitted_pairs if pair["budget_ratio"] > thresholds["budget_ratio_max"]]
    adjudicated_directional = []
    selector_wins = 0
    regrets = 0
    for pair in directional:
        adjudication = adjudications.get(pair["pair_id"])
        if adjudication is None or adjudication["winner_candidate_id"] == "TIE":
            continue
        recommendation = pair["candidate_a"] if pair["effective_decision"] == "A" else pair["candidate_b"]
        adjudicated_directional.append(pair)
        selector_wins += int(adjudication["winner_candidate_id"] == recommendation)
        regrets += int(adjudication["winner_candidate_id"] != recommendation)
    unadjudicated_disagreements = [pair["pair_id"] for pair in disagreements if pair["pair_id"] not in adjudications]
    coverage = _rate_from(len(directional), len(admitted_pairs))
    tie_rate = _rate_from(len(ties), len(admitted_pairs))
    disagreement_rate = _rate_from(len(disagreements), len(directional))
    adjudication_win_rate = _rate_from(selector_wins, len(adjudicated_directional))
    regret_rate = _rate_from(regrets, len(adjudicated_directional))
    checks = [
        {"name": "r0a_ready", "observed": r0a_ready, "threshold": True, "comparison": "exact R0a hard_gate_ready", "passed": r0a_ready},
        {"name": "r0b_human_ready", "observed": selector_ready, "threshold": True, "comparison": "exact R0b comparison_ready_human", "passed": selector_ready},
        {"name": "minimum_candidates", "observed": len(candidates), "threshold": thresholds["minimum_candidates"], "comparison": "observed >= threshold", "passed": len(candidates) >= thresholds["minimum_candidates"]},
        {"name": "minimum_projects", "observed": len(projects), "threshold": thresholds["minimum_projects"], "comparison": "observed >= threshold", "passed": len(projects) >= thresholds["minimum_projects"]},
        {"name": "minimum_pair_decisions", "observed": len(admitted_pairs), "threshold": thresholds["minimum_pair_decisions"], "comparison": "observed >= threshold", "passed": len(admitted_pairs) >= thresholds["minimum_pair_decisions"]},
        {"name": "selector_coverage", "observed": coverage["estimate"], "threshold": thresholds["selector_coverage_min"], "comparison": "estimate >= threshold", "passed": coverage["estimate"] is not None and coverage["estimate"] >= thresholds["selector_coverage_min"]},
        {"name": "tie_rate", "observed": tie_rate["estimate"], "threshold": thresholds["tie_rate_max"], "comparison": "estimate <= threshold", "passed": tie_rate["estimate"] is not None and tie_rate["estimate"] <= thresholds["tie_rate_max"]},
        {"name": "mainline_disagreement_rate", "observed": disagreement_rate["estimate"], "threshold": thresholds["mainline_disagreement_rate_max"], "comparison": "estimate <= threshold", "passed": disagreement_rate["estimate"] is not None and disagreement_rate["estimate"] <= thresholds["mainline_disagreement_rate_max"]},
        {"name": "budget_imbalance", "observed": len(budget_imbalanced), "threshold": 0, "comparison": "no pair over budget_ratio_max", "passed": not budget_imbalanced},
        {"name": "r1_conflicts", "observed": len(conflicts), "threshold": 0, "comparison": "no unresolved R1 conflict", "passed": not conflicts},
        {"name": "minimum_adjudications", "observed": len(adjudicated_directional), "threshold": thresholds["minimum_adjudications"], "comparison": "observed >= threshold", "passed": len(adjudicated_directional) >= thresholds["minimum_adjudications"]},
        {"name": "adjudication_win_rate", "observed": adjudication_win_rate["wilson_95"]["low"], "threshold": thresholds["adjudication_win_rate_min"], "comparison": "wilson_95.low >= threshold", "passed": adjudication_win_rate["wilson_95"]["low"] is not None and adjudication_win_rate["wilson_95"]["low"] >= thresholds["adjudication_win_rate_min"]},
        {"name": "regret_rate", "observed": regret_rate["wilson_95"]["high"], "threshold": thresholds["regret_rate_max"], "comparison": "wilson_95.high <= threshold", "passed": regret_rate["wilson_95"]["high"] is not None and regret_rate["wilson_95"]["high"] <= thresholds["regret_rate_max"]},
        {"name": "unadjudicated_disagreements", "observed": unadjudicated_disagreements, "threshold": [], "comparison": "all mainline disagreements adjudicated", "passed": not unadjudicated_disagreements},
    ]
    no_ready_reason = [check["name"] for check in checks if not check["passed"]]
    portfolio_ready = all(check["passed"] for check in checks)
    problem_counts = Counter(candidate["problem_identity"] for candidate in candidates.values())
    k_groups: dict[int, list[str]] = defaultdict(list)
    for problem, count in sorted(problem_counts.items()):
        k_groups[count].append(problem)
    k_buckets: dict[str, dict[str, Any]] = {}
    for count, problems in sorted(k_groups.items()):
        related = [pair for pair in admitted_pairs if pair["problem_identity"] in problems]
        k_buckets[str(count)] = {
            "problem_identities": sorted(problems),
            "candidate_count": count,
            "pairs": len(related),
            "directional_recommendations": len([pair for pair in related if pair in directional]),
            "tie_rate": _rate_from(len([pair for pair in related if pair in ties]), len(related)),
            "regret_rate": _rate_from(
                len([pair for pair in related if pair in adjudicated_directional and adjudications[pair["pair_id"]]["winner_candidate_id"] != (pair["candidate_a"] if pair["effective_decision"] == "A" else pair["candidate_b"])]),
                len([pair for pair in related if pair in adjudicated_directional]),
            ),
        }
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "portfolio_manifest_sha256": canonical_hash(manifest),
        "r0a_report_sha256": r0a_hash,
        "selector_report_sha256": selector_hash,
        "selector_evaluator": selector_report.get("evaluator"),
        "selector_identity_fingerprint": selector_fp,
        "hard_gate_evaluator": r0a_report.get("evaluator"),
        "hard_gate_identity_fingerprint": r0a_fp,
        "tie_band": float(tie_band),
        "candidate_count": len(candidates),
        "hard_pass_count": hard_pass_count,
        "hard_pass_admission_rate": _rate_from(hard_pass_count, len(candidates)),
        "project_count": len(projects),
        "pair_decision_count": len(pairs),
        "admitted_pair_count": len(admitted_pairs),
        "selector_coverage": coverage,
        "tie_rate": tie_rate,
        "mainline_disagreement_rate": disagreement_rate,
        "independent_adjudication_count": len(adjudicated_directional),
        "adjudication_win_rate": adjudication_win_rate,
        "regret_rate": regret_rate,
        "unadjudicated_disagreements": unadjudicated_disagreements,
        "adjudications": [adjudications[key] for key in sorted(adjudications)],
        "budget_imbalanced_pairs": [pair["pair_id"] for pair in budget_imbalanced],
        "r1_conflict_pairs": [pair["pair_id"] for pair in conflicts],
        "candidate_k_buckets": k_buckets,
        "shadow_pairs": pairs,
        "threshold_checks": checks,
        "portfolio_ready": portfolio_ready,
        "blocked_reasons": sorted(set(no_ready_reason)),
        "advisory_only": True,
        "automatic_switch_performed": False,
        "operator_authorization_required": True,
        "production_selection_authorized": False,
        "claim_limit": "SHADOW_PORTFOLIO_RECOMMENDATION_ONLY",
        "gate2_isolated": True,
        "gate2_evaluator_identity_fingerprint": gate2_fp,
        "gate2_isolation_receipt_sha256": isolation_receipt_hash,
        "gate2_hidden_fields": [
            "selector_recommendation",
            "candidate_scores",
            "rejected_candidate_identity",
        ],
        "selector_labels_from_gate2": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--json-output", required=True, type=Path)
    parser.add_argument("--require-ready", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest_path = args.manifest.resolve()
        report = evaluate_shadow_portfolio(_read_json(manifest_path), manifest_path.parent)
        output = args.json_output.resolve()
        _write_json(output, report)
        print(output)
        if args.require_ready and report["portfolio_ready"] is not True:
            return 3
        return 0
    except PortfolioError as exc:
        print(f"shadow portfolio rejected input: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
