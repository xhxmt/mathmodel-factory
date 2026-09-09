from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import threading

import pytest

import factory_core.authority_schema as authority_schema
from factory_core.authority_schema import (
    AUTHORITY_MIGRATION_IDS,
    AUTHORITY_SCHEMA_VERSION,
    LEGACY_CURRENT_IMPORTED,
    LEGACY_IMPORTED,
    MIGRATION_BLOCKED_OWNER_AMBIGUOUS,
    MIGRATION_INTERRUPTED,
    MIGRATION_READY,
    UNKNOWN_ASSURANCE,
    AuthorityFutureSchemaError,
    AuthorityMigrationDrift,
    AuthorityMigrationInterrupted,
    AuthorityMigrationLocked,
    AuthorityMigrationRunner,
    authority_schema_status,
    migrate_authority_schema_v2,
)
from factory_core.authority_repository import (
    AuthorityRepository,
    AuthorityRepositoryNotReady,
)


EXPECTED_AUTHORITY_TABLES = {
    "authority_schema_state",
    "authority_schema_migrations",
    "authority_workflows",
    "authority_revision_allocator",
    "authority_contract_pin_sets",
    "authority_commands",
    "authority_events",
    "authority_receipts",
    "authority_idempotency_records",
    "authority_artifact_records",
    "authority_checkpoint_ledger",
    "authority_reopen_plans",
    "authority_outbox",
    "authority_invocations",
    "authority_attempts",
    "authority_process_scopes",
    "authority_project_snapshots",
}

EXPECTED_APPEND_ONLY_TABLES = EXPECTED_AUTHORITY_TABLES - {
    "authority_schema_state",
    "authority_workflows",
    "authority_revision_allocator",
}


def _legacy_db(
    tmp_path,
    version: int,
    *,
    active_stage: int | None = None,
    checkpoint_rows: tuple[tuple[object, ...], ...] = (),
):
    path = tmp_path / f"legacy-v{version}-{active_stage}-{len(checkpoint_rows)}.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE schema_info(singleton INTEGER PRIMARY KEY, schema_version INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO schema_info VALUES (1, ?)", (version,))
        if version <= 5:
            connection.execute(
                """
                CREATE TABLE project_state(
                    singleton INTEGER PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    last_completed_step INTEGER NOT NULL,
                    active_step INTEGER
                )
                """
            )
            connection.execute(
                "INSERT INTO project_state VALUES (1, 'demo', 7, 2, NULL)"
            )
        else:
            connection.execute(
                """
                CREATE TABLE project_state(
                    singleton INTEGER PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    last_completed_step INTEGER NOT NULL,
                    active_step INTEGER,
                    runtime_generation TEXT NOT NULL,
                    scheduler_generation TEXT NOT NULL,
                    last_completed_stage INTEGER NOT NULL,
                    active_stage INTEGER
                )
                """
            )
            connection.execute(
                "INSERT INTO project_state VALUES "
                "(1, 'demo', 7, 2, NULL, 'native_v2', 'stage_v1', 1, ?)",
                (active_stage,),
            )
        if checkpoint_rows:
            connection.execute(
                """
                CREATE TABLE stage_checkpoints(
                    stage_id INTEGER NOT NULL,
                    subtask TEXT NOT NULL,
                    source_step_id INTEGER,
                    completed_step_id INTEGER,
                    input_fingerprint TEXT,
                    output_fingerprint TEXT,
                    completed_revision INTEGER,
                    receipt_json TEXT,
                    PRIMARY KEY(stage_id, subtask)
                )
                """
            )
            connection.executemany(
                "INSERT INTO stage_checkpoints VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                checkpoint_rows,
            )
        connection.commit()
    finally:
        connection.close()
    return path


def _tables(path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        connection.close()


def _legacy_snapshot(path) -> tuple[tuple[object, ...], ...]:
    connection = sqlite3.connect(path)
    try:
        objects = connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name NOT LIKE 'authority_%'
              AND tbl_name NOT LIKE 'authority_%'
            ORDER BY type, name
            """
        ).fetchall()
        rows: list[tuple[object, ...]] = []
        for (name,) in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'authority_%'
            ORDER BY name
            """
        ):
            quoted = str(name).replace('"', '""')
            rows.extend(
                ("row", name, *row)
                for row in connection.execute(f'SELECT * FROM "{quoted}" ORDER BY rowid')
            )
        return tuple((*row,) for row in objects) + tuple(rows)
    finally:
        connection.close()


@pytest.mark.parametrize("version", range(1, 10))
def test_authority_schema_installs_additively_on_legacy_v1_through_v9(tmp_path, version):
    path = _legacy_db(tmp_path, version)
    legacy_before = _legacy_snapshot(path)

    report = migrate_authority_schema_v2(path, owner_token=f"owner-v{version}")

    assert report.source_schema_version == version
    assert report.authority_schema_version == AUTHORITY_SCHEMA_VERSION
    assert report.applied_now == AUTHORITY_MIGRATION_IDS
    assert report.state == MIGRATION_BLOCKED_OWNER_AMBIGUOUS
    assert EXPECTED_AUTHORITY_TABLES <= _tables(path)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT schema_version FROM schema_info WHERE singleton=1"
        ).fetchone()[0] == version
        assert connection.execute(
            "SELECT COUNT(*) FROM authority_schema_migrations"
        ).fetchone()[0] == len(AUTHORITY_MIGRATION_IDS)
    finally:
        connection.close()
    assert _legacy_snapshot(path) == legacy_before


def test_future_legacy_schema_is_rejected_before_authority_tables_are_written(tmp_path):
    path = _legacy_db(tmp_path, 10)
    legacy_before = _legacy_snapshot(path)

    with pytest.raises(AuthorityFutureSchemaError, match="future legacy workflow schema 10"):
        migrate_authority_schema_v2(path, owner_token="future-owner")

    assert not any(name.startswith("authority_") for name in _tables(path))
    assert _legacy_snapshot(path) == legacy_before


def test_future_authority_schema_is_rejected_without_downgrade(tmp_path):
    path = _legacy_db(tmp_path, 9, active_stage=2)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE authority_schema_state(
                singleton INTEGER PRIMARY KEY,
                authority_schema_version INTEGER NOT NULL,
                source_schema_version INTEGER NOT NULL,
                state TEXT NOT NULL,
                last_completed_migration TEXT,
                lock_owner TEXT,
                blocker_code TEXT,
                details_json TEXT NOT NULL
            );
            CREATE TABLE authority_schema_migrations(
                migration_id TEXT PRIMARY KEY,
                checksum_sha256 TEXT NOT NULL,
                source_schema_version INTEGER NOT NULL,
                applied_order INTEGER NOT NULL UNIQUE
            );
            INSERT INTO authority_schema_state VALUES(
                1, 3, 9, 'READY', NULL, NULL, NULL, '{}'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()
    objects_before = _legacy_snapshot(path)
    tables_before = _tables(path)

    with pytest.raises(AuthorityFutureSchemaError, match="future authority schema 3"):
        migrate_authority_schema_v2(path, owner_token="future-authority-owner")

    assert authority_schema_status(path)["authority_schema_version"] == 3
    assert _tables(path) == tables_before
    assert _legacy_snapshot(path) == objects_before


def test_migration_interruption_records_progress_and_same_owner_resumes_idempotently(tmp_path):
    path = _legacy_db(
        tmp_path,
        9,
        active_stage=3,
        checkpoint_rows=((3, "resume", 2, 2, "in", "out", 7, '{"ok":true}'),),
    )
    interrupted_after = "A2_0004_COMMAND_EVENT_RECEIPT_IDEMPOTENCY"

    def interrupt(migration_id: str) -> None:
        if migration_id == interrupted_after:
            raise RuntimeError("injected interruption")

    with pytest.raises(AuthorityMigrationInterrupted, match=interrupted_after):
        AuthorityMigrationRunner(path, after_migration=interrupt).run("resume-owner")

    state = authority_schema_status(path)
    assert state is not None
    assert state["state"] == MIGRATION_INTERRUPTED
    assert state["lock_owner"] == "resume-owner"
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM authority_schema_migrations"
        ).fetchone()[0] == 4
    finally:
        connection.close()
    with pytest.raises(AuthorityRepositoryNotReady, match="not write-ready"):
        AuthorityRepository(path, write_shadow=True).table_count("authority_commands")

    resumed = AuthorityMigrationRunner(path).run("resume-owner")
    assert resumed.state == MIGRATION_READY
    assert resumed.already_applied == AUTHORITY_MIGRATION_IDS[:4]
    assert resumed.applied_now == AUTHORITY_MIGRATION_IDS[4:]

    repeated = AuthorityMigrationRunner(path).run("different-owner-after-ready")
    assert repeated.state == MIGRATION_READY
    assert repeated.applied_now == ()
    assert repeated.already_applied == AUTHORITY_MIGRATION_IDS


def test_durable_migration_lock_rejects_a_concurrent_owner(tmp_path):
    path = _legacy_db(
        tmp_path,
        9,
        active_stage=2,
        checkpoint_rows=((2, "lock", 1, 1, "in", "out", 7, '{"ok":true}'),),
    )
    first = AuthorityMigrationRunner(path)
    assert first.acquire("owner-a") == "RUNNING"

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(AuthorityMigrationRunner(path).run, "owner-b")
        with pytest.raises(AuthorityMigrationLocked, match="owner-a"):
            future.result(timeout=5)

    assert first.run("owner-a").state == MIGRATION_READY


def test_ambiguous_legacy_current_owner_is_preserved_as_a_migration_blocker(tmp_path):
    path = _legacy_db(tmp_path, 9, active_stage=None)

    report = migrate_authority_schema_v2(path, owner_token="ambiguous-owner")

    assert report.state == MIGRATION_BLOCKED_OWNER_AMBIGUOUS
    assert report.blocker_code == MIGRATION_BLOCKED_OWNER_AMBIGUOUS
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM authority_checkpoint_ledger"
        ).fetchone()
        assert row["checkpoint_kind"] == LEGACY_CURRENT_IMPORTED
        assert row["assurance"] == UNKNOWN_ASSURANCE
        assert row["owner_stage"] is None
        assert row["owner_resolution"] == MIGRATION_BLOCKED_OWNER_AMBIGUOUS
        assert "legacy_unknown" in row["payload_json"]
        workflow = connection.execute(
            "SELECT * FROM authority_workflows WHERE workflow_id='legacy_current'"
        ).fetchone()
        assert workflow["project_generation"] == "legacy_unknown"
        assert workflow["run_generation"] == "legacy_unknown"
        assert workflow["contract_pin_set_sha256"] is None
        assert workflow["contract_pin_availability"] == "legacy_unknown"
    finally:
        connection.close()
    with pytest.raises(AuthorityRepositoryNotReady, match="not write-ready"):
        AuthorityRepository(path, write_shadow=True).table_count("authority_commands")


def test_project_state_stage_fields_do_not_imply_a_unique_checkpoint_owner(tmp_path):
    path = _legacy_db(tmp_path, 9, active_stage=3)

    report = migrate_authority_schema_v2(path, owner_token="conflicting-stage-owner")

    assert report.state == MIGRATION_BLOCKED_OWNER_AMBIGUOUS
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM authority_checkpoint_ledger"
        ).fetchone()
        assert row["owner_stage"] is None
        assert row["owner_resolution"] == MIGRATION_BLOCKED_OWNER_AMBIGUOUS
        payload = json.loads(row["payload_json"])
        assert payload["active_stage"] == 3
        assert payload["last_completed_stage"] == 1
    finally:
        connection.close()


def test_legacy_checkpoint_backfill_has_only_legacy_or_unknown_assurance(tmp_path):
    path = _legacy_db(
        tmp_path,
        9,
        checkpoint_rows=(
            (2, "prepare", 3, 3, "input-a", "output-a", 5, '{"receipt":"ok"}'),
            (3, "execute", 4, 4, "input-b", "output-b", 6, "not-json"),
        ),
    )

    report = migrate_authority_schema_v2(path, owner_token="checkpoint-owner")

    assert report.state == MIGRATION_READY
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM authority_checkpoint_ledger ORDER BY owner_stage"
        ).fetchall()
        assert {row["checkpoint_kind"] for row in rows} == {LEGACY_CURRENT_IMPORTED}
        assert {row["assurance"] for row in rows} == {
            LEGACY_IMPORTED,
            UNKNOWN_ASSURANCE,
        }
        assert [row["owner_stage"] for row in rows] == [2, 3]
        assert {row["owner_resolution"] for row in rows} == {"EXPLICIT_LEGACY_OWNER"}
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                """
                INSERT INTO authority_checkpoint_ledger(
                    checkpoint_id, workflow_id, checkpoint_kind, checkpoint_key,
                    assurance, owner_stage, owner_resolution, source_record_key,
                    payload_json, recorded_revision
                ) VALUES (
                    'forged-verified-legacy', 'legacy_current',
                    'LEGACY_CURRENT_IMPORTED', 'forged', 'VERIFIED', 2,
                    'EXPLICIT_LEGACY_OWNER', 'forged:verified', '{}', 7
                )
                """
            )
    finally:
        connection.close()


def test_authority_ledger_and_migration_history_are_append_only(tmp_path):
    path = _legacy_db(tmp_path, 9, active_stage=4)
    migrate_authority_schema_v2(path, owner_token="append-owner")
    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE authority_checkpoint_ledger SET assurance='UNKNOWN'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM authority_schema_migrations WHERE migration_id=?",
                (AUTHORITY_MIGRATION_IDS[0],),
            )
        trigger_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        for table in EXPECTED_APPEND_ONLY_TABLES:
            assert f"{table}_append_only_update" in trigger_names
            assert f"{table}_append_only_delete" in trigger_names
            for guard_number in range(
                1, len(authority_schema._APPEND_ONLY_UNIQUE_KEYS[table]) + 1
            ):
                assert f"{table}_append_only_insert_guard_{guard_number}" in trigger_names
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("interrupt_after", "mutation", "restore", "message"),
    (
        (
            "A2_0001_BOOTSTRAP",
            "UPDATE schema_info SET schema_version=10 WHERE singleton=1",
            "UPDATE schema_info SET schema_version=9 WHERE singleton=1",
            "future legacy workflow schema 10",
        ),
        (
            "A2_0002_WORKFLOW_REVISION",
            "UPDATE project_state SET revision=10 WHERE singleton=1",
            "UPDATE project_state SET revision=7 WHERE singleton=1",
            "legacy source identity drifted",
        ),
    ),
)
def test_legacy_source_drift_between_committed_steps_fails_closed_and_can_resume_after_restore(
    tmp_path, interrupt_after, mutation, restore, message
):
    path = _legacy_db(
        tmp_path,
        9,
        active_stage=2,
        checkpoint_rows=((2, "source-fence", 1, 1, "in", "out", 7, '{"ok":true}'),),
    )

    def mutate_and_interrupt(migration_id: str) -> None:
        if migration_id != interrupt_after:
            return
        connection = sqlite3.connect(path)
        try:
            connection.execute(mutation)
            connection.commit()
        finally:
            connection.close()
        raise RuntimeError("injected source drift")

    with pytest.raises(AuthorityMigrationInterrupted, match=interrupt_after):
        AuthorityMigrationRunner(path, after_migration=mutate_and_interrupt).run(
            "source-fence-owner"
        )

    with pytest.raises((AuthorityMigrationDrift, AuthorityFutureSchemaError), match=message):
        AuthorityMigrationRunner(path).run("source-fence-owner")
    assert authority_schema_status(path)["state"] == MIGRATION_INTERRUPTED

    connection = sqlite3.connect(path)
    try:
        connection.execute(restore)
        connection.commit()
    finally:
        connection.close()
    resumed = AuthorityMigrationRunner(path).run("source-fence-owner")
    assert resumed.state == MIGRATION_READY
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT current_revision FROM authority_workflows"
        ).fetchone()[0] == 7
        assert connection.execute(
            "SELECT recorded_revision FROM authority_checkpoint_ledger"
        ).fetchone()[0] == 7
    finally:
        connection.close()


def test_final_ready_transition_revalidates_the_legacy_source_fingerprint(tmp_path):
    path = _legacy_db(
        tmp_path,
        9,
        checkpoint_rows=((2, "final-fence", 1, 1, "in", "out", 7, '{"ok":true}'),),
    )

    def mutate_after_last_step(migration_id: str) -> None:
        if migration_id != "A2_0009_LEGACY_CHECKPOINT_BACKFILL":
            return
        connection = sqlite3.connect(path)
        try:
            connection.execute("UPDATE project_state SET revision=10 WHERE singleton=1")
            connection.commit()
        finally:
            connection.close()

    with pytest.raises(AuthorityMigrationDrift, match="legacy source identity drifted"):
        AuthorityMigrationRunner(path, after_migration=mutate_after_last_step).run(
            "final-source-fence-owner"
        )
    state = authority_schema_status(path)
    assert state["state"] == MIGRATION_INTERRUPTED
    assert state["lock_owner"] == "final-source-fence-owner"


def test_source_drift_seen_during_acquire_moves_owned_run_to_interrupted(tmp_path):
    path = _legacy_db(tmp_path, 9, active_stage=2)
    runner = AuthorityMigrationRunner(path)
    assert runner.acquire("acquire-source-owner") == "RUNNING"
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE project_state SET revision=10 WHERE singleton=1")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(AuthorityMigrationDrift, match="legacy source identity drifted"):
        runner.run("acquire-source-owner")
    state = authority_schema_status(path)
    assert state["state"] == MIGRATION_INTERRUPTED
    assert state["blocker_code"] == "AuthorityMigrationDrift"


def _interrupt_after(path, migration_id: str, owner: str) -> None:
    def interrupt(current: str) -> None:
        if current == migration_id:
            raise RuntimeError("injected audit pause")

    with pytest.raises(AuthorityMigrationInterrupted, match=migration_id):
        AuthorityMigrationRunner(path, after_migration=interrupt).run(owner)


@pytest.mark.parametrize(
    "mutation",
    (
        "UPDATE authority_schema_migrations SET checksum_sha256='" + "0" * 64
        + "' WHERE applied_order=1",
        "UPDATE authority_schema_migrations SET source_schema_version=8 WHERE applied_order=1",
        "UPDATE authority_schema_migrations SET applied_order=99 WHERE applied_order=1",
    ),
)
def test_migration_history_checksum_version_and_order_drift_fail_closed(tmp_path, mutation):
    path = _legacy_db(
        tmp_path,
        9,
        checkpoint_rows=((2, "history", 1, 1, "in", "out", 7, '{"ok":true}'),),
    )
    _interrupt_after(
        path, "A2_0004_COMMAND_EVENT_RECEIPT_IDEMPOTENCY", "history-owner"
    )
    connection = sqlite3.connect(path)
    try:
        connection.execute(mutation)
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(AuthorityMigrationDrift):
        AuthorityMigrationRunner(path).run("history-owner")


def test_hook_implementation_identity_is_bound_into_applied_checksum(tmp_path, monkeypatch):
    path = _legacy_db(
        tmp_path,
        9,
        checkpoint_rows=((2, "hook", 1, 1, "in", "out", 7, '{"ok":true}'),),
    )
    _interrupt_after(path, "A2_0002_WORKFLOW_REVISION", "hook-owner")

    def changed_hook(connection):
        del connection

    monkeypatch.setitem(authority_schema._HOOKS, "_backfill_workflow", changed_hook)
    with pytest.raises(AuthorityMigrationDrift, match="checksum drifted: A2_0002"):
        AuthorityMigrationRunner(path).run("hook-owner")


def test_bootstrap_ddl_is_bound_to_the_first_migration_checksum():
    migration = authority_schema.MIGRATIONS[0]

    assert migration.migration_id == "A2_0001_BOOTSTRAP"
    assert migration.statements == authority_schema._BOOTSTRAP_STATEMENTS
    assert all(statement.strip() for statement in migration.statements)


def test_same_name_noop_trigger_squatting_is_detected_before_resume(tmp_path):
    path = _legacy_db(
        tmp_path,
        9,
        checkpoint_rows=((2, "trigger", 1, 1, "in", "out", 7, '{"ok":true}'),),
    )
    _interrupt_after(path, "A2_0007_EXECUTION_SCOPES", "trigger-owner")
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TRIGGER authority_commands_append_only_update
            BEFORE UPDATE ON authority_commands
            BEGIN
                SELECT 1;
            END
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(AuthorityMigrationDrift, match="sqlite_master identity drifted"):
        AuthorityMigrationRunner(path).run("trigger-owner")


def _different_unique_value(value: object, discriminator: int) -> object:
    if type(value) is int:
        return value + 1000 + discriminator
    if type(value) is str:
        if len(value) == 64 and set(value) <= set("0123456789abcdef"):
            replacement = ("f" if value != "f" * 64 else "e") * 64
            return replacement
        return f"{value}-replace-{discriminator}"
    raise AssertionError(f"unexpected unique-key value: {value!r}")


def test_insert_or_replace_cannot_bypass_any_append_only_unique_identity(tmp_path):
    path = _legacy_db(tmp_path, 9, active_stage=4)
    migrate_authority_schema_v2(path, owner_token="replace-guard-owner")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        assert connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        connection.executescript(
            """
            INSERT INTO authority_contract_pin_sets VALUES(
                '1111111111111111111111111111111111111111111111111111111111111111',
                'pins-v1', '{}', 'TEST', 1
            );
            INSERT INTO authority_commands VALUES(
                'command-seed', 'legacy_current', 'demo', 7, 8, 'SHADOW',
                'command-v1', '{}',
                '2222222222222222222222222222222222222222222222222222222222222222',
                '1111111111111111111111111111111111111111111111111111111111111111',
                'idem-seed'
            );
            INSERT INTO authority_events VALUES(
                'event-seed', 'legacy_current', 8, 'command-seed', 'RECORDED',
                'event-v1', '{}',
                '3333333333333333333333333333333333333333333333333333333333333333'
            );
            INSERT INTO authority_receipts VALUES(
                'receipt-seed', 'legacy_current', 8, 'command-seed', 'event-seed',
                'RECORDED', 'receipt-v1', '{}',
                '4444444444444444444444444444444444444444444444444444444444444444'
            );
            INSERT INTO authority_idempotency_records VALUES(
                'workflow', 'legacy_current', 'idem-seed',
                'authority-command-envelope-request-v1',
                '5555555555555555555555555555555555555555555555555555555555555555',
                'command-seed', 'receipt-seed', 8
            );
            INSERT INTO authority_artifact_records VALUES(
                'artifact-seed', 'legacy_current', 'TEST', 'artifact.txt', NULL,
                'legacy_unknown', 'workflow', NULL, '{}'
            );
            INSERT INTO authority_reopen_plans VALUES(
                'reopen-seed', 'legacy_current', 7, 'workflow', 'TEST', '{}', 8
            );
            INSERT INTO authority_outbox VALUES(
                'message-seed', 'legacy_current', 8, 'event-seed', 'test.topic',
                'outbox-v1', '{}',
                '6666666666666666666666666666666666666666666666666666666666666666'
            );
            INSERT INTO authority_invocations VALUES(
                'invocation-seed', 'legacy_current', 'command-seed', 'TEST', 1, 8,
                'scope-v1', '{}', '{}'
            );
            INSERT INTO authority_attempts VALUES(
                'attempt-seed', 'invocation-seed', 1, 8, 'scope-v1', '{}', '{}'
            );
            INSERT INTO authority_process_scopes VALUES(
                'process-seed', 'attempt-seed', 'TEST', 'pid:1', 8,
                'scope-v1', '{}', '{}'
            );
            INSERT INTO authority_project_snapshots VALUES(
                'snapshot-seed', 'legacy_current', 'demo', 7, 'PARTIAL', NULL,
                'snapshot-v1', '{}',
                '7777777777777777777777777777777777777777777777777777777777777777'
            );
            """
        )
        connection.commit()

        for table, unique_keys in authority_schema._APPEND_ONLY_UNIQUE_KEYS.items():
            sqlite_unique_keys = {
                tuple(
                    row[2]
                    for row in connection.execute(
                        f"PRAGMA index_info('{index[1]}')"
                    )
                )
                for index in connection.execute(f"PRAGMA index_list('{table}')")
                if index[2]
            }
            assert sqlite_unique_keys == set(unique_keys)
            columns = tuple(
                row[1] for row in connection.execute(f"PRAGMA table_info('{table}')")
            )
            seed = connection.execute(f'SELECT * FROM "{table}" LIMIT 1').fetchone()
            assert seed is not None
            for discriminator, target_key in enumerate(unique_keys, start=1):
                candidate = dict(seed)
                for other_key in unique_keys:
                    if other_key == target_key:
                        continue
                    for column in other_key:
                        if column not in target_key:
                            candidate[column] = _different_unique_value(
                                candidate[column], discriminator
                            )
                placeholders = ",".join("?" for _column in columns)
                with pytest.raises(sqlite3.IntegrityError, match="identity conflict"):
                    connection.execute(
                        f'INSERT OR REPLACE INTO "{table}" VALUES ({placeholders})',
                        tuple(candidate[column] for column in columns),
                    )
    finally:
        connection.close()


def test_same_owner_concurrent_runners_do_not_corrupt_migration_history(tmp_path):
    path = _legacy_db(
        tmp_path,
        9,
        checkpoint_rows=((2, "same-owner", 1, 1, "in", "out", 7, '{"ok":true}'),),
    )
    barrier = threading.Barrier(2)

    class SynchronizedRunner(AuthorityMigrationRunner):
        def acquire(self, owner_token: str) -> str:
            result = super().acquire(owner_token)
            barrier.wait(timeout=5)
            return result

    def run_once() -> tuple[str, str]:
        try:
            report = SynchronizedRunner(path).run("shared-owner-token")
            return ("ok", report.state)
        except AuthorityMigrationLocked as exc:
            return ("locked", str(exc))

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(lambda _index: run_once(), range(2)))

    assert sorted(outcome[0] for outcome in outcomes) == ["locked", "ok"]
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT migration_id, applied_order FROM authority_schema_migrations "
            "ORDER BY applied_order"
        ).fetchall()
        assert rows == list(zip(AUTHORITY_MIGRATION_IDS, range(1, 10), strict=True))
    finally:
        connection.close()
    assert AuthorityMigrationRunner(path).run("post-concurrency-audit").state == MIGRATION_READY


def test_authority_schema_version_and_migration_documentation_match_code():
    document = (
        Path(__file__).resolve().parents[1]
        / "docs/architecture/AUTHORITY_SCHEMA_V2_PHASE2.md"
    ).read_text(encoding="utf-8")
    normalized_document = " ".join(document.split())

    assert f"AUTHORITY_SCHEMA_VERSION = {AUTHORITY_SCHEMA_VERSION}" in document
    assert "AUTHORITY_SCHEMA_V2_WRITE_SHADOW = false" in document
    assert authority_schema.LEGACY_SOURCE_IDENTITY_SCHEMA in document
    assert "authority-command-envelope-request-v1" in document
    assert "no public generic transaction" in normalized_document
    assert "INSERT OR REPLACE" in document
    for migration_id in AUTHORITY_MIGRATION_IDS:
        assert f"`{migration_id}`" in document
