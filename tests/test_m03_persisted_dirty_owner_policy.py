from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path

import pytest

from factory_core.dirty import DirtyChange, DirtyFlag, solver_receipt_job_id
from factory_core.engine import FactoryEngine
from factory_core.persisted_dirty_owner_implementation_manifest import (
    TRUSTED_PERSISTED_DIRTY_OWNER_IMPLEMENTATION_MANIFEST_V1,
    persisted_dirty_owner_policy_implementation_sha256,
)
from factory_core.persisted_dirty_owner_policy import (
    PersistedDirtyOwnerFactV1,
    PersistedDirtyOwnerPolicyValidationError,
    apply_persisted_dirty_owner_policy,
    compile_persisted_dirty_owner_identity,
    compile_persisted_dirty_owner_policy,
    persisted_dirty_owner_policy_semantic_sha256,
    validate_persisted_dirty_owner_identity,
    validate_persisted_dirty_owner_policy,
)
from tests.support.m03_symbol_manifest import rebuild_persisted_dirty_owner_manifest


ROOT = Path(__file__).resolve().parents[1]


def _change(path: str, *, owner: int = 4) -> DirtyChange:
    return DirtyChange(DirtyFlag.RESULT, owner, path, "a", "b")


def _different(value: object) -> object:
    if type(value) is str:
        return value + ":forged"
    if type(value) is int:
        return value + 1
    if type(value) is tuple:
        return value[:-1] if value else ("forged",)
    raise AssertionError(type(value))


def test_symbol_manifest_rebuild_matches_exact_frozen_symbols() -> None:
    assert rebuild_persisted_dirty_owner_manifest(ROOT) == (
        TRUSTED_PERSISTED_DIRTY_OWNER_IMPLEMENTATION_MANIFEST_V1
    )
    assert all(
        symbol.qualified_symbol not in {"FactoryEngine", "SQLiteStateStore"}
        for symbol in TRUSTED_PERSISTED_DIRTY_OWNER_IMPLEMENTATION_MANIFEST_V1.symbols
    )


@pytest.mark.parametrize(
    "field_name",
    [item.name for item in fields(type(compile_persisted_dirty_owner_policy()))],
)
def test_each_owner_policy_semantic_field_is_source_authorized(field_name: str) -> None:
    policy = compile_persisted_dirty_owner_policy()
    forged = replace(policy, **{field_name: _different(getattr(policy, field_name))})
    with pytest.raises(PersistedDirtyOwnerPolicyValidationError):
        validate_persisted_dirty_owner_policy(forged)


@pytest.mark.parametrize(
    ("path", "facts", "expected"),
    [
        ("plain.txt", (PersistedDirtyOwnerFactV1("job", 7),), 4),
        (".factory/solver_receipts/job.submitted.json", (), 4),
        (".factory/solver_receipts/job.completed.json", (PersistedDirtyOwnerFactV1("job", None),), 4),
        (".factory/solver_receipts/job.submitted.json", (PersistedDirtyOwnerFactV1("job", 7),), 7),
        (".factory\\solver_receipts\\job.completed.json", (PersistedDirtyOwnerFactV1("job", 8),), 8),
    ],
)
def test_persisted_owner_policy_branches(path, facts, expected) -> None:
    before = _change(path)
    after = apply_persisted_dirty_owner_policy(before, facts)
    assert after.owner_stage == expected
    assert after.flag is before.flag
    assert after.cause_artifact == before.cause_artifact
    assert after.baseline_fingerprint == before.baseline_fingerprint
    assert after.current_fingerprint == before.current_fingerprint


def test_production_engine_owner_lookup_conforms_to_pure_policy() -> None:
    class Store:
        def solver_job(self, job_id):
            if job_id == "missing":
                raise KeyError(job_id)
            return {"owner_stage": {"owned": "7", "none": None}[job_id]}

    engine = object.__new__(FactoryEngine)
    engine.store = Store()
    for job_id, expected in (("owned", 7), ("none", None), ("missing", None)):
        path = f".factory/solver_receipts/{job_id}.completed.json"
        assert solver_receipt_job_id(path) == job_id
        assert engine._solver_receipt_owner_stage(path) == expected
        facts = () if job_id == "missing" else (PersistedDirtyOwnerFactV1(job_id, expected),)
        result = apply_persisted_dirty_owner_policy(_change(path), facts)
        assert result.owner_stage == (expected if expected is not None else 4)


def test_owner_policy_coherent_identity_forgery_is_rejected() -> None:
    identity = compile_persisted_dirty_owner_identity()
    forged_policy = replace(identity.policy, owner_conversion="forged")
    forged = replace(
        identity,
        policy=forged_policy,
        persisted_dirty_owner_policy_semantic_sha256="a" * 64,
    )
    with pytest.raises(PersistedDirtyOwnerPolicyValidationError):
        validate_persisted_dirty_owner_identity(forged)


def test_owner_policy_identity_goldens() -> None:
    assert persisted_dirty_owner_policy_semantic_sha256() == (
        "8c5dad1b7d08672c4c5481d15bf525bd06757004887d3c435351cb7dbed8525d"
    )
    assert persisted_dirty_owner_policy_implementation_sha256() == (
        "e3b93c91a1ecfaedf2e0ffcdcde8596629b7b42250789d5e313293d4c40b9715"
    )
