from __future__ import annotations

import json

import pytest

from factory_core.domain import (
    ExecutionResult,
    PendingAction,
    PrepareResult,
    StepContext,
    ValidationResult,
    WorkflowStatus,
)
from factory_core.engine import FactoryEngine
from factory_core.cli import main as cli_main
from factory_core.execution_pipeline import StageExecutionPipeline, StageExecutionRequest
from factory_core.human_decisions import (
    HumanDecisionKind,
    build_decision_request,
    validate_resolution,
)
from factory_core.storage import SQLiteStateStore
from factory_core.registry import StepDefinition
from factory_core.workflow_events import (
    ENVELOPE_KEY,
    ReplayIntegrityError,
    project_action_center,
    replay_events,
    replay_state,
)


def test_versioned_events_replay_exact_stable_state(tmp_path):
    store = SQLiteStateStore(tmp_path, clock=lambda: 100)
    initial = store.initialize(project_id="demo", project_type="modeling")
    updated = store.transition(
        expected_revision=initial.revision,
        event_type="PAUSED",
        changes={"status": WorkflowStatus.PAUSED},
        payload={"reason": "operator"},
    )

    events = store.events()
    assert events[0].payload[ENVELOPE_KEY]["state_patch_mode"] == "snapshot"
    assert events[1].payload[ENVELOPE_KEY]["state_patch_mode"] == "merge"
    assert replay_events(events) == replay_state(updated)


def test_replay_detects_event_state_tampering(tmp_path):
    store = SQLiteStateStore(tmp_path, clock=lambda: 100)
    store.initialize(project_id="demo", project_type="modeling")
    event = store.events()[0]
    tampered = dict(event.payload)
    tampered[ENVELOPE_KEY] = dict(tampered[ENVELOPE_KEY])
    tampered[ENVELOPE_KEY]["state_patch"] = {
        **tampered[ENVELOPE_KEY]["state_patch"],
        "status": "completed",
    }
    altered = type(event)(
        revision=event.revision,
        type=event.type,
        created_at=event.created_at,
        step=event.step,
        attempt=event.attempt,
        payload=tampered,
    )

    with pytest.raises(ReplayIntegrityError, match="state hash mismatch"):
        replay_events([altered])


def test_old_event_stream_gets_a_cutover_snapshot(tmp_path):
    store = SQLiteStateStore(tmp_path, clock=lambda: 100)
    initial = store.initialize(project_id="demo", project_type="modeling")
    with store._session() as connection:  # simulate a schema-v6 event payload
        connection.execute("DROP TRIGGER events_append_only_update")
        connection.execute(
            "UPDATE events SET payload_json=? WHERE revision=1",
            (json.dumps({"legacy": True}),),
        )
    updated = store.transition(
        expected_revision=initial.revision,
        event_type="PAUSED",
        changes={"status": WorkflowStatus.PAUSED},
    )

    assert store.events()[-1].payload[ENVELOPE_KEY]["state_patch_mode"] == "snapshot"
    assert replay_events(store.events()) == replay_state(updated)


def test_action_center_uses_one_selection_and_approval_contract(tmp_path):
    store = SQLiteStateStore(tmp_path, clock=lambda: 100)
    initial = store.initialize(project_id="demo", project_type="modeling")
    action = PendingAction(type="content_freeze_selection", gate="content_freeze")
    request = build_decision_request(
        project_id="demo",
        requested_revision=initial.revision + 1,
        action=action.to_dict(),
        reason="release gate",
    )
    pending = action.to_dict()
    pending["metadata"] = {"human_decision": request.to_dict()}
    waiting = store.transition(
        expected_revision=initial.revision,
        event_type="AWAITING_ACTION",
        changes={
            "status": WorkflowStatus.AWAITING_SELECTION,
            "pending_action": pending,
        },
        payload={"action": request.to_dict(), "reason": request.reason},
    )
    normalized = validate_resolution(
        pending,
        {
            "gate": "content_freeze",
            "selected_option_id": "approve_content_freeze",
        },
    )
    assert request.kind is HumanDecisionKind.APPROVAL
    assert normalized["approved"] is True

    engine = FactoryEngine(tmp_path, store=store)
    engine.resolve_action(normalized, expected_revision=waiting.revision)
    projection = project_action_center(store.events())
    assert projection["pending"] == []
    assert projection["history"][-1]["status"] == "resolved"


def test_projector_snapshot_is_only_a_versioned_cache(tmp_path):
    store = SQLiteStateStore(tmp_path, clock=lambda: 100)
    state = store.initialize(project_id="demo", project_type="modeling")
    store.save_projector_snapshot(
        "action-center",
        projector_version=1,
        through_revision=state.revision,
        state_hash="abc",
        snapshot={"pending": []},
    )

    assert store.projector_snapshot(
        "action-center", projector_version=1, state_hash="abc"
    )["snapshot"] == {"pending": []}
    assert store.projector_snapshot("action-center", projector_version=2) is None
    assert store.projector_snapshot("action-center", maximum_revision=0) is None


def test_human_decision_and_state_transition_commit_atomically(tmp_path):
    store = SQLiteStateStore(tmp_path, clock=lambda: 100)
    initial = store.initialize(project_id="demo", project_type="modeling")
    waiting = store.transition(
        expected_revision=initial.revision,
        event_type="AWAITING_ACTION",
        changes={
            "status": WorkflowStatus.AWAITING_SELECTION,
            "pending_action": {"type": "step3_selection", "gate": "step3"},
        },
    )
    decision = {
        "gate": "step3",
        "selected_option_id": "m1",
        "selected_at": 100,
        "artifact_refs": [
            {"path": "selection/step3_decision.json", "sha256": "abc", "size": 3}
        ],
    }

    resolved = store.resolve_human_decision(
        expected_revision=waiting.revision,
        resolution=decision,
        decision_record=decision,
    )

    assert resolved.status is WorkflowStatus.READY
    assert resolved.pending_action is None
    assert store.decision("step3") == decision
    assert store.events()[-1].payload["decision_recorded"] is True
    assert store.events()[-1].payload[ENVELOPE_KEY]["side_effect_refs"] == decision[
        "artifact_refs"
    ]


def test_execution_pipeline_returns_outcome_without_writing_state(tmp_path):
    class Lifecycle:
        def prepare(self, _context):
            return PrepareResult.prepared()

        def execute(self, _context):
            return ExecutionResult.succeeded(observation="done")

        def validate(self, _context):
            return ValidationResult.valid("result.json")

        def recover(self, _context, _error):  # pragma: no cover
            raise AssertionError

    store = SQLiteStateStore(tmp_path, clock=lambda: 100)
    state = store.initialize(project_id="demo", project_type="modeling")
    definition = StepDefinition(1, "test", 30, 1, step=Lifecycle())
    request = StageExecutionRequest(
        definition,
        StepContext(tmp_path, "demo", 1, 1, 30, state.revision),
    )

    outcome = StageExecutionPipeline(store.now_epoch).run(request)

    assert outcome.disposition == "success"
    assert outcome.execution.metadata["observation"] == "done"
    assert store.load().revision == state.revision


def test_cli_diagnostics_reports_replay_parity(tmp_path, capsys):
    store = SQLiteStateStore(tmp_path, clock=lambda: 100)
    store.initialize(project_id="demo", project_type="modeling")

    assert cli_main(["diagnostics", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["replay"] == {
        "available": True,
        "matches_authoritative_state": True,
        "error": None,
    }
