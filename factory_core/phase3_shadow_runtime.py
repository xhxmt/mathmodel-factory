"""Explicit, default-off, read-only Phase-3 full-shadow runner."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from .canonical import canonical_sha256
from .owner_compiler import OwnerCompilation
from .phase3_artifacts import (
    ArtifactManifest,
    ArtifactRemoval,
    BlockedNoReopenDisposition,
    ChangeSet,
    CheckpointLedgerEntry,
    CheckpointReattestationReceipt,
    OwnerPolicyDisposition,
    ParityReceipt,
    ReopenPlan,
    build_blocked_no_reopen_disposition,
    build_parity_receipt,
    build_reopen_plan,
    capture_artifact_manifest,
    classify_owner_policy,
    compute_change_set,
    dry_run_checkpoint_reattestation,
)


PHASE3_SHADOW_DEFAULT_ENABLED = False
PHASE3_SHADOW_RUN_SCHEMA = "phase3-full-shadow-run-v1"


@dataclass(frozen=True)
class Phase3ShadowRun:
    schema_version: str
    enabled: bool
    authoritative: bool
    dispatch_performed: bool
    owner_policy_disposition: OwnerPolicyDisposition
    manifest: ArtifactManifest | None
    change_set: ChangeSet | None
    reopen_plan: ReopenPlan | None
    blocked_disposition: BlockedNoReopenDisposition | None
    reattestation: CheckpointReattestationReceipt | None
    parity: ParityReceipt | None
    run_sha256: str


def _run_identity(value: Phase3ShadowRun) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "enabled": value.enabled,
        "authoritative": value.authoritative,
        "dispatch_performed": value.dispatch_performed,
        "owner_policy_disposition": value.owner_policy_disposition.value,
        "manifest_sha256": value.manifest.manifest_sha256 if value.manifest else None,
        "change_set_sha256": value.change_set.change_set_sha256 if value.change_set else None,
        "reopen_plan_sha256": value.reopen_plan.plan_sha256 if value.reopen_plan else None,
        "blocked_disposition_sha256": (
            value.blocked_disposition.disposition_sha256
            if value.blocked_disposition
            else None
        ),
        "reattestation_sha256": value.reattestation.receipt_sha256 if value.reattestation else None,
        "parity_sha256": value.parity.receipt_sha256 if value.parity else None,
    }


def run_phase3_full_shadow(
    *,
    enabled: bool = PHASE3_SHADOW_DEFAULT_ENABLED,
    project_root: str | Path | None = None,
    compilation: OwnerCompilation | None = None,
    paths: Iterable[str] = (),
    removals: Iterable[ArtifactRemoval] = (),
    previous_manifest: ArtifactManifest | None = None,
    workflow_id: str | None = None,
    source_revision: int | None = None,
    previous_checkpoint: CheckpointLedgerEntry | None = None,
    regenerated_valid: bool = False,
    regenerated_validation_sha256: str | None = None,
    v1_manifest_sha256: str | None = None,
) -> Phase3ShadowRun:
    """Run the complete pure shadow analysis only after explicit enablement.

    The function never writes Authority or v1 state and has no dispatch callback.
    Its default-disabled path deliberately does not inspect any supplied path.
    """

    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean")
    if not enabled:
        prototype = Phase3ShadowRun(
            PHASE3_SHADOW_RUN_SCHEMA,
            False,
            False,
            False,
            OwnerPolicyDisposition.NOT_EVALUATED,
            None,
            None,
            None,
            None,
            None,
            None,
            "0" * 64,
        )
        return replace(prototype, run_sha256=canonical_sha256(_run_identity(prototype)))
    if project_root is None or compilation is None:
        raise ValueError("enabled shadow run requires project_root and compilation")
    manifest = capture_artifact_manifest(project_root, compilation, paths)
    owner_policy_disposition = OwnerPolicyDisposition.NOT_EVALUATED
    change_set = None
    if previous_manifest is not None:
        owner_policy_disposition = classify_owner_policy(previous_manifest, manifest)
        if owner_policy_disposition is OwnerPolicyDisposition.UNCHANGED:
            change_set = compute_change_set(
                previous_manifest,
                manifest,
                removals=removals,
            )
    reopen_plan = None
    blocked_disposition = None
    if (
        change_set is not None
        and any(
            decision.disposition.value == "BLOCKED"
            for decision in change_set.dirty_decisions
        )
    ):
        if workflow_id is not None and source_revision is not None:
            blocked_disposition = build_blocked_no_reopen_disposition(
                workflow_id=workflow_id,
                source_revision=source_revision,
                change_set=change_set,
                previous_manifest=previous_manifest,
                current_manifest=manifest,
            )
    elif (
        change_set is not None
        and not any(
            decision.disposition.value == "BLOCKED"
            for decision in change_set.dirty_decisions
        )
        and any(
            decision.disposition.value == "DIRTY"
            for decision in change_set.dirty_decisions
        )
    ):
        if workflow_id is None or source_revision is None:
            raise ValueError("dirty shadow run requires workflow_id and source_revision")
        reopen_plan = build_reopen_plan(
            workflow_id=workflow_id,
            source_revision=source_revision,
            change_set=change_set,
            previous_manifest=previous_manifest,
        )
    reattestation = (
        dry_run_checkpoint_reattestation(
            previous_checkpoint,
            current_manifest_sha256=manifest.manifest_sha256,
            regenerated_valid=regenerated_valid,
            regenerated_validation_sha256=regenerated_validation_sha256,
        )
        if previous_checkpoint is not None
        else None
    )
    parity = build_parity_receipt(
        subject="phase3-artifact-manifest",
        v1_sha256=v1_manifest_sha256,
        shadow_sha256=manifest.manifest_sha256,
        blocker_codes=(blocker.code.value for blocker in manifest.blockers),
    )
    prototype = Phase3ShadowRun(
        PHASE3_SHADOW_RUN_SCHEMA,
        True,
        False,
        False,
        owner_policy_disposition,
        manifest,
        change_set,
        reopen_plan,
        blocked_disposition,
        reattestation,
        parity,
        "0" * 64,
    )
    return replace(prototype, run_sha256=canonical_sha256(_run_identity(prototype)))
