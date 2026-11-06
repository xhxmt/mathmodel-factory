"""Phase 3b: `factory action retry-step` is the one formal way to retry a
failed Step with a fresh attempt budget (replacing direct database edits and
one-off events such as FAILED_SUBTASK_RETRY_AUTHORIZED)."""

from __future__ import annotations

import pytest

from factory_core.domain import ExecutionResult, InvalidTransition, ValidationResult, WorkflowStatus
from factory_core.engine import STEP_RETRY_AUTHORIZED, FactoryEngine
from factory_core.stages import STAGE_SCHEDULER_GENERATION
from factory_core.steps.catalog import contract_for
from factory_core.storage import SQLiteStateStore
from tests.test_stage_scheduler import Lifecycle, stage_registry, trust_seeded_reviewer_gate


class InvalidUntilFixed(Lifecycle):
    """Step 14 produces an invalid artifact until the operator fixes the cause."""

    def __init__(self, project):
        super().__init__()
        self.project = project

    def execute(self, context):
        self.calls.append(context.attempt)
        return ExecutionResult.succeeded()

    def validate(self, _context):
        if (self.project / "fixed").exists():
            return ValidationResult.valid()
        return ValidationResult.invalid("abstract missing")


def _failed_project(tmp_path):
    step14 = InvalidUntilFixed(tmp_path)
    registry, _ = stage_registry(overrides={14: step14})
    store = SQLiteStateStore(tmp_path)
    store.initialize(
        project_id="demo", project_type="modeling", last_completed_step=13,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    trust_seeded_reviewer_gate(store, tmp_path)
    engine = FactoryEngine(tmp_path, store=store, registry=registry, sleeper=lambda _s: None)
    state = engine.run(max_steps=1)
    while state.status is not WorkflowStatus.FAILED:
        state = engine.run(max_steps=1)
    return engine, store, step14, state


def test_attempt_budget_exhaustion_fails_with_the_cursor_kept(tmp_path):
    _engine, store, step14, state = _failed_project(tmp_path)

    assert len(step14.calls) == contract_for(14).max_attempts
    assert (state.active_step, state.active_subtask) == (14, "abstract")
    assert state.attempt == contract_for(14).max_attempts
    assert store.events()[-1].type == "STEP_FAILED"


def test_retry_step_resets_the_attempts_and_runs_the_step_again(tmp_path):
    engine, store, step14, failed = _failed_project(tmp_path)
    used = len(step14.calls)

    retried = engine.retry_step(expected_revision=failed.revision, step_id=14, note="abstract template fixed")

    assert retried.status is WorkflowStatus.READY
    assert (retried.active_step, retried.active_subtask, retried.attempt) == (14, "abstract", 0)
    event = store.events()[-1]
    assert event.type == STEP_RETRY_AUTHORIZED
    assert event.payload["previous_attempt"] == failed.attempt
    assert event.payload["failed_error_class"] == "TRANSIENT_ARTIFACT_INVALID"
    assert event.payload["failed_revision"] == failed.revision
    assert event.payload["reason"]["message"] == "abstract template fixed"

    (tmp_path / "fixed").write_text("yes\n", encoding="utf-8")
    state = engine.run(max_steps=1)

    assert step14.calls[used:] == [1]  # a fresh attempt 1, not a recovery
    assert state.last_completed_step >= 14


@pytest.mark.parametrize("note, step, message", [
    (" ", 14, "needs a reason"),
    ("why", 12, "cursor is on Step 14, not Step 12"),
])
def test_retry_step_refuses_a_bad_request(tmp_path, note, step, message):
    engine, _store, _step14, failed = _failed_project(tmp_path)
    with pytest.raises(InvalidTransition, match=message):
        engine.retry_step(expected_revision=failed.revision, step_id=step, note=note)


def test_retry_step_needs_a_failed_project(tmp_path):
    registry, _ = stage_registry()
    store = SQLiteStateStore(tmp_path)
    store.initialize(project_id="demo", project_type="modeling", last_completed_step=13,
                     scheduler_generation=STAGE_SCHEDULER_GENERATION)
    engine = FactoryEngine(tmp_path, store=store, registry=registry)
    with pytest.raises(InvalidTransition, match="only a failed project"):
        engine.retry_step(expected_revision=store.load().revision, step_id=14, note="why")


def test_cli_retry_step_requires_step_and_reason():
    from factory_core import cli

    args = cli.build_parser().parse_args(
        ["action", "retry-step", "demo", "--step", "14", "--reason", "fixed", "--no-start"]
    )
    assert (args.name, args.step, args.reason) == ("retry-step", 14, "fixed")


def test_a_plain_resume_does_not_reset_the_budget(tmp_path):
    engine, store, step14, failed = _failed_project(tmp_path)
    (tmp_path / "fixed").write_text("yes\n", encoding="utf-8")
    used = len(step14.calls)

    engine.resume(expected_revision=failed.revision)
    state = engine.run(max_steps=1)

    assert state.status is WorkflowStatus.FAILED
    assert step14.calls[used:] == []  # recovery retries, then the budget check refuses
    assert store.events()[-1].payload.get("error_class") == "PERMANENT_ATTEMPT_BUDGET_EXHAUSTED"


def test_diagnostics_suggest_the_retry_for_a_failed_step(tmp_path):
    from factory_core.workflow_events import project_runtime_diagnostics

    _engine, store, _step14, failed = _failed_project(tmp_path)
    status = project_runtime_diagnostics(store.events(), failed)["status"]
    assert status["suggested_actions"][0] == "retry_failed_step"
    assert status["current_step"] == 14
