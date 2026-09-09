from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from factory_core.durable_operation import (
    DurableOperation,
    OperationEvent,
    OperationStatus,
    build_worker_launch_identity,
    transition_operation,
)


def _transition(operation, event, *, nonce=None, reason):
    return transition_operation(
        operation,
        event,
        expected_claim_generation=operation.claim_generation,
        dispatch_nonce=nonce,
        reason_code=reason,
    )


def test_normal_worker_lifecycle_reaches_success_with_bound_receipts():
    identity = build_worker_launch_identity(
        outbox_command_id="command-normal-1",
        invocation_id="invocation-normal-1",
        attempt_id="attempt-normal-1",
        process_scope_id="scope-normal-1",
        payload_sha256="1" * 64,
    )
    operation = DurableOperation(identity)
    receipts = []

    operation, receipt = _transition(
        operation, OperationEvent.CLAIM, reason="LEASE_ACQUIRED"
    )
    receipts.append(receipt)
    operation, receipt = _transition(
        operation,
        OperationEvent.CHECKPOINT_DISPATCH,
        nonce="dispatch-normal-1",
        reason="DISPATCH_INTENT_DURABLE",
    )
    receipts.append(receipt)
    operation, receipt = _transition(
        operation,
        OperationEvent.CONFIRM_ACTIVE,
        nonce="dispatch-normal-1",
        reason="WORKER_READY",
    )
    receipts.append(receipt)
    operation, receipt = _transition(
        operation,
        OperationEvent.CONFIRM_SUCCEEDED,
        nonce="dispatch-normal-1",
        reason="WORKER_EXIT_ZERO",
    )
    receipts.append(receipt)

    assert operation.status is OperationStatus.SUCCEEDED
    assert [receipt.transition_index for receipt in receipts] == [1, 2, 3, 4]
    assert all(
        receipt.operation_identity_sha256 == identity.identity_sha256
        for receipt in receipts
    )
    assert len({receipt.receipt_sha256 for receipt in receipts}) == 4


def test_production_cli_import_does_not_load_phase4_contract():
    root = Path(__file__).resolve().parents[1]
    script = """
import sys
import factory_core.cli

assert "factory_core.durable_operation" not in sys.modules
print("phase4-shadow-not-in-production-imports")
"""
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout == "phase4-shadow-not-in-production-imports\n"
