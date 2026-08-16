from __future__ import annotations

import hashlib
import json

import pytest

from factory_core.domain import (
    ExecutionResult,
    InvalidTransition,
    PendingAction,
    PrepareResult,
    StepContext,
    ValidationResult,
    WorkflowStatus,
)
from factory_core.artifacts import artifact_ref
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
from factory_core.stages import STAGE_SCHEDULER_GENERATION
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


def test_reject_edit_reenter_gate_and_approve(tmp_path):
    paper_dir = tmp_path / "paper"
    paper_dir.mkdir()
    paper = paper_dir / "paper.tex"
    paper.write_text("\\begin{document}draft\\end{document}\n", encoding="utf-8")
    store = SQLiteStateStore(tmp_path, clock=lambda: 100)
    initial = store.initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=15,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    action = PendingAction(type="content_freeze_selection", gate="content_freeze")
    request = build_decision_request(
        project_id="demo",
        project_dir=tmp_path,
        requested_revision=initial.revision + 1,
        generation=1,
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
        payload={"action": request.to_dict()},
    )
    rejected = validate_resolution(
        pending,
        {
            "gate": "content_freeze",
            "selected_option_id": "reject_content_freeze",
            "approved": False,
            "reason": "the conclusions still need revision",
        },
    )

    reopened = store.resolve_human_decision(
        expected_revision=waiting.revision,
        resolution=rejected,
        decision_record=rejected,
    )

    assert reopened.status is WorkflowStatus.READY
    assert reopened.pending_action is None
    assert reopened.last_completed_step == 13
    assert reopened.last_completed_stage == 8
    assert all(item["stage_id"] < 9 for item in store.stage_checkpoints())
    history = store.decision_history("content_freeze")
    assert len(history) == 1
    assert history[0]["approved"] is False
    assert project_action_center(store.events())["pending"] == []

    paper.write_text(
        "\\begin{document}repaired conclusions\\end{document}\n",
        encoding="utf-8",
    )
    next_request = build_decision_request(
        project_id="demo",
        project_dir=tmp_path,
        requested_revision=reopened.revision + 1,
        generation=2,
        action=action.to_dict(),
        reason="re-enter release gate after repair",
    )
    next_pending = action.to_dict()
    next_pending["metadata"] = {"human_decision": next_request.to_dict()}
    waiting_again = store.transition(
        expected_revision=reopened.revision,
        event_type="AWAITING_ACTION",
        changes={
            "status": WorkflowStatus.AWAITING_SELECTION,
            "pending_action": next_pending,
        },
        payload={"action": next_request.to_dict()},
    )
    approved = validate_resolution(
        next_pending,
        {
            "gate": "content_freeze",
            "selected_option_id": "approve_content_freeze",
            "approved": True,
            "reason": "repaired content is ready",
        },
    )
    accepted = store.resolve_human_decision(
        expected_revision=waiting_again.revision,
        resolution=approved,
        decision_record=approved,
    )

    assert accepted.status is WorkflowStatus.READY
    assert accepted.pending_action is None
    assert store.decision("content_freeze")["approved"] is True
    assert len(store.decision_history("content_freeze")) == 2
    projection = project_action_center(store.events())
    assert projection["pending"] == []


def test_stale_open_request_can_be_superseded_safely(tmp_path):
    paper = tmp_path / f"{tmp_path.name}_paper.tex"
    paper.write_text("original\n", encoding="utf-8")
    store = SQLiteStateStore(tmp_path, clock=lambda: 100)
    initial = store.initialize(project_id="demo", project_type="modeling")
    action = PendingAction(type="content_freeze_selection", gate="content_freeze")
    request = build_decision_request(
        project_id="demo",
        project_dir=tmp_path,
        requested_revision=initial.revision + 1,
        generation=1,
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
        payload={"action": request.to_dict()},
    )

    with pytest.raises(InvalidTransition, match="still current"):
        store.supersede_pending_decision_request(
            expected_revision=waiting.revision,
            gate="content_freeze",
        )
    assert store.load().revision == waiting.revision

    paper.write_text("repaired\n", encoding="utf-8")

    with pytest.raises(InvalidTransition, match="stale"):
        store.assert_pending_decision_current("content_freeze")

    rebound = store.supersede_pending_decision_request(
        expected_revision=waiting.revision,
        gate="content_freeze",
        reason="content was repaired",
    )

    fresh = rebound.pending_action["metadata"]["human_decision"]
    assert fresh["generation"] == 2
    assert fresh["request_id"] != request.request_id
    assert store.assert_pending_decision_current("content_freeze") == fresh
    requests = store.decision_requests("content_freeze")
    assert [item["status"] for item in requests] == ["superseded", "open"]
    projection = project_action_center(store.events())
    assert [item["request_id"] for item in projection["pending"]] == [
        fresh["request_id"]
    ]
    assert any(
        item["status"] == "superseded"
        and item["request_id"] == request.request_id
        for item in projection["history"]
    )


def test_prior_decision_receipt_survives_next_generation(tmp_path):
    selection = tmp_path / "selection"
    selection.mkdir()
    projection = selection / "step3_decision.json"
    review = tmp_path / "human_review.md"
    projection.write_text('{"selected_option_id":"m1"}\n', encoding="utf-8")
    review.write_text("first review\n", encoding="utf-8")
    store = SQLiteStateStore(tmp_path, clock=lambda: 100)
    store.initialize(project_id="demo", project_type="modeling")

    first = store.record_decision(
        "step3",
        {
            "selected_option_id": "m1",
            "selected_at": 100,
            "artifact_refs": [
                artifact_ref(tmp_path, projection),
                artifact_ref(tmp_path, review),
            ],
        },
    )
    first_ref = first["artifact_refs"][0]
    first_receipt = tmp_path / first_ref["path"]
    first_bytes = first_receipt.read_bytes()

    projection.write_text('{"selected_option_id":"m2"}\n', encoding="utf-8")
    review.write_text("second review\n", encoding="utf-8")
    second = store.record_decision(
        "step3",
        {
            "selected_option_id": "m2",
            "selected_at": 101,
            "artifact_refs": [
                artifact_ref(tmp_path, projection),
                artifact_ref(tmp_path, review),
            ],
        },
    )

    assert second["generation"] == 2
    assert first_receipt.read_bytes() == first_bytes
    assert hashlib.sha256(first_bytes).hexdigest() == first_ref["sha256"]
    history = store.decision_history("step3")
    assert history[0]["artifact_refs"] == [first_ref]
    assert history[0]["projection_refs"][0]["path"] == projection.relative_to(
        tmp_path
    ).as_posix()
    assert history[1]["artifact_refs"][0]["path"] != first_ref["path"]


def test_bound_decision_rejects_changed_subject_and_wrong_request_identity(tmp_path):
    paper = tmp_path / f"{tmp_path.name}_paper.tex"
    paper.write_text("original\n", encoding="utf-8")
    store = SQLiteStateStore(tmp_path, clock=lambda: 100)
    initial = store.initialize(project_id="demo", project_type="modeling")
    action = PendingAction(type="content_freeze_selection", gate="content_freeze")
    request = build_decision_request(
        project_id="demo",
        project_dir=tmp_path,
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
    )
    with pytest.raises(InvalidTransition, match="request id"):
        store.resolve_human_decision(
            expected_revision=waiting.revision,
            resolution={
                "gate": "content_freeze",
                "request_id": "wrong-request",
                "approved": True,
            },
        )

    paper.write_text("changed after review request\n", encoding="utf-8")
    with pytest.raises(InvalidTransition, match="bound evidence changed"):
        store.resolve_human_decision(
            expected_revision=waiting.revision,
            resolution={
                "gate": "content_freeze",
                "request_id": request.request_id,
                "approved": True,
            },
        )
    assert store.load().revision == waiting.revision
    assert store.decision_history("content_freeze") == []


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
    persisted = store.decision("step3")
    assert persisted is not None
    assert persisted["projection_refs"] == decision["artifact_refs"]
    assert persisted["artifact_refs"][0]["path"].startswith(
        ".factory/decisions/step3/"
    )
    receipt = tmp_path / persisted["artifact_refs"][0]["path"]
    assert receipt.is_file()
    assert persisted["request_id"]
    assert persisted["decision_id"]
    assert persisted["generation"] == 1
    assert store.events()[-1].payload["decision_recorded"] is True
    assert store.events()[-1].payload[ENVELOPE_KEY]["side_effect_refs"] == persisted[
        "artifact_refs"
    ]


def test_completion_event_keeps_completed_subject_and_records_next_result(tmp_path):
    store = SQLiteStateStore(tmp_path, clock=lambda: 100)
    initial = store.initialize(project_id="demo", project_type="modeling")
    started = store.transition(
        expected_revision=initial.revision,
        event_type="STAGE_TASK_STARTED",
        changes={
            "active_stage": 2,
            "active_subtask": "solve",
            "source_step_id": 5,
        },
    )
    store.transition(
        expected_revision=started.revision,
        event_type="STAGE_TASK_COMPLETED",
        changes={
            "active_stage": 3,
            "active_subtask": "write",
            "source_step_id": 6,
        },
    )

    envelope = store.events()[-1].payload[ENVELOPE_KEY]
    assert (
        envelope["subject_stage_id"],
        envelope["subject_subtask"],
        envelope["subject_source_step_id"],
    ) == (2, "solve", 5)
    assert (
        envelope["result_stage_id"],
        envelope["result_subtask"],
        envelope["result_source_step_id"],
    ) == (3, "write", 6)


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
    initial = store.initialize(project_id="demo", project_type="modeling")
    store.transition(
        expected_revision=initial.revision,
        event_type="DOMAIN_ROOT_ESTABLISHED_FOR_TEST",
        changes={},
    )

    assert cli_main(["diagnostics", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["replay"] == {
        "available": True,
        "matches_authoritative_state": True,
        "error": None,
    }


def test_aggregate_domain_root_detects_side_table_divergence(tmp_path):
    store = SQLiteStateStore(tmp_path, clock=lambda: 100)
    initial = store.initialize(project_id="demo", project_type="modeling")
    store.transition(
        expected_revision=initial.revision,
        event_type="DOMAIN_ROOT_ESTABLISHED_FOR_TEST",
        changes={},
    )
    assert store.verify_aggregate_domain_root() is True

    with store._session() as connection:
        connection.execute(
            """
            INSERT INTO dirty_flags(
                flag, owner_stage, cause_revision, cause_artifact,
                baseline_fingerprint, current_fingerprint,
                classifier_contract_sha256
            ) VALUES ('MATH_DIRTY', 8, 1, 'paper/paper.tex', ?, ?, ?)
            """,
            ("a" * 64, "b" * 64, "c" * 64),
        )

    assert store.verify_aggregate_domain_root() is False


def test_aggregate_domain_root_detects_contest_policy_divergence(tmp_path):
    store = SQLiteStateStore(tmp_path, clock=lambda: 100)
    state = store.initialize(
        project_id="demo",
        project_type="modeling",
        contest_policy={
            "profile": "contest_core_v1",
            "contest_started_at": 100,
            "contest_deadline_at": 1000,
            "content_freeze_at": 800,
            "delivery_freeze_at": 900,
            "delivery_reserve_seconds": 200,
        },
    )
    store.transition(
        expected_revision=state.revision,
        event_type="DOMAIN_ROOT_ESTABLISHED_FOR_TEST",
        changes={},
    )
    assert store.verify_aggregate_domain_root() is True

    with store._session() as connection:
        connection.execute(
            "UPDATE contest_policy SET profile='tampered' WHERE singleton=1"
        )

    assert store.verify_aggregate_domain_root() is False


def test_aggregate_domain_root_detects_project_config_divergence(tmp_path):
    store = SQLiteStateStore(tmp_path, clock=lambda: 100)
    state = store.initialize(project_id="demo", project_type="modeling")
    configured = store.configure_solver_policy(
        expected_revision=state.revision,
        mode="local",
        threshold_seconds=300,
        allowed_runtimes=["python"],
    )
    store.transition(
        expected_revision=configured.revision,
        event_type="DOMAIN_ROOT_ESTABLISHED_FOR_TEST",
        changes={},
    )
    assert store.verify_aggregate_domain_root() is True

    with store._session() as connection:
        connection.execute(
            "UPDATE project_config SET solver_mode='cloud' WHERE singleton=1"
        )

    assert store.verify_aggregate_domain_root() is False


def _record_content_freeze_approval(tmp_path):
    paper = tmp_path / f"{tmp_path.name}_paper.tex"
    paper.write_text("\\begin{document}approved\\end{document}\n", encoding="utf-8")
    store = SQLiteStateStore(tmp_path, clock=lambda: 100)
    store.initialize(project_id="demo", project_type="modeling")
    decision = store.record_decision(
        "content_freeze",
        {
            "selected_option_id": "approve_content_freeze",
            "approved": True,
            "selected_at": 100,
        },
    )
    return store, decision


def test_deleted_content_freeze_receipt_blocks_gate(tmp_path):
    store, decision = _record_content_freeze_approval(tmp_path)
    receipt = tmp_path / decision["artifact_refs"][0]["path"]
    receipt.unlink()

    assert store.decision("content_freeze") is None
    verification = store.decision_history("content_freeze")[0][
        "receipt_verification"
    ]
    assert verification["valid"] is False
    assert any("missing" in error for error in verification["errors"])


def test_tampered_content_freeze_receipt_blocks_gate(tmp_path):
    store, decision = _record_content_freeze_approval(tmp_path)
    receipt = tmp_path / decision["artifact_refs"][0]["path"]
    receipt.write_text('{"approved":false}\n', encoding="utf-8")

    assert store.decision("content_freeze") is None
    verification = store.decision_history("content_freeze")[0][
        "receipt_verification"
    ]
    assert "decision receipt SHA-256 mismatch" in verification["errors"]


def test_receipt_request_identity_must_match_database(tmp_path):
    from factory_core.decision_receipts import verify_decision_receipt

    _store, decision = _record_content_freeze_approval(tmp_path)
    receipt = tmp_path / decision["artifact_refs"][0]["path"]
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["request_id"] = "wrong-request"
    receipt.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    rebound = {
        **decision,
        "artifact_refs": [artifact_ref(tmp_path, receipt)],
    }

    verification = verify_decision_receipt(tmp_path, rebound)

    assert verification.valid is False
    assert "decision receipt request_id mismatch" in verification.errors


def test_receipt_symlink_is_rejected(tmp_path):
    store, decision = _record_content_freeze_approval(tmp_path)
    receipt = tmp_path / decision["artifact_refs"][0]["path"]
    target = receipt.with_suffix(".saved.json")
    receipt.rename(target)
    receipt.symlink_to(target)

    assert store.decision("content_freeze") is None
    verification = store.decision_history("content_freeze")[0][
        "receipt_verification"
    ]
    assert any("symlink" in error for error in verification["errors"])
