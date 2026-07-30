import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import REPO_ROOT
from factory_core.storage import SQLiteStateStore


RUNNER = Path(REPO_ROOT) / "run_paper.sh"


def make_project(root: Path, name: str = "demo") -> Path:
    project = root / "ongoing" / name
    (project / "problem").mkdir(parents=True)
    (project / "problem" / "problem_brief.md").write_text("# problem\n", encoding="utf-8")
    (project / "checkpoint.md").write_text(
        f"- **Base name**: {name}\n- **Last completed step**: 4\n",
        encoding="utf-8",
    )
    return project


def test_public_infer_step_reads_authoritative_sqlite_for_migrated_project(tmp_path):
    project = make_project(tmp_path)
    SQLiteStateStore(project).initialize(
        project_id="demo", project_type="modeling", last_completed_step=7
    )

    result = subprocess.run(
        [str(RUNNER), "--infer-step", str(project)],
        env={**os.environ, "FACTORY": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "7"


def test_cli_migration_inspect_and_apply_requires_report_digest(tmp_path):
    project = make_project(tmp_path)
    report_path = tmp_path / "migration.json"
    env = {**os.environ, "FACTORY": str(tmp_path), "PYTHONPATH": REPO_ROOT}

    inspected = subprocess.run(
        [
            sys.executable,
            "-m",
            "factory_core.cli",
            "migrate",
            "inspect",
            str(project),
            "--report",
            str(report_path),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    # This minimal fixture infers Step 0, so the report safely blocks the
    # checkpoint mismatch instead of silently importing it.
    assert inspected.returncode == 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["digest"]
    assert any(item.startswith("CHECKPOINT_MISMATCH") for item in report["conflicts"])


def test_cli_migration_apply_and_rollback_preserve_legacy_infer_compatibility(tmp_path):
    project = make_project(tmp_path)
    (project / "checkpoint.md").write_text(
        "- **Base name**: demo\n- **Last completed step**: 0\n", encoding="utf-8"
    )
    report_path = tmp_path / "migration.json"
    env = {**os.environ, "FACTORY": str(tmp_path), "PYTHONPATH": REPO_ROOT}
    inspected = subprocess.run(
        [
            sys.executable,
            "-m",
            "factory_core.cli",
            "migrate",
            "inspect",
            str(project),
            "--report",
            str(report_path),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert inspected.returncode == 0, inspected.stderr
    digest = json.loads(inspected.stdout)["digest"]

    applied = subprocess.run(
        [
            sys.executable,
            "-m",
            "factory_core.cli",
            "migrate",
            "apply",
            str(project),
            "--report",
            str(report_path),
            "--digest",
            digest,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert applied.returncode == 0, applied.stderr
    assert SQLiteStateStore(project).load().control_mode == "engine"

    rollback = subprocess.run(
        [
            sys.executable,
            "-m",
            "factory_core.cli",
            "migrate",
            "rollback",
            str(project),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rollback.returncode == 0, rollback.stderr
    assert SQLiteStateStore(project).load().control_mode == "legacy"
    inferred = subprocess.run(
        [str(RUNNER), "--infer-step", str(project)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert inferred.returncode == 0, inferred.stderr
    assert inferred.stdout.strip() == "0"


def test_public_runner_rejects_retired_social_science_execution(tmp_path):
    project = tmp_path / "ongoing" / "social"
    project.mkdir(parents=True)
    (project / "project_brief.md").write_text("# legacy\n", encoding="utf-8")
    (project / "checkpoint.md").write_text(
        "- **Last completed step**: 2\n", encoding="utf-8"
    )

    result = subprocess.run(
        [str(RUNNER), str(project)],
        env={**os.environ, "FACTORY": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 64
    assert "LEGACY_DOMAIN_RETIRED" in result.stderr
