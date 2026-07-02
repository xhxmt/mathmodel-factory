import importlib
import os
from pathlib import Path
import sys

from project_diagnostics import write_status


def write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def load_state_store_module():
    sys.modules.pop("web.backend.state_store", None)
    return importlib.import_module("web.backend.state_store")


def test_state_store_imports_without_test_sys_path_hack():
    scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
    original_sys_path = sys.path[:]
    sys.path = [entry for entry in sys.path if entry != scripts_dir]
    sys.modules.pop("project_diagnostics", None)

    try:
        mod = load_state_store_module()
    finally:
        sys.path = original_sys_path

    assert hasattr(mod, "read_runtime_status")


def test_read_runtime_status_prefers_canonical_snapshot(tmp_path):
    mod = load_state_store_module()
    write_file(tmp_path / "checkpoint.md", "- **Last completed step**: 3\n")
    write_file(tmp_path / ".heartbeat", "AWAITING_STEP8_5: reviewer entry missing\n")
    write_file(tmp_path / "consultation" / "dynamic_request.md", "# request\n")

    status = write_status(
        tmp_path,
        state="running",
        current_step=5,
        current_action="agent_run",
        reason_code="",
        reason_summary="",
        suggested_actions=["refresh_status"],
        evidence=[{"kind": "file", "path": "logs/runner.log"}],
        display_status="Running Step 5",
        consultation_gate="preflight",
        pid=os.getpid(),
        updated_at=1700000123,
    )

    runtime = mod.read_runtime_status(tmp_path, "demo")

    assert status["version"] == 2
    assert status["display_status"] == "Running Step 5"
    assert status["consultation_gate"] == "preflight"
    assert status["pid"] == os.getpid()
    assert status["updated_at"] == 1700000123

    assert runtime["base_name"] == "demo"
    assert runtime["status"] == "running"
    assert runtime["display_status"] == "Running Step 5"
    assert runtime["current_step"] == 5
    assert runtime["total_steps"] == 16
    assert runtime["progress_percent"] == 31.2
    assert runtime["last_updated"] == 1700000123
    assert runtime["is_running"] is True
    assert runtime["pid"] == os.getpid()
    assert runtime["consultation_pending"] is False
    assert runtime["consultation_gate"] == "preflight"
    assert runtime["reason_code"] == ""
    assert runtime["reason_summary"] == ""
    assert runtime["suggested_actions"] == ["refresh_status"]
    assert runtime["evidence"] == [{"kind": "file", "path": "logs/runner.log"}]


def test_read_runtime_status_rejects_stale_snapshot_pid(tmp_path):
    mod = load_state_store_module()

    write_status(
        tmp_path,
        state="running",
        current_step=6,
        current_action="agent_run",
        suggested_actions=["refresh_status"],
        evidence=[],
        display_status="Running Step 6",
        pid=999999,
        updated_at=1700000999,
    )

    runtime = mod.read_runtime_status(tmp_path, "demo")

    assert runtime["status"] == "running"
    assert runtime["is_running"] is False
    assert runtime["pid"] is None


def test_read_runtime_status_falls_back_to_legacy_consultation_markers(tmp_path):
    mod = load_state_store_module()
    write_file(tmp_path / "checkpoint.md", "- **Last completed step**: 4\n")
    write_file(tmp_path / "consultation" / "step4_request.md", "# request\n")

    runtime = mod.read_runtime_status(tmp_path, "demo")

    assert runtime["status"] == "awaiting_consultation"
    assert runtime["display_status"] == "等待咨询"
    assert runtime["current_step"] == 4
    assert runtime["total_steps"] == 16
    assert runtime["progress_percent"] == 25.0
    assert runtime["is_running"] is False
    assert runtime["pid"] is None
    assert runtime["consultation_pending"] is True
    assert runtime["consultation_gate"] == "step4"
    assert runtime["reason_code"] == "CONSULTATION_PENDING"
    assert runtime["reason_summary"] == "等待人工咨询回填"
    assert runtime["suggested_actions"] == [
        "open_consultation_request",
        "open_human_review",
        "refresh_status",
    ]
    assert runtime["evidence"] == [{"kind": "file", "path": "consultation/step4_request.md"}]


def test_read_runtime_status_ignores_ready_legacy_consultation_requests(tmp_path):
    mod = load_state_store_module()
    write_file(tmp_path / "checkpoint.md", "- **Last completed step**: 16\n")
    write_file(tmp_path / "consultation" / "preflight_request.md", "# request\n")
    write_file(tmp_path / "consultation" / "step4_request.md", "# request\n")
    write_file(
        tmp_path / "human_review.md",
        "## CONSULT preflight (Step 0) — STATUS: READY\n\n结论。\n\n"
        "## CONSULT step4 (Step 4) — STATUS: READY\n\n结论。\n",
    )

    runtime = mod.read_runtime_status(tmp_path, "demo")

    assert runtime["status"] == "completed"
    assert runtime["display_status"] == "已完成"
    assert runtime["current_step"] == 16
    assert runtime["consultation_pending"] is False
    assert runtime["consultation_gate"] is None
    assert runtime["reason_code"] == ""
    assert runtime["evidence"] == []


def test_read_runtime_status_falls_back_to_awaiting_consultation_marker(tmp_path):
    mod = load_state_store_module()
    write_file(tmp_path / "checkpoint.md", "- **Last completed step**: 8\n")
    write_file(tmp_path / ".awaiting_consultation", "GATE:dynamic STEP:8 TS:1700000000\n")

    runtime = mod.read_runtime_status(tmp_path, "demo")

    assert runtime["status"] == "awaiting_consultation"
    assert runtime["current_step"] == 8
    assert runtime["consultation_pending"] is True
    assert runtime["consultation_gate"] == "dynamic"
    assert runtime["reason_code"] == "CONSULTATION_PENDING"
    assert runtime["evidence"] == [{"kind": "file", "path": "consultation/dynamic_request.md"}]


def test_read_runtime_status_falls_back_to_legacy_running_markers(tmp_path):
    mod = load_state_store_module()
    write_file(tmp_path / "checkpoint.md", "- **Last completed step**: 2\n")
    write_file(tmp_path / ".runner.pid", f"{os.getpid()}\n")

    runtime = mod.read_runtime_status(tmp_path, "demo")

    assert runtime["status"] == "running"
    assert runtime["display_status"] == "运行中"
    assert runtime["current_step"] == 2
    assert runtime["progress_percent"] == 12.5
    assert runtime["is_running"] is True
    assert runtime["pid"] == os.getpid()
    assert runtime["consultation_pending"] is False
    assert runtime["consultation_gate"] is None
    assert runtime["reason_code"] == ""
    assert runtime["reason_summary"] == ""
    assert runtime["suggested_actions"] == ["refresh_status"]
    assert runtime["evidence"] == []


def test_write_status_preserves_zero_timestamps(tmp_path):
    status = write_status(
        tmp_path,
        state="waiting",
        current_step=1,
        current_action="verification",
        since=0,
        last_event_at=0,
        updated_at=0,
    )

    assert status["since"] == 0
    assert status["last_event_at"] == 0
    assert status["updated_at"] == 0

    mod = load_state_store_module()
    runtime = mod.read_runtime_status(tmp_path, "demo")

    assert runtime["last_updated"] == 0
