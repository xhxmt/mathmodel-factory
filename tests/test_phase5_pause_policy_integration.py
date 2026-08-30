from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from factory_core.adapters.infrastructure.pause_policy import (
    PauseAction,
    PauseMode,
    ProcessScopeKind,
    decide_pause_action,
)


def test_default_pause_mode_normal_scope_decisions():
    expected = {
        ProcessScopeKind.WORKER: PauseAction.TERMINATE_SCOPE,
        ProcessScopeKind.MODEL: PauseAction.TERMINATE_SCOPE,
        ProcessScopeKind.ATTACHED_SOLVER: PauseAction.TERMINATE_SCOPE,
        ProcessScopeKind.DURABLE_SOLVER: PauseAction.CONTINUE,
    }

    actual = {
        scope_kind: decide_pause_action(PauseMode.PAUSE, scope_kind).action
        for scope_kind in ProcessScopeKind
    }

    assert actual == expected


def test_production_cli_import_does_not_load_phase5_pause_policy():
    root = Path(__file__).resolve().parents[1]
    script = """
import sys
import factory_core.cli

assert "factory_core.adapters.infrastructure.pause_policy" not in sys.modules
print("phase5-pause-policy-not-in-production-imports")
"""
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout == "phase5-pause-policy-not-in-production-imports\n"
