import json

from factory_core.current_dirty import capture_artifact_manifest, manifest_fingerprint, classifier_contract_sha256
from factory_core.domain import SCHEMA_VERSION, WorkflowStatus
from factory_core.final_evidence_recovery import classifier_equivalent_for_restored_checkpoints
from factory_core.storage import SQLiteStateStore
from factory_core.workflow_events import canonical_hash


def _restored_checkpoint_project(tmp_path):
    """A Stage 9 checkpoint plus a recorded config-only correction for it.

    The operation that wrote these corrections is retired; projects that carry
    one keep their restored checkpoints valid through the equivalence check.
    """
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id="restored", project_type="modeling")
    (tmp_path / "sample_paper.tex").write_text("unchanged paper")
    business = manifest_fingerprint(capture_artifact_manifest(tmp_path))
    state = store.transition(expected_revision=state.revision, event_type="TEST_CHECKPOINT",
        stage_checkpoint={"stage_id": 9, "subtask": "polish", "source_step_id": 15,
            "completed_step_id": 15, "input_fingerprint": business, "output_fingerprint": business,
            "receipt": {"validated": True}})
    (tmp_path / "judge_evidence.json").write_text('{"schema_version":1}')
    config_sha = capture_artifact_manifest(tmp_path)["judge_evidence.json"]
    with store._session() as connection:
        checkpoint_id = connection.execute(
            "SELECT checkpoint_id FROM stage_checkpoint_history").fetchone()[0]
        receipt = {
            "schema_version": "factory-final-evidence-reclassification-v1",
            "restored_checkpoint_ids": [checkpoint_id],
            "config_sha256": config_sha,
            "unchanged_business_fingerprint": business,
            "new_classifier_sha256": classifier_contract_sha256(),
        }
        connection.execute(
            "INSERT INTO dirty_classifier_rebases VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (canonical_hash(receipt), SCHEMA_VERSION, SCHEMA_VERSION, "[]",
             classifier_contract_sha256(), 1, 1, json.dumps(receipt)))
    checkpoints = [c for c in store.stage_checkpoints() if c["stage_id"] == 9]
    return store, checkpoints


def test_restored_checkpoints_stay_equivalent_while_business_files_are_unchanged(tmp_path):
    store, checkpoints = _restored_checkpoint_project(tmp_path)

    assert classifier_equivalent_for_restored_checkpoints(
        store, checkpoints, classifier_contract_sha256())
    assert not classifier_equivalent_for_restored_checkpoints(
        store, checkpoints, "another-classifier")

    (tmp_path / "sample_paper.tex").write_text("drift")
    assert not classifier_equivalent_for_restored_checkpoints(
        store, checkpoints, classifier_contract_sha256())


def test_requested_run_scope_stops_before_execution(tmp_path):
    from test_stage_scheduler import stage_registry
    from factory_core.engine import FactoryEngine
    from factory_core.stages import STAGE_SCHEDULER_GENERATION
    registry, lifecycles = stage_registry()
    store = SQLiteStateStore(tmp_path)
    store.initialize(project_id="bounded", project_type="modeling", scheduler_generation=STAGE_SCHEDULER_GENERATION)
    state = FactoryEngine(tmp_path, store=store, registry=registry).run(allowed_source_steps=frozenset({16}))
    assert state.status is WorkflowStatus.PAUSED
    assert not any(event.type == "STEP_STARTED" for event in store.events())
    assert store.events()[-1].type == "RUN_BOUNDARY_REACHED"
