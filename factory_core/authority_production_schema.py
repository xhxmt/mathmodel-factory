"""Additive production-foundation migrations for Authority Schema V2.

This module is deliberately absent from the active CLI, Scheduler, Service,
Web, and frontend import graphs.  It extends an exact, READY A2_0001..A2_0009
installation with a separately verified suffix.  The published Phase-2 shadow
migration bytes and checksums remain unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
from typing import Callable

from .authority_schema import (
    AUTHORITY_SCHEMA_VERSION,
    MIGRATIONS,
    AuthorityMigrationDrift,
    AuthorityMigrationError,
    AuthorityMigrationInterrupted,
    AuthorityMigrationLocked,
    _applied_migration_rows,
    _verify_applied_migrations,
    _verify_authority_schema_objects,
    legacy_source_identity_sha256,
    verify_authority_schema_installation,
)
from .canonical import canonical_bytes, canonical_sha256
from .domain import SCHEMA_VERSION


AUTHORITY_PRODUCTION_SCHEMA_VERSION = 2
AUTHORITY_PRODUCTION_SOURCE_SCHEMA = "authority-production-source-v1"
PRODUCTION_MIGRATION_RUNNING = "RUNNING"
PRODUCTION_MIGRATION_INTERRUPTED = "INTERRUPTED"
PRODUCTION_MIGRATION_READY = "READY"


class AuthorityProductionSchemaError(RuntimeError):
    """Base error for the production-foundation migration boundary."""


class AuthorityProductionSourceError(AuthorityProductionSchemaError):
    """Raised when the explicit source is not a real supported schema-v9 DB."""


class AuthorityProductionSchemaDrift(AuthorityProductionSchemaError):
    """Raised when migration history, source facts, or SQLite objects drift."""


class AuthorityProductionFutureSchema(AuthorityProductionSchemaError):
    """Raised when the database contains a newer production foundation."""


class AuthorityProductionMigrationLocked(AuthorityProductionSchemaError):
    """Raised when another explicit operator owns migration or SQLite is busy."""


@dataclass(frozen=True)
class ProductionPreflight:
    database_id: str
    source_schema_version: int
    source_schema_identity_sha256: str
    source_fence_sha256: str
    source_database_content_sha256: str
    main_file_sha256: str
    main_file_size: int
    base_authority_state: str
    production_state: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "authority-production-preflight-v1",
            "database_id": self.database_id,
            "source_schema_version": self.source_schema_version,
            "source_schema_identity_sha256": self.source_schema_identity_sha256,
            "source_fence_sha256": self.source_fence_sha256,
            "source_database_content_sha256": self.source_database_content_sha256,
            "main_file_sha256": self.main_file_sha256,
            "main_file_size": self.main_file_size,
            "base_authority_state": self.base_authority_state,
            "production_state": self.production_state,
        }

    @property
    def preflight_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True)
class ProductionMigrationReport:
    database_id: str
    source_fence_sha256: str
    base_authority_prefix_sha256: str
    production_schema_version: int
    state: str
    applied_now: tuple[str, ...]
    already_applied: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "authority-production-migration-report-v1",
            "database_id": self.database_id,
            "source_fence_sha256": self.source_fence_sha256,
            "base_authority_prefix_sha256": self.base_authority_prefix_sha256,
            "production_schema_version": self.production_schema_version,
            "state": self.state,
            "applied_now": list(self.applied_now),
            "already_applied": list(self.already_applied),
        }


@dataclass(frozen=True)
class ProductionDatabaseIdentityBinding:
    database_id: str
    source_schema_identity_sha256: str
    source_fence_sha256: str
    source_database_content_sha256: str
    pre_authority_backup_sha256: str
    pre_authority_backup_size: int
    pre_authority_backup_evidence_sha256: str
    pre_authority_backup_lineage_sha256: str
    pre_authority_base_state: str
    pre_authority_production_state: str
    pre_authority_production_last_migration: str | None
    pre_authority_production_prefix_sha256: str
    bound_at: int

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "authority-production-database-identity-binding-v1",
            "database_id": self.database_id,
            "source_schema_identity_sha256": self.source_schema_identity_sha256,
            "source_fence_sha256": self.source_fence_sha256,
            "source_database_content_sha256": self.source_database_content_sha256,
            "pre_authority_backup_sha256": self.pre_authority_backup_sha256,
            "pre_authority_backup_size": self.pre_authority_backup_size,
            "pre_authority_backup_evidence_sha256": (
                self.pre_authority_backup_evidence_sha256
            ),
            "pre_authority_backup_lineage_sha256": (
                self.pre_authority_backup_lineage_sha256
            ),
            "pre_authority_base_state": self.pre_authority_base_state,
            "pre_authority_production_state": self.pre_authority_production_state,
            "pre_authority_production_last_migration": (
                self.pre_authority_production_last_migration
            ),
            "pre_authority_production_prefix_sha256": (
                self.pre_authority_production_prefix_sha256
            ),
            "bound_at": self.bound_at,
        }

    @property
    def binding_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True)
class _ProductionMigration:
    migration_id: str
    statements: tuple[str, ...]

    @property
    def checksum_sha256(self) -> str:
        return canonical_sha256(
            {
                "migration_id": self.migration_id,
                "statements": self.statements,
            }
        )


_PRODUCTION_BOOTSTRAP_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS authority_production_schema_state (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        production_schema_version INTEGER NOT NULL,
        source_schema_version INTEGER NOT NULL,
        source_schema_identity_sha256 TEXT NOT NULL,
        source_fence_sha256 TEXT NOT NULL,
        base_authority_prefix_sha256 TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('RUNNING', 'INTERRUPTED', 'READY')),
        last_completed_migration TEXT,
        lock_owner TEXT,
        failure_code TEXT,
        details_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS authority_production_migrations (
        migration_id TEXT PRIMARY KEY,
        checksum_sha256 TEXT NOT NULL,
        applied_order INTEGER NOT NULL UNIQUE
    )
    """,
)


def _immutable_statements(
    table: str, unique_keys: tuple[tuple[str, ...], ...]
) -> tuple[str, ...]:
    values = [
        f"""
        CREATE TRIGGER IF NOT EXISTS {table}_append_only_update
        BEFORE UPDATE ON {table}
        BEGIN
            SELECT RAISE(ABORT, '{table} is append-only');
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS {table}_append_only_delete
        BEFORE DELETE ON {table}
        BEGIN
            SELECT RAISE(ABORT, '{table} is append-only');
        END
        """,
    ]
    for index, columns in enumerate(unique_keys, start=1):
        predicate = " AND ".join(f"{column}=NEW.{column}" for column in columns)
        values.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_identity_guard_{index}
            BEFORE INSERT ON {table}
            WHEN EXISTS (SELECT 1 FROM {table} WHERE {predicate})
            BEGIN
                SELECT RAISE(ABORT, '{table} identity conflict');
            END
            """
        )
    return tuple(values)


PRODUCTION_MIGRATIONS = (
    _ProductionMigration(
        "A2_0010_PRODUCTION_MIGRATION_HISTORY",
        (
            *_PRODUCTION_BOOTSTRAP_STATEMENTS,
            *_immutable_statements(
                "authority_production_migrations",
                (("migration_id",), ("applied_order",)),
            ),
        ),
    ),
    _ProductionMigration(
        "A2_0011_WRITER_FENCE_AND_SWITCH",
        (
            """
            CREATE TABLE authority_production_writer_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                writer_id TEXT,
                writer_epoch INTEGER NOT NULL CHECK (writer_epoch >= 0),
                writer_enabled INTEGER NOT NULL CHECK (writer_enabled IN (0, 1)),
                switch_mode TEXT NOT NULL CHECK (
                    switch_mode IN ('V1_ONLY', 'CANARY', 'AUTHORITY_PRIMARY')
                ),
                switch_epoch INTEGER NOT NULL CHECK (switch_epoch >= 0),
                source_fence_sha256 TEXT NOT NULL,
                last_receipt_sha256 TEXT
            )
            """,
            """
            INSERT INTO authority_production_writer_state(
                singleton, writer_id, writer_epoch, writer_enabled,
                switch_mode, switch_epoch, source_fence_sha256,
                last_receipt_sha256
            )
            SELECT 1, NULL, 0, 0, 'V1_ONLY', 0,
                   source_fence_sha256, NULL
            FROM authority_production_schema_state WHERE singleton=1
            """,
            """
            CREATE TABLE authority_production_control_receipts (
                receipt_id TEXT PRIMARY KEY,
                receipt_kind TEXT NOT NULL CHECK (
                    receipt_kind IN (
                        'WRITER_CONFIG', 'CONSUMER_CONFIG', 'SWITCH', 'AUTO_FALLBACK'
                    )
                ),
                prior_switch_epoch INTEGER NOT NULL,
                next_switch_epoch INTEGER NOT NULL,
                prior_writer_epoch INTEGER NOT NULL,
                next_writer_epoch INTEGER NOT NULL,
                operator_subject TEXT NOT NULL,
                reason TEXT NOT NULL,
                occurred_at INTEGER NOT NULL,
                receipt_json TEXT NOT NULL,
                receipt_sha256 TEXT NOT NULL UNIQUE
            )
            """,
            """
            CREATE TABLE authority_production_command_commits (
                command_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                writer_id TEXT NOT NULL,
                writer_epoch INTEGER NOT NULL CHECK (writer_epoch >= 1),
                switch_epoch INTEGER NOT NULL CHECK (switch_epoch >= 1),
                switch_mode TEXT NOT NULL CHECK (
                    switch_mode IN ('CANARY', 'AUTHORITY_PRIMARY')
                ),
                source_fence_sha256 TEXT NOT NULL,
                bundle_sha256 TEXT NOT NULL UNIQUE,
                FOREIGN KEY(command_id) REFERENCES authority_commands(command_id),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id)
            )
            """,
            *_immutable_statements(
                "authority_production_control_receipts",
                (("receipt_id",), ("receipt_sha256",)),
            ),
            *_immutable_statements(
                "authority_production_command_commits",
                (("command_id",), ("bundle_sha256",)),
            ),
        ),
    ),
    _ProductionMigration(
        "A2_0012_TRANSACTIONAL_OUTBOX_DELIVERY",
        (
            """
            CREATE TABLE authority_production_consumer_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                consumer_id TEXT,
                consumer_epoch INTEGER NOT NULL CHECK (consumer_epoch >= 0),
                consumer_enabled INTEGER NOT NULL CHECK (consumer_enabled IN (0, 1))
            )
            """,
            """
            INSERT INTO authority_production_consumer_state(
                singleton, consumer_id, consumer_epoch, consumer_enabled
            ) VALUES (1, NULL, 0, 0)
            """,
            """
            CREATE TABLE authority_production_outbox_delivery_state (
                message_id TEXT PRIMARY KEY,
                delivery_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK (status IN (
                    'PENDING', 'CLAIMED', 'RETRY_WAIT',
                    'RECONCILIATION_REQUIRED', 'DELIVERED', 'DEAD_LETTER'
                )),
                attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
                claim_consumer_id TEXT,
                claim_consumer_epoch INTEGER,
                claim_epoch INTEGER NOT NULL CHECK (claim_epoch >= 0),
                lease_expires_at INTEGER,
                next_attempt_at INTEGER,
                last_error_code TEXT,
                provider_receipt_id TEXT,
                provider_receipt_sha256 TEXT,
                delivered_at INTEGER,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY(message_id) REFERENCES authority_outbox(message_id)
            )
            """,
            """
            INSERT INTO authority_production_outbox_delivery_state(
                message_id, delivery_key, status, attempt_count,
                claim_consumer_id, claim_consumer_epoch, claim_epoch,
                lease_expires_at, next_attempt_at, last_error_code,
                provider_receipt_id, provider_receipt_sha256,
                delivered_at, updated_at
            )
            SELECT message_id,
                   'authority-outbox:' || message_id || ':' || envelope_sha256,
                   'PENDING', 0, NULL, NULL, 0, NULL, 0, NULL,
                   NULL, NULL, NULL, 0
            FROM authority_outbox ORDER BY revision, message_id
            """,
            """
            CREATE TABLE authority_production_outbox_delivery_audit (
                audit_id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                transition_kind TEXT NOT NULL,
                consumer_id TEXT,
                consumer_epoch INTEGER,
                claim_epoch INTEGER NOT NULL,
                occurred_at INTEGER NOT NULL,
                details_json TEXT NOT NULL,
                audit_sha256 TEXT NOT NULL UNIQUE,
                FOREIGN KEY(message_id) REFERENCES authority_outbox(message_id)
            )
            """,
            """
            CREATE TABLE authority_production_provider_receipts (
                provider_receipt_id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL UNIQUE,
                delivery_key TEXT NOT NULL,
                provider_status TEXT NOT NULL,
                receipt_json TEXT NOT NULL,
                receipt_sha256 TEXT NOT NULL UNIQUE,
                recorded_at INTEGER NOT NULL,
                FOREIGN KEY(message_id) REFERENCES authority_outbox(message_id)
            )
            """,
            *_immutable_statements(
                "authority_production_outbox_delivery_audit",
                (("audit_id",), ("audit_sha256",)),
            ),
            *_immutable_statements(
                "authority_production_provider_receipts",
                (("provider_receipt_id",), ("message_id",), ("receipt_sha256",)),
            ),
        ),
    ),
    _ProductionMigration(
        "A2_0013_OPERATIONAL_EVIDENCE",
        (
            """
            CREATE TABLE authority_production_operation_receipts (
                receipt_id TEXT PRIMARY KEY,
                operation_kind TEXT NOT NULL CHECK (
                    operation_kind IN ('BACKUP_RECORDED', 'RESTORE_DRILL_RECORDED')
                ),
                occurred_at INTEGER NOT NULL,
                evidence_json TEXT NOT NULL,
                evidence_sha256 TEXT NOT NULL,
                receipt_json TEXT NOT NULL,
                receipt_sha256 TEXT NOT NULL UNIQUE
            )
            """,
            *_immutable_statements(
                "authority_production_operation_receipts",
                (("receipt_id",), ("receipt_sha256",)),
            ),
        ),
    ),
    _ProductionMigration(
        "A2_0014_DATABASE_IDENTITY_AND_BACKUP_LINEAGE",
        (
            """
            CREATE TABLE authority_production_database_identity (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                database_id TEXT NOT NULL UNIQUE,
                source_schema_identity_sha256 TEXT NOT NULL,
                source_fence_sha256 TEXT NOT NULL,
                source_database_content_sha256 TEXT NOT NULL,
                pre_authority_backup_sha256 TEXT NOT NULL,
                pre_authority_backup_size INTEGER NOT NULL CHECK (
                    pre_authority_backup_size > 0
                ),
                pre_authority_backup_evidence_sha256 TEXT NOT NULL UNIQUE,
                pre_authority_backup_lineage_sha256 TEXT NOT NULL UNIQUE,
                binding_json TEXT NOT NULL,
                binding_sha256 TEXT NOT NULL UNIQUE,
                bound_at INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE authority_production_backup_lineage (
                backup_sha256 TEXT PRIMARY KEY,
                database_id TEXT NOT NULL,
                backup_kind TEXT NOT NULL CHECK (
                    backup_kind IN ('INITIAL_PRE_AUTHORITY', 'RECORDED_CHECKPOINT')
                ),
                backup_size INTEGER NOT NULL CHECK (backup_size > 0),
                source_fence_sha256 TEXT NOT NULL,
                source_database_content_sha256 TEXT NOT NULL,
                evidence_sha256 TEXT NOT NULL UNIQUE,
                lineage_sha256 TEXT NOT NULL UNIQUE,
                recorded_at INTEGER NOT NULL
            )
            """,
            *_immutable_statements(
                "authority_production_database_identity",
                (("singleton",), ("database_id",), ("binding_sha256",)),
            ),
            *_immutable_statements(
                "authority_production_backup_lineage",
                (("backup_sha256",), ("evidence_sha256",), ("lineage_sha256",)),
            ),
            """
            UPDATE authority_production_schema_state
            SET production_schema_version=2
            WHERE singleton=1
            """,
        ),
    ),
)

PRODUCTION_MIGRATION_IDS = tuple(item.migration_id for item in PRODUCTION_MIGRATIONS)
PRODUCTION_MIGRATION_CHECKSUMS = tuple(
    item.checksum_sha256 for item in PRODUCTION_MIGRATIONS
)
BASE_AUTHORITY_PREFIX_SHA256 = canonical_sha256(
    {
        "authority_schema_version": AUTHORITY_SCHEMA_VERSION,
        "migrations": [
            {"migration_id": item.migration_id, "checksum_sha256": item.checksum_sha256}
            for item in MIGRATIONS
        ],
    }
)
EMPTY_PRODUCTION_PREFIX_SHA256 = canonical_sha256(
    {"schema": "authority-production-migration-prefix-v1", "migrations": []}
)


def backup_lineage_sha256(
    *,
    database_id: str,
    backup_kind: str,
    backup_sha256: str,
    backup_size: int,
    source_fence_sha256: str,
    source_database_content_sha256: str,
    evidence_sha256: str,
) -> str:
    return canonical_sha256(
        {
            "schema": "authority-production-backup-lineage-v1",
            "database_id": database_id,
            "backup_kind": backup_kind,
            "backup_sha256": backup_sha256,
            "backup_size": backup_size,
            "source_fence_sha256": source_fence_sha256,
            "source_database_content_sha256": source_database_content_sha256,
            "evidence_sha256": evidence_sha256,
        }
    )


_REAL_SCHEMA_V9_COLUMNS = {
    "schema_info": ("singleton", "schema_version"),
    "project_state": (
        "singleton", "schema_version", "project_id", "project_type", "control_mode",
        "runtime_generation", "scheduler_generation", "stage_catalog_version", "status",
        "last_completed_step", "active_step", "last_completed_stage", "active_stage",
        "active_subtask", "source_step_id", "attempt", "revision", "pending_action_json",
        "runner_pid", "runner_lease_id", "heartbeat_at", "storage_scope", "created_at",
        "updated_at", "last_event_at",
    ),
    "events": ("revision", "type", "created_at", "step", "attempt", "payload_json"),
    "stage_checkpoints": (
        "stage_id", "subtask", "source_step_id", "completed_step_id",
        "input_fingerprint", "output_fingerprint", "completed_revision", "receipt_json",
    ),
}

_REAL_SCHEMA_V9_REQUIRED_TABLES = frozenset(
    {
        "contest_policy", "dirty_causes", "dirty_classifier_rebases",
        "dirty_flag_clear_receipts", "dirty_flags", "events", "project_config",
        "project_state", "projection_failures", "projector_snapshots",
        "prompt_attempt_inputs", "schema_info", "solver_jobs",
        "stage_checkpoint_history", "stage_checkpoints", "stage_cursor_inputs",
        "workflow_decision_instances", "workflow_decision_requests", "workflow_decisions",
    }
)


def _plain_text(value: object, path: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise AuthorityProductionSchemaError(f"{path} must be a non-empty trimmed string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise AuthorityProductionSchemaError(f"{path} must contain valid UTF-8") from exc
    return value


def _sha256_text(value: object, path: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AuthorityProductionSchemaError(f"{path} must be lowercase SHA-256")
    return value


def authority_database_path(database: str | Path) -> Path:
    value = Path(database)
    try:
        metadata = value.lstat()
    except FileNotFoundError as exc:
        raise AuthorityProductionSourceError(f"database is missing: {value}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AuthorityProductionSourceError("database must be a non-symlink regular file")
    return value.resolve()


def connect_authority_rw(path: Path, *, timeout_seconds: float = 2.0) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=timeout_seconds, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(f"PRAGMA busy_timeout={int(timeout_seconds * 1000)}")
    return connection


def connect_authority_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=2000")
    return connection


def _file_identity(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def validate_real_schema_v9(connection: sqlite3.Connection) -> str:
    try:
        rows = connection.execute(
            "SELECT singleton, schema_version FROM schema_info"
        ).fetchall()
    except sqlite3.Error as exc:
        raise AuthorityProductionSourceError("schema_info is unavailable") from exc
    if len(rows) != 1 or rows[0]["singleton"] != 1 or rows[0]["schema_version"] != SCHEMA_VERSION:
        raise AuthorityProductionSourceError(
            f"production foundation requires exact schema-v{SCHEMA_VERSION}"
        )
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        if not str(row[0]).startswith("authority_")
    }
    missing = sorted(_REAL_SCHEMA_V9_REQUIRED_TABLES - tables)
    if missing:
        raise AuthorityProductionSourceError(
            f"database is not a complete SQLiteStateStore schema-v9: missing={missing}"
        )
    for table, expected in _REAL_SCHEMA_V9_COLUMNS.items():
        actual = tuple(
            str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
        )
        if actual != expected:
            raise AuthorityProductionSourceError(
                f"schema-v9 table identity differs: {table}"
            )
    objects = [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": None if row[3] is None else " ".join(str(row[3]).split()),
        }
        for row in connection.execute(
            """
            SELECT type, name, tbl_name, sql FROM sqlite_master
            WHERE type IN ('table', 'index', 'trigger')
              AND name NOT LIKE 'authority_%'
              AND tbl_name NOT LIKE 'authority_%'
            ORDER BY type, name
            """
        )
    ]
    return canonical_sha256(
        {
            "schema": AUTHORITY_PRODUCTION_SOURCE_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "objects": objects,
        }
    )


def _sqlite_identity_value(value: object) -> dict[str, object]:
    if value is None:
        return {"type": "null"}
    if type(value) is int:
        return {"type": "integer", "value": value}
    if type(value) is float:
        return {"type": "real", "value_hex": value.hex()}
    if type(value) is str:
        return {"type": "text", "value": value}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"type": "blob", "value_hex": bytes(value).hex()}
    raise AuthorityProductionSourceError(
        f"unsupported SQLite identity value: {type(value).__qualname__}"
    )


def legacy_database_content_sha256(connection: sqlite3.Connection) -> str:
    """Hash every schema-v9 non-Authority row independently of SQLite row order."""

    schema_identity = validate_real_schema_v9(connection)
    tables: list[dict[str, object]] = []
    names = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'authority_%' "
            "ORDER BY name"
        )
    )
    for table in names:
        columns = tuple(
            str(row[1])
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        )
        encoded_rows = [
            [_sqlite_identity_value(value) for value in row]
            for row in connection.execute(f'SELECT * FROM "{table}"')
        ]
        encoded_rows.sort(key=canonical_bytes)
        tables.append(
            {"table": table, "columns": list(columns), "rows": encoded_rows}
        )
    return canonical_sha256(
        {
            "schema": "authority-production-database-content-v1",
            "source_schema_identity_sha256": schema_identity,
            "tables": tables,
        }
    )


def _production_objects(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            str(row[0]), str(row[1]), str(row[2]),
            None if row[3] is None else " ".join(str(row[3]).split()),
        )
        for row in connection.execute(
            """
            SELECT type, name, tbl_name, sql FROM sqlite_master
            WHERE type IN ('table', 'index', 'trigger')
              AND (name LIKE 'authority_production_%'
                   OR tbl_name LIKE 'authority_production_%')
            ORDER BY type, name
            """
        )
    )


def _expected_production_objects(applied_count: int) -> tuple[tuple[object, ...], ...]:
    if not 0 <= applied_count <= len(PRODUCTION_MIGRATIONS):
        raise AuthorityProductionSchemaDrift("production migration prefix is invalid")
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        # Minimal parents for production-suffix foreign keys and deterministic
        # backfill SELECTs.  They are excluded from the production object set.
        connection.executescript(
            """
            CREATE TABLE authority_schema_state(
                singleton INTEGER PRIMARY KEY,
                source_identity_sha256 TEXT NOT NULL
            );
            CREATE TABLE authority_workflows(workflow_id TEXT PRIMARY KEY);
            CREATE TABLE authority_commands(command_id TEXT PRIMARY KEY);
            CREATE TABLE authority_outbox(
                message_id TEXT PRIMARY KEY,
                workflow_id TEXT,
                revision INTEGER,
                envelope_sha256 TEXT
            );
            """
        )
        for statement in _PRODUCTION_BOOTSTRAP_STATEMENTS:
            connection.execute(statement)
        for migration in PRODUCTION_MIGRATIONS[:applied_count]:
            for statement in migration.statements:
                connection.execute(statement)
        return _production_objects(connection)
    finally:
        connection.close()


def _verify_production_objects(connection: sqlite3.Connection, applied_count: int) -> None:
    actual = _production_objects(connection)
    expected = _expected_production_objects(applied_count)
    if actual != expected:
        actual_keys = {(row[0], row[1]) for row in actual}
        expected_keys = {(row[0], row[1]) for row in expected}
        raise AuthorityProductionSchemaDrift(
            "production sqlite_master identity drifted: "
            f"missing={sorted(expected_keys-actual_keys)}, "
            f"extra={sorted(actual_keys-expected_keys)}"
        )


def _production_rows(connection: sqlite3.Connection) -> tuple[sqlite3.Row, ...]:
    return tuple(
        connection.execute(
            "SELECT * FROM authority_production_migrations ORDER BY applied_order"
        ).fetchall()
    )


def production_prefix_sha256(connection: sqlite3.Connection) -> str:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='authority_production_migrations'"
    ).fetchone()
    rows = () if not exists else _production_rows(connection)
    return canonical_sha256(
        {
            "schema": "authority-production-migration-prefix-v1",
            "migrations": [
                {
                    "migration_id": str(row["migration_id"]),
                    "checksum_sha256": str(row["checksum_sha256"]),
                    "applied_order": int(row["applied_order"]),
                }
                for row in rows
            ],
        }
    )


def _verify_database_identity(connection: sqlite3.Connection) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM authority_production_database_identity WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise AuthorityProductionSchemaDrift("production database identity is missing")
    try:
        body = json.loads(str(row["binding_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AuthorityProductionSchemaDrift(
            "production database identity JSON is malformed"
        ) from exc
    expected = {
        "schema": "authority-production-database-identity-binding-v1",
        "database_id": row["database_id"],
        "source_schema_identity_sha256": row["source_schema_identity_sha256"],
        "source_fence_sha256": row["source_fence_sha256"],
        "source_database_content_sha256": row["source_database_content_sha256"],
        "pre_authority_backup_sha256": row["pre_authority_backup_sha256"],
        "pre_authority_backup_size": row["pre_authority_backup_size"],
        "pre_authority_backup_evidence_sha256": (
            row["pre_authority_backup_evidence_sha256"]
        ),
        "pre_authority_backup_lineage_sha256": (
            row["pre_authority_backup_lineage_sha256"]
        ),
        "pre_authority_base_state": "ABSENT",
        "pre_authority_production_state": "ABSENT",
        "pre_authority_production_last_migration": None,
        "pre_authority_production_prefix_sha256": EMPTY_PRODUCTION_PREFIX_SHA256,
        "bound_at": row["bound_at"],
    }
    if body != expected or canonical_sha256(expected) != row["binding_sha256"]:
        raise AuthorityProductionSchemaDrift("production database identity differs")
    lineage = connection.execute(
        "SELECT * FROM authority_production_backup_lineage WHERE backup_sha256=?",
        (row["pre_authority_backup_sha256"],),
    ).fetchone()
    if lineage is None or (
        lineage["database_id"] != row["database_id"]
        or lineage["backup_kind"] != "INITIAL_PRE_AUTHORITY"
        or lineage["backup_size"] != row["pre_authority_backup_size"]
        or lineage["source_fence_sha256"] != row["source_fence_sha256"]
        or lineage["source_database_content_sha256"]
        != row["source_database_content_sha256"]
        or lineage["evidence_sha256"]
        != row["pre_authority_backup_evidence_sha256"]
        or lineage["lineage_sha256"]
        != row["pre_authority_backup_lineage_sha256"]
        or lineage["recorded_at"] != row["bound_at"]
    ):
        raise AuthorityProductionSchemaDrift("initial backup lineage differs")
    expected_lineage = backup_lineage_sha256(
        database_id=str(lineage["database_id"]),
        backup_kind=str(lineage["backup_kind"]),
        backup_sha256=str(lineage["backup_sha256"]),
        backup_size=int(lineage["backup_size"]),
        source_fence_sha256=str(lineage["source_fence_sha256"]),
        source_database_content_sha256=str(
            lineage["source_database_content_sha256"]
        ),
        evidence_sha256=str(lineage["evidence_sha256"]),
    )
    if expected_lineage != lineage["lineage_sha256"]:
        raise AuthorityProductionSchemaDrift("initial backup lineage hash differs")
    return row


def verified_database_identity(connection: sqlite3.Connection) -> sqlite3.Row:
    return _verify_database_identity(connection)


def _verify_production_history(rows: tuple[sqlite3.Row, ...]) -> None:
    if len(rows) > len(PRODUCTION_MIGRATIONS):
        raise AuthorityProductionSchemaDrift("too many production migrations")
    for order, row in enumerate(rows, start=1):
        expected = PRODUCTION_MIGRATIONS[order - 1]
        if (
            row["migration_id"] != expected.migration_id
            or type(row["applied_order"]) is not int
            or row["applied_order"] != order
            or row["checksum_sha256"] != expected.checksum_sha256
        ):
            raise AuthorityProductionSchemaDrift(
                "production migration history is not the exact published prefix"
            )


def verify_production_installation(
    connection: sqlite3.Connection, *, require_ready: bool = True
) -> sqlite3.Row:
    try:
        validate_real_schema_v9(connection)
        base = verify_authority_schema_installation(connection, require_ready=True)
    except (AuthorityMigrationError, AuthorityMigrationDrift, sqlite3.Error) as exc:
        message = str(exc)
        if "source identity" in message or "source schema" in message:
            message = f"production source fence drifted: {message}"
        raise AuthorityProductionSchemaDrift(message) from exc
    state = connection.execute(
        "SELECT * FROM authority_production_schema_state WHERE singleton=1"
    ).fetchone()
    if state is None:
        raise AuthorityProductionSchemaDrift("production schema state is missing")
    version = state["production_schema_version"]
    if type(version) is not int:
        raise AuthorityProductionSchemaDrift("production schema version is malformed")
    if version > AUTHORITY_PRODUCTION_SCHEMA_VERSION:
        raise AuthorityProductionFutureSchema(
            f"future production authority schema {version} is not supported"
        )
    if version != AUTHORITY_PRODUCTION_SCHEMA_VERSION:
        raise AuthorityProductionSchemaDrift("production schema version differs")
    source_fence = legacy_source_identity_sha256(connection)
    if (
        state["source_schema_version"] != SCHEMA_VERSION
        or state["source_schema_identity_sha256"] != validate_real_schema_v9(connection)
        or state["source_fence_sha256"] != source_fence
        or state["source_fence_sha256"] != base["source_identity_sha256"]
        or state["base_authority_prefix_sha256"] != BASE_AUTHORITY_PREFIX_SHA256
    ):
        raise AuthorityProductionSchemaDrift("production source fence drifted")
    rows = _production_rows(connection)
    _verify_production_history(rows)
    _verify_production_objects(connection, len(rows))
    if len(rows) >= 5:
        identity = _verify_database_identity(connection)
        if (
            identity["source_schema_identity_sha256"]
            != state["source_schema_identity_sha256"]
            or identity["source_fence_sha256"] != state["source_fence_sha256"]
        ):
            raise AuthorityProductionSchemaDrift(
                "production database identity source binding differs"
            )
    if require_ready and (
        state["state"] != PRODUCTION_MIGRATION_READY
        or state["lock_owner"] is not None
        or len(rows) != len(PRODUCTION_MIGRATIONS)
    ):
        raise AuthorityProductionSchemaDrift("production foundation is not READY")
    if len(rows) >= 2:
        writer = connection.execute(
            "SELECT * FROM authority_production_writer_state WHERE singleton=1"
        ).fetchone()
        if writer is None or writer["source_fence_sha256"] != source_fence:
            raise AuthorityProductionSchemaDrift("writer source fence differs")
    return state


def verify_production_control_structure(connection: sqlite3.Connection) -> sqlite3.Row:
    """Verify every structural identity while deliberately not trusting source rows.

    This narrower verifier exists only so a source-fence alert can durably move
    an already configured canary/primary switch back to V1_ONLY.  The normal
    writer, reader, migration, outbox, and forward-switch paths always use the
    stricter :func:`verify_production_installation` source fence.
    """

    validate_real_schema_v9(connection)
    base = connection.execute(
        "SELECT * FROM authority_schema_state WHERE singleton=1"
    ).fetchone()
    if (
        base is None
        or base["authority_schema_version"] != AUTHORITY_SCHEMA_VERSION
        or base["source_schema_version"] != SCHEMA_VERSION
    ):
        raise AuthorityProductionSchemaDrift("base authority control structure differs")
    base_rows = _applied_migration_rows(connection)
    try:
        _verify_applied_migrations(base_rows, SCHEMA_VERSION)
        _verify_authority_schema_objects(connection, len(base_rows))
    except AuthorityMigrationError as exc:
        raise AuthorityProductionSchemaDrift(str(exc)) from exc
    if len(base_rows) != len(MIGRATIONS):
        raise AuthorityProductionSchemaDrift("base authority prefix is incomplete")
    state = connection.execute(
        "SELECT * FROM authority_production_schema_state WHERE singleton=1"
    ).fetchone()
    if (
        state is None
        or state["production_schema_version"] != AUTHORITY_PRODUCTION_SCHEMA_VERSION
        or state["base_authority_prefix_sha256"] != BASE_AUTHORITY_PREFIX_SHA256
        or state["state"] != PRODUCTION_MIGRATION_READY
        or state["lock_owner"] is not None
    ):
        raise AuthorityProductionSchemaDrift("production control structure is not READY")
    rows = _production_rows(connection)
    _verify_production_history(rows)
    _verify_production_objects(connection, len(rows))
    if len(rows) != len(PRODUCTION_MIGRATIONS):
        raise AuthorityProductionSchemaDrift("production migration prefix is incomplete")
    _verify_database_identity(connection)
    return state


def production_preflight(
    database: str | Path,
    *,
    database_id: str,
    expected_source_fence_sha256: str | None = None,
) -> ProductionPreflight:
    path = authority_database_path(database)
    identity = _plain_text(database_id, "database_id")
    connection = connect_authority_ro(path)
    try:
        connection.execute("BEGIN")
        file_sha256, file_size = _file_identity(path)
        schema_identity = validate_real_schema_v9(connection)
        source_fence = legacy_source_identity_sha256(connection)
        source_content = legacy_database_content_sha256(connection)
        if expected_source_fence_sha256 is not None and source_fence != expected_source_fence_sha256:
            raise AuthorityProductionSourceError("expected source fence does not match")
        base_row = connection.execute(
            "SELECT state FROM authority_schema_state WHERE singleton=1"
        ).fetchone() if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='authority_schema_state'"
        ).fetchone() else None
        production_row = connection.execute(
            "SELECT state FROM authority_production_schema_state WHERE singleton=1"
        ).fetchone() if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='authority_production_schema_state'"
        ).fetchone() else None
        identity_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='authority_production_database_identity'"
        ).fetchone()
        if identity_exists:
            persisted_identity = _verify_database_identity(connection)
            if persisted_identity["database_id"] != identity:
                raise AuthorityProductionSourceError(
                    "caller database_id differs from persisted database identity"
                )
        connection.commit()
    finally:
        connection.close()
    return ProductionPreflight(
        identity,
        SCHEMA_VERSION,
        schema_identity,
        source_fence,
        source_content,
        file_sha256,
        file_size,
        "ABSENT" if base_row is None else str(base_row[0]),
        "ABSENT" if production_row is None else str(production_row[0]),
    )


class AuthorityProductionMigrationRunner:
    def __init__(
        self,
        database: str | Path,
        *,
        database_id: str,
        expected_source_fence_sha256: str,
        identity_binding: ProductionDatabaseIdentityBinding,
        pre_authority_backup: str | Path,
        after_migration: Callable[[str], None] | None = None,
    ) -> None:
        self.path = authority_database_path(database)
        self.database_id = _plain_text(database_id, "database_id")
        self.expected_source_fence_sha256 = _plain_text(
            expected_source_fence_sha256, "expected_source_fence_sha256"
        )
        if type(identity_binding) is not ProductionDatabaseIdentityBinding:
            raise AuthorityProductionSchemaError(
                "identity_binding must be ProductionDatabaseIdentityBinding"
            )
        self.identity_binding = identity_binding
        self.pre_authority_backup = authority_database_path(pre_authority_backup)
        self.after_migration = after_migration

    def _verify_initial_backup_binding(
        self,
        connection: sqlite3.Connection,
        *,
        schema_identity: str,
        source_fence: str,
    ) -> None:
        binding = self.identity_binding
        if (
            binding.database_id != self.database_id
            or binding.source_schema_identity_sha256 != schema_identity
            or binding.source_fence_sha256 != source_fence
            or binding.pre_authority_base_state != "ABSENT"
            or binding.pre_authority_production_state != "ABSENT"
            or binding.pre_authority_production_last_migration is not None
            or binding.pre_authority_production_prefix_sha256
            != EMPTY_PRODUCTION_PREFIX_SHA256
            or type(binding.pre_authority_backup_size) is not int
            or binding.pre_authority_backup_size < 1
            or type(binding.bound_at) is not int
            or binding.bound_at < 0
        ):
            raise AuthorityProductionSourceError(
                "initial backup binding is not an exact pre-Authority lineage"
            )
        for path, value in (
            ("source_database_content_sha256", binding.source_database_content_sha256),
            ("pre_authority_backup_sha256", binding.pre_authority_backup_sha256),
            (
                "pre_authority_backup_evidence_sha256",
                binding.pre_authority_backup_evidence_sha256,
            ),
            (
                "pre_authority_backup_lineage_sha256",
                binding.pre_authority_backup_lineage_sha256,
            ),
        ):
            _sha256_text(value, path)
        current_content = legacy_database_content_sha256(connection)
        if current_content != binding.source_database_content_sha256:
            raise AuthorityProductionSourceError(
                "pre-Authority backup belongs to different database content"
            )
        backup_sha256, backup_size = _file_identity(self.pre_authority_backup)
        if (
            backup_sha256 != binding.pre_authority_backup_sha256
            or backup_size != binding.pre_authority_backup_size
        ):
            raise AuthorityProductionSourceError("pre-Authority backup identity differs")
        expected_lineage = backup_lineage_sha256(
            database_id=binding.database_id,
            backup_kind="INITIAL_PRE_AUTHORITY",
            backup_sha256=binding.pre_authority_backup_sha256,
            backup_size=binding.pre_authority_backup_size,
            source_fence_sha256=binding.source_fence_sha256,
            source_database_content_sha256=binding.source_database_content_sha256,
            evidence_sha256=binding.pre_authority_backup_evidence_sha256,
        )
        if expected_lineage != binding.pre_authority_backup_lineage_sha256:
            raise AuthorityProductionSourceError("pre-Authority backup lineage differs")
        backup = connect_authority_ro(self.pre_authority_backup)
        try:
            backup.execute("BEGIN")
            if (
                validate_real_schema_v9(backup) != schema_identity
                or legacy_source_identity_sha256(backup) != source_fence
                or legacy_database_content_sha256(backup) != current_content
                or backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            ):
                raise AuthorityProductionSourceError(
                    "pre-Authority backup content binding differs"
                )
            any_authority = backup.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'authority_%' LIMIT 1"
            ).fetchone()
            if any_authority is not None:
                raise AuthorityProductionSourceError(
                    "initial backup must precede every Authority migration"
                )
            backup.commit()
        finally:
            backup.close()

    def _install_database_identity(self, connection: sqlite3.Connection) -> None:
        binding = self.identity_binding
        body = binding.as_dict()
        connection.execute(
            """
            INSERT INTO authority_production_database_identity(
                singleton, database_id, source_schema_identity_sha256,
                source_fence_sha256, source_database_content_sha256,
                pre_authority_backup_sha256, pre_authority_backup_size,
                pre_authority_backup_evidence_sha256,
                pre_authority_backup_lineage_sha256, binding_json,
                binding_sha256, bound_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                binding.database_id,
                binding.source_schema_identity_sha256,
                binding.source_fence_sha256,
                binding.source_database_content_sha256,
                binding.pre_authority_backup_sha256,
                binding.pre_authority_backup_size,
                binding.pre_authority_backup_evidence_sha256,
                binding.pre_authority_backup_lineage_sha256,
                canonical_bytes(body).decode("utf-8"),
                binding.binding_sha256,
                binding.bound_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO authority_production_backup_lineage(
                backup_sha256, database_id, backup_kind, backup_size,
                source_fence_sha256, source_database_content_sha256,
                evidence_sha256, lineage_sha256, recorded_at
            ) VALUES (?, ?, 'INITIAL_PRE_AUTHORITY', ?, ?, ?, ?, ?, ?)
            """,
            (
                binding.pre_authority_backup_sha256,
                binding.database_id,
                binding.pre_authority_backup_size,
                binding.source_fence_sha256,
                binding.source_database_content_sha256,
                binding.pre_authority_backup_evidence_sha256,
                binding.pre_authority_backup_lineage_sha256,
                binding.bound_at,
            ),
        )

    @staticmethod
    def _begin(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise AuthorityProductionMigrationLocked(
                    "production migration SQLite writer lock is busy"
                ) from exc
            raise

    def _verify_source(self, connection: sqlite3.Connection) -> tuple[str, str]:
        schema_identity = validate_real_schema_v9(connection)
        try:
            verify_authority_schema_installation(connection, require_ready=True)
        except AuthorityMigrationError as exc:
            raise AuthorityProductionSchemaDrift(str(exc)) from exc
        source_fence = legacy_source_identity_sha256(connection)
        if source_fence != self.expected_source_fence_sha256:
            raise AuthorityProductionSourceError("source fence changed before migration")
        return schema_identity, source_fence

    def _mark_interrupted(self, owner: str, code: str) -> None:
        connection = connect_authority_rw(self.path)
        try:
            self._begin(connection)
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='authority_production_schema_state'"
            ).fetchone()
            if exists:
                row = connection.execute(
                    "SELECT production_schema_version, lock_owner "
                    "FROM authority_production_schema_state WHERE singleton=1"
                ).fetchone()
                if (
                    row is not None
                    and row["production_schema_version"] in {1, 2}
                    and row["lock_owner"] == owner
                ):
                    connection.execute(
                        "UPDATE authority_production_schema_state "
                        "SET state=?, failure_code=?, details_json=? WHERE singleton=1",
                        (
                            PRODUCTION_MIGRATION_INTERRUPTED,
                            code,
                            json.dumps({"failure_code": code}, sort_keys=True, separators=(",", ":")),
                        ),
                    )
            connection.commit()
        except sqlite3.Error:
            connection.rollback()
        finally:
            connection.close()

    def run(self, owner_token: str) -> ProductionMigrationReport:
        owner = _plain_text(owner_token, "owner_token")
        connection = connect_authority_rw(self.path)
        try:
            self._begin(connection)
            schema_identity, source_fence = self._verify_source(connection)
            for statement in _PRODUCTION_BOOTSTRAP_STATEMENTS:
                connection.execute(statement)
            state = connection.execute(
                "SELECT * FROM authority_production_schema_state WHERE singleton=1"
            ).fetchone()
            if state is None:
                connection.execute(
                    """
                    INSERT INTO authority_production_schema_state(
                        singleton, production_schema_version, source_schema_version,
                        source_schema_identity_sha256, source_fence_sha256,
                        base_authority_prefix_sha256, state, last_completed_migration,
                        lock_owner, failure_code, details_json
                    ) VALUES (1, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, '{}')
                    """,
                    (
                        AUTHORITY_PRODUCTION_SCHEMA_VERSION,
                        SCHEMA_VERSION,
                        schema_identity,
                        source_fence,
                        BASE_AUTHORITY_PREFIX_SHA256,
                        PRODUCTION_MIGRATION_RUNNING,
                        owner,
                    ),
                )
                rows: tuple[sqlite3.Row, ...] = ()
            else:
                if state["production_schema_version"] > AUTHORITY_PRODUCTION_SCHEMA_VERSION:
                    raise AuthorityProductionFutureSchema("future production schema")
                if (
                    state["production_schema_version"] not in {1, 2}
                    or state["source_schema_version"] != SCHEMA_VERSION
                    or state["source_schema_identity_sha256"] != schema_identity
                    or state["source_fence_sha256"] != source_fence
                    or state["base_authority_prefix_sha256"] != BASE_AUTHORITY_PREFIX_SHA256
                ):
                    raise AuthorityProductionSchemaDrift("production migration source identity differs")
                if state["lock_owner"] not in {None, owner}:
                    raise AuthorityProductionMigrationLocked(
                        f"production migration is owned by {state['lock_owner']}"
                    )
                rows = _production_rows(connection)
                _verify_production_history(rows)
                _verify_production_objects(connection, len(rows))
                if state["state"] == PRODUCTION_MIGRATION_READY:
                    if len(rows) == len(PRODUCTION_MIGRATIONS):
                        if state["production_schema_version"] != 2:
                            raise AuthorityProductionSchemaDrift(
                                "READY foundation schema version differs"
                            )
                        _verify_database_identity(connection)
                        connection.commit()
                        return ProductionMigrationReport(
                            self.database_id, source_fence, BASE_AUTHORITY_PREFIX_SHA256,
                            AUTHORITY_PRODUCTION_SCHEMA_VERSION, PRODUCTION_MIGRATION_READY,
                            (), PRODUCTION_MIGRATION_IDS,
                        )
                    if len(rows) != len(PRODUCTION_MIGRATIONS) - 1:
                        raise AuthorityProductionSchemaDrift(
                            "READY foundation lacks an upgradeable migration prefix"
                        )
                connection.execute(
                    "UPDATE authority_production_schema_state "
                    "SET state=?, lock_owner=?, failure_code=NULL WHERE singleton=1",
                    (PRODUCTION_MIGRATION_RUNNING, owner),
                )
            if len(rows) < len(PRODUCTION_MIGRATIONS):
                self._verify_initial_backup_binding(
                    connection,
                    schema_identity=schema_identity,
                    source_fence=source_fence,
                )
            _verify_production_objects(connection, len(rows))
            connection.commit()
        except Exception:
            connection.rollback()
            connection.close()
            self._mark_interrupted(owner, "PRODUCTION_MIGRATION_ACQUIRE_FAILED")
            raise
        else:
            connection.close()

        applied_now: list[str] = []
        already = [row["migration_id"] for row in rows]
        try:
            for order, migration in enumerate(PRODUCTION_MIGRATIONS, start=1):
                if order <= len(rows):
                    continue
                connection = connect_authority_rw(self.path)
                try:
                    self._begin(connection)
                    schema_identity, source_fence = self._verify_source(connection)
                    state = connection.execute(
                        "SELECT * FROM authority_production_schema_state WHERE singleton=1"
                    ).fetchone()
                    if state is None or state["lock_owner"] != owner:
                        raise AuthorityProductionMigrationLocked("migration owner fence changed")
                    current = _production_rows(connection)
                    _verify_production_history(current)
                    _verify_production_objects(connection, len(current))
                    if len(current) != order - 1:
                        raise AuthorityProductionSchemaDrift("migration prefix changed")
                    if len(current) < len(PRODUCTION_MIGRATIONS):
                        self._verify_initial_backup_binding(
                            connection,
                            schema_identity=schema_identity,
                            source_fence=source_fence,
                        )
                    for statement in migration.statements:
                        connection.execute(statement)
                    if migration.migration_id == (
                        "A2_0014_DATABASE_IDENTITY_AND_BACKUP_LINEAGE"
                    ):
                        self._install_database_identity(connection)
                    connection.execute(
                        "INSERT INTO authority_production_migrations VALUES (?, ?, ?)",
                        (migration.migration_id, migration.checksum_sha256, order),
                    )
                    connection.execute(
                        "UPDATE authority_production_schema_state "
                        "SET last_completed_migration=?, details_json=? "
                        "WHERE singleton=1 AND lock_owner=?",
                        (
                            migration.migration_id,
                            json.dumps(
                                {"last_completed_migration": migration.migration_id},
                                sort_keys=True, separators=(",", ":"),
                            ),
                            owner,
                        ),
                    )
                    current = _production_rows(connection)
                    _verify_production_history(current)
                    _verify_production_objects(connection, len(current))
                    if legacy_source_identity_sha256(connection) != self.expected_source_fence_sha256:
                        raise AuthorityProductionSourceError("source fence changed during migration")
                    if validate_real_schema_v9(connection) != schema_identity:
                        raise AuthorityProductionSourceError("schema-v9 identity changed during migration")
                    connection.commit()
                    applied_now.append(migration.migration_id)
                    rows = current
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.close()
                if self.after_migration is not None:
                    try:
                        self.after_migration(migration.migration_id)
                    except Exception as exc:
                        self._mark_interrupted(owner, "MIGRATION_OBSERVER_INTERRUPTED")
                        raise AuthorityMigrationInterrupted(
                            f"production migration interrupted after {migration.migration_id}"
                        ) from exc

            connection = connect_authority_rw(self.path)
            try:
                self._begin(connection)
                self._verify_source(connection)
                state = connection.execute(
                    "SELECT * FROM authority_production_schema_state WHERE singleton=1"
                ).fetchone()
                if state is None or state["lock_owner"] != owner:
                    raise AuthorityProductionMigrationLocked("migration owner fence changed")
                current = _production_rows(connection)
                _verify_production_history(current)
                _verify_production_objects(connection, len(current))
                if len(current) != len(PRODUCTION_MIGRATIONS):
                    raise AuthorityProductionSchemaDrift("foundation migration is incomplete")
                updated = connection.execute(
                    """
                    UPDATE authority_production_schema_state
                    SET state=?, lock_owner=NULL, failure_code=NULL, details_json=?
                    WHERE singleton=1 AND lock_owner=?
                    """,
                    (
                        PRODUCTION_MIGRATION_READY,
                        json.dumps(
                            {"state": PRODUCTION_MIGRATION_READY},
                            sort_keys=True, separators=(",", ":"),
                        ),
                        owner,
                    ),
                )
                if updated.rowcount != 1:
                    raise AuthorityProductionMigrationLocked("migration owner fence changed")
                connection.commit()
            finally:
                connection.close()
            connection = connect_authority_ro(self.path)
            try:
                connection.execute("BEGIN")
                verify_production_installation(connection, require_ready=True)
                connection.commit()
            finally:
                connection.close()
            return ProductionMigrationReport(
                self.database_id,
                self.expected_source_fence_sha256,
                BASE_AUTHORITY_PREFIX_SHA256,
                AUTHORITY_PRODUCTION_SCHEMA_VERSION,
                PRODUCTION_MIGRATION_READY,
                tuple(applied_now),
                tuple(str(value) for value in already),
            )
        except AuthorityMigrationInterrupted:
            raise
        except Exception as exc:
            self._mark_interrupted(owner, type(exc).__name__)
            raise


def migrate_authority_production_foundation(
    database: str | Path,
    *,
    database_id: str,
    expected_source_fence_sha256: str,
    owner_token: str,
    identity_binding: ProductionDatabaseIdentityBinding,
    pre_authority_backup: str | Path,
) -> ProductionMigrationReport:
    return AuthorityProductionMigrationRunner(
        database,
        database_id=database_id,
        expected_source_fence_sha256=expected_source_fence_sha256,
        identity_binding=identity_binding,
        pre_authority_backup=pre_authority_backup,
    ).run(owner_token)


def production_schema_status(database: str | Path) -> dict[str, object] | None:
    path = authority_database_path(database)
    connection = connect_authority_ro(path)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='authority_production_schema_state'"
        ).fetchone()
        if not exists:
            return None
        row = connection.execute(
            "SELECT * FROM authority_production_schema_state WHERE singleton=1"
        ).fetchone()
        return None if row is None else dict(row)
    finally:
        connection.close()
