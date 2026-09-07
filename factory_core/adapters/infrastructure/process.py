from __future__ import annotations

import os
import signal
import subprocess
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence


@dataclass(frozen=True)
class ProcessRequest:
    argv: Sequence[str]
    cwd: Path
    timeout_seconds: int
    stdout_path: Path
    stderr_path: Path | None = None
    env: Mapping[str, str] | None = None
    heartbeat: Callable[[], None] | None = None
    poll_seconds: float = 1.0
    kill_grace_seconds: float = 10.0
    on_started: Callable[[int], None] | None = None
    stop_requested: Callable[[], bool] | None = None
    pass_fds: tuple[int, ...] = ()
    adopt_orphans: bool = False


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    timed_out: bool
    duration_seconds: float
    pid: int
    metadata: dict[str, object] = field(default_factory=dict)


class ProcessSupervisor:
    def run(self, request: ProcessRequest) -> ProcessResult:
        previous = {}
        if threading.current_thread() is threading.main_thread():
            def cancelled(_signum, _frame):
                raise _SupervisorCancelled()
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, cancelled)
        try:
            return self._run(request)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def _run(self, request: ProcessRequest) -> ProcessResult:
        request.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path = request.stderr_path or request.stdout_path
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        with request.stdout_path.open("ab") as stdout_handle:
            if stderr_path == request.stdout_path:
                stderr_handle = stdout_handle
                close_stderr = False
            else:
                stderr_handle = stderr_path.open("ab")
                close_stderr = True
            try:
                try:
                    process = subprocess.Popen(
                        list(request.argv),
                        cwd=request.cwd,
                        env=dict(request.env) if request.env is not None else None,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_handle,
                        stderr=stderr_handle,
                        start_new_session=True,
                        pass_fds=request.pass_fds,
                    )
                except OSError as exc:
                    return ProcessResult(
                        returncode=127,
                        timed_out=False,
                        duration_seconds=time.monotonic() - started,
                        pid=0,
                        metadata={
                            "launch_error": type(exc).__name__,
                            "message": str(exc),
                        },
                    )
                deadline = started + request.timeout_seconds
                timed_out = False
                stopped = False
                descendants = {}
                try:
                    if request.on_started is not None:
                        request.on_started(process.pid)
                    while process.poll() is None:
                        descendants.update(_descendants(process.pid))
                        if request.adopt_orphans:
                            descendants.update({pid: token for pid, token in _descendants(os.getpid()).items()
                                                if pid != process.pid})
                        if request.stop_requested is not None and request.stop_requested():
                            stopped = True
                            self._terminate_tree(process, descendants, request.kill_grace_seconds)
                            break
                        if request.heartbeat is not None:
                            request.heartbeat()
                        if time.monotonic() >= deadline:
                            timed_out = True
                            self._terminate_tree(process, descendants, request.kill_grace_seconds)
                            break
                        time.sleep(min(request.poll_seconds, max(0.01, deadline - time.monotonic())))
                except _SupervisorCancelled:
                    stopped = True
                    self._terminate_tree(process, descendants, request.kill_grace_seconds)
                except BaseException:
                    self._terminate_tree(process, descendants, request.kill_grace_seconds)
                    raise
                returncode = process.wait()
                if request.adopt_orphans:
                    descendants.update(_descendants(os.getpid()))
                self._terminate_tree(process, descendants, 0)
            finally:
                if close_stderr:
                    stderr_handle.close()
        return ProcessResult(
            returncode=124 if timed_out else returncode,
            timed_out=timed_out,
            duration_seconds=time.monotonic() - started,
            pid=process.pid,
            metadata={"stop_requested": stopped, "observed_descendants": len(descendants),
                      "process_tree_exited": all(_process_identity(pid) != token for pid, token in descendants.items())},
        )

    @classmethod
    def _terminate_tree(cls, process, descendants, grace_seconds):
        descendants.update(_descendants(process.pid))
        for pid, token in descendants.items():
            if _process_identity(pid) == token:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        cls._terminate_group(process, grace_seconds)
        for pid, token in descendants.items():
            if _process_identity(pid) == token:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 1
        while any(_process_identity(pid) == token for pid, token in descendants.items()):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)

    @staticmethod
    def _terminate_group(process: subprocess.Popen, grace_seconds: float = 10.0) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (PermissionError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            pass
        # The leader exiting on TERM does not prove its children exited. Always
        # close the owned process group before returning from cancellation.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (PermissionError, ProcessLookupError):
            pass
        process.wait()


class _SupervisorCancelled(Exception):
    pass


def _process_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, IndexError):
        return None


def _descendants(parent):
    parents, tokens = {}, {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] != "Z":
                parents[int(entry.name)] = int(fields[1])
                tokens[int(entry.name)] = fields[19]
        except (OSError, ValueError, IndexError):
            continue
    found = {parent}
    while True:
        more = {pid for pid, ppid in parents.items() if ppid in found} - found
        if not more:
            return {pid: tokens[pid] for pid in found if pid != parent and pid in tokens}
        found.update(more)
