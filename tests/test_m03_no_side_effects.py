from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
M03_MODULES = tuple(
    ROOT / "factory_core" / name
    for name in (
        "classifier_identity.py",
        "classifier_implementation_manifest.py",
        "command_envelope.py",
        "contract_pins.py",
        "legacy_classifier_compat.py",
        "persisted_dirty_owner_implementation_manifest.py",
        "persisted_dirty_owner_policy.py",
        "project_snapshot_v0.py",
        "workflow_contract_v2.py",
    )
)


def test_m03_modules_have_no_process_network_provider_model_or_solver_dispatch() -> None:
    forbidden_imports = {
        "subprocess",
        "socket",
        "requests",
        "httpx",
        "urllib.request",
        "openai",
        "anthropic",
    }
    forbidden_calls = {
        "Popen",
        "run",
        "call",
        "check_call",
        "check_output",
        "system",
        "spawn",
        "fork",
        "socket",
        "urlopen",
    }
    findings = []
    for path in M03_MODULES:
        tree = ast.parse(path.read_bytes(), filename=path.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in forbidden_imports:
                        findings.append((path.name, node.lineno, alias.name))
            elif isinstance(node, ast.ImportFrom) and (node.module or "") in forbidden_imports:
                findings.append((path.name, node.lineno, node.module))
            elif isinstance(node, ast.Call):
                name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else ""
                )
                if name in forbidden_calls:
                    findings.append((path.name, node.lineno, name))
    assert findings == []


def test_non_snapshot_m03_modules_do_not_open_live_files_or_sqlite() -> None:
    allowed = {"project_snapshot_v0.py", "legacy_classifier_compat.py"}
    findings = []
    for path in M03_MODULES:
        if path.name in allowed:
            continue
        source = path.read_text(encoding="utf-8")
        for forbidden in (
            "read_bytes(",
            "read_text(",
            "write_bytes(",
            "write_text(",
            "sqlite3.connect",
            "time.time",
            "os.environ",
            "uuid.uuid",
            "random.",
        ):
            if forbidden in source:
                findings.append((path.name, forbidden))
    assert findings == []


def test_snapshot_connector_source_has_no_application_file_mutation_calls() -> None:
    path = ROOT / "factory_core/project_snapshot_v0.py"
    tree = ast.parse(path.read_bytes(), filename=path.name)
    forbidden = {
        "open",
        "touch",
        "write_bytes",
        "write_text",
        "unlink",
        "remove",
        "rename",
        "replace",
        "truncate",
        "mkdir",
        "makedirs",
    }
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        if name in forbidden:
            findings.append((node.lineno, name))
        if name == "open" and isinstance(node.func, ast.Attribute):
            findings.append((node.lineno, "os.open"))
    assert findings == []
