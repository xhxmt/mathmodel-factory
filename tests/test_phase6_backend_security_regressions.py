from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx
import pytest


def _settings(tmp_path: Path):
    from web.backend.config import Settings

    return Settings(
        jwt_secret="j" * 32,
        admin_password="phase6-security-test-password",
        factory_root=tmp_path,
        auth_db_file=tmp_path / "auth.db",
        phase6_snapshot_enabled=True,
        phase6_snapshot_db_file=tmp_path / "phase6.db",
    )


def _ready_payload(*, actions: list[dict[str, object]] | None = None):
    snapshot_id = "a" * 64
    return {
        "schema_version": "phase6-project-snapshot-web-v1",
        "state": "ready",
        "project_id": "demo",
        "workflow_id": "workflow-demo",
        "authority_coordinate": {
            "workflow_id": "workflow-demo",
            "project_id": "demo",
            "current_revision": 7,
        },
        "source_binding_sha256": "d" * 64,
        "snapshot_id": snapshot_id,
        "revision": 7,
        "coordinate": {"snapshot_id": snapshot_id, "revision": 7},
        "sections": [{"key": "summary", "data": {"ok": True}}],
        "actions": [] if actions is None else actions,
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }


def _active_user_and_token(settings):
    from web.backend.auth import create_access_token
    from web.backend.auth_store import AuthStore

    store = AuthStore(settings.resolved_auth_db_file)
    store.initialize()
    store.register_user("alice", "phase6 security regression password")
    store.approve_user("alice", actor="admin")
    return store, create_access_token(settings, "alice", "user", "active")


def _authorization(runtime_token: str) -> dict[str, str]:
    """Build the standard scheme from a proved runtime-only token."""

    return {"Authorization": f"Bearer {runtime_token}"}


def test_http_revision_validation_runs_after_authentication_and_project_acl(tmp_path):
    from fastapi import FastAPI

    from web.backend.phase6_api import create_phase6_router

    settings = _settings(tmp_path)
    store, token = _active_user_and_token(settings)
    authorization = _authorization(token)
    loader_calls: list[dict[str, object]] = []

    def loader(**kwargs):
        loader_calls.append(kwargs)
        return _ready_payload()

    app = FastAPI()
    app.include_router(create_phase6_router(settings, snapshot_loader=loader))
    query_variants = (
        "",
        "?expected_revision=not-an-integer",
        "?expected_revision=-1",
        "?expected_revision=9223372036854775808",
    )

    async def exercise_http_contract() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://phase6.test"
        ) as client:
            denied_responses = [
                await client.get(
                    f"/api/projects/demo/phase6-snapshot{query}",
                    headers=authorization,
                )
                for query in query_variants
            ]
            assert {
                (response.status_code, response.json()["detail"])
                for response in denied_responses
            } == {(404, "PROJECT_NOT_FOUND")}
            assert loader_calls == []

            # Authentication also precedes revision parsing: malformed input cannot
            # turn a missing token into a parameter-validation oracle.
            unauthenticated = await client.get(
                "/api/projects/demo/phase6-snapshot?expected_revision=-1"
            )
            assert unauthenticated.status_code in {401, 403}
            assert unauthenticated.status_code != 422

            store.grant_project_owner("demo", "alice", actor="admin")
            missing = await client.get(
                "/api/projects/demo/phase6-snapshot", headers=authorization
            )
            valid = await client.get(
                "/api/projects/demo/phase6-snapshot?expected_revision=7",
                headers=authorization,
            )
            assert missing.status_code == valid.status_code == 200
            assert [call["expected_revision"] for call in loader_calls] == [None, 7]

            for query in query_variants[1:]:
                invalid = await client.get(
                    f"/api/projects/demo/phase6-snapshot{query}",
                    headers=authorization,
                )
                assert invalid.status_code == 422
                assert invalid.json() == {
                    "detail": {"code": "PHASE6_EXPECTED_REVISION_INVALID"}
                }
            assert len(loader_calls) == 2

    asyncio.run(exercise_http_contract())


@pytest.mark.parametrize(
    "actions",
    (
        [
            {"id": "review", "label": "same"},
            {"id": " review ", "label": "same"},
        ],
        [
            {"id": " review ", "label": "same"},
            {"id": "review", "label": "same"},
        ],
        [
            {"id": "review", "label": "first"},
            {"id": " review ", "label": "different"},
        ],
        [
            {"id": " review ", "label": "different"},
            {"id": "review", "label": "first"},
        ],
    ),
)
def test_backend_rejects_trim_equivalent_duplicate_action_ids_in_any_order(
    tmp_path, monkeypatch, actions
):
    from fastapi import HTTPException

    from web.backend import phase6_api

    monkeypatch.setattr(phase6_api, "require_project_access", lambda *_args: None)
    endpoint = next(
        route.endpoint
        for route in phase6_api.create_phase6_router(
            _settings(tmp_path),
            snapshot_loader=lambda **_kwargs: _ready_payload(actions=actions),
        ).routes
        if getattr(route, "path", None)
        == "/api/projects/{base_name}/phase6-snapshot"
    )

    with pytest.raises(HTTPException) as rejected:
        endpoint("demo", None, object())
    assert rejected.value.status_code == 503
    assert rejected.value.detail == "PHASE6_SNAPSHOT_UNAVAILABLE"


def test_backend_accepts_unique_action_ids(tmp_path, monkeypatch):
    from web.backend import phase6_api

    monkeypatch.setattr(phase6_api, "require_project_access", lambda *_args: None)
    actions = [
        {"id": "review", "label": "Review"},
        {"id": "approve", "label": "Approve"},
    ]
    endpoint = next(
        route.endpoint
        for route in phase6_api.create_phase6_router(
            _settings(tmp_path),
            snapshot_loader=lambda **_kwargs: _ready_payload(actions=actions),
        ).routes
        if getattr(route, "path", None)
        == "/api/projects/{base_name}/phase6-snapshot"
    )

    payload = endpoint("demo", None, object())
    assert [action["id"] for action in payload["actions"]] == ["review", "approve"]


def test_unknown_adapter_codes_and_local_failures_return_only_safe_public_detail(
    tmp_path, monkeypatch, caplog
):
    from fastapi import HTTPException

    from web.backend import phase6_api

    monkeypatch.setattr(phase6_api, "require_project_access", lambda *_args: None)
    sensitive = "/srv/private/auth.db SELECT token FROM credentials"

    class CustomAdapterFailure(Exception):
        code = "CUSTOM_DATABASE_FAILURE"

    class InvalidAdapterPayload:
        def as_dict(self):
            raise RuntimeError(sensitive)

    def endpoint_for(loader):
        return next(
            route.endpoint
            for route in phase6_api.create_phase6_router(
                _settings(tmp_path), snapshot_loader=loader
            ).routes
            if getattr(route, "path", None)
            == "/api/projects/{base_name}/phase6-snapshot"
        )

    caplog.set_level(logging.ERROR, logger="web.backend.phase6_api")
    failures = (
        lambda **_kwargs: (_ for _ in ()).throw(CustomAdapterFailure(sensitive)),
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError(sensitive)),
        lambda **_kwargs: InvalidAdapterPayload(),
    )
    for loader in failures:
        with pytest.raises(HTTPException) as rejected:
            endpoint_for(loader)("demo", None, object())
        assert rejected.value.status_code == 503
        assert rejected.value.detail == "PHASE6_SNAPSHOT_UNAVAILABLE"

    # A known allowlisted store code remains stable without exposing the
    # exception message that accompanied it.
    class KnownAdapterFailure(Exception):
        code = "PHASE6_SNAPSHOT_TAMPERED"

    with pytest.raises(HTTPException) as known:
        endpoint_for(
            lambda **_kwargs: (_ for _ in ()).throw(KnownAdapterFailure(sensitive))
        )("demo", None, object())
    assert known.value.status_code == 503
    assert known.value.detail == "PHASE6_SNAPSHOT_TAMPERED"
    assert sensitive not in caplog.text
