import asyncio
import json

import pytest

from factory_core.joint_modeling import ATTESTATIONS, CANDIDATE_GATE, policy
from factory_core.service import FactoryService
from factory_core.storage import SQLiteStateStore
from test_joint_modeling import pending_request, pro_answer, project
from test_web_control_plane_api import load_main_module


def endpoint(mod, path, method="GET"):
    return next(route.endpoint for route in mod.app.routes if route.path == path and method in route.methods)


def test_project_config_api_defaults_off_and_requires_manual_revision(tmp_path):
    project = tmp_path / "ongoing" / "demo"
    project.mkdir(parents=True)
    store = SQLiteStateStore(project)
    store.initialize(project_id="demo", project_type="modeling", runtime_generation="native_v2")
    mod = load_main_module(tmp_path, tmp_path / "web/auth.db")
    user = mod.UserInfo(username="admin", role="admin")
    path = "/api/projects/{base_name}/joint-modeling"
    get = endpoint(mod, path)
    put = endpoint(mod, path, "PUT")
    assert asyncio.run(get("demo", current_user=user))["enabled"] is False
    payload = mod.project_api.JointModelingConfigPayload(enabled=True, expected_revision=store.load().revision)
    assert asyncio.run(put("demo", payload, current_user=user))["enabled"] is True
    with pytest.raises(mod.HTTPException) as exc:
        asyncio.run(put("demo", payload, current_user=user))
    assert exc.value.status_code == 409
    assert policy(tmp_path / "other")["enabled"] is False


def test_joint_endpoints_check_project_access_before_read_or_write(project, monkeypatch):
    mod = load_main_module(project.parents[1], project.parents[1] / "web/auth.db")
    user = mod.UserInfo(username="unrelated-user", role="user")
    def denied(*args):
        raise mod.HTTPException(status_code=403, detail="not assigned")
    monkeypatch.setattr(mod.project_api, "require_project_access", denied)
    before = SQLiteStateStore(project).load().revision
    paths = ["/api/projects/{base_name}/joint-modeling", "/api/projects/{base_name}/joint-modeling/consultation-package"]
    for path in paths:
        with pytest.raises(mod.HTTPException) as exc:
            asyncio.run(endpoint(mod, path)("demo", current_user=user))
        assert exc.value.status_code == 403
    with pytest.raises(mod.HTTPException) as exc:
        asyncio.run(endpoint(mod, paths[0], "PUT")("demo", mod.project_api.JointModelingConfigPayload(enabled=False, expected_revision=before), current_user=user))
    assert exc.value.status_code == 403
    assert SQLiteStateStore(project).load().revision == before


def test_joint_answer_api_requires_binding_then_commits_exact_answer(project, monkeypatch):
    package, request = pending_request(project)
    store = SQLiteStateStore(project)
    mod = load_main_module(project.parents[1], project.parents[1] / "web/auth.db")
    user = mod.UserInfo(username="admin", role="admin")
    answer = json.dumps(pro_answer(package, request))
    submit = endpoint(mod, "/api/projects/{base_name}/consultation/answer", "POST")
    incomplete = mod.project_api.ConsultationAnswer(answer=answer, expected_revision=store.load().revision, attestations={})
    with pytest.raises(mod.HTTPException) as exc:
        asyncio.run(submit("demo", incomplete, current_user=user))
    assert exc.value.status_code == 409
    assert store.decision(CANDIDATE_GATE) is None
    assert not (project / ".factory/decision_staging").exists()

    class NoWorkerService:
        def __init__(self, root):
            self.root = root
        def resolve_and_start(self, project, resolution, *, evidence_writer, expected_revision=None):
            return FactoryService(self.root).resolve(project, resolution, expected_revision=expected_revision), None

    monkeypatch.setattr(mod.project_api, "FactoryService", NoWorkerService)
    complete = mod.project_api.ConsultationAnswer(answer=answer, expected_revision=store.load().revision,
        **{key: request[key] for key in ("request_id", "generation", "subject_fingerprint", "options_fingerprint")},
        attestations={key: True for key in ATTESTATIONS})
    assert asyncio.run(submit("demo", complete, current_user=user))["status"] == "ok"
    assert store.decision(CANDIDATE_GATE)["answer"] == answer
