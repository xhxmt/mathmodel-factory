import pytest

from factory_core.domain import InvalidTransition, WorkflowStatus
from factory_core.engine import FactoryEngine
from factory_core.storage import SQLiteStateStore
from factory_core.dirty import capture_artifact_manifest, manifest_fingerprint
from factory_core.repair_recovery import usable
from tests.test_stage_scheduler import stage_registry


def setup_failed(tmp_path):
    (tmp_path / "model.md").write_text("old")
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id=tmp_path.name, project_type="math_modeling",
                             scheduler_generation="stage_v1", last_completed_step=3)
    manifest = capture_artifact_manifest(tmp_path)
    state = store.transition(expected_revision=state.revision, event_type="STEP_FAILED",
        event_step=4, changes={"status": WorkflowStatus.FAILED, "active_step": 4, "active_stage": 3,
            "active_subtask": "model_construction", "source_step_id": 4, "attempt": 5},
        subtask_baseline={"stage_id": 3, "subtask": "model_construction", "source_step_id": 4,
            "input_fingerprint": manifest_fingerprint(manifest), "manifest": manifest})
    registry, _ = stage_registry()
    return FactoryEngine(tmp_path, store=store, registry=registry), state


def test_unchanged_repair_cannot_renew_budget(tmp_path):
    engine, state = setup_failed(tmp_path)
    before = engine.store.path.read_bytes()
    with pytest.raises(InvalidTransition, match="unchanged"):
        engine.authorize_repair_retry(expected_revision=state.revision, reason="try again")
    assert engine.store.path.read_bytes() == before


def test_changed_input_gets_one_attempt_without_resetting_history(tmp_path):
    engine, state = setup_failed(tmp_path)
    events = engine.store.events()
    (tmp_path / "model.md").write_text("repaired")
    updated = engine.authorize_repair_retry(expected_revision=state.revision, reason="fixed model contract")
    assert updated.attempt == 5
    assert engine.store.events()[:len(events)] == events
    assert usable(engine, updated) == updated.revision
    started = engine.store.transition(expected_revision=updated.revision, event_type="STEP_STARTED",
                                      changes={"attempt": 6, "status": WorkflowStatus.RUNNING})
    assert usable(engine, started) is None


def test_engine_executes_the_bound_repair_attempt(tmp_path):
    engine, state = setup_failed(tmp_path)
    (tmp_path / "model.md").write_text("repaired")
    engine.authorize_repair_retry(expected_revision=state.revision, reason="repaired")
    engine.run(max_steps=1)
    assert engine.registry.get(4).lifecycle.calls == [6]
    events = engine.store.events()
    started = [e for e in events if e.type == "STEP_STARTED" and e.step == 4]
    assert len(started) == 1 and started[0].attempt == 6
    assert started[0].payload["repair_authorization_revision"] is not None
