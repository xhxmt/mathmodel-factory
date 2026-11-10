from __future__ import annotations

from pathlib import Path

from ...registry import SolverBackendRegistry
from .local import LocalSolverBackend


def build_solver_backends(code_root: str | Path) -> SolverBackendRegistry:
    """Build the one solver registry used by CLI workers and the Web service."""
    registry = SolverBackendRegistry()
    registry.register("local", LocalSolverBackend(code_root))
    return registry
