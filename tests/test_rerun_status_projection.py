import json

from factory_core.projections import write_compatibility_projections
from factory_core.storage import SQLiteStateStore


def test_stale_status_refresh_uses_current_revision_and_rejects_unbound_score(tmp_path):
    store = SQLiteStateStore(tmp_path)
    old = store.initialize(project_id=tmp_path.name, project_type="math_modeling")
    current = store.transition(expected_revision=old.revision, event_type="JUDGE_BINDING_FAILED",
                               changes={}, payload={"error_class": "TRANSIENT_JUDGE_PROVENANCE"})
    folder = tmp_path / "judge_outputs"
    folder.mkdir()
    (folder / "aggregate.json").write_text('{"verdict":"INDETERMINATE_REVIEW","overall_score":75,"score_available":true}')
    write_compatibility_projections(tmp_path, old)
    status = json.loads((tmp_path / "diagnostics/status.json").read_text())
    assert status["revision"] == current.revision
    assert status["evidence_validity"] == "INVALID"
    assert status["scientific_verdict"] == "UNAVAILABLE"
    assert status["official_score"] is None and status["diagnostic_score"] is None
    assert status["score_available"] is False and status["delivery_allowed"] is False
