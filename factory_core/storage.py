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
    RevisionConflict,
    RunnerLeaseLost,
    StateNotInitialized,
    WorkflowEvent,
    WorkflowState,
    WorkflowStatus,
)


_UNSET = object()

_SENSITIVE_KEY = re.compile(
    r"(?:password|secret|token|api[_-]?key|credential|authorization|cookie|private[_-]?key)",
    re.IGNORECASE,
)
_MUTABLE_COLUMNS = {
    "control_mode",
    "runtime_generation",
    "status",
    "last_completed_step",
    "active_step",
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
                status TEXT NOT NULL,
                last_completed_step INTEGER NOT NULL,
                active_step INTEGER,
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
        if current not in {1, 2, 3}:
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
        solver_columns = {
            column[1]
            for column in connection.execute("PRAGMA table_info(solver_jobs)").fetchall()
        }
        if "job_revision" not in solver_columns:
            connection.execute(
                "ALTER TABLE solver_jobs ADD COLUMN job_revision INTEGER NOT NULL DEFAULT 1"
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
    ) -> WorkflowState:
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
                    runtime_generation,
                    status, last_completed_step, active_step, attempt, revision,
                    pending_action_json, runner_pid, runner_lease_id, heartbeat_at,
                    storage_scope, created_at, updated_at, last_event_at
                ) VALUES (1, ?, ?, ?, 'engine', ?, ?, ?, ?, 0, 1, ?, NULL, NULL, NULL, ?, ?, ?, ?)
                """,
                (
                    SCHEMA_VERSION,
                    project_id,
                    project_type,
                    runtime_generation,
                    status.value,
                    last_completed_step,
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
            event_type = "PROJECT_IMPORTED" if imported else "PROJECT_CREATED"
            payload = _redact(import_payload or {})
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

    def transition(
        self,
        *,
        expected_revision: int,
        event_type: str,
        changes: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        expected_runner_pid: int | None | object = _UNSET,
        expected_runner_lease_id: str | None | object = _UNSET,
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
            effective_step = changes.get("active_step", row["active_step"])
            effective_attempt = int(changes.get("attempt", row["attempt"]))
            safe_payload = _redact(payload or {})
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
            updated = connection.execute("SELECT * FROM project_state WHERE singleton = 1").fetchone()
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
            connection.execute(
                "INSERT INTO events VALUES (?, 'SOLVER_POLICY_CONFIGURED', ?, NULL, 0, ?)",
                (
                    revision,
                    now,
                    json.dumps(
                        {
                            "mode": mode,
                            "threshold_seconds": threshold_seconds,
                            "allowed_runtimes": runtimes,
                        },
                        sort_keys=True,
                    ),
                ),
            )
            updated = connection.execute(
                "SELECT * FROM project_state WHERE singleton=1"
            ).fetchone()
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
                    job_id, job_revision, backend, runtime, script, workdir, argv_json,
                    max_time_seconds, external_id, status, requested_at,
                    started_at, finished_at, result_refs_json, failure_json
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["job_id"], record["backend"], record["runtime"],
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
            connection.execute(
                "INSERT INTO events VALUES (?, 'SOLVER_JOB_SUBMITTED', ?, NULL, 0, ?)",
                (
                    revision,
                    now,
                    json.dumps(
                        {
                            "job_id": record["job_id"],
                            "backend": record["backend"],
                            "runtime": record["runtime"],
                            "max_time_seconds": int(record["max_time_seconds"]),
                        },
                        sort_keys=True,
                    ),
                ),
            )
            updated = connection.execute(
                "SELECT * FROM project_state WHERE singleton=1"
            ).fetchone()
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
            connection.execute(
                "INSERT INTO events VALUES (?, ?, ?, NULL, 0, ?)",
                (
                    revision,
                    f"SOLVER_JOB_{status.upper()}",
                    now,
                    json.dumps(
                        {
                            "job_id": job_id,
                            "job_revision": job_revision,
                            "external_id": external_id,
                            "failure": _redact(failure),
                        },
                        sort_keys=True,
                    ),
                ),
            )
            updated = connection.execute(
                "SELECT * FROM project_state WHERE singleton=1"
            ).fetchone()
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
            status=WorkflowStatus(row["status"]),
            last_completed_step=row["last_completed_step"],
            active_step=row["active_step"],
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
