from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]

# Phase 7/8 code is deliberately packaged as reviewed, default-off shadow
# material.  None of these modules may become an implicit dependency of a
# production entry point.  Keep the entry-point inventory exact so adding a
# new service or worker requires an explicit isolation decision here.
LATER_PHASE_MODULES = frozenset(
    {
        "factory_core.data_egress",
        "factory_core.reference_evidence",
        "scripts.evidence_grounding",
    }
)
PRODUCTION_ENTRYPOINTS = {
    "cli": "factory_core.cli",
    "scheduler": "factory_core.shadow_scheduler",
    "service": "factory_core.service",
    "web": "web.backend.main",
    "worker": "factory_core.adapters.solvers.worker",
}


def _local_module_source(module_name: str) -> tuple[Path, bool] | None:
    relative = Path(*module_name.split("."))
    module_file = ROOT / relative.with_suffix(".py")
    if module_file.is_file():
        return module_file, False
    package_file = ROOT / relative / "__init__.py"
    if package_file.is_file():
        return package_file, True
    return None


def _absolute_import_base(node: ast.ImportFrom, package: str) -> str:
    if not node.level:
        return node.module or ""
    parts = package.split(".") if package else []
    keep = len(parts) - (node.level - 1)
    if keep < 0:
        return ""
    prefix = parts[:keep]
    if node.module:
        prefix.extend(node.module.split("."))
    return ".".join(prefix)


def _local_import_graph(entrypoint: str) -> frozenset[str]:
    visited: set[str] = set()
    pending = [entrypoint]
    while pending:
        module_name = pending.pop()
        if module_name in visited:
            continue
        source = _local_module_source(module_name)
        if source is None:
            continue
        visited.add(module_name)
        path, is_package = source
        tree = ast.parse(path.read_bytes(), filename=str(path))
        package = module_name if is_package else module_name.rpartition(".")[0]
        dependencies: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                dependencies.update(alias.name for alias in node.names)
                continue
            if not isinstance(node, ast.ImportFrom):
                continue
            base = _absolute_import_base(node, package)
            if base:
                dependencies.add(base)
            for alias in node.names:
                candidate = f"{base}.{alias.name}" if base else alias.name
                if _local_module_source(candidate) is not None:
                    dependencies.add(candidate)
        pending.extend(sorted(dependencies))
    return frozenset(visited)


def test_phase7_8_isolation_gate_covers_the_exact_production_entrypoint_set() -> None:
    assert PRODUCTION_ENTRYPOINTS == {
        "cli": "factory_core.cli",
        "scheduler": "factory_core.shadow_scheduler",
        "service": "factory_core.service",
        "web": "web.backend.main",
        "worker": "factory_core.adapters.solvers.worker",
    }
    assert len(set(PRODUCTION_ENTRYPOINTS.values())) == len(PRODUCTION_ENTRYPOINTS)
    assert all(_local_module_source(module) is not None for module in PRODUCTION_ENTRYPOINTS.values())
    assert all(_local_module_source(module) is not None for module in LATER_PHASE_MODULES)


@pytest.mark.parametrize(
    "entrypoint",
    PRODUCTION_ENTRYPOINTS.values(),
    ids=PRODUCTION_ENTRYPOINTS.keys(),
)
def test_production_transitive_import_graph_excludes_phase7_8(entrypoint: str) -> None:
    graph = _local_import_graph(entrypoint)

    assert entrypoint in graph
    assert graph.isdisjoint(LATER_PHASE_MODULES), {
        "entrypoint": entrypoint,
        "unexpected_later_phase_modules": sorted(graph & LATER_PHASE_MODULES),
    }


@pytest.mark.parametrize(
    "entrypoint",
    PRODUCTION_ENTRYPOINTS.values(),
    ids=PRODUCTION_ENTRYPOINTS.keys(),
)
def test_clean_process_import_does_not_load_phase7_8_modules(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "ADMIN_PASSWORD": "generated-isolation-test-password",
            "AUTH_DB_FILE": str(tmp_path / "auth.db"),
            "FACTORY_ROOT": str(tmp_path),
            "JWT_SECRET": "generated-isolation-key-material-32chars",
            "PHASE6_SNAPSHOT_ENABLED": "false",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    script = (
        "import importlib,json,sys;"
        f"importlib.import_module({entrypoint!r});"
        f"forbidden={sorted(LATER_PHASE_MODULES)!r};"
        "loaded=sorted(name for name in forbidden if name in sys.modules);"
        "print(json.dumps({'entrypoint':" + repr(entrypoint) + ",'loaded':loaded}));"
        "raise SystemExit(bool(loaded))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {
        "entrypoint": entrypoint,
        "loaded": [],
    }
