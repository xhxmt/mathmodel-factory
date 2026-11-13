"""The Antigravity CLI backend: ``agy -p`` with the account the CLI is logged in as.

The CLI facts these tests pin were observed on agy 1.5.5: ``-p`` takes the prompt
as its value (it never reads a plain stdin prompt), ``--output-format stream-json``
ends with a ``result`` event, and a turn cut off by ``--print-timeout`` carries
``finished_reason=print_timeout``.  Success is that event, not the exit status alone.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

from factory_core import preflight
from factory_core.adapters.infrastructure.process import ProcessResult
from factory_core.adapters.models.backends import (
    AGY_ARGV_PROMPT_LIMIT,
    AgyBackend,
    ModelRequest,
    agy_cli_path,
)


def _events(*, status="SUCCESS", response="DONE\n", finished_reason=None, model="gemini-4-carbon-high"):
    result = {"conversation_id": "c-1", "status": status, "response": response,
              "num_turns": 1, "usage": {"total_tokens": 12}}
    if finished_reason:
        result["finished_reason"] = finished_reason
    return [
        "Fetching available models...",
        json.dumps({"event": "init", "conversation_id": "c-1", "init": {"model": model}}),
        json.dumps({"event": "step_update", "step_update": {"step_index": 0, "state": "DONE"}}),
        json.dumps({"event": "result", "result": result}),
    ]


class FakeSupervisor:
    """Records the launch and writes the stream-json log the CLI would have."""

    def __init__(self, returncode: int = 0, lines: list[str] | None = None, timed_out: bool = False):
        self.request = None
        self.returncode = returncode
        self.lines = _events() if lines is None else lines
        self.timed_out = timed_out

    def run(self, request):
        self.request = request
        request.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        request.stdout_path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        return ProcessResult(self.returncode, self.timed_out, 0.01, 123)


def _cli(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / "agy"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _request(tmp_path: Path, **overrides) -> ModelRequest:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    values = dict(
        project_dir=project,
        step_id=9,
        attempt=1,
        prompt="撰写论文初稿",
        timeout_seconds=3_600,
        hang_timeout_seconds=1_800,
        model="gemini-4-carbon-high",
    )
    values.update(overrides)
    return ModelRequest(**values)


def _run(tmp_path, monkeypatch, supervisor=None, **overrides):
    monkeypatch.setenv("AGY_CLI", str(_cli(tmp_path)))
    supervisor = supervisor or FakeSupervisor()
    factory = tmp_path / "factory"
    factory.mkdir(exist_ok=True)
    result = AgyBackend(factory, supervisor).execute(_request(tmp_path, **overrides))
    return result, supervisor


def test_the_prompt_is_the_print_argument_with_both_workspaces(tmp_path, monkeypatch):
    result, supervisor = _run(tmp_path, monkeypatch)

    assert result.returncode == 0, result
    argv = [str(part) for part in supervisor.request.argv]
    assert argv[0] == str(tmp_path / "bin" / "agy")
    assert argv[argv.index("--model") + 1] == "gemini-4-carbon-high"
    assert argv[-2:] == ["-p", "撰写论文初稿"], "-p takes the prompt as its value"
    dirs = [argv[i + 1] for i, part in enumerate(argv) if part == "--add-dir"]
    assert dirs == [str((tmp_path / "project").resolve()), str((tmp_path / "factory").resolve())]
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--dangerously-skip-permissions" in argv
    assert "--disable-slash-commands" in argv
    assert "--effort" not in argv, "the effort is part of the model id unless configured"
    # The CLI's own limit fires before the supervisor's.
    assert argv[argv.index("--print-timeout") + 1] == "3570s"
    assert supervisor.request.timeout_seconds == 3_600
    assert supervisor.request.cwd == tmp_path / "project"
    assert result.metadata["prompt_transport"] == "argv"
    assert (tmp_path / "project" / result.metadata["prompt_file"]).read_text(encoding="utf-8") == "撰写论文初稿"
    assert result.metadata["agy_model"] == "gemini-4-carbon-high"
    assert result.metadata["agy_status"] == "SUCCESS"
    assert result.metadata["agy_usage"] == {"total_tokens": 12}


def test_a_configured_effort_is_passed_and_an_unknown_one_refused(tmp_path, monkeypatch):
    result, supervisor = _run(tmp_path, monkeypatch, effort="high")
    argv = [str(part) for part in supervisor.request.argv]
    assert result.returncode == 0
    assert argv[argv.index("--effort") + 1] == "high"

    refused, supervisor = _run(tmp_path, monkeypatch, supervisor=FakeSupervisor(), effort="ultra")
    assert refused.error_class == "PERMANENT_MODEL_CONFIG"
    assert supervisor.request is None


def test_an_oversized_prompt_stays_in_its_file(tmp_path, monkeypatch):
    prompt = "论" * (AGY_ARGV_PROMPT_LIMIT // 3 + 10)  # three UTF-8 bytes each
    result, supervisor = _run(tmp_path, monkeypatch, prompt=prompt)

    argv = [str(part) for part in supervisor.request.argv]
    prompt_file = tmp_path / "project" / result.metadata["prompt_file"]
    assert result.metadata["prompt_transport"] == "prompt-file"
    assert prompt_file.read_text(encoding="utf-8") == prompt
    assert str(prompt_file.resolve()) in argv[-1]
    assert len(argv[-1].encode("utf-8")) < 1_000


def test_exit_zero_without_a_successful_result_is_not_a_success(tmp_path, monkeypatch):
    no_result = FakeSupervisor(lines=_events()[:3])
    result, _ = _run(tmp_path, monkeypatch, supervisor=no_result)
    assert result.error_class == "TRANSIENT_MODEL_BACKEND"
    assert "without a result event" in result.metadata["reason"]

    errored = FakeSupervisor(lines=_events(status="ERROR", finished_reason="error"))
    result, _ = _run(tmp_path, monkeypatch, supervisor=errored)
    assert result.error_class == "TRANSIENT_MODEL_BACKEND"
    assert result.metadata["agy_finished_reason"] == "error"


def test_a_print_timeout_is_a_timeout(tmp_path, monkeypatch):
    cut = FakeSupervisor(returncode=3, lines=_events(finished_reason="print_timeout"))
    result, _ = _run(tmp_path, monkeypatch, supervisor=cut)
    assert result.error_class == "TRANSIENT_TIMEOUT"
    assert result.returncode == 3


def test_a_failed_turn_keeps_the_process_classification(tmp_path, monkeypatch):
    failed = FakeSupervisor(returncode=3, lines=["AGY_ERROR model request failed"])
    result, _ = _run(tmp_path, monkeypatch, supervisor=failed)
    assert result.error_class == "TRANSIENT_MODEL_BACKEND"
    assert result.returncode == 3


def test_the_final_response_is_written_when_requested(tmp_path, monkeypatch):
    target = tmp_path / "project" / "judge_outputs" / "final.txt"
    result, _ = _run(tmp_path, monkeypatch, supervisor=FakeSupervisor(lines=_events(response="VERDICT: PASS\n")),
                     final_response_file=target)
    assert result.returncode == 0
    assert target.read_text(encoding="utf-8") == "VERDICT: PASS\n"


def test_images_and_a_missing_cli_are_refused_before_launch(tmp_path, monkeypatch):
    supervisor = FakeSupervisor()
    result, _ = _run(tmp_path, monkeypatch, supervisor=supervisor, image_files=("figures/p1.png",))
    assert result.error_class == "PERMANENT_MULTIMODAL_UNSUPPORTED"
    assert supervisor.request is None

    monkeypatch.setenv("AGY_CLI", str(tmp_path / "nowhere" / "agy"))
    result = AgyBackend(tmp_path, supervisor).execute(_request(tmp_path))
    assert result.error_class == "PERMANENT_BACKEND_UNAVAILABLE"
    assert supervisor.request is None


def test_the_cli_is_found_on_path_then_in_the_installer_directory(tmp_path, monkeypatch):
    cli = _cli(tmp_path)
    assert agy_cli_path({"PATH": str(cli.parent)}) == str(cli)

    home = tmp_path / "home"
    installed = home / ".local" / "bin" / "agy"
    installed.parent.mkdir(parents=True)
    installed.write_text("#!/bin/sh\n", encoding="utf-8")
    installed.chmod(installed.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("HOME", str(home))
    assert agy_cli_path({"PATH": str(tmp_path / "empty")}) == str(installed)


def _entry(model="gemini-4-carbon-high"):
    return {"backend": "agy", "model": model, "key_env": "GEMINI_API_KEY"}


def test_preflight_needs_no_api_key_and_checks_the_offered_models(tmp_path, monkeypatch):
    cli = _cli(tmp_path)
    environ = {"AGY_CLI": str(cli)}
    monkeypatch.setattr(preflight, "_agy_models", lambda *_: ["gemini-4-carbon-high", "gemini-4-argon-high"])

    offered = preflight.check_model("agy-carbon", _entry(), tmp_path, environ=environ)
    assert offered.status == preflight.PASS, offered.detail
    assert "credential" not in offered.detail, "the CLI uses its own login"

    missing = preflight.check_model("agy-x", _entry("gemini-3.1-pro-preview"), tmp_path, environ=environ)
    assert missing.status == preflight.FAIL

    monkeypatch.setattr(preflight, "_agy_models", lambda *_: None)
    undecided = preflight.check_model("agy-carbon", _entry(), tmp_path, environ=environ)
    assert undecided.status == preflight.UNKNOWN

    offline = preflight.check_model("agy-carbon", _entry(), tmp_path, environ=environ, http_get=None)
    assert offline.status == preflight.UNKNOWN

    absent = preflight.check_model("agy-carbon", _entry(), tmp_path,
                                   environ={"AGY_CLI": str(tmp_path / "nowhere")})
    assert absent.status == preflight.FAIL


def test_the_model_list_is_the_first_column_of_agy_models(tmp_path):
    cli = tmp_path / "agy"
    cli.write_text(
        "#!/bin/sh\necho 'Fetching available models...' >&2\n"
        "printf 'gemini-4-carbon-high\\tGemini 4 Carbon (High)\\ngemini-4-argon-low\\tGemini 4 Argon (Low)\\n'\n",
        encoding="utf-8",
    )
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    assert preflight._agy_models(str(cli), {}, 10) == ["gemini-4-carbon-high", "gemini-4-argon-low"]
