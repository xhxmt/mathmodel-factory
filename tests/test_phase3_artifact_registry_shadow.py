from __future__ import annotations

from dataclasses import replace

import pytest

from factory_core.artifact_ownership import ArtifactOwnership
from shadow_contracts.artifact_registry import (
    ArtifactRegistrationError,
    owner_compilation_semantic_sha256,
    register_artifact_owner,
    validate_artifact_registration,
)
from factory_core.owner_compiler import (
    OwnerPriorityAuthorization,
    compile_owner_registry,
)


def _rule(pattern: str, stage: int) -> ArtifactOwnership:
    return ArtifactOwnership(
        pattern=pattern,
        owner_stage=stage,
        semantic_domain=f"stage_{stage}_artifact",
        dirty_flag="RESULT_DIRTY",
    )


def test_registration_is_deterministic_and_binds_all_authorized_matches():
    authorization = OwnerPriorityAuthorization(
        winner_pattern="results/canonical_results.json",
        winner_owner_stage=4,
        loser_pattern="results/**",
        loser_owner_stage=9,
        issue_id="PHASE3-OWNER-001",
        rationale="Canonical truth precedes the result projection fallback.",
    )
    compilation = compile_owner_registry(
        (
            _rule("results/canonical_results.json", 4),
            _rule("results/**", 9),
        ),
        priority_authorizations=(authorization,),
    )

    first = register_artifact_owner(compilation, "results/canonical_results.json")
    second = register_artifact_owner(compilation, "results\\canonical_results.json")

    assert first == second
    assert first.owner_stage == 4
    assert len(first.matching_rule_ids) == 2
    assert first.owner_rule_id == first.matching_rule_ids[0]
    assert first.owner_compilation_sha256 == owner_compilation_semantic_sha256(
        compilation
    )
    assert validate_artifact_registration(
        first, expected_compilation=compilation
    ) is first


def test_registration_fails_closed_for_missing_or_ambiguous_owner():
    missing = compile_owner_registry((_rule("models/**", 3),))
    with pytest.raises(ArtifactRegistrationError, match="NO_OWNER"):
        register_artifact_owner(missing, "results/values.json")

    ambiguous = compile_owner_registry(
        (_rule("results/**", 4), _rule("results/*.json", 5))
    )
    with pytest.raises(ArtifactRegistrationError, match="MULTIPLE_MATCH"):
        register_artifact_owner(ambiguous, "results/values.json")


def test_registration_owner_stays_frozen_and_loaded_hash_mismatch_fails():
    original = compile_owner_registry((_rule("results/**", 4),))
    changed = compile_owner_registry((_rule("results/**", 5),))
    record = register_artifact_owner(original, "results/values.json")

    assert record.owner_stage == 4
    assert register_artifact_owner(changed, record.normalized_path).owner_stage == 5
    assert record.owner_stage == 4
    assert validate_artifact_registration(
        record, expected_compilation=original
    ) is record
    with pytest.raises(
        ArtifactRegistrationError, match="loaded owner compilation identity mismatch"
    ):
        validate_artifact_registration(record, expected_compilation=changed)


def test_registration_detects_record_tampering_without_re_resolving_owner():
    compilation = compile_owner_registry((_rule("results/**", 4),))
    record = register_artifact_owner(compilation, "results/values.json")

    with pytest.raises(ArtifactRegistrationError, match="owner_id and owner_stage"):
        validate_artifact_registration(replace(record, owner_stage=5))
    with pytest.raises(ArtifactRegistrationError, match="identity mismatch"):
        validate_artifact_registration(
            replace(record, semantic_domain="tampered_domain")
        )


@pytest.mark.parametrize(
    "path",
    (
        "../results/values.json",
        "/results/values.json",
        "C:/results/values.json",
        "results/../values.json",
    ),
)
def test_registration_rejects_non_project_relative_paths(path):
    compilation = compile_owner_registry((_rule("results/**", 4),))

    with pytest.raises(ArtifactRegistrationError, match="project-relative"):
        register_artifact_owner(compilation, path)
