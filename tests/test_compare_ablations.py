import json

import experiments.compare_ablations as compare_ablations
from experiments.compare_ablations import DIMS, dim_medians


def test_dim_medians_use_current_conditional_quality_schema():
    run = {
        "dims": {
            dimension: {"weighted_mean": index + 1}
            for index, dimension in enumerate(DIMS)
        }
    }

    medians = dim_medians({"runs": [run, run]})

    assert tuple(medians) == DIMS
    assert medians["model_presentation"] == 1.0
    assert medians["sensitivity_limitations"] == 6.0


def test_load_rejects_generic_readiness_without_explicit_proxy_calibration(
    tmp_path, monkeypatch, capsys
):
    result = tmp_path / "demo_eval.json"
    result.write_text(
        json.dumps(
            {
                "comparison_ready": True,
                "median_recomputed": 80.0,
                "min_recomputed": 79.0,
                "max_recomputed": 81.0,
                "runs": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(compare_ablations, "resolve_json", lambda _base: result)

    assert compare_ablations.load("demo") is None
    assert "not calibrated-comparison-ready" in capsys.readouterr().err


def test_load_accepts_explicit_proxy_readiness(tmp_path, monkeypatch):
    result = tmp_path / "demo_eval.json"
    result.write_text(
        json.dumps(
            {
                "comparison_ready": False,
                "comparison_ready_proxy": True,
                "median_recomputed": 80.0,
                "min_recomputed": 79.0,
                "max_recomputed": 81.0,
                "runs": [],
                "verdicts": ["PASS"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(compare_ablations, "resolve_json", lambda _base: result)

    loaded = compare_ablations.load("demo")

    assert loaded is not None
    assert loaded["total"] == 80.0


def test_load_accepts_explicit_human_readiness_without_proxy(tmp_path, monkeypatch):
    result = tmp_path / "demo_eval.json"
    result.write_text(
        json.dumps(
            {
                "comparison_ready": True,
                "comparison_ready_proxy": False,
                "comparison_ready_human": True,
                "median_recomputed": 82.0,
                "min_recomputed": 82.0,
                "max_recomputed": 82.0,
                "runs": [],
                "verdicts": ["PASS"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(compare_ablations, "resolve_json", lambda _base: result)

    loaded = compare_ablations.load("demo")

    assert loaded is not None
    assert loaded["total"] == 82.0
