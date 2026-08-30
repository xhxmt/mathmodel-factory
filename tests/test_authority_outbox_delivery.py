from __future__ import annotations

import sqlite3

import pytest

import factory_core.authority_outbox_delivery as delivery_module
from factory_core.authority_operations import CANARY, V1_ONLY, AuthorityOperations
from factory_core.authority_outbox_delivery import (
    AuthorityOutboxClaimConflict,
    AuthorityOutboxConsumer,
    AuthorityOutboxConsumerFenceLost,
    ProviderDeliveryResult,
    ProviderReconciliationResult,
)
from tests.support.authority_production import (
    configure_canary,
    install_foundation,
    persist_one,
)


def _consumer(fixture):
    return AuthorityOutboxConsumer(
        fixture.database,
        consumer_id="consumer-a",
        consumer_epoch=1,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )


def _ready_message(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    persist_one(writer)
    return fixture, writer, _consumer(fixture)


def _success(receipt_id: str = "provider-receipt-1") -> ProviderDeliveryResult:
    return ProviderDeliveryResult(
        "SUCCEEDED",
        provider_receipt_id=receipt_id,
        provider_status="accepted",
        receipt_fields=(("provider_code", "200"),),
    )


def test_claim_and_success_receipt_are_fenced_and_idempotent(tmp_path):
    fixture, _writer, consumer = _ready_message(tmp_path)
    claim = consumer.claim_next(occurred_at=1300, lease_seconds=30)
    assert claim is not None
    calls = []

    def provider(request):
        calls.append(request.delivery_key)
        return _success()

    delivered = consumer.deliver_claim(
        claim,
        provider_callback=provider,
        occurred_at=1301,
        max_attempts=3,
        retry_backoff_seconds=10,
    )
    replay = consumer.deliver_claim(
        claim,
        provider_callback=provider,
        occurred_at=1302,
        max_attempts=3,
        retry_backoff_seconds=10,
    )
    assert delivered.status == replay.status == "DELIVERED"
    assert replay.callback_invoked is False
    assert len(calls) == 1
    connection = sqlite3.connect(fixture.database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM authority_production_provider_receipts"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM authority_production_outbox_delivery_audit"
        ).fetchone()[0] == 2
    finally:
        connection.close()


def test_retry_wait_uses_caller_time_then_succeeds(tmp_path):
    _fixture, _writer, consumer = _ready_message(tmp_path)
    first_claim = consumer.claim_next(occurred_at=1300, lease_seconds=30)
    assert first_claim is not None
    retry = consumer.deliver_claim(
        first_claim,
        provider_callback=lambda _request: ProviderDeliveryResult(
            "RETRYABLE_FAILURE", "TEMPORARY_PROVIDER_ERROR"
        ),
        occurred_at=1301,
        max_attempts=3,
        retry_backoff_seconds=10,
    )
    assert retry.status == "RETRY_WAIT"
    assert retry.next_attempt_at == 1311
    assert consumer.claim_next(occurred_at=1310, lease_seconds=30) is None
    second_claim = consumer.claim_next(occurred_at=1311, lease_seconds=30)
    assert second_claim is not None and second_claim.attempt_count == 2
    delivered = consumer.deliver_claim(
        second_claim,
        provider_callback=lambda _request: _success("provider-receipt-2"),
        occurred_at=1312,
        max_attempts=3,
        retry_backoff_seconds=10,
    )
    assert delivered.status == "DELIVERED"


@pytest.mark.parametrize(
    "result",
    (
        ProviderDeliveryResult("PERMANENT_FAILURE", "PROVIDER_REJECTED"),
        ProviderDeliveryResult("RETRYABLE_FAILURE", "RETRY_EXHAUSTED"),
    ),
)
def test_permanent_failure_or_retry_exhaustion_dead_letters(tmp_path, result):
    _fixture, _writer, consumer = _ready_message(tmp_path)
    claim = consumer.claim_next(occurred_at=1300, lease_seconds=30)
    assert claim is not None
    disposition = consumer.deliver_claim(
        claim,
        provider_callback=lambda _request: result,
        occurred_at=1301,
        max_attempts=1,
        retry_backoff_seconds=10,
    )
    assert disposition.status == "DEAD_LETTER"
    assert consumer.claim_next(occurred_at=9999, lease_seconds=30) is None


def test_claim_crash_window_moves_to_reconciliation_not_blind_resend(tmp_path):
    _fixture, _writer, consumer = _ready_message(tmp_path)
    claim = consumer.claim_next(occurred_at=1300, lease_seconds=5)
    assert claim is not None
    assert consumer.recover_expired_claims(occurred_at=1304) == ()
    assert consumer.recover_expired_claims(occurred_at=1305) == ("message-1",)
    assert consumer.claim_next(occurred_at=2000, lease_seconds=5) is None
    unknown = consumer.reconcile(
        "message-1",
        provider_lookup=lambda _request: ProviderReconciliationResult("UNKNOWN"),
        occurred_at=1306,
        max_attempts=3,
        retry_backoff_seconds=10,
    )
    assert unknown.status == "RECONCILIATION_REQUIRED"


def test_provider_success_before_db_receipt_is_closed_by_receipt_lookup(tmp_path, monkeypatch):
    _fixture, _writer, consumer = _ready_message(tmp_path)
    claim = consumer.claim_next(occurred_at=1300, lease_seconds=5)
    assert claim is not None
    provider_receipts = {}

    def provider(request):
        value = _success()
        provider_receipts[request.delivery_key] = value
        return value

    def crash(stage: str) -> None:
        if stage == "after_provider_before_receipt":
            raise RuntimeError("simulated process death")

    monkeypatch.setattr(delivery_module, "_delivery_failure_point", crash)
    with pytest.raises(RuntimeError, match="process death"):
        consumer.deliver_claim(
            claim,
            provider_callback=provider,
            occurred_at=1301,
            max_attempts=3,
            retry_backoff_seconds=10,
        )
    monkeypatch.setattr(delivery_module, "_delivery_failure_point", lambda _stage: None)
    assert consumer.recover_expired_claims(occurred_at=1305) == ("message-1",)
    reconciled = consumer.reconcile(
        "message-1",
        provider_lookup=lambda request: ProviderReconciliationResult(
            "FOUND_SUCCESS", provider_receipts[request.delivery_key]
        ),
        occurred_at=1306,
        max_attempts=3,
        retry_backoff_seconds=10,
    )
    assert reconciled.status == "DELIVERED"
    assert reconciled.provider_receipt_id == "provider-receipt-1"


def test_provider_confirmed_absent_requeues_and_old_claim_is_stale(tmp_path):
    _fixture, _writer, consumer = _ready_message(tmp_path)
    old_claim = consumer.claim_next(occurred_at=1300, lease_seconds=5)
    assert old_claim is not None
    consumer.recover_expired_claims(occurred_at=1305)
    retry = consumer.reconcile(
        "message-1",
        provider_lookup=lambda _request: ProviderReconciliationResult("CONFIRMED_ABSENT"),
        occurred_at=1306,
        max_attempts=3,
        retry_backoff_seconds=10,
    )
    assert retry.status == "RETRY_WAIT" and retry.next_attempt_at == 1316
    new_claim = consumer.claim_next(occurred_at=1316, lease_seconds=5)
    assert new_claim is not None and new_claim.claim_epoch == old_claim.claim_epoch + 1
    with pytest.raises(AuthorityOutboxClaimConflict, match="stale"):
        consumer.deliver_claim(
            old_claim,
            provider_callback=lambda _request: _success(),
            occurred_at=1317,
            max_attempts=3,
            retry_backoff_seconds=10,
        )


def test_consumer_handoff_invalidates_stale_consumer_epoch(tmp_path):
    fixture, _writer, stale = _ready_message(tmp_path)
    operations = AuthorityOperations(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    operations.switch_mode(
        target_mode=V1_ONLY,
        expected_switch_epoch=1,
        operator_subject="operator-a",
        reason="consumer handoff",
        occurred_at=1400,
    )
    operations.configure_writer(
        new_writer_id="writer-a",
        enabled=True,
        expected_writer_epoch=1,
        expected_switch_epoch=2,
        operator_subject="operator-a",
        reason="reenable writer",
        occurred_at=1401,
    )
    operations.configure_consumer(
        new_consumer_id="consumer-b",
        enabled=True,
        expected_consumer_epoch=1,
        expected_switch_epoch=2,
        operator_subject="operator-a",
        reason="new consumer",
        occurred_at=1402,
    )
    operations.switch_mode(
        target_mode=CANARY,
        expected_switch_epoch=2,
        operator_subject="operator-a",
        reason="resume canary",
        occurred_at=1403,
    )
    with pytest.raises(AuthorityOutboxConsumerFenceLost):
        stale.claim_next(occurred_at=1404, lease_seconds=5)
    fresh = AuthorityOutboxConsumer(
        fixture.database,
        consumer_id="consumer-b",
        consumer_epoch=2,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    assert fresh.claim_next(occurred_at=1404, lease_seconds=5) is not None


def test_new_consumer_process_with_same_durable_identity_recovers_expired_claim(tmp_path):
    fixture, _writer, first_process = _ready_message(tmp_path)
    assert first_process.claim_next(occurred_at=1300, lease_seconds=5) is not None
    restarted = _consumer(fixture)
    assert restarted.recover_expired_claims(occurred_at=1305) == ("message-1",)


def test_v1_only_permits_explicit_reconciliation_but_never_new_claim_or_dispatch(tmp_path):
    fixture, _writer, active = _ready_message(tmp_path)
    assert active.claim_next(occurred_at=1300, lease_seconds=5) is not None
    operations = AuthorityOperations(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    operations.switch_mode(
        target_mode=V1_ONLY,
        expected_switch_epoch=1,
        operator_subject="operator-a",
        reason="hard fallback with an in-flight claim",
        occurred_at=1301,
    )
    configured = operations.configure_consumer(
        new_consumer_id="reconciler-a",
        enabled=True,
        expected_consumer_epoch=1,
        expected_switch_epoch=2,
        operator_subject="operator-a",
        reason="reconciliation-only epoch",
        occurred_at=1302,
    )
    reconciler = AuthorityOutboxConsumer(
        fixture.database,
        consumer_id="reconciler-a",
        consumer_epoch=configured.consumer_epoch,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    with pytest.raises(AuthorityOutboxConsumerFenceLost, match="inactive"):
        reconciler.claim_next(occurred_at=1305, lease_seconds=5)
    assert reconciler.recover_expired_claims(occurred_at=1305) == ("message-1",)
    disposition = reconciler.reconcile(
        "message-1",
        provider_lookup=lambda _request: ProviderReconciliationResult("CONFIRMED_ABSENT"),
        occurred_at=1306,
        max_attempts=3,
        retry_backoff_seconds=10,
    )
    assert disposition.status == "RETRY_WAIT"
    with pytest.raises(AuthorityOutboxConsumerFenceLost, match="inactive"):
        reconciler.claim_next(occurred_at=1316, lease_seconds=5)


def test_multiple_intents_claim_in_revision_order_and_never_mutate_intent_rows(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    persist_one(writer, suffix="1", requested_revision=1)
    persist_one(writer, suffix="2", requested_revision=2)
    consumer = _consumer(fixture)
    first = consumer.claim_next(occurred_at=1300, lease_seconds=30)
    assert first is not None and first.request.message_id == "message-1"
    consumer.deliver_claim(
        first,
        provider_callback=lambda _request: _success("provider-receipt-1"),
        occurred_at=1301,
        max_attempts=3,
        retry_backoff_seconds=10,
    )
    second = consumer.claim_next(occurred_at=1302, lease_seconds=30)
    assert second is not None and second.request.message_id == "message-2"
    connection = sqlite3.connect(fixture.database)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE authority_outbox SET topic='changed'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM authority_outbox")
    finally:
        connection.close()
