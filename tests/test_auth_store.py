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


def test_bootstrap_admin_syncs_existing_admin_password(tmp_path):
    store = make_store(tmp_path)

    store.bootstrap_admin("first strong password")
    first = store.get_user("admin")
    store.bootstrap_admin("second strong password")
    second = store.get_user("admin")

    assert first is not None
    assert first.role == "admin"
    assert first.status == "active"
    assert second is not None
    assert second.role == "admin"
    assert second.status == "active"
    assert second.password_hash != first.password_hash
    assert store.verify_user_password("admin", "first strong password") is False
    assert store.verify_user_password("admin", "second strong password") is True


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


def test_delete_user_removes_user_and_project_acl(tmp_path):
    store = make_store(tmp_path)
    store.bootstrap_admin("admin password")
    store.register_user("alice", "alice password", "Alice")
    store.approve_user("alice", actor="admin")
    store.grant_project_owner("demo_project", "alice", actor="admin")

    store.delete_user("alice", actor="admin")

    assert store.get_user("alice") is None
    assert store.verify_user_password("alice", "alice password") is False
    assert store.user_can_access_project("alice", "demo_project") is False
    assert store.list_project_names_for_user("alice") == []
    assert [row.action for row in store.list_audit_log()][-1] == "user.delete"


def test_delete_user_rejects_builtin_admin(tmp_path):
    store = make_store(tmp_path)
    store.bootstrap_admin("admin password")

    with pytest.raises(ValueError, match="admin"):
        store.delete_user("admin", actor="admin")

    assert store.get_user("admin") is not None


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
