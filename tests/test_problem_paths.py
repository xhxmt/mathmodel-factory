def test_resolve_problem_constraints_path_prefers_canonical(tmp_path):
    from problem_paths import resolve_problem_constraints_path

    problem_dir = tmp_path / "problem"
    problem_dir.mkdir()
    (problem_dir / "constraints.md").write_text("legacy", encoding="utf-8")
    (problem_dir / "feasibility_constraints.md").write_text("canonical", encoding="utf-8")

    resolved = resolve_problem_constraints_path(tmp_path)
    assert resolved.name == "feasibility_constraints.md"


def test_resolve_problem_constraints_path_falls_back_to_legacy(tmp_path):
    from problem_paths import resolve_problem_constraints_path

    problem_dir = tmp_path / "problem"
    problem_dir.mkdir()
    (problem_dir / "constraints.md").write_text("legacy", encoding="utf-8")

    resolved = resolve_problem_constraints_path(tmp_path)
    assert resolved.name == "constraints.md"


def test_find_method_path_skips_missing_docs():
    from method_fit_score import find_method_path

    assert find_method_path("动态规划") is None
