"""Controlled synchronous process boundary for normal service integration tests."""
import os
from pathlib import Path
import subprocess

from factory_core.adapters.solvers.local import LocalSolverBackend
from factory_core.adapters.solvers.types import SolverSubmission
from factory_core.registry import SolverBackendRegistry
from factory_core.service import FactoryService


class ControlledPythonBackend:
    """Run only each test's small producer; no detached worker or provider."""
    name = "local"

    def submit(self, request):
        self.request = request
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(LocalSolverBackend._command(request), cwd=request.script.parent,
            env={**os.environ, **request.env, "PYTHONPATH": str(root)},
            capture_output=True, timeout=min(request.max_time_seconds, 10))
        self.result = result
        return SolverSubmission(request.job_id, "completed" if result.returncode == 0 else "failed",
            {"input_closure": f".factory/solver_jobs/{request.job_id}.inputs.json"})

    def status(self, job):
        return job["status"]


def controlled_service(tmp_path):
    backends = SolverBackendRegistry()
    backend = ControlledPythonBackend()
    backends.register("local", backend)
    service = FactoryService(tmp_path, solver_backends=backends)
    service.create_project("demo", "Estimate the result.", start=False)
    return service, tmp_path / "ongoing/demo", backend
