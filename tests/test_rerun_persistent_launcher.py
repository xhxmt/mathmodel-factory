import sys
import time

from factory_core.persistent_launcher import start, cancel, status


def test_concurrent_identical_starts_have_one_durable_monitor(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import pytest
    root = tmp_path / "control"
    command = [sys.executable, "-c", "import time; time.sleep(60)"]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: start(root, tmp_path, command, key="same", timeout=30), range(2)))
        assert len({r["monitor_pid"] for r in results}) == 1
        assert len({r["command_pid"] for r in results}) == 1
        with pytest.raises(ValueError, match="different command"):
            start(root, tmp_path, command, key="different", timeout=30)
    finally:
        assert cancel(root)["process_tree_exited"] is True


def test_launcher_reaps_session_child_even_if_command_leader_exits_immediately(tmp_path):
    from factory_core.adapters.infrastructure.process import _process_identity
    root = tmp_path / "control"
    pid_file = tmp_path / "orphan.pid"
    command = [sys.executable, "-c", "import pathlib,subprocess,sys; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))"]
    try:
        start(root, tmp_path, command, key="exit-parent", timeout=15)
        deadline = time.monotonic() + 5
        while status(root).get("status") != "EXITED" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert status(root)["process_tree_exited"] is True
        assert _process_identity(int(pid_file.read_text())) is None
    finally:
        cancel(root)


def test_detached_launcher_is_idempotent_and_cancels_nested_session(tmp_path):
    root = tmp_path / "control"
    child_file = tmp_path / "child.pid"
    command = [sys.executable, "-c",
        "import subprocess,sys,time,pathlib; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(90)'],start_new_session=True); "
        f"pathlib.Path({str(child_file)!r}).write_text(str(p.pid)); time.sleep(90)"]
    first = start(root, tmp_path, command, key="single", timeout=60)
    assert first["ready"] is True
    second = start(root, tmp_path, command, key="single", timeout=60)
    assert second["monitor_pid"] == first["monitor_pid"]
    deadline = time.monotonic() + 5
    while not child_file.is_file() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert child_file.is_file()
    time.sleep(0.15)  # allow one descendant observation
    result = cancel(root)
    assert result["status"] == "EXITED"
    assert result["stop_requested"] is True
    assert result["process_tree_exited"] is True
    from factory_core.adapters.infrastructure.process import _process_identity
    assert _process_identity(int(child_file.read_text())) is None
    assert start(root, tmp_path, command, key="single", timeout=60) == status(root)
