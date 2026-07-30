from __future__ import annotations

import os
import signal
import subprocess
import time
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


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    timed_out: bool
    duration_seconds: float
    pid: int
    metadata: dict[str, object] = field(default_factory=dict)


class ProcessSupervisor:
    def run(self, request: ProcessRequest) -> ProcessResult:
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
                while process.poll() is None:
                    if request.heartbeat is not None:
                        request.heartbeat()
                    if time.monotonic() >= deadline:
                        timed_out = True
                        self._terminate_group(process, request.kill_grace_seconds)
                        break
                    time.sleep(min(request.poll_seconds, max(0.01, deadline - time.monotonic())))
                returncode = process.wait()
            finally:
                if close_stderr:
                    stderr_handle.close()
        return ProcessResult(
            returncode=124 if timed_out else returncode,
            timed_out=timed_out,
            duration_seconds=time.monotonic() - started,
            pid=process.pid,
        )

    @staticmethod
    def _terminate_group(process: subprocess.Popen, grace_seconds: float = 10.0) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (PermissionError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=grace_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (PermissionError, ProcessLookupError):
            pass
        process.wait()
