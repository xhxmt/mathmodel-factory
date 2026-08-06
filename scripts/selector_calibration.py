#!/usr/bin/env python3
"""Calibrate a blind pairwise quality selector on a frozen dev/holdout set.

This is an offline, fail-closed report generator.  It does not call an
evaluator and it never changes workflow routing.  Labels live in a separate
top-level calibration section; the pairwise aggregation functions consume
only blinded observations.
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

try:
    from scripts.capability_harness import (
        _is_sha256,
        _read_json,
        _write_json,
        binomial_metric,
        canonical_hash,
        evaluator_fingerprint,
        evaluator_identity,
    )
except ModuleNotFoundError:  # Direct execution from scripts/.
    from capability_harness import (  # type: ignore
        _is_sha256,
        _read_json,
        _write_json,
        binomial_metric,
        canonical_hash,
        evaluator_fingerprint,
        evaluator_identity,
    )


MANIFEST_SCHEMA = "selector-calibration-manifest-v1"
REPORT_SCHEMA = "selector-reliability-v1"
LABEL_KINDS = {"proxy", "human"}
DECISIONS = {"A", "B", "TIE"}
STATUSES = {"OK", "FORMAT_ERROR", "INDETERMINATE"}
ORIENTATIONS = {"AB", "BA"}
MINIMUM_REPEATS = 2
FORBIDDEN_OBSERVATION_KEYS = {
    "label",
    "label_source",
    "school",
    "award",
    "award_level",
    "paper_path",
    "project_path",
    "candidate_path",
}
REQUIRED_THRESHOLDS = {
    "minimum_pairs_dev",
    "minimum_pairs_holdout",
    "minimum_repeats_per_orientation",
    "accuracy_min",
    "coverage_min",
    "ab_ba_flip_rate_max",
    "repeat_pairwise_flip_rate_max",
    "format_failure_rate_max",
    "indeterminate_rate_max",
    "tie_band_candidates",
}


class SelectorError(ValueError):
    """Raised when a selector calibration contract is invalid."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SelectorError(f"{field} must be a non-empty string")
    return value


def _hash(value: Any, field: str) -> str:
    if not _is_sha256(value):
        raise SelectorError(f"{field} must be a lowercase SHA-256")
    return value


def _positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SelectorError(f"{field} must be a positive integer")
    return value


def _rate(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SelectorError(f"{field} must be between 0 and 1")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise SelectorError(f"{field} must be between 0 and 1")
    return result


def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SelectorError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise SelectorError(f"{field} must be a finite number")
    return result


def _validate_thresholds(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != REQUIRED_THRESHOLDS:
        raise SelectorError(
            f"thresholds must configure exactly {sorted(REQUIRED_THRESHOLDS)}"
        )
    thresholds: dict[str, Any] = {}
    for field in (
        "minimum_pairs_dev",
        "minimum_pairs_holdout",
        "minimum_repeats_per_orientation",
    ):
        thresholds[field] = _positive_int(raw[field], f"thresholds.{field}")
    if thresholds["minimum_repeats_per_orientation"] < MINIMUM_REPEATS:
        raise SelectorError(
            f"thresholds.minimum_repeats_per_orientation must be at least {MINIMUM_REPEATS}"
        )
    for field in (
        "accuracy_min",
        "coverage_min",
        "ab_ba_flip_rate_max",
        "repeat_pairwise_flip_rate_max",
        "format_failure_rate_max",
        "indeterminate_rate_max",
    ):
        thresholds[field] = _rate(raw[field], f"thresholds.{field}")
    bands = raw["tie_band_candidates"]
    if not isinstance(bands, list) or not bands:
        raise SelectorError("thresholds.tie_band_candidates must be a non-empty array")
    normalised = sorted({_finite(value, "thresholds.tie_band_candidates[]") for value in bands})
    if any(value <= 0 for value in normalised):
        raise SelectorError("thresholds.tie_band_candidates must be positive")
    thresholds["tie_band_candidates"] = normalised
    return thresholds


def _candidate(value: Any, pair_id: str, side: str, hard_gate_fp: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise SelectorError(f"pair {pair_id}.candidate_{side.lower()} must be an object")
    required = {
        "candidate_id",
        "packet_sha256",
        "pdf_sha256",
        "hard_gate_identity_fingerprint",
        "quality_receipt_sha256",
        "hard_pass",
    }
    if set(value) != required or value.get("hard_pass") is not True:
        raise SelectorError(
            f"pair {pair_id}.candidate_{side.lower()} must contain a hard-pass receipt"
        )
    result = {
        "candidate_id": _text(value["candidate_id"], f"pair {pair_id} candidate {side}.candidate_id"),
        "packet_sha256": _hash(value["packet_sha256"], f"pair {pair_id} candidate {side}.packet_sha256"),
        "pdf_sha256": _hash(value["pdf_sha256"], f"pair {pair_id} candidate {side}.pdf_sha256"),
        "hard_gate_identity_fingerprint": _hash(
            value["hard_gate_identity_fingerprint"],
            f"pair {pair_id} candidate {side}.hard_gate_identity_fingerprint",
        ),
        "quality_receipt_sha256": _hash(
            value["quality_receipt_sha256"],
            f"pair {pair_id} candidate {side}.quality_receipt_sha256",
        ),
    }
    if result["hard_gate_identity_fingerprint"] != hard_gate_fp:
        raise SelectorError(f"pair {pair_id} candidate {side} has a different R0a identity")
    return result


def _observation(value: Any, pair_id: str, evaluator_fp: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SelectorError(f"pair {pair_id}.observations[] must be an object")
    leaked = FORBIDDEN_OBSERVATION_KEYS.intersection(value)
    if leaked:
        raise SelectorError(
            f"pair {pair_id} selector observation contains forbidden label/identity fields: {sorted(leaked)}"
        )
    for field in ("run_id", "orientation", "status", "evaluator_identity_fingerprint"):
        _text(value.get(field), f"pair {pair_id}.observation.{field}")
    if value["orientation"] not in ORIENTATIONS:
        raise SelectorError(f"pair {pair_id} observation orientation must be AB or BA")
    if value["status"] not in STATUSES:
        raise SelectorError(f"pair {pair_id} observation status is invalid")
    if value["evaluator_identity_fingerprint"] != evaluator_fp:
        raise SelectorError(f"pair {pair_id} observation evaluator identity drift")
    result = {
        "run_id": value["run_id"],
        "orientation": value["orientation"],
        "status": value["status"],
        "evaluator_identity_fingerprint": evaluator_fp,
    }
    packet_binding = value.get("candidate_packet_sha256")
    if not isinstance(packet_binding, dict) or set(packet_binding) != {"A", "B"}:
        raise SelectorError(f"pair {pair_id} observation must bind both candidate packet hashes")
    result["candidate_packet_sha256"] = {
        "A": _hash(packet_binding["A"], f"pair {pair_id} observation.packet.A"),
        "B": _hash(packet_binding["B"], f"pair {pair_id} observation.packet.B"),
    }
    if value["status"] == "OK":
        if value.get("winner") not in DECISIONS:
            raise SelectorError(f"pair {pair_id} valid observation needs winner A/B/TIE")
        result["winner"] = value["winner"]
        result["margin"] = _finite(value.get("margin"), f"pair {pair_id} observation.margin")
        if (
            (result["winner"] == "A" and result["margin"] < 0)
            or (result["winner"] == "B" and result["margin"] > 0)
        ):
            raise SelectorError(f"pair {pair_id} observation winner contradicts its margin")
    else:
        if value.get("winner") is not None or value.get("margin") is not None:
            raise SelectorError(
                f"pair {pair_id} non-OK observation must not contain a winner or margin"
            )
    return result


def _label(value: Any, pair_id: str, split: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SelectorError(f"label for {pair_id} must be an object")
    for field in ("kind", "winner", "source", "labeled_at", "adjudication_method"):
        _text(value.get(field), f"label {pair_id}.{field}")
    if value["kind"] not in LABEL_KINDS or value["winner"] not in DECISIONS:
        raise SelectorError(f"label {pair_id} has an invalid kind or winner")
    if value.get("split") != split:
        raise SelectorError(f"label {pair_id} split does not match its pair")
    if value["kind"] == "human" and (
        value.get("blind") is not True or value.get("selector_blinded") is not True
    ):
        raise SelectorError(f"human label {pair_id} must be independently blind and selector-blinded")
    if value["kind"] == "proxy" and not _text(value.get("proxy_scope"), f"label {pair_id}.proxy_scope"):
        raise SelectorError(f"proxy label {pair_id} needs an explicit proxy scope")
    must_not_miss = value.get("must_not_miss", False)
    if not isinstance(must_not_miss, bool):
        raise SelectorError(f"label {pair_id}.must_not_miss must be boolean")
    return {
        "pair_id": pair_id,
        "split": split,
        "kind": value["kind"],
        "winner": value["winner"],
        "source": value["source"],
        "labeled_at": value["labeled_at"],
        "adjudication_method": value["adjudication_method"],
        "must_not_miss": must_not_miss,
    }


def _orientation_decision(margins: list[float], tie_band: float) -> tuple[str, float | None]:
    if not margins:
        return "INDETERMINATE", None
    margin = float(statistics.median(margins))
    if abs(margin) <= tie_band:
        return "TIE", margin
    return ("A" if margin > 0 else "B"), margin


def aggregate_pairwise(observations: Iterable[dict[str, Any]], tie_band: float) -> dict[str, Any]:
    """Aggregate blinded repeated observations; labels are intentionally absent."""
    tie_band = _finite(tie_band, "tie_band")
    if tie_band <= 0:
        raise SelectorError("tie_band must be positive")
    rows = list(observations)
    by_orientation: dict[str, list[float]] = {"AB": [], "BA": []}
    format_failures = 0
    indeterminate = 0
    for row in rows:
        if row.get("status") == "OK":
            by_orientation[row["orientation"]].append(float(row["margin"]))
        elif row.get("status") == "FORMAT_ERROR":
            format_failures += 1
        else:
            indeterminate += 1
    orientation: dict[str, dict[str, Any]] = {}
    for name in ("AB", "BA"):
        decision, margin = _orientation_decision(by_orientation[name], tie_band)
        valid = by_orientation[name]
        if valid:
            decisions = ["TIE" if abs(value) <= tie_band else ("A" if value > 0 else "B") for value in valid]
            counts = Counter(decisions)
            repeat_total = len(decisions) * (len(decisions) - 1) // 2
            repeat_agreements = sum(count * (count - 1) // 2 for count in counts.values())
            repeat_flips = repeat_total - repeat_agreements
        else:
            repeat_flips = 0
            repeat_total = 0
        orientation[name] = {
            "decision": decision,
            "margin": margin,
            "valid_runs": len(valid),
            "repeat_flips": repeat_flips,
            "repeat_comparisons": repeat_total,
        }
    valid_margins = by_orientation["AB"] + by_orientation["BA"]
    decision, margin = _orientation_decision(valid_margins, tie_band)
    ab_ba_flip = (
        orientation["AB"]["decision"] != "INDETERMINATE"
        and orientation["BA"]["decision"] != "INDETERMINATE"
        and orientation["AB"]["decision"] != orientation["BA"]["decision"]
    )
    return {
        "decision": decision,
        "margin": margin,
        "orientation": orientation,
        "observations": len(rows),
        "valid_observations": len(valid_margins),
        "format_failures": format_failures,
        "indeterminate_observations": indeterminate,
        "ab_ba_flip": ab_ba_flip,
        "ab_ba_comparable": orientation["AB"]["decision"] != "INDETERMINATE"
        and orientation["BA"]["decision"] != "INDETERMINATE",
        "repeat_flips": sum(value["repeat_flips"] for value in orientation.values()),
        "repeat_comparisons": sum(value["repeat_comparisons"] for value in orientation.values()),
    }


def _base_metrics(rows: list[dict[str, Any]], labels: dict[str, dict[str, Any]], tie_band: float) -> dict[str, Any]:
    aggregates = []
    correct = 0
    known_correct = 0
    known_total = 0
    directional = 0
    ab_ba_flips = 0
    ab_ba_total = 0
    repeat_flips = 0
    repeat_total = 0
    format_failures = 0
    indeterminate = 0
    margins: list[float] = []
    must_not_miss_failures: list[str] = []
    for pair in rows:
        aggregate = aggregate_pairwise(pair["observations"], tie_band)
        label = labels[pair["pair_id"]]
        predicted = aggregate["decision"]
        if predicted == label["winner"]:
            correct += 1
        if label["winner"] in {"A", "B"}:
            known_total += 1
            if predicted == label["winner"]:
                known_correct += 1
        if predicted in {"A", "B"}:
            directional += 1
        if aggregate["ab_ba_comparable"]:
            ab_ba_total += 1
            ab_ba_flips += int(aggregate["ab_ba_flip"])
        repeat_flips += aggregate["repeat_flips"]
        repeat_total += aggregate["repeat_comparisons"]
        format_failures += aggregate["format_failures"]
        indeterminate += aggregate["indeterminate_observations"]
        if aggregate["margin"] is not None:
            margins.append(abs(float(aggregate["margin"])))
        if label["must_not_miss"] and predicted != label["winner"]:
            must_not_miss_failures.append(pair["pair_id"])
        aggregates.append({"pair_id": pair["pair_id"], "label": label["winner"], **aggregate})
    total_pairs = len(rows)
    total_observations = sum(len(row["observations"]) for row in rows)
    return {
        "pairs": total_pairs,
        "accuracy": binomial_metric(correct, total_pairs),
        "known_order_accuracy": binomial_metric(known_correct, known_total),
        "coverage": binomial_metric(directional, total_pairs),
        "ab_ba_flip_rate": binomial_metric(ab_ba_flips, ab_ba_total),
        "repeat_pairwise_flip_rate": binomial_metric(repeat_flips, repeat_total),
        "format_failure_rate": binomial_metric(format_failures, total_observations),
        "indeterminate_rate": binomial_metric(indeterminate, total_observations),
        "margin_error_distribution": {
            "count": len(margins),
            "median": statistics.median(margins) if margins else None,
            "p90": sorted(margins)[max(0, math.ceil(len(margins) * 0.9) - 1)] if margins else None,
            "max": max(margins) if margins else None,
        },
        "must_not_miss_failures": sorted(must_not_miss_failures),
        "pair_results": aggregates,
    }


def _threshold_check(name: str, observed: Any, threshold: Any, comparison: str, passed: bool) -> dict[str, Any]:
    return {"name": name, "observed": observed, "threshold": threshold, "comparison": comparison, "passed": bool(passed)}


def _readiness(metrics: dict[str, Any], thresholds: dict[str, Any], minimum_pairs: int) -> dict[str, Any]:
    checks = [
        _threshold_check("minimum_pairs", metrics["pairs"], minimum_pairs, "observed >= threshold", metrics["pairs"] >= minimum_pairs),
        _threshold_check("accuracy", metrics["known_order_accuracy"]["wilson_95"]["low"], thresholds["accuracy_min"], "wilson_95.low >= threshold", metrics["known_order_accuracy"]["wilson_95"]["low"] is not None and metrics["known_order_accuracy"]["wilson_95"]["low"] >= thresholds["accuracy_min"]),
        _threshold_check("coverage", metrics["coverage"]["estimate"], thresholds["coverage_min"], "estimate >= threshold", metrics["coverage"]["estimate"] is not None and metrics["coverage"]["estimate"] >= thresholds["coverage_min"]),
        _threshold_check("ab_ba_flip_rate", metrics["ab_ba_flip_rate"]["wilson_95"]["high"], thresholds["ab_ba_flip_rate_max"], "wilson_95.high <= threshold", metrics["ab_ba_flip_rate"]["wilson_95"]["high"] is not None and metrics["ab_ba_flip_rate"]["wilson_95"]["high"] <= thresholds["ab_ba_flip_rate_max"]),
        _threshold_check("repeat_pairwise_flip_rate", metrics["repeat_pairwise_flip_rate"]["wilson_95"]["high"], thresholds["repeat_pairwise_flip_rate_max"], "wilson_95.high <= threshold", metrics["repeat_pairwise_flip_rate"]["wilson_95"]["high"] is not None and metrics["repeat_pairwise_flip_rate"]["wilson_95"]["high"] <= thresholds["repeat_pairwise_flip_rate_max"]),
        _threshold_check("format_failure_rate", metrics["format_failure_rate"]["wilson_95"]["high"], thresholds["format_failure_rate_max"], "wilson_95.high <= threshold", metrics["format_failure_rate"]["wilson_95"]["high"] is not None and metrics["format_failure_rate"]["wilson_95"]["high"] <= thresholds["format_failure_rate_max"]),
        _threshold_check("indeterminate_rate", metrics["indeterminate_rate"]["wilson_95"]["high"], thresholds["indeterminate_rate_max"], "wilson_95.high <= threshold", metrics["indeterminate_rate"]["wilson_95"]["high"] is not None and metrics["indeterminate_rate"]["wilson_95"]["high"] <= thresholds["indeterminate_rate_max"]),
        _threshold_check("must_not_miss", metrics["must_not_miss_failures"], [], "no failures", not metrics["must_not_miss_failures"]),
    ]
    return {"ready": all(item["passed"] for item in checks), "checks": checks}


def _validate_manifest(manifest: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise SelectorError(f"manifest schema must be {MANIFEST_SCHEMA}")
    _text(manifest.get("run_id"), "run_id")
    if manifest.get("frozen") is not True or manifest.get("holdout_unsealed") is not False:
        raise SelectorError("selector manifest must be frozen and holdout_unsealed=false")
    dataset_version = _text(manifest.get("dataset_version"), "dataset_version")
    raw_evaluator = manifest.get("evaluator")
    if not isinstance(raw_evaluator, dict):
        raise SelectorError("evaluator must be an object")
    try:
        evaluator = evaluator_identity(raw_evaluator)
    except Exception as exc:
        raise SelectorError(str(exc)) from exc
    evaluator_fp = evaluator_fingerprint(evaluator)
    if manifest.get("evaluator_identity_fingerprint") != evaluator_fp:
        raise SelectorError("evaluator identity fingerprint mismatch")
    hard_gate_fp = _hash(manifest.get("hard_gate_identity_fingerprint"), "hard_gate_identity_fingerprint")
    thresholds = _validate_thresholds(manifest.get("thresholds"))
    pairs_raw = manifest.get("pairs")
    labels_raw = manifest.get("labels")
    if not isinstance(pairs_raw, list) or not pairs_raw:
        raise SelectorError("pairs must be a non-empty array")
    if not isinstance(labels_raw, list):
        raise SelectorError("labels must be a separate top-level array")
    pairs: list[dict[str, Any]] = []
    pair_ids: set[str] = set()
    split_families: dict[str, set[str]] = {"dev": set(), "holdout": set()}
    candidate_splits: dict[str, str] = {}
    labels: dict[str, dict[str, Any]] = {}
    for raw in pairs_raw:
        if not isinstance(raw, dict):
            raise SelectorError("pair must be an object")
        pair_id = _text(raw.get("pair_id"), "pair_id")
        if pair_id in pair_ids:
            raise SelectorError(f"duplicate pair_id: {pair_id}")
        pair_ids.add(pair_id)
        split = raw.get("split")
        if split not in {"dev", "holdout"}:
            raise SelectorError(f"pair {pair_id}.split must be dev or holdout")
        family = _text(raw.get("family_id"), f"pair {pair_id}.family_id")
        problem = _text(raw.get("problem_identity"), f"pair {pair_id}.problem_identity")
        axes = raw.get("quality_axes")
        if not isinstance(axes, list) or not axes or any(not isinstance(axis, str) or not axis.strip() for axis in axes):
            raise SelectorError(f"pair {pair_id}.quality_axes must be a non-empty string array")
        candidate_a = _candidate(raw.get("candidate_a"), pair_id, "A", hard_gate_fp)
        candidate_b = _candidate(raw.get("candidate_b"), pair_id, "B", hard_gate_fp)
        if candidate_a["candidate_id"] == candidate_b["candidate_id"]:
            raise SelectorError(f"pair {pair_id} candidates must be distinct")
        if candidate_a["packet_sha256"] == candidate_b["packet_sha256"] and candidate_a["pdf_sha256"] == candidate_b["pdf_sha256"]:
            raise SelectorError(f"pair {pair_id} candidates have identical packet and PDF bytes")
        for candidate in (candidate_a, candidate_b):
            previous = candidate_splits.get(candidate["candidate_id"])
            if previous is not None and previous != split:
                raise SelectorError(f"candidate {candidate['candidate_id']} leaks across dev/holdout")
            candidate_splits[candidate["candidate_id"]] = split
        observations_raw = raw.get("observations")
        if not isinstance(observations_raw, list):
            raise SelectorError(f"pair {pair_id}.observations must be an array")
        observations = [_observation(value, pair_id, evaluator_fp) for value in observations_raw]
        if len({row["run_id"] for row in observations}) != len(observations):
            raise SelectorError(f"pair {pair_id} observation run_ids must be unique")
        counts = Counter(row["orientation"] for row in observations)
        minimum_repeats = thresholds["minimum_repeats_per_orientation"]
        if counts["AB"] < minimum_repeats or counts["BA"] < minimum_repeats:
            raise SelectorError(f"pair {pair_id} needs balanced AB/BA repeats")
        for row in observations:
            binding = row.get("candidate_packet_sha256")
            if binding != {"A": candidate_a["packet_sha256"], "B": candidate_b["packet_sha256"]}:
                raise SelectorError(f"pair {pair_id} observation packet identity drift")
        split_families[split].add(family)
        pairs.append({
            "pair_id": pair_id,
            "split": split,
            "family_id": family,
            "problem_identity": problem,
            "quality_axes": sorted(set(axes)),
            "candidate_a": candidate_a,
            "candidate_b": candidate_b,
            "observations": observations,
        })
    overlap = split_families["dev"].intersection(split_families["holdout"])
    if overlap:
        raise SelectorError(f"dev/holdout family leakage: {sorted(overlap)}")
    if len([pair for pair in pairs if pair["split"] == "dev"]) < thresholds["minimum_pairs_dev"]:
        raise SelectorError("dev set is below its minimum pair count")
    if len([pair for pair in pairs if pair["split"] == "holdout"]) < thresholds["minimum_pairs_holdout"]:
        raise SelectorError("holdout set is below its minimum pair count")
    for raw in labels_raw:
        if not isinstance(raw, dict):
            raise SelectorError("label must be an object")
        pair_id = _text(raw.get("pair_id"), "label.pair_id")
        if pair_id in labels:
            raise SelectorError(f"duplicate label: {pair_id}")
        pair = next((item for item in pairs if item["pair_id"] == pair_id), None)
        if pair is None:
            raise SelectorError(f"label refers to unknown pair: {pair_id}")
        labels[pair_id] = _label(raw, pair_id, pair["split"])
    if set(labels) != pair_ids:
        raise SelectorError("labels must exactly cover every pair")
    return evaluator, labels, pairs, {"dataset_version": dataset_version, "evaluator_identity_fingerprint": evaluator_fp, "hard_gate_identity_fingerprint": hard_gate_fp, "thresholds": thresholds, "split_families": split_families}


def _select_tie_band(dev_pairs: list[dict[str, Any]], labels: dict[str, dict[str, Any]], candidates: list[float]) -> tuple[float, list[dict[str, Any]]]:
    evaluations = []
    for band in candidates:
        metrics = _base_metrics(dev_pairs, labels, band)
        accuracy = metrics["known_order_accuracy"]["estimate"]
        coverage = metrics["coverage"]["estimate"]
        flip = metrics["ab_ba_flip_rate"]["estimate"]
        evaluations.append({"tie_band": band, "known_order_accuracy": accuracy, "coverage": coverage, "ab_ba_flip_rate": flip, "metrics": metrics})
    selected = max(evaluations, key=lambda item: (
        -1 if item["known_order_accuracy"] is None else item["known_order_accuracy"],
        -1 if item["coverage"] is None else item["coverage"],
        -1 if item["ab_ba_flip_rate"] is None else -item["ab_ba_flip_rate"],
        -item["tie_band"],
    ))
    return selected["tie_band"], evaluations


def _group_metrics(
    rows: list[dict[str, Any]],
    labels: dict[str, dict[str, Any]],
    tie_band: float,
    field: str,
) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for pair in rows:
        values = pair["quality_axes"] if field == "quality_axes" else [pair[field]]
        for value in values:
            groups.setdefault(value, []).append(pair)
    result: dict[str, Any] = {}
    for value, group in sorted(groups.items()):
        metrics = _base_metrics(group, labels, tie_band)
        result[value] = {
            "pairs": len(group),
            "coverage": metrics["coverage"],
            "known_order_accuracy": metrics["known_order_accuracy"],
        }
    return result


def evaluate_selector_calibration(manifest: dict[str, Any]) -> dict[str, Any]:
    evaluator, labels, pairs, metadata = _validate_manifest(manifest)
    thresholds = metadata["thresholds"]
    dev_pairs = [pair for pair in pairs if pair["split"] == "dev"]
    holdout_pairs = [pair for pair in pairs if pair["split"] == "holdout"]
    tie_band, tie_band_evaluations = _select_tie_band(dev_pairs, labels, thresholds["tie_band_candidates"])
    dev_metrics = _base_metrics(dev_pairs, labels, tie_band)
    holdout_metrics = _base_metrics(holdout_pairs, labels, tie_band)
    kind_metrics: dict[str, dict[str, Any]] = {}
    readiness: dict[str, dict[str, Any]] = {}
    for kind in ("proxy", "human"):
        kind_pairs = [pair for pair in holdout_pairs if labels[pair["pair_id"]]["kind"] == kind]
        metrics = _base_metrics(kind_pairs, labels, tie_band) if kind_pairs else _base_metrics([], {}, tie_band)
        kind_metrics[kind] = metrics
        readiness[kind] = _readiness(metrics, thresholds, thresholds["minimum_pairs_holdout"])
        if not kind_pairs:
            readiness[kind] = {"ready": False, "checks": [{"name": "label_coverage", "observed": 0, "threshold": 1, "comparison": "at least one holdout pair", "passed": False}]}
    holdout_hash = canonical_hash({
        "dataset_version": metadata["dataset_version"],
        "pairs": holdout_pairs,
        "labels": {key: labels[key] for key in sorted(labels) if labels[key]["split"] == "holdout"},
    })
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "calibration_manifest_sha256": canonical_hash(manifest),
        "dataset_version": metadata["dataset_version"],
        "holdout_hash": holdout_hash,
        "evaluator": evaluator,
        "evaluator_identity_fingerprint": metadata["evaluator_identity_fingerprint"],
        "hard_gate_identity_fingerprint": metadata["hard_gate_identity_fingerprint"],
        "split_families": {
            "dev": sorted(metadata["split_families"]["dev"]),
            "holdout": sorted(metadata["split_families"]["holdout"]),
        },
        "tie_band": tie_band,
        "minimum_distinguishable_margin": tie_band,
        "tie_band_selection": {
            "source": "DEV_ONLY",
            "candidates": thresholds["tie_band_candidates"],
            "selected": tie_band,
            "evaluations": [
                {key: value for key, value in item.items() if key != "metrics"}
                for item in tie_band_evaluations
            ],
        },
        "dev": {"metrics": dev_metrics, "pair_count": len(dev_pairs)},
        "holdout": {"metrics": holdout_metrics, "pair_count": len(holdout_pairs)},
        "coverage_by_problem": _group_metrics(holdout_pairs, labels, tie_band, "problem_identity"),
        "coverage_by_quality_axis": _group_metrics(holdout_pairs, labels, tie_band, "quality_axes"),
        "holdout_by_label_kind": kind_metrics,
        "readiness": readiness,
        "comparison_ready_proxy": readiness["proxy"]["ready"],
        "comparison_ready_human": readiness["human"]["ready"],
        "comparison_ready": readiness["human"]["ready"],
        "advisory_only": True,
        "automatic_switch_performed": False,
        "operator_authorization_required": True,
        "production_selection_authorized": False,
        "claim_limit": "BLIND_PAIRWISE_SELECTOR_CALIBRATION_ONLY",
        "human_alignment": "SUPPORTED_ONLY_BY_INDEPENDENT_BLIND_HOLDOUT",
        "award_prediction": "UNAVAILABLE",
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
        report = evaluate_selector_calibration(_read_json(args.manifest.resolve()))
        output = args.json_output.resolve()
        _write_json(output, report)
        print(output)
        if args.require_ready and report["comparison_ready"] is not True:
            return 3
        return 0
    except SelectorError as exc:
        print(f"selector calibration rejected input: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
