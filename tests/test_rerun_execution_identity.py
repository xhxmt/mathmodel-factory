from pathlib import Path

import pytest

from factory_core.domain import ExecutionResult, StepContext
from factory_core.steps import STEP_CONTRACTS
from factory_core.steps.prompting import PromptRenderer
from factory_core.steps.specialized import JudgeStep
from scripts.submission_fingerprint import evaluator_contract_payload
from tests.test_native_orchestration import AlwaysValidValidator, FakeCommandRunner


@pytest.mark.parametrize("step_id", [13, 16])
def test_judge_uses_actual_execution_identity(tmp_path, step_id):
    root = Path(__file__).resolve().parents[1]
    runner = FakeCommandRunner()
    runner.python(root, tmp_path, "scripts/build_objective_evidence.py", [], label="objective")
    runner.python(root, tmp_path, "scripts/judge_packet.py", [], label="packets")
    calls = []

    class Dispatcher:
        def execute(self, request, **kwargs):
            calls.append((request, kwargs))
            request.output_file.write_text("VERDICT: PASS\n{}\n")
            return ExecutionResult.succeeded(model_id="test", backend="codex", model="test")

    step = JudgeStep(next(c for c in STEP_CONTRACTS if c.id == 13), root,
                     PromptRenderer(root), Dispatcher(), AlwaysValidValidator(), runner)
    result = step._run_role(StepContext(tmp_path, tmp_path.name, step_id, 1, 60, 0),
                            "math", "judges/math_auditor.txt")
    assert result.returncode == 0
    request, kwargs = calls[0]
    assert request.step_id == kwargs["step_key"] == step_id
    assert f"AGENT_KEY: step{step_id}_math" in request.prompt
    assert result.metadata["execution_step_id"] == step_id
    assert result.metadata["template_step_id"] == 13


def test_final_evaluator_uses_step16_configuration(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "model_config.json").write_text(
        '{"_default":{"step_13":{"primary":"precheck"},"step_16":{"primary":"final"}}}')
    contract = evaluator_contract_payload("demo", tmp_path)
    assert contract["execution_step_id"] == 16
    assert contract["model_dispatch"]["selection"]["primary_id"] == "final"
    precheck = evaluator_contract_payload("demo", tmp_path, execution_step_id=13)
    assert precheck["model_dispatch"]["selection"]["primary_id"] == "precheck"


@pytest.mark.parametrize("step_id", [14, 15])
def test_paused_upstream_cursor_cannot_stamp_downstream_prompt_identity(tmp_path, step_id):
    from factory_core.storage import SQLiteStateStore
    from factory_core.domain import WorkflowStatus
    from factory_core.steps import build_native_registry
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id=tmp_path.name, project_type="math_modeling",
        last_completed_step=5, active_step=6, status=WorkflowStatus.PAUSED,
        scheduler_generation="stage_v1")
    class NoDispatch:
        def execute(self, *_args, **_kwargs):
            pytest.fail("identity mismatch must precede dispatch")
    root = Path(__file__).resolve().parents[1]
    registry = build_native_registry(root, dispatcher=NoDispatch())
    before = store.path.read_bytes()
    result = registry.get(step_id).lifecycle.execute(StepContext(tmp_path, tmp_path.name, step_id, 1, 60, state.revision))
    assert result.error_class == "PERMANENT_EXECUTION_IDENTITY_MISMATCH"
    assert store.path.read_bytes() == before
