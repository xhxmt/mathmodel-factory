from __future__ import annotations

import json
import sqlite3

import pytest

from factory_core.artifacts import ArtifactLayer, classify_artifact
from factory_core.contest import (
    CONTEST_DURATION_SECONDS,
    DELIVERY_RESERVE_SECONDS,
    ContestDeadlineExceeded,
    ContestPolicy,
    effective_timeout,
    phase_for_step,
)
from factory_core.domain import ExecutionResult, ValidationResult, WorkflowStatus
from factory_core.engine import FactoryEngine
from factory_core.registry import StepDefinition, StepRegistry
from factory_core.storage import SQLiteStateStore
from factory_core.steps.gates import prepare_human_gates
from factory_core.steps.catalog import catalog_payload
from factory_core.service import FactoryService
from web.backend.selection_service import (
    SelectionError,
    build_content_freeze_options,
    write_selection_decision,
)
from scripts.selection_gate import approve_content_freeze


class SuccessfulLifecycle:
    def __init__(self) -> None:
        self.timeouts: list[int] = []

    def prepare(self, context):
        from factory_core.domain import PrepareResult

        self.timeouts.append(context.timeout_seconds)
        return PrepareResult.prepared()

    def execute(self, context):
        self.timeouts.append(context.timeout_seconds)
        return ExecutionResult.succeeded()

    def validate(self, context):
        return ValidationResult.valid()

    def recover(self, context, error):  # pragma: no cover - not used here
        raise AssertionError("recovery should not run")


class ReopenLifecycle(SuccessfulLifecycle):
    def execute(self, context):
        return ExecutionResult.succeeded(resume_after_step=12)


def test_default_contest_policy_reserves_six_hours_for_delivery():
    policy = ContestPolicy.default(started_at=1_000)

    assert policy.contest_deadline_at == 1_000 + CONTEST_DURATION_SECONDS
    assert policy.content_freeze_at == policy.contest_deadline_at - DELIVERY_RESERVE_SECONDS
    assert policy.delivery_freeze_at == policy.contest_deadline_at - 2 * 3_600


def test_effective_timeout_uses_content_freeze_before_delivery_and_deadline_for_delivery():
    policy = ContestPolicy.default(started_at=1_000)

    assert effective_timeout(policy, step_id=15, step_timeout=10_800, now=policy.content_freeze_at - 90) == 90
    assert effective_timeout(policy, step_id=16, step_timeout=3_600, now=policy.contest_deadline_at - 120) == 120
    with pytest.raises(ContestDeadlineExceeded):
        effective_timeout(policy, step_id=15, step_timeout=100, now=policy.content_freeze_at)


def test_contest_phase_mapping_is_contiguous_and_delivery_is_phase_eight():
    assert [phase_for_step(step).id for step in range(17)] == [
        1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 7, 7, 7, 7, 7, 8
    ]
    catalog = catalog_payload()
    assert catalog["contest_profile"] == "contest_core_v1"
    assert len(catalog["phases"]) == 8
    assert catalog["steps"][16]["timeout_seconds"] == DELIVERY_RESERVE_SECONDS
    assert [item["contest_phase"] for item in catalog["steps"]] == [
        1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 7, 7, 7, 7, 7, 8
    ]


def test_artifact_layers_keep_receipts_as_machine_evidence():
    assert classify_artifact("paper.tex") is ArtifactLayer.BUSINESS_TRUTH
    assert classify_artifact("solver/receipts/job.completed.json") is ArtifactLayer.MACHINE_EVIDENCE
    assert classify_artifact("final_audit/acceptance_receipt.json") is ArtifactLayer.MACHINE_EVIDENCE
    assert classify_artifact("checkpoint.md") is ArtifactLayer.REBUILDABLE_PROJECTION


def test_store_persists_contest_policy_and_immutable_structured_decisions(tmp_path):
    store = SQLiteStateStore(tmp_path, clock=lambda: 1_000)
    policy = ContestPolicy.default(started_at=1_000)
    store.initialize(
        project_id="demo",
        project_type="modeling",
        contest_policy=policy.to_dict(),
    )

    assert store.contest_policy() == policy.to_dict()
    decision = {
        "gate": "step3",
        "selected_primary": "m1",
        "selected_auxiliary": "m2",
        "selected_by": "human",
        "selected_at": 1_200,
        "reason": "best validated demo",
        "candidate_evidence": ["m1_demo_result.json"],
    }
    store.record_decision("step3", decision)
    store.record_decision("step3", decision)
    assert store.decision("step3") == decision

    with pytest.raises(ValueError, match="immutable"):
        store.record_decision("step3", {**decision, "selected_primary": "m2"})
    connection = sqlite3.connect(store.path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM workflow_decisions WHERE gate='step3'")
    finally:
        connection.close()


def test_engine_caps_step_context_to_global_remaining_budget(tmp_path):
    policy = ContestPolicy.default(started_at=1_000)
    clock = lambda: policy.content_freeze_at - 75
    store = SQLiteStateStore(tmp_path, clock=clock)
    store.initialize(
        project_id="demo",
        project_type="modeling",
        contest_policy=policy.to_dict(),
    )
    lifecycle = SuccessfulLifecycle()
    registry = StepRegistry()
    registry.register(
        StepDefinition(
            id=15,
            name="polish",
            timeout_seconds=600,
            max_attempts=1,
            step=lifecycle,
        )
    )

    state = FactoryEngine(tmp_path, store=store, registry=registry).run(max_steps=1)

    assert state.status is WorkflowStatus.COMPLETED
    assert lifecycle.timeouts == [75, 75]
    started = next(event for event in store.events() if event.type == "STEP_STARTED")
    assert started.payload["effective_timeout_seconds"] == 75


def test_engine_fails_closed_when_global_budget_is_exhausted(tmp_path):
    policy = ContestPolicy.default(started_at=1_000)
    store = SQLiteStateStore(tmp_path, clock=lambda: policy.content_freeze_at)
    store.initialize(
        project_id="demo",
        project_type="modeling",
        contest_policy=policy.to_dict(),
    )
    registry = StepRegistry()
    registry.register(
        StepDefinition(
            id=15,
            name="polish",
            timeout_seconds=600,
            max_attempts=1,
            step=SuccessfulLifecycle(),
        )
    )

    state = FactoryEngine(tmp_path, store=store, registry=registry).run()

    assert state.status is WorkflowStatus.FAILED
    assert store.events()[-1].type == "CONTEST_DEADLINE_EXHAUSTED"
    assert store.events()[-1].payload["error_class"] == "PERMANENT_CONTEST_DEADLINE"


def test_delivery_prepare_requires_sqlite_backed_content_freeze_decision(tmp_path):
    policy = ContestPolicy.default(started_at=1_000)
    store = SQLiteStateStore(tmp_path, clock=lambda: 2_000)
    store.initialize(
        project_id="demo",
        project_type="modeling",
        contest_policy=policy.to_dict(),
    )

    pending = prepare_human_gates(tmp_path, 16)
    assert pending.pending_action is not None
    assert pending.pending_action.gate == "content_freeze"

    (tmp_path / "selection" / "content_freeze_decision.json").write_text(
        json.dumps({"gate": "content_freeze", "selected_option_id": "tampered"}),
        encoding="utf-8",
    )
    assert prepare_human_gates(tmp_path, 16).pending_action is not None

    build_content_freeze_options(tmp_path, now_epoch=2_000)
    write_selection_decision(
        tmp_path,
        gate="content_freeze",
        selected_option_id="approve_content_freeze",
        source="human",
        reason="paper reviewed",
        now_epoch=2_001,
    )

    assert store.decision("content_freeze")["selected_by"] == "human"
    assert prepare_human_gates(tmp_path, 16).ready is True


def test_factory_service_creates_contest_core_policy_for_new_projects(tmp_path):
    service = FactoryService(tmp_path)

    service.create_project("demo", "question", start=False)

    policy = SQLiteStateStore(tmp_path / "ongoing" / "demo").contest_policy()
    assert policy is not None
    assert policy["profile"] == "contest_core_v1"
    assert policy["contest_deadline_at"] - policy["contest_started_at"] == 74 * 3_600

    with pytest.raises(SelectionError, match="not awaiting content_freeze"):
        approve_content_freeze(
            tmp_path / "ongoing" / "demo",
            reason="too early",
            now_epoch=1,
            no_resume=True,
        )


def test_delivery_freeze_blocks_automatic_reopen_without_human_override(tmp_path):
    policy = ContestPolicy.default(started_at=1_000)
    store = SQLiteStateStore(
        tmp_path, clock=lambda: policy.delivery_freeze_at + 1
    )
    store.initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=15,
        contest_policy=policy.to_dict(),
    )
    registry = StepRegistry()
    registry.register(
        StepDefinition(
            id=16,
            name="delivery",
            timeout_seconds=600,
            max_attempts=1,
            step=ReopenLifecycle(),
        )
    )

    state = FactoryEngine(tmp_path, store=store, registry=registry).run()

    assert state.status is WorkflowStatus.AWAITING_SELECTION
    assert state.pending_action["gate"] == "delivery_freeze_override"
    assert store.decision("delivery_freeze_override") is None
