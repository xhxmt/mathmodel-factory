import json
from pathlib import Path


from scripts.evaluate_calibration import evaluate_calibration, write_reports


def _result(path: Path, score: float, *, n: int = 3, n_scored: int = 3, verdicts=None):
    path.write_text(
        json.dumps(
            {
                "n": n,
                "n_scored": n_scored,
                "median_recomputed": score,
                "verdicts": verdicts or ["PASS"] * n,
            }
        ),
        encoding="utf-8",
    )


def test_pairwise_accuracy_and_missing_results_are_explicit(tmp_path):
    _result(tmp_path / "national.json", 90)
    _result(tmp_path / "provincial1.json", 80)
    manifest = {
        "papers": [
            {"id": "n1", "problem_id": "2024B", "result_path": "national.json"},
            {"id": "p1", "problem_id": "2024B", "result_path": "provincial1.json"},
            {"id": "p3", "problem_id": "2024B", "result_path": "missing.json"},
        ],
        "pairs": [
            {"higher": "n1", "lower": "p1"},
            {"higher": "p1", "lower": "p3"},
        ],
    }

    report = evaluate_calibration(manifest, tmp_path)

    assert report["pairwise"]["evaluated"] == 1
    assert report["pairwise"]["correct_points"] == 1.0
    assert report["pairwise"]["accuracy"] == 1.0
    assert report["missing_results"] == ["p3"]
    assert report["coverage_by_problem"]["2024B"] == {
        "available": 2,
        "total": 3,
        "coverage": 2 / 3,
    }


def test_pairwise_tie_gets_half_credit_and_kendall_tie(tmp_path):
    _result(tmp_path / "a.json", 80)
    _result(tmp_path / "b.json", 80)
    manifest = {
        "papers": [
            {"id": "a", "problem_id": "2024B", "result_path": "a.json"},
            {"id": "b", "problem_id": "2024B", "result_path": "b.json"},
        ],
        "pairs": [{"higher": "a", "lower": "b"}],
    }

    report = evaluate_calibration(manifest, tmp_path)

    assert report["pairwise"]["correct_points"] == 0.5
    assert report["pairwise"]["accuracy"] == 0.5
    assert report["ordering"] == {
        "concordant": 0,
        "discordant": 0,
        "ties": 1,
        "kendall_style_tau": 0.0,
    }


def test_malformed_rate_and_fatal_flaw_detection(tmp_path):
    _result(
        tmp_path / "fatal.json",
        55,
        n=3,
        n_scored=2,
        verdicts=["REOPEN_REVISION_MODEL", "REOPEN_REVISION_MODEL", None],
    )
    _result(tmp_path / "clean.json", 85, verdicts=["PASS", "PASS", "PASS"])
    manifest = {
        "papers": [
            {
                "id": "fatal",
                "problem_id": "2024A",
                "result_path": "fatal.json",
                "expected_fatal_flaw": True,
            },
            {
                "id": "clean",
                "problem_id": "2024B",
                "result_path": "clean.json",
                "expected_fatal_flaw": False,
            },
        ],
        "pairs": [],
    }

    report = evaluate_calibration(manifest, tmp_path)

    assert report["malformed_outputs"] == {
        "malformed": 1,
        "total_runs": 6,
        "rate": 1 / 6,
    }
    assert report["fatal_flaw_detection"] == {
        "detected": 1,
        "expected": 1,
        "rate": 1.0,
    }


def test_report_writer_lists_missing_entries(tmp_path):
    report = {
        "papers": [{"id": "n1", "status": "MISSING", "score": None}],
        "missing_results": ["n1"],
        "pairwise": {"evaluated": 0, "total": 1, "correct_points": 0.0, "accuracy": None},
        "ordering": {"concordant": 0, "discordant": 0, "ties": 0, "kendall_style_tau": None},
        "malformed_outputs": {"malformed": 0, "total_runs": 0, "rate": None},
        "fatal_flaw_detection": {"detected": 0, "expected": 0, "rate": None},
        "coverage_by_problem": {},
    }
    json_path = tmp_path / "report.json"
    md_path = tmp_path / "report.md"
    write_reports(report, json_path, md_path)

    assert json.loads(json_path.read_text(encoding="utf-8"))["missing_results"] == ["n1"]
    assert "MISSING" in md_path.read_text(encoding="utf-8")


def test_direct_blind_pairwise_result_takes_priority_over_absolute_scores(tmp_path):
    _result(tmp_path / "high.json", 60)
    _result(tmp_path / "low.json", 95)
    (tmp_path / "pair.json").write_text(
        json.dumps(
            {
                "kind": "blind_pairwise",
                "overall_winner": "high",
                "correctness_winner": "high",
                "writing_winner": "low",
                "samples_requested": 3,
                "samples_scored": 3,
                "malformed": 0,
            }
        ),
        encoding="utf-8",
    )
    manifest = {
        "papers": [
            {"id": "high", "problem_id": "2024B", "result_path": "high.json"},
            {"id": "low", "problem_id": "2024B", "result_path": "low.json"},
        ],
        "pairs": [
            {"higher": "high", "lower": "low", "result_path": "pair.json"}
        ],
    }

    report = evaluate_calibration(manifest, tmp_path)

    assert report["pairs"][0]["source"] == "BLIND_PAIRWISE"
    assert report["pairs"][0]["status"] == "CORRECT"
    assert report["pairwise"]["direct_coverage"] == 1.0


def test_score_reliability_requires_direct_pairs_and_split_fatal_detection(tmp_path):
    for paper_id, score, fatal_rate in (("n1", 90, 0.0), ("p1", 80, 0.0), ("fatal", 55, 1.0)):
        (tmp_path / f"{paper_id}.json").write_text(
            json.dumps(
                {
                    "samples_requested": 3,
                    "samples_scored": 3,
                    "writing": {
                        "median_score": score,
                        "dimensions": {f"d{i}": score for i in range(6)},
                    },
                    "correctness": {"median_score": score, "fatal_flaw_rate": fatal_rate},
                }
            ),
            encoding="utf-8",
        )
    (tmp_path / "pair.json").write_text(
        json.dumps(
            {
                "kind": "blind_pairwise",
                "overall_winner": "n1",
                "correctness_winner": "n1",
                "writing_winner": "n1",
                "samples_requested": 3,
                "samples_scored": 3,
                "malformed": 0,
            }
        ),
        encoding="utf-8",
    )
    manifest = {
        "readiness_policy": {
            "min_pairwise_accuracy": 1.0,
            "min_direct_pair_coverage": 1.0,
            "max_malformed_rate": 0.0,
            "min_fatal_flaw_detection_rate": 1.0,
        },
        "papers": [
            {"id": "n1", "problem_id": "2024B", "result_path": "n1.json"},
            {"id": "p1", "problem_id": "2024B", "result_path": "p1.json"},
            {
                "id": "fatal",
                "problem_id": "2024A",
                "result_path": "fatal.json",
                "expected_fatal_flaw": True,
            },
        ],
        "pairs": [{"higher": "n1", "lower": "p1", "result_path": "pair.json"}],
    }

    report = evaluate_calibration(manifest, tmp_path)

    assert report["fatal_flaw_detection"]["rate"] == 1.0
    assert report["score_reliability"]["ready"] is False
    assert report["human_calibration"]["ready"] is False


def test_proxy_readiness_uses_axis_specific_expected_winners(tmp_path):
    for paper_id, fatal_rate in (("clean", 0.0), ("broken", 1.0)):
        (tmp_path / f"paper_{paper_id}.json").write_text(
            json.dumps(
                {
                    "samples_requested": 3,
                    "samples_scored": 3,
                    "writing": {"median_score": 80, "dimensions": {f"d{i}": 80 for i in range(6)}},
                    "correctness": {"median_score": 80, "fatal_flaw_rate": fatal_rate},
                }
            ),
            encoding="utf-8",
        )
    (tmp_path / "pair.json").write_text(
        json.dumps(
            {
                "kind": "blind_pairwise",
                "overall_winner": "clean",
                "correctness_winner": "clean",
                "writing_winner": "TIE",
                "samples_requested": 3,
                "samples_scored": 3,
                "malformed": 0,
            }
        ),
        encoding="utf-8",
    )
    manifest = {
        "readiness_kind": "proxy",
        "calibration_results_dir": ".",
        "readiness_policy": {
            "min_pairwise_accuracy": 1.0,
            "min_correctness_accuracy": 1.0,
            "min_writing_accuracy": 1.0,
            "min_direct_pair_coverage": 1.0,
            "max_malformed_rate": 0.0,
            "min_fatal_flaw_detection_rate": 1.0,
        },
        "papers": [
            {"id": "clean", "problem_id": "X"},
            {"id": "broken", "problem_id": "X", "expected_fatal_flaw": True},
        ],
        "pairs": [
            {
                "higher": "clean",
                "lower": "broken",
                "result_path": "pair.json",
                "expected_overall_winner": "clean",
                "expected_correctness_winner": "clean",
                "expected_writing_winner": "TIE",
            }
        ],
    }
    report = evaluate_calibration(manifest, tmp_path)
    assert report["proxy_reliability"]["ready"] is True
    assert report["axis_reliability"]["ready"] is True
    assert report["score_reliability"]["ready"] is False
    assert report["award_prediction_ready"] is False


def test_proxy_overall_ranking_can_be_ready_while_subaxes_are_not(tmp_path):
    for paper_id, fatal_rate in (("clean", 0.0), ("broken", 1.0)):
        (tmp_path / f"paper_{paper_id}.json").write_text(
            json.dumps(
                {
                    "samples_requested": 1,
                    "samples_scored": 1,
                    "writing": {"median_score": 80, "dimensions": {f"d{i}": 80 for i in range(6)}},
                    "correctness": {"median_score": 80, "fatal_flaw_rate": fatal_rate},
                }
            ), encoding="utf-8",
        )
    (tmp_path / "pair.json").write_text(
        json.dumps(
            {
                "kind": "blind_pairwise",
                "overall_winner": "clean",
                "correctness_winner": "TIE",
                "writing_winner": "TIE",
                "samples_requested": 2,
                "samples_scored": 1,
                "malformed": 1,
                "adjudicated": True,
                "runs": [{"role": "adjudicator", "status": "OK"}],
            }
        ), encoding="utf-8",
    )
    manifest = {
        "readiness_kind": "proxy",
        "calibration_results_dir": ".",
        "readiness_policy": {
            "min_pairwise_accuracy": 1.0,
            "min_correctness_accuracy": 1.0,
            "min_writing_accuracy": 1.0,
            "min_direct_pair_coverage": 1.0,
            "max_malformed_rate": 0.5,
            "min_fatal_flaw_detection_rate": 1.0,
        },
        "papers": [
            {"id": "clean", "problem_id": "X"},
            {"id": "broken", "problem_id": "X", "expected_fatal_flaw": True},
        ],
        "pairs": [{
            "higher": "clean", "lower": "broken", "result_path": "pair.json",
            "expected_overall_winner": "clean",
            "expected_correctness_winner": "clean",
            "expected_writing_winner": "clean",
        }],
    }
    report = evaluate_calibration(manifest, tmp_path)
    assert report["pairwise"]["direct_coverage"] == 1.0
    assert report["proxy_reliability"]["ready"] is True
    assert report["axis_reliability"]["ready"] is False


def test_diagnostic_weak_prior_is_reported_but_excluded_from_accuracy(tmp_path):
    for paper_id in ("high", "low", "diagnostic"):
        _result(tmp_path / f"{paper_id}.json", 80)
    for name, winner in (("strong_pair", "high"), ("weak_pair", "diagnostic")):
        (tmp_path / f"{name}.json").write_text(
            json.dumps(
                {
                    "kind": "blind_pairwise",
                    "overall_winner": winner,
                    "correctness_winner": winner,
                    "writing_winner": winner,
                    "samples_requested": 1,
                    "samples_scored": 1,
                    "malformed": 0,
                }
            ), encoding="utf-8",
        )
    manifest = {
        "papers": [
            {"id": "high", "problem_id": "X", "result_path": "high.json"},
            {"id": "low", "problem_id": "X", "result_path": "low.json"},
            {"id": "diagnostic", "problem_id": "X", "result_path": "diagnostic.json"},
        ],
        "pairs": [
            {"higher": "high", "lower": "low", "result_path": "strong_pair.json"},
            {
                "higher": "low", "lower": "diagnostic", "result_path": "weak_pair.json",
                "readiness_eligible": False,
            },
        ],
    }
    report = evaluate_calibration(manifest, tmp_path)
    assert report["pairwise"]["accuracy"] == 1.0
    assert report["pairwise"]["readiness_total"] == 1
    assert report["pairwise"]["diagnostic_pairs"] == 1
    assert report["pairs"][1]["status"] == "REVERSED"
