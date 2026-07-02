from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import bcrypt


USERNAME_RE = re.compile(r"[A-Za-z0-9_-]+")


class AuthStoreError(RuntimeError):
    pass


class UserExists(AuthStoreError):
    pass


class UserNotFound(AuthStoreError):
    pass


class InvalidUsername(AuthStoreError):
    pass


class ProjectRequestNotFound(AuthStoreError):
    pass


class ProjectNameConflict(AuthStoreError):
    pass


@dataclass(frozen=True)
class StoredUser:
    username: str
    password_hash: str
    role: str
    status: str
    display_name: str = ""
    created_at: int = 0
    approved_at: int | None = None
    approved_by: str | None = None
    rejected_at: int | None = None
    rejected_by: str | None = None
    rejection_reason: str | None = None


@dataclass(frozen=True)
class ProjectRequestRecord:
    id: int
    requester: str
    base_name: str
    problem_path: str
    no_start: bool
    consult: bool
    status: str
    created_at: int
    decided_at: int | None = None
    decided_by: str | None = None
    decision_note: str | None = None
    launched_at: int | None = None
    launched_base_name: str | None = None
    launch_output: str | None = None
    failure_reason: str | None = None


@dataclass(frozen=True)
class AuditLogRecord:
    id: int
    actor: str
    action: str
    target_type: str
    target_id: str
    created_at: int
    metadata_json: str = ""


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'rejected', 'disabled')),
    display_name TEXT,
    created_at INTEGER NOT NULL,
    approved_at INTEGER,
    approved_by TEXT,
    rejected_at INTEGER,
    rejected_by TEXT,
    rejection_reason TEXT
);

CREATE TABLE IF NOT EXISTS project_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requester TEXT NOT NULL,
    base_name TEXT NOT NULL,
    problem_path TEXT NOT NULL,
    no_start INTEGER NOT NULL DEFAULT 0,
    consult INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'failed')),
    created_at INTEGER NOT NULL,
    decided_at INTEGER,
    decided_by TEXT,
    decision_note TEXT,
    launched_at INTEGER,
    launched_base_name TEXT,
    launch_output TEXT,
    failure_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_project_requests_status_base
ON project_requests(status, base_name);

CREATE TABLE IF NOT EXISTS project_acl (
    base_name TEXT NOT NULL,
    username TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner')),
    created_at INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    PRIMARY KEY (base_name, username)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    metadata_json TEXT
);
"""


def utc_now() -> int:
    return int(time.time())


def normalize_username(value: str) -> str:
    clean = value.strip()
    if not USERNAME_RE.fullmatch(clean):
        raise InvalidUsername(clean)
    return clean


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str | bytes) -> bool:
    encoded_hash = password_hash.encode("utf-8") if isinstance(password_hash, str) else password_hash
    try:
        return bcrypt.checkpw(password.encode("utf-8"), encoded_hash)
    except (TypeError, ValueError):
        return False


def row_to_user(row: sqlite3.Row) -> StoredUser:
    return StoredUser(
        username=str(row["username"]),
        password_hash=str(row["password_hash"]),
        role=str(row["role"]),
        status=str(row["status"]),
        display_name=row["display_name"] or "",
        created_at=int(row["created_at"]),
        approved_at=row["approved_at"],
        approved_by=row["approved_by"],
        rejected_at=row["rejected_at"],
        rejected_by=row["rejected_by"],
        rejection_reason=row["rejection_reason"],
    )


def row_to_project_request(row: sqlite3.Row) -> ProjectRequestRecord:
    return ProjectRequestRecord(
        id=int(row["id"]),
        requester=str(row["requester"]),
        base_name=str(row["base_name"]),
        problem_path=str(row["problem_path"]),
        no_start=bool(row["no_start"]),
        consult=bool(row["consult"]),
        status=str(row["status"]),
        created_at=int(row["created_at"]),
        decided_at=row["decided_at"],
        decided_by=row["decided_by"],
        decision_note=row["decision_note"],
        launched_at=row["launched_at"],
        launched_base_name=row["launched_base_name"],
        launch_output=row["launch_output"],
        failure_reason=row["failure_reason"],
    )


def row_to_audit(row: sqlite3.Row) -> AuditLogRecord:
    return AuditLogRecord(
        id=int(row["id"]),
        actor=str(row["actor"]),
        action=str(row["action"]),
        target_type=str(row["target_type"]),
        target_id=str(row["target_id"]),
        created_at=int(row["created_at"]),
        metadata_json=row["metadata_json"] or "",
    )


class AuthStore:
    def __init__(self, db_file: Path):
        self.db_file = Path(db_file)

    def initialize(self) -> None:
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)

    def bootstrap_admin(self, admin_password: str) -> StoredUser:
        self.initialize()
        existing = self.get_user("admin")
        if existing is not None:
            password_matches = verify_password(admin_password, existing.password_hash)
            has_admin_access = existing.role == "admin" and existing.status == "active"
            if password_matches and has_admin_access:
                return existing

            now = utc_now()
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE users
                    SET password_hash = ?,
                        role = 'admin',
                        status = 'active',
                        display_name = COALESCE(NULLIF(display_name, ''), 'Administrator'),
                        approved_at = COALESCE(approved_at, ?),
                        approved_by = COALESCE(approved_by, 'system'),
                        rejected_at = NULL,
                        rejected_by = NULL,
                        rejection_reason = NULL
                    WHERE username = 'admin'
                    """,
                    (
                        existing.password_hash if password_matches else hash_password(admin_password),
                        now,
                    ),
                )
                self._insert_audit(
                    conn,
                    "system",
                    "user.bootstrap_admin_sync",
                    "user",
                    "admin",
                    {
                        "password_updated": not password_matches,
                        "access_restored": not has_admin_access,
                    },
                )
            return self.require_user("admin")
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (
                    username, password_hash, role, status, display_name,
                    created_at, approved_at, approved_by
                ) VALUES (?, ?, 'admin', 'active', 'Administrator', ?, ?, 'system')
                """,
                ("admin", hash_password(admin_password), now, now),
            )
            self._insert_audit(conn, "system", "user.bootstrap_admin", "user", "admin", {})
        return self.require_user("admin")

    def register_user(self, username: str, password: str, display_name: str = "") -> StoredUser:
        username = normalize_username(username)
        now = utc_now()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO users (username, password_hash, role, status, display_name, created_at)
                    VALUES (?, ?, 'user', 'pending', ?, ?)
                    """,
                    (username, hash_password(password), display_name.strip(), now),
                )
                self._insert_audit(conn, username, "user.register", "user", username, {})
        except sqlite3.IntegrityError as exc:
            raise UserExists(username) from exc
        return self.require_user(username)

    def approve_user(self, username: str, actor: str) -> StoredUser:
        return self._set_user_status(username, "active", actor, reason="")

    def reject_user(self, username: str, actor: str, reason: str = "") -> StoredUser:
        return self._set_user_status(username, "rejected", actor, reason=reason)

    def disable_user(self, username: str, actor: str) -> StoredUser:
        return self._set_user_status(username, "disabled", actor, reason="")

    def delete_user(self, username: str, actor: str) -> None:
        username = normalize_username(username)
        if username == "admin":
            raise ValueError("admin user cannot be deleted")

        existing = self.get_user(username)
        if existing is None:
            raise UserNotFound(username)

        with self._connect() as conn:
            conn.execute("DELETE FROM project_acl WHERE username = ?", (username,))
            conn.execute("DELETE FROM users WHERE username = ?", (username,))
            self._insert_audit(
                conn,
                actor,
                "user.delete",
                "user",
                username,
                {
                    "role": existing.role,
                    "status": existing.status,
                },
            )

    def verify_user_password(self, username: str, password: str) -> bool:
        user = self.get_user(username)
        if user is None:
            return False
        return verify_password(password, user.password_hash)

    def get_user(self, username: str) -> StoredUser | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return row_to_user(row) if row else None

    def require_user(self, username: str) -> StoredUser:
        user = self.get_user(username)
        if user is None:
            raise UserNotFound(username)
        return user

    def list_users(self) -> list[StoredUser]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC, username").fetchall()
        return [row_to_user(row) for row in rows]

    def create_project_request(
        self,
        *,
        requester: str,
        base_name: str,
        problem_path: str,
        no_start: bool,
        consult: bool,
        existing_project_names: set[str],
    ) -> ProjectRequestRecord:
        base_name = normalize_username(base_name)
        if base_name in existing_project_names or self._has_live_project_request(base_name):
            raise ProjectNameConflict(base_name)
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO project_requests (
                    requester, base_name, problem_path, no_start, consult, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (requester, base_name, problem_path, int(no_start), int(consult), now),
            )
            request_id = int(cursor.lastrowid)
            self._insert_audit(
                conn,
                requester,
                "project_request.create",
                "project_request",
                str(request_id),
                {"base_name": base_name},
            )
        return self.require_project_request(request_id)

    def approve_project_request(
        self,
        request_id: int,
        *,
        actor: str,
        launched_base_name: str,
        launch_output: str,
    ) -> ProjectRequestRecord:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE project_requests
                SET status = 'approved', decided_at = ?, decided_by = ?, launched_at = ?,
                    launched_base_name = ?, launch_output = ?, failure_reason = NULL
                WHERE id = ? AND status = 'pending'
                """,
                (now, actor, now, launched_base_name, launch_output, request_id),
            )
            self._insert_audit(
                conn,
                actor,
                "project_request.approve",
                "project_request",
                str(request_id),
                {"base_name": launched_base_name},
            )
        return self.require_project_request(request_id)

    def reject_project_request(self, request_id: int, *, actor: str, reason: str = "") -> ProjectRequestRecord:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE project_requests
                SET status = 'rejected', decided_at = ?, decided_by = ?, decision_note = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, actor, reason, request_id),
            )
            self._insert_audit(
                conn,
                actor,
                "project_request.reject",
                "project_request",
                str(request_id),
                {"reason": reason},
            )
        return self.require_project_request(request_id)

    def mark_project_request_failed(self, request_id: int, *, actor: str, failure_reason: str) -> ProjectRequestRecord:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE project_requests
                SET status = 'failed', decided_at = ?, decided_by = ?, failure_reason = ?
                WHERE id = ?
                """,
                (now, actor, failure_reason, request_id),
            )
            self._insert_audit(
                conn,
                actor,
                "project_request.fail",
                "project_request",
                str(request_id),
                {"failure_reason": failure_reason},
            )
        return self.require_project_request(request_id)

    def require_project_request(self, request_id: int) -> ProjectRequestRecord:
        record = self.get_project_request(request_id)
        if record is None:
            raise ProjectRequestNotFound(str(request_id))
        return record

    def get_project_request(self, request_id: int) -> ProjectRequestRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM project_requests WHERE id = ?", (request_id,)).fetchone()
        return row_to_project_request(row) if row else None

    def list_project_requests(self, username: str | None = None) -> list[ProjectRequestRecord]:
        with self._connect() as conn:
            if username:
                rows = conn.execute(
                    "SELECT * FROM project_requests WHERE requester = ? ORDER BY created_at DESC, id DESC",
                    (username,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM project_requests ORDER BY created_at DESC, id DESC").fetchall()
        return [row_to_project_request(row) for row in rows]

    def grant_project_owner(self, base_name: str, username: str, actor: str) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO project_acl (base_name, username, role, created_at, created_by)
                VALUES (?, ?, 'owner', ?, ?)
                """,
                (base_name, username, now, actor),
            )
            self._insert_audit(
                conn,
                actor,
                "project_acl.grant_owner",
                "project",
                base_name,
                {"username": username},
            )

    def user_can_access_project(self, username: str, base_name: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM project_acl WHERE username = ? AND base_name = ? AND role = 'owner'",
                (username, base_name),
            ).fetchone()
        return row is not None

    def list_project_names_for_user(self, username: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT base_name FROM project_acl WHERE username = ? ORDER BY base_name",
                (username,),
            ).fetchall()
        return [str(row["base_name"]) for row in rows]

    def list_audit_log(self) -> list[AuditLogRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [row_to_audit(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        return conn

    def _insert_audit(
        self,
        conn: sqlite3.Connection,
        actor: str,
        action: str,
        target_type: str,
        target_id: str,
        metadata: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO audit_log (actor, action, target_type, target_id, created_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                actor,
                action,
                target_type,
                target_id,
                utc_now(),
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            ),
        )

    def _set_user_status(self, username: str, new_status: str, actor: str, reason: str) -> StoredUser:
        username = normalize_username(username)
        now = utc_now()
        with self._connect() as conn:
            if new_status == "active":
                conn.execute(
                    """
                    UPDATE users
                    SET status = 'active', approved_at = ?, approved_by = ?,
                        rejected_at = NULL, rejected_by = NULL, rejection_reason = NULL
                    WHERE username = ?
                    """,
                    (now, actor, username),
                )
                action = "user.approve"
                metadata: dict[str, Any] = {}
            elif new_status == "rejected":
                conn.execute(
                    """
                    UPDATE users
                    SET status = 'rejected', rejected_at = ?, rejected_by = ?, rejection_reason = ?
                    WHERE username = ?
                    """,
                    (now, actor, reason, username),
                )
                action = "user.reject"
                metadata = {"reason": reason}
            elif new_status == "disabled":
                conn.execute("UPDATE users SET status = 'disabled' WHERE username = ?", (username,))
                action = "user.disable"
                metadata = {}
            else:
                raise ValueError(f"unsupported user status: {new_status}")
            self._insert_audit(conn, actor, action, "user", username, metadata)
        return self.require_user(username)

    def _has_live_project_request(self, base_name: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM project_requests
                WHERE base_name = ? AND status IN ('pending', 'approved')
                LIMIT 1
                """,
                (base_name,),
            ).fetchone()
        return row is not None
