#!/usr/bin/env python3
"""Aggregate repeated judge decisions and report judge self-reliability.

This module is intentionally *not* a truth calibrator.  It answers a narrower
question: when the same immutable packet and evaluator contract are presented
repeatedly, how stable are the returned decisions and diagnostic scores?

The input is a small, machine-readable receipt (``judge-reliability-input-v1``)
containing one or more role streams.  A role stream may contain malformed runs;
those runs are retained in the report and force a conservative action where
appropriate instead of silently disappearing from the denominator.

Hard roles (normally ``math`` and ``execution``) use a veto/three-valued rule:

* every valid run is ``PASS`` and all requested runs are valid -> ``PASS``;
* any valid ``FAIL`` -> ``FAIL`` (a hard veto is never averaged away);
* all other cases -> ``INDETERMINATE``.

Paper-role scores and dimensions are descriptive medians only.  They are never
translated into award tiers or treated as human-judge agreement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


INPUT_SCHEMA = "judge-reliability-input-v1"
OUTPUT_SCHEMA = "judge-reliability-v1"
ROLE_KINDS = {"hard", "paper"}
HARD_VERDICTS = {"PASS", "FAIL", "INDETERMINATE"}
PAPER_VERDICTS = {"PASS", "REVISE", "INDETERMINATE"}
DEFAULT_MIN_RUNS = 3
MIN_REPEATABILITY_RUNS = 2
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ReliabilityError(ValueError):
    """Raised when a reliability receipt cannot be interpreted safely."""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_hash(value: Any) -> str:
    """Return a stable hash for an input/output identity receipt."""
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return _sha256_text(payload)


def _reject_nonfinite(value: Any, where: str = "input") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ReliabilityError(f"{where} contains a non-finite number")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_nonfinite(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_nonfinite(item, f"{where}[{index}]")


def _safe_text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReliabilityError(f"{where} must be a non-empty string")
    return value.strip()


def _valid_sha256(value: Any) -> bool:
    """Accept only the lowercase hexadecimal form used by runtime receipts."""
    return isinstance(value, str) and bool(SHA256_RE.fullmatch(value))


def _normalise_kind(role: str, value: Any) -> str:
    canonical = {"math": "hard", "execution": "hard", "paper": "paper"}.get(role)
    if value is None:
        value = canonical
    if canonical is not None and value != canonical:
        raise ReliabilityError(f"role {role!r} must use kind {canonical}")
    if value not in ROLE_KINDS:
        raise ReliabilityError(f"role {role!r} kind must be hard or paper")
    return str(value)


def _normalise_verdict(value: Any, kind: str, where: str) -> str:
    verdict = _safe_text(value, where).upper()
    # Role runners occasionally expose a route label instead of the role-level
    # verdict.  Normalise only unambiguous aliases; keep the raw value in the
    # run receipt so the conversion is auditable.
    aliases = {
        "REOPEN_MODEL": "FAIL",
        "REOPEN_REVISION_MODEL": "FAIL",
        "REOPEN_TEXT": "REVISE",
        "REOPEN_REVISION_TEXT": "REVISE",
        "INDETERMINATE_REVIEW": "INDETERMINATE",
    }
    verdict = aliases.get(verdict, verdict)
    allowed = HARD_VERDICTS if kind == "hard" else PAPER_VERDICTS
    if verdict not in allowed:
        raise ReliabilityError(f"{where} has unsupported verdict {value!r} for {kind} role")
    return verdict


def _valid_score(value: Any, where: str) -> float:
    if not _is_number(value) or not 0 <= float(value) <= 100:
        raise ReliabilityError(f"{where} must be a finite number in [0, 100]")
    return float(value)


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _numeric_summary(values: Iterable[float]) -> dict[str, Any]:
    numbers = [float(value) for value in values]
    if not numbers:
        return {
            "n": 0,
            "median": None,
            "min": None,
            "max": None,
            "range": None,
            "mean": None,
            "stddev_population": None,
            "mad": None,
            "iqr": None,
            "empirical_quantiles": {"p02_5": None, "p97_5": None},
        }
    median = float(statistics.median(numbers))
    q25 = _quantile(numbers, 0.25)
    q75 = _quantile(numbers, 0.75)
    return {
        "n": len(numbers),
        "median": median,
        "min": min(numbers),
        "max": max(numbers),
        "range": max(numbers) - min(numbers),
        "mean": statistics.fmean(numbers),
        "stddev_population": statistics.pstdev(numbers) if len(numbers) > 1 else 0.0,
        "mad": statistics.median([abs(value - median) for value in numbers]),
        "iqr": (q75 - q25) if q25 is not None and q75 is not None else None,
        "empirical_quantiles": {
            "p02_5": _quantile(numbers, 0.025),
            "p97_5": _quantile(numbers, 0.975),
        },
    }


def _entropy(labels: Sequence[str]) -> dict[str, Any]:
    if not labels:
        return {"bits": None, "normalised": None, "categories": {}}
    counts = Counter(labels)
    total = len(labels)
    bits = -sum((count / total) * math.log2(count / total) for count in counts.values())
    max_bits = math.log2(len(counts)) if len(counts) > 1 else 0.0
    return {
        "bits": bits,
        "normalised": bits / max_bits if max_bits else 0.0,
        "categories": dict(sorted(counts.items())),
    }


def _agreement(labels: Sequence[str]) -> dict[str, Any]:
    n = len(labels)
    counts = Counter(labels)
    if n == 0:
        return {
            "n": 0,
            "modal_verdict": None,
            "modal_count": 0,
            "modal_agreement_rate": None,
            "pairwise_agreement_rate": None,
            "pairwise_comparisons": 0,
            "pairwise_agreements": 0,
            "entropy": _entropy(labels),
        }
    modal_count = max(counts.values())
    modes = sorted(label for label, count in counts.items() if count == modal_count)
    pairwise_total = n * (n - 1) // 2
    pairwise_agree = sum(count * (count - 1) // 2 for count in counts.values())
    return {
        "n": n,
        "modal_verdict": modes[0] if len(modes) == 1 else None,
        "modal_candidates": modes,
        "modal_count": modal_count,
        "modal_agreement_rate": modal_count / n,
        "pairwise_agreement_rate": pairwise_agree / pairwise_total if pairwise_total else None,
        "pairwise_comparisons": pairwise_total,
        "pairwise_agreements": pairwise_agree,
        "entropy": _entropy(labels),
    }


def _position_consistency(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Measure AB/BA order effects when a caller supplies paired choices.

    A run may include ``pair_id``, ``orientation`` (``AB``/``BA``), and
    ``winner`` (the displayed position ``A`` or ``B``).  The winner is mapped
    back to the underlying candidate before comparing orientations.  Missing
    or malformed pair metadata is reported as UNKNOWN; it is never silently
    treated as position-neutral.
    """

    pairs: dict[str, dict[str, str]] = defaultdict(dict)
    malformed = 0
    for run in runs:
        pair_id = run.get("pair_id")
        orientation = str(run.get("orientation", "")).upper()
        winner = str(run.get("winner", "")).upper()
        if pair_id is None and "orientation" not in run and "winner" not in run:
            continue
        if not isinstance(pair_id, str) or not pair_id.strip() or orientation not in {"AB", "BA"} or winner not in {"A", "B"}:
            malformed += 1
            continue
        # In BA the displayed A slot contains underlying candidate B.
        underlying = winner if orientation == "AB" else ("B" if winner == "A" else "A")
        if orientation in pairs[pair_id]:
            malformed += 1
        pairs[pair_id][orientation] = underlying
    complete = [value for value in pairs.values() if set(value) == {"AB", "BA"}]
    incomplete_count = sum(1 for value in pairs.values() if set(value) != {"AB", "BA"})
    if malformed or not complete or incomplete_count:
        return {
            "status": "UNKNOWN",
            "reason": (
                "MISSING_OR_MALFORMED_AB_BA_PAIRS"
                if malformed
                else "INCOMPLETE_AB_BA_PAIRS"
                if incomplete_count
                else "NO_COMPLETE_AB_BA_PAIRS"
            ),
            "pairs_total": len(pairs),
            "pairs_complete": len(complete),
            "pairs_incomplete": incomplete_count,
            "malformed_count": malformed,
            "consistent_pairs": None,
            "consistency_rate": None,
            "first_position_rate": None,
        }
    consistent = sum(value["AB"] == value["BA"] for value in complete)
    first_position = sum(
        (value["AB"] == "A") + (value["BA"] == "B")
        for value in complete
    )
    total_orientations = 2 * len(complete)
    return {
        "status": "OK",
        "reason": None,
        "pairs_total": len(pairs),
        "pairs_complete": len(complete),
        "pairs_incomplete": 0,
        "malformed_count": malformed,
        "consistent_pairs": consistent,
        "consistency_rate": consistent / len(complete),
        "first_position_rate": first_position / total_orientations,
    }


def simple_nominal_alpha(units: Any) -> dict[str, Any]:
    """Compute a conservative nominal alpha over a unit/rater matrix.

    ``units`` is a list of ``{"unit_id": str, "ratings": {rater: label}}``.
    This is the nominal coincidence form reduced to pairwise disagreements:
    observed disagreement is the weighted fraction of disagreeing rater pairs
    within units; expected disagreement is the fraction after pooling all
    ratings.  With fewer than two units, fewer than two ratings per unit, or a
    single global category, alpha is mathematically unidentifiable and returns
    ``status=UNKNOWN`` rather than a fabricated zero.
    """

    unknown = lambda reason, **extra: {
        "value": None,
        "status": "UNKNOWN",
        "reason": reason,
        **extra,
    }
    if not isinstance(units, list):
        return unknown("UNITS_MISSING_OR_NOT_ARRAY", units=0, n_units=0, n_raters=0, n_observations=0, missing_count=0)
    if len(units) < 2:
        return unknown("INSUFFICIENT_UNITS", units=len(units), n_units=len(units), n_raters=0, n_observations=0, missing_count=0)
    observed_disagreements = 0
    observed_pairs = 0
    pooled: Counter[str] = Counter()
    usable_units = 0
    rater_names: set[str] = set()
    rater_sets: set[tuple[str, ...]] = set()
    observed_ratings = 0
    seen_unit_ids: set[str] = set()
    for index, unit in enumerate(units):
        if not isinstance(unit, dict):
            return unknown("INVALID_UNIT", units=len(units), n_units=usable_units, n_raters=len(rater_names), n_observations=observed_ratings, missing_count=1)
        unit_id = unit.get("unit_id")
        if not isinstance(unit_id, str) or not unit_id.strip() or unit_id in seen_unit_ids:
            return unknown("DUPLICATE_OR_MISSING_UNIT_ID", units=len(units), n_units=usable_units, n_raters=len(rater_names), n_observations=observed_ratings, missing_count=1)
        seen_unit_ids.add(unit_id)
        ratings = unit.get("ratings")
        if not isinstance(ratings, dict):
            return unknown("INVALID_RATINGS", units=len(units), n_units=usable_units, n_raters=len(rater_names), n_observations=observed_ratings, missing_count=1)
        if any(not isinstance(rater, str) or not rater.strip() for rater in ratings):
            return unknown("INVALID_RATER_ID", units=len(units), n_units=usable_units, n_raters=len(rater_names), n_observations=observed_ratings, missing_count=1)
        if any(not isinstance(label, str) or not label.strip() for label in ratings.values()):
            return unknown("INVALID_RATING", units=len(units), n_units=usable_units, n_raters=len(rater_names), n_observations=observed_ratings, missing_count=1)
        labels = [label.strip() for label in ratings.values()]
        if len(labels) < 2:
            return unknown("EACH_UNIT_NEEDS_TWO_RATINGS", units=len(units), n_units=usable_units, n_raters=len(rater_names), n_observations=observed_ratings, missing_count=1)
        usable_units += 1
        rater_names.update(ratings)
        rater_sets.add(tuple(sorted(ratings)))
        observed_ratings += len(labels)
        pooled.update(labels)
        pairs = len(labels) * (len(labels) - 1) // 2
        observed_pairs += pairs
        observed_disagreements += pairs - sum(count * (count - 1) // 2 for count in Counter(labels).values())
    if observed_pairs == 0 or len(pooled) < 2:
        return unknown("DEGENERATE_EXPECTED_DISAGREEMENT", units=usable_units, n_units=usable_units, n_raters=len(rater_names), n_observations=observed_ratings, missing_count=0, categories=dict(sorted(pooled.items())))
    if len(rater_sets) > 1:
        return unknown("INCONSISTENT_RATER_SET", units=usable_units, n_units=usable_units, n_raters=len(rater_names), n_observations=observed_ratings, missing_count=1)
    total_pooled = sum(pooled.values())
    pooled_pairs = total_pooled * (total_pooled - 1) // 2
    expected_disagreements = pooled_pairs - sum(count * (count - 1) // 2 for count in pooled.values())
    if pooled_pairs == 0 or expected_disagreements == 0:
        return unknown("DEGENERATE_EXPECTED_DISAGREEMENT", units=usable_units, n_units=usable_units, n_raters=len(rater_names), n_observations=observed_ratings, missing_count=0, categories=dict(sorted(pooled.items())))
    observed = observed_disagreements / observed_pairs
    expected = expected_disagreements / pooled_pairs
    alpha = 1.0 - observed / expected
    return {
        "value": alpha,
        "status": "OK",
        "reason": None,
        "units": usable_units,
        "n_units": usable_units,
        "n_raters": len(rater_names),
        "n_observations": observed_ratings,
        "missing_count": 0,
        "categories": dict(sorted(pooled.items())),
        "observed_disagreement": observed,
        "expected_disagreement": expected,
    }


def _validate_batch_alpha_units(
    units: Any, current_packet_identity: Mapping[str, Any], role: str
) -> dict[str, Any] | None:
    """Return an UNKNOWN receipt when alpha units are not distinct packets."""

    if not isinstance(units, list) or len(units) < 2:
        return {"value": None, "status": "UNKNOWN", "reason": "INSUFFICIENT_UNITS", "units": 0}
    identities: list[str] = []
    packet_components: list[str] = []
    conditions: set[str | None] = set()
    for index, unit in enumerate(units):
        if not isinstance(unit, Mapping):
            return {"value": None, "status": "UNKNOWN", "reason": "INVALID_UNIT_IDENTITY", "units": index}
        identity, error = _validate_packet_identity(unit.get("packet_identity"))
        if error or identity is None:
            return {
                "value": None,
                "status": "UNKNOWN",
                "reason": "BATCH_PACKET_IDENTITY_REQUIRED",
                "detail": error,
                "units": index,
            }
        if "packet_fingerprints" in identity and role not in identity["packet_fingerprints"]:
            return {
                "value": None,
                "status": "UNKNOWN",
                "reason": "BATCH_ROLE_PACKET_IDENTITY_MISSING",
                "units": index,
            }
        ratings = unit.get("ratings")
        allowed_labels = HARD_VERDICTS if role in {"math", "execution"} else PAPER_VERDICTS
        if not isinstance(ratings, Mapping) or any(label not in allowed_labels for label in ratings.values()):
            return {
                "value": None,
                "status": "UNKNOWN",
                "reason": "BATCH_LABEL_OUT_OF_DOMAIN",
                "units": index,
            }
        identities.append(canonical_hash(identity))
        packet_component = {
            key: identity[key]
            for key in ("packet_sha256", "packet_fingerprints")
            if key in identity
        }
        packet_components.append(canonical_hash(packet_component))
        conditions.add(identity.get("configuration_fingerprint") or identity.get("condition_fingerprint"))
    if len(set(identities)) != len(identities) or len(set(packet_components)) != len(packet_components):
        return {"value": None, "status": "UNKNOWN", "reason": "DUPLICATE_PACKET_UNIT", "units": len(units)}
    current_component = canonical_hash(
        {
            key: current_packet_identity[key]
            for key in ("packet_sha256", "packet_fingerprints")
            if key in current_packet_identity
        }
    )
    if any(component == current_component for component in packet_components):
        return {"value": None, "status": "UNKNOWN", "reason": "BATCH_MIXES_CURRENT_PACKET", "units": len(units)}
    if len(conditions - {None}) > 1:
        return {"value": None, "status": "UNKNOWN", "reason": "BATCH_INCONSISTENT_CONDITION", "units": len(units)}
    return None


def _packet_identity_match(raw: Mapping[str, Any], expected: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Compare a run's packet receipt with the top-level identity.

    Runtime evaluation stores one packet fingerprint per role.  A compact
    single ``packet_sha256`` is also accepted for small harnesses.  The two
    forms are never mixed: accepting a partial map would make a repeat look
    comparable while actually referring to a different role packet.
    """

    if "packet_sha256" in expected:
        if "packet_fingerprints" in raw:
            return False, "mixed packet identity forms"
        actual = raw.get("packet_sha256")
        if actual is None:
            return False, "packet_sha256 is missing"
        if actual != expected["packet_sha256"]:
            return False, "packet_sha256 does not match packet_identity"
        return True, None
    expected_map = expected.get("packet_fingerprints")
    if "packet_sha256" in raw:
        return False, "mixed packet identity forms"
    actual_map = raw.get("packet_fingerprints")
    if not isinstance(actual_map, Mapping):
        return False, "packet_fingerprints is missing"
    if dict(actual_map) != dict(expected_map):
        return False, "packet_fingerprints do not match packet_identity"
    return True, None


def _normalise_run(
    raw: Any,
    index: int,
    kind: str,
    *,
    expected_packet_identity: Mapping[str, Any],
    expected_condition_sha256: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(raw, dict):
        return None, {"index": index, "error": "run must be an object"}
    run_id = raw.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        return None, {"index": index, "error": "run_id must be a non-empty string"}
    run_id = run_id.strip()
    packet_ok, reason = _packet_identity_match(raw, expected_packet_identity)
    if not packet_ok:
        return None, {"index": index, "run_id": run_id, "error": reason}
    condition_sha256 = raw.get("condition_sha256")
    if expected_condition_sha256 is not None and condition_sha256 != expected_condition_sha256:
        reason = (
            "condition_sha256 is missing"
            if condition_sha256 is None
            else "condition_sha256 does not match condition_identity"
        )
        return None, {"index": index, "run_id": run_id, "error": reason}
    verdict_raw = raw.get("verdict", raw.get("decision"))
    try:
        verdict = _normalise_verdict(verdict_raw, kind, f"run {run_id}.verdict")
    except ReliabilityError as exc:
        return None, {"index": index, "run_id": run_id, "error": str(exc)}
    normalised: dict[str, Any] = {
        "run_id": run_id,
        "verdict": verdict,
        "raw_verdict": verdict_raw,
        "packet_identity": (
            {"packet_sha256": raw.get("packet_sha256")}
            if "packet_sha256" in expected_packet_identity
            else {"packet_fingerprints": dict(raw.get("packet_fingerprints", {}))}
        ),
    }
    if condition_sha256 is not None:
        normalised["condition_sha256"] = condition_sha256
    for field in (
        "rater_id", "unit_id", "model", "backend", "prompt_sha256", "schema_sha256",
        "pair_id", "orientation", "winner",
    ):
        if field in raw:
            normalised[field] = raw[field]
    score = raw.get("score", raw.get("overall_score"))
    if score is not None:
        if verdict == "INDETERMINATE":
            normalised["score_error"] = f"run {run_id}.score is not valid for INDETERMINATE"
        else:
            try:
                normalised["score"] = _valid_score(score, f"run {run_id}.score")
            except ReliabilityError as exc:
                normalised["score_error"] = str(exc)
    dimensions = raw.get("dimensions")
    if dimensions is not None:
        if verdict == "INDETERMINATE":
            normalised["dimensions_error"] = f"run {run_id}.dimensions is not valid for INDETERMINATE"
        elif not isinstance(dimensions, dict):
            normalised["dimensions_error"] = f"run {run_id}.dimensions must be an object"
        else:
            clean_dimensions: dict[str, float] = {}
            dimension_errors: list[str] = []
            for name, value in dimensions.items():
                try:
                    clean_dimensions[str(name)] = _valid_score(value, f"run {run_id}.dimensions.{name}")
                except ReliabilityError as exc:
                    dimension_errors.append(str(exc))
            normalised["dimensions"] = clean_dimensions
            if dimension_errors:
                normalised["dimensions_errors"] = dimension_errors
    return normalised, None


def _role_result(
    role: str,
    payload: Mapping[str, Any],
    min_runs: int,
    *,
    packet_identity: Mapping[str, Any],
    condition_sha256: str | None = None,
) -> dict[str, Any]:
    kind = _normalise_kind(role, payload.get("kind"))
    runs_raw = payload.get("runs")
    if not isinstance(runs_raw, list) or not runs_raw:
        raise ReliabilityError(f"role {role!r} runs must be a non-empty array")
    required_dimensions = payload.get("required_dimensions", [])
    if not isinstance(required_dimensions, list) or any(
        not isinstance(name, str) or not name.strip() for name in required_dimensions
    ):
        raise ReliabilityError(f"role {role!r}.required_dimensions must be a string array")
    required_dimensions = [str(name) for name in required_dimensions]
    dimension_specs_raw = payload.get("dimension_specs")
    dimension_specs: dict[str, float] = {}
    if dimension_specs_raw is not None:
        if not isinstance(dimension_specs_raw, Mapping) or not dimension_specs_raw:
            raise ReliabilityError(f"role {role!r}.dimension_specs must be a non-empty object")
        for name, maximum in dimension_specs_raw.items():
            if not isinstance(name, str) or not name.strip() or not _is_number(maximum) or not 0 < float(maximum) <= 100:
                raise ReliabilityError(f"role {role!r}.dimension_specs is invalid")
            dimension_specs[name.strip()] = float(maximum)
        if required_dimensions and set(required_dimensions) != set(dimension_specs):
            raise ReliabilityError(f"role {role!r}.required_dimensions must match dimension_specs")
        required_dimensions = sorted(dimension_specs)
    valid_runs: list[dict[str, Any]] = []
    invalid_runs: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(runs_raw):
        run, error = _normalise_run(
            raw,
            index,
            kind,
            expected_packet_identity=packet_identity,
            expected_condition_sha256=condition_sha256,
        )
        if run is not None:
            if dimension_specs:
                dimensions = run.get("dimensions")
                dimension_errors: list[str] = []
                if not isinstance(dimensions, Mapping) or set(dimensions) != set(dimension_specs):
                    dimension_errors.append("dimension keys do not match dimension_specs")
                else:
                    for name, maximum in dimension_specs.items():
                        if not _is_number(dimensions[name]) or float(dimensions[name]) > maximum:
                            dimension_errors.append(f"dimension {name} exceeds its maximum")
                    if "score" in run and not math.isclose(
                        sum(float(dimensions[name]) for name in dimension_specs),
                        run["score"],
                        abs_tol=0.01,
                    ):
                        run["score_error"] = "score does not equal sum of dimension scores"
                if dimension_errors:
                    run["dimensions_errors"] = dimension_errors
            if run["run_id"] in seen_ids:
                invalid_runs.append({"index": index, "run_id": run["run_id"], "error": "duplicate run_id"})
            else:
                seen_ids.add(run["run_id"])
                valid_runs.append(run)
        else:
            invalid_runs.append(error or {"index": index, "error": "invalid run"})

    labels = [run["verdict"] for run in valid_runs]
    agreement = _agreement(labels)
    position_consistency = _position_consistency(valid_runs)
    counts = dict(sorted(Counter(labels).items()))
    majority = agreement.get("modal_verdict") if agreement.get("modal_count", 0) * 2 > len(labels) else None
    decision_min_runs = max(MIN_REPEATABILITY_RUNS, min_runs)
    if kind == "hard":
        if "FAIL" in labels:
            effective = "FAIL"
            rule = "hard_veto_any_fail"
        elif (
            not invalid_runs
            and labels
            and all(label == "PASS" for label in labels)
            and len(labels) >= decision_min_runs
        ):
            effective = "PASS"
            rule = "unanimous_pass_with_complete_runs"
        else:
            effective = "INDETERMINATE"
            rule = "incomplete_or_nonunanimous_hard_runs"
    else:
        if not labels:
            effective = "INDETERMINATE"
            rule = "no_valid_paper_runs"
        elif "INDETERMINATE" in labels:
            effective = "INDETERMINATE"
            rule = "paper_indeterminate_is_not_averaged_away"
        elif invalid_runs or len(labels) < decision_min_runs:
            effective = "INDETERMINATE"
            rule = "incomplete_paper_runs"
        elif majority is not None:
            effective = majority
            rule = "strict_majority_paper_verdict"
        else:
            effective = "INDETERMINATE"
            rule = "paper_verdict_tie"

    scores = [
        run["score"]
        for run in valid_runs
        if "score" in run
        and "score_error" not in run
        and "dimensions_errors" not in run
    ]
    score_summary = _numeric_summary(scores)
    dimension_names = sorted({name for run in valid_runs for name in run.get("dimensions", {})})
    dimension_summary = {
        name: _numeric_summary(
            run["dimensions"][name]
            for run in valid_runs
            if name in run.get("dimensions", {}) and not run.get("dimensions_errors")
        )
        for name in dimension_names
    }
    expected_dimension_names = set(required_dimensions) if required_dimensions else set(dimension_summary)
    dimension_complete_for_recompute = bool(dimension_summary) and set(dimension_summary) == expected_dimension_names and all(
        value["n"] == len(runs_raw) and value["median"] is not None
        for value in dimension_summary.values()
    )
    dimension_medians = [
        value["median"] for value in dimension_summary.values() if value["median"] is not None
    ]
    median_recomputed = sum(dimension_medians) if dimension_complete_for_recompute else None
    median_total_delta = (
        score_summary["median"] - median_recomputed
        if score_summary["median"] is not None and median_recomputed is not None
        else None
    )
    dimension_complete_runs = sum(
        all(
            name in run.get("dimensions", {})
            and not run.get("dimensions_errors")
            for name in required_dimensions
        )
        for run in valid_runs
    ) if required_dimensions else len(valid_runs)
    # A single packet has one statistical unit; nominal alpha is therefore
    # intentionally UNKNOWN unless callers provide a multi-unit matrix.
    units = payload.get("units")
    if units is not None and payload.get("alpha_mode") != "batch":
        alpha = {
            "value": None,
            "status": "UNKNOWN",
            "reason": "SINGLE_PACKET_ALPHA_REQUIRES_BATCH",
            "units": 1,
        }
    elif units is not None:
        batch_error = _validate_batch_alpha_units(units, packet_identity, role)
        alpha = batch_error if batch_error is not None else simple_nominal_alpha(units)
    else:
        alpha = {
            "value": None,
            "status": "UNKNOWN",
            "reason": "INSUFFICIENT_UNITS",
            "detail": "single_packet_repeated_runs_alpha_undefined",
            "units": 1,
        }
    status = "VALID" if not invalid_runs else "PARTIAL"
    binding_errors = [
        item
        for item in invalid_runs
        if "packet_sha256" in str(item.get("error", ""))
        or "packet_fingerprints" in str(item.get("error", ""))
    ]
    return {
        "role": role,
        "kind": kind,
        "runs_requested": len(runs_raw),
        "runs_valid": len(valid_runs),
        "runs_invalid": len(invalid_runs),
        "minimum_runs": min_runs,
        "minimum_repeatability_runs": decision_min_runs,
        "invalid_runs": invalid_runs,
        "verdict_counts": counts,
        "majority_verdict": majority,
        "verdict": effective,
        "decision_rule": rule,
        "status": status,
        "packet_binding": {
            "status": "VALID" if not binding_errors else "INVALID",
            "expected_packet_identity": dict(packet_identity),
            "invalid_run_count": len(binding_errors),
        },
        "repeat_reliability": {
            **agreement,
            "eligible": len(labels) >= decision_min_runs and not invalid_runs and condition_sha256 is not None,
            "evaluator_condition_bound": condition_sha256 is not None,
            "coverage": len(valid_runs) / len(runs_raw),
            "missing_or_invalid_count": len(invalid_runs),
            "stability": (
                "INSUFFICIENT" if len(labels) < decision_min_runs
                else "STABLE" if not invalid_runs and len(set(labels)) == 1
                else "UNSTABLE"
            ),
            "nominal_alpha": alpha,
            "position_consistency": position_consistency,
        },
        "score_semantics": "UNCALIBRATED_DIAGNOSTIC_ONLY",
        "score": score_summary,
        "dimensions": dimension_summary,
        "median_recomputed_from_dimensions": median_recomputed,
        "median_total_delta": median_total_delta,
        "required_dimensions": required_dimensions,
        "dimension_specs": dimension_specs,
        "dimension_complete_runs": dimension_complete_runs,
        "dimension_coverage": dimension_complete_runs / len(runs_raw),
        "score_valid_runs": len(scores),
        "score_missing_or_invalid_runs": len(runs_raw) - len(scores),
        "score_coverage": len(scores) / len(runs_raw),
        "runs": valid_runs,
    }


def _overall(roles: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    if not roles:
        return {
            "verdict": "INDETERMINATE",
            "reason": "no_roles",
            "score_semantics": "UNCALIBRATED_DIAGNOSTIC_ONLY",
        }
    hard = [value for value in roles.values() if value["kind"] == "hard"]
    paper = [value for value in roles.values() if value["kind"] == "paper"]
    if any(value["verdict"] == "FAIL" for value in hard):
        verdict, reason = "FAIL", "hard_role_veto"
    elif any(value["verdict"] == "INDETERMINATE" for value in hard):
        verdict, reason = "INDETERMINATE", "hard_role_indeterminate"
    elif paper:
        paper_verdicts = {value["verdict"] for value in paper}
        if "INDETERMINATE" in paper_verdicts:
            verdict, reason = "INDETERMINATE", "paper_role_indeterminate"
        elif "REVISE" in paper_verdicts:
            verdict, reason = "REVISE", "paper_role_requires_revision"
        else:
            verdict, reason = "PASS", "all_roles_clear"
    else:
        verdict, reason = "PASS", "hard_roles_clear_no_paper_action"
    return {
        "verdict": verdict,
        "reason": reason,
        "score_semantics": "UNCALIBRATED_DIAGNOSTIC_ONLY",
        "award_prediction": "UNAVAILABLE_WITHOUT_HUMAN_CALIBRATION",
    }


def _validate_packet_identity(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a compact or role-specific packet identity.

    Returning an error instead of raising lets the caller emit a structured
    ``input_valid=false`` report.  A missing identity must never silently turn
    into a seemingly stable judge result.
    """

    if not isinstance(value, Mapping):
        return None, "packet_identity is missing or not an object"
    allowed = {
        "packet_sha256",
        "packet_fingerprints",
        "configuration_fingerprint",
        "condition_fingerprint",
    }
    extras = sorted(set(value) - allowed)
    if extras:
        return None, f"packet_identity has unsupported fields: {','.join(str(item) for item in extras)}"
    has_single = "packet_sha256" in value
    has_map = "packet_fingerprints" in value
    if has_single == has_map:
        return None, "packet_identity must contain exactly one of packet_sha256 or packet_fingerprints"
    if has_single:
        if not _valid_sha256(value.get("packet_sha256")):
            return None, "packet_identity.packet_sha256 must be lowercase SHA-256"
        identity = {"packet_sha256": value["packet_sha256"]}
    else:
        fingerprints = value.get("packet_fingerprints")
        if not isinstance(fingerprints, Mapping) or not fingerprints:
            return None, "packet_identity.packet_fingerprints must be a non-empty object"
        if any(not isinstance(role, str) or not role.strip() for role in fingerprints):
            return None, "packet_identity.packet_fingerprints has an invalid role"
        if any(not _valid_sha256(hash_value) for hash_value in fingerprints.values()):
            return None, "packet_identity.packet_fingerprints values must be lowercase SHA-256"
        normalized_fingerprints = {str(role).strip(): hash_value for role, hash_value in fingerprints.items()}
        if len(normalized_fingerprints) != len(fingerprints):
            return None, "packet_identity.packet_fingerprints has duplicate normalized roles"
        identity = {"packet_fingerprints": dict(sorted(normalized_fingerprints.items()))}
    # A configuration/condition fingerprint binds model, prompt, schema and
    # sampling settings.  It is optional for legacy harness receipts, but when
    # present it is checked on every run by _normalise_run.
    if "configuration_fingerprint" in value and "condition_fingerprint" in value:
        return None, "packet_identity must use one condition fingerprint field"
    for field in ("configuration_fingerprint", "condition_fingerprint"):
        if field in value:
            if not _valid_sha256(value.get(field)):
                return None, f"packet_identity.{field} must be lowercase SHA-256"
            identity[field] = value[field]
    return identity, None


def _invalid_report(payload: Mapping[str, Any], min_runs: int, errors: list[str]) -> dict[str, Any]:
    identity = payload.get("packet_identity")
    report = {
        "schema": OUTPUT_SCHEMA,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "input_sha256": canonical_hash(payload),
        "packet_identity": dict(identity) if isinstance(identity, Mapping) else None,
        "evaluator_identity": dict(payload.get("evaluator_identity")) if isinstance(payload.get("evaluator_identity"), Mapping) else None,
        "minimum_runs": min_runs,
        "minimum_repeatability_runs": max(MIN_REPEATABILITY_RUNS, min_runs),
        "repeatability_scope": (
            "SINGLE_SAMPLE_DIAGNOSTIC_ONLY"
            if min_runs < MIN_REPEATABILITY_RUNS
            else "REPEATED_SAMPLES"
        ),
        "required_roles": payload.get("required_roles", []),
        "missing_roles": payload.get("required_roles", []),
        "scope": "invalid",
        "input_valid": False,
        "errors": errors,
        "packet_binding": {"status": "INVALID", "errors": errors},
        "roles": {},
        "hard_gate_status": "INDETERMINATE",
        "paper_status_diagnostic": None,
        "veto_roles": [],
        "indeterminate_roles": [],
        "score_available": False,
        "workflow_gate_eligible": False,
        "overall": {
            "verdict": "INDETERMINATE",
            "reason": "invalid_packet_or_condition_identity",
            "score_semantics": "UNCALIBRATED_DIAGNOSTIC_ONLY",
            "award_prediction": "UNAVAILABLE_WITHOUT_HUMAN_CALIBRATION",
        },
        "claim_limit": "REPEATABILITY_ONLY_NO_HUMAN_TRUTH",
        "human_alignment": "UNAVAILABLE_WITHOUT_HUMAN_CALIBRATION",
        "award_prediction": "UNAVAILABLE_WITHOUT_HUMAN_CALIBRATION",
    }
    report["content_sha256"] = canonical_hash(
        {key: value for key, value in report.items() if key not in {"generated_at", "content_sha256"}}
    )
    return report


def _normalise_repeats(
    payload: Mapping[str, Any],
    roles_raw: Any,
    packet_identity: Mapping[str, Any],
) -> tuple[Any, list[str]]:
    """Convert optional repeat-centric input to the role-centric form.

    The production evaluator naturally emits one object per sample containing
    all three roles.  Supporting that shape here prevents accidental mixing of
    role streams while retaining a compact role-centric form for unit tests and
    small capability harnesses.
    """

    repeats = payload.get("repeats")
    if repeats is None:
        return roles_raw, []
    if not isinstance(repeats, list) or not repeats:
        return roles_raw, ["repeats must be a non-empty array"]
    role_meta: dict[str, dict[str, Any]] = {}
    if isinstance(roles_raw, Mapping):
        for role, value in roles_raw.items():
            if isinstance(value, Mapping):
                if "runs" in value:
                    return roles_raw, ["roles and repeats are mutually exclusive"]
                role_meta[str(role)] = {"kind": value.get("kind")}
    elif isinstance(roles_raw, list):
        for value in roles_raw:
            if isinstance(value, Mapping) and isinstance(value.get("role"), str):
                role_meta[value["role"]] = {"kind": value.get("kind")}
    streams: dict[str, dict[str, Any]] = {
        role: {**meta, "runs": []} for role, meta in role_meta.items()
    }
    errors: list[str] = []
    seen_samples: set[str] = set()
    for index, repeat in enumerate(repeats):
        if not isinstance(repeat, Mapping):
            errors.append(f"repeats[{index}] must be an object")
            continue
        sample_id = repeat.get("sample_id", repeat.get("run_id"))
        if not isinstance(sample_id, str) or not sample_id.strip():
            errors.append(f"repeats[{index}].sample_id must be a non-empty string")
            continue
        sample_id = sample_id.strip()
        if sample_id in seen_samples:
            errors.append(f"duplicate sample_id {sample_id}")
            continue
        seen_samples.add(sample_id)
        binding = repeat.get("packet_identity")
        if not isinstance(binding, Mapping):
            binding = {
                key: repeat[key]
                for key in ("packet_sha256", "packet_fingerprints", "configuration_fingerprint", "condition_fingerprint")
                if key in repeat
            }
        binding_ok, binding_error = _packet_identity_match(binding, packet_identity)
        for condition_field in ("configuration_fingerprint", "condition_fingerprint"):
            if packet_identity.get(condition_field) is not None:
                binding_ok = binding_ok and binding.get(condition_field) == packet_identity[condition_field]
                if binding.get(condition_field) is None:
                    binding_error = f"{condition_field} is missing"
                elif binding.get(condition_field) != packet_identity[condition_field]:
                    binding_error = f"{condition_field} does not match packet_identity"
        decisions = repeat.get("decisions")
        if not isinstance(decisions, Mapping):
            errors.append(f"repeat {sample_id}.decisions must be an object")
            for role in streams:
                streams[role]["runs"].append({"run_id": sample_id, "packet_sha256": None})
            continue
        # A role omitted from one repeat is represented as an invalid run.  It
        # therefore cannot disappear from the denominator or produce PASS.
        for role in set(streams) | {str(name) for name in decisions}:
            streams.setdefault(role, {"kind": None, "runs": []})
            decision = decisions.get(role)
            if isinstance(decision, Mapping):
                run = dict(decision)
                run["run_id"] = sample_id
                if not binding_ok:
                    # Deliberately retain the bad value so the run receipt says
                    # why it was rejected, rather than replacing it with the
                    # expected identity.
                    if "packet_sha256" in packet_identity:
                        run["packet_sha256"] = binding.get("packet_sha256")
                    else:
                        run["packet_fingerprints"] = binding.get("packet_fingerprints")
                else:
                    if "packet_sha256" in packet_identity:
                        run["packet_sha256"] = packet_identity["packet_sha256"]
                    else:
                        run["packet_fingerprints"] = packet_identity["packet_fingerprints"]
                    if "configuration_fingerprint" in packet_identity:
                        run["condition_sha256"] = packet_identity["configuration_fingerprint"]
                    elif "condition_fingerprint" in packet_identity:
                        run["condition_sha256"] = packet_identity["condition_fingerprint"]
                streams[role]["runs"].append(run)
            else:
                streams[role]["runs"].append({"run_id": sample_id, "packet_sha256": None})
        if not binding_ok and binding_error:
            errors.append(f"repeat {sample_id}: {binding_error}")
    return streams, errors


def aggregate_reliability(payload: Mapping[str, Any], *, min_runs: int = DEFAULT_MIN_RUNS) -> dict[str, Any]:
    """Validate and aggregate a reliability input into a versioned report."""

    if min_runs < 1:
        raise ReliabilityError("min_runs must be >= 1")
    if not isinstance(payload, Mapping):
        raise ReliabilityError("input must be a JSON object")
    _reject_nonfinite(payload)
    schema = payload.get("schema")
    if schema != INPUT_SCHEMA:
        raise ReliabilityError(f"input schema must be {INPUT_SCHEMA}")
    packet_identity, identity_error = _validate_packet_identity(payload.get("packet_identity"))
    if identity_error or packet_identity is None:
        return _invalid_report(payload, min_runs, [identity_error or "invalid packet identity"])
    roles_raw = payload.get("roles")
    repeat_errors: list[str] = []
    roles_raw, repeat_errors = _normalise_repeats(payload, roles_raw, packet_identity)
    if "roles and repeats are mutually exclusive" in repeat_errors:
        return _invalid_report(payload, min_runs, repeat_errors)
    if isinstance(roles_raw, list):
        converted: dict[str, Any] = {}
        for index, item in enumerate(roles_raw):
            if not isinstance(item, Mapping):
                raise ReliabilityError(f"roles[{index}] must be an object")
            role = _safe_text(item.get("role"), f"roles[{index}].role")
            if role in converted:
                raise ReliabilityError(f"duplicate role {role}")
            converted[role] = item
        roles_raw = converted
    if not isinstance(roles_raw, Mapping) or not roles_raw:
        raise ReliabilityError("roles must be a non-empty object or array")
    required_roles_raw = payload.get("required_roles", [])
    if not isinstance(required_roles_raw, list) or any(
        not isinstance(role, str) or not role.strip() for role in required_roles_raw
    ):
        raise ReliabilityError("required_roles must be a string array")
    required_roles = [str(role).strip() for role in required_roles_raw]
    if len(set(required_roles)) != len(required_roles):
        raise ReliabilityError("required_roles must not contain duplicates")
    if any(role not in {"math", "execution", "paper"} for role in required_roles):
        raise ReliabilityError("required_roles contains an unknown canonical role")
    role_results: dict[str, dict[str, Any]] = {}
    structural_errors = list(repeat_errors)
    seen_role_names: set[str] = set()
    for role_raw, role_payload in sorted(roles_raw.items(), key=lambda item: str(item[0])):
        role = _safe_text(role_raw, "role name")
        if role in seen_role_names:
            raise ReliabilityError(f"duplicate role after normalization: {role}")
        seen_role_names.add(role)
        if not isinstance(role_payload, Mapping):
            raise ReliabilityError(f"role {role!r} must be an object")
        role_results[role] = _role_result(
            role,
            role_payload,
            min_runs,
            packet_identity=packet_identity,
            condition_sha256=packet_identity.get("configuration_fingerprint")
            or packet_identity.get("condition_fingerprint"),
        )
    role_run_ids = {
        role: sorted(
            {run["run_id"] for run in value["runs"]}
            | {
                str(item["run_id"])
                for item in value["invalid_runs"]
                if isinstance(item.get("run_id"), str)
            }
        )
        for role, value in role_results.items()
    }
    aligned = len({tuple(run_ids) for run_ids in role_run_ids.values()}) <= 1
    if not aligned:
        structural_errors.append("cross-role run_id sets do not match")
    observed_conditions = {
        run.get("condition_sha256")
        for value in role_results.values()
        for run in value["runs"]
        if run.get("condition_sha256") is not None
    }
    expected_condition = packet_identity.get("configuration_fingerprint") or packet_identity.get("condition_fingerprint")
    if expected_condition is None and len(observed_conditions) > 1:
        structural_errors.append("runs contain inconsistent condition fingerprints")
    elif expected_condition is not None and any(condition != expected_condition for condition in observed_conditions):
        structural_errors.append("run condition fingerprint does not match packet identity")
    veto_roles = [role for role, value in role_results.items() if value["verdict"] == "FAIL" and value["kind"] == "hard"]
    indeterminate_roles = [
        role
        for role, value in role_results.items()
        if value["verdict"] == "INDETERMINATE"
    ]
    missing_roles = [role for role in required_roles if role not in role_results]
    indeterminate_roles.extend(role for role in missing_roles if role not in indeterminate_roles)
    hard_roles = [value for value in role_results.values() if value["kind"] == "hard"]
    required_hard_missing = [role for role in missing_roles if role in {"math", "execution"}]
    if veto_roles:
        hard_gate_status = "FAIL"
    elif not hard_roles or required_hard_missing or structural_errors:
        hard_gate_status = "INDETERMINATE"
    elif all(value["verdict"] == "PASS" for value in hard_roles):
        hard_gate_status = "PASS"
    else:
        hard_gate_status = "INDETERMINATE"
    paper_roles = [value for value in role_results.values() if value["kind"] == "paper"]
    if len(paper_roles) > 1:
        structural_errors.append("multiple paper role streams are not supported")
    paper_role = paper_roles[0] if paper_roles else None
    paper_status = paper_role["verdict"] if paper_role else None
    score_available = bool(
        paper_role
        and hard_gate_status == "PASS"
        and paper_role["verdict"] in {"PASS", "REVISE"}
        and paper_role["score_valid_runs"] == paper_role["runs_requested"]
        and paper_role["runs_invalid"] == 0
        and len(paper_roles) == 1
        and paper_role["dimension_complete_runs"] == paper_role["runs_requested"]
        and paper_role["score"]["n"] > 0
    )
    errors = structural_errors + [f"missing required role: {role}" for role in missing_roles] + [
        f"{role}: {item.get('error', 'invalid run')}"
        for role, value in role_results.items()
        for item in value["invalid_runs"]
    ]
    overall = _overall(role_results)
    if (errors or missing_roles) and overall["verdict"] != "FAIL":
        overall = {
            **overall,
            "verdict": "INDETERMINATE",
            "reason": "invalid_or_unbound_repeat",
        }
    binding_invalid_runs = sum(
        value["packet_binding"]["invalid_run_count"] for value in role_results.values()
    )
    output = {
        "schema": OUTPUT_SCHEMA,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "input_sha256": canonical_hash(payload),
        "packet_identity": dict(packet_identity),
        "evaluator_identity": dict(payload.get("evaluator_identity")) if isinstance(payload.get("evaluator_identity"), Mapping) else None,
        "minimum_runs": min_runs,
        "minimum_repeatability_runs": max(MIN_REPEATABILITY_RUNS, min_runs),
        "repeatability_scope": (
            "SINGLE_SAMPLE_DIAGNOSTIC_ONLY"
            if min_runs < MIN_REPEATABILITY_RUNS
            else "REPEATED_SAMPLES"
        ),
        "required_roles": required_roles,
        "missing_roles": missing_roles,
        "scope": "complete_required_roles" if required_roles else "partial_role_stream",
        "input_valid": not errors,
        "errors": errors,
        "packet_binding": {
            "status": "VALID" if not any(value["packet_binding"]["status"] == "INVALID" for value in role_results.values()) else "INVALID",
            "identity_form": "single" if "packet_sha256" in packet_identity else "role_map",
            "invalid_run_count": binding_invalid_runs,
        },
        "condition_binding": {
            "status": (
                "INCONSISTENT"
                if any("condition fingerprint" in error for error in structural_errors)
                else "BOUND"
                if expected_condition is not None
                else "UNBOUND"
            ),
            "fingerprint": expected_condition,
            "observed_distinct": sorted(str(value) for value in observed_conditions),
        },
        "cross_role_alignment": {
            "status": "VALID" if aligned else "INVALID",
            "run_ids": role_run_ids,
        },
        "roles": role_results,
        "hard_gate_status": hard_gate_status,
        "paper_status_diagnostic": paper_status,
        "veto_roles": veto_roles,
        "indeterminate_roles": indeterminate_roles,
        "score_available": score_available,
        "workflow_gate_eligible": False,
        "overall": overall,
        "claim_limit": "REPEATABILITY_ONLY_NO_HUMAN_TRUTH",
        "human_alignment": "UNAVAILABLE_WITHOUT_HUMAN_CALIBRATION",
        "award_prediction": "UNAVAILABLE_WITHOUT_HUMAN_CALIBRATION",
    }
    output["content_sha256"] = canonical_hash(
        {key: value for key, value in output.items() if key not in {"generated_at", "content_sha256"}}
    )
    return output


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(f"non-finite {token}")),
        )
    except (OSError, ValueError) as exc:
        raise ReliabilityError(f"invalid JSON input {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReliabilityError("input JSON root must be an object")
    return value


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="judge-reliability-input-v1 JSON")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--min-runs", type=int, default=DEFAULT_MIN_RUNS)
    args = parser.parse_args(argv)
    try:
        report = aggregate_reliability(_read_json(args.input), min_runs=args.min_runs)
        _atomic_write(args.output.resolve(), report)
        print(args.output.resolve())
        return 0
    except (OSError, ReliabilityError) as exc:
        print(f"judge reliability rejected input: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
