from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import errno
import os
from pathlib import Path
import subprocess
import sys

import pytest

from factory_core.artifact_ownership import ArtifactOwnership
from factory_core.owner_compiler import compile_owner_registry
import factory_core.phase3_artifacts as phase3
import factory_core.phase3_shadow_runtime as runtime


CHECKPOINT_KEY = "phase3:stage4.results"


def _compilation(stage: int = 4):
    return compile_owner_registry(
        (
            ArtifactOwnership(
                pattern="results/**",
                owner_stage=stage,
                semantic_domain="canonical_result",
                dirty_flag="RESULT_DIRTY",
            ),
        )
    )


def _record(compilation, path: str, content: bytes):
    registration = phase3.register_artifact_owner(compilation, path)
    return phase3.build_artifact_record(registration, content=content)


def _registration(compilation, path: str):
    return phase3.register_artifact_owner(compilation, path)


def _manifest(compilation, *records):
    return phase3.build_artifact_manifest(
        owner_compilation_sha256=phase3.owner_compilation_semantic_sha256(
            compilation
        ),
        records=records,
    )


def test_artifact_record_and_manifest_are_typed_frozen_and_deterministic():
    compilation = _compilation()
    first = _record(compilation, "results/a.json", b'{"value":1}\n')
    second = _record(compilation, "results/b.json", b'{"value":2}\n')

    left = _manifest(compilation, second, first)
    right = _manifest(compilation, first, second)

    assert left == right
    assert [item.normalized_path for item in left.records] == [
        "results/a.json",
        "results/b.json",
    ]
    assert first.artifact_record_id == f"artifact-{first.record_sha256}"
    with pytest.raises(FrozenInstanceError):
        first.byte_length = 999  # type: ignore[misc]

    assert phase3.artifact_registration_from_dict(
        first.registration.as_dict()
    ) == first.registration
    assert phase3.artifact_record_from_dict(first.as_dict()) == first


def test_phase3_domain_identities_are_stable_across_python_hash_seeds():
    root = Path(__file__).resolve().parents[1]
    script = r'''
from factory_core.artifact_ownership import ArtifactOwnership
from factory_core.owner_compiler import compile_owner_registry
from factory_core.phase3_artifacts import (
    CheckpointState, CheckpointTransition, build_artifact_manifest,
    build_artifact_record, build_checkpoint_entry, build_phase3_mutation,
    build_reopen_plan, compute_change_set, owner_compilation_semantic_sha256,
    register_artifact_owner,
)

compilation = compile_owner_registry((ArtifactOwnership(
    pattern="results/**", owner_stage=4, semantic_domain="canonical_result",
    dirty_flag="RESULT_DIRTY",
),))
policy = owner_compilation_semantic_sha256(compilation)
record = build_artifact_record(
    register_artifact_owner(compilation, "results/a.json"), content=b"stable"
)
previous = build_artifact_manifest(owner_compilation_sha256=policy, records=())
current = build_artifact_manifest(owner_compilation_sha256=policy, records=(record,))
changes = compute_change_set(previous, current)
plan = build_reopen_plan(
    workflow_id="legacy_current", source_revision=7,
    change_set=changes, previous_manifest=previous,
)
checkpoint = build_checkpoint_entry(
    checkpoint_key="phase3:stage4.results", owner_stage=4,
    input_manifest_sha256=current.manifest_sha256,
    state=CheckpointState.VALID, transition=CheckpointTransition.RECORDED_VALID,
    validation_sha256="1" * 64, reason_code="INITIAL_ATTESTATION",
)
mutation = build_phase3_mutation(
    artifact_records=(record,), checkpoint_entries=(checkpoint,), reopen_plan=plan,
)
print(" ".join((record.record_sha256, current.manifest_sha256,
    changes.change_set_sha256, plan.plan_sha256, checkpoint.checkpoint_sha256,
    mutation.mutation_sha256)))
'''
    outputs = []
    for seed in ("1", "42", "8675309"):
        environment = os.environ.copy()
        environment.update(
            PYTHONHASHSEED=seed,
            PYTHONDONTWRITEBYTECODE="1",
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert completed.returncode == 0, completed.stderr
        outputs.append(completed.stdout)

    assert len(set(outputs)) == 1


def test_typed_record_parser_never_coerces_scalar_field_types():
    compilation = _compilation()
    registration = phase3.register_artifact_owner(
        compilation, "results/a.json"
    )
    numeric_type = phase3.build_artifact_record(
        registration,
        content=b"a",
        artifact_type="123",
    )
    raw = numeric_type.as_dict()
    raw["artifact_type"] = 123

    with pytest.raises(phase3.Phase3ContractError, match="artifact_type"):
        phase3.artifact_record_from_dict(raw)


def test_manifest_capture_is_order_independent_and_rejects_symlink_and_traversal(
    tmp_path,
):
    project = tmp_path / "project"
    (project / "results").mkdir(parents=True)
    (project / "results/a.json").write_bytes(b"a")
    (project / "results/b.json").write_bytes(b"b")
    (project / "results/link.json").symlink_to(project / "results/a.json")
    compilation = _compilation()

    first = phase3.capture_artifact_manifest(
        project,
        compilation,
        ("results/b.json", "results/a.json", "results/link.json", "../escape"),
    )
    second = phase3.capture_artifact_manifest(
        project,
        compilation,
        ("../escape", "results/link.json", "results/a.json", "results/b.json"),
    )

    assert first == second
    assert [item.normalized_path for item in first.records] == [
        "results/a.json",
        "results/b.json",
    ]
    assert {item.code for item in first.blockers} == {
        phase3.ArtifactBlockerCode.INVALID_PATH,
        phase3.ArtifactBlockerCode.SYMLINK,
    }


@pytest.mark.parametrize(
    "path",
    (
        "./results/a.json",
        "results//a.json",
        "results/a.json/",
        "results/./a.json",
    ),
)
def test_registration_rejects_paths_that_require_lexical_normalization(path):
    with pytest.raises(phase3.ArtifactRegistrationError, match="project-relative"):
        phase3.register_artifact_owner(_compilation(), path)


def test_manifest_capture_rejects_a_project_root_with_symlinked_ancestor(tmp_path):
    real = tmp_path / "real"
    project = real / "project"
    (project / "results").mkdir(parents=True)
    (project / "results/a.json").write_bytes(b"a")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    manifest = phase3.capture_artifact_manifest(
        alias / "project", _compilation(), ("results/a.json",)
    )

    assert manifest.records == ()
    assert manifest.blockers == (
        phase3.ArtifactBlocker(
            phase3.ArtifactBlockerCode.ROOT_UNSAFE,
            "__project_root__",
            "root_unsafe",
        ),
    )


def test_manifest_capture_rejects_an_intermediate_symlink_escape(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.json").write_bytes(b"outside")
    (project / "results").symlink_to(outside, target_is_directory=True)

    manifest = phase3.capture_artifact_manifest(
        project, _compilation(), ("results/a.json",)
    )

    assert manifest.records == ()
    assert manifest.blockers[0].code is phase3.ArtifactBlockerCode.SYMLINK


def test_unreadable_artifact_is_a_typed_blocker(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "results").mkdir(parents=True)
    (project / "results/a.json").write_bytes(b"a")
    compilation = _compilation()

    def unreadable(_root: Path, _path: str) -> bytes:
        raise PermissionError("secret operating-system detail")

    monkeypatch.setattr(phase3, "_read_artifact_bytes", unreadable)
    manifest = phase3.capture_artifact_manifest(
        project, compilation, ("results/a.json",)
    )

    assert manifest.records == ()
    assert manifest.blockers == (
        phase3.ArtifactBlocker(
            phase3.ArtifactBlockerCode.UNREADABLE,
            "results/a.json",
            "unreadable",
            registration=_registration(compilation, "results/a.json"),
        ),
    )


def test_changed_during_read_is_a_typed_blocker(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "results").mkdir(parents=True)
    (project / "results/a.json").write_bytes(b"a")

    def changed(_root: Path, _path: str) -> bytes:
        raise OSError(errno.ESTALE, "unstable file")

    monkeypatch.setattr(phase3, "_read_artifact_bytes", changed)
    manifest = phase3.capture_artifact_manifest(
        project, _compilation(), ("results/a.json",)
    )

    assert manifest.records == ()
    assert (
        manifest.blockers[0].code
        is phase3.ArtifactBlockerCode.CHANGED_DURING_READ
    )


def test_change_set_fails_closed_when_owner_policy_changes():
    old_compilation = _compilation(4)
    new_compilation = _compilation(5)
    previous = _manifest(
        old_compilation,
        _record(old_compilation, "results/a.json", b"old"),
    )
    current = _manifest(
        new_compilation,
        _record(new_compilation, "results/a.json", b"new"),
    )

    assert (
        phase3.classify_owner_policy(previous, current)
        is phase3.OwnerPolicyDisposition.MIGRATION_REQUIRED
    )
    with pytest.raises(phase3.OwnerPolicyMigrationRequired, match="migration") as raised:
        phase3.compute_change_set(previous, current)
    assert raised.value.classification is phase3.OwnerPolicyDisposition.MIGRATION_REQUIRED


def test_change_set_and_reopen_plan_bind_dirty_owner_and_read_set_cas():
    compilation = _compilation()
    old_a = _record(compilation, "results/a.json", b"old")
    removed = _record(compilation, "results/removed.json", b"removed")
    new_a = _record(compilation, "results/a.json", b"new")
    added = _record(compilation, "results/added.json", b"added")
    previous = _manifest(compilation, old_a, removed)
    current = _manifest(compilation, new_a, added)
    removal = phase3.build_artifact_removal(
        previous,
        "results/removed.json",
        reason_code="EXPLICIT_UNTRACK",
    )

    changes = phase3.compute_change_set(previous, current, removals=(removal,))
    plan = phase3.build_reopen_plan(
        workflow_id="legacy_current",
        source_revision=7,
        change_set=changes,
        previous_manifest=previous,
    )

    assert {item.kind for item in changes.changes} == {
        phase3.ArtifactChangeKind.ADDED,
        phase3.ArtifactChangeKind.MODIFIED,
        phase3.ArtifactChangeKind.REMOVED,
    }
    assert {item.owner_stage for item in changes.dirty_decisions} == {4}
    by_path = {item.normalized_path: item for item in plan.read_set}
    assert by_path["results/added.json"].expected_artifact_record_id is None
    assert by_path["results/a.json"].expected_artifact_record_id == old_a.artifact_record_id
    assert by_path["results/removed.json"].expected_record_sha256 == removed.record_sha256
    assert plan.target_scope == "stage:4"
    assert phase3.validate_reopen_plan(plan) is plan
    assert phase3.reopen_plan_from_dict(plan.as_dict()) == plan

    with pytest.raises(phase3.Phase3ContractError, match="identity mismatch"):
        phase3.build_reopen_plan(
            workflow_id="legacy_current",
            source_revision=7,
            change_set=replace(changes, change_set_sha256="f" * 64),
            previous_manifest=previous,
        )


def test_owner_resolution_blocker_removal_requires_typed_operator_authorization():
    compilation = _compilation()
    policy = phase3.owner_compilation_semantic_sha256(compilation)
    previous = phase3.build_artifact_manifest(
        owner_compilation_sha256=policy,
        records=(),
        blockers=(
            phase3.ArtifactBlocker(
                phase3.ArtifactBlockerCode.OWNER_RESOLUTION_BLOCKED,
                "results/a.json",
                "owner_resolution_blocked",
            ),
        ),
    )

    with pytest.raises(
        phase3.Phase3ContractError, match="typed operator authorization"
    ):
        phase3.build_artifact_removal(
            previous,
            "results/a.json",
            reason_code="OPERATOR_UNTRACK",
        )


def test_owner_resolution_blocker_removal_derives_owner_from_authorization():
    compilation = _compilation()
    policy = phase3.owner_compilation_semantic_sha256(compilation)
    claim = phase3.build_artifact_owner_operator_claim(
        workflow_id="legacy_current",
        source_revision=7,
        command_id="command-owner-resolution",
        normalized_path="unowned/a.json",
        owner_compilation=compilation,
        owner_id="owner:stage:4",
        owner_stage=4,
        dirty_flag="RESULT_DIRTY",
        operator_subject="operator:stage-migration",
        reason_code="OWNER_POLICY_MIGRATION",
    )
    authorization = phase3.build_artifact_owner_operator_authorization(
        claim,
        issuer_writer_id="writer-a",
        issuer_writer_epoch=1,
        issuer_receipt_id="control:owner-resolution",
        issuer_receipt_sha256="a" * 64,
    )
    previous = phase3.build_artifact_manifest(
        owner_compilation_sha256=policy,
        records=(),
        blockers=(
            phase3.ArtifactBlocker(
                phase3.ArtifactBlockerCode.OWNER_RESOLUTION_BLOCKED,
                "unowned/a.json",
                "owner_resolution_blocked",
                operator_authorization=authorization,
            ),
        ),
    )

    removal = phase3.build_artifact_removal(
        previous,
        "unowned/a.json",
        reason_code="OPERATOR_UNTRACK",
    )

    assert removal.previous_blocker_code == "OWNER_RESOLUTION_BLOCKED"
    assert removal.owner_id == authorization.owner_id
    assert removal.owner_stage == authorization.owner_stage
    assert removal.dirty_flag == authorization.dirty_flag
    assert (
        phase3.artifact_owner_operator_authorization_from_dict(
            authorization.as_dict()
        )
        == authorization
    )

    with pytest.raises(phase3.Phase3ContractError, match="identity mismatch"):
        phase3.artifact_owner_operator_authorization_from_dict(
            {
                **authorization.as_dict(),
                "command_id": "command-tampered",
            }
        )
    with pytest.raises(phase3.Phase3ContractError, match="owner identity differs"):
        phase3.validate_artifact_owner_operator_authorization(
            replace(authorization, owner_id="owner:stage:8")
        )


def test_checkpoint_transition_contract_and_all_dry_run_classifications():
    manifest_a = "a" * 64
    manifest_b = "b" * 64
    validation_a = "1" * 64
    validation_b = "2" * 64
    valid = phase3.build_checkpoint_entry(
        checkpoint_key=CHECKPOINT_KEY,
        owner_stage=4,
        input_manifest_sha256=manifest_a,
        state=phase3.CheckpointState.VALID,
        transition=phase3.CheckpointTransition.RECORDED_VALID,
        validation_sha256=validation_a,
        reason_code="INITIAL_ATTESTATION",
    )
    invalidated = phase3.build_checkpoint_entry(
        checkpoint_key=CHECKPOINT_KEY,
        owner_stage=4,
        input_manifest_sha256=manifest_b,
        state=phase3.CheckpointState.INVALID,
        transition=phase3.CheckpointTransition.INVALIDATED,
        validation_sha256=None,
        previous_checkpoint_id=valid.checkpoint_id,
        previous_checkpoint_occurrence_id=valid.checkpoint_id,
        reason_code="INPUT_CHANGED",
    )
    reattested = phase3.build_checkpoint_entry(
        checkpoint_key=CHECKPOINT_KEY,
        owner_stage=4,
        input_manifest_sha256=manifest_b,
        state=phase3.CheckpointState.VALID,
        transition=phase3.CheckpointTransition.REATTESTED_VALID,
        validation_sha256=validation_b,
        previous_checkpoint_id=invalidated.checkpoint_id,
        previous_checkpoint_occurrence_id=invalidated.checkpoint_id,
        reason_code="VALIDATOR_PASS",
    )

    reused = phase3.dry_run_checkpoint_reattestation(
        valid, current_manifest_sha256=manifest_a, regenerated_valid=False
    )
    regenerated = phase3.dry_run_checkpoint_reattestation(
        invalidated,
        current_manifest_sha256=manifest_b,
        regenerated_valid=True,
        regenerated_validation_sha256=validation_b,
    )
    still_invalid = phase3.dry_run_checkpoint_reattestation(
        invalidated, current_manifest_sha256=manifest_b, regenerated_valid=False
    )

    assert reused.classification is phase3.ReattestationClassification.REUSED
    assert reused.would_write is False
    assert regenerated.classification is phase3.ReattestationClassification.REGENERATED
    assert regenerated.would_write is True
    assert still_invalid.classification is phase3.ReattestationClassification.STILL_INVALID
    assert still_invalid.would_write is False
    assert reattested.previous_checkpoint_id == invalidated.checkpoint_id
    assert phase3.checkpoint_entry_from_dict(reattested.as_dict()) == reattested

    with pytest.raises(phase3.Phase3ContractError, match="predecessor binding"):
        phase3.build_checkpoint_entry(
            checkpoint_key=CHECKPOINT_KEY,
            owner_stage=4,
            input_manifest_sha256=manifest_b,
            state=phase3.CheckpointState.INVALID,
            transition=phase3.CheckpointTransition.INVALIDATED,
            validation_sha256=None,
            reason_code="INPUT_CHANGED",
        )
    with pytest.raises(phase3.Phase3ContractError, match="identity mismatch"):
        phase3.validate_checkpoint_reattestation(
            replace(regenerated, receipt_sha256="f" * 64)
        )


@pytest.mark.parametrize(
    ("left", "right", "differences", "blockers", "expected"),
    (
        ("a" * 64, "a" * 64, (), (), phase3.ParityClassification.MATCH),
        (
            "a" * 64,
            "b" * 64,
            ("V1_PATH_NORMALIZATION",),
            (),
            phase3.ParityClassification.EXPECTED_DIFFERENCE,
        ),
        ("a" * 64, "b" * 64, (), (), phase3.ParityClassification.DIVERGENCE),
        (None, "b" * 64, (), ("V1_UNAVAILABLE",), phase3.ParityClassification.BLOCKED),
    ),
)
def test_parity_receipt_classification(left, right, differences, blockers, expected):
    receipt = phase3.build_parity_receipt(
        subject="manifest",
        v1_sha256=left,
        shadow_sha256=right,
        expected_difference_codes=differences,
        blocker_codes=blockers,
    )

    assert receipt.classification is expected
    assert len(receipt.receipt_sha256) == 64
    with pytest.raises(phase3.Phase3ContractError, match="identity mismatch"):
        phase3.validate_parity_receipt(
            replace(receipt, receipt_sha256="f" * 64)
        )


def test_default_off_runner_performs_no_filesystem_read_or_dispatch(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled runner touched the filesystem")

    monkeypatch.setattr(runtime, "capture_artifact_manifest", forbidden)
    result = runtime.run_phase3_full_shadow(
        project_root="/does/not/exist",
        paths=("results/a.json",),
    )

    assert result.enabled is False
    assert result.authoritative is False
    assert result.dispatch_performed is False
    assert (
        result.owner_policy_disposition
        is phase3.OwnerPolicyDisposition.NOT_EVALUATED
    )
    assert result.manifest is None


def test_explicit_shadow_runner_is_deterministic_and_non_authoritative(tmp_path):
    project = tmp_path / "project"
    (project / "results").mkdir(parents=True)
    (project / "results/a.json").write_bytes(b"value")
    kwargs = {
        "enabled": True,
        "project_root": project,
        "compilation": _compilation(),
        "paths": ("results/a.json",),
    }

    first = runtime.run_phase3_full_shadow(**kwargs)
    second = runtime.run_phase3_full_shadow(**kwargs)

    assert first == second
    assert first.enabled is True
    assert first.authoritative is False
    assert first.dispatch_performed is False
    assert first.manifest is not None
    assert first.parity.classification is phase3.ParityClassification.BLOCKED


def test_explicit_runner_composes_change_reopen_reattestation_and_parity(tmp_path):
    project = tmp_path / "project"
    (project / "results").mkdir(parents=True)
    (project / "results/a.json").write_bytes(b"new")
    compilation = _compilation()
    previous = _manifest(
        compilation,
        _record(compilation, "results/a.json", b"old"),
    )
    checkpoint = phase3.build_checkpoint_entry(
        checkpoint_key=CHECKPOINT_KEY,
        owner_stage=4,
        input_manifest_sha256=previous.manifest_sha256,
        state=phase3.CheckpointState.VALID,
        transition=phase3.CheckpointTransition.RECORDED_VALID,
        validation_sha256="1" * 64,
        reason_code="INITIAL_ATTESTATION",
    )

    result = runtime.run_phase3_full_shadow(
        enabled=True,
        project_root=project,
        compilation=compilation,
        paths=("results/a.json",),
        previous_manifest=previous,
        workflow_id="legacy_current",
        source_revision=7,
        previous_checkpoint=checkpoint,
        regenerated_valid=True,
        regenerated_validation_sha256="2" * 64,
        v1_manifest_sha256="f" * 64,
    )

    assert result.owner_policy_disposition is phase3.OwnerPolicyDisposition.UNCHANGED
    assert result.change_set is not None
    assert result.reopen_plan is not None
    assert result.reopen_plan.source_revision == 7
    assert result.reattestation is not None
    assert (
        result.reattestation.classification
        is phase3.ReattestationClassification.REGENERATED
    )
    assert result.parity is not None
    assert result.parity.classification is phase3.ParityClassification.DIVERGENCE
    assert result.authoritative is False
    assert result.dispatch_performed is False


def test_enabled_runner_reports_owner_policy_migration_without_reopen(tmp_path):
    project = tmp_path / "project"
    (project / "results").mkdir(parents=True)
    (project / "results/a.json").write_bytes(b"new")
    original = _compilation(4)
    previous = _manifest(
        original,
        _record(original, "results/a.json", b"old"),
    )

    result = runtime.run_phase3_full_shadow(
        enabled=True,
        project_root=project,
        compilation=_compilation(5),
        paths=("results/a.json",),
        previous_manifest=previous,
    )

    assert result.owner_policy_disposition is phase3.OwnerPolicyDisposition.MIGRATION_REQUIRED
    assert result.change_set is None
    assert result.reopen_plan is None
    assert result.authoritative is False
    assert result.dispatch_performed is False


def test_enabled_runner_keeps_invalid_paths_as_blocked_shadow_evidence(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    compilation = _compilation()
    previous = _manifest(compilation)

    result = runtime.run_phase3_full_shadow(
        enabled=True,
        project_root=project,
        compilation=compilation,
        paths=("../escape",),
        previous_manifest=previous,
    )

    assert result.change_set is not None
    assert result.change_set.changes[0].kind is phase3.ArtifactChangeKind.BLOCKED
    assert (
        result.change_set.dirty_decisions[0].disposition
        is phase3.DirtyDisposition.BLOCKED
    )
    assert result.reopen_plan is None
    assert result.parity is not None
    assert result.parity.classification is phase3.ParityClassification.BLOCKED


def test_phase3_mutation_is_nonempty_sorted_and_hash_bound():
    compilation = _compilation()
    b_record = _record(compilation, "results/b.json", b"b")
    a_record = _record(compilation, "results/a.json", b"a")

    mutation = phase3.build_phase3_mutation(artifact_records=(b_record, a_record))

    assert [item.normalized_path for item in mutation.artifact_records] == [
        "results/a.json",
        "results/b.json",
    ]
    assert phase3.validate_phase3_mutation(mutation) is mutation
    with pytest.raises(phase3.Phase3ContractError, match="at least one"):
        phase3.build_phase3_mutation()


def test_phase3_mutation_rejects_duplicate_artifact_paths_and_checkpoint_keys():
    compilation = _compilation()
    old = _record(compilation, "results/a.json", b"old")
    new = _record(compilation, "results/a.json", b"new")
    with pytest.raises(phase3.Phase3ContractError, match="paths must be unique"):
        phase3.build_phase3_mutation(artifact_records=(old, new))

    changed_compilation = _compilation(5)
    changed_owner = _record(
        changed_compilation,
        "results/b.json",
        b"different-policy",
    )
    with pytest.raises(phase3.Phase3ContractError, match="one frozen owner policy"):
        phase3.build_phase3_mutation(artifact_records=(old, changed_owner))

    first = phase3.build_checkpoint_entry(
        checkpoint_key=CHECKPOINT_KEY,
        owner_stage=4,
        input_manifest_sha256="a" * 64,
        state=phase3.CheckpointState.VALID,
        transition=phase3.CheckpointTransition.RECORDED_VALID,
        validation_sha256="1" * 64,
        reason_code="INITIAL_A",
    )
    second = phase3.build_checkpoint_entry(
        checkpoint_key=CHECKPOINT_KEY,
        owner_stage=4,
        input_manifest_sha256="b" * 64,
        state=phase3.CheckpointState.VALID,
        transition=phase3.CheckpointTransition.RECORDED_VALID,
        validation_sha256="2" * 64,
        reason_code="INITIAL_B",
    )
    with pytest.raises(phase3.Phase3ContractError, match="keys must be unique"):
        phase3.build_phase3_mutation(checkpoint_entries=(first, second))
