from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from .types import SolverRequest, SolverSubmission


class LocalSolverBackend:
    name = "local"

    def __init__(self, code_root: str | Path) -> None:
        self.code_root = Path(code_root).resolve()

    def submit(self, request: SolverRequest) -> SolverSubmission:
        command = self._command(request)
        job_dir = request.project_dir / ".factory" / "solver_jobs"
        job_dir.mkdir(parents=True, exist_ok=True)
        exit_file = job_dir / f"{request.job_id}.json"
        stdout = request.script.with_suffix(".log")
        stderr = request.project_dir / "logs" / f"{request.script.stem}_stderr.log"
        stderr.parent.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "factory_core.adapters.solvers.worker",
                "--exit-file",
                str(exit_file),
                "--cwd",
                str(request.script.parent),
                "--stdout",
                str(stdout),
                "--stderr",
                str(stderr),
                "--max-time",
                str(request.max_time_seconds),
                "--",
                *command,
            ],
            cwd=self.code_root,
            env={
                **os.environ,
                **request.env,
                "PYTHONPATH": os.pathsep.join(
                    filter(None, [str(self.code_root), os.environ.get("PYTHONPATH", "")])
                ),
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return SolverSubmission(
            external_id=str(process.pid),
            result_refs={
                "stdout": str(stdout.relative_to(request.project_dir)),
                "stderr": str(stderr.relative_to(request.project_dir)),
                "exit": str(exit_file.relative_to(request.project_dir)),
            },
        )

    def status(self, job: dict) -> str:
        exit_ref = job.get("result_refs", {}).get("exit")
        if exit_ref:
            path = Path(job["workdir"]) / exit_ref
            if not path.is_file():
                path = self._project_from_workdir(Path(job["workdir"])) / exit_ref
            if path.is_file():
                value = json.loads(path.read_text(encoding="utf-8"))
                return str(value.get("status", "failed"))
        pid = int(job.get("external_id") or 0)
        if pid and self._pid_live(pid):
            return "running"
        return "failed"

    def cancel(self, job: dict) -> None:
        pid = int(job.get("external_id") or 0)
        if not pid:
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except (PermissionError, ProcessLookupError):
            pass

    @staticmethod
    def _command(request: SolverRequest) -> list[str]:
        script = str(request.script)
        if request.runtime == "python":
            return [sys.executable, script, *request.args]
        if request.runtime == "julia":
            return ["julia", script, *request.args]
        if request.runtime in {"R", "r", "rscript", "Rscript"}:
            return ["Rscript", script, *request.args]
        if request.runtime == "matlab":
            return ["matlab", "-batch", f"cd('{request.script.parent}'); {request.script.stem}"]
        if request.runtime == "gurobi":
            return ["gurobi_cl", *request.args, script]
        raise ValueError(f"unsupported solver runtime: {request.runtime}")

    @staticmethod
    def _pid_live(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (PermissionError, ProcessLookupError):
            return False

    @staticmethod
    def _project_from_workdir(workdir: Path) -> Path:
        for candidate in (workdir, *workdir.parents):
            if (candidate / ".factory/state.db").is_file():
                return candidate
        return workdir
