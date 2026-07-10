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
