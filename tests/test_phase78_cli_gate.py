from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_default_cli_help_and_import_graph_do_not_expose_phase78() -> None:
    environment = os.environ.copy()
    environment.update({"PHASE78_ENABLED": "false", "PYTHONDONTWRITEBYTECODE": "1"})
    script = """
import json,sys
import factory_core.cli as cli
loaded=sorted(name for name in sys.modules if name.startswith('factory_core.phase7') or name.startswith('factory_core.phase8') or name.startswith('factory_core.phase78'))
print(json.dumps(loaded))
"""
    imported = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, env=environment,
        text=True, capture_output=True, check=False, timeout=30,
    )
    assert imported.returncode == 0, imported.stderr
    assert json.loads(imported.stdout) == []

    help_run = subprocess.run(
        [sys.executable, "-m", "factory_core.cli", "--help"], cwd=ROOT,
        env=environment, text=True, capture_output=True, check=False, timeout=30,
    )
    assert help_run.returncode == 0
    assert "phase78" not in help_run.stdout.lower()


def test_explicit_disabled_cli_short_circuits_before_request_path(tmp_path: Path) -> None:
    poison = tmp_path / "must-not-be-read.json"
    environment = os.environ.copy()
    environment.update({"PHASE78_ENABLED": "false", "PYTHONDONTWRITEBYTECODE": "1"})
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "factory_core.cli",
            "phase78",
            "submit",
            "demo",
            str(poison),
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert run.returncode == 64
    assert json.loads(run.stderr)["code"] == "PHASE78_SHADOW_DISABLED"
    assert not poison.exists()


def test_enabled_cli_rejects_noncanonical_work_key_before_resources(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "must-not-be-created"
    environment = os.environ.copy()
    environment.update(
        {
            "PHASE78_ENABLED": "true",
            "PHASE78_AUTHORITY_SOURCE_FENCE_SHA256": "a" * 64,
            "PHASE78_AUTHORITY_DB_FILE": str(runtime / "authority.db"),
            "PHASE78_PHASE6_DB_FILE": str(runtime / "phase6.db"),
            "PHASE78_PHASE7_DB_FILE": str(runtime / "phase7.db"),
            "PHASE78_PHASE8_DB_FILE": str(runtime / "phase8.db"),
            "PHASE78_WORK_DB_FILE": str(runtime / "work.db"),
            "PHASE78_WORK_SPOOL": str(runtime / "spool"),
            "PHASE78_PROJECT_ROOT": str(runtime / "projects"),
            "PHASE78_CAS_ROOT": str(runtime / "cas"),
            "PHASE78_SCRATCH_ROOT": str(runtime / "scratch"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "factory_core.cli",
            "phase78",
            "status",
            "demo",
            "bad/key",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert run.returncode == 1
    assert json.loads(run.stderr) == {"code": "PHASE78_REQUEST_INVALID"}
    assert not runtime.exists()
