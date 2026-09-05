"""Append-only dispatch authorization and runtime lifecycle for Phase9-A.

The dispatcher consumes a distinct operator grant before external execution.
Finalizer start authorizations retain their existing completion-only meaning.
No transaction remains open while a provider is running. Unobserved or
uncertain attempts are never resent by this service.
"""

from __future__ import annotations

import os
import hashlib
from pathlib import Path
import pwd
import time
import uuid

from .authority_production_schema import connect_authority_rw
from .canonical import canonical_bytes, canonical_sha256
from .phase9_authority_lease import authority_state_commit_lease, isolated_authority_snapshot_ro
from .phase9_forensic_replay import (
    Phase9ForensicReplayService, _strict_json, _verify_entry_gate,
)
from .phase9_replay_evidence import _producer_live_precheck


class Phase9RuntimeAuthorityError(RuntimeError):
    pass


GRANT_SCHEMA = "authority-phase9-dispatch-grant-v1"
TARGET_SCHEMA = "authority-phase9-dispatch-target-v1"
START_SCHEMA = "authority-phase9-runtime-start-v1"
OBSERVATION_SCHEMA = "authority-phase9-runtime-observation-v1"
SCOPE = {
    "phase9_role_provider_calls": True,
    "local_process_scope_probes": True,
    "delivery": False, "release": False, "submission": False,
    "migration": False, "deployment": False, "cutover": False,
    "production_outbox_dispatch": False,
}


def dispatch_target(request, *, packet_sha256: str, project_input_sha256: str,
                    model: str, effort: str, timeout_seconds: int,
                    total_timeout_seconds: int, provider=None) -> dict:
    """A stable execution identity does not contain a future completion time."""
    from .phase9_forensic_replay import PHASE9_RUNTIME_REPLAY_REQUEST_SCHEMA
    if request.schema_version != PHASE9_RUNTIME_REPLAY_REQUEST_SCHEMA:
        raise Phase9RuntimeAuthorityError("durable runtime requires the v3 replay request coordinate")
    if request.replay_mode != "TECHNICAL":
        raise Phase9RuntimeAuthorityError("ablation uses its distinct no-provider finalizer route")
    for digest in (packet_sha256, project_input_sha256):
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise Phase9RuntimeAuthorityError("runtime input digest is malformed")
    if not model or not effort:
        raise Phase9RuntimeAuthorityError("runtime model configuration is incomplete")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
        raise Phase9RuntimeAuthorityError("per-call runtime limit is invalid")
    if type(total_timeout_seconds) is not int or not 1 <= total_timeout_seconds <= 7200:
        raise Phase9RuntimeAuthorityError("total runtime limit is invalid")
    if provider is None:
        from .phase9_provider_identity import provider_identity
        provider = provider_identity()
    binding = request.as_dict()
    binding.pop("occurred_at")
    binding.pop("evidence_files")
    # Entry receipts are short-lived acquisitions. Final evidence may use a new
    # READY entry at the identical stable coordinate after a long execution.
    binding.pop("entry_gate_result_sha256")
    return {
        "schema": TARGET_SCHEMA, "replay_binding": binding,
        "packet_sha256": packet_sha256,
        "project_input_sha256": project_input_sha256,
        "model": model, "effort": effort, "provider_identity": provider,
        "roles": ["execution", "math", "paper"],
        "process_scope_actions": ["failed", "kill", "pause"],
        "max_attempts_per_role": 8,
        "timeout_seconds": timeout_seconds,
        "total_timeout_seconds": total_timeout_seconds,
        "delivery_capability": "DISABLED",
    }


def validate_dispatch_grant(grant: dict, target: dict, *, now: int, require_account: bool = True) -> None:
    expected = {
        "schema", "grant_id", "nonce_sha256", "target_sha256", "authorized",
        "authorization_mechanism", "authorization_evidence_sha256",
        "operator_uid", "operator_account", "issued_at", "expires_at",
        "scope", "grant_sha256",
    }
    if not isinstance(grant, dict) or set(grant) != expected:
        raise Phase9RuntimeAuthorityError("dispatch grant schema/keys differ")
    if (grant["schema"] != GRANT_SCHEMA or grant["authorized"] is not True
            or grant["scope"] != SCOPE
            or grant["authorization_mechanism"] != "CONTROLLED_OS_ACCOUNT"
            or grant["target_sha256"] != canonical_sha256(target)
            or grant["grant_sha256"] != canonical_sha256({k: v for k, v in grant.items() if k != "grant_sha256"})):
        raise Phase9RuntimeAuthorityError("dispatch grant target, scope or hash differs")
    for key in ("nonce_sha256", "authorization_evidence_sha256"):
        value = grant[key]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise Phase9RuntimeAuthorityError("dispatch grant evidence digest is malformed")
    if (type(grant["operator_uid"]) is not int or grant["operator_uid"] < 0
            or not isinstance(grant["operator_account"], str) or not grant["operator_account"]
            or (require_account and (grant["operator_uid"] != os.geteuid()
                or grant["operator_account"] != pwd.getpwuid(os.geteuid()).pw_name))):
        raise Phase9RuntimeAuthorityError("dispatch grant controlled account differs")
    issued, expires = grant["issued_at"], grant["expires_at"]
    if (type(issued) is not int or type(expires) is not int
            or not issued <= now <= expires or not 0 < expires - issued <= 300):
        raise Phase9RuntimeAuthorityError("dispatch grant is expired or has an invalid acquisition window")
    if not isinstance(grant["grant_id"], str) or not grant["grant_id"] or len(grant["grant_id"]) > 192:
        raise Phase9RuntimeAuthorityError("dispatch grant ID is invalid")


class Phase9RuntimeAuthority:
    def __init__(self, finalizer: Phase9ForensicReplayService):
        self.finalizer = finalizer
        self.database = finalizer.path
        self.project_root = self.database.parent.parent

    def _connect(self):
        connection = connect_authority_rw(self.database)
        connection.create_function("phase9_runtime_execution_capability", 0, lambda: 1)
        return connection

    def start(self, request, entry: dict, target: dict, grant: dict) -> dict:
        """Consume one external grant and commit the execution plan before dispatch."""
        now = int(time.time())
        validate_dispatch_grant(grant, target, now=now)
        expected_target = dispatch_target(
            request, packet_sha256=target["packet_sha256"],
            project_input_sha256=target["project_input_sha256"],
            model=target["model"], effort=target["effort"],
            timeout_seconds=target["timeout_seconds"],
            total_timeout_seconds=target["total_timeout_seconds"], provider=target["provider_identity"],
        )
        if target != expected_target or request.delivery_capability != "DISABLED":
            raise Phase9RuntimeAuthorityError("runtime plan differs from its exact replay coordinate")
        from .phase9_provider_identity import provider_identity
        if provider_identity(self.project_root) != target["provider_identity"]:
            raise Phase9RuntimeAuthorityError("approved provider installation/configuration changed")
        state_sha, _ = _verify_entry_gate(entry, request, trusted_now=now)
        with authority_state_commit_lease(self.project_root):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                validate_dispatch_grant(grant, target, now=int(time.time()))
                if connection.execute(
                    "SELECT 1 FROM authority_production_phase9_runtime_runs WHERE run_generation=? OR nonce_sha256=?",
                    (request.run_generation, grant["nonce_sha256"]),
                ).fetchone():
                    raise Phase9RuntimeAuthorityError("runtime generation or nonce was already consumed; collect without redispatch")
                _producer_live_precheck(self.finalizer, connection, request, {
                    "entry_state_receipt_sha256": state_sha,
                    "runtime_counts": {"active_descendant_count": 0, "pending_outbox_count": 0},
                })
                runtime_id = "phase9-runtime:" + uuid.uuid4().hex
                started = int(time.time())
                start = {
                    "schema": START_SCHEMA, "runtime_id": runtime_id,
                    "target": target, "target_sha256": canonical_sha256(target),
                    "grant_sha256": grant["grant_sha256"],
                    "entry_gate_result_sha256": request.entry_gate_result_sha256,
                    "entry_state_receipt_sha256": state_sha,
                    "started_at": started,
                    "deadline_at": started + target["total_timeout_seconds"],
                    "delivery_capability": "DISABLED",
                }
                start["start_sha256"] = canonical_sha256(start)
                connection.execute(
                    "INSERT INTO authority_production_phase9_dispatch_grants VALUES(?,?,?,?,?,?,?,?,?)",
                    (grant["grant_id"], grant["nonce_sha256"], canonical_sha256(target),
                     request.workflow_id, request.run_generation, grant["issued_at"],
                     grant["expires_at"], canonical_bytes(grant).decode(), grant["grant_sha256"]),
                )
                connection.execute(
                    "INSERT INTO authority_production_phase9_runtime_runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (runtime_id, grant["grant_id"], grant["nonce_sha256"], request.workflow_id,
                     request.run_generation, canonical_sha256(target), started, start["deadline_at"],
                     canonical_bytes(start).decode(), start["start_sha256"]),
                )
                connection.commit()
                return start
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _run(self, connection, runtime_id, *, require_current=True):
        self.finalizer._verify_installation(connection)
        self.finalizer._control_fence(connection)
        row = connection.execute(
            "SELECT * FROM authority_production_phase9_runtime_runs WHERE runtime_id=?", (runtime_id,),
        ).fetchone()
        if row is None:
            raise Phase9RuntimeAuthorityError("runtime does not exist")
        start = _strict_json(row["start_json"].encode(), "runtime start")
        grant_row = connection.execute(
            "SELECT * FROM authority_production_phase9_dispatch_grants WHERE grant_id=?", (row["grant_id"],)
        ).fetchone()
        if grant_row is None:
            raise Phase9RuntimeAuthorityError("runtime dispatch grant is missing")
        grant = _strict_json(grant_row["grant_json"].encode(), "runtime grant")
        validate_dispatch_grant(grant, start["target"], now=row["started_at"], require_account=require_current)
        if (start.get("schema") != START_SCHEMA
                or start.get("runtime_id") != runtime_id
                or start.get("started_at") != row["started_at"]
                or start.get("deadline_at") != row["deadline_at"]
                or row["deadline_at"] != row["started_at"] + start["target"]["total_timeout_seconds"]
                or start.get("grant_sha256") != grant_row["grant_sha256"]
                or grant["grant_sha256"] != grant_row["grant_sha256"]
                or grant["nonce_sha256"] != row["nonce_sha256"]
                or grant["target_sha256"] != row["target_sha256"]
                or grant_row["target_sha256"] != row["target_sha256"]
                or grant_row["workflow_id"] != row["workflow_id"]
                or grant_row["run_generation"] != row["run_generation"]
                or start["target"]["replay_binding"]["workflow_id"] != row["workflow_id"]
                or start["target"]["replay_binding"]["run_generation"] != row["run_generation"]
                or start.get("delivery_capability") != "DISABLED"
                or start.get("start_sha256") != row["start_sha256"]
                or canonical_sha256({k: v for k, v in start.items() if k != "start_sha256"}) != row["start_sha256"]
                or canonical_sha256(start["target"]) != row["target_sha256"]):
            raise Phase9RuntimeAuthorityError("runtime start binding differs")
        current = connection.execute(
            "SELECT run_generation FROM authority_production_run_generation_current WHERE workflow_id=?",
            (row["workflow_id"],),
        ).fetchone()
        if require_current and (current is None or current[0] != row["run_generation"]):
            raise Phase9RuntimeAuthorityError("runtime generation is no longer current")
        if require_current:
            from .phase9_forensic_replay import Phase9ForensicReplayRequestV1
            bound_request = Phase9ForensicReplayRequestV1(
                **start["target"]["replay_binding"], evidence_files=(),
                entry_gate_result_sha256=start["entry_gate_result_sha256"],
                occurred_at=start["started_at"],
            )
            self.finalizer._verify_coordinate(connection, bound_request)
            self.finalizer._verify_external_generation_inputs(connection, bound_request)
        return row, start

    def reserve_attempt(self, runtime_id: str, role: str, request_sha256: str, *, provider_call=None) -> dict:
        """Commit intent once; a crash after this point requires reconciliation."""
        if not isinstance(request_sha256, str) or len(request_sha256) != 64 or any(c not in "0123456789abcdef" for c in request_sha256):
            raise Phase9RuntimeAuthorityError("dispatch request digest is malformed")
        with authority_state_commit_lease(self.project_root):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row, start = self._run(connection, runtime_id)
                if role not in start["target"]["roles"] + start["target"]["process_scope_actions"] or int(time.time()) >= row["deadline_at"]:
                    raise Phase9RuntimeAuthorityError("role is unauthorized or runtime deadline exhausted")
                if connection.execute("SELECT 1 FROM authority_production_phase9_runtime_terminals WHERE runtime_id=?", (runtime_id,)).fetchone():
                    raise Phase9RuntimeAuthorityError("runtime is terminal")
                unresolved = connection.execute(
                    "SELECT 1 FROM authority_production_phase9_runtime_attempts a "
                    "LEFT JOIN authority_production_phase9_runtime_observations o ON o.attempt_id=a.attempt_id "
                    "WHERE a.runtime_id=? AND (o.attempt_id IS NULL OR o.outcome='UNCERTAIN')", (runtime_id,),
                ).fetchone()
                if unresolved:
                    raise Phase9RuntimeAuthorityError("an earlier dispatch is unobserved or uncertain; automatic resend prohibited")
                attempt = connection.execute(
                    "SELECT COUNT(*) FROM authority_production_phase9_runtime_attempts WHERE runtime_id=? AND role=?", (runtime_id, role),
                ).fetchone()[0] + 1
                limit = start["target"]["max_attempts_per_role"] if role in start["target"]["roles"] else 1
                if attempt > limit:
                    raise Phase9RuntimeAuthorityError("role retry budget exhausted")
                identity = uuid.uuid4().hex
                intent = {
                    "schema": "authority-phase9-runtime-dispatch-intent-v1",
                    "runtime_id": runtime_id, "role": role, "role_attempt": attempt,
                    "attempt_id": "phase9-attempt:" + identity,
                    "command_id": "phase9-command:" + identity,
                    "invocation_id": "phase9-invocation:" + identity,
                    "process_scope_id": "phase9-scope:" + identity,
                    "request_sha256": request_sha256,
                    "target_sha256": row["target_sha256"], "committed_at": int(time.time()),
                }
                if provider_call is not None:
                    from .phase9_provider_identity import validate_call
                    validate_call(provider_call, start["target"]["provider_identity"], model=start["target"]["model"], effort=start["target"]["effort"])
                    intent["provider_call"] = provider_call
                intent["dispatch_intent_sha256"] = canonical_sha256(intent)
                workflow = connection.execute(
                    "SELECT current_revision,contract_pin_set_sha256 FROM authority_workflows WHERE workflow_id=?", (row["workflow_id"],)
                ).fetchone()
                if workflow is None or type(workflow[0]) is not int or workflow[0] < 1 or not workflow[1]:
                    raise Phase9RuntimeAuthorityError("runtime requires a recorded workflow revision and contract pin")
                encoded = canonical_bytes(intent).decode()
                connection.execute("INSERT INTO authority_commands VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                    intent["command_id"], row["workflow_id"], start["target"]["replay_binding"]["project_id"],
                    workflow[0], workflow[0], "PHASE9_A_RUNTIME_DISPATCH", intent["schema"], encoded,
                    canonical_sha256(intent), workflow[1], intent["command_id"],
                ))
                connection.execute("INSERT INTO authority_invocations VALUES(?,?,?,?,?,?,?,?,?)", (
                    intent["invocation_id"], row["workflow_id"], intent["command_id"], "PHASE9_A_RUNTIME_DISPATCH",
                    attempt, workflow[0], intent["schema"], encoded, encoded,
                ))
                connection.execute("INSERT INTO authority_attempts VALUES(?,?,?,?,?,?,?)", (
                    intent["attempt_id"], intent["invocation_id"], attempt, workflow[0], intent["schema"], encoded, encoded,
                ))
                connection.execute("INSERT INTO authority_process_scopes VALUES(?,?,?,?,?,?,?,?)", (
                    intent["process_scope_id"], intent["attempt_id"], "PHASE9_A_RUNTIME_DISPATCH",
                    intent["dispatch_intent_sha256"], workflow[0], intent["schema"], encoded, encoded,
                ))
                connection.execute(
                    "INSERT INTO authority_production_phase9_runtime_attempts VALUES(?,?,?,?,?,?,?,?,?)",
                    (intent["attempt_id"], runtime_id, role, attempt, intent["invocation_id"],
                     intent["process_scope_id"], canonical_bytes(intent).decode(),
                     intent["dispatch_intent_sha256"], intent["committed_at"]),
                )
                connection.commit()
                return intent
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def observe(self, intent: dict, observation: dict) -> dict:
        """Called by the actual completion path; never substitutes missing output."""
        with authority_state_commit_lease(self.project_root):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._run(connection, intent["runtime_id"], require_current=False)
                row = connection.execute(
                    "SELECT dispatch_intent_json FROM authority_production_phase9_runtime_attempts WHERE attempt_id=?",
                    (intent["attempt_id"],),
                ).fetchone()
                if row is None or row[0] != canonical_bytes(intent).decode():
                    raise Phase9RuntimeAuthorityError("completion does not own the committed dispatch")
                if observation.get("outcome") not in {"SUCCEEDED", "FAILED", "TIMEOUT", "CANCELLED", "UNCERTAIN"}:
                    raise Phase9RuntimeAuthorityError("runtime observation outcome is invalid")
                if observation["outcome"] == "SUCCEEDED":
                    is_probe = intent["role"] in {"failed", "kill", "pause"}
                    launch = connection.execute(
                        "SELECT launch_sha256,process_pid FROM authority_production_phase9_runtime_launches WHERE attempt_id=?",
                        (intent["attempt_id"],),
                    ).fetchone()
                    if (launch is None or observation.get("launch_sha256") != launch[0]
                            or observation.get("process_pid") != launch[1]
                            or (observation.get("probe_pass") is not True if is_probe else observation.get("exit_code") != 0)
                            or observation.get("process_group_active_count") != 0
                            or not observation.get("outputs")
                            or (not is_probe and observation.get("response_model_identity") is None)):
                        raise Phase9RuntimeAuthorityError("successful observation lacks an owned launch, output or process closure")
                body = {
                    "schema": OBSERVATION_SCHEMA, "attempt_id": intent["attempt_id"],
                    "runtime_id": intent["runtime_id"],
                    "dispatch_intent_sha256": intent["dispatch_intent_sha256"],
                    "observed_at": int(time.time()), "observation": observation,
                }
                body["observation_sha256"] = canonical_sha256(body)
                connection.execute(
                    "INSERT INTO authority_production_phase9_runtime_observations VALUES(?,?,?,?,?)",
                    (intent["attempt_id"], observation["outcome"], body["observed_at"],
                     canonical_bytes(body).decode(), body["observation_sha256"]),
                )
                connection.commit()
                return body
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def accept_output(self, intent, selection):
        """Persist the native judge selection, only after observed completion."""
        with authority_state_commit_lease(self.project_root):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._run(connection, intent["runtime_id"])
                row = connection.execute("SELECT a.dispatch_intent_json,o.observation_json FROM authority_production_phase9_runtime_attempts a JOIN authority_production_phase9_runtime_observations o ON o.attempt_id=a.attempt_id WHERE a.attempt_id=?", (intent["attempt_id"],)).fetchone()
                if row is None or row[0] != canonical_bytes(intent).decode():
                    raise Phase9RuntimeAuthorityError("accepted output has no owned attempt")
                observed = _strict_json(row[1].encode(), "accepted output observation")["observation"]
                if (observed["outcome"] != "SUCCEEDED" or selection.get("role") != intent["role"]
                        or observed["outputs"].get(selection.get("source")) != {
                            "sha256": selection.get("sha256"), "byte_length": selection.get("byte_length")}):
                    raise Phase9RuntimeAuthorityError("accepted output differs from actual observation")
                body = {"schema": "phase9-native-output-selection-v1", "attempt_id": intent["attempt_id"], "selection": selection}
                body["selection_sha256"] = canonical_sha256(body)
                connection.execute("INSERT INTO authority_production_phase9_runtime_accepted_outputs VALUES(?,?,?)", (intent["attempt_id"], canonical_bytes(body).decode(), body["selection_sha256"]))
                connection.commit()
            finally:
                connection.close()

    def launch(self, intent: dict, pid: int, *, execution=None) -> dict:
        """Observe an actual owned Linux process immediately after Popen."""
        if type(pid) is not int or pid <= 0:
            raise Phase9RuntimeAuthorityError("runtime process PID is invalid")
        raw = Path(f"/proc/{pid}/stat").read_text()
        fields = raw[raw.rfind(")") + 2:].split()
        if int(fields[1]) != os.getpid() or int(fields[2]) != pid or int(fields[3]) != pid:
            raise Phase9RuntimeAuthorityError("runtime process is not an owned session leader")
        launch = {
            "schema": "authority-phase9-runtime-launch-v1", "attempt_id": intent["attempt_id"],
            "dispatch_intent_sha256": intent["dispatch_intent_sha256"],
            "process_pid": pid, "process_start_ticks": fields[19],
            "boot_id_sha256": hashlib.sha256(Path("/proc/sys/kernel/random/boot_id").read_bytes()).hexdigest(),
            "observed_at": int(time.time()),
        }
        if intent["role"] in {"math", "execution", "paper"}:
            if not intent.get("provider_call") or execution is None:
                raise Phase9RuntimeAuthorityError("provider launch lacks its approved execution identity")
            from .phase9_provider_identity import verify_launch
            verify_launch(intent, pid, execution)
            launch["execution"] = execution
        launch["launch_sha256"] = canonical_sha256(launch)
        with authority_state_commit_lease(self.project_root):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                runtime, _ = self._run(connection, intent["runtime_id"])
                if int(time.time()) >= runtime["deadline_at"]:
                    raise Phase9RuntimeAuthorityError("runtime deadline exhausted before native launch release")
                stored = connection.execute("SELECT dispatch_intent_json FROM authority_production_phase9_runtime_attempts WHERE attempt_id=?", (intent["attempt_id"],)).fetchone()
                if stored is None or stored[0] != canonical_bytes(intent).decode():
                    raise Phase9RuntimeAuthorityError("launch does not own its committed intent")
                connection.execute("INSERT INTO authority_production_phase9_runtime_launches VALUES(?,?,?,?,?)",
                                   (intent["attempt_id"], pid, fields[19], canonical_bytes(launch).decode(), launch["launch_sha256"]))
                connection.commit()
                return launch
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def collect(self, runtime_id: str) -> dict:
        with authority_state_commit_lease(self.project_root):
            with isolated_authority_snapshot_ro(self.database) as connection:
                connection.execute("BEGIN")
                row, start = self._run(connection, runtime_id, require_current=False)
                attempts = [dict(value) for value in connection.execute(
                    "SELECT a.attempt_id,a.role,a.role_attempt,o.outcome,o.observation_sha256,x.selection_json "
                    "FROM authority_production_phase9_runtime_attempts a "
                    "LEFT JOIN authority_production_phase9_runtime_observations o ON o.attempt_id=a.attempt_id "
                    "LEFT JOIN authority_production_phase9_runtime_accepted_outputs x ON x.attempt_id=a.attempt_id "
                    "WHERE a.runtime_id=? ORDER BY a.role,a.role_attempt", (runtime_id,),
                )]
                terminal = connection.execute(
                    "SELECT terminal_json FROM authority_production_phase9_runtime_terminals WHERE runtime_id=?", (runtime_id,),
                ).fetchone()
                return {
                    "runtime_id": row["runtime_id"], "start": start,
                    "attempts": attempts,
                    "terminal": None if terminal is None else _strict_json(terminal[0].encode(), "runtime terminal"),
                    "automatic_redispatch": False, "delivery_capability": "DISABLED",
                }

    def finish(self, runtime_id: str) -> dict:
        """Derive a terminal from persisted observations; never from exit 0 alone.

        COMPLETED describes role execution only. It is not a forensic PASS and
        confers no delivery capability. Missing observations remain UNCERTAIN.
        """
        with authority_state_commit_lease(self.project_root):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row, start = self._run(connection, runtime_id, require_current=False)
                existing = connection.execute(
                    "SELECT terminal_json FROM authority_production_phase9_runtime_terminals WHERE runtime_id=?", (runtime_id,)
                ).fetchone()
                if existing:
                    return _strict_json(existing[0].encode(), "runtime terminal")
                attempts = [dict(value) for value in connection.execute(
                    "SELECT a.role,a.role_attempt,a.dispatch_intent_sha256,o.outcome,o.observation_sha256 "
                    "FROM authority_production_phase9_runtime_attempts a LEFT JOIN "
                    "authority_production_phase9_runtime_observations o ON o.attempt_id=a.attempt_id "
                    "WHERE a.runtime_id=? ORDER BY a.role,a.role_attempt", (runtime_id,)
                )]
                latest = {a["role"]: a["outcome"] for a in attempts}
                uncertain = any(a["outcome"] in (None, "UNCERTAIN") for a in attempts)
                completed = latest == {role: "SUCCEEDED" for role in start["target"]["roles"] + start["target"]["process_scope_actions"]}
                selected_roles = {r[0] for r in connection.execute("SELECT a.role FROM authority_production_phase9_runtime_accepted_outputs o JOIN authority_production_phase9_runtime_attempts a ON a.attempt_id=o.attempt_id WHERE a.runtime_id=? AND a.role_attempt=(SELECT MAX(b.role_attempt) FROM authority_production_phase9_runtime_attempts b WHERE b.runtime_id=a.runtime_id AND b.role=a.role)", (runtime_id,))}
                completed = completed and selected_roles == set(start["target"]["roles"])
                now = int(time.time())
                terminal = {
                    "schema": "authority-phase9-runtime-terminal-v1",
                    "runtime_id": runtime_id, "start_sha256": start["start_sha256"],
                    "status": "UNCERTAIN" if uncertain else (
                        "COMPLETED" if completed and now <= row["deadline_at"] else "BLOCKED"),
                    "completed_at": now, "attempts": attempts,
                    "formal_phase9_completed": False, "delivery_capability": "DISABLED",
                }
                terminal["terminal_sha256"] = canonical_sha256(terminal)
                connection.execute(
                    "INSERT INTO authority_production_phase9_runtime_terminals VALUES(?,?,?,?,?)",
                    (runtime_id, terminal["status"], now, canonical_bytes(terminal).decode(), terminal["terminal_sha256"])
                )
                connection.commit()
                return terminal
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def export_runtime_receipts(self, runtime_id, request, entry, outputs, evidence_root):
        """Materialize receipts from completed observations under a fresh entry.

        This consumes no new provider grant and never re-executes an attempt.
        Output bytes must equal the output observed on the actual launch path.
        The existing finalizer still validates protocol, grounding and verdicts.
        """
        from .phase9_replay_evidence import (
            _acceptance_provenance,
            authorize_formal_phase9_runtime_receipt, record_formal_phase9_runtime_receipt,
        )
        from .phase9_forensic_replay import (
            _dependency_fingerprint_sha256, _role_generation_id, _replay_coordinate_sha256,
            PHASE9_ROLE_PROVIDER_RECEIPT_SCHEMA, PHASE9_ROLE_PROCESS_RECEIPT_SCHEMA,
            PHASE9_PROCESS_SCOPE_RECEIPT_SCHEMA, PHASE9_RUNTIME_EVIDENCE_SCHEMA,
        )
        from .phase9_runtime_export import write_equal as _write_new, bind_equal, existing_record, stage
        state_sha, _ = _verify_entry_gate(entry, request, trusted_now=int(time.time()))
        if abs(request.occurred_at - int(time.time())) > 300:
            raise Phase9RuntimeAuthorityError("completion request is stale")
        root = Path(evidence_root)
        records = []
        role_rows = []
        scope_descriptors = {}
        with authority_state_commit_lease(self.project_root):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                run, start = self._run(connection, runtime_id)
                if state_sha != start["entry_state_receipt_sha256"]:
                    raise Phase9RuntimeAuthorityError("completion entry state differs from the acquired runtime entry state")
                _producer_live_precheck(self.finalizer, connection, request, {
                    "entry_state_receipt_sha256": state_sha,
                    "runtime_counts": {"active_descendant_count": 0, "pending_outbox_count": 0},
                })
                target = start["target"]
                if dispatch_target(request, packet_sha256=target["packet_sha256"],
                                   project_input_sha256=target["project_input_sha256"],
                                   model=target["model"], effort=target["effort"],
                                   timeout_seconds=target["timeout_seconds"],
                                   total_timeout_seconds=target["total_timeout_seconds"], provider=target["provider_identity"]) != target:
                    raise Phase9RuntimeAuthorityError("completion request differs from the authorized execution")
                terminal_row = connection.execute("SELECT terminal_status FROM authority_production_phase9_runtime_terminals WHERE runtime_id=?", (runtime_id,)).fetchone()
                if terminal_row is None or terminal_row[0] != "COMPLETED" or set(outputs) != set(target["roles"]):
                    raise Phase9RuntimeAuthorityError("runtime is not a completed three-role execution")
                packet_sha = target["packet_sha256"]
                for role in target["roles"]:
                    row = connection.execute(
                        "SELECT a.*,o.observation_json,o.observation_sha256,o.observed_at FROM authority_production_phase9_runtime_attempts a "
                        "JOIN authority_production_phase9_runtime_observations o ON o.attempt_id=a.attempt_id "
                        "WHERE a.runtime_id=? AND a.role=? ORDER BY a.role_attempt DESC LIMIT 1", (runtime_id, role)
                    ).fetchone()
                    observation = _strict_json(row["observation_json"].encode(), "runtime observation")
                    raw = bytes(outputs[role])
                    output_sha = hashlib.sha256(raw).hexdigest()
                    expected_output = {"sha256": output_sha, "byte_length": len(raw)}
                    if observation["observation"]["outcome"] != "SUCCEEDED" or expected_output not in observation["observation"]["outputs"].values():
                        raise Phase9RuntimeAuthorityError("role bytes are not the observed provider output")
                    selection_row = connection.execute("SELECT selection_json FROM authority_production_phase9_runtime_accepted_outputs WHERE attempt_id=?", (row["attempt_id"],)).fetchone()
                    if selection_row is None or _strict_json(selection_row[0].encode(), "accepted output")["selection"]["sha256"] != output_sha:
                        raise Phase9RuntimeAuthorityError("export differs from native accepted output")
                    output_path = f"roles/{role}.out"
                    _write_new(root / output_path, raw)
                    dependency = _dependency_fingerprint_sha256(request, receipt_kind="ROLE", logical_id=role, input_sha256=packet_sha)
                    generation = _role_generation_id(request, role=role, dependency_fingerprint_sha256=dependency)
                    common = {
                        "candidate": {"commit": request.source_commit, "tree": request.source_tree, "parent": request.source_parent},
                        "project_id": request.project_id, "workflow_id": request.workflow_id,
                        "run_generation": request.run_generation, "role": role,
                        "role_generation": generation, "inherited": False, "predecessor_role_generation": None,
                        "invocation_id": row["invocation_id"], "attempt_id": row["attempt_id"], "process_scope_id": row["process_scope_id"],
                        "packet_sha256": packet_sha, "output_path": output_path,
                        "output_byte_length": len(raw), "output_sha256": output_sha, "occurred_at": row["observed_at"],
                    }
                    previous = None
                    for kind, suffix, component, schema in (
                        ("ROLE_PROVIDER", "provider", "provider-runtime", PHASE9_ROLE_PROVIDER_RECEIPT_SCHEMA),
                        ("ROLE_PROCESS", "process", "role-process-supervisor", PHASE9_ROLE_PROCESS_RECEIPT_SCHEMA),
                    ):
                        body = {
                            "schema": schema, "receipt_id": row["attempt_id"] + ":" + suffix,
                            **common,
                            **_acceptance_provenance(request, receipt_kind=kind, dependency_kind="ROLE", logical_id=role,
                                component=component, input_sha256=packet_sha, dependency_input_sha256=packet_sha,
                                event_sequence=1 if previous is None else 2,
                                predecessor_event_id=None if previous is None else previous["event_id"],
                                predecessor_receipt_sha256=None if previous is None else previous["receipt_sha256"]),
                        }
                        if previous is None:
                            # This is the locally observed provider-call identity,
                            # not a claim about an unavailable upstream model ID.
                            body.update(provider_call_id=row["attempt_id"], provider_status="SUCCEEDED")
                        else:
                            body.update(process_kind="ROLE", process_status="COMPLETED", exit_code=0, provider_receipt=descriptor)
                        body["receipt_sha256"] = canonical_sha256(body)
                        encoded = canonical_bytes(body)
                        logical_path = f"receipts/roles/{role}.{suffix}.json"
                        descriptor = {"logical_path": logical_path, "byte_length": len(encoded),
                                      "raw_bytes_sha256": hashlib.sha256(encoded).hexdigest(), "receipt_sha256": body["receipt_sha256"]}
                        binding = {
                            "schema": "authority-phase9-runtime-receipt-binding-v1", "attempt_id": row["attempt_id"],
                            "receipt_kind": kind, "logical_id": role, **descriptor,
                            "dependency_fingerprint_sha256": dependency, "input_sha256": packet_sha,
                            "output_sha256": output_sha, "packet_sha256": packet_sha,
                            "replay_coordinate_sha256": _replay_coordinate_sha256(request),
                            "observation_sha256": row["observation_sha256"],
                        }
                        binding["binding_sha256"] = canonical_sha256(binding)
                        bind_equal(connection, binding)
                        _write_new(root / logical_path, encoded)
                        records.append((kind, role, logical_path, body, encoded))
                        previous = body
                    role_rows.append({"role": role, "role_generation": generation, "inherited": False,
                                      "packet_sha256": packet_sha, "output_path": output_path, "output_sha256": output_sha,
                                      "process_receipt": descriptor})
                for action in target["process_scope_actions"]:
                    row = connection.execute(
                        "SELECT a.*,o.observation_json,o.observation_sha256,o.observed_at FROM authority_production_phase9_runtime_attempts a "
                        "JOIN authority_production_phase9_runtime_observations o ON o.attempt_id=a.attempt_id "
                        "WHERE a.runtime_id=? AND a.role=?", (runtime_id, action)
                    ).fetchone()
                    observation = _strict_json(row["observation_json"].encode(), "process probe observation")["observation"]
                    if observation.get("probe_pass") is not True or observation.get("process_group_active_count") != 0:
                        raise Phase9RuntimeAuthorityError("process scope lacks an observed PASS with no live member")
                    input_sha = observation["process_identity_sha256"]
                    output_sha = observation["probe_result_sha256"]
                    body = {
                        "schema": PHASE9_PROCESS_SCOPE_RECEIPT_SCHEMA, "receipt_id": row["attempt_id"] + ":scope",
                        "candidate": {"commit": request.source_commit, "tree": request.source_tree, "parent": request.source_parent},
                        "project_id": request.project_id, "workflow_id": request.workflow_id, "run_generation": request.run_generation,
                        **_acceptance_provenance(request, receipt_kind="PROCESS_SCOPE", dependency_kind="PROCESS_SCOPE", logical_id=action,
                            component="process-scope-supervisor", input_sha256=input_sha, dependency_input_sha256=input_sha, event_sequence=1),
                        "output_sha256": output_sha, "action": action.upper(), "invocation_id": row["invocation_id"],
                        "attempt_id": row["attempt_id"], "process_scope_id": row["process_scope_id"], "scope_kind": "WORKER",
                        "process_identity_sha256": input_sha, "result": observation["probe_result"]["result"],
                        "active_descendant_count": observation["process_group_active_count"], "occurred_at": row["observed_at"],
                    }
                    body["receipt_sha256"] = canonical_sha256(body)
                    encoded = canonical_bytes(body)
                    path = f"receipts/process-scopes/{action}.json"
                    descriptor = {"logical_path": path, "byte_length": len(encoded), "raw_bytes_sha256": hashlib.sha256(encoded).hexdigest(),
                                  "receipt_sha256": body["receipt_sha256"]}
                    binding = {
                        "schema": "authority-phase9-runtime-receipt-binding-v1", "attempt_id": row["attempt_id"], "receipt_kind": "PROCESS_SCOPE",
                        "logical_id": action, **descriptor, "dependency_fingerprint_sha256": body["dependency_fingerprint_sha256"],
                        "input_sha256": input_sha, "output_sha256": output_sha, "packet_sha256": None,
                        "replay_coordinate_sha256": _replay_coordinate_sha256(request), "observation_sha256": row["observation_sha256"],
                    }
                    binding["binding_sha256"] = canonical_sha256(binding)
                    bind_equal(connection, binding)
                    _write_new(root / path, encoded)
                    records.append(("PROCESS_SCOPE", action, path, body, encoded))
                    scope_descriptors[action] = descriptor
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        stage(self, runtime_id, root, "BINDINGS_COMMITTED", [body["receipt_sha256"] for _, _, _, body, _ in records])
        for kind, role, path, body, raw in records:
            with authority_state_commit_lease(self.project_root):
                with isolated_authority_snapshot_ro(self.database) as connection:
                    existing = existing_record(connection, request, kind, role, path, body, raw)
            if existing is not None:
                stage(self, runtime_id, root, kind + ":" + role, {"record_sha256": existing})
                continue
            authorization_id, nonce, _ = authorize_formal_phase9_runtime_receipt(
                database=self.database, expected_source_fence_sha256=self.finalizer.expected_source_fence_sha256,
                request=request, receipt_kind=kind, logical_id=role, logical_path=path,
                invocation_id=body["invocation_id"], attempt_id=body["attempt_id"], process_scope_id=body["process_scope_id"],
                packet_sha256=body.get("packet_sha256"), dependency_fingerprint_sha256=body["dependency_fingerprint_sha256"], input_sha256=body["input_sha256"],
            )
            recorded = record_formal_phase9_runtime_receipt(
                database=self.database, expected_source_fence_sha256=self.finalizer.expected_source_fence_sha256,
                authorization_id=authorization_id, authorization_nonce=nonce, request=request,
                receipt_kind=kind, logical_id=role, logical_path=path, raw_bytes=raw,
            )
            stage(self, runtime_id, root, kind + ":" + role, {"record_sha256": recorded})
        roles = {"schema": "authority-phase9-role-evidence-v2", "roles": role_rows}
        outbox = {"schema": PHASE9_RUNTIME_EVIDENCE_SCHEMA, "precommit_external_launch_count": 0,
                  "committed_reclaim_count": 0, "pending_outbox_count": 0, "uncertain_automatic_resend_count": 0,
                  "active_descendant_count": 0, "process_scope_receipts": scope_descriptors}
        _write_new(root / "roles.json", canonical_bytes(roles))
        _write_new(root / "outbox_supervisor.json", canonical_bytes(outbox))
        stage(self, runtime_id, root, "RUNTIME_CONTROLS_WRITTEN", {"roles": roles, "outbox_supervisor": outbox})
        return {"roles": roles, "outbox_supervisor": outbox}
