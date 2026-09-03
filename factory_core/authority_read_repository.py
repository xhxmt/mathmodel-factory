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
from .authority_operations import (
    AuthorityOperationError,
    _identifier,
    _nonnegative,
    _sha,
)
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
    artifact_occurrence_from_dict,
    phase3_mutation_from_dict,
    validate_artifact_occurrence,
)
from . import authority_production_writer as _production_writer


class AuthorityReadError(RuntimeError):
    """Raised when a supported read cannot prove its immutable identity."""


AUTHORITY_PHASE3_ARTIFACT_STATE_SCHEMA = "authority-read-phase3-artifact-state-v1"


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
class AuthorityRevisionCommandIdentity:
    """Hash-only identity of the exact command bundle at one revision."""

    workflow_id: str
    revision: int
    command_id: str
    message_id: str
    command_sha256: str
    event_sha256: str
    receipt_sha256: str
    outbox_sha256: str
    bundle_sha256: str
    phase3_mutation_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "authority-read-revision-command-identity-v1",
            "workflow_id": self.workflow_id,
            "revision": self.revision,
            "command_id": self.command_id,
            "message_id": self.message_id,
            "command_sha256": self.command_sha256,
            "event_sha256": self.event_sha256,
            "receipt_sha256": self.receipt_sha256,
            "outbox_sha256": self.outbox_sha256,
            "bundle_sha256": self.bundle_sha256,
            "phase3_mutation_sha256": self.phase3_mutation_sha256,
        }

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True)
class AuthorityCurrentRunGeneration:
    """Current atomic Phase-9 generation and its immutable creation receipt."""

    workflow_id: str
    project_id: str
    project_revision: int
    project_generation: str
    run_generation: str
    runtime_generation: str
    scheduler_generation: str
    predecessor_run_generation: str | None
    predecessor_creation_receipt_sha256: str | None
    operation_kind: str
    run_mode: str
    delivery_capability: str
    source_commit: str
    source_tree: str
    source_parent: str
    contract_pin_set_sha256: str
    official_input_manifest_sha256: str
    official_input_raw_bytes_set_sha256: str
    execution_context_receipt_sha256: str
    operator_authorization_receipt_sha256: str
    request_sha256: str
    creation_receipt_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "authority-current-run-generation-v1",
            **{
                name: getattr(self, name)
                for name in self.__dataclass_fields__
            },
        }

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


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

    def as_dict(self) -> dict[str, object]:
        validate_authority_phase3_artifact_state(self)
        result = _authority_phase3_artifact_state_identity(self)
        result["state_sha256"] = canonical_sha256(result)
        return result

    @property
    def state_sha256(self) -> str:
        validate_authority_phase3_artifact_state(self)
        return canonical_sha256(_authority_phase3_artifact_state_identity(self))

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


def _authority_phase3_artifact_state_identity(
    state: AuthorityPhase3ArtifactState,
) -> dict[str, object]:
    return {
        "schema": AUTHORITY_PHASE3_ARTIFACT_STATE_SCHEMA,
        "workflow_id": state.workflow_id,
        "through_revision": state.through_revision,
        "occurrences": [item.as_dict() for item in state.occurrences],
    }


def validate_authority_phase3_artifact_state(
    state: AuthorityPhase3ArtifactState,
) -> AuthorityPhase3ArtifactState:
    """Validate one canonical latest-occurrence projection at a read boundary."""

    if type(state) is not AuthorityPhase3ArtifactState:
        raise AuthorityReadError(
            "Phase-3 artifact state must be AuthorityPhase3ArtifactState"
        )
    try:
        workflow_id = _identifier(state.workflow_id, "workflow_id")
        through_revision = _nonnegative(
            state.through_revision,
            "through_revision",
        )
    except AuthorityOperationError as exc:
        raise AuthorityReadError(str(exc)) from exc
    if type(state.occurrences) is not tuple:
        raise AuthorityReadError("Phase-3 artifact state occurrences must be a tuple")
    paths: list[str] = []
    for occurrence in state.occurrences:
        try:
            validate_artifact_occurrence(occurrence)
        except Phase3ContractError as exc:
            raise AuthorityReadError(
                "Phase-3 artifact state occurrence does not revalidate"
            ) from exc
        if occurrence.workflow_id != workflow_id:
            raise AuthorityReadError(
                "Phase-3 artifact state occurrence workflow differs"
            )
        if occurrence.revision > through_revision:
            raise AuthorityReadError(
                "Phase-3 artifact state occurrence exceeds read boundary"
            )
        paths.append(occurrence.normalized_path)
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise AuthorityReadError(
            "Phase-3 artifact state paths must be unique and sorted"
        )
    return state


def authority_phase3_artifact_state_from_dict(
    value: object,
) -> AuthorityPhase3ArtifactState:
    """Parse and hash-check one exact JSON-safe Phase-3 artifact state wire."""

    expected = {
        "schema",
        "workflow_id",
        "through_revision",
        "occurrences",
        "state_sha256",
    }
    if (
        type(value) is not dict
        or set(value) != expected
        or value.get("schema") != AUTHORITY_PHASE3_ARTIFACT_STATE_SCHEMA
    ):
        raise AuthorityReadError("Phase-3 artifact state fields or schema differ")
    if type(value["occurrences"]) is not list:
        raise AuthorityReadError(
            "Phase-3 artifact state occurrences must be a JSON array"
        )
    try:
        occurrences = tuple(
            artifact_occurrence_from_dict(item) for item in value["occurrences"]
        )
        supplied_sha256 = _sha(value["state_sha256"], "state_sha256")
    except (AuthorityOperationError, Phase3ContractError) as exc:
        raise AuthorityReadError(
            "Phase-3 artifact state wire does not revalidate"
        ) from exc
    state = validate_authority_phase3_artifact_state(
        AuthorityPhase3ArtifactState(
            value["workflow_id"],
            value["through_revision"],
            occurrences,
        )
    )
    if state.state_sha256 != supplied_sha256:
        raise AuthorityReadError("Phase-3 artifact state SHA-256 differs")
    return state


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
class AuthorityTrustedPhase3SourceSnapshot:
    """One transaction's complete current Phase-3 trusted-source facts."""

    coordinate: AuthorityWorkflowCoordinate
    artifact_state: AuthorityPhase3ArtifactState
    selected_occurrence: ArtifactLedgerOccurrence
    revision_command: AuthorityRevisionCommandIdentity
    revision_snapshot: AuthorityRevisionSnapshot
    predecessor_event_sha256: str | None
    run_generation: AuthorityCurrentRunGeneration


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

    __slots__ = ("_path", "_expected_source_fence", "_deadline")

    def __init__(
        self,
        database: str | Path,
        *,
        expected_source_fence_sha256: str,
        deadline: object | None = None,
    ) -> None:
        self._path = authority_database_path(database)
        self._expected_source_fence = _sha(
            expected_source_fence_sha256, "expected_source_fence_sha256"
        )
        self._deadline = deadline

    def _remaining_timeout(self) -> float:
        if self._deadline is None:
            return 2.0
        check = getattr(self._deadline, "check", None)
        remaining = getattr(self._deadline, "remaining_seconds", None)
        if not callable(check) or not callable(remaining):
            raise AuthorityReadError(
                "Authority read deadline must expose check() and remaining_seconds()"
            )
        check("authority_read_before")
        value = remaining()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value <= 0
        ):
            check("authority_read_exhausted")
            raise AuthorityReadError("Authority read deadline has no remaining budget")
        return min(2.0, float(value))

    @contextmanager
    def _snapshot(self) -> Iterator[sqlite3.Connection]:
        connection = connect_authority_ro(
            self._path, timeout_seconds=self._remaining_timeout()
        )
        try:
            connection.execute("BEGIN")
            verify_production_installation(connection, require_ready=True)
            if legacy_source_identity_sha256(connection) != self._expected_source_fence:
                raise AuthorityReadError("read source fence differs")
            yield connection
            if self._deadline is not None:
                self._deadline.check("authority_read_after")
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

    def _current_run_generation(
        self,
        connection: sqlite3.Connection,
        coordinate: AuthorityWorkflowCoordinate,
    ) -> AuthorityCurrentRunGeneration:
        rows = connection.execute(
            """
            SELECT g.*,c.creation_receipt_sha256,
                   r.receipt_json,r.receipt_sha256,
                   s.succession_json,s.succession_sha256
            FROM authority_production_run_generation_current c
            JOIN authority_production_run_generations g
              ON g.run_generation=c.run_generation
             AND g.workflow_id=c.workflow_id
            JOIN authority_production_run_generation_creation_receipts r
              ON r.run_generation=g.run_generation
             AND r.workflow_id=g.workflow_id
             AND r.receipt_sha256=c.creation_receipt_sha256
            JOIN authority_production_run_generation_successions s
              ON s.run_generation=g.run_generation
             AND s.workflow_id=g.workflow_id
            WHERE c.workflow_id=?
            """,
            (coordinate.workflow_id,),
        ).fetchall()
        if len(rows) != 1:
            raise AuthorityReadError(
                "workflow does not have exactly one atomic current run generation"
            )
        row = rows[0]
        receipt_bytes = _canonical_envelope(
            row["receipt_json"], row["receipt_sha256"], "run-generation receipt"
        )
        succession_bytes = _canonical_envelope(
            row["succession_json"],
            row["succession_sha256"],
            "run-generation succession",
        )
        try:
            receipt = json.loads(receipt_bytes)
            succession = json.loads(succession_bytes)
        except (TypeError, ValueError) as exc:
            raise AuthorityReadError(
                "run-generation receipt JSON is malformed"
            ) from exc
        request = receipt.get("request") if type(receipt) is dict else None
        if type(request) is not dict:
            raise AuthorityReadError("run-generation receipt request is unavailable")
        request_keys = {
            "schema_version", "idempotency_key", "operation_kind",
            "project_id", "workflow_id", "project_revision",
            "project_generation", "runtime_generation",
            "scheduler_generation", "predecessor_run_generation",
            "predecessor_creation_receipt_sha256",
            "predecessor_terminal_receipt_sha256", "run_mode",
            "modeling_consultation_contract", "delivery_capability", "source",
            "source_inventory_sha256", "contract_pins", "official_inputs",
            "execution_context", "operator_authorization", "occurred_at",
        }
        receipt_keys = {
            "schema", "run_generation", "workflow_id", "operation_kind",
            "request_sha256", "request", "official_input_manifest_sha256",
            "official_input_raw_bytes_set_sha256",
            "execution_context_receipt_sha256",
            "operator_authorization_receipt_sha256",
            "operator_authorization_consumption_sha256",
            "authorization_target_sha256", "source_inventory_sha256",
            "occurred_at",
        }
        comparisons = {
            "workflow_id": coordinate.workflow_id,
            "project_id": coordinate.project_id,
            "project_revision": row["project_revision"],
            "project_generation": coordinate.project_generation,
            "runtime_generation": coordinate.runtime_generation,
            "scheduler_generation": coordinate.scheduler_generation,
            "predecessor_run_generation": row["predecessor_run_generation"],
            "predecessor_creation_receipt_sha256": row[
                "predecessor_creation_receipt_sha256"
            ],
            "predecessor_terminal_receipt_sha256": row[
                "predecessor_terminal_receipt_sha256"
            ],
            "operation_kind": row["operation_kind"],
            "run_mode": row["run_mode"],
            "modeling_consultation_contract": row[
                "modeling_consultation_contract"
            ],
            "delivery_capability": "DISABLED",
        }
        source = request.get("source")
        official_inputs = request.get("official_inputs")
        official_files = (
            official_inputs.get("files")
            if type(official_inputs) is dict
            else None
        )
        execution_context = request.get("execution_context")
        authorization = request.get("operator_authorization")
        request_without_authorization = dict(request)
        request_without_authorization.pop("operator_authorization", None)
        authorization_target = {
            "schema": "authority-phase9-run-generation-authorization-target-v2",
            "derived_run_generation": row["run_generation"],
            "intent": {
                "schema": "authority-phase9-run-generation-intent-v1",
                "request": request_without_authorization,
            },
        }
        authorization_target_sha256 = canonical_sha256(authorization_target)
        authorization_receipt_sha256 = (
            canonical_sha256(authorization)
            if type(authorization) is dict
            else None
        )
        authorization_consumption = {
            "schema": "authority-phase9-run-generation-authorization-consumption-v1",
            "authorization_id": (
                authorization.get("authorization_id")
                if type(authorization) is dict
                else None
            ),
            "authorization_receipt_sha256": authorization_receipt_sha256,
            "authorization_target_sha256": authorization_target_sha256,
            "request_sha256": row["request_sha256"],
            "run_generation": row["run_generation"],
            "workflow_id": coordinate.workflow_id,
            "consumed_at": request.get("occurred_at"),
        }
        raw_bytes_set = (
            {
                "schema": "authority-phase9-official-input-raw-bytes-set-v1",
                "files": [
                    {
                        "logical_path": item.get("logical_path"),
                        "byte_length": item.get("byte_length"),
                        "raw_bytes_sha256": item.get("raw_bytes_sha256"),
                    }
                    for item in official_files
                ],
            }
            if type(official_files) is list
            and all(type(item) is dict for item in official_files)
            else None
        )
        if (
            set(request) != request_keys
            or set(receipt) != receipt_keys
            or request.get("schema_version")
            != "authority-phase9-run-generation-request-v2"
            or receipt.get("schema")
            != "authority-phase9-run-generation-creation-receipt-v2"
            or row["run_generation"] != coordinate.run_generation
            or row["project_id"] != coordinate.project_id
            or row["project_generation"] != coordinate.project_generation
            or row["runtime_generation"] != coordinate.runtime_generation
            or row["scheduler_generation"] != coordinate.scheduler_generation
            or row["contract_pin_set_sha256"]
            != coordinate.contract_pin_set_sha256
            or canonical_sha256(request.get("contract_pins"))
            != row["contract_pin_set_sha256"]
            or row["delivery_capability"] != "DISABLED"
            or row["run_mode"] != "FORENSIC_REPLAY"
            or row["modeling_consultation_contract"]
            != "LEGACY_NOT_APPLICABLE"
            or any(request.get(name) != expected for name, expected in comparisons.items())
            or type(source) is not dict
            or source.get("source_commit") != row["source_commit"]
            or source.get("source_tree") != row["source_tree"]
            or source.get("source_parent") != row["source_parent"]
            or request.get("source_inventory_sha256")
            != row["source_inventory_sha256"]
            or type(official_inputs) is not dict
            or set(official_inputs) != {"schema_version", "input_generation", "files"}
            or official_inputs.get("schema_version")
            != "authority-phase9-official-input-manifest-evidence-v1"
            or type(official_files) is not list
            or not official_files
            or any(
                type(item) is not dict
                or set(item)
                != {"schema_version", "logical_path", "byte_length", "raw_bytes_sha256"}
                or item.get("schema_version")
                != "authority-phase9-official-input-file-evidence-v1"
                for item in official_files
            )
            or canonical_sha256(official_inputs)
            != row["official_input_manifest_sha256"]
            or raw_bytes_set is None
            or canonical_sha256(raw_bytes_set)
            != row["official_input_raw_bytes_set_sha256"]
            or type(execution_context) is not dict
            or canonical_sha256(execution_context)
            != row["execution_context_receipt_sha256"]
            or type(authorization) is not dict
            or authorization.get("schema_version")
            != "authority-phase9-operator-authorization-evidence-v2"
            or authorization.get("authorization_id") != row["authorization_id"]
            or authorization.get("authorized") is not True
            or authorization.get("operation_kind") != row["operation_kind"]
            or authorization.get("project_id") != coordinate.project_id
            or authorization.get("workflow_id") != coordinate.workflow_id
            or authorization.get("source_commit") != row["source_commit"]
            or authorization.get("authorized_request_sha256")
            != authorization_target_sha256
            or authorization_receipt_sha256
            != row["operator_authorization_receipt_sha256"]
            or authorization_target_sha256 != row["authorization_target_sha256"]
            or receipt.get("run_generation") != row["run_generation"]
            or receipt.get("workflow_id") != coordinate.workflow_id
            or receipt.get("operation_kind") != row["operation_kind"]
            or receipt.get("request_sha256") != row["request_sha256"]
            or receipt.get("official_input_manifest_sha256")
            != row["official_input_manifest_sha256"]
            or receipt.get("official_input_raw_bytes_set_sha256")
            != row["official_input_raw_bytes_set_sha256"]
            or receipt.get("execution_context_receipt_sha256")
            != row["execution_context_receipt_sha256"]
            or receipt.get("operator_authorization_receipt_sha256")
            != row["operator_authorization_receipt_sha256"]
            or receipt.get("operator_authorization_consumption_sha256")
            != canonical_sha256(authorization_consumption)
            or receipt.get("authorization_target_sha256")
            != row["authorization_target_sha256"]
            or receipt.get("source_inventory_sha256")
            != row["source_inventory_sha256"]
            or receipt.get("occurred_at") != request.get("occurred_at")
            or canonical_sha256(request) != row["request_sha256"]
            or succession
            != {
                "schema": "authority-phase9-run-generation-succession-v1",
                "workflow_id": coordinate.workflow_id,
                "run_generation": row["run_generation"],
                "predecessor_run_generation": row["predecessor_run_generation"],
                "predecessor_creation_receipt_sha256": row[
                    "predecessor_creation_receipt_sha256"
                ],
                "predecessor_terminal_receipt_sha256": row[
                    "predecessor_terminal_receipt_sha256"
                ],
                "request_sha256": row["request_sha256"],
            }
        ):
            raise AuthorityReadError(
                "current run-generation coordinate or receipt differs"
            )
        for field in (
            "contract_pin_set_sha256", "official_input_manifest_sha256",
            "official_input_raw_bytes_set_sha256",
            "execution_context_receipt_sha256",
            "operator_authorization_receipt_sha256", "request_sha256",
            "creation_receipt_sha256", "source_inventory_sha256",
            "authorization_target_sha256",
        ):
            _sha(row[field], field)
        for field in ("source_commit", "source_tree", "source_parent"):
            value = row[field]
            if (
                type(value) is not str
                or len(value) != 40
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise AuthorityReadError(f"{field} is not a concrete Git object ID")
        return AuthorityCurrentRunGeneration(
            coordinate.workflow_id,
            coordinate.project_id,
            int(row["project_revision"]),
            coordinate.project_generation,
            coordinate.run_generation,
            coordinate.runtime_generation,
            coordinate.scheduler_generation,
            row["predecessor_run_generation"],
            row["predecessor_creation_receipt_sha256"],
            str(row["operation_kind"]),
            str(row["run_mode"]),
            "DISABLED",
            str(row["source_commit"]),
            str(row["source_tree"]),
            str(row["source_parent"]),
            str(row["contract_pin_set_sha256"]),
            str(row["official_input_manifest_sha256"]),
            str(row["official_input_raw_bytes_set_sha256"]),
            str(row["execution_context_receipt_sha256"]),
            str(row["operator_authorization_receipt_sha256"]),
            str(row["request_sha256"]),
            str(row["creation_receipt_sha256"]),
        )

    def current_run_generation(
        self, workflow_id: str
    ) -> AuthorityCurrentRunGeneration:
        with self._snapshot() as connection:
            coordinate = self._coordinate(connection, workflow_id)
            return self._current_run_generation(connection, coordinate)

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

    def revision_command_identity(
        self,
        workflow_id: str,
        revision: int,
    ) -> AuthorityRevisionCommandIdentity:
        """Read and fully revalidate the Phase-3 command at one revision."""

        key = str(_identifier(workflow_id, "workflow_id"))
        boundary = _nonnegative(revision, "revision")
        if boundary < 1:
            raise AuthorityReadError("revision must be positive")
        with self._snapshot() as connection:
            coordinate = self._coordinate(connection, key)
            if boundary > coordinate.current_revision:
                raise AuthorityReadError("revision exceeds the current head")
            rows = connection.execute(
                """
                SELECT c.command_id,c.envelope_json AS command_json,
                       c.envelope_sha256 AS command_sha256,
                       e.envelope_json AS event_json,e.envelope_sha256 AS event_sha256,
                       r.envelope_json AS receipt_json,r.envelope_sha256 AS receipt_sha256,
                       o.message_id,o.envelope_json AS outbox_json,
                       o.envelope_sha256 AS outbox_sha256,
                       pc.bundle_sha256,i.request_schema,i.request_sha256
                FROM authority_commands c
                JOIN authority_events e ON e.command_id=c.command_id
                JOIN authority_receipts r ON r.event_id=e.event_id
                JOIN authority_outbox o ON o.event_id=e.event_id
                JOIN authority_production_command_commits pc
                  ON pc.command_id=c.command_id
                JOIN authority_idempotency_records i
                  ON i.command_id=c.command_id
                 AND i.scope_kind='workflow' AND i.scope_id=c.workflow_id
                WHERE c.workflow_id=? AND c.persisted_revision=? AND e.revision=?
                """,
                (key, boundary, boundary),
            ).fetchall()
            if len(rows) != 1:
                raise AuthorityReadError(
                    "revision command companion cardinality differs"
                )
            row = rows[0]
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
            del command_bytes, event_bytes, receipt_bytes, outbox_bytes
            phase3 = _phase3_mutation_at_revision(
                connection,
                workflow_id=key,
                revision=boundary,
                command_id=str(row["command_id"]),
            )
            if phase3 is None:
                raise AuthorityReadError(
                    "revision command has no complete Phase-3 mutation"
                )
            try:
                continuity = _production_writer._validated_phase3_bundles_through(
                    connection,
                    workflow_id=key,
                    through_revision=boundary,
                )
            except AuthorityEnvelopePersistenceError as exc:
                raise AuthorityReadError(str(exc)) from exc
            if (
                not continuity
                or continuity[-1].revision != boundary
                or continuity[-1].command_id != row["command_id"]
                or continuity[-1].mutation != phase3.mutation
            ):
                raise AuthorityReadError("Phase-3 revision continuity differs")
            request_sha256 = _production_writer._phase3_request_sha256(
                str(row["command_sha256"]), phase3.mutation.mutation_sha256
            )
            bundle_sha256 = _production_writer._phase3_bundle_sha256(
                str(row["command_sha256"]),
                str(row["event_sha256"]),
                str(row["receipt_sha256"]),
                str(row["outbox_sha256"]),
                phase3.mutation.mutation_sha256,
            )
            if (
                row["request_schema"]
                != _production_writer.AUTHORITY_PHASE3_IDEMPOTENCY_REQUEST_SCHEMA
                or row["request_sha256"] != request_sha256
                or row["bundle_sha256"] != bundle_sha256
            ):
                raise AuthorityReadError(
                    "revision command request or bundle identity differs"
                )
            return AuthorityRevisionCommandIdentity(
                key,
                boundary,
                str(row["command_id"]),
                str(row["message_id"]),
                str(row["command_sha256"]),
                str(row["event_sha256"]),
                str(row["receipt_sha256"]),
                str(row["outbox_sha256"]),
                bundle_sha256,
                phase3.mutation.mutation_sha256,
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
            return validate_authority_phase3_artifact_state(
                AuthorityPhase3ArtifactState(
                    coordinate.workflow_id,
                    boundary,
                    tuple(latest[path] for path in sorted(latest)),
                )
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

    def trusted_phase3_source_snapshot(
        self,
        *,
        workflow_id: str,
        occurrence_id: str,
    ) -> AuthorityTrustedPhase3SourceSnapshot:
        """Read coordinate, complete P3 graph and selected command atomically."""

        key = str(_identifier(workflow_id, "workflow_id"))
        selected_id = str(_identifier(occurrence_id, "occurrence_id"))
        with self._snapshot() as connection:
            coordinate = self._coordinate(connection, key)
            run_generation = self._current_run_generation(connection, coordinate)
            boundary = coordinate.current_revision
            try:
                bundles = _production_writer._validated_phase3_bundles_through(
                    connection,
                    workflow_id=key,
                    through_revision=boundary,
                )
            except AuthorityEnvelopePersistenceError as exc:
                raise AuthorityReadError(str(exc)) from exc
            latest: dict[str, ArtifactLedgerOccurrence] = {}
            for bundle in bundles:
                for occurrence in bundle.artifact_occurrences:
                    latest[occurrence.normalized_path] = occurrence
            artifact_state = validate_authority_phase3_artifact_state(
                AuthorityPhase3ArtifactState(
                    key,
                    boundary,
                    tuple(latest[path] for path in sorted(latest)),
                )
            )
            selected = tuple(
                occurrence
                for occurrence in artifact_state.occurrences
                if occurrence.occurrence_id == selected_id
            )
            if len(selected) != 1:
                raise AuthorityReadError(
                    "selected occurrence is not the exact current path head"
                )
            selected_occurrence = selected[0]
            revision = selected_occurrence.revision
            if revision < 1 or revision > boundary:
                raise AuthorityReadError("selected occurrence revision is invalid")

            rows = connection.execute(
                """
                SELECT c.command_id,c.envelope_json AS command_json,
                       c.envelope_sha256 AS command_sha256,
                       e.envelope_json AS event_json,e.envelope_sha256 AS event_sha256,
                       r.envelope_json AS receipt_json,r.envelope_sha256 AS receipt_sha256,
                       o.message_id,o.envelope_json AS outbox_json,
                       o.envelope_sha256 AS outbox_sha256,
                       pc.bundle_sha256,i.request_schema,i.request_sha256
                FROM authority_commands c
                JOIN authority_events e ON e.command_id=c.command_id
                JOIN authority_receipts r ON r.event_id=e.event_id
                JOIN authority_outbox o ON o.event_id=e.event_id
                JOIN authority_production_command_commits pc
                  ON pc.command_id=c.command_id
                JOIN authority_idempotency_records i
                  ON i.command_id=c.command_id
                 AND i.scope_kind='workflow' AND i.scope_id=c.workflow_id
                WHERE c.workflow_id=? AND c.persisted_revision=? AND e.revision=?
                """,
                (key, revision, revision),
            ).fetchall()
            if len(rows) != 1:
                raise AuthorityReadError(
                    "selected revision command companion cardinality differs"
                )
            row = rows[0]
            _canonical_envelope(
                row["command_json"], row["command_sha256"], "command"
            )
            _canonical_envelope(row["event_json"], row["event_sha256"], "event")
            _canonical_envelope(
                row["receipt_json"], row["receipt_sha256"], "receipt"
            )
            _canonical_envelope(
                row["outbox_json"], row["outbox_sha256"], "outbox"
            )
            phase3 = _phase3_mutation_at_revision(
                connection,
                workflow_id=key,
                revision=revision,
                command_id=str(row["command_id"]),
            )
            if phase3 is None:
                raise AuthorityReadError(
                    "selected revision command has no complete Phase-3 mutation"
                )
            matching_bundles = tuple(
                bundle
                for bundle in bundles
                if bundle.revision == revision
                and bundle.command_id == row["command_id"]
            )
            if (
                len(matching_bundles) != 1
                or matching_bundles[0].mutation != phase3.mutation
            ):
                raise AuthorityReadError("selected Phase-3 continuity differs")
            request_sha256 = _production_writer._phase3_request_sha256(
                str(row["command_sha256"]), phase3.mutation.mutation_sha256
            )
            bundle_sha256 = _production_writer._phase3_bundle_sha256(
                str(row["command_sha256"]),
                str(row["event_sha256"]),
                str(row["receipt_sha256"]),
                str(row["outbox_sha256"]),
                phase3.mutation.mutation_sha256,
            )
            if (
                row["request_schema"]
                != _production_writer.AUTHORITY_PHASE3_IDEMPOTENCY_REQUEST_SCHEMA
                or row["request_sha256"] != request_sha256
                or row["bundle_sha256"] != bundle_sha256
            ):
                raise AuthorityReadError(
                    "selected revision request or bundle identity differs"
                )
            command = AuthorityRevisionCommandIdentity(
                key,
                revision,
                str(row["command_id"]),
                str(row["message_id"]),
                str(row["command_sha256"]),
                str(row["event_sha256"]),
                str(row["receipt_sha256"]),
                str(row["outbox_sha256"]),
                bundle_sha256,
                phase3.mutation.mutation_sha256,
            )

            event_rows = connection.execute(
                """
                SELECT event_id,workflow_id,revision,command_id,event_type,
                       envelope_json,envelope_sha256
                FROM authority_events
                WHERE workflow_id=? AND revision>=1 AND revision<=?
                ORDER BY revision
                """,
                (key, boundary),
            ).fetchall()
            event_revisions = tuple(int(item["revision"]) for item in event_rows)
            if (
                not event_revisions
                or event_revisions[-1] != boundary
                or event_revisions
                != tuple(range(event_revisions[0], boundary + 1))
                or any(
                    bundle.revision not in event_revisions for bundle in bundles
                )
            ):
                raise AuthorityReadError(
                    "Authority revision graph is partial or non-contiguous"
                )
            events: list[AuthorityEventRecord] = []
            for event_row in event_rows:
                raw = _canonical_envelope(
                    event_row["envelope_json"],
                    event_row["envelope_sha256"],
                    "event",
                )
                events.append(
                    AuthorityEventRecord(
                        str(event_row["event_id"]),
                        str(event_row["workflow_id"]),
                        int(event_row["revision"]),
                        str(event_row["command_id"]),
                        str(event_row["event_type"]),
                        raw,
                        str(event_row["envelope_sha256"]),
                    )
                )
            revision_snapshot = AuthorityRevisionSnapshot(
                coordinate,
                boundary,
                tuple(events),
            )
            selected_events = tuple(
                event
                for event in events
                if event.revision == revision
                and event.command_id == selected_occurrence.command_id
                and event.envelope_sha256 == command.event_sha256
            )
            if len(selected_events) != 1:
                raise AuthorityReadError(
                    "selected occurrence command is absent from revision graph"
                )
            predecessor = None
            if revision > 1:
                predecessors = tuple(
                    event.envelope_sha256
                    for event in events
                    if event.revision == revision - 1
                )
                if len(predecessors) > 1:
                    raise AuthorityReadError(
                        "selected occurrence predecessor is ambiguous"
                    )
                if predecessors:
                    predecessor = predecessors[0]
            return AuthorityTrustedPhase3SourceSnapshot(
                coordinate,
                artifact_state,
                selected_occurrence,
                command,
                revision_snapshot,
                predecessor,
                run_generation,
            )

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
