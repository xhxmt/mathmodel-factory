"""Cross-store current-head fence for Phase 7+8 shadow commits."""

from __future__ import annotations

from dataclasses import dataclass

from .authority_read_repository import (
    AuthorityPhase3ArtifactState,
    AuthorityReadRepository,
    AuthorityWorkflowCoordinate,
    authority_phase3_artifact_state_from_dict,
    validate_authority_phase3_artifact_state,
)
from .phase3_artifacts import (
    ArtifactLedgerOccurrence,
    artifact_occurrence_from_dict,
    validate_artifact_occurrence,
)
from .phase6_snapshot_grants import (
    GrantScope,
    Phase6SnapshotGrantStore,
    ShadowAccessProof,
    verify_shadow_access_proof,
)
from .phase78_config import Phase78Settings
from .phase78_deadline import (
    Phase78CancellationError,
    Phase78DeadlineError,
    TotalDeadline,
)


class Phase78CurrentHeadError(RuntimeError):
    code = "PHASE78_CURRENT_HEAD_MISMATCH"


def _state(value: object) -> AuthorityPhase3ArtifactState:
    try:
        if type(value) is AuthorityPhase3ArtifactState:
            return validate_authority_phase3_artifact_state(value)
        return authority_phase3_artifact_state_from_dict(value)
    except Exception as exc:
        raise Phase78CurrentHeadError(
            "Phase 3 artifact state does not revalidate"
        ) from exc


def _occurrence(value: object) -> ArtifactLedgerOccurrence:
    try:
        if type(value) is ArtifactLedgerOccurrence:
            return validate_artifact_occurrence(value)
        return artifact_occurrence_from_dict(value)
    except Exception as exc:
        raise Phase78CurrentHeadError(
            "Phase 3 artifact occurrence does not revalidate"
        ) from exc


@dataclass(frozen=True, slots=True)
class Phase78CurrentFacts:
    coordinate: AuthorityWorkflowCoordinate
    artifact_state: AuthorityPhase3ArtifactState
    artifact_occurrence: ArtifactLedgerOccurrence
    access_proof: ShadowAccessProof


class Phase78CurrentHeadVerifier:
    """Re-read both independent heads before publish and again on replay."""

    def __init__(self, settings: Phase78Settings) -> None:
        if not settings.enabled:
            raise Phase78CurrentHeadError("Phase 7+8 shadow pipeline is disabled")
        self._settings = settings

    def verify(
        self,
        *,
        phase3_artifact_state: object,
        phase3_artifact_occurrence: object,
        phase6_access_proof: object,
        deadline: TotalDeadline | object | None = None,
    ) -> Phase78CurrentFacts:
        if deadline is not None:
            deadline.check()
        supplied_state = _state(phase3_artifact_state)
        supplied_occurrence = _occurrence(phase3_artifact_occurrence)
        try:
            supplied_proof = verify_shadow_access_proof(phase6_access_proof)
        except (Phase78CancellationError, Phase78DeadlineError):
            raise
        except Exception as exc:
            raise Phase78CurrentHeadError(
                "Phase 6 access proof does not revalidate"
            ) from exc
        source = supplied_proof.source_binding
        authority_wire = source.authority_coordinate
        workflow_id = authority_wire["workflow_id"]
        try:
            repository = AuthorityReadRepository(
                self._settings.required_path("authority_database"),
                expected_source_fence_sha256=(
                    self._settings.authority_source_fence_sha256 or ""
                ),
                deadline=deadline,
            )
            live_coordinate = repository.workflow_coordinate(workflow_id)
            live_state = repository.phase3_artifact_state(
                workflow_id,
                through_revision=live_coordinate.current_revision,
            )
            live_proof = Phase6SnapshotGrantStore(
                self._settings.required_path("phase6_database")
            ).verify_current_access_proof(supplied_proof, deadline=deadline)
        except (Phase78CancellationError, Phase78DeadlineError):
            raise
        except Exception as exc:
            raise Phase78CurrentHeadError(
                "Phase 3/6 current heads cannot be verified"
            ) from exc
        if deadline is not None:
            deadline.check()
        if live_coordinate.as_dict() != authority_wire:
            raise Phase78CurrentHeadError("Authority coordinate advanced or differs")
        if (
            supplied_state != live_state
            or supplied_state.state_sha256 != source.phase3_artifact_state_sha256
            or supplied_state.workflow_id != live_coordinate.workflow_id
            or supplied_state.through_revision != live_coordinate.current_revision
        ):
            raise Phase78CurrentHeadError("Phase 3 artifact state is not current")
        matching = tuple(
            item
            for item in live_state.occurrences
            if item.normalized_path == supplied_occurrence.normalized_path
        )
        if matching != (supplied_occurrence,):
            raise Phase78CurrentHeadError(
                "Phase 3 artifact occurrence is not the current path occurrence"
            )
        if live_proof != supplied_proof:
            raise Phase78CurrentHeadError("Phase 6 access proof is not current")
        return Phase78CurrentFacts(
            live_coordinate,
            live_state,
            supplied_occurrence,
            live_proof,
        )

    def fence(
        self,
        *,
        phase3_artifact_state: object,
        phase3_artifact_occurrence: object,
        phase6_access_proof: object,
        deadline: TotalDeadline | object | None = None,
    ):
        """Return a stage callback suitable for both durable stores."""

        def verify_at_stage(_stage: str) -> None:
            self.verify(
                phase3_artifact_state=phase3_artifact_state,
                phase3_artifact_occurrence=phase3_artifact_occurrence,
                phase6_access_proof=phase6_access_proof,
                deadline=deadline,
            )

        return verify_at_stage

    def current_callback(self, *, deadline: TotalDeadline | object | None = None):
        """Return the exact three-argument callback required by durable stores."""

        def verify_current(state: object, occurrence: object, proof: object) -> bool:
            self.verify(
                phase3_artifact_state=state,
                phase3_artifact_occurrence=occurrence,
                phase6_access_proof=proof,
                deadline=deadline,
            )
            return True

        return verify_current
