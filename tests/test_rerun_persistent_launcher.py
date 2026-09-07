import sys
import time

from factory_core.persistent_launcher import start, cancel, status


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
