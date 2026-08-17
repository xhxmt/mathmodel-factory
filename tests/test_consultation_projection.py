from __future__ import annotations

import hashlib
import json

import pytest

from factory_core.domain import (
    ExecutionResult, InvalidTransition, RecoveryDisposition, RevisionConflict,
    StepContext, StepError,
    ValidationResult, WorkflowStatus,
)
from factory_core.service import FactoryService
from factory_core.consultation_projection import (
    current_consultation_decision, stage_consultation_answer,
    consultation_staging_path, ensure_consultation_projection,
)
from factory_core.human_decisions import build_decision_request, decision_fingerprints
from factory_core.steps.catalog import contract_for
from factory_core.steps.prompt_step import PromptStep
from factory_core.steps.prompting import PromptRenderer
from factory_core.steps.gates import prepare_human_gates
from factory_core.storage import SQLiteStateStore


class AlwaysValid:
    @staticmethod
    def validate(_context):
        return ValidationResult.valid()


class RecordingDispatcher:
    def __init__(self):
        self.requests = []

    def execute(self, request, **_kwargs):
        self.requests.append(request)
        return ExecutionResult.succeeded(model_id="test")


def _resolve_cli_consultation(tmp_path, answer="Use robust plan A exactly."):
    root = tmp_path / "factory"
    service = FactoryService(root)
    state, _ = service.create_project("demo", "question", start=False)
    project = root / "ongoing" / "demo"
    store = SQLiteStateStore(project)
    request_path = project / "consultation" / "preflight_request.md"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text("preflight question\n", encoding="utf-8")
    request = build_decision_request(
        project_id="demo",
        project_dir=project,
        requested_revision=state.revision + 1,
        generation=1,
        action={"type": "human_consultation", "gate": "preflight"},
        reason="preflight question",
        evidence=("consultation/preflight_request.md",),
    )
    waiting = store.transition(
        expected_revision=state.revision,
        event_type="AWAITING_CONSULTATION_FOR_TEST",
        changes={
            "status": WorkflowStatus.AWAITING_CONSULTATION,
            "pending_action": {
                "type": "human_consultation",
                "gate": "preflight",
                "metadata": {"human_decision": request.to_dict()},
            },
        },
        payload={"action": request.to_dict()},
    )
    (project / "human_review.md").write_text(
        "# 人工审核与介入记录\n\n"
        "## CONSULT preflight (Step 1) — STATUS: READY\n"
        "咨询点：Preflight\n"
        "提交时间: now\n\n"
        f"{answer}\n",
        encoding="utf-8",
    )
    service.resume(project, expected_revision=waiting.revision)
    return root, project, store


def _prompt_step(root, dispatcher):
    (root / "prompts").mkdir(parents=True, exist_ok=True)
    (root / "prompts" / "step4_model_construction.txt").write_text(
        "Build the model for __BASE_NAME__.\n", encoding="utf-8"
    )
    return PromptStep(
        contract_for(4),
        PromptRenderer(root),
        dispatcher,
        AlwaysValid(),
    )


def test_cli_consultation_receipt_contains_exact_answer(tmp_path):
    _root, _project, store = _resolve_cli_consultation(
        tmp_path, "Line one.\n\nLine two with $x=2$."
    )

    decision = store.decision_history("preflight")[-1]

    assert decision["answer"] == "Line one.\n\nLine two with $x=2$."
    assert decision["receipt_verification"]["valid"] is True


def test_consultation_decision_rebuilds_human_review_section(tmp_path):
    _root, project, store = _resolve_cli_consultation(tmp_path)
    decision = store.decision_history("preflight")[-1]
    review = (project / "human_review.md").read_text(encoding="utf-8")

    assert "FACTORY_CONSULTATION_preflight_START" in review
    assert f"REQUEST_ID: {decision['request_id']}" in review
    assert f"DECISION_ID: {decision['decision_id']}" in review
    assert "SOURCE_OF_TRUTH: .factory/state.db" in review
    assert "Use robust plan A exactly." in review


def test_consultation_projection_tamper_blocks_agent(tmp_path):
    root, project, _store = _resolve_cli_consultation(tmp_path)
    review = project / "human_review.md"
    review.write_text(
        review.read_text(encoding="utf-8").replace("plan A", "plan B"),
        encoding="utf-8",
    )
    step = _prompt_step(root, RecordingDispatcher())

    prepared = step.prepare(StepContext(
        project, "demo", 4, 1, 30, SQLiteStateStore(project).load().revision
    ))

    assert prepared.ready is False
    assert "Consultation projection drift" in prepared.reason


def test_effective_prompt_binds_consultation_answer(tmp_path):
    root, project, _store = _resolve_cli_consultation(tmp_path)
    (root / "web").mkdir(parents=True)
    (root / "web" / "notes.json").write_text(
        json.dumps({"demo": {"step_4": "Researcher note 42"}}),
        encoding="utf-8",
    )
    dispatcher = RecordingDispatcher()
    step = _prompt_step(root, dispatcher)

    result = step.execute(StepContext(
        project, "demo", 4, 1, 30, SQLiteStateStore(project).load().revision
    ))
    prompt = dispatcher.requests[-1].prompt

    assert "Use robust plan A exactly." in prompt
    assert "Researcher note 42" in prompt
    assert result.metadata["effective_prompt_sha256"] == hashlib.sha256(
        prompt.encode("utf-8")
    ).hexdigest()
    assert result.metadata["consultation_decision_ids"]
    assert result.metadata["researcher_note_sha256"] == hashlib.sha256(
        b"Researcher note 42"
    ).hexdigest()


def test_post_decision_human_review_edit_invalidates_execution(tmp_path):
    root, project, _store = _resolve_cli_consultation(tmp_path)
    review = project / "human_review.md"

    class MutatingDispatcher(RecordingDispatcher):
        def execute(self, request, **kwargs):
            result = super().execute(request, **kwargs)
            review.write_text(
                review.read_text(encoding="utf-8").replace("plan A", "plan B"),
                encoding="utf-8",
            )
            return result

    dispatcher = MutatingDispatcher()
    step = _prompt_step(root, dispatcher)

    result = step.execute(StepContext(
        project, "demo", 4, 1, 30, SQLiteStateStore(project).load().revision
    ))

    assert result.returncode == 2
    assert result.error_class == "PERMANENT_CONSULTATION_INPUT_DRIFT"
    assert result.metadata["human_review_changed_during_execution"] is True



def test_prompt_input_receipt_is_durable_before_dispatch(tmp_path):
    root, project, _store = _resolve_cli_consultation(tmp_path)

    class ReceiptAwareDispatcher(RecordingDispatcher):
        def execute(self, request, **kwargs):
            store = SQLiteStateStore(project)
            receipts = store.prompt_attempt_inputs()
            assert len(receipts) == 1
            assert receipts[0]["schema_version"] == "factory-effective-prompt-v1"
            assert receipts[0]["effective_prompt_sha256"] == hashlib.sha256(
                request.prompt.encode("utf-8")
            ).hexdigest()
            assert store.events()[-1].type == "PROMPT_INPUT_BOUND"
            return super().execute(request, **kwargs)

    step = _prompt_step(root, ReceiptAwareDispatcher())
    context = StepContext(
        project, "demo", 4, 1, 30, SQLiteStateStore(project).load().revision
    )

    result = step.execute(context)

    assert result.returncode == 0
    assert result.metadata["prompt_input_receipt_id"]
    assert result.metadata["prompt_template_sha256"]
    assert result.metadata["model_config_sha256"]


def test_prompt_recovery_refuses_complete_without_input_receipt(tmp_path):
    root, project, store = _resolve_cli_consultation(tmp_path)
    state = store.load()
    active = store.transition(
        expected_revision=state.revision,
        event_type="INTERRUPTED_PROMPT_WITHOUT_RECEIPT",
        changes={"active_step": 4, "attempt": 1, "status": WorkflowStatus.INTERRUPTED},
    )
    step = _prompt_step(root, RecordingDispatcher())
    context = StepContext(project, "demo", 4, 1, 30, active.revision)

    decision = step.recover(context, StepError("TRANSIENT_INTERRUPTION"))

    assert decision.disposition is RecoveryDisposition.RETRY
    assert decision.metadata["prompt_input_receipt_valid"] is False


def test_prompt_interruption_after_receipt_can_recover_only_same_inputs(tmp_path):
    root, project, store = _resolve_cli_consultation(tmp_path)

    class InterruptAfterBind(RecordingDispatcher):
        def execute(self, request, **kwargs):
            assert SQLiteStateStore(project).prompt_attempt_inputs()
            raise RuntimeError("simulated process interruption")

    step = _prompt_step(root, InterruptAfterBind())
    context = StepContext(
        project, "demo", 4, 1, 30, SQLiteStateStore(project).load().revision
    )
    try:
        step.execute(context)
    except RuntimeError as exc:
        assert "simulated process interruption" in str(exc)
    state = store.load()
    active = store.transition(
        expected_revision=state.revision,
        event_type="MARK_INTERRUPTED_AFTER_PROMPT_BIND",
        changes={"active_step": 4, "attempt": 1, "status": WorkflowStatus.INTERRUPTED},
    )

    decision = step.recover(
        StepContext(project, "demo", 4, 1, 30, active.revision),
        StepError("TRANSIENT_INTERRUPTION"),
    )

    assert decision.disposition is RecoveryDisposition.COMPLETE
    assert decision.metadata["prompt_input_receipt_valid"] is True
    assert decision.metadata["prompt_input_receipt_id"]


def test_prompt_recovery_detects_researcher_note_change(tmp_path):
    root, project, store = _resolve_cli_consultation(tmp_path)
    (root / "web").mkdir(parents=True, exist_ok=True)
    notes = root / "web" / "notes.json"
    notes.write_text(json.dumps({"demo": {"step_4": "note one"}}), encoding="utf-8")
    step = _prompt_step(root, RecordingDispatcher())
    context = StepContext(
        project, "demo", 4, 1, 30, SQLiteStateStore(project).load().revision
    )
    result = step.execute(context)
    assert result.returncode == 0
    notes.write_text(json.dumps({"demo": {"step_4": "note two"}}), encoding="utf-8")
    state = store.load()
    active = store.transition(
        expected_revision=state.revision,
        event_type="INTERRUPTED_AFTER_NOTE_CHANGE",
        changes={"active_step": 4, "attempt": 1, "status": WorkflowStatus.INTERRUPTED},
    )

    decision = step.recover(
        StepContext(project, "demo", 4, 1, 30, active.revision),
        StepError("TRANSIENT_INTERRUPTION"),
    )

    assert decision.disposition is RecoveryDisposition.RETRY
    assert any("researcher_note_sha256" in item for item in decision.metadata["prompt_input_errors"])


def test_prompt_recovery_detects_consultation_projection_drift(tmp_path):
    root, project, store = _resolve_cli_consultation(tmp_path)
    step = _prompt_step(root, RecordingDispatcher())
    context = StepContext(
        project, "demo", 4, 1, 30, SQLiteStateStore(project).load().revision
    )
    assert step.execute(context).returncode == 0
    review = project / "human_review.md"
    review.write_text(
        review.read_text(encoding="utf-8").replace("plan A", "plan B"),
        encoding="utf-8",
    )
    state = store.load()
    active = store.transition(
        expected_revision=state.revision,
        event_type="INTERRUPTED_AFTER_CONSULTATION_DRIFT",
        changes={"active_step": 4, "attempt": 1, "status": WorkflowStatus.INTERRUPTED},
    )

    decision = step.recover(
        StepContext(project, "demo", 4, 1, 30, active.revision),
        StepError("TRANSIENT_INTERRUPTION"),
    )

    assert decision.disposition is RecoveryDisposition.RETRY



def test_ready_consultation_without_sqlite_decision_still_awaits(tmp_path):
    root = tmp_path / "factory"
    service = FactoryService(root)
    service.create_project("demo", "question", consult=True, start=False)
    project = root / "ongoing" / "demo"
    (project / "human_review.md").write_text(
        "## CONSULT preflight (Step 1) — STATUS: READY\nUse mutable plan A.\n",
        encoding="utf-8",
    )

    prepared = prepare_human_gates(project, 1)

    assert prepared.ready is False
    assert prepared.pending_action is not None
    assert prepared.pending_action.gate == "preflight"
    assert SQLiteStateStore(project).decision("preflight") is None


def test_migrated_ready_consultation_remains_pending_until_explicit_import(tmp_path):
    root = tmp_path / "factory"
    service = FactoryService(root)
    service.create_project("demo", "question", consult=True, start=False)
    project = root / "ongoing" / "demo"
    review = project / "human_review.md"
    review.write_text(
        "# Imported legacy review\n\n"
        "## CONSULT step4 (Step 4) — STATUS: READY\n"
        "Use the imported robust model.\n",
        encoding="utf-8",
    )

    first = prepare_human_gates(project, 4)
    second = prepare_human_gates(project, 4)

    assert first.pending_action is not None
    assert second.pending_action is not None
    assert SQLiteStateStore(project).decision("step4") is None
    assert "Use the imported robust model." in review.read_text(encoding="utf-8")


def test_consultation_symlink_cannot_satisfy_gate(tmp_path):
    root = tmp_path / "factory"
    service = FactoryService(root)
    service.create_project("demo", "question", consult=True, start=False)
    project = root / "ongoing" / "demo"
    target = tmp_path / "foreign_review.md"
    target.write_text(
        "## CONSULT preflight — STATUS: READY\nforeign answer\n",
        encoding="utf-8",
    )
    (project / "human_review.md").symlink_to(target)

    prepared = prepare_human_gates(project, 1)

    assert prepared.ready is False
    assert "symlink" in prepared.reason.lower()


def test_consultation_request_symlink_fails_closed(tmp_path):
    root = tmp_path / "factory"
    service = FactoryService(root)
    service.create_project("demo", "question", consult=True, start=False)
    project = root / "ongoing" / "demo"
    request = project / "consultation" / "preflight_request.md"
    target = tmp_path / "foreign_request.md"
    target.write_text("foreign\n", encoding="utf-8")
    request.symlink_to(target)

    prepared = prepare_human_gates(project, 1)

    assert prepared.ready is False
    assert "symlink" in prepared.reason.lower()


def test_dynamic_ready_text_without_matching_request_cannot_bypass_gate(tmp_path):
    root = tmp_path / "factory"
    service = FactoryService(root)
    service.create_project("demo", "question", consult=True, start=False)
    project = root / "ongoing" / "demo"
    (project / "consultation" / "REQUEST.md").write_text(
        "CONSULT: choose a load-bearing branch\n", encoding="utf-8"
    )
    (project / "human_review.md").write_text(
        "## CONSULT dynamic — STATUS: READY\nmutable answer\n",
        encoding="utf-8",
    )

    prepared = prepare_human_gates(project, 5)

    assert prepared.ready is False
    assert prepared.pending_action is not None
    assert prepared.pending_action.gate == "dynamic"
    assert SQLiteStateStore(project).decision("dynamic") is None



def test_preflight_consultation_subject_changes_with_problem_contract(tmp_path):
    project = tmp_path
    problem = project / "problem"
    problem.mkdir()
    (problem / "problem_brief.md").write_text("brief v1\n", encoding="utf-8")
    (problem / "problem_plan.json").write_text('{"version":1}\n', encoding="utf-8")
    (problem / "data_inventory.md").write_text("data v1\n", encoding="utf-8")
    (problem / "feasibility_constraints.md").write_text("time v1\n", encoding="utf-8")

    before = decision_fingerprints(project, "preflight")
    (problem / "problem_plan.json").write_text('{"version":2}\n', encoding="utf-8")
    after = decision_fingerprints(project, "preflight")

    assert before[0] != after[0]


def test_step4_consultation_subject_binds_step3_projection_and_candidate(tmp_path):
    project = tmp_path
    problem = project / "problem"
    problem.mkdir()
    (problem / "problem_plan.json").write_text('{"version":1}\n', encoding="utf-8")
    (problem / "feasibility_constraints.md").write_text("time v1\n", encoding="utf-8")
    (project / "chosen_method.md").write_text("PRIMARY: m1\n", encoding="utf-8")
    (project / "method_decision.md").write_text("choose m1\n", encoding="utf-8")

    before = decision_fingerprints(project, "step4")
    (project / "chosen_method.md").write_text("PRIMARY: m2\n", encoding="utf-8")
    after = decision_fingerprints(project, "step4")

    assert before[0] != after[0]


def test_dynamic_consultation_subject_binds_request_and_stage_identity(tmp_path):
    service = FactoryService(tmp_path)
    state, _ = service.create_project("demo", "question", start=False)
    project = tmp_path / "ongoing" / "demo"
    request = project / "consultation" / "REQUEST.md"
    request.parent.mkdir(parents=True, exist_ok=True)
    request.write_text("CONSULT: branch A or B\n", encoding="utf-8")
    before = decision_fingerprints(project, "dynamic")
    request.write_text("CONSULT: branch A, B, or C\n", encoding="utf-8")
    after_request = decision_fingerprints(project, "dynamic")
    assert before[0] != after_request[0]

    store = SQLiteStateStore(project)
    manifest = {"problem/problem_brief.md": "a" * 64}
    store.transition(
        expected_revision=store.load().revision,
        event_type="DYNAMIC_SUBJECT_STAGE_CURSOR",
        changes={
            "active_step": 4,
            "active_stage": 3,
            "active_subtask": "model_construction",
        },
        subtask_baseline={
            "stage_id": 3,
            "subtask": "model_construction",
            "source_step_id": 4,
            "input_fingerprint": "b" * 64,
            "manifest": manifest,
        },
    )
    after_cursor = decision_fingerprints(project, "dynamic")
    assert after_request[0] != after_cursor[0]


def test_subject_drift_makes_old_consultation_decision_non_current(tmp_path):
    root, project, store = _resolve_cli_consultation(tmp_path)
    assert current_consultation_decision(project, "preflight") is not None
    problem = project / "problem"
    problem.mkdir(exist_ok=True)
    (problem / "problem_plan.json").write_text('{"changed":true}\n', encoding="utf-8")

    assert current_consultation_decision(project, "preflight") is None
    (project / "consultation" / "enabled").write_text("preflight\n", encoding="utf-8")
    prepared = prepare_human_gates(project, 1)
    assert prepared.pending_action is not None
    assert prepared.pending_action.gate == "preflight"


def test_projection_failure_after_decision_commit_is_recorded_and_rebuildable(tmp_path, monkeypatch):
    root = tmp_path / "factory"
    service = FactoryService(root)
    state, _ = service.create_project("demo", "question", consult=True, start=False)
    project = root / "ongoing" / "demo"
    store = SQLiteStateStore(project)
    request_path = project / "consultation" / "preflight_request.md"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text("question\n", encoding="utf-8")
    request = build_decision_request(
        project_id="demo",
        project_dir=project,
        requested_revision=state.revision + 1,
        generation=1,
        action={"type": "human_consultation", "gate": "preflight"},
        reason="need preflight answer",
        evidence=("consultation/preflight_request.md",),
    )
    waiting = store.transition(
        expected_revision=state.revision,
        event_type="AWAITING_CONSULTATION_FOR_FAULT",
        changes={
            "status": WorkflowStatus.AWAITING_CONSULTATION,
            "pending_action": {
                "type": "human_consultation",
                "gate": "preflight",
                "metadata": {"human_decision": request.to_dict()},
            },
        },
        payload={"action": request.to_dict()},
    )
    (project / "human_review.md").write_text(
        "## CONSULT preflight — STATUS: READY\nUse plan A.\n",
        encoding="utf-8",
    )
    import factory_core.consultation_projection as projection_module
    original = projection_module.atomic_write_text

    def fail_review(path, content, **kwargs):
        if path.name == "human_review.md":
            raise OSError("simulated disk full")
        return original(path, content, **kwargs)

    monkeypatch.setattr(projection_module, "atomic_write_text", fail_review)
    with pytest.raises((OSError, InvalidTransition), match="simulated disk full|rebuild failed"):
        service.resume(project, expected_revision=waiting.revision)

    decision = store.decision("preflight")
    assert decision is not None
    assert store.load().pending_action is None
    assert store.projection_failures(pending_only=True)
    assert consultation_staging_path(
        project, str(decision["request_id"])
    ).is_file()

    monkeypatch.setattr(projection_module, "atomic_write_text", original)
    ensure_consultation_projection(project, "preflight")
    assert "SOURCE_OF_TRUTH: .factory/state.db" in (
        project / "human_review.md"
    ).read_text(encoding="utf-8")


def test_stale_request_concurrency_leaves_only_request_scoped_staging(tmp_path):
    service = FactoryService(tmp_path)
    state, _ = service.create_project("demo", "question", consult=True, start=False)
    project = tmp_path / "ongoing" / "demo"
    store = SQLiteStateStore(project)
    request_path = project / "consultation" / "preflight_request.md"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text("question v1\n", encoding="utf-8")
    request = build_decision_request(
        project_id="demo",
        project_dir=project,
        requested_revision=state.revision + 1,
        generation=1,
        action={"type": "human_consultation", "gate": "preflight"},
        reason="question v1",
        evidence=("consultation/preflight_request.md",),
    )
    waiting = store.transition(
        expected_revision=state.revision,
        event_type="AWAITING_STALE_CONSULTATION",
        changes={
            "status": WorkflowStatus.AWAITING_CONSULTATION,
            "pending_action": {
                "type": "human_consultation",
                "gate": "preflight",
                "metadata": {"human_decision": request.to_dict()},
            },
        },
        payload={"action": request.to_dict()},
    )
    staged = stage_consultation_answer(
        project_dir=project,
        request_id=request.request_id,
        gate="preflight",
        answer="stale answer",
        step=1,
    )
    request_path.write_text("question v2\n", encoding="utf-8")
    superseded = service.supersede_pending_decision_request(
        project,
        expected_revision=waiting.revision,
        gate="preflight",
        reason="subject changed",
    )

    with pytest.raises((InvalidTransition, RevisionConflict)):
        service.resolve(
            project,
            {
                "gate": "preflight",
                "answer": "stale answer",
                "request_id": request.request_id,
            },
            expected_revision=superseded.revision,
        )

    assert staged.is_file()
    review = project / "human_review.md"
    assert not review.is_file() or "stale answer" not in review.read_text(encoding="utf-8")
    assert consultation_staging_path(project, request.request_id) == staged


def test_prompt_recovery_detects_prompt_template_change(tmp_path):
    root, project, store = _resolve_cli_consultation(tmp_path)
    step = _prompt_step(root, RecordingDispatcher())
    context = StepContext(
        project, "demo", 4, 1, 30, SQLiteStateStore(project).load().revision
    )
    assert step.execute(context).returncode == 0
    (root / "prompts" / "step4_model_construction.txt").write_text(
        "Changed prompt for __BASE_NAME__.\n", encoding="utf-8"
    )
    state = store.load()
    active = store.transition(
        expected_revision=state.revision,
        event_type="INTERRUPTED_AFTER_TEMPLATE_CHANGE",
        changes={"active_step": 4, "attempt": 1, "status": WorkflowStatus.INTERRUPTED},
    )

    decision = step.recover(
        StepContext(project, "demo", 4, 1, 30, active.revision),
        StepError("TRANSIENT_INTERRUPTION"),
    )

    assert decision.disposition is RecoveryDisposition.RETRY
    assert any(
        "prompt_template_sha256" in item
        for item in decision.metadata["prompt_input_errors"]
    )


def test_prompt_recovery_detects_model_config_change(tmp_path):
    root, project, store = _resolve_cli_consultation(tmp_path)
    (root / "web").mkdir(parents=True, exist_ok=True)
    config = root / "web" / "model_config.json"
    config.write_text(json.dumps({"demo": {"step_4": "codex"}}), encoding="utf-8")
    step = _prompt_step(root, RecordingDispatcher())
    context = StepContext(
        project, "demo", 4, 1, 30, SQLiteStateStore(project).load().revision
    )
    assert step.execute(context).returncode == 0
    config.write_text(json.dumps({"demo": {"step_4": "claude"}}), encoding="utf-8")
    state = store.load()
    active = store.transition(
        expected_revision=state.revision,
        event_type="INTERRUPTED_AFTER_MODEL_CONFIG_CHANGE",
        changes={"active_step": 4, "attempt": 1, "status": WorkflowStatus.INTERRUPTED},
    )

    decision = step.recover(
        StepContext(project, "demo", 4, 1, 30, active.revision),
        StepError("TRANSIENT_INTERRUPTION"),
    )

    assert decision.disposition is RecoveryDisposition.RETRY
    assert any(
        "model_config_sha256" in item
        for item in decision.metadata["prompt_input_errors"]
    )


def test_semantic_reopen_crossing_consultation_owner_invalidates_decision(tmp_path):
    _root, project, store = _resolve_cli_consultation(tmp_path)
    assert current_consultation_decision(project, "preflight") is not None
    state = store.load()
    store.transition(
        expected_revision=state.revision,
        event_type="STAGE_SEMANTIC_REOPENED",
        changes={"last_completed_step": -1, "last_completed_stage": 0},
        payload={
            "semantic_owner_stage": 1,
            "resume_after_step": -1,
            "reason": "test reopen crossing preflight owner",
        },
    )

    assert current_consultation_decision(project, "preflight") is None


def test_prompt_recovery_does_not_reuse_prior_cycle_receipt_before_new_bind(tmp_path):
    root, project, store = _resolve_cli_consultation(tmp_path)
    step = _prompt_step(root, RecordingDispatcher())

    state = store.load()
    manifest = {}
    selected = store.transition(
        expected_revision=state.revision,
        event_type="STAGE_SUBTASK_SELECTED_FOR_PROMPT_CYCLE_ONE",
        changes={
            "status": WorkflowStatus.RUNNING,
            "active_stage": 3,
            "active_subtask": "model_construction",
            "source_step_id": 4,
            "active_step": 4,
            "attempt": 0,
        },
        subtask_baseline={
            "stage_id": 3,
            "subtask": "model_construction",
            "source_step_id": 4,
            "input_fingerprint": "cycle-one",
            "manifest": manifest,
        },
    )
    started = store.transition(
        expected_revision=selected.revision,
        event_type="STEP_STARTED",
        changes={"attempt": 1},
        event_step=4,
    )
    assert step.execute(
        StepContext(project, "demo", 4, 1, 30, started.revision)
    ).returncode == 0
    old_receipt = store.prompt_attempt_inputs()[-1]

    reopened = store.transition(
        expected_revision=store.load().revision,
        event_type="SEMANTIC_REOPEN_FOR_PROMPT_CYCLE_TEST",
        changes={
            "status": WorkflowStatus.RUNNING,
            "active_stage": None,
            "active_subtask": None,
            "source_step_id": None,
            "active_step": None,
            "attempt": 0,
        },
        subtask_baseline=None,
    )
    selected_again = store.transition(
        expected_revision=reopened.revision,
        event_type="STAGE_SUBTASK_SELECTED_FOR_PROMPT_CYCLE_TWO",
        changes={
            "status": WorkflowStatus.RUNNING,
            "active_stage": 3,
            "active_subtask": "model_construction",
            "source_step_id": 4,
            "active_step": 4,
            "attempt": 0,
        },
        subtask_baseline={
            "stage_id": 3,
            "subtask": "model_construction",
            "source_step_id": 4,
            "input_fingerprint": "cycle-two",
            "manifest": manifest,
        },
    )
    started_again = store.transition(
        expected_revision=selected_again.revision,
        event_type="STEP_STARTED",
        changes={"attempt": 1, "status": WorkflowStatus.INTERRUPTED},
        event_step=4,
    )

    decision = step.recover(
        StepContext(project, "demo", 4, 1, 30, started_again.revision),
        StepError("TRANSIENT_INTERRUPTION"),
    )

    assert decision.disposition is RecoveryDisposition.RETRY
    assert decision.metadata["prompt_input_receipt_valid"] is False
    assert decision.metadata["prompt_input_attempt_key"] != old_receipt["attempt_key"]


def test_consultation_staging_receipt_is_append_only(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    first = stage_consultation_answer(
        project_dir=project,
        request_id="request-1",
        gate="preflight",
        answer="Use plan A.",
    )
    assert stage_consultation_answer(
        project_dir=project,
        request_id="request-1",
        gate="preflight",
        answer="Use plan A.",
    ) == first

    with pytest.raises(ValueError, match="immutable.*already differs"):
        stage_consultation_answer(
            project_dir=project,
            request_id="request-1",
            gate="preflight",
            answer="Use plan B.",
        )

def test_direct_consultation_resolution_binds_request_scoped_staging(tmp_path):
    service = FactoryService(tmp_path)
    state, _ = service.create_project("demo", "question", consult=True, start=False)
    project = tmp_path / "ongoing" / "demo"
    store = SQLiteStateStore(project)
    request_file = project / "consultation" / "preflight_request.md"
    request_file.parent.mkdir(parents=True, exist_ok=True)
    request_file.write_text("consultation subject\n", encoding="utf-8")
    request = build_decision_request(
        project_id="demo",
        project_dir=project,
        requested_revision=state.revision + 1,
        generation=1,
        action={"type": "human_consultation", "gate": "preflight"},
        reason="need answer",
        evidence=("consultation/preflight_request.md",),
    )
    waiting = store.transition(
        expected_revision=state.revision,
        event_type="AWAITING_DIRECT_CONSULTATION",
        changes={
            "status": WorkflowStatus.AWAITING_CONSULTATION,
            "pending_action": {
                "type": "human_consultation",
                "gate": "preflight",
                "metadata": {"human_decision": request.to_dict()},
            },
        },
        payload={"action": request.to_dict()},
    )

    service.resolve(
        project,
        {
            "gate": "preflight",
            "request_id": request.request_id,
            "answer": "Use plan A.",
        },
        expected_revision=waiting.revision,
    )

    decision = store.decision("preflight")
    assert decision is not None
    staged = consultation_staging_path(project, request.request_id)
    assert staged.is_file()
    assert decision["staging_receipt"] == staged.relative_to(project).as_posix()
    assert any(
        item.get("path") == decision["staging_receipt"]
        for item in decision.get("projection_refs") or ()
    )
