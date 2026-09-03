from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

import factory_core.authority_production_schema as production_schema
from factory_core.authority_operations import (
    AuthorityOperationConflict,
    AuthorityOperationError,
    create_authority_backup,
    restore_authority_backup,
    initial_database_identity_binding,
)
from factory_core.authority_operator_workflow import run_authority_migrate_operation
from factory_core.authority_production_schema import (
    PRODUCTION_MIGRATION_CHECKSUMS,
    PRODUCTION_MIGRATION_IDS,
    PRODUCTION_MIGRATIONS,
    AuthorityProductionFutureSchema,
    AuthorityProductionMigrationLocked,
    AuthorityProductionMigrationRunner,
    AuthorityProductionSchemaDrift,
    AuthorityProductionSourceError,
    production_preflight,
    verify_production_installation,
)
from factory_core.authority_schema import MIGRATIONS, migrate_authority_schema_v2
from tests.support.authority_production import (
    create_real_schema_v9,
    install_foundation,
    prepare_initial_backup,
)


FROZEN_A2_0001_0009 = (
    "320c9e5fe62c641d085dfa18fae7aefbaa68c6c5124eebccb7902686fe705a04",
    "467767eb1006d9c6d1277ad4fe2f00fcd7eef38611904a9ac799a2349339add4",
    "27e722149118f3a550e3b818a53b79f15fdb8771d5de8ad5d4bca1979e940e0c",
    "3d3bfe695d1c9df3cc1420c4daa917bf2faf6de6093b2c5b671b0af01cac5aa5",
    "d1776f417a4d12b59e3f357e885b0ef32e5784039b046709e9dd4d2bca32a5f2",
    "74d6721a92900cd2066174e63edc9798da9a850876d47088c901584cee86c2dd",
    "8cffbb5d8a8f54792cccc7de2f92ca4f7fa19479cae70b87457210207ac13d8c",
    "94d3dbc018bd90c67a775daadeab0c11e901f076d8d3848cb28815f0b77027bf",
    "60ee72de6a201dc788511aec19ec01543bfd41671638e9850b8749e32de835f8",
)
FROZEN_A2_0010_0013 = (
    "769bbdd0dd0baac2a10238b6ceadadce9242a51e9923dee1c933db47ffffe418",
    "ec09ec8325b1b3d3c08f26ae1e43871cc8622145068a2514f2faee2e5a16d5bf",
    "721a36e275de130172a06faf82d5b298d7418bf0f7561177ea580e79b7d7caf2",
    "be46c339e1e564aa40aa205fbb249e907281c8cf0b5e31a7833d90cd0e0f8c25",
)
FROZEN_A2_0014_0016 = (
    "1cdf905f5712eb04445eed9da73ac8a48cf3fb3c6115a02c4a6ccb1c54b99a75",
    "69f50ea0989018d6dc7db8743fdf7f2875152f292933054bb21b5760fcd42b15",
    "6fa6c71a5f76388eab41a6d9e301cbce1fdce7ee041b71c9f72824eee1cb7e37",
)


def _migration_statement_bytes_sha256(migrations) -> str:
    digest = hashlib.sha256()
    for migration in migrations:
        migration_id = migration.migration_id.encode("utf-8")
        digest.update(len(migration_id).to_bytes(8, "big"))
        digest.update(migration_id)
        digest.update(len(migration.statements).to_bytes(8, "big"))
        for statement in migration.statements:
            payload = statement.encode("utf-8")
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
    return digest.hexdigest()


def test_published_shadow_migration_checksums_are_unchanged_and_suffix_is_append_only():
    assert tuple(item.checksum_sha256 for item in MIGRATIONS) == FROZEN_A2_0001_0009
    assert PRODUCTION_MIGRATION_IDS == (
        "A2_0010_PRODUCTION_MIGRATION_HISTORY",
        "A2_0011_WRITER_FENCE_AND_SWITCH",
        "A2_0012_TRANSACTIONAL_OUTBOX_DELIVERY",
        "A2_0013_OPERATIONAL_EVIDENCE",
        "A2_0014_DATABASE_IDENTITY_AND_BACKUP_LINEAGE",
        "A2_0015_PHASE9_RUN_GENERATION",
        "A2_0016_PHASE9_FORENSIC_REPLAY",
        "A2_0017_PHASE9_AUDIT_HARDENING",
        "A2_0018_PHASE9_P0_RUNNER_ATTESTATION",
        "A2_0019_PHASE9_REPLAY_EVIDENCE_ATTESTATION",
    )
    assert PRODUCTION_MIGRATION_CHECKSUMS[:4] == FROZEN_A2_0010_0013
    assert PRODUCTION_MIGRATION_CHECKSUMS[4:7] == FROZEN_A2_0014_0016
    assert PRODUCTION_MIGRATION_CHECKSUMS[7] == (
        "c886700e817098e04c325d414a0b0ed7f267a84ea60a05a0f0501e95f86fcc4d"
    )
    assert PRODUCTION_MIGRATION_CHECKSUMS[8] == (
        "eca9538c259285853547bf451a8fbf56ca8580d3963ff9b2537f979e162d5121"
    )
    assert PRODUCTION_MIGRATION_CHECKSUMS[9] == (
        "355c9419f56aa6266b3676f741e0e37a63856e820fa054e9bc217027f23646db"
    )
    assert len(PRODUCTION_MIGRATION_CHECKSUMS) == len(set(PRODUCTION_MIGRATION_CHECKSUMS)) == 10


def test_a2_0001_through_a2_0016_statement_bytes_are_frozen():
    assert _migration_statement_bytes_sha256(MIGRATIONS) == (
        "a1ee0dd9aa3735754c7f1a1dec98a60b3563ad45e9319191152e22455ec9d3d7"
    )
    assert _migration_statement_bytes_sha256(PRODUCTION_MIGRATIONS[:5]) == (
        "9a8b6abd62912b4d231d96a7c5ad4ce648278ae9262c658d90e94ea025447470"
    )
    assert _migration_statement_bytes_sha256(PRODUCTION_MIGRATIONS[:7]) == (
        "cc9cd3b778ce62eb38f374d5488677b4ece0f7112bdd0f1d6101923632ce4e31"
    )


def test_production_foundation_document_freezes_audit_and_migration_identities():
    document = (
        Path(__file__).resolve().parents[1]
        / "docs/architecture/PHASE2_PRODUCTION_AUTHORITY_FOUNDATION.md"
    ).read_text(encoding="utf-8")
    assert "PHASE2_8_SHADOW_ACCEPTANCE_PRO_AUDIT_20260826_FINAL2.zip" in document
    assert "930,349" in document
    assert "2ca331ef3f9952326119a4aa415755675129fa4163d5f0404c873038deb7811a" in document
    assert "467 passed in 21.43s" in document
    for migration, checksum in zip(PRODUCTION_MIGRATION_IDS, PRODUCTION_MIGRATION_CHECKSUMS):
        assert f"`{migration}`" in document
        assert f"`{checksum}`" in document


def test_preflight_requires_a_complete_real_sqlite_state_store_schema_v9(tmp_path):
    toy = tmp_path / "toy.db"
    connection = sqlite3.connect(toy)
    connection.executescript(
        "CREATE TABLE schema_info(singleton INTEGER PRIMARY KEY, schema_version INTEGER);"
        "INSERT INTO schema_info VALUES(1,9);"
        "CREATE TABLE project_state(singleton INTEGER PRIMARY KEY, project_id TEXT);"
    )
    connection.close()

    with pytest.raises(AuthorityProductionSourceError, match="complete SQLiteStateStore"):
        production_preflight(toy, database_id="toy")


def test_real_schema_v9_preflight_and_foundation_are_default_v1_only(tmp_path):
    fixture = install_foundation(tmp_path)
    connection = sqlite3.connect(fixture.database)
    connection.row_factory = sqlite3.Row
    try:
        state = verify_production_installation(connection)
        writer = connection.execute(
            "SELECT * FROM authority_production_writer_state WHERE singleton=1"
        ).fetchone()
        history = connection.execute(
            "SELECT migration_id, checksum_sha256 FROM authority_production_migrations "
            "ORDER BY applied_order"
        ).fetchall()
    finally:
        connection.close()
    assert state["state"] == "READY"
    assert writer["switch_mode"] == "V1_ONLY"
    assert writer["writer_enabled"] == 0
    assert tuple(row[0] for row in history) == PRODUCTION_MIGRATION_IDS
    assert tuple(row[1] for row in history) == PRODUCTION_MIGRATION_CHECKSUMS


def test_a2_0018_rejects_direct_sql_without_trusted_runner_capability(tmp_path):
    fixture = install_foundation(tmp_path)
    connection = sqlite3.connect(fixture.database)
    try:
        with pytest.raises(sqlite3.OperationalError, match="phase9_p0_write_capability"):
            connection.execute(
                """
                INSERT INTO authority_production_phase9_p0_runner_authorizations(
                    authorization_id, nonce_sha256, project_id, workflow_id,
                    run_generation, source_commit, source_tree, source_parent,
                    source_inventory_sha256, live_binding_sha256, spec_sha256,
                    operator_uid, operator_account, issued_at, expires_at,
                    intended_evidence_root, python_identity_sha256,
                    authorization_json, authorization_receipt_sha256
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "direct-sql", "1" * 64, "demo", "legacy_current",
                    "untrusted-run", "a" * 40, "b" * 40, "c" * 40,
                    "2" * 64, "3" * 64, "4" * 64, 0, "operator", 1, 2,
                    str(tmp_path / "evidence"), "5" * 64, "{}", "6" * 64,
                ),
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM authority_production_phase9_p0_runner_authorizations"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_ready_a2_0014_installation_upgrades_additively_through_a2_0018(
    tmp_path, monkeypatch
):
    migrations = production_schema.PRODUCTION_MIGRATIONS
    migration_ids = production_schema.PRODUCTION_MIGRATION_IDS
    migration_checksums = production_schema.PRODUCTION_MIGRATION_CHECKSUMS
    monkeypatch.setattr(production_schema, "PRODUCTION_MIGRATIONS", migrations[:5])
    monkeypatch.setattr(production_schema, "PRODUCTION_MIGRATION_IDS", migration_ids[:5])
    monkeypatch.setattr(
        production_schema, "PRODUCTION_MIGRATION_CHECKSUMS", migration_checksums[:5]
    )
    monkeypatch.setattr(production_schema, "AUTHORITY_PRODUCTION_SCHEMA_VERSION", 2)
    fixture = install_foundation(tmp_path, name="a2-0014-upgrade")

    monkeypatch.setattr(production_schema, "PRODUCTION_MIGRATIONS", migrations)
    monkeypatch.setattr(production_schema, "PRODUCTION_MIGRATION_IDS", migration_ids)
    monkeypatch.setattr(
        production_schema, "PRODUCTION_MIGRATION_CHECKSUMS", migration_checksums
    )
    monkeypatch.setattr(production_schema, "AUTHORITY_PRODUCTION_SCHEMA_VERSION", 7)
    report = AuthorityProductionMigrationRunner(
        fixture.database,
        database_id="a2-0014-upgrade-db",
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        identity_binding=initial_database_identity_binding(fixture.backup),
        pre_authority_backup=fixture.backup_path,
    ).run("a2-0015-upgrade-owner")

    assert report.applied_now == (
        "A2_0015_PHASE9_RUN_GENERATION",
        "A2_0016_PHASE9_FORENSIC_REPLAY",
        "A2_0017_PHASE9_AUDIT_HARDENING",
        "A2_0018_PHASE9_P0_RUNNER_ATTESTATION",
        "A2_0019_PHASE9_REPLAY_EVIDENCE_ATTESTATION",
    )
    connection = sqlite3.connect(fixture.database)
    connection.row_factory = sqlite3.Row
    try:
        state = verify_production_installation(connection)
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'authority_production_run_generation%'"
            )
        }
    finally:
        connection.close()
    assert state["production_schema_version"] == 7
    assert names == {
        "authority_production_run_generations",
        "authority_production_run_generation_current",
        "authority_production_run_generation_creation_receipts",
        "authority_production_run_generation_idempotency",
        "authority_production_run_generation_successions",
        "authority_production_run_generation_source_inventories",
        "authority_production_run_generation_authorization_consumptions",
    }


def test_ready_a2_0015_installation_upgrades_additively_through_a2_0018(
    tmp_path, monkeypatch
):
    migrations = production_schema.PRODUCTION_MIGRATIONS
    migration_ids = production_schema.PRODUCTION_MIGRATION_IDS
    migration_checksums = production_schema.PRODUCTION_MIGRATION_CHECKSUMS
    monkeypatch.setattr(production_schema, "PRODUCTION_MIGRATIONS", migrations[:6])
    monkeypatch.setattr(production_schema, "PRODUCTION_MIGRATION_IDS", migration_ids[:6])
    monkeypatch.setattr(
        production_schema, "PRODUCTION_MIGRATION_CHECKSUMS", migration_checksums[:6]
    )
    monkeypatch.setattr(production_schema, "AUTHORITY_PRODUCTION_SCHEMA_VERSION", 3)
    fixture = install_foundation(tmp_path, name="a2-0015-upgrade")

    monkeypatch.setattr(production_schema, "PRODUCTION_MIGRATIONS", migrations)
    monkeypatch.setattr(production_schema, "PRODUCTION_MIGRATION_IDS", migration_ids)
    monkeypatch.setattr(
        production_schema, "PRODUCTION_MIGRATION_CHECKSUMS", migration_checksums
    )
    monkeypatch.setattr(production_schema, "AUTHORITY_PRODUCTION_SCHEMA_VERSION", 7)
    report = AuthorityProductionMigrationRunner(
        fixture.database,
        database_id="a2-0015-upgrade-db",
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        identity_binding=initial_database_identity_binding(fixture.backup),
        pre_authority_backup=fixture.backup_path,
    ).run("a2-0016-upgrade-owner")

    assert report.applied_now == (
        "A2_0016_PHASE9_FORENSIC_REPLAY",
        "A2_0017_PHASE9_AUDIT_HARDENING",
        "A2_0018_PHASE9_P0_RUNNER_ATTESTATION",
        "A2_0019_PHASE9_REPLAY_EVIDENCE_ATTESTATION",
    )
    connection = sqlite3.connect(fixture.database)
    connection.row_factory = sqlite3.Row
    try:
        state = verify_production_installation(connection)
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'authority_production_phase9_%'"
            )
        }
    finally:
        connection.close()
    assert state["production_schema_version"] == 7
    assert names == {
        "authority_production_phase9_replays",
        "authority_production_phase9_replay_events",
        "authority_production_phase9_terminal_receipts",
        "authority_production_phase9_replay_idempotency",
        "authority_production_phase9_replay_current",
        "authority_production_phase9_evidence_receipts",
        "authority_production_phase9_gate_consumptions",
        "authority_production_phase9_p0_runner_authorizations",
        "authority_production_phase9_p0_runner_consumptions",
        "authority_production_phase9_p0_runner_attestations",
        "authority_production_phase9_replay_runtime_authorizations",
        "authority_production_phase9_replay_runtime_completions",
        "authority_production_phase9_replay_runtime_records",
        "authority_production_phase9_replay_evidence_authorizations",
        "authority_production_phase9_replay_evidence_consumptions",
        "authority_production_phase9_replay_evidence_attestations",
        "authority_production_phase9_replay_evidence_attestation_items",
        "authority_production_phase9_start_authorizations",
        "authority_production_phase9_start_authorization_consumptions",
    }


def test_ready_a2_0016_installation_upgrades_additively_through_a2_0018(
    tmp_path, monkeypatch
):
    migrations = production_schema.PRODUCTION_MIGRATIONS
    migration_ids = production_schema.PRODUCTION_MIGRATION_IDS
    migration_checksums = production_schema.PRODUCTION_MIGRATION_CHECKSUMS
    monkeypatch.setattr(production_schema, "PRODUCTION_MIGRATIONS", migrations[:7])
    monkeypatch.setattr(production_schema, "PRODUCTION_MIGRATION_IDS", migration_ids[:7])
    monkeypatch.setattr(
        production_schema, "PRODUCTION_MIGRATION_CHECKSUMS", migration_checksums[:7]
    )
    monkeypatch.setattr(production_schema, "AUTHORITY_PRODUCTION_SCHEMA_VERSION", 4)
    fixture = install_foundation(tmp_path, name="a2-0016-upgrade")

    monkeypatch.setattr(production_schema, "PRODUCTION_MIGRATIONS", migrations)
    monkeypatch.setattr(production_schema, "PRODUCTION_MIGRATION_IDS", migration_ids)
    monkeypatch.setattr(
        production_schema, "PRODUCTION_MIGRATION_CHECKSUMS", migration_checksums
    )
    monkeypatch.setattr(production_schema, "AUTHORITY_PRODUCTION_SCHEMA_VERSION", 7)
    report = AuthorityProductionMigrationRunner(
        fixture.database,
        database_id="a2-0016-upgrade-db",
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        identity_binding=initial_database_identity_binding(fixture.backup),
        pre_authority_backup=fixture.backup_path,
    ).run("a2-0017-upgrade-owner")

    assert report.applied_now == (
        "A2_0017_PHASE9_AUDIT_HARDENING",
        "A2_0018_PHASE9_P0_RUNNER_ATTESTATION",
        "A2_0019_PHASE9_REPLAY_EVIDENCE_ATTESTATION",
    )
    connection = sqlite3.connect(fixture.database)
    try:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(authority_production_run_generations)"
            )
        }
        state = connection.execute(
            "SELECT production_schema_version, state "
            "FROM authority_production_schema_state WHERE singleton=1"
        ).fetchone()
    finally:
        connection.close()
    assert {
        "source_inventory_sha256",
        "authorization_id",
        "authorization_target_sha256",
        "predecessor_terminal_receipt_sha256",
    } <= columns
    assert state == (7, "READY")


def test_a2_0017_refuses_to_grandfather_pre_hardening_phase9_rows(
    tmp_path, monkeypatch
):
    migrations = production_schema.PRODUCTION_MIGRATIONS
    migration_ids = production_schema.PRODUCTION_MIGRATION_IDS
    migration_checksums = production_schema.PRODUCTION_MIGRATION_CHECKSUMS
    monkeypatch.setattr(production_schema, "PRODUCTION_MIGRATIONS", migrations[:7])
    monkeypatch.setattr(production_schema, "PRODUCTION_MIGRATION_IDS", migration_ids[:7])
    monkeypatch.setattr(
        production_schema, "PRODUCTION_MIGRATION_CHECKSUMS", migration_checksums[:7]
    )
    monkeypatch.setattr(production_schema, "AUTHORITY_PRODUCTION_SCHEMA_VERSION", 4)
    fixture = install_foundation(tmp_path, name="a2-0016-nonempty")
    connection = sqlite3.connect(fixture.database)
    try:
        connection.execute(
            "INSERT INTO authority_production_run_generation_idempotency "
            "VALUES ('legacy_current', 'old-key', ?, ?, ?)",
            ("1" * 64, "old-run", "2" * 64),
        )
        connection.commit()
    finally:
        connection.close()

    monkeypatch.setattr(production_schema, "PRODUCTION_MIGRATIONS", migrations)
    monkeypatch.setattr(production_schema, "PRODUCTION_MIGRATION_IDS", migration_ids)
    monkeypatch.setattr(
        production_schema, "PRODUCTION_MIGRATION_CHECKSUMS", migration_checksums
    )
    monkeypatch.setattr(production_schema, "AUTHORITY_PRODUCTION_SCHEMA_VERSION", 7)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        AuthorityProductionMigrationRunner(
            fixture.database,
            database_id="a2-0016-nonempty-db",
            expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
            identity_binding=initial_database_identity_binding(fixture.backup),
            pre_authority_backup=fixture.backup_path,
        ).run("a2-0017-refuse-owner")

    connection = sqlite3.connect(fixture.database)
    try:
        state = connection.execute(
            "SELECT production_schema_version, state, last_completed_migration "
            "FROM authority_production_schema_state WHERE singleton=1"
        ).fetchone()
    finally:
        connection.close()
    assert state == (4, "INTERRUPTED", "A2_0016_PHASE9_FORENSIC_REPLAY")


def test_production_migration_interruption_is_durable_and_same_owner_resumes(tmp_path):
    _project, database = create_real_schema_v9(tmp_path)
    preflight = production_preflight(database, database_id="resume-db")
    _backup, backup_path, binding = prepare_initial_backup(
        tmp_path, database, preflight, database_id="resume-db", name="resume"
    )
    migrate_authority_schema_v2(database, owner_token="base-owner")

    def interrupt(migration_id: str) -> None:
        if migration_id == "A2_0011_WRITER_FENCE_AND_SWITCH":
            raise RuntimeError("simulated process interruption")

    runner = AuthorityProductionMigrationRunner(
        database,
        database_id="resume-db",
        expected_source_fence_sha256=preflight.source_fence_sha256,
        identity_binding=binding,
        pre_authority_backup=backup_path,
        after_migration=interrupt,
    )
    with pytest.raises(Exception, match="interrupted after A2_0011"):
        runner.run("resume-owner")
    connection = sqlite3.connect(database)
    try:
        state = connection.execute(
            "SELECT state, lock_owner FROM authority_production_schema_state"
        ).fetchone()
        count = connection.execute(
            "SELECT COUNT(*) FROM authority_production_migrations"
        ).fetchone()[0]
    finally:
        connection.close()
    assert state == ("INTERRUPTED", "resume-owner")
    assert count == 2

    report = AuthorityProductionMigrationRunner(
        database,
        database_id="resume-db",
        expected_source_fence_sha256=preflight.source_fence_sha256,
        identity_binding=binding,
        pre_authority_backup=backup_path,
    ).run("resume-owner")
    assert report.state == "READY"
    assert report.already_applied == PRODUCTION_MIGRATION_IDS[:2]
    assert report.applied_now == PRODUCTION_MIGRATION_IDS[2:]


def test_busy_sqlite_writer_lock_fails_closed(tmp_path, monkeypatch):
    _project, database = create_real_schema_v9(tmp_path)
    preflight = production_preflight(database, database_id="busy-db")
    _backup, backup_path, binding = prepare_initial_backup(
        tmp_path, database, preflight, database_id="busy-db", name="busy"
    )
    migrate_authority_schema_v2(database, owner_token="base-owner")
    original = production_schema.connect_authority_rw
    monkeypatch.setattr(
        production_schema,
        "connect_authority_rw",
        lambda path: original(path, timeout_seconds=0.01),
    )
    blocker = sqlite3.connect(database, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(AuthorityProductionMigrationLocked, match="busy"):
            AuthorityProductionMigrationRunner(
                database,
                database_id="busy-db",
                expected_source_fence_sha256=preflight.source_fence_sha256,
                identity_binding=binding,
                pre_authority_backup=backup_path,
            ).run("busy-owner")
    finally:
        blocker.rollback()
        blocker.close()
    assert production_schema.production_schema_status(database) is None


def test_source_row_drift_and_production_object_drift_fail_closed(tmp_path):
    fixture = install_foundation(tmp_path)
    connection = sqlite3.connect(fixture.database)
    connection.execute("UPDATE project_state SET status='paused' WHERE singleton=1")
    connection.commit()
    connection.row_factory = sqlite3.Row
    with pytest.raises(AuthorityProductionSchemaDrift, match="source fence"):
        verify_production_installation(connection)
    connection.close()

    fixture2 = install_foundation(tmp_path, name="object-drift")
    connection = sqlite3.connect(fixture2.database)
    connection.execute(
        "DROP TRIGGER authority_production_provider_receipts_append_only_delete"
    )
    connection.commit()
    connection.row_factory = sqlite3.Row
    with pytest.raises(AuthorityProductionSchemaDrift, match="sqlite_master"):
        verify_production_installation(connection)
    connection.close()


def test_future_production_schema_fails_closed(tmp_path):
    fixture = install_foundation(tmp_path)
    connection = sqlite3.connect(fixture.database)
    connection.execute(
        "UPDATE authority_production_schema_state "
        "SET production_schema_version=8, state='RUNNING', "
        "lock_owner='future-owner', details_json='{\"future\":true}'"
    )
    connection.commit()
    connection.row_factory = sqlite3.Row
    with pytest.raises(AuthorityProductionFutureSchema):
        verify_production_installation(connection)
    connection.close()
    with pytest.raises(AuthorityProductionFutureSchema):
        AuthorityProductionMigrationRunner(
            fixture.database,
            database_id="authority-project-db",
            expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
            identity_binding=initial_database_identity_binding(fixture.backup),
            pre_authority_backup=fixture.backup_path,
        ).run("future-owner")
    connection = sqlite3.connect(fixture.database)
    try:
        assert connection.execute(
            "SELECT production_schema_version, state, lock_owner, failure_code, details_json "
            "FROM authority_production_schema_state"
        ).fetchone() == (8, "RUNNING", "future-owner", None, '{"future":true}')
    finally:
        connection.close()


def test_wal_backup_uses_sqlite_snapshot_and_publishes_verified_regular_file(tmp_path):
    _project, database = create_real_schema_v9(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute(
        "INSERT INTO events VALUES(2,'TEST_WAL_ROW',101,NULL,0,'{}')"
    )
    connection.commit()
    assert Path(str(database) + "-wal").exists()
    preflight = production_preflight(database, database_id="wal-db")
    backup_path = tmp_path / "wal-backup.db"

    evidence = create_authority_backup(
        database,
        backup_path,
        database_id="wal-db",
        occurred_at=2000,
        expected_source_fence_sha256=preflight.source_fence_sha256,
    )
    connection.close()
    assert not backup_path.is_symlink()
    assert hashlib.sha256(backup_path.read_bytes()).hexdigest() == evidence.backup_sha256
    copied = sqlite3.connect(backup_path)
    try:
        assert copied.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copied.execute("SELECT type FROM events WHERE revision=2").fetchone()[0] == "TEST_WAL_ROW"
    finally:
        copied.close()


def test_backup_failure_before_publish_leaves_no_output(tmp_path):
    _project, database = create_real_schema_v9(tmp_path)
    preflight = production_preflight(database, database_id="failure-db")
    target = tmp_path / "never-published.db"

    def fail(stage: str) -> None:
        if stage == "before_atomic_publish":
            raise RuntimeError("injected backup failure")

    with pytest.raises(RuntimeError, match="injected backup failure"):
        create_authority_backup(
            database,
            target,
            database_id="failure-db",
            occurred_at=2000,
            expected_source_fence_sha256=preflight.source_fence_sha256,
            failure_injector=fail,
        )
    assert not target.exists()
    assert not list(tmp_path.glob(".never-published.db.*.tmp"))


def test_verified_restore_recovers_exact_pre_authority_v1_bytes_and_rows(tmp_path):
    fixture = install_foundation(tmp_path, with_backup=True)
    assert fixture.backup is not None and fixture.backup_path is not None
    before = fixture.backup_path.read_bytes()
    evidence = restore_authority_backup(
        fixture.database,
        fixture.backup_path,
        database_id="authority-project-db",
        occurred_at=3000,
        expected_current_source_fence_sha256=fixture.preflight.source_fence_sha256,
        expected_backup_sha256=fixture.backup.backup_sha256,
        expected_switch_epoch=0,
    )
    assert fixture.database.read_bytes() == before
    assert evidence.restored_sha256 == fixture.backup.backup_sha256
    assert evidence.integrity_check == "ok"
    assert evidence.restored_authority_state == "ABSENT"
    connection = sqlite3.connect(fixture.database)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT schema_version FROM schema_info").fetchone()[0] == 9
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'authority_%'"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_restore_failure_before_atomic_publish_leaves_foundation_unchanged(tmp_path):
    fixture = install_foundation(tmp_path, with_backup=True)
    assert fixture.backup is not None and fixture.backup_path is not None
    before = hashlib.sha256(fixture.database.read_bytes()).hexdigest()

    def fail(stage: str) -> None:
        if stage == "before_restore_publish":
            raise RuntimeError("injected restore failure")

    with pytest.raises(RuntimeError, match="injected restore failure"):
        restore_authority_backup(
            fixture.database,
            fixture.backup_path,
            database_id="authority-project-db",
            occurred_at=3000,
            expected_current_source_fence_sha256=fixture.preflight.source_fence_sha256,
            expected_backup_sha256=fixture.backup.backup_sha256,
            expected_switch_epoch=0,
            failure_injector=fail,
        )
    assert hashlib.sha256(fixture.database.read_bytes()).hexdigest() == before
    connection = sqlite3.connect(fixture.database)
    connection.row_factory = sqlite3.Row
    try:
        assert verify_production_installation(connection)["state"] == "READY"
    finally:
        connection.close()


def test_restore_rejects_wrong_backup_hash_and_symlinks(tmp_path):
    fixture = install_foundation(tmp_path, with_backup=True)
    assert fixture.backup is not None and fixture.backup_path is not None
    with pytest.raises(AuthorityOperationConflict, match="backup hash"):
        restore_authority_backup(
            fixture.database,
            fixture.backup_path,
            database_id="authority-project-db",
            occurred_at=3000,
            expected_current_source_fence_sha256=fixture.preflight.source_fence_sha256,
            expected_backup_sha256="0" * 64,
            expected_switch_epoch=0,
        )
    link = tmp_path / "backup-link.db"
    link.symlink_to(fixture.backup_path)
    with pytest.raises((AuthorityOperationError, AuthorityProductionSourceError)):
        restore_authority_backup(
            fixture.database,
            link,
            database_id="authority-project-db",
            occurred_at=3000,
            expected_current_source_fence_sha256=fixture.preflight.source_fence_sha256,
            expected_backup_sha256=fixture.backup.backup_sha256,
            expected_switch_epoch=0,
        )


def test_migrate_evidence_collision_fails_before_backup_or_database_mutation(tmp_path):
    _project, database = create_real_schema_v9(tmp_path)
    preflight = production_preflight(database, database_id="collision-db")
    backup = tmp_path / "collision.backup.db"
    evidence = tmp_path / "collision.evidence.json"
    evidence.write_text("occupied\n", encoding="utf-8", newline="\n")
    before = database.read_bytes()

    with pytest.raises(AuthorityOperationConflict, match="not reusable"):
        run_authority_migrate_operation(
            database,
            database_id="collision-db",
            expected_source_fence_sha256=preflight.source_fence_sha256,
            backup=backup,
            evidence_output=evidence,
            owner_token="collision-owner",
            occurred_at=1700,
        )

    assert database.read_bytes() == before
    assert not backup.exists()
    assert evidence.read_text(encoding="utf-8") == "occupied\n"


@pytest.mark.parametrize(
    "crash_stage",
    (
        "after_backup_publish",
        "after_backup_recorded",
        "after_base_ready",
        *(f"after_migration:{migration_id}" for migration_id in PRODUCTION_MIGRATION_IDS),
        "after_database_ready",
        "after_internal_receipt",
        "before_external_evidence_publish",
    ),
)
def test_migrate_operation_replays_every_durable_crash_window(tmp_path, crash_stage):
    _project, database = create_real_schema_v9(tmp_path)
    database_id = f"replay-{hashlib.sha256(crash_stage.encode()).hexdigest()[:12]}"
    preflight = production_preflight(database, database_id=database_id)
    backup = tmp_path / "replay.backup.db"
    evidence = tmp_path / "replay.evidence.json"

    def interrupt(stage: str) -> None:
        if stage == crash_stage:
            raise RuntimeError(f"crash at {stage}")

    with pytest.raises(Exception):
        run_authority_migrate_operation(
            database,
            database_id=database_id,
            expected_source_fence_sha256=preflight.source_fence_sha256,
            backup=backup,
            evidence_output=evidence,
            owner_token="replay-owner",
            occurred_at=1800,
            failure_injector=interrupt,
        )
    assert backup.exists() and evidence.exists()
    backup_sha256 = hashlib.sha256(backup.read_bytes()).hexdigest()

    completed = run_authority_migrate_operation(
        database,
        database_id=database_id,
        expected_source_fence_sha256=preflight.source_fence_sha256,
        backup=backup,
        evidence_output=evidence,
        owner_token="replay-owner",
        occurred_at=1800,
    )
    replay = run_authority_migrate_operation(
        database,
        database_id=database_id,
        expected_source_fence_sha256=preflight.source_fence_sha256,
        backup=backup,
        evidence_output=evidence,
        owner_token="replay-owner",
        occurred_at=1800,
    )

    assert replay == completed == json.loads(evidence.read_text(encoding="utf-8"))
    assert completed["schema"] == "authority-production-migrate-operation-v2"
    assert completed["backup"]["production_state"] == "ABSENT"
    assert completed["backup"]["production_last_migration"] is None
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == backup_sha256
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT state FROM authority_production_schema_state"
        ).fetchone()[0] == "READY"
        assert connection.execute(
            "SELECT COUNT(*) FROM authority_production_backup_lineage"
        ).fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.parametrize("migration_id", PRODUCTION_MIGRATION_IDS)
def test_new_migrate_operation_cannot_replace_backup_after_suffix_started(
    tmp_path, migration_id
):
    _project, database = create_real_schema_v9(tmp_path)
    database_id = f"replacement-{migration_id.lower()}"
    preflight = production_preflight(database, database_id=database_id)
    original_backup = tmp_path / "original.pre-authority.db"
    original_evidence = tmp_path / "original.migrate.json"

    def interrupt(stage: str) -> None:
        if stage == f"after_migration:{migration_id}":
            raise RuntimeError(f"crash after {migration_id}")

    with pytest.raises(Exception, match=f"interrupted after {migration_id}"):
        run_authority_migrate_operation(
            database,
            database_id=database_id,
            expected_source_fence_sha256=preflight.source_fence_sha256,
            backup=original_backup,
            evidence_output=original_evidence,
            owner_token="replacement-owner",
            occurred_at=1850,
            failure_injector=interrupt,
        )

    database_bytes = database.read_bytes()
    original_journal_bytes = original_evidence.read_bytes()
    original_backup_sha256 = hashlib.sha256(original_backup.read_bytes()).hexdigest()
    connection = sqlite3.connect(database)
    try:
        business_rows = (
            connection.execute(
                "SELECT solver_mode, solver_threshold_seconds, solver_runtimes_json "
                "FROM project_config WHERE singleton=1"
            ).fetchone(),
            tuple(
                connection.execute(
                    "SELECT revision, type, payload_json FROM events ORDER BY revision"
                ).fetchall()
            ),
        )
    finally:
        connection.close()

    replacement_backup = tmp_path / "replacement.backup.db"
    replacement_evidence = tmp_path / "replacement.migrate.json"
    with pytest.raises(
        AuthorityOperationConflict,
        match=(
            "reuse the original operation owner, backup path, and evidence path"
        ),
    ):
        run_authority_migrate_operation(
            database,
            database_id=database_id,
            expected_source_fence_sha256=preflight.source_fence_sha256,
            backup=replacement_backup,
            evidence_output=replacement_evidence,
            owner_token="replacement-owner",
            occurred_at=1850,
        )

    assert not replacement_backup.exists()
    assert not replacement_evidence.exists()
    assert database.read_bytes() == database_bytes
    assert original_evidence.read_bytes() == original_journal_bytes
    assert hashlib.sha256(original_backup.read_bytes()).hexdigest() == (
        original_backup_sha256
    )
    connection = sqlite3.connect(database)
    try:
        assert (
            connection.execute(
                "SELECT solver_mode, solver_threshold_seconds, solver_runtimes_json "
                "FROM project_config WHERE singleton=1"
            ).fetchone(),
            tuple(
                connection.execute(
                    "SELECT revision, type, payload_json FROM events ORDER BY revision"
                ).fetchall()
            ),
        ) == business_rows
    finally:
        connection.close()

    completed = run_authority_migrate_operation(
        database,
        database_id=database_id,
        expected_source_fence_sha256=preflight.source_fence_sha256,
        backup=original_backup,
        evidence_output=original_evidence,
        owner_token="replacement-owner",
        occurred_at=1850,
    )
    replay = run_authority_migrate_operation(
        database,
        database_id=database_id,
        expected_source_fence_sha256=preflight.source_fence_sha256,
        backup=original_backup,
        evidence_output=original_evidence,
        owner_token="replacement-owner",
        occurred_at=1850,
    )

    assert replay == completed == json.loads(
        original_evidence.read_text(encoding="utf-8")
    )
    assert completed["schema"] == "authority-production-migrate-operation-v2"
    assert hashlib.sha256(original_backup.read_bytes()).hexdigest() == (
        original_backup_sha256
    )
