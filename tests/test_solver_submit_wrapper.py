from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from factory_core.service import FactoryService


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "solver_submit.sh"
LEGACY_JOB_ROOT = ROOT / "run_state" / "solver_jobs"


def _solver_fixture(project: Path) -> tuple[Path, Path, Path]:
    script = project / "models" / "solve.py"
    input_path = project / "data" / "input.json"
    output = project / "results" / "p1" / "values.json"
    script.parent.mkdir(parents=True, exist_ok=True)
    input_path.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        """import json
from pathlib import Path
root = Path(__file__).resolve().parents[1]
value = json.loads((root / 'data/input.json').read_text())['value']
target = root / 'results/p1/values.json'
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps({'objective': value}) + '\\n')
""",
        encoding="utf-8",
    )
    input_path.write_text('{"value": 42}\n', encoding="utf-8")
    return script, input_path, output


def _run(project: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(WRAPPER), *args],
        cwd=project,
        env={**os.environ, "CLOUD_SOLVER_QUARANTINED": "true"},
        capture_output=True,
        text=True,
        timeout=20,
        check=check,
    )


def _submit(project: Path) -> str:
    completed = _run(
        project,
        "--type",
        "python",
        "--max-time",
        "10",
        "--input",
        "data/input.json",
        "--output",
        "results/p1/values.json",
        "--seed",
        "42",
        "models/solve.py",
    )
    return completed.stdout.strip()


def _wait_for_terminal(project: Path, job_id: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = _run(project, "--status", job_id).stdout.strip()
        if status != "RUNNING":
            assert status == "COMPLETED"
            return
        time.sleep(0.05)
    raise AssertionError(f"solver job did not finish: {job_id}")


def _assert_v2_evidence(project: Path, job_id: str) -> dict:
    evidence = json.loads(
        _run(project, "--status", job_id, "--json").stdout
    )
    assert evidence["schema"] == "solver-job-evidence-v2"
    assert evidence["job_id"] == job_id
    assert evidence["runtime"] == "python"
    assert evidence["status"] == "COMPLETED"
    assert evidence["receipt_ready"] is True
    if (project / ".factory/state.db").is_file():
        assert evidence["event_stream_bound"] is True
    assert evidence["submission"]["script"]["path"] == "models/solve.py"
    assert evidence["submission"]["inputs"][0]["path"] == "data/input.json"
    assert evidence["submission"]["seeds"] == ["42"]
    assert evidence["completion"]["outputs"][0]["path"] == "results/p1/values.json"
    assert evidence["completion"]["outputs"][0]["sha256"]
    return evidence


def test_native_wrapper_emits_ready_two_stage_receipt(tmp_path: Path) -> None:
    service = FactoryService(tmp_path)
    service.create_project("native", "test", start=False)
    project = tmp_path / "ongoing" / "native"
    _solver_fixture(project)

    job_id = _submit(project)
    _wait_for_terminal(project, job_id)
    evidence = _assert_v2_evidence(project, job_id)

    assert evidence["backend"] == "local"
    assert (project / f".factory/solver_receipts/{job_id}.submitted.json").is_file()
    assert (project / f".factory/solver_receipts/{job_id}.completed.json").is_file()


def test_legacy_wrapper_uses_same_fail_closed_v2_contract(tmp_path: Path) -> None:
    project = tmp_path / "legacy"
    project.mkdir()
    _solver_fixture(project)
    job_id = ""
    try:
        job_id = _submit(project)
        _wait_for_terminal(project, job_id)
        evidence = _assert_v2_evidence(project, job_id)

        assert evidence["backend"] == "local"
        assert (LEGACY_JOB_ROOT / f"{job_id}.submitted.json").is_file()
        assert (LEGACY_JOB_ROOT / f"{job_id}.completed.json").is_file()
    finally:
        if job_id:
            for suffix in (".meta", ".exit", ".submitted.json", ".completed.json"):
                (LEGACY_JOB_ROOT / f"{job_id}{suffix}").unlink(missing_ok=True)


def test_old_legacy_job_is_structured_but_never_receipt_ready(tmp_path: Path) -> None:
    project = tmp_path / "legacy"
    project.mkdir()
    job_id = f"local_python_legacy_{os.getpid()}"
    meta = LEGACY_JOB_ROOT / f"{job_id}.meta"
    exit_file = LEGACY_JOB_ROOT / f"{job_id}.exit"
    try:
        LEGACY_JOB_ROOT.mkdir(parents=True, exist_ok=True)
        exit_file.write_text("0\n", encoding="utf-8")
        meta.write_text(
            "\n".join(
                [
                    "pid=0",
                    "type=python",
                    "backend=local",
                    f"script={project / 'models/old.py'}",
                    f"workdir={project / 'models'}",
                    f"exit_code_file={exit_file}",
                    "max_time=10",
                    "started=100",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        evidence = json.loads(
            _run(project, "--status", job_id, "--json").stdout
        )

        assert evidence["schema"] == "solver-job-evidence-v2"
        assert evidence["receipt_ready"] is False
        assert evidence["submission"] is None
        assert evidence["completion"] is None
        assert evidence["claim_limit"] == "LEGACY_JOB_METADATA_ONLY"
    finally:
        meta.unlink(missing_ok=True)
        exit_file.unlink(missing_ok=True)
