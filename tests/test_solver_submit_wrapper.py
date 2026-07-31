import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from factory_core.service import FactoryService


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "solver_submit.sh"
EVIDENCE_FIELDS = {
    "schema",
    "job_id",
    "backend",
    "runtime",
    "script",
    "workdir",
    "status",
    "max_time_seconds",
    "requested_at",
}


def wrapper_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PATH"] = "/usr/bin:/bin"
    return env


def run_wrapper(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(WRAPPER), *args],
        cwd=project,
        env=wrapper_env(),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_native_wrapper_runs_from_project_and_binds_job_evidence(tmp_path):
    service = FactoryService(tmp_path)
    service.create_project("native-demo", "question", start=False)
    project = tmp_path / "ongoing" / "native-demo"
    script = project / "models" / "demo.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('native wrapper ok')\n", encoding="utf-8")

    submitted = run_wrapper(
        project,
        "--type",
        "python",
        "--max-time",
        "10",
        "models/demo.py",
    )
    assert submitted.returncode == 0, submitted.stderr
    job_id = submitted.stdout.strip()
    assert job_id.startswith("local_python_")

    waited = run_wrapper(project, "--wait", job_id)
    assert waited.returncode == 0, waited.stderr
    assert waited.stdout.strip() == "COMPLETED"

    evidenced = run_wrapper(project, "--status", job_id, "--json")
    assert evidenced.returncode == 0, evidenced.stderr
    payload = json.loads(evidenced.stdout)
    assert set(payload) == EVIDENCE_FIELDS
    assert payload == {
        "schema": "solver-job-evidence-v1",
        "job_id": job_id,
        "backend": "local",
        "runtime": "python",
        "script": str(script.resolve()),
        "workdir": str(script.parent.resolve()),
        "status": "COMPLETED",
        "max_time_seconds": 10,
        "requested_at": payload["requested_at"],
    }
    assert isinstance(payload["requested_at"], int)

    plain = run_wrapper(project, "--status", job_id)
    assert plain.returncode == 0, plain.stderr
    assert plain.stdout.strip() == "COMPLETED"


def test_legacy_wrapper_exposes_the_same_allowlisted_evidence(tmp_path):
    project = tmp_path / "legacy-demo"
    script = project / "models" / "demo.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('legacy wrapper fixture')\n", encoding="utf-8")

    job_id = f"local_python_test_{uuid.uuid4().hex}"
    job_dir = ROOT / "run_state" / "solver_jobs"
    meta = job_dir / f"{job_id}.meta"
    exit_file = tmp_path / f"{job_id}.exit"
    exit_file.write_text("0\n", encoding="utf-8")
    job_dir.mkdir(parents=True, exist_ok=True)
    requested_at = int(time.time())
    meta.write_text(
        "\n".join(
            [
                "type=python",
                "backend=local",
                f"script={script.resolve()}",
                f"workdir={script.parent.resolve()}",
                f"exit_code_file={exit_file}",
                "max_time=10",
                f"started={requested_at}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    try:
        evidenced = run_wrapper(project, "--status", job_id, "--json")
    finally:
        meta.unlink(missing_ok=True)
        exit_file.unlink(missing_ok=True)

    assert evidenced.returncode == 0, evidenced.stderr
    payload = json.loads(evidenced.stdout)
    assert set(payload) == EVIDENCE_FIELDS
    assert payload == {
        "schema": "solver-job-evidence-v1",
        "job_id": job_id,
        "backend": "local",
        "runtime": "python",
        "script": str(script.resolve()),
        "workdir": str(script.parent.resolve()),
        "status": "COMPLETED",
        "max_time_seconds": 10,
        "requested_at": requested_at,
    }
