from __future__ import annotations

import hashlib
import json

from factory_core.domain import ExecutionResult, StepContext, ValidationResult, WorkflowStatus
from factory_core.service import FactoryService
from factory_core.steps.catalog import contract_for
from factory_core.steps.prompt_step import PromptStep
from factory_core.steps.prompting import PromptRenderer
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
    waiting = store.transition(
        expected_revision=state.revision,
        event_type="AWAITING_CONSULTATION_FOR_TEST",
        changes={
            "status": WorkflowStatus.AWAITING_CONSULTATION,
            "pending_action": {
                "type": "human_consultation",
                "gate": "preflight",
            },
        },
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

    prepared = step.prepare(StepContext(project, "demo", 4, 1, 30, 0))

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

    result = step.execute(StepContext(project, "demo", 4, 1, 30, 0))
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

    result = step.execute(StepContext(project, "demo", 4, 1, 30, 0))

    assert result.returncode == 2
    assert result.error_class == "PERMANENT_CONSULTATION_INPUT_DRIFT"
    assert result.metadata["human_review_changed_during_execution"] is True
