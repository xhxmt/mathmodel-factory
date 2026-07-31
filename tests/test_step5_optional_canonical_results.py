from types import SimpleNamespace

from factory_core.steps import validators as validators_module
from factory_core.steps.validators import NativeArtifactValidator


def _write_lines(path, count):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"line {index}" for index in range(count)) + "\n", encoding="utf-8")


def _validate_step5(project, monkeypatch):
    monkeypatch.setattr(validators_module, "_run", lambda *_args, **_kwargs: True)
    context = SimpleNamespace(project_dir=project)
    return NativeArtifactValidator(project.parent, 5).validate(context)


def test_step5_accepts_per_problem_values_without_canonical_results(tmp_path, monkeypatch):
    project = tmp_path / "case"
    _write_lines(project / "solve_log.md", 20)
    values = project / "results" / "question_1" / "values.json"
    values.parent.mkdir(parents=True)
    values.write_text('{"status": "OPTIMAL", "objective": 12.5}\n', encoding="utf-8")

    result = _validate_step5(project, monkeypatch)

    assert result.is_valid
    assert "results/question_1/values.json" in result.evidence
    assert "results/canonical_results.json" not in result.evidence


def test_step5_reports_canonical_results_when_present(tmp_path, monkeypatch):
    project = tmp_path / "case"
    _write_lines(project / "solve_log.md", 20)
    results = project / "results"
    (results / "question_1").mkdir(parents=True)
    (results / "question_1" / "values.json").write_text('{"status": "OPTIMAL"}\n', encoding="utf-8")
    (results / "canonical_results.json").write_text('{"objective": 12.5}\n', encoding="utf-8")

    result = _validate_step5(project, monkeypatch)

    assert result.is_valid
    assert result.evidence[1:] == (
        "results/canonical_results.json",
        "results/question_1/values.json",
    )


def test_step5_still_requires_per_problem_values(tmp_path, monkeypatch):
    project = tmp_path / "case"
    _write_lines(project / "solve_log.md", 20)
    results = project / "results"
    results.mkdir(parents=True)
    (results / "canonical_results.json").write_text('{"objective": 12.5}\n', encoding="utf-8")

    result = _validate_step5(project, monkeypatch)

    assert not result.is_valid
    assert result.reason == "Step 5 solve/provenance contract invalid"
