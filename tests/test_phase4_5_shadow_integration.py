from __future__ import annotations

from factory_core.adapters.infrastructure.pause_policy import (
    PauseMode,
    ProcessScopeKind,
)
from factory_core.durable_operation import (
    OperationEvent,
    OperationStatus,
    build_worker_launch_identity,
)
from factory_core.phase4_shadow_runtime import Phase4ShadowStore
from factory_core.phase5_shadow_supervisor import (
    Phase5SupervisorStore,
    SupervisorScopeBinding,
    SupervisorStatus,
    SyntheticEffectObservation,
    SyntheticObservationOutcome,
    run_phase5_full_shadow,
)


class _RecordingPort:
    def __init__(self):
        self.calls = 0

    def record_would_apply(self, *, request_id, binding, decision):
        self.calls += 1
        assert binding.operation_identity_sha256
        assert decision.scope_kind is binding.scope_kind
        return SyntheticEffectObservation(
            observation_id="joint-observation-1",
            outcome=SyntheticObservationOutcome.CONFIRMED_APPLIED,
            observed_at=20,
            evidence_sha256="e" * 64,
        )


def _operation_identity():
    return build_worker_launch_identity(
        outbox_command_id="joint-command-1",
        invocation_id="joint-invocation-1",
        attempt_id="joint-attempt-1",
        process_scope_id="joint-scope-1",
        payload_sha256="d" * 64,
    )


def test_phase4_operation_and_phase5_supervisor_bind_restart_and_exact_replay(tmp_path):
    phase4_path = (tmp_path / "phase4.sqlite").resolve()
    phase5_path = (tmp_path / "phase5.sqlite").resolve()
    identity = _operation_identity()
    phase4 = Phase4ShadowStore(phase4_path)
    phase4.initialize()
    reserved = phase4.reserve_operation(identity, occurred_at=10)
    claimed = phase4.claim_operation(
        identity.identity_sha256,
        request_idempotency_key="joint-phase4-claim",
        claim_owner_id="joint-consumer",
        claim_owner_epoch=1,
        expected_claim_generation=0,
        occurred_at=11,
        lease_seconds=10,
    )
    checkpoint = phase4.transition(
        identity.identity_sha256,
        OperationEvent.CHECKPOINT_DISPATCH,
        request_idempotency_key="joint-phase4-checkpoint",
        expected_claim_generation=claimed.state.operation.claim_generation,
        claim_owner_id="joint-consumer",
        claim_owner_epoch=1,
        dispatch_nonce="joint-dispatch-nonce",
        reason_code="SYNTHETIC_INTENT_ONLY",
        occurred_at=12,
    )
    active = phase4.transition(
        identity.identity_sha256,
        OperationEvent.CONFIRM_ACTIVE,
        request_idempotency_key="joint-phase4-active",
        expected_claim_generation=checkpoint.state.operation.claim_generation,
        claim_owner_id="joint-consumer",
        claim_owner_epoch=1,
        dispatch_nonce="joint-dispatch-nonce",
        reason_code="SYNTHETIC_SCOPE_OBSERVED",
        occurred_at=13,
    )
    assert active.state.operation.status is OperationStatus.ACTIVE
    assert active.state.as_dict()["dispatch_performed"] is False

    binding = SupervisorScopeBinding(
        workflow_id="joint-workflow-1",
        invocation_id=identity.invocation_id,
        attempt_id=identity.attempt_id,
        process_scope_id=identity.process_scope_id,
        operation_identity_sha256=identity.identity_sha256,
        scope_kind=ProcessScopeKind.WORKER,
    )
    first_port = _RecordingPort()
    first = run_phase5_full_shadow(
        enabled=True,
        database=phase5_path,
        binding=binding,
        mode=PauseMode.PAUSE,
        request_idempotency_key="joint-phase5-pause",
        occurred_at=18,
        effect_port=first_port,
    )
    assert first_port.calls == 1
    assert first.dispatch_performed is False
    assert first.process_signal_performed is False
    assert first.provider_call_performed is False

    phase4_restarted = Phase4ShadowStore(phase4_path)
    phase4_restarted.initialize()
    phase5_restarted = Phase5SupervisorStore(phase5_path)
    phase5_restarted.initialize()
    assert phase4_restarted.load(identity.identity_sha256) == active.state
    assert phase5_restarted.load(first.request_id).status is SupervisorStatus.COMPLETED

    phase4_counts = phase4_restarted.table_counts()
    phase5_counts = phase5_restarted.table_counts()
    replay_port = _RecordingPort()
    replay = run_phase5_full_shadow(
        enabled=True,
        database=phase5_path,
        binding=binding,
        mode=PauseMode.PAUSE,
        request_idempotency_key="joint-phase5-pause",
        occurred_at=18,
        effect_port=replay_port,
    )
    assert replay.replayed is True
    assert replay_port.calls == 0
    assert phase4_restarted.table_counts() == phase4_counts
    assert phase5_restarted.table_counts() == phase5_counts
    assert reserved.receipt.dispatch_performed is False


def test_phase5_restart_after_checkpoint_requires_reconciliation_without_port(tmp_path):
    path = (tmp_path / "phase5-crash.sqlite").resolve()
    identity = _operation_identity()
    binding = SupervisorScopeBinding(
        workflow_id="joint-workflow-1",
        invocation_id=identity.invocation_id,
        attempt_id=identity.attempt_id,
        process_scope_id=identity.process_scope_id,
        operation_identity_sha256=identity.identity_sha256,
        scope_kind=ProcessScopeKind.ATTACHED_SOLVER,
    )
    store = Phase5SupervisorStore(path)
    store.initialize()
    requested = store.request_pause(
        binding,
        PauseMode.PAUSE,
        request_idempotency_key="crash-request",
        occurred_at=10,
    )
    store.checkpoint_effect(
        requested.state.request_id,
        expected_binding_sha256=binding.binding_sha256,
        request_idempotency_key="crash-checkpoint",
        occurred_at=11,
    )

    restarted = Phase5SupervisorStore(path)
    restarted.initialize()
    recovered = restarted.recover_uncertain(
        requested.state.request_id,
        expected_binding_sha256=binding.binding_sha256,
        request_idempotency_key="crash-recovery",
        occurred_at=12,
    )
    assert recovered.state.status is SupervisorStatus.RECONCILIATION_REQUIRED
    assert recovered.state.as_dict()["process_signal_performed"] is False
    assert recovered.state.as_dict()["provider_call_performed"] is False
