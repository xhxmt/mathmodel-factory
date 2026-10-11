from __future__ import annotations

import os
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable
from uuid import uuid4

from ...domain import ExecutionResult
from ...deadline import cap_timeout, deadline_scope
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
    image_files: tuple[str, ...] = ()
    effective_prompt_file: Path | None = None
    base_url: str = ""
    key_env: str = ""
    env: dict[str, str] = field(default_factory=dict)
    workdir: Path | None = None
    isolated: bool = False
    final_response_file: Path | None = None
    deadline_epoch: int | None = None
    input_observer: Callable[[str, str], None] | None = None
    prompt_format: str = "raw"
    stdin_file: Path | None = None


class _ProcessModelBackend:
    name = "process"

    def __init__(self, factory_root: Path, supervisor: ProcessSupervisor | None = None):
        self.factory_root = factory_root.resolve()
        self.supervisor = supervisor or ProcessSupervisor()

    def _run(self, request: ModelRequest, argv: list[str], label: str) -> ExecutionResult:
        from scripts.document_evidence_view import image_inputs
        try:
            images = image_inputs(request.project_dir, request.image_files)
        except (OSError, ValueError) as exc:
            return ExecutionResult.failed("PERMANENT_INPUT_CONTRACT", returncode=2, reason=str(exc))
        with deadline_scope(request.deadline_epoch):
            timeout_seconds = cap_timeout(request.timeout_seconds)
        request = replace(request, timeout_seconds=timeout_seconds)
        if request.input_observer is not None:
            request.input_observer(request.prompt, request.prompt_format)
        logs = request.project_dir / "logs"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        log = logs / f"step_{request.step_id}_{label}_{stamp}_{os.getpid()}_{uuid4().hex}.log"
        result = self.supervisor.run(
            ProcessRequest(
                argv=argv,
                cwd=request.workdir or request.project_dir,
                timeout_seconds=timeout_seconds,
                stdout_path=log,
                env={**os.environ, **request.env},
                stdin_path=request.stdin_file,
            )
        )
        metadata = {
            **result.metadata,
            "backend": self.name,
            "model": request.model,
            "log": str(log.relative_to(request.project_dir)),
            "duration_seconds": result.duration_seconds,
            "process_pid": result.pid,
            "process_timed_out": result.timed_out,
        }
        if images:
            metadata["image_inputs"] = images
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

    def command(self, request: ModelRequest):
        codex_cli = (
            request.env.get("CODEX_CLI_PATH", "")
            or os.getenv("CODEX_CLI_PATH", "")
            or "codex"
        )
        effective_model = (
            request.model
            or request.env.get("CODEX_MODEL", "")
            or os.getenv("CODEX_MODEL", "")
        )
        service_tier = (
            request.env.get("CODEX_SERVICE_TIER", "")
            or os.getenv("CODEX_SERVICE_TIER", "")
        ).strip().lower()
        if service_tier and service_tier not in {"fast", "flex"}:
            return ExecutionResult.failed(
                "PERMANENT_MODEL_CONFIG",
                returncode=2,
                reason="CODEX_SERVICE_TIER must be unset, fast, or flex",
            )
        request = replace(request, model=effective_model)
        argv = [codex_cli]
        if service_tier:
            argv.extend(["-c", f'service_tier="{service_tier}"'])
        argv.append("exec")
        if effective_model:
            argv.extend(["--model", effective_model])
        argv.extend(["-c", f'model_reasoning_effort="{request.effort or "xhigh"}"'])
        if request.isolated:
            argv.extend(["--sandbox", "workspace-write", "-c", 'approval_policy="never"', "--ephemeral"])
            if request.final_response_file is not None:
                argv.extend(["--output-last-message", str(request.final_response_file)])
        else:
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        workdir = request.workdir or request.project_dir
        for image in request.image_files:
            argv.extend(["--image", str(request.project_dir / image)])
        argv.extend(["-C", str(workdir), "--skip-git-repo-check", "--", request.prompt])
        return replace(request, workdir=workdir), argv

    def execute(self, request: ModelRequest) -> ExecutionResult:
        command = self.command(request)
        if isinstance(command, ExecutionResult):
            return command
        configured, argv = command
        return self._run(configured, argv, "codex")


class ClaudeCliBackend(_ProcessModelBackend):
    name = "claude"

    def execute(self, request: ModelRequest) -> ExecutionResult:
        if request.image_files:
            return ExecutionResult.failed("PERMANENT_MULTIMODAL_UNSUPPORTED", returncode=2,
                reason="Claude CLI adapter has no verified image transport")
        argv = ["claude", "-p", request.prompt, "--dangerously-skip-permissions", "--effort", request.effort or "max"]
        if request.model:
            argv.extend(["--model", request.model])
        return self._run(request, argv, "claude")


class DshBackend(_ProcessModelBackend):
    """Drive one workflow step with a DeepSeek Harness headless run.

    ``dsh --profile headless`` answers one task with the same agentic agent as the
    interactive surfaces - file tools, shell, the project as its workspace - prints
    the final answer, and exits.  Unlike the HTTP API route it can therefore *create*
    a step's artefacts, which is what every prompt step needs.

    Three facts about the launcher decide whether a run happened at all, and each
    fails quietly if it is assumed rather than checked:

    * the launcher's only dispatch is ``if (import.meta.main) await runCli()``, an
      expression Node older than 24 does not have.  On an older Node the process
      exits **0 having done nothing** - a step that looks successful and produced no
      artefacts.  The major version is verified before the run.
    * the mounted route reads its credential from ``DEEPSEEK_API_KEY``; a missing
      key is reported as such instead of surfacing later as an empty step.
    * the model is a property of the profile, so an explicit model is applied as a
      ``--patch`` overlay rather than as an argv flag.

    The task travels on stdin: a rendered step prompt is far larger than a
    comfortable argv element, and ``-`` is the launcher's documented spelling for
    "read the task from stdin".
    """

    name = "dsh"

    def _node_and_cli(self) -> tuple[str, str] | ExecutionResult:
        node = os.getenv("DSH_NODE", "").strip() or (shutil.which("node") or "")
        if not node:
            return ExecutionResult.failed(
                "PERMANENT_BACKEND_UNAVAILABLE", returncode=127,
                reason="no node executable found for the DeepSeek Harness launcher",
            )
        cli = os.getenv("DSH_CLI", "").strip()
        if not cli:
            found = shutil.which("dsh")
            if found:
                # The PATH entry is a symlink into the package; resolve it so the
                # argv names the launcher the version check was made against.
                cli = str(Path(found).resolve())
        if not cli or not Path(cli).is_file():
            return ExecutionResult.failed(
                "PERMANENT_BACKEND_UNAVAILABLE", returncode=127,
                reason=(
                    "the DeepSeek Harness launcher was not found; install it and set "
                    "DSH_CLI to its lib/bin.js (or put dsh on PATH)"
                ),
            )
        major = self._node_major(node)
        if major is not None and major < 24:
            return ExecutionResult.failed(
                "PERMANENT_BACKEND_UNAVAILABLE", returncode=127,
                reason=(
                    f"the DeepSeek Harness launcher needs node >= 24, found {major}: its "
                    "only dispatch is gated by `import.meta.main`, which an older node "
                    "does not have, so it exits 0 having run nothing"
                ),
            )
        return node, cli

    @staticmethod
    def _node_major(node: str) -> int | None:
        """The node major version, or None when it cannot be read."""

        try:
            completed = subprocess.run(
                [node, "--version"], capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError):
            return None
        match = re.fullmatch(r"v?(\d+)\..*", completed.stdout.strip())
        return int(match.group(1)) if match else None

    def command(self, request: ModelRequest):
        if request.image_files:
            # The harness can read an image only when a vision model and an
            # attachment path are wired for it; silently dropping the pages would
            # produce a confident answer about evidence the model never saw.
            return ExecutionResult.failed(
                "PERMANENT_MULTIMODAL_UNSUPPORTED", returncode=2,
                reason="the dsh adapter has no verified image transport",
            )
        resolved = self._node_and_cli()
        if isinstance(resolved, ExecutionResult):
            return resolved
        node, cli = resolved
        key_env = (
            request.key_env or os.getenv("DSH_API_KEY_ENV", "") or "DEEPSEEK_API_KEY"
        ).strip()
        if not (request.env.get(key_env) or os.getenv(key_env)):
            return ExecutionResult.failed(
                "PERMANENT_MODEL_CONFIG", returncode=2, missing_key_env=key_env,
                reason=f"{key_env} is required by the mounted route and is not in the environment",
            )
        logs = request.project_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        stem = f"step_{request.step_id}_dsh_{os.getpid()}_{uuid4().hex}"
        prompt_file = logs / f"{stem}.prompt.txt"
        prompt_file.write_text(request.prompt, encoding="utf-8")
        argv = [
            node,
            cli,
            "--profile",
            os.getenv("DSH_PROFILE", "").strip() or "headless",
        ]
        model = (request.model or "").strip()
        if model:
            provider = os.getenv("DSH_PROVIDER", "").strip() or "deepseek-official"
            patch_file = logs / f"{stem}.model.yml"
            patch_file.write_text(
                "- id: agent-default-model\n"
                "  config:\n"
                f"    provider: {provider}\n"
                f"    model: {model}\n",
                encoding="utf-8",
            )
            argv.extend(["--patch", str(patch_file)])
        argv.append("-")
        return replace(request, stdin_file=prompt_file), argv

    def execute(self, request: ModelRequest) -> ExecutionResult:
        command = self.command(request)
        if isinstance(command, ExecutionResult):
            return command
        configured, argv = command
        return self._run(configured, argv, "dsh")


class AgyBackend(_ProcessModelBackend):
    name = "agy"

    def execute(self, request: ModelRequest) -> ExecutionResult:
        if request.image_files:
            return ExecutionResult.failed("PERMANENT_MULTIMODAL_UNSUPPORTED", returncode=2,
                reason="Agy adapter has no verified image transport")
        prompt_file = request.project_dir / "logs" / f"step_{request.step_id}_agy_{os.getpid()}_{uuid4().hex}.prompt.txt"
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
        if request.image_files and (request.model.startswith("deepseek") or request.env.get("FACTORY_API_BACKEND") == "claude"):
            return ExecutionResult.failed("PERMANENT_MULTIMODAL_UNSUPPORTED", returncode=2,
                reason="selected API model/backend cannot receive required page images")
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
        prompt_file = request.project_dir / "logs" / f"step_{request.step_id}_api_{os.getpid()}_{uuid4().hex}.prompt.txt"
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
        from scripts.document_evidence_view import image_inputs
        try:
            images = image_inputs(request.project_dir, request.image_files)
        except (OSError, ValueError) as exc:
            return ExecutionResult.failed("PERMANENT_INPUT_CONTRACT", returncode=2, reason=str(exc))
        for image in request.image_files:
            argv.extend(["--image-file", image])
        if images:
            argv.extend(["--expected-image-inputs-sha256", hashlib.sha256(
                json.dumps(images, sort_keys=True).encode()).hexdigest()])
        if request.input_observer is not None:
            from scripts.api_agent_run import build_effective_prompt

            try:
                effective, _ = build_effective_prompt(
                    request.project_dir.resolve(), request.prompt,
                    list(request.context_files), output_file,
                )
            except (OSError, ValueError) as exc:
                return ExecutionResult.failed("PERMANENT_INPUT_CONTRACT", returncode=2, reason=str(exc))
            argv.extend(["--expected-effective-prompt-sha256",
                         hashlib.sha256(effective.encode("utf-8")).hexdigest()])
            request = replace(request, prompt=effective, prompt_format="api-inline-v1")
        return self._run(request, argv, "api")


def build_model_backends(factory_root: str | Path) -> ModelBackendRegistry:
    root = Path(factory_root).resolve()
    registry = ModelBackendRegistry()
    registry.register("codex", CodexCliBackend(root))
    registry.register("claude", ClaudeCliBackend(root))
    registry.register("agy", AgyBackend(root))
    registry.register("dsh", DshBackend(root))
    registry.register("openai", ApiAgentBackend(root))
    registry.register("deepseek", ApiAgentBackend(root))
    registry.register("gemini", ApiAgentBackend(root))
    return registry
