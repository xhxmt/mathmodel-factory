from __future__ import annotations

import json
import os
import socket
from collections import Counter
from dataclasses import FrozenInstanceError, fields, is_dataclass, replace
from enum import Enum
from pathlib import Path
import subprocess
import sys

import pytest

from factory_core.artifact_ownership import (
    ARTIFACT_OWNERSHIP_REGISTRY,
    ArtifactOwnership,
    artifact_ownership,
)
from factory_core.canonical import (
    CanonicalizationError,
    canonical_bytes,
    canonical_sha256,
)
from factory_core.owner_compiler import (
    OWNER_PRIORITY_AUTHORIZATIONS,
    OwnerContractValidationError,
    OwnerDiagnosticCode,
    OwnerPriorityAuthorization,
    compile_owner_registry,
    resolve_owner,
    validate_owner_compilation,
    validate_owner_resolution,
)
from factory_core.workflow_contract import (
    WORKFLOW_CONTRACT_BUNDLE_SCHEMA,
    WorkflowContractValidationError,
    compile_workflow_contract_bundle,
    validate_workflow_contract_bundle,
    workflow_contract_analysis_bytes,
    workflow_contract_analysis_sha256,
    workflow_contract_bytes,
    workflow_contract_sha256,
)


ROOT = Path(__file__).resolve().parents[1]
PARITY_FIXTURE = (
    ROOT / "tests" / "fixtures" / "workflow_contract_v1" / "owner_parity.json"
)
DIAGNOSTICS_FIXTURE = (
    ROOT
    / "tests"
    / "fixtures"
    / "workflow_contract_v1"
    / "owner_diagnostics.json"
)
IDENTITY_FIXTURE = (
    ROOT / "tests" / "fixtures" / "workflow_contract_v1" / "bundle_identity.json"
)


def test_bundle_compiles_complete_current_stage_step_gate_and_owner_truth() -> None:
    bundle = compile_workflow_contract_bundle()

    assert bundle.schema_version == WORKFLOW_CONTRACT_BUNDLE_SCHEMA
    assert bundle.schema_version == "workflow-contract-bundle-v1"
    assert bundle.workflow_state_schema_version == 9
    assert bundle.schema_version != str(bundle.workflow_state_schema_version)
    assert [stage.stage_id for stage in bundle.stages] == list(range(1, 11))
    assert [step.step_id for step in bundle.steps] == list(range(17))
    assert len(bundle.owner_compilation.rules) == len(ARTIFACT_OWNERSHIP_REGISTRY)

    reviewer_entry = next(
        subtask
        for stage in bundle.stages
        for subtask in stage.subtasks
        if subtask.subtask_id == "subtask:stage:6:reviewer_entry_gate"
    )
    assert reviewer_entry.source_step_id == 8
    assert reviewer_entry.checkpoint_step_id is None

    conditional = next(
        subtask
        for stage in bundle.stages
        for subtask in stage.subtasks
        if subtask.key == "conditional_math_preflight"
    )
    assert conditional.source_step_id == 13
    assert conditional.condition.operator == "ANY"
    assert conditional.condition.operands == (
        "MODEL_DIRTY",
        "MATH_DIRTY",
        "RESULT_DIRTY",
    )

    gates = {gate.gate_id: gate for gate in bundle.gates}
    expected_gate_order = [
        "gate:preflight",
        "gate:step3",
        "gate:step4",
        "gate:step8_5",
        "gate:conditional_math_preflight",
        "gate:content_freeze",
        "gate:delivery_freeze_override",
        "gate:dynamic",
        "gate-family:legacy_dynamic",
    ]
    assert [gate.gate_id for gate in bundle.gates] == expected_gate_order
    assert set(gates) == set(expected_gate_order)
    assert gates["gate:content_freeze"].authority == "project_workflow_decision"
    assert gates["gate:step8_5"].authority == "artifact_validator"


def _assert_deeply_immutable(value) -> None:
    if is_dataclass(value) and not isinstance(value, type):
        assert value.__dataclass_params__.frozen is True
        for field in fields(value):
            _assert_deeply_immutable(getattr(value, field.name))
        return
    if isinstance(value, tuple):
        for item in value:
            _assert_deeply_immutable(item)
        return
    assert value is None or isinstance(value, (bool, int, str, Enum)), type(value)


def test_bundle_value_objects_are_deeply_immutable() -> None:
    bundle = compile_workflow_contract_bundle()

    with pytest.raises(FrozenInstanceError):
        bundle.schema_version = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        bundle.stages[0].name = "changed"  # type: ignore[misc]
    assert isinstance(bundle.stages, tuple)
    assert isinstance(bundle.stages[0].subtasks, tuple)
    assert isinstance(bundle.owner_compilation.rules, tuple)
    _assert_deeply_immutable(bundle)


def test_canonical_serializer_has_explicit_deterministic_domain_rules() -> None:
    first = {
        "z": {"beta", "alpha"},
        "a": {"nested": None, "enum_like": "Å"},
        "list": [3, 2, 1],
    }
    second = {
        "list": [3, 2, 1],
        "a": {"enum_like": "Å", "nested": None},
        "z": {"alpha", "beta"},
    }

    assert canonical_bytes(first) == canonical_bytes(second)
    assert canonical_sha256(first) == canonical_sha256(second)
    assert canonical_bytes("Å") == '"Å"'.encode("utf-8")
    with pytest.raises(CanonicalizationError, match="float"):
        canonical_bytes(1.25)
    with pytest.raises(CanonicalizationError, match="string keys"):
        canonical_bytes({1: "not allowed"})


def test_canonical_json_type_boundaries_are_unambiguous_and_extensions_are_explicit() -> None:
    json_domain_values = (None, False, True, 0, 1, "0", "1", [], {})
    encoded = [canonical_bytes(value) for value in json_domain_values]

    assert len(encoded) == len(set(encoded))
    assert canonical_bytes([1, "1"]) == b'[1,"1"]'
    # These extension types intentionally project into JSON arrays under v1.
    assert canonical_bytes((1,)) == canonical_bytes([1])
    assert canonical_bytes({1}) == canonical_bytes([1])
    assert canonical_bytes(frozenset({1})) == canonical_bytes([1])
    for unsupported in (b"bytes", object(), 1.5, "\ud800"):
        with pytest.raises(CanonicalizationError):
            canonical_bytes(unsupported)


def _reverse_mapping_insertion_order(value):
    if isinstance(value, dict):
        return {
            key: _reverse_mapping_insertion_order(child)
            for key, child in reversed(tuple(value.items()))
        }
    if isinstance(value, list):
        return [_reverse_mapping_insertion_order(child) for child in value]
    return value


def test_fixture_mapping_insertion_order_does_not_change_canonical_identity() -> None:
    for fixture_path in (PARITY_FIXTURE, DIAGNOSTICS_FIXTURE, IDENTITY_FIXTURE):
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        shuffled = _reverse_mapping_insertion_order(payload)

        assert canonical_bytes(payload) == canonical_bytes(shuffled)
        assert canonical_sha256(payload) == canonical_sha256(shuffled)


def test_bundle_bytes_and_hash_are_stable_for_100_replays_and_order_sensitive_lists() -> None:
    bundles = [compile_workflow_contract_bundle() for _ in range(100)]
    byte_values = {workflow_contract_bytes(bundle) for bundle in bundles}
    hash_values = {workflow_contract_sha256(bundle) for bundle in bundles}

    assert len(byte_values) == 1
    assert len(hash_values) == 1
    original = bundles[0]
    reordered = replace(original, stages=tuple(reversed(original.stages)))
    assert workflow_contract_sha256(reordered) != workflow_contract_sha256(original)


def test_bundle_semantic_hash_is_stable_across_python_hash_seeds() -> None:
    command = (
        "from factory_core.workflow_contract import "
        "compile_workflow_contract_bundle,workflow_contract_bytes,"
        "workflow_contract_sha256;"
        "b=compile_workflow_contract_bundle();"
        "print(len(workflow_contract_bytes(b)),workflow_contract_sha256(b))"
    )
    outputs = set()
    for seed in ("0", "1", "17", "999", "random"):
        environment = dict(os.environ)
        environment["PYTHONHASHSEED"] = seed
        outputs.add(
            subprocess.check_output(
                [sys.executable, "-c", command],
                cwd=ROOT,
                env=environment,
                text=True,
            ).strip()
        )

    assert outputs == {
        "55178 2e2f3b9cb48788db5e7a28d0f0343518c3d1cd5969a57e045ef2fc946df5455d"
    }


@pytest.mark.parametrize(
    "case",
    (
        "step_prompt",
        "gate_producer",
        "owner_compiler_schema",
        "diagnostic_code",
        "diagnostic_rule_ids",
        "diagnostic_path",
        "diagnostic_witness",
        "diagnostic_explanation",
        "diagnostic_issue_id",
        "diagnostic_rationale",
    ),
)
def test_each_analysis_only_field_changes_only_analysis_identity(case) -> None:
    bundle = compile_workflow_contract_bundle()
    if case == "step_prompt":
        changed_analysis = replace(
            bundle,
            steps=(
                replace(bundle.steps[0], prompt="analysis-only:step-prompt"),
                *bundle.steps[1:],
            ),
        )
    elif case == "gate_producer":
        changed_analysis = replace(
            bundle,
            gates=(
                replace(bundle.gates[0], producer="analysis-only:gate-producer"),
                *bundle.gates[1:],
            ),
        )
    elif case == "owner_compiler_schema":
        changed_analysis = replace(
            bundle,
            owner_compilation=replace(
                bundle.owner_compilation,
                schema_version="analysis-only:owner-compiler-schema",
            ),
        )
    else:
        diagnostic = bundle.owner_compilation.diagnostics[0]
        if case == "diagnostic_code":
            changed_diagnostic = replace(
                diagnostic,
                code=next(
                    item for item in OwnerDiagnosticCode if item is not diagnostic.code
                ),
            )
        elif case == "diagnostic_rule_ids":
            changed_diagnostic = replace(
                diagnostic,
                rule_ids=(*diagnostic.rule_ids, "analysis-only-rule-id"),
            )
        else:
            diagnostic_field = {
                "diagnostic_path": "path",
                "diagnostic_witness": "witness",
                "diagnostic_explanation": "explanation",
                "diagnostic_issue_id": "issue_id",
                "diagnostic_rationale": "rationale",
            }[case]
            changed_diagnostic = replace(
                diagnostic,
                **{diagnostic_field: f"analysis-only:{diagnostic_field}"},
            )
        changed_analysis = replace(
            bundle,
            owner_compilation=replace(
                bundle.owner_compilation,
                diagnostics=(
                    changed_diagnostic,
                    *bundle.owner_compilation.diagnostics[1:],
                ),
            ),
        )

    assert workflow_contract_bytes(changed_analysis) == workflow_contract_bytes(bundle)
    assert workflow_contract_sha256(changed_analysis) == workflow_contract_sha256(bundle)
    assert workflow_contract_analysis_bytes(changed_analysis) != workflow_contract_analysis_bytes(
        bundle
    )
    assert workflow_contract_analysis_sha256(
        changed_analysis
    ) != workflow_contract_analysis_sha256(bundle)
    assert validate_workflow_contract_bundle(changed_analysis) is changed_analysis


def _forged_string_enum_member(enum_type, *, initialized: bool):
    forged = str.__new__(enum_type, "PWN")
    if initialized:
        object.__setattr__(forged, "_value_", "PWN")
        object.__setattr__(forged, "_name_", "PWN")
    assert type(forged) is enum_type
    assert not any(forged is member for member in enum_type)
    return forged


@pytest.mark.parametrize(
    "case",
    (
        "uninitialized_bundle",
        "bundle_subclass",
        "tuple_subclass",
        "analysis_object",
        "analysis_invalid_unicode",
        "forged_enum_member",
        "uninitialized_enum_member",
    ),
)
@pytest.mark.parametrize(
    "serializer",
    (
        workflow_contract_bytes,
        workflow_contract_sha256,
        workflow_contract_analysis_bytes,
        workflow_contract_analysis_sha256,
    ),
)
def test_workflow_public_serializers_require_complete_exact_runtime_structure(
    case,
    serializer,
) -> None:
    class TupleSubclass(tuple):
        pass

    bundle = compile_workflow_contract_bundle()
    if case == "uninitialized_bundle":
        supplied = object.__new__(type(bundle))
    elif case == "bundle_subclass":
        subclass = type("ForgedWorkflowContractBundle", (type(bundle),), {})
        supplied = object.__new__(subclass)
        for item in fields(bundle):
            object.__setattr__(supplied, item.name, getattr(bundle, item.name))
    elif case == "tuple_subclass":
        supplied = replace(bundle, steps=TupleSubclass(bundle.steps))
    elif case in {"analysis_object", "analysis_invalid_unicode"}:
        prompt = object() if case == "analysis_object" else "invalid:\ud800"
        supplied = replace(
            bundle,
            steps=(replace(bundle.steps[0], prompt=prompt), *bundle.steps[1:]),
        )
    else:
        diagnostic = bundle.owner_compilation.diagnostics[0]
        code = _forged_string_enum_member(
            OwnerDiagnosticCode,
            initialized=case == "forged_enum_member",
        )
        supplied = replace(
            bundle,
            owner_compilation=replace(
                bundle.owner_compilation,
                diagnostics=(
                    replace(diagnostic, code=code),
                    *bundle.owner_compilation.diagnostics[1:],
                ),
            ),
        )

    with pytest.raises(WorkflowContractValidationError):
        serializer(supplied)


def test_workflow_serializer_allows_typed_unauthorized_behavior_as_hash_counterfactual() -> None:
    bundle = compile_workflow_contract_bundle()
    forged = replace(
        bundle,
        steps=(
            replace(
                bundle.steps[0],
                budget=replace(
                    bundle.steps[0].budget,
                    timeout_seconds=bundle.steps[0].budget.timeout_seconds + 1,
                ),
            ),
            *bundle.steps[1:],
        ),
    )

    assert workflow_contract_bytes(forged) != workflow_contract_bytes(bundle)
    assert workflow_contract_sha256(forged) != workflow_contract_sha256(bundle)
    with pytest.raises(WorkflowContractValidationError):
        validate_workflow_contract_bundle(forged)


def test_semantic_hash_binds_owner_resolution_mode() -> None:
    bundle = compile_workflow_contract_bundle()
    changed = replace(
        bundle,
        owner_compilation=replace(
            bundle.owner_compilation,
            mode="ordered-last-match-v1",
        ),
    )

    assert workflow_contract_bytes(changed) != workflow_contract_bytes(bundle)
    assert workflow_contract_sha256(changed) != workflow_contract_sha256(bundle)


def test_bundle_validation_rejects_unsupported_owner_resolution_mode() -> None:
    bundle = compile_workflow_contract_bundle()
    changed = replace(
        bundle,
        owner_compilation=replace(
            bundle.owner_compilation,
            mode="ordered-last-match-v1",
        ),
    )

    with pytest.raises(
        WorkflowContractValidationError, match="unsupported owner resolution mode"
    ):
        validate_workflow_contract_bundle(changed)


def test_stage_catalog_version_and_behavior_projection_are_source_derived() -> None:
    bundle = compile_workflow_contract_bundle()
    with pytest.raises(
        WorkflowContractValidationError, match="unsupported stage_catalog_version"
    ):
        validate_workflow_contract_bundle(
            replace(bundle, stage_catalog_version="factory-stage-catalog-v999")
        )

    original = bundle.stages[0].subtasks[0]
    behavior_changes = (
        replace(original, contest_phase_id=2),
        replace(original, owner_id="owner:stage:999"),
        replace(
            original,
            budget=replace(
                original.budget,
                timeout_seconds=original.budget.timeout_seconds + 1,
            ),
        ),
        replace(original, kind="forged-kind"),
    )
    for changed_subtask in behavior_changes:
        changed_stage = replace(
            bundle.stages[0],
            subtasks=(changed_subtask, *bundle.stages[0].subtasks[1:]),
        )
        changed_bundle = replace(
            bundle,
            stages=(changed_stage, *bundle.stages[1:]),
        )
        with pytest.raises(
            WorkflowContractValidationError,
            match="behavior projection differs from source contracts",
        ):
            validate_workflow_contract_bundle(changed_bundle)


@pytest.mark.parametrize(
    ("field_name", "expected_error"),
    (
        ("contest_phase_id", "contest_phase_id mismatch"),
        ("owner_id", "owner_id mismatch"),
        ("timeout_seconds", "budget.timeout_seconds mismatch"),
        ("hang_timeout_seconds", "budget.hang_timeout_seconds mismatch"),
        ("max_attempts", "budget.max_attempts mismatch"),
        ("max_reopens", "budget.max_reopens mismatch"),
    ),
)
def test_stage_step_cross_checks_have_precise_failures(field_name, expected_error) -> None:
    bundle = compile_workflow_contract_bundle()
    step = bundle.steps[0]
    if field_name in {
        "timeout_seconds",
        "hang_timeout_seconds",
        "max_attempts",
        "max_reopens",
    }:
        changed = replace(
            step,
            budget=replace(
                step.budget,
                **{field_name: getattr(step.budget, field_name) + 1},
            ),
        )
    elif field_name == "contest_phase_id":
        changed = replace(step, contest_phase_id=2)
    else:
        changed = replace(step, owner_id="owner:stage:999")

    with pytest.raises(WorkflowContractValidationError, match=expected_error):
        validate_workflow_contract_bundle(
            replace(bundle, steps=(changed, *bundle.steps[1:]))
        )


@pytest.mark.parametrize(
    "case",
    (
        "step_id",
        "step_contract_id",
        "name",
        "implementation",
        "authority",
        "default_models",
    ),
)
def test_complete_step_behavior_projection_is_source_authorized(case) -> None:
    bundle = compile_workflow_contract_bundle()
    step = bundle.steps[0]
    values = {
        "step_id": 99,
        "step_contract_id": "step:forged",
        "name": "forged-name",
        "implementation": "forged-implementation",
        "authority": "forged-authority",
        "default_models": ("forged-model",),
    }
    changed = replace(step, **{case: values[case]})

    with pytest.raises(WorkflowContractValidationError):
        validate_workflow_contract_bundle(
            replace(bundle, steps=(changed, *bundle.steps[1:]))
        )


@pytest.mark.parametrize(
    "case",
    (
        "gate_id",
        "gate",
        "gate_family",
        "stage_id",
        "subtask_id",
        "source_step_id",
        "kind",
        "authority",
        "condition_operator",
        "condition_operands",
        "binding",
        "source_expression",
        "compatibility_diagnostic",
        "projects_pending_action",
    ),
)
def test_complete_gate_behavior_projection_is_source_authorized(case) -> None:
    bundle = compile_workflow_contract_bundle()
    gate = bundle.gates[0]
    values = {
        "gate_id": "gate:forged",
        "gate": "forged-gate",
        "gate_family": "forged-family",
        "stage_id": 9,
        "subtask_id": "subtask:forged",
        "source_step_id": 2,
        "kind": "forged-kind",
        "authority": "forged-authority",
        "binding": "forged-binding",
        "source_expression": "forged-expression",
        "compatibility_diagnostic": "FORGED",
        "projects_pending_action": True,
    }
    if case == "condition_operator":
        changed = replace(
            gate,
            condition=replace(gate.condition, operator="FORGED"),
        )
    elif case == "condition_operands":
        changed = replace(
            gate,
            condition=replace(gate.condition, operands=("forged",)),
        )
    else:
        changed = replace(gate, **{case: values[case]})

    with pytest.raises(WorkflowContractValidationError):
        validate_workflow_contract_bundle(
            replace(bundle, gates=(changed, *bundle.gates[1:]))
        )


@pytest.mark.parametrize(
    "case",
    (
        "phase_id",
        "phase_contract_id",
        "name",
        "steps",
        "human_gate",
        "authority",
    ),
)
def test_complete_contest_phase_projection_is_source_authorized(case) -> None:
    bundle = compile_workflow_contract_bundle()
    phase = bundle.contest_phases[0]
    values = {
        "phase_id": 99,
        "phase_contract_id": "contest-phase:forged",
        "name": "forged-name",
        "steps": phase.steps[:1],
        "human_gate": "forged-gate",
        "authority": "forged-authority",
    }
    changed = replace(phase, **{case: values[case]})

    with pytest.raises(WorkflowContractValidationError):
        validate_workflow_contract_bundle(
            replace(
                bundle,
                contest_phases=(changed, *bundle.contest_phases[1:]),
            )
        )


@pytest.mark.parametrize(
    "case",
    (
        "ownership_schema",
        "rule_order",
        "rule_id",
        "priority_index",
        "pattern",
        "owner_id",
        "owner_stage",
        "semantic_domain",
        "dirty_flag",
        "final_input",
        "submission_member",
        "priority_authorizations",
    ),
)
def test_complete_owner_behavior_projection_is_source_authorized(case) -> None:
    bundle = compile_workflow_contract_bundle()
    compilation = bundle.owner_compilation
    if case == "ownership_schema":
        changed_compilation = replace(
            compilation,
            ownership_schema_version="factory-artifact-ownership-forged",
        )
    elif case == "rule_order":
        changed_compilation = replace(
            compilation,
            rules=(compilation.rules[1], compilation.rules[0], *compilation.rules[2:]),
        )
    else:
        rule = (
            next(item for item in compilation.rules if item.priority_authorizations)
            if case == "priority_authorizations"
            else compilation.rules[0]
        )
        values = {
            "rule_id": "owner-rule:sha256:" + "0" * 64,
            "priority_index": rule.priority_index + 1,
            "pattern": "forged/problem/**",
            "owner_id": "owner:stage:999",
            "owner_stage": 999,
            "semantic_domain": "forged-domain",
            "dirty_flag": "FORMAT_DIRTY",
            "final_input": not rule.final_input,
            "submission_member": not rule.submission_member,
        }
        if case == "priority_authorizations":
            authorization = rule.priority_authorizations[0]
            changed_rule = replace(
                rule,
                priority_authorizations=(
                    replace(authorization, rationale="forged rationale"),
                    *rule.priority_authorizations[1:],
                ),
            )
        else:
            changed_rule = replace(rule, **{case: values[case]})
        changed_compilation = replace(
            compilation,
            rules=tuple(
                changed_rule if item is rule else item for item in compilation.rules
            ),
        )

    with pytest.raises(WorkflowContractValidationError):
        validate_workflow_contract_bundle(
            replace(bundle, owner_compilation=changed_compilation)
        )


def test_semantic_hash_changes_for_behavior_bearing_contract_fields() -> None:
    bundle = compile_workflow_contract_bundle()
    changed_rule = replace(
        bundle.owner_compilation.rules[0], pattern="different/problem/**"
    )
    changed = replace(
        bundle,
        owner_compilation=replace(
            bundle.owner_compilation,
            rules=(changed_rule, *bundle.owner_compilation.rules[1:]),
        ),
    )

    assert workflow_contract_sha256(changed) != workflow_contract_sha256(bundle)


def test_bundle_canonical_identity_and_current_diagnostics_match_golden() -> None:
    golden = json.loads(IDENTITY_FIXTURE.read_text(encoding="utf-8"))
    bundle = compile_workflow_contract_bundle()
    encoded = workflow_contract_bytes(bundle)

    assert golden["schema_version"] == "workflow-contract-bundle-golden-v1"
    assert bundle.schema_version == golden["bundle_schema_version"]
    assert (
        bundle.canonicalization_schema_version
        == golden["canonicalization_schema_version"]
    )
    assert golden["canonical_bytes_scope"] == "semantic"
    assert "owner diagnostic codes, paths, witnesses, issue references, rationale and explanation" in golden[
        "semantic_hash_excludes"
    ]
    assert len(encoded) == golden["canonical_bytes_size"]
    assert workflow_contract_sha256(bundle) == golden["canonical_sha256"]
    assert len(workflow_contract_analysis_bytes(bundle)) == golden[
        "analysis_bytes_size"
    ]
    assert workflow_contract_analysis_sha256(bundle) == golden[
        "analysis_sha256"
    ]
    assert len(bundle.stages) == golden["stage_contract_count"]
    assert len(bundle.steps) == golden["step_contract_count"]
    assert len(bundle.gates) == golden["gate_contract_count"]
    assert len(bundle.owner_compilation.rules) == golden["owner_rule_count"]
    assert len(OWNER_PRIORITY_AUTHORIZATIONS) == golden[
        "owner_priority_authorization_count"
    ]
    assert bundle.classifier.rule_identity_sha256 == golden[
        "owner_rule_identity_sha256"
    ]
    assert dict(
        sorted(
            Counter(
                diagnostic.code.value
                for diagnostic in bundle.owner_compilation.diagnostics
            ).items()
        )
    ) == golden["compatibility_diagnostic_counts"]
    with pytest.raises(
        OwnerContractValidationError, match="SHADOWED, UNREACHABLE"
    ):
        validate_owner_compilation(bundle.owner_compilation, strict=True)


def test_current_owner_compiler_returns_all_matches_and_preserves_v1_first_match() -> None:
    compilation = compile_owner_registry()
    resolution = resolve_owner(compilation, "models/m1/05_sensitivity.py")

    assert resolution.resolved_owner_stage == 5
    assert [match.pattern for match in resolution.all_matches] == [
        "models/**/05_sensitivity.py",
        "models/**",
    ]
    assert resolution.resolved_rule_id == resolution.all_matches[0].rule_id
    assert {item.code for item in resolution.diagnostics} == {
        OwnerDiagnosticCode.INTENTIONAL_PRIORITY
    }
    assert resolution.diagnostics[0].issue_id == "RUN4-OWNER-001"


def test_v1_owner_parity_fixture_covers_normal_run4_conditional_gate_and_legacy() -> None:
    payload = json.loads(PARITY_FIXTURE.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "workflow-owner-parity-fixture-v1"
    assert {case["case_id"] for case in payload["cases"]} == {
        "normal",
        "run4_owner_incident",
        "conditional_step13",
        "human_gate",
        "migration_legacy",
        "unowned_diagnostic",
    }

    compilation = compile_owner_registry()
    for case in payload["cases"]:
        for expected in case["paths"]:
            path = expected["path"]
            resolution = resolve_owner(compilation, path)
            legacy = artifact_ownership(path)
            assert resolution.resolved_owner_stage == expected["owner_stage"], (
                case["case_id"],
                path,
            )
            assert (legacy.owner_stage if legacy is not None else None) == expected[
                "owner_stage"
            ]
            assert (
                resolution.all_matches[0].pattern
                if resolution.all_matches
                else None
            ) == expected["rule_pattern"]


def _fixture_rules(rows: list[list[object]]) -> tuple[ArtifactOwnership, ...]:
    return tuple(ArtifactOwnership(*row) for row in rows)


def test_all_owner_diagnostic_fixtures_have_exact_strict_results() -> None:
    payload = json.loads(DIAGNOSTICS_FIXTURE.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "workflow-owner-diagnostics-fixture-v1"
    assert {case["case_id"] for case in payload["cases"]} == {
        "no_owner",
        "multiple_owner",
        "shadow_unreachable",
        "intentional_pair_priority",
        "unanalyzable_static_pattern",
        "v1_authorized_bootstrap_log_pair",
        "v1_known_same_owner_shadow",
    }

    for case in payload["cases"]:
        if case.get("source") == "current_registry":
            compilation = compile_owner_registry()
            relevant = tuple(
                diagnostic
                for diagnostic in compilation.diagnostics
                if (
                    referenced_patterns := {
                        rule.pattern
                        for rule in compilation.rules
                        if rule.rule_id in diagnostic.rule_ids
                    }
                )
                and referenced_patterns <= set(case["patterns"])
                and referenced_patterns & set(case["patterns"])
            )
            assert {diagnostic.code.value for diagnostic in relevant} == set(
                case["compilation_codes"]
            )
            if issue_id := case.get("issue_id"):
                assert {item.issue_id for item in relevant} == {issue_id}
            if path := case.get("path"):
                resolution = resolve_owner(compilation, path)
                assert {item.code.value for item in resolution.diagnostics} == set(
                    case["resolution_codes"]
                )
                assert validate_owner_resolution(resolution, strict=True) is resolution
            if case.get("strict_compilation") == "reject":
                with pytest.raises(OwnerContractValidationError):
                    validate_owner_compilation(compilation, strict=True)
            continue

        authorizations = ()
        if raw_authorization := case.get("authorization"):
            authorizations = (OwnerPriorityAuthorization(**raw_authorization),)
        compilation = compile_owner_registry(
            _fixture_rules(case["rules"]),
            priority_authorizations=authorizations,
        )
        if expected_codes := case.get("compilation_codes"):
            assert {item.code.value for item in compilation.diagnostics} == set(
                expected_codes
            )
            with pytest.raises(OwnerContractValidationError):
                validate_owner_compilation(compilation, strict=True)
        if path := case.get("path"):
            resolution = resolve_owner(compilation, path)
            assert {item.code.value for item in resolution.diagnostics} == set(
                case["resolution_codes"]
            )
            if case["strict_resolution"] == "accept":
                assert validate_owner_resolution(resolution, strict=True) is resolution
            else:
                with pytest.raises(OwnerContractValidationError):
                    validate_owner_resolution(resolution, strict=True)


def test_strict_resolution_rejects_no_owner_and_unintentional_multiple_owner() -> None:
    no_owner = resolve_owner(compile_owner_registry(()), "unowned.md")
    assert {item.code for item in no_owner.diagnostics} == {
        OwnerDiagnosticCode.NO_OWNER
    }
    with pytest.raises(OwnerContractValidationError, match="NO_OWNER"):
        validate_owner_resolution(no_owner, strict=True)

    rules = (
        ArtifactOwnership("models/**", 3, "model", "MODEL_DIRTY"),
        ArtifactOwnership("models/special.py", 5, "validation", "RESULT_DIRTY"),
    )
    multiple = resolve_owner(compile_owner_registry(rules), "models/special.py")
    assert len(multiple.all_matches) == 2
    assert OwnerDiagnosticCode.MULTIPLE_MATCH in {
        item.code for item in multiple.diagnostics
    }
    with pytest.raises(OwnerContractValidationError, match="MULTIPLE_MATCH"):
        validate_owner_resolution(multiple, strict=True)


def test_strict_compiler_rejects_broad_shadow_and_reports_unreachable() -> None:
    rules = (
        ArtifactOwnership("models/**", 3, "model", "MODEL_DIRTY"),
        ArtifactOwnership(
            "models/**/05_sensitivity.py", 5, "validation", "RESULT_DIRTY"
        ),
    )
    compilation = compile_owner_registry(rules)
    codes = {item.code for item in compilation.diagnostics}

    assert OwnerDiagnosticCode.SHADOWED in codes
    assert OwnerDiagnosticCode.UNREACHABLE in codes
    with pytest.raises(OwnerContractValidationError, match="SHADOWED"):
        validate_owner_compilation(compilation, strict=True)


def test_machine_readable_priority_can_be_intentional_but_never_comment_implicit() -> None:
    intentional_rules = (
        ArtifactOwnership(
            "models/special.py",
            5,
            "validation",
            "RESULT_DIRTY",
        ),
        ArtifactOwnership("models/**", 3, "model", "MODEL_DIRTY"),
    )
    authorization = OwnerPriorityAuthorization(
        winner_pattern="models/special.py",
        winner_owner_stage=5,
        loser_pattern="models/**",
        loser_owner_stage=3,
        issue_id="OWNER-TEST-001",
        rationale="The validation artifact is more specific than model source.",
    )
    intentional = resolve_owner(
        compile_owner_registry(
            intentional_rules, priority_authorizations=(authorization,)
        ),
        "models/special.py",
    )
    assert {item.code for item in intentional.diagnostics} == {
        OwnerDiagnosticCode.INTENTIONAL_PRIORITY
    }
    assert validate_owner_resolution(intentional, strict=True) is intentional

    implicit_rules = (
        ArtifactOwnership("models/special.py", 5, "validation", "RESULT_DIRTY"),
        ArtifactOwnership("models/**", 3, "model", "MODEL_DIRTY"),
    )
    implicit = resolve_owner(compile_owner_registry(implicit_rules), "models/special.py")
    assert OwnerDiagnosticCode.MULTIPLE_MATCH in {
        item.code for item in implicit.diagnostics
    }


def test_unsupported_static_pattern_analysis_is_explicitly_unanalyzable() -> None:
    rules = (
        ArtifactOwnership("models/[ab]/**", 3, "model", "MODEL_DIRTY"),
        ArtifactOwnership("models/a/file.py", 5, "validation", "RESULT_DIRTY"),
    )
    compilation = compile_owner_registry(rules)
    diagnostic = next(
        item
        for item in compilation.diagnostics
        if item.code is OwnerDiagnosticCode.UNANALYZABLE
    )
    assert "character-class" in diagnostic.explanation


def test_rule_and_owner_ids_are_stable_when_input_order_changes() -> None:
    forward = compile_owner_registry(
        ARTIFACT_OWNERSHIP_REGISTRY,
        priority_authorizations=OWNER_PRIORITY_AUTHORIZATIONS,
    )
    reverse = compile_owner_registry(
        tuple(reversed(ARTIFACT_OWNERSHIP_REGISTRY)),
        priority_authorizations=OWNER_PRIORITY_AUTHORIZATIONS,
    )

    forward_ids = {
        (rule.pattern, rule.owner_id, rule.rule_id) for rule in forward.rules
    }
    reverse_ids = {
        (rule.pattern, rule.owner_id, rule.rule_id) for rule in reverse.rules
    }
    assert forward_ids == reverse_ids
    assert all(rule.owner_id == f"owner:stage:{rule.owner_stage}" for rule in forward.rules)


def test_rule_id_and_canonical_hash_bind_exact_priority_authorization_metadata() -> None:
    rules = (
        ArtifactOwnership("models/special.py", 5, "validation", "RESULT_DIRTY"),
        ArtifactOwnership("models/**", 3, "model", "MODEL_DIRTY"),
    )
    authorization = OwnerPriorityAuthorization(
        winner_pattern="models/special.py",
        winner_owner_stage=5,
        loser_pattern="models/**",
        loser_owner_stage=3,
        issue_id="OWNER-ID-001",
        rationale="Exact test pair.",
    )
    without_authorization = compile_owner_registry(rules)
    with_authorization = compile_owner_registry(
        rules, priority_authorizations=(authorization,)
    )

    assert with_authorization.rules[0].rule_id != without_authorization.rules[0].rule_id
    assert canonical_sha256(with_authorization) != canonical_sha256(
        without_authorization
    )


def test_bundle_compilation_and_validation_are_pure_no_io_db_time_env_or_process(
    monkeypatch,
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("forbidden side effect")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr("sqlite3.connect", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr("time.time", forbidden)
    monkeypatch.setattr("random.random", forbidden)
    monkeypatch.setattr("os.getenv", forbidden)

    bundle = compile_workflow_contract_bundle()
    encoded = workflow_contract_bytes(bundle)
    digest = workflow_contract_sha256(bundle)
    resolution = resolve_owner(bundle.owner_compilation, "problem/source.md")
    assert validate_owner_resolution(resolution, strict=True) is resolution
    assert encoded
    assert len(digest) == 64
