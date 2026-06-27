import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from conftest import REPO_ROOT
from project_diagnostics import write_status


LAUNCH = os.path.join(REPO_ROOT, "launch_agents.sh")
SCRIPT_PATH = Path(REPO_ROOT) / "scripts" / "project_ctl.py"


def write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def load_project_ctl_module():
    sys.modules.pop("project_ctl", None)
    return importlib.import_module("project_ctl")


def test_kill_project_sets_marker_and_removes_pid(tmp_path):
    mod = load_project_ctl_module()
    write_file(tmp_path / "checkpoint.md", "- **Last completed step**: 2\n")
    write_file(tmp_path / ".runner.pid", f"{os.getpid()}\n")

    mod.kill_project(tmp_path)

    assert (tmp_path / ".killed").is_file()
    assert not (tmp_path / ".runner.pid").exists()


def test_pause_project_sets_marker_and_clears_runtime_state(tmp_path):
    mod = load_project_ctl_module()
    write_file(tmp_path / "checkpoint.md", "- **Last completed step**: 2\n")
    write_file(tmp_path / ".runner.pid", "999999\n")
    write_file(tmp_path / ".heartbeat", "ACTIVE:2 1700000000\n")
    write_file(tmp_path / ".runner.lock.info", "lock\n")
    write_file(tmp_path / ".runner.lock" / "info", "lock\n")

    result = mod.pause_project(tmp_path, "demo")

    assert result["paused"] is True
    assert (tmp_path / ".paused").is_file()
    assert not (tmp_path / ".runner.pid").exists()
    assert not (tmp_path / ".heartbeat").exists()
    assert not (tmp_path / ".runner.lock.info").exists()
    assert not (tmp_path / ".runner.lock" / "info").exists()


def test_resume_project_clears_pause_state_without_running_process(tmp_path):
    mod = load_project_ctl_module()
    write_file(tmp_path / "checkpoint.md", "- **Last completed step**: 3\n")
    write_file(tmp_path / ".paused", "")
    write_file(tmp_path / ".heartbeat", "ACTIVE:3 1700000000\n")
    write_file(tmp_path / ".runner.lock.info", "lock\n")

    result = mod.resume_project(tmp_path, "demo", start_runner=False)

    assert result["resumed"] is True
    assert result["started"] is False
    assert not (tmp_path / ".paused").exists()
    assert not (tmp_path / ".heartbeat").exists()
    assert not (tmp_path / ".runner.lock.info").exists()


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
    assert "=== Modeling Factory Status ===" in out.stdout
    assert "alpha" in out.stdout
    assert "PAUSED" in out.stdout
    assert "beta" in out.stdout


def test_launch_agents_pause_and_status_delegate_to_project_ctl():
    project_name = "_project_ctl_pause_test"
    project_dir = Path(REPO_ROOT) / "ongoing" / project_name
    write_file(project_dir / "checkpoint.md", "- **Last completed step**: 3\n")
    write_file(project_dir / ".runner.pid", "999999\n")
    write_file(project_dir / ".heartbeat", "ACTIVE:3 1700000000\n")

    try:
        pause_out = subprocess.run(
            [LAUNCH, "pause", project_name],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert pause_out.returncode == 0, pause_out.stderr
        assert (project_dir / ".paused").is_file()
        assert not (project_dir / ".runner.pid").exists()

        status_out = subprocess.run(
            [LAUNCH, "status"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert status_out.returncode == 0, status_out.stderr
        line = next((ln for ln in status_out.stdout.splitlines() if project_name in ln), None)
        assert line is not None, status_out.stdout
        assert "PAUSED" in line
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)


def test_launch_agents_kill_delegates_to_project_ctl():
    project_name = "_project_ctl_kill_test"
    project_dir = Path(REPO_ROOT) / "ongoing" / project_name
    write_file(project_dir / "checkpoint.md", "- **Last completed step**: 3\n")
    write_file(project_dir / ".runner.pid", f"{os.getpid()}\n")

    try:
        out = subprocess.run(
            [LAUNCH, "kill", project_name],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        assert out.returncode == 0, out.stderr
        assert (project_dir / ".killed").is_file()
        assert not (project_dir / ".runner.pid").exists()
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
