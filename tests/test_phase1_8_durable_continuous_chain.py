"""Focused durable E2E matrix for the audited Phase-1--8 source chain.

These tests intentionally use the real Authority, Phase-4, Phase-5 and
Phase-6 stores.  Direct adapters, caller-supplied placeholder hashes and SQL
generation completion are not used to construct a passing chain.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from factory_core.durable_operation import OperationEvent, build_worker_launch_identity
from factory_core.artifact_ownership import ArtifactOwnership
from factory_core.authority_read_repository import AuthorityReadRepository
from factory_core.authority_production_writer import AuthorityProductionWriter
from factory_core.owner_compiler import compile_owner_registry
from factory_core.phase3_artifacts import (
    CheckpointState,
    CheckpointTransition,
    build_artifact_manifest,
    build_artifact_record,
    build_checkpoint_entry,
    build_phase3_mutation,
    build_phase3_previous_head_continuation,
    build_reopen_plan,
    compute_change_set,
    owner_compilation_semantic_sha256,
    register_artifact_owner,
)
from factory_core.phase4_shadow_runtime import Phase4ShadowStore
from factory_core.phase5_shadow_supervisor import (
    PauseMode,
    Phase5SupervisorStore,
)
from factory_core.phase6_snapshot_grants import (
    GrantScope,
    Phase6IdempotencyConflict,
    Phase6SnapshotGrantStore,
    SectionAvailability,
    VerifiedSection,
    build_authority_source_binding,
)
from factory_core.phase6_source_assembler import (
    Phase6TrustedSourceAssembler,
    TRUSTED_SOURCE_CHAIN_SCHEMA,
    TrustedSourceChainError,
    trusted_source_chain_receipt_from_dict,
)
from factory_core.phase78_config import Phase78Settings
from factory_core.phase78_current import (
    Phase78CurrentHeadError,
    Phase78CurrentHeadVerifier,
)
from tests.test_phase78_enabled_e2e import _authority_pdf, _phase6_proof, _settings
from tests.test_phase8_reference_materializer import make_pdf
from tests.support.authority_production import bundle


@pytest.fixture
def durable_chain(tmp_path: Path):
    raw = make_pdf("Phase 1-8 durable continuous-chain focused fixture")
    fixture, coordinate, state, occurrence, _source = _authority_pdf(tmp_path, raw)
    phase6, proof = _phase6_proof(tmp_path, coordinate, state)
    settings = _settings(fixture, tmp_path, phase6.path)
    receipt = trusted_source_chain_receipt_from_dict(
        proof.source_binding.trusted_source_chain_receipt
    )
    assembler = Phase6TrustedSourceAssembler(
        authority_database=fixture.database,
        authority_source_fence_sha256=fixture.preflight.source_fence_sha256,
        phase4_database=settings.required_path("phase4_database"),
        phase5_database=settings.required_path("phase5_database"),
        phase6_store=phase6,
    )
    return SimpleNamespace(
        raw=raw,
        fixture=fixture,
        coordinate=coordinate,
        state=state,
        occurrence=occurrence,
        phase6=phase6,
        proof=proof,
        receipt=receipt,
        assembler=assembler,
        settings=settings,
        tmp_path=tmp_path,
    )


def _append_reference_revision(chain, *, raw: bytes, suffix: str):
    """Append a real Phase-3 revision using the current durable head."""

    repository = AuthorityReadRepository(
        chain.fixture.database,
        expected_source_fence_sha256=chain.fixture.preflight.source_fence_sha256,
    )
    coordinate = repository.workflow_coordinate("legacy_current")
    previous_bundle = repository.revision_command_identity(
        "legacy_current",
        coordinate.current_revision,
    )
    durable_bundle = repository.command_bundle(
        workflow_id="legacy_current",
        idempotency_key=(
            "idempotency-phase78-e2e"
            if suffix == "chain-b"
            else "idempotency-chain-b"
        ),
    )
    previous_record = durable_bundle.phase3_mutation.artifact_records[0]
    previous_checkpoint = durable_bundle.phase3_mutation.checkpoint_entries[0]
    previous_occurrence = durable_bundle.phase3_artifact_occurrences[0]
    previous_checkpoint_occurrence = durable_bundle.phase3_checkpoint_occurrences[0]

    compilation = compile_owner_registry(
        (
            ArtifactOwnership(
                pattern="references/**",
                owner_stage=4,
                semantic_domain="canonical_reference",
                dirty_flag="REFERENCE_DIRTY",
            ),
        )
    )
    record = build_artifact_record(
        register_artifact_owner(compilation, "references/source.pdf"),
        content=raw,
    )
    owner_sha = owner_compilation_semantic_sha256(compilation)
    previous_manifest = build_artifact_manifest(
        owner_compilation_sha256=owner_sha,
        records=(previous_record,),
    )
    current_manifest = build_artifact_manifest(
        owner_compilation_sha256=owner_sha,
        records=(record,),
    )
    changes = compute_change_set(previous_manifest, current_manifest)
    if previous_checkpoint.state is CheckpointState.VALID:
        checkpoint_state = CheckpointState.INVALID
        checkpoint_transition = CheckpointTransition.INVALIDATED
        validation_sha256 = None
        reason_code = "INPUT_CHANGED"
    else:
        checkpoint_state = CheckpointState.VALID
        checkpoint_transition = CheckpointTransition.REATTESTED_VALID
        validation_sha256 = "9" * 64
        reason_code = "VALIDATOR_PASS"
    checkpoint = build_checkpoint_entry(
        checkpoint_key=previous_checkpoint.checkpoint_key,
        owner_stage=4,
        input_manifest_sha256=current_manifest.manifest_sha256,
        state=checkpoint_state,
        transition=checkpoint_transition,
        validation_sha256=validation_sha256,
        previous_checkpoint_id=previous_checkpoint.checkpoint_id,
        previous_checkpoint_occurrence_id=(
            previous_checkpoint_occurrence.occurrence_id
        ),
        reason_code=reason_code,
    )
    mutation = build_phase3_mutation(
        artifact_records=current_manifest.records,
        artifact_blockers=(),
        removals=changes.removals,
        checkpoint_entries=(checkpoint,),
        reopen_plan=build_reopen_plan(
            workflow_id="legacy_current",
            source_revision=coordinate.current_revision,
            change_set=changes,
            previous_manifest=previous_manifest,
            previous_occurrence_ids={
                previous_record.normalized_path: previous_occurrence.occurrence_id
            },
        ),
        previous_manifest=previous_manifest,
        current_manifest=current_manifest,
        change_set=changes,
        blocked_disposition=None,
        previous_head=build_phase3_previous_head_continuation(
            workflow_id="legacy_current",
            source_revision=coordinate.current_revision,
            previous_manifest=previous_manifest,
            previous_revision=previous_bundle.revision,
            previous_command_id=previous_bundle.command_id,
            previous_mutation_sha256=previous_bundle.phase3_mutation_sha256,
        ),
    )
    command, event, receipt, outbox = bundle(
        requested_revision=coordinate.current_revision,
        suffix=suffix,
    )
    from tests.test_phase9_run_generation import _request as generation_request

    generation_pins = generation_request(key=f"phase3-pins-{suffix}").contract_pins
    from factory_core.canonical import canonical_sha256

    generation_pin_sha256 = canonical_sha256(generation_pins)
    command = replace(
        command,
        project_binding=replace(
            command.project_binding,
            project_generation=coordinate.project_generation,
        ),
        run_binding=replace(
            command.run_binding,
            run_generation=coordinate.run_generation,
            runtime_generation=coordinate.runtime_generation,
            scheduler_generation=coordinate.scheduler_generation,
        ),
        contract_pins=generation_pins,
    )
    event = replace(
        event,
        project_generation=coordinate.project_generation,
        run_generation=coordinate.run_generation,
        runtime_generation=coordinate.runtime_generation,
        scheduler_generation=coordinate.scheduler_generation,
        contract_pin_set_sha256=generation_pin_sha256,
    )
    receipt = replace(
        receipt,
        contract_pin_set_sha256=generation_pin_sha256,
    )
    AuthorityProductionWriter(
        chain.fixture.database,
        writer_id="writer-a",
        writer_epoch=1,
        expected_source_fence_sha256=(
            chain.fixture.preflight.source_fence_sha256
        ),
    ).persist_command_bundle(
        workflow_id="legacy_current",
        idempotency_key=f"idempotency-{suffix}",
        command=command,
        event=event,
        receipt=receipt,
        outbox=outbox,
        occurred_at=3000 if suffix == "chain-b" else 4000,
        phase3_mutation=mutation,
    )
    new_coordinate = repository.workflow_coordinate("legacy_current")
    new_state = repository.phase3_artifact_state(
        "legacy_current",
        through_revision=new_coordinate.current_revision,
    )
    return new_state.occurrences[0]


def test_first_success_restart_exact_replay_and_different_bytes_conflict(
    durable_chain,
) -> None:
    chain = durable_chain
    facts = Phase78CurrentHeadVerifier(chain.settings).verify(
        phase3_artifact_state=chain.state,
        phase3_artifact_occurrence=chain.occurrence,
        phase6_access_proof=chain.proof,
    )
    assert facts.access_proof == chain.proof
    assert chain.receipt.as_dict()["schema_version"] == TRUSTED_SOURCE_CHAIN_SCHEMA
    assert chain.receipt.as_dict()["authoritative"] is False
    generation = chain.receipt.run_generation_identity
    assert generation.delivery_capability == "DISABLED"
    assert generation.run_generation == chain.coordinate.run_generation
    assert generation.contract_pin_set_sha256 == (
        chain.coordinate.contract_pin_set_sha256
    )
    source = chain.receipt.phase4_state.source_chain_binding
    assert source is not None
    assert (source.source_commit, source.source_tree, source.source_parent) == (
        generation.source_commit,
        generation.source_tree,
        generation.source_parent,
    )
    assert source.run_generation_creation_receipt_sha256 == (
        generation.creation_receipt_sha256
    )

    restarted = Phase6SnapshotGrantStore(chain.phase6.path)
    assert restarted.verify_current_access_proof(chain.proof) == chain.proof
    snapshot = chain.proof.snapshot
    replay = restarted.append_snapshot(
        source_binding=snapshot.source_binding,
        sections=snapshot.sections,
        captured_at=snapshot.captured_at,
        valid_until=snapshot.valid_until,
        expected_previous_snapshot_id=snapshot.previous_snapshot_id,
        idempotency_key="phase6-snapshot-phase78-e2e",
    )
    assert replay.replayed is True
    assert replay.snapshot == snapshot

    changed_sections = (
        VerifiedSection(
            "reference",
            SectionAvailability.AVAILABLE,
            "f" * 64,
            "reference-section-v1",
        ),
    )
    with pytest.raises(Phase6IdempotencyConflict):
        restarted.append_snapshot(
            source_binding=snapshot.source_binding,
            sections=changed_sections,
            captured_at=snapshot.captured_at,
            valid_until=snapshot.valid_until,
            expected_previous_snapshot_id=snapshot.previous_snapshot_id,
            idempotency_key="phase6-snapshot-phase78-e2e",
        )


@pytest.mark.parametrize("missing", ["phase4_database", "phase5_database"])
def test_missing_phase4_or_phase5_durable_current_fails_closed(
    durable_chain,
    missing: str,
) -> None:
    chain = durable_chain
    absent = chain.tmp_path / f"absent-{missing}.db"
    settings = replace(chain.settings, **{missing: absent})
    with pytest.raises(Phase78CurrentHeadError):
        Phase78CurrentHeadVerifier(settings).verify(
            phase3_artifact_state=chain.state,
            phase3_artifact_occurrence=chain.occurrence,
            phase6_access_proof=chain.proof,
        )
    assert not absent.exists()


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("project_generation", "project-generation-mismatch"),
        ("run_generation", "run-generation-mismatch"),
        ("runtime_generation", "runtime-generation-mismatch"),
        ("scheduler_generation", "scheduler-generation-mismatch"),
    ],
)
def test_each_generation_mismatch_cannot_select_a_phase4_source(
    durable_chain,
    field: str,
    replacement: str,
) -> None:
    """A typed but differently coordinated P4 state is never caller-selectable."""

    chain = durable_chain
    valid = chain.receipt.phase4_state.source_chain_binding
    assert valid is not None
    mismatched = replace(valid, **{field: replacement})
    identity = build_worker_launch_identity(
        outbox_command_id=mismatched.authority_outbox_message_id,
        invocation_id=f"mismatch-{field}",
        attempt_id="mismatch-attempt",
        process_scope_id="mismatch-scope",
        payload_sha256=mismatched.phase3_current_graph_sha256,
    )
    bad = Phase4ShadowStore(chain.settings.required_path("phase4_database"))
    reserved = bad.reserve_operation(
        identity,
        occurred_at=100,
        source_chain_binding=mismatched,
    )
    with pytest.raises(TrustedSourceChainError):
        chain.assembler.assemble(
            workflow_id=chain.coordinate.workflow_id,
            occurrence_id=chain.occurrence.occurrence_id,
            operation_identity_sha256=(
                reserved.state.operation.identity.identity_sha256
            ),
            phase5_request_id=chain.receipt.phase5_state.request_id,
        )


def test_stale_phase4_current_head_is_rejected_after_same_source_supersession(
    durable_chain,
) -> None:
    chain = durable_chain
    source = chain.receipt.phase4_state.source_chain_binding
    assert source is not None
    identity = build_worker_launch_identity(
        outbox_command_id=source.authority_outbox_message_id,
        invocation_id="superseding-invocation",
        attempt_id="superseding-attempt",
        process_scope_id="superseding-scope",
        payload_sha256=source.phase3_current_graph_sha256,
    )
    Phase4ShadowStore(chain.settings.required_path("phase4_database")).reserve_operation(
        identity,
        occurred_at=100,
        source_chain_binding=source,
    )
    with pytest.raises(Exception, match="exactly one durable P4 current head"):
        chain.assembler.verify_current(chain.receipt)


def test_stale_phase5_current_head_is_rejected_after_same_operation_reissue(
    durable_chain,
) -> None:
    chain = durable_chain
    binding = chain.receipt.phase5_state.binding
    Phase5SupervisorStore(
        chain.settings.required_path("phase5_database")
    ).request_pause(
        binding,
        PauseMode.PAUSE,
        request_idempotency_key="phase5-superseding-request",
        occurred_at=100,
    )
    with pytest.raises(Exception, match="unavailable or ambiguous"):
        chain.assembler.verify_current(chain.receipt)


def test_wrong_revision_or_phase6_head_is_rejected(durable_chain) -> None:
    chain = durable_chain
    wrong_occurrence = replace(
        chain.occurrence,
        revision=chain.occurrence.revision + 1,
    )
    with pytest.raises(Phase78CurrentHeadError):
        Phase78CurrentHeadVerifier(chain.settings).verify(
            phase3_artifact_state=chain.state,
            phase3_artifact_occurrence=wrong_occurrence,
            phase6_access_proof=chain.proof,
        )

    chain.phase6.revoke_grant(
        chain.proof.grant.grant_id,
        actor_id="phase6-local-shadow-issuer",
        actor_generation="issuer-generation-1",
        reason_code="FOCUSED_STALE_HEAD",
        effective_at=20,
        idempotency_key="phase6-focused-stale-head",
    )
    with pytest.raises(Phase78CurrentHeadError):
        Phase78CurrentHeadVerifier(chain.settings).verify(
            phase3_artifact_state=chain.state,
            phase3_artifact_occurrence=chain.occurrence,
            phase6_access_proof=chain.proof,
        )


def test_semantically_equal_a_b_a_occurrence_requires_the_new_current_head(
    durable_chain,
) -> None:
    chain = durable_chain
    original = chain.occurrence
    middle = _append_reference_revision(
        chain,
        raw=make_pdf("Phase 1-8 semantic B"),
        suffix="chain-b",
    )
    current = _append_reference_revision(
        chain,
        raw=chain.raw,
        suffix="chain-a2",
    )
    assert middle.occurrence_id != original.occurrence_id
    assert current.occurrence_id != original.occurrence_id
    assert current.semantic_sha256 == original.semantic_sha256

    with pytest.raises(Exception, match="exact current path head"):
        chain.assembler.prepare_phase4_binding(
            workflow_id=chain.coordinate.workflow_id,
            occurrence_id=original.occurrence_id,
        )
    selected = chain.assembler.prepare_phase4_binding(
        workflow_id=chain.coordinate.workflow_id,
        occurrence_id=current.occurrence_id,
    )
    assert selected.selected_occurrence_id == current.occurrence_id


def test_partial_phase3_graph_fault_injection_cannot_produce_a_receipt(
    durable_chain,
) -> None:
    chain = durable_chain
    # Negative-only corruption injection: this never creates a generation or
    # passing receipt.  It proves the supported compound reader detects a
    # missing committed P3 graph member rather than projecting a partial head.
    connection = sqlite3.connect(chain.fixture.database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DROP TRIGGER authority_artifact_records_append_only_delete"
        )
        connection.execute(
            "DELETE FROM authority_artifact_records WHERE artifact_record_id=?",
            (chain.occurrence.artifact_record.artifact_record_id,),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(Exception):
        chain.assembler.prepare_phase4_binding(
            workflow_id=chain.coordinate.workflow_id,
            occurrence_id=chain.occurrence.occurrence_id,
        )


def test_cancel_requested_phase4_and_superseded_source_are_ineligible(
    durable_chain,
) -> None:
    chain = durable_chain
    phase4_path = chain.tmp_path / "cancelled-phase4.db"
    phase5_path = chain.tmp_path / "cancelled-phase5.db"
    phase6_path = chain.tmp_path / "cancelled-phase6.db"
    phase4 = Phase4ShadowStore(phase4_path)
    phase5 = Phase5SupervisorStore(phase5_path)
    phase6 = Phase6SnapshotGrantStore(phase6_path)
    phase4.initialize()
    phase5.initialize()
    phase6.initialize()
    assembler = Phase6TrustedSourceAssembler(
        authority_database=chain.fixture.database,
        authority_source_fence_sha256=(
            chain.fixture.preflight.source_fence_sha256
        ),
        phase4_database=phase4_path,
        phase5_database=phase5_path,
        phase6_store=phase6,
    )
    reserved = assembler.produce_phase4_operation(
        workflow_id=chain.coordinate.workflow_id,
        occurrence_id=chain.occurrence.occurrence_id,
        invocation_id="cancelled-invocation",
        attempt_id="cancelled-attempt",
        process_scope_id="cancelled-scope",
        occurred_at=50,
    )
    operation_id = reserved.state.operation.identity.identity_sha256
    claimed = phase4.claim_operation(
        operation_id,
        request_idempotency_key="cancelled-claim",
        claim_owner_id="cancelled-owner",
        claim_owner_epoch=1,
        expected_claim_generation=0,
        occurred_at=51,
        lease_seconds=30,
    )
    checkpoint = phase4.transition(
        operation_id,
        OperationEvent.CHECKPOINT_DISPATCH,
        request_idempotency_key="cancelled-checkpoint",
        expected_claim_generation=claimed.state.operation.claim_generation,
        claim_owner_id="cancelled-owner",
        claim_owner_epoch=1,
        dispatch_nonce="cancelled-nonce",
        reason_code="SHADOW_NO_DISPATCH",
        occurred_at=52,
    )
    cancelled = phase4.transition(
        operation_id,
        OperationEvent.REQUEST_CANCEL,
        request_idempotency_key="cancelled-request",
        expected_claim_generation=checkpoint.state.operation.claim_generation,
        claim_owner_id="cancelled-owner",
        claim_owner_epoch=1,
        dispatch_nonce="cancelled-nonce",
        reason_code="SUPERSEDED",
        occurred_at=53,
    )
    assert cancelled.state.operation.status.value == "cancel-requested"
    with pytest.raises(TrustedSourceChainError):
        assembler.assemble(
            workflow_id=chain.coordinate.workflow_id,
            occurrence_id=chain.occurrence.occurrence_id,
            operation_identity_sha256=operation_id,
            phase5_request_id="missing-cancelled-supervisor",
        )


def test_receiptless_raw_phase6_proof_is_never_phase7_or_phase8_eligible(
    durable_chain,
) -> None:
    chain = durable_chain
    source = chain.proof.source_binding
    raw_binding = build_authority_source_binding(
        authority_coordinate=source.authority_coordinate,
        authority_coordinate_sha256=source.authority_coordinate_sha256,
        authority_revision_snapshot_sha256=(
            source.authority_revision_snapshot_sha256
        ),
        authority_revision_through_revision=(
            source.authority_coordinate["current_revision"]
        ),
        source_snapshot_schema=source.source_snapshot_schema,
        source_snapshot_semantic_sha256=source.source_snapshot_semantic_sha256,
        source_snapshot_completeness=source.source_snapshot_completeness,
        source_snapshot_coordinate=source.source_snapshot_coordinate,
        phase3_artifact_state_sha256=source.phase3_artifact_state_sha256,
        phase4_operation_state_sha256=source.phase4_operation_state_sha256,
        phase5_supervisor_state_sha256=source.phase5_supervisor_state_sha256,
        trusted_source_chain_receipt=None,
    )
    raw_store = Phase6SnapshotGrantStore(chain.tmp_path / "raw-phase6.db")
    raw_store.initialize()
    snapshot = raw_store.append_snapshot(
        source_binding=raw_binding,
        sections=chain.proof.snapshot.sections,
        captured_at=30,
        valid_until=1000,
        expected_previous_snapshot_id=None,
        idempotency_key="raw-snapshot",
    ).snapshot
    grant = raw_store.issue_grant(
        snapshot_id=snapshot.snapshot_id,
        subject_type="user",
        subject_id="alice",
        subject_generation="membership-generation-1",
        scope=GrantScope.SNAPSHOT_VIEW,
        scope_key=None,
        issuer_id="phase6-local-shadow-issuer",
        issuer_generation="issuer-generation-1",
        issuer_evidence_schema="local-issuer-receipt-v1",
        issuer_receipt_sha256="2" * 64,
        issued_at=31,
        not_before=32,
        expires_at=900,
        expected_previous_grant_id=None,
        idempotency_key="raw-grant",
    ).grant
    evaluated = raw_store.evaluate_grant(
        grant.grant_id,
        subject_type="user",
        subject_id="alice",
        subject_generation="membership-generation-1",
        requested_scope=GrantScope.SNAPSHOT_VIEW,
        requested_scope_key=None,
        evaluated_at=32,
        idempotency_key="raw-evaluation",
    )
    assert evaluated.access_proof is not None
    raw_settings = replace(chain.settings, phase6_database=raw_store.path)
    with pytest.raises(Phase78CurrentHeadError):
        Phase78CurrentHeadVerifier(raw_settings).verify(
            phase3_artifact_state=chain.state,
            phase3_artifact_occurrence=chain.occurrence,
            phase6_access_proof=evaluated.access_proof,
        )


def test_default_off_opens_no_phase4_phase5_or_phase6_resource(tmp_path: Path) -> None:
    poison = tmp_path / "must-remain-absent"
    settings = Phase78Settings(
        enabled=False,
        phase4_database=poison / "phase4.db",
        phase5_database=poison / "phase5.db",
        phase6_database=poison / "phase6.db",
    )
    with pytest.raises(Phase78CurrentHeadError, match="disabled"):
        Phase78CurrentHeadVerifier(settings)
    assert not poison.exists()
