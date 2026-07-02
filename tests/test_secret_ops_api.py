import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from web.backend.config import Settings
from web.backend.schemas import UserInfo


def _payload(model):
    if hasattr(model, "model_dump"):
        return model.model_dump()
    if hasattr(model, "dict"):
        return model.dict()
    return model


def load_main_module(tmp_path: Path):
    import importlib

    sys.modules.pop("web.backend.main", None)
    os.environ["JWT_SECRET"] = "0123456789abcdef0123456789abcdef"
    os.environ["ADMIN_PASSWORD"] = "correct horse battery staple 42"
    os.environ["FACTORY_ROOT"] = str(tmp_path)
    os.environ["AUTH_DB_FILE"] = str(tmp_path / "web" / "auth.db")
    os.environ["GCP_PROJECT_ID"] = "configured-project"
    return importlib.import_module("web.backend.main")


def test_secret_ops_status_checks_secret_manager_without_exposing_values(tmp_path, monkeypatch):
    from web.backend.ops_status import build_secret_ops_status

    fake_gcloud = tmp_path / "gcloud"
    fake_gcloud.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf 'secret-value-that-must-not-leak\\n'
""",
        encoding="utf-8",
    )
    fake_gcloud.chmod(0o755)

    monkeypatch.setenv("GCLOUD_BIN", str(fake_gcloud))
    monkeypatch.setenv("MINERU_TOKEN", "loaded-from-runtime")
    monkeypatch.setenv("JWT_SECRET", "loaded-jwt-secret-value")
    settings = Settings(
        jwt_secret="0123456789abcdef0123456789abcdef",
        admin_password="correct horse battery staple 42",
        factory_root=tmp_path,
        gcp_project_id="configured-project",
    )

    status = build_secret_ops_status(settings)
    payload = _payload(status)

    assert payload["project_id"] == "configured-project"
    assert payload["gcloud_path"] == str(fake_gcloud)
    assert all(item["accessible"] for item in payload["secrets"])
    assert next(item for item in payload["secrets"] if item["env"] == "MINERU_TOKEN")["loaded"] is True
    assert "secret-value-that-must-not-leak" not in json.dumps(payload)
    assert "loaded-jwt-secret-value" not in json.dumps(payload)


def test_admin_secret_ops_route_requires_admin(tmp_path, monkeypatch):
    mod = load_main_module(tmp_path)

    def fake_status(settings):
        del settings
        return {
            "project_id": "configured-project",
            "gcloud_path": "/tmp/gcloud",
            "loader": "scripts/load_secrets.sh",
            "secrets": [],
            "local_config": [],
        }

    monkeypatch.setattr(mod, "build_secret_ops_status", fake_status)

    with pytest.raises(mod.HTTPException) as excinfo:
        asyncio.run(
            mod.get_admin_secret_ops(
                current_user=UserInfo(username="alice", role="user", status="active")
            )
        )
    assert excinfo.value.status_code == 403

    result = asyncio.run(
        mod.get_admin_secret_ops(
            current_user=UserInfo(username="admin", role="admin", status="active")
        )
    )
    assert result["project_id"] == "configured-project"


def test_admin_audit_log_route_returns_metadata_and_requires_admin(tmp_path):
    from web.backend.schemas import RegisterRequest

    mod = load_main_module(tmp_path)
    asyncio.run(
        mod.register_user(
            RegisterRequest(username="alice", password="alice password", display_name="Alice")
        )
    )

    with pytest.raises(mod.HTTPException) as excinfo:
        asyncio.run(
            mod.list_admin_audit_log(
                current_user=UserInfo(username="alice", role="user", status="active")
            )
        )
    assert excinfo.value.status_code == 403

    rows = asyncio.run(
        mod.list_admin_audit_log(
            current_user=UserInfo(username="admin", role="admin", status="active")
        )
    )
    payload = [_payload(row) for row in rows]

    assert any(row["action"] == "user.register" and row["target_id"] == "alice" for row in payload)
    assert all(isinstance(row["metadata"], dict) for row in payload)
