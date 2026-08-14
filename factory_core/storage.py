from __future__ import annotations

import json
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
            CREATE TABLE IF NOT EXISTS projector_snapshots (
                projector_name TEXT PRIMARY KEY,
                projector_version INTEGER NOT NULL,
                through_revision INTEGER NOT NULL,
                state_hash TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
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
            CREATE TABLE IF NOT EXISTS dirty_flags (
                flag TEXT PRIMARY KEY,
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
            CREATE TRIGGER IF NOT EXISTS dirty_flag_clear_receipts_append_only_update
            BEFORE UPDATE ON dirty_flag_clear_receipts
            BEGIN
                SELECT RAISE(ABORT, 'dirty clear receipts are append-only');
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
        if current not in {1, 2, 3, 4, 5, 6}:
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
            CREATE TABLE IF NOT EXISTS projector_snapshots (
                projector_name TEXT PRIMARY KEY,
                projector_version INTEGER NOT NULL,
                through_revision INTEGER NOT NULL,
                state_hash TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
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
        return build_event_payload(
            project_id=str(before["project_id"]),
            revision=revision,
            event_type=event_type,
            created_at=created_at,
            payload=_redact(payload or {}),
            before=self._state_from_row(before),
            after=self._state_from_row(after),
            force_snapshot=not prior_versioned,
        )

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

    def decision(self, gate: str) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        with self._session() as connection:
            self._upgrade_schema(connection)
            row = connection.execute(
                "SELECT decision_json FROM workflow_decisions WHERE gate = ?",
                (str(gate),),
            ).fetchone()
        return json.loads(row["decision_json"]) if row is not None else None

    def record_decision(self, gate: str, decision: dict[str, Any]) -> dict[str, Any]:
        gate = str(gate).strip()
        if not gate:
            raise ValueError("decision gate is required")
        safe = _redact(decision)
        encoded = json.dumps(safe, ensure_ascii=True, sort_keys=True)
        decided_at = int(safe.get("selected_at") or safe.get("decided_epoch") or self._clock())
        with self._session() as connection:
            self._upgrade_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT decision_json FROM workflow_decisions WHERE gate = ?",
                (gate,),
            ).fetchone()
            if prior is not None:
                if prior["decision_json"] != encoded:
                    raise ValueError(f"immutable workflow decision already exists for {gate}")
                return json.loads(prior["decision_json"])
            connection.execute(
                "INSERT INTO workflow_decisions(gate, decided_at, decision_json) VALUES (?, ?, ?)",
                (gate, decided_at, encoded),
            )
        return safe

    def resolve_human_decision(
        self,
        *,
        expected_revision: int,
        resolution: dict[str, Any],
        decision_record: dict[str, Any] | None = None,
    ) -> WorkflowState:
        """Atomically record a durable decision and clear its pending action."""

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
            decision_refs: list[dict[str, Any]] = []
            decision_sha256: str | None = None
            if decision_record is not None:
                if not gate:
                    raise InvalidTransition("durable human decisions require a gate")
                record_gate = str(decision_record.get("gate") or gate).strip()
                if record_gate != gate:
                    raise InvalidTransition(
                        f"decision record gate {record_gate} does not match {gate}"
                    )
                record_selected = (
                    decision_record.get("selected_option_id")
                    or decision_record.get("selected_primary")
                )
                resolution_selected = (
                    resolution.get("selected_option_id")
                    or resolution.get("selected_primary")
                    or resolution.get("selected")
                )
                if (
                    record_selected is not None
                    and resolution_selected is not None
                    and str(record_selected) != str(resolution_selected)
                ):
                    raise InvalidTransition(
                        "decision record selection does not match the resolution"
                    )
                if (
                    decision_record.get("answer") is not None
                    and resolution.get("answer") is not None
                    and str(decision_record["answer"]) != str(resolution["answer"])
                ):
                    raise InvalidTransition(
                        "decision record answer does not match the resolution"
                    )
                safe_decision = _redact(decision_record)
                decision_refs = list(safe_decision.get("artifact_refs") or ())
                decision_sha256 = canonical_hash(safe_decision)
                encoded = json.dumps(
                    safe_decision, ensure_ascii=True, sort_keys=True
                )
                prior = connection.execute(
                    "SELECT decision_json FROM workflow_decisions WHERE gate=?",
                    (gate,),
                ).fetchone()
                if prior is not None and prior["decision_json"] != encoded:
                    raise InvalidTransition(
                        f"immutable workflow decision already exists for {gate}"
                    )
                if prior is None:
                    decided_at = int(
                        safe_decision.get("selected_at")
                        or safe_decision.get("decided_epoch")
                        or now
                    )
                    connection.execute(
                        "INSERT INTO workflow_decisions(gate, decided_at, decision_json) "
                        "VALUES (?, ?, ?)",
                        (gate, decided_at, encoded),
                    )
            revision = expected_revision + 1
            connection.execute(
                "UPDATE project_state SET status=?, pending_action_json=NULL, "
                "revision=?, updated_at=?, last_event_at=? WHERE singleton=1",
                (WorkflowStatus.READY.value, revision, now, now),
            )
            after = connection.execute(
                "SELECT * FROM project_state WHERE singleton=1"
            ).fetchone()
            event_payload = self._versioned_event_payload(
                connection,
                before=before,
                after=after,
                revision=revision,
                event_type="ACTION_RESOLVED",
                created_at=now,
                payload={
                    "action_type": pending.get("type"),
                    "gate": gate or None,
                    "resolution": resolution,
                    "decision_recorded": decision_record is not None,
                    "decision_sha256": decision_sha256,
                    "artifact_refs": decision_refs,
                },
            )
            connection.execute(
                "INSERT INTO events(revision, type, created_at, step, attempt, payload_json) "
                "VALUES (?, 'ACTION_RESOLVED', ?, ?, ?, ?)",
                (
                    revision,
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
                rows_to_clear = connection.execute(
                    "SELECT * FROM dirty_flags WHERE owner_stage = ? ORDER BY flag",
                    (owner_stage,),
                ).fetchall()
                for dirty_row in rows_to_clear:
                    receipt = {
                        "schema_version": "factory-dirty-clear-receipt-v1",
                        "flag": dirty_row["flag"],
                        "owner_stage": owner_stage,
                        "cause_revision": dirty_row["cause_revision"],
                        "cause_artifact": dirty_row["cause_artifact"],
                        "cleared_fingerprint": str(clear["cleared_fingerprint"]),
                        "classifier_contract_sha256": str(
                            clear["classifier_contract_sha256"]
                        ),
                        "success_receipt": _redact(clear.get("success_receipt") or {}),
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
            connection.execute(
                """
                INSERT INTO projector_snapshots(
                    projector_name, projector_version, through_revision,
                    state_hash, snapshot_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(projector_name) DO UPDATE SET
                    projector_version=excluded.projector_version,
                    through_revision=excluded.through_revision,
                    state_hash=excluded.state_hash,
                    snapshot_json=excluded.snapshot_json,
                    created_at=excluded.created_at
                """,
                (
                    projector_name,
                    projector_version,
                    through_revision,
                    state_hash,
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
