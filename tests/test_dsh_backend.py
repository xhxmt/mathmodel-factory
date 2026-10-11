"""The DeepSeek Harness backend: an agentic route that does not need codex.

The launcher facts these tests pin are the ones with quiet failure modes: a Node
without ``import.meta.main`` exits 0 having run nothing, a missing credential
produced an empty step, and a task that never reached stdin would be an empty task.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from factory_core.adapters.infrastructure.process import ProcessResult
from factory_core.adapters.models.backends import DshBackend, ModelRequest


class RecordingSupervisor:
    def __init__(self, returncode: int = 0) -> None:
        self.request = None
        self.returncode = returncode

    def run(self, request):
        self.request = request
        return ProcessResult(self.returncode, False, 0.01, 123)


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _node(tmp_path: Path, version: str = "v24.21.0") -> Path:
    return _script(
        tmp_path / "node",
        f'if [ "$1" = "--version" ]; then echo "{version}"; exit 0; fi\n'
        'cat > "$DSH_TEST_STDIN"\necho LOGGED\nexit 0\n',
    )


def _request(tmp_path: Path, **overrides) -> ModelRequest:
    values = dict(
        project_dir=tmp_path,
        step_id=0,
        attempt=1,
        prompt="解析这道题目",
        timeout_seconds=30,
        hang_timeout_seconds=10,
        model="deepseek-flash",
    )
    values.update(overrides)
    return ModelRequest(**values)


def _backend(tmp_path: Path, monkeypatch, *, version: str = "v24.21.0", key: str | None = "sk-test"):
    node = _node(tmp_path, version)
    cli = _script(tmp_path / "dsh-bin.js", "exit 0\n")
    monkeypatch.setenv("DSH_NODE", str(node))
    monkeypatch.setenv("DSH_CLI", str(cli))
    if key is None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    else:
        monkeypatch.setenv("DEEPSEEK_API_KEY", key)
    return DshBackend(tmp_path)


def test_the_task_travels_on_stdin_and_the_model_as_a_patch(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch)
    supervisor = RecordingSupervisor()

    result = DshBackend(tmp_path, supervisor).execute(_request(tmp_path))

    assert result.returncode == 0
    argv = [str(part) for part in supervisor.request.argv]
    assert argv[argv.index("--profile") + 1] == "headless"
    assert argv[-1] == "-", "the task is read from stdin, not argv"
    assert supervisor.request.stdin_path.is_file()
    assert supervisor.request.stdin_path.read_text(encoding="utf-8") == "解析这道题目"
    patch = Path(argv[argv.index("--patch") + 1])
    assert patch.read_text(encoding="utf-8") == (
        "- id: agent-default-model\n"
        "  config:\n"
        "    provider: deepseek-official\n"
        "    model: deepseek-flash\n"
    )


def test_a_node_without_import_meta_main_is_refused_not_silently_successful(tmp_path, monkeypatch):
    """Node < 24 skips the launcher's only dispatch and exits 0 with no output."""

    backend = _backend(tmp_path, monkeypatch, version="v20.19.2")

    result = backend.execute(_request(tmp_path))

    assert result.error_class == "PERMANENT_BACKEND_UNAVAILABLE"
    assert "node >= 24" in result.metadata["reason"]
    assert "import.meta.main" in result.metadata["reason"]


def test_a_missing_credential_is_named_before_the_run(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch, key=None)

    result = backend.execute(_request(tmp_path, key_env="DEEPSEEK_API_KEY"))

    assert result.error_class == "PERMANENT_MODEL_CONFIG"
    assert result.metadata["missing_key_env"] == "DEEPSEEK_API_KEY"


def test_page_images_are_refused_rather_than_dropped(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch)

    result = backend.execute(_request(tmp_path, image_files=("problem/page-1.png",)))

    assert result.error_class == "PERMANENT_MULTIMODAL_UNSUPPORTED"


def test_a_missing_launcher_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_NODE", str(_node(tmp_path)))
    monkeypatch.setenv("DSH_CLI", str(tmp_path / "absent.js"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    result = DshBackend(tmp_path).execute(_request(tmp_path))

    assert result.error_class == "PERMANENT_BACKEND_UNAVAILABLE"
    assert "DSH_CLI" in result.metadata["reason"]


def test_no_model_means_no_patch_overlay(tmp_path, monkeypatch):
    """The profile's own default model stays in charge when none is named."""

    _backend(tmp_path, monkeypatch)
    supervisor = RecordingSupervisor()

    DshBackend(tmp_path, supervisor).execute(_request(tmp_path, model=""))

    argv = [str(part) for part in supervisor.request.argv]
    assert "--patch" not in argv
    assert argv[-1] == "-"


def test_the_launcher_receives_the_rendered_prompt_on_stdin(tmp_path, monkeypatch):
    """End to end through the supervisor: the stub launcher records what it read."""

    captured = tmp_path / "stdin.txt"
    backend = _backend(tmp_path, monkeypatch)
    monkeypatch.setenv("DSH_TEST_STDIN", str(captured))

    result = backend.execute(_request(tmp_path, prompt="line one\nline two\n"))

    assert result.returncode == 0
    assert captured.read_text(encoding="utf-8") == "line one\nline two\n"
