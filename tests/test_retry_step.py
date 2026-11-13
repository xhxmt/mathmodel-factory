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


def test_cli_retry_step_parses_step_and_reason():
    from factory_core import cli

    args = cli.build_parser().parse_args(
        ["action", "retry-step", "demo", "--step", "14", "--reason", "fixed", "--no-start"]
    )
    assert (args.name, args.step, args.reason) == ("retry-step", 14, "fixed")


class _CliService:
    """A FactoryService stand-in that records calls and uses the real engine."""

    calls: list = []
    start_error: Exception | None = None

    def __init__(self, engine, failed):
        self.engine, self.failed = engine, failed

    def inspect(self, _project):
        return self.engine.get_state()

    def retry_step(self, _project, *, step_id, reason, expected_revision=None):
        self.calls.append(("retry", step_id, reason, expected_revision))
        return self.engine.retry_step(
            expected_revision=expected_revision, step_id=step_id, note=reason
        )

    def start(self, _project, *, expected_revision=None):
        self.calls.append(("start", expected_revision))
        if self.start_error is not None:
            raise self.start_error
        from types import SimpleNamespace

        return SimpleNamespace(state=self.engine.get_state())


def _cli_with(monkeypatch, tmp_path, *, start_error=None):
    from factory_core import cli

    engine, _store, _step14, failed = _failed_project(tmp_path)
    service = _CliService(engine, failed)
    service.calls = []
    service.start_error = start_error
    monkeypatch.setattr(cli, "FactoryService", lambda *_a, **_k: service)
    return cli, service, failed


@pytest.mark.parametrize(
    "argv",
    [
        ["--reason", "fixed"],  # no --step
        ["--step", "14"],  # no --reason
        ["--step", "14", "--reason", ""],
    ],
)
def test_cli_retry_step_refuses_without_step_or_reason(monkeypatch, tmp_path, capsys, argv):
    cli, service, failed = _cli_with(monkeypatch, tmp_path)

    code = cli.main(["action", "retry-step", str(tmp_path), *argv, "--no-start"])

    assert code == 1
    assert "retry-step requires --step and --reason" in capsys.readouterr().err
    assert service.calls == []
    assert service.engine.get_state().revision == failed.revision


def test_cli_retry_step_starts_the_runner_at_the_retried_revision(monkeypatch, tmp_path):
    cli, service, failed = _cli_with(monkeypatch, tmp_path)

    code = cli.main(["action", "retry-step", str(tmp_path), "--step", "14", "--reason", "fixed"])

    retried = service.engine.get_state()
    assert code == 0
    assert retried.revision > failed.revision
    assert service.calls == [
        ("retry", 14, "fixed", failed.revision),
        ("start", retried.revision),
    ]


@pytest.mark.parametrize(
    "error",
    [
        "FactoryCoreError",
        # The worker launcher raises RuntimeError when the worker exits
        # before initialization or its readiness identity differs.
        "RuntimeError",
    ],
)
def test_cli_retry_step_names_the_recorded_retry_when_start_fails(monkeypatch, tmp_path, capsys, error):
    from factory_core.domain import FactoryCoreError

    exc_type = {"FactoryCoreError": FactoryCoreError, "RuntimeError": RuntimeError}[error]
    cli, service, failed = _cli_with(
        monkeypatch, tmp_path, start_error=exc_type("runner launch failed")
    )

    code = cli.main(["action", "retry-step", str(tmp_path), "--step", "14", "--reason", "fixed"])

    retried = service.engine.get_state()
    err = capsys.readouterr().err
    assert code == 1
    assert retried.status is WorkflowStatus.READY  # the retry stays committed
    assert f"recorded at revision {retried.revision}" in err
    assert "runner launch failed" in err and "resume the project" in err


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


# --- A retried attempt gets its own immutable prompt-input key -------------


def _prompt_project_at_stage_cursor(tmp_path):
    """A real PromptStep on Step 4 whose Stage cursor baseline is recorded."""

    import json

    from tests.test_consultation_projection import (
        RecordingDispatcher,
        _prompt_step,
        _resolve_cli_consultation,
    )

    root, project, store = _resolve_cli_consultation(tmp_path)
    step = _prompt_step(root, RecordingDispatcher())
    state = store.load()
    store.transition(
        expected_revision=state.revision,
        event_type="STAGE_SUBTASK_SELECTED",
        changes={"active_stage": 3, "active_subtask": "model_construction"},
        subtask_baseline={
            "stage_id": 3,
            "subtask": "model_construction",
            "source_step_id": 4,
            "input_fingerprint": "f" * 64,
            "manifest": {},
        },
    )
    notes = root / "web" / "notes.json"
    notes.parent.mkdir(parents=True, exist_ok=True)

    def set_note(text):
        notes.write_text(json.dumps({"demo": {"step_4": text}}), encoding="utf-8")

    return project, store, step, set_note


def _context(project, store):
    from factory_core.domain import StepContext

    return StepContext(project, "demo", 4, 1, 30, store.load().revision)


def test_retried_attempt_binds_corrected_inputs_under_a_new_key(tmp_path):
    project, store, step, set_note = _prompt_project_at_stage_cursor(tmp_path)
    set_note("first attempt")
    first = step.execute(_context(project, store))
    assert first.returncode == 0

    state = store.load()
    retry = store.transition(
        expected_revision=state.revision,
        event_type=STEP_RETRY_AUTHORIZED,
        changes={"attempt": 0},
        payload={"stage": 3, "subtask": "model_construction", "source_step": 4},
    )
    set_note("write the paths claim_registry.json declares")
    second = step.execute(_context(project, store))

    assert second.returncode == 0, second.error_class
    keys = [item["attempt_key"] for item in store.prompt_attempt_inputs()]
    assert first.metadata["prompt_input_attempt_key"] in keys
    assert second.metadata["prompt_input_attempt_key"] == (
        f"stage:3:model_construction:selected-revision:{retry.revision}:attempt:1"
    )
    assert len(set(keys)) == 2


def test_conflicting_prompt_input_is_recorded_and_the_step_still_runs(tmp_path):
    project, store, step, set_note = _prompt_project_at_stage_cursor(tmp_path)
    set_note("first attempt")
    first = step.execute(_context(project, store))
    assert first.returncode == 0

    set_note("changed without a retry")
    result = step.execute(_context(project, store))

    # The receipt never decides whether the Step runs.  The stored record of
    # the attempt key stays; this run carries its own, unstored receipt.
    assert result.returncode == 0
    assert not result.error_class
    assert "different content" in result.metadata["prompt_input_errors"][0]
    assert result.metadata["prompt_input_receipt_durable"] is False
    assert result.metadata["prompt_input_attempt_key"].endswith(":attempt:1")
    assert result.metadata["researcher_note_sha256"] != first.metadata["researcher_note_sha256"]
    stored = store.prompt_attempt_input(result.metadata["prompt_input_attempt_key"])
    assert stored["receipt_id"] == first.metadata["prompt_input_receipt_id"]
