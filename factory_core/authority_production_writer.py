"""The only production-capable Authority command writer facade.

The facade is not imported by any active application entrypoint.  It has one
mutation: a complete command/event/receipt/outbox commit with revision,
idempotency, writer, switch, source, and delivery-state fences in the same
``BEGIN IMMEDIATE`` transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3

from .authority_envelopes import (
    EventEnvelopeV1,
    OutboxMessageV1,
    ReceiptEnvelopeV1,
    event_envelope_sha256,
    outbox_message_sha256,
    receipt_envelope_sha256,
    stable_authority_sha256,
    validate_event_envelope,
    validate_outbox_message,
    validate_receipt_envelope,
)
from .authority_operations import (
    AUTHORITY_PRIMARY,
    CANARY,
    CONTROL_RECEIPT_SCHEMA,
    PHASE3_OWNER_OPERATOR_GRANT_SET_SCHEMA,
    _identifier,
    _nonnegative,
    _sha,
)
from .authority_production_schema import (
    authority_database_path,
    connect_authority_rw,
    legacy_source_identity_sha256,
    verify_production_installation,
)
from .authority_repository import (
    AUTHORITY_IDEMPOTENCY_REQUEST_SCHEMA,
    AuthorityEnvelopePersistenceError,
    AuthorityIdempotencyConflict,
    AuthorityRevisionConflict,
    _allocate_revision,
    _commit_idempotency,
    _enqueue_outbox,
    _idempotency_record,
    _persist_command_envelope,
    _persist_contract_pins,
    _persist_event_envelope,
    _persist_receipt_envelope,
    _text,
    _update_workflow_revision,
    _workflow,
)
from .canonical import canonical_bytes, canonical_sha256
from .command_envelope import (
    CommandEnvelopeV1,
    command_envelope_sha256,
    validate_command_envelope_structure,
)
from .phase3_artifacts import (
    ARTIFACT_OWNER_OPERATOR_ISSUER_KIND,
    ArtifactBlockerCode,
    ArtifactLedgerOccurrence,
    ArtifactOccurrenceKind,
    ArtifactOwnerOperatorAuthorization,
    ArtifactReadExpectation,
    ArtifactRegistrationError,
    CheckpointLedgerOccurrence,
    CheckpointState,
    CheckpointTransition,
    Phase3ContractError,
    Phase3Mutation,
    Phase3PreviousHeadKind,
    artifact_occurrence_from_dict,
    artifact_owner_operator_claim_from_authorization,
    artifact_owner_operator_claim_from_dict,
    build_artifact_occurrence,
    build_checkpoint_occurrence,
    checkpoint_occurrence_from_dict,
    owner_compilation_from_binding,
    phase3_mutation_from_dict,
    register_artifact_owner,
    validate_phase3_mutation,
)


AUTHORITY_PHASE3_IDEMPOTENCY_REQUEST_SCHEMA = (
    "authority-command-phase3-mutation-request-v2"
)
AUTHORITY_PRODUCTION_BUNDLE_SCHEMA_V1 = "authority-production-command-bundle-v1"
AUTHORITY_PRODUCTION_BUNDLE_SCHEMA_V2 = "authority-production-command-bundle-v2"
AUTHORITY_PHASE3_ARTIFACT_ROW_SCHEMA = "authority-phase3-artifact-row-v3"
AUTHORITY_PHASE3_CHECKPOINT_ROW_SCHEMA = "authority-phase3-checkpoint-row-v3"
AUTHORITY_PHASE3_REOPEN_ROW_SCHEMA = "authority-phase3-reopen-row-v3"


class AuthorityProductionWriterError(RuntimeError):
    """Base error for the fenced production writer."""


class AuthorityProductionWriterDisabled(AuthorityProductionWriterError):
    """Raised while the persisted switch remains V1_ONLY or writer-disabled."""


class AuthorityProductionWriterFenceLost(AuthorityProductionWriterError):
    """Raised when durable writer identity or epoch no longer matches."""


class AuthorityProductionWriterBusy(AuthorityProductionWriterError):
    """Raised when another SQLite writer owns the database transaction."""


@dataclass(frozen=True)
class _PersistedPhase3MutationBundle:
    workflow_id: str
    revision: int
    command_id: str
    mutation: Phase3Mutation
    artifact_occurrences: tuple[ArtifactLedgerOccurrence, ...]
    checkpoint_occurrences: tuple[CheckpointLedgerOccurrence, ...]


@dataclass(frozen=True)
class AuthorityProductionCommitResult:
    workflow_id: str
    revision: int
    command_id: str
    event_id: str
    receipt_id: str
    message_id: str
    request_sha256: str
    bundle_sha256: str
    committed_writer_id: str
    committed_writer_epoch: int
    committed_switch_epoch: int
    committed_switch_mode: str
    replayed: bool
    request_schema: str = AUTHORITY_IDEMPOTENCY_REQUEST_SCHEMA
    bundle_schema: str = AUTHORITY_PRODUCTION_BUNDLE_SCHEMA_V1
    phase3_mutation_sha256: str | None = None

    def as_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": "authority-production-commit-result-v1",
            "workflow_id": self.workflow_id,
            "revision": self.revision,
            "command_id": self.command_id,
            "event_id": self.event_id,
            "receipt_id": self.receipt_id,
            "message_id": self.message_id,
            "request_sha256": self.request_sha256,
            "bundle_sha256": self.bundle_sha256,
            "committed_writer_id": self.committed_writer_id,
            "committed_writer_epoch": self.committed_writer_epoch,
            "committed_switch_epoch": self.committed_switch_epoch,
            "committed_switch_mode": self.committed_switch_mode,
            "replayed": self.replayed,
        }
        if self.phase3_mutation_sha256 is not None:
            value["schema"] = "authority-production-commit-result-v2"
            value["request_schema"] = self.request_schema
            value["bundle_schema"] = self.bundle_schema
            value["phase3_mutation_sha256"] = self.phase3_mutation_sha256
        return value


def _writer_failure_point(_stage: str) -> None:
    """Test seam; production code never installs an injector."""


def _bundle_sha256(
    command_sha256: str,
    event_sha256: str,
    receipt_sha256: str,
    outbox_sha256: str,
) -> str:
    return canonical_sha256(
        {
            "schema": AUTHORITY_PRODUCTION_BUNDLE_SCHEMA_V1,
            "command_sha256": command_sha256,
            "event_sha256": event_sha256,
            "receipt_sha256": receipt_sha256,
            "outbox_sha256": outbox_sha256,
        }
    )


def _phase3_request_sha256(
    command_sha256: str, phase3_mutation_sha256: str
) -> str:
    return canonical_sha256(
        {
            "schema": AUTHORITY_PHASE3_IDEMPOTENCY_REQUEST_SCHEMA,
            "command_sha256": command_sha256,
            "phase3_mutation_sha256": phase3_mutation_sha256,
        }
    )


def _phase3_bundle_sha256(
    command_sha256: str,
    event_sha256: str,
    receipt_sha256: str,
    outbox_sha256: str,
    phase3_mutation_sha256: str,
) -> str:
    return canonical_sha256(
        {
            "schema": AUTHORITY_PRODUCTION_BUNDLE_SCHEMA_V2,
            "command_sha256": command_sha256,
            "event_sha256": event_sha256,
            "receipt_sha256": receipt_sha256,
            "outbox_sha256": outbox_sha256,
            "phase3_mutation_sha256": phase3_mutation_sha256,
        }
    )


def _phase3_row_json(value: dict[str, object]) -> str:
    return canonical_bytes(value).decode("utf-8", errors="strict")


def _decode_phase3_row(
    raw_value: object, *, schema: str, field: str
) -> dict[str, object]:
    if type(raw_value) is not str:
        raise AuthorityEnvelopePersistenceError(f"{field} is not canonical JSON")
    try:
        raw = raw_value.encode("utf-8", errors="strict")
        value = json.loads(raw_value)
    except (UnicodeEncodeError, json.JSONDecodeError) as exc:
        raise AuthorityEnvelopePersistenceError(
            f"{field} is not canonical JSON"
        ) from exc
    if type(value) is not dict or canonical_bytes(value) != raw:
        raise AuthorityEnvelopePersistenceError(f"{field} is not canonical JSON")
    if value.get("schema") != schema:
        raise AuthorityEnvelopePersistenceError(f"{field} schema differs")
    return value


def _artifact_from_persisted_row(row: sqlite3.Row):
    value = _decode_phase3_row(
        row["metadata_json"],
        schema=AUTHORITY_PHASE3_ARTIFACT_ROW_SCHEMA,
        field="Phase-3 Artifact Record metadata",
    )
    if set(value) != {"schema", "occurrence", "phase3_mutation"}:
        raise AuthorityEnvelopePersistenceError(
            "Phase-3 Artifact occurrence metadata fields differ"
        )
    try:
        occurrence = artifact_occurrence_from_dict(value["occurrence"])
        mutation = phase3_mutation_from_dict(value["phase3_mutation"])
    except Phase3ContractError as exc:
        raise AuthorityEnvelopePersistenceError(str(exc)) from exc
    if occurrence.mutation_sha256 != mutation.mutation_sha256:
        raise AuthorityEnvelopePersistenceError(
            "persisted Phase-3 Artifact occurrence mutation differs"
        )
    if occurrence.kind is ArtifactOccurrenceKind.RECORD:
        assert occurrence.artifact_record is not None
        expected_columns = (
            occurrence.artifact_record.artifact_type,
            occurrence.artifact_record.content_sha256,
            occurrence.artifact_record.availability.value,
            occurrence.artifact_record.registration.owner_id,
        )
        if occurrence.artifact_record not in mutation.artifact_records:
            raise AuthorityEnvelopePersistenceError(
                "persisted Phase-3 Artifact Record is absent from mutation"
            )
    elif occurrence.kind is ArtifactOccurrenceKind.BLOCKER:
        assert occurrence.blocker is not None
        assert mutation.current_manifest is not None
        expected_columns = (
            "PHASE3_BLOCKER",
            None,
            "ERROR",
            f"phase3:policy:{mutation.current_manifest.owner_compilation_sha256}",
        )
        if occurrence.blocker not in mutation.artifact_blockers:
            raise AuthorityEnvelopePersistenceError(
                "persisted Phase-3 blocker is absent from mutation"
            )
    else:
        assert occurrence.removal is not None
        expected_columns = (
            "PHASE3_REMOVAL_TOMBSTONE",
            occurrence.removal.previous_record_sha256,
            "ERROR",
            occurrence.removal.owner_id,
        )
        if occurrence.removal not in mutation.removals:
            raise AuthorityEnvelopePersistenceError(
                "persisted Phase-3 removal is absent from mutation"
            )
    if (
        row["artifact_record_id"] != occurrence.occurrence_id
        or row["workflow_id"] != occurrence.workflow_id
        or row["recorded_revision"] != occurrence.revision
        or row["artifact_path"] != occurrence.normalized_path
        or (
            row["artifact_type"],
            row["content_sha256"],
            row["availability"],
            row["owner_scope"],
        )
        != expected_columns
    ):
        raise AuthorityEnvelopePersistenceError(
            "persisted Phase-3 Artifact occurrence columns differ"
        )
    return occurrence, mutation


def _checkpoint_from_persisted_row(row: sqlite3.Row):
    value = _decode_phase3_row(
        row["payload_json"],
        schema=AUTHORITY_PHASE3_CHECKPOINT_ROW_SCHEMA,
        field="Phase-3 checkpoint payload",
    )
    if set(value) != {"schema", "occurrence", "phase3_mutation"}:
        raise AuthorityEnvelopePersistenceError(
            "Phase-3 checkpoint payload fields differ"
        )
    try:
        occurrence = checkpoint_occurrence_from_dict(value["occurrence"])
        mutation = phase3_mutation_from_dict(value["phase3_mutation"])
    except Phase3ContractError as exc:
        raise AuthorityEnvelopePersistenceError(str(exc)) from exc
    entry = occurrence.checkpoint_entry
    expected_assurance = (
        "VERIFIED" if entry.state is CheckpointState.VALID else "RECORDED"
    )
    if (
        occurrence.mutation_sha256 != mutation.mutation_sha256
        or entry not in mutation.checkpoint_entries
        or row["checkpoint_id"] != occurrence.occurrence_id
        or row["workflow_id"] != occurrence.workflow_id
        or row["recorded_revision"] != occurrence.revision
        or row["checkpoint_kind"] != "RECORDED"
        or row["checkpoint_key"] != entry.checkpoint_key
        or row["assurance"] != expected_assurance
        or row["owner_stage"] != entry.owner_stage
        or row["owner_resolution"] != "RECORDED_OWNER"
        or row["source_record_key"]
        != f"phase3:checkpoint-occurrence:{occurrence.occurrence_id}"
    ):
        raise AuthorityEnvelopePersistenceError(
            "persisted Phase-3 checkpoint columns differ"
        )
    return occurrence, mutation


def _phase3_control_fields(
    mutation: Phase3Mutation,
) -> tuple[str, int, str, str, tuple[ArtifactReadExpectation, ...]]:
    if mutation.reopen_plan is not None:
        return (
            mutation.reopen_plan.reopen_plan_id,
            mutation.reopen_plan.source_revision,
            mutation.reopen_plan.target_scope,
            mutation.reopen_plan.reason_code,
            mutation.reopen_plan.read_set,
        )
    if mutation.blocked_disposition is not None:
        return (
            mutation.blocked_disposition.disposition_id,
            mutation.blocked_disposition.source_revision,
            mutation.blocked_disposition.target_scope,
            mutation.blocked_disposition.reason_code,
            mutation.blocked_disposition.read_set,
        )
    raise AuthorityEnvelopePersistenceError(
        "complete Phase-3 mutation requires a reopen-ledger control record"
    )


_CONTROL_PROJECTION_FIELDS = {
    "switch_mode",
    "switch_epoch",
    "writer_id",
    "writer_epoch",
    "writer_enabled",
    "consumer_id",
    "consumer_epoch",
    "consumer_enabled",
}


def _verify_artifact_owner_authorization_receipt(
    connection: sqlite3.Connection,
    authorization: ArtifactOwnerOperatorAuthorization,
) -> None:
    try:
        claim = artifact_owner_operator_claim_from_authorization(authorization)
    except Phase3ContractError as exc:
        raise AuthorityEnvelopePersistenceError(str(exc)) from exc
    row = connection.execute(
        "SELECT * FROM authority_production_control_receipts WHERE receipt_id=?",
        (authorization.issuer_receipt_id,),
    ).fetchone()
    if row is None:
        raise AuthorityEnvelopePersistenceError(
            "Phase-3 owner authorization issuer receipt is unavailable"
        )
    try:
        body = json.loads(row["receipt_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise AuthorityEnvelopePersistenceError(
            "Phase-3 owner authorization issuer receipt is malformed"
        ) from exc
    if type(body) is not dict or set(body) != {
        "schema",
        "receipt_kind",
        "prior",
        "next",
        "operator_subject",
        "reason",
        "occurred_at",
        "evidence",
    }:
        raise AuthorityEnvelopePersistenceError(
            "Phase-3 owner authorization issuer receipt fields differ"
        )
    prior = body["prior"]
    next_state = body["next"]
    if (
        type(prior) is not dict
        or type(next_state) is not dict
        or set(prior) != _CONTROL_PROJECTION_FIELDS
        or set(next_state) != _CONTROL_PROJECTION_FIELDS
    ):
        raise AuthorityEnvelopePersistenceError(
            "Phase-3 owner authorization control projection differs"
        )
    for projection in (prior, next_state):
        if (
            type(projection["switch_mode"]) is not str
            or type(projection["switch_epoch"]) is not int
            or type(projection["writer_epoch"]) is not int
            or type(projection["writer_enabled"]) is not bool
            or type(projection["consumer_epoch"]) is not int
            or type(projection["consumer_enabled"]) is not bool
            or (
                projection["writer_id"] is not None
                and type(projection["writer_id"]) is not str
            )
            or (
                projection["consumer_id"] is not None
                and type(projection["consumer_id"]) is not str
            )
        ):
            raise AuthorityEnvelopePersistenceError(
                "Phase-3 owner authorization control projection is malformed"
            )
    receipt_sha256 = canonical_sha256(body)
    if (
        body["schema"] != CONTROL_RECEIPT_SCHEMA
        or body["receipt_kind"] != "WRITER_CONFIG"
        or row["receipt_kind"] != "WRITER_CONFIG"
        or row["receipt_id"] != f"control:{receipt_sha256[:32]}"
        or row["receipt_sha256"] != receipt_sha256
        or row["receipt_sha256"] != authorization.issuer_receipt_sha256
        or row["receipt_json"] != canonical_bytes(body).decode("utf-8")
        or row["operator_subject"] != body["operator_subject"]
        or row["reason"] != body["reason"]
        or row["occurred_at"] != body["occurred_at"]
        or row["prior_switch_epoch"] != prior["switch_epoch"]
        or row["next_switch_epoch"] != next_state["switch_epoch"]
        or row["prior_writer_epoch"] != prior["writer_epoch"]
        or row["next_writer_epoch"] != next_state["writer_epoch"]
    ):
        raise AuthorityEnvelopePersistenceError(
            "Phase-3 owner authorization issuer receipt identity differs"
        )
    if (
        authorization.issuer_kind != ARTIFACT_OWNER_OPERATOR_ISSUER_KIND
        or authorization.operator_subject != body["operator_subject"]
        or authorization.issuer_writer_id != next_state["writer_id"]
        or authorization.issuer_writer_epoch != next_state["writer_epoch"]
        or next_state["switch_mode"] != "V1_ONLY"
        or next_state["writer_enabled"] is not True
        or prior["writer_epoch"] + 1 != next_state["writer_epoch"]
    ):
        raise AuthorityEnvelopePersistenceError(
            "Phase-3 owner authorization issuer coordinate differs"
        )
    evidence = body["evidence"]
    if type(evidence) is not dict or set(evidence) != {
        "schema",
        "claims",
        "grant_set_sha256",
    }:
        raise AuthorityEnvelopePersistenceError(
            "Phase-3 owner authorization grant evidence differs"
        )
    claims_value = evidence["claims"]
    if (
        evidence["schema"] != PHASE3_OWNER_OPERATOR_GRANT_SET_SCHEMA
        or type(claims_value) is not list
        or evidence["grant_set_sha256"]
        != canonical_sha256(
            {
                "schema": PHASE3_OWNER_OPERATOR_GRANT_SET_SCHEMA,
                "claims": claims_value,
            }
        )
    ):
        raise AuthorityEnvelopePersistenceError(
            "Phase-3 owner authorization grant-set identity differs"
        )
    try:
        claims = tuple(
            artifact_owner_operator_claim_from_dict(item) for item in claims_value
        )
    except Phase3ContractError as exc:
        raise AuthorityEnvelopePersistenceError(str(exc)) from exc
    if (
        tuple(sorted(claims, key=lambda item: item.claim_sha256)) != claims
        or len({item.claim_sha256 for item in claims}) != len(claims)
        or sum(item == claim for item in claims) != 1
    ):
        raise AuthorityEnvelopePersistenceError(
            "Phase-3 owner authorization grant is absent or ambiguous"
        )

    compilation = owner_compilation_from_binding(
        authorization.owner_compilation
    )
    try:
        register_artifact_owner(compilation, authorization.normalized_path)
    except ArtifactRegistrationError:
        return
    raise AuthorityEnvelopePersistenceError(
        "Phase-3 owner path resolves without operator authorization"
    )


def _verify_phase3_owner_authorizations(
    connection: sqlite3.Connection,
    mutation: Phase3Mutation,
    *,
    workflow_id: str,
    source_revision: int,
    command_id: str,
    writer_id: str,
    writer_epoch: int,
) -> None:
    if mutation.current_manifest is None:
        raise AuthorityEnvelopePersistenceError(
            "complete Phase-3 mutation graph is unavailable"
        )
    policy_sha256 = mutation.current_manifest.owner_compilation_sha256
    for blocker in mutation.artifact_blockers:
        authorization = blocker.operator_authorization
        if authorization is None:
            continue
        if blocker.code is not ArtifactBlockerCode.OWNER_RESOLUTION_BLOCKED:
            raise AuthorityEnvelopePersistenceError(
                "Phase-3 operator authorization is attached to a non-owner blocker"
            )
        if (
            authorization.workflow_id != workflow_id
            or authorization.source_revision != source_revision
            or authorization.command_id != command_id
            or authorization.normalized_path != blocker.normalized_path
            or authorization.owner_compilation_sha256 != policy_sha256
            or authorization.issuer_writer_id != writer_id
            or authorization.issuer_writer_epoch != writer_epoch
        ):
            raise AuthorityRevisionConflict(
                "Phase-3 owner authorization coordinate differs"
            )
        _verify_artifact_owner_authorization_receipt(connection, authorization)


def _phase3_bundle_at_revision(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    revision: int,
    command_id: str | None = None,
) -> _PersistedPhase3MutationBundle | None:
    artifact_rows = connection.execute(
        "SELECT * FROM authority_artifact_records "
        "WHERE workflow_id=? AND recorded_revision=? ORDER BY artifact_record_id",
        (workflow_id, revision),
    ).fetchall()
    checkpoint_rows = connection.execute(
        "SELECT * FROM authority_checkpoint_ledger "
        "WHERE workflow_id=? AND recorded_revision=? AND checkpoint_kind='RECORDED' "
        "ORDER BY checkpoint_id",
        (workflow_id, revision),
    ).fetchall()
    reopen_rows = connection.execute(
        "SELECT * FROM authority_reopen_plans "
        "WHERE workflow_id=? AND recorded_revision=? ORDER BY reopen_plan_id",
        (workflow_id, revision),
    ).fetchall()
    if not artifact_rows and not checkpoint_rows and not reopen_rows:
        return None
    if not artifact_rows or not checkpoint_rows or len(reopen_rows) != 1:
        raise AuthorityEnvelopePersistenceError("Phase-3 ledger bundle is incomplete")
    artifact_occurrences: list[ArtifactLedgerOccurrence] = []
    checkpoint_occurrences: list[CheckpointLedgerOccurrence] = []
    stored_mutations: list[Phase3Mutation] = []
    command_ids: set[str] = set()
    for row in artifact_rows:
        occurrence, mutation = _artifact_from_persisted_row(row)
        artifact_occurrences.append(occurrence)
        stored_mutations.append(mutation)
        command_ids.add(occurrence.command_id)
    for row in checkpoint_rows:
        occurrence, mutation = _checkpoint_from_persisted_row(row)
        checkpoint_occurrences.append(occurrence)
        stored_mutations.append(mutation)
        command_ids.add(occurrence.command_id)
    reopen_value = _decode_phase3_row(
        reopen_rows[0]["evidence_json"],
        schema=AUTHORITY_PHASE3_REOPEN_ROW_SCHEMA,
        field="Phase-3 ReopenPlan evidence",
    )
    if set(reopen_value) != {
        "schema",
        "command_id",
        "phase3_mutation_sha256",
        "phase3_mutation",
    }:
        raise AuthorityEnvelopePersistenceError("Phase-3 ReopenPlan fields differ")
    try:
        mutation = phase3_mutation_from_dict(reopen_value["phase3_mutation"])
    except Phase3ContractError as exc:
        raise AuthorityEnvelopePersistenceError(str(exc)) from exc
    stored_mutations.append(mutation)
    command_ids.add(str(_identifier(reopen_value["command_id"], "command_id")))
    if len(command_ids) != 1:
        raise AuthorityEnvelopePersistenceError("Phase-3 ledger command identity differs")
    actual_command_id = next(iter(command_ids))
    if command_id is not None and actual_command_id != command_id:
        raise AuthorityEnvelopePersistenceError("Phase-3 ledger command identity differs")
    if (
        reopen_value["phase3_mutation_sha256"] != mutation.mutation_sha256
        or any(item != mutation for item in stored_mutations)
    ):
        raise AuthorityEnvelopePersistenceError("Phase-3 mutation identity differs")
    control_id, source_revision, target_scope, reason_code, _read_set = (
        _phase3_control_fields(mutation)
    )
    if (
        reopen_rows[0]["reopen_plan_id"] != control_id
        or reopen_rows[0]["source_revision"] != source_revision
        or reopen_rows[0]["target_scope"] != target_scope
        or reopen_rows[0]["reason_code"] != reason_code
    ):
        raise AuthorityEnvelopePersistenceError("Phase-3 ReopenPlan columns differ")
    if len(artifact_occurrences) != (
        len(mutation.artifact_records)
        + len(mutation.artifact_blockers)
        + len(mutation.removals)
    ) or len(checkpoint_occurrences) != len(mutation.checkpoint_entries):
        raise AuthorityEnvelopePersistenceError("Phase-3 ledger typed cardinality differs")
    return _PersistedPhase3MutationBundle(
        workflow_id=workflow_id,
        revision=revision,
        command_id=actual_command_id,
        mutation=mutation,
        artifact_occurrences=tuple(
            sorted(artifact_occurrences, key=lambda item: item.occurrence_id)
        ),
        checkpoint_occurrences=tuple(
            sorted(checkpoint_occurrences, key=lambda item: item.occurrence_id)
        ),
    )


def _phase3_revisions_through(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    through_revision: int,
) -> tuple[int, ...]:
    rows = connection.execute(
        """
        SELECT recorded_revision FROM authority_artifact_records
        WHERE workflow_id=? AND recorded_revision IS NOT NULL AND recorded_revision<=?
        UNION
        SELECT recorded_revision FROM authority_checkpoint_ledger
        WHERE workflow_id=? AND recorded_revision IS NOT NULL AND recorded_revision<=?
          AND checkpoint_kind='RECORDED'
        UNION
        SELECT recorded_revision FROM authority_reopen_plans
        WHERE workflow_id=? AND recorded_revision IS NOT NULL AND recorded_revision<=?
        ORDER BY recorded_revision
        """,
        (
            workflow_id,
            through_revision,
            workflow_id,
            through_revision,
            workflow_id,
            through_revision,
        ),
    ).fetchall()
    return tuple(int(row[0]) for row in rows)


def _validated_phase3_bundles_through(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    through_revision: int,
) -> tuple[_PersistedPhase3MutationBundle, ...]:
    bundles: list[_PersistedPhase3MutationBundle] = []
    previous: _PersistedPhase3MutationBundle | None = None
    for revision in _phase3_revisions_through(
        connection,
        workflow_id=workflow_id,
        through_revision=through_revision,
    ):
        bundle = _phase3_bundle_at_revision(
            connection,
            workflow_id=workflow_id,
            revision=revision,
        )
        assert bundle is not None
        mutation = bundle.mutation
        if (
            mutation.previous_manifest is None
            or mutation.current_manifest is None
            or mutation.change_set is None
            or mutation.previous_head is None
        ):
            raise AuthorityEnvelopePersistenceError(
                "persisted Phase-3 bundle lacks its complete graph"
            )
        head = mutation.previous_head
        if head.workflow_id != workflow_id:
            raise AuthorityEnvelopePersistenceError(
                "Phase-3 previous head workflow differs"
            )
        if previous is None:
            if head.kind is not Phase3PreviousHeadKind.BOOTSTRAP:
                raise AuthorityEnvelopePersistenceError(
                    "Phase-3 previous head bootstrap differs"
                )
        else:
            if head.kind is not Phase3PreviousHeadKind.CONTINUATION:
                raise AuthorityEnvelopePersistenceError(
                    "Phase-3 previous head continuity differs"
                )
            if (
                mutation.previous_manifest != previous.mutation.current_manifest
                or head.previous_revision != previous.revision
                or head.previous_command_id != previous.command_id
                or head.previous_mutation_sha256
                != previous.mutation.mutation_sha256
                or head.previous_manifest_sha256
                != previous.mutation.current_manifest.manifest_sha256
            ):
                raise AuthorityEnvelopePersistenceError(
                    "Phase-3 previous head continuity differs"
                )
        commit = connection.execute(
            "SELECT workflow_id, revision, writer_id, writer_epoch "
            "FROM authority_production_command_commits WHERE command_id=?",
            (bundle.command_id,),
        ).fetchone()
        if (
            commit is None
            or commit["workflow_id"] != workflow_id
            or commit["revision"] != revision
        ):
            raise AuthorityEnvelopePersistenceError(
                "Phase-3 owner authorization commit coordinate differs"
            )
        _verify_phase3_owner_authorizations(
            connection,
            mutation,
            workflow_id=workflow_id,
            source_revision=head.source_revision,
            command_id=bundle.command_id,
            writer_id=str(commit["writer_id"]),
            writer_epoch=int(commit["writer_epoch"]),
        )
        previous = bundle
        bundles.append(bundle)
    return tuple(bundles)


def _latest_phase3_bundle_before_source(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    source_revision: int,
) -> _PersistedPhase3MutationBundle | None:
    bundles = _validated_phase3_bundles_through(
        connection,
        workflow_id=workflow_id,
        through_revision=source_revision,
    )
    return bundles[-1] if bundles else None


def _latest_phase3_occurrence(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    normalized_path: str,
    through_revision: int,
) -> ArtifactLedgerOccurrence | None:
    row = connection.execute(
        """
        SELECT * FROM authority_artifact_records
        WHERE workflow_id=? AND artifact_path=?
          AND recorded_revision IS NOT NULL AND recorded_revision<=?
        ORDER BY recorded_revision DESC, artifact_record_id DESC
        LIMIT 1
        """,
        (workflow_id, normalized_path, through_revision),
    ).fetchone()
    if row is None:
        return None
    occurrence, _mutation = _artifact_from_persisted_row(row)
    return occurrence


def _owner_policy_sha256_for_occurrence(
    occurrence: ArtifactLedgerOccurrence,
) -> str:
    if occurrence.kind is ArtifactOccurrenceKind.RECORD:
        assert occurrence.artifact_record is not None
        return occurrence.artifact_record.registration.owner_compilation_sha256
    if occurrence.kind is ArtifactOccurrenceKind.BLOCKER:
        assert occurrence.blocker is not None
        if occurrence.blocker.registration is not None:
            return occurrence.blocker.registration.owner_compilation_sha256
        if occurrence.blocker.operator_authorization is not None:
            return occurrence.blocker.operator_authorization.owner_compilation_sha256
        raise AuthorityEnvelopePersistenceError(
            "Phase-3 blocker requires a typed owner binding"
        )
    assert occurrence.removal is not None
    return occurrence.removal.owner_compilation_sha256


def _verify_phase3_previous_head(
    connection: sqlite3.Connection,
    mutation: Phase3Mutation,
    *,
    workflow_id: str,
    source_revision: int,
) -> None:
    if mutation.previous_manifest is None or mutation.previous_head is None:
        raise AuthorityEnvelopePersistenceError(
            "complete Phase-3 mutation graph is unavailable"
        )
    latest = _latest_phase3_bundle_before_source(
        connection,
        workflow_id=workflow_id,
        source_revision=source_revision,
    )
    head = mutation.previous_head
    if latest is None:
        if head.kind is not Phase3PreviousHeadKind.BOOTSTRAP:
            raise AuthorityRevisionConflict("Phase-3 previous head bootstrap differs")
        return
    if head.kind is not Phase3PreviousHeadKind.CONTINUATION:
        raise AuthorityRevisionConflict("Phase-3 previous head root reset differs")
    if mutation.previous_manifest != latest.mutation.current_manifest:
        raise AuthorityRevisionConflict("Phase-3 previous manifest head differs")
    if (
        head.previous_revision != latest.revision
        or head.previous_command_id != latest.command_id
        or head.previous_mutation_sha256 != latest.mutation.mutation_sha256
        or head.previous_manifest_sha256
        != latest.mutation.current_manifest.manifest_sha256
    ):
        raise AuthorityRevisionConflict("Phase-3 previous head continuity differs")


def _require_complete_phase3_mutation(
    mutation: Phase3Mutation,
    *,
    workflow_id: str,
    source_revision: int,
) -> Phase3Mutation:
    try:
        checked = validate_phase3_mutation(mutation)
    except Phase3ContractError as exc:
        raise AuthorityEnvelopePersistenceError(str(exc)) from exc
    if (
        checked.previous_manifest is None
        or checked.current_manifest is None
        or checked.change_set is None
        or not checked.checkpoint_entries
        or checked.previous_head is None
        or not (
            checked.artifact_records
            or checked.artifact_blockers
            or checked.removals
        )
    ):
        raise AuthorityEnvelopePersistenceError(
            "persisted Phase-3 mutation must atomically bind the complete graph "
            "and all three ledger tables"
        )
    _control_id, control_source_revision, _target_scope, _reason_code, _read_set = (
        _phase3_control_fields(checked)
    )
    if (
        checked.previous_head.workflow_id != workflow_id
        or checked.previous_head.source_revision != source_revision
        or control_source_revision != source_revision
    ):
        raise AuthorityRevisionConflict(
            "Phase-3 control source coordinate differs"
        )
    return checked


# Kept as a private compatibility alias while the single public writer method
# remains the only mutation surface.
def _require_phase3_mutation(
    mutation: Phase3Mutation,
    *,
    workflow_id: str,
    source_revision: int,
) -> Phase3Mutation:
    return _require_complete_phase3_mutation(
        mutation,
        workflow_id=workflow_id,
        source_revision=source_revision,
    )


def _verify_phase3_read_set(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    through_revision: int,
    read_set: tuple[ArtifactReadExpectation, ...],
) -> None:
    for expectation in read_set:
        occurrence = _latest_phase3_occurrence(
            connection,
            workflow_id=workflow_id,
            normalized_path=expectation.normalized_path,
            through_revision=through_revision,
        )
        expects_record = expectation.expected_artifact_record_id is not None
        expects_blocker = expectation.expected_blocker_code is not None
        if not expects_record and not expects_blocker:
            if occurrence is None:
                if expectation.expected_occurrence_id is not None:
                    raise AuthorityRevisionConflict(
                        "Phase-3 Artifact absence occurrence CAS lost"
                    )
                continue
            if occurrence.kind is not ArtifactOccurrenceKind.REMOVAL:
                raise AuthorityRevisionConflict(
                    "Phase-3 Artifact Record absence CAS lost"
                )
            if (
                expectation.expected_occurrence_id is None
                or occurrence.occurrence_id != expectation.expected_occurrence_id
            ):
                raise AuthorityRevisionConflict(
                    "Phase-3 Artifact absence occurrence CAS lost"
                )
            continue
        if occurrence is None or expectation.expected_occurrence_id is None:
            raise AuthorityRevisionConflict("Phase-3 Artifact Record CAS lost")
        if occurrence.occurrence_id != expectation.expected_occurrence_id:
            raise AuthorityRevisionConflict(
                "Phase-3 Artifact occurrence CAS lost"
            )
        if expects_record:
            record = occurrence.artifact_record
            if (
                occurrence.kind is not ArtifactOccurrenceKind.RECORD
                or record is None
                or record.artifact_record_id
                != expectation.expected_artifact_record_id
                or record.record_sha256 != expectation.expected_record_sha256
            ):
                raise AuthorityRevisionConflict(
                    "Phase-3 Artifact Record CAS hash lost"
                )
        elif (
            occurrence.kind is not ArtifactOccurrenceKind.BLOCKER
            or occurrence.blocker is None
            or occurrence.blocker.code.value != expectation.expected_blocker_code
        ):
            raise AuthorityRevisionConflict("Phase-3 Artifact Record CAS hash lost")


def _verify_phase3_checkpoint_predecessors(
    connection: sqlite3.Connection,
    mutation: Phase3Mutation,
    *,
    workflow_id: str,
) -> None:
    for entry in mutation.checkpoint_entries:
        latest = connection.execute(
            """
            SELECT * FROM authority_checkpoint_ledger
            WHERE workflow_id=? AND checkpoint_key=?
            ORDER BY COALESCE(recorded_revision, -1) DESC, rowid DESC
            LIMIT 1
            """,
            (workflow_id, entry.checkpoint_key),
        ).fetchone()
        if entry.previous_checkpoint_id is None:
            if latest is not None:
                raise AuthorityRevisionConflict(
                    "Phase-3 checkpoint initial root reset differs"
                )
            if entry.previous_checkpoint_occurrence_id is not None:
                raise AuthorityRevisionConflict(
                    "Phase-3 initial checkpoint carries an occurrence predecessor"
                )
            continue
        if (
            latest is None
            or latest["workflow_id"] != workflow_id
            or entry.previous_checkpoint_occurrence_id is None
            or latest["checkpoint_id"] != entry.previous_checkpoint_occurrence_id
        ):
            raise AuthorityRevisionConflict("Phase-3 checkpoint predecessor CAS lost")
        previous_occurrence, _mutation = _checkpoint_from_persisted_row(latest)
        previous = previous_occurrence.checkpoint_entry
        if (
            previous.checkpoint_id != entry.previous_checkpoint_id
            or previous.checkpoint_key != entry.checkpoint_key
            or previous.owner_stage != entry.owner_stage
        ):
            raise AuthorityRevisionConflict("Phase-3 checkpoint predecessor differs")
        if entry.transition is CheckpointTransition.INVALIDATED:
            transition_valid = previous.state is CheckpointState.VALID
        elif entry.transition in {
            CheckpointTransition.REATTESTED_VALID,
            CheckpointTransition.REATTESTED_INVALID,
        }:
            transition_valid = previous.state is CheckpointState.INVALID
        else:
            transition_valid = False
        if not transition_valid:
            raise AuthorityRevisionConflict(
                "Phase-3 checkpoint state transition is invalid"
            )


def _verify_phase3_owner_policy(
    connection: sqlite3.Connection,
    mutation: Phase3Mutation,
    *,
    workflow_id: str,
    through_revision: int,
) -> None:
    if mutation.previous_manifest is None or mutation.current_manifest is None:
        raise AuthorityEnvelopePersistenceError(
            "complete Phase-3 mutation graph is unavailable"
        )
    policy_sha256 = mutation.current_manifest.owner_compilation_sha256
    if mutation.previous_manifest.owner_compilation_sha256 != policy_sha256:
        raise AuthorityEnvelopePersistenceError(
            "Phase-3 owner policy migration is required"
        )
    for record in mutation.artifact_records:
        if record.registration.owner_compilation_sha256 != policy_sha256:
            raise AuthorityEnvelopePersistenceError(
                "Phase-3 owner policy migration is required"
            )
    for blocker in mutation.artifact_blockers:
        if blocker.registration is not None:
            blocker_policy_sha256 = blocker.registration.owner_compilation_sha256
        elif blocker.operator_authorization is not None:
            blocker_policy_sha256 = (
                blocker.operator_authorization.owner_compilation_sha256
            )
        else:
            raise AuthorityEnvelopePersistenceError(
                "Phase-3 blocker requires a typed owner binding"
            )
        if blocker_policy_sha256 != policy_sha256:
            raise AuthorityEnvelopePersistenceError(
                "Phase-3 owner policy migration is required"
            )
    for removal in mutation.removals:
        if removal.owner_compilation_sha256 != policy_sha256:
            raise AuthorityEnvelopePersistenceError(
                "Phase-3 owner policy migration is required"
            )
    _control_id, _source_revision, _target_scope, _reason_code, read_set = (
        _phase3_control_fields(mutation)
    )
    for expectation in read_set:
        occurrence = _latest_phase3_occurrence(
            connection,
            workflow_id=workflow_id,
            normalized_path=expectation.normalized_path,
            through_revision=through_revision,
        )
        if occurrence is None:
            if expectation.expected_occurrence_id is not None:
                raise AuthorityRevisionConflict(
                    "Phase-3 Artifact absence occurrence CAS lost"
                )
            continue
        if _owner_policy_sha256_for_occurrence(occurrence) != policy_sha256:
            raise AuthorityEnvelopePersistenceError(
                "Phase-3 owner policy migration is required"
            )


def _persist_phase3_mutation(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    revision: int,
    command_id: str,
    mutation: Phase3Mutation,
) -> None:
    artifact_occurrences = tuple(
        build_artifact_occurrence(
            workflow_id=workflow_id,
            revision=revision,
            command_id=command_id,
            mutation_sha256=mutation.mutation_sha256,
            artifact_record=record,
        )
        for record in mutation.artifact_records
    ) + tuple(
        build_artifact_occurrence(
            workflow_id=workflow_id,
            revision=revision,
            command_id=command_id,
            mutation_sha256=mutation.mutation_sha256,
            blocker=blocker,
        )
        for blocker in mutation.artifact_blockers
    ) + tuple(
        build_artifact_occurrence(
            workflow_id=workflow_id,
            revision=revision,
            command_id=command_id,
            mutation_sha256=mutation.mutation_sha256,
            removal=removal,
        )
        for removal in mutation.removals
    )
    for occurrence in artifact_occurrences:
        if occurrence.kind is ArtifactOccurrenceKind.RECORD:
            assert occurrence.artifact_record is not None
            artifact_type = occurrence.artifact_record.artifact_type
            content_sha256 = occurrence.artifact_record.content_sha256
            availability = occurrence.artifact_record.availability.value
            owner_scope = occurrence.artifact_record.registration.owner_id
        elif occurrence.kind is ArtifactOccurrenceKind.BLOCKER:
            assert mutation.current_manifest is not None
            artifact_type = "PHASE3_BLOCKER"
            content_sha256 = None
            availability = "ERROR"
            owner_scope = (
                f"phase3:policy:{mutation.current_manifest.owner_compilation_sha256}"
            )
        else:
            assert occurrence.removal is not None
            artifact_type = "PHASE3_REMOVAL_TOMBSTONE"
            content_sha256 = occurrence.removal.previous_record_sha256
            availability = "ERROR"
            owner_scope = occurrence.removal.owner_id
        metadata = _phase3_row_json(
            {
                "schema": AUTHORITY_PHASE3_ARTIFACT_ROW_SCHEMA,
                "occurrence": occurrence.as_dict(),
                "phase3_mutation": mutation.as_dict(),
            }
        )
        connection.execute(
            """
            INSERT INTO authority_artifact_records(
                artifact_record_id, workflow_id, artifact_type, artifact_path,
                content_sha256, availability, owner_scope, recorded_revision,
                metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                occurrence.occurrence_id,
                workflow_id,
                artifact_type,
                occurrence.normalized_path,
                content_sha256,
                availability,
                owner_scope,
                revision,
                metadata,
            ),
        )
    _writer_failure_point("after_phase3_artifact_records")
    for entry in mutation.checkpoint_entries:
        occurrence = build_checkpoint_occurrence(
            workflow_id=workflow_id,
            revision=revision,
            command_id=command_id,
            mutation_sha256=mutation.mutation_sha256,
            checkpoint_entry=entry,
        )
        payload = _phase3_row_json(
            {
                "schema": AUTHORITY_PHASE3_CHECKPOINT_ROW_SCHEMA,
                "occurrence": occurrence.as_dict(),
                "phase3_mutation": mutation.as_dict(),
            }
        )
        connection.execute(
            """
            INSERT INTO authority_checkpoint_ledger(
                checkpoint_id, workflow_id, checkpoint_kind, checkpoint_key,
                assurance, owner_stage, owner_resolution, source_record_key,
                payload_json, recorded_revision
            ) VALUES (?, ?, 'RECORDED', ?, ?, ?, 'RECORDED_OWNER', ?, ?, ?)
            """,
            (
                occurrence.occurrence_id,
                workflow_id,
                entry.checkpoint_key,
                "VERIFIED" if entry.state is CheckpointState.VALID else "RECORDED",
                entry.owner_stage,
                f"phase3:checkpoint-occurrence:{occurrence.occurrence_id}",
                payload,
                revision,
            ),
        )
    _writer_failure_point("after_phase3_checkpoint_ledger")
    control_id, source_revision, target_scope, reason_code, _read_set = (
        _phase3_control_fields(mutation)
    )
    evidence = _phase3_row_json(
        {
            "schema": AUTHORITY_PHASE3_REOPEN_ROW_SCHEMA,
            "command_id": command_id,
            "phase3_mutation_sha256": mutation.mutation_sha256,
            "phase3_mutation": mutation.as_dict(),
        }
    )
    connection.execute(
        """
        INSERT INTO authority_reopen_plans(
            reopen_plan_id, workflow_id, source_revision, target_scope,
            reason_code, evidence_json, recorded_revision
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            control_id,
            workflow_id,
            source_revision,
            target_scope,
            reason_code,
            evidence,
            revision,
        ),
    )
    _writer_failure_point("after_phase3_reopen_plan")


def _verify_persisted_phase3_mutation(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    revision: int,
    command_id: str,
    mutation: Phase3Mutation,
) -> None:
    artifact_rows = connection.execute(
        "SELECT * FROM authority_artifact_records WHERE workflow_id=? AND recorded_revision=?",
        (workflow_id, revision),
    ).fetchall()
    checkpoint_rows = connection.execute(
        "SELECT * FROM authority_checkpoint_ledger WHERE workflow_id=? AND recorded_revision=? "
        "AND checkpoint_kind='RECORDED'",
        (workflow_id, revision),
    ).fetchall()
    reopen_rows = connection.execute(
        "SELECT * FROM authority_reopen_plans WHERE workflow_id=? AND recorded_revision=?",
        (workflow_id, revision),
    ).fetchall()
    if (
        len(artifact_rows)
        != (
            len(mutation.artifact_records)
            + len(mutation.artifact_blockers)
            + len(mutation.removals)
        )
        or len(checkpoint_rows) != len(mutation.checkpoint_entries)
        or len(reopen_rows) != 1
    ):
        raise AuthorityEnvelopePersistenceError(
            "idempotency replay Phase-3 ledger cardinality differs"
        )
    expected_artifacts = {
        occurrence.occurrence_id: occurrence
        for occurrence in (
            tuple(
                build_artifact_occurrence(
                    workflow_id=workflow_id,
                    revision=revision,
                    command_id=command_id,
                    mutation_sha256=mutation.mutation_sha256,
                    artifact_record=item,
                )
                for item in mutation.artifact_records
            )
            + tuple(
                build_artifact_occurrence(
                    workflow_id=workflow_id,
                    revision=revision,
                    command_id=command_id,
                    mutation_sha256=mutation.mutation_sha256,
                    blocker=item,
                )
                for item in mutation.artifact_blockers
            )
            + tuple(
                build_artifact_occurrence(
                    workflow_id=workflow_id,
                    revision=revision,
                    command_id=command_id,
                    mutation_sha256=mutation.mutation_sha256,
                    removal=item,
                )
                for item in mutation.removals
            )
        )
    }
    for row in artifact_rows:
        occurrence, stored_mutation = _artifact_from_persisted_row(row)
        if (
            expected_artifacts.get(occurrence.occurrence_id) != occurrence
            or occurrence.command_id != command_id
            or stored_mutation != mutation
        ):
            raise AuthorityEnvelopePersistenceError(
                "idempotency replay Phase-3 Artifact Record differs"
            )
    expected_checkpoints = {
        occurrence.occurrence_id: occurrence
        for occurrence in (
            build_checkpoint_occurrence(
                workflow_id=workflow_id,
                revision=revision,
                command_id=command_id,
                mutation_sha256=mutation.mutation_sha256,
                checkpoint_entry=item,
            )
            for item in mutation.checkpoint_entries
        )
    }
    for row in checkpoint_rows:
        occurrence, stored_mutation = _checkpoint_from_persisted_row(row)
        if (
            expected_checkpoints.get(occurrence.occurrence_id) != occurrence
            or occurrence.command_id != command_id
            or stored_mutation != mutation
        ):
            raise AuthorityEnvelopePersistenceError(
                "idempotency replay Phase-3 checkpoint differs"
            )
    reopen_value = _decode_phase3_row(
        reopen_rows[0]["evidence_json"],
        schema=AUTHORITY_PHASE3_REOPEN_ROW_SCHEMA,
        field="Phase-3 ReopenPlan evidence",
    )
    if set(reopen_value) != {
        "schema",
        "command_id",
        "phase3_mutation_sha256",
        "phase3_mutation",
    }:
        raise AuthorityEnvelopePersistenceError("Phase-3 ReopenPlan fields differ")
    try:
        stored_mutation = phase3_mutation_from_dict(
            reopen_value["phase3_mutation"]
        )
    except Phase3ContractError as exc:
        raise AuthorityEnvelopePersistenceError(str(exc)) from exc
    control_id, source_revision, target_scope, reason_code, _read_set = (
        _phase3_control_fields(mutation)
    )
    if (
        stored_mutation != mutation
        or reopen_value["command_id"] != command_id
        or reopen_value["phase3_mutation_sha256"] != mutation.mutation_sha256
        or reopen_rows[0]["reopen_plan_id"] != control_id
        or reopen_rows[0]["source_revision"] != source_revision
        or reopen_rows[0]["target_scope"] != target_scope
        or reopen_rows[0]["reason_code"] != reason_code
    ):
        raise AuthorityEnvelopePersistenceError(
            "idempotency replay Phase-3 ReopenPlan differs"
        )


class AuthorityProductionWriter:
    """Fenced single-mutation facade; no connection or allocator is exposed."""

    __slots__ = ("_path", "_writer_id", "_writer_epoch", "_expected_source_fence")

    def __init__(
        self,
        database: str | Path,
        *,
        writer_id: str,
        writer_epoch: int,
        expected_source_fence_sha256: str,
    ) -> None:
        self._path = authority_database_path(database)
        self._writer_id = str(_identifier(writer_id, "writer_id"))
        self._writer_epoch = _nonnegative(writer_epoch, "writer_epoch")
        if self._writer_epoch < 1:
            raise AuthorityProductionWriterError("writer_epoch must be positive")
        self._expected_source_fence = _sha(
            expected_source_fence_sha256, "expected_source_fence_sha256"
        )

    def _writer_state(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM authority_production_writer_state WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise AuthorityProductionWriterFenceLost("writer state is missing")
        if row["switch_mode"] not in {CANARY, AUTHORITY_PRIMARY} or not row["writer_enabled"]:
            raise AuthorityProductionWriterDisabled("Authority writer is not active")
        if row["writer_id"] != self._writer_id or row["writer_epoch"] != self._writer_epoch:
            raise AuthorityProductionWriterFenceLost("durable writer identity or epoch differs")
        if row["source_fence_sha256"] != self._expected_source_fence:
            raise AuthorityProductionWriterFenceLost("writer source fence differs")
        return row

    def persist_command_bundle(
        self,
        *,
        workflow_id: str,
        idempotency_key: str,
        command: CommandEnvelopeV1,
        event: EventEnvelopeV1,
        receipt: ReceiptEnvelopeV1,
        outbox: OutboxMessageV1,
        occurred_at: int,
        phase3_mutation: Phase3Mutation | None = None,
    ) -> AuthorityProductionCommitResult:
        validate_command_envelope_structure(command)
        validate_event_envelope(event)
        validate_receipt_envelope(receipt)
        validate_outbox_message(outbox)
        workflow_key = _text(workflow_id, "workflow_id")
        idempotency = _text(idempotency_key, "idempotency_key")
        now = _nonnegative(occurred_at, "occurred_at")
        command_sha256 = command_envelope_sha256(command)
        event_sha256 = event_envelope_sha256(event)
        receipt_sha256 = receipt_envelope_sha256(receipt)
        outbox_sha256 = outbox_message_sha256(outbox)
        checked_phase3: Phase3Mutation | None = None
        if phase3_mutation is None:
            request_schema = AUTHORITY_IDEMPOTENCY_REQUEST_SCHEMA
            request_sha256 = command_sha256
            bundle_schema = AUTHORITY_PRODUCTION_BUNDLE_SCHEMA_V1
            bundle_sha256 = _bundle_sha256(
                command_sha256, event_sha256, receipt_sha256, outbox_sha256
            )
        else:
            try:
                checked_phase3 = validate_phase3_mutation(phase3_mutation)
            except Phase3ContractError as exc:
                raise AuthorityEnvelopePersistenceError(str(exc)) from exc
            request_schema = AUTHORITY_PHASE3_IDEMPOTENCY_REQUEST_SCHEMA
            request_sha256 = _phase3_request_sha256(
                command_sha256, checked_phase3.mutation_sha256
            )
            bundle_schema = AUTHORITY_PRODUCTION_BUNDLE_SCHEMA_V2
            bundle_sha256 = _phase3_bundle_sha256(
                command_sha256,
                event_sha256,
                receipt_sha256,
                outbox_sha256,
                checked_phase3.mutation_sha256,
            )
        connection = connect_authority_rw(self._path, timeout_seconds=2)
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    raise AuthorityProductionWriterBusy("Authority writer lock is busy") from exc
                raise
            verify_production_installation(connection, require_ready=True)
            if legacy_source_identity_sha256(connection) != self._expected_source_fence:
                raise AuthorityProductionWriterFenceLost("source fence changed")
            writer_state = self._writer_state(connection)
            existing = _idempotency_record(connection, "workflow", workflow_key, idempotency)
            if existing is not None:
                if existing["request_schema"] != request_schema:
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
                           o.envelope_sha256 AS outbox_sha256,
                           pc.bundle_sha256, pc.writer_id, pc.writer_epoch,
                           pc.switch_epoch, pc.switch_mode
                    FROM authority_idempotency_records i
                    JOIN authority_commands c ON c.command_id=i.command_id
                    JOIN authority_receipts r ON r.receipt_id=i.receipt_id
                    JOIN authority_events e ON e.event_id=r.event_id
                    JOIN authority_outbox o ON o.event_id=e.event_id
                    JOIN authority_production_command_commits pc ON pc.command_id=c.command_id
                    JOIN authority_production_outbox_delivery_state ds ON ds.message_id=o.message_id
                    WHERE i.scope_kind='workflow' AND i.scope_id=? AND i.idempotency_key=?
                    """,
                    (workflow_key, idempotency),
                ).fetchone()
                if row is None:
                    raise AuthorityEnvelopePersistenceError(
                        "idempotency record lacks its production commit bundle"
                    )
                if (
                    row["command_sha256"], row["event_sha256"],
                    row["receipt_sha256"], row["outbox_sha256"], row["bundle_sha256"],
                ) != (
                    command_sha256, event_sha256, receipt_sha256,
                    outbox_sha256, bundle_sha256,
                ):
                    raise AuthorityEnvelopePersistenceError(
                        "idempotency replay companion envelope identity differs"
                    )
                if checked_phase3 is not None:
                    _verify_persisted_phase3_mutation(
                        connection,
                        workflow_id=workflow_key,
                        revision=int(row["committed_revision"]),
                        command_id=str(row["command_id"]),
                        mutation=checked_phase3,
                    )
                    assert checked_phase3.previous_head is not None
                    _verify_phase3_owner_authorizations(
                        connection,
                        checked_phase3,
                        workflow_id=workflow_key,
                        source_revision=checked_phase3.previous_head.source_revision,
                        command_id=str(row["command_id"]),
                        writer_id=str(row["writer_id"]),
                        writer_epoch=int(row["writer_epoch"]),
                    )
                connection.commit()
                return AuthorityProductionCommitResult(
                    workflow_key, int(row["committed_revision"]),
                    str(row["command_id"]), str(row["event_id"]),
                    str(row["receipt_id"]), str(row["message_id"]),
                    request_sha256, bundle_sha256, str(row["writer_id"]),
                    int(row["writer_epoch"]), int(row["switch_epoch"]),
                    str(row["switch_mode"]), True,
                    request_schema=request_schema,
                    bundle_schema=bundle_schema,
                    phase3_mutation_sha256=(
                        None
                        if checked_phase3 is None
                        else checked_phase3.mutation_sha256
                    ),
                )

            workflow = _workflow(connection, workflow_key)
            if workflow["current_revision_availability"] != "RECORDED":
                raise AuthorityRevisionConflict("workflow current revision is legacy_unknown")
            current_revision = workflow["current_revision"]
            if type(current_revision) is not int:
                raise AuthorityRevisionConflict("workflow current revision is unavailable")
            if checked_phase3 is not None:
                checked_phase3 = _require_phase3_mutation(
                    checked_phase3,
                    workflow_id=workflow_key,
                    source_revision=current_revision,
                )
                _verify_phase3_previous_head(
                    connection,
                    checked_phase3,
                    workflow_id=workflow_key,
                    source_revision=current_revision,
                )
                _control_id, _source_revision, _target_scope, _reason_code, read_set = (
                    _phase3_control_fields(checked_phase3)
                )
                _verify_phase3_read_set(
                    connection,
                    workflow_id=workflow_key,
                    through_revision=current_revision,
                    read_set=read_set,
                )
                _verify_phase3_owner_policy(
                    connection,
                    checked_phase3,
                    workflow_id=workflow_key,
                    through_revision=current_revision,
                )
                _verify_phase3_owner_authorizations(
                    connection,
                    checked_phase3,
                    workflow_id=workflow_key,
                    source_revision=current_revision,
                    command_id=command.command_id,
                    writer_id=self._writer_id,
                    writer_epoch=self._writer_epoch,
                )
                _verify_phase3_checkpoint_predecessors(
                    connection,
                    checked_phase3,
                    workflow_id=workflow_key,
                )
            if command.project_binding.project_id != workflow["project_id"]:
                raise AuthorityEnvelopePersistenceError("command project differs from workflow")
            if command.project_binding.project_generation != workflow["project_generation"]:
                raise AuthorityEnvelopePersistenceError("command project generation differs")
            if command.project_binding.project_revision != current_revision:
                raise AuthorityRevisionConflict(
                    f"expected workflow revision {current_revision}, got "
                    f"{command.project_binding.project_revision}"
                )
            if (
                command.run_binding.runtime_generation,
                command.run_binding.scheduler_generation,
                command.run_binding.run_generation,
            ) != (
                workflow["runtime_generation"],
                workflow["scheduler_generation"],
                workflow["run_generation"],
            ):
                raise AuthorityEnvelopePersistenceError("command run generations differ")
            allocator = connection.execute(
                "SELECT next_revision FROM authority_revision_allocator WHERE workflow_id=?",
                (workflow_key,),
            ).fetchone()
            if allocator is None or allocator["next_revision"] != current_revision + 1:
                raise AuthorityRevisionConflict("workflow revision allocator drifted")
            revision = _allocate_revision(connection, workflow_key)
            if revision != current_revision + 1:
                raise AuthorityRevisionConflict("workflow revision allocation is not monotonic")
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
            _writer_failure_point("after_contract_pins")
            _persist_command_envelope(
                connection, command, workflow_id=workflow_key,
                persisted_revision=revision, idempotency_key=idempotency,
            )
            _writer_failure_point("after_command")
            _persist_event_envelope(connection, event)
            _writer_failure_point("after_event")
            _persist_receipt_envelope(connection, receipt)
            _writer_failure_point("after_receipt")
            _enqueue_outbox(connection, outbox)
            _writer_failure_point("after_outbox_intent")
            if checked_phase3 is not None:
                _persist_phase3_mutation(
                    connection,
                    workflow_id=workflow_key,
                    revision=revision,
                    command_id=command.command_id,
                    mutation=checked_phase3,
                )
            delivery_key = f"authority-outbox:{outbox.message_id}:{outbox_sha256}"
            connection.execute(
                """
                INSERT INTO authority_production_outbox_delivery_state(
                    message_id, delivery_key, status, attempt_count,
                    claim_consumer_id, claim_consumer_epoch, claim_epoch,
                    lease_expires_at, next_attempt_at, last_error_code,
                    provider_receipt_id, provider_receipt_sha256,
                    delivered_at, updated_at
                ) VALUES (?, ?, 'PENDING', 0, NULL, NULL, 0,
                          NULL, ?, NULL, NULL, NULL, NULL, ?)
                """,
                (outbox.message_id, delivery_key, now, now),
            )
            connection.execute(
                """
                INSERT INTO authority_production_command_commits(
                    command_id, workflow_id, revision, writer_id, writer_epoch,
                    switch_epoch, switch_mode, source_fence_sha256, bundle_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command.command_id, workflow_key, revision, self._writer_id,
                    self._writer_epoch, int(writer_state["switch_epoch"]),
                    str(writer_state["switch_mode"]), self._expected_source_fence,
                    bundle_sha256,
                ),
            )
            _writer_failure_point("after_production_commit_evidence")
            _commit_idempotency(
                connection, scope_kind="workflow", scope_id=workflow_key,
                idempotency_key=idempotency, request_sha256=request_sha256,
                command_id=command.command_id, receipt_id=receipt.receipt_id,
                revision=revision,
                request_schema=request_schema,
            )
            _update_workflow_revision(
                connection, workflow_id=workflow_key,
                current_revision=current_revision, revision=revision,
                pin_sha256=pin_sha256,
            )
            _writer_failure_point("before_commit")
            self._writer_state(connection)
            if legacy_source_identity_sha256(connection) != self._expected_source_fence:
                raise AuthorityProductionWriterFenceLost("source fence changed before commit")
            connection.commit()
            return AuthorityProductionCommitResult(
                workflow_key, revision, command.command_id, event.event_id,
                receipt.receipt_id, outbox.message_id, request_sha256,
                bundle_sha256, self._writer_id, self._writer_epoch,
                int(writer_state["switch_epoch"]),
                str(writer_state["switch_mode"]), False,
                request_schema=request_schema,
                bundle_schema=bundle_schema,
                phase3_mutation_sha256=(
                    None
                    if checked_phase3 is None
                    else checked_phase3.mutation_sha256
                ),
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
