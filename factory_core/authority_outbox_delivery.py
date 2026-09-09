"""Transactional Authority outbox delivery state and recovery boundary.

The module never performs network I/O.  Callers inject delivery and provider
reconciliation callbacks.  Unknown provider outcomes move to explicit
reconciliation instead of being blindly resent.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Callable

from .authority_operations import (
    AUTHORITY_PRIMARY,
    CANARY,
    _identifier,
    _nonnegative,
    _sha,
    _text,
)
from .authority_production_schema import (
    authority_database_path,
    connect_authority_ro,
    connect_authority_rw,
    legacy_source_identity_sha256,
    verify_production_installation,
)
from .canonical import canonical_bytes, canonical_sha256


class AuthorityOutboxError(RuntimeError):
    """Base error for outbox delivery state transitions."""


class AuthorityOutboxConsumerFenceLost(AuthorityOutboxError):
    """Raised when consumer id/epoch or switch/source fence differs."""


class AuthorityOutboxClaimConflict(AuthorityOutboxError):
    """Raised when a claim is stale or no longer owns the message."""


@dataclass(frozen=True)
class OutboxDeliveryRequest:
    message_id: str
    workflow_id: str
    revision: int
    topic: str
    delivery_key: str
    envelope_bytes: bytes
    envelope_sha256: str


@dataclass(frozen=True)
class OutboxClaim:
    request: OutboxDeliveryRequest
    consumer_id: str
    consumer_epoch: int
    claim_epoch: int
    attempt_count: int
    lease_expires_at: int


@dataclass(frozen=True)
class ProviderDeliveryResult:
    status: str
    error_code: str | None = None
    provider_receipt_id: str | None = None
    provider_status: str | None = None
    receipt_fields: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {"SUCCEEDED", "RETRYABLE_FAILURE", "PERMANENT_FAILURE"}:
            raise AuthorityOutboxError("unsupported provider delivery status")
        if self.status == "SUCCEEDED":
            _identifier(self.provider_receipt_id, "provider_receipt_id")
            _identifier(self.provider_status, "provider_status")
            if self.error_code is not None:
                raise AuthorityOutboxError("successful provider result cannot have error_code")
        else:
            _identifier(self.error_code, "error_code")
            if self.provider_receipt_id is not None or self.provider_status is not None:
                raise AuthorityOutboxError("failed provider result cannot carry a receipt")
        if type(self.receipt_fields) is not tuple:
            raise AuthorityOutboxError("receipt_fields must be an immutable tuple")
        prior = None
        for pair in self.receipt_fields:
            if type(pair) is not tuple or len(pair) != 2:
                raise AuthorityOutboxError("provider receipt field is malformed")
            key = _identifier(pair[0], "receipt_field.key")
            _text(pair[1], "receipt_field.value")
            if prior is not None and str(key) <= prior:
                raise AuthorityOutboxError("provider receipt fields must be uniquely sorted")
            prior = str(key)


@dataclass(frozen=True)
class ProviderReconciliationResult:
    status: str
    delivery_result: ProviderDeliveryResult | None = None

    def __post_init__(self) -> None:
        if self.status not in {"FOUND_SUCCESS", "CONFIRMED_ABSENT", "UNKNOWN"}:
            raise AuthorityOutboxError("unsupported provider reconciliation status")
        if self.status == "FOUND_SUCCESS":
            if (
                type(self.delivery_result) is not ProviderDeliveryResult
                or self.delivery_result.status != "SUCCEEDED"
            ):
                raise AuthorityOutboxError("FOUND_SUCCESS requires a success receipt")
        elif self.delivery_result is not None:
            raise AuthorityOutboxError("non-success reconciliation cannot carry a receipt")


@dataclass(frozen=True)
class DeliveryDisposition:
    message_id: str
    status: str
    attempt_count: int
    claim_epoch: int
    next_attempt_at: int | None
    provider_receipt_id: str | None
    callback_invoked: bool


def _delivery_failure_point(_stage: str) -> None:
    """Test seam used to model process death after provider success."""


def _canonical_outbox(row: sqlite3.Row) -> bytes:
    value = row["envelope_json"]
    digest = row["envelope_sha256"]
    if type(value) is not str or type(digest) is not str:
        raise AuthorityOutboxError("outbox envelope identity is malformed")
    try:
        raw = value.encode("utf-8", errors="strict")
        decoded = json.loads(value)
    except (UnicodeEncodeError, json.JSONDecodeError) as exc:
        raise AuthorityOutboxError("outbox envelope is malformed") from exc
    if canonical_bytes(decoded) != raw or hashlib.sha256(raw).hexdigest() != digest:
        raise AuthorityOutboxError("outbox envelope bytes or hash differ")
    return raw


def _request(row: sqlite3.Row) -> OutboxDeliveryRequest:
    return OutboxDeliveryRequest(
        str(row["message_id"]), str(row["workflow_id"]), int(row["revision"]),
        str(row["topic"]), str(row["delivery_key"]), _canonical_outbox(row),
        str(row["envelope_sha256"]),
    )


class AuthorityOutboxConsumer:
    __slots__ = ("_path", "_consumer_id", "_consumer_epoch", "_expected_source_fence")

    def __init__(
        self,
        database: str | Path,
        *,
        consumer_id: str,
        consumer_epoch: int,
        expected_source_fence_sha256: str,
    ) -> None:
        self._path = authority_database_path(database)
        self._consumer_id = str(_identifier(consumer_id, "consumer_id"))
        self._consumer_epoch = _nonnegative(consumer_epoch, "consumer_epoch")
        if self._consumer_epoch < 1:
            raise AuthorityOutboxError("consumer_epoch must be positive")
        self._expected_source_fence = _sha(
            expected_source_fence_sha256, "expected_source_fence_sha256"
        )

    def _verify(
        self,
        connection: sqlite3.Connection,
        *,
        reconciliation_only: bool = False,
    ) -> None:
        verify_production_installation(connection, require_ready=True)
        if legacy_source_identity_sha256(connection) != self._expected_source_fence:
            raise AuthorityOutboxConsumerFenceLost("consumer source fence differs")
        writer = connection.execute(
            "SELECT switch_mode FROM authority_production_writer_state WHERE singleton=1"
        ).fetchone()
        consumer = connection.execute(
            "SELECT * FROM authority_production_consumer_state WHERE singleton=1"
        ).fetchone()
        allowed_modes = (
            {CANARY, AUTHORITY_PRIMARY, "V1_ONLY"}
            if reconciliation_only
            else {CANARY, AUTHORITY_PRIMARY}
        )
        if writer is None or writer["switch_mode"] not in allowed_modes:
            raise AuthorityOutboxConsumerFenceLost("Authority delivery switch is inactive")
        if (
            consumer is None
            or not consumer["consumer_enabled"]
            or consumer["consumer_id"] != self._consumer_id
            or consumer["consumer_epoch"] != self._consumer_epoch
        ):
            raise AuthorityOutboxConsumerFenceLost("durable consumer identity or epoch differs")

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        message_id: str,
        transition_kind: str,
        consumer_id: str | None,
        consumer_epoch: int | None,
        claim_epoch: int,
        occurred_at: int,
        details: dict[str, object],
    ) -> str:
        body = {
            "schema": "authority-outbox-delivery-audit-v1",
            "message_id": message_id,
            "transition_kind": transition_kind,
            "consumer_id": consumer_id,
            "consumer_epoch": consumer_epoch,
            "claim_epoch": claim_epoch,
            "occurred_at": occurred_at,
            "details": details,
        }
        digest = canonical_sha256(body)
        audit_id = f"outbox-audit:{digest[:32]}"
        connection.execute(
            """
            INSERT INTO authority_production_outbox_delivery_audit(
                audit_id, message_id, transition_kind, consumer_id,
                consumer_epoch, claim_epoch, occurred_at, details_json, audit_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id, message_id, transition_kind, consumer_id,
                consumer_epoch, claim_epoch, occurred_at,
                canonical_bytes(details).decode("utf-8"), digest,
            ),
        )
        return digest

    def claim_next(self, *, occurred_at: int, lease_seconds: int) -> OutboxClaim | None:
        now = _nonnegative(occurred_at, "occurred_at")
        lease = _nonnegative(lease_seconds, "lease_seconds")
        if lease < 1:
            raise AuthorityOutboxError("lease_seconds must be positive")
        connection = connect_authority_rw(self._path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify(connection)
            row = connection.execute(
                """
                SELECT o.*, ds.delivery_key, ds.status, ds.attempt_count,
                       ds.claim_epoch, ds.next_attempt_at
                FROM authority_outbox o
                JOIN authority_production_outbox_delivery_state ds
                  ON ds.message_id=o.message_id
                WHERE ds.status='PENDING'
                   OR (ds.status='RETRY_WAIT' AND ds.next_attempt_at<=?)
                ORDER BY o.revision, o.message_id LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            request = _request(row)
            claim_epoch = int(row["claim_epoch"]) + 1
            attempt = int(row["attempt_count"]) + 1
            expires = now + lease
            updated = connection.execute(
                """
                UPDATE authority_production_outbox_delivery_state
                SET status='CLAIMED', attempt_count=?, claim_consumer_id=?,
                    claim_consumer_epoch=?, claim_epoch=?, lease_expires_at=?,
                    next_attempt_at=NULL, last_error_code=NULL, updated_at=?
                WHERE message_id=? AND status IN ('PENDING', 'RETRY_WAIT')
                  AND claim_epoch=?
                """,
                (
                    attempt, self._consumer_id, self._consumer_epoch,
                    claim_epoch, expires, now, request.message_id,
                    int(row["claim_epoch"]),
                ),
            )
            if updated.rowcount != 1:
                raise AuthorityOutboxClaimConflict("outbox claim CAS was lost")
            self._audit(
                connection, message_id=request.message_id, transition_kind="CLAIMED",
                consumer_id=self._consumer_id, consumer_epoch=self._consumer_epoch,
                claim_epoch=claim_epoch, occurred_at=now,
                details={"attempt_count": attempt, "lease_expires_at": expires},
            )
            connection.commit()
            return OutboxClaim(
                request, self._consumer_id, self._consumer_epoch,
                claim_epoch, attempt, expires,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def recover_expired_claims(self, *, occurred_at: int) -> tuple[str, ...]:
        now = _nonnegative(occurred_at, "occurred_at")
        connection = connect_authority_rw(self._path)
        recovered: list[str] = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify(connection, reconciliation_only=True)
            rows = connection.execute(
                """
                SELECT message_id, claim_consumer_id, claim_consumer_epoch, claim_epoch
                FROM authority_production_outbox_delivery_state
                WHERE status='CLAIMED' AND lease_expires_at<=?
                ORDER BY message_id
                """,
                (now,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE authority_production_outbox_delivery_state
                    SET status='RECONCILIATION_REQUIRED', lease_expires_at=NULL,
                        last_error_code='CLAIM_EXPIRED_PROVIDER_STATE_UNKNOWN', updated_at=?
                    WHERE message_id=? AND status='CLAIMED' AND claim_epoch=?
                    """,
                    (now, row["message_id"], row["claim_epoch"]),
                )
                self._audit(
                    connection, message_id=str(row["message_id"]),
                    transition_kind="EXPIRED_TO_RECONCILIATION",
                    consumer_id=row["claim_consumer_id"],
                    consumer_epoch=row["claim_consumer_epoch"],
                    claim_epoch=int(row["claim_epoch"]), occurred_at=now,
                    details={"reason_code": "CLAIM_EXPIRED_PROVIDER_STATE_UNKNOWN"},
                )
                recovered.append(str(row["message_id"]))
            connection.commit()
            return tuple(recovered)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _claim_row(
        self, connection: sqlite3.Connection, claim: OutboxClaim
    ) -> sqlite3.Row:
        if type(claim) is not OutboxClaim:
            raise AuthorityOutboxError("claim type is unsupported")
        row = connection.execute(
            """
            SELECT o.*, ds.delivery_key, ds.status, ds.attempt_count,
                   ds.claim_consumer_id, ds.claim_consumer_epoch,
                   ds.claim_epoch, ds.lease_expires_at, ds.next_attempt_at,
                   ds.provider_receipt_id
            FROM authority_outbox o
            JOIN authority_production_outbox_delivery_state ds
              ON ds.message_id=o.message_id
            WHERE o.message_id=?
            """,
            (claim.request.message_id,),
        ).fetchone()
        if row is None:
            raise AuthorityOutboxClaimConflict("claimed message is missing")
        if row["status"] == "DELIVERED":
            return row
        if (
            row["status"] != "CLAIMED"
            or row["claim_consumer_id"] != claim.consumer_id
            or row["claim_consumer_epoch"] != claim.consumer_epoch
            or row["claim_epoch"] != claim.claim_epoch
            or row["attempt_count"] != claim.attempt_count
            or row["delivery_key"] != claim.request.delivery_key
        ):
            raise AuthorityOutboxClaimConflict("claim fence is stale")
        return row

    def _apply_result(
        self,
        claim: OutboxClaim,
        result: ProviderDeliveryResult,
        *,
        occurred_at: int,
        max_attempts: int,
        retry_backoff_seconds: int,
        callback_invoked: bool,
        reconciliation_only: bool = False,
    ) -> DeliveryDisposition:
        now = _nonnegative(occurred_at, "occurred_at")
        maximum = _nonnegative(max_attempts, "max_attempts")
        backoff = _nonnegative(retry_backoff_seconds, "retry_backoff_seconds")
        if maximum < 1:
            raise AuthorityOutboxError("max_attempts must be positive")
        connection = connect_authority_rw(self._path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify(connection, reconciliation_only=reconciliation_only)
            row = self._claim_row(connection, claim)
            if row["status"] == "DELIVERED":
                connection.commit()
                return DeliveryDisposition(
                    claim.request.message_id, "DELIVERED", int(row["attempt_count"]),
                    int(row["claim_epoch"]), None, row["provider_receipt_id"], False,
                )
            if result.status == "SUCCEEDED":
                receipt_body = {
                    "schema": "authority-provider-receipt-v1",
                    "provider_receipt_id": result.provider_receipt_id,
                    "message_id": claim.request.message_id,
                    "delivery_key": claim.request.delivery_key,
                    "provider_status": result.provider_status,
                    "fields": [list(pair) for pair in result.receipt_fields],
                }
                receipt_sha = canonical_sha256(receipt_body)
                connection.execute(
                    """
                    INSERT INTO authority_production_provider_receipts(
                        provider_receipt_id, message_id, delivery_key,
                        provider_status, receipt_json, receipt_sha256, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.provider_receipt_id, claim.request.message_id,
                        claim.request.delivery_key, result.provider_status,
                        canonical_bytes(receipt_body).decode("utf-8"), receipt_sha, now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE authority_production_outbox_delivery_state
                    SET status='DELIVERED', lease_expires_at=NULL, next_attempt_at=NULL,
                        last_error_code=NULL, provider_receipt_id=?,
                        provider_receipt_sha256=?, delivered_at=?, updated_at=?
                    WHERE message_id=? AND status='CLAIMED' AND claim_epoch=?
                    """,
                    (
                        result.provider_receipt_id, receipt_sha, now, now,
                        claim.request.message_id, claim.claim_epoch,
                    ),
                )
                status = "DELIVERED"
                next_attempt = None
                error_code = None
            else:
                dead = result.status == "PERMANENT_FAILURE" or claim.attempt_count >= maximum
                status = "DEAD_LETTER" if dead else "RETRY_WAIT"
                next_attempt = None if dead else now + backoff
                error_code = str(result.error_code)
                connection.execute(
                    """
                    UPDATE authority_production_outbox_delivery_state
                    SET status=?, lease_expires_at=NULL, next_attempt_at=?,
                        last_error_code=?, updated_at=?
                    WHERE message_id=? AND status='CLAIMED' AND claim_epoch=?
                    """,
                    (
                        status, next_attempt, error_code, now,
                        claim.request.message_id, claim.claim_epoch,
                    ),
                )
            self._audit(
                connection, message_id=claim.request.message_id,
                transition_kind=status, consumer_id=self._consumer_id,
                consumer_epoch=self._consumer_epoch, claim_epoch=claim.claim_epoch,
                occurred_at=now,
                details={
                    "attempt_count": claim.attempt_count,
                    "error_code": error_code,
                    "next_attempt_at": next_attempt,
                    "provider_receipt_id": result.provider_receipt_id,
                },
            )
            connection.commit()
            return DeliveryDisposition(
                claim.request.message_id, status, claim.attempt_count,
                claim.claim_epoch, next_attempt, result.provider_receipt_id,
                callback_invoked,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def deliver_claim(
        self,
        claim: OutboxClaim,
        *,
        provider_callback: Callable[[OutboxDeliveryRequest], ProviderDeliveryResult],
        occurred_at: int,
        max_attempts: int,
        retry_backoff_seconds: int,
    ) -> DeliveryDisposition:
        if not callable(provider_callback):
            raise AuthorityOutboxError("provider_callback must be callable")
        probe = connect_authority_ro(self._path)
        try:
            probe.execute("BEGIN")
            self._verify(probe)
            row = self._claim_row(probe, claim)
            if row["status"] == "DELIVERED":
                probe.commit()
                return DeliveryDisposition(
                    claim.request.message_id, "DELIVERED", int(row["attempt_count"]),
                    int(row["claim_epoch"]), None, row["provider_receipt_id"], False,
                )
            probe.commit()
        finally:
            probe.close()
        result = provider_callback(claim.request)
        if type(result) is not ProviderDeliveryResult:
            raise AuthorityOutboxError("provider callback returned an unsupported result")
        _delivery_failure_point("after_provider_before_receipt")
        return self._apply_result(
            claim, result, occurred_at=occurred_at, max_attempts=max_attempts,
            retry_backoff_seconds=retry_backoff_seconds, callback_invoked=True,
        )

    def reconcile(
        self,
        message_id: str,
        *,
        provider_lookup: Callable[[OutboxDeliveryRequest], ProviderReconciliationResult],
        occurred_at: int,
        max_attempts: int,
        retry_backoff_seconds: int,
    ) -> DeliveryDisposition:
        message = str(_identifier(message_id, "message_id"))
        if not callable(provider_lookup):
            raise AuthorityOutboxError("provider_lookup must be callable")
        connection = connect_authority_ro(self._path)
        try:
            connection.execute("BEGIN")
            self._verify(connection, reconciliation_only=True)
            row = connection.execute(
                """
                SELECT o.*, ds.delivery_key, ds.status, ds.attempt_count,
                       ds.claim_consumer_id, ds.claim_consumer_epoch,
                       ds.claim_epoch, ds.provider_receipt_id
                FROM authority_outbox o
                JOIN authority_production_outbox_delivery_state ds
                  ON ds.message_id=o.message_id
                WHERE o.message_id=?
                """,
                (message,),
            ).fetchone()
            if row is None:
                raise AuthorityOutboxClaimConflict("reconciliation message is missing")
            if row["status"] == "DELIVERED":
                connection.commit()
                return DeliveryDisposition(
                    message, "DELIVERED", int(row["attempt_count"]),
                    int(row["claim_epoch"]), None, row["provider_receipt_id"], False,
                )
            if row["status"] != "RECONCILIATION_REQUIRED":
                raise AuthorityOutboxClaimConflict("message does not require reconciliation")
            request = _request(row)
            attempt = int(row["attempt_count"])
            claim_epoch = int(row["claim_epoch"])
            connection.commit()
        finally:
            connection.close()
        result = provider_lookup(request)
        if type(result) is not ProviderReconciliationResult:
            raise AuthorityOutboxError("provider lookup returned an unsupported result")
        if result.status == "UNKNOWN":
            return DeliveryDisposition(
                message, "RECONCILIATION_REQUIRED", attempt, claim_epoch,
                None, None, True,
            )
        synthetic_claim = OutboxClaim(
            request, self._consumer_id, self._consumer_epoch,
            claim_epoch, attempt, _nonnegative(occurred_at, "occurred_at"),
        )
        promote = connect_authority_rw(self._path)
        try:
            promote.execute("BEGIN IMMEDIATE")
            self._verify(promote, reconciliation_only=True)
            updated = promote.execute(
                """
                UPDATE authority_production_outbox_delivery_state
                SET status='CLAIMED', claim_consumer_id=?, claim_consumer_epoch=?,
                    lease_expires_at=?
                WHERE message_id=? AND status='RECONCILIATION_REQUIRED' AND claim_epoch=?
                """,
                (
                    self._consumer_id, self._consumer_epoch,
                    int(occurred_at), message, claim_epoch,
                ),
            )
            if updated.rowcount != 1:
                raise AuthorityOutboxClaimConflict("reconciliation claim changed")
            promote.commit()
        except Exception:
            promote.rollback()
            raise
        finally:
            promote.close()
        delivery_result = (
            result.delivery_result
            if result.status == "FOUND_SUCCESS"
            else ProviderDeliveryResult("RETRYABLE_FAILURE", "PROVIDER_CONFIRMED_ABSENT")
        )
        assert delivery_result is not None
        return self._apply_result(
            synthetic_claim, delivery_result, occurred_at=occurred_at,
            max_attempts=max_attempts, retry_backoff_seconds=retry_backoff_seconds,
            callback_invoked=True, reconciliation_only=True,
        )
