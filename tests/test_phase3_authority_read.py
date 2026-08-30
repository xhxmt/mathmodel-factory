from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

import factory_core.authority_production_writer as writer_module
from factory_core.authority_read_repository import (
    AuthorityReadError,
    AuthorityReadRepository,
)
from factory_core.phase3_artifacts import (
    build_artifact_occurrence,
    build_checkpoint_occurrence,
    build_phase3_mutation,
    build_phase3_previous_head_bootstrap,
)
from tests.support.authority_production import (
    configure_canary,
    install_foundation,
    persist_one,
)
from tests.test_phase3_authority_writer import (
    CHECKPOINT_KEY,
    _blocked_mutation,
    _complete_mutation,
    _latest_artifact_occurrence_id,
    _latest_checkpoint_occurrence_id,
    _persist,
    _phase3_head_kwargs,
)


def _file_identity(path: Path) -> tuple[str, int, int]:
    metadata = path.stat()
    return (
        hashlib.sha256(path.read_bytes()).hexdigest(),
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def test_phase3_command_bundle_is_typed_hash_checked_and_revision_atomic(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    mutation, record, checkpoint = _complete_mutation(
        source_revision=1, content=b"phase3-read"
    )
    committed = _persist(writer, mutation)
    repository = AuthorityReadRepository(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )

    bundle = repository.command_bundle(
        workflow_id="legacy_current", idempotency_key="idempotency-phase3"
    )

    assert bundle.revision == committed.revision == 2
    assert bundle.bundle_sha256 == committed.bundle_sha256
    assert bundle.request_sha256 == committed.request_sha256
    assert bundle.request_schema == committed.request_schema
    assert bundle.bundle_schema == committed.bundle_schema
    assert bundle.phase3_mutation_sha256 == mutation.mutation_sha256
    assert bundle.phase3_mutation == mutation
    assert bundle.phase3_mutation.artifact_records == (record,)
    assert bundle.phase3_mutation.checkpoint_entries == (checkpoint,)
    assert bundle.identity_dict()["schema"] == "authority-read-command-bundle-v2"


def test_legacy_command_bundle_read_shape_and_hash_remain_v1(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    committed, _values = persist_one(writer)
    repository = AuthorityReadRepository(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )

    bundle = repository.command_bundle(
        workflow_id="legacy_current", idempotency_key="idempotency-1"
    )

    assert bundle.bundle_sha256 == committed.bundle_sha256
    assert bundle.phase3_mutation is None
    assert bundle.phase3_mutation_sha256 is None
    assert bundle.identity_dict()["schema"] == "authority-read-command-bundle-v1"
    assert "request_schema" not in bundle.identity_dict()


def test_phase3_query_only_read_creates_no_sidecar_and_modifies_no_bytes(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    mutation, _record_value, _checkpoint = _complete_mutation(
        source_revision=1, content=b"phase3-read-only"
    )
    _persist(writer, mutation)
    connection = sqlite3.connect(fixture.database)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.close()
    wal = Path(str(fixture.database) + "-wal")
    shm = Path(str(fixture.database) + "-shm")
    assert not wal.exists() and not shm.exists()
    before = _file_identity(fixture.database)

    bundle = AuthorityReadRepository(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    ).command_bundle(
        workflow_id="legacy_current", idempotency_key="idempotency-phase3"
    )

    assert bundle.phase3_mutation == mutation
    assert _file_identity(fixture.database) == before
    assert not wal.exists() and not shm.exists()


def test_reader_reconstructs_blocked_phase3_bundle_after_restart(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    mutation = _blocked_mutation(source_revision=1)
    committed = _persist(writer, mutation, suffix="blocked-read")

    repository = AuthorityReadRepository(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    bundle = repository.command_bundle(
        workflow_id="legacy_current",
        idempotency_key="idempotency-blocked-read",
    )
    del repository

    restarted = AuthorityReadRepository(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    state = restarted.phase3_artifact_state(
        "legacy_current", through_revision=committed.revision
    )

    assert bundle.phase3_mutation == mutation
    assert bundle.phase3_mutation.reopen_plan is None
    assert bundle.phase3_mutation.blocked_disposition == mutation.blocked_disposition
    assert bundle.phase3_mutation.blocked_disposition is not None
    assert bundle.identity_dict()["phase3_mutation_sha256"] == mutation.mutation_sha256
    assert (
        bundle.phase3_mutation.blocked_disposition.disposition_id
        == mutation.blocked_disposition.disposition_id
    )
    assert state.present_records == ()
    assert state.tombstones == ()
    assert state.blockers == mutation.current_manifest.blockers


def test_phase3_typed_row_tamper_fails_closed_after_schema_is_restored(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    mutation, _record_value, _checkpoint = _complete_mutation(
        source_revision=1, content=b"phase3-tamper"
    )
    _persist(writer, mutation)
    connection = sqlite3.connect(fixture.database)
    trigger_name = "authority_artifact_records_append_only_update"
    trigger_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        (trigger_name,),
    ).fetchone()[0]
    connection.execute(f'DROP TRIGGER "{trigger_name}"')
    raw = connection.execute(
        "SELECT metadata_json FROM authority_artifact_records WHERE recorded_revision=2"
    ).fetchone()[0]
    value = json.loads(raw)
    value["phase3_mutation"]["mutation_sha256"] = "f" * 64
    connection.execute(
        "UPDATE authority_artifact_records SET metadata_json=? WHERE recorded_revision=2",
        (writer_module._phase3_row_json(value),),
    )
    connection.execute(trigger_sql)
    connection.commit()
    connection.close()
    repository = AuthorityReadRepository(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )

    with pytest.raises(AuthorityReadError, match="Phase-3 mutation identity mismatch"):
        repository.command_bundle(
            workflow_id="legacy_current", idempotency_key="idempotency-phase3"
        )


def test_reader_rejects_adjacent_phase3_previous_head_continuity_tamper(tmp_path):
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
    tampered = build_phase3_mutation(
        artifact_records=second.artifact_records,
        artifact_blockers=second.artifact_blockers,
        removals=second.removals,
        checkpoint_entries=second.checkpoint_entries,
        blocked_disposition=second.blocked_disposition,
        previous_manifest=second.previous_manifest,
        current_manifest=second.current_manifest,
        change_set=second.change_set,
        previous_head=build_phase3_previous_head_bootstrap(
            workflow_id="legacy_current",
            source_revision=second.previous_head.source_revision,
            previous_manifest=second.previous_manifest,
        ),
    )
    blocker_occurrence = build_artifact_occurrence(
        workflow_id="legacy_current",
        revision=second_commit.revision,
        command_id=second_commit.command_id,
        mutation_sha256=tampered.mutation_sha256,
        blocker=tampered.artifact_blockers[0],
    )
    checkpoint_occurrence = build_checkpoint_occurrence(
        workflow_id="legacy_current",
        revision=second_commit.revision,
        command_id=second_commit.command_id,
        mutation_sha256=tampered.mutation_sha256,
        checkpoint_entry=tampered.checkpoint_entries[0],
    )
    connection = sqlite3.connect(fixture.database)
    trigger_rows = connection.execute(
        """
        SELECT name, sql
        FROM sqlite_master
        WHERE type='trigger'
          AND tbl_name IN (
            'authority_artifact_records',
            'authority_checkpoint_ledger',
            'authority_reopen_plans'
          )
          AND name LIKE '%append_only_update'
        """
    ).fetchall()
    for name, _sql in trigger_rows:
        connection.execute(f'DROP TRIGGER "{name}"')
    connection.execute(
        """
        UPDATE authority_artifact_records
        SET artifact_record_id=?, metadata_json=?
        WHERE workflow_id='legacy_current' AND recorded_revision=?
        """,
        (
            blocker_occurrence.occurrence_id,
            writer_module._phase3_row_json(
                {
                    "schema": writer_module.AUTHORITY_PHASE3_ARTIFACT_ROW_SCHEMA,
                    "occurrence": blocker_occurrence.as_dict(),
                    "phase3_mutation": tampered.as_dict(),
                }
            ),
            second_commit.revision,
        ),
    )
    connection.execute(
        """
        UPDATE authority_checkpoint_ledger
        SET checkpoint_id=?, source_record_key=?, payload_json=?
        WHERE workflow_id='legacy_current'
          AND recorded_revision=?
          AND checkpoint_kind='RECORDED'
        """,
        (
            checkpoint_occurrence.occurrence_id,
            f"phase3:checkpoint-occurrence:{checkpoint_occurrence.occurrence_id}",
            writer_module._phase3_row_json(
                {
                    "schema": writer_module.AUTHORITY_PHASE3_CHECKPOINT_ROW_SCHEMA,
                    "occurrence": checkpoint_occurrence.as_dict(),
                    "phase3_mutation": tampered.as_dict(),
                }
            ),
            second_commit.revision,
        ),
    )
    connection.execute(
        """
        UPDATE authority_reopen_plans
        SET evidence_json=?
        WHERE workflow_id='legacy_current' AND recorded_revision=?
        """,
        (
            writer_module._phase3_row_json(
                {
                    "schema": writer_module.AUTHORITY_PHASE3_REOPEN_ROW_SCHEMA,
                    "command_id": second_commit.command_id,
                    "phase3_mutation_sha256": tampered.mutation_sha256,
                    "phase3_mutation": tampered.as_dict(),
                }
            ),
            second_commit.revision,
        ),
    )
    for _name, sql in trigger_rows:
        connection.execute(sql)
    connection.commit()
    connection.close()

    repository = AuthorityReadRepository(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    with pytest.raises(AuthorityReadError, match="previous head continuity differs"):
        repository.phase3_artifact_state(
            "legacy_current", through_revision=second_commit.revision
        )


def test_reader_rejects_an_incomplete_phase3_bundle_on_a_v1_revision(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    persist_one(writer)
    mutation, record, _checkpoint = _complete_mutation(
        source_revision=2, content=b"stray-artifact-row"
    )
    metadata = writer_module._phase3_row_json(
        {
            "schema": writer_module.AUTHORITY_PHASE3_ARTIFACT_ROW_SCHEMA,
            "command_id": "command-1",
            "phase3_mutation_sha256": mutation.mutation_sha256,
            "artifact_record": record.as_dict(),
        }
    )
    connection = sqlite3.connect(fixture.database)
    connection.execute(
        """
        INSERT INTO authority_artifact_records(
            artifact_record_id, workflow_id, artifact_type, artifact_path,
            content_sha256, availability, owner_scope, recorded_revision,
            metadata_json
        ) VALUES (?, 'legacy_current', ?, ?, ?, ?, ?, 2, ?)
        """,
        (
            record.artifact_record_id,
            record.artifact_type,
            record.normalized_path,
            record.content_sha256,
            record.availability.value,
            record.registration.owner_id,
            metadata,
        ),
    )
    connection.commit()
    connection.close()

    repository = AuthorityReadRepository(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    with pytest.raises(AuthorityReadError, match="bundle is incomplete"):
        repository.command_bundle(
            workflow_id="legacy_current", idempotency_key="idempotency-1"
        )


def test_production_cli_import_does_not_load_packaged_phase3_foundation():
    root = Path(__file__).resolve().parents[1]
    script = """
import sys
import factory_core.cli

for name in (
    "factory_core.phase3_artifacts",
    "factory_core.phase3_shadow_runtime",
    "factory_core.authority_production_writer",
    "factory_core.authority_read_repository",
):
    assert name not in sys.modules, name
print("phase3-foundation-not-in-active-cli-imports")
"""
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout == "phase3-foundation-not-in-active-cli-imports\n"
