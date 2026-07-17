#!/usr/bin/env python3
"""Evaluate offline judge calibration against labeled real-paper ordering."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


MODEL_REOPEN = "REOPEN_REVISION_MODEL"
CALIBRATION_SCHEMA_VERSION = 3
PROMPT_DIR = Path(__file__).resolve().parents[1] / "evaluation" / "prompts"
RUNTIME_EVALUATOR_SCHEMA = "judge-role-v1"
RUNTIME_PACKET_MODALITY = "modeling-factory-judge-packet-v2"


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value.lower()
    )


def _manifest_contract(manifest: dict[str, Any]) -> dict[str, Any]:
    value = manifest.get("calibration_contract")
    return value if isinstance(value, dict) else {}


def _expected_models(manifest: dict[str, Any], contract: dict[str, Any]) -> set[str]:
    values = contract.get("models", contract.get("model", manifest.get("models", [])))
    if isinstance(values, str):
        return {values}
    return {str(value) for value in values if isinstance(value, (str, int, float))} if isinstance(values, list) else set()


def _identity_required(contract: dict[str, Any]) -> set[str]:
    values = contract.get("identity_required")
    if not isinstance(values, list):
        return set()
    return {str(value) for value in values if isinstance(value, str) and value}


def _result_models(data: dict[str, Any]) -> set[str]:
    identity = data.get("evaluator_identity")
    models = identity.get("models") if isinstance(identity, dict) else data.get("models")
    output = {
        str(value) for value in models
        if isinstance(value, (str, int, float)) and str(value)
    } if isinstance(models, list) else set()
    primary = data.get("model")
    adjudicator = data.get("adjudicator_model")
    if primary:
        output.add(str(primary))
    if adjudicator:
        output.add(str(adjudicator))
    return output


def _check_evaluator_identity(data: dict[str, Any], reasons: list[str]) -> None:
    """Ensure a composite result names every model that could own the decision."""
    primary = str(data.get("model") or "")
    adjudicator = str(data.get("adjudicator_model") or "") or None
    config = data.get("model_config") if isinstance(data.get("model_config"), dict) else {}
    identity = data.get("evaluator_identity") if isinstance(data.get("evaluator_identity"), dict) else {}
    expected_models = sorted({model for model in (primary, adjudicator) if model})
    configured_adjudicator = str(config.get("adjudicator_model") or "") or None
    if (
        config.get("model") != primary
        or configured_adjudicator != adjudicator
        or config.get("models") != expected_models
    ):
        reasons.append("model_config_mismatch")
    if not identity:
        reasons.append("evaluator_identity_missing")
        return
    if identity:
        expected_kind = "composite" if adjudicator else "primary_only"
        if (
            identity.get("schema") != "calibration-evaluator-v1"
            or identity.get("kind") != expected_kind
            or identity.get("primary_model") != primary
            or (identity.get("adjudicator_model") or None) != adjudicator
            or identity.get("models") != expected_models
            or data.get("models") != expected_models
        ):
            reasons.append("evaluator_identity_mismatch")
        decision_source = identity.get("decision_source")
        if data.get("adjudicated") is True and decision_source != "adjudicator":
            reasons.append("decision_source_mismatch")
        if data.get("adjudicated") is not True and decision_source == "adjudicator":
            reasons.append("decision_source_mismatch")


def _expected_schema(manifest: dict[str, Any], contract: dict[str, Any]) -> int:
    value = contract.get("schema_version", manifest.get("result_schema_version", CALIBRATION_SCHEMA_VERSION))
    try:
        return int(value)
    except (TypeError, ValueError):
        return CALIBRATION_SCHEMA_VERSION


def _runtime_score_validation(manifest: dict[str, Any]) -> tuple[bool, dict[str, bool], dict[str, Any]]:
    """Require an explicit construct-validity declaration for runtime scores.

    The standalone calibration harness judges anonymized paper text/PDFs.  The
    runtime evaluator instead consumes role-specific Modeling Factory packets,
    so harness reliability must not silently transfer across those modalities.
    """
    declared = manifest.get("runtime_score_validation")
    value = declared if isinstance(declared, dict) else {}
    checks = {
        "explicitly_validated": value.get("validated") is True,
        "evaluator_schema_match": value.get("evaluator_schema") == RUNTIME_EVALUATOR_SCHEMA,
        "packet_modality_match": value.get("packet_modality") == RUNTIME_PACKET_MODALITY,
    }
    return all(checks.values()), checks, {
        "validated": value.get("validated") is True,
        "evaluator_schema": value.get("evaluator_schema"),
        "packet_modality": value.get("packet_modality"),
    }


def _template_hash(kind: str) -> str | None:
    name = "calibration_pairwise.txt" if kind == "blind_pairwise" else "calibration_absolute.txt"
    return _sha256(PROMPT_DIR / name)


def _identity_check(
    data: dict[str, Any] | None,
    result_path: Path,
    *,
    item: dict[str, Any] | None,
    paper_item: bool,
    root: Path,
    manifest: dict[str, Any],
    contract: dict[str, Any],
) -> tuple[bool, list[str], str | None]:
    """Reject legacy or changed calibration artifacts before readiness checks."""
    if not data:
        return False, ["missing_result"], None
    reasons: list[str] = []
    required = _identity_required(contract)
    supported_identity = {
        "model", "prompt_sha256", "prompt_template_sha256", "input_fingerprint", "result_sha256"
    }
    for field in sorted(required - supported_identity):
        reasons.append(f"identity_requirement_unsupported:{field}")
    expected_schema = _expected_schema(manifest, contract)
    if data.get("schema_version") != expected_schema:
        reasons.append("schema_mismatch")
    expected_prompt_schema = contract.get("prompt_schema_version", 1)
    if data.get("prompt_schema_version") != expected_prompt_schema:
        reasons.append("prompt_schema_mismatch")
    expected_models = _expected_models(manifest, contract)
    result_models = _result_models(data)
    model = str(data.get("model") or "")
    if "model" in required and not model:
        reasons.append("model_missing")
    elif expected_models and result_models - expected_models:
        reasons.append("model_mismatch")
    if "model" in required and model:
        _check_evaluator_identity(data, reasons)
    prompt_hash = data.get("prompt_sha256")
    if "prompt_sha256" in required and not prompt_hash:
        reasons.append("prompt_hash_missing")
    template_hash = _template_hash(str(data.get("kind") or ""))
    recorded_template_hash = data.get("prompt_template_sha256")
    if "prompt_template_sha256" in required and not recorded_template_hash:
        reasons.append("prompt_template_hash_missing")
    elif recorded_template_hash and template_hash and recorded_template_hash != template_hash:
        reasons.append("prompt_template_stale")
    if "prompt_sha256" in required and prompt_hash:
        prompt_runs = data.get("prompt_run_sha256")
        if not isinstance(prompt_runs, list) or not prompt_runs or not all(
            _is_sha256(value) for value in prompt_runs
        ):
            reasons.append("prompt_hash_inputs_missing")
        else:
            if _canonical_hash(prompt_runs) != prompt_hash:
                reasons.append("prompt_hash_mismatch")
            requested = data.get("samples_requested")
            if isinstance(requested, int) and requested != len(prompt_runs):
                reasons.append("prompt_hash_count_mismatch")
    if "input_fingerprint" in required and not data.get("input_fingerprint"):
        reasons.append("input_fingerprint_missing")
    if "input_fingerprint" in required and data.get("input_fingerprint") and prompt_hash:
        if data.get("kind") == "blind_pairwise":
            components = {
                "source_paper_sha256": data.get("source_paper_sha256"),
                "prompt_sha256": prompt_hash,
            }
        else:
            components = {
                "paper_sha256": data.get("paper_sha256") or data.get("source_paper_sha256"),
                "prompt_sha256": prompt_hash,
            }
        if _canonical_hash(components) != data.get("input_fingerprint"):
            reasons.append("input_fingerprint_mismatch")
    if item and paper_item:
        paper_path = root / str(item.get("paper_path") or "")
        paper_hash = _sha256(paper_path)
        recorded = data.get("paper_sha256") or data.get("source_paper_sha256")
        if paper_hash and recorded != paper_hash:
            reasons.append("paper_changed")
        if data.get("paper_id") and str(data.get("paper_id")) != str(item.get("id")):
            reasons.append("paper_id_mismatch")
    result_hash = _sha256(result_path)
    expected_result_hash = item.get("result_sha256") if item else None
    if "result_sha256" in required and not expected_result_hash:
        reasons.append("result_hash_unpinned")
    elif expected_result_hash and result_hash and expected_result_hash != result_hash:
        reasons.append("result_hash_mismatch")
    # The file hash is kept outside the result to avoid a self-referential hash.
    return not reasons, reasons, result_hash


def _load_result(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _score(data: dict[str, Any]) -> float | None:
    writing = data.get("writing") if isinstance(data.get("writing"), dict) else {}
    llm = data.get("llm_score") if isinstance(data.get("llm_score"), dict) else {}
    for value in (
        writing.get("median_score"),
        llm.get("median_recomputed"),
        data.get("median_recomputed"),
        llm.get("median_total"),
        data.get("median_total"),
    ):
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _correctness_score(data: dict[str, Any]) -> float | None:
    section = data.get("correctness") if isinstance(data.get("correctness"), dict) else {}
    value = section.get("median_score")
    return float(value) if isinstance(value, (int, float)) else None


def _writing_dimensions(data: dict[str, Any]) -> dict[str, float]:
    writing = data.get("writing") if isinstance(data.get("writing"), dict) else {}
    dimensions = writing.get("dimensions") if isinstance(writing.get("dimensions"), dict) else {}
    return {
        str(name): float(value)
        for name, value in dimensions.items()
        if isinstance(value, (int, float))
    }


def _pair_result(path: Path) -> dict[str, Any] | None:
    data = _load_result(path)
    if not data or data.get("kind") != "blind_pairwise":
        return None
    return data


def _verdicts(data: dict[str, Any]) -> list[str | None]:
    values = data.get("verdicts")
    if not isinstance(values, list):
        llm = data.get("llm_score")
        if isinstance(llm, dict):
            values = llm.get("verdicts") or llm.get("verdict_distribution")
    if isinstance(values, dict):
        expanded: list[str] = []
        for verdict, count in values.items():
            if isinstance(count, int) and count > 0:
                expanded.extend([str(verdict)] * count)
        return expanded
    return [value if isinstance(value, str) else None for value in values] if isinstance(values, list) else []


def _fatal_detected(data: dict[str, Any]) -> bool:
    correctness = data.get("correctness") if isinstance(data.get("correctness"), dict) else {}
    fatal_rate = correctness.get("fatal_flaw_rate")
    if isinstance(fatal_rate, (int, float)):
        return fatal_rate >= 0.5
    verdicts = [value for value in _verdicts(data) if value]
    if not verdicts:
        return False
    return sum(value == MODEL_REOPEN for value in verdicts) * 2 >= len(verdicts)


def evaluate_calibration(
    manifest: dict[str, Any], root: Path, results_dir_override: str | None = None
) -> dict[str, Any]:
    root = root.resolve()
    paper_rows: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    malformed = 0
    total_runs = 0
    fatal_expected = 0
    fatal_detected = 0
    coverage: dict[str, dict[str, int]] = {}
    models: set[str] = set()
    contract = _manifest_contract(manifest)
    contract_present = bool(contract)
    stale_results: list[dict[str, Any]] = []
    result_hashes: dict[str, str] = {}
    paper_hashes: dict[str, str] = {}
    identity_models: set[str] = set()

    for item in manifest.get("papers", []):
        paper_id = str(item["id"])
        problem_id = str(item.get("problem_id") or "UNKNOWN")
        result_ref = item.get("calibration_result_path") or item.get("result_path")
        if results_dir_override:
            result_ref = f"{results_dir_override.rstrip('/')}/paper_{paper_id}.json"
        if not result_ref and manifest.get("calibration_results_dir"):
            result_ref = f"{str(manifest['calibration_results_dir']).rstrip('/')}/paper_{paper_id}.json"
        result_ref = result_ref or ""
        result_path = root / str(result_ref)
        data = _load_result(result_path)
        identity_ok, identity_reasons, result_hash = _identity_check(
            data, result_path, item=item, paper_item=True,
            root=root, manifest=manifest, contract=contract
        )
        if result_hash:
            result_hashes[f"paper:{paper_id}"] = result_hash
        if data:
            identity_models.update(_result_models(data))
        if item.get("paper_path"):
            paper_hash = _sha256(root / str(item["paper_path"]))
            if paper_hash:
                paper_hashes[paper_id] = paper_hash
        if not identity_ok:
            stale_results.append({"id": paper_id, "kind": "paper", "reasons": identity_reasons})
        if data:
            models.update(_result_models(data))
        score = _score(data) if data else None
        available = data is not None and score is not None
        row = {
            "id": paper_id,
            "problem_id": problem_id,
            "award_tier": item.get("award_tier"),
            "category": item.get("category"),
            "paper_path": item.get("paper_path"),
            "result_path": item.get("result_path"),
            "calibration_result_path": item.get("calibration_result_path"),
            "status": "AVAILABLE" if available else "MISSING",
            "score": score,
            "correctness_score": _correctness_score(data) if data else None,
            "writing_dimensions": _writing_dimensions(data) if data else {},
            "fatal_flaw_detected": _fatal_detected(data) if data else False,
            "identity_ok": identity_ok,
            "identity_reasons": identity_reasons,
            "result_sha256": result_hash,
        }
        paper_rows.append(row)
        by_id[paper_id] = row
        stats = coverage.setdefault(problem_id, {"available": 0, "total": 0})
        stats["total"] += 1
        if available:
            stats["available"] += 1
        else:
            missing.append(paper_id)

        if data:
            n = data.get("n")
            n_scored = data.get("n_scored")
            if not isinstance(n, int):
                n = data.get("samples_requested")
            if not isinstance(n_scored, int):
                n_scored = data.get("samples_scored")
            if not isinstance(n, int):
                llm = data.get("llm_score")
                n = llm.get("n") if isinstance(llm, dict) else None
            if not isinstance(n_scored, int):
                llm = data.get("llm_score")
                n_scored = llm.get("n_scored") if isinstance(llm, dict) else None
            if isinstance(n, int) and n >= 0:
                total_runs += n
                malformed += max(0, n - (n_scored if isinstance(n_scored, int) else 0))

        if item.get("expected_fatal_flaw") is True and data:
            fatal_expected += 1
            if row["fatal_flaw_detected"]:
                fatal_detected += 1

    pair_rows: list[dict[str, Any]] = []
    correct_points = 0.0
    concordant = discordant = ties = 0
    direct_evaluated = 0
    direct_complete = 0
    diagnostic_pairs = 0
    pair_total_runs = 0
    pair_malformed = 0
    axis_correct = {"overall": 0.0, "correctness": 0.0, "writing": 0.0}
    axis_evaluated = {"overall": 0, "correctness": 0, "writing": 0}
    for pair in manifest.get("pairs", []):
        higher_id = str(pair["higher"])
        lower_id = str(pair["lower"])
        readiness_eligible = pair.get("readiness_eligible") is not False
        higher = by_id.get(higher_id)
        lower = by_id.get(lower_id)
        direct_path_ref = pair.get("result_path")
        if results_dir_override:
            pair_id = str(pair.get("id") or f"{higher_id}__vs__{lower_id}")
            direct_path_ref = f"{results_dir_override.rstrip('/')}/pair_{pair_id}.json"
        direct = _pair_result(root / str(direct_path_ref)) if direct_path_ref else None
        direct_path = root / str(direct_path_ref) if direct_path_ref else Path("")
        direct_identity_ok, direct_identity_reasons, direct_result_hash = _identity_check(
            direct, direct_path, item=pair, paper_item=False,
            root=root, manifest=manifest, contract=contract
        )
        if direct and direct_identity_ok:
            recorded_sources = direct.get("source_paper_sha256")
            for paper_id in (higher_id, lower_id):
                expected_source = paper_hashes.get(paper_id)
                if not isinstance(recorded_sources, dict) or recorded_sources.get(paper_id) != expected_source:
                    direct_identity_ok = False
                    direct_identity_reasons.append(f"source_paper_mismatch:{paper_id}")
        if direct_result_hash:
            result_hashes[f"pair:{pair.get('id') or higher_id + '__vs__' + lower_id}"] = direct_result_hash
        if not direct_identity_ok and direct is not None:
            stale_results.append({
                "id": str(pair.get("id") or f"{higher_id}__vs__{lower_id}"),
                "kind": "pair",
                "reasons": direct_identity_reasons,
            })
        if direct:
            direct_models = _result_models(direct)
            models.update(direct_models)
            identity_models.update(direct_models)
        # Keep stale direct artifacts visible for diagnostics, but freshness is
        # a separate readiness check and therefore cannot make the report ready.
        if direct and direct.get("overall_winner") in {higher_id, lower_id, "TIE"}:
            requested = direct.get("samples_requested")
            scored = direct.get("samples_scored")
            malformed_count = direct.get("malformed")
            if isinstance(requested, int) and requested >= 0:
                pair_total_runs += requested
            if isinstance(malformed_count, int) and malformed_count >= 0:
                pair_malformed += malformed_count
            adjudicator_recovered = bool(direct.get("adjudicated")) and any(
                isinstance(run, dict)
                and run.get("role") == "adjudicator"
                and run.get("status") != "MALFORMED"
                for run in direct.get("runs", [])
            )
            complete = adjudicator_recovered or (
                isinstance(requested, int)
                and requested > 0
                and isinstance(scored, int)
                and scored == requested
                and malformed_count == 0
            )
            expected_overall = str(pair.get("expected_overall_winner") or higher_id)
            winner = direct["overall_winner"]
            if winner == expected_overall:
                credit, status = 1.0, "CORRECT"
                if readiness_eligible:
                    concordant += 1
            elif winner == "TIE":
                credit, status = 0.5, "TIE"
                if readiness_eligible:
                    ties += 1
            else:
                credit, status = 0.0, "REVERSED"
                if readiness_eligible:
                    discordant += 1
            if readiness_eligible:
                direct_evaluated += 1
                direct_complete += int(complete)
                correct_points += credit
            else:
                diagnostic_pairs += 1
            for axis, result_key in (
                ("overall", "overall_winner"),
                ("correctness", "correctness_winner"),
                ("writing", "writing_winner"),
            ):
                expected = pair.get(f"expected_{axis}_winner")
                actual = direct.get(result_key)
                if readiness_eligible and expected in {higher_id, lower_id, "TIE"} and actual in {higher_id, lower_id, "TIE"}:
                    axis_evaluated[axis] += 1
                    axis_correct[axis] += 1.0 if actual == expected else 0.0
            pair_rows.append(
                {
                    "higher": higher_id,
                    "lower": lower_id,
                    "status": status,
                    "credit": credit,
                    "source": "BLIND_PAIRWISE",
                    "winner": winner,
                    "correctness_winner": direct.get("correctness_winner"),
                    "writing_winner": direct.get("writing_winner"),
                    "samples_scored": direct.get("samples_scored"),
                    "samples_requested": direct.get("samples_requested"),
                    "complete": complete,
                    "label_type": pair.get("label_type") or "AWARD_WEAK_PRIOR",
                    "expected_overall_winner": expected_overall,
                    "readiness_eligible": readiness_eligible,
                    "identity_ok": direct_identity_ok,
                    "identity_reasons": direct_identity_reasons,
                    "evaluator_kind": (
                        direct.get("evaluator_identity", {}).get("kind")
                        if isinstance(direct.get("evaluator_identity"), dict) else None
                    ),
                    "decision_source": (
                        direct.get("evaluator_identity", {}).get("decision_source")
                        if isinstance(direct.get("evaluator_identity"), dict) else (
                            "adjudicator" if direct.get("adjudicated") else "primary_or_legacy"
                        )
                    ),
                    "models": sorted(_result_models(direct)),
                }
            )
            continue
        if not higher or not lower or higher["score"] is None or lower["score"] is None:
            pair_rows.append(
                {"higher": higher_id, "lower": lower_id, "status": "MISSING", "credit": None, "source": "NONE"}
            )
            continue
        delta = float(higher["score"]) - float(lower["score"])
        if abs(delta) <= 1e-9:
            credit = 0.5
            status = "TIE"
            ties += 1
        elif delta > 0:
            credit = 1.0
            status = "CORRECT"
            concordant += 1
        else:
            credit = 0.0
            status = "REVERSED"
            discordant += 1
        if readiness_eligible:
            correct_points += credit
        pair_rows.append(
            {
                "higher": higher_id,
                "lower": lower_id,
                "status": status,
                "credit": credit,
                "source": "ABSOLUTE_SCORE_FALLBACK",
                "higher_score": higher["score"],
                "lower_score": lower["score"],
                "readiness_eligible": readiness_eligible,
            }
        )

    evaluated_pairs = sum(
        row["credit"] is not None and row.get("readiness_eligible", True) for row in pair_rows
    )
    tau_denominator = concordant + discordant + ties
    coverage_out = {
        problem: {
            "available": values["available"],
            "total": values["total"],
            "coverage": values["available"] / values["total"] if values["total"] else None,
        }
        for problem, values in sorted(coverage.items())
    }
    malformed += pair_malformed
    total_runs += pair_total_runs
    pair_accuracy = correct_points / evaluated_pairs if evaluated_pairs else None
    malformed_rate = malformed / total_runs if total_runs else None
    fatal_rate = fatal_detected / fatal_expected if fatal_expected else None
    fatal_true_positive = fatal_detected
    fatal_false_negative = max(0, fatal_expected - fatal_true_positive)
    fatal_expected_clean = sum(
        1
        for item in manifest.get("papers", [])
        if item.get("expected_fatal_flaw") is False
    )
    fatal_false_positive = sum(
        int(row.get("fatal_flaw_detected") is True)
        for row, item in zip(paper_rows, manifest.get("papers", []))
        if item.get("expected_fatal_flaw") is False
    )
    fatal_true_negative = max(0, fatal_expected_clean - fatal_false_positive)
    fatal_sensitivity = (
        fatal_true_positive / (fatal_true_positive + fatal_false_negative)
        if fatal_true_positive + fatal_false_negative else None
    )
    fatal_specificity = (
        fatal_true_negative / (fatal_true_negative + fatal_false_positive)
        if fatal_true_negative + fatal_false_positive else None
    )
    fatal_precision = (
        fatal_true_positive / (fatal_true_positive + fatal_false_positive)
        if fatal_true_positive + fatal_false_positive else None
    )
    fatal_fpr = (
        fatal_false_positive / (fatal_false_positive + fatal_true_negative)
        if fatal_false_positive + fatal_true_negative else None
    )
    policy = manifest.get("readiness_policy") if isinstance(manifest.get("readiness_policy"), dict) else {}
    required_pair_accuracy = float(policy.get("min_pairwise_accuracy", 0.75))
    required_direct_coverage = float(policy.get("min_direct_pair_coverage", 1.0))
    max_malformed_rate = float(policy.get("max_malformed_rate", 0.1))
    min_fatal_rate = float(policy.get("min_fatal_flaw_detection_rate", 1.0))
    min_fatal_sensitivity = float(policy.get("min_fatal_sensitivity", min_fatal_rate))
    min_fatal_specificity = float(policy.get("min_fatal_specificity", 0.8))
    min_fatal_precision = float(policy.get("min_fatal_precision", 0.8))
    max_fatal_fpr = float(policy.get("max_fatal_false_positive_rate", 0.2))
    min_correctness_accuracy = float(policy.get("min_correctness_accuracy", required_pair_accuracy))
    min_writing_accuracy = float(policy.get("min_writing_accuracy", required_pair_accuracy))
    expected_models = _expected_models(manifest, contract)
    models_match = contract_present and bool(identity_models) and (
        not expected_models or identity_models <= expected_models
    ) and not any(
        any(
            reason in {
                "model_mismatch", "model_config_mismatch", "evaluator_identity_missing",
                "evaluator_identity_mismatch", "decision_source_mismatch",
            }
            for reason in entry.get("reasons", [])
        )
        for entry in stale_results
    )
    schema_match = contract_present and not any(
        "schema_mismatch" in entry.get("reasons", []) for entry in stale_results
    )
    freshness_ok = contract_present and not stale_results
    eligible_pair_count = sum(pair.get("readiness_eligible") is not False for pair in manifest.get("pairs", []))
    direct_coverage = direct_complete / eligible_pair_count if eligible_pair_count else None
    split_axis_coverage = (
        sum(
            row.get("correctness_score") is not None
            and row.get("score") is not None
            and len(row.get("writing_dimensions") or {}) >= 6
            for row in paper_rows
        ) / len(paper_rows)
        if paper_rows else None
    )
    readiness_checks = {
        "all_papers_scored": not missing,
        "split_axis_coverage": split_axis_coverage == 1.0,
        "direct_pair_coverage": direct_coverage is not None and direct_coverage >= required_direct_coverage,
        "pairwise_accuracy": pair_accuracy is not None and pair_accuracy >= required_pair_accuracy,
        "malformed_output_rate": malformed_rate is not None and malformed_rate <= max_malformed_rate,
        "fatal_flaw_detection": fatal_rate is not None and fatal_rate >= min_fatal_rate,
        "fatal_sensitivity": fatal_sensitivity is not None and fatal_sensitivity >= min_fatal_sensitivity,
        "fatal_specificity": fatal_specificity is not None and fatal_specificity >= min_fatal_specificity,
        "fatal_precision": fatal_precision is not None and fatal_precision >= min_fatal_precision,
        "fatal_false_positive_rate": fatal_fpr is not None and fatal_fpr <= max_fatal_fpr,
        "calibration_freshness": freshness_ok,
        "model_config_match": models_match,
        "schema_match": schema_match,
    }
    axis_accuracy = {
        axis: axis_correct[axis] / axis_evaluated[axis] if axis_evaluated[axis] else None
        for axis in axis_evaluated
    }
    axis_checks = {
        "correctness_pairwise_accuracy": (
            axis_accuracy["correctness"] is not None
            and axis_accuracy["correctness"] >= min_correctness_accuracy
        ),
        "writing_pairwise_accuracy": (
            axis_accuracy["writing"] is not None
            and axis_accuracy["writing"] >= min_writing_accuracy
        ),
    }
    proxy_ready = manifest.get("readiness_kind") == "proxy" and all(readiness_checks.values())
    axis_ready = proxy_ready and all(axis_checks.values())
    human_ready = manifest.get("readiness_kind") == "human" and all(readiness_checks.values())
    runtime_contract_match, runtime_checks, runtime_declared = _runtime_score_validation(manifest)
    runtime_proxy_ready = runtime_contract_match and proxy_ready
    runtime_human_ready = runtime_contract_match and human_ready
    pair_evaluator_counts = {
        "primary_only": sum(row.get("evaluator_kind") == "primary_only" for row in pair_rows),
        "composite": sum(row.get("evaluator_kind") == "composite" for row in pair_rows),
        "adjudicator_decisions": sum(row.get("decision_source") == "adjudicator" for row in pair_rows),
        "legacy_or_unidentified": sum(
            row.get("evaluator_kind") not in {"primary_only", "composite"} for row in pair_rows
        ),
    }
    return {
        "models": sorted(models),
        "calibration_contract": {
            "present": contract_present,
            "schema_version": _expected_schema(manifest, contract),
            "models": sorted(expected_models),
            "identity_required": sorted(_identity_required(contract)),
            "prompt_templates": {
                "pairwise": _template_hash("blind_pairwise"),
                "absolute": _template_hash("split_absolute"),
            },
            "manifest_sha256": _canonical_hash(contract) if contract_present else None,
        },
        "calibration_identity": {
            "fresh": freshness_ok,
            "stale_results": stale_results,
            "result_sha256": result_hashes,
            "paper_sha256": paper_hashes,
            "source_fingerprint": _canonical_hash({
                "contract": contract,
                "result_sha256": result_hashes,
                "paper_sha256": paper_hashes,
            }),
        },
        "papers": paper_rows,
        "missing_results": sorted(missing),
        "pairs": pair_rows,
        "pairwise": {
            "evaluated": evaluated_pairs,
            "total": len(pair_rows),
            "readiness_total": eligible_pair_count,
            "diagnostic_pairs": diagnostic_pairs,
            "correct_points": correct_points,
            "accuracy": pair_accuracy,
            "direct_evaluated": direct_evaluated,
            "direct_complete": direct_complete,
            "direct_coverage": direct_coverage,
            "axis_accuracy": axis_accuracy,
            "axis_evaluated": axis_evaluated,
        },
        "ordering": {
            "concordant": concordant,
            "discordant": discordant,
            "ties": ties,
            "kendall_style_tau": (
                (concordant - discordant) / tau_denominator if tau_denominator else None
            ),
        },
        "malformed_outputs": {
            "malformed": malformed,
            "total_runs": total_runs,
            "rate": malformed_rate,
        },
        "fatal_flaw_detection": {
            "detected": fatal_detected,
            "expected": fatal_expected,
            "rate": fatal_rate,
            "true_positive": fatal_true_positive,
            "false_negative": fatal_false_negative,
            "true_negative": fatal_true_negative,
            "false_positive": fatal_false_positive,
            "sensitivity": fatal_sensitivity,
            "specificity": fatal_specificity,
            "precision": fatal_precision,
            "false_positive_rate": fatal_fpr,
        },
        "coverage_by_problem": coverage_out,
        "split_axis_coverage": split_axis_coverage,
        "score_reliability": {
            "ready": human_ready,
            "checks": readiness_checks,
            "policy": {
                "min_pairwise_accuracy": required_pair_accuracy,
                "min_direct_pair_coverage": required_direct_coverage,
                "max_malformed_rate": max_malformed_rate,
                "min_fatal_flaw_detection_rate": min_fatal_rate,
                "min_fatal_sensitivity": min_fatal_sensitivity,
                "min_fatal_specificity": min_fatal_specificity,
                "min_fatal_precision": min_fatal_precision,
                "max_fatal_false_positive_rate": max_fatal_fpr,
                "min_correctness_accuracy": min_correctness_accuracy,
                "min_writing_accuracy": min_writing_accuracy,
            },
            "meaning": "Absolute scores and award prediction require independent human calibration",
        },
        "proxy_reliability": {
            "ready": proxy_ready,
            "checks": readiness_checks,
            "axis_ready": axis_ready,
            "axis_checks": axis_checks,
            "scope": ["overall_pairwise_ranking", "fatal_defect_detection"],
            "evaluator_composition": pair_evaluator_counts,
            "meaning": "Safe only for bounded overall A/B ranking against deterministic perturbations",
        },
        "runtime_score_reliability": {
            "ready": runtime_proxy_ready or runtime_human_ready,
            "proxy_ready": runtime_proxy_ready,
            "human_ready": runtime_human_ready,
            "checks": runtime_checks,
            "declared_contract": runtime_declared,
            "required_contract": {
                "evaluator_schema": RUNTIME_EVALUATOR_SCHEMA,
                "packet_modality": RUNTIME_PACKET_MODALITY,
            },
            "meaning": (
                "Runtime comparison requires calibration of the exact judge-role-v1 "
                "evaluator over Modeling Factory judge packet v2 inputs"
            ),
        },
        "axis_reliability": {
            "ready": axis_ready,
            "checks": axis_checks,
            "meaning": "Correctness and writing sub-axis labels require separate validation",
        },
        "human_calibration": {
            "ready": human_ready,
            "reason": None if human_ready else "no independent human ground truth",
        },
        "award_prediction_ready": human_ready,
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


def write_reports(report: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# Evaluation Calibration Report",
        "",
        f"Models: {', '.join(report.get('models', [])) or 'N/A'}",
        "",
        "## Paper Coverage",
        "",
        "| Paper | Problem | Award tier | Status | Correctness | Writing |",
        "|---|---|---|---|---:|---:|",
    ]
    for row in report["papers"]:
        lines.append(
            f"| {row['id']} | {row.get('problem_id', '')} | {row.get('award_tier') or ''} | "
            f"{row['status']} | {_fmt(row.get('correctness_score'))} | {_fmt(row['score'])} |"
        )
    pairwise = report["pairwise"]
    ordering = report["ordering"]
    malformed = report["malformed_outputs"]
    fatal = report["fatal_flaw_detection"]
    reliability = report.get("score_reliability", {})
    proxy = report.get("proxy_reliability", {})
    composition = proxy.get("evaluator_composition", {})
    runtime = report.get("runtime_score_reliability", {})
    axis = report.get("axis_reliability", {})
    lines.extend(
        [
            "",
            "## Metrics",
            "",
            f"- Pairwise award-order accuracy: {_fmt(pairwise['accuracy'])} "
            f"({pairwise['evaluated']}/{pairwise.get('readiness_total', pairwise['total'])} readiness pairs; "
            f"{pairwise.get('diagnostic_pairs', 0)} diagnostic pairs excluded)",
            f"- Kendall-style ordering: {_fmt(ordering['kendall_style_tau'])}",
            f"- Malformed-output rate: {_fmt(malformed['rate'])} "
            f"({malformed['malformed']}/{malformed['total_runs']})",
            f"- Fatal-flaw detection rate: {_fmt(fatal['rate'])} "
            f"({fatal['detected']}/{fatal['expected']})",
            f"- Fatal sensitivity: {_fmt(fatal.get('sensitivity'))}; specificity: {_fmt(fatal.get('specificity'))}; "
            f"precision: {_fmt(fatal.get('precision'))}; false-positive rate: {_fmt(fatal.get('false_positive_rate'))}",
            f"- Fatal confusion counts: TP={fatal.get('true_positive', 0)}, FN={fatal.get('false_negative', 0)}, "
            f"TN={fatal.get('true_negative', 0)}, FP={fatal.get('false_positive', 0)}",
            f"- Direct blind-pair coverage: {_fmt(pairwise.get('direct_coverage'))}",
            f"- Split correctness/writing coverage: {_fmt(report.get('split_axis_coverage'))}",
            f"- Step 13 score reliability: {'READY' if reliability.get('ready') else 'NOT READY'}",
            f"- Proxy A/B reliability: {'READY' if proxy.get('ready') else 'NOT READY'}",
            f"- Pair evaluator composition: primary-only={composition.get('primary_only', 0)}, "
            f"composite={composition.get('composite', 0)}, "
            f"adjudicator-decided={composition.get('adjudicator_decisions', 0)}, "
            f"legacy/unidentified={composition.get('legacy_or_unidentified', 0)}",
            f"- Runtime score reliability: {'READY' if runtime.get('ready') else 'NOT READY'}",
            f"- Correctness/writing axis reliability: {'READY' if axis.get('ready') else 'NOT READY'}",
            f"- Human calibration: {'READY' if report.get('human_calibration', {}).get('ready') else 'NOT READY'}",
            f"- Award prediction: {'READY' if report.get('award_prediction_ready') else 'NOT READY'}",
            f"- Calibration freshness: {'FRESH' if report.get('calibration_identity', {}).get('fresh') else 'STALE'}",
            "",
            "## Blind Pairwise Results",
            "",
            "| Expected higher | Expected lower | Result | Source | Evaluator | Decision owner | Complete |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for row in report.get("pairs", []):
        lines.append(
            f"| {row['higher']} | {row['lower']} | "
            f"{row['status'] if row.get('readiness_eligible', True) else 'DIAGNOSTIC_' + row['status']} | "
            f"{row.get('source', '')} | {row.get('evaluator_kind') or 'N/A'} | "
            f"{row.get('decision_source') or 'N/A'} | {row.get('complete', 'N/A')} |"
        )
    lines.extend(["", "## Reliability Checks", ""])
    for name, ok in (proxy.get("checks") or reliability.get("checks", {})).items():
        lines.append(f"- {'PASS' if ok else 'FAIL'}: {name}")
    for name, ok in runtime.get("checks", {}).items():
        lines.append(f"- {'PASS' if ok else 'FAIL'}: runtime_{name}")
    for name, ok in axis.get("checks", {}).items():
        lines.append(f"- {'PASS' if ok else 'FAIL'}: {name}")
    lines.extend(["", "## Missing Results", ""])
    if report["missing_results"]:
        lines.extend(f"- {paper_id}: MISSING" for paper_id in report["missing_results"])
    else:
        lines.append("- None")
    lines.extend(["", "## Calibration Identity", ""])
    identity = report.get("calibration_identity", {})
    lines.append(f"- Freshness: {'FRESH' if identity.get('fresh') else 'STALE'}")
    stale = identity.get("stale_results") or []
    if stale:
        for entry in stale:
            lines.append(f"- {entry.get('kind', 'result')} {entry.get('id', '')}: STALE ({', '.join(entry.get('reasons', []))})")
    else:
        lines.append("- No stale result artifacts")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--existing-results", action="store_true")
    parser.add_argument("--json-output")
    parser.add_argument("--markdown-output")
    parser.add_argument("--require-ready", action="store_true", help="Exit 1 unless human score reliability is ready.")
    parser.add_argument("--require-proxy-ready", action="store_true", help="Exit 1 unless proxy A/B reliability is ready.")
    parser.add_argument("--results-dir", help="Override result directory referenced by the manifest.")
    args = parser.parse_args()
    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = (manifest_path.parent / str(manifest.get("path_root") or ".")).resolve()
    report = evaluate_calibration(manifest, root, args.results_dir)
    json_path = Path(args.json_output) if args.json_output else manifest_path.parent / "calibration_report.json"
    md_path = Path(args.markdown_output) if args.markdown_output else manifest_path.parent / "calibration_report.md"
    write_reports(report, json_path, md_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.require_ready and not report["score_reliability"]["ready"]:
        return 1
    if args.require_proxy_ready and not report["proxy_reliability"]["ready"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
