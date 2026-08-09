from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory_core.governance.overrides import (
    CONTINUE_AFTER_GATE2,
    DELIVER_SNAPSHOT,
    SQLiteOverrideProvider,
)
from scripts.workflow_state import (
    gate2_continuation_override,
    gate2_delivery_override,
)
from web.backend.auth_store import AuthStore


def _store(root: Path) -> AuthStore:
    store = AuthStore(root / "web/auth.db")
    store.initialize()
    store.bootstrap_admin("correct horse battery staple test only")
    return store


def test_project_local_override_has_no_authority(tmp_path: Path) -> None:
    project = tmp_path / "ongoing/demo"
    project.mkdir(parents=True)
    (project / "gate2_delivery_override.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "scope": "continue_to_step16",
                "reason": "project file must not authorize itself",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert gate2_continuation_override(project, tmp_path) is False
    assert gate2_delivery_override(project, tmp_path, "a" * 64) is False


def test_admin_override_scopes_and_snapshot_binding_are_enforced(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    continuation = store.issue_delivery_override(
        base_name="demo",
        scope=CONTINUE_AFTER_GATE2,
        source_verdict="REOPEN_REVISION_MODEL",
        reason="continue to final content for inspection",
        actor="admin",
    )
    delivery = store.issue_delivery_override(
        base_name="demo",
        scope=DELIVER_SNAPSHOT,
        bound_snapshot_id="b" * 64,
        source_verdict="REOPEN_REVISION_TEXT",
        reason="accept this exact final snapshot",
        actor="admin",
    )
    provider = SQLiteOverrideProvider(tmp_path / "web/auth.db")

    assert provider.active_override("demo", CONTINUE_AFTER_GATE2) is not None
    assert provider.active_override(
        "demo", DELIVER_SNAPSHOT, snapshot_id="a" * 64
    ) is None
    exact = provider.active_override(
        "demo", DELIVER_SNAPSHOT, snapshot_id="b" * 64
    )
    assert exact is not None
    assert exact.override_id == delivery.override_id
    assert provider.consume(continuation.override_id) is True
    consumed = provider.get_override(continuation.override_id)
    assert consumed is not None
    assert consumed.consumed_at is not None
    assert provider.active_override("demo", CONTINUE_AFTER_GATE2) is None

    revoked = store.revoke_delivery_override(delivery.override_id, actor="admin")
    assert revoked.revoked_at is not None
    assert provider.active_override(
        "demo", DELIVER_SNAPSHOT, snapshot_id="b" * 64
    ) is None
    assert continuation.bound_snapshot_id is None


def test_non_admin_cannot_issue_delivery_override(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.register_user("viewer", "correct horse battery staple viewer")
    store.approve_user("viewer", actor="admin")

    with pytest.raises(ValueError, match="active administrator"):
        store.issue_delivery_override(
            base_name="demo",
            scope=CONTINUE_AFTER_GATE2,
            source_verdict="REOPEN_REVISION_MODEL",
            reason="not authorized",
            actor="viewer",
        )


def test_deliver_snapshot_override_requires_exact_sha256(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="lowercase SHA-256"):
        store.issue_delivery_override(
            base_name="demo",
            scope=DELIVER_SNAPSHOT,
            bound_snapshot_id=None,
            source_verdict="REOPEN_REVISION_TEXT",
            reason="missing binding",
            actor="admin",
        )
