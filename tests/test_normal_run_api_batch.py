"""Native Judge -> dispatcher -> API adapter -> runner -> frozen receipt."""

import hashlib
import json
from pathlib import Path
import sys

import pytest

from factory_core.adapters.infrastructure.commands import CommandRunner
from factory_core.adapters.infrastructure.process import ProcessResult
from factory_core.adapters.models.backends import ApiAgentBackend
from factory_core.adapters.models.dispatcher import ModelDispatcher, ModelPolicy
from factory_core.domain import StepContext
from factory_core.judge_batch import verify
from factory_core.registry import ModelBackendRegistry
from factory_core.steps import STEP_CONTRACTS
from factory_core.steps.prompting import PromptRenderer
from factory_core.steps.specialized import JudgeStep
from scripts import api_agent_run
from tests.test_native_orchestration import AlwaysValidValidator, FakeCommandRunner


def native_api(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    preparation = FakeCommandRunner()
    preparation.python(root, tmp_path, "scripts/build_objective_evidence.py", [], label="objective")
    preparation.python(root, tmp_path, "scripts/judge_packet.py", [], label="packets")
    calls = []

    def provider(prompt, *args, **kwargs):
        # Observe the prepared input before returning the controlled network response.
        frozen = list((tmp_path / "judge_outputs/batches").glob("*/*/input_prompt"))
        assert any(path.read_text() == prompt for path in frozen)
        calls.append(prompt)
        return "VERDICT: PASS\n{\"schema_version\":\"judge-role-v1\"}\n"

    monkeypatch.setattr(api_agent_run.llm_judge_call, "call", provider)

    class ApiTransport:
        before_runner = None

        def run(self, request):
            if self.before_runner is not None:
                self.before_runner()
            # Use the adapter's actual CLI arguments and the real runner main.
            with monkeypatch.context() as patch:
                patch.setattr(sys, "argv", list(request.argv)[1:])
                code = api_agent_run.main()
            return ProcessResult(code, False, 0.01, 123)

    transport = ApiTransport()
    registry = ModelBackendRegistry()
    registry.register("openai", ApiAgentBackend(root, supervisor=transport))
    dispatcher = ModelDispatcher(root, registry)
    monkeypatch.setattr(dispatcher, "policy_for", lambda *a: ModelPolicy("test-api"))
    monkeypatch.setattr(dispatcher, "_entry", lambda model_id: (
        dict(backend="openai", model="test-api", effort="", base_url="", key_env="")
        if model_id == "test-api" else None
    ))
    step = JudgeStep(next(c for c in STEP_CONTRACTS if c.id == 13), root,
                     PromptRenderer(root), dispatcher, AlwaysValidValidator(), CommandRunner())

    def run(role="math", step_id=16):
        template = "paper_reviewer" if role == "paper" else role + "_auditor"
        return step._run_role(StepContext(tmp_path, tmp_path.name, step_id, 1, 60, 0),
                              role, f"judges/{template}.txt")

    return run, transport, calls


@pytest.mark.parametrize("role,step_id", [("math", 13), ("math", 16),
                                          ("execution", 16), ("paper", 16)])
def test_native_api_freezes_and_reuses_exact_sent_input(tmp_path, monkeypatch, role, step_id):
    run, _, calls = native_api(tmp_path, monkeypatch)
    first = run(role, step_id)
    assert first.returncode == 0, first
    binding = first.metadata["audit_binding"]
    folder = tmp_path / binding["archive"]
    request = json.loads((folder / "request.json").read_text())
    response, metadata = verify(tmp_path, binding)
    actual_hash = hashlib.sha256(calls[0].encode()).hexdigest()
    assert request["prompt_format"] == "api-inline-v1"
    assert request["template_prompt_sha256"] != actual_hash
    assert request["prompt_sha256"] == metadata["effective_prompt_sha256"] == actual_hash
    assert metadata["rendered_prompt_sha256"] == actual_hash
    assert metadata["execution_step_id"] == step_id
    assert (folder / "input_prompt").read_text() == (folder / "prompt").read_text() == calls[0]
    assert hashlib.sha256(response).hexdigest() == metadata["response_sha256"]
    second = run(role, step_id)
    assert second.returncode == 0 and second.metadata["reused"]
    assert second.metadata["call_id"] == first.metadata["call_id"]
    assert len(calls) == 1


def test_api_rejects_context_change_before_network_boundary(tmp_path, monkeypatch):
    run, transport, calls = native_api(tmp_path, monkeypatch)
    transport.before_runner = lambda: (tmp_path / "judge_packets/math/context.txt").write_text("updated evidence")
    result = run()
    assert result.returncode != 0
    assert calls == []
    assert not list((tmp_path / "judge_outputs/batches").rglob("committed.json"))


def test_api_metadata_must_still_match_response(tmp_path, monkeypatch):
    run, _, calls = native_api(tmp_path, monkeypatch)
    assert run().returncode == 0
    (tmp_path / "judge_outputs/math.md").write_text("VERDICT: INDETERMINATE\n")
    assert run().error_class == "PERMANENT_JUDGE_EVIDENCE_BINDING"
    assert len(calls) == 1
