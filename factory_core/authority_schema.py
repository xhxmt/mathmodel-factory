"""Additive, resumable authority-schema v2 migrations.

This module deliberately leaves the legacy ``schema_info`` version and every
legacy table untouched.  It is not imported by the active scheduler.  A
caller must explicitly run the migration against a project database, and the
separate write repository remains default-off after installation.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import inspect
import json
from pathlib import Path
import sqlite3
import stat
from typing import Callable


AUTHORITY_SCHEMA_VERSION = 2
LEGACY_SOURCE_IDENTITY_SCHEMA = "authority-legacy-source-identity-v1"
LEGACY_SCHEMA_MIN_VERSION = 1
LEGACY_SCHEMA_MAX_VERSION = 9
LEGACY_UNKNOWN = "legacy_unknown"
LEGACY_CURRENT_IMPORTED = "LEGACY_CURRENT_IMPORTED"
LEGACY_IMPORTED = "LEGACY_IMPORTED"
UNKNOWN_ASSURANCE = "UNKNOWN"
MIGRATION_BLOCKED_OWNER_AMBIGUOUS = "MIGRATION_BLOCKED_OWNER_AMBIGUOUS"

MIGRATION_RUNNING = "RUNNING"
MIGRATION_INTERRUPTED = "INTERRUPTED"
MIGRATION_READY = "READY"


class AuthorityMigrationError(RuntimeError):
    """Base error for the additive authority migration boundary."""


class AuthorityFutureSchemaError(AuthorityMigrationError):
    """Raised before writes when a legacy or authority schema is too new."""


class AuthorityMigrationLocked(AuthorityMigrationError):
    """Raised when another explicit migration owner holds the durable lock."""


class AuthorityMigrationDrift(AuthorityMigrationError):
    """Raised when an applied migration no longer has its recorded identity."""


class AuthorityMigrationInterrupted(AuthorityMigrationError):
    """Raised after a committed migration step when an observer interrupts."""


@dataclass(frozen=True)
class AuthorityMigrationReport:
    source_schema_version: int
    authority_schema_version: int
    state: str
    applied_now: tuple[str, ...]
    already_applied: tuple[str, ...]
    blocker_code: str | None


@dataclass(frozen=True)
class _Migration:
    migration_id: str
    statements: tuple[str, ...]
    hook_name: str | None = None

    @property
    def checksum_sha256(self) -> str:
        hook_identity = None
        if self.hook_name is not None:
            try:
                hook = _HOOKS[self.hook_name]
                hook_identity = hashlib.sha256(
                    inspect.getsource(hook).encode("utf-8", errors="strict")
                ).hexdigest()
            except (KeyError, OSError, TypeError) as exc:
                raise AuthorityMigrationDrift(
                    f"authority migration hook identity is unavailable: {self.hook_name}"
                ) from exc
        value = json.dumps(
            {
                "migration_id": self.migration_id,
                "statements": self.statements,
                "hook_name": self.hook_name,
                "hook_implementation_sha256": hook_identity,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(value).hexdigest()


_BOOTSTRAP_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS authority_schema_state (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        authority_schema_version INTEGER NOT NULL,
        source_schema_version INTEGER NOT NULL CHECK (
            source_schema_version BETWEEN 1 AND 9
        ),
        source_identity_schema TEXT NOT NULL,
        source_identity_sha256 TEXT NOT NULL CHECK (
            length(source_identity_sha256) = 64
            AND source_identity_sha256 = lower(source_identity_sha256)
        ),
        state TEXT NOT NULL CHECK (
            state IN ('RUNNING', 'INTERRUPTED', 'READY',
                      'MIGRATION_BLOCKED_OWNER_AMBIGUOUS')
        ),
        last_completed_migration TEXT,
        lock_owner TEXT,
        blocker_code TEXT,
        details_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS authority_schema_migrations (
        migration_id TEXT PRIMARY KEY,
        checksum_sha256 TEXT NOT NULL,
        source_schema_version INTEGER NOT NULL CHECK (
            source_schema_version BETWEEN 1 AND 9
        ),
        applied_order INTEGER NOT NULL UNIQUE
    )
    """,
)


_APPEND_ONLY_TABLES = (
    "authority_schema_migrations",
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
)


_APPEND_ONLY_UNIQUE_KEYS = {
    "authority_schema_migrations": (("migration_id",), ("applied_order",)),
    "authority_contract_pin_sets": (("pin_set_sha256",),),
    "authority_commands": (("command_id",), ("envelope_sha256",)),
    "authority_events": (
        ("event_id",),
        ("envelope_sha256",),
        ("workflow_id", "revision"),
    ),
    "authority_receipts": (
        ("receipt_id",),
        ("event_id",),
        ("envelope_sha256",),
    ),
    "authority_idempotency_records": (
        ("scope_kind", "scope_id", "idempotency_key"),
    ),
    "authority_artifact_records": (("artifact_record_id",),),
    "authority_checkpoint_ledger": (("checkpoint_id",), ("source_record_key",)),
    "authority_reopen_plans": (("reopen_plan_id",),),
    "authority_outbox": (("message_id",), ("event_id",), ("envelope_sha256",)),
    "authority_invocations": (("invocation_id",),),
    "authority_attempts": (("attempt_id",), ("invocation_id", "attempt_number")),
    "authority_process_scopes": (("process_scope_id",),),
    "authority_project_snapshots": (("snapshot_id",), ("snapshot_sha256",)),
}


def _append_only_triggers() -> tuple[str, ...]:
    statements: list[str] = []
    for table in _APPEND_ONLY_TABLES:
        statements.extend(
            (
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
            )
        )
        for guard_number, columns in enumerate(_APPEND_ONLY_UNIQUE_KEYS[table], start=1):
            predicate = " AND ".join(f"{column} = NEW.{column}" for column in columns)
            statements.append(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table}_append_only_insert_guard_{guard_number}
                BEFORE INSERT ON {table}
                WHEN EXISTS (SELECT 1 FROM {table} WHERE {predicate})
                BEGIN
                    SELECT RAISE(ABORT, '{table} append-only identity conflict');
                END
                """
            )
    return tuple(statements)


MIGRATIONS = (
    _Migration("A2_0001_BOOTSTRAP", _BOOTSTRAP_STATEMENTS),
    _Migration(
        "A2_0002_WORKFLOW_REVISION",
        (
            """
            CREATE TABLE IF NOT EXISTS authority_workflows (
                workflow_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                project_generation TEXT NOT NULL,
                run_generation TEXT NOT NULL,
                runtime_generation TEXT NOT NULL,
                scheduler_generation TEXT NOT NULL,
                current_revision INTEGER CHECK (current_revision IS NULL OR current_revision >= 0),
                current_revision_availability TEXT NOT NULL CHECK (
                    current_revision_availability IN ('RECORDED', 'legacy_unknown')
                ),
                contract_pin_set_sha256 TEXT,
                contract_pin_availability TEXT NOT NULL CHECK (
                    contract_pin_availability IN ('RECORDED', 'legacy_unknown')
                ),
                authority_state TEXT NOT NULL CHECK (
                    authority_state IN ('LEGACY_IMPORTED_SHADOW', 'RECORDED_SHADOW')
                )
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS authority_revision_allocator (
                workflow_id TEXT PRIMARY KEY,
                next_revision INTEGER NOT NULL CHECK (next_revision >= 1),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id)
            )
            """,
        ),
        "_backfill_workflow",
    ),
    _Migration(
        "A2_0003_CONTRACT_PINS",
        (
            """
            CREATE TABLE IF NOT EXISTS authority_contract_pin_sets (
                pin_set_sha256 TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                pin_set_json TEXT NOT NULL,
                provenance TEXT NOT NULL,
                first_recorded_revision INTEGER NOT NULL CHECK (first_recorded_revision >= 1)
            )
            """,
        ),
    ),
    _Migration(
        "A2_0004_COMMAND_EVENT_RECEIPT_IDEMPOTENCY",
        (
            """
            CREATE TABLE IF NOT EXISTS authority_commands (
                command_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                requested_revision INTEGER NOT NULL CHECK (requested_revision >= 0),
                persisted_revision INTEGER NOT NULL CHECK (persisted_revision >= 1),
                command_type TEXT NOT NULL,
                envelope_schema TEXT NOT NULL,
                envelope_json TEXT NOT NULL,
                envelope_sha256 TEXT NOT NULL UNIQUE,
                contract_pin_set_sha256 TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(contract_pin_set_sha256)
                    REFERENCES authority_contract_pin_sets(pin_set_sha256)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS authority_events (
                event_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                command_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                envelope_schema TEXT NOT NULL,
                envelope_json TEXT NOT NULL,
                envelope_sha256 TEXT NOT NULL UNIQUE,
                UNIQUE(workflow_id, revision),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(command_id) REFERENCES authority_commands(command_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS authority_receipts (
                receipt_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                command_id TEXT NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                outcome TEXT NOT NULL,
                envelope_schema TEXT NOT NULL,
                envelope_json TEXT NOT NULL,
                envelope_sha256 TEXT NOT NULL UNIQUE,
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(command_id) REFERENCES authority_commands(command_id),
                FOREIGN KEY(event_id) REFERENCES authority_events(event_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS authority_idempotency_records (
                scope_kind TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_schema TEXT NOT NULL,
                request_sha256 TEXT NOT NULL,
                command_id TEXT NOT NULL,
                receipt_id TEXT NOT NULL,
                committed_revision INTEGER NOT NULL CHECK (committed_revision >= 1),
                PRIMARY KEY(scope_kind, scope_id, idempotency_key),
                FOREIGN KEY(command_id) REFERENCES authority_commands(command_id),
                FOREIGN KEY(receipt_id) REFERENCES authority_receipts(receipt_id)
            )
            """,
        ),
    ),
    _Migration(
        "A2_0005_ARTIFACT_CHECKPOINT_LEDGER",
        (
            """
            CREATE TABLE IF NOT EXISTS authority_artifact_records (
                artifact_record_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                artifact_type TEXT NOT NULL,
                artifact_path TEXT NOT NULL,
                content_sha256 TEXT,
                availability TEXT NOT NULL CHECK (
                    availability IN ('RECORDED', 'legacy_unknown', 'REDACTED', 'ERROR')
                ),
                owner_scope TEXT NOT NULL,
                recorded_revision INTEGER,
                metadata_json TEXT NOT NULL,
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS authority_checkpoint_ledger (
                checkpoint_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                checkpoint_kind TEXT NOT NULL CHECK (
                    checkpoint_kind IN ('RECORDED', 'LEGACY_CURRENT_IMPORTED')
                ),
                checkpoint_key TEXT NOT NULL,
                assurance TEXT NOT NULL CHECK (
                    assurance IN ('RECORDED', 'VERIFIED', 'LEGACY_IMPORTED', 'UNKNOWN')
                ),
                owner_stage INTEGER,
                owner_resolution TEXT NOT NULL CHECK (
                    owner_resolution IN (
                        'RECORDED_OWNER', 'EXPLICIT_LEGACY_OWNER',
                        'MIGRATION_BLOCKED_OWNER_AMBIGUOUS'
                    )
                ),
                source_record_key TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                recorded_revision INTEGER,
                CHECK (
                    checkpoint_kind != 'LEGACY_CURRENT_IMPORTED'
                    OR assurance IN ('LEGACY_IMPORTED', 'UNKNOWN')
                ),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id)
            )
            """,
        ),
    ),
    _Migration(
        "A2_0006_REOPEN_PLAN_OUTBOX",
        (
            """
            CREATE TABLE IF NOT EXISTS authority_reopen_plans (
                reopen_plan_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                source_revision INTEGER NOT NULL CHECK (source_revision >= 0),
                target_scope TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                recorded_revision INTEGER NOT NULL CHECK (recorded_revision >= 1),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS authority_outbox (
                message_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                event_id TEXT NOT NULL UNIQUE,
                topic TEXT NOT NULL,
                envelope_schema TEXT NOT NULL,
                envelope_json TEXT NOT NULL,
                envelope_sha256 TEXT NOT NULL UNIQUE,
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(event_id) REFERENCES authority_events(event_id)
            )
            """,
        ),
    ),
    _Migration(
        "A2_0007_EXECUTION_SCOPES",
        (
            """
            CREATE TABLE IF NOT EXISTS authority_invocations (
                invocation_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                command_id TEXT NOT NULL,
                invocation_kind TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                recorded_revision INTEGER NOT NULL CHECK (recorded_revision >= 1),
                scope_schema TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(command_id) REFERENCES authority_commands(command_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS authority_attempts (
                attempt_id TEXT PRIMARY KEY,
                invocation_id TEXT NOT NULL,
                attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
                recorded_revision INTEGER NOT NULL CHECK (recorded_revision >= 1),
                scope_schema TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                UNIQUE(invocation_id, attempt_number),
                FOREIGN KEY(invocation_id) REFERENCES authority_invocations(invocation_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS authority_process_scopes (
                process_scope_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL,
                process_kind TEXT NOT NULL,
                process_identity TEXT NOT NULL,
                recorded_revision INTEGER NOT NULL CHECK (recorded_revision >= 1),
                scope_schema TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                FOREIGN KEY(attempt_id) REFERENCES authority_attempts(attempt_id)
            )
            """,
        ),
    ),
    _Migration(
        "A2_0008_PROJECT_SNAPSHOT_APPEND_ONLY",
        (
            """
            CREATE TABLE IF NOT EXISTS authority_project_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                project_revision INTEGER NOT NULL CHECK (project_revision >= 0),
                completeness TEXT NOT NULL,
                contract_pin_set_sha256 TEXT,
                snapshot_schema TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                snapshot_sha256 TEXT NOT NULL UNIQUE,
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(contract_pin_set_sha256)
                    REFERENCES authority_contract_pin_sets(pin_set_sha256)
            )
            """,
            *_append_only_triggers(),
        ),
    ),
    _Migration("A2_0009_LEGACY_CHECKPOINT_BACKFILL", (), "_backfill_checkpoints"),
)

AUTHORITY_MIGRATION_IDS = tuple(item.migration_id for item in MIGRATIONS)


def _plain_text(value: object, path: str) -> str:
    if type(value) is not str or not value:
        raise AuthorityMigrationError(f"{path} must be a non-empty plain string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise AuthorityMigrationError(f"{path} must contain valid UTF-8") from exc
    return value


def _database_path(path: str | Path) -> Path:
    value = Path(path)
    try:
        metadata = value.lstat()
    except FileNotFoundError as exc:
        raise AuthorityMigrationError(f"authority migration database is missing: {value}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AuthorityMigrationError("authority migration requires a non-symlink regular database")
    return value.resolve()


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=0.25, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 250")
    return connection


def _legacy_schema_version(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute(
            "SELECT schema_version FROM schema_info WHERE singleton=1"
        ).fetchone()
    except sqlite3.Error as exc:
        raise AuthorityMigrationError("legacy schema_info is unavailable") from exc
    if row is None or type(row[0]) is not int:
        raise AuthorityMigrationError("legacy schema version is missing or malformed")
    version = int(row[0])
    if version > LEGACY_SCHEMA_MAX_VERSION:
        raise AuthorityFutureSchemaError(
            f"future legacy workflow schema {version} is not supported"
        )
    if version < LEGACY_SCHEMA_MIN_VERSION:
        raise AuthorityMigrationError(f"legacy workflow schema {version} is not supported")
    return version


_LEGACY_SOURCE_TABLES = ("schema_info", "project_state", "stage_checkpoints")


def _source_value(value: object) -> dict[str, object]:
    if value is None:
        return {"type": "null"}
    if type(value) is int:
        return {"type": "integer", "value": value}
    if type(value) is float:
        return {"type": "real", "value": value.hex()}
    if type(value) is str:
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise AuthorityMigrationDrift("legacy source contains non-UTF-8 text") from exc
        return {"type": "text", "value": value}
    if type(value) is bytes:
        return {"type": "blob", "value": value.hex()}
    raise AuthorityMigrationDrift(
        f"unsupported SQLite value in legacy source: {type(value).__name__}"
    )


def legacy_source_identity_sha256(connection: sqlite3.Connection) -> str:
    """Fingerprint exactly the immutable legacy inputs consumed by v2 backfill."""

    objects = [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": row[3],
        }
        for row in connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name IN ('schema_info', 'project_state', 'stage_checkpoints')
               OR tbl_name IN ('schema_info', 'project_state', 'stage_checkpoints')
            ORDER BY type, name
            """
        )
    ]
    tables: list[dict[str, object]] = []
    for table in _LEGACY_SOURCE_TABLES:
        if not _table_exists(connection, table):
            tables.append({"name": table, "availability": LEGACY_UNKNOWN})
            continue
        columns = _table_columns(connection, table)
        quoted = table.replace('"', '""')
        try:
            rows = connection.execute(
                f'SELECT rowid, * FROM "{quoted}" ORDER BY rowid'
            ).fetchall()
        except sqlite3.Error as exc:
            raise AuthorityMigrationDrift(
                f"legacy source table {table} lacks stable rowid ordering"
            ) from exc
        tables.append(
            {
                "name": table,
                "availability": "RECORDED",
                "columns": columns,
                "rows": [
                    [_source_value(value) for value in tuple(row)]
                    for row in rows
                ],
            }
        )
    payload = _canonical_json(
        {
            "schema": LEGACY_SOURCE_IDENTITY_SCHEMA,
            "objects": objects,
            "tables": tables,
        }
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _table_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})"))


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _legacy_text(value: object) -> str:
    if type(value) is not str or not value:
        return LEGACY_UNKNOWN
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return LEGACY_UNKNOWN
    return value


def _legacy_integer(value: object, *, minimum: int = 0) -> int | None:
    return value if type(value) is int and value >= minimum else None


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _backfill_workflow(connection: sqlite3.Connection) -> None:
    project_id = LEGACY_UNKNOWN
    runtime_generation = LEGACY_UNKNOWN
    scheduler_generation = LEGACY_UNKNOWN
    revision: int | None = None
    if _table_exists(connection, "project_state"):
        columns = set(_table_columns(connection, "project_state"))
        row = connection.execute("SELECT * FROM project_state WHERE singleton=1").fetchone()
        if row is not None:
            if "project_id" in columns:
                project_id = _legacy_text(row["project_id"])
            if "runtime_generation" in columns:
                runtime_generation = _legacy_text(row["runtime_generation"])
            if "scheduler_generation" in columns:
                scheduler_generation = _legacy_text(row["scheduler_generation"])
            if "revision" in columns:
                revision = _legacy_integer(row["revision"])
    connection.execute(
        """
        INSERT OR IGNORE INTO authority_workflows(
            workflow_id, project_id, project_generation, run_generation,
            runtime_generation, scheduler_generation, current_revision,
            current_revision_availability, contract_pin_set_sha256,
            contract_pin_availability, authority_state
        ) VALUES ('legacy_current', ?, 'legacy_unknown', 'legacy_unknown', ?, ?, ?, ?,
                  NULL, 'legacy_unknown', 'LEGACY_IMPORTED_SHADOW')
        """,
        (
            project_id,
            runtime_generation,
            scheduler_generation,
            revision,
            "RECORDED" if revision is not None else LEGACY_UNKNOWN,
        ),
    )
    if revision is not None:
        connection.execute(
            "INSERT OR IGNORE INTO authority_revision_allocator(workflow_id, next_revision) "
            "VALUES ('legacy_current', ?)",
            (revision + 1,),
        )


def _legacy_scalar(value: object) -> str | int | bool:
    if type(value) in {str, int, bool}:
        if type(value) is str:
            return _legacy_text(value)
        return value
    return LEGACY_UNKNOWN


def _checkpoint_id(source_record_key: str, payload_json: str) -> str:
    value = _canonical_json(
        {
            "schema": "authority-legacy-checkpoint-id-v1",
            "source_record_key": source_record_key,
            "payload_sha256": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
        }
    ).encode("utf-8")
    return "legacy-checkpoint-" + hashlib.sha256(value).hexdigest()


def _insert_checkpoint(
    connection: sqlite3.Connection,
    *,
    checkpoint_key: str,
    assurance: str,
    owner_stage: int | None,
    source_record_key: str,
    payload: dict[str, object],
    recorded_revision: int | None,
) -> None:
    owner_resolution = (
        "EXPLICIT_LEGACY_OWNER"
        if owner_stage is not None
        else MIGRATION_BLOCKED_OWNER_AMBIGUOUS
    )
    payload_json = _canonical_json(payload)
    connection.execute(
        """
        INSERT OR IGNORE INTO authority_checkpoint_ledger(
            checkpoint_id, workflow_id, checkpoint_kind, checkpoint_key,
            assurance, owner_stage, owner_resolution, source_record_key,
            payload_json, recorded_revision
        ) VALUES (?, 'legacy_current', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _checkpoint_id(source_record_key, payload_json),
            LEGACY_CURRENT_IMPORTED,
            checkpoint_key,
            assurance,
            owner_stage,
            owner_resolution,
            source_record_key,
            payload_json,
            recorded_revision,
        ),
    )


def _backfill_checkpoints(connection: sqlite3.Connection) -> None:
    inserted = False
    if _table_exists(connection, "stage_checkpoints"):
        columns = set(_table_columns(connection, "stage_checkpoints"))
        rows = connection.execute("SELECT rowid AS _legacy_rowid, * FROM stage_checkpoints").fetchall()
        for row in rows:
            stage = _legacy_integer(row["stage_id"], minimum=1) if "stage_id" in columns else None
            subtask = _legacy_text(row["subtask"]) if "subtask" in columns else LEGACY_UNKNOWN
            source_record_key = (
                f"stage_checkpoints:{row['_legacy_rowid']}:"
                f"{stage if stage is not None else LEGACY_UNKNOWN}:{subtask}"
            )
            receipt_valid = False
            if "receipt_json" in columns and type(row["receipt_json"]) is str:
                try:
                    receipt_valid = type(json.loads(row["receipt_json"])) is dict
                except (TypeError, json.JSONDecodeError):
                    receipt_valid = False
            payload = {
                "completed_step_id": _legacy_scalar(row["completed_step_id"])
                if "completed_step_id" in columns
                else LEGACY_UNKNOWN,
                "input_fingerprint": _legacy_scalar(row["input_fingerprint"])
                if "input_fingerprint" in columns
                else LEGACY_UNKNOWN,
                "legacy_receipt_availability": "RECORDED" if receipt_valid else LEGACY_UNKNOWN,
                "output_fingerprint": _legacy_scalar(row["output_fingerprint"])
                if "output_fingerprint" in columns
                else LEGACY_UNKNOWN,
                "source_step_id": _legacy_scalar(row["source_step_id"])
                if "source_step_id" in columns
                else LEGACY_UNKNOWN,
                "stage_id": stage if stage is not None else LEGACY_UNKNOWN,
                "subtask": subtask,
            }
            recorded = (
                _legacy_integer(row["completed_revision"])
                if "completed_revision" in columns
                else None
            )
            _insert_checkpoint(
                connection,
                checkpoint_key=f"stage:{stage if stage is not None else LEGACY_UNKNOWN}:{subtask}",
                assurance=LEGACY_IMPORTED if receipt_valid else UNKNOWN_ASSURANCE,
                owner_stage=stage,
                source_record_key=source_record_key,
                payload=payload,
                recorded_revision=recorded,
            )
            inserted = True
    if inserted:
        return

    columns = set(_table_columns(connection, "project_state")) if _table_exists(connection, "project_state") else set()
    row = (
        connection.execute("SELECT * FROM project_state WHERE singleton=1").fetchone()
        if columns
        else None
    )
    recorded_revision = None
    payload: dict[str, object] = {
        "active_stage": LEGACY_UNKNOWN,
        "active_step": LEGACY_UNKNOWN,
        "last_completed_stage": LEGACY_UNKNOWN,
        "last_completed_step": LEGACY_UNKNOWN,
        "source": "project_state_current",
    }
    if row is not None:
        if "revision" in columns:
            recorded_revision = _legacy_integer(row["revision"])
        for field in ("active_stage", "active_step", "last_completed_stage", "last_completed_step"):
            if field in columns:
                payload[field] = _legacy_scalar(row[field])
    _insert_checkpoint(
        connection,
        checkpoint_key="legacy_current",
        assurance=UNKNOWN_ASSURANCE,
        owner_stage=None,
        source_record_key="project_state:singleton:legacy_current",
        payload=payload,
        recorded_revision=recorded_revision,
    )


_HOOKS = {
    "_backfill_workflow": _backfill_workflow,
    "_backfill_checkpoints": _backfill_checkpoints,
}


def _authority_schema_objects(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            str(row[0]),
            str(row[1]),
            str(row[2]),
            None if row[3] is None else " ".join(str(row[3]).split()),
        )
        for row in connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE type IN ('table', 'index', 'trigger')
              AND (name LIKE 'authority_%' OR tbl_name LIKE 'authority_%')
              AND name NOT LIKE 'authority_production_%'
              AND tbl_name NOT LIKE 'authority_production_%'
            ORDER BY type, name
            """
        )
    )


def _expected_authority_schema_objects(applied_count: int) -> tuple[tuple[object, ...], ...]:
    if not 0 <= applied_count <= len(MIGRATIONS):
        raise AuthorityMigrationDrift("authority migration prefix length is invalid")
    expected = sqlite3.connect(":memory:")
    try:
        expected.execute("PRAGMA foreign_keys = ON")
        for statement in _BOOTSTRAP_STATEMENTS:
            expected.execute(statement)
        for migration in MIGRATIONS[:applied_count]:
            for statement in migration.statements:
                expected.execute(statement)
        return _authority_schema_objects(expected)
    finally:
        expected.close()


def _verify_authority_schema_objects(
    connection: sqlite3.Connection, applied_count: int
) -> None:
    expected = _expected_authority_schema_objects(applied_count)
    actual = _authority_schema_objects(connection)
    if actual != expected:
        expected_map = {(row[0], row[1]): row for row in expected}
        actual_map = {(row[0], row[1]): row for row in actual}
        missing = sorted(set(expected_map) - set(actual_map))
        extra = sorted(set(actual_map) - set(expected_map))
        changed = sorted(
            key
            for key in set(expected_map) & set(actual_map)
            if expected_map[key] != actual_map[key]
        )
        raise AuthorityMigrationDrift(
            "authority sqlite_master identity drifted: "
            f"missing={missing}, extra={extra}, changed={changed}"
        )


def _verify_source_fence(
    connection: sqlite3.Connection, state: sqlite3.Row
) -> tuple[int, str]:
    required = {
        "source_schema_version",
        "source_identity_schema",
        "source_identity_sha256",
    }
    if not required <= set(state.keys()):
        raise AuthorityMigrationDrift("authority source identity columns are unavailable")
    source_version = _legacy_schema_version(connection)
    source_identity = legacy_source_identity_sha256(connection)
    if state["source_identity_schema"] != LEGACY_SOURCE_IDENTITY_SCHEMA:
        raise AuthorityMigrationDrift("recorded legacy source identity schema drifted")
    if type(state["source_schema_version"]) is not int:
        raise AuthorityMigrationDrift("recorded legacy source schema version is malformed")
    if int(state["source_schema_version"]) != source_version:
        raise AuthorityMigrationDrift("recorded legacy source schema version drifted")
    if state["source_identity_sha256"] != source_identity:
        raise AuthorityMigrationDrift("recorded legacy source identity drifted")
    return source_version, source_identity


def _applied_migration_rows(connection: sqlite3.Connection) -> tuple[sqlite3.Row, ...]:
    return tuple(
        connection.execute(
            "SELECT * FROM authority_schema_migrations ORDER BY applied_order"
        ).fetchall()
    )


def _verify_applied_migrations(
    rows: tuple[sqlite3.Row, ...], source_version: int
) -> None:
    if len(rows) > len(MIGRATIONS):
        raise AuthorityMigrationDrift("too many applied authority migrations")
    for expected_order, row in enumerate(rows, start=1):
        expected = MIGRATIONS[expected_order - 1]
        if row["migration_id"] != expected.migration_id:
            raise AuthorityMigrationDrift(
                "authority migration history is not an exact applied prefix"
            )
        if type(row["applied_order"]) is not int or row["applied_order"] != expected_order:
            raise AuthorityMigrationDrift("authority migration applied_order drifted")
        if (
            type(row["source_schema_version"]) is not int
            or row["source_schema_version"] != source_version
        ):
            raise AuthorityMigrationDrift(
                f"authority migration source version drifted: {expected.migration_id}"
            )
        if row["checksum_sha256"] != expected.checksum_sha256:
            raise AuthorityMigrationDrift(
                f"authority migration checksum drifted: {expected.migration_id}"
            )


def verify_authority_schema_installation(
    connection: sqlite3.Connection, *, require_ready: bool
) -> sqlite3.Row:
    """Fail closed on source, history, or SQLite object drift."""

    try:
        state = connection.execute(
            "SELECT * FROM authority_schema_state WHERE singleton=1"
        ).fetchone()
    except sqlite3.Error as exc:
        raise AuthorityMigrationDrift("authority schema state is unavailable") from exc
    if state is None:
        raise AuthorityMigrationDrift("authority schema state is missing")
    if type(state["authority_schema_version"]) is not int:
        raise AuthorityMigrationDrift("authority schema version is malformed")
    authority_version = int(state["authority_schema_version"])
    if authority_version > AUTHORITY_SCHEMA_VERSION:
        raise AuthorityFutureSchemaError(
            f"future authority schema {authority_version} is not supported"
        )
    if authority_version != AUTHORITY_SCHEMA_VERSION:
        raise AuthorityMigrationDrift(
            f"authority schema {authority_version} cannot be interpreted as v2"
        )
    source_version, _identity = _verify_source_fence(connection, state)
    rows = _applied_migration_rows(connection)
    _verify_applied_migrations(rows, source_version)
    _verify_authority_schema_objects(connection, len(rows))
    if require_ready:
        if state["state"] != MIGRATION_READY:
            raise AuthorityMigrationDrift(
                f"authority schema is not write-ready: {state['state']}"
            )
        if len(rows) != len(MIGRATIONS):
            raise AuthorityMigrationDrift("final authority state lacks required migrations")
        if state["lock_owner"] is not None:
            raise AuthorityMigrationDrift("READY authority schema retains a migration owner")
    return state


class AuthorityMigrationRunner:
    """Run migrations under one operator-controlled runner and owner token.

    Reusing the same owner token is the crash-resume mechanism.  It is not a
    lease and must not be used by two intentionally active runners.
    """

    def __init__(
        self,
        database: str | Path,
        *,
        after_migration: Callable[[str], None] | None = None,
    ) -> None:
        self.path = _database_path(database)
        self.after_migration = after_migration

    def _begin(self, connection: sqlite3.Connection) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise AuthorityMigrationLocked("SQLite migration writer lock is busy") from exc
            raise

    def acquire(self, owner_token: str) -> str:
        owner = _plain_text(owner_token, "migration owner token")
        connection = _connect(self.path)
        try:
            self._begin(connection)
            source_version = _legacy_schema_version(connection)
            source_identity = legacy_source_identity_sha256(connection)
            for statement in _BOOTSTRAP_STATEMENTS:
                connection.execute(statement)
            row = connection.execute(
                "SELECT * FROM authority_schema_state WHERE singleton=1"
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO authority_schema_state(
                        singleton, authority_schema_version, source_schema_version,
                        source_identity_schema, source_identity_sha256, state,
                        last_completed_migration, lock_owner, blocker_code, details_json
                    ) VALUES (1, ?, ?, ?, ?, ?, NULL, ?, NULL, '{}')
                    """,
                    (
                        AUTHORITY_SCHEMA_VERSION,
                        source_version,
                        LEGACY_SOURCE_IDENTITY_SCHEMA,
                        source_identity,
                        MIGRATION_RUNNING,
                        owner,
                    ),
                )
                _verify_authority_schema_objects(connection, 0)
                connection.commit()
                return MIGRATION_RUNNING
            if "authority_schema_version" not in set(row.keys()):
                raise AuthorityMigrationDrift("authority schema version is unavailable")
            if type(row["authority_schema_version"]) is not int:
                raise AuthorityMigrationDrift("authority schema version is malformed")
            recorded_authority = int(row["authority_schema_version"])
            if recorded_authority > AUTHORITY_SCHEMA_VERSION:
                raise AuthorityFutureSchemaError(
                    f"future authority schema {recorded_authority} is not supported"
                )
            if recorded_authority != AUTHORITY_SCHEMA_VERSION:
                raise AuthorityMigrationDrift(
                    f"authority schema {recorded_authority} cannot be interpreted as v2"
                )
            _verify_source_fence(connection, row)
            applied = _applied_migration_rows(connection)
            _verify_applied_migrations(applied, source_version)
            _verify_authority_schema_objects(connection, len(applied))
            lock_owner = row["lock_owner"]
            if lock_owner is not None and lock_owner != owner:
                raise AuthorityMigrationLocked(f"authority migration is owned by {lock_owner}")
            if (
                row["state"] in {MIGRATION_READY, MIGRATION_BLOCKED_OWNER_AMBIGUOUS}
                and lock_owner is None
            ):
                if len(applied) != len(MIGRATIONS):
                    raise AuthorityMigrationDrift(
                        "final authority state lacks required migrations"
                    )
                connection.commit()
                return str(row["state"])
            connection.execute(
                "UPDATE authority_schema_state SET state=?, lock_owner=?, blocker_code=NULL "
                "WHERE singleton=1",
                (MIGRATION_RUNNING, owner),
            )
            connection.commit()
            return MIGRATION_RUNNING
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _mark_interrupted(self, owner: str, code: str) -> None:
        connection = _connect(self.path)
        try:
            self._begin(connection)
            if not _table_exists(connection, "authority_schema_state"):
                connection.commit()
                return
            try:
                row = connection.execute(
                    "SELECT lock_owner FROM authority_schema_state WHERE singleton=1"
                ).fetchone()
            except sqlite3.Error:
                connection.rollback()
                return
            if row is not None and row["lock_owner"] == owner:
                connection.execute(
                    "UPDATE authority_schema_state SET state=?, blocker_code=?, details_json=? "
                    "WHERE singleton=1",
                    (
                        MIGRATION_INTERRUPTED,
                        code,
                        _canonical_json({"error_code": code}),
                    ),
                )
            connection.commit()
        finally:
            connection.close()

    def run(self, owner_token: str) -> AuthorityMigrationReport:
        owner = _plain_text(owner_token, "migration owner token")
        try:
            initial_state = self.acquire(owner)
        except (AuthorityMigrationDrift, AuthorityFutureSchemaError) as exc:
            self._mark_interrupted(owner, type(exc).__name__)
            raise
        applied_now: list[str] = []
        already_applied: list[str] = []
        if initial_state in {MIGRATION_READY, MIGRATION_BLOCKED_OWNER_AMBIGUOUS}:
            connection = _connect(self.path)
            try:
                state = verify_authority_schema_installation(
                    connection, require_ready=initial_state == MIGRATION_READY
                )
                source_version = int(state["source_schema_version"])
                applied = _applied_migration_rows(connection)
                if len(applied) != len(AUTHORITY_MIGRATION_IDS):
                    raise AuthorityMigrationDrift("final authority state lacks required migrations")
                return AuthorityMigrationReport(
                    source_version,
                    AUTHORITY_SCHEMA_VERSION,
                    initial_state,
                    (),
                    AUTHORITY_MIGRATION_IDS,
                    state["blocker_code"],
                )
            finally:
                connection.close()

        try:
            for order, migration in enumerate(MIGRATIONS, start=1):
                connection = _connect(self.path)
                try:
                    self._begin(connection)
                    state = connection.execute(
                        "SELECT * FROM authority_schema_state WHERE singleton=1"
                    ).fetchone()
                    if state is None or state["lock_owner"] != owner:
                        raise AuthorityMigrationLocked("authority migration lock ownership changed")
                    source_version, _source_identity = _verify_source_fence(
                        connection, state
                    )
                    applied = _applied_migration_rows(connection)
                    _verify_applied_migrations(applied, source_version)
                    _verify_authority_schema_objects(connection, len(applied))
                    if order <= len(applied):
                        already_applied.append(migration.migration_id)
                        connection.commit()
                        continue
                    if order != len(applied) + 1:
                        raise AuthorityMigrationDrift(
                            "authority migration history is not an exact applied prefix"
                        )
                    for statement in migration.statements:
                        connection.execute(statement)
                    if migration.hook_name is not None:
                        _HOOKS[migration.hook_name](connection)
                    connection.execute(
                        """
                        INSERT INTO authority_schema_migrations(
                            migration_id, checksum_sha256, source_schema_version,
                            applied_order
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            migration.migration_id,
                            migration.checksum_sha256,
                            source_version,
                            order,
                        ),
                    )
                    updated = connection.execute(
                        "UPDATE authority_schema_state SET last_completed_migration=?, "
                        "details_json=? WHERE singleton=1 AND lock_owner=?",
                        (
                            migration.migration_id,
                            _canonical_json({"last_completed_migration": migration.migration_id}),
                            owner,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise AuthorityMigrationLocked(
                            "authority migration lock ownership changed"
                        )
                    current_state = connection.execute(
                        "SELECT * FROM authority_schema_state WHERE singleton=1"
                    ).fetchone()
                    if current_state is None:
                        raise AuthorityMigrationDrift("authority schema state disappeared")
                    _verify_source_fence(connection, current_state)
                    current_applied = _applied_migration_rows(connection)
                    _verify_applied_migrations(current_applied, source_version)
                    _verify_authority_schema_objects(connection, len(current_applied))
                    connection.commit()
                    applied_now.append(migration.migration_id)
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
                            f"authority migration interrupted after {migration.migration_id}"
                        ) from exc

            connection = _connect(self.path)
            try:
                self._begin(connection)
                state = connection.execute(
                    "SELECT * FROM authority_schema_state WHERE singleton=1"
                ).fetchone()
                if state is None or state["lock_owner"] != owner:
                    raise AuthorityMigrationLocked("authority migration lock ownership changed")
                source_version, _source_identity = _verify_source_fence(connection, state)
                applied = _applied_migration_rows(connection)
                _verify_applied_migrations(applied, source_version)
                if len(applied) != len(MIGRATIONS):
                    raise AuthorityMigrationDrift("final authority state lacks required migrations")
                _verify_authority_schema_objects(connection, len(applied))
                ambiguous = connection.execute(
                    "SELECT 1 FROM authority_checkpoint_ledger "
                    "WHERE owner_resolution=? LIMIT 1",
                    (MIGRATION_BLOCKED_OWNER_AMBIGUOUS,),
                ).fetchone() is not None
                final_state = (
                    MIGRATION_BLOCKED_OWNER_AMBIGUOUS if ambiguous else MIGRATION_READY
                )
                blocker = MIGRATION_BLOCKED_OWNER_AMBIGUOUS if ambiguous else None
                updated = connection.execute(
                    """
                    UPDATE authority_schema_state
                    SET state=?, lock_owner=NULL, blocker_code=?, details_json=?
                    WHERE singleton=1 AND lock_owner=?
                    """,
                    (
                        final_state,
                        blocker,
                        _canonical_json(
                            {
                                "authority_schema_version": AUTHORITY_SCHEMA_VERSION,
                                "blocker_code": blocker if blocker is not None else LEGACY_UNKNOWN,
                            }
                        ),
                        owner,
                    ),
                )
                if updated.rowcount != 1:
                    raise AuthorityMigrationLocked("authority migration lock ownership changed")
                final_row = connection.execute(
                    "SELECT * FROM authority_schema_state WHERE singleton=1"
                ).fetchone()
                if final_row is None:
                    raise AuthorityMigrationDrift("authority schema state disappeared")
                _verify_source_fence(connection, final_row)
                _verify_applied_migrations(
                    _applied_migration_rows(connection), source_version
                )
                _verify_authority_schema_objects(connection, len(MIGRATIONS))
                connection.commit()
            finally:
                connection.close()
            return AuthorityMigrationReport(
                source_version,
                AUTHORITY_SCHEMA_VERSION,
                final_state,
                tuple(applied_now),
                tuple(already_applied),
                blocker,
            )
        except AuthorityMigrationInterrupted:
            raise
        except Exception as exc:
            self._mark_interrupted(owner, type(exc).__name__)
            raise


def migrate_authority_schema_v2(
    database: str | Path,
    *,
    owner_token: str,
) -> AuthorityMigrationReport:
    """Install or resume the additive authority schema for a legacy v1-v9 DB."""

    return AuthorityMigrationRunner(database).run(owner_token)


def authority_schema_status(database: str | Path) -> dict[str, object] | None:
    """Read the additive schema state without creating or upgrading anything."""

    path = _database_path(database)
    connection = _connect(path)
    try:
        if not _table_exists(connection, "authority_schema_state"):
            return None
        row = connection.execute(
            "SELECT * FROM authority_schema_state WHERE singleton=1"
        ).fetchone()
        return None if row is None else dict(row)
    finally:
        connection.close()
