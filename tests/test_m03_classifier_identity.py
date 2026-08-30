from __future__ import annotations

from dataclasses import fields, replace
import hashlib
from pathlib import Path

import pytest

from factory_core.canonical import canonical_sha256
from factory_core.classifier_identity import (
    DirtyClassifierIdentityValidationError,
    compile_dirty_classifier_identity_bundle,
    compile_dirty_classifier_semantic_contract,
    dirty_classifier_analysis_sha256,
    dirty_classifier_semantic_sha256,
    validate_dirty_classifier_identity_bundle,
    validate_dirty_classifier_semantic_contract,
)
from factory_core.classifier_implementation_manifest import (
    TRUSTED_DIRTY_CLASSIFIER_OPERATIONAL_MANIFEST_V1,
    dirty_classifier_operational_implementation_sha256,
    validate_classifier_operational_manifest,
)
from factory_core.legacy_classifier_compat import (
    LEGACY_CLASSIFIER_CONTRACT_SHA256_V9,
    verify_legacy_classifier_contract_sha256_v9,
)
from factory_core.workflow_contract import compile_workflow_contract_bundle
from factory_core.shadow_scheduler import SchedulerCore, StageV1ReadinessAdapter
from tests.test_m02_shadow_scheduler import _step13_math_snapshot
from tests.support.m03_source_manifest import (
    classifier_operational_manifest_sha256,
    rebuild_classifier_operational_manifest,
    with_synthetic_member_bytes,
)


ROOT = Path(__file__).resolve().parents[1]


def _different(value: object) -> object:
    if type(value) is str:
        return value + ":forged"
    if type(value) is int:
        return value + 1
    if type(value) is bool:
        return not value
    if type(value) is tuple:
        return value[:-1] if value else ("forged",)
    raise AssertionError(type(value))


def test_legacy_classifier_compatibility_identity_is_frozen() -> None:
    assert verify_legacy_classifier_contract_sha256_v9() == (
        LEGACY_CLASSIFIER_CONTRACT_SHA256_V9
    )


def test_operational_manifest_rebuild_matches_checked_in_source_bytes() -> None:
    rebuilt = rebuild_classifier_operational_manifest(ROOT)
    assert rebuilt == TRUSTED_DIRTY_CLASSIFIER_OPERATIONAL_MANIFEST_V1
    assert classifier_operational_manifest_sha256(ROOT) == (
        dirty_classifier_operational_implementation_sha256()
    )
    assert tuple(member.relative_path for member in rebuilt.members) == (
        "factory_core/artifact_ownership.py",
        "factory_core/dirty.py",
        "factory_core/paper_sources.py",
    )


def test_transitive_paper_sources_change_moves_operational_not_legacy_identity() -> None:
    baseline = rebuild_classifier_operational_manifest(ROOT)
    paper = (ROOT / "factory_core/paper_sources.py").read_bytes()
    forged = with_synthetic_member_bytes(
        baseline,
        "factory_core/paper_sources.py",
        paper + b"\n# synthetic implementation-only mutation\n",
    )
    assert canonical_sha256(forged) != canonical_sha256(baseline)
    dirty = (ROOT / "factory_core/dirty.py").read_bytes()
    ownership = (ROOT / "factory_core/artifact_ownership.py").read_bytes()
    legacy = hashlib.sha256(
        b"factory-dirty-classifier-v9" + b"\0" + dirty + b"\0" + ownership
    ).hexdigest()
    assert legacy == LEGACY_CLASSIFIER_CONTRACT_SHA256_V9


@pytest.mark.parametrize(
    "field_name",
    [item.name for item in fields(type(compile_dirty_classifier_semantic_contract()))],
)
def test_each_semantic_projection_field_is_source_authorized(field_name: str) -> None:
    contract = compile_dirty_classifier_semantic_contract()
    forged = replace(contract, **{field_name: _different(getattr(contract, field_name))})
    with pytest.raises(DirtyClassifierIdentityValidationError):
        validate_dirty_classifier_semantic_contract(forged)


def test_coherent_semantic_self_hash_forgery_is_rejected() -> None:
    bundle = compile_dirty_classifier_identity_bundle()
    forged_contract = replace(
        bundle.semantic_contract,
        semantic_dirty_flags=bundle.semantic_contract.semantic_dirty_flags[:-1],
        step13_condition_operands=bundle.semantic_contract.step13_condition_operands[:-1],
    )
    forged = replace(
        bundle,
        semantic_contract=forged_contract,
        dirty_classifier_semantic_sha256=canonical_sha256(forged_contract),
    )
    with pytest.raises(DirtyClassifierIdentityValidationError):
        validate_dirty_classifier_identity_bundle(forged)


def test_step13_binding_and_math_route_remain_source_authorized() -> None:
    contract = compile_dirty_classifier_semantic_contract()
    workflow = compile_workflow_contract_bundle()
    expected = workflow.classifier.semantic_dirty_flags
    assert contract.step13_condition_operands == expected
    assert "MATH_DIRTY" in expected
    assert sum(
        subtask.source_step_id == 13
        for stage in workflow.stages
        for subtask in stage.subtasks
    ) == 1
    readiness = StageV1ReadinessAdapter(workflow).adapt(
        _step13_math_snapshot(workflow)
    )
    assert SchedulerCore.plan(workflow, readiness).plan.execution_route == "step:13"


def test_analysis_only_evidence_ref_changes_only_analysis_identity() -> None:
    first = compile_dirty_classifier_identity_bundle(analysis_evidence_refs=("evidence:a",))
    second = compile_dirty_classifier_identity_bundle(analysis_evidence_refs=("evidence:b",))
    assert first.dirty_classifier_semantic_sha256 == second.dirty_classifier_semantic_sha256
    assert first.dirty_classifier_operational_implementation_sha256 == (
        second.dirty_classifier_operational_implementation_sha256
    )
    assert dirty_classifier_analysis_sha256(first) != dirty_classifier_analysis_sha256(second)


def test_manifest_runtime_shape_and_trust_boundary_fail_closed() -> None:
    manifest = TRUSTED_DIRTY_CLASSIFIER_OPERATIONAL_MANIFEST_V1
    first = manifest.members[0]
    forged = replace(manifest, members=(replace(first, byte_size=first.byte_size + 1),) + manifest.members[1:])
    with pytest.raises(Exception):
        validate_classifier_operational_manifest(forged)


def test_classifier_semantic_identity_golden() -> None:
    assert dirty_classifier_semantic_sha256() == (
        "1138ae842c8df3dbbc3e52e88dc2080e8fc136e820c0a242d2342e8c12e54ca3"
    )
    assert dirty_classifier_operational_implementation_sha256() == (
        "717ab065619f01741f64b64992d7461d6298f26bf3be8b668ea3e9282a0566fb"
    )
