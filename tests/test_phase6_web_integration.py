from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _endpoint(router):
    return next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", None)
        == "/api/projects/{base_name}/phase6-snapshot"
    )


def _settings(tmp_path: Path, *, enabled: bool):
    from web.backend.config import Settings

    return Settings(
        jwt_secret="j" * 32,
        admin_password="phase6-test-password",
        factory_root=tmp_path,
        auth_db_file=tmp_path / "auth.db",
        phase6_snapshot_enabled=enabled,
        phase6_snapshot_db_file=tmp_path / "phase6.db",
    )


def _user():
    from web.backend.schemas import UserInfo

    return UserInfo(username="alice", role="user", status="active")


def _ready_payload(revision: int = 7):
    snapshot_id = "a" * 64
    return {
        "schema_version": "phase6-project-snapshot-web-v1",
        "state": "ready",
        "project_id": "demo",
        "workflow_id": "workflow-demo",
        "authority_coordinate": {
            "workflow_id": "workflow-demo",
            "project_id": "demo",
            "current_revision": revision,
        },
        "source_binding_sha256": "d" * 64,
        "snapshot_id": snapshot_id,
        "revision": revision,
        "coordinate": {"snapshot_id": snapshot_id, "revision": revision},
        "sections": [{"key": "summary", "data": {"ok": True}}],
        "actions": [],
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }


def _create_real_phase6_snapshot(
    database: Path,
    *,
    completeness: str,
) -> str:
    """Build one real standalone Phase-6 store for HTTP integration tests."""

    from factory_core.canonical import canonical_sha256
    from factory_core.phase6_snapshot_grants import (
        Phase6SnapshotGrantStore,
        SectionAvailability,
        VerifiedSection,
        build_authority_source_binding,
    )

    def digest(character: str) -> str:
        return character * 64

    revision = 7
    authority_coordinate = {
        "schema": "authority-workflow-coordinate-v1",
        "workflow_id": "workflow-demo",
        "project_id": "demo",
        "project_generation": "project-generation-demo",
        "run_generation": "run-generation-demo",
        "runtime_generation": "runtime-generation-demo",
        "scheduler_generation": "scheduler-generation-demo",
        "current_revision": revision,
        "contract_pin_set_sha256": digest("a"),
        "authority_state": "active",
        "source_fence_sha256": digest("b"),
        "switch_mode": "shadow",
        "switch_epoch": 3,
    }
    source_coordinate = {
        "schema_version": "snapshot-coordinate-v0",
        "project_id": "demo",
        "workflow_schema_version": 1,
        "project_revision": revision,
        "project_generation": "project-generation-demo",
        "run_generation": "run-generation-demo",
        "runtime_generation": "runtime-generation-demo",
        "scheduler_generation": "scheduler-generation-demo",
        "recorded_contract_pin_set_sha256": digest("a"),
    }
    binding = build_authority_source_binding(
        authority_coordinate=authority_coordinate,
        authority_coordinate_sha256=canonical_sha256(authority_coordinate),
        authority_revision_snapshot_sha256=digest("1"),
        authority_revision_through_revision=revision,
        source_snapshot_schema="project-snapshot-v0-source-authorized-v3",
        source_snapshot_semantic_sha256=canonical_sha256(
            {
                "schema_version": "phase6-real-web-fixture-v1",
                "completeness": completeness,
            }
        ),
        source_snapshot_completeness=completeness,
        source_snapshot_coordinate=source_coordinate,
        phase3_artifact_state_sha256=digest("3"),
        phase4_operation_state_sha256=digest("4"),
        phase5_supervisor_state_sha256=digest("5"),
    )
    store = Phase6SnapshotGrantStore(database)
    store.initialize()
    return store.append_snapshot(
        source_binding=binding,
        sections=(
            VerifiedSection(
                "overview",
                SectionAvailability.AVAILABLE,
                digest("f"),
                "overview-section-v1",
            ),
        ),
        captured_at=10,
        valid_until=100,
        expected_previous_snapshot_id=None,
        idempotency_key=f"real-web-{completeness.lower()}",
    ).snapshot.snapshot_id


def test_phase6_backend_flag_defaults_off_and_rejects_invalid_values(monkeypatch):
    from web.backend.config import load_settings

    monkeypatch.delenv("PHASE6_SNAPSHOT_ENABLED", raising=False)
    assert load_settings().phase6_snapshot_enabled is False

    monkeypatch.setenv("PHASE6_SNAPSHOT_ENABLED", "true")
    assert load_settings().phase6_snapshot_enabled is True

    monkeypatch.setenv("PHASE6_SNAPSHOT_ENABLED", "truthy")
    with pytest.raises(RuntimeError, match="PHASE6_SNAPSHOT_ENABLED must be one of"):
        load_settings()


def test_phase6_endpoint_checks_acl_before_gate_or_loader(monkeypatch, tmp_path):
    from fastapi import HTTPException
    from web.backend import phase6_api

    events: list[str] = []

    def access(_settings, _user, _base_name):
        events.append("acl")

    def loader(**_kwargs):
        events.append("loader")
        return _ready_payload()

    monkeypatch.setattr(phase6_api, "require_project_access", access)
    endpoint = _endpoint(
        phase6_api.create_phase6_router(
            _settings(tmp_path, enabled=False), snapshot_loader=loader
        )
    )
    with pytest.raises(HTTPException) as captured:
        endpoint("private-project", None, _user())
    assert captured.value.status_code == 404
    assert captured.value.detail == "PHASE6_SNAPSHOT_DISABLED"
    assert events == ["acl"]

    events.clear()

    def denied(*_args):
        events.append("acl")
        raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")

    monkeypatch.setattr(phase6_api, "require_project_access", denied)
    endpoint = _endpoint(
        phase6_api.create_phase6_router(
            _settings(tmp_path, enabled=True), snapshot_loader=loader
        )
    )
    with pytest.raises(HTTPException) as captured:
        endpoint("private-project", None, _user())
    assert captured.value.detail == "PROJECT_NOT_FOUND"
    assert events == ["acl"]


def test_phase6_endpoint_returns_one_revision_atomic_sanitized_payload(monkeypatch, tmp_path):
    from web.backend import phase6_api

    calls = []
    monkeypatch.setattr(phase6_api, "require_project_access", lambda *_args: None)

    def loader(**kwargs):
        calls.append(kwargs)
        return _ready_payload(revision=11)

    endpoint = _endpoint(
        phase6_api.create_phase6_router(
            _settings(tmp_path, enabled=True), snapshot_loader=loader
        )
    )
    payload = endpoint("demo", 11, _user())
    assert len(calls) == 1
    assert calls[0] == {
        "db_path": tmp_path / "phase6.db",
        "project_id": "demo",
        "expected_revision": 11,
    }
    assert payload["coordinate"] == {
        "snapshot_id": "a" * 64,
        "revision": 11,
    }
    assert payload["server_revision"] == 11
    assert payload["schema_version"] == "phase6-project-snapshot-web-v1"
    assert payload["project_id"] == "demo"
    assert payload["authoritative"] is False
    assert payload["authority_transferred"] is False
    assert payload["dispatch_performed"] is False
    assert payload["actions"] == []


def test_phase6_endpoint_rejects_stale_or_unsafe_payloads(monkeypatch, tmp_path):
    from fastapi import HTTPException
    from web.backend import phase6_api

    monkeypatch.setattr(phase6_api, "require_project_access", lambda *_args: None)
    settings = _settings(tmp_path, enabled=True)

    endpoint = _endpoint(
        phase6_api.create_phase6_router(
            settings, snapshot_loader=lambda **_kwargs: _ready_payload(revision=5)
        )
    )
    with pytest.raises(HTTPException) as stale:
        endpoint("demo", 4, _user())
    assert stale.value.status_code == 409
    assert stale.value.detail == {
        "code": "PHASE6_SNAPSHOT_STALE",
        "server_revision": 5,
    }

    unsafe = _ready_payload()
    unsafe["authoritative"] = True
    endpoint = _endpoint(
        phase6_api.create_phase6_router(
            settings, snapshot_loader=lambda **_kwargs: unsafe
        )
    )
    with pytest.raises(HTTPException) as rejected:
        endpoint("demo", None, _user())
    assert rejected.value.status_code == 503
    assert rejected.value.detail == "PHASE6_SNAPSHOT_UNAVAILABLE"

    foreign_project = _ready_payload()
    foreign_project["project_id"] = "different-project"
    endpoint = _endpoint(
        phase6_api.create_phase6_router(
            settings, snapshot_loader=lambda **_kwargs: foreign_project
        )
    )
    with pytest.raises(HTTPException) as rejected:
        endpoint("demo", None, _user())
    assert rejected.value.status_code == 503
    assert rejected.value.detail == "PHASE6_SNAPSHOT_UNAVAILABLE"

    unknown_schema = _ready_payload()
    unknown_schema["schema_version"] = "phase6-project-snapshot-web-v2"
    endpoint = _endpoint(
        phase6_api.create_phase6_router(
            settings, snapshot_loader=lambda **_kwargs: unknown_schema
        )
    )
    with pytest.raises(HTTPException) as rejected:
        endpoint("demo", None, _user())
    assert rejected.value.status_code == 503
    assert rejected.value.detail == "PHASE6_SNAPSHOT_UNAVAILABLE"

    mixed_authority = _ready_payload()
    mixed_authority["authority_coordinate"]["project_id"] = "other"
    endpoint = _endpoint(
        phase6_api.create_phase6_router(
            settings, snapshot_loader=lambda **_kwargs: mixed_authority
        )
    )
    with pytest.raises(HTTPException) as rejected:
        endpoint("demo", None, _user())
    assert rejected.value.status_code == 503
    assert rejected.value.detail == "PHASE6_SNAPSHOT_UNAVAILABLE"

    mixed = _ready_payload()
    mixed["sections"][0]["coordinate"] = {
        "snapshot_id": "c" * 64,
        "revision": 7,
    }
    endpoint = _endpoint(
        phase6_api.create_phase6_router(
            settings, snapshot_loader=lambda **_kwargs: mixed
        )
    )
    with pytest.raises(HTTPException) as rejected:
        endpoint("demo", None, _user())
    assert rejected.value.status_code == 503
    assert rejected.value.detail == "PHASE6_SNAPSHOT_UNAVAILABLE"

    mixed_action = _ready_payload()
    mixed_action["actions"] = [
        {
            "id": "review",
            "snapshot_id": "a" * 64,
            "revision": 8,
        }
    ]
    endpoint = _endpoint(
        phase6_api.create_phase6_router(
            settings, snapshot_loader=lambda **_kwargs: mixed_action
        )
    )
    with pytest.raises(HTTPException) as rejected:
        endpoint("demo", None, _user())
    assert rejected.value.status_code == 503
    assert rejected.value.detail == "PHASE6_SNAPSHOT_UNAVAILABLE"


def test_phase6_endpoint_maps_typed_errors_without_leaking_messages(monkeypatch, tmp_path):
    from fastapi import HTTPException
    from web.backend import phase6_api

    monkeypatch.setattr(phase6_api, "require_project_access", lambda *_args: None)

    class StoreFailure(Exception):
        code = "PHASE6_SNAPSHOT_TAMPERED"

    def fail(**_kwargs):
        raise StoreFailure("sensitive /absolute/path and SQL details")

    endpoint = _endpoint(
        phase6_api.create_phase6_router(
            _settings(tmp_path, enabled=True), snapshot_loader=fail
        )
    )
    with pytest.raises(HTTPException) as rejected:
        endpoint("demo", None, _user())
    assert rejected.value.status_code == 503
    assert rejected.value.detail == "PHASE6_SNAPSHOT_TAMPERED"
    assert "sensitive" not in str(rejected.value.detail)

    def injected_http_exception(**_kwargs):
        raise HTTPException(
            status_code=418,
            detail="sensitive /absolute/path and SELECT secret FROM table",
        )

    endpoint = _endpoint(
        phase6_api.create_phase6_router(
            _settings(tmp_path, enabled=True),
            snapshot_loader=injected_http_exception,
        )
    )
    with pytest.raises(HTTPException) as rejected:
        endpoint("demo", None, _user())
    assert rejected.value.status_code == 503
    assert rejected.value.detail == "PHASE6_SNAPSHOT_UNAVAILABLE"


def test_main_registers_endpoint_without_importing_phase6_core(tmp_path):
    auth_db = tmp_path / "auth.db"
    environment = os.environ.copy()
    environment.update(
        {
            "JWT_SECRET": "j" * 32,
            "ADMIN_PASSWORD": "phase6-main-test-password",
            "AUTH_DB_FILE": str(auth_db),
            "FACTORY_ROOT": str(tmp_path),
            "PHASE6_SNAPSHOT_ENABLED": "false",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from web.backend.main import app; "
                "paths=set(app.openapi()['paths']); "
                "print('/api/projects/{base_name}/phase6-snapshot' in paths); "
                "print('factory_core.phase6_snapshot_grants' in sys.modules)"
            ),
        ],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "True\nFalse\n"


def test_phase6_http_route_enforces_auth_and_real_project_acl(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from web.backend.auth import create_access_token
    from web.backend.auth_store import AuthStore
    from web.backend.phase6_api import create_phase6_router

    settings = _settings(tmp_path, enabled=True)
    store = AuthStore(settings.resolved_auth_db_file)
    store.initialize()
    store.bootstrap_admin(settings.admin_password)
    store.register_user("alice", "alice phase6 password")
    store.approve_user("alice", actor="admin")
    issued_token = create_access_token(settings, "alice", "user", "active")
    monkeypatch.setenv("PHASE46_TEST_ACCESS_TOKEN", issued_token)
    runtime_token = os.environ["PHASE46_TEST_ACCESS_TOKEN"]
    loader_calls = []

    def loader(**kwargs):
        loader_calls.append(kwargs)
        return _ready_payload()

    app = FastAPI()
    app.include_router(create_phase6_router(settings, snapshot_loader=loader))
    with TestClient(app) as client:
        unauthenticated = client.get("/api/projects/demo/phase6-snapshot")
        assert unauthenticated.status_code in {401, 403}
        assert loader_calls == []

        denied = client.get(
            "/api/projects/demo/phase6-snapshot",
            headers={"Authorization": f"Bearer {runtime_token}"},
        )
        assert denied.status_code == 404
        assert denied.json()["detail"] == "PROJECT_NOT_FOUND"
        assert loader_calls == []

        store.grant_project_owner("demo", "alice", actor="admin")
        allowed = client.get(
            "/api/projects/demo/phase6-snapshot",
            headers={"Authorization": f"Bearer {runtime_token}"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["server_revision"] == 7
        assert len(loader_calls) == 1


@pytest.mark.parametrize(
    ("completeness", "expected_status"),
    (("COMPLETE", 200), ("PARTIAL", 404)),
)
def test_phase6_http_endpoint_uses_real_store_and_fails_partial_closed(
    tmp_path,
    monkeypatch,
    completeness,
    expected_status,
):
    """Exercise HTTP -> lazy core adapter -> durable SQLite without a loader stub."""

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from web.backend import phase6_api
    from web.backend.auth import create_access_token
    from web.backend.auth_store import AuthStore

    settings = _settings(tmp_path, enabled=True)
    snapshot_id = _create_real_phase6_snapshot(
        settings.resolved_phase6_snapshot_db_file,
        completeness=completeness,
    )
    auth_store = AuthStore(settings.resolved_auth_db_file)
    auth_store.initialize()
    auth_store.bootstrap_admin(settings.admin_password)
    auth_store.register_user("alice", "alice phase6 real-store password")
    auth_store.approve_user("alice", actor="admin")
    issued_token = create_access_token(settings, "alice", "user", "active")
    monkeypatch.setenv("PHASE46_TEST_ACCESS_TOKEN", issued_token)
    runtime_token = os.environ["PHASE46_TEST_ACCESS_TOKEN"]
    monkeypatch.setattr(phase6_api, "require_project_access", lambda *_args: None)

    app = FastAPI()
    # Deliberately omit snapshot_loader: the route must lazy-load the real
    # factory_core adapter and read the caller-selected durable SQLite store.
    app.include_router(phase6_api.create_phase6_router(settings))
    with TestClient(app) as client:
        response = client.get(
            "/api/projects/demo/phase6-snapshot?expected_revision=7",
            headers={"Authorization": f"Bearer {runtime_token}"},
        )

    assert response.status_code == expected_status
    if completeness == "PARTIAL":
        assert response.json()["detail"] == "PHASE6_SOURCE_INELIGIBLE"
        return
    payload = response.json()
    assert payload["state"] == "ready"
    assert payload["snapshot_id"] == snapshot_id
    assert payload["revision"] == payload["server_revision"] == 7
    assert payload["coordinate"] == {
        "snapshot_id": snapshot_id,
        "revision": 7,
    }
    assert all(
        payload[field] is False
        for field in (
            "authoritative",
            "authority_transferred",
            "dispatch_performed",
        )
    )


def test_phase6_frontend_gate_projection_and_request_lifecycle():
    script = r"""
import assert from 'node:assert/strict'
import { phase6FullShadowEnabled } from './web/frontend/src/lib/phase6RuntimeGate.js'
import { buildVerifiedPhase6SnapshotViewModel } from './web/frontend/src/lib/phase6SnapshotProjection.js'
import { nextPhase6ActionIndex } from './web/frontend/src/lib/phase6RovingFocus.js'
import {
  Phase6SnapshotClientError,
  createPhase6SnapshotRequestCoordinator,
  phase6SnapshotUrl,
  requestPhase6ProjectSnapshot,
} from './web/frontend/src/lib/phase6SnapshotClient.js'

assert.equal(phase6FullShadowEnabled({}), false)
assert.equal(phase6FullShadowEnabled({ VITE_PHASE6_FULL_SHADOW_ENABLED: true }), false)
assert.equal(phase6FullShadowEnabled({ VITE_PHASE6_FULL_SHADOW_ENABLED: 'TRUE' }), false)
assert.equal(phase6FullShadowEnabled({ VITE_PHASE6_FULL_SHADOW_ENABLED: 'true' }), true)
assert.equal(phase6SnapshotUrl('a/b', 7), '/api/projects/a%2Fb/phase6-snapshot?expected_revision=7')
assert.equal(nextPhase6ActionIndex('ArrowDown', 0, 3), 1)
assert.equal(nextPhase6ActionIndex('ArrowRight', 2, 3), 0)
assert.equal(nextPhase6ActionIndex('ArrowUp', 0, 3), 2)
assert.equal(nextPhase6ActionIndex('ArrowLeft', 1, 3), 0)
assert.equal(nextPhase6ActionIndex('Home', 2, 3), 0)
assert.equal(nextPhase6ActionIndex('End', 0, 3), 2)
assert.equal(nextPhase6ActionIndex('Tab', 0, 3), null)
assert.equal(nextPhase6ActionIndex('ArrowDown', 0, 0), null)

const snapshotId = 'b'.repeat(64)
const readyPayload = {
  schema_version: 'phase6-project-snapshot-web-v1',
  state: 'ready',
  project_id: 'demo',
  snapshot_id: snapshotId,
  revision: 7,
  server_revision: 7,
  coordinate: { snapshot_id: snapshotId, revision: 7 },
  sections: [{ key: 'summary', data: { nested: [1] } }],
  actions: [],
  authoritative: false,
  authority_transferred: false,
  dispatch_performed: false,
}
const ready = buildVerifiedPhase6SnapshotViewModel(readyPayload, 'demo')
assert.equal(ready.state, 'ready')
assert.equal(ready.actionCenter.clear, true)
assert.equal(Object.isFrozen(ready), true)
assert.equal(Object.isFrozen(ready.sections[0].data.nested), true)
assert.equal(ready.project_id, 'demo')

for (const unsafe of [
  { ...readyPayload, authoritative: true },
  { ...readyPayload, authority_transferred: true },
  { ...readyPayload, dispatch_performed: true },
]) {
  const view = buildVerifiedPhase6SnapshotViewModel(unsafe)
  assert.equal(view.state, 'unknown')
  assert.equal(view.actionCenter.clear, false)
  assert.deepEqual(view.sections, [])
}
assert.equal(
  buildVerifiedPhase6SnapshotViewModel({ ...readyPayload, server_revision: 8 }).reason_code,
  'MIXED_SERVER_REVISION',
)
const invalidActions = buildVerifiedPhase6SnapshotViewModel({
  ...readyPayload,
  actions: [{ id: ' ' }],
})
assert.equal(invalidActions.state, 'unknown')
assert.equal(invalidActions.actionCenter.clear, false)
assert.equal(
  buildVerifiedPhase6SnapshotViewModel({ ...readyPayload, project_id: 'other' }, 'demo').reason_code,
  'INVALID_PROJECT_BINDING',
)
assert.equal(
  buildVerifiedPhase6SnapshotViewModel({ ...readyPayload, schema_version: 'future-v2' }, 'demo').state,
  'unknown',
)

const fetchCalls = []
const tokenValue = process.env.PHASE46_NODE_TEST_TOKEN
assert.ok(tokenValue)
const bearerScheme = 'Bearer'
const fetched = await requestPhase6ProjectSnapshot('demo', {
  expectedRevision: 7,
  tokenProvider: () => tokenValue,
  fetchImpl: async (url, options) => {
    fetchCalls.push([url, options])
    return { ok: true, status: 200, json: async () => readyPayload }
  },
})
assert.equal(fetched.snapshot_id, snapshotId)
assert.equal(fetchCalls[0][0], '/api/projects/demo/phase6-snapshot?expected_revision=7')
assert.equal(fetchCalls[0][1].headers.Authorization, bearerScheme + ' ' + tokenValue)
assert.equal(fetchCalls[0][1].cache, 'no-store')

let tries = []
const staleCoordinator = createPhase6SnapshotRequestCoordinator({
  request: async (_base, { expectedRevision }) => {
    tries.push(expectedRevision)
    if (tries.length === 1) {
      throw new Phase6SnapshotClientError('PHASE6_SNAPSHOT_STALE', { status: 409, serverRevision: 8 })
    }
    return { ...readyPayload, revision: 8, server_revision: 8, coordinate: { snapshot_id: snapshotId, revision: 8 } }
  },
})
const refreshed = await staleCoordinator.load('demo', { expectedRevision: 7 })
assert.equal(refreshed.applied, true)
assert.equal(refreshed.retried, true)
assert.deepEqual(tries, [7, null])

tries = []
const boundedCoordinator = createPhase6SnapshotRequestCoordinator({
  request: async () => {
    tries.push('call')
    throw new Phase6SnapshotClientError('PHASE6_SNAPSHOT_STALE', { status: 409 })
  },
})
const bounded = await boundedCoordinator.load('demo', { expectedRevision: 7 })
assert.equal(bounded.applied, true)
assert.equal(bounded.error.code, 'PHASE6_SNAPSHOT_STALE')
assert.equal(tries.length, 2)

let releaseOld
const oldPromise = new Promise((resolve) => { releaseOld = resolve })
const generationCoordinator = createPhase6SnapshotRequestCoordinator({
  request: async (base) => base === 'old' ? oldPromise : readyPayload,
})
const oldLoad = generationCoordinator.load('old')
const newLoad = await generationCoordinator.load('new')
releaseOld(readyPayload)
const oldResult = await oldLoad
assert.equal(newLoad.applied, true)
assert.equal(oldResult.applied, false)
assert.equal(oldResult.reason, 'stale_generation')

let rejectOldStale
let staleOldCalls = 0
const oldStalePromise = new Promise((_resolve, reject) => { rejectOldStale = reject })
const staleGenerationCoordinator = createPhase6SnapshotRequestCoordinator({
  request: async (base) => {
    if (base === 'stale-old') {
      staleOldCalls += 1
      return oldStalePromise
    }
    return readyPayload
  },
})
const staleOldLoad = staleGenerationCoordinator.load('stale-old', { expectedRevision: 7 })
await staleGenerationCoordinator.load('new')
rejectOldStale(new Phase6SnapshotClientError('PHASE6_SNAPSHOT_STALE', { status: 409 }))
const staleOldResult = await staleOldLoad
assert.equal(staleOldResult.applied, false)
assert.equal(staleOldResult.reason, 'stale_generation')
assert.equal(staleOldCalls, 1)
"""
    completed = subprocess.run(
        ["node", "--input-type=module"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
        env={**os.environ, "PHASE46_NODE_TEST_TOKEN": f"runtime-{os.getpid()}"},
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""


def test_phase6_panel_has_keyboard_and_aria_contract():
    panel = (
        REPO_ROOT
        / "web/frontend/src/components/Phase6ProjectSnapshotPanel.vue"
    ).read_text(encoding="utf-8")
    assert 'aria-labelledby="phase6-snapshot-title"' in panel
    assert ':aria-busy="loading ? \'true\' : \'false\'"' in panel
    assert ':role="errorState ? \'alert\' : \'status\'"' in panel
    assert '@keydown.enter.prevent="activateAction(action)"' in panel
    assert '@keydown.space.prevent="activateAction(action)"' in panel
    assert 'role="toolbar"' in panel
    assert ':disabled="action.disabled === true"' in panel
    assert ':aria-disabled="action.disabled === true ? \'true\' : \'false\'"' in panel
    assert (
        ':tabindex="action.disabled !== true && '
        'actionIndex === activeActionIndex ? 0 : -1"'
        in panel
    )
    assert ':data-action-id="action.id"' in panel
    assert '@keydown="moveActionFocus($event, actionIndex)"' in panel
    assert "v-html" not in panel
