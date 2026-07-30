import json
import subprocess
import sys

import pytest

from factory_core.domain import InvalidTransition, RevisionConflict, WorkflowStatus
from factory_core.projections import write_compatibility_projections
from factory_core.service import FactoryService, WorkerHandle
from factory_core.storage import SQLiteStateStore
from factory_core.registry import StepRegistry
from web.backend import project_actions


class RecordingWorkerLauncher:
    def __init__(self):
        self.calls = []

    def spawn(self, project, *, expected_revision=None):
        project = project.resolve()
        self.calls.append((project, expected_revision))
        store = SQLiteStateStore(project)
        updated = store.transition(
            expected_revision=expected_revision,
            event_type="WORKER_LAUNCHED",
            changes={
                "status": WorkflowStatus.RUNNING,
                "runner_pid": 4242,
                "runner_lease_id": "test:4242",
                "heartbeat_at": 1234,
            },
            payload={"worker_pid": 4242, "log": "logs/test-worker.log"},
        )
        write_compatibility_projections(project, updated)
        return WorkerHandle(4242, project / "logs/test-worker.log", updated)


def test_service_creates_native_project_and_consultation_projection(tmp_path):
    service = FactoryService(tmp_path)

    state, worker = service.create_project(
        "demo", "/tmp/problem.pdf", consult=True, start=False
    )

    project = tmp_path / "ongoing" / "demo"
    assert worker is None
    assert state.runtime_generation == "native_v2"
    assert state.status is WorkflowStatus.READY
    assert (project / "consultation/enabled").is_file()
    assert "Research question**: /tmp/problem.pdf" in (
        project / "checkpoint.md"
    ).read_text(encoding="utf-8")


def test_service_and_web_action_share_revision_and_event_contract(tmp_path):
    service = FactoryService(tmp_path)
    service.create_project("demo", "question", start=False)
    before = service.inspect("demo")

    result = project_actions.run_action(
        tmp_path, "pause", "demo", expected_revision=before.revision
    )

    after = service.inspect("demo")
    assert result.ok is True
    assert json.loads(result.stdout)["revision"] == after.revision
    assert after.revision == before.revision + 1
    assert SQLiteStateStore(tmp_path / "ongoing/demo").events()[-1].type == "PAUSED"


def test_service_rejects_stale_web_revision_without_partial_event(tmp_path):
    service = FactoryService(tmp_path)
    service.create_project("demo", "question", start=False)
    current = service.pause("demo")
    event_count = len(SQLiteStateStore(tmp_path / "ongoing/demo").events())

    result = project_actions.run_action(
        tmp_path, "kill", "demo", expected_revision=current.revision - 1
    )

    assert result.ok is False
    assert "expected revision" in result.stderr
    assert len(SQLiteStateStore(tmp_path / "ongoing/demo").events()) == event_count


def test_service_resolves_ready_selection_before_resume(tmp_path):
    service = FactoryService(tmp_path)
    state, _ = service.create_project("demo", "question", start=False)
    project = tmp_path / "ongoing/demo"
    store = SQLiteStateStore(project)
    waiting = store.transition(
        expected_revision=state.revision,
        event_type="AWAITING_ACTION",
        changes={
            "status": WorkflowStatus.AWAITING_SELECTION,
            "pending_action": {"type": "step3_selection", "gate": "step3"},
        },
    )
    (project / "selection").mkdir()
    (project / "selection/step3_decision.json").write_text("{}\n", encoding="utf-8")

    resumed = service.resume(project, expected_revision=waiting.revision)

    assert resumed.status is WorkflowStatus.READY
    assert resumed.pending_action is None
    assert [event.type for event in store.events()][-2:] == ["ACTION_RESOLVED", "RESUMED"]


def test_service_rejects_stale_revision_before_resolving_pending_action(tmp_path):
    service = FactoryService(tmp_path)
    state, _ = service.create_project("demo", "question", start=False)
    project = tmp_path / "ongoing/demo"
    store = SQLiteStateStore(project)
    waiting = store.transition(
        expected_revision=state.revision,
        event_type="AWAITING_ACTION",
        changes={
            "status": WorkflowStatus.AWAITING_SELECTION,
            "pending_action": {"type": "step3_selection", "gate": "step3"},
        },
    )
    (project / "selection").mkdir()
    (project / "selection/step3_decision.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(RevisionConflict):
        service.resume(project, expected_revision=waiting.revision - 1)

    assert store.load().pending_action is not None


def test_resume_and_start_launches_worker_with_final_revision(tmp_path):
    launcher = RecordingWorkerLauncher()
    service = FactoryService(tmp_path, worker_launcher=launcher)
    state, _ = service.create_project("demo", "question", start=False)
    paused = service.pause("demo", expected_revision=state.revision)

    running, worker = service.resume_and_start(
        "demo", expected_revision=paused.revision
    )

    project = (tmp_path / "ongoing/demo").resolve()
    assert worker.pid == 4242
    assert running.status is WorkflowStatus.RUNNING
    assert running.runner_pid == 4242
    assert launcher.calls == [(project, paused.revision + 1)]
    assert [event.type for event in SQLiteStateStore(project).events()][-2:] == [
        "RESUMED",
        "WORKER_LAUNCHED",
    ]
    assert running.revision == paused.revision + 2


def test_resume_and_start_rejects_stale_revision_without_event_or_worker(tmp_path):
    launcher = RecordingWorkerLauncher()
    service = FactoryService(tmp_path, worker_launcher=launcher)
    state, _ = service.create_project("demo", "question", start=False)
    current = service.pause("demo", expected_revision=state.revision)
    store = SQLiteStateStore(tmp_path / "ongoing/demo")
    event_count = len(store.events())

    with pytest.raises(RevisionConflict, match="expected revision"):
        service.resume_and_start("demo", expected_revision=current.revision - 1)

    assert launcher.calls == []
    assert len(store.events()) == event_count
    assert store.load().status is WorkflowStatus.PAUSED


@pytest.mark.parametrize(
    "status",
    [WorkflowStatus.KILLED, WorkflowStatus.COMPLETED, WorkflowStatus.ARCHIVING],
)
def test_start_rejects_terminal_and_archiving_states(tmp_path, status):
    launcher = RecordingWorkerLauncher()
    service = FactoryService(tmp_path, worker_launcher=launcher)
    state, _ = service.create_project("demo", "question", start=False)
    store = SQLiteStateStore(tmp_path / "ongoing/demo")
    store.transition(
        expected_revision=state.revision,
        event_type="TEST_STATE",
        changes={"status": status},
    )

    with pytest.raises(InvalidTransition, match="cannot be started"):
        service.start("demo")

    assert launcher.calls == []


def test_start_rejects_live_runner_even_when_snapshot_is_ready(tmp_path):
    launcher = RecordingWorkerLauncher()
    service = FactoryService(tmp_path, worker_launcher=launcher)
    state, _ = service.create_project("demo", "question", start=False)
    store = SQLiteStateStore(tmp_path / "ongoing/demo")
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        store.transition(
            expected_revision=state.revision,
            event_type="SIMULATED_STEP_BOUNDARY",
            changes={
                "status": WorkflowStatus.READY,
                "runner_pid": sleeper.pid,
                "runner_lease_id": "still-running",
            },
        )

        with pytest.raises(InvalidTransition, match="already has live worker"):
            service.start("demo")
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)

    assert launcher.calls == []


def test_web_resume_rejects_ready_snapshot_with_live_runner(tmp_path, monkeypatch):
    launcher = RecordingWorkerLauncher()
    service = FactoryService(tmp_path, worker_launcher=launcher)
    state, _ = service.create_project("demo", "question", start=False)
    store = SQLiteStateStore(tmp_path / "ongoing/demo")
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        ready_with_owner = store.transition(
            expected_revision=state.revision,
            event_type="SIMULATED_STEP_BOUNDARY",
            changes={
                "status": WorkflowStatus.READY,
                "runner_pid": sleeper.pid,
                "runner_lease_id": "still-running",
            },
        )

        monkeypatch.setattr(project_actions, "FactoryService", lambda _root: service)
        result = project_actions.run_action(
            tmp_path,
            "resume",
            "demo",
            expected_revision=ready_with_owner.revision,
        )
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)

    assert result.ok is False
    assert "already has live worker" in result.stderr
    assert launcher.calls == []


def test_web_resume_after_rollback_uses_legacy_registry(tmp_path, monkeypatch):
    launcher = RecordingWorkerLauncher()
    native_calls = []

    def native_registry(_root):
        native_calls.append(True)
        return StepRegistry()

    service = FactoryService(
        tmp_path,
        worker_launcher=launcher,
        native_registry_factory=native_registry,
    )
    service.create_project("demo", "question", start=False)

    rolled_back = service.rollback_migration("demo")

    assert rolled_back.control_mode == "legacy"
    assert rolled_back.runtime_generation == "legacy_adapter"
    assert native_calls == [True]

    monkeypatch.setattr(project_actions, "FactoryService", lambda _root: service)
    result = project_actions.run_action(
        tmp_path, "resume", "demo", expected_revision=rolled_back.revision
    )

    assert result.ok is True
    assert native_calls == [True]
    assert launcher.calls
    assert all(
        definition.step is None
        and definition.handler is not None
        and "legacy" in type(definition.handler).__module__
        for definition in service.engine("demo").registry
    )


def test_resume_and_start_resolves_selection_before_worker_launch(tmp_path):
    launcher = RecordingWorkerLauncher()
    service = FactoryService(tmp_path, worker_launcher=launcher)
    state, _ = service.create_project("demo", "question", start=False)
    project = tmp_path / "ongoing/demo"
    store = SQLiteStateStore(project)
    waiting = store.transition(
        expected_revision=state.revision,
        event_type="AWAITING_ACTION",
        changes={
            "status": WorkflowStatus.AWAITING_SELECTION,
            "pending_action": {"type": "step3_selection", "gate": "step3"},
        },
    )
    (project / "selection").mkdir()
    (project / "selection/step3_decision.json").write_text("{}\n", encoding="utf-8")

    running, _ = service.resume_and_start(
        project, expected_revision=waiting.revision
    )

    assert running.status is WorkflowStatus.RUNNING
    assert running.pending_action is None
    assert [event.type for event in store.events()][-3:] == [
        "ACTION_RESOLVED",
        "RESUMED",
        "WORKER_LAUNCHED",
    ]


def test_resume_and_start_resolves_consultation_before_worker_launch(tmp_path):
    launcher = RecordingWorkerLauncher()
    service = FactoryService(tmp_path, worker_launcher=launcher)
    state, _ = service.create_project("demo", "question", start=False)
    project = tmp_path / "ongoing/demo"
    store = SQLiteStateStore(project)
    waiting = store.transition(
        expected_revision=state.revision,
        event_type="AWAITING_ACTION",
        changes={
            "status": WorkflowStatus.AWAITING_CONSULTATION,
            "pending_action": {"type": "human_consultation", "gate": "preflight"},
        },
    )
    (project / "human_review.md").write_text(
        "## CONSULT preflight (Step 0) - STATUS: READY\nanswer\n",
        encoding="utf-8",
    )

    running, _ = service.resume_and_start(
        project, expected_revision=waiting.revision
    )

    assert running.status is WorkflowStatus.RUNNING
    assert running.pending_action is None
    assert [event.type for event in store.events()][-3:] == [
        "ACTION_RESOLVED",
        "RESUMED",
        "WORKER_LAUNCHED",
    ]


def test_resolve_and_start_accepts_decision_before_writing_evidence(tmp_path):
    launcher = RecordingWorkerLauncher()
    service = FactoryService(tmp_path, worker_launcher=launcher)
    state, _ = service.create_project("demo", "question", start=False)
    project = tmp_path / "ongoing/demo"
    store = SQLiteStateStore(project)
    waiting = store.transition(
        expected_revision=state.revision,
        event_type="AWAITING_ACTION",
        changes={
            "status": WorkflowStatus.AWAITING_SELECTION,
            "pending_action": {"type": "step3_selection", "gate": "step3"},
        },
    )
    observed = []

    def write_evidence():
        observed.append(store.load())
        (project / "decision.txt").write_text("m1\n", encoding="utf-8")

    running, _ = service.resolve_and_start(
        project,
        {"source": "test", "selected_option_id": "m1"},
        evidence_writer=write_evidence,
        expected_revision=waiting.revision,
    )

    assert observed[0].status is WorkflowStatus.READY
    assert observed[0].pending_action is None
    assert running.status is WorkflowStatus.RUNNING
    assert [event.type for event in store.events()][-3:] == [
        "ACTION_RESOLVED",
        "RESUMED",
        "WORKER_LAUNCHED",
    ]


def test_resolve_and_start_stale_revision_does_not_write_or_launch(tmp_path):
    launcher = RecordingWorkerLauncher()
    service = FactoryService(tmp_path, worker_launcher=launcher)
    state, _ = service.create_project("demo", "question", start=False)
    project = tmp_path / "ongoing/demo"
    store = SQLiteStateStore(project)
    waiting = store.transition(
        expected_revision=state.revision,
        event_type="AWAITING_ACTION",
        changes={
            "status": WorkflowStatus.AWAITING_SELECTION,
            "pending_action": {"type": "step3_selection", "gate": "step3"},
        },
    )
    writes = []

    with pytest.raises(RevisionConflict, match="expected revision"):
        service.resolve_and_start(
            project,
            {"source": "test"},
            evidence_writer=lambda: writes.append(True),
            expected_revision=waiting.revision - 1,
        )

    assert writes == []
    assert launcher.calls == []
    assert store.load().pending_action is not None


def test_resolve_and_start_restores_pending_action_when_projection_fails(tmp_path):
    launcher = RecordingWorkerLauncher()
    service = FactoryService(tmp_path, worker_launcher=launcher)
    state, _ = service.create_project("demo", "question", start=False)
    project = tmp_path / "ongoing/demo"
    store = SQLiteStateStore(project)
    waiting = store.transition(
        expected_revision=state.revision,
        event_type="AWAITING_ACTION",
        changes={
            "status": WorkflowStatus.AWAITING_SELECTION,
            "pending_action": {"type": "step3_selection", "gate": "step3"},
        },
    )

    def fail_projection():
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        service.resolve_and_start(
            project,
            {"source": "test"},
            evidence_writer=fail_projection,
            expected_revision=waiting.revision,
        )

    restored = store.load()
    assert restored.status is WorkflowStatus.AWAITING_SELECTION
    assert restored.pending_action == waiting.pending_action
    assert launcher.calls == []
    assert [event.type for event in store.events()][-2:] == [
        "ACTION_RESOLVED",
        "ACTION_PROJECTION_FAILED",
    ]
