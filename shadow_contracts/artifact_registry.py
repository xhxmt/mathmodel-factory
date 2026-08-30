"""Compatibility import for historical Phase-3 registration tests.

The complete implementation now lives in the packaged but default-off
``factory_core.phase3_artifacts`` module.  Keeping this module preserves the
accepted Phase-3/2-8 test contract while ``shadow_contracts`` itself remains
excluded from production package discovery.
"""

from factory_core.phase3_artifacts import (  # noqa: F401
    ARTIFACT_REGISTRATION_SCHEMA,
    ArtifactRegistration,
    ArtifactRegistrationError,
    artifact_registration_from_dict,
    normalize_artifact_path,
    owner_compilation_semantic_sha256,
    register_artifact_owner,
    validate_artifact_registration,
)


__all__ = (
    "ARTIFACT_REGISTRATION_SCHEMA",
    "ArtifactRegistration",
    "ArtifactRegistrationError",
    "artifact_registration_from_dict",
    "normalize_artifact_path",
    "owner_compilation_semantic_sha256",
    "register_artifact_owner",
    "validate_artifact_registration",
)
