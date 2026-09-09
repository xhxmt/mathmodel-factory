from __future__ import annotations

import pytest

from factory_core.artifact_ownership import ArtifactOwnership
from factory_core.owner_compiler import (
    OwnerContractValidationError,
    OwnerDiagnosticCode,
    OwnerPriorityAuthorization,
    compile_owner_registry,
    resolve_owner,
    validate_owner_resolution,
)


def test_priority_authorization_is_pair_scoped_not_a_winner_wildcard() -> None:
    rules = (
        ArtifactOwnership("models/special.py", 5, "validation", "RESULT_DIRTY"),
        ArtifactOwnership("models/**", 3, "model", "MODEL_DIRTY"),
        ArtifactOwnership("models/special.*", 7, "paper", "MATH_DIRTY"),
    )
    authorization = OwnerPriorityAuthorization(
        winner_pattern="models/special.py",
        winner_owner_stage=5,
        loser_pattern="models/**",
        loser_owner_stage=3,
        issue_id="OWNER-TEST-PAIR-001",
        rationale="Only the known validation-over-model fallback pair is authorized.",
    )

    resolution = resolve_owner(
        compile_owner_registry(rules, priority_authorizations=(authorization,)),
        "models/special.py",
    )
    codes = [diagnostic.code for diagnostic in resolution.diagnostics]

    assert OwnerDiagnosticCode.INTENTIONAL_PRIORITY in codes
    assert OwnerDiagnosticCode.MULTIPLE_MATCH in codes
    assert any(
        diagnostic.issue_id == "OWNER-TEST-PAIR-001"
        and len(diagnostic.rule_ids) == 2
        for diagnostic in resolution.diagnostics
    )
    with pytest.raises(OwnerContractValidationError, match="MULTIPLE_MATCH"):
        validate_owner_resolution(resolution, strict=True)
