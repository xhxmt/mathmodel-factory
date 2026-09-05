import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory_core.domain import ExecutionResult
from factory_core.phase9_runtime import Phase9RuntimeError, run_step13_components, _ObservedDispatcher
from factory_core.adapters.models.backends import ModelRequest


def _setup(tmp_path, monkeypatch, prepared=True):
    source, project, records = (tmp_path / name for name in ("source", "project", "records"))
    source.mkdir()
    project.mkdir()
    calls = []

    class Step:
        def prepare_packets(self, context):
            calls.append("prepare")
            return (ExecutionResult.succeeded() if prepared
                    else ExecutionResult.failed("MISSING_PACKET", returncode=2))

        def execute_precheck(self, context):
            calls.append("math_only")
            return ExecutionResult.succeeded(judge_verdict="PRECHECK_PASS")

        def execute_prepared(self, context):
            calls.append("three_roles")
            return ExecutionResult.succeeded(judge_verdict="INDETERMINATE_REVIEW",
                                             resume_after_step=11)

    step = Step()
    monkeypatch.setattr("factory_core.phase9_runtime.build_native_registry",
                        lambda source: SimpleNamespace(get=lambda n: SimpleNamespace(lifecycle=step)))
    monkeypatch.setattr("factory_core.phase9_runtime._source_identity", lambda p: {"fixture": True})
    monkeypatch.setattr("scripts.judge_packet.packet_fingerprints", lambda p: {"math": "fixture"})
    return dict(source=source, project=project, records=records,
                timeout_seconds=5, total_timeout_seconds=30), calls


@pytest.mark.parametrize("mode,called,status", [
    ("NORMAL_STEP13", "math_only", "COMPONENT_PASS"),
    ("FORENSIC_THREE_ROLE", "three_roles", "BLOCKED"),
])
def test_coordinator_routes_roles_without_promoting_component_exit(tmp_path, monkeypatch, mode, called, status):
    kwargs, calls = _setup(tmp_path, monkeypatch)
    result = run_step13_components(**kwargs, mode=mode)
    assert calls == ["prepare", called]
    assert result["status"] == status
    assert result["formal_phase9_completed"] is False
    assert result["delivery_capability"] == "DISABLED"
    assert json.loads((kwargs["records"] / "terminal.json").read_text()) == result


def test_failed_preparation_has_no_dispatch(tmp_path, monkeypatch):
    kwargs, calls = _setup(tmp_path, monkeypatch, prepared=False)
    result = run_step13_components(**kwargs, mode="FORENSIC_THREE_ROLE")
    assert calls == ["prepare"]
    assert result["model_dispatch_count"] == 0
    assert result["status"] == "BLOCKED"


def test_prepare_only_and_attempt_reservation(tmp_path, monkeypatch):
    kwargs, calls = _setup(tmp_path, monkeypatch)
    result = run_step13_components(**kwargs, mode="NORMAL_STEP13", prepare_only=True)
    assert result["status"] == "PREPARED"
    assert calls == ["prepare"]
    with pytest.raises(FileExistsError):
        run_step13_components(**kwargs, mode="NORMAL_STEP13")
    assert calls == ["prepare"]


def test_ablation_cannot_enter_technical_component_runner(tmp_path, monkeypatch):
    kwargs, calls = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("ABLATE_NO_JUDGE", "1")
    with pytest.raises(Phase9RuntimeError, match="separate"):
        run_step13_components(**kwargs, mode="FORENSIC_THREE_ROLE")
    assert not calls


def test_dispatch_exception_is_counted_as_uncertain_attempt(tmp_path):
    calls = tmp_path / "calls"
    calls.mkdir()
    dispatcher = _ObservedDispatcher(tmp_path, calls, lambda: None, "gpt-6-astra", "medium")

    def uncertain(request):
        assert request.model == "gpt-6-astra"
        assert request.effort == "medium"
        raise RuntimeError("transport outcome unavailable")

    dispatcher.backend = SimpleNamespace(execute=uncertain)
    request = ModelRequest(tmp_path, 13, 1, "prompt", 5, 5,
                           output_file=tmp_path / "math.md")
    with pytest.raises(RuntimeError, match="unavailable"):
        dispatcher.execute(request, step_key=13, defaults=("ignored",))
    assert len(dispatcher.calls) == 1
    assert dispatcher.calls[0]["status"] == "OUTCOME_UNCERTAIN"
    assert len(list(calls.glob("*/completion.json"))) == 1


def test_total_deadline_caps_preparation_and_prepare_only_cannot_outlive_it(tmp_path, monkeypatch):
    from factory_core.deadline import cap_timeout

    kwargs, calls = _setup(tmp_path, monkeypatch)
    now = [1000]
    monkeypatch.setattr("time.time", lambda: now[0])

    class SlowPreparation:
        def prepare_packets(self, context):
            assert cap_timeout(600) == 30
            now[0] += 31
            return ExecutionResult.succeeded()

    monkeypatch.setattr("factory_core.phase9_runtime.build_native_registry",
                        lambda source: SimpleNamespace(get=lambda n: SimpleNamespace(lifecycle=SlowPreparation())))
    result = run_step13_components(**kwargs, mode="NORMAL_STEP13", prepare_only=True)
    assert result["status"] == "BLOCKED"
    assert "deadline" in result["reason"]
    assert cap_timeout(600) == 600  # context was restored


def test_records_cannot_overlap_executing_source(tmp_path, monkeypatch):
    kwargs, calls = _setup(tmp_path, monkeypatch)
    kwargs["records"] = kwargs["source"] / "observations"
    with pytest.raises(Phase9RuntimeError, match="overlap"):
        run_step13_components(**kwargs, mode="NORMAL_STEP13")
    assert not kwargs["records"].exists()
    assert not calls


@pytest.mark.parametrize("returncode,timed_out", [(1, False), (124, True)])
def test_post_launch_nonzero_is_uncertain_and_never_automatically_resent(tmp_path, monkeypatch, returncode, timed_out):
    from factory_core.adapters.models.backends import CodexCliBackend
    observations, external = [], []
    calls = tmp_path / "calls"
    calls.mkdir()
    class Authority:
        def reserve_attempt(self, *args, **kwargs):
            if observations:
                raise RuntimeError("uncertain; automatic resend prohibited")
            return {"attempt_id": "UNIT", "provider_call": kwargs["provider_call"]}
        def observe(self, intent, observation):
            observations.append(observation)
    profile = {"native": {"path": "/UNIT/codex"}}
    monkeypatch.setattr("factory_core.phase9_provider_identity.provider_identity", lambda *args: profile)
    def failed(self, request):
        external.append(request)
        self.supervisor.launch_record = {"launch_sha256": "5" * 64}
        self.supervisor.active_count = 0
        return ExecutionResult.failed("TRANSIENT_TIMEOUT", returncode=returncode, process_pid=123, process_timed_out=timed_out)
    monkeypatch.setattr(CodexCliBackend, "execute", failed)
    dispatcher = _ObservedDispatcher(tmp_path, calls, lambda: None, "gpt-6-astra", "medium", authority=Authority(), runtime_id="UNIT")
    request = ModelRequest(tmp_path, 13, 1, "prompt", 5, 5, output_file=tmp_path / "math.md")
    dispatcher.execute(request, step_key=13, defaults=())
    assert observations[0]["outcome"] == "UNCERTAIN"
    with pytest.raises(RuntimeError, match="resend prohibited"):
        dispatcher.execute(request, step_key=13, defaults=())
    assert len(external) == 1


def test_accepted_final_response_is_sealed_instead_of_nonempty_primary(tmp_path):
    import hashlib
    calls = tmp_path / "calls"
    calls.mkdir()
    (calls / "unit").mkdir()
    dispatcher = _ObservedDispatcher(tmp_path, calls, lambda: None, "gpt-6-astra", "medium")
    raw = b"VERDICT: PASS\n"
    digest = {"sha256": hashlib.sha256(raw).hexdigest(), "byte_length": len(raw)}
    dispatcher.calls.append({"role": "paper", "invocation_id": "unit", "outputs": {
        "output": {"sha256": "0" * 64, "byte_length": 15}, "final_response": digest}})
    output = tmp_path / "paper.md"
    output.write_bytes(raw)
    dispatcher.record_accepted_output("paper", output)
    selected = json.loads((calls / "unit/accepted_output.json").read_text())
    assert selected["source"] == "final_response"
    assert selected["sha256"] == digest["sha256"]
    output.write_bytes(b"unobserved")
    with pytest.raises(Phase9RuntimeError, match="not observed"):
        dispatcher.record_accepted_output("paper", output)


def test_provider_identity_rejects_script_and_binds_ancestor_configuration(tmp_path, monkeypatch):
    import factory_core.phase9_provider_identity as provider
    script = tmp_path / "codex"
    script.write_bytes(b"#!/usr/bin/env python3\nprint('standin')\n")
    monkeypatch.setenv("CODEX_CLI_PATH", str(script))
    with pytest.raises(ValueError, match="native Codex"):
        provider.provider_identity(tmp_path)
    # Identity-only ELF fixture, never executed or promoted to a provider.
    script.write_bytes(b"\x7fELFUNIT_FIXTURE_NOT_EXECUTABLE")
    before = provider.provider_identity(tmp_path)
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text('model = "changed"\n')
    assert provider.provider_identity(tmp_path) != before
