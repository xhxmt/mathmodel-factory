from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cloud import solver_api, solver_runner


@pytest.mark.parametrize(
    "job_id",
    ["../escape", "/absolute", "with.dot", "space value", "a" * 65, "-prefix"],
)
def test_job_id_rejects_traversal_and_unsafe_identifiers(job_id):
    with pytest.raises(solver_runner.InputValidationError):
        solver_runner.validate_job_id(job_id)


@pytest.mark.parametrize(
    "path",
    ["../secret", "/etc/passwd", "nested/../escape", "nested\\escape", "a//b", "model..py"],
)
def test_working_file_path_rejects_ambiguous_or_traversing_names(path):
    with pytest.raises(solver_runner.InputValidationError):
        solver_runner.validate_relative_path(path, allow_nested=True)


def test_submission_rejects_duplicate_script_path_and_oversized_files():
    with pytest.raises(solver_runner.InputValidationError, match="unique"):
        solver_runner.validate_submission_files("solve.py", "print(1)", {"solve.py": "x = 1"})

    with pytest.raises(solver_runner.InputValidationError, match="single-file"):
        solver_runner.validate_submission_files(
            "solve.py",
            "print(1)",
            {"large.txt": "x" * (solver_runner.MAX_WORKING_FILE_BYTES + 1)},
        )


def test_environment_is_allowlisted_and_does_not_inherit_control_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_API_SECRET", "must-not-be-inherited")
    environment = solver_runner.build_solver_environment(
        tmp_path / "input",
        tmp_path / "output",
        {"OMP_NUM_THREADS": "2", "SOLVER_RANDOM_SEED": "42"},
    )

    assert environment["OMP_NUM_THREADS"] == "2"
    assert environment["SOLVER_RANDOM_SEED"] == "42"
    assert "CONTROL_API_SECRET" not in environment
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in environment

    with pytest.raises(solver_runner.InputValidationError, match="not allowed"):
        solver_runner.validate_env_vars({"LD_PRELOAD": "/tmp/attack.so"})


def _run_script(
    tmp_path: Path,
    monkeypatch,
    source: str,
    working_files: dict[str, str] | None = None,
):
    monkeypatch.delenv("SOLVER_RUN_UID", raising=False)
    monkeypatch.delenv("SOLVER_RUN_GID", raising=False)
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    jobs.mkdir()
    results.mkdir()
    input_dir, output_dir, script_path, identity = solver_runner.prepare_workspace(
        jobs / "safe-job",
        "solve.py",
        source,
        working_files or {"models/data.txt": "read-only input"},
    )
    outcome = solver_runner.run_solver(
        "python",
        script_path,
        input_dir,
        output_dir,
        results / "stdout.log",
        results / "stderr.log",
        max_time=5,
        env_vars={"OMP_NUM_THREADS": "1"},
        identity=identity,
    )
    return outcome, input_dir, output_dir, results


def test_task_sitecustomize_cannot_run_before_resource_limits(tmp_path, monkeypatch):
    sitecustomize = f"""
import resource
from pathlib import Path
marker = Path("site-limit.txt")
if not marker.exists():
    marker.write_text(str(resource.getrlimit(resource.RLIMIT_NOFILE)[0]))
"""
    outcome, _input_dir, output_dir, _results = _run_script(
        tmp_path,
        monkeypatch,
        "print('sitecustomize ran under limits')\n",
        {"sitecustomize.py": sitecustomize},
    )

    assert outcome["status"] == "completed"
    assert (output_dir / "site-limit.txt").read_text(encoding="utf-8") == str(
        solver_runner.MAX_OPEN_FILES
    )


def test_solver_uses_read_only_input_separate_output_and_clean_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_API_SECRET", "must-not-be-inherited")
    outcome, input_dir, output_dir, results = _run_script(
        tmp_path,
        monkeypatch,
        """
import json
import os
from pathlib import Path
assert os.getenv("CONTROL_API_SECRET") is None
assert Path(os.environ["SOLVER_INPUT_DIR"], "models/data.txt").read_text() == "read-only input"
Path("result.json").write_text(json.dumps({"cwd": str(Path.cwd()), "secret": os.getenv("CONTROL_API_SECRET")}))
print("bounded")
""",
    )

    assert outcome["status"] == "completed"
    assert stat.S_IMODE(input_dir.stat().st_mode) == 0o555
    assert stat.S_IMODE((input_dir / "models" / "data.txt").stat().st_mode) == 0o444
    result = json.loads((output_dir / "result.json").read_text(encoding="utf-8"))
    assert result == {"cwd": str(output_dir), "secret": None}
    assert (results / "stdout.log").read_text(encoding="utf-8").strip() == "bounded"


def test_solver_rejects_symbolic_link_output(tmp_path, monkeypatch):
    outcome, _input_dir, _output_dir, _results = _run_script(
        tmp_path,
        monkeypatch,
        "import os\nos.symlink('/etc/passwd', 'leak.txt')\n",
    )

    assert outcome["status"] == "failed"
    assert outcome["error_code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert "symbolic links" in outcome["error_message"]


def test_solver_stops_output_file_explosion(tmp_path, monkeypatch):
    outcome, _input_dir, _output_dir, _results = _run_script(
        tmp_path,
        monkeypatch,
        f"for i in range({solver_runner.MAX_OUTPUT_FILES + 1}):\n    open(f'out-{{i}}.txt', 'w').write('x')\n",
    )

    assert outcome["status"] == "failed"
    assert outcome["error_code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert "file count" in outcome["error_message"]


def test_solver_stops_output_directory_explosion(tmp_path, monkeypatch):
    outcome, _input_dir, _output_dir, _results = _run_script(
        tmp_path,
        monkeypatch,
        "from pathlib import Path\n"
        f"for i in range({solver_runner.MAX_OUTPUT_DIRECTORIES + 1}):\n"
        "    Path(f'dir-{i}').mkdir()\n",
    )

    assert outcome["status"] == "failed"
    assert outcome["error_code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert "directory count" in outcome["error_message"]


def test_solver_rejects_unsafe_output_path(tmp_path, monkeypatch):
    outcome, _input_dir, _output_dir, _results = _run_script(
        tmp_path,
        monkeypatch,
        "from pathlib import Path\nPath('unsafe name.txt').write_text('bad')\n",
    )

    assert outcome["status"] == "failed"
    assert outcome["error_code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert "unsafe path" in outcome["error_message"]


def test_output_collection_rejects_unsafe_names_and_skips_private_temp(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / ".tmp").mkdir()
    (output_dir / ".tmp" / "scratch.txt").write_text("temporary", encoding="utf-8")
    (output_dir / "safe.json").write_text("{}", encoding="utf-8")
    (output_dir / "unsafe name.txt").write_text("bad", encoding="utf-8")

    with pytest.raises(solver_runner.OutputLimitError, match="unsafe path"):
        list(solver_runner.collect_output_files(output_dir))

    (output_dir / "unsafe name.txt").unlink()
    collected = list(solver_runner.collect_output_files(output_dir))
    assert [(path.name, relative.as_posix()) for path, relative in collected] == [
        ("safe.json", "safe.json")
    ]


def test_api_rejects_oversized_body_before_json_parsing():
    client = TestClient(solver_api.app)
    response = client.post(
        "/solve/python",
        content=b"x" * (solver_runner.MAX_REQUEST_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["error_code"] == "REQUEST_TOO_LARGE"


def test_api_validation_does_not_echo_submitted_script(monkeypatch):
    monkeypatch.setenv("SOLVER_EXECUTION_ENABLED", "true")
    client = TestClient(solver_api.app)
    sensitive_script = "print('private-payload-marker')"
    response = client.post(
        "/solve/python",
        json={
            "solver_type": "python",
            "script_content": sensitive_script,
            "script_name": "../escape.py",
        },
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "INVALID_REQUEST"
    assert "private-payload-marker" not in response.text


def test_api_rejects_unavailable_runtime_at_submission(monkeypatch):
    monkeypatch.setenv("SOLVER_EXECUTION_ENABLED", "true")
    client = TestClient(solver_api.app)
    response = client.post(
        "/solve/julia",
        json={
            "solver_type": "julia",
            "script_content": "println(1)",
            "script_name": "solve.jl",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "RUNTIME_UNAVAILABLE"
    assert response.json()["detail"]["available_solvers"] == ["python"]


def test_api_rejects_parallel_job_on_same_instance(monkeypatch):
    monkeypatch.setenv("SOLVER_EXECUTION_ENABLED", "true")
    monkeypatch.setitem(
        solver_api.job_registry,
        "active-job",
        {"job_id": "active-job", "status": "running"},
    )
    client = TestClient(solver_api.app)
    try:
        response = client.post(
            "/solve/python",
            json={
                "solver_type": "python",
                "script_content": "print(1)",
                "script_name": "solve.py",
            },
        )
    finally:
        solver_api.job_registry.pop("active-job", None)

    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "INSTANCE_BUSY"
    assert response.headers["Retry-After"] == "5"


def test_api_rejects_invalid_job_id_on_read_routes():
    client = TestClient(solver_api.app)
    traversal_response = client.get("/jobs/..%2Fescape/status")
    response = client.get("/jobs/with.dot/status")

    assert traversal_response.status_code == 404
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_JOB_ID"
