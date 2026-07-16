from scripts.enrich_evaluation_result import enrich_aggregate
from pathlib import Path


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
    aggregate = {"n": 1, "n_scored": 1, "median_recomputed": 80, "verdicts": ["PASS"]}
    precheck = {"passed": True, "checks": []}

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
        calibration_report={"proxy_reliability": {"ready": True}},
    )

    assert blocked["comparison_ready"] is False
    assert ready["comparison_ready"] is True
    assert ready["comparison_ready_human"] is False


def test_run_evaluation_uses_enrichment_module():
    text = Path("evaluation/run_evaluation.sh").read_text(encoding="utf-8")

    assert "PRECHECK_JSON=" in text
    assert "enrich_evaluation_result.py" in text
    assert "--precheck \"$PRECHECK_JSON\"" in text
    assert "--calibration-report" in text
