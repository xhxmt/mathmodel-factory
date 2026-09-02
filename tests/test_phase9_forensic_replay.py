from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import pwd
import sqlite3

import pytest

from factory_core.canonical import canonical_bytes, canonical_sha256
from factory_core.phase9_config import (
    Phase9ConfigurationError,
    load_phase9_settings,
)
from factory_core.phase9_entry import verify_phase9_entry_gate
from factory_core.phase9_forensic_replay import (
    ABLATE_NO_JUDGE,
    CREATE,
    PHASE9_ACCEPTANCE_CASES,
    PHASE9_REPLAY_REQUEST_SCHEMA,
    PHASE9_START_AUTHORIZATION_SCHEMA,
    RESUME_TARGET,
    TECHNICAL,
    Phase9ForensicReplayConflict,
    Phase9ForensicReplayRequestV1,
    Phase9ForensicReplaySafetyError,
    Phase9ForensicReplayService,
    ReplayEvidenceFileV1,
    collect_phase9_forensic_replay_state,
    phase9_forensic_replay_request_from_dict,
    preflight_phase9_forensic_replay,
)
from factory_core.phase9_run_generation import (
    ROTATE as ROTATE_GENERATION,
    Phase9RunGenerationService,
)
from scripts.phase9_forensic_replay import main as replay_main
from tests.test_phase9_entry_gate import (
    _entry_authorization,
    _p0_receipts,
    _ready_fixture,
    _source_repository,
)


PHASE9_TABLES = (
    "authority_production_phase9_replays",
    "authority_production_phase9_replay_events",
    "authority_production_phase9_terminal_receipts",
    "authority_production_phase9_replay_idempotency",
    "authority_production_phase9_replay_current",
)


def _ready_gate(
    input_root, request, candidate, state,
    p0_root, p0_root_sha, p0_receipts,
):
    source = {
        "mode": "GIT",
        "candidate": candidate.as_dict(),
        "verified_tree": candidate.tree,
        "worktree_clean": True,
    }
    result = verify_phase9_entry_gate(
        state=state,
        source_verification=source,
        p0_receipts=p0_receipts,
        p0_evidence_root=p0_root,
        p0_evidence_root_sha256=p0_root_sha,
        operator_authorization=_entry_authorization(
            candidate, request, p0_root_sha
        ),
        official_input_manifest=request.official_inputs.as_dict(),
        official_input_root=input_root,
        execution_context=request.execution_context.as_dict(),
        evaluated_at=2100,
        trusted_now=2100,
    )
    assert result["status"] == "READY"
    return result


def _authorization(request, *, occurred_at=2200):
    body = {
        "schema": PHASE9_START_AUTHORIZATION_SCHEMA,
        "authorization_id": "phase9-start-test-fixture",
        "authorization_mechanism": "CONTROLLED_OS_ACCOUNT",
        "authorized": True,
        "operator_uid": os.geteuid(),
        "operator_account": pwd.getpwuid(os.geteuid()).pw_name,
        "operation": "PHASE9_A_FORENSIC_REPLAY",
        "project_id": request.project_id,
        "workflow_id": request.workflow_id,
        "run_generation": request.run_generation,
        "source_commit": request.source_commit,
        "entry_gate_result_sha256": request.entry_gate_result_sha256,
        "issued_at": occurred_at - 100,
        "expires_at": occurred_at + 100,
        "authorization_scope": {
            "phase9_a_forensic_replay": True,
            "provider_or_network": False,
            "production_outbox_or_delivery": False,
            "release": False,
            "deployment": False,
            "migration": False,
            "cutover": False,
        },
    }
    body["authorization_receipt_sha256"] = canonical_sha256(body)
    return body


def _write_evidence(
    root: Path,
    *,
    gate: dict[str, object],
    request_fields: dict[str, object],
    mode: str = TECHNICAL,
    missing_claim: bool = False,
    missing_dispatch_count: int = 0,
    bad_verdict: bool = False,
    bad_snapshot: bool = False,
    delivery_enabled: bool = False,
):
    root.mkdir()
    packet_raw = b"phase9 deterministic packet bytes\n"
    packet_sha = hashlib.sha256(packet_raw).hexdigest()
    required_claims = ["claim-a", "claim-b"]
    present_claims = ["claim-a"] if missing_claim else required_claims
    packet = {
        "schema": "authority-phase9-packet-evidence-v1",
        "required_claims": required_claims,
        "present_claims": present_claims,
        "packet_path": "payload/packet.bin",
        "packet_sha256": packet_sha,
        "dispatch_count": missing_dispatch_count if missing_claim else 0,
    }
    if mode == TECHNICAL:
        roles = []
        role_layers = {}
        for role in ("execution", "math", "paper"):
            output_path = f"roles/{role}.out"
            output = f"{role} independently generated output\n".encode()
            (root / "roles").mkdir(exist_ok=True)
            (root / output_path).write_bytes(output)
            roles.append(
                {
                    "role": role,
                    "role_generation": (
                        f"{role}-generation-{request_fields['run_generation'][-12:]}"
                    ),
                    "inherited": False,
                    "packet_sha256": packet_sha,
                    "output_path": output_path,
                    "output_sha256": hashlib.sha256(output).hexdigest(),
                    "process_receipt_sha256": hashlib.sha256(
                        f"process:{role}".encode()
                    ).hexdigest(),
                }
            )
            role_layers[role] = {
                "raw": "PASS",
                "protocol": "PASS",
                "grounding": "PASS",
                "effective": "PASS",
            }
        if bad_verdict:
            role_layers["math"]["effective"] = "FAIL"
        verdict = {
            "schema": "authority-phase9-verdict-evidence-v1",
            "roles": role_layers,
            "effective_verdict": "PASS",
            "exit_code": 0,
        }
        terminal = {
            "terminal_reason": "FORENSIC_REPLAY_COMPLETED",
            "requested_resume_target": RESUME_TARGET,
            "effective_verdict": "PASS",
            "exit_code": 0,
        }
    else:
        roles = []
        verdict = {
            "schema": "authority-phase9-verdict-evidence-v1",
            "roles": {},
            "effective_verdict": "NOT_APPLICABLE",
            "exit_code": 17,
        }
        terminal = {
            "terminal_reason": "PERMANENT_ABLATION_NO_DELIVERY",
            "requested_resume_target": RESUME_TARGET,
            "effective_verdict": "NOT_APPLICABLE",
            "exit_code": 17,
        }
    coordinate = {
        "project_id": request_fields["project_id"],
        "project_revision": request_fields["project_revision"],
        "run_generation": request_fields["run_generation"],
    }
    snapshot = {
        "schema": "authority-phase9-snapshot-evidence-v1",
        "coordinate": coordinate,
        "sections": [
            {
                "section": "artifacts",
                "coordinate": coordinate,
                "read_failed": bad_snapshot,
                "read_status": "GAP" if bad_snapshot else "AVAILABLE",
            },
            {
                "section": "workflow",
                "coordinate": coordinate,
                "read_failed": False,
                "read_status": "AVAILABLE",
            },
        ],
    }
    runtime = {
        "schema": "authority-phase9-runtime-safety-evidence-v1",
        "precommit_external_launch_count": 0,
        "committed_reclaim_count": 1,
        "pending_outbox_count": 0,
        "uncertain_automatic_resend_count": 0,
        "active_descendant_count": 0,
        "process_scope_receipts": {
            name: hashlib.sha256(f"scope:{name}".encode()).hexdigest()
            for name in ("failed", "kill", "pause")
        },
    }
    acceptance = {
        "schema": "authority-phase9-acceptance-evidence-v1",
        "cases": [
            {
                "case_id": case,
                "result": "PASS",
                "receipt_sha256": hashlib.sha256(f"case:{case}".encode()).hexdigest(),
            }
            for case in PHASE9_ACCEPTANCE_CASES
        ],
        "delivery": {
            "delivery_capability": "ENABLED" if delivery_enabled else "DISABLED",
            "release_created": False,
            "final_acceptance_created": False,
            "final_submission_created": False,
            "reusable": False,
            "delivery_override_applied": False,
        },
        "terminal": terminal,
    }
    controls = {
        "entry_gate.json": gate,
        "start_authorization.json": _authorization(
            type("Request", (), request_fields)(), occurred_at=request_fields["occurred_at"]
        ),
        "packet.json": packet,
        "roles.json": {"schema": "authority-phase9-role-evidence-v1", "roles": roles},
        "verdict.json": verdict,
        "snapshot.json": snapshot,
        "outbox_supervisor.json": runtime,
        "acceptance.json": acceptance,
    }
    (root / "payload").mkdir()
    (root / "payload/packet.bin").write_bytes(packet_raw)
    for name, value in controls.items():
        (root / name).write_bytes(canonical_bytes(value))
    return controls


def _request_for_evidence(
    root: Path,
    generation_request,
    state,
    gate,
    *,
    mode=TECHNICAL,
    **evidence_options,
):
    fields = {
        "project_id": generation_request.project_id,
        "workflow_id": generation_request.workflow_id,
        "project_revision": generation_request.project_revision,
        "run_generation": state.run_generation,
        "source_commit": generation_request.source.source_commit,
        "entry_gate_result_sha256": gate["gate_result_sha256"],
        "occurred_at": 2200,
    }
    _write_evidence(
        root, gate=gate, request_fields=fields, mode=mode, **evidence_options
    )
    files = []
    paths = [item for item in root.rglob("*") if item.is_file()]
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        raw = path.read_bytes()
        files.append(
            ReplayEvidenceFileV1(
                path.relative_to(root).as_posix(),
                len(raw),
                hashlib.sha256(raw).hexdigest(),
            )
        )
    return Phase9ForensicReplayRequestV1(
        PHASE9_REPLAY_REQUEST_SCHEMA,
        "phase9-replay-key-1",
        CREATE,
        generation_request.project_id,
        generation_request.workflow_id,
        generation_request.project_revision,
        generation_request.project_generation,
        state.run_generation,
        state.creation_receipt_sha256,
        None,
        None,
        mode,
        RESUME_TARGET,
        "DISABLED",
        generation_request.source.source_commit,
        generation_request.source.source_tree,
        generation_request.source.source_parent,
        gate["gate_result_sha256"],
        tuple(files),
        2200,
    )


def _fixture(tmp_path, *, mode=TECHNICAL, **evidence_options):
    (
        foundation, input_root, generation_request, candidate, state,
        p0_root, p0_root_sha, p0_receipts,
    ) = _ready_fixture(tmp_path)
    gate = _ready_gate(
        input_root, generation_request, candidate, state,
        p0_root, p0_root_sha, p0_receipts,
    )
    root = tmp_path / "phase9-replay-evidence"
    request = _request_for_evidence(
        root, generation_request, state, gate, mode=mode, **evidence_options
    )
    service = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        evidence_root=root,
        clock=lambda: request.occurred_at,
    )
    return foundation, root, request, service


def _counts(database: Path):
    connection = sqlite3.connect(database)
    try:
        return {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in PHASE9_TABLES
        }
    finally:
        connection.close()


def _reindex_evidence(
    root: Path, request: Phase9ForensicReplayRequestV1
) -> Phase9ForensicReplayRequestV1:
    files = []
    paths = [item for item in root.rglob("*") if item.is_file()]
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        raw = path.read_bytes()
        files.append(
            ReplayEvidenceFileV1(
                path.relative_to(root).as_posix(),
                len(raw),
                hashlib.sha256(raw).hexdigest(),
            )
        )
    return replace(request, evidence_files=tuple(files))


def test_configuration_is_default_off_and_does_not_parse_paths():
    settings = load_phase9_settings(
        {
            "PHASE9_ENABLED": "false",
            "PHASE9_AUTHORITY_DB_FILE": "relative-would-be-invalid",
        }
    )
    assert settings.enabled is False
    assert settings.authority_database is None
    with pytest.raises(Phase9ConfigurationError, match="lowercase SHA-256"):
        load_phase9_settings({"PHASE9_ENABLED": "true"})


def test_preflight_atomic_execute_exact_replay_and_read_only_collection(tmp_path):
    foundation, root, request, service = _fixture(tmp_path)
    preflight = preflight_phase9_forensic_replay(
        request, evidence_root=root, trusted_now=request.occurred_at
    )
    assert preflight["status"] == "READY"
    first = service.execute(request)
    replay = service.execute(request)
    assert first.replayed is False
    assert replay == replace(first, replayed=True)
    assert first.terminal_reason == "FORENSIC_REPLAY_COMPLETED"
    assert _counts(foundation.database) == {
        "authority_production_phase9_replays": 1,
        "authority_production_phase9_replay_events": 6,
        "authority_production_phase9_terminal_receipts": 1,
        "authority_production_phase9_replay_idempotency": 1,
        "authority_production_phase9_replay_current": 1,
    }
    before = hashlib.sha256(foundation.database.read_bytes()).hexdigest()
    state = collect_phase9_forensic_replay_state(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        workflow_id=request.workflow_id,
    )
    after = hashlib.sha256(foundation.database.read_bytes()).hexdigest()
    assert state["status"] == "COMPLETED"
    assert state["terminal_receipt_sha256"] == first.receipt_sha256
    assert state["event_count"] == 6
    assert before == after


def test_same_idempotency_key_with_different_request_conflicts(tmp_path):
    _foundation, _root, request, service = _fixture(tmp_path)
    service.execute(request)
    changed = replace(request, occurred_at=request.occurred_at + 1)
    with pytest.raises(Phase9ForensicReplayConflict, match="idempotency key"):
        service.execute(changed)


def test_start_authorization_uses_trusted_clock_and_exact_gate_result(tmp_path):
    _foundation, root, request, _service = _fixture(tmp_path)
    authorization_path = root / "start_authorization.json"

    expired = _authorization(request, occurred_at=request.occurred_at)
    expired["issued_at"] = request.occurred_at - 200
    expired["expires_at"] = request.occurred_at + 50
    expired.pop("authorization_receipt_sha256")
    expired["authorization_receipt_sha256"] = canonical_sha256(expired)
    authorization_path.write_bytes(canonical_bytes(expired))
    expired_request = _reindex_evidence(root, request)
    result = preflight_phase9_forensic_replay(
        expired_request,
        evidence_root=root,
        trusted_now=request.occurred_at + 100,
    )
    assert result["status"] == "BLOCKED"
    assert "not valid at trusted current time" in result["blockers"][0]["detail"]

    wrong_gate = _authorization(request, occurred_at=request.occurred_at)
    wrong_gate["entry_gate_result_sha256"] = "a" * 64
    wrong_gate.pop("authorization_receipt_sha256")
    wrong_gate["authorization_receipt_sha256"] = canonical_sha256(wrong_gate)
    authorization_path.write_bytes(canonical_bytes(wrong_gate))
    wrong_gate_request = _reindex_evidence(root, request)
    result = preflight_phase9_forensic_replay(
        wrong_gate_request,
        evidence_root=root,
        trusted_now=request.occurred_at,
    )
    assert result["status"] == "BLOCKED"
    assert "start authorization coordinate differs" in result["blockers"][0]["detail"]


@pytest.mark.parametrize(
    "checkpoint",
    [
        "after_replay", "after_event_1", "after_event_3", "after_event_6",
        "after_receipt", "after_current_pointer", "before_commit",
    ],
)
def test_fault_injection_rolls_back_every_phase9_table(tmp_path, checkpoint):
    foundation, root, request, _service = _fixture(tmp_path)

    def fault(name):
        if name == checkpoint:
            raise RuntimeError(f"fault:{name}")

    service = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        evidence_root=root,
        fault_hook=fault,
        clock=lambda: request.occurred_at,
    )
    with pytest.raises(RuntimeError, match="fault"):
        service.execute(request)
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


def test_missing_claims_are_blocked_and_dispatch_must_be_zero(tmp_path):
    _foundation, root, request, service = _fixture(tmp_path, missing_claim=True)
    preflight = preflight_phase9_forensic_replay(
        request, evidence_root=root, trusted_now=request.occurred_at
    )
    assert preflight["status"] == "BLOCKED"
    assert [item["code"] for item in preflight["blockers"]] == ["MISSING_PACKET_CLAIMS"]
    with pytest.raises(Phase9ForensicReplaySafetyError, match="BLOCKED"):
        service.execute(request)

    other = tmp_path / "other"
    foundation, root2, request2, _ = _fixture(
        other, missing_claim=True, missing_dispatch_count=1
    )
    result = preflight_phase9_forensic_replay(
        request2, evidence_root=root2, trusted_now=request2.occurred_at
    )
    assert [item["code"] for item in result["blockers"]] == [
        "DISPATCH_WITH_MISSING_CLAIMS", "MISSING_PACKET_CLAIMS"
    ]
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


@pytest.mark.parametrize(
    ("option", "message"),
    [
        ({"bad_verdict": True}, "contradictory effective role verdict"),
        ({"bad_snapshot": True}, "snapshot read failure must be ERROR"),
    ],
)
def test_verdict_and_snapshot_fail_closed(tmp_path, option, message):
    _foundation, root, request, service = _fixture(tmp_path, **option)
    result = preflight_phase9_forensic_replay(
        request, evidence_root=root, trusted_now=request.occurred_at
    )
    assert result["status"] == "BLOCKED"
    assert message in result["blockers"][0]["detail"]
    with pytest.raises(Phase9ForensicReplaySafetyError, match=message):
        service.execute(request)


def test_ablation_is_typed_nonzero_nonreusable_and_delivery_disabled(tmp_path):
    foundation, root, request, service = _fixture(tmp_path, mode=ABLATE_NO_JUDGE)
    assert preflight_phase9_forensic_replay(
        request, evidence_root=root, trusted_now=request.occurred_at
    )["status"] == "READY"
    result = service.execute(request)
    assert result.terminal_reason == "PERMANENT_ABLATION_NO_DELIVERY"
    assert result.effective_verdict == "NOT_APPLICABLE"
    state = collect_phase9_forensic_replay_state(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        workflow_id=request.workflow_id,
    )
    assert state["exit_code"] == 17
    assert state["delivery_capability"] == "DISABLED"


def test_delivery_evidence_cannot_enable_or_create_release(tmp_path):
    foundation, root, request, service = _fixture(tmp_path, delivery_enabled=True)
    result = preflight_phase9_forensic_replay(
        request, evidence_root=root, trusted_now=request.occurred_at
    )
    assert result["status"] == "BLOCKED"
    assert result["blockers"] == [
        {"code": "DELIVERY_FENCE", "detail": "delivery evidence differs"}
    ]
    with pytest.raises(Phase9ForensicReplaySafetyError, match="BLOCKED"):
        service.execute(request)
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


def test_evidence_toctou_before_commit_rolls_back(tmp_path):
    foundation, root, request, _service = _fixture(tmp_path)
    target = root / "payload/packet.bin"

    def mutate(name):
        if name == "after_receipt":
            target.write_bytes(b"changed")

    service = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(), evidence_root=root,
        fault_hook=mutate,
        clock=lambda: request.occurred_at,
    )
    with pytest.raises(Phase9ForensicReplaySafetyError, match="evidence bytes differ"):
        service.execute(request)
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


def test_current_pointer_guards_reject_direct_update_and_delete(tmp_path):
    foundation, _root, request, service = _fixture(tmp_path)
    service.execute(request)
    connection = sqlite3.connect(foundation.database)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="succession graph"):
            connection.execute(
                "UPDATE authority_production_phase9_replay_current "
                "SET replay_id='invented', run_generation='invented'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute("DELETE FROM authority_production_phase9_replay_current")
    finally:
        connection.rollback()
        connection.close()


def test_generation_rotation_cas_switches_phase9_current_pointer(tmp_path):
    (
        foundation, input_root, generation_request, candidate, first_state,
        p0_root, p0_root_sha, p0_receipts,
    ) = _ready_fixture(tmp_path)
    first_gate = _ready_gate(
        input_root, generation_request, candidate, first_state,
        p0_root, p0_root_sha, p0_receipts,
    )
    first_root = tmp_path / "first-replay"
    first_request = _request_for_evidence(
        first_root, generation_request, first_state, first_gate
    )
    first_service = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(), evidence_root=first_root,
        clock=lambda: first_request.occurred_at,
    )
    first = first_service.execute(first_request)

    rotation_authorization = replace(
        generation_request.operator_authorization,
        authorization_id="phase9-generation-rotation-authorization",
        operation_kind=ROTATE_GENERATION,
    )
    rotation_request = replace(
        generation_request,
        idempotency_key="phase9-entry-generation-rotation-key",
        operation_kind=ROTATE_GENERATION,
        predecessor_run_generation=first_state.run_generation,
        predecessor_creation_receipt_sha256=first_state.creation_receipt_sha256,
        operator_authorization=rotation_authorization,
        occurred_at=2300,
    )
    context = tmp_path / "execution-context.json"
    rotation = Phase9RunGenerationService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(), official_input_root=input_root,
        execution_context_receipt_path=context,
        clock=lambda: 2300,
    ).create_or_rotate(rotation_request)
    from factory_core.phase9_entry import collect_phase9_entry_state

    second_state = collect_phase9_entry_state(
        foundation.database,
        workflow_id=rotation_request.workflow_id,
        candidate=candidate,
    )
    assert second_state.run_generation == rotation.run_generation
    second_gate = _ready_gate(
        input_root, rotation_request, candidate, second_state,
        p0_root, p0_root_sha, p0_receipts,
    )
    second_root = tmp_path / "second-replay"
    second_request = _request_for_evidence(
        second_root, rotation_request, second_state, second_gate
    )
    second_request = replace(
        second_request,
        idempotency_key="phase9-replay-key-2",
        operation_kind=ROTATE_GENERATION,
        predecessor_replay_id=first.replay_id,
        predecessor_terminal_receipt_sha256=first.receipt_sha256,
        occurred_at=2400,
    )
    # The controlled-account receipt remains valid at the rotated occurrence.
    auth_path = second_root / "start_authorization.json"
    auth = _authorization(second_request, occurred_at=2400)
    auth_path.write_bytes(canonical_bytes(auth))
    files = []
    paths = [item for item in second_root.rglob("*") if item.is_file()]
    for path in sorted(paths, key=lambda item: item.relative_to(second_root).as_posix()):
        raw = path.read_bytes()
        files.append(
            ReplayEvidenceFileV1(
                path.relative_to(second_root).as_posix(), len(raw),
                hashlib.sha256(raw).hexdigest(),
            )
        )
    second_request = replace(second_request, evidence_files=tuple(files))
    second_service = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(), evidence_root=second_root,
        clock=lambda: second_request.occurred_at,
    )
    second = second_service.execute(second_request)
    state = collect_phase9_forensic_replay_state(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        workflow_id=second_request.workflow_id,
    )
    assert state["replay_id"] == second.replay_id
    assert state["terminal_receipt_sha256"] == second.receipt_sha256
    assert _counts(foundation.database)["authority_production_phase9_replays"] == 2


def test_symlinked_evidence_is_blocked_before_database_mutation(tmp_path):
    foundation, root, request, service = _fixture(tmp_path)
    packet = root / "payload/packet.bin"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(packet.read_bytes())
    packet.unlink()
    packet.symlink_to(outside)
    result = preflight_phase9_forensic_replay(
        request, evidence_root=root, trusted_now=request.occurred_at
    )
    assert result["status"] == "BLOCKED"
    assert "symlink" in result["blockers"][0]["detail"]
    with pytest.raises(Phase9ForensicReplaySafetyError, match="symlink"):
        service.execute(request)
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


def test_request_round_trip_is_identity_stable(tmp_path):
    _foundation, _root, request, _service = _fixture(tmp_path)
    decoded = phase9_forensic_replay_request_from_dict(request.as_dict())
    assert decoded == request
    assert decoded.request_sha256 == request.request_sha256


def test_cli_disabled_returns_before_missing_request_or_configured_paths(monkeypatch, capsys):
    monkeypatch.delenv("PHASE9_ENABLED", raising=False)
    monkeypatch.setenv("PHASE9_AUTHORITY_DB_FILE", "relative-invalid")
    code = replay_main(["execute", "--request", "/definitely/missing.json", "--confirm"])
    captured = capsys.readouterr()
    assert code == 2
    assert '"code":"PHASE9_DISABLED"' in captured.out
    assert captured.err == ""
