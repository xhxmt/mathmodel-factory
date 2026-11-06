import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REPO_ROOT
from project_diagnostics import write_status
from factory_core.domain import RevisionConflict, WorkflowStatus
from factory_core.storage import SQLiteStateStore
from factory_core.adapters.infrastructure.process import _process_identity


LAUNCH = os.path.join(REPO_ROOT, "launch_agents.sh")
SCRIPT_PATH = Path(REPO_ROOT) / "scripts" / "project_ctl.py"


def write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def load_project_ctl_module():
    sys.modules.pop("project_ctl", None)
    return importlib.import_module("project_ctl")


def test_pid_liveness_and_process_group_termination():
    mod = load_project_ctl_module()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    identity = _process_identity(process.pid)
    try:
        assert mod._is_pid_live(process.pid) is True
        mod._terminate_runner(process.pid, identity)
        process.wait(timeout=5)
        assert mod._is_pid_live(process.pid) is False
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_compat_termination_requires_recorded_identity(monkeypatch):
    mod = load_project_ctl_module()
    calls = []
    monkeypatch.setattr('factory_core.adapters.infrastructure.process._process_identity',
                        lambda pid: calls.append(('identity', pid)))
    monkeypatch.setattr('factory_core.service.os.kill', lambda *args: calls.append(('signal', args)))
    with pytest.raises(RuntimeError, match='persisted launch identity'):
        mod._terminate_runner(123456789)
    assert calls == []


def test_engine_project_controls_write_revisioned_state_and_projections(tmp_path):
    mod = load_project_ctl_module()
    write_file(tmp_path / "checkpoint.md", "- **Last completed step**: 2\n")
    initial = SQLiteStateStore(tmp_path).initialize(
        project_id="demo", project_type="modeling", last_completed_step=2
    )

    paused = mod.pause_project(tmp_path, "demo")

    state = SQLiteStateStore(tmp_path).load()
    assert paused["control_mode"] == "engine"
    assert paused["revision"] == initial.revision + 1
    assert state.status is WorkflowStatus.PAUSED
    assert (tmp_path / ".paused").is_file()
    resumed = mod.resume_project(tmp_path, "demo", start_runner=False)
    assert resumed["revision"] == paused["revision"] + 1
    assert SQLiteStateStore(tmp_path).load().status is WorkflowStatus.READY


def test_engine_resume_rejects_unresolved_selection(tmp_path):
    mod = load_project_ctl_module()
    write_file(tmp_path / "checkpoint.md", "- **Last completed step**: 2\n")
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id="demo", project_type="modeling", last_completed_step=2)
    store.transition(
        expected_revision=state.revision,
        event_type="AWAITING_ACTION",
        changes={
            "status": WorkflowStatus.AWAITING_SELECTION,
            "active_step": 3,
            "pending_action": {"type": "step3_selection", "gate": "step3"},
        },
    )

    with pytest.raises(RuntimeError, match="unresolved action"):
        mod.resume_project(tmp_path, "demo", start_runner=False)


def test_engine_summary_does_not_derive_pending_state_from_stale_files(tmp_path):
    mod = load_project_ctl_module()
    write_file(tmp_path / "checkpoint.md", "- **Last completed step**: 3\n")
    write_file(tmp_path / "selection" / "step3_options.json", "{}\n")
    SQLiteStateStore(tmp_path).initialize(
        project_id="demo", project_type="modeling", last_completed_step=3
    )

    summary = mod.project_summary(tmp_path, "demo")

    assert summary["status"] == "ready"
    assert summary["selection_pending"] is False
    assert summary["revision"] == 1


def test_engine_control_rejects_stale_expected_revision(tmp_path):
    mod = load_project_ctl_module()
    write_file(tmp_path / "checkpoint.md", "- **Last completed step**: 2\n")
    state = SQLiteStateStore(tmp_path).initialize(
        project_id="demo", project_type="modeling", last_completed_step=2
    )

    with pytest.raises(RevisionConflict, match="expected revision"):
        mod.pause_project(
            tmp_path, "demo", expected_revision=state.revision + 1
        )

    assert SQLiteStateStore(tmp_path).load().status is WorkflowStatus.READY


def test_project_ctl_cli_reports_revision_conflict_without_traceback(tmp_path):
    write_file(tmp_path / "checkpoint.md", "- **Last completed step**: 2\n")
    state = SQLiteStateStore(tmp_path).initialize(
        project_id="demo", project_type="modeling", last_completed_step=2
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "pause",
            str(tmp_path),
            "demo",
            "--expected-revision",
            str(state.revision + 1),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "ERROR: expected revision" in result.stderr
    assert "Traceback" not in result.stderr


def test_engine_control_commits_state_before_terminating_runner(tmp_path, monkeypatch):
    mod = load_project_ctl_module()
    write_file(tmp_path / "checkpoint.md", "- **Last completed step**: 2\n")
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(
        project_id="demo", project_type="modeling", last_completed_step=2
    )
    running = store.transition(
        expected_revision=state.revision,
        event_type="RUN_STARTED",
        changes={
            "status": WorkflowStatus.RUNNING,
            "active_step": 3,
            "runner_pid": 424242,
            "runner_lease_id": "controlled-lease",
        },
        payload={"worker_pid": 424242, "lease_id": "controlled-lease", "worker_identity": "controlled-start"},
    )
    observed = []
    # Pid 424242 stands for the live runner recorded above.
    from factory_core.adapters.infrastructure import process as processes

    monkeypatch.setattr(processes, "_process_identity", lambda pid: "controlled-start")
    monkeypatch.setattr(
        mod.FactoryService,
        "_terminate_runner",
        staticmethod(
            lambda pid, identity: observed.append(
                (pid, identity, SQLiteStateStore(tmp_path).load().status)
            )
        ),
    )

    result = mod.pause_project(
        tmp_path, "demo", expected_revision=running.revision
    )

    assert result["paused"] is True
    assert observed == [(424242, "controlled-start", WorkflowStatus.PAUSED)]


def test_project_summary_reads_canonical_status(tmp_path):
    mod = load_project_ctl_module()
    write_status(
        tmp_path,
        state="running",
        current_step=4,
        current_action="agent_run",
        display_status="Running Step 4",
        pid=os.getpid(),
        updated_at=1700000222,
        reason_code="",
        reason_summary="",
        suggested_actions=["refresh_status"],
        evidence=[{"kind": "file", "path": "logs/runner.log"}],
    )

    summary = mod.project_summary(tmp_path, "demo")

    assert summary["base_name"] == "demo"
    assert summary["status"] == "running"
    assert summary["display_status"] == "Running Step 4"
    assert summary["current_step"] == 4
    assert summary["last_updated"] == 1700000222


def test_project_ctl_cli_summary_outputs_json(tmp_path):
    write_status(
        tmp_path,
        state="ready",
        current_step=1,
        current_action="fallback",
        display_status="Ready",
        updated_at=1700000333,
    )

    out = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "summary", str(tmp_path), "demo"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert payload["base_name"] == "demo"
    assert payload["status"] == "ready"
    assert payload["last_updated"] == 1700000333


def test_project_ctl_cli_status_outputs_project_rows(tmp_path):
    ongoing = tmp_path / "ongoing"
    complete = tmp_path / "complete"
    write_file(ongoing / "alpha" / "checkpoint.md", "- **Last completed step**: 1\n- **Timestamp**: 2026-01-01 00:00\n")
    write_file(ongoing / "alpha" / ".paused", "")
    write_file(complete / "beta" / "checkpoint.md", "- **Last completed step**: 16\n- **Timestamp**: 2026-01-02 00:00\n")

    out = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "status", "--factory-root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert out.returncode == 0, out.stderr
    assert "PROJECT" in out.stdout
    assert "alpha" in out.stdout
    assert "HISTORICAL_READ_ONLY" in out.stdout
    assert "beta" in out.stdout


def test_launch_agents_new_initializes_python_engine_state():
    project_name = "_factory_engine_new_test"
    project_dir = Path(REPO_ROOT) / "ongoing" / project_name
    shutil.rmtree(project_dir, ignore_errors=True)
    try:
        out = subprocess.run(
            [LAUNCH, "new", "--no-start", project_name, "/tmp/problem.md"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        assert out.returncode == 0, out.stderr
        state = SQLiteStateStore(project_dir).load()
        assert state.project_type == "modeling"
        assert state.control_mode == "engine"
        assert state.last_completed_step == -1
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
