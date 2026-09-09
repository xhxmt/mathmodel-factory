import json
from pathlib import Path

from scripts.claim_graph import claim_binding_issues
from factory_core.steps.validators import NativeArtifactValidator
from factory_core.domain import StepContext
from tests.test_claim_graph import _declared_registry, _write


def setup_claim(tmp_path, field="value"):
    _write(tmp_path, "problem/problem_brief.md", "Question 1\nQuestion 2\n")
    _write(tmp_path, "model.md", "model")
    registry = _declared_registry(second_artifact="results/problem3A/values.json")
    registry["claims"][1]["artifacts"][0]["field"] = field
    _write(tmp_path, "claim_registry.json", json.dumps(registry))


def test_claim_binding_reports_exact_missing_path_and_owner(tmp_path):
    setup_claim(tmp_path)
    issue = claim_binding_issues(tmp_path)[0]
    assert issue["path"] == "results/problem3A/values.json"
    assert issue["resume_after_step"] == 4
    validator = NativeArtifactValidator(Path(__file__).resolve().parents[1], 13)
    result = validator.validate(StepContext(tmp_path, tmp_path.name, 13, 1, 60, 0))
    assert not result.is_valid
    assert result.metadata["resume_after_step"] == 4


def test_claim_binding_checks_field_and_preserves_future_declarations(tmp_path):
    setup_claim(tmp_path, "metrics.rmse[0]")
    assert claim_binding_issues(tmp_path, through_stage=3) == []
    _write(tmp_path, "results/problem3A/values.json", '{"metrics":{}}')
    assert claim_binding_issues(tmp_path)[0]["field"] == "metrics.rmse[0]"
    _write(tmp_path, "results/problem3A/values.json", '{"metrics":{"rmse":[0.1]}}')
    assert claim_binding_issues(tmp_path) == []
