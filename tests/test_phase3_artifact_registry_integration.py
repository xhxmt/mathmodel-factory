from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from factory_core.owner_compiler import compile_owner_registry
from shadow_contracts.artifact_registry import (
    register_artifact_owner,
    validate_artifact_registration,
)


def test_current_registry_registration_freezes_the_normal_canonical_result_owner():
    compilation = compile_owner_registry()

    registered = register_artifact_owner(
        compilation, "results/canonical_results.json"
    )

    assert registered.owner_stage == 4
    assert registered.owner_id == "owner:stage:4"
    assert registered.semantic_domain == "canonical_result"
    assert registered.dirty_flag == "RESULT_DIRTY"
    assert validate_artifact_registration(
        registered, expected_compilation=compilation
    ) is registered


def test_production_cli_import_does_not_load_direct_test_only_phase3_contract():
    root = Path(__file__).resolve().parents[1]
    script = """
import sys
import factory_core.cli

assert "shadow_contracts" not in sys.modules
assert "shadow_contracts.artifact_registry" not in sys.modules
print("phase3-shadow-not-in-production-imports")
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
    assert completed.stdout == "phase3-shadow-not-in-production-imports\n"
