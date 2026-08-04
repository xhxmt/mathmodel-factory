from __future__ import annotations

import os
import shutil
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from ...domain import ExecutionResult
from ...registry import ModelBackendRegistry
from ..infrastructure.process import ProcessRequest, ProcessSupervisor


@dataclass(frozen=True)
class ModelRequest:
    project_dir: Path
    step_id: int
    attempt: int
    prompt: str
    timeout_seconds: int
    hang_timeout_seconds: int
    model: str = ""
    effort: str = ""
    output_file: Path | None = None
    context_files: tuple[str, ...] = ()
    effective_prompt_file: Path | None = None
    base_url: str = ""
    key_env: str = ""
    env: dict[str, str] = field(default_factory=dict)
    workdir: Path | None = None
    isolated: bool = False
    final_response_file: Path | None = None


class _ProcessModelBackend:
    name = "process"

    def __init__(self, factory_root: Path, supervisor: ProcessSupervisor | None = None):
        self.factory_root = factory_root.resolve()
        self.supervisor = supervisor or ProcessSupervisor()

    def _run(self, request: ModelRequest, argv: list[str], label: str) -> ExecutionResult:
        logs = request.project_dir / "logs"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        log = logs / f"step_{request.step_id}_{label}_{stamp}_{os.getpid()}.log"
        result = self.supervisor.run(
            ProcessRequest(
                argv=argv,
                cwd=request.workdir or request.project_dir,
                timeout_seconds=request.timeout_seconds,
                stdout_path=log,
                env={**os.environ, **request.env},
            )
        )
        metadata = {
            "backend": self.name,
            "model": request.model,
            "log": str(log.relative_to(request.project_dir)),
            "duration_seconds": result.duration_seconds,
        }
        if result.returncode == 0:
            return ExecutionResult.succeeded(**metadata)
        if result.metadata.get("launch_error"):
            error = "PERMANENT_BACKEND_UNAVAILABLE"
        elif self._unsupported_model_error(log):
            error = "PERMANENT_MODEL_UNSUPPORTED"
        else:
            error = "TRANSIENT_TIMEOUT" if result.timed_out else "TRANSIENT_MODEL_BACKEND"
        metadata.update(result.metadata)
        return ExecutionResult.failed(error, returncode=result.returncode, **metadata)

    @staticmethod
    def _unsupported_model_error(log: Path) -> bool:
        """Recognize stable model/configuration rejection without exposing log text."""
        try:
            text = log.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            return False
        markers = (
            "model is not supported",
            "model not supported",
            "unknown model",
            "unsupported model",
            "does not exist or you do not have access to it",
        )
        if "model \"" in text and "is not supported" in text:
            return True
        return any(marker in text for marker in markers)


class CodexCliBackend(_ProcessModelBackend):
    name = "codex"

    def execute(self, request: ModelRequest) -> ExecutionResult:
        effective_model = (
            request.model
            or request.env.get("CODEX_MODEL", "")
            or os.getenv("CODEX_MODEL", "")
        )
        request = replace(request, model=effective_model)
        argv = ["codex", "exec"]
        if effective_model:
            argv.extend(["--model", effective_model])
        argv.extend(["-c", f'model_reasoning_effort="{request.effort or "xhigh"}"'])
        if request.isolated:
            argv.extend(["--full-auto", "--ephemeral"])
            if request.final_response_file is not None:
                argv.extend(["--output-last-message", str(request.final_response_file)])
        else:
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        workdir = request.workdir or request.project_dir
        argv.extend(["-C", str(workdir), "--skip-git-repo-check", request.prompt])
        return self._run(replace(request, workdir=workdir), argv, "codex")


class ClaudeCliBackend(_ProcessModelBackend):
    name = "claude"

    def execute(self, request: ModelRequest) -> ExecutionResult:
        argv = ["claude", "-p", request.prompt, "--dangerously-skip-permissions", "--effort", request.effort or "max"]
        if request.model:
            argv.extend(["--model", request.model])
        return self._run(request, argv, "claude")


class AgyBackend(_ProcessModelBackend):
    name = "agy"

    def execute(self, request: ModelRequest) -> ExecutionResult:
        prompt_file = request.project_dir / "logs" / f"step_{request.step_id}_agy_{os.getpid()}.prompt.txt"
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(request.prompt, encoding="utf-8")
        python = self.factory_root / ".venv" / "bin" / "python3"
        if not python.is_file():
            python = Path(sys.executable)
        argv = [
            str(python),
            str(self.factory_root / "scripts" / "agy_run.py"),
            "--prompt-file",
            str(prompt_file),
            "--timeout-secs",
            str(max(60, request.timeout_seconds - 30)),
            "--workspace",
            str(request.project_dir),
            "--workspace",
            str(self.factory_root),
            "--model",
            request.model or "gemini-3.1-pro-preview",
        ]
        return self._run(request, argv, "agy")


class ApiAgentBackend(_ProcessModelBackend):
    name = "api"

    def execute(self, request: ModelRequest) -> ExecutionResult:
        if request.output_file is None:
            return ExecutionResult.failed("PERMANENT_OUTPUT_CONTRACT", returncode=2)
        try:
            output_file = request.output_file.resolve().relative_to(
                request.project_dir.resolve()
            ).as_posix()
            effective_prompt_file = (
                request.effective_prompt_file.resolve()
                .relative_to(request.project_dir.resolve())
                .as_posix()
                if request.effective_prompt_file is not None
                else None
            )
        except (OSError, ValueError):
            return ExecutionResult.failed(
                "PERMANENT_OUTPUT_CONTRACT",
                returncode=2,
                reason="API output paths must be inside the project",
            )
        prompt_file = request.project_dir / "logs" / f"step_{request.step_id}_api_{os.getpid()}.prompt.txt"
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(request.prompt, encoding="utf-8")
        argv = [
            sys.executable,
            str(self.factory_root / "scripts" / "api_agent_run.py"),
            "--model",
            request.model,
            "--backend",
            request.env.get("FACTORY_API_BACKEND", "openai"),
            "--prompt-file",
            str(prompt_file),
            "--project",
            str(request.project_dir),
            "--output-file",
            output_file,
            "--overwrite",
            "--timeout",
            str(max(60, request.timeout_seconds - 30)),
        ]
        if request.base_url:
            argv.extend(["--base-url", request.base_url])
        if request.key_env:
            argv.extend(["--key-env", request.key_env])
        if effective_prompt_file is not None:
            argv.extend(["--effective-prompt-file", effective_prompt_file])
        for context_file in request.context_files:
            argv.extend(["--context-file", context_file])
        return self._run(request, argv, "api")


def build_model_backends(factory_root: str | Path) -> ModelBackendRegistry:
    root = Path(factory_root).resolve()
    registry = ModelBackendRegistry()
    registry.register("codex", CodexCliBackend(root))
    registry.register("claude", ClaudeCliBackend(root))
    registry.register("agy", AgyBackend(root))
    registry.register("openai", ApiAgentBackend(root))
    registry.register("deepseek", ApiAgentBackend(root))
    registry.register("gemini", ApiAgentBackend(root))
    return registry
