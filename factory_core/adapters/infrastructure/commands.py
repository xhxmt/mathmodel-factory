from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .process import ProcessRequest, ProcessSupervisor


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    accepted: bool
    log_path: Path
    timed_out: bool = False
    metadata: dict[str, object] | None = None


class CommandRunner:
    """Run bounded repository tools with one process-supervision contract."""

    def __init__(self, supervisor: ProcessSupervisor | None = None) -> None:
        self.supervisor = supervisor or ProcessSupervisor()

    def run(
        self,
        project: Path,
        argv: Sequence[str | Path],
        *,
        label: str,
        timeout_seconds: int = 600,
        accepted: Iterable[int] = (0,),
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        log_path: Path | None = None,
    ) -> CommandResult:
        target = log_path or (
            project
            / "logs"
            / f"native_{label}_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.log"
        )
        result = self.supervisor.run(
            ProcessRequest(
                argv=[str(value) for value in argv],
                cwd=(cwd or project).resolve(),
                timeout_seconds=timeout_seconds,
                stdout_path=target,
                env={**os.environ, **(env or {})},
            )
        )
        return CommandResult(
            returncode=result.returncode,
            accepted=result.returncode in set(accepted),
            log_path=target,
            timed_out=result.timed_out,
            metadata=result.metadata,
        )

    def python(
        self,
        factory_root: Path,
        project: Path,
        script: str,
        args: Sequence[str | Path],
        **kwargs,
    ) -> CommandResult:
        return self.run(
            project,
            [sys.executable, factory_root / script, *args],
            cwd=factory_root,
            **kwargs,
        )
