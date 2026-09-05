"""Read-side proof of receipts emitted by the durable Phase9 dispatcher.

This is the A2_0020 source graph. It joins the grant, committed intent, actual
launch, completion observation and final receipt binding; a caller-supplied
completion envelope alone cannot satisfy it.
"""
from __future__ import annotations

from .canonical import canonical_bytes, canonical_sha256
from .phase9_forensic_replay import (
    Phase9ForensicReplayConflict, _strict_json, _replay_coordinate_sha256,
)


def _body(row, column, hash_column, hash_field):
    value = _strict_json(row[column].encode(), column)
    if (value.get(hash_field) != row[hash_column]
            or canonical_sha256({k: v for k, v in value.items() if k != hash_field}) != row[hash_column]):
        raise Phase9ForensicReplayConflict("runtime source graph self-hash differs")
    return value


def runtime_receipt_source_sha256(connection, *, request, receipt_kind, logical_id,
                                  logical_path, raw_bytes_sha256, byte_length,
                                  receipt_sha256, dependency_fingerprint_sha256,
                                  input_sha256, output_sha256, packet_sha256,
                                  invocation_id, attempt_id, process_scope_id):
    from .phase9_runtime_authority import dispatch_target, SCOPE

    tables = {}
    lookups = (
        ("attempt", "attempts", "attempt_id", attempt_id),
        ("launch", "launches", "attempt_id", attempt_id),
        ("observation", "observations", "attempt_id", attempt_id),
    )
    for name, suffix, key, identity in lookups:
        row = connection.execute(f"SELECT * FROM authority_production_phase9_runtime_{suffix} WHERE {key}=?", (identity,)).fetchone()
        if row is None:
            raise Phase9ForensicReplayConflict("runtime source graph lacks an actual launch/completion")
        tables[name] = dict(row)
    attempt = tables["attempt"]
    for name, table, key, identity in (
        ("run", "runtime_runs", "runtime_id", attempt["runtime_id"]),
        ("terminal", "runtime_terminals", "runtime_id", attempt["runtime_id"]),
    ):
        row = connection.execute(f"SELECT * FROM authority_production_phase9_{table} WHERE {key}=?", (identity,)).fetchone()
        if row is None:
            raise Phase9ForensicReplayConflict("runtime source graph lacks its durable terminal")
        tables[name] = dict(row)
    run = tables["run"]
    row = connection.execute("SELECT * FROM authority_production_phase9_dispatch_grants WHERE grant_id=?", (run["grant_id"],)).fetchone()
    binding_row = connection.execute(
        "SELECT * FROM authority_production_phase9_runtime_receipt_bindings WHERE attempt_id=? AND receipt_kind=?",
        (attempt_id, receipt_kind),
    ).fetchone()
    if row is None or binding_row is None:
        raise Phase9ForensicReplayConflict("runtime source graph lacks a grant or receipt binding")
    tables["grant"], tables["binding"] = dict(row), dict(binding_row)
    grant = _body(tables["grant"], "grant_json", "grant_sha256", "grant_sha256")
    start = _body(run, "start_json", "start_sha256", "start_sha256")
    intent = _body(attempt, "dispatch_intent_json", "dispatch_intent_sha256", "dispatch_intent_sha256")
    for name, table, key, identity in (
        ("command", "authority_commands", "command_id", intent["command_id"]),
        ("invocation", "authority_invocations", "invocation_id", invocation_id),
        ("authority_attempt", "authority_attempts", "attempt_id", attempt_id),
        ("scope", "authority_process_scopes", "process_scope_id", process_scope_id),
    ):
        row = connection.execute(f"SELECT * FROM {table} WHERE {key}=?", (identity,)).fetchone()
        if row is None:
            raise Phase9ForensicReplayConflict("runtime source graph lacks its precommitted Authority identity")
        tables[name] = dict(row)
    encoded_intent = canonical_bytes(intent).decode()
    if (tables["command"]["envelope_json"] != encoded_intent
            or tables["command"]["envelope_sha256"] != canonical_sha256(intent)
            or tables["command"]["workflow_id"] != request.workflow_id
            or tables["command"]["project_id"] != request.project_id
            or tables["command"]["command_type"] != "PHASE9_A_RUNTIME_DISPATCH"
            or tables["invocation"]["command_id"] != intent["command_id"]
            or tables["authority_attempt"]["invocation_id"] != invocation_id
            or tables["scope"]["attempt_id"] != attempt_id
            or tables["scope"]["process_identity"] != intent["dispatch_intent_sha256"]
            or any(tables[name][field] != encoded_intent for name in ("invocation", "authority_attempt", "scope")
                   for field in ("scope_json", "metadata_json"))):
        raise Phase9ForensicReplayConflict("runtime Authority dispatch graph differs")
    launch = _body(tables["launch"], "launch_json", "launch_sha256", "launch_sha256")
    observation = _body(tables["observation"], "observation_json", "observation_sha256", "observation_sha256")
    terminal = _body(tables["terminal"], "terminal_json", "terminal_sha256", "terminal_sha256")
    binding = _body(tables["binding"], "binding_json", "binding_sha256", "binding_sha256")
    target = start["target"]
    is_probe = receipt_kind == "PROCESS_SCOPE"
    expected_target = dispatch_target(request, packet_sha256=target["packet_sha256"] if is_probe else packet_sha256,
                                     project_input_sha256=target["project_input_sha256"],
                                     model=target["model"], effort=target["effort"],
                                     timeout_seconds=target["timeout_seconds"],
                                     total_timeout_seconds=target["total_timeout_seconds"])
    observed = observation["observation"]
    expected_binding = {
        "schema": "authority-phase9-runtime-receipt-binding-v1",
        "attempt_id": attempt_id, "receipt_kind": receipt_kind,
        "logical_id": logical_id, "logical_path": logical_path,
        "raw_bytes_sha256": raw_bytes_sha256, "byte_length": byte_length,
        "receipt_sha256": receipt_sha256,
        "dependency_fingerprint_sha256": dependency_fingerprint_sha256,
        "input_sha256": input_sha256, "output_sha256": output_sha256,
        "packet_sha256": packet_sha256,
        "replay_coordinate_sha256": _replay_coordinate_sha256(request),
        "observation_sha256": observation["observation_sha256"],
    }
    expected_binding["binding_sha256"] = canonical_sha256(expected_binding)
    outputs = observed.get("outputs", {})
    if (binding != expected_binding or target != expected_target
            or grant.get("authorized") is not True or grant.get("scope") != SCOPE
            or grant["target_sha256"] != canonical_sha256(target)
            or run["target_sha256"] != grant["target_sha256"]
            or run["nonce_sha256"] != grant["nonce_sha256"]
            or start["grant_sha256"] != grant["grant_sha256"]
            or attempt["role"] != logical_id or intent["role"] != logical_id
            or intent["invocation_id"] != invocation_id or attempt["invocation_id"] != invocation_id
            or intent["process_scope_id"] != process_scope_id or attempt["process_scope_id"] != process_scope_id
            or intent["target_sha256"] != run["target_sha256"]
            or launch["dispatch_intent_sha256"] != intent["dispatch_intent_sha256"]
            or observation["dispatch_intent_sha256"] != intent["dispatch_intent_sha256"]
            or observation["attempt_id"] != attempt_id or launch["attempt_id"] != attempt_id
            or observed.get("launch_sha256") != launch["launch_sha256"]
            or observed.get("process_pid") != launch["process_pid"]
            or observed.get("process_group_active_count") != 0
            or observed.get("outcome") != "SUCCEEDED"
            or (observed.get("probe_pass") is not True if is_probe else observed.get("exit_code") != 0)
            or (observed.get("probe_result_sha256") != output_sha256 if is_probe
                else not any(value.get("sha256") == output_sha256 for value in outputs.values()))
            or (input_sha256 != observed.get("process_identity_sha256") or packet_sha256 is not None if is_probe else input_sha256 != packet_sha256)
            or tables["observation"]["outcome"] != "SUCCEEDED"
            or terminal["start_sha256"] != start["start_sha256"]
            or terminal["status"] != "COMPLETED"
            or tables["terminal"]["terminal_status"] != "COMPLETED"
            or not grant["issued_at"] <= run["started_at"] <= grant["expires_at"]
            or not run["started_at"] <= intent["committed_at"] <= launch["observed_at"] <= observation["observed_at"] <= terminal["completed_at"] <= request.occurred_at
            or terminal["completed_at"] > run["deadline_at"]):
        raise Phase9ForensicReplayConflict("runtime receipt differs from its actual execution source")
    return canonical_sha256({"schema": "authority-phase9-runtime-source-v2", "rows": tables})
