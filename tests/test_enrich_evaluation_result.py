from scripts.enrich_evaluation_result import enrich_aggregate
from pathlib import Path


def _ready_calibration():
    return {
        "models": ["deepseek-chat"],
        "calibration_contract": {"present": True, "models": ["deepseek-chat"], "schema_version": 3},
        "calibration_identity": {"fresh": True, "source_fingerprint": "fingerprint", "stale_results": []},
        "proxy_reliability": {"ready": True, "checks": {"pairwise_accuracy": True}},
        "runtime_score_reliability": {
            "ready": True,
            "proxy_ready": True,
            "human_ready": False,
            "checks": {"evaluator_schema_match": True, "packet_modality_match": True},
        },
        "axis_reliability": {"ready": False, "checks": {"writing_pairwise_accuracy": False}},
    }


def _hard_aggregate():
    return {
        "n": 1,
        "n_scored": 1,
        "verdicts": ["PASS"],
        "model": "deepseek-chat",
        "roles": [
            {"role": "math", "status": "PASS", "verdict": "PASS"},
            {"role": "execution", "status": "PASS", "verdict": "PASS"},
        ],
        "scoring_eligible": True,
    }


def test_enrich_aggregate_splits_structural_and_llm_signals():
    aggregate = {
        "base": "demo",
        "n": 3,
        "n_scored": 3,
        "median_recomputed": 82.4,
        "min_recomputed": 79.0,
        "max_recomputed": 84.0,
        "median_total": 83.0,
        "verdicts": ["PASS", "PASS", "REOPEN_REVISION_TEXT"],
    }
    precheck = {
        "passed": False,
        "inferred_step": 15,
        "checks": [
            {"name": "infer_step", "ok": False, "detail": "15 (expected 16)", "severity": "error"},
            {"name": "papers_pdf", "ok": True, "detail": "ok", "severity": "error"},
        ],
    }

    enriched = enrich_aggregate(
        aggregate,
        precheck=precheck,
        unmatched_numbers="2",
        inloop_total="86.4",
        calibration_report={"proxy_reliability": {"ready": False, "checks": {"pairwise_accuracy": False}}},
    )

    assert enriched["structural"]["precheck_passed"] is False
    assert enriched["structural"]["unmatched_numbers"] == "2"
    assert enriched["structural"]["blocking_evidence"][0]["name"] == "infer_step"
    assert enriched["llm_score"]["median_recomputed"] == 82.4
    assert enriched["llm_score"]["spread_recomputed"] == 5.0
    assert enriched["llm_score"]["verdict_distribution"] == {
        "PASS": 2,
        "REOPEN_REVISION_TEXT": 1,
    }
    assert enriched["comparison_ready"] is False
    assert enriched["calibration"]["ready"] is False


def test_comparison_ready_requires_calibration_readiness():
    aggregate = _hard_aggregate() | {"median_recomputed": 80}
    precheck = {"passed": True, "scoring_eligible": True, "checks": []}

    blocked = enrich_aggregate(
        aggregate.copy(),
        precheck=precheck,
        unmatched_numbers="0",
        inloop_total="80",
        calibration_report={"proxy_reliability": {"ready": False}},
    )
    ready = enrich_aggregate(
        aggregate.copy(),
        precheck=precheck,
        unmatched_numbers="0",
        inloop_total="80",
        calibration_report=_ready_calibration(),
    )

    assert blocked["comparison_ready"] is False
    assert ready["comparison_ready"] is True
    assert ready["comparison_ready_human"] is False


def test_proxy_harness_readiness_alone_does_not_validate_runtime_scores():
    calibration = _ready_calibration()
    calibration.pop("runtime_score_reliability")

    enriched = enrich_aggregate(
        _hard_aggregate(),
        precheck={"passed": True, "scoring_eligible": True},
        unmatched_numbers="0",
        inloop_total="80",
        calibration_report=calibration,
    )

    assert enriched["calibration"]["proxy_ready"] is True
    assert enriched["calibration"]["runtime_ready"] is False
    assert enriched["comparison_ready_proxy"] is False
    assert enriched["comparison_ready"] is False


def test_explicit_packet_ineligibility_cannot_be_overridden_by_precheck():
    aggregate = _hard_aggregate() | {"scoring_eligible": False}

    enriched = enrich_aggregate(
        aggregate,
        precheck={"passed": True, "scoring_eligible": True},
        unmatched_numbers="0",
        inloop_total="80",
        calibration_report=_ready_calibration(),
    )

    assert enriched["structural"]["scoring_eligible"] is False
    assert enriched["comparison_ready_proxy"] is False
    assert enriched["comparison_ready"] is False


def test_generic_comparison_ready_accepts_runtime_human_validation_without_proxy():
    calibration = _ready_calibration()
    calibration["proxy_reliability"]["ready"] = False
    calibration["score_reliability"] = {"ready": True}
    calibration["runtime_score_reliability"].update(
        {"ready": True, "proxy_ready": False, "human_ready": True}
    )

    enriched = enrich_aggregate(
        _hard_aggregate(),
        precheck={"passed": True, "scoring_eligible": True},
        unmatched_numbers="0",
        inloop_total="80",
        calibration_report=calibration,
    )

    assert enriched["comparison_ready_proxy"] is False
    assert enriched["comparison_ready_human"] is True
    assert enriched["comparison_ready"] is True


def test_axis_readiness_is_reported_without_blocking_overall_pairwise_use():
    aggregate = _hard_aggregate()
    calibration = _ready_calibration()
    enriched = enrich_aggregate(
        aggregate,
        precheck={"passed": True, "scoring_eligible": True},
        unmatched_numbers="0",
        inloop_total="80",
        calibration_report=calibration,
    )
    assert enriched["comparison_ready"] is True
    assert enriched["calibration"]["axis_ready"] is False


def test_hard_role_pass_is_derived_from_every_aggregate_run():
    from scripts.enrich_evaluation_result import hard_roles_pass

    ready = {
        "runs": [
            {"role_statuses": {"math": "PASS", "execution": "PASS", "paper": "PASS"}},
            {"role_statuses": {"math": "PASS", "execution": "PASS", "paper": "REVISE"}},
        ]
    }
    blocked = {
        "runs": [
            {"role_statuses": {"math": "PASS", "execution": "FAIL", "paper": "PASS"}},
        ]
    }

    assert hard_roles_pass(ready) is True
    assert hard_roles_pass(blocked) is False


def test_run_evaluation_uses_enrichment_module():
    text = Path("evaluation/run_evaluation.sh").read_text(encoding="utf-8")

    assert "PRECHECK_JSON=" in text
    assert "enrich_evaluation_result.py" in text
    assert "--precheck \"$PRECHECK_JSON\"" in text
    assert "--calibration-report" in text
