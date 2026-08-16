from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from .domain import (
    SCHEMA_VERSION,
    InvalidTransition,
    RevisionConflict,
    RunnerLeaseLost,
    StateNotInitialized,
    WorkflowEvent,
    WorkflowState,
    WorkflowStatus,
)
from .stages import (
    STAGE_CATALOG_VERSION,
    STAGE_SCHEDULER_GENERATION,
    STEP_SCHEDULER_GENERATION,
    completed_stage_for_step,
    initial_stage_checkpoints,
)
from .workflow_events import ENVELOPE_KEY, build_event_payload, canonical_hash


_UNSET = object()

_SENSITIVE_KEY = re.compile(
    r"(?:password|secret|token|api[_-]?key|credential|authorization|cookie|private[_-]?key)",
    re.IGNORECASE,
)
_MUTABLE_COLUMNS = {
    "control_mode",
    "runtime_generation",
    "scheduler_generation",
    "stage_catalog_version",
    "status",
    "last_completed_step",
    "active_step",
    "last_completed_stage",
    "active_stage",
    "active_subtask",
    "source_step_id",
    "attempt",
    "pending_action",
    "runner_pid",
    "runner_lease_id",
    "heartbeat_at",
    "storage_scope",
}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    return value


class SQLiteStateStore:
    def __init__(self, project_dir: str | Path, *, clock: Callable[[], float] = time.time):
        self.project_dir = Path(project_dir).resolve()
        self.path = self.project_dir / ".factory" / "state.db"
        self._clock = clock

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _session(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_info (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL,
                project_id TEXT NOT NULL,
                project_type TEXT NOT NULL,
                control_mode TEXT NOT NULL,
                runtime_generation TEXT NOT NULL,
                scheduler_generation TEXT NOT NULL,
                stage_catalog_version TEXT,
                status TEXT NOT NULL,
                last_completed_step INTEGER NOT NULL,
                active_step INTEGER,
                last_completed_stage INTEGER NOT NULL,
                active_stage INTEGER,
                active_subtask TEXT,
                source_step_id INTEGER,
                attempt INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                pending_action_json TEXT,
                runner_pid INTEGER,
                runner_lease_id TEXT,
                heartbeat_at INTEGER,
                storage_scope TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_event_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                revision INTEGER PRIMARY KEY,
                type TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                step INTEGER,
                attempt INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_config (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                solver_mode TEXT NOT NULL,
                solver_threshold_seconds INTEGER NOT NULL,
                solver_runtimes_json TEXT NOT NULL,
                updated_revision INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS solver_jobs (
                job_id TEXT PRIMARY KEY,
                job_revision INTEGER NOT NULL,
                idempotency_key TEXT,
                request_sha256 TEXT,
                owner_stage INTEGER,
                owner_subtask TEXT,
                owner_revision INTEGER,
                attempt_id TEXT,
                backend TEXT NOT NULL,
                runtime TEXT NOT NULL,
                script TEXT NOT NULL,
                workdir TEXT NOT NULL,
                argv_json TEXT NOT NULL,
                max_time_seconds INTEGER NOT NULL,
                external_id TEXT,
                status TEXT NOT NULL,
                requested_at INTEGER NOT NULL,
                started_at INTEGER,
                finished_at INTEGER,
                result_refs_json TEXT NOT NULL,
                failure_json TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS solver_jobs_idempotency_key_unique
            ON solver_jobs(idempotency_key) WHERE idempotency_key IS NOT NULL;
            CREATE TABLE IF NOT EXISTS contest_policy (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                profile TEXT NOT NULL,
                contest_started_at INTEGER NOT NULL,
                contest_deadline_at INTEGER NOT NULL,
                content_freeze_at INTEGER NOT NULL,
                delivery_freeze_at INTEGER NOT NULL,
                delivery_reserve_seconds INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workflow_decisions (
                gate TEXT PRIMARY KEY,
                decided_at INTEGER NOT NULL,
                decision_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workflow_decision_requests (
                request_id TEXT PRIMARY KEY,
                gate_type TEXT NOT NULL,
                generation INTEGER NOT NULL,
                kind TEXT NOT NULL,
                action_type TEXT NOT NULL,
                requested_revision INTEGER NOT NULL,
                subject_fingerprint TEXT NOT NULL,
                options_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                request_json TEXT NOT NULL,
                UNIQUE(gate_type, generation)
            );
            CREATE TABLE IF NOT EXISTS workflow_decision_instances (
                decision_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                outcome TEXT NOT NULL,
                approved INTEGER,
                selected_option_id TEXT,
                reason TEXT NOT NULL,
                evidence_manifest_sha256 TEXT NOT NULL,
                decided_by TEXT NOT NULL,
                decided_at INTEGER NOT NULL,
                decision_json TEXT NOT NULL,
                FOREIGN KEY(request_id) REFERENCES workflow_decision_requests(request_id)
            );
            CREATE TABLE IF NOT EXISTS projector_snapshots (
                projector_name TEXT PRIMARY KEY,
                projector_version INTEGER NOT NULL,
                through_revision INTEGER NOT NULL,
                state_hash TEXT NOT NULL,
                through_event_id TEXT,
                through_event_payload_sha256 TEXT,
                source_chain_root_sha256 TEXT,
                snapshot_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projection_failures (
                revision INTEGER NOT NULL,
                projector_name TEXT NOT NULL,
                error_type TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                resolved_at INTEGER,
                PRIMARY KEY(revision, projector_name)
            );
            CREATE TABLE IF NOT EXISTS stage_cursor_inputs (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                stage_id INTEGER NOT NULL,
                subtask TEXT NOT NULL,
                source_step_id INTEGER NOT NULL,
                input_fingerprint TEXT NOT NULL,
                baseline_json TEXT NOT NULL,
                selected_revision INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage_checkpoints (
                stage_id INTEGER NOT NULL,
                subtask TEXT NOT NULL,
                source_step_id INTEGER NOT NULL,
                completed_step_id INTEGER,
                input_fingerprint TEXT NOT NULL,
                output_fingerprint TEXT NOT NULL,
                completed_revision INTEGER NOT NULL,
                receipt_json TEXT NOT NULL,
                PRIMARY KEY(stage_id, subtask)
            );
            CREATE TABLE IF NOT EXISTS stage_checkpoint_history (
                checkpoint_id TEXT PRIMARY KEY,
                stage_id INTEGER NOT NULL,
                subtask TEXT NOT NULL,
                source_step_id INTEGER NOT NULL,
                completed_step_id INTEGER,
                input_fingerprint TEXT NOT NULL,
                output_fingerprint TEXT NOT NULL,
                completed_revision INTEGER NOT NULL,
                receipt_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dirty_flags (
                flag TEXT PRIMARY KEY,
                owner_stage INTEGER NOT NULL,
                cause_revision INTEGER NOT NULL,
                cause_artifact TEXT NOT NULL,
                baseline_fingerprint TEXT NOT NULL,
                current_fingerprint TEXT NOT NULL,
                classifier_contract_sha256 TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dirty_causes (
                cause_id TEXT PRIMARY KEY,
                flag TEXT NOT NULL,
                owner_stage INTEGER NOT NULL,
                cause_revision INTEGER NOT NULL,
                cause_artifact TEXT NOT NULL,
                baseline_fingerprint TEXT NOT NULL,
                current_fingerprint TEXT NOT NULL,
                classifier_contract_sha256 TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dirty_flag_clear_receipts (
                revision INTEGER NOT NULL,
                flag TEXT NOT NULL,
                owner_stage INTEGER NOT NULL,
                cleared_fingerprint TEXT NOT NULL,
                classifier_contract_sha256 TEXT NOT NULL,
                receipt_json TEXT NOT NULL,
                PRIMARY KEY(revision, flag)
            );
            CREATE TRIGGER IF NOT EXISTS events_append_only_update
            BEFORE UPDATE ON events
            BEGIN
                SELECT RAISE(ABORT, 'workflow events are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS events_append_only_delete
            BEFORE DELETE ON events
            BEGIN
                SELECT RAISE(ABORT, 'workflow events are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS workflow_decisions_append_only_update
            BEFORE UPDATE ON workflow_decisions
            BEGIN
                SELECT RAISE(ABORT, 'workflow decisions are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS workflow_decisions_append_only_delete
            BEFORE DELETE ON workflow_decisions
            BEGIN
                SELECT RAISE(ABORT, 'workflow decisions are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS workflow_decision_requests_immutable_identity
            BEFORE UPDATE OF request_id, gate_type, generation, kind, action_type,
                             requested_revision, subject_fingerprint,
                             options_fingerprint, created_at, request_json
            ON workflow_decision_requests
            BEGIN
                SELECT RAISE(ABORT, 'workflow decision requests have immutable identity');
            END;
            CREATE TRIGGER IF NOT EXISTS workflow_decision_requests_append_only_delete
            BEFORE DELETE ON workflow_decision_requests
            BEGIN
                SELECT RAISE(ABORT, 'workflow decision requests are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS workflow_decision_instances_append_only_update
            BEFORE UPDATE ON workflow_decision_instances
            BEGIN
                SELECT RAISE(ABORT, 'workflow decision instances are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS workflow_decision_instances_append_only_delete
            BEFORE DELETE ON workflow_decision_instances
            BEGIN
                SELECT RAISE(ABORT, 'workflow decision instances are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS dirty_flag_clear_receipts_append_only_update
            BEFORE UPDATE ON dirty_flag_clear_receipts
            BEGIN
                SELECT RAISE(ABORT, 'dirty clear receipts are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS stage_checkpoint_history_append_only_update
            BEFORE UPDATE ON stage_checkpoint_history
            BEGIN
                SELECT RAISE(ABORT, 'stage checkpoint history is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS stage_checkpoint_history_append_only_delete
            BEFORE DELETE ON stage_checkpoint_history
            BEGIN
                SELECT RAISE(ABORT, 'stage checkpoint history is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS dirty_causes_append_only_update
            BEFORE UPDATE ON dirty_causes
            BEGIN
                SELECT RAISE(ABORT, 'dirty causes are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS dirty_causes_append_only_delete
            BEFORE DELETE ON dirty_causes
            BEGIN
                SELECT RAISE(ABORT, 'dirty causes are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS dirty_flag_clear_receipts_append_only_delete
            BEFORE DELETE ON dirty_flag_clear_receipts
            BEGIN
                SELECT RAISE(ABORT, 'dirty clear receipts are append-only');
            END;
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO schema_info(singleton, schema_version) VALUES (1, ?)",
            (SCHEMA_VERSION,),
        )
        current = connection.execute(
            "SELECT schema_version FROM schema_info WHERE singleton = 1"
        ).fetchone()[0]
        if current != SCHEMA_VERSION:
            raise RuntimeError(f"unsupported workflow schema {current}; expected {SCHEMA_VERSION}")

    @staticmethod
    def _upgrade_schema(connection: sqlite3.Connection) -> None:
        try:
            row = connection.execute(
                "SELECT schema_version FROM schema_info WHERE singleton = 1"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            raise StateNotInitialized("workflow schema is not initialized") from exc
        if row is None:
            raise StateNotInitialized("workflow schema is not initialized")
        current = int(row[0])
        if current == SCHEMA_VERSION:
            return
        if current not in {1, 2, 3, 4, 5, 6, 7}:
            raise RuntimeError(
                f"unsupported workflow schema {current}; expected {SCHEMA_VERSION}"
            )
        connection.execute("BEGIN IMMEDIATE")
        columns = {
            column[1]
            for column in connection.execute("PRAGMA table_info(project_state)").fetchall()
        }
        if "runtime_generation" not in columns:
            connection.execute(
                "ALTER TABLE project_state ADD COLUMN runtime_generation TEXT NOT NULL "
                "DEFAULT 'legacy_adapter'"
            )
        if "scheduler_generation" not in columns:
            connection.execute(
                "ALTER TABLE project_state ADD COLUMN scheduler_generation TEXT NOT NULL "
                f"DEFAULT '{STEP_SCHEDULER_GENERATION}'"
            )
        if "stage_catalog_version" not in columns:
            connection.execute(
                "ALTER TABLE project_state ADD COLUMN stage_catalog_version TEXT"
            )
        if "last_completed_stage" not in columns:
            connection.execute(
                "ALTER TABLE project_state ADD COLUMN last_completed_stage INTEGER NOT NULL "
                "DEFAULT 0"
            )
        if "active_stage" not in columns:
            connection.execute("ALTER TABLE project_state ADD COLUMN active_stage INTEGER")
        if "active_subtask" not in columns:
            connection.execute("ALTER TABLE project_state ADD COLUMN active_subtask TEXT")
        if "source_step_id" not in columns:
            connection.execute("ALTER TABLE project_state ADD COLUMN source_step_id INTEGER")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS project_config (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                solver_mode TEXT NOT NULL,
                solver_threshold_seconds INTEGER NOT NULL,
                solver_runtimes_json TEXT NOT NULL,
                updated_revision INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS solver_jobs (
                job_id TEXT PRIMARY KEY,
                job_revision INTEGER NOT NULL,
                idempotency_key TEXT,
                request_sha256 TEXT,
                owner_stage INTEGER,
                owner_subtask TEXT,
                owner_revision INTEGER,
                attempt_id TEXT,
                backend TEXT NOT NULL,
                runtime TEXT NOT NULL,
                script TEXT NOT NULL,
                workdir TEXT NOT NULL,
                argv_json TEXT NOT NULL,
                max_time_seconds INTEGER NOT NULL,
                external_id TEXT,
                status TEXT NOT NULL,
                requested_at INTEGER NOT NULL,
                started_at INTEGER,
                finished_at INTEGER,
                result_refs_json TEXT NOT NULL,
                failure_json TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS contest_policy (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                profile TEXT NOT NULL,
                contest_started_at INTEGER NOT NULL,
                contest_deadline_at INTEGER NOT NULL,
                content_freeze_at INTEGER NOT NULL,
                delivery_freeze_at INTEGER NOT NULL,
                delivery_reserve_seconds INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_decisions (
                gate TEXT PRIMARY KEY,
                decided_at INTEGER NOT NULL,
                decision_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_decision_requests (
                request_id TEXT PRIMARY KEY,
                gate_type TEXT NOT NULL,
                generation INTEGER NOT NULL,
                kind TEXT NOT NULL,
                action_type TEXT NOT NULL,
                requested_revision INTEGER NOT NULL,
                subject_fingerprint TEXT NOT NULL,
                options_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                request_json TEXT NOT NULL,
                UNIQUE(gate_type, generation)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_decision_instances (
                decision_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                outcome TEXT NOT NULL,
                approved INTEGER,
                selected_option_id TEXT,
                reason TEXT NOT NULL,
                evidence_manifest_sha256 TEXT NOT NULL,
                decided_by TEXT NOT NULL,
                decided_at INTEGER NOT NULL,
                decision_json TEXT NOT NULL,
                FOREIGN KEY(request_id) REFERENCES workflow_decision_requests(request_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS projector_snapshots (
                projector_name TEXT PRIMARY KEY,
                projector_version INTEGER NOT NULL,
                through_revision INTEGER NOT NULL,
                state_hash TEXT NOT NULL,
                through_event_id TEXT,
                through_event_payload_sha256 TEXT,
                source_chain_root_sha256 TEXT,
                snapshot_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS projection_failures (
                revision INTEGER NOT NULL,
                projector_name TEXT NOT NULL,
                error_type TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                resolved_at INTEGER,
                PRIMARY KEY(revision, projector_name)
            )
            """
        )
        projector_columns = {
            column[1]
            for column in connection.execute(
                "PRAGMA table_info(projector_snapshots)"
            ).fetchall()
        }
        for column in (
            "through_event_id",
            "through_event_payload_sha256",
            "source_chain_root_sha256",
        ):
            if column not in projector_columns:
                connection.execute(
                    f"ALTER TABLE projector_snapshots ADD COLUMN {column} TEXT"
                )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS stage_cursor_inputs (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                stage_id INTEGER NOT NULL,
                subtask TEXT NOT NULL,
                source_step_id INTEGER NOT NULL,
                input_fingerprint TEXT NOT NULL,
                baseline_json TEXT NOT NULL,
                selected_revision INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS stage_checkpoints (
                stage_id INTEGER NOT NULL,
                subtask TEXT NOT NULL,
                source_step_id INTEGER NOT NULL,
                completed_step_id INTEGER,
                input_fingerprint TEXT NOT NULL,
                output_fingerprint TEXT NOT NULL,
                completed_revision INTEGER NOT NULL,
                receipt_json TEXT NOT NULL,
                PRIMARY KEY(stage_id, subtask)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS stage_checkpoint_history (
                checkpoint_id TEXT PRIMARY KEY,
                stage_id INTEGER NOT NULL,
                subtask TEXT NOT NULL,
                source_step_id INTEGER NOT NULL,
                completed_step_id INTEGER,
                input_fingerprint TEXT NOT NULL,
                output_fingerprint TEXT NOT NULL,
                completed_revision INTEGER NOT NULL,
                receipt_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dirty_flags (
                flag TEXT PRIMARY KEY,
                owner_stage INTEGER NOT NULL,
                cause_revision INTEGER NOT NULL,
                cause_artifact TEXT NOT NULL,
                baseline_fingerprint TEXT NOT NULL,
                current_fingerprint TEXT NOT NULL,
                classifier_contract_sha256 TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dirty_causes (
                cause_id TEXT PRIMARY KEY,
                flag TEXT NOT NULL,
                owner_stage INTEGER NOT NULL,
                cause_revision INTEGER NOT NULL,
                cause_artifact TEXT NOT NULL,
                baseline_fingerprint TEXT NOT NULL,
                current_fingerprint TEXT NOT NULL,
                classifier_contract_sha256 TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dirty_flag_clear_receipts (
                revision INTEGER NOT NULL,
                flag TEXT NOT NULL,
                owner_stage INTEGER NOT NULL,
                cleared_fingerprint TEXT NOT NULL,
                classifier_contract_sha256 TEXT NOT NULL,
                receipt_json TEXT NOT NULL,
                PRIMARY KEY(revision, flag)
            )
            """
        )
        for checkpoint in connection.execute(
            "SELECT * FROM stage_checkpoints ORDER BY completed_revision, stage_id, subtask"
        ).fetchall():
            checkpoint_id = canonical_hash(
                {
                    "stage_id": int(checkpoint["stage_id"]),
                    "subtask": str(checkpoint["subtask"]),
                    "revision": int(checkpoint["completed_revision"]),
                    "input": str(checkpoint["input_fingerprint"]),
                    "output": str(checkpoint["output_fingerprint"]),
                }
            )[:32]
            connection.execute(
                """
                INSERT OR IGNORE INTO stage_checkpoint_history(
                    checkpoint_id, stage_id, subtask, source_step_id,
                    completed_step_id, input_fingerprint, output_fingerprint,
                    completed_revision, receipt_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_id,
                    checkpoint["stage_id"],
                    checkpoint["subtask"],
                    checkpoint["source_step_id"],
                    checkpoint["completed_step_id"],
                    checkpoint["input_fingerprint"],
                    checkpoint["output_fingerprint"],
                    checkpoint["completed_revision"],
                    checkpoint["receipt_json"],
                ),
            )
        for dirty in connection.execute(
            "SELECT * FROM dirty_flags ORDER BY cause_revision, flag"
        ).fetchall():
            cause_id = canonical_hash(
                {
                    "revision": int(dirty["cause_revision"]),
                    "flag": str(dirty["flag"]),
                    "owner_stage": int(dirty["owner_stage"]),
                    "artifact": str(dirty["cause_artifact"]),
                    "baseline": str(dirty["baseline_fingerprint"]),
                    "current": str(dirty["current_fingerprint"]),
                }
            )[:32]
            connection.execute(
                """
                INSERT OR IGNORE INTO dirty_causes(
                    cause_id, flag, owner_stage, cause_revision,
                    cause_artifact, baseline_fingerprint,
                    current_fingerprint, classifier_contract_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cause_id,
                    dirty["flag"],
                    dirty["owner_stage"],
                    dirty["cause_revision"],
                    dirty["cause_artifact"],
                    dirty["baseline_fingerprint"],
                    dirty["current_fingerprint"],
                    dirty["classifier_contract_sha256"],
                ),
            )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS workflow_decisions_append_only_update
            BEFORE UPDATE ON workflow_decisions
            BEGIN
                SELECT RAISE(ABORT, 'workflow decisions are append-only');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS workflow_decisions_append_only_delete
            BEFORE DELETE ON workflow_decisions
            BEGIN
                SELECT RAISE(ABORT, 'workflow decisions are append-only');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS workflow_decision_requests_immutable_identity
            BEFORE UPDATE OF request_id, gate_type, generation, kind, action_type,
                             requested_revision, subject_fingerprint,
                             options_fingerprint, created_at, request_json
            ON workflow_decision_requests
            BEGIN
                SELECT RAISE(ABORT, 'workflow decision requests have immutable identity');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS workflow_decision_requests_append_only_delete
            BEFORE DELETE ON workflow_decision_requests
            BEGIN
                SELECT RAISE(ABORT, 'workflow decision requests are append-only');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS workflow_decision_instances_append_only_update
            BEFORE UPDATE ON workflow_decision_instances
            BEGIN
                SELECT RAISE(ABORT, 'workflow decision instances are append-only');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS workflow_decision_instances_append_only_delete
            BEFORE DELETE ON workflow_decision_instances
            BEGIN
                SELECT RAISE(ABORT, 'workflow decision instances are append-only');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS dirty_flag_clear_receipts_append_only_update
            BEFORE UPDATE ON dirty_flag_clear_receipts
            BEGIN
                SELECT RAISE(ABORT, 'dirty clear receipts are append-only');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS dirty_flag_clear_receipts_append_only_delete
            BEFORE DELETE ON dirty_flag_clear_receipts
            BEGIN
                SELECT RAISE(ABORT, 'dirty clear receipts are append-only');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS stage_checkpoint_history_append_only_update
            BEFORE UPDATE ON stage_checkpoint_history
            BEGIN
                SELECT RAISE(ABORT, 'stage checkpoint history is append-only');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS stage_checkpoint_history_append_only_delete
            BEFORE DELETE ON stage_checkpoint_history
            BEGIN
                SELECT RAISE(ABORT, 'stage checkpoint history is append-only');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS dirty_causes_append_only_update
            BEFORE UPDATE ON dirty_causes
            BEGIN
                SELECT RAISE(ABORT, 'dirty causes are append-only');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS dirty_causes_append_only_delete
            BEFORE DELETE ON dirty_causes
            BEGIN
                SELECT RAISE(ABORT, 'dirty causes are append-only');
            END
            """
        )
        project_row = connection.execute(
            "SELECT project_id FROM project_state WHERE singleton=1"
        ).fetchone()
        project_id = str(project_row["project_id"]) if project_row is not None else "legacy"
        legacy_rows = connection.execute(
            "SELECT gate, decided_at, decision_json FROM workflow_decisions ORDER BY decided_at, gate"
        ).fetchall()
        for legacy in legacy_rows:
            gate = str(legacy["gate"])
            request_id = canonical_hash(
                {"project_id": project_id, "gate": gate, "generation": 1, "legacy": True}
            )[:24]
            try:
                decision = json.loads(legacy["decision_json"])
            except (TypeError, json.JSONDecodeError):
                decision = {"gate": gate, "legacy_payload_invalid": True}
            kind = str(decision.get("kind") or (
                "approval"
                if gate in {"content_freeze", "delivery_freeze_override"}
                else "selection"
            ))
            request_payload = {
                "request_id": request_id,
                "gate": gate,
                "generation": 1,
                "kind": kind,
                "type": "legacy_unbound",
                "requested_revision": 0,
                "subject_fingerprint": "LEGACY_UNBOUND",
                "options_fingerprint": "LEGACY_UNBOUND",
                "reason": {"code": "legacy_unbound", "message": "Migrated schema-v7 decision"},
                "evidence": [],
                "metadata": {"migration": "schema_v8"},
            }
            connection.execute(
                """
                INSERT OR IGNORE INTO workflow_decision_requests(
                    request_id, gate_type, generation, kind, action_type,
                    requested_revision, subject_fingerprint, options_fingerprint,
                    status, created_at, request_json
                ) VALUES (?, ?, 1, ?, 'legacy_unbound', 0, 'LEGACY_UNBOUND',
                          'LEGACY_UNBOUND', 'legacy_unbound', ?, ?)
                """,
                (
                    request_id,
                    gate,
                    kind,
                    int(legacy["decided_at"]),
                    json.dumps(request_payload, ensure_ascii=True, sort_keys=True),
                ),
            )
            selected = (
                decision.get("selected_option_id")
                or decision.get("selected_primary")
                or decision.get("selected")
            )
            approved = decision.get("approved")
            if kind == "approval" and not isinstance(approved, bool):
                normalized_selection = str(selected or "").lower()
                if normalized_selection.startswith(("approve", "allow", "override")):
                    approved = True
                elif normalized_selection.startswith(("reject", "deny")):
                    approved = False
                else:
                    approved = None
                # Never preserve a truthy legacy string such as "false" as an
                # approval. Current gates accept the boolean true only.
                if approved is None:
                    decision.pop("approved", None)
                else:
                    decision["approved"] = approved
            outcome = (
                "approved" if approved is True else "rejected" if approved is False else "selected"
            )
            decision_id = canonical_hash(
                {"request_id": request_id, "decision": decision}
            )[:32]
            connection.execute(
                """
                INSERT OR IGNORE INTO workflow_decision_instances(
                    decision_id, request_id, kind, outcome, approved,
                    selected_option_id, reason, evidence_manifest_sha256,
                    decided_by, decided_at, decision_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'LEGACY_UNBOUND', ?, ?, ?)
                """,
                (
                    decision_id,
                    request_id,
                    kind,
                    outcome,
                    None if approved is None else int(approved),
                    None if selected is None else str(selected),
                    str(decision.get("reason") or ""),
                    str(decision.get("selected_by") or decision.get("source") or "legacy"),
                    int(legacy["decided_at"]),
                    json.dumps(decision, ensure_ascii=True, sort_keys=True),
                ),
            )
        rows = connection.execute(
            "SELECT singleton, last_completed_step, scheduler_generation "
            "FROM project_state"
        ).fetchall()
        for state_row in rows:
            if state_row["scheduler_generation"] == STAGE_SCHEDULER_GENERATION:
                stage_version = STAGE_CATALOG_VERSION
            else:
                stage_version = None
            connection.execute(
                "UPDATE project_state SET last_completed_stage=?, "
                "stage_catalog_version=COALESCE(stage_catalog_version, ?) "
                "WHERE singleton=?",
                (
                    completed_stage_for_step(state_row["last_completed_step"]),
                    stage_version,
                    state_row["singleton"],
                ),
            )
        solver_columns = {
            column[1]
            for column in connection.execute("PRAGMA table_info(solver_jobs)").fetchall()
        }
        if "job_revision" not in solver_columns:
            connection.execute(
                "ALTER TABLE solver_jobs ADD COLUMN job_revision INTEGER NOT NULL DEFAULT 1"
            )
        solver_columns = {
            column[1]
            for column in connection.execute("PRAGMA table_info(solver_jobs)").fetchall()
        }
        for column, definition in (
            ("idempotency_key", "TEXT"),
            ("request_sha256", "TEXT"),
            ("owner_stage", "INTEGER"),
            ("owner_subtask", "TEXT"),
            ("owner_revision", "INTEGER"),
            ("attempt_id", "TEXT"),
        ):
            if column not in solver_columns:
                connection.execute(
                    f"ALTER TABLE solver_jobs ADD COLUMN {column} {definition}"
                )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS solver_jobs_idempotency_key_unique "
            "ON solver_jobs(idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        receipt_rows = connection.execute(
            "SELECT payload_json FROM events WHERE type='SOLVER_JOB_RECEIPT_SUBMITTED'"
        ).fetchall()
        for receipt_row in receipt_rows:
            try:
                receipt_payload = json.loads(receipt_row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            job_id = receipt_payload.get("job_id")
            request_sha256 = receipt_payload.get("request_sha256")
            if job_id and request_sha256:
                connection.execute(
                    "UPDATE solver_jobs SET request_sha256=COALESCE(request_sha256, ?) "
                    "WHERE job_id=?",
                    (str(request_sha256), str(job_id)),
                )
        connection.execute(
            "UPDATE project_state SET schema_version = ? WHERE singleton = 1",
            (SCHEMA_VERSION,),
        )
        connection.execute(
            "UPDATE schema_info SET schema_version = ? WHERE singleton = 1",
            (SCHEMA_VERSION,),
        )
        connection.commit()

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        try:
            row = connection.execute(
                "SELECT schema_version FROM schema_info WHERE singleton = 1"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            raise StateNotInitialized("workflow schema is not initialized") from exc
        if row is None:
            raise StateNotInitialized("workflow schema is not initialized")
        if row[0] != SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported workflow schema {row[0]}; expected {SCHEMA_VERSION}"
            )

    def _versioned_event_payload(
        self,
        connection: sqlite3.Connection,
        *,
        before: sqlite3.Row,
        after: sqlite3.Row,
        revision: int,
        event_type: str,
        created_at: int,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        prior_event = connection.execute(
            "SELECT payload_json FROM events ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        prior_versioned = False
        if prior_event is not None:
            try:
                prior_versioned = isinstance(
                    json.loads(prior_event["payload_json"]).get(ENVELOPE_KEY), dict
                )
            except (AttributeError, json.JSONDecodeError):
                prior_versioned = False
        safe_payload = dict(_redact(payload or {}))
        effect_hashes = self._domain_effect_hashes(connection)
        safe_payload["effect_hashes_after"] = effect_hashes
        safe_payload["aggregate_root_hash_after"] = canonical_hash(effect_hashes)
        return build_event_payload(
            project_id=str(before["project_id"]),
            revision=revision,
            event_type=event_type,
            created_at=created_at,
            payload=safe_payload,
            before=self._state_from_row(before),
            after=self._state_from_row(after),
            force_snapshot=not prior_versioned,
        )

    @staticmethod
    def _domain_effect_hashes(connection: sqlite3.Connection) -> dict[str, str]:
        def rows_hash(query: str) -> str:
            rows = connection.execute(query).fetchall()
            return canonical_hash([dict(row) for row in rows])

        return {
            "contest_policy": rows_hash(
                "SELECT * FROM contest_policy ORDER BY singleton"
            ),
            "project_config": rows_hash(
                "SELECT * FROM project_config ORDER BY singleton"
            ),
            "decision_requests": rows_hash(
                "SELECT * FROM workflow_decision_requests ORDER BY gate_type, generation"
            ),
            "decision_instances": rows_hash(
                "SELECT * FROM workflow_decision_instances ORDER BY request_id"
            ),
            "dirty_flags": rows_hash("SELECT * FROM dirty_flags ORDER BY flag"),
            "dirty_causes": rows_hash(
                "SELECT * FROM dirty_causes ORDER BY cause_revision, cause_id"
            ),
            "stage_checkpoints": rows_hash(
                "SELECT * FROM stage_checkpoints ORDER BY stage_id, subtask"
            ),
            "checkpoint_history": rows_hash(
                "SELECT * FROM stage_checkpoint_history ORDER BY completed_revision, checkpoint_id"
            ),
            "solver_jobs": rows_hash("SELECT * FROM solver_jobs ORDER BY job_id"),
            "dirty_clear_receipts": rows_hash(
                "SELECT * FROM dirty_flag_clear_receipts ORDER BY revision, flag"
            ),
        }

    def aggregate_domain_root(self) -> dict[str, Any]:
        if not self.path.is_file():
            raise StateNotInitialized(f"workflow state does not exist: {self.path}")
        with self._session() as connection:
            self._upgrade_schema(connection)
            effect_hashes = self._domain_effect_hashes(connection)
        return {
            "effect_hashes": effect_hashes,
            "aggregate_root_hash": canonical_hash(effect_hashes),
        }

    def verify_aggregate_domain_root(self) -> bool:
        events = self.events()
        expected = None
        expected_effects: dict[str, str] | None = None
        for event in reversed(events):
            envelope = event.payload.get(ENVELOPE_KEY)
            if isinstance(envelope, dict) and envelope.get("aggregate_root_hash_after"):
                expected = str(envelope["aggregate_root_hash_after"])
                raw_effects = envelope.get("effect_hashes_after")
                if isinstance(raw_effects, dict):
                    expected_effects = {
                        str(key): str(value) for key, value in raw_effects.items()
                    }
                break
        if expected is None:
            return True
        current = self.aggregate_domain_root()
        current_effects = current["effect_hashes"]
        if expected_effects is not None and set(expected_effects) != set(current_effects):
            # Schema-v8 events created before policy/config entered the aggregate
            # root remain verifiable for every domain they originally covered.
            return all(
                current_effects.get(key) == value
                for key, value in expected_effects.items()
            )
        return current["aggregate_root_hash"] == expected

    def initialize(
        self,
        *,
        project_id: str,
        project_type: str,
        last_completed_step: int = -1,
        status: WorkflowStatus = WorkflowStatus.READY,
        active_step: int | None = None,
        pending_action: dict[str, Any] | None = None,
        imported: bool = False,
        import_payload: dict[str, Any] | None = None,
        runtime_generation: str = "native_v2",
        scheduler_generation: str = STEP_SCHEDULER_GENERATION,
        stage_catalog_version: str | None = None,
        contest_policy: dict[str, Any] | None = None,
    ) -> WorkflowState:
        if scheduler_generation not in {
            STEP_SCHEDULER_GENERATION,
            STAGE_SCHEDULER_GENERATION,
        }:
            raise ValueError(
                f"unsupported scheduler generation: {scheduler_generation}"
            )
        if (
            scheduler_generation == STAGE_SCHEDULER_GENERATION
            and runtime_generation != "native_v2"
        ):
            raise ValueError("Stage scheduling requires the native_v2 runtime")
        if scheduler_generation == STAGE_SCHEDULER_GENERATION:
            stage_catalog_version = stage_catalog_version or STAGE_CATALOG_VERSION
        elif stage_catalog_version is not None:
            raise ValueError("Step scheduler projects cannot own a Stage catalog version")
        now = int(self._clock())
        scope = self.project_dir.parent.name if self.project_dir.parent.name in {"ongoing", "complete"} else "external"
        with self._session() as connection:
            self._create_schema(connection)
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT 1 FROM project_state WHERE singleton = 1").fetchone()
            if existing:
                return self._state_from_row(
                    connection.execute("SELECT * FROM project_state WHERE singleton = 1").fetchone()
                )
            connection.execute(
                """
                INSERT INTO project_state(
                    singleton, schema_version, project_id, project_type, control_mode,
                    runtime_generation, scheduler_generation, stage_catalog_version,
                    status, last_completed_step, active_step, last_completed_stage,
                    active_stage, active_subtask, source_step_id, attempt, revision,
                    pending_action_json, runner_pid, runner_lease_id, heartbeat_at,
                    storage_scope, created_at, updated_at, last_event_at
                ) VALUES (
                    1, ?, ?, ?, 'engine', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1,
                    ?, NULL, NULL, NULL, ?, ?, ?, ?
                )
                """,
                (
                    SCHEMA_VERSION,
                    project_id,
                    project_type,
                    runtime_generation,
                    scheduler_generation,
                    stage_catalog_version,
                    status.value,
                    last_completed_step,
                    active_step,
                    completed_stage_for_step(last_completed_step),
                    None,
                    None,
                    active_step,
                    json.dumps(_redact(pending_action), ensure_ascii=True, sort_keys=True)
                    if pending_action is not None
                    else None,
                    scope,
                    now,
                    now,
                    now,
                ),
            )
            if scheduler_generation == STAGE_SCHEDULER_GENERATION:
                for checkpoint in initial_stage_checkpoints(last_completed_step):
                    receipt = {
                        "schema_version": "factory-stage-checkpoint-v1",
                        "source": "compatibility_cursor_seed",
                        **checkpoint,
                    }
                    connection.execute(
                        """
                        INSERT INTO stage_checkpoints(
                            stage_id, subtask, source_step_id, completed_step_id,
                            input_fingerprint, output_fingerprint,
                            completed_revision, receipt_json
                        ) VALUES (?, ?, ?, ?, 'MIGRATION_SEED', 'MIGRATION_SEED', 1, ?)
                        """,
                        (
                            checkpoint["stage_id"],
                            checkpoint["subtask"],
                            checkpoint["source_step_id"],
                            checkpoint["completed_step_id"],
                            json.dumps(receipt, ensure_ascii=True, sort_keys=True),
                        ),
                    )
                    checkpoint_id = canonical_hash(
                        {
                            "stage_id": int(checkpoint["stage_id"]),
                            "subtask": str(checkpoint["subtask"]),
                            "revision": 1,
                            "input": "MIGRATION_SEED",
                            "output": "MIGRATION_SEED",
                        }
                    )[:32]
                    connection.execute(
                        """
                        INSERT INTO stage_checkpoint_history(
                            checkpoint_id, stage_id, subtask, source_step_id,
                            completed_step_id, input_fingerprint,
                            output_fingerprint, completed_revision, receipt_json
                        ) VALUES (?, ?, ?, ?, ?, 'MIGRATION_SEED',
                                  'MIGRATION_SEED', 1, ?)
                        """,
                        (
                            checkpoint_id,
                            checkpoint["stage_id"],
                            checkpoint["subtask"],
                            checkpoint["source_step_id"],
                            checkpoint["completed_step_id"],
                            json.dumps(receipt, ensure_ascii=True, sort_keys=True),
                        ),
                    )
            if contest_policy is not None:
                connection.execute(
                    """
                    INSERT INTO contest_policy(
                        singleton, profile, contest_started_at, contest_deadline_at,
                        content_freeze_at, delivery_freeze_at, delivery_reserve_seconds
                    ) VALUES (1, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(contest_policy["profile"]),
                        int(contest_policy["contest_started_at"]),
                        int(contest_policy["contest_deadline_at"]),
                        int(contest_policy["content_freeze_at"]),
                        int(contest_policy["delivery_freeze_at"]),
                        int(contest_policy["delivery_reserve_seconds"]),
                    ),
                )
            event_type = "PROJECT_IMPORTED" if imported else "PROJECT_CREATED"
            payload = _redact(import_payload or {})
            effect_hashes = self._domain_effect_hashes(connection)
            payload["effect_hashes_after"] = effect_hashes
            payload["aggregate_root_hash_after"] = canonical_hash(effect_hashes)
            row = connection.execute(
                "SELECT * FROM project_state WHERE singleton = 1"
            ).fetchone()
            payload = build_event_payload(
                project_id=project_id,
                revision=1,
                event_type=event_type,
                created_at=now,
                payload=payload,
                before=None,
                after=self._state_from_row(row),
                force_snapshot=True,
            )
            connection.execute(
                """INSERT INTO events(
                       revision, type, created_at, step, attempt, payload_json
                   ) VALUES (1, ?, ?, NULL, 0, ?)""",
                (event_type, now, json.dumps(payload, ensure_ascii=True, sort_keys=True)),
            )
            row = connection.execute("SELECT * FROM project_state WHERE singleton = 1").fetchone()
        return self._state_from_row(row)

    def load(self) -> WorkflowState:
        if not self.path.is_file():
            raise StateNotInitialized(f"workflow state does not exist: {self.path}")
        with self._session() as connection:
            self._upgrade_schema(connection)
            self._validate_schema(connection)
            row = connection.execute("SELECT * FROM project_state WHERE singleton = 1").fetchone()
        if row is None:
            raise StateNotInitialized(f"workflow state is not initialized: {self.path}")
        return self._state_from_row(row)

    def now_epoch(self) -> int:
        return int(self._clock())

    def contest_policy(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        with self._session() as connection:
            self._upgrade_schema(connection)
            row = connection.execute(
                "SELECT * FROM contest_policy WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return None
        return {
            "profile": row["profile"],
            "contest_started_at": row["contest_started_at"],
            "contest_deadline_at": row["contest_deadline_at"],
            "content_freeze_at": row["content_freeze_at"],
            "delivery_freeze_at": row["delivery_freeze_at"],
            "delivery_reserve_seconds": row["delivery_reserve_seconds"],
        }

    @staticmethod
    def _insert_decision_request(
        connection: sqlite3.Connection,
        request: dict[str, Any],
        *,
        created_at: int,
    ) -> None:
        required = {
            "request_id",
            "gate",
            "generation",
            "kind",
            "type",
            "requested_revision",
            "subject_fingerprint",
            "options_fingerprint",
        }
        missing = sorted(key for key in required if request.get(key) in {None, ""})
        if missing:
            raise InvalidTransition(
                f"human decision request is missing fields: {', '.join(missing)}"
            )
        encoded = json.dumps(_redact(request), ensure_ascii=True, sort_keys=True)
        prior = connection.execute(
            "SELECT request_json FROM workflow_decision_requests WHERE request_id=?",
            (str(request["request_id"]),),
        ).fetchone()
        if prior is not None:
            if prior["request_json"] != encoded:
                raise InvalidTransition("human decision request id was reused")
            return
        connection.execute(
            """
            INSERT INTO workflow_decision_requests(
                request_id, gate_type, generation, kind, action_type,
                requested_revision, subject_fingerprint, options_fingerprint,
                status, created_at, request_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (
                str(request["request_id"]),
                str(request["gate"]),
                int(request["generation"]),
                str(request["kind"]),
                str(request["type"]),
                int(request["requested_revision"]),
                str(request["subject_fingerprint"]),
                str(request["options_fingerprint"]),
                int(created_at),
                encoded,
            ),
        )

    @staticmethod
    def _decision_payload(row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["decision_json"])
        payload.update(
            request_id=row["request_id"],
            decision_id=row["decision_id"],
            generation=row["generation"],
            gate=row["gate_type"],
            kind=row["kind"],
            outcome=row["outcome"],
            subject_fingerprint=row["subject_fingerprint"],
            options_fingerprint=row["options_fingerprint"],
        )
        if row["approved"] is not None:
            payload["approved"] = bool(row["approved"])
        return payload

    @staticmethod
    def _latest_decision_row(
        connection: sqlite3.Connection, gate: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT r.request_id, r.gate_type, r.generation, r.kind,
                   r.subject_fingerprint, r.options_fingerprint, r.request_json,
                   d.decision_id, d.outcome, d.approved, d.decision_json
            FROM workflow_decision_requests AS r
            LEFT JOIN workflow_decision_instances AS d ON d.request_id=r.request_id
            WHERE r.gate_type=?
            ORDER BY r.generation DESC
            LIMIT 1
            """,
            (gate,),
        ).fetchone()

    def next_decision_generation(self, gate: str) -> int:
        if not self.path.is_file():
            return 1
        with self._session() as connection:
            self._upgrade_schema(connection)
            row = connection.execute(
                "SELECT COALESCE(MAX(generation), 0) AS generation "
                "FROM workflow_decision_requests WHERE gate_type=?",
                (str(gate),),
            ).fetchone()
        return int(row["generation"]) + 1

    def decision_requests(self, gate: str | None = None) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        with self._session() as connection:
            self._upgrade_schema(connection)
            if gate is None:
                rows = connection.execute(
                    "SELECT * FROM workflow_decision_requests ORDER BY created_at, gate_type, generation"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM workflow_decision_requests WHERE gate_type=? "
                    "ORDER BY generation",
                    (str(gate),),
                ).fetchall()
        return [
            {
                **json.loads(row["request_json"]),
                "status": row["status"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def decision_history(self, gate: str | None = None) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        query = """
            SELECT r.request_id, r.gate_type, r.generation, r.kind,
                   r.subject_fingerprint, r.options_fingerprint,
                   d.decision_id, d.outcome, d.approved, d.decision_json
            FROM workflow_decision_requests AS r
            JOIN workflow_decision_instances AS d ON d.request_id=r.request_id
        """
        params: tuple[Any, ...] = ()
        if gate is not None:
            query += " WHERE r.gate_type=?"
            params = (str(gate),)
        query += " ORDER BY r.gate_type, r.generation"
        with self._session() as connection:
            self._upgrade_schema(connection)
            rows = connection.execute(query, params).fetchall()
        return [
            self._with_decision_receipt_verification(self._decision_payload(row))
            for row in rows
        ]

    def _with_decision_receipt_verification(
        self, decision: dict[str, Any]
    ) -> dict[str, Any]:
        from .decision_receipts import verify_decision_receipt

        verification = verify_decision_receipt(self.project_dir, decision)
        return {**decision, "receipt_verification": verification.to_dict()}

    def decision_for_request(self, request_id: str) -> dict[str, Any] | None:
        """Return immutable history by request identity, independent of Gate currency."""

        if not self.path.is_file() or not str(request_id).strip():
            return None
        with self._session() as connection:
            self._upgrade_schema(connection)
            row = connection.execute(
                """
                SELECT r.request_id, r.gate_type, r.generation, r.kind,
                       r.subject_fingerprint, r.options_fingerprint,
                       d.decision_id, d.outcome, d.approved, d.decision_json
                FROM workflow_decision_requests AS r
                JOIN workflow_decision_instances AS d ON d.request_id=r.request_id
                WHERE r.request_id=?
                """,
                (str(request_id),),
            ).fetchone()
        return (
            self._with_decision_receipt_verification(self._decision_payload(row))
            if row is not None
            else None
        )

    def decision(self, gate: str, *, current_only: bool = True) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        gate = str(gate)
        with self._session() as connection:
            self._upgrade_schema(connection)
            row = self._latest_decision_row(connection, gate)
        if row is None or row["decision_id"] is None:
            return None
        if current_only and row["subject_fingerprint"] != "LEGACY_UNBOUND":
            from .human_decisions import decision_fingerprints

            request = json.loads(row["request_json"])
            current_subject, current_options = decision_fingerprints(
                self.project_dir,
                gate,
                tuple(request.get("evidence") or ()),
            )
            if (
                current_subject != row["subject_fingerprint"]
                or current_options != row["options_fingerprint"]
            ):
                return None
        payload = self._with_decision_receipt_verification(
            self._decision_payload(row)
        )
        if not payload["receipt_verification"]["valid"]:
            return None
        return payload

    def repair_decision_receipt(self, request_id: str) -> dict[str, Any]:
        """Recreate a missing receipt only when bytes match the persisted hash."""

        import hashlib

        request_id = str(request_id).strip()
        if not request_id or not self.path.is_file():
            raise InvalidTransition("decision request does not exist")
        with self._session() as connection:
            self._upgrade_schema(connection)
            row = connection.execute(
                """
                SELECT r.request_id, r.gate_type, r.generation, r.kind,
                       r.subject_fingerprint, r.options_fingerprint,
                       r.request_json, d.decision_id, d.outcome, d.approved,
                       d.decided_at, d.decision_json
                FROM workflow_decision_requests AS r
                JOIN workflow_decision_instances AS d ON d.request_id=r.request_id
                WHERE r.request_id=?
                """,
                (request_id,),
            ).fetchone()
        if row is None:
            raise InvalidTransition("resolved decision request does not exist")
        decision_json = json.loads(row["decision_json"])
        references = decision_json.get("artifact_refs") or []
        if len(references) != 1 or not isinstance(references[0], dict):
            raise InvalidTransition("decision has no unique immutable receipt reference")
        reference = references[0]
        receipt = {
            "schema_version": "factory-human-decision-receipt-v1",
            "decision_id": str(row["decision_id"]),
            "request_id": str(row["request_id"]),
            "gate": str(row["gate_type"]),
            "generation": int(row["generation"]),
            "kind": str(row["kind"]),
            "outcome": str(row["outcome"]),
            "approved": (
                None if row["approved"] is None else bool(row["approved"])
            ),
            "decided_at": int(row["decided_at"]),
            "subject_fingerprint": str(row["subject_fingerprint"]),
            "options_fingerprint": str(row["options_fingerprint"]),
            "decision": self._decision_identity_payload(decision_json),
            "projection_refs": list(decision_json.get("projection_refs") or ()),
        }
        encoded = (
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        if reference.get("size") != len(encoded) or reference.get(
            "sha256"
        ) != hashlib.sha256(encoded).hexdigest():
            raise InvalidTransition(
                "persisted decision cannot reproduce the original receipt hash"
            )
        relative = reference.get("path")
        expected_gate = re.sub(r"[^A-Za-z0-9._-]", "_", str(row["gate_type"]))
        expected = Path(
            ".factory",
            "decisions",
            expected_gate,
            request_id,
            f"{row['decision_id']}.json",
        )
        if (
            not isinstance(relative, str)
            or Path(relative).is_absolute()
            or Path(relative) != expected
            or ".." in Path(relative).parts
        ):
            raise InvalidTransition("decision receipt path does not match identity")
        target = self.project_dir / expected
        if target.exists() or target.is_symlink():
            from .decision_receipts import verify_decision_receipt

            verification = verify_decision_receipt(
                self.project_dir, self._decision_payload(row)
            )
            if verification.valid:
                return {"status": "already_valid", **verification.to_dict()}
            raise InvalidTransition(
                "receipt path already exists but is invalid; refusing to overwrite evidence"
            )
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
        directory_descriptor = os.open(self.project_dir, directory_flags)
        try:
            for component in expected.parts[:-1]:
                try:
                    next_descriptor = os.open(
                        component, directory_flags, dir_fd=directory_descriptor
                    )
                except FileNotFoundError:
                    os.mkdir(component, 0o700, dir_fd=directory_descriptor)
                    next_descriptor = os.open(
                        component, directory_flags, dir_fd=directory_descriptor
                    )
                os.close(directory_descriptor)
                directory_descriptor = next_descriptor
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
            descriptor = os.open(
                expected.name, flags, 0o600, dir_fd=directory_descriptor
            )
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                os.unlink(expected.name, dir_fd=directory_descriptor)
                raise
        except BaseException:
            raise
        finally:
            os.close(directory_descriptor)
        from .decision_receipts import verify_decision_receipt

        verification = verify_decision_receipt(
            self.project_dir, self._decision_payload(row)
        )
        if not verification.valid:
            raise InvalidTransition(
                "reconstructed receipt did not pass immutable verification"
            )
        return {"status": "repaired", **verification.to_dict()}

    def assert_pending_decision_current(self, gate: str | None = None) -> dict[str, Any]:
        state = self.load()
        pending = state.pending_action or {}
        pending_gate = str(pending.get("gate") or "")
        if gate and pending_gate != str(gate):
            raise InvalidTransition(
                f"project is awaiting {pending_gate or 'no gate'}, not {gate}"
            )
        request = (pending.get("metadata") or {}).get("human_decision") or {}
        if not request:
            raise InvalidTransition("pending action has no decision request identity")
        from .human_decisions import decision_fingerprints

        subject, options = decision_fingerprints(
            self.project_dir,
            pending_gate,
            tuple(request.get("evidence") or ()),
        )
        if subject != request.get("subject_fingerprint") or options != request.get(
            "options_fingerprint"
        ):
            raise InvalidTransition(
                "human decision request is stale because its bound evidence changed"
            )
        return dict(request)

    def supersede_pending_decision_request(
        self,
        *,
        expected_revision: int,
        gate: str | None = None,
        reason: str = "Rebind the pending request to current project evidence",
    ) -> WorkflowState:
        """Atomically replace one stale open request with a fresh generation."""

        from .human_decisions import build_decision_request

        now = int(self._clock())
        with self._session() as connection:
            self._upgrade_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            before = connection.execute(
                "SELECT * FROM project_state WHERE singleton=1"
            ).fetchone()
            if before is None:
                raise StateNotInitialized(
                    f"workflow state is not initialized: {self.path}"
                )
            if int(before["revision"]) != int(expected_revision):
                raise RevisionConflict(
                    f"expected revision {expected_revision}, found {before['revision']}"
                )
            pending = (
                json.loads(before["pending_action_json"])
                if before["pending_action_json"]
                else None
            )
            if pending is None:
                raise InvalidTransition("project has no pending human decision")
            pending_gate = str(pending.get("gate") or "")
            if gate and pending_gate != str(gate):
                raise InvalidTransition(
                    f"project is awaiting {pending_gate or 'no gate'}, not {gate}"
                )
            old_request = (
                (pending.get("metadata") or {}).get("human_decision") or {}
            )
            old_request_id = str(old_request.get("request_id") or "")
            if not old_request_id:
                raise InvalidTransition(
                    "pending action has no decision request identity"
                )
            old_row = connection.execute(
                "SELECT * FROM workflow_decision_requests WHERE request_id=?",
                (old_request_id,),
            ).fetchone()
            if old_row is None:
                raise InvalidTransition("pending human decision request is not registered")
            decided = connection.execute(
                "SELECT 1 FROM workflow_decision_instances WHERE request_id=?",
                (old_request_id,),
            ).fetchone()
            if decided is not None:
                raise InvalidTransition("a resolved decision request cannot be superseded")
            from .human_decisions import decision_fingerprints

            current_subject, current_options = decision_fingerprints(
                self.project_dir,
                pending_gate,
                tuple(old_request.get("evidence") or ()),
            )
            if (
                current_subject == str(old_row["subject_fingerprint"])
                and current_options == str(old_row["options_fingerprint"])
            ):
                raise InvalidTransition(
                    "pending human decision request is still current and cannot be superseded"
                )
            generation_row = connection.execute(
                "SELECT COALESCE(MAX(generation), 0) AS generation "
                "FROM workflow_decision_requests WHERE gate_type=?",
                (pending_gate,),
            ).fetchone()
            fresh = build_decision_request(
                project_id=str(before["project_id"]),
                project_dir=self.project_dir,
                requested_revision=int(expected_revision) + 1,
                generation=int(generation_row["generation"]) + 1,
                action=pending,
                reason={
                    "code": "request_superseded",
                    "message": str(reason),
                    "evidence": list(old_request.get("evidence") or ()),
                },
                evidence=tuple(old_request.get("evidence") or ()),
            ).to_dict()
            self._insert_decision_request(connection, fresh, created_at=now)
            connection.execute(
                "UPDATE workflow_decision_requests SET status='superseded' "
                "WHERE request_id=? AND status='open'",
                (old_request_id,),
            )
            pending_metadata = dict(pending.get("metadata") or {})
            pending_metadata["human_decision"] = fresh
            rebound_pending = {**pending, "metadata": pending_metadata}
            revision = int(expected_revision) + 1
            connection.execute(
                "UPDATE project_state SET pending_action_json=?, revision=?, "
                "updated_at=?, last_event_at=? WHERE singleton=1",
                (
                    json.dumps(rebound_pending, ensure_ascii=True, sort_keys=True),
                    revision,
                    now,
                    now,
                ),
            )
            after = connection.execute(
                "SELECT * FROM project_state WHERE singleton=1"
            ).fetchone()
            event_payload = self._versioned_event_payload(
                connection,
                before=before,
                after=after,
                revision=revision,
                event_type="HUMAN_DECISION_REQUEST_SUPERSEDED",
                created_at=now,
                payload={
                    "gate": pending_gate,
                    "superseded_request_id": old_request_id,
                    "request_id": fresh["request_id"],
                    "generation": fresh["generation"],
                    "action": fresh,
                    "reason": str(reason),
                },
            )
            connection.execute(
                "INSERT INTO events(revision, type, created_at, step, attempt, payload_json) "
                "VALUES (?, 'HUMAN_DECISION_REQUEST_SUPERSEDED', ?, ?, ?, ?)",
                (
                    revision,
                    now,
                    before["active_step"],
                    before["attempt"],
                    json.dumps(event_payload, ensure_ascii=True, sort_keys=True),
                ),
            )
        return self._state_from_row(after)

    @staticmethod
    def _decision_identity_payload(decision: dict[str, Any]) -> dict[str, Any]:
        """Return human intent without mutable projections or derived identity."""

        excluded = {
            "artifact_refs",
            "projection_refs",
            "decision_id",
            "outcome",
        }
        return {
            key: value for key, value in decision.items() if key not in excluded
        }

    def _materialize_decision_receipt(
        self,
        *,
        request: sqlite3.Row,
        decision: dict[str, Any],
        decision_id: str,
        outcome: str,
        approved: bool | None,
        decided_at: int,
    ) -> dict[str, Any]:
        """Write one immutable receipt and make it the decision's sole evidence ref."""

        from .artifacts import artifact_ref, atomic_write_text

        safe = dict(decision)
        projection_refs = list(safe.pop("artifact_refs", ()) or ())
        projection_refs.extend(list(safe.pop("projection_refs", ()) or ()))
        identity = self._decision_identity_payload(safe)
        receipt = {
            "schema_version": "factory-human-decision-receipt-v1",
            "decision_id": decision_id,
            "request_id": str(request["request_id"]),
            "gate": str(request["gate_type"]),
            "generation": int(request["generation"]),
            "kind": str(request["kind"]),
            "outcome": outcome,
            "approved": approved,
            "decided_at": int(decided_at),
            "subject_fingerprint": str(request["subject_fingerprint"]),
            "options_fingerprint": str(request["options_fingerprint"]),
            "decision": identity,
            "projection_refs": projection_refs,
        }
        gate_component = re.sub(
            r"[^A-Za-z0-9._-]", "_", str(request["gate_type"])
        )
        receipt_path = (
            self.project_dir
            / ".factory"
            / "decisions"
            / gate_component
            / str(request["request_id"])
            / f"{decision_id}.json"
        )
        try:
            receipt_path.resolve(strict=False).relative_to(self.project_dir)
        except ValueError as exc:  # pragma: no cover - components are sanitized
            raise InvalidTransition("decision receipt escaped the project") from exc
        encoded_receipt = (
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        if receipt_path.exists():
            if receipt_path.is_symlink() or not receipt_path.is_file():
                raise InvalidTransition("decision receipt path is not a regular file")
            if receipt_path.read_text(encoding="utf-8") != encoded_receipt:
                raise InvalidTransition("immutable decision receipt collision")
        else:
            atomic_write_text(receipt_path, encoded_receipt)
        safe["decision_id"] = decision_id
        if projection_refs:
            safe["projection_refs"] = projection_refs
        safe["artifact_refs"] = [artifact_ref(self.project_dir, receipt_path)]
        return safe

    def _insert_decision_instance(
        self,
        connection: sqlite3.Connection,
        *,
        request: sqlite3.Row,
        decision: dict[str, Any],
        decided_at: int,
    ) -> dict[str, Any]:
        safe = dict(_redact(decision))
        request_id = str(request["request_id"])
        safe.update(
            gate=str(request["gate_type"]),
            request_id=request_id,
            generation=int(request["generation"]),
            kind=str(request["kind"]),
            subject_fingerprint=str(request["subject_fingerprint"]),
            options_fingerprint=str(request["options_fingerprint"]),
        )
        selected = (
            safe.get("selected_option_id")
            or safe.get("selected_primary")
            or safe.get("selected")
        )
        approved = safe.get("approved")
        if request["kind"] == "approval" and not isinstance(approved, bool):
            normalized = str(selected or "").lower()
            if normalized.startswith(("approve", "allow", "override")):
                approved = True
            elif normalized.startswith(("reject", "deny")):
                approved = False
            else:
                raise InvalidTransition("approval decisions require approved=true or false")
            safe["approved"] = approved
        outcome = (
            "approved"
            if approved is True
            else "rejected"
            if approved is False
            else "answered"
            if request["kind"] == "consultation"
            else "selected"
        )
        prior = connection.execute(
            "SELECT decision_id, outcome, decision_json FROM workflow_decision_instances WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if prior is not None:
            prior_payload = json.loads(prior["decision_json"])
            for keys in (
                ("selected_option_id", "selected_primary", "selected"),
                ("approved",),
                ("answer", "response"),
            ):
                prior_value = next(
                    (prior_payload.get(key) for key in keys if prior_payload.get(key) is not None),
                    None,
                )
                current_value = next(
                    (safe.get(key) for key in keys if safe.get(key) is not None),
                    None,
                )
                if (
                    prior_value is not None
                    and current_value is not None
                    and prior_value != current_value
                ):
                    raise InvalidTransition(
                        "immutable decision already exists for this request"
                    )
            return {
                **prior_payload,
                "decision_id": prior["decision_id"],
                "outcome": prior["outcome"],
            }
        decision_id = canonical_hash(
            {
                "request_id": request_id,
                "decision": self._decision_identity_payload(safe),
                "decided_at": decided_at,
            }
        )[:32]
        supplied_decision_id = str(safe.get("decision_id") or "")
        if supplied_decision_id and supplied_decision_id != decision_id:
            raise InvalidTransition("decision id does not match immutable decision intent")
        safe = self._materialize_decision_receipt(
            request=request,
            decision=safe,
            decision_id=decision_id,
            outcome=outcome,
            approved=approved if isinstance(approved, bool) else None,
            decided_at=decided_at,
        )
        refs = safe.get("artifact_refs") or ()
        evidence_sha256 = canonical_hash(refs)
        encoded = json.dumps(safe, ensure_ascii=True, sort_keys=True)
        connection.execute(
            """
            INSERT INTO workflow_decision_instances(
                decision_id, request_id, kind, outcome, approved,
                selected_option_id, reason, evidence_manifest_sha256,
                decided_by, decided_at, decision_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                request_id,
                str(request["kind"]),
                outcome,
                None if approved is None else int(approved),
                None if selected is None else str(selected),
                str(safe.get("reason") or ""),
                evidence_sha256,
                str(safe.get("selected_by") or safe.get("source") or "unknown"),
                int(decided_at),
                encoded,
            ),
        )
        connection.execute(
            "UPDATE workflow_decision_requests SET status=? WHERE request_id=?",
            ("rejected" if approved is False else "resolved", request_id),
        )
        return {**safe, "decision_id": decision_id, "outcome": outcome}

    def record_decision(self, gate: str, decision: dict[str, Any]) -> dict[str, Any]:
        gate = str(gate).strip()
        if not gate:
            raise ValueError("decision gate is required")
        safe = dict(_redact(decision))
        now = int(safe.get("selected_at") or safe.get("decided_epoch") or self._clock())
        if self.path.is_file():
            current_state = self.load()
            if str((current_state.pending_action or {}).get("gate") or "") == gate:
                self.resolve_human_decision(
                    expected_revision=current_state.revision,
                    resolution=safe,
                    decision_record=safe,
                )
                history = self.decision_history(gate)
                if not history:  # pragma: no cover - the transaction guarantees this
                    raise InvalidTransition("decision was not persisted")
                return history[-1]
        with self._session() as connection:
            self._upgrade_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT * FROM project_state WHERE singleton=1"
            ).fetchone()
            pending = (
                json.loads(state["pending_action_json"])
                if state is not None and state["pending_action_json"]
                else {}
            )
            pending_request = (pending.get("metadata") or {}).get("human_decision") or {}
            request_id = (
                str(pending_request.get("request_id") or "")
                if str(pending.get("gate") or "") == gate
                else ""
            )
            request = (
                connection.execute(
                    "SELECT * FROM workflow_decision_requests WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                if request_id
                else None
            )
            latest = self._latest_decision_row(connection, gate)
            if request is None and latest is not None and latest["decision_id"] is not None:
                prior = self._decision_payload(latest)
                if all(prior.get(key) == value for key, value in safe.items()):
                    return prior
            if request is None:
                from .human_decisions import build_decision_request

                generation_row = connection.execute(
                    "SELECT COALESCE(MAX(generation), 0) AS generation "
                    "FROM workflow_decision_requests WHERE gate_type=?",
                    (gate,),
                ).fetchone()
                built = build_decision_request(
                    project_id=str(state["project_id"] if state is not None else self.project_dir.name),
                    project_dir=self.project_dir,
                    requested_revision=int(state["revision"] if state is not None else 0),
                    generation=int(generation_row["generation"]) + 1,
                    action={"type": f"{gate}_decision", "gate": gate},
                    reason=str(safe.get("reason") or "direct decision record"),
                    evidence=tuple(safe.get("candidate_evidence") or ()),
                ).to_dict()
                self._insert_decision_request(connection, built, created_at=now)
                request = connection.execute(
                    "SELECT * FROM workflow_decision_requests WHERE request_id=?",
                    (built["request_id"],),
                ).fetchone()
            from .human_decisions import decision_fingerprints

            current_subject, current_options = decision_fingerprints(
                self.project_dir,
                gate,
                tuple(json.loads(request["request_json"]).get("evidence") or ()),
            )
            if request["subject_fingerprint"] != "LEGACY_UNBOUND" and (
                current_subject != request["subject_fingerprint"]
                or current_options != request["options_fingerprint"]
            ):
                raise InvalidTransition(
                    "human decision request is stale because its bound evidence changed"
                )
            persisted = self._insert_decision_instance(
                connection, request=request, decision=safe, decided_at=now
            )
            if state is not None:
                revision = int(state["revision"]) + 1
                connection.execute(
                    "UPDATE project_state SET revision=?, updated_at=?, last_event_at=? "
                    "WHERE singleton=1",
                    (revision, now, now),
                )
                after = connection.execute(
                    "SELECT * FROM project_state WHERE singleton=1"
                ).fetchone()
                event_payload = self._versioned_event_payload(
                    connection,
                    before=state,
                    after=after,
                    revision=revision,
                    event_type="HUMAN_DECISION_RECORDED",
                    created_at=now,
                    payload={
                        "gate": gate,
                        "request_id": persisted.get("request_id"),
                        "decision_id": persisted.get("decision_id"),
                        "generation": persisted.get("generation"),
                        "resolution": persisted,
                        "decision_recorded": True,
                        "artifact_refs": list(persisted.get("artifact_refs") or ()),
                    },
                )
                connection.execute(
                    "INSERT INTO events(revision, type, created_at, step, attempt, payload_json) "
                    "VALUES (?, 'HUMAN_DECISION_RECORDED', ?, ?, ?, ?)",
                    (
                        revision,
                        now,
                        state["active_step"],
                        state["attempt"],
                        json.dumps(event_payload, ensure_ascii=True, sort_keys=True),
                    ),
                )
            return persisted

    def resolve_human_decision(
        self,
        *,
        expected_revision: int,
        resolution: dict[str, Any],
        decision_record: dict[str, Any] | None = None,
    ) -> WorkflowState:
        """Atomically record one immutable decision instance and advance its request."""

        now = int(self._clock())
        with self._session() as connection:
            self._upgrade_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            before = connection.execute(
                "SELECT * FROM project_state WHERE singleton=1"
            ).fetchone()
            if before is None:
                raise StateNotInitialized(
                    f"workflow state is not initialized: {self.path}"
                )
            if int(before["revision"]) != expected_revision:
                raise RevisionConflict(
                    f"expected revision {expected_revision}, found {before['revision']}"
                )
            pending = (
                json.loads(before["pending_action_json"])
                if before["pending_action_json"]
                else None
            )
            if pending is None:
                raise InvalidTransition("project has no pending human decision")
            gate = str(resolution.get("gate") or pending.get("gate") or "").strip()
            if not gate:
                raise InvalidTransition("durable human decisions require a gate")
            request_payload = (pending.get("metadata") or {}).get("human_decision") or {}
            supplied_request_id = str(resolution.get("request_id") or "")
            pending_request_id = str(request_payload.get("request_id") or "")
            if (
                supplied_request_id
                and pending_request_id
                and supplied_request_id != pending_request_id
            ):
                raise InvalidTransition(
                    "resolution does not match the pending request id"
                )
            for field_name in (
                "generation",
                "subject_fingerprint",
                "options_fingerprint",
            ):
                supplied_value = resolution.get(field_name)
                expected_value = request_payload.get(field_name)
                if supplied_value not in {None, "", expected_value}:
                    raise InvalidTransition(
                        f"resolution does not match the pending {field_name}"
                    )
            request_id = str(request_payload.get("request_id") or resolution.get("request_id") or "")
            request = connection.execute(
                "SELECT * FROM workflow_decision_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if request is None:
                from .human_decisions import build_decision_request

                generation_row = connection.execute(
                    "SELECT COALESCE(MAX(generation), 0) AS generation "
                    "FROM workflow_decision_requests WHERE gate_type=?",
                    (gate,),
                ).fetchone()
                request_payload = build_decision_request(
                    project_id=str(before["project_id"]),
                    project_dir=self.project_dir,
                    requested_revision=expected_revision,
                    generation=int(generation_row["generation"]) + 1,
                    action=pending,
                    reason="compatibility request identity synthesized at resolution",
                ).to_dict()
                self._insert_decision_request(
                    connection, request_payload, created_at=now
                )
                request_id = str(request_payload["request_id"])
                request = connection.execute(
                    "SELECT * FROM workflow_decision_requests WHERE request_id=?",
                    (request_id,),
                ).fetchone()
            if request is None or str(request["gate_type"]) != gate:
                raise InvalidTransition("pending human decision request is not registered")
            from .human_decisions import decision_fingerprints

            if request["subject_fingerprint"] != "LEGACY_UNBOUND":
                current_subject, current_options = decision_fingerprints(
                    self.project_dir,
                    gate,
                    tuple(request_payload.get("evidence") or ()),
                )
                if (
                    current_subject != request["subject_fingerprint"]
                    or current_options != request["options_fingerprint"]
                ):
                    raise InvalidTransition(
                        "human decision request is stale because its bound evidence changed"
                    )
            safe_decision = {**dict(_redact(decision_record or {})), **dict(_redact(resolution))}
            record_gate = str(safe_decision.get("gate") or gate).strip()
            if record_gate != gate:
                raise InvalidTransition(
                    f"decision record gate {record_gate} does not match {gate}"
                )
            persisted = self._insert_decision_instance(
                connection,
                request=request,
                decision=safe_decision,
                decided_at=int(
                    safe_decision.get("selected_at")
                    or safe_decision.get("decided_epoch")
                    or now
                ),
            )
            decision_refs = list(persisted.get("artifact_refs") or ())
            decision_sha256 = canonical_hash(persisted)
            revision = expected_revision + 1
            reopened_request: dict[str, Any] | None = None
            reopen_after_step: int | None = None
            invalidated_checkpoints: list[dict[str, Any]] = []
            if persisted.get("approved") is False and gate == "content_freeze":
                reopen_after_step = int(before["last_completed_step"])
                reopen_after_stage = int(before["last_completed_stage"])
                # A rejection reopens authored work.  A replacement request is
                # created only when the workflow reaches Gate 2 again, after
                # the repaired content has a fresh fingerprint.
                reopen_after_step = min(reopen_after_step, 13)
                reopen_after_stage = min(reopen_after_stage, 8)
                invalidated_checkpoints = [
                    {
                        "stage_id": int(row["stage_id"]),
                        "subtask": str(row["subtask"]),
                        "source_step_id": row["source_step_id"],
                    }
                    for row in connection.execute(
                        "SELECT stage_id, subtask, source_step_id "
                        "FROM stage_checkpoints WHERE source_step_id > ? "
                        "OR (completed_step_id IS NULL AND source_step_id >= ?) "
                        "ORDER BY stage_id, subtask",
                        (reopen_after_step, reopen_after_step),
                    ).fetchall()
                ]
                connection.execute(
                    "DELETE FROM stage_checkpoints WHERE source_step_id > ? "
                    "OR (completed_step_id IS NULL AND source_step_id >= ?)",
                    (reopen_after_step, reopen_after_step),
                )
                connection.execute(
                    "UPDATE project_state SET status=?, pending_action_json=NULL, "
                    "last_completed_step=?, active_step=NULL, attempt=0, "
                    "last_completed_stage=?, active_stage=NULL, active_subtask=NULL, "
                    "source_step_id=NULL, revision=?, updated_at=?, last_event_at=? "
                    "WHERE singleton=1",
                    (
                        WorkflowStatus.READY.value,
                        reopen_after_step,
                        reopen_after_stage,
                        revision,
                        now,
                        now,
                    ),
                )
            elif persisted.get("approved") is False:
                from .human_decisions import build_decision_request

                reopened_request = build_decision_request(
                    project_id=str(before["project_id"]),
                    project_dir=self.project_dir,
                    requested_revision=revision,
                    generation=int(request["generation"]) + 1,
                    action=pending,
                    reason={
                        "code": "approval_rejected",
                        "message": str(
                            persisted.get("reason") or "Approval was rejected"
                        ),
                        "evidence": list(request_payload.get("evidence") or ()),
                    },
                    evidence=tuple(request_payload.get("evidence") or ()),
                ).to_dict()
                self._insert_decision_request(
                    connection, reopened_request, created_at=now
                )
                pending_metadata = dict(pending.get("metadata") or {})
                pending_metadata["human_decision"] = reopened_request
                reopened_pending = {**pending, "metadata": pending_metadata}
                connection.execute(
                    "UPDATE project_state SET status=?, pending_action_json=?, "
                    "revision=?, updated_at=?, last_event_at=? WHERE singleton=1",
                    (
                        WorkflowStatus.AWAITING_SELECTION.value,
                        json.dumps(
                            reopened_pending, ensure_ascii=True, sort_keys=True
                        ),
                        revision,
                        now,
                        now,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE project_state SET status=?, pending_action_json=NULL, "
                    "revision=?, updated_at=?, last_event_at=? WHERE singleton=1",
                    (WorkflowStatus.READY.value, revision, now, now),
                )
            after = connection.execute(
                "SELECT * FROM project_state WHERE singleton=1"
            ).fetchone()
            resolved_event_type = (
                "WORK_REOPENED"
                if persisted.get("approved") is False
                and gate == "content_freeze"
                else "ACTION_RESOLVED"
            )
            event_payload = self._versioned_event_payload(
                connection,
                before=before,
                after=after,
                revision=revision,
                event_type=resolved_event_type,
                created_at=now,
                payload={
                    "action_type": pending.get("type"),
                    "gate": gate or None,
                    "request_id": request_id,
                    "decision_id": persisted.get("decision_id"),
                    "generation": request["generation"],
                    "resolution": persisted,
                    "decision_recorded": True,
                    "decision_sha256": decision_sha256,
                    "artifact_refs": decision_refs,
                    "reopened_request": reopened_request,
                    "reopen_after_step": reopen_after_step,
                    "reopen_stage": 9 if gate == "content_freeze" and reopen_after_step is not None else None,
                    "stage": 9 if reopen_after_step is not None else None,
                    "subtask": "content_freeze_guard" if reopen_after_step is not None else None,
                    "decision": "rejected" if reopen_after_step is not None else None,
                    "invalidated_checkpoints": invalidated_checkpoints,
                    "next_task": (
                        "repair content and rerun Stage 9 before requesting Gate 2"
                        if reopen_after_step is not None
                        else None
                    ),
                },
            )
            connection.execute(
                "INSERT INTO events(revision, type, created_at, step, attempt, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    revision,
                    resolved_event_type,
                    now,
                    before["active_step"],
                    before["attempt"],
                    json.dumps(event_payload, ensure_ascii=True, sort_keys=True),
                ),
            )
        return self._state_from_row(after)

    def transition(
        self,
        *,
        expected_revision: int,
        event_type: str,
        changes: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        expected_runner_pid: int | None | object = _UNSET,
        expected_runner_lease_id: str | None | object = _UNSET,
        subtask_baseline: dict[str, Any] | None | object = _UNSET,
        stage_checkpoint: dict[str, Any] | None = None,
        stage_checkpoint_seed: list[dict[str, Any]] | None = None,
        dirty_changes: list[dict[str, Any]] | None = None,
        clear_dirty_stage: dict[str, Any] | None = None,
        invalidate_checkpoints_after_step: int | None = None,
        replace_stage_checkpoints: bool = False,
        event_step: int | None | object = _UNSET,
    ) -> WorkflowState:
        changes = dict(changes or {})
        unknown = set(changes) - _MUTABLE_COLUMNS
        if unknown:
            raise ValueError(f"unsupported state fields: {sorted(unknown)}")
        current_artifact_fingerprint: str | None = None
        if clear_dirty_stage is not None:
            from .dirty import capture_artifact_manifest, manifest_fingerprint

            current_artifact_fingerprint = manifest_fingerprint(
                capture_artifact_manifest(self.project_dir)
            )
        now = int(self._clock())
        with self._session() as connection:
            self._upgrade_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            self._validate_schema(connection)
            row = connection.execute("SELECT * FROM project_state WHERE singleton = 1").fetchone()
            if row is None:
                raise StateNotInitialized(f"workflow state is not initialized: {self.path}")
            if (
                expected_runner_pid is not _UNSET
                and row["runner_pid"] != expected_runner_pid
            ) or (
                expected_runner_lease_id is not _UNSET
                and row["runner_lease_id"] != expected_runner_lease_id
            ):
                raise RunnerLeaseLost(
                    f"runner lease lost at revision {row['revision']}"
                )
            if row["revision"] != expected_revision:
                raise RevisionConflict(
                    f"expected revision {expected_revision}, found {row['revision']}"
                )
            revision = expected_revision + 1
            values: dict[str, Any] = {}
            for key, value in changes.items():
                if key == "status" and isinstance(value, WorkflowStatus):
                    value = value.value
                if key == "pending_action":
                    key = "pending_action_json"
                    value = (
                        json.dumps(_redact(value), ensure_ascii=True, sort_keys=True)
                        if value is not None
                        else None
                    )
                values[key] = value
            values.update(revision=revision, updated_at=now, last_event_at=now)
            assignments = ", ".join(f"{key} = ?" for key in values)
            connection.execute(
                f"UPDATE project_state SET {assignments} WHERE singleton = 1",
                tuple(values.values()),
            )
            pending_for_request = changes.get("pending_action", _UNSET)
            if isinstance(pending_for_request, dict):
                request = (
                    (pending_for_request.get("metadata") or {}).get("human_decision")
                    or {}
                )
                required_request_fields = {
                    "request_id",
                    "gate",
                    "generation",
                    "kind",
                    "type",
                    "requested_revision",
                    "subject_fingerprint",
                    "options_fingerprint",
                }
                if request and required_request_fields <= set(request):
                    self._insert_decision_request(
                        connection, dict(request), created_at=now
                    )
            if subtask_baseline is not _UNSET:
                if subtask_baseline is None:
                    connection.execute(
                        "DELETE FROM stage_cursor_inputs WHERE singleton = 1"
                    )
                else:
                    baseline = dict(subtask_baseline)
                    manifest = baseline.get("manifest")
                    if not isinstance(manifest, dict):
                        raise ValueError("subtask baseline manifest must be a mapping")
                    connection.execute(
                        """
                        INSERT INTO stage_cursor_inputs(
                            singleton, stage_id, subtask, source_step_id,
                            input_fingerprint, baseline_json, selected_revision
                        ) VALUES (1, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(singleton) DO UPDATE SET
                            stage_id=excluded.stage_id,
                            subtask=excluded.subtask,
                            source_step_id=excluded.source_step_id,
                            input_fingerprint=excluded.input_fingerprint,
                            baseline_json=excluded.baseline_json,
                            selected_revision=excluded.selected_revision
                        """,
                        (
                            int(baseline["stage_id"]),
                            str(baseline["subtask"]),
                            int(baseline["source_step_id"]),
                            str(baseline["input_fingerprint"]),
                            json.dumps(manifest, ensure_ascii=True, sort_keys=True),
                            revision,
                        ),
                    )
            if replace_stage_checkpoints:
                connection.execute("DELETE FROM stage_checkpoints")
            if invalidate_checkpoints_after_step is not None:
                connection.execute(
                    "DELETE FROM stage_checkpoints WHERE source_step_id > ? "
                    "OR (completed_step_id IS NULL AND source_step_id >= ?)",
                    (
                        int(invalidate_checkpoints_after_step),
                        int(invalidate_checkpoints_after_step),
                    ),
                )
            if stage_checkpoint is not None:
                checkpoint = dict(stage_checkpoint)
                receipt = _redact(dict(checkpoint.get("receipt") or {}))
                checkpoint_id = canonical_hash(
                    {
                        "stage_id": int(checkpoint["stage_id"]),
                        "subtask": str(checkpoint["subtask"]),
                        "revision": revision,
                        "input": str(checkpoint["input_fingerprint"]),
                        "output": str(checkpoint["output_fingerprint"]),
                    }
                )[:32]
                connection.execute(
                    """
                    INSERT INTO stage_checkpoint_history(
                        checkpoint_id, stage_id, subtask, source_step_id,
                        completed_step_id, input_fingerprint, output_fingerprint,
                        completed_revision, receipt_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint_id,
                        int(checkpoint["stage_id"]),
                        str(checkpoint["subtask"]),
                        int(checkpoint["source_step_id"]),
                        checkpoint.get("completed_step_id"),
                        str(checkpoint["input_fingerprint"]),
                        str(checkpoint["output_fingerprint"]),
                        revision,
                        json.dumps(receipt, ensure_ascii=True, sort_keys=True),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO stage_checkpoints(
                        stage_id, subtask, source_step_id, completed_step_id,
                        input_fingerprint, output_fingerprint,
                        completed_revision, receipt_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(stage_id, subtask) DO UPDATE SET
                        source_step_id=excluded.source_step_id,
                        completed_step_id=excluded.completed_step_id,
                        input_fingerprint=excluded.input_fingerprint,
                        output_fingerprint=excluded.output_fingerprint,
                        completed_revision=excluded.completed_revision,
                        receipt_json=excluded.receipt_json
                    """,
                    (
                        int(checkpoint["stage_id"]),
                        str(checkpoint["subtask"]),
                        int(checkpoint["source_step_id"]),
                        checkpoint.get("completed_step_id"),
                        str(checkpoint["input_fingerprint"]),
                        str(checkpoint["output_fingerprint"]),
                        revision,
                        json.dumps(receipt, ensure_ascii=True, sort_keys=True),
                    ),
                )
            for checkpoint in stage_checkpoint_seed or []:
                receipt = _redact(dict(checkpoint.get("receipt") or {}))
                connection.execute(
                    """
                    INSERT INTO stage_checkpoints(
                        stage_id, subtask, source_step_id, completed_step_id,
                        input_fingerprint, output_fingerprint,
                        completed_revision, receipt_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(stage_id, subtask) DO NOTHING
                    """,
                    (
                        int(checkpoint["stage_id"]),
                        str(checkpoint["subtask"]),
                        int(checkpoint["source_step_id"]),
                        checkpoint.get("completed_step_id"),
                        str(checkpoint.get("input_fingerprint") or "MIGRATION_SEED"),
                        str(checkpoint.get("output_fingerprint") or "MIGRATION_SEED"),
                        revision,
                        json.dumps(receipt, ensure_ascii=True, sort_keys=True),
                    ),
                )
            for dirty in dirty_changes or []:
                cause_id = canonical_hash(
                    {
                        "revision": revision,
                        "flag": str(dirty["flag"]),
                        "owner_stage": int(dirty["owner_stage"]),
                        "artifact": str(dirty["cause_artifact"]),
                        "baseline": str(dirty["baseline_fingerprint"]),
                        "current": str(dirty["current_fingerprint"]),
                    }
                )[:32]
                connection.execute(
                    """
                    INSERT INTO dirty_causes(
                        cause_id, flag, owner_stage, cause_revision,
                        cause_artifact, baseline_fingerprint,
                        current_fingerprint, classifier_contract_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cause_id,
                        str(dirty["flag"]),
                        int(dirty["owner_stage"]),
                        revision,
                        str(dirty["cause_artifact"]),
                        str(dirty["baseline_fingerprint"]),
                        str(dirty["current_fingerprint"]),
                        str(dirty["classifier_contract_sha256"]),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO dirty_flags(
                        flag, owner_stage, cause_revision, cause_artifact,
                        baseline_fingerprint, current_fingerprint,
                        classifier_contract_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(flag) DO UPDATE SET
                        owner_stage=excluded.owner_stage,
                        cause_revision=excluded.cause_revision,
                        cause_artifact=excluded.cause_artifact,
                        baseline_fingerprint=excluded.baseline_fingerprint,
                        current_fingerprint=excluded.current_fingerprint,
                        classifier_contract_sha256=excluded.classifier_contract_sha256
                    """,
                    (
                        str(dirty["flag"]),
                        int(dirty["owner_stage"]),
                        revision,
                        str(dirty["cause_artifact"]),
                        str(dirty["baseline_fingerprint"]),
                        str(dirty["current_fingerprint"]),
                        str(dirty["classifier_contract_sha256"]),
                    ),
                )
            if clear_dirty_stage is not None:
                clear = dict(clear_dirty_stage)
                owner_stage = int(clear["owner_stage"])
                if stage_checkpoint is None:
                    raise InvalidTransition(
                        "dirty flags can only be cleared with a Stage checkpoint"
                    )
                checkpoint = dict(stage_checkpoint)
                success_receipt = dict(clear.get("success_receipt") or {})
                if int(checkpoint.get("stage_id", -1)) != owner_stage:
                    raise InvalidTransition(
                        "dirty clear owner does not match the successful Stage checkpoint"
                    )
                if (
                    success_receipt.get("schema_version")
                    != "factory-stage-checkpoint-v1"
                    or success_receipt.get("status") != "PASS"
                    or int(success_receipt.get("stage", -1)) != owner_stage
                ):
                    raise InvalidTransition(
                        "dirty clear requires a PASS factory Stage checkpoint receipt"
                    )
                cleared_fingerprint = str(clear["cleared_fingerprint"])
                classifier_fingerprint = str(clear["classifier_contract_sha256"])
                if (
                    str(checkpoint.get("output_fingerprint")) != cleared_fingerprint
                    or str(success_receipt.get("output_fingerprint"))
                    != cleared_fingerprint
                    or current_artifact_fingerprint != cleared_fingerprint
                ):
                    raise InvalidTransition(
                        "dirty clear fingerprint is stale or does not match the checkpoint"
                    )
                if (
                    str(success_receipt.get("classifier_contract_sha256"))
                    != classifier_fingerprint
                ):
                    raise InvalidTransition(
                        "dirty clear classifier does not match the checkpoint receipt"
                    )
                rows_to_clear = connection.execute(
                    "SELECT * FROM dirty_flags WHERE owner_stage = ? ORDER BY flag",
                    (owner_stage,),
                ).fetchall()
                for dirty_row in rows_to_clear:
                    if dirty_row["classifier_contract_sha256"] != classifier_fingerprint:
                        raise InvalidTransition(
                            "dirty clear classifier does not match the dirty cause"
                        )
                    receipt = {
                        "schema_version": "factory-dirty-clear-receipt-v1",
                        "flag": dirty_row["flag"],
                        "owner_stage": owner_stage,
                        "cause_revision": dirty_row["cause_revision"],
                        "cause_artifact": dirty_row["cause_artifact"],
                        "cleared_fingerprint": cleared_fingerprint,
                        "classifier_contract_sha256": classifier_fingerprint,
                        "success_receipt": _redact(success_receipt),
                    }
                    connection.execute(
                        """
                        INSERT INTO dirty_flag_clear_receipts(
                            revision, flag, owner_stage, cleared_fingerprint,
                            classifier_contract_sha256, receipt_json
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            revision,
                            dirty_row["flag"],
                            owner_stage,
                            str(clear["cleared_fingerprint"]),
                            str(clear["classifier_contract_sha256"]),
                            json.dumps(receipt, ensure_ascii=True, sort_keys=True),
                        ),
                    )
                connection.execute(
                    "DELETE FROM dirty_flags WHERE owner_stage = ?", (owner_stage,)
                )
            effective_step = (
                changes.get("active_step", row["active_step"])
                if event_step is _UNSET
                else event_step
            )
            effective_attempt = int(changes.get("attempt", row["attempt"]))
            updated = connection.execute(
                "SELECT * FROM project_state WHERE singleton = 1"
            ).fetchone()
            safe_payload = self._versioned_event_payload(
                connection,
                before=row,
                after=updated,
                revision=revision,
                event_type=event_type,
                created_at=now,
                payload=_redact(payload or {}),
            )
            connection.execute(
                "INSERT INTO events(revision, type, created_at, step, attempt, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    revision,
                    event_type,
                    now,
                    effective_step,
                    effective_attempt,
                    json.dumps(safe_payload, ensure_ascii=True, sort_keys=True),
                ),
            )
        return self._state_from_row(updated)

    def events(self, *, since_revision: int = 0) -> list[WorkflowEvent]:
        if not self.path.is_file():
            return []
        with self._session() as connection:
            self._upgrade_schema(connection)
            self._validate_schema(connection)
            rows = connection.execute(
                "SELECT * FROM events WHERE revision > ? ORDER BY revision", (since_revision,)
            ).fetchall()
        return [
            WorkflowEvent(
                revision=row["revision"],
                type=row["type"],
                created_at=row["created_at"],
                step=row["step"],
                attempt=row["attempt"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    def record_projection_failure(
        self,
        *,
        revision: int,
        projector_name: str,
        error_type: str,
    ) -> None:
        with self._session() as connection:
            self._upgrade_schema(connection)
            connection.execute(
                """
                INSERT INTO projection_failures(
                    revision, projector_name, error_type, status, created_at
                ) VALUES (?, ?, ?, 'pending', ?)
                ON CONFLICT(revision, projector_name) DO UPDATE SET
                    error_type=excluded.error_type,
                    status='pending',
                    created_at=excluded.created_at,
                    resolved_at=NULL
                """,
                (
                    int(revision),
                    str(projector_name),
                    str(error_type),
                    int(self._clock()),
                ),
            )

    def projection_failures(self, *, pending_only: bool = False) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        query = "SELECT * FROM projection_failures"
        if pending_only:
            query += " WHERE status='pending'"
        query += " ORDER BY revision, projector_name"
        with self._session() as connection:
            self._upgrade_schema(connection)
            rows = connection.execute(query).fetchall()
        return [dict(row) for row in rows]

    def resolve_projection_failure(self, *, revision: int, projector_name: str) -> None:
        with self._session() as connection:
            self._upgrade_schema(connection)
            connection.execute(
                "UPDATE projection_failures SET status='resolved', resolved_at=? "
                "WHERE revision=? AND projector_name=?",
                (int(self._clock()), int(revision), str(projector_name)),
            )

    def save_projector_snapshot(
        self,
        projector_name: str,
        *,
        projector_version: int,
        through_revision: int,
        state_hash: str,
        snapshot: dict[str, Any],
    ) -> None:
        """Cache a pure projector result; workflow truth remains the event log."""

        if not projector_name or projector_version < 1 or through_revision < 0:
            raise ValueError("invalid projector snapshot identity")
        with self._session() as connection:
            self._upgrade_schema(connection)
            event_rows = connection.execute(
                "SELECT revision, payload_json FROM events WHERE revision <= ? ORDER BY revision",
                (int(through_revision),),
            ).fetchall()
            through_event_id = None
            through_event_payload_sha256 = None
            source_chain_root_sha256 = None
            if event_rows:
                payload_hashes: list[dict[str, Any]] = []
                for event_row in event_rows:
                    raw_payload = str(event_row["payload_json"])
                    payload_sha = canonical_hash(json.loads(raw_payload))
                    payload_hashes.append(
                        {"revision": int(event_row["revision"]), "sha256": payload_sha}
                    )
                last_payload = json.loads(event_rows[-1]["payload_json"])
                envelope = last_payload.get(ENVELOPE_KEY) or {}
                through_event_id = envelope.get("event_id")
                through_event_payload_sha256 = payload_hashes[-1]["sha256"]
                source_chain_root_sha256 = canonical_hash(payload_hashes)
            connection.execute(
                """
                INSERT INTO projector_snapshots(
                    projector_name, projector_version, through_revision,
                    state_hash, through_event_id, through_event_payload_sha256,
                    source_chain_root_sha256, snapshot_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(projector_name) DO UPDATE SET
                    projector_version=excluded.projector_version,
                    through_revision=excluded.through_revision,
                    state_hash=excluded.state_hash,
                    through_event_id=excluded.through_event_id,
                    through_event_payload_sha256=excluded.through_event_payload_sha256,
                    source_chain_root_sha256=excluded.source_chain_root_sha256,
                    snapshot_json=excluded.snapshot_json,
                    created_at=excluded.created_at
                """,
                (
                    projector_name,
                    projector_version,
                    through_revision,
                    state_hash,
                    through_event_id,
                    through_event_payload_sha256,
                    source_chain_root_sha256,
                    json.dumps(_redact(snapshot), ensure_ascii=True, sort_keys=True),
                    int(self._clock()),
                ),
            )

    def projector_snapshot(
        self,
        projector_name: str,
        *,
        projector_version: int | None = None,
        maximum_revision: int | None = None,
        state_hash: str | None = None,
    ) -> dict[str, Any] | None:
        with self._session() as connection:
            self._upgrade_schema(connection)
            row = connection.execute(
                "SELECT * FROM projector_snapshots WHERE projector_name=?",
                (projector_name,),
            ).fetchone()
        if row is None:
            return None
        if projector_version is not None and row["projector_version"] != projector_version:
            return None
        if maximum_revision is not None and row["through_revision"] > maximum_revision:
            return None
        if state_hash is not None and row["state_hash"] != state_hash:
            return None
        return {
            "projector_name": row["projector_name"],
            "projector_version": row["projector_version"],
            "through_revision": row["through_revision"],
            "state_hash": row["state_hash"],
            "through_event_id": row["through_event_id"],
            "through_event_payload_sha256": row["through_event_payload_sha256"],
            "source_chain_root_sha256": row["source_chain_root_sha256"],
            "snapshot": json.loads(row["snapshot_json"]),
            "created_at": row["created_at"],
        }

    def completed_stage_subtasks(self) -> set[tuple[int, str]]:
        if not self.path.is_file():
            return set()
        with self._session() as connection:
            self._upgrade_schema(connection)
            rows = connection.execute(
                "SELECT stage_id, subtask FROM stage_checkpoints"
            ).fetchall()
        return {(int(row["stage_id"]), str(row["subtask"])) for row in rows}

    def stage_checkpoints(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        with self._session() as connection:
            self._upgrade_schema(connection)
            rows = connection.execute(
                "SELECT * FROM stage_checkpoints ORDER BY stage_id, completed_revision"
            ).fetchall()
        return [
            {
                "stage_id": row["stage_id"],
                "subtask": row["subtask"],
                "source_step_id": row["source_step_id"],
                "completed_step_id": row["completed_step_id"],
                "input_fingerprint": row["input_fingerprint"],
                "output_fingerprint": row["output_fingerprint"],
                "completed_revision": row["completed_revision"],
                "receipt": json.loads(row["receipt_json"]),
            }
            for row in rows
        ]

    def stage_cursor_input(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        with self._session() as connection:
            self._upgrade_schema(connection)
            row = connection.execute(
                "SELECT * FROM stage_cursor_inputs WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return None
        return {
            "stage_id": row["stage_id"],
            "subtask": row["subtask"],
            "source_step_id": row["source_step_id"],
            "input_fingerprint": row["input_fingerprint"],
            "manifest": json.loads(row["baseline_json"]),
            "selected_revision": row["selected_revision"],
        }

    def dirty_flags(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        with self._session() as connection:
            self._upgrade_schema(connection)
            rows = connection.execute(
                "SELECT * FROM dirty_flags ORDER BY flag"
            ).fetchall()
        return [dict(row) for row in rows]

    def dirty_clear_receipts(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        with self._session() as connection:
            self._upgrade_schema(connection)
            rows = connection.execute(
                "SELECT * FROM dirty_flag_clear_receipts ORDER BY revision, flag"
            ).fetchall()
        return [
            {
                **dict(row),
                "receipt": json.loads(row["receipt_json"]),
            }
            for row in rows
        ]

    def solver_policy(self) -> dict[str, Any]:
        with self._session() as connection:
            self._upgrade_schema(connection)
            self._validate_schema(connection)
            row = connection.execute(
                "SELECT * FROM project_config WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return {
                "mode": "local",
                "threshold_seconds": 300,
                "allowed_runtimes": ["python"],
                "updated_revision": 0,
            }
        return {
            "mode": row["solver_mode"],
            "threshold_seconds": row["solver_threshold_seconds"],
            "allowed_runtimes": json.loads(row["solver_runtimes_json"]),
            "updated_revision": row["updated_revision"],
        }

    def configure_solver_policy(
        self,
        *,
        expected_revision: int,
        mode: str,
        threshold_seconds: int,
        allowed_runtimes: list[str],
    ) -> WorkflowState:
        if mode not in {"local", "cloud", "auto"}:
            raise ValueError(f"unsupported solver mode: {mode}")
        if threshold_seconds < 1 or threshold_seconds > 86_400:
            raise ValueError("solver threshold must be between 1 and 86400 seconds")
        runtimes = sorted({str(value) for value in allowed_runtimes if str(value)})
        if not runtimes:
            raise ValueError("at least one solver runtime is required")
        now = int(self._clock())
        with self._session() as connection:
            self._upgrade_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM project_state WHERE singleton = 1"
            ).fetchone()
            if row["revision"] != expected_revision:
                raise RevisionConflict(
                    f"expected revision {expected_revision}, found {row['revision']}"
                )
            revision = expected_revision + 1
            connection.execute(
                """
                INSERT INTO project_config(
                    singleton, solver_mode, solver_threshold_seconds,
                    solver_runtimes_json, updated_revision
                ) VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    solver_mode=excluded.solver_mode,
                    solver_threshold_seconds=excluded.solver_threshold_seconds,
                    solver_runtimes_json=excluded.solver_runtimes_json,
                    updated_revision=excluded.updated_revision
                """,
                (mode, threshold_seconds, json.dumps(runtimes), revision),
            )
            connection.execute(
                "UPDATE project_state SET revision=?, updated_at=?, last_event_at=? WHERE singleton=1",
                (revision, now, now),
            )
            updated = connection.execute(
                "SELECT * FROM project_state WHERE singleton=1"
            ).fetchone()
            event_payload = self._versioned_event_payload(
                connection,
                before=row,
                after=updated,
                revision=revision,
                event_type="SOLVER_POLICY_CONFIGURED",
                created_at=now,
                payload={
                    "mode": mode,
                    "threshold_seconds": threshold_seconds,
                    "allowed_runtimes": runtimes,
                },
            )
            connection.execute(
                "INSERT INTO events VALUES (?, 'SOLVER_POLICY_CONFIGURED', ?, NULL, 0, ?)",
                (
                    revision,
                    now,
                    json.dumps(event_payload, sort_keys=True),
                ),
            )
        return self._state_from_row(updated)

    def create_solver_job(
        self, *, expected_revision: int, record: dict[str, Any]
    ) -> WorkflowState:
        now = int(self._clock())
        with self._session() as connection:
            self._upgrade_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT * FROM project_state WHERE singleton=1"
            ).fetchone()
            if state["revision"] != expected_revision:
                raise RevisionConflict(
                    f"expected revision {expected_revision}, found {state['revision']}"
                )
            revision = expected_revision + 1
            connection.execute(
                """
                INSERT INTO solver_jobs(
                    job_id, job_revision, idempotency_key, request_sha256,
                    owner_stage, owner_subtask, owner_revision, attempt_id,
                    backend, runtime, script, workdir, argv_json,
                    max_time_seconds, external_id, status, requested_at,
                    started_at, finished_at, result_refs_json, failure_json
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["job_id"], record.get("idempotency_key"),
                    record.get("request_sha256"), record.get("owner_stage"),
                    record.get("owner_subtask"), record.get("owner_revision"),
                    record.get("attempt_id"), record["backend"], record["runtime"],
                    record["script"], record["workdir"],
                    json.dumps(record.get("argv", []), sort_keys=True),
                    int(record["max_time_seconds"]), record.get("external_id"),
                    record.get("status", "submitted"), now,
                    record.get("started_at"), record.get("finished_at"),
                    json.dumps(_redact(record.get("result_refs", {})), sort_keys=True),
                    json.dumps(_redact(record.get("failure")), sort_keys=True)
                    if record.get("failure") is not None else None,
                ),
            )
            connection.execute(
                "UPDATE project_state SET revision=?, updated_at=?, last_event_at=? WHERE singleton=1",
                (revision, now, now),
            )
            updated = connection.execute(
                "SELECT * FROM project_state WHERE singleton=1"
            ).fetchone()
            event_payload = self._versioned_event_payload(
                connection,
                before=state,
                after=updated,
                revision=revision,
                event_type="SOLVER_JOB_SUBMITTED",
                created_at=now,
                payload={
                    "job_id": record["job_id"],
                    "backend": record["backend"],
                    "runtime": record["runtime"],
                    "max_time_seconds": int(record["max_time_seconds"]),
                    "idempotency_key": record.get("idempotency_key"),
                    "owner_stage": record.get("owner_stage"),
                    "owner_subtask": record.get("owner_subtask"),
                    "owner_revision": record.get("owner_revision"),
                    "attempt_id": record.get("attempt_id"),
                },
            )
            connection.execute(
                "INSERT INTO events VALUES (?, 'SOLVER_JOB_SUBMITTED', ?, NULL, 0, ?)",
                (
                    revision,
                    now,
                    json.dumps(event_payload, sort_keys=True),
                ),
            )
        return self._state_from_row(updated)

    def update_solver_job(
        self,
        job_id: str,
        *,
        expected_job_revision: int,
        status: str,
        external_id: str | None = None,
        result_refs: dict[str, Any] | None = None,
        failure: dict[str, Any] | None = None,
    ) -> WorkflowState:
        now = int(self._clock())
        with self._session() as connection:
            self._upgrade_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT * FROM solver_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if job is None:
                raise KeyError(f"solver job not found: {job_id}")
            if job["job_revision"] != expected_job_revision:
                raise RevisionConflict(
                    f"expected solver job revision {expected_job_revision}, "
                    f"found {job['job_revision']}"
                )
            state = connection.execute(
                "SELECT * FROM project_state WHERE singleton=1"
            ).fetchone()
            revision = int(state["revision"]) + 1
            job_revision = expected_job_revision + 1
            started = job["started_at"] or (now if status == "running" else None)
            finished = now if status in {"completed", "failed", "timeout", "cancelled"} else None
            connection.execute(
                """
                UPDATE solver_jobs SET job_revision=?, status=?, external_id=COALESCE(?, external_id),
                    started_at=COALESCE(?, started_at), finished_at=COALESCE(?, finished_at),
                    result_refs_json=COALESCE(?, result_refs_json),
                    failure_json=COALESCE(?, failure_json)
                WHERE job_id=?
                """,
                (
                    job_revision, status, external_id, started, finished,
                    json.dumps(_redact(result_refs), sort_keys=True) if result_refs is not None else None,
                    json.dumps(_redact(failure), sort_keys=True) if failure is not None else None,
                    job_id,
                ),
            )
            connection.execute(
                "UPDATE project_state SET revision=?, updated_at=?, last_event_at=? WHERE singleton=1",
                (revision, now, now),
            )
            updated = connection.execute(
                "SELECT * FROM project_state WHERE singleton=1"
            ).fetchone()
            event_type = f"SOLVER_JOB_{status.upper()}"
            event_payload = self._versioned_event_payload(
                connection,
                before=state,
                after=updated,
                revision=revision,
                event_type=event_type,
                created_at=now,
                payload={
                    "job_id": job_id,
                    "job_revision": job_revision,
                    "external_id": external_id,
                    "failure": _redact(failure),
                },
            )
            connection.execute(
                "INSERT INTO events VALUES (?, ?, ?, NULL, 0, ?)",
                (
                    revision,
                    event_type,
                    now,
                    json.dumps(event_payload, sort_keys=True),
                ),
            )
        return self._state_from_row(updated)

    def record_solver_receipt(
        self,
        job_id: str,
        *,
        stage: str,
        receipt_path: str,
        receipt_sha256: str,
        content_sha256: str,
        request_sha256: str,
    ) -> WorkflowState:
        """Append an idempotent content-addressed receipt event."""

        if stage not in {"submitted", "completed"}:
            raise ValueError("solver receipt stage must be submitted or completed")
        event_type = f"SOLVER_JOB_RECEIPT_{stage.upper()}"
        payload = {
            "job_id": job_id,
            "stage": stage,
            "receipt_path": receipt_path,
            "receipt_sha256": receipt_sha256,
            "content_sha256": content_sha256,
            "request_sha256": request_sha256,
        }
        now = int(self._clock())
        with self._session() as connection:
            self._upgrade_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT 1 FROM solver_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if job is None:
                raise KeyError(f"solver job not found: {job_id}")
            prior_rows = connection.execute(
                "SELECT payload_json FROM events WHERE type=? ORDER BY revision",
                (event_type,),
            ).fetchall()
            for row in prior_rows:
                prior = json.loads(row["payload_json"])
                if prior.get("job_id") != job_id:
                    continue
                prior_legacy = {
                    key: value for key, value in prior.items() if key != ENVELOPE_KEY
                }
                if prior_legacy != payload:
                    raise ValueError(
                        f"immutable {stage} solver receipt event already differs for {job_id}"
                    )
                state = connection.execute(
                    "SELECT * FROM project_state WHERE singleton=1"
                ).fetchone()
                return self._state_from_row(state)
            state = connection.execute(
                "SELECT * FROM project_state WHERE singleton=1"
            ).fetchone()
            revision = int(state["revision"]) + 1
            connection.execute(
                "UPDATE project_state SET revision=?, updated_at=?, last_event_at=? WHERE singleton=1",
                (revision, now, now),
            )
            if stage == "submitted":
                connection.execute(
                    "UPDATE solver_jobs SET request_sha256=? WHERE job_id=?",
                    (request_sha256, job_id),
                )
            updated = connection.execute(
                "SELECT * FROM project_state WHERE singleton=1"
            ).fetchone()
            event_payload = self._versioned_event_payload(
                connection,
                before=state,
                after=updated,
                revision=revision,
                event_type=event_type,
                created_at=now,
                payload=payload,
            )
            connection.execute(
                "INSERT INTO events VALUES (?, ?, ?, NULL, 0, ?)",
                (revision, event_type, now, json.dumps(event_payload, sort_keys=True)),
            )
        return self._state_from_row(updated)

    def solver_job(self, job_id: str) -> dict[str, Any]:
        with self._session() as connection:
            self._upgrade_schema(connection)
            row = connection.execute(
                "SELECT * FROM solver_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"solver job not found: {job_id}")
        return self._solver_job_from_row(row)

    def solver_job_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        with self._session() as connection:
            self._upgrade_schema(connection)
            row = connection.execute(
                "SELECT * FROM solver_jobs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        return self._solver_job_from_row(row) if row is not None else None

    def solver_jobs(self) -> list[dict[str, Any]]:
        with self._session() as connection:
            self._upgrade_schema(connection)
            rows = connection.execute(
                "SELECT * FROM solver_jobs ORDER BY requested_at, job_id"
            ).fetchall()
        return [self._solver_job_from_row(row) for row in rows]

    @staticmethod
    def _solver_job_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "job_id": row["job_id"],
            "job_revision": row["job_revision"],
            "idempotency_key": row["idempotency_key"],
            "request_sha256": row["request_sha256"],
            "owner_stage": row["owner_stage"],
            "owner_subtask": row["owner_subtask"],
            "owner_revision": row["owner_revision"],
            "attempt_id": row["attempt_id"],
            "backend": row["backend"],
            "runtime": row["runtime"],
            "script": row["script"],
            "workdir": row["workdir"],
            "argv": json.loads(row["argv_json"]),
            "max_time_seconds": row["max_time_seconds"],
            "external_id": row["external_id"],
            "status": row["status"],
            "requested_at": row["requested_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "result_refs": json.loads(row["result_refs_json"]),
            "failure": json.loads(row["failure_json"]) if row["failure_json"] else None,
        }

    def prepare_for_move(self) -> None:
        if not self.path.is_file():
            return
        connection = self._connect()
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            connection.commit()
            connection.execute("PRAGMA journal_mode = DELETE").fetchall()
        finally:
            connection.close()

    @staticmethod
    def _state_from_row(row: sqlite3.Row) -> WorkflowState:
        pending = json.loads(row["pending_action_json"]) if row["pending_action_json"] else None
        return WorkflowState(
            schema_version=row["schema_version"],
            project_id=row["project_id"],
            project_type=row["project_type"],
            control_mode=row["control_mode"],
            runtime_generation=row["runtime_generation"],
            scheduler_generation=row["scheduler_generation"],
            stage_catalog_version=row["stage_catalog_version"],
            status=WorkflowStatus(row["status"]),
            last_completed_step=row["last_completed_step"],
            active_step=row["active_step"],
            last_completed_stage=row["last_completed_stage"],
            active_stage=row["active_stage"],
            active_subtask=row["active_subtask"],
            source_step_id=row["source_step_id"],
            attempt=row["attempt"],
            revision=row["revision"],
            pending_action=pending,
            runner_pid=row["runner_pid"],
            runner_lease_id=row["runner_lease_id"],
            heartbeat_at=row["heartbeat_at"],
            storage_scope=row["storage_scope"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_event_at=row["last_event_at"],
        )
