from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

import pytest

from factory_core.canonical import canonical_bytes
from factory_core.durable_operation import OperationStatus
import factory_core.phase78_work_ledger as ledger_module
from factory_core.phase4_shadow_runtime import (
    Phase4ShadowFenceError,
    Phase4ShadowStore,
)
from factory_core.phase78_work_ledger import (
    PHASE78_WORK_RUN_SCHEMA,
    Phase78ReconcileOutcome,
    Phase78WorkContractError,
    Phase78WorkIdempotencyConflict,
    Phase78WorkKind,
    Phase78WorkLedger,
    Phase78WorkStoreError,
    run_phase78_work_ledger_shadow,
)
from factory_core.phase78_deadline import Phase78DeadlineError, TotalDeadline


def _ledger(tmp_path: Path) -> Phase78WorkLedger:
    tmp_path.mkdir(parents=True, exist_ok=True)
    ledger = Phase78WorkLedger(
        (tmp_path / "phase78-work.sqlite").resolve(),
        (tmp_path / "phase78-spool").resolve(),
    )
    ledger.initialize()
    return ledger


def _submit(
    ledger: Phase78WorkLedger,
    *,
    key: str = "job-1",
    payload: dict[str, object] | None = None,
    occurred_at: int = 1,
):
    return ledger.submit(
        caller_idempotency_key=key,
        workflow_id="workflow-1",
        work_kind=Phase78WorkKind.PHASE7_GROUNDING,
        payload={"artifact": "results/a.json"} if payload is None else payload,
        occurred_at=occurred_at,
    )


def _claim(
    ledger: Phase78WorkLedger,
    *,
    key: str = "job-1",
    request_key: str = "claim-1",
    expected: int = 0,
    occurred_at: int = 2,
    lease_seconds: int = 10,
    deadline=None,
):
    return ledger.claim(
        key,
        request_idempotency_key=request_key,
        claim_owner_id="worker-1",
        claim_owner_epoch=1,
        expected_claim_generation=expected,
        occurred_at=occurred_at,
        lease_seconds=lease_seconds,
        deadline=deadline,
    )


def _checkpoint(
    ledger: Phase78WorkLedger,
    *,
    key: str = "job-1",
    generation: int = 1,
    deadline=None,
):
    return ledger.checkpoint_local_worker(
        key,
        request_idempotency_key="checkpoint-1",
        expected_claim_generation=generation,
        claim_owner_id="worker-1",
        claim_owner_epoch=1,
        local_worker_nonce="local-worker-1",
        reason_code="LOCAL_SHADOW_WORKER_CHECKPOINTED",
        occurred_at=3,
        deadline=deadline,
    )


def test_default_off_returns_before_path_spool_or_phase4(monkeypatch):
    monkeypatch.setattr(
        ledger_module,
        "Path",
        lambda *_args, **_kwargs: pytest.fail("disabled runner constructed a Path"),
    )
    monkeypatch.setattr(
        ledger_module,
        "Phase4ShadowStore",
        lambda *_args, **_kwargs: pytest.fail("disabled runner constructed Phase 4"),
    )

    result = run_phase78_work_ledger_shadow()

    assert result.schema_version == PHASE78_WORK_RUN_SCHEMA
    assert result.enabled is result.ledger_verified is False
    assert result.job_count == 0
    assert all(
        getattr(result, field) is False
        for field in (
            "authoritative",
            "authority_transferred",
            "provider_call_performed",
            "outbox_dispatch_performed",
        )
    )
    assert len(result.run_sha256) == 64


def test_normal_submit_claim_checkpoint_complete_replay_and_restart(tmp_path):
    ledger = _ledger(tmp_path)
    submitted = _submit(ledger)
    replay = _submit(ledger, occurred_at=99)
    claimed = _claim(ledger)
    checkpointed = _checkpoint(ledger)
    completed = ledger.complete(
        "job-1",
        request_idempotency_key="complete-1",
        expected_claim_generation=1,
        claim_owner_id="worker-1",
        claim_owner_epoch=1,
        local_worker_nonce="local-worker-1",
        reason_code="LOCAL_SHADOW_WORKER_SUCCEEDED",
        occurred_at=4,
    )

    assert submitted.view.status is OperationStatus.PENDING
    assert submitted.replayed is False
    assert replay.replayed is True and replay.view == submitted.view
    assert claimed.view.status is OperationStatus.CLAIMED
    assert checkpointed.view.status is OperationStatus.DISPATCH_CHECKPOINTED
    assert completed.view.status is OperationStatus.SUCCEEDED
    assert completed.view.as_dict()["local_worker_launch_checkpointed"] is True
    assert all(
        completed.view.as_dict()[field] is False
        for field in (
            "authoritative",
            "authority_transferred",
            "provider_call_performed",
            "outbox_dispatch_performed",
        )
    )

    spool_stat = ledger.request_spool.stat()
    request = next(ledger.request_spool.glob("request-*.json"))
    assert stat.S_IMODE(spool_stat.st_mode) == 0o700
    assert stat.S_IMODE(request.stat().st_mode) == 0o600
    restarted = Phase78WorkLedger(ledger.database, ledger.request_spool)
    restarted.initialize()
    assert restarted.load("job-1").status is OperationStatus.SUCCEEDED
    assert restarted.list() == (restarted.load("job-1"),)


def test_restart_pending_and_expired_lease_reclaim(tmp_path):
    ledger = _ledger(tmp_path)
    _submit(ledger)
    restarted = Phase78WorkLedger(ledger.database, ledger.request_spool)
    restarted.initialize()
    assert restarted.load("job-1").status is OperationStatus.PENDING
    claimed = _claim(restarted, occurred_at=10, lease_seconds=5)
    assert claimed.view.state.lease_expires_at == 15

    second = Phase78WorkLedger(ledger.database, ledger.request_spool)
    second.initialize()
    with pytest.raises(Phase78WorkContractError, match="not reclaimable"):
        second.reclaim(
            "job-1",
            request_idempotency_key="reclaim-too-soon",
            claim_owner_id="worker-2",
            claim_owner_epoch=2,
            expected_claim_generation=1,
            occurred_at=14,
            lease_seconds=5,
        )
    reclaimed = second.reclaim(
        "job-1",
        request_idempotency_key="reclaim-2",
        claim_owner_id="worker-2",
        claim_owner_epoch=2,
        expected_claim_generation=1,
        occurred_at=15,
        lease_seconds=5,
    )
    assert reclaimed.view.status is OperationStatus.CLAIMED
    assert reclaimed.view.state.operation.claim_generation == 2
    assert reclaimed.view.state.retry_count == 1


def test_concurrent_same_key_converges_and_different_bytes_conflict(tmp_path):
    ledger = _ledger(tmp_path)

    def submit():
        participant = Phase78WorkLedger(ledger.database, ledger.request_spool)
        return _submit(participant)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = tuple(pool.map(lambda _index: submit(), range(16)))
    identities = {item.view.operation_identity_sha256 for item in results}
    assert len(identities) == 1
    assert sum(not item.replayed for item in results) == 1
    assert len(ledger.list()) == 1

    with pytest.raises(Phase78WorkIdempotencyConflict, match="different job bytes"):
        _submit(ledger, payload={"artifact": "results/b.json"})
    assert len(ledger.list()) == 1


def test_spool_survives_reservation_failure_and_restart_reconstructs_pending(
    tmp_path, monkeypatch
):
    ledger = _ledger(tmp_path)
    original = Phase4ShadowStore.reserve_operation
    failed = False

    def fail_once(self, identity, *, occurred_at):
        nonlocal failed
        if self.path == ledger.database and not failed:
            failed = True
            raise RuntimeError("synthetic reserve interruption")
        return original(self, identity, occurred_at=occurred_at)

    monkeypatch.setattr(Phase4ShadowStore, "reserve_operation", fail_once)
    with pytest.raises(RuntimeError, match="reserve interruption"):
        _submit(ledger)
    assert len(tuple(ledger.request_spool.glob("request-*.json"))) == 1

    monkeypatch.setattr(Phase4ShadowStore, "reserve_operation", original)
    restarted = Phase78WorkLedger(ledger.database, ledger.request_spool)
    restarted.initialize()
    assert restarted.load("job-1").status is OperationStatus.PENDING
    assert _submit(restarted).replayed is True


def test_fail_cancel_and_uncertain_reconcile_normal_flows(tmp_path):
    failed_ledger = _ledger(tmp_path / "failed")
    _submit(failed_ledger)
    _claim(failed_ledger)
    failed = failed_ledger.fail(
        "job-1",
        request_idempotency_key="fail-1",
        expected_claim_generation=1,
        claim_owner_id="worker-1",
        claim_owner_epoch=1,
        local_worker_nonce=None,
        reason_code="LOCAL_SHADOW_PRELAUNCH_FAILED",
        occurred_at=3,
    )
    assert failed.view.status is OperationStatus.FAILED

    cancelled_ledger = _ledger(tmp_path / "cancelled")
    _submit(cancelled_ledger)
    _claim(cancelled_ledger)
    _checkpoint(cancelled_ledger)
    cancelled = cancelled_ledger.cancel(
        "job-1",
        request_idempotency_key="cancel-1",
        cancellation_reason="user_cancel",
        expected_claim_generation=1,
        claim_owner_id="worker-1",
        claim_owner_epoch=1,
        local_worker_nonce="local-worker-1",
        occurred_at=4,
    )
    assert cancelled.view.status is OperationStatus.CANCELLED
    assert cancelled_ledger.cancel(
        "job-1",
        request_idempotency_key="cancel-1",
        cancellation_reason="user_cancel",
        expected_claim_generation=1,
        claim_owner_id="worker-1",
        claim_owner_epoch=1,
        local_worker_nonce="local-worker-1",
        occurred_at=4,
    ).replayed is True

    reconcile_ledger = _ledger(tmp_path / "reconcile")
    _submit(reconcile_ledger)
    _claim(reconcile_ledger)
    _checkpoint(reconcile_ledger)
    required = reconcile_ledger.reconcile(
        "job-1",
        request_idempotency_key="reconcile-1",
        expected_claim_generation=1,
        claim_owner_id="worker-1",
        claim_owner_epoch=1,
        local_worker_nonce="local-worker-1",
        occurred_at=4,
        outcome=None,
    )
    assert required.view.status is OperationStatus.RECONCILIATION_REQUIRED
    restarted = Phase78WorkLedger(
        reconcile_ledger.database, reconcile_ledger.request_spool
    )
    restarted.initialize()
    reconciled = restarted.reconcile(
        "job-1",
        request_idempotency_key="reconcile-1",
        expected_claim_generation=1,
        claim_owner_id="worker-1",
        claim_owner_epoch=1,
        local_worker_nonce="local-worker-1",
        occurred_at=5,
        outcome=Phase78ReconcileOutcome.SUCCEEDED,
    )
    assert reconciled.view.status is OperationStatus.SUCCEEDED


def test_cancellation_receipt_survives_interrupted_transition_restart_and_conflicts(
    tmp_path, monkeypatch
):
    ledger = _ledger(tmp_path)
    _submit(ledger)
    _claim(ledger)
    _checkpoint(ledger)
    original_transition = Phase78WorkLedger._transition
    interrupted = False

    def interrupt_after_receipt(self, *args, **kwargs):
        nonlocal interrupted
        if kwargs.get("allow_cancellation_intent") and not interrupted:
            interrupted = True
            raise SystemExit("synthetic exit after durable cancellation receipt")
        return original_transition(self, *args, **kwargs)

    monkeypatch.setattr(Phase78WorkLedger, "_transition", interrupt_after_receipt)
    with pytest.raises(SystemExit, match="after durable cancellation receipt"):
        ledger.cancel(
            "job-1",
            request_idempotency_key="cancel-shutdown-1",
            cancellation_reason="shutdown",
            expected_claim_generation=1,
            claim_owner_id="worker-1",
            claim_owner_epoch=1,
            local_worker_nonce="local-worker-1",
            occurred_at=4,
        )

    observed = ledger.load("job-1")
    assert observed.status is OperationStatus.DISPATCH_CHECKPOINTED
    assert observed.cancellation is not None
    assert observed.cancellation.cancellation_reason == "shutdown"
    assert observed.as_dict()["cancellation_reason"] == "shutdown"
    summary = observed.as_dict()["cancellation"]
    assert set(summary) == {
        "schema_version",
        "cancellation_reason",
        "occurred_at",
        "receipt_sha256",
        "authoritative",
        "authority_transferred",
        "provider_call_performed",
        "outbox_dispatch_performed",
    }
    assert "local_worker_nonce" not in json.dumps(observed.as_dict())
    receipt_path = next(ledger.request_spool.glob("cancellation-*.json"))
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600

    monkeypatch.setattr(Phase78WorkLedger, "_transition", original_transition)
    restarted = Phase78WorkLedger(ledger.database, ledger.request_spool)
    restarted.initialize()
    completed = restarted.cancel(
        "job-1",
        request_idempotency_key="cancel-shutdown-1",
        cancellation_reason="shutdown",
        expected_claim_generation=1,
        claim_owner_id="worker-1",
        claim_owner_epoch=1,
        local_worker_nonce="local-worker-1",
        occurred_at=4,
    )
    assert completed.view.status is OperationStatus.CANCELLED
    assert completed.view.cancellation == observed.cancellation
    assert completed.replayed is True

    another_restart = Phase78WorkLedger(ledger.database, ledger.request_spool)
    another_restart.initialize()
    exact_replay = another_restart.cancel(
        "job-1",
        request_idempotency_key="cancel-shutdown-1",
        cancellation_reason="shutdown",
        expected_claim_generation=1,
        claim_owner_id="worker-1",
        claim_owner_epoch=1,
        local_worker_nonce="local-worker-1",
        occurred_at=4,
    )
    assert exact_replay.replayed is True
    assert exact_replay.view.cancellation.cancellation_reason == "shutdown"

    with pytest.raises(
        Phase78WorkIdempotencyConflict, match="different bytes"
    ):
        another_restart.cancel(
            "job-1",
            request_idempotency_key="cancel-superseded-1",
            cancellation_reason="superseded",
            expected_claim_generation=1,
            claim_owner_id="worker-1",
            claim_owner_epoch=1,
            local_worker_nonce="local-worker-1",
            occurred_at=4,
        )
    with pytest.raises(
        Phase78WorkIdempotencyConflict, match="different bytes"
    ):
        another_restart.cancel(
            "job-1",
            request_idempotency_key="cancel-shutdown-1",
            cancellation_reason="shutdown",
            expected_claim_generation=1,
            claim_owner_id="worker-1",
            claim_owner_epoch=1,
            local_worker_nonce="local-worker-1",
            occurred_at=5,
        )


def test_deadline_classification_shared_budget_and_stale_generation_fence(tmp_path):
    ledger = _ledger(tmp_path)
    _submit(ledger)

    class ClassifiedCancellation(RuntimeError):
        code = "PHASE78_REQUEST_CANCELLED"
        reason = "user_cancel"

    cancellation = ClassifiedCancellation("cancelled")

    class CancelledDeadline:
        def check(self, stage):
            assert stage == "phase78-work-ledger"
            raise cancellation

        def remaining_seconds(self):
            raise AssertionError("remaining must not run after cancellation")

    with pytest.raises(ClassifiedCancellation) as caught:
        _claim(ledger, deadline=CancelledDeadline())
    assert caught.value is cancellation
    assert ledger.load("job-1").status is OperationStatus.PENDING

    class RecordingDeadline:
        def __init__(self):
            self.stages = []

        def check(self, stage):
            self.stages.append(stage)

        def remaining_seconds(self):
            return 10.0

    deadline = RecordingDeadline()
    _claim(ledger, deadline=deadline)
    with pytest.raises(Phase4ShadowFenceError, match="generation is stale"):
        ledger.checkpoint_local_worker(
            "job-1",
            request_idempotency_key="stale-checkpoint",
            expected_claim_generation=0,
            claim_owner_id="worker-1",
            claim_owner_epoch=1,
            local_worker_nonce="local-worker-1",
            reason_code="STALE_GENERATION",
            occurred_at=3,
            deadline=deadline,
        )
    current = ledger.load("job-1")
    assert current.status is OperationStatus.CLAIMED
    assert current.state.operation.transition_index == 1
    assert len(deadline.stages) >= 4


def test_initialization_spool_scan_and_guard_share_the_caller_deadline(tmp_path):
    first = _ledger(tmp_path)
    held = first._open_spool(create=True)
    descriptor = held.fileno("phase78-spool")
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    second = Phase78WorkLedger(first.database, first.request_spool)
    started = time.monotonic()
    try:
        with pytest.raises(Phase78DeadlineError) as captured:
            second.initialize(deadline=TotalDeadline(25))
        assert captured.value.code == "PHASE78_DEADLINE_EXCEEDED"
        assert time.monotonic() - started < 0.5
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        held.close("phase78-spool")

    second.initialize(deadline=TotalDeadline(1_000))


def test_path_mode_identity_and_spool_tamper_fail_closed(tmp_path):
    with pytest.raises(Phase78WorkContractError, match="must be absolute"):
        Phase78WorkLedger("relative.sqlite", "relative-spool")
    with pytest.raises(Phase78WorkContractError, match="outside request spool"):
        Phase78WorkLedger(
            (tmp_path / "spool" / "work.sqlite").resolve(),
            (tmp_path / "spool").resolve(),
        )

    ledger = _ledger(tmp_path / "valid")
    submitted = _submit(ledger)
    assert submitted.view.state.operation.identity.payload_sha256 == (
        submitted.view.job.job_sha256
    )
    request = next(ledger.request_spool.glob("request-*.json"))
    original = request.read_bytes()
    request.chmod(0o644)
    with pytest.raises(Phase78WorkStoreError, match="0600 regular file"):
        ledger.load("job-1")
    request.chmod(0o600)
    decoded = json.loads(original)
    decoded["job_sha256"] = "f" * 64
    request.write_bytes(canonical_bytes(decoded))
    request.chmod(0o600)
    with pytest.raises(Phase78WorkStoreError, match="canonical SHA-256 differs"):
        ledger.load("job-1")

    target = tmp_path / "real-spool"
    target.mkdir(mode=0o700)
    symlink = tmp_path / "spool-link"
    symlink.symlink_to(target, target_is_directory=True)
    linked = Phase78WorkLedger((tmp_path / "linked.sqlite").resolve(), symlink)
    with pytest.raises(Phase78WorkStoreError, match="spool is unavailable"):
        linked.initialize()


def test_import_has_no_provider_or_authority_dispatch_dependency(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = """
import sys
import factory_core.phase78_work_ledger
for name in (
    'factory_core.authority_outbox_delivery',
    'factory_core.adapters.solvers.cloud_run',
):
    assert name not in sys.modules, name
print('phase78-local-ledger-has-no-provider-port')
"""
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPYCACHEPREFIX"] = str(tmp_path / "pycache")
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
    assert completed.stdout == "phase78-local-ledger-has-no-provider-port\n"
