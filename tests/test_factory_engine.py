from dataclasses import dataclass, field
import os
import subprocess
import sys

import pytest

from factory_core.domain import (
    ExecutionResult,
    InvalidTransition,
    PendingAction,
    PrepareResult,
    RecoveryDecision,
    RecoveryDisposition,
    RunnerBusy,
    RunnerLeaseLost,
    StepContext,
    ValidationResult,
    WorkflowStatus,
)
from factory_core.engine import FactoryEngine
from factory_core.registry import StepDefinition
from factory_core.storage import SQLiteStateStore
from tests._stage_engine import (
    active_step_cursor,
    initialize_stage_project,
    stage_registry_with,
)


@dataclass
class FakeHandler:
    results: list[ExecutionResult]
    calls: list[int] = field(default_factory=list)

    def execute(self, context):
        self.calls.append(context.attempt)
        return self.results.pop(0)


@dataclass
class FakeValidator:
    results: list[ValidationResult]

    def validate(self, context):
        return self.results.pop(0)


def registry_for(handler, validator, *, max_attempts=3):
    """The Stage catalog with Step 1 under test; every other Step succeeds."""

    return stage_registry_with(
        StepDefinition(
            id=1,
            name="first",
            timeout_seconds=30,
            max_attempts=max_attempts,
            handler=handler,
            validator=validator,
        )
    )


def test_engine_retries_through_registry_without_step_number_branches(tmp_path):
    handler = FakeHandler([ExecutionResult.failed("TRANSIENT"), ExecutionResult.succeeded()])
    validator = FakeValidator([ValidationResult.invalid("missing"), ValidationResult.valid()])
    store = SQLiteStateStore(tmp_path)
    initialize_stage_project(store)
    engine = FactoryEngine(tmp_path, store=store, registry=registry_for(handler, validator), sleeper=lambda _: None)

    state = engine.run()

    assert state.status is WorkflowStatus.COMPLETED
    assert state.last_completed_step == 16
    assert handler.calls == [1, 2]
    types = [event.type for event in store.events()]
    assert types[:7] == [
        "PROJECT_CREATED",
        "RUN_STARTED",
        "STAGE_SUBTASK_SELECTED",
        "STEP_STARTED",
        "RETRY_SCHEDULED",
        "STEP_STARTED",
        "STEP_SUCCEEDED",
    ]
    assert types[-1] == "PROJECT_COMPLETED"


def test_engine_failure_events_preserve_execution_and_validation_metadata(tmp_path):
    from factory_core.engine import INFRASTRUCTURE_RETRY_BUDGET

    # A Judge role without a usable verdict is non-content (phase 3c): it
    # retries the Judge's infrastructure, never a content attempt, and ends as
    # PERMANENT_JUDGE_INFRASTRUCTURE once that budget is spent.
    failures = INFRASTRUCTURE_RETRY_BUDGET + 1
    handler = FakeHandler(
        [
            ExecutionResult.failed(
                "TRANSIENT_JUDGE_ROLE",
                returncode=2,
                role="math",
                backend="codex",
            )
            for _ in range(failures)
        ]
    )
    validator = FakeValidator(
        [
            ValidationResult.invalid(
                "judge output missing",
                metadata={"failed_check": "grounding", "missing_artifacts": ["judge_outputs/math.md"]},
            )
            for _ in range(failures)
        ]
    )
    store = SQLiteStateStore(tmp_path)
    initialize_stage_project(store)
    engine = FactoryEngine(
        tmp_path,
        store=store,
        registry=registry_for(handler, validator, max_attempts=2),
        sleeper=lambda _: None,
    )

    state = engine.run()

    assert state.status is WorkflowStatus.FAILED
    assert handler.calls == list(range(1, failures + 1))  # more than max_attempts=2
    retries = [event for event in store.events() if event.type == "RETRY_SCHEDULED"]
    assert len(retries) == INFRASTRUCTURE_RETRY_BUDGET
    failure = store.events()[-1]
    for event in (retries[0], failure):
        assert event.payload["failed_check"] == "grounding"
        assert event.payload["role"] == "math"
        assert event.payload["backend"] == "codex"
        assert event.payload["missing_artifacts"] == ["judge_outputs/math.md"]
    assert failure.payload["error_class"] == "PERMANENT_JUDGE_INFRASTRUCTURE"
    assert failure.payload["exhausted_error_class"] == "TRANSIENT_JUDGE_ROLE"
    assert failure.payload["content_attempts_used"] == 0


def test_judge_infrastructure_classes_share_one_retry_budget(tmp_path):
    from factory_core.engine import INFRASTRUCTURE_RETRY_BUDGET

    # Exactly budget + 1 failures, so every listed class is actually consumed.
    classes = ["TRANSIENT_JUDGE_ROLE", "TRANSIENT_JUDGE_PACKET", "TRANSIENT_JUDGE_INDETERMINATE",
               "TRANSIENT_JUDGE_ROUTING"]
    assert len(classes) == INFRASTRUCTURE_RETRY_BUDGET + 1
    handler = FakeHandler([ExecutionResult.failed(name, returncode=2) for name in classes])
    validator = FakeValidator([ValidationResult.invalid("no usable verdict") for _ in classes])
    store = SQLiteStateStore(tmp_path)
    initialize_stage_project(store)
    engine = FactoryEngine(
        tmp_path, store=store, registry=registry_for(handler, validator, max_attempts=2),
        sleeper=lambda _: None,
    )

    state = engine.run()

    # Mixing classes does not multiply the budget: one terminal class, one budget.
    assert state.status is WorkflowStatus.FAILED
    assert len(handler.calls) == INFRASTRUCTURE_RETRY_BUDGET + 1
    assert store.events()[-1].payload["error_class"] == "PERMANENT_JUDGE_INFRASTRUCTURE"


@pytest.mark.parametrize(
    ("error_class", "content_attempt"),
    [("TRANSIENT_JUDGE_ROUTING", False), ("TRANSIENT_JUDGE_INCOMPLETE", True)],
)
def test_a_single_attempt_step_retries_a_routing_failure_but_not_an_incomplete_pass(
    tmp_path, error_class, content_attempt
):
    # Step 16 has one content attempt.  A decision-router failure routed no
    # verdict, so it retries the Judge's infrastructure; a PASS from a review
    # that never completed is a workflow state and still spends the attempt.
    handler = FakeHandler([ExecutionResult.failed(error_class, returncode=2), ExecutionResult.succeeded()])
    validator = FakeValidator([ValidationResult.invalid("no routed verdict"), ValidationResult.valid()])
    store = SQLiteStateStore(tmp_path)
    initialize_stage_project(store)
    engine = FactoryEngine(
        tmp_path, store=store, registry=registry_for(handler, validator, max_attempts=1),
        sleeper=lambda _: None,
    )

    state = engine.run()

    if content_attempt:
        assert state.status is WorkflowStatus.FAILED
        assert handler.calls == [1]
    else:
        assert state.status is WorkflowStatus.COMPLETED
        assert len(handler.calls) == 2


def test_engine_classifies_exit_zero_with_missing_artifact(tmp_path):
    handler = FakeHandler([ExecutionResult.succeeded(model_id="codex-test")])
    validator = FakeValidator(
        [ValidationResult.invalid("artifact missing", "model.md")]
    )
    store = SQLiteStateStore(tmp_path)
    initialize_stage_project(store)
    engine = FactoryEngine(
        tmp_path,
        store=store,
        registry=registry_for(handler, validator, max_attempts=1),
    )

    state = engine.run()

    assert state.status is WorkflowStatus.FAILED
    payload = store.events()[-1].payload
    assert payload["error_class"] == "TRANSIENT_ARTIFACT_MISSING"
    assert payload["missing_artifacts"] == ["model.md"]
    assert payload["model_id"] == "codex-test"


def test_engine_persists_pending_action_and_stops_dispatch(tmp_path):
    handler = FakeHandler([ExecutionResult.succeeded()])
    validator = FakeValidator(
        [ValidationResult.awaiting(PendingAction(type="step3_selection", gate="step3"))]
    )
    store = SQLiteStateStore(tmp_path)
    initial = initialize_stage_project(store)
    engine = FactoryEngine(tmp_path, store=store, registry=registry_for(handler, validator))

    awaiting = engine.run()

    assert awaiting.status is WorkflowStatus.AWAITING_SELECTION
    assert awaiting.pending_action["type"] == "step3_selection"
    resolved = engine.resolve_action(
        {"selected_option_id": "m1"}, expected_revision=awaiting.revision
    )
    assert resolved.status is WorkflowStatus.READY
    assert resolved.pending_action is None
    assert resolved.revision > initial.revision


def test_engine_rejects_resolution_for_a_different_pending_gate(tmp_path):
    handler = FakeHandler([ExecutionResult.succeeded()])
    validator = FakeValidator(
        [ValidationResult.awaiting(PendingAction(type="step3_selection", gate="step3"))]
    )
    store = SQLiteStateStore(tmp_path)
    initialize_stage_project(store)
    engine = FactoryEngine(tmp_path, store=store, registry=registry_for(handler, validator))
    awaiting = engine.run()

    with pytest.raises(InvalidTransition, match="cannot be resolved"):
        engine.resolve_action(
            {"gate": "content_freeze"}, expected_revision=awaiting.revision
        )

    assert store.load().pending_action["gate"] == "step3"


def test_recovery_promotes_valid_artifacts_after_interrupted_step(tmp_path):
    handler = FakeHandler([])
    validator = FakeValidator([ValidationResult.valid()])
    store = SQLiteStateStore(tmp_path)
    state = initialize_stage_project(store)
    store.transition(
        expected_revision=state.revision,
        event_type="STEP_STARTED",
        changes={"status": WorkflowStatus.RUNNING, **active_step_cursor(1), "attempt": 1},
    )
    engine = FactoryEngine(tmp_path, store=store, registry=registry_for(handler, validator))

    recovered = engine.recover()

    assert recovered.status is WorkflowStatus.READY
    assert recovered.last_completed_step == 1
    # The Stage cursor moves on to the next subtask.
    assert recovered.active_step == 2
    assert store.events()[-1].type == "RECOVERY_DECIDED"
    assert store.events()[-1].payload["decision"] == "promote_valid_stage_subtask"


def test_recovery_preserves_validator_reopen_target(tmp_path):
    handler = FakeHandler([])
    validator = FakeValidator(
        [ValidationResult.invalid("reopen", metadata={"resume_after_step": 0})]
    )
    store = SQLiteStateStore(tmp_path)
    state = initialize_stage_project(store, last_completed_step=0)
    store.transition(
        expected_revision=state.revision,
        event_type="STEP_STARTED",
        changes={"status": WorkflowStatus.RUNNING, **active_step_cursor(1), "attempt": 1},
    )
    engine = FactoryEngine(
        tmp_path, store=store, registry=registry_for(handler, validator)
    )

    recovered = engine.recover()

    assert recovered.last_completed_step == 0
    assert recovered.active_step is None
    assert store.events()[-1].payload["decision"] == "resume_from_reopen"


def test_engine_rejects_second_live_runner(tmp_path):
    handler = FakeHandler([])
    validator = FakeValidator([])
    store = SQLiteStateStore(tmp_path)
    state = initialize_stage_project(store)
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        store.transition(
            expected_revision=state.revision,
            event_type="STEP_STARTED",
            changes={
                "status": WorkflowStatus.RUNNING,
                **active_step_cursor(1),
                "attempt": 1,
                "runner_pid": sleeper.pid,
            },
        )
        engine = FactoryEngine(
            tmp_path, store=store, registry=registry_for(handler, validator)
        )

        with pytest.raises(RunnerBusy):
            engine.run()
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)


def test_resume_rejects_active_runner_state(tmp_path):
    handler = FakeHandler([])
    validator = FakeValidator([])
    store = SQLiteStateStore(tmp_path)
    state = initialize_stage_project(store)
    running = store.transition(
        expected_revision=state.revision,
        event_type="RUN_STARTED",
        changes={"status": WorkflowStatus.RUNNING, "active_step": 1},
    )
    engine = FactoryEngine(
        tmp_path, store=store, registry=registry_for(handler, validator)
    )

    with pytest.raises(InvalidTransition, match="running state cannot be resumed"):
        engine.resume(expected_revision=running.revision)

    assert store.load().revision == running.revision


def test_archive_recovery_finishes_from_archiving_state(tmp_path):
    root = tmp_path
    project = root / "ongoing" / "demo"
    project.mkdir(parents=True)
    store = SQLiteStateStore(project)
    state = initialize_stage_project(store)
    state = store.transition(
        expected_revision=state.revision,
        event_type="PROJECT_COMPLETED",
        changes={"status": WorkflowStatus.COMPLETED, "last_completed_step": 16},
    )
    store.transition(
        expected_revision=state.revision,
        event_type="PROJECT_ARCHIVE_REQUESTED",
        changes={"status": WorkflowStatus.ARCHIVING},
    )
    engine = FactoryEngine(project, store=store)

    archived = engine.archive_completed(root)

    assert archived.status is WorkflowStatus.COMPLETED
    assert archived.storage_scope == "complete"
    assert (root / "complete" / "demo" / ".factory" / "state.db").is_file()
    assert not project.exists()


def test_reopen_result_is_committed_before_artifact_validation(tmp_path):
    handler = FakeHandler(
        [ExecutionResult.succeeded(resume_after_step=0), ExecutionResult.succeeded()]
    )
    validator = FakeValidator([ValidationResult.valid()])
    store = SQLiteStateStore(tmp_path)
    initialize_stage_project(store, last_completed_step=0)
    engine = FactoryEngine(
        tmp_path, store=store, registry=registry_for(handler, validator)
    )

    state = engine.run()

    assert state.status is WorkflowStatus.COMPLETED
    assert handler.calls == [1, 1]
    assert [event.type for event in store.events()].count("STEP_REOPENED") == 1


def test_new_execution_backend_is_registered_without_scheduler_change():
    from factory_core.registry import BackendRegistry

    backend = FakeHandler([ExecutionResult.succeeded()])
    registry = BackendRegistry()

    registry.register("new-cloud-solver", backend)

    assert registry.get("new-cloud-solver") is backend


def test_projection_failure_does_not_rollback_authoritative_transition(tmp_path):
    handler = FakeHandler([ExecutionResult.succeeded()])
    validator = FakeValidator([ValidationResult.valid()])
    store = SQLiteStateStore(tmp_path)
    initialize_stage_project(store)

    def broken_projection(_project, _state):
        raise OSError("read-only projection")

    engine = FactoryEngine(
        tmp_path,
        store=store,
        registry=registry_for(handler, validator),
        projector=broken_projection,
    )

    with pytest.warns(RuntimeWarning, match="state committed"):
        state = engine.run()

    assert state.status is WorkflowStatus.COMPLETED
    assert store.load().status is WorkflowStatus.COMPLETED
    failures = store.projection_failures(pending_only=True)
    assert failures
    assert {failure["error_type"] for failure in failures} == {"OSError"}
    for failure in failures:
        store.resolve_projection_failure(
            revision=failure["revision"],
            projector_name=failure["projector_name"],
        )
    assert store.projection_failures(pending_only=True) == []


def test_engine_dispatches_native_step_lifecycle_without_legacy_handler(tmp_path):
    class NativeStep:
        def __init__(self):
            self.calls = []

        def prepare(self, context):
            self.calls.append("prepare")
            return PrepareResult.prepared("input-ready")

        def execute(self, context):
            self.calls.append("execute")
            return ExecutionResult.succeeded()

        def validate(self, context):
            self.calls.append("validate")
            return ValidationResult.valid("artifact")

        def recover(self, context, error):
            self.calls.append("recover")
            return RecoveryDecision(RecoveryDisposition.RETRY)

    native = NativeStep()
    registry = stage_registry_with(
        StepDefinition(
            id=1,
            name="native",
            timeout_seconds=30,
            max_attempts=1,
            step=native,
        )
    )
    store = SQLiteStateStore(tmp_path)
    initialize_stage_project(store)

    state = FactoryEngine(tmp_path, store=store, registry=registry).run()

    assert state.status is WorkflowStatus.COMPLETED
    assert state.runtime_generation == "native_v2"
    assert native.calls == ["prepare", "execute", "validate"]


class LeaseReplacementStep:
    def __init__(self, store):
        self.store = store

    def prepare(self, _context):
        return PrepareResult.prepared()

    def execute(self, _context):
        current = self.store.load()
        paused = self.store.transition(
            expected_revision=current.revision,
            event_type="PAUSED_BY_TEST",
            changes={
                "status": WorkflowStatus.PAUSED,
                "runner_pid": None,
                "runner_lease_id": None,
                "heartbeat_at": None,
            },
        )
        resumed = self.store.transition(
            expected_revision=paused.revision,
            event_type="RESUMED_BY_TEST",
            changes={
                "status": WorkflowStatus.READY,
                "active_step": None,
                "attempt": 0,
            },
        )
        launched = self.store.transition(
            expected_revision=resumed.revision,
            event_type="WORKER_B_LAUNCHED",
            changes={
                "status": WorkflowStatus.RUNNING,
                "runner_pid": os.getpid(),
                "runner_lease_id": "worker-b-lease",
            },
        )
        self.store.transition(
            expected_revision=launched.revision,
            event_type="WORKER_B_STEP_STARTED",
            changes={**active_step_cursor(1), "attempt": 1},
        )
        return ExecutionResult.succeeded()

    def validate(self, _context):
        raise AssertionError("worker A must lose its lease before validation")

    def recover(self, _context, _error):
        return RecoveryDecision(RecoveryDisposition.RETRY)


def test_old_worker_cannot_commit_after_pause_resume_replaces_lease(tmp_path):
    store = SQLiteStateStore(tmp_path)
    initialize_stage_project(store, last_completed_step=0)
    registry = stage_registry_with(
        StepDefinition(
            id=1,
            name="lease-race",
            timeout_seconds=30,
            max_attempts=1,
            max_reopens=0,
            step=LeaseReplacementStep(store),
        )
    )

    with pytest.raises(RunnerLeaseLost):
        FactoryEngine(tmp_path, store=store, registry=registry).run()

    assert store.events()[-1].type == "WORKER_B_STEP_STARTED"
    assert "STEP_SUCCEEDED" not in [event.type for event in store.events()]


class RecoveryOnlyStep:
    def __init__(self, decision):
        self.decision = decision

    def prepare(self, _context):
        raise AssertionError("recovery stop state must not dispatch")

    def execute(self, _context):
        raise AssertionError("recovery stop state must not execute")

    def validate(self, _context):
        return ValidationResult.invalid("unused")

    def recover(self, _context, _error):
        return self.decision


@pytest.mark.parametrize(
    ("decision", "expected_status"),
    [
        (
            RecoveryDecision(
                RecoveryDisposition.AWAIT,
                pending_action=PendingAction(type="step3_selection", gate="step3"),
            ),
            WorkflowStatus.AWAITING_SELECTION,
        ),
        (RecoveryDecision(RecoveryDisposition.FAIL), WorkflowStatus.FAILED),
    ],
)
def test_run_does_not_overwrite_recovery_stop_states(
    tmp_path, decision, expected_status
):
    store = SQLiteStateStore(tmp_path)
    state = initialize_stage_project(store, last_completed_step=0)
    store.transition(
        expected_revision=state.revision,
        event_type="STEP_STARTED",
        changes={
            "status": WorkflowStatus.RUNNING,
            **active_step_cursor(1),
            "attempt": 1,
            "runner_pid": os.getpid(),
            "runner_lease_id": "launch-lease",
        },
    )
    registry = stage_registry_with(
        StepDefinition(1, "recover", 30, 1, step=RecoveryOnlyStep(decision))
    )

    recovered = FactoryEngine(tmp_path, store=store, registry=registry).run()

    assert recovered.status is expected_status
    assert store.events()[-1].type == "RECOVERY_DECIDED"
    assert store.events()[-1].payload["decision"] in {
        "await_pending_action",
        "fail_recovery",
    }


def test_recovery_reopen_consumes_the_same_budget_as_normal_reopen(tmp_path):
    decision = RecoveryDecision(RecoveryDisposition.REOPEN, resume_after_step=0)
    store = SQLiteStateStore(tmp_path)
    state = initialize_stage_project(store, last_completed_step=0)
    registry = stage_registry_with(
        StepDefinition(
            1,
            "recover-reopen",
            30,
            1,
            max_reopens=1,
            step=RecoveryOnlyStep(decision),
        )
    )
    engine = FactoryEngine(tmp_path, store=store, registry=registry)

    store.transition(
        expected_revision=state.revision,
        event_type="FIRST_CRASH",
        changes={"status": WorkflowStatus.INTERRUPTED, **active_step_cursor(1), "attempt": 1},
    )
    first = engine.recover()
    assert first.status is WorkflowStatus.READY
    assert store.events()[-1].type == "STEP_REOPENED"

    store.transition(
        expected_revision=first.revision,
        event_type="SECOND_CRASH",
        changes={"status": WorkflowStatus.INTERRUPTED, **active_step_cursor(1), "attempt": 1},
    )
    second = engine.recover()

    assert second.status is WorkflowStatus.FAILED
    assert store.events()[-1].type == "STEP_FAILED"
    assert [event.type for event in store.events()].count("STEP_REOPENED") == 1


def test_parallel_prepare_shortage_uses_standard_retry_budget(tmp_path):
    from factory_core.steps.catalog import contract_for
    from factory_core.steps.specialized import ParallelProposalStep

    validator = FakeValidator(
        [
            ValidationResult.invalid("missing streams"),
            ValidationResult.invalid("missing streams"),
        ]
    )
    step = ParallelProposalStep(
        contract_for(2),
        renderer=None,
        dispatcher=None,
        validator=validator,
    )
    registry = stage_registry_with(
        StepDefinition(
            id=2,
            name="parallel",
            timeout_seconds=30,
            max_attempts=2,
            max_reopens=0,
            step=step,
        )
    )
    store = SQLiteStateStore(tmp_path)
    initialize_stage_project(store, last_completed_step=1)

    state = FactoryEngine(
        tmp_path, store=store, registry=registry, sleeper=lambda _delay: None
    ).run()

    assert state.status is WorkflowStatus.FAILED
    assert [event.type for event in store.events()][-3:] == [
        "RETRY_SCHEDULED",
        "STEP_STARTED",
        "STEP_FAILED",
    ]
    assert store.events()[-1].payload["error_class"] == (
        "TRANSIENT_INSUFFICIENT_STREAMS"
    )


# --------------------------------------------------------------------------
# Non-content failures (a checker produced no verdict) and the attempt budget
# --------------------------------------------------------------------------


def _engine_with(tmp_path, results, *, max_attempts):
    handler = FakeHandler(list(results))
    validator = FakeValidator(
        [ValidationResult.valid() if result.returncode == 0 else ValidationResult.invalid("audit") for result in results]
    )
    store = SQLiteStateStore(tmp_path)
    initialize_stage_project(store)
    engine = FactoryEngine(
        tmp_path, store=store, registry=registry_for(handler, validator, max_attempts=max_attempts),
        sleeper=lambda _: None,
    )
    return engine, store, handler


def test_a_checker_without_a_verdict_never_consumes_a_content_attempt(tmp_path):
    undecided = ExecutionResult.failed("TRANSIENT_INCREMENTAL_AUDIT", returncode=75)
    engine, store, handler = _engine_with(
        tmp_path, [undecided, undecided, ExecutionResult.succeeded()], max_attempts=1
    )

    state = engine.run()

    assert state.status is WorkflowStatus.COMPLETED
    assert handler.calls == [1, 2, 3]
    retries = [event for event in store.events() if event.type == "RETRY_SCHEDULED"]
    assert [event.payload["infrastructure_retries"] for event in retries] == [1, 2]
    assert [event.payload["content_attempts_used"] for event in retries] == [0, 0]
    assert store.status_snapshot()["event_replay_valid"]


def test_an_exhausted_infrastructure_budget_is_its_own_terminal_class(tmp_path):
    from factory_core.engine import INFRASTRUCTURE_RETRY_BUDGET

    undecided = ExecutionResult.failed("TRANSIENT_INCREMENTAL_AUDIT", returncode=75)
    engine, store, handler = _engine_with(
        tmp_path, [undecided] * (INFRASTRUCTURE_RETRY_BUDGET + 1), max_attempts=5
    )

    state = engine.run()

    assert state.status is WorkflowStatus.FAILED
    assert len(handler.calls) == INFRASTRUCTURE_RETRY_BUDGET + 1
    failure = store.events()[-1]
    assert failure.type == "STEP_FAILED"
    assert failure.payload["error_class"] == "PERMANENT_AUDIT_INFRASTRUCTURE"
    assert failure.payload["exhausted_error_class"] == "TRANSIENT_INCREMENTAL_AUDIT"
    assert failure.payload["content_attempts_used"] == 0


def test_content_failures_keep_their_budget_between_infrastructure_retries(tmp_path):
    undecided = ExecutionResult.failed("TRANSIENT_INCREMENTAL_AUDIT", returncode=75)
    finding = ExecutionResult.failed("AUDIT_REPAIR_REQUIRED", returncode=1)
    engine, store, handler = _engine_with(
        tmp_path, [undecided, finding, undecided, finding], max_attempts=2
    )

    state = engine.run()

    assert state.status is WorkflowStatus.FAILED
    assert handler.calls == [1, 2, 3, 4]
    failure = store.events()[-1]
    assert failure.payload["error_class"] == "AUDIT_REPAIR_REQUIRED"
    assert failure.payload["content_attempts_used"] == 2
    assert store.status_snapshot()["event_replay_valid"]


def test_a_model_backend_failure_never_consumes_a_content_attempt(tmp_path):
    # canary 1: Step 0 had one attempt, and one codex upstream failure ended the run.
    backend = ExecutionResult.failed("TRANSIENT_MODEL_BACKEND", returncode=1, backend="codex")
    engine, store, handler = _engine_with(
        tmp_path, [backend, backend, ExecutionResult.succeeded()], max_attempts=1
    )

    assert engine.run().status is WorkflowStatus.COMPLETED
    assert handler.calls == [1, 2, 3]
    retries = [event for event in store.events() if event.type == "RETRY_SCHEDULED"]
    assert [event.payload["content_attempts_used"] for event in retries] == [0, 0]


def test_each_infrastructure_class_has_its_own_budget(tmp_path):
    from factory_core.engine import INFRASTRUCTURE_RETRY_BUDGET

    audit = ExecutionResult.failed("TRANSIENT_INCREMENTAL_AUDIT", returncode=75)
    backend = ExecutionResult.failed("TRANSIENT_MODEL_BACKEND", returncode=1, backend="codex")
    results = [audit] * INFRASTRUCTURE_RETRY_BUDGET + [backend] * INFRASTRUCTURE_RETRY_BUDGET
    engine, store, handler = _engine_with(tmp_path, [*results, ExecutionResult.succeeded()], max_attempts=1)

    assert engine.run().status is WorkflowStatus.COMPLETED
    retries = [event for event in store.events() if event.type == "RETRY_SCHEDULED"]
    assert [event.payload["infrastructure_retries"] for event in retries] == [1, 2, 3, 1, 2, 3]


def test_an_exhausted_backend_budget_has_its_own_terminal_class(tmp_path):
    from factory_core.engine import INFRASTRUCTURE_RETRY_BUDGET

    backend = ExecutionResult.failed("TRANSIENT_MODEL_BACKEND", returncode=1, backend="codex")
    engine, store, _ = _engine_with(tmp_path, [backend] * (INFRASTRUCTURE_RETRY_BUDGET + 1), max_attempts=5)

    assert engine.run().status is WorkflowStatus.FAILED
    failure = store.events()[-1]
    assert failure.payload["error_class"] == "PERMANENT_MODEL_BACKEND_INFRASTRUCTURE"
    assert failure.payload["exhausted_error_class"] == "TRANSIENT_MODEL_BACKEND"
    assert failure.payload["content_attempts_used"] == 0
    assert all(
        event.payload["content_attempts_used"] == 0
        for event in store.events() if event.type == "RETRY_SCHEDULED"
    )


def test_a_model_timeout_still_consumes_a_content_attempt(tmp_path):
    timeout = ExecutionResult.failed("TRANSIENT_TIMEOUT", returncode=124)
    engine, store, handler = _engine_with(tmp_path, [timeout, ExecutionResult.succeeded()], max_attempts=1)

    assert engine.run().status is WorkflowStatus.FAILED
    assert handler.calls == [1]


def test_step_0_has_three_attempts():
    from factory_core.steps.catalog import contract_for

    assert contract_for(0).max_attempts == 3


def test_engine_refuses_to_run_or_recover_a_step_scheduler_project(tmp_path):
    from factory_core.stages import STEP_SCHEDULER_GENERATION

    store = SQLiteStateStore(tmp_path)
    state = store.initialize(
        project_id="demo",
        project_type="modeling",
        scheduler_generation=STEP_SCHEDULER_GENERATION,
    )
    store.transition(
        expected_revision=state.revision,
        event_type="STEP_STARTED",
        changes={"status": WorkflowStatus.INTERRUPTED, "active_step": 1, "attempt": 1},
    )
    engine = FactoryEngine(tmp_path, store=store, registry=stage_registry_with())
    revision = store.load().revision

    with pytest.raises(InvalidTransition, match="STAGE_SCHEDULER_REQUIRED"):
        engine.run()
    with pytest.raises(InvalidTransition, match="STAGE_SCHEDULER_REQUIRED"):
        engine.recover()

    assert store.load().revision == revision
