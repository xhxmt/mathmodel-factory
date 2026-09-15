"""Lazy, explicit Authority route for solver configuration only.

The normal engine route does not import Authority. Mode selection is advisory;
the fenced writer and database triggers enforce it again inside transactions.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time

from .domain import InvalidTransition, RevisionConflict


@dataclass(frozen=True)
class AuthoritySolverRoute:
    database: Path
    workflow: dict
    writer: dict
    legacy_policy: dict
    latest_policy_key: str | None


def authority_solver_route(project: Path) -> AuthoritySolverRoute | None:
    database = project / ".factory/state.db"
    if not database.is_file():
        return None
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        installed = connection.execute(
            "SELECT name FROM sqlite_master WHERE name='authority_production_schema_state'"
        ).fetchone()
        if installed is None:
            return None
        row = connection.execute(
            "SELECT * FROM authority_production_schema_state WHERE singleton=1"
        ).fetchone()
        if row is None or row["state"] != "READY":
            raise InvalidTransition("Authority migration is not ready; legacy fallback is disabled")
        writer = connection.execute(
            "SELECT * FROM authority_production_writer_state WHERE singleton=1"
        ).fetchone()
        if writer is None:
            raise InvalidTransition("Authority writer state is missing")
        if writer["switch_mode"] == "V1_ONLY":
            return None
        if writer["switch_mode"] not in {"CANARY", "AUTHORITY_PRIMARY"}:
            raise InvalidTransition("Authority switch mode is unsupported")

        from .authority_production_schema import verify_production_installation
        verify_production_installation(connection, require_ready=True)
        if not writer["writer_enabled"] or writer["writer_id"] != "factory-service":
            raise InvalidTransition("Authority solver configuration requires the enabled factory-service writer")
        native = connection.execute("SELECT project_id FROM project_state WHERE singleton=1").fetchone()
        workflows = connection.execute(
            "SELECT * FROM authority_workflows WHERE project_id=?", (native["project_id"],)
        ).fetchall()
        if len(workflows) != 1 or workflows[0]["current_revision_availability"] != "RECORDED":
            raise InvalidTransition("Authority solver route requires one recorded project workflow")
        workflow = dict(workflows[0])
        old = connection.execute("SELECT * FROM project_config WHERE singleton=1").fetchone()
        legacy_policy = {"mode": old["solver_mode"], "threshold_seconds": old["solver_threshold_seconds"],
                         "allowed_runtimes": json.loads(old["solver_runtimes_json"]), "updated_revision": old["updated_revision"]} if old else {
            "mode": "local", "threshold_seconds": 300, "allowed_runtimes": ["python"], "updated_revision": 0}
        latest = connection.execute(
            "SELECT idempotency_key FROM authority_commands WHERE workflow_id=? "
            "AND command_type='CONFIGURE_SOLVER_POLICY' AND persisted_revision<=? "
            "ORDER BY persisted_revision DESC LIMIT 1",
            (workflow["workflow_id"], workflow["current_revision"]),
        ).fetchone()
        return AuthoritySolverRoute(database, workflow, dict(writer), legacy_policy, None if latest is None else latest[0])


def read_authority_solver_policy(route: AuthoritySolverRoute) -> dict:
    from .authority_read_repository import AuthorityReadRepository
    from .authority_solver_policy import decode_policy, POLICY_COMMAND, POLICY_EVENT, POLICY_SCHEMA
    from .canonical import canonical_sha256

    policy = route.legacy_policy
    if route.latest_policy_key is not None:
        reader = AuthorityReadRepository(route.database, expected_source_fence_sha256=route.writer["source_fence_sha256"])
        bundle = reader.command_bundle(workflow_id=route.workflow["workflow_id"], idempotency_key=route.latest_policy_key)
        event = json.loads(bundle.event_bytes)
        command = json.loads(bundle.command_bytes)
        if event["event_type"] != POLICY_EVENT or command["command_type"] != POLICY_COMMAND:
            raise InvalidTransition("Authority solver policy envelope type differs")
        if len(event["fields"]) != 1 or event["fields"][0]["key"] != "policy_json":
            raise InvalidTransition("Authority solver policy event fields differ")
        value = decode_policy(event["fields"][0]["value"])
        if command["payload_binding"] != {"payload_schema": POLICY_SCHEMA, "payload_sha256": canonical_sha256(value)}:
            raise InvalidTransition("Authority solver policy payload identity differs")
        policy = {key: value[key] for key in ("mode", "threshold_seconds", "allowed_runtimes")}
        policy["updated_revision"] = bundle.revision
    return {**policy, "revision": route.workflow["current_revision"], "authority": "authority"}


def configure_authority_solver_policy(route, *, mode, threshold_seconds, allowed_runtimes, expected_revision):
    from .authority_envelopes import (EVENT_ENVELOPE_SCHEMA, RECEIPT_ENVELOPE_SCHEMA, OUTBOX_MESSAGE_SCHEMA,
                                     EnvelopeFieldV1, EventEnvelopeV1, ReceiptEnvelopeV1, OutboxMessageV1)
    from .authority_production_writer import AuthorityProductionWriter
    from .authority_repository import AuthorityRevisionConflict
    from .authority_solver_policy import normalize_policy, POLICY_SCHEMA, POLICY_EVENT, POLICY_TOPIC
    from .canonical import canonical_bytes, canonical_sha256
    from .command_envelope import (COMMAND_ENVELOPE_SCHEMA, ActorRefV1, ActorType, CommandEnvelopeV1, CommandType,
                                   NoEntityScopeV1, NoSubjectScopeV1, PayloadBindingV1, ProjectGenerationBindingV1,
                                   RunGenerationBindingV1, compile_read_set)
    from .contract_pins import compile_contract_pin_set
    from .workflow_contract_v2 import compile_workflow_contract_bundle_v2

    policy = normalize_policy(mode, threshold_seconds, allowed_runtimes)
    workflow = route.workflow
    if expected_revision is None:
        raise InvalidTransition(
            "Authority solver policy requires an explicit expected_revision; "
            "read the current solver policy before retrying"
        )
    revision = expected_revision
    if type(revision) is not int or revision < 1:
        raise ValueError("expected_revision must be a positive integer")
    pins = compile_contract_pin_set(compile_workflow_contract_bundle_v2())
    pin_hash = canonical_sha256(pins)
    policy_hash = canonical_sha256(policy)
    identity = canonical_sha256({"schema": POLICY_SCHEMA, "workflow_id": workflow["workflow_id"],
                                 "project_id": workflow["project_id"], "project_generation": workflow["project_generation"],
                                 "run_generation": workflow["run_generation"], "revision": revision, "policy": policy})
    command_id, event_id, receipt_id, message_id = (f"{prefix}:{identity}" for prefix in ("solver-policy", "solver-policy-event", "solver-policy-receipt", "solver-policy-notice"))
    command = CommandEnvelopeV1(COMMAND_ENVELOPE_SCHEMA, command_id, CommandType.CONFIGURE_SOLVER_POLICY,
        ProjectGenerationBindingV1(workflow["project_id"], workflow["project_generation"], revision),
        RunGenerationBindingV1(workflow["runtime_generation"], workflow["scheduler_generation"], workflow["run_generation"]),
        NoEntityScopeV1(), NoSubjectScopeV1(), ActorRefV1(ActorType.SERVICE, "factory-service"),
        PayloadBindingV1(POLICY_SCHEMA, policy_hash), compile_read_set(()), pins)
    event = EventEnvelopeV1(EVENT_ENVELOPE_SCHEMA, event_id, workflow["project_id"], workflow["workflow_id"], revision + 1,
        POLICY_EVENT, command_id, workflow["project_generation"], workflow["run_generation"], workflow["runtime_generation"],
        workflow["scheduler_generation"], pin_hash, (EnvelopeFieldV1("policy_json", canonical_bytes(policy).decode()),))
    receipt = ReceiptEnvelopeV1(RECEIPT_ENVELOPE_SCHEMA, receipt_id, workflow["project_id"], workflow["workflow_id"], revision + 1,
        command_id, event_id, "RECORDED", pin_hash, (EnvelopeFieldV1("policy_sha256", policy_hash),))
    outbox = OutboxMessageV1(OUTBOX_MESSAGE_SCHEMA, message_id, workflow["workflow_id"], revision + 1, event_id, POLICY_TOPIC,
        (EnvelopeFieldV1("policy_sha256", policy_hash), EnvelopeFieldV1("receipt_id", receipt_id)))
    writer = AuthorityProductionWriter(route.database, writer_id="factory-service", writer_epoch=route.writer["writer_epoch"],
        expected_source_fence_sha256=route.writer["source_fence_sha256"])
    try:
        committed = writer.persist_command_bundle(workflow_id=workflow["workflow_id"], idempotency_key=command_id,
            command=command, event=event, receipt=receipt, outbox=outbox, occurred_at=int(time.time()))
    except AuthorityRevisionConflict as exc:
        raise RevisionConflict(str(exc)) from exc
    return {key: value for key, value in policy.items() if key != "schema"} | {
        "revision": committed.revision, "updated_revision": committed.revision, "authority": "authority"}
