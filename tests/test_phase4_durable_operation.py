import pytest

from factory_core.durable_operation import (
    DurableOperation,
    DurableOperationIdentity,
    InvalidOperationIdentity,
    InvalidOperationTransition,
    OperationEvent,
    OperationStatus,
    OperationType,
    build_worker_launch_identity,
    derive_worker_launch_idempotency_key,
    transition_operation,
)


PAYLOAD_SHA = "1" * 64


def identity(command_id="command-1", payload_sha=PAYLOAD_SHA):
    return build_worker_launch_identity(
        outbox_command_id=command_id,
        invocation_id="invocation-1",
        attempt_id="attempt-1",
        process_scope_id="scope-1",
        payload_sha256=payload_sha,
    )


def apply(operation, event, *, nonce=None, reason="TEST_REASON"):
    return transition_operation(
        operation,
        event,
        expected_claim_generation=operation.claim_generation,
        dispatch_nonce=nonce,
        reason_code=reason,
    )


def claimed_operation():
    operation, _receipt = apply(DurableOperation(identity()), OperationEvent.CLAIM)
    return operation


def checkpointed_operation():
    operation = claimed_operation()
    operation, _receipt = apply(
        operation, OperationEvent.CHECKPOINT_DISPATCH, nonce="dispatch-1"
    )
    return operation


def test_idempotency_key_is_logical_operation_identity_not_outbox_row_identity():
    first = identity("command-1")
    duplicate = identity("command-2")
    changed_payload = identity("command-3", "2" * 64)

    assert first.idempotency_key == duplicate.idempotency_key
    assert first.identity_sha256 != duplicate.identity_sha256
    assert changed_payload.idempotency_key != first.idempotency_key


def test_identity_rejects_noncanonical_hash_and_mismatched_idempotency_key():
    with pytest.raises(InvalidOperationIdentity, match="lowercase SHA-256"):
        identity(payload_sha="A" * 64)

    with pytest.raises(InvalidOperationIdentity, match="idempotency_key does not match"):
        DurableOperationIdentity(
            schema_version="durable-worker-operation-identity-v1",
            operation_type=OperationType.WORKER_LAUNCH,
            outbox_command_id="command-1",
            invocation_id="invocation-1",
            attempt_id="attempt-1",
            process_scope_id="scope-1",
            payload_sha256=PAYLOAD_SHA,
            idempotency_key="worker-launch-v1:" + "0" * 64,
        )


def test_idempotency_derivation_rejects_ambiguous_identifier():
    with pytest.raises(InvalidOperationIdentity, match="canonical operation identifier"):
        derive_worker_launch_idempotency_key(
            invocation_id=" invocation-1",
            attempt_id="attempt-1",
            payload_sha256=PAYLOAD_SHA,
        )


def test_claim_checkpoint_active_success_path_emits_bound_receipts():
    operation = DurableOperation(identity())
    operation, claim = apply(operation, OperationEvent.CLAIM, reason="LEASE_ACQUIRED")
    operation, checkpoint = apply(
        operation,
        OperationEvent.CHECKPOINT_DISPATCH,
        nonce="dispatch-1",
        reason="DISPATCH_INTENT_DURABLE",
    )
    operation, active = apply(
        operation,
        OperationEvent.CONFIRM_ACTIVE,
        nonce="dispatch-1",
        reason="WORKER_READY",
    )
    operation, completed = apply(
        operation,
        OperationEvent.CONFIRM_SUCCEEDED,
        nonce="dispatch-1",
        reason="WORKER_EXIT_ZERO",
    )

    assert operation.status is OperationStatus.SUCCEEDED
    assert [
        claim.transition_index,
        checkpoint.transition_index,
        active.transition_index,
        completed.transition_index,
    ] == [1, 2, 3, 4]
    assert len({receipt.receipt_sha256 for receipt in (claim, checkpoint, active, completed)}) == 4
    assert all(
        receipt.operation_identity_sha256 == operation.identity.identity_sha256
        for receipt in (claim, checkpoint, active, completed)
    )


def test_expired_predispatch_claim_can_be_reclaimed_with_new_generation():
    operation = claimed_operation()

    reclaimed, receipt = apply(
        operation, OperationEvent.RECLAIM_EXPIRED, reason="CLAIM_LEASE_EXPIRED"
    )

    assert reclaimed.status is OperationStatus.CLAIMED
    assert reclaimed.claim_generation == 2
    assert receipt.claim_generation == 2


def test_stale_claim_generation_cannot_checkpoint_dispatch():
    operation = claimed_operation()

    with pytest.raises(InvalidOperationTransition, match="claim generation is stale"):
        transition_operation(
            operation,
            OperationEvent.CHECKPOINT_DISPATCH,
            expected_claim_generation=0,
            dispatch_nonce="dispatch-1",
            reason_code="STALE_CONSUMER",
        )


def test_dispatch_nonce_is_required_once_checkpoint_exists():
    operation = checkpointed_operation()

    with pytest.raises(InvalidOperationTransition, match="dispatch nonce is stale or missing"):
        apply(operation, OperationEvent.CONFIRM_ACTIVE)
    with pytest.raises(InvalidOperationTransition, match="dispatch nonce is stale or missing"):
        apply(operation, OperationEvent.CONFIRM_ACTIVE, nonce="dispatch-2")


def test_dispatch_uncertain_requires_reconciliation_and_cannot_be_reclaimed():
    operation = checkpointed_operation()
    operation, _receipt = apply(
        operation,
        OperationEvent.MARK_DISPATCH_UNCERTAIN,
        nonce="dispatch-1",
        reason="ACK_LOST",
    )

    with pytest.raises(InvalidOperationTransition, match="not allowed"):
        apply(operation, OperationEvent.CLAIM)
    with pytest.raises(InvalidOperationTransition, match="not allowed"):
        apply(operation, OperationEvent.CHECKPOINT_DISPATCH, nonce="dispatch-2")

    operation, _receipt = apply(
        operation,
        OperationEvent.REQUIRE_RECONCILIATION,
        nonce="dispatch-1",
        reason="PROVIDER_LOOKUP_REQUIRED",
    )
    operation, _receipt = apply(
        operation,
        OperationEvent.RECONCILE_ACTIVE,
        nonce="dispatch-1",
        reason="EXISTING_WORKER_FOUND",
    )
    assert operation.status is OperationStatus.ACTIVE


def test_cancel_requires_checkpoint_and_records_signal_before_completion():
    with pytest.raises(InvalidOperationTransition, match="not allowed"):
        apply(claimed_operation(), OperationEvent.REQUEST_CANCEL)

    operation = checkpointed_operation()
    operation, _receipt = apply(
        operation,
        OperationEvent.REQUEST_CANCEL,
        nonce="dispatch-1",
        reason="USER_PAUSE",
    )
    operation, _receipt = apply(
        operation,
        OperationEvent.RECORD_CANCEL_SIGNAL,
        nonce="dispatch-1",
        reason="TERM_SENT",
    )
    operation, receipt = apply(
        operation,
        OperationEvent.CONFIRM_CANCELLED,
        nonce="dispatch-1",
        reason="PROCESS_TREE_REAPED",
    )

    assert operation.status is OperationStatus.CANCELLED
    assert receipt.as_dict()["dispatch_nonce"] == "dispatch-1"


@pytest.mark.parametrize(
    ("terminal_event", "terminal_status"),
    [
        (OperationEvent.CONFIRM_SUCCEEDED, OperationStatus.SUCCEEDED),
        (OperationEvent.CONFIRM_FAILED, OperationStatus.FAILED),
    ],
)
def test_terminal_operations_reject_all_further_transitions(terminal_event, terminal_status):
    operation = checkpointed_operation()
    operation, _receipt = apply(
        operation,
        terminal_event,
        nonce="dispatch-1",
        reason="TERMINAL_OBSERVED",
    )
    assert operation.status is terminal_status

    with pytest.raises(InvalidOperationTransition, match="not allowed"):
        apply(
            operation,
            OperationEvent.REQUIRE_RECONCILIATION,
            nonce="dispatch-1",
        )


def test_unknown_wire_event_fails_closed():
    with pytest.raises(InvalidOperationTransition, match="unsupported operation event"):
        apply(DurableOperation(identity()), "dispatch-now")
