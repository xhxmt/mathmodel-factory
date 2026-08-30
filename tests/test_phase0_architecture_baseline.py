from __future__ import annotations

import ast
import json
import re
import warnings
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SCHEMA_SOURCE = ROOT / "factory_core" / "domain.py"
WRITER_ALLOWLIST = (
    ROOT / "docs" / "architecture" / "application_writer_allowlist_v1.json"
)
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "v1_characterization"
FIXTURE_INDEX = FIXTURE_ROOT / "index.json"

ACTIVE_SCHEMA_DOCS = (
    ROOT / "CLAUDE.md",
    ROOT / "README.md",
    ROOT / "STEPS.md",
    ROOT / "modeling_guide.md",
    ROOT / "docs" / "architecture" / "ORCHESTRATION_ENGINE.md",
    ROOT / "docs" / "architecture" / "RUNTIME_INFRASTRUCTURE_CONVERGENCE_PLAN.md",
    ROOT / "docs" / "architecture" / "STAGE_SIMPLIFICATION_PLAN.md",
)
PHASE0_LINK_DOCS = (
    ROOT / "DOCUMENTATION_INDEX.md",
    ROOT / "docs" / "architecture" / "ORCHESTRATION_ENGINE.md",
    ROOT / "docs" / "architecture" / "RUNTIME_INFRASTRUCTURE_CONVERGENCE_PLAN.md",
    ROOT / "docs" / "architecture" / "STAGE_SIMPLIFICATION_PLAN.md",
    ROOT
    / "docs"
    / "architecture"
    / "decisions"
    / "ADR-0001-phase0-source-truth.md",
    FIXTURE_ROOT / "README.md",
)
REQUIRED_CHARACTERIZATIONS = {
    "normal",
    "dirty",
    "semantic_reopen",
    "human_gate",
    "recovery",
    "packet_rebuild",
    "technical_terminal",
    "solver_10_of_10_receipt",
}
DIRECT_STORE_METHODS = {
    "record_decision",
    "supersede_pending_decision_request",
    "bind_prompt_attempt_input",
    "record_projection_failure",
    "resolve_projection_failure",
}
PRODUCTION_SCAN_ROOTS = (
    ROOT / "factory_core",
    ROOT / "web" / "backend",
    ROOT / "scripts",
)


def _runtime_schema_version() -> int:
    tree = ast.parse(RUNTIME_SCHEMA_SOURCE.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "SCHEMA_VERSION"
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("factory_core/domain.py does not define SCHEMA_VERSION")


def _is_store_receiver(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "store" or node.id.endswith("_store")
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "SQLiteStateStore"
    )


def _called_methods(path: Path) -> list[str]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in DIRECT_STORE_METHODS
        and _is_store_receiver(node.func.value)
    ]


def _current_direct_writer_callsites() -> Counter[tuple[str, str]]:
    calls: Counter[tuple[str, str]] = Counter()
    for scan_root in PRODUCTION_SCAN_ROOTS:
        for path in scan_root.rglob("*.py"):
            relative = path.relative_to(ROOT).as_posix()
            if relative in {"factory_core/storage.py", "factory_core/transitions.py"}:
                continue
            for method in _called_methods(path):
                calls[(relative, method)] += 1
    return calls


def _test_function_exists(node_id: str) -> bool:
    relative, separator, function_name = node_id.partition("::")
    if not separator or not function_name.startswith("test_"):
        return False
    path = ROOT / relative
    if not path.is_file():
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
        for node in tree.body
    )


def test_active_workflow_docs_match_runtime_schema_version() -> None:
    runtime_version = _runtime_schema_version()
    assert runtime_version == 9
    marker = re.compile(rf"schema[- ]v{runtime_version}\b", re.IGNORECASE)
    for path in ACTIVE_SCHEMA_DOCS:
        text = path.read_text(encoding="utf-8")
        message = f"{path}: missing workflow schema-v{runtime_version} marker"
        assert marker.search(text), message
        assert "Schema v8 also stores" not in text, path
        assert "database schema is version 7" not in text, path


def test_application_writer_inventory_matches_current_direct_callsites() -> None:
    payload = json.loads(WRITER_ALLOWLIST.read_text(encoding="utf-8"))
    assert payload["schema"] == "application-writer-allowlist-v1"
    assert payload["baseline_commit"] == "357947948f034325ea6202694c20bf435910d011"
    assert payload["runtime_schema_version"] == _runtime_schema_version()
    assert payload["enforcement"] == "characterization_only"
    assurance = payload["current_scan_assurance"]
    assert assurance["level"] == "naming_heuristic_only"
    assert assurance["may_miss_new_bypasses"] is True
    assert set(assurance["known_limitations"]) == {
        "no_type_resolution",
        "no_assignment_alias_tracking",
        "no_self_or_attribute_receiver_resolution",
        "no_cross_function_receiver_resolution",
        "may_miss_renamed_receiver_variables",
    }
    declared = Counter(
        {
            (entry["path"], entry["method"]): entry["occurrences"]
            for entry in payload["current_direct_store_callsites"]
        }
    )
    assert declared == _current_direct_writer_callsites()
    assert (
        payload["future_static_gate"]["target_writer"]
        == "factory_core/transitions.py"
    )
    assert any(
        requirement.startswith("Use receiver-aware AST resolution")
        for requirement in payload["future_static_gate"]["activation_requirements"]
    )


def test_phase0_document_links_resolve() -> None:
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for path in PHASE0_LINK_DOCS:
        for raw_target in link_pattern.findall(path.read_text(encoding="utf-8")):
            target = raw_target.split("#", 1)[0].strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            assert (path.parent / target).resolve().exists(), (
                f"{path}: missing link target {raw_target}"
            )


def test_v1_characterization_index_is_complete_and_resolvable() -> None:
    index = json.loads(FIXTURE_INDEX.read_text(encoding="utf-8"))
    assert index["schema"] == "v1-characterization-index-v1"
    assert (
        index["baseline_commit"]
        == "357947948f034325ea6202694c20bf435910d011"
    )
    assert index["runtime_schema_version"] == _runtime_schema_version()
    assert {entry["category"] for entry in index["entries"]} == REQUIRED_CHARACTERIZATIONS
    referenced_cases = {entry["case_file"] for entry in index["entries"]}
    present_cases = {
        path.relative_to(FIXTURE_ROOT).as_posix()
        for path in (FIXTURE_ROOT / "cases").glob("*.json")
    }
    assert referenced_cases == present_cases

    fixture_ids: set[str] = set()
    gap_ids: set[str] = set()
    for entry in index["entries"]:
        fixture_id = entry["fixture_id"]
        assert fixture_id not in fixture_ids
        fixture_ids.add(fixture_id)
        case_path = FIXTURE_ROOT / entry["case_file"]
        case = json.loads(case_path.read_text(encoding="utf-8"))
        assert case["schema"] == "v1-characterization-case-v1"
        assert case["fixture_id"] == fixture_id
        assert case["category"] == entry["category"]
        assert case["fixture_kind"] == "pytest_runtime_tmp_path_descriptor"
        assert case["source_paths"]
        assert all((ROOT / source).exists() for source in case["source_paths"])
        assert case["pytest_nodes"]
        assert all(_test_function_exists(node_id) for node_id in case["pytest_nodes"])
        assert case["v1_expected"]
        assert case["contracts"]
        assert case["machine_readable_fields"]
        assert set(case["machine_readable_fields"]) == set(case["v1_expected"])
        assert case["missing_evidence"]
        for gap in case["missing_evidence"]:
            assert re.fullmatch(r"GAP-V1-[A-Z0-9-]+", gap["gap_id"])
            assert gap["gap_id"] not in gap_ids
            gap_ids.add(gap["gap_id"])
            assert gap["description"]
            assert gap["impact"]
