"""Pure identity bridge for the Phase 2-8 shadow integration acceptance.

The existing Phase-3 registration intentionally does not bind artifact bytes or
an authority coordinate.  This direct-test-only adapter adds exactly that
composition identity.  It performs no I/O and grants no production authority.
"""

from __future__ import annotations

from dataclasses import dataclass

from factory_core.canonical import canonical_sha256
from shadow_contracts.artifact_registry import (
    ArtifactRegistration,
    validate_artifact_registration,
)


SHADOW_CHAIN_COORDINATE_SCHEMA = "phase2-8-shadow-coordinate-v1"
SHADOW_ARTIFACT_BINDING_SCHEMA = "phase2-8-shadow-artifact-binding-v1"


class ShadowIntegrationError(ValueError):
    """Raised when a cross-phase shadow identity is incomplete."""


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ShadowIntegrationError(f"{field} must be a non-blank string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ShadowIntegrationError(f"{field} must be valid UTF-8") from exc
    return value


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ShadowIntegrationError(f"{field} must be lowercase SHA-256 hex")
    return value


def _nonnegative_integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ShadowIntegrationError(f"{field} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class ShadowChainCoordinate:
    schema_version: str
    project_id: str
    workflow_id: str
    subject: str
    project_generation: str
    runtime_generation: str
    scheduler_generation: str
    run_generation: str
    revision: int
    authority_request_sha256: str
    authority_event_sha256: str
    authority_receipt_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "workflow_id": self.workflow_id,
            "subject": self.subject,
            "project_generation": self.project_generation,
            "runtime_generation": self.runtime_generation,
            "scheduler_generation": self.scheduler_generation,
            "run_generation": self.run_generation,
            "revision": self.revision,
            "authority_request_sha256": self.authority_request_sha256,
            "authority_event_sha256": self.authority_event_sha256,
            "authority_receipt_sha256": self.authority_receipt_sha256,
        }

    @property
    def coordinate_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True)
class ShadowArtifactBinding:
    schema_version: str
    coordinate: ShadowChainCoordinate
    coordinate_sha256: str
    normalized_path: str
    artifact_sha256: str
    artifact_byte_length: int
    registration_sha256: str
    owner_compilation_sha256: str
    owner_id: str
    owner_stage: int

    def _identity_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "coordinate": self.coordinate.as_dict(),
            "coordinate_sha256": self.coordinate_sha256,
            "normalized_path": self.normalized_path,
            "artifact_sha256": self.artifact_sha256,
            "artifact_byte_length": self.artifact_byte_length,
            "registration_sha256": self.registration_sha256,
            "owner_compilation_sha256": self.owner_compilation_sha256,
            "owner_id": self.owner_id,
            "owner_stage": self.owner_stage,
        }

    @property
    def binding_sha256(self) -> str:
        return canonical_sha256(self._identity_dict())

    def as_dict(self) -> dict[str, object]:
        result = self._identity_dict()
        result["binding_sha256"] = self.binding_sha256
        return result


def build_shadow_chain_coordinate(
    *,
    project_id: str,
    workflow_id: str,
    project_generation: str,
    runtime_generation: str,
    scheduler_generation: str,
    run_generation: str,
    revision: int,
    authority_request_sha256: str,
    authority_event_sha256: str,
    authority_receipt_sha256: str,
) -> ShadowChainCoordinate:
    """Bind one imported authority coordinate to a deterministic subject."""

    normalized_project = _text(project_id, "project_id")
    normalized_workflow = _text(workflow_id, "workflow_id")
    normalized_run = _text(run_generation, "run_generation")
    subject = (
        f"project:{normalized_project}/workflow:{normalized_workflow}/"
        f"run:{normalized_run}"
    )
    return ShadowChainCoordinate(
        schema_version=SHADOW_CHAIN_COORDINATE_SCHEMA,
        project_id=normalized_project,
        workflow_id=normalized_workflow,
        subject=subject,
        project_generation=_text(project_generation, "project_generation"),
        runtime_generation=_text(runtime_generation, "runtime_generation"),
        scheduler_generation=_text(
            scheduler_generation, "scheduler_generation"
        ),
        run_generation=normalized_run,
        revision=_nonnegative_integer(revision, "revision"),
        authority_request_sha256=_sha256(
            authority_request_sha256, "authority_request_sha256"
        ),
        authority_event_sha256=_sha256(
            authority_event_sha256, "authority_event_sha256"
        ),
        authority_receipt_sha256=_sha256(
            authority_receipt_sha256, "authority_receipt_sha256"
        ),
    )


def bind_registered_artifact(
    *,
    coordinate: ShadowChainCoordinate,
    registration: ArtifactRegistration,
    artifact_sha256: str,
    artifact_byte_length: int,
) -> ShadowArtifactBinding:
    """Bind Phase-3 ownership to Phase-2 identity and declared artifact bytes."""

    if not isinstance(coordinate, ShadowChainCoordinate):
        raise ShadowIntegrationError("coordinate must be ShadowChainCoordinate")
    checked_registration = validate_artifact_registration(registration)
    return ShadowArtifactBinding(
        schema_version=SHADOW_ARTIFACT_BINDING_SCHEMA,
        coordinate=coordinate,
        coordinate_sha256=coordinate.coordinate_sha256,
        normalized_path=checked_registration.normalized_path,
        artifact_sha256=_sha256(artifact_sha256, "artifact_sha256"),
        artifact_byte_length=_nonnegative_integer(
            artifact_byte_length, "artifact_byte_length"
        ),
        registration_sha256=checked_registration.registration_sha256,
        owner_compilation_sha256=checked_registration.owner_compilation_sha256,
        owner_id=checked_registration.owner_id,
        owner_stage=checked_registration.owner_stage,
    )
