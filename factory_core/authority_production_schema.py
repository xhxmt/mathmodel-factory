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


AUTHORITY_PRODUCTION_SCHEMA_VERSION = 8
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
    _ProductionMigration(
        "A2_0015_PHASE9_RUN_GENERATION",
        (
            """
            CREATE TABLE authority_production_run_generations (
                run_generation TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                project_revision INTEGER NOT NULL CHECK (project_revision >= 0),
                project_generation TEXT NOT NULL,
                runtime_generation TEXT NOT NULL,
                scheduler_generation TEXT NOT NULL,
                predecessor_run_generation TEXT,
                predecessor_creation_receipt_sha256 TEXT,
                operation_kind TEXT NOT NULL CHECK (
                    operation_kind IN ('CREATE', 'ROTATE')
                ),
                run_mode TEXT NOT NULL,
                modeling_consultation_contract TEXT NOT NULL,
                delivery_capability TEXT NOT NULL CHECK (
                    delivery_capability = 'DISABLED'
                ),
                source_commit TEXT NOT NULL,
                source_tree TEXT NOT NULL,
                source_parent TEXT NOT NULL,
                contract_pin_set_sha256 TEXT NOT NULL,
                official_input_manifest_sha256 TEXT NOT NULL,
                official_input_raw_bytes_set_sha256 TEXT NOT NULL,
                execution_context_receipt_sha256 TEXT NOT NULL,
                operator_authorization_receipt_sha256 TEXT NOT NULL,
                request_sha256 TEXT NOT NULL UNIQUE,
                created_at INTEGER NOT NULL CHECK (created_at >= 0),
                UNIQUE(workflow_id, run_generation),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(predecessor_run_generation)
                    REFERENCES authority_production_run_generations(run_generation),
                FOREIGN KEY(predecessor_creation_receipt_sha256)
                    REFERENCES authority_production_run_generation_creation_receipts(
                        receipt_sha256
                    ) DEFERRABLE INITIALLY DEFERRED,
                FOREIGN KEY(contract_pin_set_sha256)
                    REFERENCES authority_contract_pin_sets(pin_set_sha256)
            )
            """,
            """
            CREATE TABLE authority_production_run_generation_creation_receipts (
                receipt_id TEXT PRIMARY KEY,
                run_generation TEXT NOT NULL UNIQUE,
                workflow_id TEXT NOT NULL,
                operation_kind TEXT NOT NULL CHECK (
                    operation_kind IN ('CREATE', 'ROTATE')
                ),
                request_sha256 TEXT NOT NULL UNIQUE,
                occurred_at INTEGER NOT NULL CHECK (occurred_at >= 0),
                receipt_json TEXT NOT NULL,
                receipt_sha256 TEXT NOT NULL UNIQUE,
                FOREIGN KEY(run_generation)
                    REFERENCES authority_production_run_generations(run_generation),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id)
            )
            """,
            """
            CREATE TABLE authority_production_run_generation_idempotency (
                workflow_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_sha256 TEXT NOT NULL,
                run_generation TEXT NOT NULL,
                creation_receipt_sha256 TEXT NOT NULL,
                PRIMARY KEY(workflow_id, idempotency_key),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(run_generation)
                    REFERENCES authority_production_run_generations(run_generation),
                FOREIGN KEY(creation_receipt_sha256)
                    REFERENCES authority_production_run_generation_creation_receipts(
                        receipt_sha256
                    )
            )
            """,
            """
            CREATE TABLE authority_production_run_generation_successions (
                run_generation TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                predecessor_run_generation TEXT,
                predecessor_creation_receipt_sha256 TEXT,
                succession_json TEXT NOT NULL,
                succession_sha256 TEXT NOT NULL UNIQUE,
                FOREIGN KEY(run_generation)
                    REFERENCES authority_production_run_generations(run_generation),
                FOREIGN KEY(predecessor_run_generation)
                    REFERENCES authority_production_run_generations(run_generation),
                FOREIGN KEY(predecessor_creation_receipt_sha256)
                    REFERENCES authority_production_run_generation_creation_receipts(
                        receipt_sha256
                    ),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id)
            )
            """,
            """
            CREATE TABLE authority_production_run_generation_current (
                workflow_id TEXT PRIMARY KEY,
                run_generation TEXT NOT NULL UNIQUE,
                creation_receipt_sha256 TEXT NOT NULL,
                updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(run_generation)
                    REFERENCES authority_production_run_generations(run_generation),
                FOREIGN KEY(creation_receipt_sha256)
                    REFERENCES authority_production_run_generation_creation_receipts(
                        receipt_sha256
                    )
            )
            """,
            *_immutable_statements(
                "authority_production_run_generations",
                (("run_generation",), ("request_sha256",)),
            ),
            *_immutable_statements(
                "authority_production_run_generation_creation_receipts",
                (("receipt_id",), ("run_generation",), ("receipt_sha256",)),
            ),
            *_immutable_statements(
                "authority_production_run_generation_idempotency",
                (("workflow_id", "idempotency_key"),),
            ),
            *_immutable_statements(
                "authority_production_run_generation_successions",
                (("run_generation",), ("succession_sha256",)),
            ),
            """
            CREATE TRIGGER authority_production_run_generation_current_insert_guard
            BEFORE INSERT ON authority_production_run_generation_current
            WHEN NOT EXISTS (
                SELECT 1
                FROM authority_production_run_generations g
                JOIN authority_production_run_generation_creation_receipts r
                  ON r.run_generation=g.run_generation
                 AND r.receipt_sha256=NEW.creation_receipt_sha256
                JOIN authority_production_run_generation_successions s
                  ON s.run_generation=g.run_generation
                WHERE g.run_generation=NEW.run_generation
                  AND g.workflow_id=NEW.workflow_id
                  AND g.operation_kind='CREATE'
                  AND g.predecessor_run_generation IS NULL
                  AND g.predecessor_creation_receipt_sha256 IS NULL
                  AND s.predecessor_run_generation IS NULL
                  AND s.predecessor_creation_receipt_sha256 IS NULL
            )
            BEGIN
                SELECT RAISE(ABORT, 'run-generation current insert lacks creation graph');
            END
            """,
            """
            CREATE TRIGGER authority_production_run_generation_current_update_guard
            BEFORE UPDATE ON authority_production_run_generation_current
            WHEN NEW.workflow_id != OLD.workflow_id
              OR NEW.updated_at < OLD.updated_at
              OR NOT EXISTS (
                SELECT 1
                FROM authority_production_run_generations g
                JOIN authority_production_run_generation_creation_receipts r
                  ON r.run_generation=g.run_generation
                 AND r.receipt_sha256=NEW.creation_receipt_sha256
                JOIN authority_production_run_generation_successions s
                  ON s.run_generation=g.run_generation
                 AND s.predecessor_run_generation=OLD.run_generation
                 AND s.predecessor_creation_receipt_sha256=
                     OLD.creation_receipt_sha256
                WHERE g.run_generation=NEW.run_generation
                  AND g.workflow_id=OLD.workflow_id
                  AND g.operation_kind='ROTATE'
                  AND g.predecessor_run_generation=OLD.run_generation
                  AND g.predecessor_creation_receipt_sha256=
                      OLD.creation_receipt_sha256
            )
            BEGIN
                SELECT RAISE(ABORT, 'run-generation current update lacks succession graph');
            END
            """,
            """
            CREATE TRIGGER authority_production_run_generation_current_delete_guard
            BEFORE DELETE ON authority_production_run_generation_current
            BEGIN
                SELECT RAISE(ABORT, 'run-generation current pointer cannot be deleted');
            END
            """,
            """
            CREATE TRIGGER authority_production_workflows_run_generation_update_guard
            BEFORE UPDATE OF project_generation, run_generation,
                             runtime_generation, scheduler_generation
            ON authority_workflows
            WHEN NEW.project_generation != OLD.project_generation
              OR NEW.run_generation != OLD.run_generation
              OR NEW.runtime_generation != OLD.runtime_generation
              OR NEW.scheduler_generation != OLD.scheduler_generation
            BEGIN
                SELECT CASE WHEN NOT EXISTS (
                    SELECT 1
                    FROM authority_production_run_generation_current c
                    JOIN authority_production_run_generations g
                      ON g.run_generation=c.run_generation
                    JOIN authority_production_run_generation_creation_receipts r
                      ON r.run_generation=c.run_generation
                     AND r.receipt_sha256=c.creation_receipt_sha256
                    WHERE c.workflow_id=NEW.workflow_id
                      AND g.workflow_id=NEW.workflow_id
                      AND g.project_id=NEW.project_id
                      AND g.project_revision=NEW.current_revision
                      AND g.project_generation=NEW.project_generation
                      AND g.run_generation=NEW.run_generation
                      AND g.runtime_generation=NEW.runtime_generation
                      AND g.scheduler_generation=NEW.scheduler_generation
                ) THEN RAISE(
                    ABORT,
                    'workflow generation update lacks current companion graph'
                ) END;
            END
            """,
            """
            UPDATE authority_production_schema_state
            SET production_schema_version=3
            WHERE singleton=1
            """,
        ),
    ),
    _ProductionMigration(
        "A2_0016_PHASE9_FORENSIC_REPLAY",
        (
            """
            CREATE TABLE authority_production_phase9_replays (
                replay_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                project_revision INTEGER NOT NULL CHECK (project_revision >= 0),
                project_generation TEXT NOT NULL,
                run_generation TEXT NOT NULL UNIQUE,
                run_generation_creation_receipt_sha256 TEXT NOT NULL,
                operation_kind TEXT NOT NULL CHECK (
                    operation_kind IN ('CREATE', 'ROTATE')
                ),
                predecessor_replay_id TEXT,
                predecessor_terminal_receipt_sha256 TEXT,
                replay_mode TEXT NOT NULL CHECK (
                    replay_mode IN ('TECHNICAL', 'ABLATE_NO_JUDGE')
                ),
                requested_resume_target TEXT NOT NULL CHECK (
                    requested_resume_target = 'STEP13_PACKET_REBUILD'
                ),
                delivery_capability TEXT NOT NULL CHECK (
                    delivery_capability = 'DISABLED'
                ),
                source_commit TEXT NOT NULL,
                source_tree TEXT NOT NULL,
                source_parent TEXT NOT NULL,
                entry_gate_result_sha256 TEXT NOT NULL,
                evidence_set_sha256 TEXT NOT NULL,
                request_json TEXT NOT NULL,
                request_sha256 TEXT NOT NULL UNIQUE,
                started_at INTEGER NOT NULL CHECK (started_at >= 0),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(run_generation)
                    REFERENCES authority_production_run_generations(run_generation),
                FOREIGN KEY(run_generation_creation_receipt_sha256)
                    REFERENCES authority_production_run_generation_creation_receipts(
                        receipt_sha256
                    ),
                FOREIGN KEY(predecessor_replay_id)
                    REFERENCES authority_production_phase9_replays(replay_id),
                FOREIGN KEY(predecessor_terminal_receipt_sha256)
                    REFERENCES authority_production_phase9_terminal_receipts(
                        receipt_sha256
                    ) DEFERRABLE INITIALLY DEFERRED
            )
            """,
            """
            CREATE TABLE authority_production_phase9_replay_events (
                replay_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 1),
                event_kind TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN (
                        'READY', 'PACKET_REBUILT', 'ROLES_COLLECTED',
                        'VERDICT_COMPUTED', 'SNAPSHOT_CAPTURED', 'COMPLETED'
                    )
                ),
                predecessor_event_sha256 TEXT,
                event_json TEXT NOT NULL,
                event_sha256 TEXT NOT NULL UNIQUE,
                occurred_at INTEGER NOT NULL CHECK (occurred_at >= 0),
                PRIMARY KEY(replay_id, sequence),
                FOREIGN KEY(replay_id)
                    REFERENCES authority_production_phase9_replays(replay_id),
                FOREIGN KEY(predecessor_event_sha256)
                    REFERENCES authority_production_phase9_replay_events(event_sha256)
            )
            """,
            """
            CREATE TABLE authority_production_phase9_terminal_receipts (
                receipt_id TEXT PRIMARY KEY,
                replay_id TEXT NOT NULL UNIQUE,
                workflow_id TEXT NOT NULL,
                run_generation TEXT NOT NULL UNIQUE,
                terminal_reason TEXT NOT NULL CHECK (
                    terminal_reason IN (
                        'FORENSIC_REPLAY_COMPLETED',
                        'PERMANENT_ABLATION_NO_DELIVERY'
                    )
                ),
                exit_code INTEGER NOT NULL,
                effective_verdict TEXT NOT NULL,
                final_event_sha256 TEXT NOT NULL UNIQUE,
                receipt_json TEXT NOT NULL,
                receipt_sha256 TEXT NOT NULL UNIQUE,
                occurred_at INTEGER NOT NULL CHECK (occurred_at >= 0),
                FOREIGN KEY(replay_id)
                    REFERENCES authority_production_phase9_replays(replay_id),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(run_generation)
                    REFERENCES authority_production_run_generations(run_generation),
                FOREIGN KEY(final_event_sha256)
                    REFERENCES authority_production_phase9_replay_events(event_sha256)
            )
            """,
            """
            CREATE TABLE authority_production_phase9_replay_idempotency (
                workflow_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_sha256 TEXT NOT NULL,
                replay_id TEXT NOT NULL,
                terminal_receipt_sha256 TEXT NOT NULL,
                PRIMARY KEY(workflow_id, idempotency_key),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(replay_id)
                    REFERENCES authority_production_phase9_replays(replay_id),
                FOREIGN KEY(terminal_receipt_sha256)
                    REFERENCES authority_production_phase9_terminal_receipts(
                        receipt_sha256
                    )
            )
            """,
            """
            CREATE TABLE authority_production_phase9_replay_current (
                workflow_id TEXT PRIMARY KEY,
                replay_id TEXT NOT NULL UNIQUE,
                run_generation TEXT NOT NULL UNIQUE,
                terminal_receipt_sha256 TEXT NOT NULL,
                final_event_sha256 TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state = 'COMPLETED'),
                updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(replay_id)
                    REFERENCES authority_production_phase9_replays(replay_id),
                FOREIGN KEY(run_generation)
                    REFERENCES authority_production_run_generations(run_generation),
                FOREIGN KEY(terminal_receipt_sha256)
                    REFERENCES authority_production_phase9_terminal_receipts(
                        receipt_sha256
                    ),
                FOREIGN KEY(final_event_sha256)
                    REFERENCES authority_production_phase9_replay_events(event_sha256)
            )
            """,
            *_immutable_statements(
                "authority_production_phase9_replays",
                (("replay_id",), ("request_sha256",), ("run_generation",)),
            ),
            *_immutable_statements(
                "authority_production_phase9_replay_events",
                (("replay_id", "sequence"), ("event_sha256",)),
            ),
            *_immutable_statements(
                "authority_production_phase9_terminal_receipts",
                (("receipt_id",), ("replay_id",), ("receipt_sha256",)),
            ),
            *_immutable_statements(
                "authority_production_phase9_replay_idempotency",
                (("workflow_id", "idempotency_key"),),
            ),
            """
            CREATE TRIGGER authority_production_phase9_replay_current_insert_guard
            BEFORE INSERT ON authority_production_phase9_replay_current
            WHEN NOT EXISTS (
                SELECT 1
                FROM authority_production_phase9_replays p
                JOIN authority_production_phase9_terminal_receipts r
                  ON r.replay_id=p.replay_id
                 AND r.receipt_sha256=NEW.terminal_receipt_sha256
                 AND r.final_event_sha256=NEW.final_event_sha256
                WHERE p.replay_id=NEW.replay_id
                  AND p.workflow_id=NEW.workflow_id
                  AND p.run_generation=NEW.run_generation
                  AND p.operation_kind='CREATE'
                  AND p.predecessor_replay_id IS NULL
                  AND p.predecessor_terminal_receipt_sha256 IS NULL
            )
            BEGIN
                SELECT RAISE(ABORT, 'phase9 current insert lacks terminal graph');
            END
            """,
            """
            CREATE TRIGGER authority_production_phase9_replay_current_update_guard
            BEFORE UPDATE ON authority_production_phase9_replay_current
            WHEN NEW.workflow_id != OLD.workflow_id
              OR NEW.updated_at < OLD.updated_at
              OR NOT EXISTS (
                SELECT 1
                FROM authority_production_phase9_replays p
                JOIN authority_production_phase9_terminal_receipts r
                  ON r.replay_id=p.replay_id
                 AND r.receipt_sha256=NEW.terminal_receipt_sha256
                 AND r.final_event_sha256=NEW.final_event_sha256
                WHERE p.replay_id=NEW.replay_id
                  AND p.workflow_id=OLD.workflow_id
                  AND p.run_generation=NEW.run_generation
                  AND p.operation_kind='ROTATE'
                  AND p.predecessor_replay_id=OLD.replay_id
                  AND p.predecessor_terminal_receipt_sha256=
                      OLD.terminal_receipt_sha256
            )
            BEGIN
                SELECT RAISE(ABORT, 'phase9 current update lacks succession graph');
            END
            """,
            """
            CREATE TRIGGER authority_production_phase9_replay_current_delete_guard
            BEFORE DELETE ON authority_production_phase9_replay_current
            BEGIN
                SELECT RAISE(ABORT, 'phase9 current pointer cannot be deleted');
            END
            """,
            """
            UPDATE authority_production_schema_state
            SET production_schema_version=4
            WHERE singleton=1
            """,
        ),
    ),
    _ProductionMigration(
        "A2_0017_PHASE9_AUDIT_HARDENING",
        (
            """
            CREATE TABLE authority_production_a2_0017_empty_guard (
                marker INTEGER NOT NULL CHECK (marker = 1)
            )
            """,
            """
            INSERT INTO authority_production_a2_0017_empty_guard(marker)
            SELECT 0
            WHERE EXISTS (
                SELECT 1 FROM authority_production_run_generations
                UNION ALL
                SELECT 1 FROM authority_production_run_generation_current
                UNION ALL
                SELECT 1 FROM authority_production_run_generation_creation_receipts
                UNION ALL
                SELECT 1 FROM authority_production_run_generation_idempotency
                UNION ALL
                SELECT 1 FROM authority_production_run_generation_successions
                UNION ALL
                SELECT 1 FROM authority_production_phase9_replays
                UNION ALL
                SELECT 1 FROM authority_production_phase9_replay_events
                UNION ALL
                SELECT 1 FROM authority_production_phase9_terminal_receipts
                UNION ALL
                SELECT 1 FROM authority_production_phase9_replay_idempotency
                UNION ALL
                SELECT 1 FROM authority_production_phase9_replay_current
            )
            """,
            """
            DROP TABLE authority_production_a2_0017_empty_guard
            """,
            """
            CREATE TABLE authority_production_run_generation_source_inventories (
                inventory_sha256 TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL CHECK (
                    schema_version =
                        'authority-phase9-git-tracked-source-inventory-v1'
                ),
                source_commit TEXT NOT NULL,
                source_tree TEXT NOT NULL,
                source_parent TEXT NOT NULL,
                path_count INTEGER NOT NULL CHECK (path_count > 0),
                total_bytes INTEGER NOT NULL CHECK (total_bytes >= 0),
                inventory_json TEXT NOT NULL,
                recorded_at INTEGER NOT NULL CHECK (recorded_at >= 0)
            )
            """,
            """
            ALTER TABLE authority_production_run_generations
            ADD COLUMN source_inventory_sha256 TEXT
                REFERENCES authority_production_run_generation_source_inventories(
                    inventory_sha256
                )
            """,
            """
            ALTER TABLE authority_production_run_generations
            ADD COLUMN authorization_id TEXT
            """,
            """
            ALTER TABLE authority_production_run_generations
            ADD COLUMN authorization_target_sha256 TEXT
            """,
            """
            ALTER TABLE authority_production_run_generations
            ADD COLUMN predecessor_terminal_receipt_sha256 TEXT
                REFERENCES authority_production_phase9_terminal_receipts(receipt_sha256)
            """,
            """
            ALTER TABLE authority_production_run_generation_successions
            ADD COLUMN predecessor_terminal_receipt_sha256 TEXT
                REFERENCES authority_production_phase9_terminal_receipts(receipt_sha256)
            """,
            """
            CREATE TABLE authority_production_run_generation_authorization_consumptions (
                authorization_id TEXT PRIMARY KEY,
                authorization_receipt_sha256 TEXT NOT NULL UNIQUE,
                authorization_target_sha256 TEXT NOT NULL UNIQUE,
                request_sha256 TEXT NOT NULL UNIQUE,
                run_generation TEXT NOT NULL UNIQUE,
                workflow_id TEXT NOT NULL,
                consumed_at INTEGER NOT NULL CHECK (consumed_at >= 0),
                receipt_json TEXT NOT NULL,
                receipt_sha256 TEXT NOT NULL UNIQUE,
                FOREIGN KEY(run_generation)
                    REFERENCES authority_production_run_generations(run_generation),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id)
            )
            """,
            """
            CREATE TABLE authority_production_phase9_evidence_receipts (
                replay_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                run_generation TEXT NOT NULL,
                receipt_kind TEXT NOT NULL CHECK (
                    receipt_kind IN (
                        'ROLE_PROCESS', 'ROLE_PROVIDER', 'PROCESS_SCOPE',
                        'ACCEPTANCE_CASE'
                    )
                ),
                logical_id TEXT NOT NULL,
                logical_path TEXT NOT NULL,
                byte_length INTEGER NOT NULL CHECK (byte_length > 0),
                raw_bytes_sha256 TEXT NOT NULL,
                receipt_json TEXT NOT NULL,
                receipt_sha256 TEXT NOT NULL UNIQUE,
                occurred_at INTEGER NOT NULL CHECK (occurred_at >= 0),
                PRIMARY KEY(replay_id, receipt_kind, logical_id),
                UNIQUE(replay_id, logical_path),
                UNIQUE(raw_bytes_sha256),
                FOREIGN KEY(replay_id)
                    REFERENCES authority_production_phase9_replays(replay_id),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(run_generation)
                    REFERENCES authority_production_run_generations(run_generation)
            )
            """,
            """
            CREATE TABLE authority_production_phase9_gate_consumptions (
                gate_result_sha256 TEXT PRIMARY KEY,
                entry_state_receipt_sha256 TEXT NOT NULL,
                start_authorization_id TEXT NOT NULL UNIQUE,
                start_authorization_receipt_sha256 TEXT NOT NULL UNIQUE,
                workflow_id TEXT NOT NULL,
                run_generation TEXT NOT NULL UNIQUE,
                replay_id TEXT NOT NULL UNIQUE,
                request_sha256 TEXT NOT NULL UNIQUE,
                consumed_at INTEGER NOT NULL CHECK (consumed_at >= 0),
                receipt_json TEXT NOT NULL,
                receipt_sha256 TEXT NOT NULL UNIQUE,
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(run_generation)
                    REFERENCES authority_production_run_generations(run_generation),
                FOREIGN KEY(replay_id)
                    REFERENCES authority_production_phase9_replays(replay_id)
            )
            """,
            *_immutable_statements(
                "authority_production_run_generation_source_inventories",
                (("inventory_sha256",),),
            ),
            *_immutable_statements(
                "authority_production_run_generation_authorization_consumptions",
                (
                    ("authorization_id",),
                    ("authorization_receipt_sha256",),
                    ("authorization_target_sha256",),
                    ("request_sha256",),
                    ("run_generation",),
                    ("receipt_sha256",),
                ),
            ),
            *_immutable_statements(
                "authority_production_phase9_evidence_receipts",
                (
                    ("replay_id", "receipt_kind", "logical_id"),
                    ("replay_id", "logical_path"),
                    ("raw_bytes_sha256",),
                    ("receipt_sha256",),
                ),
            ),
            *_immutable_statements(
                "authority_production_phase9_gate_consumptions",
                (
                    ("gate_result_sha256",),
                    ("start_authorization_id",),
                    ("start_authorization_receipt_sha256",),
                    ("run_generation",),
                    ("replay_id",),
                    ("request_sha256",),
                    ("receipt_sha256",),
                ),
            ),
            """
            CREATE TRIGGER authority_production_run_generations_a2_0017_insert_guard
            BEFORE INSERT ON authority_production_run_generations
            WHEN NEW.run_mode IS NOT 'FORENSIC_REPLAY'
              OR NEW.modeling_consultation_contract IS NOT 'LEGACY_NOT_APPLICABLE'
              OR NEW.delivery_capability IS NOT 'DISABLED'
              OR NEW.source_inventory_sha256 IS NULL
              OR NEW.authorization_id IS NULL
              OR NEW.authorization_target_sha256 IS NULL
              OR (NEW.operation_kind='CREATE'
                  AND NEW.predecessor_terminal_receipt_sha256 IS NOT NULL)
              OR (NEW.operation_kind='ROTATE'
                  AND (
                      NEW.predecessor_terminal_receipt_sha256 IS NULL
                      OR NOT EXISTS (
                          SELECT 1
                          FROM authority_production_phase9_replay_current c
                          JOIN authority_production_phase9_terminal_receipts t
                            ON t.replay_id=c.replay_id
                           AND t.workflow_id=c.workflow_id
                           AND t.run_generation=c.run_generation
                           AND t.receipt_sha256=c.terminal_receipt_sha256
                          WHERE c.workflow_id=NEW.workflow_id
                            AND c.run_generation=NEW.predecessor_run_generation
                            AND c.terminal_receipt_sha256=
                                NEW.predecessor_terminal_receipt_sha256
                      )
                  ))
              OR NOT EXISTS (
                  SELECT 1
                  FROM authority_production_run_generation_source_inventories i
                  WHERE i.inventory_sha256=NEW.source_inventory_sha256
                    AND i.source_commit=NEW.source_commit
                    AND i.source_tree=NEW.source_tree
                    AND i.source_parent=NEW.source_parent
              )
            BEGIN
                SELECT RAISE(ABORT, 'run-generation lacks hardened audit binding');
            END
            """,
            """
            CREATE TRIGGER authority_production_run_generation_successions_a2_0017_guard
            BEFORE INSERT ON authority_production_run_generation_successions
            WHEN NOT EXISTS (
                SELECT 1
                FROM authority_production_run_generations g
                WHERE g.run_generation=NEW.run_generation
                  AND g.workflow_id=NEW.workflow_id
                  AND g.predecessor_run_generation IS NEW.predecessor_run_generation
                  AND g.predecessor_creation_receipt_sha256 IS
                      NEW.predecessor_creation_receipt_sha256
                  AND g.predecessor_terminal_receipt_sha256 IS
                      NEW.predecessor_terminal_receipt_sha256
            )
            BEGIN
                SELECT RAISE(ABORT, 'run-generation succession binding differs');
            END
            """,
            """
            CREATE TRIGGER authority_production_run_generation_authorization_consumption_guard
            BEFORE INSERT ON authority_production_run_generation_authorization_consumptions
            WHEN NOT EXISTS (
                SELECT 1
                FROM authority_production_run_generations g
                WHERE g.run_generation=NEW.run_generation
                  AND g.workflow_id=NEW.workflow_id
                  AND g.request_sha256=NEW.request_sha256
                  AND g.authorization_id=NEW.authorization_id
                  AND g.authorization_target_sha256=
                      NEW.authorization_target_sha256
                  AND g.operator_authorization_receipt_sha256=
                      NEW.authorization_receipt_sha256
            )
            BEGIN
                SELECT RAISE(ABORT, 'run-generation authorization consumption differs');
            END
            """,
            """
            CREATE TRIGGER authority_production_run_generation_creation_receipts_a2_0017_guard
            BEFORE INSERT ON authority_production_run_generation_creation_receipts
            WHEN NOT EXISTS (
                SELECT 1
                FROM authority_production_run_generations g
                JOIN authority_production_run_generation_authorization_consumptions a
                  ON a.run_generation=g.run_generation
                 AND a.workflow_id=g.workflow_id
                 AND a.request_sha256=g.request_sha256
                 AND a.authorization_id=g.authorization_id
                 AND a.authorization_target_sha256=g.authorization_target_sha256
                 AND a.authorization_receipt_sha256=
                     g.operator_authorization_receipt_sha256
                WHERE g.run_generation=NEW.run_generation
                  AND g.workflow_id=NEW.workflow_id
                  AND g.operation_kind=NEW.operation_kind
                  AND g.request_sha256=NEW.request_sha256
            )
            BEGIN
                SELECT RAISE(ABORT, 'run-generation receipt lacks consumed authorization');
            END
            """,
            """
            CREATE TRIGGER authority_production_run_generation_current_a2_0017_insert_guard
            BEFORE INSERT ON authority_production_run_generation_current
            WHEN NOT EXISTS (
                SELECT 1
                FROM authority_production_run_generations g
                JOIN authority_production_run_generation_authorization_consumptions a
                  ON a.run_generation=g.run_generation
                 AND a.request_sha256=g.request_sha256
                WHERE g.run_generation=NEW.run_generation
                  AND g.workflow_id=NEW.workflow_id
                  AND g.source_inventory_sha256 IS NOT NULL
            )
            BEGIN
                SELECT RAISE(ABORT, 'run-generation current lacks hardened graph');
            END
            """,
            """
            CREATE TRIGGER authority_production_run_generation_current_a2_0017_update_guard
            BEFORE UPDATE ON authority_production_run_generation_current
            WHEN NOT EXISTS (
                SELECT 1
                FROM authority_production_run_generations g
                JOIN authority_production_run_generation_authorization_consumptions a
                  ON a.run_generation=g.run_generation
                 AND a.request_sha256=g.request_sha256
                WHERE g.run_generation=NEW.run_generation
                  AND g.workflow_id=OLD.workflow_id
                  AND g.source_inventory_sha256 IS NOT NULL
            )
            BEGIN
                SELECT RAISE(ABORT, 'run-generation current lacks hardened graph');
            END
            """,
            """
            CREATE TRIGGER authority_production_phase9_terminal_a2_0017_gate_guard
            BEFORE INSERT ON authority_production_phase9_terminal_receipts
            WHEN NOT EXISTS (
                SELECT 1
                FROM authority_production_phase9_gate_consumptions c
                JOIN authority_production_phase9_replays p
                  ON p.replay_id=c.replay_id
                 AND p.workflow_id=c.workflow_id
                 AND p.run_generation=c.run_generation
                 AND p.request_sha256=c.request_sha256
                 AND p.entry_gate_result_sha256=c.gate_result_sha256
                WHERE c.replay_id=NEW.replay_id
                  AND c.workflow_id=NEW.workflow_id
                  AND c.run_generation=NEW.run_generation
                  AND NOT EXISTS (
                      SELECT 1
                      FROM authority_production_phase9_evidence_receipts e
                      WHERE e.replay_id=NEW.replay_id
                        AND (
                            e.workflow_id!=NEW.workflow_id
                            OR e.run_generation!=NEW.run_generation
                        )
                  )
                  AND (
                      SELECT COUNT(*)
                      FROM authority_production_phase9_evidence_receipts e
                      WHERE e.replay_id=NEW.replay_id
                        AND e.workflow_id=NEW.workflow_id
                        AND e.run_generation=NEW.run_generation
                        AND e.receipt_kind='PROCESS_SCOPE'
                  )=3
                  AND (
                      SELECT COUNT(*)
                      FROM authority_production_phase9_evidence_receipts e
                      WHERE e.replay_id=NEW.replay_id
                        AND e.workflow_id=NEW.workflow_id
                        AND e.run_generation=NEW.run_generation
                        AND e.receipt_kind='ACCEPTANCE_CASE'
                  )=17
                  AND (
                      (
                          p.replay_mode='TECHNICAL'
                          AND (
                              SELECT COUNT(*)
                              FROM authority_production_phase9_evidence_receipts e
                              WHERE e.replay_id=NEW.replay_id
                                AND e.workflow_id=NEW.workflow_id
                                AND e.run_generation=NEW.run_generation
                                AND e.receipt_kind='ROLE_PROCESS'
                          )=3
                          AND (
                              SELECT COUNT(*)
                              FROM authority_production_phase9_evidence_receipts e
                              WHERE e.replay_id=NEW.replay_id
                                AND e.workflow_id=NEW.workflow_id
                                AND e.run_generation=NEW.run_generation
                                AND e.receipt_kind='ROLE_PROVIDER'
                          )=3
                          AND (
                              SELECT COUNT(*)
                              FROM authority_production_phase9_evidence_receipts e
                              WHERE e.replay_id=NEW.replay_id
                          )=26
                      )
                      OR (
                          p.replay_mode='ABLATE_NO_JUDGE'
                          AND (
                              SELECT COUNT(*)
                              FROM authority_production_phase9_evidence_receipts e
                              WHERE e.replay_id=NEW.replay_id
                                AND e.receipt_kind IN (
                                    'ROLE_PROCESS', 'ROLE_PROVIDER'
                                )
                          )=0
                          AND (
                              SELECT COUNT(*)
                              FROM authority_production_phase9_evidence_receipts e
                              WHERE e.replay_id=NEW.replay_id
                          )=20
                      )
                  )
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'phase9 terminal lacks consumed gate or exact evidence inventory'
                );
            END
            """,
            """
            UPDATE authority_production_schema_state
            SET production_schema_version=5
            WHERE singleton=1
            """,
        ),
    ),
    _ProductionMigration(
        "A2_0018_PHASE9_P0_RUNNER_ATTESTATION",
        (
            """
            CREATE TABLE authority_production_phase9_p0_runner_authorizations (
                authorization_id TEXT PRIMARY KEY,
                nonce_sha256 TEXT NOT NULL UNIQUE,
                project_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                run_generation TEXT NOT NULL,
                source_commit TEXT NOT NULL,
                source_tree TEXT NOT NULL,
                source_parent TEXT NOT NULL,
                source_inventory_sha256 TEXT NOT NULL,
                live_binding_sha256 TEXT NOT NULL,
                spec_sha256 TEXT NOT NULL,
                operator_uid INTEGER NOT NULL CHECK (operator_uid >= 0),
                operator_account TEXT NOT NULL,
                issued_at INTEGER NOT NULL CHECK (issued_at >= 0),
                expires_at INTEGER NOT NULL CHECK (expires_at > issued_at),
                intended_evidence_root TEXT NOT NULL,
                python_identity_sha256 TEXT NOT NULL,
                authorization_json TEXT NOT NULL,
                authorization_receipt_sha256 TEXT NOT NULL UNIQUE,
                UNIQUE(workflow_id, run_generation, authorization_id),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(run_generation)
                    REFERENCES authority_production_run_generations(run_generation)
            )
            """,
            """
            CREATE TABLE authority_production_phase9_p0_runner_consumptions (
                authorization_id TEXT PRIMARY KEY,
                nonce_sha256 TEXT NOT NULL UNIQUE,
                invocation_id TEXT NOT NULL UNIQUE,
                consumed_at INTEGER NOT NULL CHECK (consumed_at >= 0),
                consumption_json TEXT NOT NULL,
                consumption_receipt_sha256 TEXT NOT NULL UNIQUE,
                FOREIGN KEY(authorization_id)
                    REFERENCES authority_production_phase9_p0_runner_authorizations(
                        authorization_id
                    )
            )
            """,
            """
            CREATE TABLE authority_production_phase9_p0_runner_attestations (
                authorization_id TEXT PRIMARY KEY,
                authorization_receipt_sha256 TEXT NOT NULL,
                consumption_receipt_sha256 TEXT NOT NULL,
                authority_runner_evidence_sha256 TEXT NOT NULL,
                invocation_id TEXT NOT NULL UNIQUE,
                project_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                run_generation TEXT NOT NULL UNIQUE,
                source_inventory_sha256 TEXT NOT NULL,
                live_binding_sha256 TEXT NOT NULL,
                spec_sha256 TEXT NOT NULL,
                evidence_root_sha256 TEXT NOT NULL UNIQUE,
                command_record_sha256 TEXT NOT NULL,
                raw_log_byte_length INTEGER NOT NULL CHECK (raw_log_byte_length > 0),
                raw_log_sha256 TEXT NOT NULL,
                junit_byte_length INTEGER NOT NULL CHECK (junit_byte_length > 0),
                junit_sha256 TEXT NOT NULL,
                outcome_sha256 TEXT NOT NULL,
                receipt_set_sha256 TEXT NOT NULL,
                started_at INTEGER NOT NULL CHECK (started_at >= 0),
                finished_at INTEGER NOT NULL CHECK (finished_at >= started_at),
                attested_at INTEGER NOT NULL CHECK (attested_at >= finished_at),
                exit_code INTEGER NOT NULL CHECK (exit_code = 0),
                attestation_json TEXT NOT NULL,
                attestation_sha256 TEXT NOT NULL UNIQUE,
                FOREIGN KEY(authorization_id)
                    REFERENCES authority_production_phase9_p0_runner_consumptions(
                        authorization_id
                    ),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(run_generation)
                    REFERENCES authority_production_run_generations(run_generation)
            )
            """,
            *_immutable_statements(
                "authority_production_phase9_p0_runner_authorizations",
                (
                    ("authorization_id",),
                    ("nonce_sha256",),
                    ("authorization_receipt_sha256",),
                ),
            ),
            *_immutable_statements(
                "authority_production_phase9_p0_runner_consumptions",
                (
                    ("authorization_id",),
                    ("nonce_sha256",),
                    ("invocation_id",),
                    ("consumption_receipt_sha256",),
                ),
            ),
            *_immutable_statements(
                "authority_production_phase9_p0_runner_attestations",
                (
                    ("authorization_id",),
                    ("invocation_id",),
                    ("workflow_id", "run_generation"),
                    ("evidence_root_sha256",),
                    ("attestation_sha256",),
                ),
            ),
            """
            CREATE TRIGGER authority_production_phase9_p0_runner_authorization_guard
            BEFORE INSERT ON authority_production_phase9_p0_runner_authorizations
            WHEN phase9_p0_write_capability() != 1
              OR NOT EXISTS (
                  SELECT 1
                  FROM authority_production_run_generations g
                  JOIN authority_production_run_generation_current c
                    ON c.workflow_id=g.workflow_id
                   AND c.run_generation=g.run_generation
                  WHERE g.project_id=NEW.project_id
                    AND g.workflow_id=NEW.workflow_id
                    AND g.run_generation=NEW.run_generation
                    AND g.source_commit=NEW.source_commit
                    AND g.source_tree=NEW.source_tree
                    AND g.source_parent=NEW.source_parent
                    AND g.source_inventory_sha256=NEW.source_inventory_sha256
              )
              OR EXISTS (
                  SELECT 1
                  FROM authority_production_phase9_p0_runner_attestations a
                  WHERE a.workflow_id=NEW.workflow_id
                    AND a.run_generation=NEW.run_generation
              )
            BEGIN
                SELECT RAISE(ABORT, 'P0 runner authorization is not trusted/current');
            END
            """,
            """
            CREATE TRIGGER authority_production_phase9_p0_runner_consumption_guard
            BEFORE INSERT ON authority_production_phase9_p0_runner_consumptions
            WHEN phase9_p0_write_capability() != 1
              OR NOT EXISTS (
                  SELECT 1
                  FROM authority_production_phase9_p0_runner_authorizations a
                  WHERE a.authorization_id=NEW.authorization_id
                    AND a.nonce_sha256=NEW.nonce_sha256
                    AND NEW.consumed_at>=a.issued_at
                    AND NEW.consumed_at<=a.expires_at
              )
            BEGIN
                SELECT RAISE(ABORT, 'P0 runner authorization cannot be consumed');
            END
            """,
            """
            CREATE TRIGGER authority_production_phase9_p0_runner_attestation_guard
            BEFORE INSERT ON authority_production_phase9_p0_runner_attestations
            WHEN phase9_p0_write_capability() != 1
              OR NOT EXISTS (
                  SELECT 1
                  FROM authority_production_phase9_p0_runner_authorizations a
                  JOIN authority_production_phase9_p0_runner_consumptions c
                    ON c.authorization_id=a.authorization_id
                   AND c.nonce_sha256=a.nonce_sha256
                  WHERE a.authorization_id=NEW.authorization_id
                    AND c.invocation_id=NEW.invocation_id
                    AND a.authorization_receipt_sha256=
                        NEW.authorization_receipt_sha256
                    AND c.consumption_receipt_sha256=
                        NEW.consumption_receipt_sha256
                    AND a.project_id=NEW.project_id
                    AND a.workflow_id=NEW.workflow_id
                    AND a.run_generation=NEW.run_generation
                    AND a.source_inventory_sha256=NEW.source_inventory_sha256
                    AND a.live_binding_sha256=NEW.live_binding_sha256
                    AND a.spec_sha256=NEW.spec_sha256
                    AND NEW.attested_at<=a.expires_at
              )
            BEGIN
                SELECT RAISE(ABORT, 'P0 runner attestation lacks live authorization');
            END
            """,
            """
            UPDATE authority_production_schema_state
            SET production_schema_version=6
            WHERE singleton=1
            """,
        ),
    ),
    _ProductionMigration(
        "A2_0019_PHASE9_REPLAY_EVIDENCE_ATTESTATION",
        (
            """
            CREATE TABLE authority_production_a2_0019_empty_guard (
                marker INTEGER NOT NULL CHECK (marker = 1)
            )
            """,
            """
            INSERT INTO authority_production_a2_0019_empty_guard(marker)
            SELECT 0
            WHERE EXISTS (
                SELECT 1 FROM authority_production_phase9_replays
                UNION ALL
                SELECT 1 FROM authority_production_phase9_replay_events
                UNION ALL
                SELECT 1 FROM authority_production_phase9_terminal_receipts
                UNION ALL
                SELECT 1 FROM authority_production_phase9_replay_idempotency
                UNION ALL
                SELECT 1 FROM authority_production_phase9_replay_current
                UNION ALL
                SELECT 1 FROM authority_production_phase9_evidence_receipts
                UNION ALL
                SELECT 1 FROM authority_production_phase9_gate_consumptions
            )
            """,
            """
            DROP TABLE authority_production_a2_0019_empty_guard
            """,
            """
            DROP TRIGGER authority_production_phase9_terminal_a2_0017_gate_guard
            """,
            """
            DROP TABLE authority_production_phase9_evidence_receipts
            """,
            """
            CREATE TABLE authority_production_phase9_evidence_receipts (
                replay_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                run_generation TEXT NOT NULL,
                receipt_kind TEXT NOT NULL CHECK (
                    receipt_kind IN (
                        'ROLE_PROCESS', 'ROLE_PROVIDER', 'PROCESS_SCOPE',
                        'ACCEPTANCE_CASE', 'PACKET', 'OUTBOX', 'SNAPSHOT',
                        'VERDICT'
                    )
                ),
                logical_id TEXT NOT NULL,
                logical_path TEXT NOT NULL,
                byte_length INTEGER NOT NULL CHECK (byte_length > 0),
                raw_bytes_sha256 TEXT NOT NULL,
                receipt_json TEXT NOT NULL,
                receipt_sha256 TEXT NOT NULL UNIQUE,
                occurred_at INTEGER NOT NULL CHECK (occurred_at >= 0),
                PRIMARY KEY(replay_id, receipt_kind, logical_id),
                UNIQUE(replay_id, logical_path),
                UNIQUE(raw_bytes_sha256),
                FOREIGN KEY(replay_id)
                    REFERENCES authority_production_phase9_replays(replay_id),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(run_generation)
                    REFERENCES authority_production_run_generations(run_generation)
            )
            """,
            *_immutable_statements(
                "authority_production_phase9_evidence_receipts",
                (
                    ("replay_id", "receipt_kind", "logical_id"),
                    ("replay_id", "logical_path"),
                    ("raw_bytes_sha256",),
                    ("receipt_sha256",),
                ),
            ),
            """
            CREATE TRIGGER authority_production_phase9_terminal_a2_0017_gate_guard
            BEFORE INSERT ON authority_production_phase9_terminal_receipts
            WHEN NOT EXISTS (
                SELECT 1
                FROM authority_production_phase9_gate_consumptions c
                JOIN authority_production_phase9_replays p
                  ON p.replay_id=c.replay_id
                 AND p.workflow_id=c.workflow_id
                 AND p.run_generation=c.run_generation
                 AND p.request_sha256=c.request_sha256
                 AND p.entry_gate_result_sha256=c.gate_result_sha256
                WHERE c.replay_id=NEW.replay_id
                  AND c.workflow_id=NEW.workflow_id
                  AND c.run_generation=NEW.run_generation
                  AND NOT EXISTS (
                      SELECT 1
                      FROM authority_production_phase9_evidence_receipts e
                      WHERE e.replay_id=NEW.replay_id
                        AND (
                            e.workflow_id!=NEW.workflow_id
                            OR e.run_generation!=NEW.run_generation
                        )
                  )
                  AND (
                      SELECT COUNT(*)
                      FROM authority_production_phase9_evidence_receipts e
                      WHERE e.replay_id=NEW.replay_id
                        AND e.workflow_id=NEW.workflow_id
                        AND e.run_generation=NEW.run_generation
                        AND e.receipt_kind='PROCESS_SCOPE'
                  )=3
                  AND (
                      SELECT COUNT(*)
                      FROM authority_production_phase9_evidence_receipts e
                      WHERE e.replay_id=NEW.replay_id
                        AND e.workflow_id=NEW.workflow_id
                        AND e.run_generation=NEW.run_generation
                        AND e.receipt_kind='ACCEPTANCE_CASE'
                  )=17
                  AND (
                      SELECT COUNT(*)
                      FROM authority_production_phase9_evidence_receipts e
                      WHERE e.replay_id=NEW.replay_id
                        AND e.workflow_id=NEW.workflow_id
                        AND e.run_generation=NEW.run_generation
                        AND e.receipt_kind IN (
                            'PACKET', 'OUTBOX', 'SNAPSHOT', 'VERDICT'
                        )
                  )=4
                  AND (
                      (
                          p.replay_mode='TECHNICAL'
                          AND (
                              SELECT COUNT(*)
                              FROM authority_production_phase9_evidence_receipts e
                              WHERE e.replay_id=NEW.replay_id
                                AND e.workflow_id=NEW.workflow_id
                                AND e.run_generation=NEW.run_generation
                                AND e.receipt_kind='ROLE_PROCESS'
                          )=3
                          AND (
                              SELECT COUNT(*)
                              FROM authority_production_phase9_evidence_receipts e
                              WHERE e.replay_id=NEW.replay_id
                                AND e.workflow_id=NEW.workflow_id
                                AND e.run_generation=NEW.run_generation
                                AND e.receipt_kind='ROLE_PROVIDER'
                          )=3
                          AND (
                              SELECT COUNT(*)
                              FROM authority_production_phase9_evidence_receipts e
                              WHERE e.replay_id=NEW.replay_id
                          )=30
                      )
                      OR (
                          p.replay_mode='ABLATE_NO_JUDGE'
                          AND (
                              SELECT COUNT(*)
                              FROM authority_production_phase9_evidence_receipts e
                              WHERE e.replay_id=NEW.replay_id
                                AND e.receipt_kind IN (
                                    'ROLE_PROCESS', 'ROLE_PROVIDER'
                                )
                          )=0
                          AND (
                              SELECT COUNT(*)
                              FROM authority_production_phase9_evidence_receipts e
                              WHERE e.replay_id=NEW.replay_id
                          )=24
                      )
                  )
            )
            BEGIN
                SELECT RAISE(ABORT, 'Phase9 terminal lacks exact typed evidence');
            END
            """,
            """
            CREATE TABLE authority_production_phase9_replay_runtime_authorizations (
                authorization_id TEXT PRIMARY KEY,
                nonce_sha256 TEXT NOT NULL UNIQUE,
                project_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                run_generation TEXT NOT NULL,
                source_commit TEXT NOT NULL,
                source_tree TEXT NOT NULL,
                source_parent TEXT NOT NULL,
                source_inventory_sha256 TEXT NOT NULL,
                replay_coordinate_sha256 TEXT NOT NULL,
                receipt_kind TEXT NOT NULL CHECK (
                    receipt_kind IN (
                        'ROLE_PROCESS', 'ROLE_PROVIDER', 'PROCESS_SCOPE'
                    )
                ),
                logical_id TEXT NOT NULL,
                logical_path TEXT NOT NULL,
                invocation_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                process_scope_id TEXT NOT NULL,
                packet_sha256 TEXT,
                dependency_fingerprint_sha256 TEXT NOT NULL,
                input_sha256 TEXT NOT NULL,
                operator_uid INTEGER NOT NULL CHECK (operator_uid >= 0),
                operator_account TEXT NOT NULL,
                issued_at INTEGER NOT NULL CHECK (issued_at >= 0),
                expires_at INTEGER NOT NULL CHECK (expires_at > issued_at),
                authorization_json TEXT NOT NULL,
                authorization_receipt_sha256 TEXT NOT NULL UNIQUE,
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(run_generation)
                    REFERENCES authority_production_run_generations(run_generation)
            )
            """,
            """
            CREATE TABLE authority_production_phase9_replay_runtime_completions (
                completion_sha256 TEXT PRIMARY KEY,
                authorization_id TEXT NOT NULL UNIQUE,
                nonce_sha256 TEXT NOT NULL UNIQUE,
                authorization_receipt_sha256 TEXT NOT NULL UNIQUE,
                execution_domain TEXT NOT NULL CHECK (
                    execution_domain='FORMAL_PHASE9_A'
                ),
                workflow_id TEXT NOT NULL,
                run_generation TEXT NOT NULL,
                receipt_kind TEXT NOT NULL CHECK (
                    receipt_kind IN (
                        'ROLE_PROCESS', 'ROLE_PROVIDER', 'PROCESS_SCOPE'
                    )
                ),
                logical_id TEXT NOT NULL,
                invocation_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                process_scope_id TEXT NOT NULL,
                packet_sha256 TEXT,
                dependency_fingerprint_sha256 TEXT NOT NULL,
                input_sha256 TEXT NOT NULL,
                output_sha256 TEXT NOT NULL,
                logical_path TEXT NOT NULL,
                byte_length INTEGER NOT NULL CHECK (byte_length > 0),
                raw_bytes_sha256 TEXT NOT NULL UNIQUE,
                receipt_sha256 TEXT NOT NULL UNIQUE,
                authority_source_sha256 TEXT NOT NULL,
                completed_at INTEGER NOT NULL CHECK (completed_at >= 0),
                completion_json TEXT NOT NULL,
                UNIQUE(run_generation, receipt_kind, logical_id),
                FOREIGN KEY(authorization_id)
                    REFERENCES authority_production_phase9_replay_runtime_authorizations(
                        authorization_id
                    ),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(run_generation)
                    REFERENCES authority_production_run_generations(run_generation),
                FOREIGN KEY(invocation_id)
                    REFERENCES authority_invocations(invocation_id),
                FOREIGN KEY(attempt_id) REFERENCES authority_attempts(attempt_id),
                FOREIGN KEY(process_scope_id)
                    REFERENCES authority_process_scopes(process_scope_id)
            )
            """,
            """
            CREATE TABLE authority_production_phase9_replay_runtime_records (
                record_sha256 TEXT PRIMARY KEY,
                execution_domain TEXT NOT NULL CHECK (
                    execution_domain IN ('FORMAL_PHASE9_A', 'TEST_FIXTURE')
                ),
                workflow_id TEXT NOT NULL,
                run_generation TEXT NOT NULL,
                receipt_kind TEXT NOT NULL CHECK (
                    receipt_kind IN (
                        'ROLE_PROCESS', 'ROLE_PROVIDER', 'PROCESS_SCOPE'
                    )
                ),
                logical_id TEXT NOT NULL,
                invocation_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                process_scope_id TEXT NOT NULL,
                packet_sha256 TEXT,
                dependency_fingerprint_sha256 TEXT NOT NULL,
                input_sha256 TEXT NOT NULL,
                output_sha256 TEXT NOT NULL,
                logical_path TEXT NOT NULL,
                byte_length INTEGER NOT NULL CHECK (byte_length > 0),
                raw_bytes_sha256 TEXT NOT NULL UNIQUE,
                receipt_sha256 TEXT NOT NULL UNIQUE,
                authority_source_sha256 TEXT NOT NULL,
                runtime_completion_sha256 TEXT NOT NULL UNIQUE,
                record_json TEXT NOT NULL,
                recorded_at INTEGER NOT NULL CHECK (recorded_at >= 0),
                UNIQUE(run_generation, receipt_kind, logical_id),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(run_generation)
                    REFERENCES authority_production_run_generations(run_generation),
                FOREIGN KEY(invocation_id)
                    REFERENCES authority_invocations(invocation_id),
                FOREIGN KEY(attempt_id) REFERENCES authority_attempts(attempt_id),
                FOREIGN KEY(process_scope_id)
                    REFERENCES authority_process_scopes(process_scope_id),
                FOREIGN KEY(runtime_completion_sha256)
                    REFERENCES authority_production_phase9_replay_runtime_completions(
                        completion_sha256
                    )
            )
            """,
            """
            CREATE TABLE authority_production_phase9_replay_evidence_authorizations (
                authorization_id TEXT PRIMARY KEY,
                nonce_sha256 TEXT NOT NULL UNIQUE,
                project_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                run_generation TEXT NOT NULL,
                replay_mode TEXT NOT NULL CHECK (
                    replay_mode IN ('TECHNICAL', 'ABLATE_NO_JUDGE')
                ),
                source_commit TEXT NOT NULL,
                source_tree TEXT NOT NULL,
                source_parent TEXT NOT NULL,
                source_inventory_sha256 TEXT NOT NULL,
                entry_gate_result_sha256 TEXT NOT NULL,
                entry_state_receipt_sha256 TEXT NOT NULL,
                replay_coordinate_sha256 TEXT NOT NULL,
                intended_evidence_root TEXT NOT NULL,
                acceptance_spec_sha256 TEXT NOT NULL,
                operator_uid INTEGER NOT NULL CHECK (operator_uid >= 0),
                operator_account TEXT NOT NULL,
                issued_at INTEGER NOT NULL CHECK (issued_at >= 0),
                expires_at INTEGER NOT NULL CHECK (expires_at > issued_at),
                authorization_json TEXT NOT NULL,
                authorization_receipt_sha256 TEXT NOT NULL UNIQUE,
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(run_generation)
                    REFERENCES authority_production_run_generations(run_generation)
            )
            """,
            """
            CREATE TABLE authority_production_phase9_replay_evidence_consumptions (
                authorization_id TEXT PRIMARY KEY,
                nonce_sha256 TEXT NOT NULL UNIQUE,
                invocation_id TEXT NOT NULL UNIQUE,
                consumed_at INTEGER NOT NULL CHECK (consumed_at >= 0),
                consumption_json TEXT NOT NULL,
                consumption_receipt_sha256 TEXT NOT NULL UNIQUE,
                FOREIGN KEY(authorization_id)
                    REFERENCES authority_production_phase9_replay_evidence_authorizations(
                        authorization_id
                    )
            )
            """,
            """
            CREATE TABLE authority_production_phase9_replay_evidence_attestations (
                attestation_sha256 TEXT PRIMARY KEY,
                authorization_id TEXT NOT NULL UNIQUE,
                authorization_receipt_sha256 TEXT NOT NULL UNIQUE,
                consumption_receipt_sha256 TEXT NOT NULL UNIQUE,
                invocation_id TEXT NOT NULL UNIQUE,
                execution_domain TEXT NOT NULL CHECK (
                    execution_domain IN ('FORMAL_PHASE9_A', 'TEST_FIXTURE')
                ),
                project_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                run_generation TEXT NOT NULL UNIQUE,
                replay_mode TEXT NOT NULL CHECK (
                    replay_mode IN ('TECHNICAL', 'ABLATE_NO_JUDGE')
                ),
                replay_coordinate_sha256 TEXT NOT NULL UNIQUE,
                source_inventory_sha256 TEXT NOT NULL,
                entry_gate_result_sha256 TEXT NOT NULL UNIQUE,
                entry_state_receipt_sha256 TEXT NOT NULL,
                evidence_payload_set_sha256 TEXT NOT NULL UNIQUE,
                typed_receipt_set_sha256 TEXT NOT NULL UNIQUE,
                runtime_record_set_sha256 TEXT NOT NULL,
                packet_sha256 TEXT NOT NULL,
                roles_sha256 TEXT NOT NULL,
                verdict_sha256 TEXT NOT NULL,
                snapshot_sha256 TEXT NOT NULL,
                runtime_safety_sha256 TEXT NOT NULL,
                acceptance_sha256 TEXT NOT NULL,
                acceptance_spec_sha256 TEXT NOT NULL,
                acceptance_command_json TEXT NOT NULL,
                acceptance_command_sha256 TEXT NOT NULL,
                acceptance_event_log BLOB NOT NULL,
                acceptance_event_log_sha256 TEXT NOT NULL,
                acceptance_event_nonce TEXT NOT NULL,
                acceptance_raw_log BLOB NOT NULL,
                acceptance_raw_log_sha256 TEXT NOT NULL,
                acceptance_junit_xml BLOB NOT NULL,
                acceptance_junit_sha256 TEXT NOT NULL,
                acceptance_outcome_json TEXT NOT NULL,
                acceptance_outcome_sha256 TEXT NOT NULL,
                started_at INTEGER NOT NULL CHECK (started_at >= 0),
                finished_at INTEGER NOT NULL CHECK (finished_at >= started_at),
                attested_at INTEGER NOT NULL CHECK (attested_at >= finished_at),
                attestation_json TEXT NOT NULL,
                FOREIGN KEY(authorization_id)
                    REFERENCES authority_production_phase9_replay_evidence_consumptions(
                        authorization_id
                    ),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(run_generation)
                    REFERENCES authority_production_run_generations(run_generation)
            )
            """,
            """
            CREATE TABLE authority_production_phase9_replay_evidence_attestation_items (
                attestation_sha256 TEXT NOT NULL,
                receipt_kind TEXT NOT NULL CHECK (
                    receipt_kind IN (
                        'ROLE_PROCESS', 'ROLE_PROVIDER', 'PROCESS_SCOPE',
                        'ACCEPTANCE_CASE', 'PACKET', 'OUTBOX', 'SNAPSHOT',
                        'VERDICT'
                    )
                ),
                logical_id TEXT NOT NULL,
                logical_path TEXT NOT NULL,
                byte_length INTEGER NOT NULL CHECK (byte_length > 0),
                raw_bytes_sha256 TEXT NOT NULL,
                receipt_sha256 TEXT NOT NULL,
                source_kind TEXT NOT NULL CHECK (
                    source_kind IN (
                        'RUNTIME_RECORD', 'ACCEPTANCE_RUNNER',
                        'EVIDENCE_PRODUCER'
                    )
                ),
                source_record_sha256 TEXT NOT NULL,
                invocation_id TEXT,
                attempt_id TEXT,
                process_scope_id TEXT,
                packet_sha256 TEXT,
                dependency_fingerprint_sha256 TEXT NOT NULL,
                input_sha256 TEXT NOT NULL,
                output_sha256 TEXT NOT NULL,
                item_json TEXT NOT NULL,
                item_sha256 TEXT NOT NULL UNIQUE,
                PRIMARY KEY(attestation_sha256, receipt_kind, logical_id),
                UNIQUE(attestation_sha256, logical_path),
                UNIQUE(attestation_sha256, raw_bytes_sha256),
                FOREIGN KEY(attestation_sha256)
                    REFERENCES authority_production_phase9_replay_evidence_attestations(
                        attestation_sha256
                    ) DEFERRABLE INITIALLY DEFERRED
            )
            """,
            """
            CREATE TABLE authority_production_phase9_start_authorizations (
                authorization_id TEXT PRIMARY KEY,
                nonce_sha256 TEXT NOT NULL UNIQUE,
                authorization_target_sha256 TEXT NOT NULL,
                evidence_attestation_sha256 TEXT NOT NULL,
                evidence_payload_set_sha256 TEXT NOT NULL,
                project_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                run_generation TEXT NOT NULL,
                source_commit TEXT NOT NULL,
                source_tree TEXT NOT NULL,
                source_parent TEXT NOT NULL,
                source_inventory_sha256 TEXT NOT NULL,
                entry_gate_result_sha256 TEXT NOT NULL,
                entry_state_receipt_sha256 TEXT NOT NULL,
                start_authorization_byte_length INTEGER NOT NULL CHECK (
                    start_authorization_byte_length > 0
                ),
                start_authorization_raw_bytes_sha256 TEXT NOT NULL UNIQUE,
                final_evidence_set_sha256 TEXT NOT NULL UNIQUE,
                operator_uid INTEGER NOT NULL CHECK (operator_uid >= 0),
                operator_account TEXT NOT NULL,
                issued_at INTEGER NOT NULL CHECK (issued_at >= 0),
                expires_at INTEGER NOT NULL CHECK (expires_at > issued_at),
                authorization_json TEXT NOT NULL,
                authorization_receipt_sha256 TEXT NOT NULL UNIQUE,
                FOREIGN KEY(evidence_attestation_sha256)
                    REFERENCES authority_production_phase9_replay_evidence_attestations(
                        attestation_sha256
                    ),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(run_generation)
                    REFERENCES authority_production_run_generations(run_generation)
            )
            """,
            """
            CREATE TABLE authority_production_phase9_start_authorization_consumptions (
                authorization_id TEXT PRIMARY KEY,
                nonce_sha256 TEXT NOT NULL UNIQUE,
                request_sha256 TEXT NOT NULL UNIQUE,
                replay_id TEXT NOT NULL UNIQUE,
                workflow_id TEXT NOT NULL,
                run_generation TEXT NOT NULL UNIQUE,
                consumed_at INTEGER NOT NULL CHECK (consumed_at >= 0),
                consumption_json TEXT NOT NULL,
                consumption_receipt_sha256 TEXT NOT NULL UNIQUE,
                FOREIGN KEY(authorization_id)
                    REFERENCES authority_production_phase9_start_authorizations(
                        authorization_id
                    ),
                FOREIGN KEY(replay_id)
                    REFERENCES authority_production_phase9_replays(replay_id),
                FOREIGN KEY(workflow_id) REFERENCES authority_workflows(workflow_id),
                FOREIGN KEY(run_generation)
                    REFERENCES authority_production_run_generations(run_generation)
            )
            """,
            *_immutable_statements(
                "authority_production_phase9_replay_runtime_authorizations",
                (
                    ("authorization_id",), ("nonce_sha256",),
                    ("authorization_receipt_sha256",),
                ),
            ),
            *_immutable_statements(
                "authority_production_phase9_replay_runtime_completions",
                (
                    ("completion_sha256",), ("authorization_id",),
                    ("nonce_sha256",), ("authorization_receipt_sha256",),
                    ("run_generation", "receipt_kind", "logical_id"),
                    ("raw_bytes_sha256",), ("receipt_sha256",),
                ),
            ),
            *_immutable_statements(
                "authority_production_phase9_replay_runtime_records",
                (
                    ("record_sha256",),
                    ("run_generation", "receipt_kind", "logical_id"),
                    ("raw_bytes_sha256",),
                    ("receipt_sha256",),
                ),
            ),
            *_immutable_statements(
                "authority_production_phase9_replay_evidence_authorizations",
                (
                    ("authorization_id",), ("nonce_sha256",),
                    ("authorization_receipt_sha256",),
                ),
            ),
            *_immutable_statements(
                "authority_production_phase9_replay_evidence_consumptions",
                (
                    ("authorization_id",), ("nonce_sha256",),
                    ("invocation_id",), ("consumption_receipt_sha256",),
                ),
            ),
            *_immutable_statements(
                "authority_production_phase9_replay_evidence_attestations",
                (
                    ("attestation_sha256",), ("authorization_id",),
                    ("run_generation",), ("entry_gate_result_sha256",),
                    ("evidence_payload_set_sha256",),
                ),
            ),
            *_immutable_statements(
                "authority_production_phase9_replay_evidence_attestation_items",
                (
                    ("attestation_sha256", "receipt_kind", "logical_id"),
                    ("attestation_sha256", "logical_path"),
                    ("item_sha256",),
                ),
            ),
            *_immutable_statements(
                "authority_production_phase9_start_authorizations",
                (
                    ("authorization_id",), ("nonce_sha256",),
                    ("authorization_receipt_sha256",),
                ),
            ),
            *_immutable_statements(
                "authority_production_phase9_start_authorization_consumptions",
                (
                    ("authorization_id",), ("nonce_sha256",),
                    ("request_sha256",), ("replay_id",),
                    ("run_generation",), ("consumption_receipt_sha256",),
                ),
            ),
            """
            CREATE TRIGGER authority_production_phase9_runtime_authorization_guard
            BEFORE INSERT ON authority_production_phase9_replay_runtime_authorizations
            WHEN phase9_replay_runtime_completion_capability() != 1
              OR NOT EXISTS (
                  SELECT 1
                  FROM authority_production_run_generations g
                  JOIN authority_production_run_generation_current c
                    ON c.workflow_id=g.workflow_id
                   AND c.run_generation=g.run_generation
                  WHERE g.project_id=NEW.project_id
                    AND g.workflow_id=NEW.workflow_id
                    AND g.run_generation=NEW.run_generation
                    AND g.source_commit=NEW.source_commit
                    AND g.source_tree=NEW.source_tree
                    AND g.source_parent=NEW.source_parent
                    AND g.source_inventory_sha256=NEW.source_inventory_sha256
              )
            BEGIN
                SELECT RAISE(ABORT, 'Phase9 runtime authorization is not trusted/current');
            END
            """,
            """
            CREATE TRIGGER authority_production_phase9_runtime_completion_guard
            BEFORE INSERT ON authority_production_phase9_replay_runtime_completions
            WHEN phase9_replay_runtime_completion_capability() != 1
              OR NOT EXISTS (
                  SELECT 1
                  FROM authority_production_phase9_replay_runtime_authorizations a
                  JOIN authority_invocations i
                    ON i.invocation_id=NEW.invocation_id
                  JOIN authority_attempts t
                    ON t.attempt_id=NEW.attempt_id
                   AND t.invocation_id=i.invocation_id
                  JOIN authority_process_scopes s
                    ON s.process_scope_id=NEW.process_scope_id
                   AND s.attempt_id=t.attempt_id
                  WHERE a.authorization_id=NEW.authorization_id
                    AND a.nonce_sha256=NEW.nonce_sha256
                    AND a.authorization_receipt_sha256=
                        NEW.authorization_receipt_sha256
                    AND a.workflow_id=NEW.workflow_id
                    AND a.run_generation=NEW.run_generation
                    AND a.receipt_kind=NEW.receipt_kind
                    AND a.logical_id=NEW.logical_id
                    AND a.logical_path=NEW.logical_path
                    AND a.invocation_id=NEW.invocation_id
                    AND a.attempt_id=NEW.attempt_id
                    AND a.process_scope_id=NEW.process_scope_id
                    AND a.packet_sha256 IS NEW.packet_sha256
                    AND a.dependency_fingerprint_sha256=
                        NEW.dependency_fingerprint_sha256
                    AND a.input_sha256=NEW.input_sha256
                    AND NEW.completed_at>=a.issued_at
                    AND NEW.completed_at<=a.expires_at
              )
            BEGIN
                SELECT RAISE(ABORT, 'Phase9 runtime completion lacks one-use authority');
            END
            """,
            """
            CREATE TRIGGER authority_production_phase9_runtime_record_guard
            BEFORE INSERT ON authority_production_phase9_replay_runtime_records
            WHEN phase9_replay_evidence_write_capability() != 1
              OR NOT EXISTS (
                  SELECT 1
                  FROM authority_production_phase9_replay_runtime_completions c
                  JOIN authority_invocations i
                    ON i.invocation_id=c.invocation_id
                  JOIN authority_attempts a ON a.invocation_id=i.invocation_id
                  JOIN authority_process_scopes s ON s.attempt_id=a.attempt_id
                  JOIN authority_commands cmd ON cmd.command_id=i.command_id
                  JOIN authority_production_run_generations g
                    ON g.workflow_id=i.workflow_id
                  WHERE c.completion_sha256=NEW.runtime_completion_sha256
                    AND c.execution_domain='FORMAL_PHASE9_A'
                    AND c.workflow_id=NEW.workflow_id
                    AND c.run_generation=NEW.run_generation
                    AND c.receipt_kind=NEW.receipt_kind
                    AND c.logical_id=NEW.logical_id
                    AND c.invocation_id=NEW.invocation_id
                    AND c.attempt_id=NEW.attempt_id
                    AND c.process_scope_id=NEW.process_scope_id
                    AND c.packet_sha256 IS NEW.packet_sha256
                    AND c.dependency_fingerprint_sha256=
                        NEW.dependency_fingerprint_sha256
                    AND c.input_sha256=NEW.input_sha256
                    AND c.output_sha256=NEW.output_sha256
                    AND c.logical_path=NEW.logical_path
                    AND c.byte_length=NEW.byte_length
                    AND c.raw_bytes_sha256=NEW.raw_bytes_sha256
                    AND c.receipt_sha256=NEW.receipt_sha256
                    AND c.authority_source_sha256=NEW.authority_source_sha256
                    AND i.invocation_id=NEW.invocation_id
                    AND a.attempt_id=NEW.attempt_id
                    AND s.process_scope_id=NEW.process_scope_id
                    AND i.workflow_id=NEW.workflow_id
                    AND g.run_generation=NEW.run_generation
                    AND cmd.workflow_id=NEW.workflow_id
                    AND cmd.project_id=g.project_id
                    AND cmd.command_type='PHASE9_A_RUNTIME_COMPLETION'
                    AND cmd.envelope_schema=
                        'authority-phase9-runtime-completion-v1'
                    AND i.invocation_kind='PHASE9_A_RUNTIME_COMPLETION'
                    AND i.scope_schema=
                        'authority-phase9-runtime-completion-v1'
                    AND a.scope_schema=
                        'authority-phase9-runtime-completion-v1'
                    AND s.process_kind='PHASE9_A_RUNTIME_COMPLETION'
                    AND s.scope_schema=
                        'authority-phase9-runtime-completion-v1'
              )
            BEGIN
                SELECT RAISE(ABORT, 'Phase9 runtime record lacks trusted scope');
            END
            """,
            """
            CREATE TRIGGER authority_production_phase9_evidence_authorization_guard
            BEFORE INSERT ON authority_production_phase9_replay_evidence_authorizations
            WHEN phase9_replay_evidence_write_capability() != 1
              OR NOT EXISTS (
                  SELECT 1
                  FROM authority_production_run_generations g
                  JOIN authority_production_run_generation_current c
                    ON c.workflow_id=g.workflow_id
                   AND c.run_generation=g.run_generation
                  WHERE g.project_id=NEW.project_id
                    AND g.workflow_id=NEW.workflow_id
                    AND g.run_generation=NEW.run_generation
                    AND g.source_commit=NEW.source_commit
                    AND g.source_tree=NEW.source_tree
                    AND g.source_parent=NEW.source_parent
                    AND g.source_inventory_sha256=NEW.source_inventory_sha256
              )
            BEGIN
                SELECT RAISE(ABORT, 'Phase9 evidence authorization is not trusted/current');
            END
            """,
            """
            CREATE TRIGGER authority_production_phase9_evidence_consumption_guard
            BEFORE INSERT ON authority_production_phase9_replay_evidence_consumptions
            WHEN phase9_replay_evidence_write_capability() != 1
              OR NOT EXISTS (
                  SELECT 1
                  FROM authority_production_phase9_replay_evidence_authorizations a
                  WHERE a.authorization_id=NEW.authorization_id
                    AND a.nonce_sha256=NEW.nonce_sha256
                    AND NEW.consumed_at>=a.issued_at
                    AND NEW.consumed_at<=a.expires_at
              )
            BEGIN
                SELECT RAISE(ABORT, 'Phase9 evidence authorization cannot be consumed');
            END
            """,
            """
            CREATE TRIGGER authority_production_phase9_evidence_item_guard
            BEFORE INSERT ON authority_production_phase9_replay_evidence_attestation_items
            WHEN phase9_replay_evidence_write_capability() != 1
              OR (
                  NEW.source_kind='RUNTIME_RECORD'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM authority_production_phase9_replay_runtime_records r
                      WHERE r.record_sha256=NEW.source_record_sha256
                        AND r.execution_domain='FORMAL_PHASE9_A'
                        AND r.receipt_kind=NEW.receipt_kind
                        AND r.logical_id=NEW.logical_id
                        AND r.logical_path=NEW.logical_path
                        AND r.byte_length=NEW.byte_length
                        AND r.raw_bytes_sha256=NEW.raw_bytes_sha256
                        AND r.receipt_sha256=NEW.receipt_sha256
                        AND r.invocation_id=NEW.invocation_id
                        AND r.attempt_id=NEW.attempt_id
                        AND r.process_scope_id=NEW.process_scope_id
                        AND r.packet_sha256 IS NEW.packet_sha256
                        AND r.dependency_fingerprint_sha256=
                            NEW.dependency_fingerprint_sha256
                        AND r.input_sha256=NEW.input_sha256
                        AND r.output_sha256=NEW.output_sha256
                  )
              )
              OR (
                  NEW.source_kind='ACCEPTANCE_RUNNER'
                  AND (
                      NEW.receipt_kind!='ACCEPTANCE_CASE'
                      OR NEW.invocation_id IS NOT NULL
                      OR NEW.attempt_id IS NOT NULL
                      OR NEW.process_scope_id IS NOT NULL
                      OR NEW.packet_sha256 IS NOT NULL
                  )
              )
              OR (
                  NEW.source_kind='EVIDENCE_PRODUCER'
                  AND (
                      NEW.receipt_kind NOT IN (
                          'PACKET', 'OUTBOX', 'SNAPSHOT', 'VERDICT'
                      )
                      OR NEW.invocation_id IS NOT NULL
                      OR NEW.attempt_id IS NOT NULL
                      OR NEW.process_scope_id IS NOT NULL
                      OR NEW.packet_sha256 IS NOT NULL
                      OR NOT EXISTS (
                          SELECT 1
                          FROM authority_production_phase9_replay_evidence_consumptions c
                          WHERE c.consumption_receipt_sha256=
                              NEW.source_record_sha256
                      )
                  )
              )
            BEGIN
                SELECT RAISE(ABORT, 'Phase9 evidence item lacks trusted source');
            END
            """,
            """
            CREATE TRIGGER authority_production_phase9_evidence_attestation_guard
            BEFORE INSERT ON authority_production_phase9_replay_evidence_attestations
            WHEN phase9_replay_evidence_write_capability() != 1
              OR NEW.execution_domain!='FORMAL_PHASE9_A'
              OR NOT EXISTS (
                  SELECT 1
                  FROM authority_production_phase9_replay_evidence_authorizations a
                  JOIN authority_production_phase9_replay_evidence_consumptions c
                    ON c.authorization_id=a.authorization_id
                   AND c.nonce_sha256=a.nonce_sha256
                  WHERE a.authorization_id=NEW.authorization_id
                    AND a.authorization_receipt_sha256=
                        NEW.authorization_receipt_sha256
                    AND c.consumption_receipt_sha256=
                        NEW.consumption_receipt_sha256
                    AND c.invocation_id=NEW.invocation_id
                    AND a.project_id=NEW.project_id
                    AND a.workflow_id=NEW.workflow_id
                    AND a.run_generation=NEW.run_generation
                    AND a.replay_mode=NEW.replay_mode
                    AND a.replay_coordinate_sha256=
                        NEW.replay_coordinate_sha256
                    AND a.source_inventory_sha256=
                        NEW.source_inventory_sha256
                    AND a.entry_gate_result_sha256=
                        NEW.entry_gate_result_sha256
                    AND a.entry_state_receipt_sha256=
                        NEW.entry_state_receipt_sha256
                    AND a.acceptance_spec_sha256=
                        NEW.acceptance_spec_sha256
                    AND NEW.attested_at<=a.expires_at
              )
              OR (
                  SELECT COUNT(*)
                  FROM authority_production_phase9_replay_evidence_attestation_items i
                  WHERE i.attestation_sha256=NEW.attestation_sha256
                    AND i.receipt_kind='PROCESS_SCOPE'
              )!=3
              OR (
                  SELECT COUNT(*)
                  FROM authority_production_phase9_replay_evidence_attestation_items i
                  WHERE i.attestation_sha256=NEW.attestation_sha256
                    AND i.receipt_kind='ACCEPTANCE_CASE'
              )!=17
              OR (
                  NEW.replay_mode='TECHNICAL'
                  AND (
                      SELECT COUNT(*)
                      FROM authority_production_phase9_replay_evidence_attestation_items i
                      WHERE i.attestation_sha256=NEW.attestation_sha256
                        AND i.receipt_kind IN ('ROLE_PROCESS', 'ROLE_PROVIDER')
                  )!=6
              )
              OR (
                  NEW.replay_mode='ABLATE_NO_JUDGE'
                  AND EXISTS (
                      SELECT 1
                      FROM authority_production_phase9_replay_evidence_attestation_items i
                      WHERE i.attestation_sha256=NEW.attestation_sha256
                        AND i.receipt_kind IN ('ROLE_PROCESS', 'ROLE_PROVIDER')
                  )
              )
              OR (
                  SELECT COUNT(*)
                  FROM authority_production_phase9_replay_evidence_attestation_items i
                  WHERE i.attestation_sha256=NEW.attestation_sha256
                    AND i.receipt_kind IN ('PACKET','OUTBOX','SNAPSHOT','VERDICT')
              )!=4
            BEGIN
                SELECT RAISE(ABORT, 'Phase9 evidence attestation lacks exact trusted inventory');
            END
            """,
            """
            CREATE TRIGGER authority_production_phase9_start_authorization_guard
            BEFORE INSERT ON authority_production_phase9_start_authorizations
            WHEN phase9_replay_evidence_write_capability() != 1
              OR NOT EXISTS (
                  SELECT 1
                  FROM authority_production_phase9_replay_evidence_attestations a
                  WHERE a.attestation_sha256=NEW.evidence_attestation_sha256
                    AND a.execution_domain='FORMAL_PHASE9_A'
                    AND a.project_id=NEW.project_id
                    AND a.workflow_id=NEW.workflow_id
                    AND a.run_generation=NEW.run_generation
                    AND a.source_inventory_sha256=NEW.source_inventory_sha256
                    AND a.entry_gate_result_sha256=NEW.entry_gate_result_sha256
                    AND a.entry_state_receipt_sha256=
                        NEW.entry_state_receipt_sha256
                    AND a.evidence_payload_set_sha256=
                        NEW.evidence_payload_set_sha256
              )
            BEGIN
                SELECT RAISE(ABORT, 'Phase9 start authorization lacks formal attestation');
            END
            """,
            """
            CREATE TRIGGER authority_production_phase9_start_consumption_guard
            BEFORE INSERT ON authority_production_phase9_start_authorization_consumptions
            WHEN phase9_replay_start_write_capability() != 1
              OR NOT EXISTS (
                  SELECT 1
                  FROM authority_production_phase9_start_authorizations a
                  JOIN authority_production_phase9_replays p
                    ON p.replay_id=NEW.replay_id
                   AND p.workflow_id=NEW.workflow_id
                   AND p.run_generation=NEW.run_generation
                   AND p.request_sha256=NEW.request_sha256
                  WHERE a.authorization_id=NEW.authorization_id
                    AND a.nonce_sha256=NEW.nonce_sha256
                    AND a.workflow_id=NEW.workflow_id
                    AND a.run_generation=NEW.run_generation
                    AND NEW.consumed_at>=a.issued_at
                    AND NEW.consumed_at<=a.expires_at
              )
            BEGIN
                SELECT RAISE(ABORT, 'Phase9 start authorization cannot be consumed');
            END
            """,
            """
            CREATE TRIGGER authority_production_phase9_terminal_a2_0019_attestation_guard
            BEFORE INSERT ON authority_production_phase9_terminal_receipts
            WHEN NOT EXISTS (
                SELECT 1
                FROM authority_production_phase9_replays p
                JOIN authority_production_phase9_start_authorization_consumptions c
                  ON c.replay_id=p.replay_id
                 AND c.workflow_id=p.workflow_id
                 AND c.run_generation=p.run_generation
                 AND c.request_sha256=p.request_sha256
                JOIN authority_production_phase9_start_authorizations s
                  ON s.authorization_id=c.authorization_id
                JOIN authority_production_phase9_replay_evidence_attestations a
                  ON a.attestation_sha256=s.evidence_attestation_sha256
                WHERE p.replay_id=NEW.replay_id
                  AND p.workflow_id=NEW.workflow_id
                  AND p.run_generation=NEW.run_generation
                  AND a.execution_domain='FORMAL_PHASE9_A'
                  AND p.entry_gate_result_sha256=s.entry_gate_result_sha256
                  AND p.evidence_set_sha256=s.final_evidence_set_sha256
                  AND NOT EXISTS (
                      SELECT 1
                      FROM authority_production_phase9_replay_evidence_attestation_items i
                      LEFT JOIN authority_production_phase9_evidence_receipts e
                        ON e.replay_id=p.replay_id
                       AND e.receipt_kind=i.receipt_kind
                       AND e.logical_id=i.logical_id
                       AND e.logical_path=i.logical_path
                       AND e.byte_length=i.byte_length
                       AND e.raw_bytes_sha256=i.raw_bytes_sha256
                       AND e.receipt_sha256=i.receipt_sha256
                      WHERE i.attestation_sha256=a.attestation_sha256
                        AND e.replay_id IS NULL
                  )
                  AND (
                      SELECT COUNT(*)
                      FROM authority_production_phase9_replay_evidence_attestation_items i
                      WHERE i.attestation_sha256=a.attestation_sha256
                  )=(
                      SELECT COUNT(*)
                      FROM authority_production_phase9_evidence_receipts e
                      WHERE e.replay_id=p.replay_id
                  )
            )
            BEGIN
                SELECT RAISE(ABORT, 'Phase9 terminal lacks Authority replay attestation');
            END
            """,
            """
            UPDATE authority_production_schema_state
            SET production_schema_version=7
            WHERE singleton=1
            """,
        ),
    ),
)

# This suffix is append-only. Never change an A2_0010..A2_0019 statement to
# retrofit runtime authorization into an already published completion grant.
_A19_RUNTIME_RECORD_GUARD = next(
    statement for statement in PRODUCTION_MIGRATIONS[-1].statements
    if "CREATE TRIGGER authority_production_phase9_runtime_record_guard" in statement
)
_A19_SCOPE_START = _A19_RUNTIME_RECORD_GUARD.index("AND cmd.command_type=")
_A19_SCOPE_END = _A19_RUNTIME_RECORD_GUARD.index("\n              )\n            BEGIN", _A19_SCOPE_START)
_A20_RUNTIME_RECORD_GUARD = (
    _A19_RUNTIME_RECORD_GUARD[:_A19_SCOPE_START]
    + "AND ((" + _A19_RUNTIME_RECORD_GUARD[_A19_SCOPE_START + 4:_A19_SCOPE_END] + ") OR ("
    + """
                        cmd.command_type='PHASE9_A_RUNTIME_DISPATCH'
                        AND cmd.envelope_schema='authority-phase9-runtime-dispatch-intent-v1'
                        AND i.invocation_kind='PHASE9_A_RUNTIME_DISPATCH'
                        AND i.scope_schema=cmd.envelope_schema
                        AND a.scope_schema=cmd.envelope_schema
                        AND s.process_kind='PHASE9_A_RUNTIME_DISPATCH'
                        AND s.scope_schema=cmd.envelope_schema
                        AND i.scope_json=cmd.envelope_json
                        AND a.scope_json=cmd.envelope_json
                        AND s.scope_json=cmd.envelope_json
                        AND EXISTS (
                            SELECT 1 FROM authority_production_phase9_runtime_attempts da
                            JOIN authority_production_phase9_runtime_runs dr ON dr.runtime_id=da.runtime_id
                            JOIN authority_production_phase9_runtime_launches dl ON dl.attempt_id=da.attempt_id
                            JOIN authority_production_phase9_runtime_observations obs ON obs.attempt_id=da.attempt_id
                            JOIN authority_production_phase9_runtime_terminals dt ON dt.runtime_id=da.runtime_id
                            JOIN authority_production_phase9_runtime_receipt_bindings rb ON rb.attempt_id=da.attempt_id
                            WHERE da.attempt_id=NEW.attempt_id
                              AND da.invocation_id=NEW.invocation_id
                              AND da.process_scope_id=NEW.process_scope_id
                              AND da.role=NEW.logical_id
                              AND dr.workflow_id=NEW.workflow_id
                              AND dr.run_generation=NEW.run_generation
                              AND obs.outcome='SUCCEEDED' AND dt.terminal_status='COMPLETED'
                              AND rb.receipt_kind=NEW.receipt_kind
                              AND json_extract(rb.binding_json,'$.raw_bytes_sha256')=NEW.raw_bytes_sha256
                              AND json_extract(rb.binding_json,'$.receipt_sha256')=NEW.receipt_sha256
                              AND json_extract(rb.binding_json,'$.byte_length')=NEW.byte_length
                              AND json_extract(rb.binding_json,'$.logical_path')=NEW.logical_path
                              AND json_extract(rb.binding_json,'$.observation_sha256')=obs.observation_sha256
                        )
                    ))"""
    + _A19_RUNTIME_RECORD_GUARD[_A19_SCOPE_END:]
)
PRODUCTION_MIGRATIONS += (
    _ProductionMigration(
        "A2_0020_PHASE9_RUNTIME_EXECUTION",
        (
            """
            CREATE TABLE authority_production_phase9_dispatch_grants (
                grant_id TEXT PRIMARY KEY,
                nonce_sha256 TEXT NOT NULL UNIQUE,
                target_sha256 TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                run_generation TEXT NOT NULL,
                issued_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                grant_json TEXT NOT NULL,
                grant_sha256 TEXT NOT NULL UNIQUE
            )
            """,
            """
            CREATE TABLE authority_production_phase9_runtime_runs (
                runtime_id TEXT PRIMARY KEY,
                grant_id TEXT NOT NULL UNIQUE REFERENCES authority_production_phase9_dispatch_grants(grant_id),
                nonce_sha256 TEXT NOT NULL UNIQUE,
                workflow_id TEXT NOT NULL,
                run_generation TEXT NOT NULL UNIQUE,
                target_sha256 TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                deadline_at INTEGER NOT NULL,
                start_json TEXT NOT NULL,
                start_sha256 TEXT NOT NULL UNIQUE
            )
            """,
            """
            CREATE TABLE authority_production_phase9_runtime_attempts (
                attempt_id TEXT PRIMARY KEY,
                runtime_id TEXT NOT NULL REFERENCES authority_production_phase9_runtime_runs(runtime_id),
                role TEXT NOT NULL CHECK (role IN ('math','execution','paper','failed','kill','pause')),
                role_attempt INTEGER NOT NULL CHECK (role_attempt BETWEEN 1 AND 8),
                invocation_id TEXT NOT NULL UNIQUE,
                process_scope_id TEXT NOT NULL UNIQUE,
                dispatch_intent_json TEXT NOT NULL,
                dispatch_intent_sha256 TEXT NOT NULL UNIQUE,
                committed_at INTEGER NOT NULL,
                UNIQUE(runtime_id, role, role_attempt)
            )
            """,
            """
            CREATE TABLE authority_production_phase9_runtime_observations (
                attempt_id TEXT PRIMARY KEY REFERENCES authority_production_phase9_runtime_attempts(attempt_id),
                outcome TEXT NOT NULL CHECK (outcome IN ('SUCCEEDED','FAILED','TIMEOUT','CANCELLED','UNCERTAIN')),
                observed_at INTEGER NOT NULL,
                observation_json TEXT NOT NULL,
                observation_sha256 TEXT NOT NULL UNIQUE
            )
            """,
            """
            CREATE TABLE authority_production_phase9_runtime_launches (
                attempt_id TEXT PRIMARY KEY REFERENCES authority_production_phase9_runtime_attempts(attempt_id),
                process_pid INTEGER NOT NULL CHECK(process_pid > 0),
                process_start_ticks TEXT NOT NULL,
                launch_json TEXT NOT NULL,
                launch_sha256 TEXT NOT NULL UNIQUE
            )
            """,
            """
            CREATE TABLE authority_production_phase9_runtime_accepted_outputs (
                attempt_id TEXT PRIMARY KEY REFERENCES authority_production_phase9_runtime_attempts(attempt_id),
                selection_json TEXT NOT NULL,
                selection_sha256 TEXT NOT NULL UNIQUE
            )
            """,
            """
            CREATE TABLE authority_production_phase9_runtime_receipt_bindings (
                attempt_id TEXT NOT NULL REFERENCES authority_production_phase9_runtime_attempts(attempt_id),
                receipt_kind TEXT NOT NULL CHECK(receipt_kind IN ('ROLE_PROVIDER','ROLE_PROCESS','PROCESS_SCOPE')),
                replay_coordinate_sha256 TEXT NOT NULL,
                binding_json TEXT NOT NULL,
                binding_sha256 TEXT NOT NULL UNIQUE,
                PRIMARY KEY(attempt_id,receipt_kind)
            )
            """,
            """
            CREATE TABLE authority_production_phase9_runtime_terminals (
                runtime_id TEXT PRIMARY KEY REFERENCES authority_production_phase9_runtime_runs(runtime_id),
                terminal_status TEXT NOT NULL CHECK (terminal_status IN ('COMPLETED','BLOCKED','UNCERTAIN')),
                completed_at INTEGER NOT NULL,
                terminal_json TEXT NOT NULL,
                terminal_sha256 TEXT NOT NULL UNIQUE
            )
            """,
            """
            CREATE TABLE authority_production_phase9_runtime_export_stages (
                runtime_id TEXT NOT NULL REFERENCES authority_production_phase9_runtime_runs(runtime_id),
                evidence_root TEXT NOT NULL,
                stage TEXT NOT NULL,
                stage_json TEXT NOT NULL,
                stage_sha256 TEXT NOT NULL UNIQUE,
                PRIMARY KEY(runtime_id,evidence_root,stage)
            )
            """,
            *_immutable_statements("authority_production_phase9_runtime_export_stages", (("runtime_id", "evidence_root", "stage"),)),
            """
            CREATE TRIGGER authority_production_phase9_runtime_export_stages_writer_guard
            BEFORE INSERT ON authority_production_phase9_runtime_export_stages
            WHEN COALESCE(phase9_runtime_execution_capability(),0) != 1
            BEGIN
                SELECT RAISE(ABORT, 'Phase9 export requires its trusted execution path');
            END
            """,
            *tuple(
                statement
                for table, identity in (
                    ("authority_production_phase9_dispatch_grants", "grant_id"),
                    ("authority_production_phase9_runtime_runs", "runtime_id"),
                    ("authority_production_phase9_runtime_attempts", "attempt_id"),
                    ("authority_production_phase9_runtime_observations", "attempt_id"),
                    ("authority_production_phase9_runtime_launches", "attempt_id"),
                    ("authority_production_phase9_runtime_accepted_outputs", "attempt_id"),
                    ("authority_production_phase9_runtime_terminals", "runtime_id"),
                )
                for statement in (
                    *_immutable_statements(table, ((identity,),)),
                    f"""
                    CREATE TRIGGER {table}_runtime_writer_guard
                    BEFORE INSERT ON {table}
                    WHEN COALESCE(phase9_runtime_execution_capability(),0) != 1
                    BEGIN
                        SELECT RAISE(ABORT, 'Phase9 runtime requires its trusted execution path');
                    END
                    """,
                )
            ),
            *_immutable_statements("authority_production_phase9_runtime_receipt_bindings", (("attempt_id", "receipt_kind"),)),
            """
            CREATE TRIGGER authority_production_phase9_runtime_receipt_bindings_writer_guard
            BEFORE INSERT ON authority_production_phase9_runtime_receipt_bindings
            WHEN COALESCE(phase9_runtime_execution_capability(),0) != 1
            BEGIN
                SELECT RAISE(ABORT, 'Phase9 runtime requires its trusted execution path');
            END
            """,
            "DROP TRIGGER authority_production_phase9_runtime_record_guard",
            _A20_RUNTIME_RECORD_GUARD,
            """
            UPDATE authority_production_schema_state
            SET production_schema_version=8
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


def connect_authority_ro(
    path: Path, *, timeout_seconds: float = 2.0
) -> sqlite3.Connection:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or timeout_seconds <= 0
    ):
        raise AuthorityProductionSourceError(
            "read-only Authority timeout must be positive seconds"
        )
    uri = f"file:{path.as_posix()}?mode=ro"
    connection = sqlite3.connect(
        uri, uri=True, timeout=float(timeout_seconds), isolation_level=None
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        f"PRAGMA busy_timeout={max(1, int(float(timeout_seconds) * 1000))}"
    )
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
            CREATE TABLE authority_contract_pin_sets(pin_set_sha256 TEXT PRIMARY KEY);
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
                    and row["production_schema_version"] in {1, 2, 3, 4, 5, 6, 7, 8}
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
        """Run while excluding delivery commits for project-local state."""

        from .phase9_authority_lease import authority_database_commit_lease

        with authority_database_commit_lease(self.path):
            return self._run_under_commit_lease(owner_token)

    def _run_under_commit_lease(
        self, owner_token: str
    ) -> ProductionMigrationReport:
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
                    state["production_schema_version"] not in {1, 2, 3, 4, 5, 6, 7, 8}
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
                        if (
                            state["production_schema_version"]
                            != AUTHORITY_PRODUCTION_SCHEMA_VERSION
                        ):
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
                    expected_prefix_version = max(1, len(rows) - 3)
                    if (
                        len(rows) < 5
                        or len(rows) >= len(PRODUCTION_MIGRATIONS)
                        or state["production_schema_version"]
                        != expected_prefix_version
                    ):
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
