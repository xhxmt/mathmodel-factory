import pytest

from factory_core.domain import InvalidTransition, WorkflowStatus
from factory_core.repair_recovery import usable
from tests.test_rerun_repair_recovery import setup_failed
from tests.test_rerun_claim_bindings import setup_claim


def test_future_owner_declarations_do_not_block_model_repair(tmp_path):
    engine, state = setup_failed(tmp_path)
    setup_claim(tmp_path)
    updated = engine.authorize_repair_retry(expected_revision=state.revision, reason="repair model")
    assert updated.status == WorkflowStatus.READY
    assert usable(engine, updated) == updated.revision


def test_mismatched_failed_cursor_baseline_cannot_authorize_repair(tmp_path):
    engine, state = setup_failed(tmp_path)
    state = engine.store.transition(expected_revision=state.revision,
        event_type="STEP_FAILED", event_step=6,
        changes={"active_step": 6, "source_step_id": 6, "active_stage": 5,
                 "active_subtask": "sensitivity"})
    (tmp_path / "model.md").write_text("new model")
    before = engine.store.path.read_bytes()
    with pytest.raises(InvalidTransition, match="baseline"):
        engine.authorize_repair_retry(expected_revision=state.revision, reason="repair")
    assert engine.store.path.read_bytes() == before


def test_missing_upstream_is_not_reported_as_attempt_budget_exhaustion(tmp_path):
    engine, state = setup_failed(tmp_path)
    baseline = engine.store.stage_cursor_input()
    state = engine.store.transition(expected_revision=state.revision,
        event_type="STEP_FAILED", event_step=6,
        changes={"active_step": 6, "source_step_id": 6, "active_stage": 5,
                 "active_subtask": "sensitivity"},
        subtask_baseline={**baseline, "stage_id": 5, "source_step_id": 6,
                          "subtask": "sensitivity"})
    setup_claim(tmp_path)
    before = engine.store.path.read_bytes()
    with pytest.raises(InvalidTransition, match="REPAIR_UPSTREAM_MISSING.*results/problem3A/values.json"):
        engine.authorize_repair_retry(expected_revision=state.revision, reason="repair")
    assert engine.store.path.read_bytes() == before


def test_changed_implementation_can_authorize_one_attempt_with_same_inputs(tmp_path):
    engine, state = setup_failed(tmp_path)
    baseline = engine.store.stage_cursor_input()
    state = engine.store.transition(expected_revision=state.revision,
        event_type="STEP_STARTED", event_step=4, changes={},
        payload={"input_fingerprint": baseline["input_fingerprint"],
                 "implementation_version": "0" * 64})
    before_attempt = state.attempt
    updated = engine.authorize_repair_retry(expected_revision=state.revision, reason="new implementation")
    assert updated.attempt == before_attempt
    assert usable(engine, updated) == updated.revision


def test_an_older_attempt_does_not_attest_current_failed_implementation(tmp_path):
    engine, state = setup_failed(tmp_path)
    baseline = engine.store.stage_cursor_input()
    state = engine.store.transition(expected_revision=state.revision,
        event_type="STEP_STARTED", event_step=4, changes={"attempt": 4},
        payload={"input_fingerprint": baseline["input_fingerprint"],
                 "implementation_version": "0" * 64})
    state = engine.store.transition(expected_revision=state.revision,
        event_type="STEP_FAILED", event_step=4, changes={"attempt": 5})
    before = engine.store.path.read_bytes()
    with pytest.raises(InvalidTransition, match="unchanged"):
        engine.authorize_repair_retry(expected_revision=state.revision, reason="repair")
    assert engine.store.path.read_bytes() == before
