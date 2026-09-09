from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
PHASE3_RUNTIME_MODULES = (
    "factory_core.phase3_artifacts",
    "factory_core.phase3_shadow_runtime",
    "factory_core.authority_production_writer",
    "factory_core.authority_read_repository",
)


def _run_import(script: str, *, environment: dict[str, str] | None = None):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if environment:
        env.update(environment)
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _local_module_source(module_name: str) -> tuple[Path, bool] | None:
    relative = Path(*module_name.split("."))
    module_file = ROOT / relative.with_suffix(".py")
    if module_file.is_file():
        return module_file, False
    package_file = ROOT / relative / "__init__.py"
    if package_file.is_file():
        return package_file, True
    return None


def _local_import_graph(entrypoint: str) -> set[str]:
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
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        package = module_name if is_package else module_name.rpartition(".")[0]
        dependencies: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                dependencies.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parts = package.split(".") if package else []
                    keep = len(parts) - (node.level - 1)
                    if keep < 0:
                        continue
                    prefix = parts[:keep]
                    if node.module:
                        prefix.extend(node.module.split("."))
                    base = ".".join(prefix)
                else:
                    base = node.module or ""
                if base:
                    dependencies.add(base)
                for alias in node.names:
                    candidate = f"{base}.{alias.name}" if base else alias.name
                    if _local_module_source(candidate) is not None:
                        dependencies.add(candidate)
        pending.extend(dependencies)
    return visited


def test_packaged_factory_core_includes_phase3_and_excludes_shadow_compatibility():
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    includes = configuration["tool"]["setuptools"]["packages"]["find"]["include"]
    packages = {
        ".".join(path.parent.relative_to(ROOT).parts)
        for path in ROOT.rglob("__init__.py")
        if ".git" not in path.parts
    }

    assert "factory_core*" in includes
    assert "factory_core" in packages
    assert "scripts" in packages
    assert (ROOT / "factory_core/phase3_artifacts.py").is_file()
    assert (ROOT / "factory_core/phase3_shadow_runtime.py").is_file()
    assert "shadow_contracts" in packages
    assert not any(pattern.startswith("shadow_contracts") for pattern in includes)


def test_cli_and_scheduler_import_graphs_do_not_load_phase3_runtime():
    module_names = repr(PHASE3_RUNTIME_MODULES)
    completed = _run_import(
        f"""
import sys
import factory_core.cli
import factory_core.service
import factory_core.shadow_scheduler

for name in {module_names}:
    assert name not in sys.modules, name
print("phase3-not-in-cli-scheduler-imports")
"""
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout == "phase3-not-in-cli-scheduler-imports\n"


def test_web_import_graph_does_not_load_phase3_runtime():
    graph = _local_import_graph("web.backend.main")

    assert "web.backend.main" in graph
    assert not set(PHASE3_RUNTIME_MODULES) & graph


def test_writer_imports_typed_values_but_never_imports_shadow_runner():
    completed = _run_import(
        """
import sys
import factory_core.authority_production_writer

assert "factory_core.phase3_artifacts" in sys.modules
assert "factory_core.phase3_shadow_runtime" not in sys.modules
print("phase3-runner-isolated-from-writer")
"""
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout == "phase3-runner-isolated-from-writer\n"
