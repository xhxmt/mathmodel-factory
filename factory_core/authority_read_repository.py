"""Supported query-only, revision-atomic Authority repository."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Iterator

from .authority_repository import (
    AUTHORITY_IDEMPOTENCY_REQUEST_SCHEMA,
    AuthorityEnvelopePersistenceError,
)
from .authority_operations import _identifier, _nonnegative, _sha
from .authority_production_schema import (
    authority_database_path,
    connect_authority_ro,
    legacy_source_identity_sha256,
    verify_production_installation,
)
from .canonical import canonical_bytes, canonical_sha256
from .phase3_artifacts import (
    ArtifactLedgerOccurrence,
    ArtifactOccurrenceKind,
    CheckpointLedgerOccurrence,
    Phase3ContractError,
    Phase3Mutation,
    phase3_mutation_from_dict,
)
from . import authority_production_writer as _production_writer


class AuthorityReadError(RuntimeError):
    """Raised when a supported read cannot prove its immutable identity."""


@dataclass(frozen=True)
class AuthorityWorkflowCoordinate:
    workflow_id: str
    project_id: str
    project_generation: str
    run_generation: str
    runtime_generation: str
    scheduler_generation: str
    current_revision: int
    contract_pin_set_sha256: str | None
    authority_state: str
    source_fence_sha256: str
    switch_mode: str
    switch_epoch: int

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "authority-workflow-coordinate-v1",
            "workflow_id": self.workflow_id,
            "project_id": self.project_id,
            "project_generation": self.project_generation,
            "run_generation": self.run_generation,
            "runtime_generation": self.runtime_generation,
            "scheduler_generation": self.scheduler_generation,
            "current_revision": self.current_revision,
            "contract_pin_set_sha256": self.contract_pin_set_sha256,
            "authority_state": self.authority_state,
            "source_fence_sha256": self.source_fence_sha256,
            "switch_mode": self.switch_mode,
            "switch_epoch": self.switch_epoch,
        }

    @property
    def coordinate_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True)
class AuthorityEventRecord:
    event_id: str
    workflow_id: str
    revision: int
    command_id: str
    event_type: str
    envelope_bytes: bytes
    envelope_sha256: str


@dataclass(frozen=True)
class AuthorityCommandBundle:
    workflow_id: str
    revision: int
    command_id: str
    event_id: str
    receipt_id: str
    message_id: str
    command_bytes: bytes
    event_bytes: bytes
    receipt_bytes: bytes
    outbox_bytes: bytes
    command_sha256: str
    event_sha256: str
    receipt_sha256: str
    outbox_sha256: str
    bundle_sha256: str
    writer_id: str
    writer_epoch: int
    switch_epoch: int
    switch_mode: str
    delivery_status: str
    delivery_key: str
    request_schema: str = AUTHORITY_IDEMPOTENCY_REQUEST_SCHEMA
    request_sha256: str | None = None
    bundle_schema: str = _production_writer.AUTHORITY_PRODUCTION_BUNDLE_SCHEMA_V1
    phase3_mutation_sha256: str | None = None
    phase3_mutation: Phase3Mutation | None = None
    phase3_artifact_occurrences: tuple[ArtifactLedgerOccurrence, ...] = ()
    phase3_checkpoint_occurrences: tuple[CheckpointLedgerOccurrence, ...] = ()

    def identity_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": "authority-read-command-bundle-v1",
            "workflow_id": self.workflow_id,
            "revision": self.revision,
            "command_id": self.command_id,
            "event_id": self.event_id,
            "receipt_id": self.receipt_id,
            "message_id": self.message_id,
            "command_sha256": self.command_sha256,
            "event_sha256": self.event_sha256,
            "receipt_sha256": self.receipt_sha256,
            "outbox_sha256": self.outbox_sha256,
            "bundle_sha256": self.bundle_sha256,
            "writer_id": self.writer_id,
            "writer_epoch": self.writer_epoch,
            "switch_epoch": self.switch_epoch,
            "switch_mode": self.switch_mode,
            "delivery_status": self.delivery_status,
            "delivery_key": self.delivery_key,
        }
        if self.phase3_mutation_sha256 is not None:
            value["schema"] = "authority-read-command-bundle-v2"
            value["request_schema"] = self.request_schema
            value["request_sha256"] = self.request_sha256
            value["bundle_schema"] = self.bundle_schema
            value["phase3_mutation_sha256"] = self.phase3_mutation_sha256
            value["phase3_artifact_occurrence_ids"] = tuple(
                item.occurrence_id for item in self.phase3_artifact_occurrences
            )
            value["phase3_checkpoint_occurrence_ids"] = tuple(
                item.occurrence_id for item in self.phase3_checkpoint_occurrences
            )
        return value


@dataclass(frozen=True)
class AuthorityPhase3MutationBundle:
    workflow_id: str
    revision: int
    command_id: str
    mutation: Phase3Mutation
    artifact_occurrences: tuple[ArtifactLedgerOccurrence, ...]
    checkpoint_occurrences: tuple[CheckpointLedgerOccurrence, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "authority-read-phase3-mutation-bundle-v1",
            "workflow_id": self.workflow_id,
            "revision": self.revision,
            "command_id": self.command_id,
            "phase3_mutation_sha256": self.mutation.mutation_sha256,
            "artifact_record_ids": [
                item.artifact_record_id for item in self.mutation.artifact_records
            ],
            "artifact_occurrence_ids": [
                item.occurrence_id for item in self.artifact_occurrences
            ],
            "checkpoint_ids": [
                item.checkpoint_id for item in self.mutation.checkpoint_entries
            ],
            "checkpoint_occurrence_ids": [
                item.occurrence_id for item in self.checkpoint_occurrences
            ],
            "reopen_plan_id": (
                self.mutation.reopen_plan.reopen_plan_id
                if self.mutation.reopen_plan is not None
                else (
                    None
                    if self.mutation.blocked_disposition is None
                    else self.mutation.blocked_disposition.disposition_id
                )
            ),
        }


@dataclass(frozen=True)
class AuthorityPhase3ArtifactState:
    workflow_id: str
    through_revision: int
    occurrences: tuple[ArtifactLedgerOccurrence, ...]

    @property
    def present_records(self):
        return tuple(
            occurrence.artifact_record
            for occurrence in self.occurrences
            if occurrence.kind is ArtifactOccurrenceKind.RECORD
        )

    @property
    def blockers(self):
        return tuple(
            occurrence.blocker
            for occurrence in self.occurrences
            if occurrence.kind is ArtifactOccurrenceKind.BLOCKER
        )

    @property
    def tombstones(self):
        return tuple(
            occurrence.removal
            for occurrence in self.occurrences
            if occurrence.kind is ArtifactOccurrenceKind.REMOVAL
        )


@dataclass(frozen=True)
class AuthorityRevisionSnapshot:
    coordinate: AuthorityWorkflowCoordinate
    through_revision: int
    events: tuple[AuthorityEventRecord, ...]

    @property
    def snapshot_sha256(self) -> str:
        return canonical_sha256(
            {
                "schema": "authority-revision-stream-v1",
                "coordinate": self.coordinate.as_dict(),
                "through_revision": self.through_revision,
                "events": [
                    {
                        "event_id": event.event_id,
                        "revision": event.revision,
                        "command_id": event.command_id,
                        "event_type": event.event_type,
                        "envelope_sha256": event.envelope_sha256,
                    }
                    for event in self.events
                ],
            }
        )


@dataclass(frozen=True)
class AuthorityOutboxDeliveryView:
    message_id: str
    workflow_id: str
    revision: int
    delivery_key: str
    status: str
    attempt_count: int
    claim_consumer_id: str | None
    claim_consumer_epoch: int | None
    claim_epoch: int
    lease_expires_at: int | None
    next_attempt_at: int | None
    provider_receipt_id: str | None
    provider_receipt_sha256: str | None


def _canonical_envelope(value: object, stored_sha256: object, path: str) -> bytes:
    if type(value) is not str or type(stored_sha256) is not str:
        raise AuthorityReadError(f"{path} envelope identity is malformed")
    try:
        raw = value.encode("utf-8", errors="strict")
        decoded = json.loads(value)
    except (UnicodeEncodeError, json.JSONDecodeError) as exc:
        raise AuthorityReadError(f"{path} envelope is not valid canonical JSON") from exc
    if canonical_bytes(decoded) != raw:
        raise AuthorityReadError(f"{path} envelope bytes are not canonical")
    if hashlib.sha256(raw).hexdigest() != stored_sha256:
        raise AuthorityReadError(f"{path} envelope hash differs")
    return raw


def _phase3_wrapper_text(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise AuthorityReadError(f"Phase-3 {field} is malformed")
    return value


def _phase3_mutation_at_revision(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    revision: int,
    command_id: str,
) -> AuthorityPhase3MutationBundle | None:
    try:
        bundle = _production_writer._phase3_bundle_at_revision(
            connection,
            workflow_id=workflow_id,
            revision=revision,
            command_id=command_id,
        )
    except AuthorityEnvelopePersistenceError as exc:
        raise AuthorityReadError(str(exc)) from exc
    if bundle is None:
        return None
    return AuthorityPhase3MutationBundle(
        workflow_id,
        revision,
        command_id,
        bundle.mutation,
        bundle.artifact_occurrences,
        bundle.checkpoint_occurrences,
    )


class AuthorityReadRepository:
    """Frozen supported reads over one explicit read-only SQLite transaction."""

    __slots__ = ("_path", "_expected_source_fence")

    def __init__(
        self, database: str | Path, *, expected_source_fence_sha256: str
    ) -> None:
        self._path = authority_database_path(database)
        self._expected_source_fence = _sha(
            expected_source_fence_sha256, "expected_source_fence_sha256"
        )

    @contextmanager
    def _snapshot(self) -> Iterator[sqlite3.Connection]:
        connection = connect_authority_ro(self._path)
        try:
            connection.execute("BEGIN")
            verify_production_installation(connection, require_ready=True)
            if legacy_source_identity_sha256(connection) != self._expected_source_fence:
                raise AuthorityReadError("read source fence differs")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _coordinate(
        self, connection: sqlite3.Connection, workflow_id: str
    ) -> AuthorityWorkflowCoordinate:
        key = str(_identifier(workflow_id, "workflow_id"))
        row = connection.execute(
            """
            SELECT w.*, s.switch_mode, s.switch_epoch
            FROM authority_workflows w
            CROSS JOIN authority_production_writer_state s
            WHERE w.workflow_id=? AND s.singleton=1
            """,
            (key,),
        ).fetchone()
        if row is None:
            raise AuthorityReadError(f"unknown workflow: {key}")
        if (
            row["current_revision_availability"] != "RECORDED"
            or type(row["current_revision"]) is not int
        ):
            raise AuthorityReadError("workflow revision is unavailable")
        pin = row["contract_pin_set_sha256"]
        if row["contract_pin_availability"] == "RECORDED":
            _sha(pin, "contract_pin_set_sha256")
        elif pin is not None:
            raise AuthorityReadError("unavailable contract pin unexpectedly has bytes")
        return AuthorityWorkflowCoordinate(
            key, str(row["project_id"]), str(row["project_generation"]),
            str(row["run_generation"]), str(row["runtime_generation"]),
            str(row["scheduler_generation"]), int(row["current_revision"]),
            pin, str(row["authority_state"]), self._expected_source_fence,
            str(row["switch_mode"]), int(row["switch_epoch"]),
        )

    def workflow_coordinate(self, workflow_id: str) -> AuthorityWorkflowCoordinate:
        with self._snapshot() as connection:
            return self._coordinate(connection, workflow_id)

    def command_bundle(
        self, *, workflow_id: str, idempotency_key: str
    ) -> AuthorityCommandBundle:
        key = str(_identifier(workflow_id, "workflow_id"))
        idempotency = str(_identifier(idempotency_key, "idempotency_key"))
        with self._snapshot() as connection:
            coordinate = self._coordinate(connection, key)
            rows = connection.execute(
                """
                SELECT c.command_id, c.workflow_id, c.persisted_revision,
                       i.request_schema, i.request_sha256,
                       c.envelope_json AS command_json,
                       c.envelope_sha256 AS command_sha256,
                       e.event_id, e.revision, e.command_id AS event_command_id,
                       e.envelope_json AS event_json, e.envelope_sha256 AS event_sha256,
                       r.receipt_id, r.command_id AS receipt_command_id,
                       r.event_id AS receipt_event_id,
                       r.envelope_json AS receipt_json,
                       r.envelope_sha256 AS receipt_sha256,
                       o.message_id, o.event_id AS outbox_event_id,
                       o.envelope_json AS outbox_json,
                       o.envelope_sha256 AS outbox_sha256,
                       pc.bundle_sha256, pc.writer_id, pc.writer_epoch,
                       pc.switch_epoch, pc.switch_mode,
                       ds.status AS delivery_status, ds.delivery_key
                FROM authority_idempotency_records i
                JOIN authority_commands c ON c.command_id=i.command_id
                JOIN authority_events e ON e.command_id=c.command_id
                JOIN authority_receipts r ON r.event_id=e.event_id
                JOIN authority_outbox o ON o.event_id=e.event_id
                JOIN authority_production_command_commits pc ON pc.command_id=c.command_id
                JOIN authority_production_outbox_delivery_state ds ON ds.message_id=o.message_id
                WHERE i.scope_kind='workflow' AND i.scope_id=? AND i.idempotency_key=?
                """,
                (key, idempotency),
            ).fetchall()
            if not rows:
                raise AuthorityReadError("command bundle is unavailable")
            if len(rows) != 1:
                raise AuthorityReadError(
                    "command bundle companion cardinality differs"
                )
            row = rows[0]
            if not (
                row["workflow_id"] == key
                and row["persisted_revision"] == row["revision"]
                and row["event_command_id"] == row["command_id"]
                and row["receipt_command_id"] == row["command_id"]
                and row["receipt_event_id"] == row["event_id"]
                and row["outbox_event_id"] == row["event_id"]
                and int(row["revision"]) <= coordinate.current_revision
            ):
                raise AuthorityReadError("command bundle cross-identity differs")
            command_bytes = _canonical_envelope(
                row["command_json"], row["command_sha256"], "command"
            )
            event_bytes = _canonical_envelope(
                row["event_json"], row["event_sha256"], "event"
            )
            receipt_bytes = _canonical_envelope(
                row["receipt_json"], row["receipt_sha256"], "receipt"
            )
            outbox_bytes = _canonical_envelope(
                row["outbox_json"], row["outbox_sha256"], "outbox"
            )
            phase3_bundle = _phase3_mutation_at_revision(
                connection,
                workflow_id=key,
                revision=int(row["revision"]),
                command_id=str(row["command_id"]),
            )
            if phase3_bundle is None:
                request_schema = AUTHORITY_IDEMPOTENCY_REQUEST_SCHEMA
                request_sha256 = str(row["command_sha256"])
                bundle_schema = (
                    _production_writer.AUTHORITY_PRODUCTION_BUNDLE_SCHEMA_V1
                )
                expected_bundle = _production_writer._bundle_sha256(
                    str(row["command_sha256"]),
                    str(row["event_sha256"]),
                    str(row["receipt_sha256"]),
                    str(row["outbox_sha256"]),
                )
                mutation_sha256 = None
                mutation = None
                artifact_occurrences = ()
                checkpoint_occurrences = ()
            else:
                try:
                    continuity = _production_writer._validated_phase3_bundles_through(
                        connection,
                        workflow_id=key,
                        through_revision=int(row["revision"]),
                    )
                except AuthorityEnvelopePersistenceError as exc:
                    raise AuthorityReadError(str(exc)) from exc
                if (
                    not continuity
                    or continuity[-1].revision != phase3_bundle.revision
                    or continuity[-1].command_id != phase3_bundle.command_id
                    or continuity[-1].mutation != phase3_bundle.mutation
                ):
                    raise AuthorityReadError(
                        "Phase-3 previous head continuity differs"
                    )
                request_schema = (
                    _production_writer.AUTHORITY_PHASE3_IDEMPOTENCY_REQUEST_SCHEMA
                )
                mutation = phase3_bundle.mutation
                artifact_occurrences = phase3_bundle.artifact_occurrences
                checkpoint_occurrences = phase3_bundle.checkpoint_occurrences
                mutation_sha256 = mutation.mutation_sha256
                request_sha256 = _production_writer._phase3_request_sha256(
                    str(row["command_sha256"]), mutation_sha256
                )
                bundle_schema = (
                    _production_writer.AUTHORITY_PRODUCTION_BUNDLE_SCHEMA_V2
                )
                expected_bundle = _production_writer._phase3_bundle_sha256(
                    str(row["command_sha256"]),
                    str(row["event_sha256"]),
                    str(row["receipt_sha256"]),
                    str(row["outbox_sha256"]),
                    mutation_sha256,
                )
            if (
                row["request_schema"] != request_schema
                or row["request_sha256"] != request_sha256
            ):
                raise AuthorityReadError("production request identity differs")
            if row["bundle_sha256"] != expected_bundle:
                raise AuthorityReadError("production command bundle hash differs")
            return AuthorityCommandBundle(
                key, int(row["revision"]), str(row["command_id"]),
                str(row["event_id"]), str(row["receipt_id"]), str(row["message_id"]),
                command_bytes, event_bytes, receipt_bytes, outbox_bytes,
                str(row["command_sha256"]), str(row["event_sha256"]),
                str(row["receipt_sha256"]), str(row["outbox_sha256"]),
                expected_bundle, str(row["writer_id"]), int(row["writer_epoch"]),
                int(row["switch_epoch"]), str(row["switch_mode"]),
                str(row["delivery_status"]),
                str(row["delivery_key"]),
                request_schema,
                request_sha256,
                bundle_schema,
                mutation_sha256,
                mutation,
                artifact_occurrences,
                checkpoint_occurrences,
            )

    def phase3_artifact_state(
        self,
        workflow_id: str,
        *,
        through_revision: int | None = None,
    ) -> AuthorityPhase3ArtifactState:
        """Return one typed latest occurrence per path at a revision boundary."""

        with self._snapshot() as connection:
            coordinate = self._coordinate(connection, workflow_id)
            boundary = (
                coordinate.current_revision
                if through_revision is None
                else _nonnegative(through_revision, "through_revision")
            )
            if boundary > coordinate.current_revision:
                raise AuthorityReadError(
                    "through_revision exceeds the snapshot revision"
                )
            try:
                bundles = _production_writer._validated_phase3_bundles_through(
                    connection,
                    workflow_id=coordinate.workflow_id,
                    through_revision=boundary,
                )
            except AuthorityEnvelopePersistenceError as exc:
                raise AuthorityReadError(str(exc)) from exc
            latest: dict[str, ArtifactLedgerOccurrence] = {}
            for bundle in bundles:
                for occurrence in bundle.artifact_occurrences:
                    latest[occurrence.normalized_path] = occurrence
            return AuthorityPhase3ArtifactState(
                coordinate.workflow_id,
                boundary,
                tuple(latest[path] for path in sorted(latest)),
            )

    def revision_snapshot(
        self,
        workflow_id: str,
        *,
        after_revision: int = 0,
        through_revision: int | None = None,
    ) -> AuthorityRevisionSnapshot:
        after = _nonnegative(after_revision, "after_revision")
        if through_revision is not None:
            through = _nonnegative(through_revision, "through_revision")
            if through < after:
                raise AuthorityReadError("through_revision precedes after_revision")
        else:
            through = None
        with self._snapshot() as connection:
            coordinate = self._coordinate(connection, workflow_id)
            boundary = coordinate.current_revision if through is None else through
            if boundary > coordinate.current_revision:
                raise AuthorityReadError("through_revision exceeds the snapshot revision")
            if after > boundary:
                raise AuthorityReadError("after_revision exceeds through_revision")
            rows = connection.execute(
                """
                SELECT event_id, workflow_id, revision, command_id, event_type,
                       envelope_json, envelope_sha256
                FROM authority_events
                WHERE workflow_id=? AND revision>? AND revision<=?
                ORDER BY revision
                """,
                (coordinate.workflow_id, after, boundary),
            ).fetchall()
            events: list[AuthorityEventRecord] = []
            last = after
            for row in rows:
                if type(row["revision"]) is not int or int(row["revision"]) <= last:
                    raise AuthorityReadError("event stream revision order is malformed")
                raw = _canonical_envelope(
                    row["envelope_json"], row["envelope_sha256"], "event"
                )
                events.append(
                    AuthorityEventRecord(
                        str(row["event_id"]), str(row["workflow_id"]),
                        int(row["revision"]), str(row["command_id"]),
                        str(row["event_type"]), raw, str(row["envelope_sha256"]),
                    )
                )
                last = int(row["revision"])
            return AuthorityRevisionSnapshot(coordinate, boundary, tuple(events))

    def outbox_delivery_state(self, message_id: str) -> AuthorityOutboxDeliveryView:
        message = str(_identifier(message_id, "message_id"))
        with self._snapshot() as connection:
            row = connection.execute(
                """
                SELECT o.message_id, o.workflow_id, o.revision,
                       ds.delivery_key, ds.status, ds.attempt_count,
                       ds.claim_consumer_id, ds.claim_consumer_epoch,
                       ds.claim_epoch, ds.lease_expires_at, ds.next_attempt_at,
                       ds.provider_receipt_id, ds.provider_receipt_sha256
                FROM authority_outbox o
                JOIN authority_production_outbox_delivery_state ds
                  ON ds.message_id=o.message_id
                WHERE o.message_id=?
                """,
                (message,),
            ).fetchone()
            if row is None:
                raise AuthorityReadError("outbox delivery state is unavailable")
            if row["status"] not in {
                "PENDING", "CLAIMED", "RETRY_WAIT", "RECONCILIATION_REQUIRED",
                "DELIVERED", "DEAD_LETTER",
            }:
                raise AuthorityReadError("outbox delivery status is malformed")
            return AuthorityOutboxDeliveryView(
                message, str(row["workflow_id"]), int(row["revision"]),
                str(row["delivery_key"]), str(row["status"]),
                int(row["attempt_count"]), row["claim_consumer_id"],
                row["claim_consumer_epoch"], int(row["claim_epoch"]),
                row["lease_expires_at"], row["next_attempt_at"],
                row["provider_receipt_id"], row["provider_receipt_sha256"],
            )
