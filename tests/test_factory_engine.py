from dataclasses import dataclass, field
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
    StepContext,
    ValidationResult,
    WorkflowStatus,
)
from factory_core.engine import FactoryEngine
from factory_core.registry import StepDefinition, StepRegistry
from factory_core.storage import SQLiteStateStore


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
    registry = StepRegistry()
    registry.register(
        StepDefinition(
            id=1,
            name="first",
            timeout_seconds=30,
            max_attempts=max_attempts,
            handler=handler,
            validator=validator,
        )
    )
    return registry


def test_engine_retries_through_registry_without_step_number_branches(tmp_path):
    handler = FakeHandler([ExecutionResult.failed("TRANSIENT"), ExecutionResult.succeeded()])
    validator = FakeValidator([ValidationResult.invalid("missing"), ValidationResult.valid()])
    store = SQLiteStateStore(tmp_path)
    store.initialize(project_id="demo", project_type="modeling")
    engine = FactoryEngine(tmp_path, store=store, registry=registry_for(handler, validator), sleeper=lambda _: None)

    state = engine.run()

    assert state.status is WorkflowStatus.COMPLETED
    assert state.last_completed_step == 1
    assert handler.calls == [1, 2]
    assert [event.type for event in store.events()] == [
        "PROJECT_CREATED",
        "RUN_STARTED",
        "STEP_STARTED",
        "RETRY_SCHEDULED",
        "STEP_STARTED",
        "STEP_SUCCEEDED",
        "PROJECT_COMPLETED",
    ]


def test_engine_persists_pending_action_and_stops_dispatch(tmp_path):
    handler = FakeHandler([ExecutionResult.succeeded()])
    validator = FakeValidator(
        [ValidationResult.awaiting(PendingAction(type="step3_selection", gate="step3"))]
    )
    store = SQLiteStateStore(tmp_path)
    initial = store.initialize(project_id="demo", project_type="modeling")
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


def test_recovery_promotes_valid_artifacts_after_interrupted_step(tmp_path):
    handler = FakeHandler([])
    validator = FakeValidator([ValidationResult.valid()])
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id="demo", project_type="modeling")
    store.transition(
        expected_revision=state.revision,
        event_type="STEP_STARTED",
        changes={"status": WorkflowStatus.RUNNING, "active_step": 1, "attempt": 1},
    )
    engine = FactoryEngine(tmp_path, store=store, registry=registry_for(handler, validator))

    recovered = engine.recover()

    assert recovered.status is WorkflowStatus.READY
    assert recovered.last_completed_step == 1
    assert recovered.active_step is None
    assert store.events()[-1].type == "RECOVERY_DECIDED"
    assert store.events()[-1].payload["decision"] == "promote_valid_artifacts"


def test_recovery_preserves_validator_fast_forward_target(tmp_path):
    handler = FakeHandler([])
    validator = FakeValidator(
        [ValidationResult.valid(metadata={"completed_through_step": 3})]
    )
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id="demo", project_type="modeling")
    store.transition(
        expected_revision=state.revision,
        event_type="STEP_STARTED",
        changes={"status": WorkflowStatus.RUNNING, "active_step": 1, "attempt": 1},
    )
    engine = FactoryEngine(
        tmp_path, store=store, registry=registry_for(handler, validator)
    )

    recovered = engine.recover()

    assert recovered.last_completed_step == 3
    assert store.events()[-1].payload["decision"] == "promote_valid_artifacts"


def test_recovery_preserves_validator_reopen_target(tmp_path):
    handler = FakeHandler([])
    validator = FakeValidator(
        [ValidationResult.invalid("reopen", metadata={"resume_after_step": 0})]
    )
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(
        project_id="demo", project_type="modeling", last_completed_step=0
    )
    store.transition(
        expected_revision=state.revision,
        event_type="STEP_STARTED",
        changes={"status": WorkflowStatus.RUNNING, "active_step": 1, "attempt": 1},
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
    state = store.initialize(project_id="demo", project_type="modeling")
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        store.transition(
            expected_revision=state.revision,
            event_type="STEP_STARTED",
            changes={
                "status": WorkflowStatus.RUNNING,
                "active_step": 1,
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
    state = store.initialize(project_id="demo", project_type="modeling")
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
    state = store.initialize(project_id="demo", project_type="modeling")
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


def test_step_result_can_fast_forward_without_scheduler_branch(tmp_path):
    handler = FakeHandler([ExecutionResult.succeeded(completed_through_step=3)])
    validator = FakeValidator([ValidationResult.valid()])
    store = SQLiteStateStore(tmp_path)
    store.initialize(project_id="demo", project_type="modeling")
    engine = FactoryEngine(
        tmp_path, store=store, registry=registry_for(handler, validator)
    )

    state = engine.run()

    assert state.status is WorkflowStatus.COMPLETED
    assert state.last_completed_step == 3


def test_reopen_result_is_committed_before_artifact_validation(tmp_path):
    handler = FakeHandler(
        [ExecutionResult.succeeded(resume_after_step=0), ExecutionResult.succeeded()]
    )
    validator = FakeValidator([ValidationResult.valid()])
    store = SQLiteStateStore(tmp_path)
    store.initialize(
        project_id="demo", project_type="modeling", last_completed_step=0
    )
    engine = FactoryEngine(
        tmp_path, store=store, registry=registry_for(handler, validator)
    )

    state = engine.run()

    assert state.status is WorkflowStatus.COMPLETED
    assert handler.calls == [1, 1]
    assert [event.type for event in store.events()].count("STEP_REOPENED") == 1


def test_legacy_registry_preserves_step_specific_timeout_contracts():
    from factory_core.adapters.legacy import build_legacy_registry

    registry = build_legacy_registry("/tmp", "/tmp/legacy_runner.sh")

    assert registry.get(2).timeout_seconds == 28_800
    assert registry.get(3).timeout_seconds == 7_200
    assert registry.get(16).timeout_seconds == 3_600


def test_legacy_timeout_reaps_process_after_sigkill(tmp_path, monkeypatch):
    from factory_core.adapters import legacy

    class TimedOutProcess:
        pid = 12345

        def __init__(self):
            self.wait_calls = []

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            if len(self.wait_calls) <= 2:
                raise subprocess.TimeoutExpired("legacy_runner", timeout)
            return -9

    process = TimedOutProcess()
    signals = []
    monkeypatch.setattr(legacy.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(legacy.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    handler = legacy.LegacyStepHandler(tmp_path, tmp_path / "legacy_runner.sh")
    context = StepContext(
        project_dir=tmp_path,
        project_id="demo",
        step_id=1,
        attempt=1,
        timeout_seconds=30,
        revision=1,
    )

    result = handler.execute(context)

    assert result.returncode == 124
    assert result.error_class == "TRANSIENT_TIMEOUT"
    assert process.wait_calls == [210, 10, None]
    assert signals == [
        (process.pid, legacy.signal.SIGTERM),
        (process.pid, legacy.signal.SIGKILL),
    ]


def test_new_execution_backend_is_registered_without_scheduler_change():
    from factory_core.registry import BackendRegistry

    backend = FakeHandler([ExecutionResult.succeeded()])
    registry = BackendRegistry()

    registry.register("new-cloud-solver", backend)

    assert registry.get("new-cloud-solver") is backend


def test_recovery_does_not_reopen_resolved_consultation(tmp_path):
    from factory_core.adapters.legacy import LegacyArtifactValidator

    (tmp_path / ".awaiting_consultation").write_text(
        "GATE:step4 STEP:4\n", encoding="utf-8"
    )
    (tmp_path / "human_review.md").write_text(
        "## CONSULT step4 (Step 4) - STATUS: READY\n\nUse option A.\n",
        encoding="utf-8",
    )
    validator = LegacyArtifactValidator("/tmp", "/tmp/legacy_runner.sh")
    validator.infer_step = lambda _project: 3
    context = StepContext(
        project_dir=tmp_path,
        project_id="demo",
        step_id=4,
        attempt=1,
        timeout_seconds=30,
        revision=2,
    )

    result = validator.validate(context)

    assert result.pending_action is None
    assert result.is_valid is False


def test_legacy_validator_reports_durable_reopen_target(tmp_path):
    from factory_core.adapters.legacy import LegacyArtifactValidator

    (tmp_path / "checkpoint.md").write_text(
        "- **Last completed step**: 11\n", encoding="utf-8"
    )
    (tmp_path / ".gate2_reopen_to_revision").touch()
    validator = LegacyArtifactValidator("/tmp", "/tmp/legacy_runner.sh")
    context = StepContext(
        project_dir=tmp_path,
        project_id="demo",
        step_id=13,
        attempt=1,
        timeout_seconds=30,
        revision=2,
    )

    result = validator.validate(context)

    assert result.is_valid is False
    assert result.metadata == {"resume_after_step": 11}


def test_projection_failure_does_not_rollback_authoritative_transition(tmp_path):
    handler = FakeHandler([ExecutionResult.succeeded()])
    validator = FakeValidator([ValidationResult.valid()])
    store = SQLiteStateStore(tmp_path)
    store.initialize(project_id="demo", project_type="modeling")

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
    registry = StepRegistry()
    registry.register(
        StepDefinition(
            id=1,
            name="native",
            timeout_seconds=30,
            max_attempts=1,
            step=native,
        )
    )
    store = SQLiteStateStore(tmp_path)
    store.initialize(
        project_id="demo", project_type="modeling", runtime_generation="native_v2"
    )

    state = FactoryEngine(tmp_path, store=store, registry=registry).run()

    assert state.status is WorkflowStatus.COMPLETED
    assert state.runtime_generation == "native_v2"
    assert native.calls == ["prepare", "execute", "validate"]
