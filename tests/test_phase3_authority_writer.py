from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

import factory_core.authority_production_writer as writer_module
from factory_core.artifact_ownership import ArtifactOwnership
from factory_core.authority_production_writer import (
    AUTHORITY_PHASE3_IDEMPOTENCY_REQUEST_SCHEMA,
    AUTHORITY_PRODUCTION_BUNDLE_SCHEMA_V2,
)
from factory_core.authority_repository import (
    AuthorityEnvelopePersistenceError,
    AuthorityIdempotencyConflict,
    AuthorityRevisionConflict,
)
from factory_core.owner_compiler import compile_owner_registry
from factory_core.phase3_artifacts import (
    ArtifactBlocker,
    ArtifactBlockerCode,
    CheckpointState,
    CheckpointTransition,
    DirtyDisposition,
    build_artifact_removal,
    build_artifact_manifest,
    build_artifact_record,
    build_blocked_no_reopen_disposition,
    build_checkpoint_entry,
    build_phase3_mutation,
    build_phase3_previous_head_bootstrap,
    build_phase3_previous_head_continuation,
    build_reopen_plan,
    compute_change_set,
    owner_compilation_semantic_sha256,
    register_artifact_owner,
)
from tests.support.authority_production import (
    bundle,
    configure_canary,
    install_foundation,
    table_counts,
)


PHASE3_TABLES = (
    "authority_artifact_records",
    "authority_checkpoint_ledger",
    "authority_reopen_plans",
)
CHECKPOINT_KEY = "phase3:stage4.results"


def _compilation():
    return compile_owner_registry(
        (
            ArtifactOwnership(
                pattern="results/**",
                owner_stage=4,
                semantic_domain="canonical_result",
                dirty_flag="RESULT_DIRTY",
            ),
        )
    )


def _record(content: bytes):
    compilation = _compilation()
    registration = register_artifact_owner(
        compilation, "results/canonical_results.json"
    )
    return compilation, build_artifact_record(registration, content=content)


def _empty_manifest(compilation):
    return build_artifact_manifest(
        owner_compilation_sha256=owner_compilation_semantic_sha256(compilation),
        records=(),
    )


def _blocker(
    compilation,
    *,
    path: str = "results/canonical_results.json",
    code: ArtifactBlockerCode = ArtifactBlockerCode.UNREADABLE,
    detail: str = "unreadable",
):
    return ArtifactBlocker(
        code,
        path,
        detail,
        registration=register_artifact_owner(compilation, path),
    )


def _full_mutation(
    *,
    source_revision: int,
    previous_manifest,
    current_manifest,
    checkpoint,
    workflow_id: str = "legacy_current",
    previous_occurrence_ids: dict[str, str] | None = None,
    previous_phase3_revision: int | None = None,
    previous_phase3_command_id: str | None = None,
    previous_phase3_mutation_sha256: str | None = None,
):
    changes = compute_change_set(
        previous_manifest,
        current_manifest,
    )
    if (
        previous_phase3_revision is None
        and previous_phase3_command_id is None
        and previous_phase3_mutation_sha256 is None
    ):
        previous_head = build_phase3_previous_head_bootstrap(
            workflow_id=workflow_id,
            source_revision=source_revision,
            previous_manifest=previous_manifest,
        )
    else:
        assert previous_phase3_revision is not None
        assert previous_phase3_command_id is not None
        assert previous_phase3_mutation_sha256 is not None
        previous_head = build_phase3_previous_head_continuation(
            workflow_id=workflow_id,
            source_revision=source_revision,
            previous_manifest=previous_manifest,
            previous_revision=previous_phase3_revision,
            previous_command_id=previous_phase3_command_id,
            previous_mutation_sha256=previous_phase3_mutation_sha256,
        )
    blocked = any(
        item.disposition is DirtyDisposition.BLOCKED
        for item in changes.dirty_decisions
    )
    plan = (
        None
        if blocked
        else build_reopen_plan(
            workflow_id=workflow_id,
            source_revision=source_revision,
            change_set=changes,
            previous_manifest=previous_manifest,
            previous_occurrence_ids=previous_occurrence_ids,
        )
    )
    blocked_disposition = (
        build_blocked_no_reopen_disposition(
            workflow_id=workflow_id,
            source_revision=source_revision,
            change_set=changes,
            previous_manifest=previous_manifest,
            current_manifest=current_manifest,
            previous_occurrence_ids=previous_occurrence_ids,
        )
        if blocked
        else None
    )
    return build_phase3_mutation(
        artifact_records=current_manifest.records,
        artifact_blockers=current_manifest.blockers,
        removals=changes.removals,
        checkpoint_entries=(checkpoint,),
        reopen_plan=plan,
        previous_manifest=previous_manifest,
        current_manifest=current_manifest,
        change_set=changes,
        blocked_disposition=blocked_disposition,
        previous_head=previous_head,
    )


def _complete_mutation(
    *,
    source_revision: int,
    content: bytes,
    previous_record=None,
    previous_checkpoint=None,
    previous_occurrence_id: str | None = None,
    previous_checkpoint_occurrence_id: str | None = None,
    workflow_id: str = "legacy_current",
    previous_phase3_revision: int | None = None,
    previous_phase3_command_id: str | None = None,
    previous_phase3_mutation_sha256: str | None = None,
):
    compilation, current_record = _record(content)
    compilation_sha256 = owner_compilation_semantic_sha256(compilation)
    previous = build_artifact_manifest(
        owner_compilation_sha256=compilation_sha256,
        records=() if previous_record is None else (previous_record,),
    )
    current = build_artifact_manifest(
        owner_compilation_sha256=compilation_sha256,
        records=(current_record,),
    )
    if previous_checkpoint is None:
        state = CheckpointState.VALID
        transition = CheckpointTransition.RECORDED_VALID
        validation_sha256 = "9" * 64
        reason_code = "INITIAL_ATTESTATION"
    elif previous_checkpoint.state is CheckpointState.VALID:
        state = CheckpointState.INVALID
        transition = CheckpointTransition.INVALIDATED
        validation_sha256 = None
        reason_code = "INPUT_CHANGED"
    else:
        state = CheckpointState.VALID
        transition = CheckpointTransition.REATTESTED_VALID
        validation_sha256 = "9" * 64
        reason_code = "VALIDATOR_PASS"
    checkpoint = build_checkpoint_entry(
        checkpoint_key=CHECKPOINT_KEY,
        owner_stage=4,
        input_manifest_sha256=current.manifest_sha256,
        state=state,
        transition=transition,
        validation_sha256=validation_sha256,
        previous_checkpoint_id=(
            None if previous_checkpoint is None else previous_checkpoint.checkpoint_id
        ),
        previous_checkpoint_occurrence_id=(
            previous_checkpoint_occurrence_id
        ),
        reason_code=reason_code,
    )
    previous_occurrence_ids = (
        {}
        if previous_occurrence_id is None
        else {current_record.normalized_path: previous_occurrence_id}
    )
    return (
        _full_mutation(
            source_revision=source_revision,
            previous_manifest=previous,
            current_manifest=current,
            checkpoint=checkpoint,
            workflow_id=workflow_id,
            previous_occurrence_ids=previous_occurrence_ids,
            previous_phase3_revision=previous_phase3_revision,
            previous_phase3_command_id=previous_phase3_command_id,
            previous_phase3_mutation_sha256=previous_phase3_mutation_sha256,
        ),
        current_record,
        checkpoint,
    )


def _phase3_head_kwargs(commit, mutation) -> dict[str, object]:
    return {
        "previous_phase3_revision": commit.revision,
        "previous_phase3_command_id": commit.command_id,
        "previous_phase3_mutation_sha256": mutation.mutation_sha256,
    }


def _persist(writer, mutation, *, requested_revision=1, suffix="phase3"):
    command, event, receipt, outbox = bundle(
        requested_revision=requested_revision, suffix=suffix
    )
    return writer.persist_command_bundle(
        workflow_id="legacy_current",
        idempotency_key=f"idempotency-{suffix}",
        command=command,
        event=event,
        receipt=receipt,
        outbox=outbox,
        occurred_at=2000,
        phase3_mutation=mutation,
    )


def _blocked_mutation(
    *,
    source_revision: int,
    workflow_id: str = "legacy_current",
    previous_manifest=None,
    previous_checkpoint=None,
    previous_checkpoint_occurrence_id: str | None = None,
    previous_occurrence_ids: dict[str, str] | None = None,
    previous_phase3_revision: int | None = None,
    previous_phase3_command_id: str | None = None,
    previous_phase3_mutation_sha256: str | None = None,
):
    compilation = _compilation()
    previous = previous_manifest or _empty_manifest(compilation)
    current = build_artifact_manifest(
        owner_compilation_sha256=owner_compilation_semantic_sha256(compilation),
        records=(),
        blockers=(_blocker(compilation),),
    )
    if previous_checkpoint is None:
        state = CheckpointState.INVALID
        transition = CheckpointTransition.RECORDED_INVALID
        reason_code = "BLOCKED_SCAN"
    elif previous_checkpoint.state is CheckpointState.VALID:
        state = CheckpointState.INVALID
        transition = CheckpointTransition.INVALIDATED
        reason_code = "BLOCKED_SCAN"
    else:
        state = CheckpointState.INVALID
        transition = CheckpointTransition.REATTESTED_INVALID
        reason_code = "BLOCKED_SCAN"
    checkpoint = build_checkpoint_entry(
        checkpoint_key=CHECKPOINT_KEY,
        owner_stage=4,
        input_manifest_sha256=current.manifest_sha256,
        state=state,
        transition=transition,
        validation_sha256=None,
        previous_checkpoint_id=(
            None if previous_checkpoint is None else previous_checkpoint.checkpoint_id
        ),
        previous_checkpoint_occurrence_id=previous_checkpoint_occurrence_id,
        reason_code=reason_code,
    )
    return _full_mutation(
        source_revision=source_revision,
        previous_manifest=previous,
        current_manifest=current,
        checkpoint=checkpoint,
        workflow_id=workflow_id,
        previous_occurrence_ids=previous_occurrence_ids,
        previous_phase3_revision=previous_phase3_revision,
        previous_phase3_command_id=previous_phase3_command_id,
        previous_phase3_mutation_sha256=previous_phase3_mutation_sha256,
    )


def _latest_artifact_occurrence_id(database, *, revision: int, path: str) -> str:
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            """
            SELECT artifact_record_id
            FROM authority_artifact_records
            WHERE workflow_id='legacy_current'
              AND recorded_revision=?
              AND artifact_path=?
            """,
            (revision, path),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return row[0]


def _latest_checkpoint_occurrence_id(database, *, revision: int, checkpoint_key: str) -> str:
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            """
            SELECT checkpoint_id
            FROM authority_checkpoint_ledger
            WHERE workflow_id='legacy_current'
              AND recorded_revision=?
              AND checkpoint_key=?
              AND checkpoint_kind='RECORDED'
            """,
            (revision, checkpoint_key),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return row[0]


def test_no_mutation_keeps_frozen_v1_request_and_bundle_hashes(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    command, event, receipt, outbox = bundle(requested_revision=1)
    result = writer.persist_command_bundle(
        workflow_id="legacy_current",
        idempotency_key="v1-golden",
        command=command,
        event=event,
        receipt=receipt,
        outbox=outbox,
        occurred_at=2000,
    )

    assert result.request_sha256 == (
        "966bdaf46f2a70ef85f36b340619ad101e2aefedb5740e42ab2d539276488bed"
    )
    assert result.bundle_sha256 == (
        "99e8f0fcd162348715f5eb6cc4cea2f745253459d882dca7237074be11433f60"
    )
    assert result.phase3_mutation_sha256 is None
    assert result.as_dict()["schema"] == "authority-production-commit-result-v1"


def test_production_writer_keeps_one_public_mutation_surface():
    public_callables = {
        name
        for name, value in writer_module.AuthorityProductionWriter.__dict__.items()
        if not name.startswith("_") and callable(value)
    }
    assert public_callables == {"persist_command_bundle"}


def test_writer_rejects_partial_phase3_mutations_before_any_write(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    complete, record, checkpoint = _complete_mutation(
        source_revision=1, content=b"complete"
    )
    assert complete.reopen_plan is not None
    partials = (
        build_phase3_mutation(artifact_records=(record,)),
        build_phase3_mutation(checkpoint_entries=(checkpoint,)),
        build_phase3_mutation(reopen_plan=complete.reopen_plan),
    )
    before = table_counts(fixture.database, PHASE3_TABLES)

    for index, partial in enumerate(partials):
        with pytest.raises(
            AuthorityEnvelopePersistenceError, match="all three ledger tables"
        ):
            _persist(writer, partial, suffix=f"partial-{index}")

    assert table_counts(fixture.database, PHASE3_TABLES) == before


def test_missing_control_record_is_typed_with_and_without_optimization_and_preconnect(
    tmp_path, monkeypatch
):
    complete, _record_value, _checkpoint = _complete_mutation(
        source_revision=1, content=b"missing-control"
    )
    assert complete.reopen_plan is not None
    malformed = replace(complete, reopen_plan=None)
    expected = (
        "complete Phase-3 mutation requires a reopen-ledger control record"
    )

    script = f"""
from dataclasses import replace
from factory_core.phase3_artifacts import Phase3ContractError, validate_phase3_mutation
from tests.test_phase3_authority_writer import _complete_mutation

complete, _record_value, _checkpoint = _complete_mutation(
    source_revision=1, content=b\"missing-control\"
)
malformed = replace(complete, reopen_plan=None)
try:
    validate_phase3_mutation(malformed)
except Phase3ContractError as exc:
    if str(exc) != {expected!r}:
        raise SystemExit(f\"unexpected error: {{exc}}\")
    print(type(exc).__name__ + \"::\" + str(exc))
else:
    raise SystemExit(\"missing typed contract error\")
"""
    project_root = Path(__file__).resolve().parents[1]
    results = []
    for flags in ((), ("-O",)):
        result = subprocess.run(
            [sys.executable, *flags, "-c", script],
            cwd=project_root,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        results.append((result.stdout, result.stderr))
    assert results[0] == results[1] == (
        f"Phase3ContractError::{expected}\n",
        "",
    )

    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    connection_attempted = False

    def reject_connection(*_args, **_kwargs):
        nonlocal connection_attempted
        connection_attempted = True
        raise AssertionError("database connection must not be attempted")

    monkeypatch.setattr(writer_module, "connect_authority_rw", reject_connection)
    with pytest.raises(AuthorityEnvelopePersistenceError, match=expected):
        _persist(writer, malformed, suffix="missing-control")
    assert connection_attempted is False


def test_phase3_v2_identity_and_three_ledger_tables_commit_atomically(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    before = table_counts(fixture.database, PHASE3_TABLES)
    mutation, record, checkpoint = _complete_mutation(
        source_revision=1, content=b"first"
    )

    result = _persist(writer, mutation)

    after = table_counts(fixture.database, PHASE3_TABLES)
    assert after == {name: before[name] + 1 for name in PHASE3_TABLES}
    assert result.request_schema == AUTHORITY_PHASE3_IDEMPOTENCY_REQUEST_SCHEMA
    assert result.bundle_schema == AUTHORITY_PRODUCTION_BUNDLE_SCHEMA_V2
    assert result.phase3_mutation_sha256 == mutation.mutation_sha256
    assert result.request_sha256 != result.command_id
    connection = sqlite3.connect(fixture.database)
    try:
        artifact_occurrence_id = connection.execute(
            "SELECT artifact_record_id FROM authority_artifact_records "
            "WHERE recorded_revision=2"
        ).fetchone()[0]
        checkpoint_occurrence_id = connection.execute(
            "SELECT checkpoint_id FROM authority_checkpoint_ledger "
            "WHERE recorded_revision=2 AND checkpoint_kind='RECORDED'"
        ).fetchone()[0]
        assert artifact_occurrence_id.startswith("phase3-artifact-occurrence-")
        assert artifact_occurrence_id != record.artifact_record_id
        assert checkpoint_occurrence_id.startswith("phase3-checkpoint-occurrence-")
        assert checkpoint_occurrence_id != checkpoint.checkpoint_id
        assert connection.execute(
            "SELECT request_schema, request_sha256 FROM authority_idempotency_records "
            "WHERE idempotency_key='idempotency-phase3'"
        ).fetchone() == (result.request_schema, result.request_sha256)
    finally:
        connection.close()


def test_phase3_exact_replay_and_mutation_hash_conflict(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    mutation, _record_value, _checkpoint = _complete_mutation(
        source_revision=1, content=b"first"
    )
    first = _persist(writer, mutation)

    replay = _persist(writer, mutation)
    different, _other, _other_checkpoint = _complete_mutation(
        source_revision=1, content=b"different"
    )

    assert replay.replayed is True
    assert replay.request_sha256 == first.request_sha256
    assert replay.bundle_sha256 == first.bundle_sha256
    with pytest.raises(AuthorityIdempotencyConflict):
        _persist(writer, different)


def test_blocked_mutation_persists_via_existing_reopen_ledger_and_replays_exactly(
    tmp_path,
):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    mutation = _blocked_mutation(source_revision=1)

    committed = _persist(writer, mutation, suffix="blocked")
    replay = _persist(writer, mutation, suffix="blocked")

    assert mutation.reopen_plan is None
    assert mutation.blocked_disposition is not None
    assert replay.replayed is True
    assert replay.request_sha256 == committed.request_sha256
    assert replay.bundle_sha256 == committed.bundle_sha256
    connection = sqlite3.connect(fixture.database)
    try:
        row = connection.execute(
            "SELECT reopen_plan_id, source_revision, target_scope, reason_code "
            "FROM authority_reopen_plans WHERE recorded_revision=?",
            (committed.revision,),
        ).fetchone()
    finally:
        connection.close()
    assert row == (
        mutation.blocked_disposition.disposition_id,
        mutation.blocked_disposition.source_revision,
        mutation.blocked_disposition.target_scope,
        mutation.blocked_disposition.reason_code,
    )


def test_previous_phase3_head_rejects_cropped_persisted_record_manifest(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    compilation = _compilation()
    policy = owner_compilation_semantic_sha256(compilation)
    record_a_v1 = build_artifact_record(
        register_artifact_owner(compilation, "results/a.json"),
        content=b"a-v1",
    )
    record_b_v1 = build_artifact_record(
        register_artifact_owner(compilation, "results/b.json"),
        content=b"b-v1",
    )
    empty = build_artifact_manifest(owner_compilation_sha256=policy, records=())
    initial_manifest = build_artifact_manifest(
        owner_compilation_sha256=policy,
        records=(record_a_v1, record_b_v1),
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
    initial_commit = _persist(writer, initial_mutation, suffix="record-head-first")
    cropped_previous = build_artifact_manifest(
        owner_compilation_sha256=policy,
        records=(record_a_v1,),
    )
    cropped_current = build_artifact_manifest(
        owner_compilation_sha256=policy,
        records=(
            build_artifact_record(record_a_v1.registration, content=b"a-v2"),
        ),
    )
    cropped_changes = compute_change_set(cropped_previous, cropped_current)
    cropped_plan = build_reopen_plan(
        workflow_id="legacy_current",
        source_revision=initial_commit.revision,
        change_set=cropped_changes,
        previous_manifest=cropped_previous,
        previous_occurrence_ids={
            record_a_v1.normalized_path: _latest_artifact_occurrence_id(
                fixture.database,
                revision=initial_commit.revision,
                path=record_a_v1.normalized_path,
            )
        },
    )
    cropped_checkpoint = build_checkpoint_entry(
        checkpoint_key=CHECKPOINT_KEY,
        owner_stage=4,
        input_manifest_sha256=cropped_current.manifest_sha256,
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
    cropped_mutation = build_phase3_mutation(
        artifact_records=cropped_current.records,
        checkpoint_entries=(cropped_checkpoint,),
        reopen_plan=cropped_plan,
        previous_manifest=cropped_previous,
        current_manifest=cropped_current,
        change_set=cropped_changes,
        previous_head=build_phase3_previous_head_continuation(
            workflow_id="legacy_current",
            source_revision=initial_commit.revision,
            previous_manifest=cropped_previous,
            previous_revision=initial_commit.revision,
            previous_command_id=initial_commit.command_id,
            previous_mutation_sha256=initial_mutation.mutation_sha256,
        ),
    )

    with pytest.raises(AuthorityRevisionConflict, match="previous manifest head differs"):
        _persist(
            writer,
            cropped_mutation,
            requested_revision=initial_commit.revision,
            suffix="record-head-cropped",
        )


def test_previous_phase3_head_rejects_cropped_persisted_blocker_manifest(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    first = _blocked_mutation(source_revision=1)
    first_commit = _persist(writer, first, suffix="blocked-first")
    cropped = _complete_mutation(
        source_revision=first_commit.revision,
        content=b"resolved",
        previous_checkpoint=first.checkpoint_entries[0],
        previous_checkpoint_occurrence_id=_latest_checkpoint_occurrence_id(
            fixture.database,
            revision=first_commit.revision,
            checkpoint_key=CHECKPOINT_KEY,
        ),
        **_phase3_head_kwargs(first_commit, first),
    )

    with pytest.raises(AuthorityRevisionConflict, match="previous manifest head differs"):
        _persist(
            writer,
            cropped[0],
            requested_revision=first_commit.revision,
            suffix="blocked-head-cropped",
        )


def test_previous_phase3_head_rejects_stale_same_manifest_blocker_fork(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    first = _blocked_mutation(source_revision=1)
    first_commit = _persist(writer, first, suffix="blocked-first")
    blocker_path = first.current_manifest.blockers[0].normalized_path
    second = _blocked_mutation(
        source_revision=first_commit.revision,
        previous_manifest=first.current_manifest,
        previous_checkpoint=first.checkpoint_entries[0],
        previous_checkpoint_occurrence_id=_latest_checkpoint_occurrence_id(
            fixture.database,
            revision=first_commit.revision,
            checkpoint_key=CHECKPOINT_KEY,
        ),
        previous_occurrence_ids={
            blocker_path: _latest_artifact_occurrence_id(
                fixture.database,
                revision=first_commit.revision,
                path=blocker_path,
            )
        },
        **_phase3_head_kwargs(first_commit, first),
    )
    second_commit = _persist(
        writer,
        second,
        requested_revision=first_commit.revision,
        suffix="blocked-second",
    )
    stale = _blocked_mutation(
        source_revision=second_commit.revision,
        previous_manifest=second.current_manifest,
        previous_checkpoint=second.checkpoint_entries[0],
        previous_checkpoint_occurrence_id=_latest_checkpoint_occurrence_id(
            fixture.database,
            revision=second_commit.revision,
            checkpoint_key=CHECKPOINT_KEY,
        ),
        previous_occurrence_ids={
            blocker_path: _latest_artifact_occurrence_id(
                fixture.database,
                revision=second_commit.revision,
                path=blocker_path,
            )
        },
        **_phase3_head_kwargs(first_commit, first),
    )

    with pytest.raises(AuthorityRevisionConflict, match="previous head continuity differs"):
        _persist(
            writer,
            stale,
            requested_revision=second_commit.revision,
            suffix="blocked-stale-head",
        )


def test_writer_rejects_rebuild_that_omits_prior_tombstone_occurrence(tmp_path):
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
        source_revision=first_commit.revision,
        change_set=changes,
        previous_manifest=previous,
        previous_occurrence_ids={
            record.normalized_path: _latest_artifact_occurrence_id(
                fixture.database,
                revision=first_commit.revision,
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
            revision=first_commit.revision,
            checkpoint_key=checkpoint.checkpoint_key,
        ),
        reason_code="INPUT_REMOVED",
    )
    removal_mutation = build_phase3_mutation(
        artifact_records=current.records,
        removals=changes.removals,
        checkpoint_entries=(removed_checkpoint,),
        reopen_plan=plan,
        previous_manifest=previous,
        current_manifest=current,
        change_set=changes,
        previous_head=build_phase3_previous_head_continuation(
            workflow_id="legacy_current",
            source_revision=first_commit.revision,
            previous_manifest=previous,
            previous_revision=first_commit.revision,
            previous_command_id=first_commit.command_id,
            previous_mutation_sha256=first.mutation_sha256,
        ),
    )
    removal_commit = _persist(
        writer,
        removal_mutation,
        requested_revision=first_commit.revision,
        suffix="removed",
    )
    rebuilt, _rebuilt_record, _rebuilt_checkpoint = _complete_mutation(
        source_revision=removal_commit.revision,
        content=b"rebuilt",
        previous_checkpoint=removed_checkpoint,
        previous_checkpoint_occurrence_id=_latest_checkpoint_occurrence_id(
            fixture.database,
            revision=removal_commit.revision,
            checkpoint_key=CHECKPOINT_KEY,
        ),
        **_phase3_head_kwargs(removal_commit, removal_mutation),
    )

    with pytest.raises(AuthorityRevisionConflict, match="absence occurrence CAS lost"):
        _persist(
            writer,
            rebuilt,
            requested_revision=removal_commit.revision,
            suffix="rebuilt-omits-tombstone",
        )


def test_read_set_and_checkpoint_predecessor_cas(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    first, old_record, old_checkpoint = _complete_mutation(
        source_revision=1, content=b"first"
    )
    first_commit = _persist(writer, first, requested_revision=1, suffix="first")
    second, _new_record, _new_checkpoint = _complete_mutation(
        source_revision=2,
        content=b"second",
        previous_record=old_record,
        previous_checkpoint=old_checkpoint,
        previous_occurrence_id=_latest_artifact_occurrence_id(
            fixture.database,
            revision=first_commit.revision,
            path=old_record.normalized_path,
        ),
        previous_checkpoint_occurrence_id=_latest_checkpoint_occurrence_id(
            fixture.database,
            revision=first_commit.revision,
            checkpoint_key=old_checkpoint.checkpoint_key,
        ),
        **_phase3_head_kwargs(first_commit, first),
    )

    committed = _persist(writer, second, requested_revision=2, suffix="second")

    assert committed.revision == 3


def test_read_set_cas_rejects_unpersisted_prior_identity(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    first, persisted_record, _checkpoint = _complete_mutation(
        source_revision=1, content=b"first"
    )
    first_commit = _persist(writer, first, requested_revision=1, suffix="first")
    stale, _current, _current_checkpoint = _complete_mutation(
        source_revision=2,
        content=b"second",
        previous_record=persisted_record,
        previous_occurrence_id="phase3-artifact-occurrence-missing",
        **_phase3_head_kwargs(first_commit, first),
    )

    with pytest.raises(AuthorityRevisionConflict, match="Artifact occurrence CAS"):
        _persist(writer, stale, requested_revision=2, suffix="stale")


def test_checkpoint_predecessor_must_remain_the_current_head(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    first, first_record, first_checkpoint = _complete_mutation(
        source_revision=1, content=b"first"
    )
    first_commit = _persist(writer, first, requested_revision=1, suffix="first")
    second, second_record, _second_checkpoint = _complete_mutation(
        source_revision=2,
        content=b"second",
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
    stale, _third_record, _third_checkpoint = _complete_mutation(
        source_revision=3,
        content=b"third",
        previous_record=second_record,
        previous_checkpoint=first_checkpoint,
        previous_occurrence_id=_latest_artifact_occurrence_id(
            fixture.database,
            revision=second_commit.revision,
            path=second_record.normalized_path,
        ),
        previous_checkpoint_occurrence_id=_latest_checkpoint_occurrence_id(
            fixture.database,
            revision=first_commit.revision,
            checkpoint_key=first_checkpoint.checkpoint_key,
        ),
        **_phase3_head_kwargs(second_commit, second),
    )

    with pytest.raises(AuthorityRevisionConflict, match="predecessor CAS"):
        _persist(writer, stale, requested_revision=3, suffix="stale-checkpoint")


def test_checkpoint_existing_head_rejects_a_new_initial_root(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    first, first_record, _first_checkpoint = _complete_mutation(
        source_revision=1, content=b"first"
    )
    first_commit = _persist(writer, first, requested_revision=1, suffix="first")
    reset, _second_record, _second_checkpoint = _complete_mutation(
        source_revision=2,
        content=b"second",
        previous_record=first_record,
        previous_occurrence_id=_latest_artifact_occurrence_id(
            fixture.database,
            revision=first_commit.revision,
            path=first_record.normalized_path,
        ),
        **_phase3_head_kwargs(first_commit, first),
    )

    with pytest.raises(AuthorityRevisionConflict, match="initial root reset"):
        _persist(writer, reset, requested_revision=2, suffix="reset-root")


def test_checkpoint_existing_head_requires_occurrence_predecessor(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    first, first_record, first_checkpoint = _complete_mutation(
        source_revision=1, content=b"first"
    )
    first_commit = _persist(writer, first, requested_revision=1, suffix="first")
    missing_occurrence, _second_record, _second_checkpoint = _complete_mutation(
        source_revision=2,
        content=b"second",
        previous_record=first_record,
        previous_checkpoint=first_checkpoint,
        previous_occurrence_id=_latest_artifact_occurrence_id(
            fixture.database,
            revision=first_commit.revision,
            path=first_record.normalized_path,
        ),
        previous_checkpoint_occurrence_id=None,
        **_phase3_head_kwargs(first_commit, first),
    )

    with pytest.raises(AuthorityRevisionConflict, match="predecessor CAS"):
        _persist(
            writer,
            missing_occurrence,
            requested_revision=2,
            suffix="missing-checkpoint-occurrence",
        )


def test_checkpoint_transition_is_checked_against_predecessor_state(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    first, first_record, first_checkpoint = _complete_mutation(
        source_revision=1, content=b"first"
    )
    first_commit = _persist(writer, first, requested_revision=1, suffix="first")
    second, _second_record, _ignored_checkpoint = _complete_mutation(
        source_revision=2,
        content=b"second",
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
    invalid_transition = build_checkpoint_entry(
        checkpoint_key=first_checkpoint.checkpoint_key,
        owner_stage=first_checkpoint.owner_stage,
        input_manifest_sha256=second.checkpoint_entries[0].input_manifest_sha256,
        state=CheckpointState.VALID,
        transition=CheckpointTransition.REATTESTED_VALID,
        validation_sha256="8" * 64,
        previous_checkpoint_id=first_checkpoint.checkpoint_id,
        previous_checkpoint_occurrence_id=_latest_checkpoint_occurrence_id(
            fixture.database,
            revision=first_commit.revision,
            checkpoint_key=first_checkpoint.checkpoint_key,
        ),
        reason_code="INVALID_STATE_EDGE",
    )
    invalid = build_phase3_mutation(
        artifact_records=second.artifact_records,
        artifact_blockers=second.artifact_blockers,
        removals=second.removals,
        checkpoint_entries=(invalid_transition,),
        reopen_plan=second.reopen_plan,
        previous_manifest=second.previous_manifest,
        current_manifest=second.current_manifest,
        change_set=second.change_set,
        previous_head=second.previous_head,
    )

    with pytest.raises(AuthorityRevisionConflict, match="state transition"):
        _persist(writer, invalid, requested_revision=2, suffix="invalid-transition")


def test_writer_rejects_owner_policy_rewrite_that_bypasses_classifier(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    first, first_record, first_checkpoint = _complete_mutation(
        source_revision=1, content=b"first"
    )
    first_commit = _persist(writer, first, requested_revision=1, suffix="first")
    second, _second_record, _second_checkpoint = _complete_mutation(
        source_revision=2,
        content=b"second",
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
    migrated_compilation = compile_owner_registry(
        (
            ArtifactOwnership(
                pattern="results/**",
                owner_stage=5,
                semantic_domain="canonical_result",
                dirty_flag="RESULT_DIRTY",
            ),
        )
    )
    rewritten_registration = register_artifact_owner(
        migrated_compilation, first_record.normalized_path
    )
    rewritten_record = build_artifact_record(
        rewritten_registration, content=b"second"
    )
    bypass = build_phase3_mutation(
        artifact_records=(rewritten_record,),
        checkpoint_entries=second.checkpoint_entries,
        reopen_plan=second.reopen_plan,
    )

    with pytest.raises(
        AuthorityEnvelopePersistenceError, match="complete graph"
    ):
        _persist(writer, bypass, requested_revision=2, suffix="owner-rewrite")


@pytest.mark.parametrize(
    "failure_stage",
    (
        "after_phase3_artifact_records",
        "after_phase3_checkpoint_ledger",
        "after_phase3_reopen_plan",
    ),
)
def test_phase3_failure_injection_rolls_back_all_three_tables_and_revision(
    tmp_path, monkeypatch, failure_stage
):
    fixture = install_foundation(tmp_path, name=f"phase3-{failure_stage}")
    writer = configure_canary(fixture)
    mutation, _record_value, _checkpoint = _complete_mutation(
        source_revision=1, content=b"first"
    )
    before = table_counts(
        fixture.database,
        PHASE3_TABLES
        + (
            "authority_commands",
            "authority_events",
            "authority_receipts",
            "authority_outbox",
        ),
    )

    def fail(stage: str) -> None:
        if stage == failure_stage:
            raise RuntimeError(f"injected {stage}")

    monkeypatch.setattr(writer_module, "_writer_failure_point", fail)
    with pytest.raises(RuntimeError, match="injected"):
        _persist(writer, mutation)
    assert table_counts(fixture.database, tuple(before)) == before
    connection = sqlite3.connect(fixture.database)
    try:
        assert connection.execute(
            "SELECT current_revision FROM authority_workflows"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT next_revision FROM authority_revision_allocator"
        ).fetchone()[0] == 2
    finally:
        connection.close()


def test_two_phase3_writers_cannot_win_the_same_revision(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    barrier = threading.Barrier(2)

    def commit(suffix: str):
        mutation, _record_value, _checkpoint = _complete_mutation(
            source_revision=1, content=suffix.encode("ascii")
        )
        barrier.wait()
        try:
            return "committed", _persist(writer, mutation, suffix=suffix).revision
        except AuthorityRevisionConflict:
            return "conflict", None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(commit, ("left", "right")))

    assert outcomes == [("committed", 2), ("conflict", None)]
