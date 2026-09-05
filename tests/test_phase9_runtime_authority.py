"""Isolated Authority lifecycle tests; no formal/independent evidence is issued."""
import json
import os
import pwd
import sqlite3
import sys
from dataclasses import replace

import pytest

from factory_core.canonical import canonical_sha256
from factory_core.phase9_runtime_authority import (
    GRANT_SCHEMA, SCOPE, Phase9RuntimeAuthority, Phase9RuntimeAuthorityError,
    dispatch_target, validate_dispatch_grant,
)
from tests.test_phase9_forensic_replay import _fixture


def _setup(tmp_path, monkeypatch):
    foundation, root, request, finalizer = _fixture(tmp_path, attest=False)
    now = [request.occurred_at]
    monkeypatch.setattr("time.time", lambda: now[0])
    authority = Phase9RuntimeAuthority(finalizer)
    target = dispatch_target(request, packet_sha256="1" * 64,
                             project_input_sha256="2" * 64, model="gpt-6-astra",
                             effort="medium", timeout_seconds=900, total_timeout_seconds=3600)
    grant = dict(schema=GRANT_SCHEMA, grant_id="ISOLATED-TEST-ONLY",
                 nonce_sha256="3" * 64, target_sha256=canonical_sha256(target),
                 authorized=True, authorization_mechanism="CONTROLLED_OS_ACCOUNT",
                 authorization_evidence_sha256="4" * 64, operator_uid=os.geteuid(),
                 operator_account=pwd.getpwuid(os.geteuid()).pw_name,
                 issued_at=now[0], expires_at=now[0] + 300, scope=SCOPE)
    grant["grant_sha256"] = canonical_sha256(grant)
    entry = json.loads((root / "entry_gate.json").read_text())
    return authority, request, target, grant, entry, now


def test_stable_runtime_target_is_independent_of_completion_time_and_new_entry(tmp_path, monkeypatch):
    _, request, target, _, _, _ = _setup(tmp_path, monkeypatch)
    later = replace(request, occurred_at=request.occurred_at + 1800,
                    entry_gate_result_sha256="9" * 64)
    assert dispatch_target(later, packet_sha256="1" * 64,
                           project_input_sha256="2" * 64, model="gpt-6-astra",
                           effort="medium", timeout_seconds=900, total_timeout_seconds=3600) == target


def test_grant_expiry_and_scope_are_not_runtime_deadline(tmp_path, monkeypatch):
    authority, request, target, grant, entry, now = _setup(tmp_path, monkeypatch)
    start = authority.start(request, entry, target, grant)
    now[0] += 301
    with pytest.raises(Phase9RuntimeAuthorityError, match="expired"):
        validate_dispatch_grant(grant, target, now=now[0])
    intent = authority.reserve_attempt(start["runtime_id"], "math", "5" * 64)
    assert intent["committed_at"] == now[0]
    assert authority.collect(start["runtime_id"])["attempts"][0]["outcome"] is None


def test_unobserved_dispatch_and_generation_reuse_cannot_redispatch(tmp_path, monkeypatch):
    authority, request, target, grant, entry, _ = _setup(tmp_path, monkeypatch)
    start = authority.start(request, entry, target, grant)
    authority.reserve_attempt(start["runtime_id"], "math", "5" * 64)
    with pytest.raises(Phase9RuntimeAuthorityError, match="unobserved"):
        authority.reserve_attempt(start["runtime_id"], "paper", "6" * 64)
    with pytest.raises(Phase9RuntimeAuthorityError, match="consumed"):
        authority.start(request, entry, target, grant)
    terminal = authority.finish(start["runtime_id"])
    assert terminal["status"] == "UNCERTAIN"
    assert terminal["formal_phase9_completed"] is False
    assert authority.finish(start["runtime_id"]) == terminal


def test_runtime_tables_are_immutable_and_direct_writers_cannot_insert(tmp_path, monkeypatch):
    authority, request, target, grant, entry, _ = _setup(tmp_path, monkeypatch)
    start = authority.start(request, entry, target, grant)
    connection = sqlite3.connect(authority.database)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM authority_production_phase9_runtime_runs")
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("UPDATE authority_production_phase9_runtime_runs SET deadline_at=deadline_at+100")
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("INSERT INTO authority_production_phase9_runtime_terminals VALUES(?,?,?,?,?)",
                               (start["runtime_id"], "COMPLETED", 1000, "{}", "0" * 64))
    finally:
        connection.close()


def test_deadline_exhaustion_and_delivery_scope_reject_before_dispatch(tmp_path, monkeypatch):
    authority, request, target, grant, entry, now = _setup(tmp_path, monkeypatch)
    start = authority.start(request, entry, target, grant)
    now[0] = start["deadline_at"]
    with pytest.raises(Phase9RuntimeAuthorityError, match="deadline"):
        authority.reserve_attempt(start["runtime_id"], "math", "5" * 64)
    assert authority.finish(start["runtime_id"])["status"] == "BLOCKED"


def test_actual_test_process_completion_reaches_runtime_receipt_writer(tmp_path, monkeypatch):
    """Real OS processes, synthetic provider text, isolated DB: NOT formal evidence."""
    from factory_core.phase9_runtime import _ObservedDispatcher
    from factory_core.adapters.models.backends import CodexCliBackend, ModelRequest

    authority, request, target, grant, entry, _ = _setup(tmp_path, monkeypatch)
    start = authority.start(request, entry, target, grant)
    calls = tmp_path / "observed-test-processes"
    calls.mkdir()
    scratch = tmp_path / "provider-scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    dispatcher = _ObservedDispatcher(authority.finalizer.source_repository, calls, lambda: None,
                                     "gpt-6-astra", "medium", authority=authority, runtime_id=start["runtime_id"])

    def execute_test_process(self, configured):
        return self._run(configured, [sys.executable, "-B", "-c",
                         "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('SYNTHETIC TEST PROVIDER OUTPUT\\n')",
                         str(configured.output_file)], "synthetic_test_provider")

    monkeypatch.setattr(CodexCliBackend, "execute", execute_test_process)
    outputs = {}
    for role in target["roles"]:
        output = authority.project_root / (role + ".md")
        result = dispatcher.execute(ModelRequest(authority.project_root, 13, 1, "SYNTHETIC TEST PROMPT", 10, 10, output_file=output), step_key=13, defaults=())
        assert result.returncode == 0
        outputs[role] = output.read_bytes()
    from factory_core.phase9_runtime_probes import run_process_scope_probes
    run_process_scope_probes(authority, start["runtime_id"], tmp_path / "scope-probes")
    assert authority.finish(start["runtime_id"])["status"] == "COMPLETED"
    exported = authority.export_runtime_receipts(start["runtime_id"], request, entry, outputs, tmp_path / "synthetic-export")
    assert len(exported["roles"]["roles"]) == 3
    connection = sqlite3.connect(authority.database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM authority_production_phase9_replay_runtime_records").fetchone()[0] == 9
        assert connection.execute("SELECT COUNT(*) FROM authority_production_phase9_runtime_launches").fetchone()[0] == 6
        assert connection.execute("SELECT COUNT(*) FROM authority_commands WHERE command_type='PHASE9_A_RUNTIME_DISPATCH'").fetchone()[0] == 6
    finally:
        connection.close()


def test_formal_cli_is_default_off_before_touching_supplied_paths(monkeypatch, capsys):
    from scripts.phase9_authorized_runtime import main
    monkeypatch.setenv("PHASE9_ENABLED", "false")
    monkeypatch.setenv("PHASE9_AUTHORITY_DB_FILE", "not-an-absolute-path")
    assert main(["execute", "--grant", "/missing/grant.json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "PHASE9_DISABLED"
    assert result["model_dispatch_count"] == 0


def test_success_cannot_be_recorded_without_an_observed_os_launch(tmp_path, monkeypatch):
    authority, request, target, grant, entry, _ = _setup(tmp_path, monkeypatch)
    start = authority.start(request, entry, target, grant)
    intent = authority.reserve_attempt(start["runtime_id"], "math", "5" * 64)
    with pytest.raises(Phase9RuntimeAuthorityError, match="owned launch"):
        authority.observe(intent, {"outcome": "SUCCEEDED", "exit_code": 0,
                                   "outputs": {"output": {"sha256": "6" * 64, "byte_length": 1}},
                                   "process_group_active_count": 0, "response_model_identity": "unavailable"})
    assert authority.finish(start["runtime_id"])["status"] == "UNCERTAIN"
