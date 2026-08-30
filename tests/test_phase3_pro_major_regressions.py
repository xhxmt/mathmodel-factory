from __future__ import annotations

from dataclasses import replace
import json
import sqlite3

import pytest

from factory_core.authority_repository import AuthorityEnvelopePersistenceError
from factory_core.authority_read_repository import AuthorityReadRepository
from factory_core.phase3_artifacts import (
    ArtifactBlocker,
    ArtifactBlockerCode,
    ArtifactChangeKind,
    CheckpointState,
    CheckpointTransition,
    DirtyDisposition,
    Phase3ContractError,
    build_artifact_removal,
    build_artifact_manifest,
    build_artifact_record,
    build_checkpoint_entry,
    build_phase3_mutation,
    build_phase3_previous_head_bootstrap,
    build_phase3_previous_head_continuation,
    build_reopen_plan,
    compute_change_set,
    owner_compilation_semantic_sha256,
    register_artifact_owner,
)
from factory_core.phase3_shadow_runtime import run_phase3_full_shadow
from tests.support.authority_production import (
    bundle,
    configure_canary,
    install_foundation,
)
from tests.test_phase3_authority_writer import (
    CHECKPOINT_KEY,
    _compilation,
    _complete_mutation,
    _latest_artifact_occurrence_id,
    _latest_checkpoint_occurrence_id,
    _phase3_head_kwargs,
    _persist,
    _record,
)


def test_mutation_rejects_cross_round_component_splicing():
    complete, _record_value, _checkpoint = _complete_mutation(
        source_revision=1, content=b"round-a"
    )
    _comp, other_record = _record(b"round-b")

    with pytest.raises(Phase3ContractError):
        build_phase3_mutation(
            artifact_records=(other_record,),
            artifact_blockers=complete.artifact_blockers,
            removals=complete.removals,
            checkpoint_entries=complete.checkpoint_entries,
            reopen_plan=complete.reopen_plan,
            previous_manifest=complete.previous_manifest,
            current_manifest=complete.current_manifest,
            change_set=complete.change_set,
        )


def test_previous_unreadable_blocker_cannot_be_silently_omitted():
    compilation = _compilation()
    policy = owner_compilation_semantic_sha256(compilation)
    registration = register_artifact_owner(
        compilation, "results/canonical_results.json"
    )
    previous = build_artifact_manifest(
        owner_compilation_sha256=policy,
        records=(),
        blockers=(
            ArtifactBlocker(
                ArtifactBlockerCode.UNREADABLE,
                "results/canonical_results.json",
                "unreadable",
                registration=registration,
            ),
        ),
    )
    current = build_artifact_manifest(
        owner_compilation_sha256=policy,
        records=(),
    )

    change_set = compute_change_set(previous, current)

    assert len(change_set.changes) == 1
    assert change_set.changes[0].kind is ArtifactChangeKind.BLOCKED
    assert change_set.dirty_decisions[0].disposition is DirtyDisposition.BLOCKED


def test_shadow_runtime_blocks_a_previous_blocker_omitted_from_next_scan(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    compilation = _compilation()
    policy = owner_compilation_semantic_sha256(compilation)
    registration = register_artifact_owner(
        compilation, "results/canonical_results.json"
    )
    previous = build_artifact_manifest(
        owner_compilation_sha256=policy,
        records=(),
        blockers=(
            ArtifactBlocker(
                ArtifactBlockerCode.UNREADABLE,
                "results/canonical_results.json",
                "unreadable",
                registration=registration,
            ),
        ),
    )

    result = run_phase3_full_shadow(
        enabled=True,
        project_root=project,
        compilation=compilation,
        paths=(),
        previous_manifest=previous,
    )

    assert result.authoritative is False
    assert result.dispatch_performed is False
    assert result.change_set is not None
    assert result.change_set.changes[0].kind is ArtifactChangeKind.BLOCKED
    assert result.reopen_plan is None


def test_manifest_binds_requested_inventory_and_non_string_path_uses_fixed_sentinel(
    tmp_path,
):
    project = tmp_path / "project"
    project.mkdir()
    compilation = _compilation()
    first = run_phase3_full_shadow(
        enabled=True,
        project_root=project,
        compilation=compilation,
        paths=(object(),),
    ).manifest
    second = run_phase3_full_shadow(
        enabled=True,
        project_root=project,
        compilation=compilation,
        paths=(object(),),
    ).manifest
    assert first is not None and second is not None
    assert first == second
    assert first.tracked_paths == ("__phase3_invalid_path__/non_string",)
    assert first.blockers[0].normalized_path == "__phase3_invalid_path__/non_string"

    unsafe_root = tmp_path / "missing-root"
    a = run_phase3_full_shadow(
        enabled=True,
        project_root=unsafe_root,
        compilation=compilation,
        paths=("results/a.json",),
    ).manifest
    b = run_phase3_full_shadow(
        enabled=True,
        project_root=unsafe_root,
        compilation=compilation,
        paths=("results/b.json",),
    ).manifest
    assert a is not None and b is not None
    assert a.tracked_paths != b.tracked_paths
    assert a.manifest_sha256 != b.manifest_sha256


def test_checkpoint_key_requires_explicit_phase3_namespace():
    with pytest.raises(Phase3ContractError, match="phase3:stage"):
        build_checkpoint_entry(
            checkpoint_key="stage4.results",
            owner_stage=4,
            input_manifest_sha256="a" * 64,
            state=CheckpointState.VALID,
            transition=CheckpointTransition.RECORDED_VALID,
            validation_sha256="b" * 64,
            reason_code="INITIAL_ATTESTATION",
        )


def test_previous_blocker_can_close_via_current_record_or_explicit_removal():
    compilation = _compilation()
    policy = owner_compilation_semantic_sha256(compilation)
    registration = register_artifact_owner(
        compilation, "results/canonical_results.json"
    )
    previous = build_artifact_manifest(
        owner_compilation_sha256=policy,
        records=(),
        blockers=(
            ArtifactBlocker(
                ArtifactBlockerCode.UNREADABLE,
                "results/canonical_results.json",
                "unreadable",
                registration=registration,
            ),
        ),
    )

    _comp, current_record = _record(b"resolved")
    resolved_current = build_artifact_manifest(
        owner_compilation_sha256=policy,
        records=(current_record,),
    )
    resolved_change_set = compute_change_set(previous, resolved_current)
    assert resolved_change_set.changes[0].kind is ArtifactChangeKind.RESOLVED
    assert resolved_change_set.dirty_decisions[0].reason_code == "ARTIFACT_BLOCKER_RESOLVED"
    resolved_plan = build_reopen_plan(
        workflow_id="legacy_current",
        source_revision=7,
        change_set=resolved_change_set,
        previous_manifest=previous,
    )
    assert (
        resolved_plan.read_set[0].expected_blocker_code
        == ArtifactBlockerCode.UNREADABLE.value
    )

    explicit_removal = build_artifact_removal(
        previous,
        "results/canonical_results.json",
        reason_code="EXPLICIT_UNTRACK",
    )
    removed_current = build_artifact_manifest(
        owner_compilation_sha256=policy,
        records=(),
    )
    removed_change_set = compute_change_set(
        previous,
        removed_current,
        removals=(explicit_removal,),
    )
    assert removed_change_set.changes[0].kind is ArtifactChangeKind.REMOVED
    assert removed_change_set.removals == (explicit_removal,)
    removed_plan = build_reopen_plan(
        workflow_id="legacy_current",
        source_revision=8,
        change_set=removed_change_set,
        previous_manifest=previous,
    )
    assert (
        removed_plan.read_set[0].expected_blocker_code
        == ArtifactBlockerCode.UNREADABLE.value
    )


def test_persisted_artifact_occurrence_id_is_revision_bound_even_when_semantic_record_reappears(
    tmp_path,
):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    first, first_record, first_checkpoint = _complete_mutation(
        source_revision=1, content=b"same-semantic-record"
    )
    first_commit = _persist(writer, first, suffix="first")
    second, second_record, second_checkpoint = _complete_mutation(
        source_revision=2,
        content=b"intermediate",
        previous_record=first_record,
        previous_checkpoint=first_checkpoint,
        previous_occurrence_id=_latest_artifact_occurrence_id(
            fixture.database,
            revision=first_commit.revision,
            path=first_record.normalized_path,
        ),
        previous_checkpoint_occurrence_id=_latest_checkpoint_occurrence_id(
            fixture.database,
            revision=first_commit.revision,
            checkpoint_key=first_checkpoint.checkpoint_key,
        ),
        **_phase3_head_kwargs(first_commit, first),
    )
    second_commit = _persist(writer, second, requested_revision=2, suffix="second")
    third, third_record, _third_checkpoint = _complete_mutation(
        source_revision=3,
        content=b"same-semantic-record",
        previous_record=second_record,
        previous_checkpoint=second_checkpoint,
        previous_occurrence_id=_latest_artifact_occurrence_id(
            fixture.database,
            revision=second_commit.revision,
            path=second_record.normalized_path,
        ),
        previous_checkpoint_occurrence_id=_latest_checkpoint_occurrence_id(
            fixture.database,
            revision=second_commit.revision,
            checkpoint_key=second_checkpoint.checkpoint_key,
        ),
        **_phase3_head_kwargs(second_commit, second),
    )
    third_commit = _persist(writer, third, requested_revision=3, suffix="third")

    connection = sqlite3.connect(fixture.database)
    try:
        rows = connection.execute(
            "SELECT artifact_record_id, metadata_json, recorded_revision "
            "FROM authority_artifact_records "
            "WHERE workflow_id='legacy_current' "
            "AND recorded_revision IN (?, ?) "
            "ORDER BY recorded_revision",
            (first_commit.revision, third_commit.revision),
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == 2
    assert rows[0][0] != rows[1][0]
    assert rows[0][0].startswith("phase3-artifact-occurrence-")
    assert rows[1][0].startswith("phase3-artifact-occurrence-")
    first_payload = json.loads(rows[0][1])
    third_payload = json.loads(rows[1][1])
    assert first_payload["occurrence"]["artifact_record"]["artifact_record_id"] == first_record.artifact_record_id
    assert third_payload["occurrence"]["artifact_record"]["artifact_record_id"] == third_record.artifact_record_id
    assert first_payload["occurrence"]["artifact_record"]["record_sha256"] == third_payload["occurrence"]["artifact_record"]["record_sha256"]


def test_complete_mutation_rejects_wrong_checkpoint_manifest_and_partial_graph():
    complete, _record_value, _checkpoint = _complete_mutation(
        source_revision=1, content=b"round-a"
    )

    with pytest.raises(Phase3ContractError, match="graph must bind"):
        build_phase3_mutation(
            artifact_records=complete.artifact_records,
            artifact_blockers=complete.artifact_blockers,
            removals=complete.removals,
            checkpoint_entries=complete.checkpoint_entries,
            reopen_plan=complete.reopen_plan,
            previous_manifest=complete.previous_manifest,
            change_set=complete.change_set,
        )

    bad_checkpoint = build_checkpoint_entry(
        checkpoint_key=CHECKPOINT_KEY,
        owner_stage=4,
        input_manifest_sha256="f" * 64,
        state=complete.checkpoint_entries[0].state,
        transition=complete.checkpoint_entries[0].transition,
        validation_sha256=complete.checkpoint_entries[0].validation_sha256,
        reason_code=complete.checkpoint_entries[0].reason_code,
    )
    with pytest.raises(Phase3ContractError, match="input manifest differs"):
        build_phase3_mutation(
            artifact_records=complete.artifact_records,
            artifact_blockers=complete.artifact_blockers,
            removals=complete.removals,
            checkpoint_entries=(bad_checkpoint,),
            reopen_plan=complete.reopen_plan,
            previous_manifest=complete.previous_manifest,
            current_manifest=complete.current_manifest,
            change_set=complete.change_set,
            previous_head=complete.previous_head,
        )


def test_pure_removal_persists_a_revision_bound_tombstone_atomically(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    first, record, checkpoint = _complete_mutation(
        source_revision=1, content=b"remove-me"
    )
    first_commit = _persist(writer, first, requested_revision=1, suffix="first")

    compilation = _compilation()
    policy = owner_compilation_semantic_sha256(compilation)
    previous = build_artifact_manifest(
        owner_compilation_sha256=policy,
        records=(record,),
    )
    current = build_artifact_manifest(
        owner_compilation_sha256=policy,
        records=(),
    )
    removal = build_artifact_removal(
        previous,
        record.normalized_path,
        reason_code="EXPLICIT_UNTRACK",
    )
    changes = compute_change_set(previous, current, removals=(removal,))
    plan = build_reopen_plan(
        workflow_id="legacy_current",
        source_revision=2,
        change_set=changes,
        previous_manifest=previous,
        previous_occurrence_ids={
            record.normalized_path: _latest_artifact_occurrence_id(
                fixture.database,
                revision=2,
                path=record.normalized_path,
            )
        },
    )
    removed_checkpoint = build_checkpoint_entry(
        checkpoint_key=CHECKPOINT_KEY,
        owner_stage=4,
        input_manifest_sha256=current.manifest_sha256,
        state=CheckpointState.INVALID,
        transition=CheckpointTransition.INVALIDATED,
        validation_sha256=None,
        previous_checkpoint_id=checkpoint.checkpoint_id,
        previous_checkpoint_occurrence_id=_latest_checkpoint_occurrence_id(
            fixture.database,
            revision=2,
            checkpoint_key=checkpoint.checkpoint_key,
        ),
        reason_code="INPUT_REMOVED",
    )
    mutation = build_phase3_mutation(
        artifact_records=current.records,
        artifact_blockers=current.blockers,
        removals=changes.removals,
        checkpoint_entries=(removed_checkpoint,),
        reopen_plan=plan,
        previous_manifest=previous,
        current_manifest=current,
        change_set=changes,
        previous_head=build_phase3_previous_head_continuation(
            workflow_id="legacy_current",
            source_revision=2,
            previous_manifest=previous,
            previous_revision=first_commit.revision,
            previous_command_id=first_commit.command_id,
            previous_mutation_sha256=first.mutation_sha256,
        ),
    )

    try:
        committed = _persist(
            writer, mutation, requested_revision=2, suffix="pure-removal"
        )
    except AuthorityEnvelopePersistenceError as exc:
        pytest.fail(f"pure removal was rejected instead of persisted: {exc}")

    connection = sqlite3.connect(fixture.database)
    try:
        row = connection.execute(
            "SELECT artifact_type, artifact_path, recorded_revision "
            "FROM authority_artifact_records WHERE recorded_revision=?",
            (committed.revision,),
        ).fetchone()
    finally:
        connection.close()
    assert row == (
        "PHASE3_REMOVAL_TOMBSTONE",
        record.normalized_path,
        committed.revision,
    )

    replay = _persist(
        writer, mutation, requested_revision=2, suffix="pure-removal"
    )
    assert replay.replayed is True
    repository = AuthorityReadRepository(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    removed_bundle = repository.command_bundle(
        workflow_id="legacy_current",
        idempotency_key="idempotency-pure-removal",
    )
    assert removed_bundle.phase3_mutation == mutation
    assert len(removed_bundle.phase3_artifact_occurrences) == 1
    tombstone_occurrence = removed_bundle.phase3_artifact_occurrences[0]
    assert tombstone_occurrence.removal == removal
    assert tombstone_occurrence.workflow_id == "legacy_current"
    assert tombstone_occurrence.revision == committed.revision
    removed_state = repository.phase3_artifact_state(
        "legacy_current", through_revision=committed.revision
    )
    assert removed_state.present_records == ()
    assert removed_state.tombstones == (removal,)

    rebuilt_mutation, rebuilt_record, rebuilt_checkpoint = _complete_mutation(
        source_revision=committed.revision,
        content=b"rebuilt",
        previous_checkpoint=removed_checkpoint,
        previous_occurrence_id=tombstone_occurrence.occurrence_id,
        previous_checkpoint_occurrence_id=(
            removed_bundle.phase3_checkpoint_occurrences[0].occurrence_id
        ),
        **_phase3_head_kwargs(committed, mutation),
    )
    rebuilt_commit = _persist(
        writer,
        rebuilt_mutation,
        requested_revision=committed.revision,
        suffix="rebuilt-after-removal",
    )
    assert rebuilt_commit.revision == committed.revision + 1
    latest_state = repository.phase3_artifact_state("legacy_current")
    assert latest_state.present_records == (rebuilt_record,)
    assert latest_state.tombstones == ()
    assert rebuilt_checkpoint.input_manifest_sha256 == (
        rebuilt_mutation.current_manifest.manifest_sha256
    )


def test_mixed_modification_and_removal_persist_as_one_atomic_occurrence_set(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    compilation = _compilation()
    policy = owner_compilation_semantic_sha256(compilation)
    first_a = build_artifact_record(
        register_artifact_owner(compilation, "results/canonical_results.json"),
        content=b"a-v1",
    )
    first_b = build_artifact_record(
        register_artifact_owner(compilation, "results/secondary.json"),
        content=b"b-v1",
    )
    empty = build_artifact_manifest(owner_compilation_sha256=policy, records=())
    initial_manifest = build_artifact_manifest(
        owner_compilation_sha256=policy,
        records=(first_a, first_b),
    )
    initial_changes = compute_change_set(empty, initial_manifest)
    initial_plan = build_reopen_plan(
        workflow_id="legacy_current",
        source_revision=1,
        change_set=initial_changes,
        previous_manifest=empty,
    )
    initial_checkpoint = build_checkpoint_entry(
        checkpoint_key=CHECKPOINT_KEY,
        owner_stage=4,
        input_manifest_sha256=initial_manifest.manifest_sha256,
        state=CheckpointState.VALID,
        transition=CheckpointTransition.RECORDED_VALID,
        validation_sha256="7" * 64,
        reason_code="INITIAL_ATTESTATION",
    )
    initial_mutation = build_phase3_mutation(
        artifact_records=initial_manifest.records,
        checkpoint_entries=(initial_checkpoint,),
        reopen_plan=initial_plan,
        previous_manifest=empty,
        current_manifest=initial_manifest,
        change_set=initial_changes,
        previous_head=build_phase3_previous_head_bootstrap(
            workflow_id="legacy_current",
            source_revision=1,
            previous_manifest=empty,
        ),
    )
    initial_commit = _persist(writer, initial_mutation, suffix="mixed-first")

    modified_a = build_artifact_record(first_a.registration, content=b"a-v2")
    current = build_artifact_manifest(
        owner_compilation_sha256=policy,
        records=(modified_a,),
    )
    removal = build_artifact_removal(
        initial_manifest,
        first_b.normalized_path,
        reason_code="EXPLICIT_UNTRACK",
    )
    changes = compute_change_set(
        initial_manifest,
        current,
        removals=(removal,),
    )
    prior_state = AuthorityReadRepository(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    ).phase3_artifact_state("legacy_current")
    previous_occurrences = {
        item.normalized_path: item.occurrence_id
        for item in prior_state.occurrences
    }
    plan = build_reopen_plan(
        workflow_id="legacy_current",
        source_revision=initial_commit.revision,
        change_set=changes,
        previous_manifest=initial_manifest,
        previous_occurrence_ids=previous_occurrences,
    )
    checkpoint = build_checkpoint_entry(
        checkpoint_key=CHECKPOINT_KEY,
        owner_stage=4,
        input_manifest_sha256=current.manifest_sha256,
        state=CheckpointState.INVALID,
        transition=CheckpointTransition.INVALIDATED,
        validation_sha256=None,
        previous_checkpoint_id=initial_checkpoint.checkpoint_id,
        previous_checkpoint_occurrence_id=_latest_checkpoint_occurrence_id(
            fixture.database,
            revision=initial_commit.revision,
            checkpoint_key=CHECKPOINT_KEY,
        ),
        reason_code="INPUT_CHANGED",
    )
    mutation = build_phase3_mutation(
        artifact_records=current.records,
        removals=changes.removals,
        checkpoint_entries=(checkpoint,),
        reopen_plan=plan,
        previous_manifest=initial_manifest,
        current_manifest=current,
        change_set=changes,
        previous_head=build_phase3_previous_head_continuation(
            workflow_id="legacy_current",
            source_revision=initial_commit.revision,
            previous_manifest=initial_manifest,
            previous_revision=initial_commit.revision,
            previous_command_id=initial_commit.command_id,
            previous_mutation_sha256=initial_mutation.mutation_sha256,
        ),
    )
    committed = _persist(
        writer,
        mutation,
        requested_revision=initial_commit.revision,
        suffix="mixed-second",
    )

    connection = sqlite3.connect(fixture.database)
    try:
        kinds = connection.execute(
            "SELECT artifact_type FROM authority_artifact_records "
            "WHERE recorded_revision=? ORDER BY artifact_type",
            (committed.revision,),
        ).fetchall()
    finally:
        connection.close()
    assert kinds == [("PHASE3_REMOVAL_TOMBSTONE",), ("PROJECT_FILE",)]


def test_same_semantic_record_and_checkpoint_can_exist_in_two_workflows(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    first, first_record, first_checkpoint = _complete_mutation(
        source_revision=1,
        content=b"shared",
    )
    first_commit = _persist(writer, first, suffix="workflow-one")
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
                   'RECORDED', NULL,
                   'legacy_unknown', authority_state
            FROM authority_workflows WHERE workflow_id='legacy_current'
            """
        )
        connection.execute(
            "INSERT INTO authority_revision_allocator(workflow_id, next_revision) "
            "SELECT 'workflow-2', 2"
        )
        connection.commit()
    finally:
        connection.close()

    second, second_record, second_checkpoint = _complete_mutation(
        source_revision=1,
        content=b"shared",
        workflow_id="workflow-2",
    )
    command, event, receipt, outbox = bundle(
        requested_revision=1,
        suffix="workflow-two",
    )
    command = replace(
        command,
        project_binding=replace(command.project_binding, project_id="demo-2"),
    )
    event = replace(event, project_id="demo-2", workflow_id="workflow-2")
    receipt = replace(receipt, project_id="demo-2", workflow_id="workflow-2")
    outbox = replace(outbox, workflow_id="workflow-2")
    second_commit = writer.persist_command_bundle(
        workflow_id="workflow-2",
        idempotency_key="idempotency-workflow-two",
        command=command,
        event=event,
        receipt=receipt,
        outbox=outbox,
        occurred_at=2000,
        phase3_mutation=second,
    )

    assert first_record.record_sha256 == second_record.record_sha256
    assert first_checkpoint.checkpoint_sha256 == second_checkpoint.checkpoint_sha256
    connection = sqlite3.connect(fixture.database)
    try:
        artifact_ids = connection.execute(
            "SELECT artifact_record_id FROM authority_artifact_records "
            "WHERE recorded_revision=2 ORDER BY workflow_id"
        ).fetchall()
        checkpoint_ids = connection.execute(
            "SELECT checkpoint_id FROM authority_checkpoint_ledger "
            "WHERE recorded_revision=2 AND checkpoint_kind='RECORDED' "
            "ORDER BY workflow_id"
        ).fetchall()
    finally:
        connection.close()
    assert first_commit.revision == second_commit.revision == 2
    assert len({item[0] for item in artifact_ids}) == 2
    assert len({item[0] for item in checkpoint_ids}) == 2


def test_same_semantic_checkpoint_can_recur_at_a_later_revision(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    first, first_record, first_checkpoint = _complete_mutation(
        source_revision=1,
        content=b"A",
    )
    first_commit = _persist(writer, first, suffix="checkpoint-a-first")
    second, second_record, second_checkpoint = _complete_mutation(
        source_revision=2,
        content=b"B",
        previous_record=first_record,
        previous_checkpoint=first_checkpoint,
        previous_occurrence_id=_latest_artifact_occurrence_id(
            fixture.database,
            revision=first_commit.revision,
            path=first_record.normalized_path,
        ),
        previous_checkpoint_occurrence_id=_latest_checkpoint_occurrence_id(
            fixture.database,
            revision=first_commit.revision,
            checkpoint_key=CHECKPOINT_KEY,
        ),
        **_phase3_head_kwargs(first_commit, first),
    )
    second_commit = _persist(
        writer,
        second,
        requested_revision=2,
        suffix="checkpoint-b",
    )
    third, third_record, _third_checkpoint = _complete_mutation(
        source_revision=3,
        content=b"A",
        previous_record=second_record,
        previous_checkpoint=second_checkpoint,
        previous_occurrence_id=_latest_artifact_occurrence_id(
            fixture.database,
            revision=second_commit.revision,
            path=second_record.normalized_path,
        ),
        previous_checkpoint_occurrence_id=_latest_checkpoint_occurrence_id(
            fixture.database,
            revision=second_commit.revision,
            checkpoint_key=CHECKPOINT_KEY,
        ),
        **_phase3_head_kwargs(second_commit, second),
    )
    third = build_phase3_mutation(
        artifact_records=third.artifact_records,
        artifact_blockers=third.artifact_blockers,
        removals=third.removals,
        checkpoint_entries=third.checkpoint_entries,
        reopen_plan=third.reopen_plan,
        previous_manifest=third.previous_manifest,
        current_manifest=third.current_manifest,
        change_set=third.change_set,
        previous_head=third.previous_head,
    )
    third_commit = _persist(
        writer,
        third,
        requested_revision=3,
        suffix="checkpoint-a-again",
    )
    assert third_record.record_sha256 == first_record.record_sha256
    connection = sqlite3.connect(fixture.database)
    try:
        rows = connection.execute(
            "SELECT checkpoint_id, payload_json FROM authority_checkpoint_ledger "
            "WHERE workflow_id='legacy_current' AND recorded_revision IN (?, ?) "
            "ORDER BY recorded_revision",
            (first_commit.revision, third_commit.revision),
        ).fetchall()
    finally:
        connection.close()
    assert rows[0][0] != rows[1][0]
    payloads = [json.loads(row[1]) for row in rows]
    assert (
        payloads[0]["occurrence"]["checkpoint_entry"]["input_manifest_sha256"]
        == payloads[1]["occurrence"]["checkpoint_entry"]["input_manifest_sha256"]
        == first_checkpoint.input_manifest_sha256
    )
    assert (
        payloads[0]["occurrence"]["checkpoint_entry"]["state"]
        == payloads[1]["occurrence"]["checkpoint_entry"]["state"]
        == first_checkpoint.state.value
    )
    assert (
        payloads[0]["occurrence"]["checkpoint_entry"]["checkpoint_sha256"]
        != payloads[1]["occurrence"]["checkpoint_entry"]["checkpoint_sha256"]
    )
