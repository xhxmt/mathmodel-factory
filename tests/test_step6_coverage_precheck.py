from scripts.step6_coverage_precheck import check_results_baseline


def test_sensitivity_output_directory_is_not_treated_as_a_subproblem(tmp_path):
    problem = tmp_path / "results" / "problem1"
    problem.mkdir(parents=True)
    (problem / "values.json").write_text('{"status": "FEASIBLE"}', encoding="utf-8")
    sensitivity = tmp_path / "results" / "sensitivity"
    sensitivity.mkdir()
    (sensitivity / "s1.json").write_text("{}", encoding="utf-8")

    passed, detail = check_results_baseline(tmp_path)

    assert passed is True
    assert "1 个子问题" in detail
