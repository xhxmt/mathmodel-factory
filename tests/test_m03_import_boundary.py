from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NEW_MODULE_NAMES = {
    "factory_core.command_envelope",
    "factory_core.classifier_identity",
    "factory_core.classifier_implementation_manifest",
    "factory_core.contract_pins",
    "factory_core.legacy_classifier_compat",
    "factory_core.persisted_dirty_owner_implementation_manifest",
    "factory_core.persisted_dirty_owner_policy",
    "factory_core.project_snapshot_v0",
    "factory_core.workflow_contract_v2",
}
PRODUCTION_IMPORT_BOUNDARY = (
    "factory_core/engine.py",
    "factory_core/service.py",
    "factory_core/storage.py",
    "factory_core/transitions.py",
    "factory_core/adapters/legacy.py",
    "factory_core/shadow_scheduler.py",
    "web/backend/state_store.py",
    "web/backend/project_api.py",
)


def _imports(relative: str) -> set[str]:
    tree = ast.parse((ROOT / relative).read_bytes(), filename=relative)
    values = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            values.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = "factory_core." + module
            values.add(module.rstrip("."))
    return values


def test_existing_production_graph_does_not_import_m03_modules() -> None:
    for relative in PRODUCTION_IMPORT_BOUNDARY:
        assert _imports(relative).isdisjoint(NEW_MODULE_NAMES), relative


def test_legacy_classifier_hash_import_is_isolated_to_compatibility_module() -> None:
    offenders = []
    for path in sorted((ROOT / "factory_core").glob("*.py")):
        if path.name == "legacy_classifier_compat.py":
            continue
        if path.name in {"dirty.py", "engine.py", "dirty_rebase.py"}:
            continue
        if path.name in {
            "classifier_identity.py",
            "contract_pins.py",
            "project_snapshot_v0.py",
            "command_envelope.py",
            "workflow_contract_v2.py",
        }:
            tree = ast.parse(path.read_bytes(), filename=path.name)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and any(
                    alias.name == "classifier_contract_sha256" for alias in node.names
                ):
                    offenders.append(path.name)
                if isinstance(node, ast.Call) and (
                    (isinstance(node.func, ast.Name) and node.func.id == "classifier_contract_sha256")
                    or (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "classifier_contract_sha256"
                    )
                ):
                    offenders.append(path.name)
    assert offenders == []


def test_snapshot_builder_does_not_import_or_call_production_store() -> None:
    path = ROOT / "factory_core/project_snapshot_v0.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=path.name)
    assert all(
        not (
            isinstance(node, (ast.Import, ast.ImportFrom))
            and "storage" in ast.unparse(node)
        )
        for node in ast.walk(tree)
    )
    assert "immutable=1" not in source
    assert "nolock=1" not in source
    assert "journal_mode" not in source.lower()
    assert "locking_mode" not in source.lower()
    assert "persist_wal" not in source.lower()
    assert ".backup(" not in source
