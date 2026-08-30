from __future__ import annotations

from dataclasses import replace
import json
import sqlite3

import pytest

import factory_core.authority_production_writer as writer_module
from factory_core.authority_operations import CANARY, AuthorityOperations
from factory_core.authority_production_writer import AuthorityProductionWriter
from factory_core.authority_production_schema import AuthorityProductionSchemaError
from factory_core.authority_read_repository import (
    AuthorityReadError,
    AuthorityReadRepository,
)
from factory_core.authority_repository import (
    AuthorityEnvelopePersistenceError,
    AuthorityRevisionConflict,
)
from factory_core.artifact_ownership import ArtifactOwnership
from factory_core.owner_compiler import compile_owner_registry
from factory_core.phase3_artifacts import (
    ArtifactBlocker,
    ArtifactBlockerCode,
    CheckpointState,
    CheckpointTransition,
    build_artifact_manifest,
    build_artifact_owner_operator_claim,
    build_artifact_owner_operator_authorization,
    build_checkpoint_entry,
    owner_compilation_semantic_sha256,
)
from tests.support.authority_production import bundle, install_foundation
from tests.test_phase3_authority_writer import (
    PHASE3_TABLES,
    _compilation,
    _full_mutation,
    _persist,
    table_counts,
)


def _claim(
    *,
    command_suffix: str = "authorized",
    workflow_id: str = "legacy_current",
    source_revision: int = 1,
    path: str = "unowned/a.json",
    compilation=None,
    owner_stage: int = 4,
    dirty_flag: str = "RESULT_DIRTY",
):
    return build_artifact_owner_operator_claim(
        workflow_id=workflow_id,
        source_revision=source_revision,
        command_id=f"command-{command_suffix}",
        normalized_path=path,
        owner_compilation=compilation or _compilation(),
        owner_id=f"owner:stage:{owner_stage}",
        owner_stage=owner_stage,
        dirty_flag=dirty_flag,
        operator_subject="operator:owner-resolution",
        reason_code="OWNER_RESOLUTION_FAILURE",
    )


def _configure_with_claim(fixture, claim):
    operations = AuthorityOperations(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    control = operations.configure_writer(
        new_writer_id="writer-a",
        enabled=True,
        expected_writer_epoch=0,
        expected_switch_epoch=0,
        operator_subject=claim.operator_subject,
        reason="test owner authorization issuer",
        occurred_at=1100,
        phase3_owner_claims=(claim,),
    )
    operations.configure_consumer(
        new_consumer_id="consumer-a",
        enabled=True,
        expected_consumer_epoch=0,
        expected_switch_epoch=0,
        operator_subject=claim.operator_subject,
        reason="test canary consumer",
        occurred_at=1101,
    )
    operations.switch_mode(
        target_mode=CANARY,
        expected_switch_epoch=0,
        operator_subject=claim.operator_subject,
        reason="test canary activation",
        occurred_at=1102,
    )
    assert control.receipt_id is not None and control.receipt_sha256 is not None
    writer = AuthorityProductionWriter(
        fixture.database,
        writer_id="writer-a",
        writer_epoch=control.writer_epoch,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    return writer, control


def _authorization(claim, control, **overrides):
    values = {
        "issuer_writer_id": "writer-a",
        "issuer_writer_epoch": control.writer_epoch,
        "issuer_receipt_id": control.receipt_id,
        "issuer_receipt_sha256": control.receipt_sha256,
    }
    values.update(overrides)
    return build_artifact_owner_operator_authorization(claim, **values)


def _mutation(authorization, *, workflow_id="legacy_current", source_revision=1):
    policy_sha256 = authorization.owner_compilation_sha256
    previous = build_artifact_manifest(
        owner_compilation_sha256=policy_sha256,
        records=(),
    )
    current = build_artifact_manifest(
        owner_compilation_sha256=policy_sha256,
        records=(),
        blockers=(
            ArtifactBlocker(
                ArtifactBlockerCode.OWNER_RESOLUTION_BLOCKED,
                authorization.normalized_path,
                "owner_resolution_blocked",
                operator_authorization=authorization,
            ),
        ),
    )
    checkpoint = build_checkpoint_entry(
        checkpoint_key=f"phase3:stage{authorization.owner_stage}.owner-resolution",
        owner_stage=authorization.owner_stage,
        input_manifest_sha256=current.manifest_sha256,
        state=CheckpointState.INVALID,
        transition=CheckpointTransition.RECORDED_INVALID,
        validation_sha256=None,
        reason_code="BLOCKED_SCAN",
    )
    return _full_mutation(
        source_revision=source_revision,
        previous_manifest=previous,
        current_manifest=current,
        checkpoint=checkpoint,
        workflow_id=workflow_id,
    )


def _assert_no_command_or_phase3_residue(database, before):
    assert table_counts(database, PHASE3_TABLES + ("authority_commands",)) == before


def test_resolvable_stage4_path_cannot_use_authorization_to_claim_stage9(tmp_path):
    fixture = install_foundation(tmp_path)
    compilation = _compilation()
    policy_sha256 = owner_compilation_semantic_sha256(compilation)
    previous = build_artifact_manifest(
        owner_compilation_sha256=policy_sha256,
        records=(),
    )
    claim = build_artifact_owner_operator_claim(
        workflow_id="legacy_current",
        source_revision=1,
        command_id="command-forged-stage9",
        normalized_path="results/a.json",
        owner_compilation=compilation,
        owner_id="owner:stage:9",
        owner_stage=9,
        dirty_flag="FORGED_STAGE9_DIRTY",
        operator_subject="operator:forged",
        reason_code="OWNER_RESOLUTION_OVERRIDE",
    )
    operations = AuthorityOperations(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    writer_control = operations.configure_writer(
        new_writer_id="writer-a",
        enabled=True,
        expected_writer_epoch=0,
        expected_switch_epoch=0,
        operator_subject="operator:forged",
        reason="test owner authorization issuer",
        occurred_at=1100,
        phase3_owner_claims=(claim,),
    )
    operations.configure_consumer(
        new_consumer_id="consumer-a",
        enabled=True,
        expected_consumer_epoch=0,
        expected_switch_epoch=0,
        operator_subject="operator:forged",
        reason="test canary consumer",
        occurred_at=1101,
    )
    operations.switch_mode(
        target_mode=CANARY,
        expected_switch_epoch=0,
        operator_subject="operator:forged",
        reason="test canary activation",
        occurred_at=1102,
    )
    assert writer_control.receipt_id is not None
    assert writer_control.receipt_sha256 is not None
    authorization = build_artifact_owner_operator_authorization(
        claim,
        issuer_writer_id="writer-a",
        issuer_writer_epoch=writer_control.writer_epoch,
        issuer_receipt_id=writer_control.receipt_id,
        issuer_receipt_sha256=writer_control.receipt_sha256,
    )
    writer = AuthorityProductionWriter(
        fixture.database,
        writer_id="writer-a",
        writer_epoch=writer_control.writer_epoch,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    current = build_artifact_manifest(
        owner_compilation_sha256=policy_sha256,
        records=(),
        blockers=(
            ArtifactBlocker(
                ArtifactBlockerCode.OWNER_RESOLUTION_BLOCKED,
                "results/a.json",
                "owner_resolution_blocked",
                operator_authorization=authorization,
            ),
        ),
    )
    checkpoint = build_checkpoint_entry(
        checkpoint_key="phase3:stage9.forged",
        owner_stage=9,
        input_manifest_sha256=current.manifest_sha256,
        state=CheckpointState.INVALID,
        transition=CheckpointTransition.RECORDED_INVALID,
        validation_sha256=None,
        reason_code="BLOCKED_SCAN",
    )
    mutation = _full_mutation(
        source_revision=1,
        previous_manifest=previous,
        current_manifest=current,
        checkpoint=checkpoint,
    )
    before = table_counts(fixture.database, PHASE3_TABLES)

    with pytest.raises(
        AuthorityEnvelopePersistenceError,
        match="resolves without operator authorization",
    ):
        _persist(writer, mutation, suffix="forged-stage9")

    assert table_counts(fixture.database, PHASE3_TABLES) == before


def test_legal_owner_resolution_failure_persists_restarts_and_replays(tmp_path):
    fixture = install_foundation(tmp_path)
    claim = _claim()
    writer, control = _configure_with_claim(fixture, claim)
    authorization = _authorization(claim, control)
    mutation = _mutation(authorization)

    committed = _persist(writer, mutation, suffix="authorized")
    replayed = _persist(writer, mutation, suffix="authorized")

    assert replayed.replayed is True
    assert replayed.bundle_sha256 == committed.bundle_sha256
    restarted = AuthorityReadRepository(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    bundle = restarted.command_bundle(
        workflow_id="legacy_current",
        idempotency_key="idempotency-authorized",
    )
    assert bundle.phase3_mutation == mutation
    assert bundle.phase3_artifact_occurrences[0].blocker.operator_authorization == (
        authorization
    )
    assert restarted.phase3_artifact_state("legacy_current").blockers == (
        mutation.artifact_blockers[0],
    )


@pytest.mark.parametrize(
    "variant",
    (
        "command",
        "path",
        "policy",
        "stage_owner",
        "dirty",
        "issuer",
        "issuer_epoch",
        "receipt",
        "receipt_hash",
    ),
)
def test_signed_claim_mismatch_rejects_without_residue(tmp_path, variant):
    fixture = install_foundation(tmp_path, name=f"authorization-{variant}")
    signed_claim = _claim()
    writer, control = _configure_with_claim(fixture, signed_claim)
    candidate = signed_claim
    authorization_overrides = {}
    command_suffix = "authorized"
    if variant == "command":
        command_suffix = "other-command"
    elif variant == "path":
        candidate = _claim(path="unowned/other.json")
    elif variant == "policy":
        candidate = _claim(
            compilation=compile_owner_registry(
                (ArtifactOwnership("other/**", 4, "other", "RESULT_DIRTY"),)
            )
        )
    elif variant == "stage_owner":
        candidate = _claim(owner_stage=9)
    elif variant == "dirty":
        candidate = _claim(dirty_flag="FORGED_DIRTY")
    elif variant == "issuer":
        authorization_overrides["issuer_writer_id"] = "writer-forged"
    elif variant == "issuer_epoch":
        authorization_overrides["issuer_writer_epoch"] = 2
    elif variant == "receipt":
        authorization_overrides["issuer_receipt_id"] = "control:missing"
    elif variant == "receipt_hash":
        authorization_overrides["issuer_receipt_sha256"] = "f" * 64
    authorization = _authorization(
        candidate,
        control,
        **authorization_overrides,
    )
    mutation = _mutation(authorization)
    before = table_counts(fixture.database, PHASE3_TABLES + ("authority_commands",))

    with pytest.raises(
        (AuthorityEnvelopePersistenceError, AuthorityRevisionConflict)
    ):
        _persist(writer, mutation, suffix=command_suffix)

    _assert_no_command_or_phase3_residue(fixture.database, before)


def test_stale_source_revision_authorization_rejects_without_residue(tmp_path):
    fixture = install_foundation(tmp_path)
    claim = _claim(source_revision=1)
    writer, control = _configure_with_claim(fixture, claim)
    authorization = _authorization(claim, control)
    from tests.support.authority_production import persist_one

    persist_one(writer, suffix="advance-v1")
    mutation = _mutation(authorization, source_revision=2)
    before = table_counts(fixture.database, PHASE3_TABLES + ("authority_commands",))

    with pytest.raises(AuthorityRevisionConflict, match="coordinate differs"):
        _persist(
            writer,
            mutation,
            requested_revision=2,
            suffix="authorized",
        )

    _assert_no_command_or_phase3_residue(fixture.database, before)


def test_same_authorization_cannot_cross_workflow(tmp_path):
    fixture = install_foundation(tmp_path)
    claim = _claim()
    writer, control = _configure_with_claim(fixture, claim)
    authorization = _authorization(claim, control)
    connection = sqlite3.connect(fixture.database)
    try:
        connection.execute(
            """
            INSERT INTO authority_workflows(
                workflow_id, project_id, project_generation, run_generation,
                runtime_generation, scheduler_generation, current_revision,
                current_revision_availability, contract_pin_set_sha256,
                contract_pin_availability, authority_state
            )
            SELECT 'workflow-2', 'demo-2', project_generation, run_generation,
                   runtime_generation, scheduler_generation, 1,
                   'RECORDED', NULL, 'legacy_unknown', authority_state
            FROM authority_workflows WHERE workflow_id='legacy_current'
            """
        )
        connection.execute(
            "INSERT INTO authority_revision_allocator(workflow_id, next_revision) "
            "VALUES ('workflow-2', 2)"
        )
        connection.commit()
    finally:
        connection.close()
    mutation = _mutation(authorization, workflow_id="workflow-2")
    command, event, receipt, outbox = bundle(
        requested_revision=1,
        suffix="authorized",
    )
    command = replace(
        command,
        project_binding=replace(command.project_binding, project_id="demo-2"),
    )
    event = replace(event, project_id="demo-2", workflow_id="workflow-2")
    receipt = replace(receipt, project_id="demo-2", workflow_id="workflow-2")
    outbox = replace(outbox, workflow_id="workflow-2")
    before = table_counts(fixture.database, PHASE3_TABLES + ("authority_commands",))

    with pytest.raises(AuthorityRevisionConflict, match="coordinate differs"):
        writer.persist_command_bundle(
            workflow_id="workflow-2",
            idempotency_key="idempotency-authorized-workflow-2",
            command=command,
            event=event,
            receipt=receipt,
            outbox=outbox,
            occurred_at=2000,
            phase3_mutation=mutation,
        )

    _assert_no_command_or_phase3_residue(fixture.database, before)


@pytest.mark.parametrize("tamper_kind", ("delete", "payload"))
def test_reader_fails_closed_when_authorization_receipt_is_sql_tampered(
    tmp_path, tamper_kind
):
    fixture = install_foundation(tmp_path, name=f"receipt-tamper-{tamper_kind}")
    claim = _claim()
    writer, control = _configure_with_claim(fixture, claim)
    authorization = _authorization(claim, control)
    mutation = _mutation(authorization)
    _persist(writer, mutation, suffix="authorized")
    connection = sqlite3.connect(fixture.database)
    try:
        trigger_names = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='authority_production_control_receipts'"
        ).fetchall()
        for (trigger_name,) in trigger_names:
            connection.execute(f'DROP TRIGGER "{trigger_name}"')
        if tamper_kind == "delete":
            connection.execute(
                "DELETE FROM authority_production_control_receipts WHERE receipt_id=?",
                (control.receipt_id,),
            )
        else:
            body = json.loads(
                connection.execute(
                    "SELECT receipt_json FROM authority_production_control_receipts "
                    "WHERE receipt_id=?",
                    (control.receipt_id,),
                ).fetchone()[0]
            )
            body["evidence"]["claims"][0]["dirty_flag"] = "SQL_TAMPERED"
            connection.execute(
                "UPDATE authority_production_control_receipts SET receipt_json=? "
                "WHERE receipt_id=?",
                (json.dumps(body, separators=(",", ":"), sort_keys=True), control.receipt_id),
            )
        connection.commit()
    finally:
        connection.close()

    restarted = AuthorityReadRepository(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    with pytest.raises((AuthorityReadError, AuthorityProductionSchemaError)):
        restarted.command_bundle(
            workflow_id="legacy_current",
            idempotency_key="idempotency-authorized",
        )


def test_authorized_phase3_failure_injection_rolls_back_all_command_rows(
    tmp_path, monkeypatch
):
    fixture = install_foundation(tmp_path)
    claim = _claim()
    writer, control = _configure_with_claim(fixture, claim)
    mutation = _mutation(_authorization(claim, control))
    before = table_counts(fixture.database, PHASE3_TABLES + ("authority_commands",))

    def fail(stage: str) -> None:
        if stage == "after_phase3_artifact_records":
            raise RuntimeError("injected authorized Phase-3 failure")

    monkeypatch.setattr(writer_module, "_writer_failure_point", fail)
    with pytest.raises(RuntimeError, match="injected authorized"):
        _persist(writer, mutation, suffix="authorized")

    _assert_no_command_or_phase3_residue(fixture.database, before)
