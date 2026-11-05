"""Phase 1b: a failed Step reports every candidate and the most explanatory cause."""

from __future__ import annotations

from pathlib import Path

import pytest

from factory_core.adapters.models.backends import ModelRequest
from factory_core.adapters.models.dispatcher import ModelDispatcher
from factory_core.domain import ExecutionResult
from factory_core.registry import ModelBackendRegistry


class _Backend:
    def __init__(self, result: ExecutionResult) -> None:
        self.result = result

    def execute(self, request):
        return self.result


def _log(project: Path, name: str, text: str) -> str:
    path = project / "logs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path.relative_to(project))


def _dispatch(monkeypatch, tmp_path, results: dict[str, ExecutionResult], chain=("dsh-x", "claude-x", "codex-x")):
    backends = ModelBackendRegistry()
    for model_id, result in results.items():
        backends.register(model_id, _Backend(result))
    monkeypatch.setattr("factory_core.adapters.models.dispatcher.get_step_model_ids",
                        lambda *_args: (chain[0], chain[1]))
    monkeypatch.setattr(
        "factory_core.adapters.models.dispatcher.get_model_entry",
        lambda _path, model_id: {"backend": model_id, "model": model_id, "effort": "", "base_url": "", "key_env": ""},
    )
    request = ModelRequest(project_dir=tmp_path, step_id=9, attempt=1, prompt="draft",
                           timeout_seconds=10, hang_timeout_seconds=5)
    return ModelDispatcher(tmp_path, backends).execute(request, step_key=9, defaults=(chain[2],))


def test_a_402_on_the_primary_is_reported_over_missing_fallbacks(monkeypatch, tmp_path):
    # canary4 / run2: the DeepSeek route ran out of balance, then the codex
    # fallback failed to launch, and only "codex 127" reached the event.
    log = _log(tmp_path, "dsh.log", "request failed\nHTTP 402 Payment Required: Insufficient Balance\n")
    result = _dispatch(monkeypatch, tmp_path, {
        "dsh-x": ExecutionResult.failed("TRANSIENT_MODEL_BACKEND", returncode=1, log=log, duration_seconds=3.2),
        "claude-x": ExecutionResult.failed("PERMANENT_BACKEND_UNAVAILABLE", returncode=127, launch_error="ENOENT"),
        "codex-x": ExecutionResult.failed("PERMANENT_BACKEND_UNAVAILABLE", returncode=127, launch_error="ENOENT"),
    })

    assert result.error_class == "TRANSIENT_MODEL_BACKEND"
    assert result.metadata["primary_candidate"] == "dsh-x"
    assert result.metadata["provider_signal"] == "balance or quota exhausted"
    assert result.metadata["reason"] == "provider: balance or quota exhausted"
    attempts = result.metadata["candidate_attempts"]
    assert [attempt["model_id"] for attempt in attempts] == ["dsh-x", "claude-x", "codex-x"]
    assert [attempt["error_class"] for attempt in attempts] == [
        "TRANSIENT_MODEL_BACKEND", "PERMANENT_BACKEND_UNAVAILABLE", "PERMANENT_BACKEND_UNAVAILABLE",
    ]
    assert attempts[0]["provider_signal"] == "balance or quota exhausted"


def test_a_model_that_ran_explains_more_than_a_fallback_that_could_not_start(monkeypatch, tmp_path):
    result = _dispatch(monkeypatch, tmp_path, {
        "dsh-x": ExecutionResult.failed("PERMANENT_BACKEND_UNAVAILABLE", returncode=127, launch_error="ENOENT"),
        "claude-x": ExecutionResult.failed("TRANSIENT_TIMEOUT", returncode=124),
        "codex-x": ExecutionResult.failed("PERMANENT_BACKEND_UNAVAILABLE", returncode=127, launch_error="ENOENT"),
    })
    assert result.error_class == "TRANSIENT_TIMEOUT"
    assert result.metadata["primary_candidate"] == "claude-x"
    assert "provider_signal" not in result.metadata


@pytest.mark.parametrize("text", [
    "processed line 402 of 900\n",
    "token count 4029, budget 402\n",
])
def test_a_stray_402_is_not_a_provider_signal(monkeypatch, tmp_path, text):
    log = _log(tmp_path, "dsh.log", text)
    result = _dispatch(monkeypatch, tmp_path, {
        "dsh-x": ExecutionResult.failed("TRANSIENT_MODEL_BACKEND", returncode=1, log=log),
        "claude-x": ExecutionResult.failed("TRANSIENT_MODEL_BACKEND", returncode=1),
        "codex-x": ExecutionResult.failed("TRANSIENT_MODEL_BACKEND", returncode=1),
    })
    assert "provider_signal" not in result.metadata
    assert result.metadata["primary_candidate"] == "dsh-x"  # ties go to the earliest candidate


def test_a_success_after_a_failed_primary_is_still_a_success(monkeypatch, tmp_path):
    result = _dispatch(monkeypatch, tmp_path, {
        "dsh-x": ExecutionResult.failed("TRANSIENT_MODEL_BACKEND", returncode=1),
        "claude-x": ExecutionResult.succeeded(model="claude-x"),
        "codex-x": ExecutionResult.failed("TRANSIENT_MODEL_BACKEND", returncode=1),
    })
    assert result.returncode == 0
    assert result.metadata["model_id"] == "claude-x"


def test_each_attempt_carries_its_evidence(monkeypatch, tmp_path):
    log = _log(tmp_path, "dsh.log", "error: HTTP 402 Insufficient Balance\n")
    result = _dispatch(monkeypatch, tmp_path, {
        "dsh-x": ExecutionResult.failed("TRANSIENT_MODEL_BACKEND", returncode=1, log=log, duration_seconds=3.2,
                                        reason=""),
        "claude-x": ExecutionResult.failed("PERMANENT_BACKEND_UNAVAILABLE", returncode=127,
                                           launch_error="FileNotFoundError", message="claude: not found"),
        "codex-x": ExecutionResult.failed("TRANSIENT_TIMEOUT", returncode=124, duration_seconds=600.0),
    })
    first, second, third = result.metadata["candidate_attempts"]
    assert first == {"model_id": "dsh-x", "backend": "dsh-x", "error_class": "TRANSIENT_MODEL_BACKEND",
                     "returncode": 1, "log": log, "duration_seconds": 3.2,
                     "provider_signal": "balance or quota exhausted"}
    assert second["launch_error"] == "FileNotFoundError" and second["message"] == "claude: not found"
    assert third["duration_seconds"] == 600.0
    # An empty backend reason does not hide the provider signal.
    assert result.metadata["reason"] == "provider: balance or quota exhausted"


def test_a_quarantined_candidate_is_reported_as_skipped(monkeypatch, tmp_path):
    results = {
        "dsh-x": ExecutionResult.failed("PERMANENT_MODEL_UNSUPPORTED", returncode=2),
        "claude-x": ExecutionResult.failed("TRANSIENT_MODEL_BACKEND", returncode=1),
        "codex-x": ExecutionResult.failed("TRANSIENT_MODEL_BACKEND", returncode=1),
    }
    backends = ModelBackendRegistry()
    for model_id, result in results.items():
        backends.register(model_id, _Backend(result))
    monkeypatch.setattr("factory_core.adapters.models.dispatcher.get_step_model_ids", lambda *_a: ("dsh-x", "claude-x"))
    monkeypatch.setattr("factory_core.adapters.models.dispatcher.get_model_entry",
                        lambda _p, m: {"backend": m, "model": m, "effort": "", "base_url": "", "key_env": ""})
    dispatcher = ModelDispatcher(tmp_path, backends)
    request = ModelRequest(project_dir=tmp_path, step_id=9, attempt=1, prompt="draft",
                           timeout_seconds=10, hang_timeout_seconds=5)
    dispatcher.execute(request, step_key=9, defaults=("codex-x",))
    second = dispatcher.execute(request, step_key=9, defaults=("codex-x",))

    skipped = second.metadata["candidate_attempts"][0]
    assert skipped["model_id"] == "dsh-x"
    assert skipped["note"].startswith("skipped: quarantined")
    assert second.metadata["primary_candidate"] == "claude-x"


def test_candidates_skipped_by_codex_only_are_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_ONLY", "1")
    result = _dispatch(monkeypatch, tmp_path, {
        "dsh-x": ExecutionResult.succeeded(),
        "claude-x": ExecutionResult.succeeded(),
        "codex-x": ExecutionResult.succeeded(),
    })
    assert result.returncode != 0
    assert [attempt["note"] for attempt in result.metadata["candidate_attempts"]] == [
        "skipped: CODEX_ONLY allows only the codex backend"
    ] * 3


@pytest.mark.parametrize("text, signal", [
    ("# backoff: respect the provider rate limit when batching\nTraceback: KeyError 'x'\n", None),
    ("the overloaded operator is defined in model.py\n", None),
    ("stream error: rate limit exceeded, retrying in 10s\n", "rate limited"),
    ("reconnecting... We're currently experiencing high demand\n", "provider overloaded"),
    ("request failed with status 429\n", "rate limited"),
    ("Too Many Requests\n", "rate limited"),
])
def test_ambiguous_provider_phrases_need_a_failure_on_the_same_line(monkeypatch, tmp_path, text, signal):
    log = _log(tmp_path, "m.log", text)
    result = _dispatch(monkeypatch, tmp_path, {
        "dsh-x": ExecutionResult.failed("TRANSIENT_MODEL_BACKEND", returncode=1, log=log),
        "claude-x": ExecutionResult.failed("TRANSIENT_MODEL_BACKEND", returncode=1),
        "codex-x": ExecutionResult.failed("TRANSIENT_MODEL_BACKEND", returncode=1),
    })
    assert result.metadata.get("provider_signal") == signal


def test_a_zero_exit_without_an_artifact_is_never_a_provider_refusal(monkeypatch, tmp_path):
    log = _log(tmp_path, "m.log", "warning: rate limit exceeded, retrying\n")
    backends = ModelBackendRegistry()
    backends.register("only", _Backend(ExecutionResult.succeeded(log=log)))
    monkeypatch.setattr("factory_core.adapters.models.dispatcher.get_step_model_ids", lambda *_a: ("only", ""))
    monkeypatch.setattr("factory_core.adapters.models.dispatcher.get_model_entry",
                        lambda _p, m: {"backend": m, "model": m, "effort": "", "base_url": "", "key_env": ""})
    dispatcher = ModelDispatcher(tmp_path, backends, artifact_valid=lambda _step, _project: False)
    request = ModelRequest(project_dir=tmp_path, step_id=9, attempt=1, prompt="draft",
                           timeout_seconds=10, hang_timeout_seconds=5)
    result = dispatcher.execute(request, step_key=9, defaults=("only",))
    assert result.error_class == "TRANSIENT_ARTIFACT_MISSING"
    assert "provider_signal" not in result.metadata
