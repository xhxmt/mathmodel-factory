"""Phase 1e: a failure event names the failure that actually happened."""

from __future__ import annotations

from pathlib import Path

from factory_core.domain import ExecutionResult, ValidationResult, WorkflowStatus
from factory_core.engine import FactoryEngine
from factory_core.registry import StepDefinition, StepRegistry
from factory_core.storage import SQLiteStateStore
from scripts.step8_5_gate import collect_step8_5_state, not_ready_reason


class _Step:
    def __init__(self, result: ExecutionResult, reason: str) -> None:
        self.result, self.reason = result, reason

    def execute(self, context):
        return self.result

    def validate(self, context):
        return ValidationResult.invalid(self.reason)


def _fail_once(tmp_path: Path, result: ExecutionResult, reason: str):
    step = _Step(result, reason)
    registry = StepRegistry()
    registry.register(StepDefinition(id=1, name="first", timeout_seconds=30, max_attempts=1,
                                     handler=step, validator=step))
    store = SQLiteStateStore(tmp_path)
    store.initialize(project_id="demo", project_type="modeling")
    state = FactoryEngine(tmp_path, store=store, registry=registry, sleeper=lambda _: None).run()
    assert state.status is WorkflowStatus.FAILED
    return store.events()[-1].payload


def test_a_backend_failure_is_named_before_the_missing_artifact(tmp_path):
    # run4 rev 987-1005: codex exited after about one second and the event
    # read "Step 8.5 reviewer-entry gate is not ready: None".
    payload = _fail_once(
        tmp_path,
        ExecutionResult.failed("TRANSIENT_MODEL_BACKEND", returncode=1, backend="codex",
                               model_id="codex", duration_seconds=1.0007),
        "Step 8.5 reviewer-entry gate is not ready: missing or empty: entry_gate.md",
    )
    assert payload["error_class"] == "TRANSIENT_MODEL_BACKEND"
    assert payload["reason"] == (
        "TRANSIENT_MODEL_BACKEND: codex exited with returncode 1 after 1.0 s; "
        "then Step 8.5 reviewer-entry gate is not ready: missing or empty: entry_gate.md"
    )
    assert payload["validation_reason"].startswith("Step 8.5 reviewer-entry gate is not ready")


def test_a_backend_failure_keeps_its_own_reason(tmp_path):
    payload = _fail_once(
        tmp_path,
        ExecutionResult.failed("PERMANENT_BACKEND_UNAVAILABLE", returncode=2, model_id="dsh-deepseek",
                               reason="HTTP 402: insufficient balance"),
        "Step 9 paper draft invalid",
    )
    assert payload["reason"] == (
        "PERMANENT_BACKEND_UNAVAILABLE from dsh-deepseek: HTTP 402: insufficient balance; "
        "then Step 9 paper draft invalid"
    )


def test_audit_and_judge_failures_keep_the_validator_message(tmp_path):
    payload = _fail_once(
        tmp_path,
        ExecutionResult.failed("AUDIT_REPAIR_REQUIRED", returncode=1, backend="codex"),
        "paper audit FAIL: PAPER_AUDIT_FAILED",
    )
    assert payload["reason"] == "paper audit FAIL: PAPER_AUDIT_FAILED"
    assert "validation_reason" not in payload


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_the_reviewer_entry_gate_says_why_it_is_not_ready(tmp_path):
    project = tmp_path
    assert not_ready_reason(collect_step8_5_state(project)) == (
        "missing or empty: anchor_figure_plan.md, entry_gate.md, reviewer_entry_map.md"
    )
    _write(project / "reviewer_entry_map.md", "map\n")
    _write(project / "anchor_figure_plan.md", "plan\n")
    _write(project / "entry_gate.md", "VERDICT: REVISE\n")
    assert not_ready_reason(collect_step8_5_state(project)) == "entry_gate.md verdict is REVISE"
    _write(project / "entry_gate.md", "no verdict yet\n")
    assert "no VERDICT" in not_ready_reason(collect_step8_5_state(project))
    _write(project / "entry_gate.md", "VERDICT: PASS\n")
    assert not_ready_reason(collect_step8_5_state(project)) == ""
