import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.capability_harness import evaluator_fingerprint
from scripts.selector_calibration import (
    MANIFEST_SCHEMA,
    REPORT_SCHEMA,
    SelectorError,
    aggregate_pairwise,
    evaluate_selector_calibration,
    main,
)


def _evaluator():
    return {
        "model": "selector-judge-v1",
        "backend": "test-backend",
        "prompt_sha256": "a" * 64,
        "schema_sha256": "b" * 64,
    }


def _candidate(pair_id, side, index, hard_gate_fp):
    return {
        "candidate_id": f"candidate-{pair_id}-{side}",
        "packet_sha256": f"{index:064x}",
        "pdf_sha256": f"{index + 100:064x}",
        "hard_gate_identity_fingerprint": hard_gate_fp,
        "quality_receipt_sha256": f"{index + 200:064x}",
        "hard_pass": True,
    }


def _pair(pair_id, split, family, winner, index, evaluator_fp, hard_gate_fp):
    margin = 0.8 if winner == "A" else -0.8 if winner == "B" else 0.0
    observations = []
    for orientation in ("AB", "BA"):
        for repeat in (1, 2):
            observations.append(
                {
                    "run_id": f"{pair_id}-{orientation}-{repeat}",
                    "orientation": orientation,
                    "status": "OK",
                    "winner": winner,
                    "margin": margin,
                    "evaluator_identity_fingerprint": evaluator_fp,
                    "candidate_packet_sha256": {
                        "A": f"{index:064x}",
                        "B": f"{index + 1:064x}",
                    },
                }
            )
    return {
        "pair_id": pair_id,
        "split": split,
        "family_id": family,
        "problem_identity": "problem-1",
        "quality_axes": ["correctness", "grounding"],
        "candidate_a": _candidate(pair_id, "A", index, hard_gate_fp),
        "candidate_b": _candidate(pair_id, "B", index + 1, hard_gate_fp),
        "observations": observations,
    }


def _manifest():
    evaluator = _evaluator()
    evaluator_fp = evaluator_fingerprint(evaluator)
    hard_gate_fp = "c" * 64
    pairs = [
        _pair("dev-a", "dev", "dev-family-a", "A", 1, evaluator_fp, hard_gate_fp),
        _pair("dev-b", "dev", "dev-family-b", "B", 3, evaluator_fp, hard_gate_fp),
        _pair("holdout-proxy-a", "holdout", "test-family-a", "A", 5, evaluator_fp, hard_gate_fp),
        _pair("holdout-proxy-b", "holdout", "test-family-b", "B", 7, evaluator_fp, hard_gate_fp),
        _pair("holdout-human-a", "holdout", "test-family-c", "A", 9, evaluator_fp, hard_gate_fp),
        _pair("holdout-human-b", "holdout", "test-family-d", "B", 11, evaluator_fp, hard_gate_fp),
    ]
    labels = []
    for pair in pairs:
        kind = "proxy" if "proxy" in pair["pair_id"] else "human"
        label = {
            "pair_id": pair["pair_id"],
            "split": pair["split"],
            "kind": kind,
            "winner": next(
                row["winner"] for row in pair["observations"] if row["status"] == "OK"
            ),
            "source": "deterministic-mutation" if kind == "proxy" else "blind-adjudication-panel",
            "labeled_at": "2026-08-05T00:00:00Z",
            "adjudication_method": "oracle" if kind == "proxy" else "independent-pairwise-adjudication",
            "must_not_miss": kind == "human",
        }
        if kind == "human":
            label.update({"blind": True, "selector_blinded": True})
        else:
            label["proxy_scope"] = "declared deterministic quality mutation"
        labels.append(label)
    return {
        "schema": MANIFEST_SCHEMA,
        "run_id": "selector-run-001",
        "dataset_version": "selector-dataset-v1",
        "frozen": True,
        "holdout_unsealed": False,
        "evaluator": evaluator,
        "evaluator_identity_fingerprint": evaluator_fp,
        "hard_gate_identity_fingerprint": hard_gate_fp,
        "thresholds": {
            "minimum_pairs_dev": 2,
            "minimum_pairs_holdout": 2,
            "minimum_repeats_per_orientation": 2,
            "accuracy_min": 0.2,
            "coverage_min": 0.5,
            "ab_ba_flip_rate_max": 1.0,
            "repeat_pairwise_flip_rate_max": 1.0,
            "format_failure_rate_max": 1.0,
            "indeterminate_rate_max": 1.0,
            "tie_band_candidates": [0.05, 0.2],
        },
        "pairs": pairs,
        "labels": labels,
    }


def test_selector_report_separates_proxy_and_human_readiness():
    report = evaluate_selector_calibration(_manifest())
    assert report["schema"] == REPORT_SCHEMA
    assert report["tie_band"] == 0.05
    assert report["comparison_ready_proxy"] is True
    assert report["comparison_ready_human"] is True
    assert report["advisory_only"] is True
    assert report["automatic_switch_performed"] is False
    assert report["operator_authorization_required"] is True
    assert report["holdout_hash"]


def test_aggregate_pairwise_uses_tie_band_and_retains_failures():
    observations = [
        {"orientation": "AB", "status": "OK", "winner": "A", "margin": 0.01},
        {"orientation": "AB", "status": "FORMAT_ERROR"},
        {"orientation": "BA", "status": "OK", "winner": "A", "margin": 0.02},
        {"orientation": "BA", "status": "INDETERMINATE"},
    ]
    result = aggregate_pairwise(observations, 0.05)
    assert result["decision"] == "TIE"
    assert result["format_failures"] == 1
    assert result["indeterminate_observations"] == 1


def test_family_leakage_and_identity_drift_fail_closed():
    manifest = _manifest()
    manifest["pairs"][2]["family_id"] = manifest["pairs"][0]["family_id"]
    with pytest.raises(SelectorError, match="family leakage"):
        evaluate_selector_calibration(manifest)

    manifest = _manifest()
    manifest["pairs"][0]["observations"][0]["evaluator_identity_fingerprint"] = "f" * 64
    with pytest.raises(SelectorError, match="identity drift"):
        evaluate_selector_calibration(manifest)


def test_selector_input_cannot_smuggle_labels_or_unbalanced_orientations():
    manifest = _manifest()
    manifest["pairs"][0]["observations"][0]["school"] = "hidden-label"
    with pytest.raises(SelectorError, match="forbidden label"):
        evaluate_selector_calibration(manifest)

    manifest = _manifest()
    manifest["pairs"][0]["observations"] = manifest["pairs"][0]["observations"][:-1]
    with pytest.raises(SelectorError, match="balanced AB/BA"):
        evaluate_selector_calibration(manifest)

    manifest = _manifest()
    manifest["pairs"][0]["observations"][0]["margin"] = -0.8
    with pytest.raises(SelectorError, match="contradicts its margin"):
        evaluate_selector_calibration(manifest)


def test_must_not_miss_reversal_prevents_human_readiness():
    manifest = _manifest()
    human = next(pair for pair in manifest["pairs"] if pair["pair_id"] == "holdout-human-a")
    for observation in human["observations"]:
        observation["margin"] *= -1
        observation["winner"] = "B"
    report = evaluate_selector_calibration(manifest)
    assert report["comparison_ready_human"] is False
    assert "holdout-human-a" in report["readiness"]["human"]["checks"][-1]["observed"]


def test_cli_require_ready_returns_three_without_mutation(tmp_path: Path):
    manifest = _manifest()
    manifest["thresholds"]["accuracy_min"] = 1.0
    manifest_path = tmp_path / "manifest.json"
    output_path = tmp_path / "report.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert main([str(manifest_path), "--json-output", str(output_path), "--require-ready"]) == 3
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["comparison_ready"] is False
