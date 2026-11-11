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
        env=dict(os.environ),
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
