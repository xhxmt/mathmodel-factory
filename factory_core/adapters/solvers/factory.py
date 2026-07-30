from __future__ import annotations

import os
from pathlib import Path

from ...registry import SolverBackendRegistry
from .cloud_run import CloudRunHttpTransport, CloudRunSolverBackend, CloudTransport
from .local import LocalSolverBackend


def build_solver_backends(
    code_root: str | Path,
    *,
    cloud_transport: CloudTransport | None = None,
    quarantined: bool | None = None,
) -> SolverBackendRegistry:
    """Build the one solver registry used by CLI workers and the Web service."""
    is_quarantined = (
        os.getenv("CLOUD_SOLVER_QUARANTINED", "true").strip().lower() == "true"
        if quarantined is None
        else quarantined
    )
    registry = SolverBackendRegistry()
    registry.register("local", LocalSolverBackend(code_root))
    registry.register(
        "cloud_run",
        CloudRunSolverBackend(
            cloud_transport or CloudRunHttpTransport(),
            quarantined=is_quarantined,
        ),
    )
    return registry
