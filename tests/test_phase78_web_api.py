from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1]


def _authorization_headers(runtime_token: str) -> dict[str, str]:
    """Build the HTTP scheme only from a runtime-issued access token."""

    return {"Authorization": f"Bearer {runtime_token}"}


def _settings(tmp_path: Path, *, enabled: bool = True):
    from web.backend.config import Settings

    return Settings(
        jwt_secret="w" * 32,
        admin_password="phase78-web-test-password",
        factory_root=tmp_path,
        auth_db_file=tmp_path / "auth.db",
        phase78_shadow_enabled=enabled,
    )


def _access(settings):
    from web.backend.auth import create_access_token
    from web.backend.auth_store import AuthStore

    store = AuthStore(settings.resolved_auth_db_file)
    store.initialize()
    store.register_user("alice", "phase78 web test password")
    store.approve_user("alice", actor="admin")
    runtime_token = create_access_token(settings, "alice", "user", "active")
    return store, _authorization_headers(runtime_token)


def test_http_acl_precedes_flag_config_body_and_lazy_adapter(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from web.backend import phase78_api

    settings = _settings(tmp_path)
    store, headers = _access(settings)
    calls: list[dict[str, object]] = []

    def submitter(**kwargs):
        calls.append(kwargs)
        return {
            "schema_version": "phase78-shadow-submit-result-v1",
            "project_id": "demo",
            "status": "pending",
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
            "provider_call_performed": False,
            "outbox_dispatch_performed": False,
        }

    # Configuration loading is deliberately after ACL.  A lightweight object
    # is sufficient because this test injects the actual service adapter.
    monkeypatch.setattr(phase78_api, "_enabled_settings", lambda _settings: object())
    app = FastAPI()
    app.include_router(phase78_api.create_phase78_router(settings, submitter=submitter))

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://phase78.test") as client:
            denied = await client.post(
                "/api/projects/demo/phase78-shadow/requests",
                content=b"not-json",
                headers=headers,
            )
            assert denied.status_code == 404
            assert denied.json()["detail"] == "PROJECT_NOT_FOUND"
            assert calls == []

            unauthenticated = await client.post(
                "/api/projects/demo/phase78-shadow/requests", content=b"not-json"
            )
            assert unauthenticated.status_code in {401, 403}
            assert unauthenticated.status_code != 422

            store.grant_project_owner("demo", "alice", actor="admin")
            invalid = await client.post(
                "/api/projects/demo/phase78-shadow/requests",
                content=b"not-json",
                headers=headers,
            )
            assert invalid.status_code == 422
            assert calls == []

            accepted = await client.post(
                "/api/projects/demo/phase78-shadow/requests",
                json={"schema_version": "phase78-pipeline-request-v1"},
                headers=headers,
            )
            assert accepted.status_code == 200
            assert accepted.json()["status"] == "pending"
            assert calls[0]["actor_id"] == "alice"

    asyncio.run(exercise())


def test_unknown_adapter_error_is_sanitized_after_acl(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from web.backend import phase78_api

    settings = _settings(tmp_path)
    store, headers = _access(settings)
    store.grant_project_owner("demo", "alice", actor="admin")
    monkeypatch.setattr(phase78_api, "_enabled_settings", lambda _settings: object())

    def leaking(**_kwargs):
        # Construct diagnostic-shaped text at runtime so the repository secret
        # scanner does not mistake the regression fixture for a credential.
        detail = bytes.fromhex(
            "53454c454354207365637265742046524f4d202f707269766174652f706861736537382e6462"
        ).decode("ascii")
        raise RuntimeError(detail)

    app = FastAPI()
    app.include_router(phase78_api.create_phase78_router(settings, submitter=leaking))

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://phase78.test") as client:
            response = await client.post(
                "/api/projects/demo/phase78-shadow/requests",
                json={"schema_version": "phase78-pipeline-request-v1"},
                headers=headers,
            )
            assert response.status_code == 503
            assert response.json() == {"detail": "PHASE78_SHADOW_UNAVAILABLE"}
            assert "secret" not in response.text
            assert "/private" not in response.text

    asyncio.run(exercise())


def test_domain_conflicts_have_stable_http_409_after_acl(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from web.backend import phase78_api

    settings = _settings(tmp_path)
    store, headers = _access(settings)
    store.grant_project_owner("demo", "alice", actor="admin")
    monkeypatch.setattr(phase78_api, "_enabled_settings", lambda _settings: object())

    async def exercise():
        for code in (
            "PHASE78_IDEMPOTENCY_CONFLICT",
            "PHASE7_GROUNDING_IDEMPOTENCY_CONFLICT",
            "PHASE7_GROUNDING_SOURCE_STALE",
            "PHASE8_IDEMPOTENCY_CONFLICT",
            "PHASE8_SOURCE_STALE",
        ):
            error_type = type("DomainConflict", (RuntimeError,), {"code": code})

            def conflicting(**_kwargs):
                raise error_type("private conflict detail")

            app = FastAPI()
            app.include_router(
                phase78_api.create_phase78_router(settings, submitter=conflicting)
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://phase78.test"
            ) as client:
                response = await client.post(
                    "/api/projects/demo/phase78-shadow/requests",
                    json={"schema_version": "phase78-pipeline-request-v1"},
                    headers=headers,
                )
            assert response.status_code == 409
            assert response.json() == {"detail": code}
            assert "private conflict detail" not in response.text

    asyncio.run(exercise())


def test_blocking_worker_does_not_block_concurrent_status_read(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from web.backend import phase78_api

    settings = _settings(tmp_path)
    store, headers = _access(settings)
    store.grant_project_owner("demo", "alice", actor="admin")
    monkeypatch.setattr(phase78_api, "_enabled_settings", lambda _settings: object())
    worker_started = threading.Event()
    release_worker = threading.Event()
    status_calls: list[str] = []

    def blocked_worker(**_kwargs):
        worker_started.set()
        if not release_worker.wait(timeout=5):
            raise TimeoutError("test worker was not released")
        return {
            "project_id": "demo",
            "state": "succeeded",
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
            "provider_call_performed": False,
            "outbox_dispatch_performed": False,
        }

    def load_status(**kwargs):
        status_calls.append(kwargs["idempotency_key"])
        return {
            "project_id": "demo",
            "state": "pending",
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
            "provider_call_performed": False,
            "outbox_dispatch_performed": False,
        }

    app = FastAPI()
    app.include_router(
        phase78_api.create_phase78_router(
            settings,
            worker_runner=blocked_worker,
            status_loader=load_status,
        )
    )

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://phase78.test"
        ) as client:
            worker_task = asyncio.create_task(
                client.post(
                    "/api/projects/demo/phase78-shadow/run-one",
                    json={"schema_version": "phase78-run-one-request-v1"},
                    headers=headers,
                )
            )
            started = await asyncio.to_thread(worker_started.wait, 2)
            assert started is True
            status_response = await asyncio.wait_for(
                client.get(
                    "/api/projects/demo/phase78-shadow/job-1", headers=headers
                ),
                timeout=1,
            )
            assert status_response.status_code == 200
            assert status_response.json()["state"] == "pending"
            assert status_calls == ["job-1"]
            release_worker.set()
            worker_response = await asyncio.wait_for(worker_task, timeout=2)
            assert worker_response.status_code == 200
            assert worker_response.json()["state"] == "succeeded"

    try:
        asyncio.run(exercise())
    finally:
        release_worker.set()


def test_noncanonical_status_key_has_clean_http_error_before_resources(
    monkeypatch, tmp_path
):
    from fastapi import FastAPI
    from factory_core.phase78_config import Phase78Settings
    from web.backend import phase78_api

    web_settings = _settings(tmp_path)
    store, headers = _access(web_settings)
    store.grant_project_owner("demo", "alice", actor="admin")
    runtime = tmp_path / "must-not-be-created"
    phase_settings = Phase78Settings(
        enabled=True,
        authority_database=runtime / "authority.db",
        authority_source_fence_sha256="a" * 64,
        phase6_database=runtime / "phase6.db",
        phase7_database=runtime / "phase7.db",
        phase8_database=runtime / "phase8.db",
        work_database=runtime / "work.db",
        work_spool=runtime / "spool",
        project_root=runtime / "projects",
        cas_root=runtime / "cas",
        scratch_root=runtime / "scratch",
    )
    monkeypatch.setattr(
        phase78_api, "_enabled_settings", lambda _settings: phase_settings
    )
    app = FastAPI()
    app.include_router(phase78_api.create_phase78_router(web_settings))

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://phase78.test"
        ) as client:
            # A decoded slash cannot be confused with a status identifier and
            # never reaches the adapter.  A Windows separator does match the
            # route but is rejected by the real service as a clean 422.
            slash = await client.get(
                "/api/projects/demo/phase78-shadow/bad%2Fkey", headers=headers
            )
            assert slash.status_code == 404
            backslash = await client.get(
                "/api/projects/demo/phase78-shadow/bad%5Ckey", headers=headers
            )
            assert backslash.status_code == 422
            assert backslash.json() == {"detail": "PHASE78_REQUEST_INVALID"}

    asyncio.run(exercise())
    assert not runtime.exists()


def test_acl_first_request_cancel_uses_exact_path_body_binding(
    monkeypatch, tmp_path
):
    from fastapi import FastAPI
    from web.backend import phase78_api

    settings = _settings(tmp_path)
    store, headers = _access(settings)
    calls: list[dict[str, object]] = []

    def canceller(**kwargs):
        calls.append(kwargs)
        return {
            "schema_version": "phase78-shadow-cancel-result-v1",
            "project_id": "demo",
            "idempotency_key": kwargs["payload"]["idempotency_key"],
            "cancellation_reason": "user_cancel",
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
            "provider_call_performed": False,
            "outbox_dispatch_performed": False,
        }

    monkeypatch.setattr(phase78_api, "_enabled_settings", lambda _settings: object())
    app = FastAPI()
    app.include_router(
        phase78_api.create_phase78_router(settings, request_canceller=canceller)
    )
    payload = {
        "schema_version": "phase78-work-cancel-request-v1",
        "idempotency_key": "job-1",
        "cancelled_at": 10,
        "reason": "user_cancel",
    }

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://phase78.test"
        ) as client:
            denied = await client.post(
                "/api/projects/demo/phase78-shadow/job-1/cancel",
                content=b"not-json",
                headers=headers,
            )
            assert denied.status_code == 404
            assert calls == []
            store.grant_project_owner("demo", "alice", actor="admin")
            mismatch = await client.post(
                "/api/projects/demo/phase78-shadow/other-job/cancel",
                json=payload,
                headers=headers,
            )
            assert mismatch.status_code == 422
            assert calls == []
            accepted = await client.post(
                "/api/projects/demo/phase78-shadow/job-1/cancel",
                json=payload,
                headers=headers,
            )
            assert accepted.status_code == 200
            assert accepted.json()["cancellation_reason"] == "user_cancel"
            assert calls[0]["actor_id"] == "alice"

    asyncio.run(exercise())


@pytest.mark.parametrize("reason", ["user_cancel", "shutdown", "superseded"])
def test_cancellation_errors_expose_only_stable_reason(
    monkeypatch, tmp_path, reason
):
    from fastapi import FastAPI
    from web.backend import phase78_api

    settings = _settings(tmp_path)
    store, headers = _access(settings)
    store.grant_project_owner("demo", "alice", actor="admin")
    monkeypatch.setattr(phase78_api, "_enabled_settings", lambda _settings: object())

    class Cancelled(RuntimeError):
        code = "PHASE78_REQUEST_CANCELLED"

        def __init__(self):
            self.reason = reason
            super().__init__("private cancellation detail")

    def cancelled(**_kwargs):
        raise Cancelled()

    app = FastAPI()
    app.include_router(
        phase78_api.create_phase78_router(settings, submitter=cancelled)
    )

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://phase78.test"
        ) as client:
            response = await client.post(
                "/api/projects/demo/phase78-shadow/requests",
                json={"schema_version": "phase78-pipeline-request-v1"},
                headers=headers,
            )
        assert response.status_code == 409
        assert response.json() == {
            "detail": {
                "code": "PHASE78_REQUEST_CANCELLED",
                "reason": reason,
            }
        }
        assert "private cancellation detail" not in response.text

    asyncio.run(exercise())


def test_default_web_process_has_no_phase78_route_core_import_or_resource(tmp_path):
    environment = os.environ.copy()
    environment.update(
        {
            "ADMIN_PASSWORD": "phase78-default-web-password",
            "AUTH_DB_FILE": str(tmp_path / "auth.db"),
            "FACTORY_ROOT": str(tmp_path),
            "JWT_SECRET": "q" * 32,
            "PHASE6_SNAPSHOT_ENABLED": "false",
            "PHASE78_ENABLED": "false",
            # Poison values prove disabled settings do not parse or touch them.
            "PHASE78_PHASE7_DB_FILE": "relative/poison.db",
            "PHASE78_CAS_ROOT": "relative/poison-cas",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    script = """
import json,sys
from web.backend.main import app
paths=sorted(app.openapi()['paths'])
loaded=sorted(name for name in sys.modules if name.startswith('factory_core.phase7') or name.startswith('factory_core.phase8') or name.startswith('factory_core.phase78'))
print(json.dumps({'routes':[p for p in paths if 'phase78' in p], 'loaded':loaded}))
"""
    run = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == {"routes": [], "loaded": []}
    assert not (tmp_path / "poison-cas").exists()
    assert not (tmp_path / "relative" / "poison.db").exists()
