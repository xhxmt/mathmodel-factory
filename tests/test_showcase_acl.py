from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path

import pytest

from web.backend.auth_store import AuthStore
from web.backend.config import Settings
from web.backend.schemas import UserInfo
from web.backend.showcase_api import create_showcase_router


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        jwt_secret="0123456789abcdef0123456789abcdef",
        admin_password="correct horse battery staple 42",
        factory_root=tmp_path,
        auth_db_file=tmp_path / "web" / "auth.db",
        showcase_projects=("legacy_public",),
    )


def write_paper(root: Path, base_name: str) -> None:
    project = root / "complete" / base_name
    project.mkdir(parents=True)
    (project / f"{base_name}_paper.pdf").write_bytes(b"%PDF-1.4\n")


def endpoint(router, path: str):
    for route in router.routes:
        if route.path == path:
            return route.endpoint
    raise AssertionError(f"missing route {path}")


def load_main_module(tmp_path: Path):
    sys.modules.pop("web.backend.main", None)
    os.environ["JWT_SECRET"] = "0123456789abcdef0123456789abcdef"
    os.environ["ADMIN_PASSWORD"] = "correct horse battery staple 42"
    os.environ["FACTORY_ROOT"] = str(tmp_path)
    os.environ["AUTH_DB_FILE"] = str(tmp_path / "web" / "auth.db")
    return importlib.import_module("web.backend.main")


def test_showcase_acl_bootstraps_legacy_guest_once_and_cleans_deleted_users(tmp_path):
    settings = make_settings(tmp_path)
    store = AuthStore(settings.resolved_auth_db_file)
    store.initialize()

    store.bootstrap_guest_showcase((*settings.showcase_projects, "../invalid"))
    store.bootstrap_guest_showcase(("later_env_value",))
    assert store.list_showcase_project_names("guest") == ["legacy_public"]

    store.replace_showcase_projects("guest", [], actor="admin")
    store.bootstrap_guest_showcase(settings.showcase_projects)
    assert store.list_showcase_project_names("guest") == []

    store.register_user("alice", "alice password")
    audience = store.showcase_user_audience("alice")
    store.replace_showcase_projects(audience, ["personal"], actor="admin")
    assert store.list_showcase_project_names(audience) == ["personal"]
    assert store.user_can_access_project("alice", "personal") is False

    store.delete_user("alice", actor="admin")
    assert store.list_showcase_project_names(audience) == []


def test_showcase_router_separates_guest_and_user_visibility(tmp_path):
    settings = make_settings(tmp_path)
    for base_name in ("public", "personal", "private"):
        write_paper(tmp_path, base_name)

    store = AuthStore(settings.resolved_auth_db_file)
    store.initialize()
    store.replace_showcase_projects("guest", ["public"], actor="admin")
    store.replace_showcase_projects(store.showcase_user_audience("alice"), ["personal"], actor="admin")
    router = create_showcase_router(settings, store)

    guest_list = endpoint(router, "/api/showcase/papers")
    user_list = endpoint(router, "/api/showcase/user-papers")
    guest_pdf = endpoint(router, "/api/showcase/papers/{base_name}/pdf")
    user_pdf = endpoint(router, "/api/showcase/user-papers/{base_name}/pdf")

    guest = asyncio.run(guest_list())
    alice = asyncio.run(user_list(current_user=UserInfo(username="alice", role="user")))
    bob = asyncio.run(user_list(current_user=UserInfo(username="bob", role="user")))
    admin = asyncio.run(user_list(current_user=UserInfo(username="admin", role="admin")))

    assert [paper.base_name for paper in guest] == ["public"]
    assert [paper.base_name for paper in alice] == ["personal", "public"]
    assert [paper.base_name for paper in bob] == ["public"]
    assert [paper.base_name for paper in admin] == ["personal", "private", "public"]
    assert all(paper.pdf_url.startswith("/api/showcase/user-papers/") for paper in alice)

    public_response = asyncio.run(guest_pdf("public"))
    assert public_response.headers["cache-control"] == "no-store"
    asyncio.run(
        user_pdf("personal", current_user=UserInfo(username="alice", role="user"))
    )
    with pytest.raises(Exception) as guest_denied:
        asyncio.run(guest_pdf("personal"))
    assert getattr(guest_denied.value, "status_code", None) == 404
    with pytest.raises(Exception) as user_denied:
        asyncio.run(
            user_pdf("personal", current_user=UserInfo(username="bob", role="user"))
        )
    assert getattr(user_denied.value, "status_code", None) == 404


def test_admin_showcase_api_validates_candidates_and_requires_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOWCASE_PROJECTS", "public")
    write_paper(tmp_path, "public")
    write_paper(tmp_path, "personal")
    incomplete = tmp_path / "complete" / "missing_pdf"
    incomplete.mkdir(parents=True)
    mod = load_main_module(tmp_path)
    asyncio.run(
        mod.register_user(
            mod.RegisterRequest(username="alice", password="alice password", display_name="Alice")
        )
    )
    asyncio.run(
        mod.register_user(
            mod.RegisterRequest(username="guest", password="guest password", display_name="Named Guest")
        )
    )
    admin = UserInfo(username="admin", role="admin")

    initial = asyncio.run(mod.get_admin_showcase(current_user=admin))
    assert {paper.base_name for paper in initial.candidates} == {"public", "personal"}
    assert initial.audiences[0].id == "guest"
    assert any(audience.id == "user:alice" for audience in initial.audiences)
    assert any(audience.id == "user:guest" for audience in initial.audiences)

    updated = asyncio.run(
        mod.update_admin_showcase_audience(
            "user:alice",
            mod.ShowcaseVisibilityUpdate(base_names=["personal"]),
            current_user=admin,
        )
    )
    assert updated.base_names == ["personal"]

    with pytest.raises(mod.HTTPException) as unavailable:
        asyncio.run(
            mod.update_admin_showcase_audience(
                "guest",
                mod.ShowcaseVisibilityUpdate(base_names=["missing_pdf"]),
                current_user=admin,
            )
        )
    assert unavailable.value.status_code == 400

    with pytest.raises(mod.HTTPException) as forbidden:
        asyncio.run(
            mod.get_admin_showcase(current_user=UserInfo(username="alice", role="user"))
        )
    assert forbidden.value.status_code == 403
