import pytest

from factory_core.domain import ExecutionResult, InvalidTransition, ValidationResult, WorkflowStatus
from factory_core.engine import FactoryEngine
from factory_core.registry import StepDefinition, StepRegistry
from factory_core.storage import SQLiteStateStore
from factory_core.technical_continuation import authorize, execute
from tests.test_stage_scheduler import Lifecycle


@pytest.mark.parametrize("verdict", ["FAIL", "REVISE", "INDETERMINATE", "INFRA", "REOPEN"])
def test_authorized_failed_gate_routes_once_to_real_downstream_steps(tmp_path, verdict):
    calls = []

    class FailedGate(Lifecycle):
        def execute(self, context):
            calls.append(context.step_id)
            if verdict == "REOPEN":
                return ExecutionResult.failed("JUDGE_FAIL", resume_after_step=3)
            return ExecutionResult.failed("TRANSIENT_JUDGE_INFRASTRUCTURE" if verdict == "INFRA" else "JUDGE_FAIL",
                                           judge_verdict=verdict)
        def validate(self, context):
            return ValidationResult.invalid(verdict)

    class Downstream(Lifecycle):
        def execute(self, context):
            calls.append(context.step_id)
            state = store.load()
            assert state.active_step == state.source_step_id == context.step_id
            assert state.active_stage == 9
            return ExecutionResult.succeeded()
        def execute_analysis(self, context):
            assert context.step_id == 16 and store.load().active_stage == 10
            calls.append(16)
            return ExecutionResult.failed("PERMANENT_JUDGE_EVIDENCE_BINDING", evidence_valid=False)

    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id=tmp_path.name, project_type="math_modeling", last_completed_step=12)
    registry = StepRegistry()
    for step_id in range(13, 17):
        registry.register(StepDefinition(step_id, f"step{step_id}", 30, 1,
                           step=FailedGate() if step_id == 13 else Downstream()))
    engine = FactoryEngine(tmp_path, store=store, registry=registry)
    authorize(engine, expected_revision=state.revision, reason="explicit downstream evaluation")
    stopped = engine.run()
    assert stopped.status == WorkflowStatus.PAUSED
    assert calls == [13]
    final = execute(engine, expected_revision=stopped.revision)
    assert calls == [13, 14, 15, 16]
    assert final.status == WorkflowStatus.PAUSED and final.last_completed_step == 12
    assert not any(e.type == "STEP_SUCCEEDED" and e.step == 13 for e in store.events())
    assert any(e.type in {"STEP_FAILED", "STEP_REOPENED"} and e.step == 13 for e in store.events())
    before = store.path.read_bytes()
    with pytest.raises(InvalidTransition, match="consumed"):
        execute(engine, expected_revision=final.revision)
    assert store.path.read_bytes() == before
