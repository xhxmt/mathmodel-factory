from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SolverRequest:
    job_id: str
    project_dir: Path
    runtime: str
    script: Path
    args: tuple[str, ...] = ()
    max_time_seconds: int = 1_800
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SolverSubmission:
    external_id: str
    status: str = "running"
    result_refs: dict[str, str] = field(default_factory=dict)
