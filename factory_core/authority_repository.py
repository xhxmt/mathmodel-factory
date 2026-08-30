"""Minimal repositories for the default-off authority-schema v2 shadow.

Nothing in the active Scheduler, Web API, or legacy writer imports this file.
All mutation methods require an explicit ``write_shadow=True`` constructor
argument; the module-level default is intentionally false.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import re
from pathlib import Path
import sqlite3
import stat
from typing import Iterator

from .authority_envelopes import (
    EventEnvelopeV1,
    OutboxMessageV1,
    ReceiptEnvelopeV1,
    event_envelope_bytes,
    event_envelope_sha256,
    outbox_message_bytes,
    outbox_message_sha256,
    receipt_envelope_bytes,
    receipt_envelope_sha256,
    stable_authority_bytes,
    stable_authority_sha256,
    validate_event_envelope,
    validate_outbox_message,
    validate_receipt_envelope,
)
from .authority_schema import (
    AuthorityMigrationError,
    verify_authority_schema_installation,
)
from .command_envelope import (
    CommandEnvelopeV1,
    command_envelope_bytes,
    command_envelope_sha256,
    validate_command_envelope_structure,
)
from .project_snapshot_v0 import (
    ProjectSnapshotV0,
    project_snapshot_v0_semantic_bytes,
    project_snapshot_v0_semantic_sha256,
    validate_project_snapshot_v0,
)


AUTHORITY_SCHEMA_V2_WRITE_SHADOW = False
AUTHORITY_IDEMPOTENCY_REQUEST_SCHEMA = "authority-command-envelope-request-v1"

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class AuthorityRepositoryError(RuntimeError):
    """Base repository error."""


class AuthorityWriteShadowDisabled(AuthorityRepositoryError):
    """Raised when a caller did not explicitly enable shadow writes."""


class AuthorityRepositoryNotReady(AuthorityRepositoryError):
    """Raised when the additive migration is absent or blocked."""


class AuthorityRevisionConflict(AuthorityRepositoryError):
    """Raised when an envelope does not bind the current workflow revision."""


class AuthorityIdempotencyConflict(AuthorityRepositoryError):
    """Raised when an idempotency key is reused for different request bytes."""


class AuthorityEnvelopePersistenceError(AuthorityRepositoryError):
    """Raised when linked durable envelopes disagree."""


@dataclass(frozen=True)
class AuthorityCommitResult:
    workflow_id: str
    committed_revision: int
    command_id: str
    event_id: str
    receipt_id: str
    outbox_message_id: str
    request_sha256: str
    replayed: bool


def _text(value: object, path: str, *, sha: bool = False) -> str:
    if type(value) is not str or not value:
        raise AuthorityEnvelopePersistenceError(f"{path} must be a non-empty plain string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise AuthorityEnvelopePersistenceError(f"{path} must contain valid UTF-8") from exc
    pattern = _SHA256_RE if sha else _IDENTIFIER_RE
    if pattern.fullmatch(value) is None:
        kind = "lowercase SHA-256" if sha else "bounded authority identifier"
        raise AuthorityEnvelopePersistenceError(f"{path} must be a {kind}")
    return value


def _database_path(path: str | Path) -> Path:
    value = Path(path)
    try:
        metadata = value.lstat()
    except FileNotFoundError as exc:
        raise AuthorityRepositoryNotReady(f"authority database is missing: {value}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AuthorityRepositoryNotReady("authority repository requires a regular non-symlink DB")
    return value.resolve()


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=2, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 2000")
    return connection


def _workflow(connection: sqlite3.Connection, workflow_id: str) -> sqlite3.Row:
    key = _text(workflow_id, "workflow_id")
    row = connection.execute(
        "SELECT * FROM authority_workflows WHERE workflow_id=?", (key,)
    ).fetchone()
    if row is None:
        raise AuthorityEnvelopePersistenceError(f"unknown authority workflow: {key}")
    return row


def _allocate_revision(connection: sqlite3.Connection, workflow_id: str) -> int:
    key = _text(workflow_id, "workflow_id")
    row = connection.execute(
        "SELECT next_revision FROM authority_revision_allocator WHERE workflow_id=?",
        (key,),
    ).fetchone()
    if row is None or type(row["next_revision"]) is not int:
        raise AuthorityRevisionConflict("workflow revision allocator is unavailable")
    revision = int(row["next_revision"])
    connection.execute(
        "UPDATE authority_revision_allocator SET next_revision=? WHERE workflow_id=?",
        (revision + 1, key),
    )
    return revision


def _idempotency_record(
    connection: sqlite3.Connection,
    scope_kind: str,
    scope_id: str,
    idempotency_key: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM authority_idempotency_records
        WHERE scope_kind=? AND scope_id=? AND idempotency_key=?
        """,
        (
            _text(scope_kind, "idempotency.scope_kind"),
            _text(scope_id, "idempotency.scope_id"),
            _text(idempotency_key, "idempotency.key"),
        ),
    ).fetchone()


def _persist_contract_pins(
    connection: sqlite3.Connection, command: CommandEnvelopeV1, revision: int
) -> str:
    validate_command_envelope_structure(command)
    pin_bytes = stable_authority_bytes(command.contract_pins)
    pin_json = pin_bytes.decode("utf-8", errors="strict")
    pin_sha256 = stable_authority_sha256(command.contract_pins)
    row = connection.execute(
        "SELECT * FROM authority_contract_pin_sets WHERE pin_set_sha256=?",
        (pin_sha256,),
    ).fetchone()
    if row is None:
        connection.execute(
            """
            INSERT INTO authority_contract_pin_sets(
                pin_set_sha256, schema_version, pin_set_json, provenance,
                first_recorded_revision
            ) VALUES (?, ?, ?, 'RECORDED_SHADOW', ?)
            """,
            (pin_sha256, command.contract_pins.schema_version, pin_json, revision),
        )
    elif (
        row["schema_version"] != command.contract_pins.schema_version
        or row["pin_set_json"] != pin_json
        or row["provenance"] != "RECORDED_SHADOW"
    ):
        raise AuthorityEnvelopePersistenceError("recorded contract pin identity differs")
    return pin_sha256


def _persist_command_envelope(
    connection: sqlite3.Connection,
    command: CommandEnvelopeV1,
    *,
    workflow_id: str,
    persisted_revision: int,
    idempotency_key: str,
) -> str:
    envelope = validate_command_envelope_structure(command)
    value = command_envelope_bytes(envelope)
    digest = command_envelope_sha256(envelope)
    pin_sha256 = stable_authority_sha256(envelope.contract_pins)
    connection.execute(
        """
        INSERT INTO authority_commands(
            command_id, workflow_id, project_id, requested_revision,
            persisted_revision, command_type, envelope_schema,
            envelope_json, envelope_sha256, contract_pin_set_sha256,
            idempotency_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            envelope.command_id,
            workflow_id,
            envelope.project_binding.project_id,
            envelope.project_binding.project_revision,
            persisted_revision,
            envelope.command_type.value,
            envelope.schema_version,
            value.decode("utf-8", errors="strict"),
            digest,
            pin_sha256,
            _text(idempotency_key, "idempotency_key"),
        ),
    )
    return digest


def _persist_event_envelope(connection: sqlite3.Connection, event: EventEnvelopeV1) -> str:
    envelope = validate_event_envelope(event)
    value = event_envelope_bytes(envelope)
    digest = event_envelope_sha256(envelope)
    connection.execute(
        """
        INSERT INTO authority_events(
            event_id, workflow_id, revision, command_id, event_type,
            envelope_schema, envelope_json, envelope_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            envelope.event_id,
            envelope.workflow_id,
            envelope.revision,
            envelope.command_id,
            envelope.event_type,
            envelope.schema_version,
            value.decode("utf-8", errors="strict"),
            digest,
        ),
    )
    return digest


def _persist_receipt_envelope(
    connection: sqlite3.Connection, receipt: ReceiptEnvelopeV1
) -> str:
    envelope = validate_receipt_envelope(receipt)
    value = receipt_envelope_bytes(envelope)
    digest = receipt_envelope_sha256(envelope)
    connection.execute(
        """
        INSERT INTO authority_receipts(
            receipt_id, workflow_id, revision, command_id, event_id,
            outcome, envelope_schema, envelope_json, envelope_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            envelope.receipt_id,
            envelope.workflow_id,
            envelope.revision,
            envelope.command_id,
            envelope.event_id,
            envelope.outcome,
            envelope.schema_version,
            value.decode("utf-8", errors="strict"),
            digest,
        ),
    )
    return digest


def _enqueue_outbox(connection: sqlite3.Connection, message: OutboxMessageV1) -> str:
    envelope = validate_outbox_message(message)
    value = outbox_message_bytes(envelope)
    digest = outbox_message_sha256(envelope)
    connection.execute(
        """
        INSERT INTO authority_outbox(
            message_id, workflow_id, revision, event_id, topic,
            envelope_schema, envelope_json, envelope_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            envelope.message_id,
            envelope.workflow_id,
            envelope.revision,
            envelope.event_id,
            envelope.topic,
            envelope.schema_version,
            value.decode("utf-8", errors="strict"),
            digest,
        ),
    )
    return digest


def _commit_idempotency(
    connection: sqlite3.Connection,
    *,
    scope_kind: str,
    scope_id: str,
    idempotency_key: str,
    request_sha256: str,
    command_id: str,
    receipt_id: str,
    revision: int,
    request_schema: str = AUTHORITY_IDEMPOTENCY_REQUEST_SCHEMA,
) -> None:
    connection.execute(
        """
        INSERT INTO authority_idempotency_records(
            scope_kind, scope_id, idempotency_key, request_schema,
            request_sha256, command_id, receipt_id, committed_revision
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _text(scope_kind, "idempotency.scope_kind"),
            _text(scope_id, "idempotency.scope_id"),
            _text(idempotency_key, "idempotency.key"),
            _text(request_schema, "idempotency.request_schema"),
            _text(request_sha256, "idempotency.request_sha256", sha=True),
            _text(command_id, "idempotency.command_id"),
            _text(receipt_id, "idempotency.receipt_id"),
            revision,
        ),
    )


def _update_workflow_revision(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    current_revision: int,
    revision: int,
    pin_sha256: str,
) -> None:
    updated = connection.execute(
        """
        UPDATE authority_workflows
        SET current_revision=?, contract_pin_set_sha256=?,
            contract_pin_availability='RECORDED'
        WHERE workflow_id=? AND current_revision=?
        """,
        (revision, pin_sha256, workflow_id, current_revision),
    )
    if updated.rowcount != 1:
        raise AuthorityRevisionConflict("workflow revision update lost its fence")


class AuthorityRepository:
    """Explicit authority-schema repository with default-off writes."""

    def __init__(
        self,
        database: str | Path,
        *,
        write_shadow: bool = AUTHORITY_SCHEMA_V2_WRITE_SHADOW,
    ) -> None:
        if type(write_shadow) is not bool:
            raise AuthorityRepositoryError("write_shadow must be a plain bool")
        self.path = _database_path(database)
        self.write_shadow = write_shadow

    def _require_ready(self, connection: sqlite3.Connection) -> None:
        try:
            verify_authority_schema_installation(connection, require_ready=True)
        except (AuthorityMigrationError, sqlite3.Error, KeyError, TypeError) as exc:
            raise AuthorityRepositoryNotReady(str(exc)) from exc

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        if not self.write_shadow:
            raise AuthorityWriteShadowDisabled(
                "AUTHORITY_SCHEMA_V2_WRITE_SHADOW is disabled"
            )
        connection = _connect(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._require_ready(connection)
            production_foundation = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='authority_production_schema_state'"
            ).fetchone()
            if production_foundation is not None:
                raise AuthorityWriteShadowDisabled(
                    "shadow repository writes are disabled after production foundation installation"
                )
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def persist_command_bundle(
        self,
        *,
        workflow_id: str,
        idempotency_key: str,
        command: CommandEnvelopeV1,
        event: EventEnvelopeV1,
        receipt: ReceiptEnvelopeV1,
        outbox: OutboxMessageV1,
    ) -> AuthorityCommitResult:
        validate_command_envelope_structure(command)
        validate_event_envelope(event)
        validate_receipt_envelope(receipt)
        validate_outbox_message(outbox)
        workflow_key = _text(workflow_id, "workflow_id")
        idempotency = _text(idempotency_key, "idempotency_key")
        request_sha256 = command_envelope_sha256(command)
        event_sha256 = event_envelope_sha256(event)
        receipt_sha256 = receipt_envelope_sha256(receipt)
        outbox_sha256 = outbox_message_sha256(outbox)
        with self._write_transaction() as connection:
            existing = _idempotency_record(
                connection, "workflow", workflow_key, idempotency
            )
            if existing is not None:
                if existing["request_schema"] != AUTHORITY_IDEMPOTENCY_REQUEST_SCHEMA:
                    raise AuthorityEnvelopePersistenceError(
                        "idempotency request schema differs from the supported contract"
                    )
                if existing["request_sha256"] != request_sha256:
                    raise AuthorityIdempotencyConflict(
                        "idempotency key is already bound to different command bytes"
                    )
                row = connection.execute(
                    """
                    SELECT c.command_id, e.event_id, r.receipt_id, o.message_id,
                           i.committed_revision,
                           c.envelope_sha256 AS command_sha256,
                           e.envelope_sha256 AS event_sha256,
                           r.envelope_sha256 AS receipt_sha256,
                           o.envelope_sha256 AS outbox_sha256
                    FROM authority_idempotency_records i
                    JOIN authority_commands c ON c.command_id=i.command_id
                    JOIN authority_receipts r ON r.receipt_id=i.receipt_id
                    JOIN authority_events e ON e.event_id=r.event_id
                    JOIN authority_outbox o ON o.event_id=e.event_id
                    WHERE i.scope_kind='workflow' AND i.scope_id=?
                      AND i.idempotency_key=?
                    """,
                    (workflow_key, idempotency),
                ).fetchone()
                if row is None:
                    raise AuthorityEnvelopePersistenceError(
                        "idempotency record lacks its immutable envelope bundle"
                    )
                expected_bundle_identity = (
                    request_sha256,
                    event_sha256,
                    receipt_sha256,
                    outbox_sha256,
                )
                recorded_bundle_identity = (
                    row["command_sha256"],
                    row["event_sha256"],
                    row["receipt_sha256"],
                    row["outbox_sha256"],
                )
                if expected_bundle_identity != recorded_bundle_identity:
                    raise AuthorityEnvelopePersistenceError(
                        "idempotency replay companion envelope identity differs"
                    )
                return AuthorityCommitResult(
                    workflow_key,
                    int(row["committed_revision"]),
                    str(row["command_id"]),
                    str(row["event_id"]),
                    str(row["receipt_id"]),
                    str(row["message_id"]),
                    request_sha256,
                    True,
                )

            workflow = _workflow(connection, workflow_key)
            if workflow["current_revision_availability"] != "RECORDED":
                raise AuthorityRevisionConflict("workflow current revision is legacy_unknown")
            current_revision = workflow["current_revision"]
            if type(current_revision) is not int:
                raise AuthorityRevisionConflict("workflow current revision is unavailable")
            if command.project_binding.project_id != workflow["project_id"]:
                raise AuthorityEnvelopePersistenceError("command project differs from workflow")
            if command.project_binding.project_generation != workflow["project_generation"]:
                raise AuthorityEnvelopePersistenceError("command project generation differs")
            if command.project_binding.project_revision != current_revision:
                raise AuthorityRevisionConflict(
                    f"expected workflow revision {current_revision}, got "
                    f"{command.project_binding.project_revision}"
                )
            expected_run = (
                workflow["runtime_generation"],
                workflow["scheduler_generation"],
                workflow["run_generation"],
            )
            actual_run = (
                command.run_binding.runtime_generation,
                command.run_binding.scheduler_generation,
                command.run_binding.run_generation,
            )
            if actual_run != expected_run:
                raise AuthorityEnvelopePersistenceError("command run generations differ")

            revision = _allocate_revision(connection, workflow_key)
            if revision != current_revision + 1:
                raise AuthorityRevisionConflict(
                    "workflow revision allocator is not the next monotonic revision"
                )
            pin_sha256 = stable_authority_sha256(command.contract_pins)
            if (
                event.workflow_id != workflow_key
                or event.project_id != command.project_binding.project_id
                or event.command_id != command.command_id
                or event.revision != revision
                or event.project_generation != command.project_binding.project_generation
                or event.run_generation != command.run_binding.run_generation
                or event.runtime_generation != command.run_binding.runtime_generation
                or event.scheduler_generation != command.run_binding.scheduler_generation
                or event.contract_pin_set_sha256 != pin_sha256
            ):
                raise AuthorityEnvelopePersistenceError("event envelope binding differs")
            if (
                receipt.workflow_id != workflow_key
                or receipt.project_id != event.project_id
                or receipt.command_id != command.command_id
                or receipt.event_id != event.event_id
                or receipt.revision != revision
                or receipt.contract_pin_set_sha256 != pin_sha256
            ):
                raise AuthorityEnvelopePersistenceError("receipt envelope binding differs")
            if (
                outbox.workflow_id != workflow_key
                or outbox.event_id != event.event_id
                or outbox.revision != revision
            ):
                raise AuthorityEnvelopePersistenceError("outbox envelope binding differs")

            _persist_contract_pins(connection, command, revision)
            _persist_command_envelope(
                connection,
                command,
                workflow_id=workflow_key,
                persisted_revision=revision,
                idempotency_key=idempotency,
            )
            _persist_event_envelope(connection, event)
            _persist_receipt_envelope(connection, receipt)
            _enqueue_outbox(connection, outbox)
            _commit_idempotency(
                connection,
                scope_kind="workflow",
                scope_id=workflow_key,
                idempotency_key=idempotency,
                request_sha256=request_sha256,
                command_id=command.command_id,
                receipt_id=receipt.receipt_id,
                revision=revision,
            )
            _update_workflow_revision(
                connection,
                workflow_id=workflow_key,
                current_revision=current_revision,
                revision=revision,
                pin_sha256=pin_sha256,
            )
            return AuthorityCommitResult(
                workflow_key,
                revision,
                command.command_id,
                event.event_id,
                receipt.receipt_id,
                outbox.message_id,
                request_sha256,
                False,
            )

    def persist_project_snapshot(
        self,
        *,
        snapshot_id: str,
        workflow_id: str,
        snapshot: ProjectSnapshotV0,
    ) -> str:
        checked = validate_project_snapshot_v0(snapshot)
        identifier = _text(snapshot_id, "snapshot_id")
        workflow_key = _text(workflow_id, "workflow_id")
        value = project_snapshot_v0_semantic_bytes(checked)
        digest = project_snapshot_v0_semantic_sha256(checked)
        with self._write_transaction() as connection:
            workflow = _workflow(connection, workflow_key)
            if checked.coordinate.project_id != workflow["project_id"]:
                raise AuthorityEnvelopePersistenceError("snapshot project differs from workflow")
            if (
                workflow["current_revision_availability"] != "RECORDED"
                or type(workflow["current_revision"]) is not int
            ):
                raise AuthorityRevisionConflict("workflow current revision is unavailable")
            if checked.coordinate.project_revision != workflow["current_revision"]:
                raise AuthorityRevisionConflict(
                    "snapshot project revision differs from the current workflow revision"
                )
            expected_project_generation = (
                None
                if workflow["project_generation"] == "legacy_unknown"
                else workflow["project_generation"]
            )
            expected_run_generation = (
                None
                if workflow["run_generation"] == "legacy_unknown"
                else workflow["run_generation"]
            )
            if (
                checked.coordinate.project_generation != expected_project_generation
                or checked.coordinate.run_generation != expected_run_generation
                or checked.coordinate.runtime_generation != workflow["runtime_generation"]
                or checked.coordinate.scheduler_generation != workflow["scheduler_generation"]
            ):
                raise AuthorityEnvelopePersistenceError(
                    "snapshot generation coordinate differs from workflow"
                )
            pin_sha256 = checked.coordinate.recorded_contract_pin_set_sha256
            expected_pin = (
                workflow["contract_pin_set_sha256"]
                if workflow["contract_pin_availability"] == "RECORDED"
                else None
            )
            if pin_sha256 != expected_pin:
                raise AuthorityEnvelopePersistenceError(
                    "snapshot contract pin coordinate differs from workflow"
                )
            connection.execute(
                """
                INSERT INTO authority_project_snapshots(
                    snapshot_id, workflow_id, project_id, project_revision,
                    completeness, contract_pin_set_sha256, snapshot_schema,
                    snapshot_json, snapshot_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    workflow_key,
                    checked.coordinate.project_id,
                    checked.coordinate.project_revision,
                    checked.completeness.value,
                    pin_sha256,
                    checked.schema_version,
                    value.decode("utf-8", errors="strict"),
                    digest,
                ),
            )
        return digest

    def table_count(self, table: str) -> int:
        if table not in {
            "authority_contract_pin_sets",
            "authority_commands",
            "authority_events",
            "authority_receipts",
            "authority_idempotency_records",
            "authority_outbox",
            "authority_checkpoint_ledger",
            "authority_project_snapshots",
        }:
            raise AuthorityRepositoryError("unsupported authority table count")
        connection = _connect(self.path)
        try:
            self._require_ready(connection)
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            connection.close()
