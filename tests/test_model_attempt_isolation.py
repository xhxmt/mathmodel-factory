from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from factory_core.adapters.infrastructure.process import ProcessResult
from factory_core.adapters.models.backends import AgyBackend, ApiAgentBackend, CodexCliBackend, ModelRequest


def request_for(project, prompt):
    return ModelRequest(
        project_dir=project, step_id=2, attempt=1, prompt=prompt,
        timeout_seconds=10, hang_timeout_seconds=5, model="gpt-5.6-luna",
        output_file=project / f"{prompt}.txt",
    )


def test_concurrent_logs_keep_failure_classification_per_call(tmp_path, monkeypatch):
    monkeypatch.setattr("factory_core.adapters.models.backends.time.strftime", lambda _: "same-second")
    barrier = Barrier(2)

    class Supervisor:
        def run(self, request):
            prompt = request.argv[-1]
            request.stdout_path.parent.mkdir(parents=True, exist_ok=True)
            with request.stdout_path.open("a", encoding="utf-8") as handle:
                handle.write("unsupported model\n" if prompt == "unsupported" else "connection reset\n")
            barrier.wait(timeout=5)
            return ProcessResult(1, False, 0.01, 123)

    backend = CodexCliBackend(tmp_path, Supervisor())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda text: backend.execute(request_for(tmp_path, text)), ["unsupported", "transient"]))

    assert results[0].error_class == "PERMANENT_MODEL_UNSUPPORTED"
    assert results[1].error_class == "TRANSIENT_MODEL_BACKEND"
    logs = [tmp_path / result.metadata["log"] for result in results]
    assert len(set(logs)) == 2
    assert [path.read_text() for path in logs] == ["unsupported model\n", "connection reset\n"]


@pytest.mark.parametrize("backend_type", [ApiAgentBackend])
def test_concurrent_file_based_calls_keep_distinct_prompts(tmp_path, monkeypatch, backend_type):
    monkeypatch.setattr("factory_core.adapters.models.backends.time.strftime", lambda _: "same-second")
    barrier = Barrier(2)

    class Supervisor:
        def run(self, request):
            prompt_path = Path(request.argv[request.argv.index("--prompt-file") + 1])
            barrier.wait(timeout=5)
            return ProcessResult(0, False, 0.01, 123, {"observed_prompt": prompt_path.read_text(), "prompt_path": str(prompt_path)})

    backend = backend_type(tmp_path, Supervisor())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda text: backend.execute(request_for(tmp_path, text)), ["stream_a", "stream_b"]))

    assert [result.returncode for result in results] == [0, 0]
    assert [result.metadata["observed_prompt"] for result in results] == ["stream_a", "stream_b"]
    assert len({result.metadata["prompt_path"] for result in results}) == 2
    assert len({result.metadata["log"] for result in results}) == 2


def test_concurrent_agy_calls_keep_distinct_prompts_and_logs(tmp_path, monkeypatch):
    """The agy CLI takes the prompt as ``-p``; each call still keeps its own prompt file and log."""

    monkeypatch.setattr("factory_core.adapters.models.backends.time.strftime", lambda _: "same-second")
    cli = tmp_path / "agy"
    cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cli.chmod(0o755)
    monkeypatch.setenv("AGY_CLI", str(cli))
    barrier = Barrier(2)

    class Supervisor:
        def run(self, request):
            prompt = request.argv[-1]
            request.stdout_path.parent.mkdir(parents=True, exist_ok=True)
            request.stdout_path.write_text(
                '{"event":"result","result":{"status":"SUCCESS","response":"%s"}}\n' % prompt,
                encoding="utf-8",
            )
            barrier.wait(timeout=5)
            return ProcessResult(0, False, 0.01, 123, {"observed_prompt": prompt})

    backend = AgyBackend(tmp_path, Supervisor())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda text: backend.execute(request_for(tmp_path, text)), ["stream_a", "stream_b"]))

    assert [result.returncode for result in results] == [0, 0]
    assert [result.metadata["observed_prompt"] for result in results] == ["stream_a", "stream_b"]
    files = [tmp_path / result.metadata["prompt_file"] for result in results]
    assert [path.read_text(encoding="utf-8") for path in files] == ["stream_a", "stream_b"]
    assert len({result.metadata["log"] for result in results}) == 2
