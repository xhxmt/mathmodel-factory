"""Emit one deterministic M0.2 readiness/plan/parity identity tuple."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from factory_core.shadow_scheduler import (
    RECORDED_SNAPSHOT_SCHEMA,
    ParityValue,
    SchedulerCore,
    StageV1ReadinessAdapter,
    TransitionAction,
    build_parity_receipt,
    parity_receipt_bytes,
    parity_receipt_sha256,
    readiness_input_analysis_bytes,
    readiness_input_analysis_sha256,
    readiness_input_semantic_bytes,
    readiness_input_semantic_sha256,
    transition_plan_analysis_bytes,
    transition_plan_analysis_sha256,
    transition_plan_semantic_bytes,
    transition_plan_semantic_sha256,
)
from factory_core.workflow_contract import compile_workflow_contract_bundle


def replay_values():
    snapshot = {
        "schema_version": RECORDED_SNAPSHOT_SCHEMA,
        "project_id": "fixture:m02-replay",
        "project_revision": 42,
        "run_generation": "run-generation:7",
        "runtime_generation": "native_v2",
        "scheduler_generation": "stage_v1",
        "workflow_status": "ready",
        "last_completed_step": -1,
        "last_completed_stage": 0,
        "attempt": 0,
        "stage_cursor": {
            "coordinate": {
                "stage_id": 1,
                "subtask": "problem_setup",
                "source_step_id": 0,
            },
            "active_step": 0,
            "attempt": 0,
        },
        "checkpoint_heads": [],
        "dirty_refs": [],
        "pending_action_refs": [],
        "active_invocation_refs": [],
        "terminal_delivery": {
            "is_terminal": False,
            "terminal_reason": None,
            "delivery_capability": "contest-delivery-eligible",
            "delivery_allowed": False,
        },
        "domain_readiness": [],
        "immutable_refs": [
            {
                "kind": "recorded-stage-cursor",
                "ref": "snapshot:revision:42",
                "sha256": "4" * 64,
                "behavior_binding": False,
            }
        ],
        "availability": [
            {
                "fact": "recorded-workflow-state",
                "availability": "AVAILABLE",
                "required_for_plan": True,
                "gap_id": None,
            }
        ],
    }
    bundle = compile_workflow_contract_bundle()
    readiness_input = StageV1ReadinessAdapter(bundle).adapt(snapshot)
    decision = SchedulerCore.plan(bundle, readiness_input)
    receipt = build_parity_receipt(
        bundle,
        fixture_id="M02-CROSS-SEED-REPLAY",
        decision=decision,
        v1=ParityValue(
            action=TransitionAction.DISPATCH,
            coordinate=decision.plan.target,
            execution_route=decision.plan.execution_route,
        ),
        evidence_refs=("tests/support/m02_shadow_replay.py",),
    )
    return readiness_input, decision, receipt


def replay_identity() -> dict[str, object]:
    readiness_input, decision, receipt = replay_values()
    return {
        "readiness_semantic_bytes": len(readiness_input_semantic_bytes(readiness_input)),
        "readiness_semantic_sha256": readiness_input_semantic_sha256(readiness_input),
        "readiness_analysis_bytes": len(readiness_input_analysis_bytes(readiness_input)),
        "readiness_analysis_sha256": readiness_input_analysis_sha256(readiness_input),
        "plan_semantic_bytes": len(transition_plan_semantic_bytes(decision.plan)),
        "plan_semantic_sha256": transition_plan_semantic_sha256(decision.plan),
        "plan_analysis_bytes": len(transition_plan_analysis_bytes(decision.plan)),
        "plan_analysis_sha256": transition_plan_analysis_sha256(decision.plan),
        "parity_bytes": len(parity_receipt_bytes(receipt)),
        "parity_sha256": parity_receipt_sha256(receipt),
        "action": decision.plan.action.value,
        "target": decision.plan.target,
        "parity_status": receipt.status.value,
        "contract_semantic_sha256": readiness_input.contract.semantic_sha256,
        "contract_analysis_sha256": readiness_input.contract.analysis_sha256,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--value",
        choices=(
            "identity",
            "readiness-semantic",
            "readiness-analysis",
            "plan-semantic",
            "plan-analysis",
            "parity",
        ),
        default="identity",
    )
    arguments = parser.parse_args()
    readiness_input, decision, receipt = replay_values()
    canonical_values = {
        "readiness-semantic": readiness_input_semantic_bytes(readiness_input),
        "readiness-analysis": readiness_input_analysis_bytes(readiness_input),
        "plan-semantic": transition_plan_semantic_bytes(decision.plan),
        "plan-analysis": transition_plan_analysis_bytes(decision.plan),
        "parity": parity_receipt_bytes(receipt),
    }
    if arguments.value == "identity":
        print(
            json.dumps(
                replay_identity(),
                ensure_ascii=False,
                sort_keys=True,
                default=lambda value: value.__dict__,
            )
        )
    else:
        sys.stdout.buffer.write(canonical_values[arguments.value])
