"""Detached local command monitor with durable readiness and single-writer start."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .adapters.infrastructure.process import ProcessRequest, ProcessSupervisor, _process_identity
from .projections import _atomic_text


def status(root):
    path = root / "status.json"
    value = json.loads(path.read_text()) if path.is_file() else {"status": "NOT_STARTED"}
    if value.get("status") == "RUNNING" and _process_identity(value["monitor_pid"]) != value["monitor_identity"]:
        value = {**value, "status": "INTERRUPTED", "process_tree_exited": False}
    return value


def start(root, cwd, command, *, key, timeout=3600, ready_timeout=10):
    root, cwd = root.resolve(), cwd.resolve()
    root.mkdir(parents=True, exist_ok=True)
    request = {"cwd": str(cwd), "command": list(command), "key": key, "timeout": timeout}
    lock = (root / "writer.lock").open("a")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            deadline = time.monotonic() + ready_timeout
            while not (root / "request.json").is_file() and time.monotonic() < deadline:
                time.sleep(0.02)
            old = json.loads((root / "request.json").read_text())
            if old != request:
                raise ValueError("a different command already owns this launcher")
            deadline = time.monotonic() + ready_timeout
            while time.monotonic() < deadline:
                current = status(root)
                if current.get("status") in {"RUNNING", "EXITED"}:
                    return current
                time.sleep(0.02)
            raise TimeoutError("existing launcher did not produce a ready receipt")
        if (root / "request.json").is_file():
            if json.loads((root / "request.json").read_text()) != request:
                raise ValueError("launcher root is immutable; use a new root for a new request")
            return status(root)
        _atomic_text(root / "request.json", json.dumps(request, sort_keys=True))
        with (root / "monitor.log").open("ab") as log:
            subprocess.Popen([sys.executable, "-m", "factory_core.persistent_launcher", "monitor",
                str(root), "--lock-fd", str(lock.fileno())], cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                pass_fds=(lock.fileno(),), env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    finally:
        lock.close()  # inherited descriptor keeps the lock for the monitor lifetime
    deadline = time.monotonic() + ready_timeout
    while time.monotonic() < deadline:
        current = status(root)
        if current.get("status") in {"RUNNING", "EXITED"}:
            return current
        time.sleep(0.02)
    raise TimeoutError("launcher readiness timed out; inspect monitor.log before retrying")


def cancel(root, *, wait_seconds=15):
    current = status(root)
    if current.get("status") != "RUNNING":
        return current
    if _process_identity(current["monitor_pid"]) == current["monitor_identity"]:
        os.kill(current["monitor_pid"], signal.SIGTERM)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        current = status(root)
        if current.get("status") == "EXITED":
            if current.get("process_tree_exited") is not True:
                raise RuntimeError("launcher did not verify process-tree exit")
            return current
        time.sleep(0.05)
    raise TimeoutError("launcher cancellation is not yet verified")


def monitor(root, lock_fd):
    # Only this dedicated monitor adopts orphans. The shared engine process
    # does not become a global subreaper for unrelated application children.
    import ctypes
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot enable Linux child subreaper")
    request = json.loads((root / "request.json").read_text())
    started = {"monitor_pid": os.getpid(), "monitor_identity": _process_identity(os.getpid()),
               "key": request["key"], "status": "RUNNING", "ready": True}
    def ready(pid):
        _atomic_text(root / "status.json", json.dumps({**started, "command_pid": pid}))
    try:
        result = ProcessSupervisor().run(ProcessRequest(argv=request["command"],
            cwd=Path(request["cwd"]), timeout_seconds=request["timeout"],
            stdout_path=root / "stdout.log", stderr_path=root / "stderr.log",
            on_started=ready, poll_seconds=0.05, kill_grace_seconds=0.2, adopt_orphans=True))
        _atomic_text(root / "status.json", json.dumps({**started, "status": "EXITED",
            "exit_code": result.returncode, "timed_out": result.timed_out,
            "process_tree_exited": result.metadata.get("process_tree_exited", result.pid == 0),
            "stop_requested": result.metadata.get("stop_requested", False)}))
    finally:
        os.close(lock_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "status", "cancel", "monitor"])
    parser.add_argument("root", type=Path)
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--key")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--lock-fd", type=int)
    args, command = parser.parse_known_args()
    if args.action == "monitor":
        monitor(args.root, args.lock_fd)
        return 0
    if args.action == "start":
        if not args.key or args.cwd is None or not command:
            parser.error("start requires --key, --cwd, and -- command")
        value = start(args.root, args.cwd, command[1:] if command[0] == "--" else command,
                      key=args.key, timeout=args.timeout)
    else:
        value = cancel(args.root) if args.action == "cancel" else status(args.root)
    print(json.dumps(value, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
