from __future__ import annotations

import ast
from itertools import combinations
from pathlib import Path

from factory_core.dirty import DirtyFlag, semantic_flags
from factory_core.domain import SCHEMA_VERSION, WorkflowState, WorkflowStatus
from factory_core.stages import (
    GATE_POLICIES,
    STEP_SCHEDULER_GENERATION,
    _pending_projection,
)


ROOT = Path(__file__).resolve().parents[1]
PRE_M03_PURE_MODULES = frozenset(
    {
        "factory_core/canonical.py",
        "factory_core/owner_compiler.py",
        "factory_core/shadow_scheduler.py",
        "factory_core/workflow_contract.py",
    }
)
PURE_M03_COMPAT_MODULES = frozenset(
    {
        "factory_core/legacy_classifier_compat.py",
        "factory_core/classifier_identity.py",
        "factory_core/classifier_implementation_manifest.py",
        "factory_core/persisted_dirty_owner_policy.py",
        "factory_core/persisted_dirty_owner_implementation_manifest.py",
        "factory_core/workflow_contract_v2.py",
        "factory_core/contract_pins.py",
        "factory_core/project_snapshot_v0.py",
        "factory_core/command_envelope.py",
    }
)
PURE_M01_MODULES = PRE_M03_PURE_MODULES | PURE_M03_COMPAT_MODULES
FORBIDDEN_RUNTIME_IMPORTS = {
    "factory_core.canonical",
    "factory_core.owner_compiler",
    "factory_core.workflow_contract",
    "canonical",
    "owner_compiler",
    "workflow_contract",
}

# Phase 2-9's identity, evidence, and shadow stores intentionally reuse the
# side-effect-free canonical/owner primitives that M0.1 originally introduced
# as pure modules.  Keep this exception closed by both consumer path and the
# exact primitive family each consumer may reference; it is not a general
# exemption for production runtime modules.
PHASE2_8_PURE_MODULE_CONSUMERS = {
    "factory_core/phase9_provider_identity.py": frozenset({"canonical"}),
    "factory_core/phase9_provider_sandbox.py": frozenset({"canonical"}),
    "factory_core/phase9_runtime.py": frozenset({"canonical"}),
    "factory_core/phase9_runtime_authority.py": frozenset({"canonical"}),
    "factory_core/phase9_runtime_coordinator.py": frozenset({"canonical"}),
    "factory_core/phase9_runtime_export.py": frozenset({"canonical"}),
    "factory_core/phase9_runtime_probes.py": frozenset({"canonical"}),
    "factory_core/phase9_runtime_receipts.py": frozenset({"canonical"}),
    "factory_core/authority_operations.py": frozenset({"canonical"}),
    "factory_core/authority_operator_workflow.py": frozenset({"canonical"}),
    "factory_core/authority_outbox_delivery.py": frozenset({"canonical"}),
    "factory_core/authority_production_schema.py": frozenset({"canonical"}),
    "factory_core/authority_production_writer.py": frozenset({"canonical"}),
    "factory_core/authority_read_repository.py": frozenset({"canonical"}),
    "factory_core/data_egress.py": frozenset({"canonical"}),
    "factory_core/phase3_artifacts.py": frozenset(
        {"canonical", "owner_compiler"}
    ),
    "factory_core/phase3_shadow_runtime.py": frozenset(
        {"canonical", "owner_compiler"}
    ),
    "factory_core/phase4_shadow_runtime.py": frozenset({"canonical"}),
    "factory_core/phase5_shadow_supervisor.py": frozenset({"canonical"}),
    "factory_core/phase6_snapshot_grants.py": frozenset({"canonical"}),
    "factory_core/phase6_source_assembler.py": frozenset({"canonical"}),
    "factory_core/phase9_entry.py": frozenset({"canonical"}),
    "factory_core/phase9_forensic_replay.py": frozenset({"canonical"}),
    "factory_core/phase9_p0_evidence.py": frozenset({"canonical"}),
    "factory_core/phase9_replay_evidence.py": frozenset({"canonical"}),
    "factory_core/phase9_run_generation.py": frozenset(
        {"canonical", "workflow_contract"}
    ),
    "factory_core/phase78_service.py": frozenset({"canonical"}),
    "factory_core/phase78_work_ledger.py": frozenset({"canonical"}),
    "factory_core/phase78_worker.py": frozenset({"canonical"}),
    "factory_core/phase7_grounding_runtime.py": frozenset({"canonical"}),
    "factory_core/phase8_evidence_egress_runtime.py": frozenset({"canonical"}),
    "factory_core/reference_evidence.py": frozenset({"canonical"}),
    "factory_core/reference_materializer.py": frozenset({"canonical"}),
}


def _pure_module_key(module_name: str) -> str:
    return module_name.rsplit(".", 1)[-1]


def _state(gate: str | None) -> WorkflowState:
    return WorkflowState(
        schema_version=SCHEMA_VERSION,
        project_id="parity",
        project_type="modeling",
        control_mode="engine",
        runtime_generation="native_v2",
        scheduler_generation=STEP_SCHEDULER_GENERATION,
        stage_catalog_version=None,
        status=WorkflowStatus.AWAITING_SELECTION,
        last_completed_step=0,
        active_step=1,
        last_completed_stage=0,
        active_stage=None,
        active_subtask=None,
        source_step_id=1,
        attempt=0,
        revision=0,
        pending_action=(
            {"type": "test", "gate": gate} if gate is not None else None
        ),
        runner_pid=None,
        runner_lease_id=None,
        heartbeat_at=None,
        storage_scope="ongoing",
        created_at=0,
        updated_at=0,
        last_event_at=0,
    )


def test_pending_projection_preserves_every_pre_m01_gate_branch() -> None:
    expected = {
        "step3": (2, "method_selection", 3),
        "step8_5": (6, "reviewer_entry_gate", 8),
        "content_freeze": (10, "content_freeze_guard", 16),
        "delivery_freeze_override": (10, "content_freeze_guard", 16),
        "conditional_math_preflight": None,
        "unknown_gate": None,
        "": None,
        None: None,
    }

    assert {gate: _pending_projection(_state(gate)) for gate in expected} == expected
    conditional = next(
        policy
        for policy in GATE_POLICIES
        if policy.gate == "conditional_math_preflight"
    )
    assert conditional.projects_pending_action is False


def test_semantic_flags_preserves_the_complete_pre_m01_flag_power_set() -> None:
    all_flags = tuple(flag.value for flag in DirtyFlag)
    semantic = {
        DirtyFlag.MODEL.value,
        DirtyFlag.MATH.value,
        DirtyFlag.RESULT.value,
    }

    for size in range(len(all_flags) + 1):
        for selected in combinations(all_flags, size):
            records = [{"flag": flag} for flag in selected]
            expected = set(selected) & semantic
            assert semantic_flags(records) == expected
            assert semantic_flags(tuple(records)) == expected

    noisy = (
        {"flag": "MODEL_DIRTY"},
        {"flag": "MODEL_DIRTY"},
        {"flag": "UNKNOWN"},
        {"flag": None},
        {},
    )
    assert semantic_flags(noisy) == {"MODEL_DIRTY"}


def test_production_runtime_does_not_import_or_reference_m01_pure_modules() -> None:
    violations: list[str] = []
    observed_consumers: dict[str, frozenset[str]] = {}
    for path in sorted((ROOT / "factory_core").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative in PURE_M01_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        referenced_primitives: set[str] = set()
        file_violations: list[str] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module in FORBIDDEN_RUNTIME_IMPORTS
            ):
                referenced_primitives.add(_pure_module_key(node.module))
                file_violations.append(f"{relative}:{node.lineno}:from {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in FORBIDDEN_RUNTIME_IMPORTS:
                        referenced_primitives.add(_pure_module_key(alias.name))
                        file_violations.append(
                            f"{relative}:{node.lineno}:import {alias.name}"
                        )
        source = path.read_text(encoding="utf-8")
        for module_name in (
            "workflow_contract",
            "owner_compiler",
            "factory_core.canonical",
        ):
            if module_name in source:
                referenced_primitives.add(_pure_module_key(module_name))
                file_violations.append(f"{relative}:references {module_name}")

        approved_primitives = PHASE2_8_PURE_MODULE_CONSUMERS.get(relative)
        if approved_primitives is None:
            violations.extend(file_violations)
            continue
        observed = frozenset(referenced_primitives)
        observed_consumers[relative] = observed
        if observed != approved_primitives:
            violations.append(
                f"{relative}:approved pure-module references changed: "
                f"expected {sorted(approved_primitives)!r}, "
                f"observed {sorted(observed)!r}"
            )

    assert violations == []
    assert observed_consumers == PHASE2_8_PURE_MODULE_CONSUMERS


def test_phase2_8_pure_module_consumers_are_an_exact_closed_set() -> None:
    expected = {
        "factory_core/phase9_provider_identity.py": frozenset({"canonical"}),
        "factory_core/phase9_provider_sandbox.py": frozenset({"canonical"}),
        "factory_core/phase9_runtime.py": frozenset({"canonical"}),
        "factory_core/phase9_runtime_authority.py": frozenset({"canonical"}),
        "factory_core/phase9_runtime_coordinator.py": frozenset({"canonical"}),
        "factory_core/phase9_runtime_export.py": frozenset({"canonical"}),
        "factory_core/phase9_runtime_probes.py": frozenset({"canonical"}),
        "factory_core/phase9_runtime_receipts.py": frozenset({"canonical"}),
        "factory_core/authority_operations.py": frozenset({"canonical"}),
        "factory_core/authority_operator_workflow.py": frozenset({"canonical"}),
        "factory_core/authority_outbox_delivery.py": frozenset({"canonical"}),
        "factory_core/authority_production_schema.py": frozenset({"canonical"}),
        "factory_core/authority_production_writer.py": frozenset({"canonical"}),
        "factory_core/authority_read_repository.py": frozenset({"canonical"}),
        "factory_core/data_egress.py": frozenset({"canonical"}),
        "factory_core/phase3_artifacts.py": frozenset(
            {"canonical", "owner_compiler"}
        ),
        "factory_core/phase3_shadow_runtime.py": frozenset(
            {"canonical", "owner_compiler"}
        ),
        "factory_core/phase4_shadow_runtime.py": frozenset({"canonical"}),
        "factory_core/phase5_shadow_supervisor.py": frozenset({"canonical"}),
        "factory_core/phase6_snapshot_grants.py": frozenset({"canonical"}),
        "factory_core/phase6_source_assembler.py": frozenset({"canonical"}),
        "factory_core/phase9_entry.py": frozenset({"canonical"}),
        "factory_core/phase9_forensic_replay.py": frozenset({"canonical"}),
        "factory_core/phase9_p0_evidence.py": frozenset({"canonical"}),
        "factory_core/phase9_replay_evidence.py": frozenset({"canonical"}),
        "factory_core/phase9_run_generation.py": frozenset(
            {"canonical", "workflow_contract"}
        ),
        "factory_core/phase78_service.py": frozenset({"canonical"}),
        "factory_core/phase78_work_ledger.py": frozenset({"canonical"}),
        "factory_core/phase78_worker.py": frozenset({"canonical"}),
        "factory_core/phase7_grounding_runtime.py": frozenset({"canonical"}),
        "factory_core/phase8_evidence_egress_runtime.py": frozenset({"canonical"}),
        "factory_core/reference_evidence.py": frozenset({"canonical"}),
        "factory_core/reference_materializer.py": frozenset({"canonical"}),
    }

    assert PHASE2_8_PURE_MODULE_CONSUMERS == expected
    assert len(PHASE2_8_PURE_MODULE_CONSUMERS) == 33
    assert set(PHASE2_8_PURE_MODULE_CONSUMERS).isdisjoint(PURE_M01_MODULES)
    assert all((ROOT / path).is_file() for path in PHASE2_8_PURE_MODULE_CONSUMERS)


def test_m03_pure_module_compatibility_exception_is_exact_closed_set() -> None:
    expected = frozenset(
        {
            "factory_core/legacy_classifier_compat.py",
            "factory_core/classifier_identity.py",
            "factory_core/classifier_implementation_manifest.py",
            "factory_core/persisted_dirty_owner_policy.py",
            "factory_core/persisted_dirty_owner_implementation_manifest.py",
            "factory_core/workflow_contract_v2.py",
            "factory_core/contract_pins.py",
            "factory_core/project_snapshot_v0.py",
            "factory_core/command_envelope.py",
        }
    )

    assert PURE_M03_COMPAT_MODULES == expected
    assert len(PURE_M03_COMPAT_MODULES) == 9
    assert PURE_M01_MODULES == PRE_M03_PURE_MODULES | expected
    assert "factory_core/tenth_production_neutral_module.py" not in PURE_M01_MODULES
