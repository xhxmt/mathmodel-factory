from __future__ import annotations

import builtins
import copy
from collections import Counter
from dataclasses import FrozenInstanceError, fields, is_dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import random
import re
import socket
import sqlite3
import subprocess
import sys
import time
import uuid

import pytest

import factory_core.shadow_scheduler as shadow_scheduler_module
from factory_core.canonical import canonical_bytes, canonical_sha256
from factory_core.owner_compiler import OwnerDiagnosticCode
from factory_core.shadow_scheduler import (
    RECORDED_SNAPSHOT_SCHEMA,
    SHADOW_SCHEDULER_ENABLED_BY_DEFAULT,
    SUPPORTED_ACTIVE_INVOCATION_STATES_BY_TYPE,
    SUPPORTED_DOMAIN_READINESS_STATES,
    SUPPORTED_WORKFLOW_STATUSES,
    WORKFLOW_STATUS_PLAN_DISPOSITIONS,
    ContractIdentities,
    ExpectedCorrection,
    ParityStatus,
    ParityValue,
    ReadinessState,
    ScheduleCoordinate,
    SchedulerCore,
    SnapshotValidationError,
    StageV1ReadinessAdapter,
    TransitionAction,
    build_parity_receipt,
    parity_value,
    parity_receipt_bytes,
    parity_receipt_sha256,
    readiness_input_analysis_bytes,
    readiness_input_analysis_sha256,
    readiness_input_semantic_bytes,
    readiness_input_semantic_sha256,
    readiness_result_analysis_bytes,
    readiness_result_analysis_sha256,
    readiness_result_semantic_bytes,
    readiness_result_semantic_sha256,
    transition_plan_analysis_bytes,
    transition_plan_analysis_sha256,
    transition_plan_semantic_bytes,
    transition_plan_semantic_sha256,
)
from factory_core.workflow_contract import (
    WorkflowContractValidationError,
    compile_workflow_contract_bundle,
    validate_workflow_contract_bundle,
    workflow_contract_analysis_bytes,
    workflow_contract_analysis_sha256,
    workflow_contract_bytes,
    workflow_contract_sha256,
)


ROOT = Path(__file__).resolve().parents[1]
PARITY_MATRIX = (
    ROOT / "tests" / "fixtures" / "scheduler_shadow_v1" / "parity_cases.json"
)
CHARACTERIZATION_INDEX = (
    ROOT / "tests" / "fixtures" / "v1_characterization" / "index.json"
)
REPLAY_SCRIPT = ROOT / "tests" / "support" / "m02_shadow_replay.py"
GOLDEN_IDENTITY = (
    ROOT / "tests" / "fixtures" / "scheduler_shadow_v1" / "golden_identity.json"
)
CONTRACT_BOUNDARY_ERRORS = (
    WorkflowContractValidationError,
    SnapshotValidationError,
)


def _coordinate(bundle, subtask_key: str) -> dict[str, object]:
    for stage in bundle.stages:
        for subtask in stage.subtasks:
            if subtask.key == subtask_key:
                return {
                    "stage_id": stage.stage_id,
                    "subtask": subtask.key,
                    "source_step_id": subtask.source_step_id,
                }
    raise AssertionError(f"missing subtask {subtask_key}")


def _checkpoint(bundle, subtask_key: str, revision: int) -> dict[str, object]:
    coordinate = _coordinate(bundle, subtask_key)
    subtask = next(
        item
        for stage in bundle.stages
        for item in stage.subtasks
        if item.key == subtask_key
    )
    return {
        "coordinate": coordinate,
        "checkpoint_step_id": subtask.checkpoint_step_id,
        "completed_revision": revision,
        "receipt_sha256": hashlib.sha256(
            f"{subtask_key}:{revision}".encode("utf-8")
        ).hexdigest(),
    }


def _all_checkpoints(bundle) -> list[dict[str, object]]:
    return [
        _checkpoint(bundle, subtask.key, revision=index + 1)
        for index, subtask in enumerate(
            subtask for stage in bundle.stages for subtask in stage.subtasks
        )
    ]


def _checkpoints_before(bundle, subtask_key: str) -> list[dict[str, object]]:
    values = []
    for stage in bundle.stages:
        for subtask in stage.subtasks:
            if subtask.key == subtask_key:
                return values
            values.append(_checkpoint(bundle, subtask.key, revision=len(values) + 1))
    raise AssertionError(f"missing subtask {subtask_key}")


def _snapshot(bundle) -> dict[str, object]:
    coordinate = _coordinate(bundle, "problem_setup")
    return {
        "schema_version": RECORDED_SNAPSHOT_SCHEMA,
        "project_id": "fixture:m02",
        "project_revision": 42,
        "run_generation": "run-generation:7",
        "runtime_generation": bundle.runtime_generation,
        "scheduler_generation": bundle.scheduler_generation,
        "workflow_status": "ready",
        "last_completed_step": -1,
        "last_completed_stage": 0,
        "attempt": 0,
        "stage_cursor": {
            "coordinate": coordinate,
            "active_step": coordinate["source_step_id"],
            "attempt": 0,
        },
        "checkpoint_heads": [],
        "dirty_refs": [],
        "pending_action_refs": [],
        "active_invocation_refs": [],
        "terminal_delivery": {
            "is_terminal": False,
            "terminal_reason": None,
            "delivery_capability": "contest-delivery-eligible",
            "delivery_allowed": False,
        },
        "domain_readiness": [],
        "immutable_refs": [
            {
                "kind": "recorded-stage-cursor",
                "ref": "snapshot:revision:42",
                "sha256": "4" * 64,
                "behavior_binding": False,
            }
        ],
        "availability": [
            {
                "fact": "recorded-workflow-state",
                "availability": "AVAILABLE",
                "required_for_plan": True,
                "gap_id": None,
            }
        ],
    }


def _adapt_plan(bundle, snapshot):
    readiness_input = StageV1ReadinessAdapter(bundle).adapt(snapshot)
    return readiness_input, SchedulerCore.plan(bundle, readiness_input)


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
    assert value is None or isinstance(value, (bool, int, str, TransitionAction, ReadinessState))


def _subclass_clone(value):
    subclass = type(f"Forged{type(value).__name__}", (type(value),), {})
    forged = object.__new__(subclass)
    for item in fields(value):
        object.__setattr__(forged, item.name, getattr(value, item.name))
    object.__setattr__(forged, "mutable_extra", [])
    return forged


def test_adapter_and_plan_are_deeply_immutable_and_defensively_freeze_input() -> None:
    bundle = compile_workflow_contract_bundle()
    source = _snapshot(bundle)
    readiness_input, decision = _adapt_plan(bundle, source)
    before = canonical_bytes((readiness_input, decision))

    source["project_revision"] = 999
    source["immutable_refs"][0]["sha256"] = "9" * 64  # type: ignore[index]
    source["checkpoint_heads"].append(  # type: ignore[union-attr]
        _checkpoint(bundle, "problem_setup", revision=99)
    )

    assert canonical_bytes((readiness_input, decision)) == before
    with pytest.raises(FrozenInstanceError):
        readiness_input.project_revision = 1  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        decision.plan.action = TransitionAction.WAIT  # type: ignore[misc]
    _assert_deeply_immutable(readiness_input)
    _assert_deeply_immutable(decision)


def _mutate_project_id(value):
    value["project_id"] = "fixture:m02:changed"


def _mutate_revision(value):
    value["project_revision"] = 43


def _mutate_run_generation(value):
    value["run_generation"] = "run-generation:8"


def _mutate_workflow_status(value):
    value["workflow_status"] = "running"


def _mutate_completed_step(value):
    value["last_completed_step"] = 0


def _mutate_completed_stage(value):
    value["last_completed_stage"] = 1


def _mutate_attempt(value):
    value["attempt"] = 1
    value["stage_cursor"]["attempt"] = 1


def _mutate_cursor(value):
    value["stage_cursor"] = None


def _mutate_checkpoint(value):
    bundle = compile_workflow_contract_bundle()
    value["checkpoint_heads"].append(_checkpoint(bundle, "problem_setup", 41))


def _mutate_dirty(value):
    value["dirty_refs"].append(
        {"flag": "MODEL_DIRTY", "owner_stage": 1, "cause_ref": "artifact:model"}
    )


def _mutate_pending(value):
    value["pending_action_refs"].append(
        {
            "action_type": "human_approval",
            "gate": "content_freeze",
            "request_ref": "decision-request:1",
            "generation": 1,
            "coordinate": value["stage_cursor"]["coordinate"],
        }
    )


def _mutate_invocation(value):
    value["active_invocation_refs"].append(
        {
            "invocation_id": "solver:1",
            "invocation_type": "solver",
            "state": "running",
            "coordinate": value["stage_cursor"]["coordinate"],
        }
    )


def _mutate_terminal(value):
    value["workflow_status"] = "failed"
    value["terminal_delivery"]["is_terminal"] = True
    value["terminal_delivery"]["terminal_reason"] = "TECHNICAL_TERMINAL"


def _mutate_delivery_capability(value):
    value["terminal_delivery"]["delivery_capability"] = "technical-no-delivery"


def _mutate_domain(value):
    value["domain_readiness"].append(
        {
            "domain": "packet",
            "state": "READY",
            "action_hint": "STAY",
            "target": value["stage_cursor"]["coordinate"],
            "reason_code": "PACKET_READY",
            "evidence_ref": "fixture:packet",
        }
    )


def _mutate_behavior_ref(value):
    value["immutable_refs"][0]["behavior_binding"] = True


def _mutate_availability(value):
    value["availability"][0]["availability"] = "UNAVAILABLE_LEGACY_UNBOUND"
    value["availability"][0]["gap_id"] = "GAP-M02-STATE"


@pytest.mark.parametrize(
    "mutation",
    (
        _mutate_project_id,
        _mutate_revision,
        _mutate_run_generation,
        _mutate_workflow_status,
        _mutate_completed_step,
        _mutate_completed_stage,
        _mutate_attempt,
        _mutate_cursor,
        _mutate_checkpoint,
        _mutate_dirty,
        _mutate_pending,
        _mutate_invocation,
        _mutate_terminal,
        _mutate_delivery_capability,
        _mutate_domain,
        _mutate_behavior_ref,
        _mutate_availability,
    ),
)
def test_each_readiness_behavior_field_family_changes_semantic_identity(mutation) -> None:
    bundle = compile_workflow_contract_bundle()
    baseline = _snapshot(bundle)
    changed = copy.deepcopy(baseline)
    mutation(changed)

    first = StageV1ReadinessAdapter(bundle).adapt(baseline)
    second = StageV1ReadinessAdapter(bundle).adapt(changed)

    assert readiness_input_semantic_sha256(first) != readiness_input_semantic_sha256(second)


@pytest.mark.parametrize(
    "case",
    (
        "immutable_ref_digest",
        "immutable_ref_kind",
        "immutable_ref_ref",
        "availability_gap_id",
        "checkpoint_receipt_digest",
        "checkpoint_completed_revision",
        "dirty_ref_cause_ref",
        "domain_evidence_ref",
    ),
)
def test_each_readiness_analysis_only_field_changes_only_analysis_identity(case) -> None:
    bundle = compile_workflow_contract_bundle()
    baseline = _snapshot(bundle)
    if case == "checkpoint_receipt_digest":
        baseline["stage_cursor"] = None
        baseline["last_completed_step"] = 0
        baseline["checkpoint_heads"] = [_checkpoint(bundle, "problem_setup", 40)]
    elif case == "domain_evidence_ref":
        baseline["domain_readiness"] = [
            {
                "domain": "packet",
                "state": "READY",
                "action_hint": "STAY",
                "target": baseline["stage_cursor"]["coordinate"],
                "reason_code": "PACKET_READY",
                "evidence_ref": "analysis-only:evidence:first",
            }
        ]
    elif case == "checkpoint_completed_revision":
        baseline["stage_cursor"] = None
        baseline["last_completed_step"] = 0
        baseline["checkpoint_heads"] = [_checkpoint(bundle, "problem_setup", 40)]
    elif case == "dirty_ref_cause_ref":
        baseline["dirty_refs"] = [
            {"flag": "MODEL_DIRTY", "owner_stage": 1, "cause_ref": "first"}
        ]
    changed = copy.deepcopy(baseline)
    if case == "immutable_ref_digest":
        changed["immutable_refs"][0]["sha256"] = "5" * 64
    elif case == "immutable_ref_kind":
        changed["immutable_refs"][0]["kind"] = "analysis-only-kind"
    elif case == "immutable_ref_ref":
        changed["immutable_refs"][0]["ref"] = "analysis-only:ref"
    elif case == "availability_gap_id":
        changed["availability"][0]["gap_id"] = "ANALYSIS-ONLY-GAP-TEXT"
    elif case == "checkpoint_receipt_digest":
        changed["checkpoint_heads"][0]["receipt_sha256"] = "5" * 64
    elif case == "checkpoint_completed_revision":
        changed["checkpoint_heads"][0]["completed_revision"] = 41
    elif case == "dirty_ref_cause_ref":
        changed["dirty_refs"][0]["cause_ref"] = "second"
    else:
        changed["domain_readiness"][0]["evidence_ref"] = (
            "analysis-only:evidence:second"
        )

    first_input, first = _adapt_plan(bundle, baseline)
    second_input, second = _adapt_plan(bundle, changed)

    assert readiness_input_semantic_sha256(first_input) == readiness_input_semantic_sha256(second_input)
    assert readiness_input_analysis_sha256(first_input) != readiness_input_analysis_sha256(second_input)
    assert transition_plan_semantic_sha256(first.plan) == transition_plan_semantic_sha256(second.plan)
    assert transition_plan_analysis_sha256(first.plan) != transition_plan_analysis_sha256(second.plan)


def _bundle_with_analysis_only_change(bundle, case):
    if case == "step_prompt":
        return replace(
            bundle,
            steps=(
                replace(bundle.steps[0], prompt="analysis-only:step-prompt"),
                *bundle.steps[1:],
            ),
        )
    if case == "gate_producer":
        return replace(
            bundle,
            gates=(
                replace(bundle.gates[0], producer="analysis-only:gate-producer"),
                *bundle.gates[1:],
            ),
        )
    if case == "owner_compiler_schema":
        return replace(
            bundle,
            owner_compilation=replace(
                bundle.owner_compilation,
                schema_version="analysis-only:owner-compiler-schema",
            ),
        )
    diagnostic = bundle.owner_compilation.diagnostics[0]
    if case == "diagnostic_code":
        changed = replace(
            diagnostic,
            code=next(
                item for item in OwnerDiagnosticCode if item is not diagnostic.code
            ),
        )
    elif case == "diagnostic_rule_ids":
        changed = replace(
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
        changed = replace(
            diagnostic,
            **{diagnostic_field: f"analysis-only:{diagnostic_field}"},
        )
    return replace(
        bundle,
        owner_compilation=replace(
            bundle.owner_compilation,
            diagnostics=(changed, *bundle.owner_compilation.diagnostics[1:]),
        ),
    )


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
def test_each_workflow_analysis_only_field_preserves_semantics_and_binds_chain(
    case,
) -> None:
    bundle = compile_workflow_contract_bundle()
    analysis_changed = _bundle_with_analysis_only_change(bundle, case)
    snapshot = _snapshot(bundle)

    first_input, first = _adapt_plan(bundle, snapshot)
    second_input, second = _adapt_plan(analysis_changed, snapshot)
    receipt = build_parity_receipt(
        analysis_changed,
        fixture_id=f"M02-ANALYSIS-ONLY-{case}",
        decision=second,
        v1=parity_value(second.plan),
        evidence_refs=("tests/test_m02_shadow_scheduler.py",),
    )

    assert validate_workflow_contract_bundle(analysis_changed) is analysis_changed
    assert workflow_contract_bytes(analysis_changed) == workflow_contract_bytes(bundle)
    assert workflow_contract_sha256(analysis_changed) == workflow_contract_sha256(bundle)
    assert workflow_contract_analysis_bytes(
        analysis_changed
    ) != workflow_contract_analysis_bytes(bundle)
    assert workflow_contract_analysis_sha256(
        analysis_changed
    ) != workflow_contract_analysis_sha256(bundle)
    assert first_input.contract.semantic_sha256 == second_input.contract.semantic_sha256
    assert first_input.contract.analysis_sha256 != second_input.contract.analysis_sha256
    assert second_input.contract.analysis_sha256 == workflow_contract_analysis_sha256(
        analysis_changed
    )
    assert second.readiness.contract == second_input.contract
    assert second.plan.contract == second_input.contract
    assert receipt.contract == second_input.contract
    assert receipt.status is ParityStatus.MATCH
    assert transition_plan_semantic_sha256(first.plan) == transition_plan_semantic_sha256(second.plan)
    assert transition_plan_analysis_sha256(first.plan) != transition_plan_analysis_sha256(second.plan)


def test_unvalidated_step_behavior_changes_semantic_hash_but_is_not_trusted() -> None:
    bundle = compile_workflow_contract_bundle()
    changed_step = replace(bundle.steps[0], default_models=("behavior-changed",))
    behavior_changed = replace(bundle, steps=(changed_step, *bundle.steps[1:]))
    snapshot = _snapshot(bundle)
    valid_input, valid_decision = _adapt_plan(bundle, snapshot)

    assert workflow_contract_sha256(behavior_changed) != workflow_contract_sha256(bundle)
    for entrypoint in ("validator", "adapter", "core", "receipt"):
        with pytest.raises(CONTRACT_BOUNDARY_ERRORS):
            if entrypoint == "validator":
                validate_workflow_contract_bundle(behavior_changed)
            elif entrypoint == "adapter":
                StageV1ReadinessAdapter(behavior_changed).adapt(snapshot)
            elif entrypoint == "core":
                SchedulerCore.plan(behavior_changed, valid_input)
            else:
                build_parity_receipt(
                    behavior_changed,
                    fixture_id="M02-UNAUTHORIZED-STEP-BEHAVIOR",
                    decision=valid_decision,
                    v1=ParityValue(
                        valid_decision.plan.action,
                        valid_decision.plan.target,
                        valid_decision.plan.execution_route,
                    ),
                    evidence_refs=("tests/test_m02_shadow_scheduler.py",),
                )


def test_unsupported_or_unverified_bundle_fails_before_adapter_or_receipt() -> None:
    bundle = compile_workflow_contract_bundle()
    unsupported_owner = replace(bundle.owner_compilation, mode="ordered-last-match-v1")
    unsupported = replace(bundle, owner_compilation=unsupported_owner)
    malformed = replace(bundle, schema_version="unverified-hand-built")
    _, decision = _adapt_plan(bundle, _snapshot(bundle))
    receipts = []

    for invalid in (unsupported, malformed):
        with pytest.raises(WorkflowContractValidationError):
            StageV1ReadinessAdapter(invalid)
        with pytest.raises(WorkflowContractValidationError):
            receipts.append(
                build_parity_receipt(
                    invalid,
                    fixture_id="M02-INVALID",
                    decision=decision,
                    v1=ParityValue(TransitionAction.DISPATCH, decision.plan.target),
                    evidence_refs=("tests/test_m02_shadow_scheduler.py",),
                )
            )
    assert receipts == []


def test_snapshot_requires_explicit_recorded_facts_and_atomic_known_cursor() -> None:
    bundle = compile_workflow_contract_bundle()
    missing = _snapshot(bundle)
    del missing["pending_action_refs"]
    with pytest.raises(SnapshotValidationError, match="pending_action_refs"):
        StageV1ReadinessAdapter(bundle).adapt(missing)

    non_atomic = _snapshot(bundle)
    non_atomic["stage_cursor"]["active_step"] = 16
    with pytest.raises(SnapshotValidationError, match="not atomic"):
        StageV1ReadinessAdapter(bundle).adapt(non_atomic)

    unknown = _snapshot(bundle)
    unknown["stage_cursor"]["coordinate"]["subtask"] = "not-in-catalog"
    with pytest.raises(SnapshotValidationError, match="outside"):
        StageV1ReadinessAdapter(bundle).adapt(unknown)

    unavailable_without_gap = _snapshot(bundle)
    unavailable_without_gap["availability"][0]["availability"] = (
        "UNAVAILABLE_LEGACY_UNBOUND"
    )
    with pytest.raises(SnapshotValidationError, match="explicit gap_id"):
        StageV1ReadinessAdapter(bundle).adapt(unavailable_without_gap)


def test_unknown_workflow_status_is_rejected_before_dispatch() -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot["workflow_status"] = "unknown-workflow-status"

    with pytest.raises(SnapshotValidationError, match="workflow_status.*unsupported"):
        StageV1ReadinessAdapter(bundle).adapt(snapshot)


def test_unknown_active_invocation_state_is_rejected_before_dispatch() -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot["active_invocation_refs"] = [
        {
            "invocation_id": "solver:unknown-state",
            "invocation_type": "solver",
            "state": "unknown-invocation-state",
            "coordinate": snapshot["stage_cursor"]["coordinate"],
        }
    ]

    with pytest.raises(
        SnapshotValidationError, match=r"active_invocation_refs\[0\]\.state.*unsupported"
    ):
        StageV1ReadinessAdapter(bundle).adapt(snapshot)


def test_unknown_domain_readiness_state_is_rejected_before_dispatch() -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot["domain_readiness"] = [
        {
            "domain": "adversarial-domain",
            "state": "unknown-domain-state",
            "action_hint": None,
            "target": None,
            "reason_code": "UNKNOWN_DOMAIN_STATE",
            "evidence_ref": "fixture:unknown-domain-state",
        }
    ]

    with pytest.raises(
        SnapshotValidationError, match=r"domain_readiness\[0\]\.state.*unsupported"
    ):
        StageV1ReadinessAdapter(bundle).adapt(snapshot)


@pytest.mark.parametrize("invalid", ("", "READY", "ready "))
def test_workflow_status_vocabulary_is_case_and_whitespace_exact(invalid: str) -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot["workflow_status"] = invalid

    with pytest.raises(SnapshotValidationError):
        StageV1ReadinessAdapter(bundle).adapt(snapshot)


@pytest.mark.parametrize("invalid", ("", "Solver", "solver ", "provider"))
def test_active_invocation_type_vocabulary_is_exact(invalid: str) -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot["active_invocation_refs"] = [
        {
            "invocation_id": "invocation:1",
            "invocation_type": invalid,
            "state": "running",
            "coordinate": snapshot["stage_cursor"]["coordinate"],
        }
    ]

    with pytest.raises(SnapshotValidationError):
        StageV1ReadinessAdapter(bundle).adapt(snapshot)


@pytest.mark.parametrize("invalid", ("", "RUNNING", "running ", "completed"))
def test_active_invocation_state_vocabulary_is_exact_and_active_only(
    invalid: str,
) -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot["active_invocation_refs"] = [
        {
            "invocation_id": "solver:1",
            "invocation_type": "solver",
            "state": invalid,
            "coordinate": snapshot["stage_cursor"]["coordinate"],
        }
    ]

    with pytest.raises(SnapshotValidationError):
        StageV1ReadinessAdapter(bundle).adapt(snapshot)


@pytest.mark.parametrize("invalid", ("", "ready", "READY ", "UNKNOWN"))
def test_domain_readiness_state_vocabulary_is_exact(invalid: str) -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot["domain_readiness"] = [
        {
            "domain": "domain-vocabulary",
            "state": invalid,
            "action_hint": None,
            "target": None,
            "reason_code": "DOMAIN_VOCABULARY",
            "evidence_ref": "fixture:domain-vocabulary",
        }
    ]

    with pytest.raises(SnapshotValidationError):
        StageV1ReadinessAdapter(bundle).adapt(snapshot)


@pytest.mark.parametrize("invalid", ("", "available", "AVAILABLE ", "UNKNOWN"))
def test_fact_availability_vocabulary_is_exact(invalid: str) -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot["availability"][0]["availability"] = invalid

    with pytest.raises(SnapshotValidationError):
        StageV1ReadinessAdapter(bundle).adapt(snapshot)


@pytest.mark.parametrize("invalid", ("", "model_dirty", "MODEL_DIRTY ", "UNKNOWN"))
def test_dirty_flag_vocabulary_is_exact(invalid: str) -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot["dirty_refs"] = [
        {"flag": invalid, "owner_stage": 1, "cause_ref": "artifact:model"}
    ]

    with pytest.raises(SnapshotValidationError):
        StageV1ReadinessAdapter(bundle).adapt(snapshot)


@pytest.mark.parametrize(
    "invalid",
    ("", "CONTEST-DELIVERY-ELIGIBLE", "contest-delivery-eligible ", "unknown"),
)
def test_delivery_capability_vocabulary_is_exact(invalid: str) -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot["terminal_delivery"]["delivery_capability"] = invalid

    with pytest.raises(SnapshotValidationError):
        StageV1ReadinessAdapter(bundle).adapt(snapshot)


@pytest.mark.parametrize(
    ("workflow_status", "disposition", "expected_action"),
    (
        ("archiving", "WAIT", TransitionAction.WAIT),
        ("awaiting_consultation", "WAIT", TransitionAction.WAIT),
        ("awaiting_selection", "WAIT", TransitionAction.WAIT),
        ("completed", "TERMINAL_ONLY", TransitionAction.TERMINATE),
        ("failed", "WAIT", TransitionAction.WAIT),
        ("interrupted", "WAIT", TransitionAction.WAIT),
        ("killed", "TERMINAL_ONLY", TransitionAction.TERMINATE),
        ("paused", "WAIT", TransitionAction.WAIT),
        ("ready", "RUNNABLE", TransitionAction.DISPATCH),
        ("retrying", "RUNNABLE", TransitionAction.DISPATCH),
        ("running", "RUNNABLE", TransitionAction.DISPATCH),
    ),
)
def test_every_current_workflow_status_has_an_exhaustive_planner_action(
    workflow_status: str,
    disposition: str,
    expected_action: TransitionAction,
) -> None:
    expected_dispositions = {
        "archiving": "WAIT",
        "awaiting_consultation": "WAIT",
        "awaiting_selection": "WAIT",
        "completed": "TERMINAL_ONLY",
        "failed": "WAIT",
        "interrupted": "WAIT",
        "killed": "TERMINAL_ONLY",
        "paused": "WAIT",
        "ready": "RUNNABLE",
        "retrying": "RUNNABLE",
        "running": "RUNNABLE",
    }
    assert frozenset(expected_dispositions) == SUPPORTED_WORKFLOW_STATUSES
    assert dict(WORKFLOW_STATUS_PLAN_DISPOSITIONS) == expected_dispositions
    assert WORKFLOW_STATUS_PLAN_DISPOSITIONS[workflow_status] == disposition

    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot["workflow_status"] = workflow_status
    if workflow_status in {"completed", "killed"}:
        snapshot["terminal_delivery"] = {
            "is_terminal": True,
            "terminal_reason": f"RECORDED_{workflow_status.upper()}",
            "delivery_capability": "technical-no-delivery",
            "delivery_allowed": False,
        }

    _, decision = _adapt_plan(bundle, snapshot)

    assert decision.plan.action is expected_action


def test_interrupted_explicit_recovery_domain_action_precedes_generic_status_wait() -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot["workflow_status"] = "interrupted"
    snapshot["domain_readiness"] = [
        {
            "domain": "recorded-recovery",
            "state": "READY",
            "action_hint": "REOPEN",
            "target": _coordinate(bundle, "problem_setup"),
            "reason_code": "RECORDED_RECOVERY_REOPEN",
            "evidence_ref": "fixture:recorded-recovery",
        }
    ]

    _, decision = _adapt_plan(bundle, snapshot)

    assert decision.plan.action is TransitionAction.REOPEN
    assert decision.plan.reason_code == "RECORDED_RECOVERY_REOPEN"


@pytest.mark.parametrize(
    ("invocation_type", "state"),
    tuple(
        (invocation_type, state)
        for invocation_type, states in sorted(
            SUPPORTED_ACTIVE_INVOCATION_STATES_BY_TYPE.items()
        )
        for state in sorted(states)
    ),
)
def test_all_current_active_invocation_values_wait(
    invocation_type: str, state: str
) -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot["active_invocation_refs"] = [
        {
            "invocation_id": f"{invocation_type}:{state}",
            "invocation_type": invocation_type,
            "state": state,
            "coordinate": snapshot["stage_cursor"]["coordinate"],
        }
    ]

    _, decision = _adapt_plan(bundle, snapshot)

    assert decision.plan.action is TransitionAction.WAIT


@pytest.mark.parametrize("state", sorted(SUPPORTED_DOMAIN_READINESS_STATES))
def test_all_current_domain_readiness_values_retain_documented_behavior(
    state: str,
) -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot["domain_readiness"] = [
        {
            "domain": "known-domain",
            "state": state,
            "action_hint": "WAIT" if state != "READY" else None,
            "target": None,
            "reason_code": f"KNOWN_{state}",
            "evidence_ref": "fixture:known-domain",
        }
    ]

    _, decision = _adapt_plan(bundle, snapshot)

    expected = TransitionAction.DISPATCH if state == "READY" else TransitionAction.WAIT
    assert decision.plan.action is expected


@pytest.mark.parametrize(
    ("workflow_status", "is_terminal", "terminal_reason", "capability", "allowed", "match"),
    (
        ("ready", True, "FALSE_TERMINAL", "technical-no-delivery", False, "conflicts"),
        ("completed", False, None, "contest-delivery-eligible", False, "requires"),
        ("failed", True, None, "technical-no-delivery", False, "terminal_reason"),
        ("ready", False, "IMPOSSIBLE_REASON", "technical-no-delivery", False, "terminal_reason"),
        ("ready", False, None, "contest-delivery-eligible", True, "explicit terminal"),
        ("failed", True, "FAILED", "contest-delivery-eligible", True, "completed"),
        ("completed", True, "DONE", "technical-no-delivery", True, "capability"),
    ),
)
def test_terminal_delivery_fact_contradictions_fail_closed(
    workflow_status: str,
    is_terminal: bool,
    terminal_reason: str | None,
    capability: str,
    allowed: bool,
    match: str,
) -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot["workflow_status"] = workflow_status
    snapshot["terminal_delivery"] = {
        "is_terminal": is_terminal,
        "terminal_reason": terminal_reason,
        "delivery_capability": capability,
        "delivery_allowed": allowed,
    }

    with pytest.raises(SnapshotValidationError, match=match):
        StageV1ReadinessAdapter(bundle).adapt(snapshot)


@pytest.mark.parametrize(
    ("state", "action"),
    (("READY", "WAIT"), ("WAITING", "DISPATCH"), ("BLOCKED", "REOPEN")),
)
def test_contradictory_domain_state_and_action_fail_closed(
    state: str, action: str
) -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot["domain_readiness"] = [
        {
            "domain": "contradictory-domain",
            "state": state,
            "action_hint": action,
            "target": snapshot["stage_cursor"]["coordinate"],
            "reason_code": "CONTRADICTORY_DOMAIN",
            "evidence_ref": "fixture:contradictory-domain",
        }
    ]

    with pytest.raises(SnapshotValidationError):
        StageV1ReadinessAdapter(bundle).adapt(snapshot)


def test_domain_terminate_hint_cannot_substitute_for_terminal_facts() -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot["domain_readiness"] = [
        {
            "domain": "false-terminal",
            "state": "READY",
            "action_hint": "TERMINATE",
            "target": None,
            "reason_code": "FALSE_TERMINAL",
            "evidence_ref": "fixture:false-terminal",
        }
    ]

    with pytest.raises(SnapshotValidationError, match="terminal_delivery"):
        StageV1ReadinessAdapter(bundle).adapt(snapshot)


@pytest.mark.parametrize("invalid_hash", ("", "a" * 63, "A" * 64, "g" * 64))
@pytest.mark.parametrize("hash_field", ("checkpoint", "immutable"))
def test_hash_shaped_snapshot_fields_require_lowercase_sha256(
    hash_field: str, invalid_hash: str
) -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    if hash_field == "checkpoint":
        snapshot["checkpoint_heads"] = [_checkpoint(bundle, "problem_setup", 40)]
        snapshot["checkpoint_heads"][0]["receipt_sha256"] = invalid_hash
    else:
        snapshot["immutable_refs"][0]["sha256"] = invalid_hash

    with pytest.raises(SnapshotValidationError, match="64 lowercase hexadecimal"):
        StageV1ReadinessAdapter(bundle).adapt(snapshot)


def test_checkpoint_and_dirty_owner_facts_must_match_recorded_revision_and_catalog() -> None:
    bundle = compile_workflow_contract_bundle()
    adapter = StageV1ReadinessAdapter(bundle)

    wrong_step = _snapshot(bundle)
    wrong_step["checkpoint_heads"] = [_checkpoint(bundle, "problem_setup", 40)]
    wrong_step["checkpoint_heads"][0]["checkpoint_step_id"] = 1
    with pytest.raises(SnapshotValidationError, match="validated Stage catalog"):
        adapter.adapt(wrong_step)

    future_revision = _snapshot(bundle)
    future_revision["checkpoint_heads"] = [_checkpoint(bundle, "problem_setup", 43)]
    with pytest.raises(SnapshotValidationError, match="exceeds project_revision"):
        adapter.adapt(future_revision)

    unknown_owner = _snapshot(bundle)
    unknown_owner["dirty_refs"] = [
        {"flag": "MODEL_DIRTY", "owner_stage": 999, "cause_ref": "artifact:model"}
    ]
    with pytest.raises(SnapshotValidationError, match="outside"):
        adapter.adapt(unknown_owner)


def test_scheduler_core_revalidates_behavior_vocabularies_for_forged_dtos() -> None:
    bundle = compile_workflow_contract_bundle()
    adapter = StageV1ReadinessAdapter(bundle)
    baseline = adapter.adapt(_snapshot(bundle))

    invocation_snapshot = _snapshot(bundle)
    invocation_snapshot["active_invocation_refs"] = [
        {
            "invocation_id": "solver:running",
            "invocation_type": "solver",
            "state": "running",
            "coordinate": invocation_snapshot["stage_cursor"]["coordinate"],
        }
    ]
    invocation_input = adapter.adapt(invocation_snapshot)

    domain_snapshot = _snapshot(bundle)
    domain_snapshot["domain_readiness"] = [
        {
            "domain": "known-domain",
            "state": "READY",
            "action_hint": None,
            "target": None,
            "reason_code": "KNOWN_DOMAIN",
            "evidence_ref": "fixture:known-domain",
        }
    ]
    domain_input = adapter.adapt(domain_snapshot)

    dirty_snapshot = _snapshot(bundle)
    dirty_snapshot["dirty_refs"] = [
        {"flag": "MODEL_DIRTY", "owner_stage": 1, "cause_ref": "artifact:model"}
    ]
    dirty_input = adapter.adapt(dirty_snapshot)

    forged_values = (
        replace(baseline, workflow_status="unknown-workflow-status"),
        replace(
            invocation_input,
            active_invocation_refs=(
                replace(
                    invocation_input.active_invocation_refs[0],
                    state="unknown-invocation-state",
                ),
            ),
        ),
        replace(
            domain_input,
            domain_readiness=(
                replace(domain_input.domain_readiness[0], state="unknown-domain-state"),
            ),
        ),
        replace(
            dirty_input,
            dirty_refs=(replace(dirty_input.dirty_refs[0], flag="UNKNOWN_DIRTY"),),
        ),
        replace(baseline, semantic_dirty_flags=("UNKNOWN_DIRTY",)),
        replace(
            baseline,
            availability=(
                replace(baseline.availability[0], availability="UNKNOWN"),
            ),
        ),
        replace(
            baseline,
            terminal_delivery=replace(
                baseline.terminal_delivery, delivery_capability="unknown"
            ),
        ),
    )

    for forged in forged_values:
        with pytest.raises(SnapshotValidationError):
            SchedulerCore.plan(bundle, forged)


def test_scheduler_core_rejects_forged_catalog_domain_and_checkpoint_facts() -> None:
    bundle = compile_workflow_contract_bundle()
    adapter = StageV1ReadinessAdapter(bundle)
    baseline = adapter.adapt(_snapshot(bundle))
    outside = ScheduleCoordinate(999, "outside-catalog", 999)

    foreign_entry = replace(baseline.catalog[0], coordinate=outside)
    forged_catalog = replace(
        baseline,
        stage_cursor=None,
        checkpoint_heads=(),
        catalog=(foreign_entry,),
    )

    recovery = _snapshot(bundle)
    recovery["workflow_status"] = "interrupted"
    recovery["domain_readiness"] = [
        {
            "domain": "recovery",
            "state": "READY",
            "action_hint": "REOPEN",
            "target": _coordinate(bundle, "problem_setup"),
            "reason_code": "RECOVERY_REQUIRED",
            "evidence_ref": "fixture:recovery",
        }
    ]
    recovery_input = adapter.adapt(recovery)
    forged_domains = tuple(
        replace(
            recovery_input,
            domain_readiness=(
                replace(
                    recovery_input.domain_readiness[0],
                    action_hint=action,
                    target=outside,
                ),
            ),
        )
        for action in (TransitionAction.REOPEN, TransitionAction.ADVANCE)
    )

    checkpoint_snapshot = _snapshot(bundle)
    checkpoint_snapshot["stage_cursor"] = None
    checkpoint_snapshot["checkpoint_heads"] = [
        _checkpoint(bundle, "problem_setup", 41)
    ]
    checkpoint_input = adapter.adapt(checkpoint_snapshot)
    forged_checkpoint = replace(
        checkpoint_input,
        checkpoint_heads=(
            replace(
                checkpoint_input.checkpoint_heads[0],
                completed_revision=checkpoint_input.project_revision + 1,
            ),
        ),
    )

    for forged in (forged_catalog, *forged_domains, forged_checkpoint):
        with pytest.raises(SnapshotValidationError):
            SchedulerCore.plan(bundle, forged)


def test_scheduler_core_rejects_forged_identity_generation_and_cursor_links() -> None:
    bundle = compile_workflow_contract_bundle()
    baseline = StageV1ReadinessAdapter(bundle).adapt(_snapshot(bundle))
    assert baseline.stage_cursor is not None
    forged_values = (
        replace(baseline, schema_version="stage-v1-readiness-input-forged"),
        replace(
            baseline,
            contract=replace(baseline.contract, semantic_sha256="0" * 64),
        ),
        replace(baseline, runtime_generation="native_v999"),
        replace(baseline, scheduler_generation="stage_v999"),
        replace(
            baseline,
            stage_cursor=replace(baseline.stage_cursor, active_step=999),
        ),
        replace(
            baseline,
            stage_cursor=replace(baseline.stage_cursor, attempt=baseline.attempt + 1),
        ),
    )

    for forged in forged_values:
        with pytest.raises(SnapshotValidationError):
            SchedulerCore.plan(bundle, forged)


def _bundle_with_subtask_condition(bundle, key, *, operator, operands):
    stages = tuple(
        replace(
            stage,
            subtasks=tuple(
                replace(
                    subtask,
                    condition=replace(
                        subtask.condition,
                        operator=operator,
                        operands=operands,
                    ),
                )
                if subtask.key == key
                else subtask
                for subtask in stage.subtasks
            ),
        )
        for stage in bundle.stages
    )
    return replace(bundle, stages=stages)


def _bundle_with_stage_catalog_forgery(bundle, case):
    first_stage = bundle.stages[0]
    first, second = first_stage.subtasks
    if case == "unknown-source-step":
        changed = replace(first, source_step_id=999)
        subtasks = (changed, second)
    elif case == "source-checkpoint-mismatch":
        changed = replace(first, source_step_id=second.source_step_id)
        subtasks = (changed, second)
    elif case == "duplicate-schedule-coordinate":
        changed = replace(
            second,
            key=first.key,
            source_step_id=first.source_step_id,
        )
        subtasks = (first, changed)
    elif case == "duplicate-subtask-key":
        changed = replace(second, key=first.key)
        subtasks = (first, changed)
    elif case == "duplicate-subtask-id":
        changed = replace(second, subtask_id=first.subtask_id)
        subtasks = (first, changed)
    else:  # pragma: no cover - the parametrization is the exhaustive case list.
        raise AssertionError(f"unsupported Stage catalog forgery {case!r}")
    return replace(
        bundle,
        stages=(replace(first_stage, subtasks=subtasks), *bundle.stages[1:]),
    )


def _test_only_self_signed_contract(bundle) -> ContractIdentities:
    """Create an identity from supplied bytes without authorizing the bundle.

    This helper exists only to ensure negative core/receipt tests cannot pass
    because they accidentally reuse the legitimate bundle identity.
    """

    semantic = workflow_contract_bytes(bundle)
    analysis = workflow_contract_analysis_bytes(bundle)
    return ContractIdentities(
        semantic_sha256=workflow_contract_sha256(bundle),
        semantic_bytes=len(semantic),
        analysis_sha256=workflow_contract_analysis_sha256(bundle),
        analysis_bytes=len(analysis),
        owner_resolution_mode=bundle.owner_compilation.mode,
    )


def _test_only_identity_matched_input_and_decision(
    source_bundle,
    forged_bundle,
    snapshot=None,
):
    """Build a coherent supplied-bundle input without a production shortcut."""

    recorded = snapshot or _snapshot(source_bundle)
    source_input = StageV1ReadinessAdapter(source_bundle).adapt(recorded)
    forged_input = replace(
        source_input,
        contract=_test_only_self_signed_contract(forged_bundle),
        runtime_generation=forged_bundle.runtime_generation,
        scheduler_generation=forged_bundle.scheduler_generation,
        catalog=shadow_scheduler_module._catalog(forged_bundle),
        semantic_dirty_flags=tuple(forged_bundle.classifier.semantic_dirty_flags),
    )
    forged_decision = SchedulerCore._plan_validated(
        forged_input,
        validated_step_ids=frozenset(step.step_id for step in forged_bundle.steps),
    )
    return forged_input, forged_decision


def _invoke_bundle_boundary(entrypoint, source_bundle, forged_bundle, snapshot=None):
    recorded = snapshot or _snapshot(source_bundle)
    if entrypoint == "validator":
        return validate_workflow_contract_bundle(forged_bundle)
    if entrypoint == "adapter":
        return StageV1ReadinessAdapter(forged_bundle).adapt(recorded)
    forged_input, forged_decision = _test_only_identity_matched_input_and_decision(
        source_bundle,
        forged_bundle,
        recorded,
    )
    assert forged_input.contract == _test_only_self_signed_contract(forged_bundle)
    if entrypoint == "core":
        return SchedulerCore.plan(forged_bundle, forged_input)
    if entrypoint == "receipt":
        return build_parity_receipt(
            forged_bundle,
            fixture_id="M02-FORGED-BUNDLE",
            decision=forged_decision,
            v1=parity_value(forged_decision.plan),
            evidence_refs=("tests/test_m02_shadow_scheduler.py",),
        )
    raise AssertionError(f"unsupported public entrypoint {entrypoint!r}")


def _bundle_with_step_forgery(bundle, case):
    step = bundle.steps[0]
    if case in {
        "timeout_seconds",
        "hang_timeout_seconds",
        "max_attempts",
        "max_reopens",
    }:
        changed = replace(
            step,
            budget=replace(
                step.budget,
                **{case: getattr(step.budget, case) + 1},
            ),
        )
    elif case == "default_models":
        changed = replace(step, default_models=("forged-model",))
    elif case == "implementation":
        changed = replace(step, implementation="forged-implementation")
    elif case == "contest_phase_id":
        changed = replace(step, contest_phase_id=2)
    elif case == "owner_id":
        changed = replace(step, owner_id="owner:stage:999")
    elif case == "authority":
        changed = replace(step, authority="forged-authority")
    elif case == "step_contract_id":
        changed = replace(step, step_contract_id="step:forged")
    elif case == "step_order":
        return replace(
            bundle,
            steps=(bundle.steps[1], bundle.steps[0], *bundle.steps[2:]),
        )
    else:  # pragma: no cover - exhaustive parametrization.
        raise AssertionError(f"unsupported Step forgery {case!r}")
    return replace(bundle, steps=(changed, *bundle.steps[1:]))


@pytest.mark.parametrize(
    ("case", "error_match"),
    (
        ("timeout_seconds", "budget.timeout_seconds mismatch"),
        ("hang_timeout_seconds", "budget.hang_timeout_seconds mismatch"),
        ("max_attempts", "budget.max_attempts mismatch"),
        ("max_reopens", "budget.max_reopens mismatch"),
        ("default_models", "behavior drift at index 0 field default_models"),
        ("implementation", "behavior drift at index 0 field implementation"),
        ("contest_phase_id", "contest_phase_id mismatch"),
        ("owner_id", "owner_id mismatch"),
        ("authority", "behavior drift at index 0 field authority"),
        ("step_contract_id", "behavior drift at index 0 field step_contract_id"),
        ("step_order", "Step IDs must remain ordered 0 through 16"),
    ),
)
@pytest.mark.parametrize("entrypoint", ("validator", "adapter", "core", "receipt"))
def test_step_source_trust_root_matrix_fails_closed_before_use(
    case,
    error_match,
    entrypoint,
) -> None:
    bundle = compile_workflow_contract_bundle()
    forged = _bundle_with_step_forgery(bundle, case)

    with pytest.raises(WorkflowContractValidationError, match=error_match):
        _invoke_bundle_boundary(entrypoint, bundle, forged)


def _bundle_with_classifier_forgery(bundle, case):
    classifier = bundle.classifier
    semantic = classifier.semantic_dirty_flags
    if case == "semantic_empty":
        changed = replace(classifier, semantic_dirty_flags=())
    elif case == "semantic_subset":
        changed = replace(classifier, semantic_dirty_flags=semantic[:1])
    elif case == "semantic_order":
        changed = replace(classifier, semantic_dirty_flags=tuple(reversed(semantic)))
    elif case == "semantic_unknown":
        changed = replace(classifier, semantic_dirty_flags=("UNKNOWN_DIRTY",))
    elif case == "semantic_supported_nonsemantic":
        changed = replace(classifier, semantic_dirty_flags=("FORMAT_DIRTY",))
    elif case == "classifier_id":
        changed = replace(classifier, classifier_id="classifier:forged")
    elif case == "dirty_classifier_schema":
        changed = replace(
            classifier,
            dirty_classifier_schema_version="factory-dirty-classifier-forged",
        )
    elif case == "ownership_schema":
        changed = replace(
            classifier,
            ownership_schema_version="factory-artifact-ownership-forged",
        )
    elif case == "rule_identity":
        changed = replace(classifier, rule_identity_sha256="0" * 64)
    elif case == "dirty_flags_subset":
        changed = replace(classifier, dirty_flags=classifier.dirty_flags[:-1])
    elif case == "dirty_flags_order":
        changed = replace(
            classifier,
            dirty_flags=tuple(reversed(classifier.dirty_flags)),
        )
    elif case == "step13_operands_mismatch":
        return _bundle_with_subtask_condition(
            bundle,
            "conditional_math_preflight",
            operator="ANY",
            operands=semantic[:1],
        )
    else:  # pragma: no cover - exhaustive parametrization.
        raise AssertionError(f"unsupported classifier forgery {case!r}")
    return replace(bundle, classifier=changed)


@pytest.mark.parametrize(
    ("case", "error_match"),
    (
        ("semantic_empty", "drift field semantic_dirty_flags"),
        ("semantic_subset", "drift field semantic_dirty_flags"),
        ("semantic_order", "drift field semantic_dirty_flags"),
        ("semantic_unknown", "drift field semantic_dirty_flags"),
        ("semantic_supported_nonsemantic", "drift field semantic_dirty_flags"),
        ("classifier_id", "drift field classifier_id"),
        ("dirty_classifier_schema", "drift field dirty_classifier_schema_version"),
        ("ownership_schema", "drift field ownership_schema_version"),
        ("rule_identity", "drift field rule_identity_sha256"),
        ("dirty_flags_subset", "drift field dirty_flags"),
        ("dirty_flags_order", "drift field dirty_flags"),
        (
            "step13_operands_mismatch",
            "conditional_math_preflight operands must exactly match source semantics",
        ),
    ),
)
@pytest.mark.parametrize("entrypoint", ("validator", "adapter", "core", "receipt"))
def test_classifier_source_trust_root_matrix_fails_closed_before_use(
    case,
    error_match,
    entrypoint,
) -> None:
    bundle = compile_workflow_contract_bundle()
    forged = _bundle_with_classifier_forgery(bundle, case)

    with pytest.raises(WorkflowContractValidationError, match=error_match):
        _invoke_bundle_boundary(entrypoint, bundle, forged)


def _step13_math_snapshot(bundle):
    snapshot = _snapshot(bundle)
    coordinate = _coordinate(bundle, "conditional_math_preflight")
    snapshot["stage_cursor"] = {
        "coordinate": coordinate,
        "active_step": 13,
        "attempt": 0,
    }
    snapshot["last_completed_step"] = 12
    snapshot["last_completed_stage"] = 7
    snapshot["checkpoint_heads"] = _checkpoints_before(
        bundle, "conditional_math_preflight"
    )
    snapshot["dirty_refs"] = [
        {"flag": "MATH_DIRTY", "owner_stage": 8, "cause_ref": "artifact:math"}
    ]
    return snapshot


def _bundle_with_coherent_classifier_step13_forgery(bundle):
    semantic_flags = ("FORMAT_DIRTY",)
    stages_changed = _bundle_with_subtask_condition(
        bundle,
        "conditional_math_preflight",
        operator="ANY",
        operands=semantic_flags,
    )
    return replace(
        stages_changed,
        classifier=replace(
            bundle.classifier,
            semantic_dirty_flags=semantic_flags,
        ),
    )


def test_math_dirty_route_is_step13_and_coherent_forged_skip_never_plans() -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _step13_math_snapshot(bundle)
    _, decision = _adapt_plan(bundle, snapshot)
    assert decision.plan.execution_route == "step:13"

    forged = _bundle_with_coherent_classifier_step13_forgery(bundle)
    for entrypoint in ("validator", "adapter", "core", "receipt"):
        with pytest.raises(
            WorkflowContractValidationError,
            match="conditional_math_preflight contains an unknown semantic operand",
        ):
            _invoke_bundle_boundary(
                entrypoint,
                bundle,
                forged,
                snapshot,
            )


@pytest.mark.parametrize(
    "case",
    (
        "step_timeout",
        "gate_binding",
        "contest_phase_steps",
        "owner_rule_pattern",
        "coherent_classifier_step13",
    ),
)
def test_source_authorization_kill_test_proves_identity_matched_forgery_penetrates(
    case,
    monkeypatch,
) -> None:
    """Removing only source authorization must make this canary fail loudly."""

    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    if case == "step_timeout":
        forged = _bundle_with_step_forgery(bundle, "timeout_seconds")
    elif case == "gate_binding":
        forged = _bundle_with_adjacent_behavior_forgery(bundle, "gate_binding")
    elif case == "contest_phase_steps":
        forged = _bundle_with_adjacent_behavior_forgery(
            bundle,
            "contest_phase_steps",
        )
    elif case == "owner_rule_pattern":
        forged = _bundle_with_adjacent_behavior_forgery(
            bundle,
            "owner_rule_pattern",
        )
    else:
        forged = _bundle_with_coherent_classifier_step13_forgery(bundle)
        snapshot = _step13_math_snapshot(bundle)

    forged_input, forged_decision = _test_only_identity_matched_input_and_decision(
        bundle,
        forged,
        snapshot,
    )
    assert forged_input.contract.semantic_sha256 == workflow_contract_sha256(forged)
    assert forged_input.contract.analysis_sha256 == workflow_contract_analysis_sha256(
        forged
    )

    monkeypatch.setattr(
        shadow_scheduler_module,
        "validate_workflow_contract_bundle",
        lambda supplied: supplied,
    )
    penetrated = SchedulerCore.plan(forged, forged_input)
    assert penetrated == forged_decision
    receipt = build_parity_receipt(
        forged,
        fixture_id=f"M02-SOURCE-AUTH-KILL-{case}",
        decision=forged_decision,
        v1=parity_value(forged_decision.plan),
        evidence_refs=("tests/test_m02_shadow_scheduler.py",),
    )
    assert receipt.status is ParityStatus.MATCH
    if case == "coherent_classifier_step13":
        assert forged_decision.plan.execution_route == (
            "stage-subtask:conditional_math_preflight_skip"
        )
    else:
        assert forged_decision.plan.action is TransitionAction.DISPATCH


def _bundle_with_runtime_shape_forgery(bundle, case):
    if case == "bundle_object":
        return object()
    if case == "bundle_subclass":
        return _subclass_clone(bundle)
    if case == "bundle_uninitialized":
        return object.__new__(type(bundle))
    if case == "stages_list":
        return replace(bundle, stages=list(bundle.stages))
    if case == "steps_list":
        return replace(bundle, steps=list(bundle.steps))
    if case == "gates_list":
        return replace(bundle, gates=list(bundle.gates))
    if case == "contest_phases_list":
        return replace(bundle, contest_phases=list(bundle.contest_phases))
    if case == "owner_rules_list":
        return replace(
            bundle,
            owner_compilation=replace(
                bundle.owner_compilation,
                rules=list(bundle.owner_compilation.rules),
            ),
        )
    if case == "stage_subtasks_list":
        stage = bundle.stages[0]
        return replace(
            bundle,
            stages=(replace(stage, subtasks=list(stage.subtasks)), *bundle.stages[1:]),
        )
    if case == "step_default_models_list":
        step = bundle.steps[0]
        return replace(
            bundle,
            steps=(
                replace(step, default_models=list(step.default_models)),
                *bundle.steps[1:],
            ),
        )
    if case == "condition_operands_list":
        gate = bundle.gates[0]
        return replace(
            bundle,
            gates=(
                replace(
                    gate,
                    condition=replace(
                        gate.condition,
                        operands=list(gate.condition.operands),
                    ),
                ),
                *bundle.gates[1:],
            ),
        )
    if case == "phase_steps_list":
        phase = bundle.contest_phases[0]
        return replace(
            bundle,
            contest_phases=(
                replace(phase, steps=list(phase.steps)),
                *bundle.contest_phases[1:],
            ),
        )
    if case in {"classifier_dirty_flags_list", "classifier_semantic_flags_list"}:
        field_name = (
            "dirty_flags"
            if case == "classifier_dirty_flags_list"
            else "semantic_dirty_flags"
        )
        return replace(
            bundle,
            classifier=replace(
                bundle.classifier,
                **{field_name: list(getattr(bundle.classifier, field_name))},
            ),
        )
    if case == "priority_authorizations_list":
        rule_index, rule = next(
            (index, item)
            for index, item in enumerate(bundle.owner_compilation.rules)
            if item.priority_authorizations
        )
        changed = replace(
            rule,
            priority_authorizations=list(rule.priority_authorizations),
        )
        rules = tuple(
            changed if index == rule_index else item
            for index, item in enumerate(bundle.owner_compilation.rules)
        )
        return replace(
            bundle,
            owner_compilation=replace(bundle.owner_compilation, rules=rules),
        )
    if case == "diagnostics_list":
        return replace(
            bundle,
            owner_compilation=replace(
                bundle.owner_compilation,
                diagnostics=list(bundle.owner_compilation.diagnostics),
            ),
        )
    if case == "diagnostic_rule_ids_list":
        diagnostic = bundle.owner_compilation.diagnostics[0]
        changed = replace(diagnostic, rule_ids=list(diagnostic.rule_ids))
        return replace(
            bundle,
            owner_compilation=replace(
                bundle.owner_compilation,
                diagnostics=(
                    changed,
                    *bundle.owner_compilation.diagnostics[1:],
                ),
            ),
        )
    if case == "stage_item_none":
        return replace(bundle, stages=(None, *bundle.stages[1:]))
    if case == "step_item_none":
        return replace(bundle, steps=(None, *bundle.steps[1:]))
    if case == "step_item_subclass":
        return replace(
            bundle,
            steps=(_subclass_clone(bundle.steps[0]), *bundle.steps[1:]),
        )
    if case == "step_item_uninitialized":
        return replace(
            bundle,
            steps=(object.__new__(type(bundle.steps[0])), *bundle.steps[1:]),
        )
    if case == "gate_item_none":
        return replace(bundle, gates=(None, *bundle.gates[1:]))
    if case == "phase_item_none":
        return replace(
            bundle,
            contest_phases=(None, *bundle.contest_phases[1:]),
        )
    if case == "budget_none":
        step = bundle.steps[0]
        return replace(
            bundle,
            steps=(replace(step, budget=None), *bundle.steps[1:]),
        )
    if case == "condition_none":
        gate = bundle.gates[0]
        return replace(
            bundle,
            gates=(replace(gate, condition=None), *bundle.gates[1:]),
        )
    if case == "classifier_none":
        return replace(bundle, classifier=None)
    if case == "owner_rule_none":
        return replace(
            bundle,
            owner_compilation=replace(
                bundle.owner_compilation,
                rules=(None, *bundle.owner_compilation.rules[1:]),
            ),
        )
    if case == "prompt_object":
        step = bundle.steps[0]
        return replace(
            bundle,
            steps=(replace(step, prompt=object()), *bundle.steps[1:]),
        )
    if case == "prompt_invalid_unicode":
        step = bundle.steps[0]
        return replace(
            bundle,
            steps=(replace(step, prompt="invalid:\ud800"), *bundle.steps[1:]),
        )
    if case == "producer_object":
        gate = bundle.gates[0]
        return replace(
            bundle,
            gates=(replace(gate, producer=object()), *bundle.gates[1:]),
        )
    if case == "owner_schema_object":
        return replace(
            bundle,
            owner_compilation=replace(
                bundle.owner_compilation,
                schema_version=object(),
            ),
        )
    if case in {"diagnostic_path_integer", "diagnostic_code_string"}:
        diagnostic = bundle.owner_compilation.diagnostics[0]
        changed = (
            replace(diagnostic, path=7)
            if case == "diagnostic_path_integer"
            else replace(diagnostic, code=diagnostic.code.value)
        )
        return replace(
            bundle,
            owner_compilation=replace(
                bundle.owner_compilation,
                diagnostics=(changed, *bundle.owner_compilation.diagnostics[1:]),
            ),
        )
    if case == "step_id_boolean":
        step = bundle.steps[0]
        return replace(
            bundle,
            steps=(replace(step, step_id=False), *bundle.steps[1:]),
        )
    if case == "gate_stage_id_boolean":
        gate = bundle.gates[0]
        return replace(
            bundle,
            gates=(replace(gate, stage_id=True), *bundle.gates[1:]),
        )
    if case == "phase_human_gate_integer":
        phase = bundle.contest_phases[0]
        return replace(
            bundle,
            contest_phases=(
                replace(phase, human_gate=3),
                *bundle.contest_phases[1:],
            ),
        )
    if case == "projects_pending_action_integer":
        gate = bundle.gates[0]
        return replace(
            bundle,
            gates=(
                replace(gate, projects_pending_action=1),
                *bundle.gates[1:],
            ),
        )
    raise AssertionError(f"unsupported runtime-shape forgery {case!r}")


RUNTIME_SHAPE_CASES = (
    ("bundle_object", "bundle has an unsupported runtime type"),
    ("bundle_subclass", "bundle has an unsupported runtime type"),
    ("bundle_uninitialized", "bundle.schema_version is missing"),
    ("stages_list", "bundle.stages must be an immutable tuple"),
    ("steps_list", "bundle.steps must be an immutable tuple"),
    ("gates_list", "bundle.gates must be an immutable tuple"),
    (
        "contest_phases_list",
        "bundle.contest_phases must be an immutable tuple",
    ),
    (
        "owner_rules_list",
        "bundle.owner_compilation.rules must be an immutable tuple",
    ),
    (
        "stage_subtasks_list",
        "bundle.stages[0].subtasks must be an immutable tuple",
    ),
    (
        "step_default_models_list",
        "bundle.steps[0].default_models must be an immutable tuple",
    ),
    (
        "condition_operands_list",
        "bundle.gates[0].condition.operands must be an immutable tuple",
    ),
    (
        "phase_steps_list",
        "bundle.contest_phases[0].steps must be an immutable tuple",
    ),
    (
        "classifier_dirty_flags_list",
        "bundle.classifier.dirty_flags must be an immutable tuple",
    ),
    (
        "classifier_semantic_flags_list",
        "bundle.classifier.semantic_dirty_flags must be an immutable tuple",
    ),
    (
        "priority_authorizations_list",
        "bundle.owner_compilation.rules[11].priority_authorizations must be an immutable tuple",
    ),
    (
        "diagnostics_list",
        "bundle.owner_compilation.diagnostics must be an immutable tuple",
    ),
    (
        "diagnostic_rule_ids_list",
        "bundle.owner_compilation.diagnostics[0].rule_ids must be an immutable tuple",
    ),
    ("stage_item_none", "bundle.stages[0] has an unsupported runtime type"),
    ("step_item_none", "bundle.steps[0] has an unsupported runtime type"),
    ("step_item_subclass", "bundle.steps[0] has an unsupported runtime type"),
    ("step_item_uninitialized", "bundle.steps[0].step_id is missing"),
    ("gate_item_none", "bundle.gates[0] has an unsupported runtime type"),
    (
        "phase_item_none",
        "bundle.contest_phases[0] has an unsupported runtime type",
    ),
    ("budget_none", "bundle.steps[0].budget has an unsupported runtime type"),
    (
        "condition_none",
        "bundle.gates[0].condition has an unsupported runtime type",
    ),
    ("classifier_none", "bundle.classifier has an unsupported runtime type"),
    (
        "owner_rule_none",
        "bundle.owner_compilation.rules[0] has an unsupported runtime type",
    ),
    ("prompt_object", "bundle.steps[0].prompt must be a string or None"),
    (
        "prompt_invalid_unicode",
        "bundle.steps[0].prompt must contain valid UTF-8 scalar values",
    ),
    ("producer_object", "bundle.gates[0].producer must be a string"),
    (
        "owner_schema_object",
        "bundle.owner_compilation.schema_version must be a string",
    ),
    (
        "diagnostic_path_integer",
        "bundle.owner_compilation.diagnostics[0].path must be a string or None",
    ),
    (
        "diagnostic_code_string",
        "bundle.owner_compilation.diagnostics[0].code has an unsupported runtime type",
    ),
    ("step_id_boolean", "bundle.steps[0].step_id must be an integer"),
    ("gate_stage_id_boolean", "bundle.gates[0].stage_id must be an integer or None"),
    (
        "phase_human_gate_integer",
        "bundle.contest_phases[0].human_gate must be a string or None",
    ),
    (
        "projects_pending_action_integer",
        "bundle.gates[0].projects_pending_action must be a boolean",
    ),
)


@pytest.mark.parametrize(("case", "error_message"), RUNTIME_SHAPE_CASES)
@pytest.mark.parametrize("entrypoint", ("validator", "adapter", "core", "receipt"))
def test_bundle_runtime_type_boundary_fails_closed_at_all_public_entrypoints(
    case,
    error_message,
    entrypoint,
) -> None:
    bundle = compile_workflow_contract_bundle()
    malformed = _bundle_with_runtime_shape_forgery(bundle, case)
    snapshot = _snapshot(bundle)
    valid_input, valid_decision = _adapt_plan(bundle, snapshot)

    with pytest.raises(
        WorkflowContractValidationError,
        match=f"^{re.escape(error_message)}$",
    ):
        if entrypoint == "validator":
            validate_workflow_contract_bundle(malformed)
        elif entrypoint == "adapter":
            StageV1ReadinessAdapter(malformed).adapt(snapshot)
        elif entrypoint == "core":
            SchedulerCore.plan(malformed, valid_input)
        else:
            build_parity_receipt(
                malformed,
                fixture_id=f"M02-RUNTIME-TYPE-{case}",
                decision=valid_decision,
                v1=parity_value(valid_decision.plan),
                evidence_refs=("tests/test_m02_shadow_scheduler.py",),
            )


def test_workflow_bundle_dto_graph_is_deeply_immutable() -> None:
    bundle = compile_workflow_contract_bundle()
    _assert_deeply_immutable(bundle)
    assert type(bundle.stages) is tuple
    assert all(type(stage.subtasks) is tuple for stage in bundle.stages)
    assert type(bundle.steps) is tuple
    assert all(type(step.default_models) is tuple for step in bundle.steps)
    assert type(bundle.gates) is tuple
    assert all(type(gate.condition.operands) is tuple for gate in bundle.gates)
    assert type(bundle.contest_phases) is tuple
    assert all(type(phase.steps) is tuple for phase in bundle.contest_phases)
    assert type(bundle.classifier.dirty_flags) is tuple
    assert type(bundle.classifier.semantic_dirty_flags) is tuple
    assert type(bundle.owner_compilation.rules) is tuple
    assert all(
        type(rule.priority_authorizations) is tuple
        for rule in bundle.owner_compilation.rules
    )
    assert type(bundle.owner_compilation.diagnostics) is tuple
    assert all(
        type(diagnostic.rule_ids) is tuple
        for diagnostic in bundle.owner_compilation.diagnostics
    )


@pytest.mark.parametrize(
    ("case", "error_message"),
    (
        ("mapping_subclass", "recorded_snapshot must be a plain mapping"),
        ("nested_mapping_subclass", "stage_cursor must be a plain mapping"),
        ("sequence_subclass", "checkpoint_heads must be a plain list or tuple"),
        ("string_subclass", "project_id must be a non-empty string"),
        ("integer_subclass", "project_revision must be an integer"),
        (
            "invalid_unicode",
            "project_id must contain valid UTF-8 scalar values",
        ),
    ),
)
def test_recorded_snapshot_public_boundary_requires_plain_runtime_types(
    case,
    error_message,
) -> None:
    class MappingSubclass(dict):
        pass

    class SequenceSubclass(list):
        pass

    class StringSubclass(str):
        pass

    class IntegerSubclass(int):
        pass

    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    supplied = snapshot
    if case == "mapping_subclass":
        supplied = MappingSubclass(snapshot)
    elif case == "nested_mapping_subclass":
        snapshot["stage_cursor"] = MappingSubclass(snapshot["stage_cursor"])
    elif case == "sequence_subclass":
        snapshot["checkpoint_heads"] = SequenceSubclass()
    elif case == "string_subclass":
        snapshot["project_id"] = StringSubclass(snapshot["project_id"])
    elif case == "integer_subclass":
        snapshot["project_revision"] = IntegerSubclass(snapshot["project_revision"])
    else:
        snapshot["project_id"] = "invalid:\ud800"

    with pytest.raises(SnapshotValidationError, match=f"^{re.escape(error_message)}$"):
        StageV1ReadinessAdapter(bundle).adapt(supplied)


@pytest.mark.parametrize(
    ("case", "error_message"),
    (
        (
            "string_subclass",
            "domain_readiness[0].action_hint must be a non-empty string",
        ),
        (
            "integer_subclass",
            "domain_readiness[0].action_hint must be a non-empty string",
        ),
        (
            "custom_object",
            "domain_readiness[0].action_hint must be a non-empty string",
        ),
        (
            "raising_string_object",
            "domain_readiness[0].action_hint must be a non-empty string",
        ),
        (
            "invalid_unicode",
            "domain_readiness[0].action_hint must contain valid UTF-8 scalar values",
        ),
        (
            "unknown_string",
            "domain_readiness[0].action_hint is unsupported",
        ),
    ),
)
def test_domain_action_hint_raw_boundary_is_exact_and_fail_closed(
    case,
    error_message,
) -> None:
    class StringSubclass(str):
        pass

    class IntegerSubclass(int):
        def __str__(self):
            return "WAIT"

    class CustomObject:
        def __str__(self):
            return "WAIT"

    class RaisingStringObject:
        def __str__(self):
            raise AttributeError("must not escape the snapshot boundary")

    supplied = {
        "string_subclass": StringSubclass("WAIT"),
        "integer_subclass": IntegerSubclass(1),
        "custom_object": CustomObject(),
        "raising_string_object": RaisingStringObject(),
        "invalid_unicode": "invalid:\ud800",
        "unknown_string": "UNKNOWN",
    }[case]
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot["domain_readiness"] = [
        {
            "domain": "packet",
            "state": "WAITING",
            "action_hint": supplied,
            "target": snapshot["stage_cursor"]["coordinate"],
            "reason_code": "PACKET_WAITING",
            "evidence_ref": "fixture:packet",
        }
    ]

    with pytest.raises(SnapshotValidationError, match=f"^{re.escape(error_message)}$"):
        StageV1ReadinessAdapter(bundle).adapt(snapshot)


def test_domain_action_hint_legal_enum_string_preserves_result() -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot["domain_readiness"] = [
        {
            "domain": "packet",
            "state": "WAITING",
            "action_hint": "WAIT",
            "target": snapshot["stage_cursor"]["coordinate"],
            "reason_code": "PACKET_WAITING",
            "evidence_ref": "fixture:packet",
        }
    ]

    readiness = StageV1ReadinessAdapter(bundle).adapt(snapshot)
    assert readiness.domain_readiness[0].action_hint is TransitionAction.WAIT


@pytest.mark.parametrize(
    ("case", "error_message"),
    (
        ("dto_subclass", "readiness_input has an unsupported runtime type"),
        ("uninitialized_dto", "readiness_input.schema_version is missing"),
        ("tuple_subclass", "semantic_dirty_flags must be an immutable tuple"),
        ("string_subclass", "project_id must be a non-empty string"),
        ("integer_subclass", "project_revision must be an integer"),
        (
            "nested_dto_subclass",
            "stage_cursor has an unsupported runtime type",
        ),
        (
            "contract_string_subclass",
            "contract.semantic_sha256 must be exactly 64 lowercase hexadecimal characters",
        ),
        (
            "catalog_item_subclass",
            "catalog[0] has an unsupported runtime type",
        ),
        (
            "invalid_unicode",
            "project_id must contain valid UTF-8 scalar values",
        ),
    ),
)
def test_scheduler_core_public_dto_boundary_is_exact_and_fail_closed(
    case,
    error_message,
) -> None:
    class TupleSubclass(tuple):
        pass

    class StringSubclass(str):
        pass

    class IntegerSubclass(int):
        pass

    bundle = compile_workflow_contract_bundle()
    readiness = StageV1ReadinessAdapter(bundle).adapt(_snapshot(bundle))
    if case == "dto_subclass":
        supplied = _subclass_clone(readiness)
    elif case == "uninitialized_dto":
        supplied = object.__new__(type(readiness))
    elif case == "tuple_subclass":
        supplied = replace(
            readiness,
            semantic_dirty_flags=TupleSubclass(readiness.semantic_dirty_flags),
        )
    elif case == "string_subclass":
        supplied = replace(readiness, project_id=StringSubclass(readiness.project_id))
    elif case == "integer_subclass":
        supplied = replace(
            readiness,
            project_revision=IntegerSubclass(readiness.project_revision),
        )
    elif case == "nested_dto_subclass":
        supplied = replace(readiness, stage_cursor=_subclass_clone(readiness.stage_cursor))
    elif case == "contract_string_subclass":
        supplied = replace(
            readiness,
            contract=replace(
                readiness.contract,
                semantic_sha256=StringSubclass(readiness.contract.semantic_sha256),
            ),
        )
    elif case == "catalog_item_subclass":
        supplied = replace(
            readiness,
            catalog=(_subclass_clone(readiness.catalog[0]), *readiness.catalog[1:]),
        )
    else:
        supplied = replace(readiness, project_id="invalid:\ud800")

    with pytest.raises(SnapshotValidationError, match=f"^{re.escape(error_message)}$"):
        SchedulerCore.plan(bundle, supplied)


@pytest.mark.parametrize(
    ("case", "error_message"),
    (
        ("decision_subclass", "decision has an unsupported runtime type"),
        ("v1_subclass", "v1 has an unsupported runtime type"),
        ("evidence_sequence_subclass", "evidence_refs must be a plain list or tuple"),
        ("evidence_string_subclass", "evidence_refs[0] must be a non-empty string"),
        (
            "fixture_invalid_unicode",
            "fixture_id must contain valid UTF-8 scalar values",
        ),
    ),
)
def test_parity_receipt_public_dto_boundary_is_exact_and_fail_closed(
    case,
    error_message,
) -> None:
    class SequenceSubclass(tuple):
        pass

    class StringSubclass(str):
        pass

    bundle = compile_workflow_contract_bundle()
    readiness = StageV1ReadinessAdapter(bundle).adapt(_snapshot(bundle))
    decision = SchedulerCore.plan(bundle, readiness)
    fixture_id = "M02-PUBLIC-DTO"
    supplied_decision = decision
    v1 = parity_value(decision.plan)
    evidence_refs = ("tests/test_m02_shadow_scheduler.py",)
    if case == "decision_subclass":
        supplied_decision = _subclass_clone(decision)
    elif case == "v1_subclass":
        v1 = _subclass_clone(v1)
    elif case == "evidence_sequence_subclass":
        evidence_refs = SequenceSubclass(evidence_refs)
    elif case == "evidence_string_subclass":
        evidence_refs = (StringSubclass(evidence_refs[0]),)
    else:
        fixture_id = "invalid:\ud800"

    with pytest.raises(SnapshotValidationError, match=f"^{re.escape(error_message)}$"):
        build_parity_receipt(
            bundle,
            fixture_id=fixture_id,
            decision=supplied_decision,
            v1=v1,
            evidence_refs=evidence_refs,
        )


def test_parity_receipt_serializers_reject_invalid_unicode_before_canonicalization() -> None:
    bundle = compile_workflow_contract_bundle()
    readiness = StageV1ReadinessAdapter(bundle).adapt(_snapshot(bundle))
    decision = SchedulerCore.plan(bundle, readiness)
    receipt = build_parity_receipt(
        bundle,
        fixture_id="M02-SERIALIZER-BOUNDARY",
        decision=decision,
        v1=parity_value(decision.plan),
        evidence_refs=("tests/test_m02_shadow_scheduler.py",),
    )
    forged = replace(receipt, fixture_id="invalid:\ud800")

    for serializer in (parity_receipt_bytes, parity_receipt_sha256):
        with pytest.raises(
            SnapshotValidationError,
            match=(
                "^parity_receipt.fixture_id must contain valid UTF-8 scalar values$"
            ),
        ):
            serializer(forged)


def _forged_enum_member(enum_type, *, initialized: bool):
    forged = str.__new__(enum_type, "PWN")
    if initialized:
        object.__setattr__(forged, "_value_", "PWN")
        object.__setattr__(forged, "_name_", "PWN")
    assert type(forged) is enum_type
    assert not any(forged is member for member in enum_type)
    return forged


@pytest.mark.parametrize("value_kind", ("readiness_input", "readiness_result", "plan"))
@pytest.mark.parametrize(
    "case",
    (
        "uninitialized_dto",
        "dto_subclass",
        "tuple_subclass",
        "analysis_object",
        "analysis_invalid_unicode",
    ),
)
def test_public_serializers_validate_complete_runtime_structure_before_projection(
    value_kind,
    case,
) -> None:
    class TupleSubclass(tuple):
        pass

    bundle = compile_workflow_contract_bundle()
    readiness, decision = _adapt_plan(bundle, _snapshot(bundle))
    if value_kind == "readiness_input":
        baseline = readiness
        serializers = (
            readiness_input_semantic_bytes,
            readiness_input_semantic_sha256,
            readiness_input_analysis_bytes,
            readiness_input_analysis_sha256,
        )
        if case == "tuple_subclass":
            supplied = replace(
                baseline,
                semantic_dirty_flags=TupleSubclass(baseline.semantic_dirty_flags),
            )
        elif case in {"analysis_object", "analysis_invalid_unicode"}:
            analysis = object() if case == "analysis_object" else "invalid:\ud800"
            supplied = replace(
                baseline,
                contract=replace(baseline.contract, analysis_sha256=analysis),
            )
        else:
            supplied = baseline
    elif value_kind == "readiness_result":
        baseline = decision.readiness
        serializers = (
            readiness_result_semantic_bytes,
            readiness_result_semantic_sha256,
            readiness_result_analysis_bytes,
            readiness_result_analysis_sha256,
        )
        if case == "tuple_subclass":
            supplied = replace(
                baseline,
                reason_codes=TupleSubclass(baseline.reason_codes),
            )
        elif case in {"analysis_object", "analysis_invalid_unicode"}:
            analysis = object() if case == "analysis_object" else "invalid:\ud800"
            supplied = replace(baseline, input_analysis_sha256=analysis)
        else:
            supplied = baseline
    else:
        baseline = decision.plan
        serializers = (
            transition_plan_semantic_bytes,
            transition_plan_semantic_sha256,
            transition_plan_analysis_bytes,
            transition_plan_analysis_sha256,
        )
        if case == "tuple_subclass":
            supplied = replace(
                baseline,
                proposed_mutations=TupleSubclass(baseline.proposed_mutations),
            )
        elif case in {"analysis_object", "analysis_invalid_unicode"}:
            analysis = object() if case == "analysis_object" else "invalid:\ud800"
            supplied = replace(baseline, readiness_analysis_sha256=analysis)
        else:
            supplied = baseline

    if case == "uninitialized_dto":
        supplied = object.__new__(type(baseline))
    elif case == "dto_subclass":
        supplied = _subclass_clone(baseline)

    for serializer in serializers:
        with pytest.raises(SnapshotValidationError):
            serializer(supplied)


@pytest.mark.parametrize("initialized", (True, False))
def test_registered_enum_identity_is_required_before_all_public_uses(initialized) -> None:
    bundle = compile_workflow_contract_bundle()
    readiness, decision = _adapt_plan(bundle, _snapshot(bundle))
    forged_action = _forged_enum_member(TransitionAction, initialized=initialized)
    forged_state = _forged_enum_member(ReadinessState, initialized=initialized)
    forged_status = _forged_enum_member(ParityStatus, initialized=initialized)

    raw_snapshot = _snapshot(bundle)
    raw_snapshot["domain_readiness"] = [
        {
            "domain": "packet",
            "state": "WAITING",
            "action_hint": forged_action,
            "target": raw_snapshot["stage_cursor"]["coordinate"],
            "reason_code": "PACKET_WAITING",
            "evidence_ref": "fixture:packet",
        }
    ]
    with pytest.raises(SnapshotValidationError):
        StageV1ReadinessAdapter(bundle).adapt(raw_snapshot)

    forged_fact = replace(
        readiness.domain_readiness[0]
        if readiness.domain_readiness
        else shadow_scheduler_module.DomainReadinessFact(
            domain="packet",
            state="WAITING",
            action_hint=TransitionAction.WAIT,
            target=readiness.stage_cursor.coordinate,
            reason_code="PACKET_WAITING",
            evidence_ref="fixture:packet",
        ),
        action_hint=forged_action,
    )
    with pytest.raises(SnapshotValidationError):
        SchedulerCore.plan(bundle, replace(readiness, domain_readiness=(forged_fact,)))

    forged_readiness = replace(decision.readiness, state=forged_state)
    with pytest.raises(SnapshotValidationError):
        build_parity_receipt(
            bundle,
            fixture_id="M02-FORGED-READINESS-ENUM",
            decision=replace(decision, readiness=forged_readiness),
            v1=parity_value(decision.plan),
            evidence_refs=("tests/test_m02_shadow_scheduler.py",),
        )

    forged_plan = replace(decision.plan, action=forged_action)
    for projection in (
        parity_value,
        transition_plan_semantic_bytes,
        transition_plan_semantic_sha256,
        transition_plan_analysis_bytes,
        transition_plan_analysis_sha256,
    ):
        with pytest.raises(SnapshotValidationError):
            projection(forged_plan)
    with pytest.raises(SnapshotValidationError):
        build_parity_receipt(
            bundle,
            fixture_id="M02-FORGED-PLAN-ENUM",
            decision=replace(decision, plan=forged_plan),
            v1=parity_value(decision.plan),
            evidence_refs=("tests/test_m02_shadow_scheduler.py",),
        )

    receipt = build_parity_receipt(
        bundle,
        fixture_id="M02-FORGED-STATUS-ENUM",
        decision=decision,
        v1=parity_value(decision.plan),
        evidence_refs=("tests/test_m02_shadow_scheduler.py",),
    )
    for serializer in (parity_receipt_bytes, parity_receipt_sha256):
        with pytest.raises(SnapshotValidationError):
            serializer(replace(receipt, status=forged_status))


@pytest.mark.parametrize("initialized", (True, False))
@pytest.mark.parametrize("entrypoint", ("validator", "adapter", "core", "receipt"))
def test_owner_diagnostic_registered_enum_identity_fails_at_public_bundle_boundary(
    initialized,
    entrypoint,
) -> None:
    bundle = compile_workflow_contract_bundle()
    diagnostic = bundle.owner_compilation.diagnostics[0]
    forged = replace(
        bundle,
        owner_compilation=replace(
            bundle.owner_compilation,
            diagnostics=(
                replace(
                    diagnostic,
                    code=_forged_enum_member(
                        OwnerDiagnosticCode,
                        initialized=initialized,
                    ),
                ),
                *bundle.owner_compilation.diagnostics[1:],
            ),
        ),
    )

    with pytest.raises(WorkflowContractValidationError, match="registered enum member"):
        _invoke_bundle_boundary(entrypoint, bundle, forged)


@pytest.mark.parametrize(
    "supplied",
    (
        object(),
        "invalid:\ud800",
        "V2-ERROR-WITH-DECISION",
    ),
)
def test_v2_error_code_cannot_be_ignored_when_decision_exists(supplied) -> None:
    bundle = compile_workflow_contract_bundle()
    _, decision = _adapt_plan(bundle, _snapshot(bundle))
    with pytest.raises(SnapshotValidationError):
        build_parity_receipt(
            bundle,
            fixture_id="M02-V2-ERROR-CONFLICT",
            decision=decision,
            v1=parity_value(decision.plan),
            evidence_refs=("tests/test_m02_shadow_scheduler.py",),
            v2_error_code=supplied,
        )


def test_v2_error_code_exact_text_boundary_and_valid_error_receipt() -> None:
    class StringSubclass(str):
        pass

    bundle = compile_workflow_contract_bundle()
    for supplied in (StringSubclass("V2-ERROR"), object(), "invalid:\ud800"):
        with pytest.raises(SnapshotValidationError):
            build_parity_receipt(
                bundle,
                fixture_id="M02-V2-ERROR-TYPE",
                decision=None,
                v1=None,
                evidence_refs=("tests/test_m02_shadow_scheduler.py",),
                v2_error_code=supplied,
            )

    receipt = build_parity_receipt(
        bundle,
        fixture_id="M02-V2-ERROR-VALID",
        decision=None,
        v1=None,
        evidence_refs=("tests/test_m02_shadow_scheduler.py",),
        v2_error_code="V2-ERROR",
    )
    assert receipt.status is ParityStatus.V2_ERROR
    assert receipt.error_code == "V2-ERROR"


def _bundle_with_adjacent_behavior_forgery(bundle, case):
    if case == "gate_condition":
        gate = bundle.gates[0]
        changed = replace(
            gate,
            condition=replace(gate.condition, operands=("forged-condition",)),
        )
        return replace(bundle, gates=(changed, *bundle.gates[1:]))
    if case == "gate_binding":
        gate = bundle.gates[0]
        return replace(
            bundle,
            gates=(replace(gate, binding="forged-binding"), *bundle.gates[1:]),
        )
    if case == "contest_phase_steps":
        phase = bundle.contest_phases[0]
        return replace(
            bundle,
            contest_phases=(
                replace(phase, steps=phase.steps[:1]),
                *bundle.contest_phases[1:],
            ),
        )
    if case == "owner_rule_pattern":
        rule = bundle.owner_compilation.rules[0]
        return replace(
            bundle,
            owner_compilation=replace(
                bundle.owner_compilation,
                rules=(
                    replace(rule, pattern="forged/problem/**"),
                    *bundle.owner_compilation.rules[1:],
                ),
            ),
        )
    raise AssertionError(f"unsupported adjacent behavior forgery {case!r}")


@pytest.mark.parametrize(
    ("case", "error_match"),
    (
        ("gate_condition", "Gate behavior drift at index 0 field condition.operands"),
        ("gate_binding", "Gate behavior drift at index 0 field binding"),
        (
            "contest_phase_steps",
            "ContestPhase mapping must contain Step 1 exactly once",
        ),
        ("owner_rule_pattern", "owner behavior rule drift at index 0 field pattern"),
    ),
)
@pytest.mark.parametrize("entrypoint", ("validator", "adapter", "core", "receipt"))
def test_adjacent_behavior_trust_root_matrix_fails_closed(
    case,
    error_match,
    entrypoint,
) -> None:
    bundle = compile_workflow_contract_bundle()
    forged = _bundle_with_adjacent_behavior_forgery(bundle, case)

    with pytest.raises(WorkflowContractValidationError, match=error_match):
        _invoke_bundle_boundary(entrypoint, bundle, forged)


@pytest.mark.parametrize(
    ("case", "error_match"),
    (
        ("unknown-source-step", "does not resolve to a unique Step"),
        ("source-checkpoint-mismatch", "source/checkpoint mapping drift"),
        ("duplicate-schedule-coordinate", "ScheduleCoordinate values must be unique"),
        ("duplicate-subtask-key", "keys must be unique within each Stage"),
        ("duplicate-subtask-id", "IDs must be globally unique"),
    ),
)
@pytest.mark.parametrize("entrypoint", ("adapter", "core", "receipt"))
def test_stage_catalog_forgeries_fail_closed_at_every_public_entrypoint(
    case,
    error_match,
    entrypoint,
) -> None:
    bundle = compile_workflow_contract_bundle()
    valid_input, valid_decision = _adapt_plan(bundle, _snapshot(bundle))
    forged = _bundle_with_stage_catalog_forgery(bundle, case)

    with pytest.raises(CONTRACT_BOUNDARY_ERRORS, match=error_match):
        if entrypoint == "adapter":
            StageV1ReadinessAdapter(forged).adapt(_snapshot(bundle))
        elif entrypoint == "core":
            SchedulerCore.plan(forged, valid_input)
        else:
            build_parity_receipt(
                forged,
                fixture_id=f"M02-STAGE-CATALOG-{case}",
                decision=valid_decision,
                v1=ParityValue(
                    valid_decision.plan.action,
                    valid_decision.plan.target,
                    valid_decision.plan.execution_route,
                ),
                evidence_refs=("tests/test_m02_shadow_scheduler.py",),
            )


def test_shadow_boundary_pins_the_fixed_step13_condition_contract() -> None:
    bundle = compile_workflow_contract_bundle()
    valid_input, valid_decision = _adapt_plan(bundle, _snapshot(bundle))
    semantic_flags = bundle.classifier.semantic_dirty_flags
    invalid_bundles = (
        _bundle_with_subtask_condition(
            bundle,
            "conditional_math_preflight",
            operator="ANY",
            operands=("FORMAT_DIRTY",),
        ),
        _bundle_with_subtask_condition(
            bundle,
            "conditional_math_preflight",
            operator="UNKNOWN",
            operands=semantic_flags,
        ),
        _bundle_with_subtask_condition(
            bundle,
            "problem_setup",
            operator="ANY",
            operands=semantic_flags,
        ),
        _bundle_with_subtask_condition(
            bundle,
            "conditional_math_preflight",
            operator="ALWAYS",
            operands=(),
        ),
    )

    for invalid in invalid_bundles:
        with pytest.raises(CONTRACT_BOUNDARY_ERRORS):
            StageV1ReadinessAdapter(invalid)
        with pytest.raises(CONTRACT_BOUNDARY_ERRORS):
            SchedulerCore.plan(invalid, valid_input)
        with pytest.raises(CONTRACT_BOUNDARY_ERRORS):
            build_parity_receipt(
                invalid,
                fixture_id="M02-INVALID-CONDITION",
                decision=valid_decision,
                v1=ParityValue(
                    valid_decision.plan.action,
                    valid_decision.plan.target,
                    valid_decision.plan.execution_route,
                ),
                evidence_refs=("tests/test_m02_shadow_scheduler.py",),
            )


@pytest.mark.parametrize(
    ("operands", "error_match"),
    (
        (("MODEL_DIRTY", "MODEL_DIRTY"), "operands contain duplicates"),
        (("UNKNOWN_DIRTY",), "unknown semantic operand"),
    ),
)
def test_condition_operand_rejection_branches_cover_duplicate_and_unknown_values(
    operands,
    error_match,
) -> None:
    bundle = compile_workflow_contract_bundle()
    valid_input, valid_decision = _adapt_plan(bundle, _snapshot(bundle))
    invalid = _bundle_with_subtask_condition(
        bundle,
        "conditional_math_preflight",
        operator="ANY",
        operands=operands,
    )

    for entrypoint in ("adapter", "core", "receipt"):
        with pytest.raises(CONTRACT_BOUNDARY_ERRORS, match=error_match):
            if entrypoint == "adapter":
                StageV1ReadinessAdapter(invalid).adapt(_snapshot(bundle))
            elif entrypoint == "core":
                SchedulerCore.plan(invalid, valid_input)
            else:
                build_parity_receipt(
                    invalid,
                    fixture_id=f"M02-CONDITION-{error_match}",
                    decision=valid_decision,
                    v1=ParityValue(
                        valid_decision.plan.action,
                        valid_decision.plan.target,
                        valid_decision.plan.execution_route,
                    ),
                    evidence_refs=("tests/test_m02_shadow_scheduler.py",),
                )


def test_normal_progression_and_current_checkpoint_heads() -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot["stage_cursor"] = None
    snapshot["checkpoint_heads"] = [_checkpoint(bundle, "problem_setup", 40)]
    snapshot["last_completed_step"] = 0

    readiness_input, decision = _adapt_plan(bundle, snapshot)

    assert decision.readiness.state is ReadinessState.READY
    assert decision.plan.action is TransitionAction.DISPATCH
    assert decision.plan.target == ScheduleCoordinate(1, "research_and_viability", 1)
    assert decision.plan.execution_route == "step:1"
    assert decision.plan.proposed_mutations == ()
    assert decision.plan.performed_side_effects == ()
    assert decision.plan.authoritative is False
    assert tuple(item.coordinate for item in readiness_input.checkpoint_heads) == (
        ScheduleCoordinate(1, "problem_setup", 0),
    )


@pytest.mark.parametrize(
    ("semantic_dirty", "expected_route"),
    (
        (False, "stage-subtask:conditional_math_preflight_skip"),
        (True, "step:13"),
    ),
)
def test_conditional_step13_parity(semantic_dirty: bool, expected_route: str) -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot["stage_cursor"] = None
    snapshot["checkpoint_heads"] = _checkpoints_before(
        bundle, "conditional_math_preflight"
    )
    snapshot["last_completed_step"] = 12
    snapshot["last_completed_stage"] = 7
    if semantic_dirty:
        snapshot["dirty_refs"] = [
            {
                "flag": "MATH_DIRTY",
                "owner_stage": 8,
                "cause_ref": "artifact:paper/model.tex",
            }
        ]

    _, decision = _adapt_plan(bundle, snapshot)
    receipt = build_parity_receipt(
        bundle,
        fixture_id=f"M02-CONDITIONAL-{semantic_dirty}",
        decision=decision,
        v1=ParityValue(
            TransitionAction.DISPATCH,
            decision.plan.target,
            expected_route,
        ),
        evidence_refs=("factory_core/engine.py:687-695",),
    )

    assert decision.plan.target == ScheduleCoordinate(8, "conditional_math_preflight", 13)
    assert decision.plan.execution_route == expected_route
    assert receipt.status is ParityStatus.MATCH


def test_human_gate_pending_parity() -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    coordinate = _coordinate(bundle, "content_freeze_guard")
    snapshot["stage_cursor"] = {
        "coordinate": coordinate,
        "active_step": 16,
        "attempt": 0,
    }
    snapshot["pending_action_refs"] = [
        {
            "action_type": "human_approval",
            "gate": "content_freeze",
            "request_ref": "decision-request:content-freeze:2",
            "generation": 2,
            "coordinate": coordinate,
        }
    ]

    _, decision = _adapt_plan(bundle, snapshot)
    receipt = build_parity_receipt(
        bundle,
        fixture_id="M02-HUMAN-GATE-PENDING",
        decision=decision,
        v1=ParityValue(
            TransitionAction.WAIT,
            ScheduleCoordinate(10, "content_freeze_guard", 16),
        ),
        evidence_refs=("factory_core/engine.py:148-156",),
    )

    assert decision.readiness.state is ReadinessState.WAITING
    assert receipt.status is ParityStatus.MATCH


@pytest.mark.parametrize("invocation_type", ("recovery", "solver"))
def test_recovery_and_solver_current_wait_parity(invocation_type: str) -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    coordinate = _coordinate(bundle, "solve")
    snapshot["stage_cursor"] = {
        "coordinate": coordinate,
        "active_step": 5,
        "attempt": 0,
    }
    snapshot["active_invocation_refs"] = [
        {
            "invocation_id": f"{invocation_type}:job-1",
            "invocation_type": invocation_type,
            "state": "recovering" if invocation_type == "recovery" else "running",
            "coordinate": coordinate,
        }
    ]

    _, decision = _adapt_plan(bundle, snapshot)
    receipt = build_parity_receipt(
        bundle,
        fixture_id=f"M02-{invocation_type.upper()}-CURRENT",
        decision=decision,
        v1=ParityValue(TransitionAction.WAIT, ScheduleCoordinate(4, "solve", 5)),
        evidence_refs=("recorded-active-invocation",),
    )

    assert decision.plan.action is TransitionAction.WAIT
    assert receipt.status is ParityStatus.MATCH


@pytest.mark.parametrize(
    ("reason", "delivery_allowed"),
    (("PROJECT_COMPLETED", True), ("TECHNICAL_TERMINAL_NO_DELIVERY", False)),
)
def test_terminal_and_technical_nondelivery_parity(reason: str, delivery_allowed: bool) -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot["stage_cursor"] = None
    snapshot["workflow_status"] = "completed" if delivery_allowed else "failed"
    snapshot["terminal_delivery"] = {
        "is_terminal": True,
        "terminal_reason": reason,
        "delivery_capability": (
            "contest-delivery-eligible" if delivery_allowed else "technical-no-delivery"
        ),
        "delivery_allowed": delivery_allowed,
    }
    if delivery_allowed:
        snapshot["checkpoint_heads"] = _all_checkpoints(bundle)
        snapshot["last_completed_step"] = 16
        snapshot["last_completed_stage"] = 10

    _, decision = _adapt_plan(bundle, snapshot)
    receipt = build_parity_receipt(
        bundle,
        fixture_id=f"M02-{reason}",
        decision=decision,
        v1=ParityValue(TransitionAction.TERMINATE, None),
        evidence_refs=("recorded-terminal-delivery-facts",),
    )

    assert decision.readiness.state is ReadinessState.TERMINAL
    assert decision.plan.action is TransitionAction.TERMINATE
    assert decision.readiness.contract.semantic_sha256 == decision.plan.contract.semantic_sha256
    assert decision.readiness.contract.analysis_sha256 == decision.plan.contract.analysis_sha256
    assert receipt.status is ParityStatus.MATCH


def _semantic_reopen_decision(bundle):
    snapshot = _snapshot(bundle)
    coordinate = _coordinate(bundle, "revision")
    snapshot["stage_cursor"] = {
        "coordinate": coordinate,
        "active_step": 12,
        "attempt": 0,
    }
    snapshot["dirty_refs"] = [
        {"flag": "MODEL_DIRTY", "owner_stage": 1, "cause_ref": "artifact:model"},
        {"flag": "RESULT_DIRTY", "owner_stage": 4, "cause_ref": "artifact:result"},
    ]
    snapshot["domain_readiness"] = [
        {
            "domain": "semantic-reopen",
            "state": "READY",
            "action_hint": "REOPEN",
            "target": _coordinate(bundle, "problem_setup"),
            "reason_code": "EARLIEST_DIRTY_OWNER_STAGE_1",
            "evidence_ref": "V1-SEMANTIC-REOPEN-001",
        }
    ]
    return _adapt_plan(bundle, snapshot)[1]


def _normal_terminal_decision(bundle):
    snapshot = _snapshot(bundle)
    snapshot["stage_cursor"] = None
    snapshot["workflow_status"] = "completed"
    snapshot["last_completed_step"] = 16
    snapshot["last_completed_stage"] = 10
    snapshot["checkpoint_heads"] = _all_checkpoints(bundle)
    snapshot["terminal_delivery"] = {
        "is_terminal": True,
        "terminal_reason": "PROJECT_COMPLETED",
        "delivery_capability": "contest-delivery-eligible",
        "delivery_allowed": True,
    }
    return _adapt_plan(bundle, snapshot)[1]


def test_characterization_matrix_has_only_exact_matches_or_explicit_unbound_gaps() -> None:
    bundle = compile_workflow_contract_bundle()
    matrix = json.loads(PARITY_MATRIX.read_text(encoding="utf-8"))
    characterization = json.loads(CHARACTERIZATION_INDEX.read_text(encoding="utf-8"))
    entries = matrix["existing_characterization"]
    assert {item["fixture_id"] for item in entries} == {
        item["fixture_id"] for item in characterization["entries"]
    }

    decisions = {
        "V1-NORMAL-001": _normal_terminal_decision(bundle),
        "V1-SEMANTIC-REOPEN-001": _semantic_reopen_decision(bundle),
    }
    receipts = []
    for item in entries:
        coordinate_raw = item.get("v1_coordinate")
        v1 = None
        if "v1_action" in item:
            coordinate = (
                None
                if coordinate_raw is None
                else ScheduleCoordinate(**coordinate_raw)
            )
            v1 = ParityValue(
                TransitionAction(item["v1_action"]),
                coordinate,
                item.get("v1_execution_route"),
            )
        receipt = build_parity_receipt(
            bundle,
            fixture_id=item["fixture_id"],
            decision=decisions.get(item["fixture_id"]),
            v1=v1,
            evidence_refs=item["evidence_refs"],
            gap_ids=item["gap_ids"],
        )
        receipts.append(receipt)
        assert receipt.status.value == item["expected_status"]

        if item["binding"] == "BOUND":
            assert receipt.status in {
                ParityStatus.MATCH,
                ParityStatus.EXPECTED_CORRECTION,
            }
        else:
            assert receipt.status is ParityStatus.V1_UNREPRESENTABLE
            case_path = ROOT / item["evidence_refs"][0]
            case = json.loads(case_path.read_text(encoding="utf-8"))
            source_gaps = {gap["gap_id"] for gap in case["missing_evidence"]}
            assert set(receipt.gap_ids) <= source_gaps

    assert not {
        ParityStatus.UNEXPLAINED_DIFFERENCE,
        ParityStatus.V2_ERROR,
    } & {receipt.status for receipt in receipts}


def test_expected_correction_is_exact_and_unknown_difference_fails_closed() -> None:
    bundle = compile_workflow_contract_bundle()
    _, decision = _adapt_plan(bundle, _snapshot(bundle))
    v1 = ParityValue(TransitionAction.WAIT, decision.plan.target)
    v2 = ParityValue(
        TransitionAction.DISPATCH,
        decision.plan.target,
        decision.plan.execution_route,
    )
    correction = ExpectedCorrection(
        issue_id="RUN4-M02-EXAMPLE-001",
        fixture_id="M02-EXACT-CORRECTION",
        expected_v1=v1,
        expected_v2=v2,
        evidence_refs=("issue:RUN4-M02-EXAMPLE-001",),
    )

    receipt = build_parity_receipt(
        bundle,
        fixture_id="M02-EXACT-CORRECTION",
        decision=decision,
        v1=v1,
        expected_correction=correction,
        evidence_refs=("tests/test_m02_shadow_scheduler.py",),
    )
    unknown = build_parity_receipt(
        bundle,
        fixture_id="M02-UNKNOWN",
        decision=decision,
        v1=v1,
        evidence_refs=("tests/test_m02_shadow_scheduler.py",),
    )

    assert receipt.status is ParityStatus.EXPECTED_CORRECTION
    assert receipt.issue_id == "RUN4-M02-EXAMPLE-001"
    assert unknown.status is ParityStatus.UNEXPLAINED_DIFFERENCE
    with pytest.raises(ValueError, match="exactly bind"):
        build_parity_receipt(
            bundle,
            fixture_id="M02-DIFFERENT-FIXTURE",
            decision=decision,
            v1=v1,
            expected_correction=correction,
            evidence_refs=("tests/test_m02_shadow_scheduler.py",),
        )


def test_route_only_parity_difference_is_unexplained_and_old_correction_is_rejected() -> None:
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    coordinate = _coordinate(bundle, "conditional_math_preflight")
    snapshot["stage_cursor"] = {
        "coordinate": coordinate,
        "active_step": 13,
        "attempt": 0,
    }
    snapshot["checkpoint_heads"] = _checkpoints_before(
        bundle, "conditional_math_preflight"
    )
    _, decision = _adapt_plan(bundle, snapshot)
    v1 = ParityValue(
        TransitionAction.DISPATCH,
        decision.plan.target,
        "step:13",
    )

    receipt = build_parity_receipt(
        bundle,
        fixture_id="M02-ROUTE-ONLY-DIFFERENCE",
        decision=decision,
        v1=v1,
        evidence_refs=("tests/test_m02_shadow_scheduler.py",),
    )
    assert decision.plan.execution_route == (
        "stage-subtask:conditional_math_preflight_skip"
    )
    assert receipt.status is ParityStatus.UNEXPLAINED_DIFFERENCE

    old_correction = ExpectedCorrection(
        issue_id="RUN4-M02-OLD-ROUTE-CORRECTION",
        fixture_id="M02-ROUTE-ONLY-DIFFERENCE",
        expected_v1=v1,
        expected_v2=ParityValue(
            TransitionAction.DISPATCH,
            decision.plan.target,
        ),
        evidence_refs=("issue:RUN4-M02-OLD-ROUTE-CORRECTION",),
    )
    with pytest.raises(ValueError, match="exactly bind"):
        build_parity_receipt(
            bundle,
            fixture_id="M02-ROUTE-ONLY-DIFFERENCE",
            decision=decision,
            v1=v1,
            expected_correction=old_correction,
            evidence_refs=("tests/test_m02_shadow_scheduler.py",),
        )


def test_parity_receipt_rejects_forged_shadow_decisions_before_match() -> None:
    bundle = compile_workflow_contract_bundle()
    _, decision = _adapt_plan(bundle, _snapshot(bundle))
    matching_v1 = ParityValue(
        decision.plan.action,
        decision.plan.target,
        decision.plan.execution_route,
    )
    forged_decisions = (
        replace(
            decision,
            readiness=replace(
                decision.readiness,
                input_semantic_sha256="0" * 64,
            ),
        ),
        replace(
            decision,
            plan=replace(
                decision.plan,
                readiness_semantic_sha256="0" * 64,
            ),
        ),
        replace(decision, plan=replace(decision.plan, authoritative=True)),
        replace(
            decision,
            plan=replace(decision.plan, proposed_mutations=("write:checkpoint",)),
        ),
        replace(
            decision,
            plan=replace(decision.plan, performed_side_effects=("dispatch:solver",)),
        ),
        replace(
            decision,
            readiness_input=replace(
                decision.readiness_input,
                schema_version="stage-v1-readiness-input-forged",
            ),
        ),
    )

    for forged in forged_decisions:
        with pytest.raises(SnapshotValidationError):
            build_parity_receipt(
                bundle,
                fixture_id="M02-FORGED-DECISION",
                decision=forged,
                v1=matching_v1,
                evidence_refs=("tests/test_m02_shadow_scheduler.py",),
            )


def _tree_root(path: Path) -> str:
    values = []
    for item in sorted(path.rglob("*")):
        if item.is_file():
            values.append((str(item.relative_to(path)), hashlib.sha256(item.read_bytes()).hexdigest()))
    return canonical_sha256(tuple(values))


def _database_roots(path: Path) -> tuple[object, ...]:
    with sqlite3.connect(path) as connection:
        return tuple(connection.execute("SELECT * FROM authoritative_roots ORDER BY name"))


def test_shadow_has_zero_write_dispatch_and_authoritative_root_impact(tmp_path, monkeypatch) -> None:
    project = tmp_path / "synthetic-project"
    project.mkdir()
    (project / "checkpoint.md").write_text("revision: 42\n", encoding="utf-8")
    database = project / "state.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE authoritative_roots(name TEXT PRIMARY KEY, value TEXT)")
        connection.executemany(
            "INSERT INTO authoritative_roots VALUES (?, ?)",
            (
                ("revision", "42"),
                ("checkpoint_heads", "stage:1:problem_setup"),
                ("dirty", "[]"),
                ("pending", "[]"),
                ("process", "[]"),
                ("outbox", "[]"),
            ),
        )
    before_tree = _tree_root(project)
    before_database = _database_roots(database)
    bundle = compile_workflow_contract_bundle()
    snapshot = _snapshot(bundle)
    snapshot_before = canonical_bytes(snapshot)
    calls: Counter[str] = Counter()

    def forbidden(name):
        def call(*_args, **_kwargs):
            calls[name] += 1
            raise AssertionError(f"forbidden shadow side effect: {name}")

        return call

    with monkeypatch.context() as blocked:
        blocked.setattr(builtins, "open", forbidden("open"))
        blocked.setattr(Path, "open", forbidden("Path.open"))
        blocked.setattr(Path, "read_text", forbidden("Path.read_text"))
        blocked.setattr(Path, "read_bytes", forbidden("Path.read_bytes"))
        blocked.setattr(Path, "iterdir", forbidden("Path.iterdir"))
        blocked.setattr(Path, "glob", forbidden("Path.glob"))
        blocked.setattr(Path, "rglob", forbidden("Path.rglob"))
        blocked.setattr(os, "scandir", forbidden("os.scandir"))
        blocked.setattr(os, "walk", forbidden("os.walk"))
        blocked.setattr(sqlite3, "connect", forbidden("sqlite3.connect"))
        blocked.setattr(subprocess, "Popen", forbidden("subprocess.Popen"))
        blocked.setattr(subprocess, "run", forbidden("subprocess.run"))
        blocked.setattr(socket, "socket", forbidden("socket.socket"))
        blocked.setattr(socket, "create_connection", forbidden("socket.create_connection"))
        blocked.setattr(time, "time", forbidden("time.time"))
        blocked.setattr(time, "monotonic", forbidden("time.monotonic"))
        blocked.setattr(random, "random", forbidden("random.random"))
        blocked.setattr(uuid, "uuid4", forbidden("uuid.uuid4"))
        readiness_input = StageV1ReadinessAdapter(bundle).adapt(snapshot)
        decision = SchedulerCore.plan(bundle, readiness_input)
        receipt = build_parity_receipt(
            bundle,
            fixture_id="M02-NO-SIDE-EFFECT",
            decision=decision,
            v1=ParityValue(
                TransitionAction.DISPATCH,
                decision.plan.target,
                decision.plan.execution_route,
            ),
            evidence_refs=("synthetic-authoritative-roots",),
        )

    assert calls == Counter()
    assert canonical_bytes(snapshot) == snapshot_before
    assert _tree_root(project) == before_tree
    assert _database_roots(database) == before_database
    assert decision.plan.proposed_mutations == ()
    assert decision.plan.performed_side_effects == ()
    assert receipt.status is ParityStatus.MATCH


def test_production_v1_and_frozen_legacy_do_not_import_or_enable_shadow() -> None:
    engine_source = (ROOT / "factory_core" / "engine.py").read_text(encoding="utf-8")
    service_source = (ROOT / "factory_core" / "service.py").read_text(encoding="utf-8")
    storage_source = (ROOT / "factory_core" / "storage.py").read_text(encoding="utf-8")
    legacy_source = (ROOT / "factory_core" / "adapters" / "legacy.py").read_text(
        encoding="utf-8"
    )
    legacy_runner = (
        ROOT / "factory_core" / "adapters" / "legacy_runner.sh"
    ).read_text(encoding="utf-8")

    assert SHADOW_SCHEDULER_ENABLED_BY_DEFAULT is False
    for source in (engine_source, service_source, storage_source, legacy_source, legacy_runner):
        assert "shadow_scheduler" not in source
        assert "SchedulerCore" not in source
        assert "StageV1ReadinessAdapter" not in source


def test_cross_process_five_seed_replay_matches_checked_in_golden() -> None:
    outputs = []
    for seed in ("0", "1", "17", "999", "random"):
        environment = dict(os.environ)
        environment["PYTHONHASHSEED"] = seed
        result = subprocess.run(
            [sys.executable, str(REPLAY_SCRIPT)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        outputs.append(result.stdout.strip())

    assert len(set(outputs)) == 1
    golden = json.loads(GOLDEN_IDENTITY.read_text(encoding="utf-8"))
    assert json.loads(outputs[0]) == golden


def test_repeated_readiness_plan_and_parity_bytes_are_identical() -> None:
    bundle = compile_workflow_contract_bundle()
    values = []
    for _ in range(25):
        readiness_input, decision = _adapt_plan(bundle, copy.deepcopy(_snapshot(bundle)))
        receipt = build_parity_receipt(
            bundle,
            fixture_id="M02-REPEAT",
            decision=decision,
            v1=ParityValue(
                TransitionAction.DISPATCH,
                decision.plan.target,
                decision.plan.execution_route,
            ),
            evidence_refs=("tests/test_m02_shadow_scheduler.py",),
        )
        values.append(
            (
                canonical_bytes(readiness_input),
                canonical_bytes(decision.readiness),
                canonical_bytes(decision.plan),
                parity_receipt_bytes(receipt),
                parity_receipt_sha256(receipt),
            )
        )

    assert len(set(values)) == 1
