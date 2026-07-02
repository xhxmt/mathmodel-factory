# User Admin Project Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a persistent multi-user login system for tfisher.de where self-registered users require administrator approval, project starts require administrator approval, and approved project owners can fully manage only their own projects.

**Architecture:** Add a local SQLite authorization store under `web/auth.db`, then route all authentication, project creation, project access, model configuration, Cloud Solver, and WebSocket project updates through explicit role and ACL checks. Keep the current single FastAPI service and Vue dashboard; add focused backend modules for persistence and access control, then expose minimal role-aware frontend views for registration, user approval, project request approval, and owner project work.

**Tech Stack:** Python 3, FastAPI, sqlite3, bcrypt, PyJWT, pytest, Vue 3 + Vite, axios, Node runtime helper tests.

---

## File Map

- Create: `web/backend/auth_store.py`
  - Owns SQLite schema, admin bootstrap, password hashing, user registration/approval, project request lifecycle, project ACL, and audit log writes.
- Create: `web/backend/access_control.py`
  - Owns admin checks, active-user checks, project owner checks, project list filtering, and shared HTTP errors.
- Modify: `web/backend/config.py`
  - Adds `auth_db_file` support through `AUTH_DB_FILE` with default `web/auth.db`.
- Modify: `web/backend/schemas.py`
  - Adds auth, admin, project request, and role/status response models; extends `UserInfo` and `LoginResponse`.
- Modify: `web/backend/auth.py`
  - Keeps token helpers and WS ticket store, but verifies users against `AuthStore` for request-time active status.
- Modify: `web/backend/main.py`
  - Initializes the auth DB, bootstraps `admin`, replaces login with persistent auth, and adds registration/admin user routes.
- Modify: `web/backend/project_api.py`
  - Adds project request routes, admin approval launch flow, ACL grants, owner filtering, and project/model permission checks.
- Modify: `web/backend/cloud_api.py`
  - Applies admin-only checks to global cloud endpoints and owner/admin checks to project cloud endpoints.
- Modify: `web/backend/ws.py`
  - Tracks authenticated WS connections by user and filters project updates before sending.
- Create: `tests/test_auth_store.py`
  - Unit tests for DB schema, admin bootstrap, user status lifecycle, project request lifecycle, ACL, and audit logs.
- Modify: `tests/test_control_plane_auth.py`
  - Tests persistent login behavior, registration status errors, active user token loading, and admin-only user approval routes.
- Modify: `tests/test_web_control_plane_api.py`
  - Tests project ACL filtering, project request approval, launch failure handling, model config restrictions, cloud restrictions, and WS filtering.
- Modify: `web/frontend/src/lib/contracts.js`
  - Adds normalizers for auth users and project requests.
- Modify: `web/frontend/src/lib/api.js`
  - Adds `authRegister`, `AdminUsers`, and `ProjectRequests` helpers; preserves current `Projects` and `Models` helpers.
- Modify: `web/frontend/src/composables/useAuth.js`
  - Tracks `username`, `role`, and `status` from `/api/auth/me` and login responses.
- Modify: `web/frontend/src/components/LoginForm.vue`
  - Adds login/register segmented mode and status-specific messages.
- Create: `web/frontend/src/components/AdminPanel.vue`
  - Admin overlay for pending users and pending project requests.
- Create: `web/frontend/src/components/ProjectRequestsPanel.vue`
  - User/admin panel for project request status and decisions.
- Modify: `web/frontend/src/components/NewProjectModal.vue`
  - Role-aware submit: admins create immediately, normal users submit project requests.
- Modify: `web/frontend/src/App.vue`
  - Gates admin-only controls, surfaces admin/user request panels, and adjusts create-request messaging.
- Modify: `web/frontend/src/components/ProjectWorkspace.vue`
  - Passes auth role to model controls where needed and relies on backend ACL for all project data.
- Modify: `tests/test_web_frontend_runtime_helpers.py`
  - Adds Node helper tests for role/status auth state, contract normalizers, and request helper shapes.
- Verify only at the end: `python3 -m pytest tests/test_auth_store.py tests/test_control_plane_auth.py tests/test_web_control_plane_api.py tests/test_web_frontend_runtime_helpers.py -q`, then `cd web/frontend && npm run build`.

Implementation warning: the current worktree already contains unrelated modified files. Every commit command below uses exact paths; do not run `git add .`.

---

### Task 1: Persistent Auth Store And Settings

**Files:**
- Create: `web/backend/auth_store.py`
- Modify: `web/backend/config.py`
- Test: `tests/test_auth_store.py`

- [ ] **Step 1: Write the failing auth store tests**

Create `tests/test_auth_store.py` with:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from web.backend.auth_store import AuthStore, ProjectNameConflict, UserExists
from web.backend.config import Settings


def make_store(tmp_path: Path) -> AuthStore:
    settings = Settings(
        jwt_secret="0123456789abcdef0123456789abcdef",
        admin_password="correct horse battery staple 42",
        factory_root=tmp_path,
        auth_db_file=tmp_path / "web" / "auth.db",
    )
    store = AuthStore(settings.auth_db_file)
    store.initialize()
    return store


def test_bootstrap_admin_once_and_verify_password(tmp_path):
    store = make_store(tmp_path)

    store.bootstrap_admin("first strong password")
    first = store.get_user("admin")
    store.bootstrap_admin("second strong password")
    second = store.get_user("admin")

    assert first is not None
    assert first.role == "admin"
    assert first.status == "active"
    assert second is not None
    assert second.password_hash == first.password_hash
    assert store.verify_user_password("admin", "first strong password") is True
    assert store.verify_user_password("admin", "second strong password") is False


def test_register_user_pending_then_admin_approves_and_disables(tmp_path):
    store = make_store(tmp_path)
    store.bootstrap_admin("admin password")

    created = store.register_user("alice", "alice password", "Alice")

    assert created.username == "alice"
    assert created.role == "user"
    assert created.status == "pending"
    assert store.verify_user_password("alice", "alice password") is True

    with pytest.raises(UserExists):
        store.register_user("alice", "another password", "Other Alice")

    approved = store.approve_user("alice", actor="admin")
    assert approved.status == "active"
    assert approved.approved_by == "admin"

    disabled = store.disable_user("alice", actor="admin")
    assert disabled.status == "disabled"

    audit_actions = [row.action for row in store.list_audit_log()]
    assert audit_actions == [
        "user.bootstrap_admin",
        "user.register",
        "user.approve",
        "user.disable",
    ]


def test_reject_user_records_reason(tmp_path):
    store = make_store(tmp_path)
    store.bootstrap_admin("admin password")
    store.register_user("bob", "bob password", "")

    rejected = store.reject_user("bob", actor="admin", reason="not allowed")

    assert rejected.status == "rejected"
    assert rejected.rejection_reason == "not allowed"
    assert rejected.rejected_by == "admin"


def test_project_request_lifecycle_acl_and_name_conflicts(tmp_path):
    store = make_store(tmp_path)
    store.bootstrap_admin("admin password")
    store.register_user("alice", "alice password", "")
    store.approve_user("alice", actor="admin")

    request = store.create_project_request(
        requester="alice",
        base_name="cumcm_a",
        problem_path=str(tmp_path / "problem.pdf"),
        no_start=False,
        consult=True,
        existing_project_names=set(),
    )

    assert request.id > 0
    assert request.status == "pending"
    assert request.consult is True

    with pytest.raises(ProjectNameConflict):
        store.create_project_request(
            requester="alice",
            base_name="cumcm_a",
            problem_path=str(tmp_path / "problem2.pdf"),
            no_start=False,
            consult=False,
            existing_project_names=set(),
        )

    approved = store.approve_project_request(
        request.id,
        actor="admin",
        launched_base_name="cumcm_a",
        launch_output="created",
    )
    store.grant_project_owner("cumcm_a", "alice", actor="admin")

    assert approved.status == "approved"
    assert approved.launched_base_name == "cumcm_a"
    assert store.user_can_access_project("alice", "cumcm_a") is True
    assert store.user_can_access_project("bob", "cumcm_a") is False
    assert store.list_project_names_for_user("alice") == ["cumcm_a"]


def test_rejected_project_request_releases_name(tmp_path):
    store = make_store(tmp_path)
    store.bootstrap_admin("admin password")
    store.register_user("alice", "alice password", "")
    store.approve_user("alice", actor="admin")

    first = store.create_project_request(
        requester="alice",
        base_name="retry_name",
        problem_path="/tmp/problem.pdf",
        no_start=False,
        consult=False,
        existing_project_names=set(),
    )
    store.reject_project_request(first.id, actor="admin", reason="rename and retry")

    second = store.create_project_request(
        requester="alice",
        base_name="retry_name",
        problem_path="/tmp/problem.pdf",
        no_start=False,
        consult=False,
        existing_project_names=set(),
    )

    assert second.id != first.id
    assert second.status == "pending"
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `python3 -m pytest tests/test_auth_store.py -q`

Expected: FAIL with `ModuleNotFoundError` or missing `AuthStore`.

- [ ] **Step 3: Add `AUTH_DB_FILE` to settings**

In `web/backend/config.py`, add `auth_db_file` to the dataclass and load it from the environment:

```python
@dataclass(frozen=True)
class Settings:
    jwt_secret: str
    admin_password: str
    factory_root: Path = Path(__file__).resolve().parents[2]
    jwt_hours: int = 24
    auth_db_file: Path | None = None
    cors_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
    )

    @property
    def resolved_auth_db_file(self) -> Path:
        return self.auth_db_file or self.factory_root / "web" / "auth.db"
```

In `load_settings()`, pass:

```python
auth_db_env = (os.getenv("AUTH_DB_FILE") or "").strip()
auth_db_file=Path(auth_db_env) if auth_db_env else None,
```

Use `settings.resolved_auth_db_file` when constructing `AuthStore`; tests may pass `auth_db_file` directly.

- [ ] **Step 4: Create the SQLite auth store**

Create `web/backend/auth_store.py` with these public records and exceptions:

```python
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
```

Implement `AuthStore` with this schema and public API:

```python
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
            return existing
        password_hash = hash_password(admin_password)
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (
                    username, password_hash, role, status, display_name,
                    created_at, approved_at, approved_by
                ) VALUES (?, ?, 'admin', 'active', 'Administrator', ?, ?, 'system')
                """,
                ("admin", password_hash, now, now),
            )
            self._insert_audit(conn, "system", "user.bootstrap_admin", "user", "admin", {})
        return self.require_user("admin")

    def register_user(self, username: str, password: str, display_name: str = "") -> StoredUser:
        username = normalize_username(username)
        password_hash = hash_password(password)
        now = utc_now()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO users (username, password_hash, role, status, display_name, created_at)
                    VALUES (?, ?, 'user', 'pending', ?, ?)
                    """,
                    (username, password_hash, display_name.strip(), now),
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
            self._insert_audit(conn, requester, "project_request.create", "project_request", str(request_id), {"base_name": base_name})
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
            self._insert_audit(conn, actor, "project_request.approve", "project_request", str(request_id), {"base_name": launched_base_name})
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
            self._insert_audit(conn, actor, "project_request.reject", "project_request", str(request_id), {"reason": reason})
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
            self._insert_audit(conn, actor, "project_request.fail", "project_request", str(request_id), {"failure_reason": failure_reason})
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
            self._insert_audit(conn, actor, "project_acl.grant_owner", "project", base_name, {"username": username})

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
```

Use this schema:

```python
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
```

Use helper behavior:

```python
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
```

Implement `_connect()` with `sqlite3.Row`, `_insert_audit()` with `json.dumps(metadata, ensure_ascii=False, sort_keys=True)`, `_set_user_status()` so `active` writes `approved_at/approved_by`, `rejected` writes `rejected_at/rejected_by/rejection_reason`, and `disabled` only changes `status`; implement `_has_live_project_request()` so `pending` and `approved` conflict; implement row mappers returning the dataclasses above.

- [ ] **Step 5: Run the auth store tests**

Run: `python3 -m pytest tests/test_auth_store.py -q`

Expected: PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add web/backend/auth_store.py web/backend/config.py tests/test_auth_store.py
git commit -m "feat: add persistent auth store"
```

---

### Task 2: Persistent Login, Registration, And Admin User Approval

**Files:**
- Modify: `web/backend/schemas.py`
- Modify: `web/backend/auth.py`
- Modify: `web/backend/main.py`
- Modify: `tests/test_control_plane_auth.py`

- [ ] **Step 1: Add failing auth API tests**

Append to `tests/test_control_plane_auth.py`:

```python
import pytest

from web.backend.auth import create_access_token, get_current_user
from web.backend.auth_store import AuthStore
from web.backend.schemas import LoginRequest, RegisterRequest, UserDecisionRequest, UserInfo


def test_current_user_loads_active_user_from_auth_store(tmp_path):
    settings = Settings(
        jwt_secret="0123456789abcdef0123456789abcdef",
        admin_password="correct horse battery staple 42",
        factory_root=tmp_path,
        auth_db_file=tmp_path / "web" / "auth.db",
    )
    store = AuthStore(settings.resolved_auth_db_file)
    store.initialize()
    store.bootstrap_admin(settings.admin_password)

    token = create_access_token(settings, "admin", "admin")
    credentials = type("Credentials", (), {"credentials": token})()

    user = get_current_user(settings)(payload=None, credentials=credentials)

    assert user.username == "admin"
    assert user.role == "admin"
    assert user.status == "active"


def test_current_user_rejects_disabled_token_user(tmp_path):
    settings = Settings(
        jwt_secret="0123456789abcdef0123456789abcdef",
        admin_password="correct horse battery staple 42",
        factory_root=tmp_path,
        auth_db_file=tmp_path / "web" / "auth.db",
    )
    store = AuthStore(settings.resolved_auth_db_file)
    store.initialize()
    store.bootstrap_admin(settings.admin_password)
    store.disable_user("admin", actor="admin")
    token = create_access_token(settings, "admin", "admin")
    credentials = type("Credentials", (), {"credentials": token})()

    with pytest.raises(Exception) as excinfo:
        get_current_user(settings)(payload=None, credentials=credentials)

    assert getattr(excinfo.value, "status_code", None) == 401


def test_register_login_and_admin_approval_routes(tmp_path, monkeypatch):
    mod = load_main_module()
    settings = mod.settings.__class__(
        jwt_secret="0123456789abcdef0123456789abcdef",
        admin_password="correct horse battery staple 42",
        factory_root=tmp_path,
        auth_db_file=tmp_path / "web" / "auth.db",
    )
    monkeypatch.setattr(mod, "settings", settings)
    mod.auth_store = AuthStore(settings.resolved_auth_db_file)
    mod.auth_store.initialize()
    mod.auth_store.bootstrap_admin(settings.admin_password)

    registered = asyncio.run(mod.register_user(RegisterRequest(username="alice", password="alice password", display_name="Alice")))
    assert registered.username == "alice"
    assert registered.status == "pending"

    with pytest.raises(mod.HTTPException) as pending_login:
        asyncio.run(mod.login(LoginRequest(username="alice", password="alice password")))
    assert pending_login.value.status_code == 403
    assert pending_login.value.detail == "USER_PENDING"

    admin = UserInfo(username="admin", role="admin", status="active")
    approved = asyncio.run(mod.approve_user("alice", UserDecisionRequest(reason=""), current_user=admin))
    assert approved.status == "active"

    logged_in = asyncio.run(mod.login(LoginRequest(username="alice", password="alice password")))
    assert logged_in.username == "alice"
    assert logged_in.role == "user"
    assert logged_in.status == "active"
    assert logged_in.access_token
```

- [ ] **Step 2: Run the focused auth tests and confirm failure**

Run: `python3 -m pytest tests/test_control_plane_auth.py -q`

Expected: FAIL because `RegisterRequest`, `UserDecisionRequest`, `status`, and persistent route behavior do not exist.

- [ ] **Step 3: Extend schemas**

In `web/backend/schemas.py`, change and add:

```python
class UserInfo(BaseModel):
    username: str
    role: str
    status: str = "active"
    display_name: str = ""


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str
    status: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""


class UserResponse(BaseModel):
    username: str
    role: str
    status: str
    display_name: str = ""
    created_at: int = 0
    approved_at: int | None = None
    approved_by: str | None = None
    rejected_at: int | None = None
    rejected_by: str | None = None
    rejection_reason: str | None = None


class UserDecisionRequest(BaseModel):
    reason: str = ""
```

- [ ] **Step 4: Update token and current-user logic**

In `web/backend/auth.py`, import `AuthStore`, update `verify_password()` to accept string hashes, and make `get_current_user(settings)` verify the persisted user:

```python
def create_access_token(settings: Settings, username: str, role: str, status: str = "active") -> str:
    expiration = datetime.now(timezone.utc) + timedelta(hours=settings.jwt_hours)
    payload = {"sub": username, "role": role, "status": status, "exp": expiration}
    return jwt.encode(payload, settings.jwt_secret, algorithm=JWT_ALGORITHM)


def get_current_user(settings: Settings) -> Callable[[dict[str, Any]], UserInfo]:
    verify = require_token(settings)

    def _get_current_user(
        payload: dict[str, Any] | None = Depends(verify),
        credentials: HTTPAuthorizationCredentials | None = None,
    ) -> UserInfo:
        if payload is None and credentials is not None:
            payload = decode_token(settings, credentials.credentials)
        if payload is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
        store = AuthStore(settings.resolved_auth_db_file)
        store.initialize()
        user = store.get_user(payload["sub"])
        if user is None or user.status != "active":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="USER_NOT_ACTIVE")
        return UserInfo(username=user.username, role=user.role, status=user.status, display_name=user.display_name or "")

    return _get_current_user
```

Keep `user_db(settings)` for backward-compatible tests, but do not use it in `main.login`.

- [ ] **Step 5: Initialize auth store and replace auth routes**

In `web/backend/main.py`, add:

```python
from .auth_store import AuthStore, InvalidUsername, UserExists
from .schemas import RegisterRequest, UserDecisionRequest, UserResponse
```

After settings validation:

```python
auth_store = AuthStore(settings.resolved_auth_db_file)
auth_store.initialize()
auth_store.bootstrap_admin(settings.admin_password)
```

Add helpers:

```python
def _user_response(user) -> UserResponse:
    return UserResponse(
        username=user.username,
        role=user.role,
        status=user.status,
        display_name=user.display_name or "",
        created_at=user.created_at,
        approved_at=user.approved_at,
        approved_by=user.approved_by,
        rejected_at=user.rejected_at,
        rejected_by=user.rejected_by,
        rejection_reason=user.rejection_reason,
    )


def _require_admin(current_user: UserInfo) -> None:
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="ADMIN_REQUIRED")
```

Replace login body with:

```python
@app.post("/api/auth/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    user = auth_store.get_user(request.username)
    if not user or not auth_store.verify_user_password(request.username, request.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect username or password")
    if user.status == "pending":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="USER_PENDING")
    if user.status == "rejected":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="USER_REJECTED")
    if user.status == "disabled":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="USER_DISABLED")

    access_token = create_access_token(settings, user.username, user.role, user.status)
    return LoginResponse(access_token=access_token, username=user.username, role=user.role, status=user.status)
```

Add routes:

```python
@app.post("/api/auth/register", response_model=UserResponse)
async def register_user(request: RegisterRequest):
    try:
        user = auth_store.register_user(request.username, request.password, request.display_name)
    except UserExists as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="USER_EXISTS") from exc
    except InvalidUsername as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="INVALID_USERNAME") from exc
    return _user_response(user)


@app.get("/api/admin/users", response_model=list[UserResponse])
async def list_users(current_user: UserInfo = Depends(get_current_user(settings))):
    _require_admin(current_user)
    return [_user_response(user) for user in auth_store.list_users()]


@app.post("/api/admin/users/{username}/approve", response_model=UserResponse)
async def approve_user(
    username: str,
    decision: UserDecisionRequest = UserDecisionRequest(),
    current_user: UserInfo = Depends(get_current_user(settings)),
):
    del decision
    _require_admin(current_user)
    return _user_response(auth_store.approve_user(username, actor=current_user.username))


@app.post("/api/admin/users/{username}/reject", response_model=UserResponse)
async def reject_user(
    username: str,
    decision: UserDecisionRequest,
    current_user: UserInfo = Depends(get_current_user(settings)),
):
    _require_admin(current_user)
    return _user_response(auth_store.reject_user(username, actor=current_user.username, reason=decision.reason))


@app.post("/api/admin/users/{username}/disable", response_model=UserResponse)
async def disable_user(
    username: str,
    decision: UserDecisionRequest = UserDecisionRequest(),
    current_user: UserInfo = Depends(get_current_user(settings)),
):
    del decision
    _require_admin(current_user)
    return _user_response(auth_store.disable_user(username, actor=current_user.username))
```

Update `/api/auth/me` and `/api/auth/ws-ticket` to include role/status:

```python
@app.post("/api/auth/ws-ticket", response_model=WsTicketResponse)
async def issue_ws_ticket(current_user: UserInfo = Depends(get_current_user(settings))):
    ticket = ticket_store.issue({"sub": current_user.username, "role": current_user.role, "status": current_user.status})
    return WsTicketResponse(ticket=ticket)
```

- [ ] **Step 6: Run auth tests**

Run: `python3 -m pytest tests/test_auth_store.py tests/test_control_plane_auth.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 2**

```bash
git add web/backend/schemas.py web/backend/auth.py web/backend/main.py tests/test_control_plane_auth.py
git commit -m "feat: add registration and admin user approval"
```

---

### Task 3: Access Control For Projects And Global Model Configuration

**Files:**
- Create: `web/backend/access_control.py`
- Modify: `web/backend/project_api.py`
- Modify: `web/backend/main.py`
- Modify: `tests/test_web_control_plane_api.py`

- [ ] **Step 1: Add failing project ACL and model permission tests**

First update `load_main_module()` in `tests/test_web_control_plane_api.py` so router endpoints are callable under the fake FastAPI used by these tests:

```python
def load_main_module(factory_root=None, auth_db_file=None):
    sys.modules.pop("web.backend.main", None)
    sys.modules.pop("fastapi", None)
    sys.modules.pop("fastapi.middleware.cors", None)
    sys.modules.pop("fastapi.responses", None)
    sys.modules.pop("fastapi.security", None)
    sys.modules.pop("pydantic", None)
    sys.modules.pop("dotenv", None)

    os.environ["JWT_SECRET"] = "0123456789abcdef0123456789abcdef"
    os.environ["ADMIN_PASSWORD"] = "strong-password"
    if factory_root is not None:
        os.environ["FACTORY_ROOT"] = str(factory_root)
    else:
        os.environ.pop("FACTORY_ROOT", None)
    if auth_db_file is not None:
        os.environ["AUTH_DB_FILE"] = str(auth_db_file)
    else:
        os.environ.pop("AUTH_DB_FILE", None)

    fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code=None, detail=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class DummyRoute:
        def __init__(self, path, endpoint):
            self.path = path
            self.endpoint = endpoint

    class DummyFastAPI:
        def __init__(self, *args, **kwargs):
            self.routes = []

        def add_middleware(self, *args, **kwargs):
            return None

        def include_router(self, router, *args, **kwargs):
            self.routes.extend(getattr(router, "routes", []))
            return None

        def _route(self, path, *args, **kwargs):
            def decorator(fn):
                self.routes.append(DummyRoute(path, fn))
                return fn
            return decorator

        def get(self, path, *args, **kwargs):
            return self._route(path, *args, **kwargs)

        def post(self, path, *args, **kwargs):
            return self._route(path, *args, **kwargs)

        def put(self, path, *args, **kwargs):
            return self._route(path, *args, **kwargs)

        def websocket(self, path, *args, **kwargs):
            return self._route(path, *args, **kwargs)
```

Keep the rest of `load_main_module()` module stubs, then append these tests to `tests/test_web_control_plane_api.py`:

```python
def _make_project(root, name):
    project = root / "ongoing" / name
    project.mkdir(parents=True)
    (project / "checkpoint.md").write_text("- **Last completed step**: 1\n", encoding="utf-8")
    write_status(
        project,
        state="running",
        current_step=2,
        current_action="agent_run",
        display_status="Running",
        updated_at=1700000000,
    )
    return project


def _install_auth_store(mod):
    settings = mod.settings
    mod.auth_store = mod.AuthStore(settings.resolved_auth_db_file)
    mod.auth_store.initialize()
    mod.auth_store.bootstrap_admin(settings.admin_password)
    mod.auth_store.register_user("alice", "alice password", "")
    mod.auth_store.approve_user("alice", actor="admin")
    mod.auth_store.register_user("bob", "bob password", "")
    mod.auth_store.approve_user("bob", actor="admin")
    return settings


def test_user_project_list_is_filtered_by_acl(tmp_path):
    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    _install_auth_store(mod)
    _make_project(tmp_path, "owned")
    _make_project(tmp_path, "other")
    mod.auth_store.grant_project_owner("owned", "alice", actor="admin")

    alice = mod.UserInfo(username="alice", role="user", status="active")
    projects = asyncio.run(mod.get_projects(current_user=alice))

    assert [project.base_name for project in projects] == ["owned"]


def test_non_owner_project_file_returns_404(tmp_path):
    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    _install_auth_store(mod)
    _make_project(tmp_path, "owned")
    _make_project(tmp_path, "other")
    mod.auth_store.grant_project_owner("owned", "alice", actor="admin")
    alice = mod.UserInfo(username="alice", role="user", status="active")

    with pytest.raises(mod.HTTPException) as excinfo:
        asyncio.run(mod.get_checkpoint("other", current_user=alice))

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "PROJECT_NOT_FOUND"


def test_admin_can_see_all_projects(tmp_path):
    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    _install_auth_store(mod)
    _make_project(tmp_path, "owned")
    _make_project(tmp_path, "other")
    admin = mod.UserInfo(username="admin", role="admin", status="active")

    projects = asyncio.run(mod.get_projects(current_user=admin))

    assert sorted(project.base_name for project in projects) == ["other", "owned"]


def test_model_registry_is_admin_only_but_owner_can_save_project_config(tmp_path):
    from web.backend.schemas import ModelConfigPayload, ModelRegistryPayload, StepAssignment

    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    _install_auth_store(mod)
    _make_project(tmp_path, "owned")
    mod.auth_store.grant_project_owner("owned", "alice", actor="admin")
    alice = mod.UserInfo(username="alice", role="user", status="active")

    with pytest.raises(mod.HTTPException) as registry_error:
        asyncio.run(mod.put_model_registry(ModelRegistryPayload(models=[]), current_user=alice))
    assert registry_error.value.status_code == 403

    payload = ModelConfigPayload(scope="owned", steps={"step_7": StepAssignment(primary="claude")})
    saved = asyncio.run(mod.put_model_config(payload, current_user=alice))
    assert saved["status"] == "ok"

    default_payload = ModelConfigPayload(scope="_default", steps={"step_7": StepAssignment(primary="claude")})
    with pytest.raises(mod.HTTPException) as default_error:
        asyncio.run(mod.put_model_config(default_payload, current_user=alice))
    assert default_error.value.status_code == 403
```

- [ ] **Step 2: Run the focused API tests and confirm failure**

Run: `python3 -m pytest tests/test_web_control_plane_api.py -q`

Expected: FAIL because list filtering and route permission checks are not implemented.

- [ ] **Step 3: Create access control helpers**

Create `web/backend/access_control.py`:

```python
from __future__ import annotations

from collections.abc import Iterable

from fastapi import HTTPException, status

from .auth_store import AuthStore
from .config import Settings
from .schemas import ProjectStatus, UserInfo


def get_store(settings: Settings) -> AuthStore:
    store = AuthStore(settings.resolved_auth_db_file)
    store.initialize()
    return store


def require_admin(user: UserInfo) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="ADMIN_REQUIRED")


def require_active(user: UserInfo) -> None:
    if user.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="USER_NOT_ACTIVE")


def can_manage_project(settings: Settings, user: UserInfo, base_name: str) -> bool:
    require_active(user)
    if user.role == "admin":
        return True
    return get_store(settings).user_can_access_project(user.username, base_name)


def require_project_access(settings: Settings, user: UserInfo, base_name: str) -> None:
    if not can_manage_project(settings, user, base_name):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")


def filter_visible_projects(settings: Settings, user: UserInfo, projects: Iterable[ProjectStatus]) -> list[ProjectStatus]:
    require_active(user)
    project_list = list(projects)
    if user.role == "admin":
        return project_list
    allowed = set(get_store(settings).list_project_names_for_user(user.username))
    return [project for project in project_list if project.base_name in allowed]
```

- [ ] **Step 4: Apply project access checks in `project_api.py`**

Import helpers:

```python
from .access_control import filter_visible_projects, require_admin, require_project_access
from .auth_store import AuthStore
```

At the start of every route that takes `base_name`, before reading files or running actions, call:

```python
require_project_access(settings, current_user, base_name)
```

Routes that need this check:

- `/api/projects/{base_name}/status`
- `/api/projects/{base_name}/diagnostics`
- `/api/projects/{base_name}/checkpoint`
- `/api/projects/{base_name}/logs`
- `/api/projects/{base_name}/steps`
- `/api/projects/{base_name}/files`
- `/api/projects/{base_name}/file`
- `/api/projects/{base_name}/raw`
- `/api/projects/{base_name}/paper`
- `/api/projects/{base_name}/consultation`
- `/api/projects/{base_name}/consultation/answer`
- `/api/projects/{base_name}/modeling-directions`
- `/api/projects/{base_name}/modeling-directions/selection`
- `/api/projects/{base_name}/action`

Change project list route:

```python
@router.get("/api/projects", response_model=list[ProjectStatus])
async def get_projects(current_user: UserInfo = Depends(get_current_user(settings))):
    return filter_visible_projects(settings, current_user, list_all_projects(settings))
```

Change model routes by inserting these checks at the start of the existing handlers:

```python
# First executable line inside put_model_registry()
require_admin(current_user)
```

```python
# Immediately after `scope = payload.scope.strip()` inside put_model_config()
if current_user.role != "admin":
    if scope == "_default":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="ADMIN_REQUIRED")
    require_project_access(settings, current_user, scope)
```

Keep `GET /api/models` available to active users because workspace model selectors need the registry; only writes are restricted.

- [ ] **Step 5: Update `main.py` compatibility wrappers**

The standalone compatibility `get_projects()` in `web/backend/main.py` must also filter:

```python
async def get_projects(current_user: UserInfo = Depends(get_current_user(settings))):
    from .access_control import filter_visible_projects

    return filter_visible_projects(settings, current_user, list_all_projects(settings))
```

The top-level `project_action()` compatibility route must call:

```python
from .access_control import require_project_access
require_project_access(settings, current_user, base_name)
```

- [ ] **Step 6: Run API permission tests**

Run: `python3 -m pytest tests/test_auth_store.py tests/test_control_plane_auth.py tests/test_web_control_plane_api.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 3**

```bash
git add web/backend/access_control.py web/backend/project_api.py web/backend/main.py tests/test_web_control_plane_api.py
git commit -m "feat: enforce project ownership permissions"
```

---

### Task 4: Project Request Submission And Admin Approval Launch

**Files:**
- Modify: `web/backend/schemas.py`
- Modify: `web/backend/project_api.py`
- Modify: `web/backend/main.py`
- Modify: `tests/test_web_control_plane_api.py`

- [ ] **Step 1: Add failing project request API tests**

Append to `tests/test_web_control_plane_api.py`:

```python
def test_user_new_project_requires_project_request(tmp_path):
    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    _install_auth_store(mod)
    problem = tmp_path / "problem.pdf"
    problem.write_text("problem", encoding="utf-8")
    alice = mod.UserInfo(username="alice", role="user", status="active")

    with pytest.raises(mod.HTTPException) as excinfo:
        asyncio.run(
            mod.create_new_project(
                mod.NewProjectRequest(base_name="alice_project", problem_path=str(problem)),
                current_user=alice,
            )
        )

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "PROJECT_APPROVAL_REQUIRED"


def test_user_can_submit_and_list_own_project_request(tmp_path):
    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    _install_auth_store(mod)
    problem = tmp_path / "problem.pdf"
    problem.write_text("problem", encoding="utf-8")
    alice = mod.UserInfo(username="alice", role="user", status="active")

    created = asyncio.run(
        mod.create_project_request(
            mod.ProjectRequestCreate(base_name="alice_project", problem_path=str(problem), no_start=False, consult=True),
            current_user=alice,
        )
    )
    listed = asyncio.run(mod.list_project_requests(current_user=alice))

    assert created.status == "pending"
    assert created.requester == "alice"
    assert [item.id for item in listed] == [created.id]


def test_admin_approves_project_request_launches_and_grants_acl(tmp_path, monkeypatch):
    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    _install_auth_store(mod)
    problem = tmp_path / "problem.pdf"
    problem.write_text("problem", encoding="utf-8")
    alice = mod.UserInfo(username="alice", role="user", status="active")
    admin = mod.UserInfo(username="admin", role="admin", status="active")
    created = asyncio.run(
        mod.create_project_request(
            mod.ProjectRequestCreate(base_name="alice_project", problem_path=str(problem), no_start=False, consult=False),
            current_user=alice,
        )
    )

    class DummyResult:
        returncode = 0
        stdout = "created alice_project"
        stderr = ""

    calls = []
    monkeypatch.setattr(mod.project_api, "run_project_launcher", lambda settings, request: calls.append(request.base_name) or DummyResult())
    approved = asyncio.run(mod.approve_project_request(created.id, mod.ProjectRequestDecision(note="ok"), current_user=admin))

    assert approved.status == "approved"
    assert calls == ["alice_project"]
    assert mod.auth_store.user_can_access_project("alice", "alice_project") is True


def test_failed_project_request_launch_marks_failed(tmp_path, monkeypatch):
    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    _install_auth_store(mod)
    problem = tmp_path / "problem.pdf"
    problem.write_text("problem", encoding="utf-8")
    alice = mod.UserInfo(username="alice", role="user", status="active")
    admin = mod.UserInfo(username="admin", role="admin", status="active")
    created = asyncio.run(
        mod.create_project_request(
            mod.ProjectRequestCreate(base_name="bad_project", problem_path=str(problem)),
            current_user=alice,
        )
    )

    class DummyResult:
        returncode = 2
        stdout = ""
        stderr = "launcher failed"

    monkeypatch.setattr(mod.project_api, "run_project_launcher", lambda settings, request: DummyResult())

    with pytest.raises(mod.HTTPException) as excinfo:
        asyncio.run(mod.approve_project_request(created.id, mod.ProjectRequestDecision(note="ok"), current_user=admin))

    assert excinfo.value.status_code == 500
    failed = mod.auth_store.require_project_request(created.id)
    assert failed.status == "failed"
    assert "launcher failed" in failed.failure_reason
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `python3 -m pytest tests/test_web_control_plane_api.py -q`

Expected: FAIL because project request schemas and routes do not exist.

- [ ] **Step 3: Add project request schemas**

In `web/backend/schemas.py`, add:

```python
class ProjectRequestCreate(BaseModel):
    base_name: str
    problem_path: str
    no_start: bool = False
    consult: bool = False

    @field_validator("base_name")
    @classmethod
    def _check_base_name(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("base_name 仅允许字母、数字、下划线、连字符")
        return value


class ProjectRequestDecision(BaseModel):
    note: str = ""


class ProjectRequestResponse(BaseModel):
    id: int
    requester: str
    base_name: str
    problem_path: str
    no_start: bool = False
    consult: bool = False
    status: str
    created_at: int
    decided_at: int | None = None
    decided_by: str | None = None
    decision_note: str | None = None
    launched_at: int | None = None
    launched_base_name: str | None = None
    launch_output: str | None = None
    failure_reason: str | None = None
```

- [ ] **Step 4: Add launcher helper and project request routes**

In `web/backend/project_api.py`, import the new schemas and store exceptions. Add helpers outside `create_project_router()`:

```python
def _project_request_response(record) -> ProjectRequestResponse:
    return ProjectRequestResponse(
        id=record.id,
        requester=record.requester,
        base_name=record.base_name,
        problem_path=record.problem_path,
        no_start=record.no_start,
        consult=record.consult,
        status=record.status,
        created_at=record.created_at,
        decided_at=record.decided_at,
        decided_by=record.decided_by,
        decision_note=record.decision_note,
        launched_at=record.launched_at,
        launched_base_name=record.launched_base_name,
        launch_output=record.launch_output,
        failure_reason=record.failure_reason,
    )


def existing_project_names(settings: Settings) -> set[str]:
    names = set()
    for root in (settings.ongoing_dir, settings.complete_dir):
        if root.is_dir():
            names.update(path.name for path in root.iterdir() if path.is_dir())
    return names


def run_project_launcher(settings: Settings, request: ProjectRequestCreate | NewProjectRequest):
    problem_path = Path(request.problem_path)
    cmd = ["/usr/bin/bash", str(settings.launch_script), "new"]
    if request.no_start:
        cmd.append("--no-start")
    if request.consult:
        cmd.append("--consult")
    cmd.extend([request.base_name, str(problem_path.resolve())])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=settings.factory_root,
        env={**os.environ, "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
        check=False,
    )
```

Inside `create_project_router()`, set:

```python
    store = AuthStore(settings.resolved_auth_db_file)
    store.initialize()
```

Add routes:

```python
    @router.get("/api/project-requests", response_model=list[ProjectRequestResponse])
    async def list_project_requests(current_user: UserInfo = Depends(get_current_user(settings))):
        if current_user.role == "admin":
            records = store.list_project_requests()
        else:
            records = store.list_project_requests(current_user.username)
        return [_project_request_response(record) for record in records]

    @router.post("/api/project-requests", response_model=ProjectRequestResponse)
    async def create_project_request(
        request: ProjectRequestCreate,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        if current_user.role == "admin":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ADMIN_USE_DIRECT_CREATE")
        problem_path = Path(request.problem_path)
        if not problem_path.exists():
            raise HTTPException(status_code=400, detail=f"Problem file not found: {request.problem_path}")
        try:
            record = store.create_project_request(
                requester=current_user.username,
                base_name=request.base_name,
                problem_path=str(problem_path),
                no_start=request.no_start,
                consult=request.consult,
                existing_project_names=existing_project_names(settings),
            )
        except ProjectNameConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="PROJECT_NAME_EXISTS") from exc
        await manager.broadcast({"type": "project_request_created", "request_id": record.id, "requester": current_user.username})
        return _project_request_response(record)

    @router.post("/api/admin/project-requests/{request_id}/approve", response_model=ProjectRequestResponse)
    async def approve_project_request(
        request_id: int,
        decision: ProjectRequestDecision,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        require_admin(current_user)
        record = store.require_project_request(request_id)
        launcher_payload = ProjectRequestCreate(
            base_name=record.base_name,
            problem_path=record.problem_path,
            no_start=record.no_start,
            consult=record.consult,
        )
        result = run_project_launcher(settings, launcher_payload)
        if result.returncode != 0:
            failed = store.mark_project_request_failed(
                request_id,
                actor=current_user.username,
                failure_reason=result.stderr or result.stdout or "project launch failed",
            )
            await manager.broadcast({"type": "project_request_failed", "request_id": request_id})
            raise HTTPException(status_code=500, detail=failed.failure_reason)
        approved = store.approve_project_request(
            request_id,
            actor=current_user.username,
            launched_base_name=record.base_name,
            launch_output=result.stdout,
        )
        store.grant_project_owner(record.base_name, record.requester, actor=current_user.username)
        await manager.broadcast({"type": "project_created", "project": record.base_name, "user": record.requester})
        return _project_request_response(approved)

    @router.post("/api/admin/project-requests/{request_id}/reject", response_model=ProjectRequestResponse)
    async def reject_project_request(
        request_id: int,
        decision: ProjectRequestDecision,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        require_admin(current_user)
        rejected = store.reject_project_request(request_id, actor=current_user.username, reason=decision.note)
        await manager.broadcast({"type": "project_request_rejected", "request_id": request_id})
        return _project_request_response(rejected)
```

Change `/api/projects/new`:

```python
        if current_user.role != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="PROJECT_APPROVAL_REQUIRED")
        result = run_project_launcher(settings, request)
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Failed to create project: {result.stderr}")
```

- [ ] **Step 5: Re-export project request endpoints from `main.py`**

After existing router endpoint exports, add:

```python
list_project_requests = _router_endpoint(project_router, "/api/project-requests")
create_project_request = _router_endpoint(project_router, "/api/project-requests")
approve_project_request = _router_endpoint(project_router, "/api/admin/project-requests/{request_id}/approve")
reject_project_request = _router_endpoint(project_router, "/api/admin/project-requests/{request_id}/reject")
project_api = __import__("web.backend.project_api", fromlist=["run_project_launcher"])
```

Keep the existing `create_new_project` export.

- [ ] **Step 6: Run request workflow tests**

Run: `python3 -m pytest tests/test_auth_store.py tests/test_web_control_plane_api.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 4**

```bash
git add web/backend/schemas.py web/backend/project_api.py web/backend/main.py tests/test_web_control_plane_api.py
git commit -m "feat: add project approval workflow"
```

---

### Task 5: Cloud And WebSocket Permission Filtering

**Files:**
- Modify: `web/backend/cloud_api.py`
- Modify: `web/backend/ws.py`
- Modify: `web/backend/main.py`
- Modify: `tests/test_web_control_plane_api.py`

- [ ] **Step 1: Add failing cloud and WS permission tests**

Append to `tests/test_web_control_plane_api.py`:

```python
def test_cloud_global_config_is_admin_only_and_project_config_requires_owner(tmp_path):
    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    _install_auth_store(mod)
    _make_project(tmp_path, "owned")
    mod.auth_store.grant_project_owner("owned", "alice", actor="admin")
    alice = mod.UserInfo(username="alice", role="user", status="active")
    bob = mod.UserInfo(username="bob", role="user", status="active")

    with pytest.raises(mod.HTTPException) as global_error:
        asyncio.run(mod.cloud_config(current_user=alice))
    assert global_error.value.status_code == 403

    owned = asyncio.run(mod.project_cloud_config("owned", current_user=alice))
    assert owned["enabled"] is False

    with pytest.raises(mod.HTTPException) as owner_error:
        asyncio.run(mod.project_cloud_config("owned", current_user=bob))
    assert owner_error.value.status_code == 404


class RecordingWebSocket:
    def __init__(self, ticket):
        self.query_params = {"ticket": ticket}
        self.accepted = False
        self.closed = None
        self.sent = []
        self._sends_before_stop = 1

    async def accept(self):
        self.accepted = True

    async def close(self, code=None):
        self.closed = code

    async def send_json(self, payload):
        self.sent.append(payload)
        self._sends_before_stop -= 1
        if self._sends_before_stop <= 0:
            raise RuntimeError("stop websocket test")


def test_websocket_filters_status_update_projects_by_ticket_user(tmp_path):
    mod = load_main_module(factory_root=tmp_path, auth_db_file=tmp_path / "web" / "auth.db")
    _install_auth_store(mod)
    _make_project(tmp_path, "owned")
    _make_project(tmp_path, "other")
    mod.auth_store.grant_project_owner("owned", "alice", actor="admin")

    ticket = mod.ticket_store.issue({"sub": "alice", "role": "user", "status": "active"})
    websocket = RecordingWebSocket(ticket)

    with pytest.raises(RuntimeError, match="stop websocket test"):
        asyncio.run(mod.websocket_endpoint(websocket))

    assert websocket.accepted is True
    status_messages = [msg for msg in websocket.sent if msg.get("type") == "status_update"]
    assert status_messages
    assert [item["base_name"] for item in status_messages[0]["projects"]] == ["owned"]
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `python3 -m pytest tests/test_web_control_plane_api.py -q`

Expected: FAIL because cloud and WebSocket filtering are not implemented.

- [ ] **Step 3: Apply cloud route permissions**

In `web/backend/cloud_api.py`, import:

```python
from .access_control import require_admin, require_project_access
```

Then insert permission checks as the first executable line in these existing routes:

```python
# cloud_status()
require_admin(current_user)
```

```python
# cloud_config()
require_admin(current_user)
```

```python
# get_project_cloud_config(), before project_cloud_config(settings, base_name)
require_project_access(settings, current_user, base_name)
```

```python
# enable_cloud_solver(), before set_project_cloud_enabled(settings, base_name, True)
require_project_access(settings, current_user, base_name)
```

```python
# disable_cloud_solver(), before set_project_cloud_enabled(settings, base_name, False)
require_project_access(settings, current_user, base_name)
```

- [ ] **Step 4: Track WS connection identities and filter project lists**

In `web/backend/ws.py`, change `ConnectionManager`:

```python
class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: dict[WebSocket, dict] = {}

    async def connect(self, websocket: WebSocket, payload: dict | None = None) -> None:
        await websocket.accept()
        self.active_connections[websocket] = payload or {}

    def disconnect(self, websocket: WebSocket) -> None:
        self.active_connections.pop(websocket, None)

    async def broadcast(self, message: dict) -> None:
        stale: list[WebSocket] = []
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                stale.append(connection)
        for connection in stale:
            self.disconnect(connection)

    def connections(self) -> list[tuple[WebSocket, dict]]:
        return list(self.active_connections.items())
```

Add helper:

```python
def _payload_user(payload: dict) -> UserInfo:
    return UserInfo(
        username=payload.get("sub", ""),
        role=payload.get("role", "user"),
        status=payload.get("status", "active"),
    )
```

In `create_monitor_task()`, replace the broadcasted full list with per-connection filtered sends:

```python
                projects = list_all_projects(settings)
                current_state = {project.base_name: project.dict() for project in projects}
                for websocket, payload in manager.connections():
                    user = _payload_user(payload)
                    visible = filter_visible_projects(settings, user, projects)
                    await websocket.send_json(
                        {"type": "status_update", "projects": [project.dict() for project in visible]}
                    )
                    for base_name, state in current_state.items():
                        if last_state.get(base_name) != state and any(project.base_name == base_name for project in visible):
                            await websocket.send_json({"type": "project_updated", "project": base_name, "status": state})
```

In `create_ws_router()`, replace `await manager.connect(websocket)` and the unfiltered send loop with:

```python
        await manager.connect(websocket, payload)
        user = _payload_user(payload)
```

```python
                user = _payload_user(payload)
                projects = filter_visible_projects(settings, user, list_all_projects(settings))
                await websocket.send_json({"type": "status_update", "projects": [project.dict() for project in projects]})
```

Keep `manager.broadcast()` for generic invalidation messages; the frontend responds to those by refetching filtered HTTP lists.

- [ ] **Step 5: Update test doubles affected by `connect(websocket, payload)`**

In `tests/test_control_plane_auth.py`, update `DummyManager.connect`:

```python
    async def connect(self, websocket, payload=None):
        del payload
        self.connected = True
        await websocket.accept()
```

- [ ] **Step 6: Run permission tests**

Run: `python3 -m pytest tests/test_control_plane_auth.py tests/test_web_control_plane_api.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 5**

```bash
git add web/backend/cloud_api.py web/backend/ws.py tests/test_control_plane_auth.py tests/test_web_control_plane_api.py
git commit -m "feat: filter realtime and cloud access by role"
```

---

### Task 6: Frontend API Contracts And Auth State

**Files:**
- Modify: `web/frontend/src/lib/contracts.js`
- Modify: `web/frontend/src/lib/api.js`
- Modify: `web/frontend/src/composables/useAuth.js`
- Modify: `tests/test_web_frontend_runtime_helpers.py`

- [ ] **Step 1: Add failing frontend helper tests**

Append to `tests/test_web_frontend_runtime_helpers.py`:

```python
def test_frontend_auth_and_project_request_contracts():
    result = run_node(
        """
import assert from 'node:assert/strict'
import {
  normalizeAuthUser,
  normalizeProjectRequest,
} from './web/frontend/src/lib/contracts.js'

assert.deepEqual(normalizeAuthUser({ username: 'alice', role: 'user', status: 'active', display_name: 'Alice' }), {
  username: 'alice',
  role: 'user',
  status: 'active',
  display_name: 'Alice',
})
assert.equal(normalizeAuthUser({ username: 'admin' }).role, 'user')
assert.deepEqual(normalizeProjectRequest({ id: '7', requester: 'alice', base_name: 'demo', consult: 1 }), {
  id: 7,
  requester: 'alice',
  base_name: 'demo',
  problem_path: '',
  no_start: false,
  consult: true,
  status: 'pending',
  created_at: 0,
  decided_at: null,
  decided_by: null,
  decision_note: null,
  launched_at: null,
  launched_base_name: null,
  launch_output: null,
  failure_reason: null,
})
"""
    )

    assert result.returncode == 0, result.stderr


def test_use_auth_tracks_role_and_status_from_login_and_bootstrap():
    result = run_node(
        """
import assert from 'node:assert/strict'
globalThis.localStorage = {
  data: new Map(),
  getItem(key) { return this.data.get(key) || null },
  setItem(key, value) { this.data.set(key, String(value)) },
  removeItem(key) { this.data.delete(key) },
}
const api = await import('./web/frontend/src/lib/api.js')
api.__setAuthMeForTest(async () => ({ username: 'alice', role: 'user', status: 'active' }))
const { useAuth } = await import('./web/frontend/src/composables/useAuth.js')

const auth = useAuth()
auth.login({ username: 'alice', role: 'user', status: 'active' })
assert.equal(auth.username.value, 'alice')
assert.equal(auth.role.value, 'user')
assert.equal(auth.status.value, 'active')

localStorage.setItem('access_token', 'token')
localStorage.setItem('username', 'alice')
const ok = await auth.bootstrap()
assert.equal(ok, true)
assert.equal(auth.role.value, 'user')
auth.logout()
assert.equal(auth.isAuthenticated.value, false)
assert.equal(auth.role.value, '')
"""
    )

    assert result.returncode == 0, result.stderr
```

- [ ] **Step 2: Run helper tests and confirm failure**

Run: `python3 -m pytest tests/test_web_frontend_runtime_helpers.py -q`

Expected: FAIL because normalizers and auth state fields do not exist.

- [ ] **Step 3: Add frontend normalizers**

In `web/frontend/src/lib/contracts.js`, add:

```javascript
export function normalizeAuthUser(raw = {}) {
  return {
    username: String(raw.username || ''),
    role: String(raw.role || 'user'),
    status: String(raw.status || ''),
    display_name: String(raw.display_name || ''),
  }
}

export function normalizeProjectRequest(raw = {}) {
  return {
    id: numberOr(raw.id, 0),
    requester: String(raw.requester || ''),
    base_name: String(raw.base_name || ''),
    problem_path: String(raw.problem_path || ''),
    no_start: Boolean(raw.no_start),
    consult: Boolean(raw.consult),
    status: String(raw.status || 'pending'),
    created_at: numberOr(raw.created_at, 0),
    decided_at: raw.decided_at === undefined || raw.decided_at === null || raw.decided_at === '' ? null : numberOr(raw.decided_at, null),
    decided_by: stringOrNull(raw.decided_by),
    decision_note: stringOrNull(raw.decision_note),
    launched_at: raw.launched_at === undefined || raw.launched_at === null || raw.launched_at === '' ? null : numberOr(raw.launched_at, null),
    launched_base_name: stringOrNull(raw.launched_base_name),
    launch_output: stringOrNull(raw.launch_output),
    failure_reason: stringOrNull(raw.failure_reason),
  }
}
```

- [ ] **Step 4: Add API helpers**

In `web/frontend/src/lib/api.js`, import the new normalizers and add test injection support:

```javascript
import {
  normalizeArtifact,
  normalizeAuthUser,
  normalizeCloudConfig,
  normalizeProjectRequest,
  normalizeProjectStatus,
  normalizeStepsPayload,
} from './contracts.js'

let authMeOverride = null
export function __setAuthMeForTest(fn) { authMeOverride = fn }
```

Change auth helpers:

```javascript
export async function authLogin(username, password) {
  const { data } = await api.post('/api/auth/login', { username, password })
  return Object.assign({}, data, normalizeAuthUser(data))
}
export async function authRegister(payload) {
  const { data } = await api.post('/api/auth/register', payload)
  return normalizeAuthUser(data)
}
export async function authMe() {
  if (authMeOverride) return normalizeAuthUser(await authMeOverride())
  const { data } = await api.get('/api/auth/me')
  return normalizeAuthUser(data)
}
```

Add admin and project request clients:

```javascript
export const AdminUsers = {
  list: () => api.get('/api/admin/users').then((r) => (Array.isArray(r.data) ? r.data.map(normalizeAuthUser) : [])),
  approve: (username, reason = '') => api.post(`/api/admin/users/${username}/approve`, { reason }).then((r) => normalizeAuthUser(r.data)),
  reject: (username, reason = '') => api.post(`/api/admin/users/${username}/reject`, { reason }).then((r) => normalizeAuthUser(r.data)),
  disable: (username, reason = '') => api.post(`/api/admin/users/${username}/disable`, { reason }).then((r) => normalizeAuthUser(r.data)),
}

export const ProjectRequests = {
  list: () => api.get('/api/project-requests').then((r) => (Array.isArray(r.data) ? r.data.map(normalizeProjectRequest) : [])),
  create: (payload) => api.post('/api/project-requests', payload).then((r) => normalizeProjectRequest(r.data)),
  approve: (id, note = '') => api.post(`/api/admin/project-requests/${id}/approve`, { note }).then((r) => normalizeProjectRequest(r.data)),
  reject: (id, note = '') => api.post(`/api/admin/project-requests/${id}/reject`, { note }).then((r) => normalizeProjectRequest(r.data)),
}
```

- [ ] **Step 5: Track role/status in `useAuth.js`**

Change `web/frontend/src/composables/useAuth.js` to:

```javascript
import { computed, ref } from 'vue'
import { authMe } from '../lib/api.js'

const isAuthenticated = ref(false)
const username = ref('')
const role = ref('')
const status = ref('')
const displayName = ref('')
const isAdmin = computed(() => role.value === 'admin')

function applyUser(data) {
  username.value = data.username || ''
  role.value = data.role || ''
  status.value = data.status || ''
  displayName.value = data.display_name || ''
}

export function useAuth() {
  async function bootstrap() {
    const token = localStorage.getItem('access_token')
    const storedUser = localStorage.getItem('username')
    if (!token || !storedUser) return false
    const me = await authMe()
    applyUser(me)
    isAuthenticated.value = true
    return true
  }

  function login(data) {
    isAuthenticated.value = true
    localStorage.setItem('username', data.username)
    applyUser(data)
  }

  function logout() {
    localStorage.removeItem('access_token')
    localStorage.removeItem('username')
    isAuthenticated.value = false
    username.value = ''
    role.value = ''
    status.value = ''
    displayName.value = ''
  }

  return { isAuthenticated, username, role, status, displayName, isAdmin, bootstrap, login, logout }
}
```

- [ ] **Step 6: Run frontend helper tests**

Run: `python3 -m pytest tests/test_web_frontend_runtime_helpers.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 6**

```bash
git add web/frontend/src/lib/contracts.js web/frontend/src/lib/api.js web/frontend/src/composables/useAuth.js tests/test_web_frontend_runtime_helpers.py
git commit -m "feat: add frontend auth and request contracts"
```

---

### Task 7: Login/Register UI, Admin Panel, And Project Request UI

**Files:**
- Modify: `web/frontend/src/components/LoginForm.vue`
- Create: `web/frontend/src/components/AdminPanel.vue`
- Create: `web/frontend/src/components/ProjectRequestsPanel.vue`
- Modify: `web/frontend/src/components/NewProjectModal.vue`
- Modify: `web/frontend/src/App.vue`
- Modify: `web/frontend/src/components/ProjectWorkspace.vue`

- [ ] **Step 1: Update `LoginForm.vue` for login/register mode**

Replace the form state with:

```javascript
data() {
  return {
    mode: 'login',
    username: '',
    displayName: '',
    password: '',
    confirmPassword: '',
    loading: false,
    error: '',
    notice: '',
  }
}
```

Import `authRegister`:

```javascript
import { authLogin, authRegister } from '../lib/api.js'
```

Use a compact segmented control above the fields:

```html
<div class="seg">
  <button type="button" class="seg-b" :class="{ on: mode === 'login' }" @click="mode = 'login'" :disabled="loading">登录</button>
  <button type="button" class="seg-b" :class="{ on: mode === 'register' }" @click="mode = 'register'" :disabled="loading">注册</button>
</div>
```

Add display name and confirmation fields only in register mode:

```html
<label v-if="mode === 'register'" class="fl">
  <span class="fl-lbl label">显示名</span>
  <span class="fl-wrap">
    <Icon name="id-card" :size="15" class="fl-ic" />
    <input v-model="displayName" class="field fl-in" type="text" autocomplete="name" :disabled="loading" />
  </span>
</label>
<label v-if="mode === 'register'" class="fl">
  <span class="fl-lbl label">确认密码</span>
  <span class="fl-wrap">
    <Icon name="lock-keyhole" :size="15" class="fl-ic" />
    <input v-model="confirmPassword" class="field fl-in" type="password" autocomplete="new-password" :disabled="loading" required />
  </span>
</label>
<div v-if="notice" class="bc-ok">
  <Icon name="check-circle" :size="14" /> {{ notice }}
</div>
```

Change `submit()`:

```javascript
async submit() {
  this.loading = true
  this.error = ''
  this.notice = ''
  try {
    if (this.mode === 'register') {
      if (this.password !== this.confirmPassword) {
        this.error = '两次密码不一致'
        return
      }
      await authRegister({ username: this.username, password: this.password, display_name: this.displayName })
      this.notice = '账号等待管理员批准'
      this.mode = 'login'
      this.password = ''
      this.confirmPassword = ''
      return
    }
    const data = await authLogin(this.username, this.password)
    localStorage.setItem('access_token', data.access_token)
    localStorage.setItem('username', data.username)
    this.$emit('login-success', data)
  } catch (err) {
    const detail = err.response?.data?.detail
    const messages = {
      USER_PENDING: '账号等待管理员批准',
      USER_REJECTED: '账号申请已被拒绝',
      USER_DISABLED: '账号已停用',
      USER_EXISTS: '用户名已存在',
      INVALID_USERNAME: '用户名仅允许字母、数字、下划线、连字符',
    }
    this.error = messages[detail] || detail || '登录失败，请检查用户名和密码'
  } finally {
    this.loading = false
  }
}
```

Add `.seg`, `.seg-b`, `.seg-b.on`, and `.bc-ok` styles matching the existing compact dashboard controls.

- [ ] **Step 2: Create `ProjectRequestsPanel.vue`**

Create `web/frontend/src/components/ProjectRequestsPanel.vue` with props:

```javascript
props: {
  admin: { type: Boolean, default: false },
}
```

Use `ProjectRequests.list()` on mount. Render a modal with:

- title `项目审批` for admin and `项目申请` for user;
- list rows showing `base_name`, `requester`, `status`, and `failure_reason || decision_note`;
- admin-only approve/reject icon buttons for `pending` requests;
- emits `changed` after approve/reject.

Implementation methods:

```javascript
async load() {
  this.loading = true
  try {
    this.requests = await ProjectRequests.list()
  } finally {
    this.loading = false
  }
},
async approve(item) {
  this.busy = item.id
  try {
    await ProjectRequests.approve(item.id, '')
    await this.load()
    this.$emit('changed')
  } finally {
    this.busy = null
  }
},
async reject(item) {
  this.busy = item.id
  try {
    await ProjectRequests.reject(item.id, 'rejected by admin')
    await this.load()
    this.$emit('changed')
  } finally {
    this.busy = null
  }
}
```

Use icon buttons with `check`, `x`, and `refresh-cw`; use status labels `pending`, `approved`, `rejected`, `failed` as compact badges.

- [ ] **Step 3: Create `AdminPanel.vue`**

Create `web/frontend/src/components/AdminPanel.vue` with:

- imports: `AdminUsers`, `ProjectRequests`, `Icon`;
- local state: `users`, `requests`, `loading`, `busyUser`, `busyRequest`, `error`;
- mount loads both lists;
- user actions call `AdminUsers.approve(username)`, `AdminUsers.reject(username, 'rejected by admin')`, `AdminUsers.disable(username)`;
- project request actions call `ProjectRequests.approve(id)` and `ProjectRequests.reject(id, 'rejected by admin')`;
- emits `changed` after any action.

Render two full-width sections inside one modal:

- `用户审批`: pending users first, then active/rejected/disabled users;
- `项目审批`: pending project requests first, then recent decided requests.

Keep rows dense: username/name/status plus icon buttons. Do not include a marketing explanation paragraph.

- [ ] **Step 4: Make `NewProjectModal.vue` role-aware**

Add prop:

```javascript
props: {
  isAdmin: { type: Boolean, default: false },
}
```

Import `ProjectRequests`:

```javascript
import api, { Projects, ProjectRequests, formatBytes } from '../lib/api.js'
```

Change labels:

```html
<div class="mh-l"><Icon name="plus" :size="16" /><span>{{ isAdmin ? '新建建模项目' : '申请建模项目' }}</span></div>
```

```html
<template v-else><Icon :name="isAdmin ? 'plus' : 'send'" :size="14" /> {{ isAdmin ? '创建项目' : '提交申请' }}</template>
```

Change submit:

```javascript
const result = this.isAdmin ? await Projects.create(this.form) : await ProjectRequests.create(this.form)
this.$emit(this.isAdmin ? 'project-created' : 'project-requested', result)
this.$emit('close')
```

Add `project-requested` to emits:

```javascript
emits: ['close', 'project-created', 'project-requested'],
```

- [ ] **Step 5: Gate admin controls and add panels in `App.vue`**

In setup, get auth role:

```javascript
const { isAuthenticated, username, role, status, isAdmin, bootstrap, login, logout: clearAuth } = useAuth()
```

Add refs:

```javascript
const showAdmin = ref(false)
const showRequests = ref(false)
```

Header controls:

```html
<button class="btn btn-amber" @click="openNew">
  <Icon :name="isAdmin ? 'plus' : 'send'" :size="15" />
  <span class="hide-sm">{{ isAdmin ? '新建' : '申请' }}</span>
</button>
<button v-if="isAdmin" class="btn btn-icon btn-ghost" @click="showAdmin = true" title="管理员"><Icon name="shield" :size="15" /></button>
<button class="btn btn-icon btn-ghost" @click="showRequests = true" title="项目申请"><Icon name="inbox" :size="15" /></button>
<button v-if="isAdmin" class="btn btn-icon btn-ghost" @click="showModels = true" title="模型管理"><Icon name="cpu" :size="15" /></button>
```

Modal mounts:

```html
<NewProjectModal
  v-if="showNew"
  :is-admin="isAdmin"
  @close="showNew = false"
  @project-created="onCreated"
  @project-requested="onRequested"
/>
<AdminPanel v-if="showAdmin" @close="showAdmin = false" @changed="onAdminChanged" />
<ProjectRequestsPanel v-if="showRequests" :admin="isAdmin" @close="showRequests = false" @changed="onAdminChanged" />
```

Add imports/components for `AdminPanel` and `ProjectRequestsPanel`.

Methods:

```javascript
function onRequested(result) {
  toasts.success(`项目 ${result.base_name} 已提交审批`)
}
async function onAdminChanged() {
  await refreshProjects()
}
```

Return `role`, `status`, `isAdmin`, `showAdmin`, `showRequests`, `onRequested`, and `onAdminChanged`.

- [ ] **Step 6: Hide global model manager inside project workspace for normal users**

In `App.vue`, pass the admin flag into the workspace:

```html
<ProjectWorkspace
  v-if="selectedProject"
  :project="selectedProject"
  :is-admin="isAdmin"
  @close="closeWorkspace"
  @action="onAction"
  @refresh="fetchProjects"
/>
```

In `ProjectWorkspace.vue`, extend props:

```javascript
props: {
  project: { type: Object, required: true },
  isAdmin: { type: Boolean, default: false },
},
```

Hide only the global `ModelManager` entry points; keep per-project step assignment visible because backend allows project owners to save their own project config:

```html
<button v-if="isAdmin" class="btn btn-sm btn-ghost" @click="showModels = true" title="模型管理">
```

```html
<PipelineTimeline
  v-else-if="activeTab === 'pipeline'"
  class="rise"
  :current-step="project.current_step"
  :steps-data="stepsData"
  :awaiting="project.consultation_pending"
  :registry="modelRegistry"
  :assignments="projectAssignments"
  @open-file="requestFile"
  @open-paper="requestPaper"
  @assign="onAssign"
  @manage-models="isAdmin ? (showModels = true) : null"
/>
```

```html
<ModelManager v-if="showModels && isAdmin" @close="showModels = false" />
```

- [ ] **Step 7: Build the frontend**

Run:

```bash
cd web/frontend
npm run build
```

Expected: PASS with Vite production build output.

- [ ] **Step 8: Commit Task 7**

```bash
git add \
  web/frontend/src/components/LoginForm.vue \
  web/frontend/src/components/AdminPanel.vue \
  web/frontend/src/components/ProjectRequestsPanel.vue \
  web/frontend/src/components/NewProjectModal.vue \
  web/frontend/src/App.vue \
  web/frontend/src/components/ProjectWorkspace.vue
git commit -m "feat: add user approval dashboard UI"
```

---

### Task 8: End-To-End Verification And Deployment

**Files:**
- Modify only if verification exposes a concrete defect in files already changed by Tasks 1-7.

- [ ] **Step 1: Run backend focused tests**

Run:

```bash
python3 -m pytest \
  tests/test_auth_store.py \
  tests/test_control_plane_auth.py \
  tests/test_web_control_plane_api.py \
  tests/test_web_frontend_runtime_helpers.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run broader control-plane regression tests**

Run:

```bash
python3 -m pytest \
  tests/test_control_plane_state_store.py \
  tests/test_control_plane_uploads.py \
  tests/test_control_plane_consultation.py \
  tests/test_web_diagnostics_backend.py \
  tests/test_web_step8_5_metadata.py \
  -q
```

Expected: PASS.

- [ ] **Step 3: Build frontend**

Run:

```bash
cd web/frontend
npm run build
```

Expected: PASS.

- [ ] **Step 4: Local smoke test with temporary DB**

Run from repo root:

```bash
AUTH_DB_FILE=/tmp/paper_factory_auth_smoke.db \
JWT_SECRET=0123456789abcdef0123456789abcdef \
ADMIN_PASSWORD='correct horse battery staple 42' \
python3 -m uvicorn web.backend.main:app --host 127.0.0.1 --port 8001
```

In another shell:

```bash
curl -s -X POST http://127.0.0.1:8001/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"correct horse battery staple 42"}'
curl -s -X POST http://127.0.0.1:8001/api/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"smoke_user","password":"smoke password","display_name":"Smoke"}'
```

Expected: admin login returns `access_token`, registration returns `status:"pending"`.

- [ ] **Step 5: Deploy to tfisher.de**

Run:

```bash
sudo ./deploy.sh
```

Expected: deployment completes without service start failure.

- [ ] **Step 6: Production smoke test**

Use the browser:

1. Log in as `admin`.
2. Register `smoke_user_ui`.
3. Log in as `admin`, approve `smoke_user_ui`.
4. Log in as `smoke_user_ui`.
5. Submit one project request using a small problem file.
6. Log in as `admin`, reject that request.
7. Confirm `smoke_user_ui` sees the rejected request and does not see historical admin projects.

- [ ] **Step 7: Commit verification fixes or record no code changes**

If no code changes were needed, do not create an empty commit. If verification required fixes, run `git status --short`, stage only the files changed by the verification fix with explicit path arguments, and commit:

```bash
git status --short
git add web/backend/auth_store.py web/backend/access_control.py web/backend/auth.py web/backend/main.py web/backend/project_api.py web/backend/cloud_api.py web/backend/ws.py web/backend/schemas.py web/frontend/src/lib/contracts.js web/frontend/src/lib/api.js web/frontend/src/composables/useAuth.js web/frontend/src/components/LoginForm.vue web/frontend/src/components/AdminPanel.vue web/frontend/src/components/ProjectRequestsPanel.vue web/frontend/src/components/NewProjectModal.vue web/frontend/src/App.vue web/frontend/src/components/ProjectWorkspace.vue tests/test_auth_store.py tests/test_control_plane_auth.py tests/test_web_control_plane_api.py tests/test_web_frontend_runtime_helpers.py
git commit -m "fix: harden user approval workflow"
```

---

## Self-Review Checklist

- Spec coverage:
  - Self-registration with `pending`: Task 2 and Task 7.
  - Admin approve/reject/disable users: Task 2 and Task 7.
  - SQLite `web/auth.db`: Task 1.
  - Project creation request flow: Task 4 and Task 7.
  - Admin approval launches project and writes ACL: Task 4.
  - Normal users see/control only owned projects: Task 3, Task 5, Task 8.
  - Admin sees/controls all projects: Task 3 and Task 7.
  - Global model config restricted to admin: Task 3 and Task 7.
  - Cloud and WebSocket filtering: Task 5.
  - Audit log for registration, approval, project request, ACL: Task 1 and Task 4.
  - Historical projects admin-only by default: Task 3 uses ACL filtering, so users get no historical projects unless granted.
- Type consistency:
  - Backend status values are `pending`, `active`, `rejected`, `disabled` for users and `pending`, `approved`, `rejected`, `failed` for project requests.
  - Frontend normalizers use the same field names as backend response models.
  - Project request approval routes use `/api/admin/project-requests/{request_id}/approve` and `/reject`.
- Scope:
  - No email verification, password reset, OAuth, groups, quotas, billing, API tokens, or project multi-user collaboration are included.
  - First-version history migration is intentionally admin-only visibility.
