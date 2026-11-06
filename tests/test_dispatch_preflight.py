"""Phase 3d: a provider or disk failure visible before dispatch pauses the run
without starting the Step, so no attempt is spent and no model is called."""

from __future__ import annotations

import json

from factory_core import dispatch_preflight
from factory_core.domain import WorkflowStatus
from factory_core.engine import FactoryEngine
from factory_core.stages import STAGE_SCHEDULER_GENERATION
from factory_core.storage import SQLiteStateStore
from factory_core.workflow_events import project_runtime_diagnostics
from tests.test_stage_scheduler import stage_registry, trust_seeded_reviewer_gate

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def _http(status, body=b"{}"):
    calls = []

    def get(url, headers, timeout):
        calls.append(url)
        assert "sk-test" not in url
        return status, body

    return get, calls


def _check(tmp_path, http_get, *, step=13, min_free_bytes=0):
    # Step 13's contract routes to deepseek-chat (api.deepseek.com): the
    # preflight probes the documented balance endpoint.
    return dispatch_preflight.check_dispatch(
        ROOT, tmp_path, step,
        environ={"DEEPSEEK_API_KEY": "sk-test-not-a-real-key"},
        http_get=http_get, min_free_bytes=min_free_bytes, mounts_text="",
    )


def test_a_402_balance_blocks_the_dispatch(tmp_path):
    get, calls = _http(402)

    report = _check(tmp_path, get)

    assert report is not None and report["step"] == 13
    assert calls and all("balance" in url for url in calls)
    assert "402" in report["message"]
    assert "sk-test" not in json.dumps(report)  # no credential in the report


def test_an_available_balance_does_not_block(tmp_path):
    body = json.dumps({"is_available": True, "balance_infos": [{"total_balance": "9", "currency": "CNY"}]})
    get, _ = _http(200, body.encode())
    assert _check(tmp_path, get) is None


def test_an_undecidable_provider_never_blocks(tmp_path):
    def unreachable(*_a):
        raise OSError("no network")

    assert _check(tmp_path, unreachable) is None  # UNKNOWN is not FAIL
    get, _ = _http(429)
    assert _check(tmp_path, get) is None  # a rate limit is WARN


def test_low_scratch_space_blocks(tmp_path):
    body = json.dumps({"is_available": True, "balance_infos": []})
    get, _ = _http(200, body.encode())
    report = _check(tmp_path, get, min_free_bytes=2**62)
    assert report is not None
    assert any(item["name"].startswith("scratch:") for item in report["blocking"])


def test_a_crashing_check_never_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch_preflight, "check_routes", lambda *a, **k: 1 / 0)
    assert _check(tmp_path, None) is None


def test_the_engine_pauses_before_starting_the_step_and_spends_no_attempt(tmp_path):
    registry, lifecycles = stage_registry()
    store = SQLiteStateStore(tmp_path)
    store.initialize(project_id="demo", project_type="modeling", last_completed_step=13,
                     scheduler_generation=STAGE_SCHEDULER_GENERATION)
    trust_seeded_reviewer_gate(store, tmp_path)
    report = {"step": 14, "blocking": [], "models": [], "message": "step:14:abstract: every candidate fails"}
    healthy = {"value": False}
    engine = FactoryEngine(
        tmp_path, store=store, registry=registry,
        dispatch_preflight=lambda definition: None if healthy["value"] else report,
    )

    paused = engine.run(max_steps=1)

    assert paused.status is WorkflowStatus.PAUSED
    assert lifecycles[14].calls == []  # no model call
    assert not any(event.type == "STEP_STARTED" for event in store.events())
    assert (paused.active_stage, paused.active_subtask, paused.attempt) == (9, "abstract", 0)
    event = store.events()[-1]
    assert event.payload["reason"]["subcode"] == "PROVIDER_UNAVAILABLE"
    assert event.payload["preflight"] == report
    status = project_runtime_diagnostics(store.events(), paused)["status"]
    assert status["reason_code"] == "PROVIDER_UNAVAILABLE"
    assert status["suggested_actions"][0] == "resume_project"
    assert status["preflight"] == report

    healthy["value"] = True  # the balance was topped up
    engine.resume(expected_revision=paused.revision)
    state = engine.run(max_steps=1)

    assert lifecycles[14].calls == [1]  # attempt 1: nothing was spent
    assert state.last_completed_step >= 14


def test_the_service_hook_honours_the_switch(tmp_path, monkeypatch):
    monkeypatch.setenv(dispatch_preflight.ENABLE_ENV, "0")
    assert dispatch_preflight.for_project(ROOT, tmp_path) is None
    monkeypatch.setenv(dispatch_preflight.ENABLE_ENV, "1")
    hook = dispatch_preflight.for_project(ROOT, tmp_path)
    assert callable(hook)


def test_the_v2_final_judge_is_not_probed_on_step_13_routes(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv(dispatch_preflight.ENABLE_ENV, "1")
    probed = []
    monkeypatch.setattr(dispatch_preflight, "check_dispatch", lambda *a, **k: probed.append(a[2]) or {"x": 1})
    hook = dispatch_preflight.for_project(ROOT, tmp_path)

    assert hook(SimpleNamespace(id=13, name="final_judge")) is None
    assert hook(SimpleNamespace(id=13, name="judge_gate")) == {"x": 1}
    assert probed == [13]
