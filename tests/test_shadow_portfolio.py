import json
from pathlib import Path

import pytest

from scripts.capability_harness import evaluator_fingerprint, file_sha256
from scripts.hard_gate_calibration import REPORT_SCHEMA as R0A_REPORT_SCHEMA
from scripts.selector_calibration import REPORT_SCHEMA as R0B_REPORT_SCHEMA
from scripts.shadow_portfolio import (
    MANIFEST_SCHEMA,
    REPORT_SCHEMA,
    PortfolioError,
    evaluate_shadow_portfolio,
    main,
)


def _evaluator(model="selector-judge-v1"):
    return {
        "model": model,
        "backend": "test-backend",
        "prompt_sha256": "a" * 64,
        "schema_sha256": "b" * 64,
    }


def _candidate(candidate_id, problem, family, index, evaluator_fp):
    return {
        "candidate_id": candidate_id,
        "problem_identity": problem,
        "family_id": family,
        "project_id": f"project-{candidate_id}",
        "method_stream_sha256": f"{index:064x}",
        "code_sha256": f"{index + 10:064x}",
        "solver_receipt_sha256": f"{index + 20:064x}",
        "canonical_result_sha256": f"{index + 30:064x}",
        "packet_sha256": f"{index + 40:064x}",
        "pdf_sha256": f"{index + 50:064x}",
        "budget": 100.0 + index,
        "seed": f"seed-{index}",
        "hard_gate_identity_fingerprint": evaluator_fp,
        "hard_gate_decisions": {"math": "PASS", "execution": "PASS"},
        "r1_r2_hard_pass": True,
    }


def _reports(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    hard_evaluator = _evaluator("hard-gate-judge-v1")
    selector_evaluator = _evaluator("selector-judge-v1")
    hard_fp = evaluator_fingerprint(hard_evaluator)
    selector_fp = evaluator_fingerprint(selector_evaluator)
    r0a = {
        "schema": R0A_REPORT_SCHEMA,
        "evaluator": hard_evaluator,
        "evaluator_identity_fingerprint": hard_fp,
        "hard_gate_ready": True,
        "automatic_switch_performed": False,
        "operator_authorization_required": True,
        "claim_limit": "EXACT_RUNTIME_ORACLE_CAPABILITY_AND_REPEATABILITY_ONLY",
    }
    r0b = {
        "schema": R0B_REPORT_SCHEMA,
        "evaluator": selector_evaluator,
        "evaluator_identity_fingerprint": selector_fp,
        "hard_gate_identity_fingerprint": hard_fp,
        "comparison_ready_human": True,
        "comparison_ready_proxy": True,
        "tie_band": 0.1,
        "holdout_hash": "d" * 64,
        "split_families": {"dev": ["cal-dev"], "holdout": ["cal-holdout"]},
        "advisory_only": True,
        "automatic_switch_performed": False,
        "operator_authorization_required": True,
        "production_selection_authorized": False,
        "claim_limit": "BLIND_PAIRWISE_SELECTOR_CALIBRATION_ONLY",
    }
    r0a_path = tmp_path / "r0a.json"
    r0b_path = tmp_path / "r0b.json"
    r0a_path.write_text(json.dumps(r0a), encoding="utf-8")
    r0b_path.write_text(json.dumps(r0b), encoding="utf-8")
    return hard_fp, selector_fp, r0a, r0a_path, r0b, r0b_path


def _fixture(tmp_path: Path):
    hard_fp, selector_fp, r0a, r0a_path, r0b, r0b_path = _reports(tmp_path)
    candidates = [
        _candidate("p1-a", "problem-1", "shadow-1", 1, hard_fp),
        _candidate("p1-b", "problem-1", "shadow-2", 2, hard_fp),
        _candidate("p2-a", "problem-2", "shadow-3", 3, hard_fp),
        _candidate("p2-b", "problem-2", "shadow-4", 4, hard_fp),
    ]
    pairs = [
        {
            "pair_id": "pair-1",
            "candidate_a": "p1-a",
            "candidate_b": "p1-b",
            "problem_identity": "problem-1",
            "margin": 0.8,
            "decision": "A",
            "mainline_candidate_id": "p1-a",
            "selector_identity_fingerprint": selector_fp,
            "r1_conflicts": [],
        },
        {
            "pair_id": "pair-2",
            "candidate_a": "p2-a",
            "candidate_b": "p2-b",
            "problem_identity": "problem-2",
            "margin": -0.8,
            "decision": "B",
            "mainline_candidate_id": "p2-a",
            "selector_identity_fingerprint": selector_fp,
            "r1_conflicts": [],
        },
    ]
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "run_id": "shadow-001",
        "frozen": True,
        "selector_identity_fingerprint": selector_fp,
        "gate2_isolation": {
            "gate2_evaluator_identity_fingerprint": "f" * 64,
            "receipt_sha256": "e" * 64,
            "selector_recommendation_hidden": True,
            "candidate_scores_hidden": True,
            "rejected_candidate_identity_hidden": True,
        },
        "r0a_report": {"path": r0a_path.name, "sha256": file_sha256(r0a_path)},
        "selector_report": {"path": r0b_path.name, "sha256": file_sha256(r0b_path)},
        "thresholds": {
            "minimum_candidates": 2,
            "minimum_projects": 2,
            "minimum_pair_decisions": 2,
            "selector_coverage_min": 0.5,
            "tie_rate_max": 0.5,
            "mainline_disagreement_rate_max": 1.0,
            "budget_ratio_max": 2.0,
            "minimum_adjudications": 2,
            "adjudication_win_rate_min": 0.2,
            "regret_rate_max": 1.0,
        },
        "candidates": candidates,
        "pair_decisions": pairs,
        "adjudications": [
            {
                "pair_id": "pair-1",
                "winner_candidate_id": "p1-a",
                "source": "independent-blind-panel",
                "adjudicated_at": "2026-08-05T00:00:00Z",
                "method": "blind-pairwise-adjudication",
                "blind": True,
                "selector_blinded": True,
            },
            {
                "pair_id": "pair-2",
                "winner_candidate_id": "p2-b",
                "source": "independent-blind-panel",
                "adjudicated_at": "2026-08-05T00:00:00Z",
                "method": "blind-pairwise-adjudication",
                "blind": True,
                "selector_blinded": True,
            },
        ],
    }
    return manifest, r0a, r0a_path, r0b, r0b_path


def test_shadow_portfolio_is_advisory_and_reports_mainline_disagreement(tmp_path):
    manifest, r0a, r0a_path, r0b, r0b_path = _fixture(tmp_path)
    report = evaluate_shadow_portfolio(manifest, tmp_path, r0a, r0a_path, r0b, r0b_path)
    assert report["schema"] == REPORT_SCHEMA
    assert report["portfolio_ready"] is True
    assert report["mainline_disagreement_rate"]["estimate"] == 0.5
    assert report["advisory_only"] is True
    assert report["automatic_switch_performed"] is False
    assert report["gate2_isolated"] is True
    assert report["selector_labels_from_gate2"] is False


def test_unready_r0b_is_reported_but_never_authorizes(tmp_path):
    manifest, r0a, r0a_path, r0b, r0b_path = _fixture(tmp_path)
    r0b["comparison_ready_human"] = False
    r0b_path.write_text(json.dumps(r0b), encoding="utf-8")
    manifest["selector_report"]["sha256"] = file_sha256(r0b_path)
    report = evaluate_shadow_portfolio(manifest, tmp_path, r0a, r0a_path, r0b, r0b_path)
    assert report["portfolio_ready"] is False
    assert "r0b_human_ready" in report["blocked_reasons"]


def test_hard_gate_and_r1_conflict_are_not_selector_wins(tmp_path):
    manifest, r0a, r0a_path, r0b, r0b_path = _fixture(tmp_path)
    manifest["candidates"][1]["r1_r2_hard_pass"] = False
    manifest["pair_decisions"][0]["margin"] = None
    manifest["pair_decisions"][0]["decision"] = None
    manifest["pair_decisions"][1]["r1_conflicts"] = ["bound_conflict"]
    report = evaluate_shadow_portfolio(manifest, tmp_path, r0a, r0a_path, r0b, r0b_path)
    assert report["admitted_pair_count"] == 1
    assert report["shadow_pairs"][0]["effective_decision"] == "HARD_GATE_BLOCKED"
    assert report["shadow_pairs"][1]["effective_decision"] == "R1_VETO"
    assert report["portfolio_ready"] is False


def test_identity_drift_and_tie_margin_fail_closed(tmp_path):
    manifest, r0a, r0a_path, r0b, r0b_path = _fixture(tmp_path)
    manifest["pair_decisions"][0]["selector_identity_fingerprint"] = "f" * 64
    with pytest.raises(PortfolioError, match="identity drift"):
        evaluate_shadow_portfolio(manifest, tmp_path, r0a, r0a_path, r0b, r0b_path)

    manifest, r0a, r0a_path, r0b, r0b_path = _fixture(tmp_path / "tie")
    manifest["pair_decisions"][0]["margin"] = 0.05
    manifest["pair_decisions"][0]["decision"] = "A"
    with pytest.raises(PortfolioError, match="TIE rule"):
        evaluate_shadow_portfolio(manifest, tmp_path / "tie", r0a, r0a_path, r0b, r0b_path)


def test_r0b_family_leakage_and_unpinned_report_fail_closed(tmp_path):
    manifest, r0a, r0a_path, r0b, r0b_path = _fixture(tmp_path)
    manifest["candidates"][0]["family_id"] = "cal-holdout"
    with pytest.raises(PortfolioError, match="calibration families"):
        evaluate_shadow_portfolio(manifest, tmp_path, r0a, r0a_path, r0b, r0b_path)

    manifest, r0a, r0a_path, r0b, r0b_path = _fixture(tmp_path / "pin")
    manifest["selector_report"]["sha256"] = "0" * 64
    with pytest.raises(PortfolioError, match="not pinned"):
        evaluate_shadow_portfolio(
            manifest, tmp_path / "pin", r0a, r0a_path, r0b, r0b_path
        )


def test_gate2_isolation_requires_a_distinct_evaluator_and_hidden_selector_data(tmp_path):
    manifest, r0a, r0a_path, r0b, r0b_path = _fixture(tmp_path)
    manifest["gate2_isolation"]["gate2_evaluator_identity_fingerprint"] = manifest[
        "selector_identity_fingerprint"
    ]
    with pytest.raises(PortfolioError, match="Gate 2 evaluator must differ"):
        evaluate_shadow_portfolio(manifest, tmp_path, r0a, r0a_path, r0b, r0b_path)

    manifest, r0a, r0a_path, r0b, r0b_path = _fixture(tmp_path / "hidden")
    manifest["gate2_isolation"]["selector_recommendation_hidden"] = False
    with pytest.raises(PortfolioError, match="selector_recommendation_hidden"):
        evaluate_shadow_portfolio(
            manifest, tmp_path / "hidden", r0a, r0a_path, r0b, r0b_path
        )


def test_cli_require_ready_distinguishes_ready_and_valid_not_ready(tmp_path):
    manifest, r0a, r0a_path, r0b, r0b_path = _fixture(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "report.json"
    assert main([str(manifest_path), "--json-output", str(output), "--require-ready"]) == 0
    manifest["thresholds"]["minimum_candidates"] = 99
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert main([str(manifest_path), "--json-output", str(output), "--require-ready"]) == 3
