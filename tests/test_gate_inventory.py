from __future__ import annotations

import ast
from pathlib import Path

import pytest

from factory_core.stages import GATE_POLICIES, gate_policy
from factory_core.workflow_contract import compile_workflow_contract_bundle


ROOT = Path(__file__).resolve().parents[1]
NATIVE_PRODUCER_ROOTS = tuple(
    path
    for path in sorted((ROOT / "factory_core").rglob("*.py"))
    if path.relative_to(ROOT).as_posix()
    not in {
        "factory_core/adapters/legacy.py",
        "factory_core/workflow_contract.py",
    }
)
LEGACY_PRODUCER = ROOT / "factory_core" / "adapters" / "legacy.py"


def _pending_action_gate_arguments(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    stack: list[str] = []
    records: list[tuple[str, str | None]] = []

    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if name == "PendingAction":
                keyword = next(
                    (item for item in node.keywords if item.arg == "gate"), None
                )
                value = (
                    keyword.value.value
                    if keyword is not None
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                    else None
                )
                module = path.relative_to(ROOT).with_suffix("").as_posix().replace(
                    "/", "."
                )
                records.append((".".join((module, *stack)), value))
            self.generic_visit(node)

    Visitor().visit(tree)
    return tuple(records)


def test_every_native_pending_action_gate_producer_is_in_the_bundle_inventory() -> None:
    bundle = compile_workflow_contract_bundle()
    exact = {gate.gate: gate for gate in bundle.gates if gate.gate_family == "exact"}
    producers = {gate.producer for gate in bundle.gates}
    records = tuple(
        record
        for path in NATIVE_PRODUCER_ROOTS
        for record in _pending_action_gate_arguments(path)
    )

    for producer, literal_gate in records:
        assert producer in producers
        if literal_gate is not None:
            assert literal_gate in exact

    assert {"preflight", "step4", "dynamic"} <= set(exact)


def test_native_pending_action_producers_have_exact_policy_parity() -> None:
    records = {
        record
        for path in NATIVE_PRODUCER_ROOTS
        for record in _pending_action_gate_arguments(path)
    }

    assert records == {
        ("factory_core.engine.FactoryEngine.run", "delivery_freeze_override"),
        ("factory_core.steps.gates._consultation_gate", None),
        ("factory_core.steps.gates.prepare_human_gates", "content_freeze"),
        ("factory_core.steps.gates.prepare_human_gates", "step3"),
        ("factory_core.steps.validators.NativeArtifactValidator._step_9", "step8_5"),
    }
    inventoried_producers = {policy.producer for policy in GATE_POLICIES}
    assert {producer for producer, _gate in records} <= inventoried_producers


def test_unknown_exact_gate_policy_fails_closed_without_a_default() -> None:
    with pytest.raises(KeyError, match="not uniquely defined"):
        gate_policy("unknown_gate")

    legacy_families = tuple(
        policy for policy in GATE_POLICIES if policy.gate_family != "exact"
    )
    assert len(legacy_families) == 1
    assert legacy_families[0].gate_family == "legacy_arbitrary"
    assert legacy_families[0].compatibility_diagnostic == "UNANALYZABLE"


def test_dynamic_native_and_legacy_gate_families_are_explicit_not_fake_stages() -> None:
    bundle = compile_workflow_contract_bundle()
    gates = {gate.gate_id: gate for gate in bundle.gates}

    dynamic = gates["gate:dynamic"]
    assert dynamic.stage_id is None
    assert dynamic.source_step_id is None
    assert dynamic.binding == "active_stage_or_stage_1_fallback"
    assert dynamic.compatibility_diagnostic is None

    legacy = gates["gate-family:legacy_dynamic"]
    assert legacy.gate_family == "legacy_arbitrary"
    assert legacy.stage_id is None
    assert legacy.source_step_id is None
    assert legacy.compatibility_diagnostic == "UNANALYZABLE"
    adapter_source = (
        ROOT / "factory_core" / "adapters" / "legacy.py"
    ).read_text(encoding="utf-8")
    assert legacy.source_expression in adapter_source

    exact = {gate.gate for gate in bundle.gates if gate.gate_family == "exact"}
    for producer, literal_gate in _pending_action_gate_arguments(LEGACY_PRODUCER):
        if literal_gate is None:
            assert producer == legacy.producer
        else:
            assert literal_gate in exact


def test_contest_human_gates_are_all_represented_by_exact_gate_contracts() -> None:
    bundle = compile_workflow_contract_bundle()
    exact = {gate.gate for gate in bundle.gates if gate.gate_family == "exact"}
    contest_human_gates = {
        phase.human_gate
        for phase in bundle.contest_phases
        if phase.human_gate is not None
    }

    assert contest_human_gates <= exact
