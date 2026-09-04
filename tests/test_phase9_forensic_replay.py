from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import pwd
import sqlite3
import subprocess
import threading

import pytest

from factory_core import phase9_forensic_replay as replay_module
from factory_core import phase9_replay_evidence as replay_evidence_module
from factory_core.canonical import canonical_bytes, canonical_sha256
from factory_core.phase9_config import (
    Phase9ConfigurationError,
    load_phase9_settings,
)
from factory_core.phase9_entry import p0_evidence_root_sha256, verify_phase9_entry_gate
from factory_core.phase9_forensic_replay import (
    ABLATE_NO_JUDGE,
    CREATE,
    PHASE9_ACCEPTANCE_CASES,
    PHASE9_ACCEPTANCE_CASE_RECEIPT_SCHEMA,
    PHASE9_ACCEPTANCE_COMMAND_SCHEMA,
    PHASE9_ACCEPTANCE_ENVIRONMENT_SCHEMA,
    PHASE9_ACCEPTANCE_EVIDENCE_SCHEMA,
    PHASE9_ACCEPTANCE_PYTHON_SCHEMA,
    PHASE9_ACCEPTANCE_RESULT_SCHEMA,
    PHASE9_ACCEPTANCE_SPEC_SHA256,
    PHASE9_ACCEPTANCE_TEST_NODES,
    PHASE9_PROCESS_SCOPE_RECEIPT_SCHEMA,
    PHASE9_REPLAY_REQUEST_SCHEMA,
    PHASE9_ROLE_EVIDENCE_SCHEMA,
    PHASE9_ROLE_PROCESS_RECEIPT_SCHEMA,
    PHASE9_ROLE_PROVIDER_RECEIPT_SCHEMA,
    PHASE9_RUNTIME_EVIDENCE_SCHEMA,
    PHASE9_START_AUTHORIZATION_SCHEMA,
    phase9_replay_evidence_payload_set_sha256,
    phase9_start_authorization_target_sha256,
    RESUME_TARGET,
    TECHNICAL,
    Phase9ForensicReplayConflict,
    Phase9ForensicReplayError,
    Phase9ForensicReplayRequestV1,
    Phase9ForensicReplayResult,
    Phase9ForensicReplaySafetyError,
    Phase9ForensicReplayService,
    ReplayEvidenceFileV1,
    collect_phase9_forensic_replay_state,
    phase9_forensic_replay_request_from_dict,
    preflight_phase9_forensic_replay,
    validate_current_phase9_completed_replay_in_transaction,
)
from factory_core.phase9_run_generation import (
    ROTATE as ROTATE_GENERATION,
    Phase9RunGenerationSafetyError,
    Phase9RunGenerationService,
    read_current_git_source_snapshot,
)
from factory_core.phase9_replay_evidence import (
    authorize_formal_phase9_runtime_receipt,
    produce_formal_phase9_replay_evidence,
    record_formal_phase9_runtime_receipt,
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
    "authority_production_phase9_evidence_receipts",
    "authority_production_phase9_gate_consumptions",
)
EXPECTED_PHASE9_ACCEPTANCE_CASES = (
    "AC-DEL-001",
    "AC-DEL-002",
    "AC-OUT-001",
    "AC-OUT-002",
    "AC-OUT-004",
    "AC-PACKET-001",
    "AC-PACKET-002",
    "AC-PACKET-003",
    "AC-RUN4-001",
    "AC-RUN4-002",
    "AC-SNAP-001",
    "AC-SNAP-002",
    "AC-SUP-001",
    "AC-SUP-002",
    "AC-SUP-004",
    "AC-VERDICT-001",
    "AC-VERDICT-003",
)
EXPECTED_ACCEPTANCE_BLOCKED_ENVIRONMENT_NAMES = [
    "ANTHROPIC_API_KEY",
    "AUTHORITY_DATABASE",
    "AUTHORITY_DB",
    "CLOUD_SOLVER_URL",
    "DATABASE_URL",
    "DEPLOYMENT_ENV",
    "OPENAI_API_KEY",
    "PHASE78_ENABLED",
    "PHASE9_ENABLED",
    "PRODUCTION_DATABASE",
    "PRODUCTION_DB",
    "PRODUCTION_OUTBOX",
    "PRODUCTION_RELEASE",
    "PROVIDER_API_KEY",
    "SOLVER_API_KEY",
]
EXPECTED_ACCEPTANCE_CAPABILITIES = {
    "provider_or_network": False,
    "production_outbox_or_delivery": False,
    "release": False,
    "deployment": False,
    "migration": False,
    "cutover": False,
}


def test_acceptance_inventory_matches_the_independent_contract_list():
    assert PHASE9_ACCEPTANCE_CASES == EXPECTED_PHASE9_ACCEPTANCE_CASES


def _acceptance_python_descriptor() -> tuple[str, dict[str, object]]:
    path = Path(os.sys.executable).resolve(strict=True)
    metadata = path.lstat()
    raw = path.read_bytes()
    return str(path), {
        "schema": PHASE9_ACCEPTANCE_PYTHON_SCHEMA,
        "requested_path": str(path),
        "resolved_path": str(path),
        "byte_length": len(raw),
        "raw_bytes_sha256": hashlib.sha256(raw).hexdigest(),
        "mode": metadata.st_mode & 0o7777,
    }


def _acceptance_environment(working_directory: str) -> dict[str, object]:
    body = {
        "schema": PHASE9_ACCEPTANCE_ENVIRONMENT_SCHEMA,
        "inherited": False,
        "variables": {
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONPATH": working_directory,
        },
        "blocked_host_variable_names": (
            EXPECTED_ACCEPTANCE_BLOCKED_ENVIRONMENT_NAMES
        ),
        "capabilities": EXPECTED_ACCEPTANCE_CAPABILITIES,
    }
    return {**body, "environment_sha256": canonical_sha256(body)}


def _ready_gate(
    input_root, request, candidate, state,
    p0_root, p0_root_sha, p0_receipts,
):
    source = {
        "mode": "GIT",
        "candidate": candidate.as_dict(),
        "verified_tree": candidate.tree,
        "source_inventory_sha256": state.source_inventory_sha256,
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
    assert result["status"] == "READY", result
    return result


def _authorization(
    request,
    *,
    entry_state_receipt_sha256,
    occurred_at=2200,
    evidence_attestation_sha256="a" * 64,
):
    body = {
        "schema": PHASE9_START_AUTHORIZATION_SCHEMA,
        "authorization_id": f"phase9-start-{request.run_generation[-32:]}",
        "nonce_sha256": hashlib.sha256(
            f"nonce:{request.run_generation}".encode()
        ).hexdigest(),
        "authorization_mechanism": "CONTROLLED_OS_ACCOUNT",
        "authorized": True,
        "operator_uid": os.geteuid(),
        "operator_account": pwd.getpwuid(os.geteuid()).pw_name,
        "operation": "PHASE9_A_FORENSIC_REPLAY",
        "project_id": request.project_id,
        "workflow_id": request.workflow_id,
        "run_generation": request.run_generation,
        "source_commit": request.source_commit,
        "source_tree": request.source_tree,
        "source_parent": request.source_parent,
        "source_inventory_sha256": request.source_inventory_sha256,
        "entry_gate_result_sha256": request.entry_gate_result_sha256,
        "entry_state_receipt_sha256": entry_state_receipt_sha256,
        "replay_coordinate_sha256": replay_module._replay_coordinate_sha256(request),
        "evidence_attestation_sha256": evidence_attestation_sha256,
        "evidence_payload_set_sha256": (
            phase9_replay_evidence_payload_set_sha256(request)
        ),
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
    body["authorization_target_sha256"] = phase9_start_authorization_target_sha256(
        request,
        evidence_attestation_sha256=evidence_attestation_sha256,
        entry_state_receipt_sha256=entry_state_receipt_sha256,
    )
    body["authorization_receipt_sha256"] = canonical_sha256(body)
    return body


def _write_hashed_json(root: Path, logical_path: str, body, hash_field: str):
    value = dict(body)
    value[hash_field] = canonical_sha256(value)
    raw = canonical_bytes(value)
    path = root / logical_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {
        "logical_path": logical_path,
        "byte_length": len(raw),
        "raw_bytes_sha256": hashlib.sha256(raw).hexdigest(),
        "receipt_sha256": value[hash_field],
    }


def _write_raw(root: Path, logical_path: str, raw: bytes):
    path = root / logical_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {
        "logical_path": logical_path,
        "byte_length": len(raw),
        "raw_bytes_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _coordinate_fields(request_fields):
    return {
        "candidate": request_fields["candidate"],
        "project_id": request_fields["project_id"],
        "workflow_id": request_fields["workflow_id"],
        "run_generation": request_fields["run_generation"],
    }


def _replay_coordinate_sha256(request_fields):
    return canonical_sha256(
        {
            "schema": "authority-phase9-replay-coordinate-v1",
            "idempotency_key": request_fields["idempotency_key"],
            "operation_kind": request_fields["operation_kind"],
            "candidate": request_fields["candidate"],
            "source_inventory_sha256": request_fields[
                "source_inventory_sha256"
            ],
            "project_id": request_fields["project_id"],
            "workflow_id": request_fields["workflow_id"],
            "project_revision": request_fields["project_revision"],
            "project_generation": request_fields["project_generation"],
            "run_generation": request_fields["run_generation"],
            "run_generation_creation_receipt_sha256": request_fields[
                "run_generation_creation_receipt_sha256"
            ],
            "predecessor_replay_id": request_fields[
                "predecessor_replay_id"
            ],
            "predecessor_terminal_receipt_sha256": request_fields[
                "predecessor_terminal_receipt_sha256"
            ],
            "replay_mode": request_fields["replay_mode"],
            "requested_resume_target": request_fields[
                "requested_resume_target"
            ],
            "delivery_capability": "DISABLED",
            "entry_gate_result_sha256": request_fields[
                "entry_gate_result_sha256"
            ],
            "occurred_at": request_fields["occurred_at"],
        }
    )


def _dependency_fingerprint(
    request_fields, *, receipt_kind, logical_id, input_sha256
):
    return canonical_sha256(
        {
            "schema": "authority-phase9-evidence-dependency-fingerprint-v1",
            "receipt_kind": receipt_kind,
            "logical_id": logical_id,
            "replay_coordinate_sha256": _replay_coordinate_sha256(
                request_fields
            ),
            "source_run_generation": request_fields["run_generation"],
            "source_inventory_sha256": request_fields[
                "source_inventory_sha256"
            ],
            "input_sha256": input_sha256,
        }
    )


def _evidence_event_id(*, receipt_kind, logical_id, dependency):
    return "phase9-evidence-event:" + canonical_sha256(
        {
            "schema": "authority-phase9-evidence-event-identity-v1",
            "receipt_kind": receipt_kind,
            "logical_id": logical_id,
            "dependency_fingerprint_sha256": dependency,
        }
    )


def _role_generation(request_fields, *, role, dependency):
    return "phase9-role-generation:" + canonical_sha256(
        {
            "schema": "authority-phase9-role-generation-identity-v1",
            "role": role,
            "replay_coordinate_sha256": _replay_coordinate_sha256(
                request_fields
            ),
            "source_run_generation": request_fields["run_generation"],
            "dependency_fingerprint_sha256": dependency,
        }
    )


def _producer(request_fields, component):
    return {
        "schema": "authority-phase9-evidence-producer-v1",
        "execution_domain": "FORMAL_PHASE9_A",
        "component": component,
        "component_version": "2",
        "source_commit": request_fields["candidate"]["commit"],
        "source_tree": request_fields["candidate"]["tree"],
        "source_parent": request_fields["candidate"]["parent"],
        "source_inventory_sha256": request_fields["source_inventory_sha256"],
    }


def _provenance(
    request_fields,
    *,
    receipt_kind,
    logical_id,
    component,
    input_sha256,
    dependency_kind=None,
    dependency_input_sha256=None,
    event_sequence,
    predecessor_event_id=None,
    predecessor_receipt_sha256=None,
):
    dependency = _dependency_fingerprint(
        request_fields,
        receipt_kind=dependency_kind or receipt_kind,
        logical_id=logical_id,
        input_sha256=dependency_input_sha256 or input_sha256,
    )
    return {
        "producer": _producer(request_fields, component),
        "replay_coordinate_sha256": _replay_coordinate_sha256(request_fields),
        "source_run_generation": request_fields["run_generation"],
        "dependency_fingerprint_sha256": dependency,
        "event_id": _evidence_event_id(
            receipt_kind=receipt_kind,
            logical_id=logical_id,
            dependency=dependency,
        ),
        "event_sequence": event_sequence,
        "predecessor_event_id": predecessor_event_id,
        "predecessor_receipt_sha256": predecessor_receipt_sha256,
        "input_sha256": input_sha256,
    }


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
    identity_suffix = _replay_coordinate_sha256(request_fields)[-16:]
    required_claims = ["claim-a", "claim-b"]
    present_claims = ["claim-a"] if missing_claim else required_claims
    packet_raw = canonical_bytes(
        {
            "schema": "authority-phase9-packet-v2",
            "rebuild_start": RESUME_TARGET,
            "required_claims": required_claims,
            "claims": [
                {
                    "claim_id": claim,
                    "content_sha256": hashlib.sha256(
                        f"content:{claim}".encode()
                    ).hexdigest(),
                }
                for claim in present_claims
            ],
        }
    )
    packet_sha = hashlib.sha256(packet_raw).hexdigest()
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
            role_dependency = _dependency_fingerprint(
                request_fields,
                receipt_kind="ROLE",
                logical_id=role,
                input_sha256=packet_sha,
            )
            role_generation = _role_generation(
                request_fields,
                role=role,
                dependency=role_dependency,
            )
            identity = {
                "invocation_id": f"phase9-{role}-invocation-{identity_suffix}",
                "attempt_id": f"phase9-{role}-attempt-{identity_suffix}",
                "process_scope_id": f"phase9-{role}-scope-{identity_suffix}",
            }
            provider_receipt = _write_hashed_json(
                root,
                f"receipts/roles/{role}.provider.json",
                {
                    "schema": PHASE9_ROLE_PROVIDER_RECEIPT_SCHEMA,
                    "receipt_id": f"phase9-{role}-provider-receipt",
                    **_coordinate_fields(request_fields),
                    **_provenance(
                        request_fields,
                        receipt_kind="ROLE_PROVIDER",
                        dependency_kind="ROLE",
                        logical_id=role,
                        component="provider-runtime",
                        input_sha256=packet_sha,
                        event_sequence=1,
                    ),
                    "role": role,
                    "role_generation": role_generation,
                    "inherited": False,
                    "predecessor_role_generation": None,
                    **identity,
                    "packet_sha256": packet_sha,
                    "output_path": output_path,
                    "output_byte_length": len(output),
                    "output_sha256": hashlib.sha256(output).hexdigest(),
                    "provider_call_id": f"phase9-{role}-provider-call",
                    "provider_status": "SUCCEEDED",
                    "occurred_at": request_fields["occurred_at"] - 1,
                },
                "receipt_sha256",
            )
            process_receipt = _write_hashed_json(
                root,
                f"receipts/roles/{role}.process.json",
                {
                    "schema": PHASE9_ROLE_PROCESS_RECEIPT_SCHEMA,
                    "receipt_id": f"phase9-{role}-process-receipt",
                    **_coordinate_fields(request_fields),
                    **_provenance(
                        request_fields,
                        receipt_kind="ROLE_PROCESS",
                        dependency_kind="ROLE",
                        logical_id=role,
                        component="role-process-supervisor",
                        input_sha256=packet_sha,
                        event_sequence=2,
                        predecessor_event_id=(
                            json.loads(
                                (
                                    root
                                    / f"receipts/roles/{role}.provider.json"
                                ).read_text()
                            )["event_id"]
                        ),
                        predecessor_receipt_sha256=provider_receipt[
                            "receipt_sha256"
                        ],
                    ),
                    "role": role,
                    "role_generation": role_generation,
                    "inherited": False,
                    "predecessor_role_generation": None,
                    **identity,
                    "process_kind": "ROLE",
                    "process_status": "COMPLETED",
                    "exit_code": 0,
                    "packet_sha256": packet_sha,
                    "output_path": output_path,
                    "output_byte_length": len(output),
                    "output_sha256": hashlib.sha256(output).hexdigest(),
                    "provider_receipt": provider_receipt,
                    "occurred_at": request_fields["occurred_at"],
                },
                "receipt_sha256",
            )
            roles.append(
                {
                    "role": role,
                    "role_generation": role_generation,
                    "inherited": False,
                    "packet_sha256": packet_sha,
                    "output_path": output_path,
                    "output_sha256": hashlib.sha256(output).hexdigest(),
                    "process_receipt": process_receipt,
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
    process_scope_receipts = {}
    for action in ("failed", "kill", "pause"):
        process_identity_sha256 = hashlib.sha256(
            f"identity:{action}".encode()
        ).hexdigest()
        scope_output_sha256 = canonical_sha256(
            {
                "schema": "authority-phase9-process-scope-result-v1",
                "action": action.upper(),
                "process_identity_sha256": process_identity_sha256,
                "result": "PASS",
                "active_descendant_count": 0,
            }
        )
        process_scope_receipts[action] = _write_hashed_json(
            root,
            f"receipts/process-scopes/{action}.json",
            {
                "schema": PHASE9_PROCESS_SCOPE_RECEIPT_SCHEMA,
                "receipt_id": f"phase9-{action}-scope-receipt",
                **_coordinate_fields(request_fields),
                **_provenance(
                    request_fields,
                    receipt_kind="PROCESS_SCOPE",
                    logical_id=action,
                    component="process-scope-supervisor",
                    input_sha256=process_identity_sha256,
                    event_sequence=1,
                ),
                "output_sha256": scope_output_sha256,
                "action": action.upper(),
                "invocation_id": f"phase9-{action}-invocation-{identity_suffix}",
                "attempt_id": f"phase9-{action}-attempt-{identity_suffix}",
                "process_scope_id": f"phase9-{action}-scope-{identity_suffix}",
                "scope_kind": "WORKER",
                "process_identity_sha256": process_identity_sha256,
                "result": "PASS",
                "active_descendant_count": 0,
                "occurred_at": request_fields["occurred_at"],
            },
            "receipt_sha256",
        )
    runtime = {
        "schema": PHASE9_RUNTIME_EVIDENCE_SCHEMA,
        "precommit_external_launch_count": 0,
        "committed_reclaim_count": 0,
        "pending_outbox_count": 0,
        "uncertain_automatic_resend_count": 0,
        "active_descendant_count": 0,
        "process_scope_receipts": process_scope_receipts,
    }
    case_records = []
    acceptance_working_directory = str(_source_repository())
    acceptance_python, acceptance_python_descriptor = (
        _acceptance_python_descriptor()
    )
    acceptance_environment = _acceptance_environment(
        acceptance_working_directory
    )
    aggregate_binding = {
        name: hashlib.sha256(("fixture:" + name).encode()).hexdigest()
        for name in (
            "aggregate_command_sha256", "aggregate_raw_log_sha256",
            "aggregate_junit_sha256", "aggregate_event_log_sha256",
            "aggregate_outcome_sha256",
        )
    }
    for case in EXPECTED_PHASE9_ACCEPTANCE_CASES:
        test_node = PHASE9_ACCEPTANCE_TEST_NODES[case]
        raw_log = _write_raw(
            root,
            f"acceptance/{case}/raw.log",
            (
                "============================= test session starts "
                "==============================\n"
                "collected 1 item\n\n"
                f"{test_node} PASSED [100%]\n\n"
                "============================== 1 passed in 0.01s "
                "===============================\n"
            ).encode(),
        )
        raw_sha256 = raw_log["raw_bytes_sha256"]
        command_input_sha256 = canonical_sha256(
            {
                "schema": "authority-phase9-acceptance-command-input-v1",
                "case_id": case,
                "test_node": test_node,
                "acceptance_spec_sha256": PHASE9_ACCEPTANCE_SPEC_SHA256,
                "source_inventory_sha256": request_fields[
                    "source_inventory_sha256"
                ],
            }
        )
        command_provenance = _provenance(
            request_fields,
            receipt_kind="ACCEPTANCE_COMMAND",
            dependency_kind="ACCEPTANCE_CASE",
            logical_id=case,
            component="acceptance-command-runner",
            input_sha256=command_input_sha256,
            dependency_input_sha256=command_input_sha256,
            event_sequence=1,
        )
        command = _write_hashed_json(
            root,
            f"acceptance/{case}/command.json",
            {
                "schema": PHASE9_ACCEPTANCE_COMMAND_SCHEMA,
                "execution_domain": "FORMAL_PHASE9_A",
                **_coordinate_fields(request_fields),
                **command_provenance,
                "output_sha256": raw_sha256,
                "case_id": case,
                "acceptance_spec_sha256": PHASE9_ACCEPTANCE_SPEC_SHA256,
                "test_node": test_node,
                "source_inventory_sha256": request_fields[
                    "source_inventory_sha256"
                ],
                "command_argv": [
                    acceptance_python,
                    "-I",
                    "-B",
                    "-m",
                    "pytest",
                    "-p",
                    "no:cacheprovider",
                    "-vv",
                    "--tb=short",
                    "--color=no",
                    f"--basetemp={root.parent / 'acceptance-basetemp' / case}",
                    test_node,
                ],
                "working_directory": acceptance_working_directory,
                "python_executable": acceptance_python,
                "python_executable_descriptor": acceptance_python_descriptor,
                "environment": acceptance_environment,
                "started_at": request_fields["occurred_at"] - 2,
                "completed_at": request_fields["occurred_at"] - 1,
                "exit_code": 0,
                "raw_log": raw_log,
                **aggregate_binding,
            },
            "record_sha256",
        )
        result_provenance = _provenance(
            request_fields,
            receipt_kind="ACCEPTANCE_RESULT",
            dependency_kind="ACCEPTANCE_CASE",
            logical_id=case,
            component="acceptance-result-parser",
            input_sha256=raw_sha256,
            dependency_input_sha256=command_input_sha256,
            event_sequence=2,
            predecessor_event_id=command_provenance["event_id"],
            predecessor_receipt_sha256=command["receipt_sha256"],
        )
        result_output_sha256 = canonical_sha256(
            {
                "schema": "authority-phase9-pytest-case-outcome-v1",
                "case_id": case,
                "collected": 1,
                "passed": 1,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "xfailed": 0,
                "xpassed": 0,
                "warnings": 0,
                "exit_code": 0,
            }
        )
        test_result = _write_hashed_json(
            root,
            f"acceptance/{case}/result.json",
            {
                "schema": PHASE9_ACCEPTANCE_RESULT_SCHEMA,
                "execution_domain": "FORMAL_PHASE9_A",
                **_coordinate_fields(request_fields),
                **result_provenance,
                "output_sha256": result_output_sha256,
                "command_record": command,
                "raw_log": raw_log,
                "case_id": case,
                "status": "PASS",
                "collected": 1,
                "passed": 1,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "xfailed": 0,
                "xpassed": 0,
                "warnings": 0,
                "exit_code": 0,
                **aggregate_binding,
            },
            "result_sha256",
        )
        receipt_provenance = _provenance(
            request_fields,
            receipt_kind="ACCEPTANCE_CASE",
            dependency_kind="ACCEPTANCE_CASE",
            logical_id=case,
            component="acceptance-case-finalizer",
            input_sha256=test_result["receipt_sha256"],
            dependency_input_sha256=command_input_sha256,
            event_sequence=3,
            predecessor_event_id=result_provenance["event_id"],
            predecessor_receipt_sha256=test_result["receipt_sha256"],
        )
        receipt = _write_hashed_json(
            root,
            f"receipts/acceptance/{case}.json",
            {
                "schema": PHASE9_ACCEPTANCE_CASE_RECEIPT_SCHEMA,
                "receipt_id": f"phase9-{case.lower()}-receipt",
                **_coordinate_fields(request_fields),
                **receipt_provenance,
                "output_sha256": test_result["receipt_sha256"],
                "case_id": case,
                "result": "PASS",
                "command_record": command,
                "raw_log": raw_log,
                "test_result": test_result,
                **aggregate_binding,
                "occurred_at": request_fields["occurred_at"],
            },
            "receipt_sha256",
        )
        case_records.append({"case_id": case, "result": "PASS", "receipt": receipt})
    acceptance = {
        "schema": PHASE9_ACCEPTANCE_EVIDENCE_SCHEMA,
        "cases": case_records,
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
        "packet.json": packet,
        "roles.json": {"schema": PHASE9_ROLE_EVIDENCE_SCHEMA, "roles": roles},
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
    idempotency_key="phase9-replay-key-1",
    operation_kind=CREATE,
    predecessor_replay_id=None,
    predecessor_terminal_receipt_sha256=None,
    occurred_at=2200,
    **evidence_options,
):
    source_snapshot = read_current_git_source_snapshot(_source_repository())
    assert source_snapshot.source == generation_request.source
    fields = {
        "idempotency_key": idempotency_key,
        "operation_kind": operation_kind,
        "project_id": generation_request.project_id,
        "workflow_id": generation_request.workflow_id,
        "project_revision": generation_request.project_revision,
        "project_generation": generation_request.project_generation,
        "run_generation": state.run_generation,
        "run_generation_creation_receipt_sha256": (
            state.creation_receipt_sha256
        ),
        "predecessor_replay_id": predecessor_replay_id,
        "predecessor_terminal_receipt_sha256": (
            predecessor_terminal_receipt_sha256
        ),
        "replay_mode": mode,
        "requested_resume_target": RESUME_TARGET,
        "source_commit": generation_request.source.source_commit,
        "source_tree": generation_request.source.source_tree,
        "source_parent": generation_request.source.source_parent,
        "source_inventory_sha256": (
            source_snapshot.tracked_inventory.inventory_sha256
        ),
        "candidate": {
            "commit": generation_request.source.source_commit,
            "tree": generation_request.source.source_tree,
            "parent": generation_request.source.source_parent,
        },
        "entry_gate_result_sha256": gate["gate_result_sha256"],
        "occurred_at": occurred_at,
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
    draft = Phase9ForensicReplayRequestV1(
        PHASE9_REPLAY_REQUEST_SCHEMA,
        idempotency_key,
        operation_kind,
        generation_request.project_id,
        generation_request.workflow_id,
        generation_request.project_revision,
        generation_request.project_generation,
        state.run_generation,
        state.creation_receipt_sha256,
        predecessor_replay_id,
        predecessor_terminal_receipt_sha256,
        mode,
        RESUME_TARGET,
        "DISABLED",
        generation_request.source.source_commit,
        generation_request.source.source_tree,
        generation_request.source.source_parent,
        source_snapshot.tracked_inventory.inventory_sha256,
        gate["gate_result_sha256"],
        tuple(files),
        occurred_at,
    )
    (root / "start_authorization.json").write_bytes(
        canonical_bytes(
            _authorization(
                draft,
                entry_state_receipt_sha256=gate["state_receipt_sha256"],
                occurred_at=occurred_at,
            )
        )
    )
    return _reindex_evidence(root, draft)


def _fake_formal_acceptance_probe(occurred_at: int) -> dict[str, object]:
    nodes = [PHASE9_ACCEPTANCE_TEST_NODES[case] for case in PHASE9_ACCEPTANCE_CASES]
    source_root = str(_source_repository())
    nonce = "a" * 32
    events = [
        {
            "schema": "paper-factory-trusted-pytest-events-v2",
            "nonce": nonce,
            "sequence": 0,
            "event": "session_start",
            "rootdir": source_root,
        },
        {
            "schema": "paper-factory-trusted-pytest-events-v2",
            "nonce": nonce,
            "sequence": 1,
            "event": "collection",
            "nodeids": nodes,
        },
    ]
    for node in nodes:
        for phase in ("setup", "call", "teardown"):
            events.append(
                {
                    "schema": "paper-factory-trusted-pytest-events-v2",
                    "nonce": nonce,
                    "sequence": len(events),
                    "event": "phase_fact",
                    "nodeid": node,
                    "when": phase,
                    "outcome": "passed",
                    "xfail_declared": False,
                    "source": "runtest_call_excinfo",
                }
            )
    events.append(
        {
            "schema": "paper-factory-trusted-pytest-events-v2",
            "nonce": nonce,
            "sequence": len(events),
            "event": "session_finish",
            "exitstatus": 0,
        }
    )
    event_log = b"".join(canonical_bytes(value) + b"\n" for value in events)
    raw_log = (
        "============================= test session starts ==============================\n"
        "collected 17 items\n\n"
        + "\n".join(f"{node} PASSED [100%]" for node in nodes)
        + "\n\n============================== 17 passed in 0.01s ==============================\n"
    ).encode()
    cases_xml = "".join(
        '<testcase classname="tests.test_phase9_acceptance_probes" '
        f'name="{node.rsplit("::", 1)[1]}" />'
        for node in nodes
    )
    junit = (
        '<testsuites><testsuite name="phase9" tests="17" failures="0" '
        f'errors="0" skipped="0">{cases_xml}</testsuite></testsuites>'
    ).encode()
    outcome = {
        "schema": "authority-phase9-replay-acceptance-outcome-v1",
        "execution_domain": "FORMAL_PHASE9_A",
        "acceptance_spec_sha256": PHASE9_ACCEPTANCE_SPEC_SHA256,
        "cases": [
            {
                "case_id": case,
                "test_node": PHASE9_ACCEPTANCE_TEST_NODES[case],
                "status": "PASS",
            }
            for case in PHASE9_ACCEPTANCE_CASES
        ],
        "collected": 17,
        "passed": 17,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "exit_code": 0,
    }
    outcome["outcome_sha256"] = canonical_sha256(outcome)
    event_log_sha256 = hashlib.sha256(event_log).hexdigest()
    raw_log_sha256 = hashlib.sha256(raw_log).hexdigest()
    junit_sha256 = hashlib.sha256(junit).hexdigest()
    command = {
        "schema": "authority-phase9-replay-acceptance-command-v1",
        "execution_domain": "FORMAL_PHASE9_A",
        "acceptance_spec_sha256": PHASE9_ACCEPTANCE_SPEC_SHA256,
        "cwd": source_root,
        "python": {},
        "python_runtime": {},
        "sandbox": {},
        "trusted_reporter_sha256": "0" * 64,
        "trusted_event_sha256": event_log_sha256,
        "argv": [
            str(Path(os.sys.executable).resolve()), "-I", "-S", "-B",
            "trusted-reporter.py", "--runtime-site-packages", "/site-packages",
            "--source-root", source_root, "--",
            f"--basetemp=/formal-phase9-basetemp", *nodes,
        ],
        "sandbox_argv": [],
        "environment": {},
        "started_at": occurred_at - 1,
        "finished_at": occurred_at,
        "exit_code": 0,
        "raw_log_sha256": raw_log_sha256,
        "junit_sha256": junit_sha256,
        "outcome_sha256": outcome["outcome_sha256"],
    }
    return {
        "acceptance_event_log": event_log,
        "acceptance_event_log_sha256": event_log_sha256,
        "acceptance_event_nonce": nonce,
        "acceptance_raw_log": raw_log,
        "acceptance_raw_log_sha256": raw_log_sha256,
        "acceptance_junit_xml": junit,
        "acceptance_junit_sha256": junit_sha256,
        "acceptance_command_json": canonical_bytes(command).decode(),
        "acceptance_command_sha256": canonical_sha256(command),
        "acceptance_outcome_json": canonical_bytes(outcome).decode(),
        "acceptance_outcome_sha256": outcome["outcome_sha256"],
        "started_at": occurred_at - 1,
        "finished_at": occurred_at,
    }


def _attest_fixture_evidence(
    *,
    foundation,
    root: Path,
    request: Phase9ForensicReplayRequestV1,
    input_root: Path,
    context_path: Path,
) -> Phase9ForensicReplayRequestV1:
    """Simulate trusted completion hooks; production has no fixture bypass."""

    (root / "start_authorization.json").unlink()
    runtime_receipts: list[tuple[str, str, str, bytes]] = []
    for path in sorted((root / "receipts/roles").glob("*.json")) \
        if (root / "receipts/roles").exists() else ():
        name, suffix, _json = path.name.split(".")
        kind = "ROLE_PROVIDER" if suffix == "provider" else "ROLE_PROCESS"
        runtime_receipts.append(
            (kind, name, path.relative_to(root).as_posix(), path.read_bytes())
        )
    for path in sorted((root / "receipts/process-scopes").glob("*.json")):
        runtime_receipts.append(
            (
                "PROCESS_SCOPE",
                path.stem,
                path.relative_to(root).as_posix(),
                path.read_bytes(),
            )
        )
    (root / "acceptance.json").unlink()
    shutil.rmtree(root / "acceptance")
    shutil.rmtree(root / "receipts/acceptance")
    draft = _reindex_evidence(root, request)
    runtime_authorizations: dict[tuple[str, str], tuple[str, str, str]] = {}
    for kind, logical_id, logical_path, raw in runtime_receipts:
        body = json.loads(raw)
        runtime_authorizations[(kind, logical_id)] = (
            authorize_formal_phase9_runtime_receipt(
                database=foundation.database,
                expected_source_fence_sha256=(
                    foundation.preflight.source_fence_sha256
                ),
                request=draft,
                receipt_kind=kind,
                logical_id=logical_id,
                logical_path=logical_path,
                invocation_id=body["invocation_id"],
                attempt_id=body["attempt_id"],
                process_scope_id=body["process_scope_id"],
                packet_sha256=body.get("packet_sha256"),
                dependency_fingerprint_sha256=body[
                    "dependency_fingerprint_sha256"
                ],
                input_sha256=body["input_sha256"],
            )
        )
    connection = sqlite3.connect(foundation.database)
    try:
        pin = connection.execute(
            "SELECT contract_pin_set_sha256 FROM authority_workflows "
            "WHERE workflow_id=?",
            (draft.workflow_id,),
        ).fetchone()[0]
        grouped: dict[tuple[str, str, str], list[tuple[str, str, str, bytes]]] = {}
        for value in runtime_receipts:
            body = json.loads(value[3])
            identity = (
                body["invocation_id"], body["attempt_id"],
                body["process_scope_id"],
            )
            grouped.setdefault(identity, []).append(value)
        for identity, group in grouped.items():
            bodies = [json.loads(value[3]) for value in group]
            first = bodies[0]
            receipt_bindings = sorted(
                (
                    {
                        "receipt_kind": kind,
                        "logical_path": logical_path,
                        "byte_length": len(raw),
                        "raw_bytes_sha256": hashlib.sha256(raw).hexdigest(),
                        "receipt_sha256": body["receipt_sha256"],
                    }
                    for (kind, _logical_id, logical_path, raw), body
                    in zip(group, bodies)
                ),
                key=lambda value: value["receipt_kind"],
            )
            completion = {
                "schema": "authority-phase9-runtime-completion-v1",
                "execution_domain": "FORMAL_PHASE9_A",
                "candidate": {
                    "commit": draft.source_commit,
                    "tree": draft.source_tree,
                    "parent": draft.source_parent,
                },
                "project_id": draft.project_id,
                "workflow_id": draft.workflow_id,
                "run_generation": draft.run_generation,
                "replay_coordinate_sha256": (
                    replay_module._replay_coordinate_sha256(draft)
                ),
                "source_inventory_sha256": draft.source_inventory_sha256,
                "logical_id": group[0][1],
                "receipt_kinds": sorted(value[0] for value in group),
                "invocation_id": identity[0],
                "attempt_id": identity[1],
                "process_scope_id": identity[2],
                "dependency_fingerprint_sha256": first[
                    "dependency_fingerprint_sha256"
                ],
                "input_sha256": first["input_sha256"],
                "output_sha256": first["output_sha256"],
                "packet_sha256": first.get("packet_sha256"),
                "receipts": receipt_bindings,
                "status": "COMPLETED",
                "delivery_capability": "DISABLED",
            }
            completion_json = canonical_bytes(completion).decode("utf-8")
            command_id = f"phase9-runtime-command:{identity[0]}"
            connection.execute(
                "INSERT INTO authority_commands VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    command_id, draft.workflow_id, draft.project_id,
                    draft.project_revision, max(1, draft.project_revision),
                    "PHASE9_A_RUNTIME_COMPLETION",
                    "authority-phase9-runtime-completion-v1",
                    completion_json,
                    hashlib.sha256(canonical_bytes(completion)).hexdigest(), pin,
                    f"phase9-runtime-idempotency:{identity[0]}",
                ),
            )
            connection.execute(
                "INSERT INTO authority_invocations VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    identity[0], draft.workflow_id, command_id,
                    "PHASE9_A_RUNTIME_COMPLETION", 1, 1,
                    "authority-phase9-runtime-completion-v1",
                    completion_json, completion_json,
                ),
            )
            connection.execute(
                "INSERT INTO authority_attempts VALUES(?,?,?,?,?,?,?)",
                (
                    identity[1], identity[0], 1, 1,
                    "authority-phase9-runtime-completion-v1",
                    completion_json, completion_json,
                ),
            )
            connection.execute(
                "INSERT INTO authority_process_scopes VALUES(?,?,?,?,?,?,?,?)",
                (
                    identity[2], identity[1],
                    "PHASE9_A_RUNTIME_COMPLETION",
                    f"phase9-runtime:{identity[2]}", 1,
                    "authority-phase9-runtime-completion-v1",
                    completion_json, completion_json,
                ),
            )
        connection.commit()
    finally:
        connection.close()
    for kind, logical_id, logical_path, raw in runtime_receipts:
        authorization_id, authorization_nonce, _receipt_sha256 = (
            runtime_authorizations[(kind, logical_id)]
        )
        record_formal_phase9_runtime_receipt(
            database=foundation.database,
            expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
            authorization_id=authorization_id,
            authorization_nonce=authorization_nonce,
            request=draft,
            receipt_kind=kind,
            logical_id=logical_id,
            logical_path=logical_path,
            raw_bytes=raw,
        )
    original_runner = replay_evidence_module._run_fixed_acceptance_probes
    original_time = replay_evidence_module.time.time
    replay_evidence_module._run_fixed_acceptance_probes = lambda **_kwargs: (
        _fake_formal_acceptance_probe(draft.occurred_at)
    )
    replay_evidence_module.time.time = lambda: draft.occurred_at
    try:
        return produce_formal_phase9_replay_evidence(
            database=foundation.database,
            expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
            source_repository=_source_repository(),
            evidence_root=root,
            official_input_root=input_root,
            execution_context_receipt_path=context_path,
            python_executable=os.sys.executable,
            request=draft,
        )
    finally:
        replay_evidence_module._run_fixed_acceptance_probes = original_runner
        replay_evidence_module.time.time = original_time


def _fixture(tmp_path, *, mode=TECHNICAL, attest=True, **evidence_options):
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
    if attest:
        request = _attest_fixture_evidence(
            foundation=foundation,
            root=root,
            request=request,
            input_root=input_root,
            context_path=tmp_path / "execution-context.json",
        )
    service = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        evidence_root=root,
        official_input_root=input_root,
        execution_context_receipt_path=tmp_path / "execution-context.json",
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


def _database_family_snapshot(database: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in sorted(database.parent.glob(f"{database.name}*"))
        if path.is_file()
    }


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


def _shorten_start_authorization(
    foundation,
    root: Path,
    request: Phase9ForensicReplayRequestV1,
    *,
    expires_at: int,
) -> Phase9ForensicReplayRequestV1:
    """Issue the test fixture's same authorization with a shorter valid TTL."""

    path = root / "start_authorization.json"
    body = json.loads(path.read_text())
    body["expires_at"] = expires_at
    body.pop("authorization_receipt_sha256")
    body["authorization_receipt_sha256"] = canonical_sha256(body)
    raw = canonical_bytes(body)
    path.write_bytes(raw)
    updated = _reindex_evidence(root, request)
    connection = sqlite3.connect(foundation.database)
    try:
        table = "authority_production_phase9_start_authorizations"
        triggers = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND tbl_name=?",
            (table,),
        ).fetchall()
        for name, _sql in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(
            "UPDATE authority_production_phase9_start_authorizations "
            "SET expires_at=?, start_authorization_byte_length=?, "
            "start_authorization_raw_bytes_sha256=?, "
            "final_evidence_set_sha256=?, authorization_json=?, "
            "authorization_receipt_sha256=? WHERE authorization_id=?",
            (
                expires_at,
                len(raw),
                hashlib.sha256(raw).hexdigest(),
                updated.evidence_set_sha256,
                raw.decode("utf-8"),
                body["authorization_receipt_sha256"],
                body["authorization_id"],
            ),
        )
        for _name, sql in triggers:
            connection.execute(sql)
        connection.commit()
    finally:
        connection.close()
    return updated


def _refresh_start_authorization(
    root: Path, request: Phase9ForensicReplayRequestV1
) -> Phase9ForensicReplayRequestV1:
    """Rebind a test authorization so a forged payload reaches its semantic guard."""

    authorization_path = root / "start_authorization.json"
    previous = json.loads(authorization_path.read_text())
    payload_request = _reindex_evidence(root, request)
    authorization_path.write_bytes(
        canonical_bytes(
            _authorization(
                payload_request,
                entry_state_receipt_sha256=previous[
                    "entry_state_receipt_sha256"
                ],
                occurred_at=payload_request.occurred_at,
                evidence_attestation_sha256=previous[
                    "evidence_attestation_sha256"
                ],
            )
        )
    )
    return _reindex_evidence(root, payload_request)


def _refresh_component_receipt(
    root: Path,
    request: Phase9ForensicReplayRequestV1,
    *,
    kind: str,
    input_sha256: str | None = None,
) -> Phase9ForensicReplayRequestV1:
    """Rehash one trusted component fixture after a deliberate control mutation."""

    logical_path, _schema, evidence_path, _component = (
        replay_module._COMPONENT_RECEIPTS[kind]
    )
    path = root / logical_path
    body = json.loads(path.read_text())
    body.pop("receipt_sha256")
    if input_sha256 is None:
        input_sha256 = str(body["input_sha256"])
    output_sha256 = hashlib.sha256((root / evidence_path).read_bytes()).hexdigest()
    dependency = replay_module._dependency_fingerprint_sha256(
        request,
        receipt_kind=kind,
        logical_id=kind.lower(),
        input_sha256=input_sha256,
    )
    authorization = json.loads((root / "start_authorization.json").read_text())
    body.update(
        {
            "dependency_fingerprint_sha256": dependency,
            "event_id": replay_module._evidence_event_id(
                receipt_kind=kind,
                logical_id=kind.lower(),
                dependency_fingerprint_sha256=dependency,
            ),
            "input_sha256": input_sha256,
            "output_sha256": output_sha256,
            "evidence_sha256": output_sha256,
            "authority_source_sha256": (
                replay_module._component_authority_source_sha256(
                    kind,
                    request,
                    entry_state_receipt_sha256=authorization[
                        "entry_state_receipt_sha256"
                    ],
                    input_sha256=input_sha256,
                    output_sha256=output_sha256,
                )
            ),
        }
    )
    _write_hashed_json(root, logical_path, body, "receipt_sha256")
    return _refresh_start_authorization(root, request)


def _request_fields_from_request(request: Phase9ForensicReplayRequestV1):
    return {
        "idempotency_key": request.idempotency_key,
        "operation_kind": request.operation_kind,
        "candidate": {
            "commit": request.source_commit,
            "tree": request.source_tree,
            "parent": request.source_parent,
        },
        "source_inventory_sha256": request.source_inventory_sha256,
        "project_id": request.project_id,
        "workflow_id": request.workflow_id,
        "project_revision": request.project_revision,
        "project_generation": request.project_generation,
        "run_generation": request.run_generation,
        "run_generation_creation_receipt_sha256": (
            request.run_generation_creation_receipt_sha256
        ),
        "predecessor_replay_id": request.predecessor_replay_id,
        "predecessor_terminal_receipt_sha256": (
            request.predecessor_terminal_receipt_sha256
        ),
        "replay_mode": request.replay_mode,
        "requested_resume_target": request.requested_resume_target,
        "entry_gate_result_sha256": request.entry_gate_result_sha256,
        "occurred_at": request.occurred_at,
    }


def _rewrite_process_scope_receipt(
    root: Path,
    request: Phase9ForensicReplayRequestV1,
    *,
    action: str,
    changes: dict[str, object],
) -> Phase9ForensicReplayRequestV1:
    path = root / f"receipts/process-scopes/{action}.json"
    receipt = json.loads(path.read_text())
    receipt.pop("receipt_sha256")
    receipt.update(changes)
    reference = _write_hashed_json(
        root,
        f"receipts/process-scopes/{action}.json",
        receipt,
        "receipt_sha256",
    )
    runtime_path = root / "outbox_supervisor.json"
    runtime = json.loads(runtime_path.read_text())
    runtime["process_scope_receipts"][action] = reference
    runtime_path.write_bytes(canonical_bytes(runtime))
    return _refresh_start_authorization(root, request)


def _rewrite_acceptance_case_raw(
    root: Path,
    request: Phase9ForensicReplayRequestV1,
    *,
    case_id: str,
    raw: bytes,
) -> Phase9ForensicReplayRequestV1:
    """Cascade all hashes while intentionally preserving the claimed PASS totals."""

    fields = _request_fields_from_request(request)
    raw_reference = _write_raw(
        root, f"acceptance/{case_id}/raw.log", raw
    )
    raw_sha256 = raw_reference["raw_bytes_sha256"]

    result_path = root / f"acceptance/{case_id}/result.json"
    result = json.loads(result_path.read_text())
    result.pop("result_sha256")
    result.update(
        _provenance(
            fields,
            receipt_kind="ACCEPTANCE_RESULT",
            dependency_kind="ACCEPTANCE_CASE",
            logical_id=case_id,
            component="acceptance-result-parser",
            input_sha256=raw_sha256,
            dependency_input_sha256=raw_sha256,
            event_sequence=1,
        )
    )
    result["raw_log"] = raw_reference
    result_reference = _write_hashed_json(
        root,
        f"acceptance/{case_id}/result.json",
        result,
        "result_sha256",
    )

    command_path = root / f"acceptance/{case_id}/command.json"
    command = json.loads(command_path.read_text())
    command.pop("record_sha256")
    result_provenance = json.loads(result_path.read_text())
    command_provenance = _provenance(
        fields,
        receipt_kind="ACCEPTANCE_COMMAND",
        dependency_kind="ACCEPTANCE_CASE",
        logical_id=case_id,
        component="acceptance-command-runner",
        input_sha256=raw_sha256,
        dependency_input_sha256=raw_sha256,
        event_sequence=2,
        predecessor_event_id=result_provenance["event_id"],
        predecessor_receipt_sha256=result_reference["receipt_sha256"],
    )
    command.update(command_provenance)
    command["output_sha256"] = result_reference["receipt_sha256"]
    command["raw_log"] = raw_reference
    command["test_result"] = result_reference
    command_reference = _write_hashed_json(
        root,
        f"acceptance/{case_id}/command.json",
        command,
        "record_sha256",
    )

    receipt_path = root / f"receipts/acceptance/{case_id}.json"
    receipt = json.loads(receipt_path.read_text())
    receipt.pop("receipt_sha256")
    receipt.update(
        _provenance(
            fields,
            receipt_kind="ACCEPTANCE_CASE",
            dependency_kind="ACCEPTANCE_CASE",
            logical_id=case_id,
            component="acceptance-case-finalizer",
            input_sha256=command_reference["receipt_sha256"],
            dependency_input_sha256=raw_sha256,
            event_sequence=3,
            predecessor_event_id=command_provenance["event_id"],
            predecessor_receipt_sha256=command_reference["receipt_sha256"],
        )
    )
    receipt["output_sha256"] = result_reference["receipt_sha256"]
    receipt["command_record"] = command_reference
    receipt["raw_log"] = raw_reference
    receipt["test_result"] = result_reference
    receipt_reference = _write_hashed_json(
        root,
        f"receipts/acceptance/{case_id}.json",
        receipt,
        "receipt_sha256",
    )

    acceptance_path = root / "acceptance.json"
    acceptance = json.loads(acceptance_path.read_text())
    for case in acceptance["cases"]:
        if case["case_id"] == case_id:
            case["receipt"] = receipt_reference
            break
    acceptance_path.write_bytes(canonical_bytes(acceptance))
    return _refresh_start_authorization(root, request)


def _rewrite_acceptance_command_runner(
    root: Path,
    request: Phase9ForensicReplayRequestV1,
    *,
    case_id: str,
    working_directory: str | None = None,
    python_executable: str | None = None,
) -> Phase9ForensicReplayRequestV1:
    """Rehash a forged runner record so only live runner binding can reject it."""

    fields = _request_fields_from_request(request)
    command_path = root / f"acceptance/{case_id}/command.json"
    command = json.loads(command_path.read_text())
    command.pop("record_sha256")
    if working_directory is not None:
        command["working_directory"] = working_directory
        environment = dict(command["environment"])
        environment.pop("environment_sha256")
        environment["variables"] = {
            **environment["variables"],
            "PYTHONPATH": working_directory,
        }
        environment["environment_sha256"] = canonical_sha256(environment)
        command["environment"] = environment
    if python_executable is not None:
        command["python_executable"] = python_executable
        command["command_argv"][0] = python_executable
        command["python_executable_descriptor"] = {
            "schema": PHASE9_ACCEPTANCE_PYTHON_SCHEMA,
            "requested_path": python_executable,
            "resolved_path": python_executable,
            "byte_length": 1,
            "raw_bytes_sha256": "0" * 64,
            "mode": 0o755,
        }
    command_reference = _write_hashed_json(
        root,
        f"acceptance/{case_id}/command.json",
        command,
        "record_sha256",
    )
    result_path = root / f"acceptance/{case_id}/result.json"
    result = json.loads(result_path.read_text())
    result.pop("result_sha256")
    result["command_record"] = command_reference
    result["predecessor_receipt_sha256"] = command_reference["receipt_sha256"]
    result_reference = _write_hashed_json(
        root,
        f"acceptance/{case_id}/result.json",
        result,
        "result_sha256",
    )
    receipt_path = root / f"receipts/acceptance/{case_id}.json"
    receipt = json.loads(receipt_path.read_text())
    receipt.pop("receipt_sha256")
    receipt["input_sha256"] = result_reference["receipt_sha256"]
    receipt["output_sha256"] = result_reference["receipt_sha256"]
    receipt["predecessor_event_id"] = result["event_id"]
    receipt["predecessor_receipt_sha256"] = result_reference["receipt_sha256"]
    receipt["command_record"] = command_reference
    receipt["test_result"] = result_reference
    receipt_reference = _write_hashed_json(
        root,
        f"receipts/acceptance/{case_id}.json",
        receipt,
        "receipt_sha256",
    )
    acceptance_path = root / "acceptance.json"
    acceptance = json.loads(acceptance_path.read_text())
    for item in acceptance["cases"]:
        if item["case_id"] == case_id:
            item["receipt"] = receipt_reference
            break
    acceptance_path.write_bytes(canonical_bytes(acceptance))
    return _refresh_start_authorization(root, request)


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


def test_enabled_configuration_requires_both_live_external_input_paths():
    values = {
        "PHASE9_ENABLED": "true",
        "PHASE9_AUTHORITY_SOURCE_FENCE_SHA256": "1" * 64,
        "PHASE9_AUTHORITY_DB_FILE": "/phase9/authority.db",
        "PHASE9_SOURCE_REPOSITORY": "/phase9/source",
        "PHASE9_EVIDENCE_ROOT": "/phase9/evidence",
        "PHASE9_OFFICIAL_INPUT_ROOT": "/phase9/official-input",
        "PHASE9_EXECUTION_CONTEXT_RECEIPT": "/phase9/context.json",
    }
    settings = load_phase9_settings(values)
    assert settings.official_input_root == Path("/phase9/official-input")
    assert settings.execution_context_receipt_path == Path(
        "/phase9/context.json"
    )
    for name in (
        "PHASE9_OFFICIAL_INPUT_ROOT",
        "PHASE9_EXECUTION_CONTEXT_RECEIPT",
    ):
        missing = dict(values)
        del missing[name]
        with pytest.raises(Phase9ConfigurationError, match=name):
            load_phase9_settings(missing)


def test_preflight_atomic_execute_exact_replay_and_read_only_collection(tmp_path):
    foundation, root, request, service = _fixture(tmp_path)
    preflight = preflight_phase9_forensic_replay(
        request, evidence_root=root, trusted_now=request.occurred_at
    )
    assert preflight["status"] == "READY"
    first = service.execute(request)
    replay = service.execute(request)
    assert first.replayed is False
    assert replay == first
    assert replay.as_dict() == first.as_dict()
    assert first.terminal_reason == "FORENSIC_REPLAY_COMPLETED"
    assert _counts(foundation.database) == {
        "authority_production_phase9_replays": 1,
        "authority_production_phase9_replay_events": 6,
        "authority_production_phase9_terminal_receipts": 1,
        "authority_production_phase9_replay_idempotency": 1,
        "authority_production_phase9_replay_current": 1,
        "authority_production_phase9_evidence_receipts": 30,
        "authority_production_phase9_gate_consumptions": 1,
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
    assert state["typed_receipt_count"] == 30
    assert before == after


def test_same_idempotency_key_with_different_request_conflicts(tmp_path):
    foundation, _root, request, service = _fixture(tmp_path)
    service.execute(request)
    before_files = _database_family_snapshot(foundation.database)
    changed = replace(request, occurred_at=request.occurred_at + 1)
    with pytest.raises(Phase9ForensicReplayConflict, match="idempotency key"):
        service.execute(changed)
    assert _database_family_snapshot(foundation.database) == before_files


def test_same_key_cannot_be_reused_by_another_workflow_before_live_gates(
    tmp_path,
):
    foundation, _root, request, service = _fixture(tmp_path)
    service.execute(request)
    before_files = _database_family_snapshot(foundation.database)
    changed = replace(request, workflow_id="workflow-global-key-conflict")
    changed_service = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        evidence_root=tmp_path / "missing-cross-workflow-evidence",
        official_input_root=tmp_path / "missing-cross-workflow-input",
        execution_context_receipt_path=(
            tmp_path / "missing-cross-workflow-context.json"
        ),
        clock=lambda: changed.occurred_at + 301,
    )

    with pytest.raises(
        Phase9ForensicReplayConflict,
        match="idempotency key.*workflow|workflow.*idempotency key",
    ):
        changed_service.execute(changed)

    assert _database_family_snapshot(foundation.database) == before_files
    connection = sqlite3.connect(foundation.database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM authority_production_phase9_replay_idempotency "
            "WHERE idempotency_key=?",
            (request.idempotency_key,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM authority_production_phase9_replays "
            "WHERE workflow_id=?",
            (changed.workflow_id,),
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_same_key_structurally_invalid_replay_is_explicit_conflict(tmp_path):
    foundation, _root, request, service = _fixture(tmp_path)
    service.execute(request)
    before_files = _database_family_snapshot(foundation.database)

    with pytest.raises(Phase9ForensicReplayConflict, match="idempotency key"):
        service.execute(replace(request, delivery_capability="ENABLED"))

    assert _database_family_snapshot(foundation.database) == before_files


def test_same_key_run_generation_mismatch_conflicts_without_mutation(tmp_path):
    foundation, _root, request, service = _fixture(tmp_path)
    service.execute(request)
    before_bytes = foundation.database.read_bytes()
    before = _counts(foundation.database)
    changed = replace(request, run_generation="run-generation:different")

    with pytest.raises(Phase9ForensicReplayConflict, match="idempotency key"):
        service.execute(changed)

    assert foundation.database.read_bytes() == before_bytes
    assert _counts(foundation.database) == before


def test_start_authorization_uses_trusted_clock_and_exact_gate_result(tmp_path):
    _foundation, root, request, _service = _fixture(tmp_path)
    authorization_path = root / "start_authorization.json"
    original = json.loads(authorization_path.read_text())
    entry_state_receipt_sha256 = original["entry_state_receipt_sha256"]

    expired = _authorization(
        request,
        entry_state_receipt_sha256=entry_state_receipt_sha256,
        occurred_at=request.occurred_at,
    )
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

    stale_issue = _authorization(
        request,
        entry_state_receipt_sha256=entry_state_receipt_sha256,
        occurred_at=request.occurred_at,
    )
    stale_issue["issued_at"] = request.occurred_at - 301
    stale_issue.pop("authorization_receipt_sha256")
    stale_issue["authorization_receipt_sha256"] = canonical_sha256(stale_issue)
    authorization_path.write_bytes(canonical_bytes(stale_issue))
    stale_issue_request = _reindex_evidence(root, request)
    result = preflight_phase9_forensic_replay(
        stale_issue_request,
        evidence_root=root,
        trusted_now=request.occurred_at,
    )
    assert result["status"] == "BLOCKED"
    assert "issue time exceeds trusted clock skew" in result["blockers"][0]["detail"]

    wrong_gate = _authorization(
        request,
        entry_state_receipt_sha256=entry_state_receipt_sha256,
        occurred_at=request.occurred_at,
    )
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
        "after_gate_consumption", "after_typed_receipts", "after_receipt",
        "after_current_pointer", "before_commit",
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
        official_input_root=_service.official_input_root,
        execution_context_receipt_path=(
            _service.execution_context_receipt_path
        ),
        fault_hook=fault,
        clock=lambda: request.occurred_at,
    )
    with pytest.raises(RuntimeError, match="fault"):
        service.execute(request)
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


def test_missing_claims_are_blocked_and_dispatch_must_be_zero(tmp_path):
    _foundation, root, request, service = _fixture(
        tmp_path, mode=ABLATE_NO_JUDGE
    )
    packet_raw = canonical_bytes(
        {
            "schema": "authority-phase9-packet-v2",
            "rebuild_start": RESUME_TARGET,
            "required_claims": ["claim-a", "claim-b"],
            "claims": [
                {
                    "claim_id": "claim-a",
                    "content_sha256": hashlib.sha256(
                        b"content:claim-a"
                    ).hexdigest(),
                }
            ],
        }
    )
    (root / "payload/packet.bin").write_bytes(packet_raw)
    packet = json.loads((root / "packet.json").read_text())
    packet["present_claims"] = ["claim-a"]
    packet["packet_sha256"] = hashlib.sha256(packet_raw).hexdigest()
    (root / "packet.json").write_bytes(canonical_bytes(packet))
    request = _refresh_component_receipt(
        root,
        request,
        kind="PACKET",
        input_sha256=hashlib.sha256(packet_raw).hexdigest(),
    )
    preflight = preflight_phase9_forensic_replay(
        request, evidence_root=root, trusted_now=request.occurred_at
    )
    assert preflight["status"] == "BLOCKED"
    assert [item["code"] for item in preflight["blockers"]] == ["MISSING_PACKET_CLAIMS"]
    with pytest.raises(Phase9ForensicReplaySafetyError, match="BLOCKED"):
        service.execute(request)

    other = tmp_path / "other"
    foundation, root2, request2, _ = _fixture(
        other, mode=ABLATE_NO_JUDGE
    )
    packet_raw2 = packet_raw
    (root2 / "payload/packet.bin").write_bytes(packet_raw2)
    packet2 = json.loads((root2 / "packet.json").read_text())
    packet2["present_claims"] = ["claim-a"]
    packet2["packet_sha256"] = hashlib.sha256(packet_raw2).hexdigest()
    packet2["dispatch_count"] = 1
    (root2 / "packet.json").write_bytes(canonical_bytes(packet2))
    request2 = _refresh_component_receipt(
        root2,
        request2,
        kind="PACKET",
        input_sha256=hashlib.sha256(packet_raw2).hexdigest(),
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
        ({"bad_snapshot": True}, "read failure and ERROR status must agree"),
    ],
)
def test_verdict_and_snapshot_fail_closed(tmp_path, option, message):
    _foundation, root, request, service = _fixture(
        tmp_path, attest=False, **option
    )
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
    assert state["typed_receipt_count"] == 24


def test_delivery_evidence_cannot_enable_or_create_release(tmp_path):
    foundation, root, request, service = _fixture(tmp_path)
    acceptance_path = root / "acceptance.json"
    acceptance = json.loads(acceptance_path.read_text())
    acceptance["delivery"]["delivery_capability"] = "ENABLED"
    acceptance_path.write_bytes(canonical_bytes(acceptance))
    request = _refresh_start_authorization(root, request)
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
        official_input_root=_service.official_input_root,
        execution_context_receipt_path=(
            _service.execution_context_receipt_path
        ),
        fault_hook=mutate,
        clock=lambda: request.occurred_at,
    )
    with pytest.raises(Phase9ForensicReplaySafetyError, match="evidence bytes differ"):
        service.execute(request)
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


def test_same_bytes_evidence_identity_replacement_before_commit_rolls_back(tmp_path):
    foundation, root, request, _service = _fixture(tmp_path)
    target = root / "receipts/process-scopes/pause.json"

    def replace_with_same_bytes(name):
        if name == "after_receipt":
            replacement = target.with_name("pause.replacement")
            replacement.write_bytes(target.read_bytes())
            os.replace(replacement, target)

    service = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        evidence_root=root,
        official_input_root=_service.official_input_root,
        execution_context_receipt_path=(
            _service.execution_context_receipt_path
        ),
        fault_hook=replace_with_same_bytes,
        clock=lambda: request.occurred_at,
    )
    with pytest.raises(Phase9ForensicReplayConflict, match="evidence changed"):
        service.execute(request)
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


@pytest.mark.parametrize("external_input", ["official", "execution-context"])
def test_external_generation_input_change_before_commit_rolls_back(
    tmp_path, external_input
):
    foundation, root, request, fixture_service = _fixture(tmp_path)
    target = (
        fixture_service.official_input_root / "official/problem.pdf"
        if external_input == "official"
        else fixture_service.execution_context_receipt_path
    )

    def mutate_external_input(name):
        if name == "after_receipt":
            target.write_bytes(b"changed external generation input\n")

    service = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        evidence_root=root,
        official_input_root=fixture_service.official_input_root,
        execution_context_receipt_path=(
            fixture_service.execution_context_receipt_path
        ),
        fault_hook=mutate_external_input,
        clock=lambda: request.occurred_at,
    )
    with pytest.raises(
        Phase9ForensicReplayConflict,
        match="current official input or execution context differs",
    ):
        service.execute(request)
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


def test_shared_external_input_verifiers_run_at_both_boundaries(
    tmp_path, monkeypatch
):
    _foundation, _root, request, service = _fixture(tmp_path)
    official = replay_module.verify_official_input_snapshot
    context = replay_module.verify_execution_context_receipt
    source = replay_module.read_verified_execution_source_snapshot
    calls = {"official": 0, "context": 0, "source": 0}

    def verify_official(*args, **kwargs):
        calls["official"] += 1
        return official(*args, **kwargs)

    def verify_context(*args, **kwargs):
        calls["context"] += 1
        return context(*args, **kwargs)

    def verify_source(*args, **kwargs):
        calls["source"] += 1
        return source(*args, **kwargs)

    monkeypatch.setattr(
        replay_module, "verify_official_input_snapshot", verify_official
    )
    monkeypatch.setattr(
        replay_module, "verify_execution_context_receipt", verify_context
    )
    monkeypatch.setattr(
        replay_module, "read_verified_execution_source_snapshot", verify_source
    )
    service.execute(request)
    # Query-only preflight, RW transaction start, preterminal, and precommit.
    assert calls == {"official": 4, "context": 4, "source": 4}


@pytest.mark.parametrize("failed_call", (3, 4), ids=("preterminal", "precommit"))
def test_loaded_execution_source_drift_at_late_boundaries_rolls_back(
    tmp_path,
    monkeypatch,
    failed_call,
):
    foundation, _root, request, service = _fixture(tmp_path)
    original = replay_module.read_verified_execution_source_snapshot
    calls = 0

    def verify(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == failed_call:
            raise Phase9RunGenerationSafetyError(
                "execution source differs from candidate Git tree"
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(
        replay_module, "read_verified_execution_source_snapshot", verify
    )
    before = _database_family_snapshot(foundation.database)
    with pytest.raises(
        Phase9ForensicReplayConflict,
        match="executing source differs",
    ):
        service.execute(request)
    assert calls == failed_call
    assert _database_family_snapshot(foundation.database) == before
    assert _counts(foundation.database) == {
        table: 0 for table in PHASE9_TABLES
    }


@pytest.mark.parametrize("external_input", ["official", "execution-context"])
def test_same_bytes_external_input_identity_replacement_rolls_back(
    tmp_path, external_input
):
    foundation, root, request, fixture_service = _fixture(tmp_path)
    target = (
        fixture_service.official_input_root / "official/problem.pdf"
        if external_input == "official"
        else fixture_service.execution_context_receipt_path
    )

    def replace_external_input(name):
        if name == "after_receipt":
            replacement = target.with_name(target.name + ".replacement")
            replacement.write_bytes(target.read_bytes())
            os.replace(replacement, target)

    service = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        evidence_root=root,
        official_input_root=fixture_service.official_input_root,
        execution_context_receipt_path=(
            fixture_service.execution_context_receipt_path
        ),
        fault_hook=replace_external_input,
        clock=lambda: request.occurred_at,
    )
    with pytest.raises(
        Phase9ForensicReplayConflict,
        match="official input or execution context changed",
    ):
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
    first_request = _attest_fixture_evidence(
        foundation=foundation,
        root=first_root,
        request=first_request,
        input_root=input_root,
        context_path=tmp_path / "execution-context.json",
    )
    first_service = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(), evidence_root=first_root,
        official_input_root=input_root,
        execution_context_receipt_path=tmp_path / "execution-context.json",
        clock=lambda: first_request.occurred_at,
    )
    first = first_service.execute(first_request)

    rotation_authorization = replace(
        generation_request.operator_authorization,
        authorization_id="phase9-generation-rotation-authorization",
        operation_kind=ROTATE_GENERATION,
        issued_at=2200,
    )
    rotation_request = replace(
        generation_request,
        idempotency_key="phase9-entry-generation-rotation-key",
        operation_kind=ROTATE_GENERATION,
        predecessor_run_generation=first_state.run_generation,
        predecessor_creation_receipt_sha256=first_state.creation_receipt_sha256,
        predecessor_terminal_receipt_sha256=first.receipt_sha256,
        operator_authorization=rotation_authorization,
        occurred_at=2300,
    )
    rotation_authorization = replace(
        rotation_authorization,
        authorized_request_sha256=rotation_request.authorization_target_sha256,
    )
    rotation_authorization = replace(
        rotation_authorization,
        authorization_statement_sha256=(
            rotation_authorization.expected_statement_sha256
        ),
    )
    rotation_request = replace(
        rotation_request,
        operator_authorization=rotation_authorization,
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
    second_p0_root = tmp_path / "second-p0-evidence"
    second_p0_receipts = _p0_receipts(
        candidate,
        second_p0_root,
        project_id=rotation_request.project_id,
        workflow_id=rotation_request.workflow_id,
        run_generation=second_state.run_generation,
        source_inventory_sha256=second_state.source_inventory_sha256,
        authority_database=foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
    )
    second_state = collect_phase9_entry_state(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        workflow_id=rotation_request.workflow_id,
        candidate=candidate,
    )
    second_p0_root_sha = p0_evidence_root_sha256(second_p0_root)
    second_gate = _ready_gate(
        input_root, rotation_request, candidate, second_state,
        second_p0_root, second_p0_root_sha, second_p0_receipts,
    )
    second_root = tmp_path / "second-replay"
    second_request = _request_for_evidence(
        second_root,
        rotation_request,
        second_state,
        second_gate,
        idempotency_key="phase9-replay-key-2",
        operation_kind=ROTATE_GENERATION,
        predecessor_replay_id=first.replay_id,
        predecessor_terminal_receipt_sha256=first.receipt_sha256,
        occurred_at=2400,
    )
    second_request = _attest_fixture_evidence(
        foundation=foundation,
        root=second_root,
        request=second_request,
        input_root=input_root,
        context_path=context,
    )
    second_service = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(), evidence_root=second_root,
        official_input_root=input_root,
        execution_context_receipt_path=context,
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
    before_recovery = foundation.database.read_bytes()
    predecessor_recovery = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        evidence_root=first_root,
        official_input_root=input_root,
        execution_context_receipt_path=context,
        clock=lambda: first_request.occurred_at + 301,
    ).execute(first_request)
    assert predecessor_recovery == first
    assert predecessor_recovery.as_dict() == first.as_dict()
    assert foundation.database.read_bytes() == before_recovery

    connection = sqlite3.connect(foundation.database)
    connection.row_factory = sqlite3.Row
    table = "authority_production_phase9_replays"
    try:
        triggers = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND tbl_name=?",
            (table,),
        ).fetchall()
        for name, _sql in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        dangling = dict(
            connection.execute(
                f"SELECT * FROM {table} WHERE replay_id=?",
                (second.replay_id,),
            ).fetchone()
        )
        dangling.update(
            replay_id="phase9-replay:historical-tip-dangling",
            run_generation="run-generation:historical-tip-dangling",
            predecessor_replay_id=second.replay_id,
            predecessor_terminal_receipt_sha256=second.receipt_sha256,
            request_sha256="d" * 64,
        )
        columns = tuple(dangling)
        connection.execute(
            f"INSERT INTO {table}({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            tuple(dangling[name] for name in columns),
        )
        for _name, sql in triggers:
            connection.execute(sql)
        connection.commit()
    finally:
        connection.close()
    damaged = _database_family_snapshot(foundation.database)

    # The dangling replay may be rejected directly by the replay chain or by
    # the nested run-generation provenance walk that validates the same chain.
    with pytest.raises(Phase9ForensicReplayConflict):
        first_service.execute(first_request)

    assert _database_family_snapshot(foundation.database) == damaged


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


def test_extra_empty_evidence_directory_is_rejected_by_the_closure(tmp_path):
    foundation, root, request, _service = _fixture(tmp_path)
    (root / "unrecorded-empty-directory").mkdir()

    result = preflight_phase9_forensic_replay(
        request, evidence_root=root, trusted_now=request.occurred_at
    )
    assert result["status"] == "BLOCKED"
    assert "evidence inventory differs" in result["blockers"][0]["detail"]
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


def test_evidence_enumeration_error_fails_closed(tmp_path, monkeypatch):
    foundation, root, request, _service = _fixture(tmp_path)

    def cannot_list_directory(_descriptor):
        raise PermissionError("fixture enumeration denied")

    monkeypatch.setattr(replay_module.os, "listdir", cannot_list_directory)
    result = preflight_phase9_forensic_replay(
        request, evidence_root=root, trusted_now=request.occurred_at
    )
    assert result["status"] == "BLOCKED"
    assert "cannot be enumerated" in result["blockers"][0]["detail"]
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


def test_evidence_root_replacement_while_descriptor_is_open_fails_closed(
    tmp_path,
    monkeypatch,
):
    foundation, root, request, _service = _fixture(tmp_path)
    original = replay_module._StableDirectoryTree.__enter__

    def enter_and_replace(tree):
        opened = original(tree)
        preserved = root.with_name(root.name + ".preserved")
        os.replace(root, preserved)
        root.mkdir()
        return opened

    monkeypatch.setattr(
        replay_module._StableDirectoryTree,
        "__enter__",
        enter_and_replace,
    )
    result = preflight_phase9_forensic_replay(
        request, evidence_root=root, trusted_now=request.occurred_at
    )
    assert result["status"] == "BLOCKED"
    assert "directory changed while being verified" in result["blockers"][0]["detail"]
    assert _counts(foundation.database) == {
        table: 0 for table in PHASE9_TABLES
    }


def test_evidence_directory_replacement_while_descriptor_is_open_fails_closed(
    tmp_path,
    monkeypatch,
):
    foundation, root, request, _service = _fixture(tmp_path)
    original = replay_module._StableDirectoryTree.directory
    replaced = False

    def open_and_replace(tree, parts):
        nonlocal replaced
        descriptor = original(tree, parts)
        if parts == ("receipts",) and not replaced:
            replaced = True
            target = root / "receipts"
            preserved = root.parent / "receipts.preserved"
            os.replace(target, preserved)
            target.mkdir()
        return descriptor

    monkeypatch.setattr(
        replay_module._StableDirectoryTree,
        "directory",
        open_and_replace,
    )
    result = preflight_phase9_forensic_replay(
        request, evidence_root=root, trusted_now=request.occurred_at
    )
    assert result["status"] == "BLOCKED"
    assert "directory changed while being verified" in result["blockers"][0]["detail"]
    assert _counts(foundation.database) == {
        table: 0 for table in PHASE9_TABLES
    }


def test_request_round_trip_is_identity_stable(tmp_path):
    _foundation, _root, request, _service = _fixture(tmp_path)
    decoded = phase9_forensic_replay_request_from_dict(request.as_dict())
    assert decoded == request
    assert decoded.request_sha256 == request.request_sha256


def test_bare_process_scope_digest_cannot_replace_a_typed_receipt(tmp_path):
    foundation, root, request, _service = _fixture(tmp_path)
    path = root / "outbox_supervisor.json"
    body = json.loads(path.read_text())
    body["process_scope_receipts"]["failed"] = "a" * 64
    path.write_bytes(canonical_bytes(body))
    changed = _refresh_start_authorization(root, request)

    result = preflight_phase9_forensic_replay(
        changed, evidence_root=root, trusted_now=changed.occurred_at
    )
    assert result["status"] == "BLOCKED"
    assert "must be one JSON object" in result["blockers"][0]["detail"]
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


@pytest.mark.parametrize(
    "logical_path",
    (
        "receipts/roles/execution.provider.json",
        "receipts/roles/execution.process.json",
        "receipts/process-scopes/pause.json",
        f"receipts/acceptance/{EXPECTED_PHASE9_ACCEPTANCE_CASES[0]}.json",
    ),
    ids=("role-provider", "role-process", "process-scope", "acceptance-case"),
)
def test_every_typed_receipt_category_requires_its_external_file(
    tmp_path,
    logical_path,
):
    foundation, root, request, service = _fixture(tmp_path)
    (root / logical_path).unlink()
    changed = _refresh_start_authorization(root, request)

    result = preflight_phase9_forensic_replay(
        changed, evidence_root=root, trusted_now=changed.occurred_at
    )
    assert result["status"] == "BLOCKED"
    assert "file binding differs" in result["blockers"][0]["detail"]
    with pytest.raises(Phase9ForensicReplaySafetyError):
        service.execute(changed)
    assert _counts(foundation.database) == {
        table: 0 for table in PHASE9_TABLES
    }


def test_casefold_alias_of_a_typed_receipt_is_rejected_before_mutation(tmp_path):
    foundation, root, request, service = _fixture(tmp_path)
    original = root / "receipts/process-scopes/pause.json"
    alias = original.with_name("PAUSE.json")
    alias.write_bytes(original.read_bytes())
    changed = _refresh_start_authorization(root, request)

    with pytest.raises(
        Phase9ForensicReplaySafetyError,
        match="Unicode or case-folding ambiguity",
    ):
        service.execute(changed)
    assert _counts(foundation.database) == {
        table: 0 for table in PHASE9_TABLES
    }


@pytest.mark.parametrize(
    ("changes", "detail"),
    (
        ({"run_generation": "run-generation:other"}, "coordinate differs"),
        ({"scope_kind": "PROJECT"}, "result differs"),
        (
            {
                "dependency_fingerprint_sha256": "0" * 64,
                "event_id": _evidence_event_id(
                    receipt_kind="PROCESS_SCOPE",
                    logical_id="pause",
                    dependency="0" * 64,
                ),
            },
            "provenance differs",
        ),
    ),
    ids=("cross-generation", "cross-scope", "dependency-fingerprint"),
)
def test_rehashed_process_receipt_cannot_cross_its_boundaries(
    tmp_path,
    changes,
    detail,
):
    foundation, root, request, service = _fixture(tmp_path)
    changed = _rewrite_process_scope_receipt(
        root,
        request,
        action="pause",
        changes=changes,
    )

    result = preflight_phase9_forensic_replay(
        changed, evidence_root=root, trusted_now=changed.occurred_at
    )
    assert result["status"] == "BLOCKED"
    assert detail in result["blockers"][0]["detail"]
    with pytest.raises(Phase9ForensicReplaySafetyError):
        service.execute(changed)
    assert _counts(foundation.database) == {
        table: 0 for table in PHASE9_TABLES
    }


@pytest.mark.parametrize(
    "field",
    (
        "pending_outbox_count",
        "uncertain_automatic_resend_count",
        "active_descendant_count",
    ),
)
def test_runtime_scalar_nonzero_cannot_disagree_with_zero_typed_scope_results(
    tmp_path,
    field,
):
    foundation, root, request, service = _fixture(tmp_path)
    runtime_path = root / "outbox_supervisor.json"
    runtime = json.loads(runtime_path.read_text())
    runtime[field] = 1
    runtime_path.write_bytes(canonical_bytes(runtime))
    changed = _refresh_component_receipt(root, request, kind="OUTBOX")

    result = preflight_phase9_forensic_replay(
        changed, evidence_root=root, trusted_now=changed.occurred_at
    )
    assert result["status"] == "BLOCKED"
    assert any(item["code"] == field.upper() for item in result["blockers"])
    with pytest.raises(Phase9ForensicReplaySafetyError, match="BLOCKED"):
        service.execute(changed)
    assert _counts(foundation.database) == {
        table: 0 for table in PHASE9_TABLES
    }


def test_packet_claims_are_derived_from_exact_packet_v2_bytes(tmp_path):
    foundation, root, request, _service = _fixture(tmp_path)
    packet_path = root / "packet.json"
    packet = json.loads(packet_path.read_text())
    packet["required_claims"] = ["forged-claim"]
    packet["present_claims"] = ["forged-claim"]
    packet_path.write_bytes(canonical_bytes(packet))
    changed = _refresh_start_authorization(root, request)

    result = preflight_phase9_forensic_replay(
        changed, evidence_root=root, trusted_now=changed.occurred_at
    )
    assert result["status"] == "BLOCKED"
    assert "differs from exact packet-v2 bytes" in result["blockers"][0]["detail"]
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


def test_rehashed_fixture_domain_process_receipt_is_not_formal_evidence(tmp_path):
    foundation, root, request, _service = _fixture(tmp_path)
    receipt_path = root / "receipts/process-scopes/pause.json"
    receipt = json.loads(receipt_path.read_text())
    receipt.pop("receipt_sha256")
    receipt["producer"]["execution_domain"] = "TEST_FIXTURE"
    receipt_reference = _write_hashed_json(
        root,
        "receipts/process-scopes/pause.json",
        receipt,
        "receipt_sha256",
    )
    runtime_path = root / "outbox_supervisor.json"
    runtime = json.loads(runtime_path.read_text())
    runtime["process_scope_receipts"]["pause"] = receipt_reference
    runtime_path.write_bytes(canonical_bytes(runtime))
    changed = _refresh_start_authorization(root, request)

    result = preflight_phase9_forensic_replay(
        changed, evidence_root=root, trusted_now=changed.occurred_at
    )
    assert result["status"] == "BLOCKED"
    assert "producer differs" in result["blockers"][0]["detail"]
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


def test_role_generation_is_derived_from_current_replay_not_self_reported(tmp_path):
    foundation, root, request, _service = _fixture(tmp_path)
    role = "execution"
    provider_path = root / f"receipts/roles/{role}.provider.json"
    provider = json.loads(provider_path.read_text())
    provider.pop("receipt_sha256")
    provider["role_generation"] = "old-role-generation"
    provider_reference = _write_hashed_json(
        root,
        f"receipts/roles/{role}.provider.json",
        provider,
        "receipt_sha256",
    )

    process_path = root / f"receipts/roles/{role}.process.json"
    process = json.loads(process_path.read_text())
    process.pop("receipt_sha256")
    process["role_generation"] = "old-role-generation"
    process["provider_receipt"] = provider_reference
    process["predecessor_receipt_sha256"] = provider_reference[
        "receipt_sha256"
    ]
    process_reference = _write_hashed_json(
        root,
        f"receipts/roles/{role}.process.json",
        process,
        "receipt_sha256",
    )

    roles_path = root / "roles.json"
    roles = json.loads(roles_path.read_text())
    roles["roles"][0]["role_generation"] = "old-role-generation"
    roles["roles"][0]["process_receipt"] = process_reference
    roles_path.write_bytes(canonical_bytes(roles))
    changed = _refresh_start_authorization(root, request)

    result = preflight_phase9_forensic_replay(
        changed, evidence_root=root, trusted_now=changed.occurred_at
    )
    assert result["status"] == "BLOCKED"
    assert "role provider receipt binding differs" in result["blockers"][0]["detail"]
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


def test_rehashed_renamed_role_output_cannot_claim_fresh_with_predecessor_provenance(
    tmp_path,
):
    foundation, root, request, service = _fixture(tmp_path)
    role = "execution"
    original_output = root / f"roles/{role}.out"
    old_output_path = f"roles/{role}.predecessor.out"
    (root / old_output_path).write_bytes(original_output.read_bytes())
    original_output.unlink()

    provider_path = root / f"receipts/roles/{role}.provider.json"
    provider = json.loads(provider_path.read_text())
    provider.pop("receipt_sha256")
    provider["output_path"] = old_output_path
    provider["inherited"] = False
    provider["predecessor_role_generation"] = "phase9-role-generation:old"
    provider["predecessor_event_id"] = "phase9-evidence-event:old"
    provider["predecessor_receipt_sha256"] = "1" * 64
    provider_reference = _write_hashed_json(
        root,
        f"receipts/roles/{role}.provider.json",
        provider,
        "receipt_sha256",
    )

    process_path = root / f"receipts/roles/{role}.process.json"
    process = json.loads(process_path.read_text())
    process.pop("receipt_sha256")
    process["output_path"] = old_output_path
    process["inherited"] = False
    process["predecessor_role_generation"] = "phase9-role-generation:old"
    process["predecessor_receipt_sha256"] = provider_reference[
        "receipt_sha256"
    ]
    process["provider_receipt"] = provider_reference
    process_reference = _write_hashed_json(
        root,
        f"receipts/roles/{role}.process.json",
        process,
        "receipt_sha256",
    )

    roles_path = root / "roles.json"
    roles = json.loads(roles_path.read_text())
    roles["roles"][0]["output_path"] = old_output_path
    roles["roles"][0]["inherited"] = False
    roles["roles"][0]["process_receipt"] = process_reference
    roles_path.write_bytes(canonical_bytes(roles))
    changed = _refresh_start_authorization(root, request)

    result = preflight_phase9_forensic_replay(
        changed, evidence_root=root, trusted_now=changed.occurred_at
    )
    assert result["status"] == "BLOCKED"
    assert "binding differs" in result["blockers"][0]["detail"]
    with pytest.raises(Phase9ForensicReplaySafetyError):
        service.execute(changed)
    assert _counts(foundation.database) == {
        table: 0 for table in PHASE9_TABLES
    }


@pytest.mark.parametrize(
    "raw_log, expected_detail",
    [
        (b"PASS AC-DEL-001\n", "collected count"),
        (
            (
                "collected 1 item\n"
                f"{PHASE9_ACCEPTANCE_TEST_NODES['AC-DEL-001']} "
                "PASSED [100%]\n"
            ).encode(),
            "terminal pytest summary",
        ),
        (
            (
                "============================= test session starts "
                "==============================\n"
                "collected 1 item\n\n"
                f"{PHASE9_ACCEPTANCE_TEST_NODES['AC-DEL-001']} "
                "ERROR [100%]\n\n"
                "==================================== ERRORS "
                "====================================\n"
                "=============================== 1 error in 0.01s "
                "================================\n"
            ).encode(),
            "exact reviewed passing node inventory",
        ),
    ],
)
def test_acceptance_pass_cannot_be_self_reported_over_a_nonpassing_raw_log(
    tmp_path, raw_log, expected_detail
):
    foundation, root, request, _service = _fixture(tmp_path)
    changed = _rewrite_acceptance_case_raw(
        root,
        request,
        case_id="AC-DEL-001",
        raw=raw_log,
    )

    result = preflight_phase9_forensic_replay(
        changed, evidence_root=root, trusted_now=changed.occurred_at
    )
    assert result["status"] == "BLOCKED"
    assert expected_detail in result["blockers"][0]["detail"]
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


@pytest.mark.parametrize("runner_path", ["working-directory", "python"])
def test_rehashed_acceptance_command_requires_the_live_formal_runner(
    tmp_path, runner_path
):
    foundation, root, request, service = _fixture(tmp_path)
    missing = str(tmp_path / f"missing-{runner_path}")
    changed = _rewrite_acceptance_command_runner(
        root,
        request,
        case_id="AC-DEL-001",
        working_directory=missing if runner_path == "working-directory" else None,
        python_executable=missing if runner_path == "python" else None,
    )
    with pytest.raises(
        Phase9ForensicReplayConflict,
        match=(
            "working directory differs"
            if runner_path == "working-directory"
            else "Python executable bytes differ"
        ),
    ):
        service.execute(changed)
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


def test_missing_external_acceptance_receipt_is_blocked(tmp_path):
    foundation, root, request, _service = _fixture(tmp_path)
    case_id = EXPECTED_PHASE9_ACCEPTANCE_CASES[0]
    (root / f"receipts/acceptance/{case_id}.json").unlink()
    changed = _refresh_start_authorization(root, request)

    result = preflight_phase9_forensic_replay(
        changed, evidence_root=root, trusted_now=changed.occurred_at
    )
    assert result["status"] == "BLOCKED"
    assert "file binding differs" in result["blockers"][0]["detail"]
    with pytest.raises(Phase9ForensicReplaySafetyError, match="file binding differs"):
        service = Phase9ForensicReplayService(
            foundation.database,
            expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
            source_repository=_source_repository(), evidence_root=root,
            official_input_root=_service.official_input_root,
            execution_context_receipt_path=(
                _service.execution_context_receipt_path
            ),
            clock=lambda: changed.occurred_at,
        )
        service.execute(changed)
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


def test_cross_project_acceptance_receipt_is_blocked_even_when_rehashed(tmp_path):
    foundation, root, request, _service = _fixture(tmp_path)
    case_id = EXPECTED_PHASE9_ACCEPTANCE_CASES[0]
    receipt_path = root / f"receipts/acceptance/{case_id}.json"
    receipt = json.loads(receipt_path.read_text())
    receipt.pop("receipt_sha256")
    receipt["project_id"] = "different-project"
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    receipt_path.write_bytes(canonical_bytes(receipt))
    raw = receipt_path.read_bytes()
    reference = {
        "logical_path": receipt_path.relative_to(root).as_posix(),
        "byte_length": len(raw),
        "raw_bytes_sha256": hashlib.sha256(raw).hexdigest(),
        "receipt_sha256": receipt["receipt_sha256"],
    }
    acceptance_path = root / "acceptance.json"
    acceptance = json.loads(acceptance_path.read_text())
    acceptance["cases"][0]["receipt"] = reference
    acceptance_path.write_bytes(canonical_bytes(acceptance))
    changed = _refresh_start_authorization(root, request)

    result = preflight_phase9_forensic_replay(
        changed, evidence_root=root, trusted_now=changed.occurred_at
    )
    assert result["status"] == "BLOCKED"
    assert "coordinate differs" in result["blockers"][0]["detail"]
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


def test_ready_gate_is_recollected_before_any_phase9_mutation(tmp_path):
    foundation, _root, request, service = _fixture(tmp_path)
    connection = sqlite3.connect(foundation.database)
    try:
        connection.execute(
            """
            INSERT INTO solver_jobs(
                job_id, job_revision, backend, runtime, script, workdir,
                argv_json, max_time_seconds, status, requested_at,
                result_refs_json
            ) VALUES ('phase9-late-active-job', 1, 'local', 'python', 'x.py', '.',
                      '[]', 1, 'running', 1, '{}')
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(Phase9ForensicReplayConflict, match="live Phase9 entry state"):
        service.execute(request)
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


def test_live_gate_is_recollected_again_before_commit(tmp_path, monkeypatch):
    foundation, _root, request, service = _fixture(tmp_path)
    original = replay_module.collect_phase9_entry_state_in_transaction
    calls = 0

    def collect(*args, **kwargs):
        nonlocal calls
        calls += 1
        state = original(*args, **kwargs)
        if calls == 2:
            assert args[0].execute(
                "SELECT COUNT(*) FROM "
                "authority_production_phase9_terminal_receipts"
            ).fetchone()[0] == 0
            return replace(state, active_process_count=1)
        return state

    monkeypatch.setattr(
        replay_module, "collect_phase9_entry_state_in_transaction", collect
    )
    with pytest.raises(Phase9ForensicReplayConflict, match="live Phase9 entry state"):
        service.execute(request)
    assert calls == 2
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


@pytest.mark.parametrize(
    "boundary_call",
    (1, 2),
    ids=("transaction-start", "preterminal"),
)
@pytest.mark.parametrize(
    "drift",
    (
        {"pending_outbox_count": 1},
        {"pending_outbox_count": 1, "old_generation_post_boundary_event_count": 1},
        {"unresolved_migration_count": 1},
        {"run_generation": "run-generation:stale-current-pointer"},
    ),
    ids=(
        "pending-outbox",
        "uncertain-dispatch",
        "unresolved-migration",
        "predecessor-current-pointer",
    ),
)
def test_live_gate_named_state_drift_fails_closed_at_each_boundary(
    tmp_path,
    monkeypatch,
    boundary_call,
    drift,
):
    foundation, _root, request, service = _fixture(tmp_path)
    original = replay_module.collect_phase9_entry_state_in_transaction
    calls = 0

    def collect(*args, **kwargs):
        nonlocal calls
        calls += 1
        state = original(*args, **kwargs)
        if calls == boundary_call:
            return replace(state, **drift)
        return state

    monkeypatch.setattr(
        replay_module, "collect_phase9_entry_state_in_transaction", collect
    )
    with pytest.raises(
        Phase9ForensicReplayConflict,
        match="live Phase9 entry state",
    ):
        service.execute(request)
    assert calls == boundary_call
    assert _counts(foundation.database) == {
        table: 0 for table in PHASE9_TABLES
    }


def test_start_authorization_expiry_is_rechecked_before_commit(tmp_path):
    foundation, root, request, _service = _fixture(tmp_path)
    # Formal authorization TTL and permitted request-clock skew are both 300s.
    # Crossing expiry therefore trips the request-age fence first; either way,
    # the second in-transaction trusted-time read must roll everything back.
    expired_now = request.occurred_at + 301
    clock_values = iter((request.occurred_at, expired_now))
    service = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        evidence_root=root,
        official_input_root=_service.official_input_root,
        execution_context_receipt_path=(
            _service.execution_context_receipt_path
        ),
        clock=lambda: next(clock_values, expired_now),
    )
    with pytest.raises(
        Phase9ForensicReplaySafetyError,
        match="request occurrence metadata exceeds trusted clock skew",
    ):
        service.execute(request)
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


def test_trusted_clock_regression_before_commit_rolls_back(tmp_path):
    foundation, root, request, _service = _fixture(tmp_path)
    clock_values = iter((request.occurred_at, request.occurred_at - 1))
    service = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        evidence_root=root,
        official_input_root=_service.official_input_root,
        execution_context_receipt_path=(
            _service.execution_context_receipt_path
        ),
        clock=lambda: next(clock_values),
    )
    with pytest.raises(Phase9ForensicReplayConflict, match="time moved backwards"):
        service.execute(request)
    assert _counts(foundation.database) == {table: 0 for table in PHASE9_TABLES}


def test_gate_consumption_is_durable_and_exact_replay_does_not_consume_twice(tmp_path):
    foundation, _root, request, service = _fixture(tmp_path)
    first = service.execute(request)
    replayed = service.execute(request)
    assert replayed == first
    assert replayed.as_dict() == first.as_dict()
    connection = sqlite3.connect(foundation.database)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM authority_production_phase9_gate_consumptions"
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == 1
    assert rows[0]["gate_result_sha256"] == request.entry_gate_result_sha256
    assert rows[0]["request_sha256"] == request.request_sha256
    assert rows[0]["replay_id"] == request.replay_id


@pytest.mark.parametrize("closed_gate", ("authorization_expired", "request_stale"))
def test_exact_replay_survives_closed_new_write_time_gates(tmp_path, closed_gate):
    """Committed recovery precedes freshness and one-use authorization gates."""

    foundation, root, request, service = _fixture(tmp_path)
    if closed_gate == "authorization_expired":
        request = _shorten_start_authorization(
            foundation,
            root,
            request,
            expires_at=request.occurred_at + 50,
        )
    first = service.execute(request)
    before_bytes = foundation.database.read_bytes()
    before_counts = _counts(foundation.database)
    connection = sqlite3.connect(foundation.database)
    try:
        stored_receipt_before = connection.execute(
            "SELECT receipt_json, receipt_sha256 FROM "
            "authority_production_phase9_terminal_receipts WHERE replay_id=?",
            (request.replay_id,),
        ).fetchone()
    finally:
        connection.close()
    before_files = _database_family_snapshot(foundation.database)
    late = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        evidence_root=root,
        official_input_root=service.official_input_root,
        execution_context_receipt_path=service.execution_context_receipt_path,
        clock=(
            (lambda: request.occurred_at + 51)
            if closed_gate == "authorization_expired"
            else (lambda: request.occurred_at + 301)
        ),
    )

    replayed = late.execute(request)

    assert replayed == first
    assert replayed.as_dict() == first.as_dict()
    assert foundation.database.read_bytes() == before_bytes
    assert _database_family_snapshot(foundation.database) == before_files
    assert _counts(foundation.database) == before_counts
    connection = sqlite3.connect(foundation.database)
    try:
        assert connection.execute(
            "SELECT receipt_json, receipt_sha256 FROM "
            "authority_production_phase9_terminal_receipts WHERE replay_id=?",
            (request.replay_id,),
        ).fetchone() == stored_receipt_before
        assert connection.execute(
            "SELECT COUNT(*) FROM "
            "authority_production_phase9_start_authorization_consumptions"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM authority_production_phase9_gate_consumptions"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_exact_replay_uses_stored_operator_identity_not_the_current_os_account(
    tmp_path,
    monkeypatch,
):
    foundation, _root, request, service = _fixture(tmp_path)
    first = service.execute(request)
    before = foundation.database.read_bytes()

    monkeypatch.setattr(replay_module.os, "geteuid", lambda: 999_999)
    monkeypatch.setattr(
        replay_module.pwd,
        "getpwuid",
        lambda _uid: type("Account", (), {"pw_name": "different-account"})(),
    )

    assert service.execute(request) == first
    assert foundation.database.read_bytes() == before


@pytest.mark.parametrize(
    "closed_gate",
    ("authorization_expired", "request_stale"),
)
def test_confirmed_forensic_cli_recovers_exact_commit_before_live_time_gates(
    tmp_path,
    closed_gate,
):
    foundation, root, request, service = _fixture(tmp_path)
    if closed_gate == "authorization_expired":
        request = _shorten_start_authorization(
            foundation,
            root,
            request,
            expires_at=request.occurred_at + 50,
        )
    first = service.execute(request)
    before_bytes = foundation.database.read_bytes()
    before_files = _database_family_snapshot(foundation.database)
    before_counts = _counts(foundation.database)
    request_path = tmp_path / f"forensic-{closed_gate}.json"
    request_path.write_bytes(canonical_bytes(request.as_dict()))
    trusted_now = request.occurred_at + (
        51 if closed_gate == "authorization_expired" else 301
    )
    clock_hook = tmp_path / f"clock-{closed_gate}"
    clock_hook.mkdir()
    (clock_hook / "sitecustomize.py").write_text(
        "import time\n"
        f"time.time = lambda: {trusted_now}\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(clock_hook),
        "PHASE9_ENABLED": "true",
        "PHASE9_AUTHORITY_DB_FILE": str(foundation.database),
        "PHASE9_AUTHORITY_SOURCE_FENCE_SHA256": (
            foundation.preflight.source_fence_sha256
        ),
        "PHASE9_SOURCE_REPOSITORY": str(_source_repository()),
        "PHASE9_EVIDENCE_ROOT": str(root),
        "PHASE9_OFFICIAL_INPUT_ROOT": str(service.official_input_root),
        "PHASE9_EXECUTION_CONTEXT_RECEIPT": str(
            service.execution_context_receipt_path
        ),
    }

    command = [
        os.sys.executable,
        "-B",
        str(_source_repository() / "scripts" / "phase9_forensic_replay.py"),
        "execute",
        "--request",
        str(request_path),
        "--confirm",
    ]
    completed = subprocess.run(
        command,
        cwd=_source_repository(),
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == first.as_dict()
    assert foundation.database.read_bytes() == before_bytes
    assert _database_family_snapshot(foundation.database) == before_files
    assert _counts(foundation.database) == before_counts

    if closed_gate == "authorization_expired":
        different = request.as_dict()
        different["delivery_capability"] = "ENABLED"
        request_path.write_bytes(canonical_bytes(different))
        conflict = subprocess.run(
            command,
            cwd=_source_repository(),
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert conflict.returncode == 2
        assert "idempotency key has different request bytes" in conflict.stderr
        assert foundation.database.read_bytes() == before_bytes
        assert _database_family_snapshot(foundation.database) == before_files
        assert _counts(foundation.database) == before_counts

        malformed = request.as_dict()
        malformed["evidence_files"][0]["raw_bytes_sha256"] = "not-a-sha256"
        request_path.write_bytes(canonical_bytes(malformed))
        malformed_conflict = subprocess.run(
            command,
            cwd=_source_repository(),
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert malformed_conflict.returncode == 2
        assert (
            "idempotency key has different request bytes"
            in malformed_conflict.stderr
        )
        assert foundation.database.read_bytes() == before_bytes
        assert _database_family_snapshot(foundation.database) == before_files
        assert _counts(foundation.database) == before_counts


@pytest.mark.parametrize(
    ("offset", "message"),
    (
        pytest.param(51, "not valid at trusted current time", id="authorization_expired"),
        pytest.param(
            301,
            "request occurrence metadata exceeds trusted clock skew",
            id="request_stale",
        ),
    ),
)
def test_new_replay_still_rejects_closed_time_gates_without_mutation(
    tmp_path,
    offset,
    message,
):
    foundation, root, request, service = _fixture(tmp_path)
    if offset == 51:
        request = _shorten_start_authorization(
            foundation,
            root,
            request,
            expires_at=request.occurred_at + 50,
        )
    before_bytes = foundation.database.read_bytes()
    before_counts = _counts(foundation.database)
    before_files = _database_family_snapshot(foundation.database)
    late = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        evidence_root=root,
        official_input_root=service.official_input_root,
        execution_context_receipt_path=service.execution_context_receipt_path,
        clock=lambda: request.occurred_at + offset,
    )

    with pytest.raises(Phase9ForensicReplaySafetyError, match=message):
        late.execute(request)

    assert foundation.database.read_bytes() == before_bytes
    assert _database_family_snapshot(foundation.database) == before_files
    assert _counts(foundation.database) == before_counts


def test_unsafe_snapshot_state_is_a_domain_conflict_without_mutation(tmp_path):
    foundation, _root, request, service = _fixture(tmp_path)
    journal = Path(f"{foundation.database}-journal")
    journal.write_bytes(b"ambiguous-hot-journal")
    before = _database_family_snapshot(foundation.database)

    with pytest.raises(
        Phase9ForensicReplayConflict,
        match="Authority state snapshot cannot be read safely",
    ):
        service.execute(request)

    assert _database_family_snapshot(foundation.database) == before


@pytest.mark.parametrize("damage", ("missing", "moved_key", "moved_workflow"))
def test_idempotency_miss_cannot_fall_through_an_existing_replay(
    tmp_path,
    damage,
):
    foundation, _root, request, service = _fixture(tmp_path)
    service.execute(request)
    table = "authority_production_phase9_replay_idempotency"
    connection = sqlite3.connect(foundation.database)
    try:
        triggers = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND tbl_name=?",
            (table,),
        ).fetchall()
        for name, _sql in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        if damage == "missing":
            connection.execute(
                f"DELETE FROM {table} WHERE workflow_id=? AND idempotency_key=?",
                (request.workflow_id, request.idempotency_key),
            )
        elif damage == "moved_key":
            connection.execute(
                f"UPDATE {table} SET idempotency_key='phase9-replay-key-moved' "
                "WHERE workflow_id=? AND idempotency_key=?",
                (request.workflow_id, request.idempotency_key),
            )
        else:
            connection.execute(
                f"UPDATE {table} SET workflow_id='workflow-id-moved' "
                "WHERE workflow_id=? AND idempotency_key=?",
                (request.workflow_id, request.idempotency_key),
            )
        for _name, sql in triggers:
            connection.execute(sql)
        connection.commit()
    finally:
        connection.close()
    service.evidence_root = tmp_path / "evidence-no-longer-live"
    service._clock = lambda: request.occurred_at + 301
    before = _database_family_snapshot(foundation.database)

    with pytest.raises(
        Phase9ForensicReplayConflict,
        match="idempotency",
    ):
        service.execute(request)

    assert _database_family_snapshot(foundation.database) == before


def test_orphaned_replay_request_reserves_global_key_across_workflows(
    tmp_path,
):
    foundation, _root, request, service = _fixture(tmp_path)
    service.execute(request)
    table = "authority_production_phase9_replay_idempotency"
    connection = sqlite3.connect(foundation.database)
    try:
        triggers = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND tbl_name=?",
            (table,),
        ).fetchall()
        for name, _sql in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(
            f"DELETE FROM {table} WHERE workflow_id=? AND idempotency_key=?",
            (request.workflow_id, request.idempotency_key),
        )
        for _name, sql in triggers:
            connection.execute(sql)
        connection.commit()
    finally:
        connection.close()
    changed = replace(request, workflow_id="workflow-orphaned-global-key")
    changed_service = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        evidence_root=tmp_path / "missing-orphaned-key-evidence",
        official_input_root=tmp_path / "missing-orphaned-key-input",
        execution_context_receipt_path=(tmp_path / "missing-orphaned-key-context"),
        clock=lambda: changed.occurred_at + 301,
    )
    before = _database_family_snapshot(foundation.database)

    with pytest.raises(
        Phase9ForensicReplayConflict,
        match="trace lacks its exact global idempotency key binding",
    ):
        changed_service.execute(changed)

    assert _database_family_snapshot(foundation.database) == before
    assert _counts(foundation.database)["authority_production_phase9_replays"] == 1


@pytest.mark.parametrize(
    "damage",
    (
        "terminal_receipt",
        "terminal_event",
        "missing_typed_receipt",
        "missing_generation_succession",
        "missing_generation_current",
        "missing_replay_current",
        "source_inventory_recorded_at",
        "missing_business_object",
        "idempotency_alias",
    ),
)
def test_exact_recovery_rejects_corrupt_or_incomplete_terminal_graph(
    tmp_path,
    damage,
):
    foundation, _root, request, service = _fixture(tmp_path)
    service.execute(request)
    table = {
        "terminal_receipt": "authority_production_phase9_terminal_receipts",
        "terminal_event": "authority_production_phase9_replay_events",
        "missing_typed_receipt": "authority_production_phase9_evidence_receipts",
        "missing_generation_succession": (
            "authority_production_run_generation_successions"
        ),
        "missing_generation_current": (
            "authority_production_run_generation_current"
        ),
        "missing_replay_current": "authority_production_phase9_replay_current",
        "source_inventory_recorded_at": (
            "authority_production_run_generation_source_inventories"
        ),
        "missing_business_object": "authority_production_phase9_replays",
        "idempotency_alias": "authority_production_phase9_replay_idempotency",
    }[damage]
    connection = sqlite3.connect(foundation.database)
    try:
        triggers = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND tbl_name=?",
            (table,),
        ).fetchall()
        for name, _sql in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        if damage == "terminal_receipt":
            connection.execute(
                "UPDATE authority_production_phase9_terminal_receipts "
                "SET receipt_json='{}' WHERE replay_id=?",
                (request.replay_id,),
            )
        elif damage == "terminal_event":
            connection.execute(
                "UPDATE authority_production_phase9_replay_events "
                "SET event_json='{}' WHERE replay_id=? AND sequence=6",
                (request.replay_id,),
            )
        elif damage == "missing_typed_receipt":
            connection.execute(
                "DELETE FROM authority_production_phase9_evidence_receipts "
                "WHERE replay_id=? AND receipt_kind='PACKET'",
                (request.replay_id,),
            )
        elif damage == "missing_generation_succession":
            connection.execute(
                "DELETE FROM authority_production_run_generation_successions "
                "WHERE run_generation=?",
                (request.run_generation,),
            )
        elif damage == "missing_generation_current":
            connection.execute(
                "DELETE FROM authority_production_run_generation_current "
                "WHERE workflow_id=?",
                (request.workflow_id,),
            )
        elif damage == "missing_replay_current":
            connection.execute(
                "DELETE FROM authority_production_phase9_replay_current "
                "WHERE workflow_id=?",
                (request.workflow_id,),
            )
        elif damage == "source_inventory_recorded_at":
            connection.execute(
                "UPDATE authority_production_run_generation_source_inventories "
                "SET recorded_at=0 WHERE inventory_sha256=?",
                (request.source_inventory_sha256,),
            )
        elif damage == "idempotency_alias":
            connection.execute(
                "INSERT INTO authority_production_phase9_replay_idempotency "
                "SELECT workflow_id, 'phase9-replay-key-alias', request_sha256, "
                "replay_id, terminal_receipt_sha256 FROM "
                "authority_production_phase9_replay_idempotency "
                "WHERE workflow_id=? AND idempotency_key=?",
                (request.workflow_id, request.idempotency_key),
            )
        else:
            connection.execute(
                "DELETE FROM authority_production_phase9_replays WHERE replay_id=?",
                (request.replay_id,),
            )
        for _name, sql in triggers:
            connection.execute(sql)
        connection.commit()
    finally:
        connection.close()
    damaged_bytes = foundation.database.read_bytes()
    damaged_counts = _counts(foundation.database)

    with pytest.raises(Phase9ForensicReplayConflict):
        service.execute(request)

    assert foundation.database.read_bytes() == damaged_bytes
    assert _counts(foundation.database) == damaged_counts


def test_exact_recovery_rejects_cross_workflow_dangling_successor(tmp_path):
    """A successor edge cannot be hidden by assigning it another workflow."""

    foundation, _root, request, service = _fixture(tmp_path)
    service.execute(request)
    connection = sqlite3.connect(foundation.database)
    connection.row_factory = sqlite3.Row
    table = "authority_production_phase9_replays"
    try:
        triggers = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND tbl_name=?",
            (table,),
        ).fetchall()
        for name, _sql in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        source = dict(
            connection.execute(
                f"SELECT * FROM {table} WHERE replay_id=?",
                (request.replay_id,),
            ).fetchone()
        )
        source.update(
            replay_id="phase9-replay:cross-workflow-dangling",
            workflow_id="workflow-cross-dangling",
            run_generation="run-generation:cross-workflow-dangling",
            operation_kind="ROTATE",
            predecessor_replay_id=request.replay_id,
            predecessor_terminal_receipt_sha256=connection.execute(
                "SELECT receipt_sha256 FROM "
                "authority_production_phase9_terminal_receipts "
                "WHERE replay_id=?",
                (request.replay_id,),
            ).fetchone()[0],
            request_sha256="d" * 64,
        )
        columns = tuple(source)
        connection.execute(
            f"INSERT INTO {table}({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            tuple(source[name] for name in columns),
        )
        for _name, sql in triggers:
            connection.execute(sql)
        connection.commit()
    finally:
        connection.close()
    damaged = _database_family_snapshot(foundation.database)

    with pytest.raises(
        Phase9ForensicReplayConflict, match="dangling successor"
    ):
        service.execute(request)

    assert _database_family_snapshot(foundation.database) == damaged


def test_two_concurrent_identical_replays_commit_once_and_recover_once(tmp_path):
    foundation, root, request, first_service = _fixture(tmp_path)
    second_service = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        evidence_root=root,
        official_input_root=first_service.official_input_root,
        execution_context_receipt_path=first_service.execution_context_receipt_path,
        clock=lambda: request.occurred_at,
    )
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def invoke(service):
        try:
            barrier.wait(timeout=10)
            results.append(service.execute(request))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    workers = [
        threading.Thread(target=invoke, args=(first_service,)),
        threading.Thread(target=invoke, args=(second_service,)),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=120)

    assert errors == []
    assert len(results) == 2
    assert all(result.replayed is False for result in results)
    assert results[0] == results[1]
    assert results[0].as_dict() == results[1].as_dict()
    assert _counts(foundation.database) == {
        "authority_production_phase9_replays": 1,
        "authority_production_phase9_replay_events": 6,
        "authority_production_phase9_terminal_receipts": 1,
        "authority_production_phase9_replay_idempotency": 1,
        "authority_production_phase9_replay_current": 1,
        "authority_production_phase9_evidence_receipts": 30,
        "authority_production_phase9_gate_consumptions": 1,
    }


def test_exact_peer_commit_queued_on_lease_preempts_closed_live_gates(
    tmp_path,
):
    foundation, root, request, first_service = _fixture(tmp_path)
    first_inside_transaction = threading.Event()
    second_started = threading.Event()

    def pause_first(checkpoint):
        if checkpoint == "after_live_gate_start":
            first_inside_transaction.set()
            assert second_started.wait(timeout=30)

    first_service.fault_hook = pause_first
    second_service = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        evidence_root=root,
        official_input_root=first_service.official_input_root,
        execution_context_receipt_path=first_service.execution_context_receipt_path,
        clock=lambda: request.occurred_at + 301,
    )
    second_has_lease = threading.Event()
    allow_recovery = threading.Event()
    original_recover = second_service._recover_committed

    def pause_after_lease(value):
        second_has_lease.set()
        assert allow_recovery.wait(timeout=30)
        return original_recover(value)

    second_service._recover_committed = pause_after_lease
    outcomes = []
    errors = []

    def invoke(service, *, mark_started=False):
        try:
            if mark_started:
                second_started.set()
            outcomes.append(service.execute(request))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    second_service.evidence_root = tmp_path / "evidence-no-longer-live"
    first_worker = threading.Thread(target=invoke, args=(first_service,))
    second_worker = threading.Thread(
        target=invoke, args=(second_service,), kwargs={"mark_started": True}
    )
    first_worker.start()
    assert first_inside_transaction.wait(timeout=60)
    second_worker.start()
    assert second_has_lease.wait(timeout=60)
    before = _database_family_snapshot(foundation.database)
    allow_recovery.set()
    first_worker.join(timeout=120)
    second_worker.join(timeout=120)

    assert not first_worker.is_alive()
    assert not second_worker.is_alive()
    assert errors == []
    assert len(outcomes) == 2
    assert all(result.replayed is False for result in outcomes)
    assert outcomes[0] == outcomes[1]
    assert outcomes[0].as_dict() == outcomes[1].as_dict()
    assert _database_family_snapshot(foundation.database) == before


def test_queued_same_key_cross_workflow_request_cannot_overwrite_commit(
    tmp_path,
):
    foundation, root, request, first_service = _fixture(tmp_path)
    first_inside_transaction = threading.Event()
    second_started = threading.Event()

    def pause_first(checkpoint):
        if checkpoint == "after_live_gate_start":
            first_inside_transaction.set()
            assert second_started.wait(timeout=30)

    first_service.fault_hook = pause_first
    changed = replace(request, workflow_id="workflow-concurrent-global-key")
    second_service = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        evidence_root=tmp_path / "missing-concurrent-workflow-evidence",
        official_input_root=tmp_path / "missing-concurrent-workflow-input",
        execution_context_receipt_path=(
            tmp_path / "missing-concurrent-workflow-context.json"
        ),
        clock=lambda: changed.occurred_at + 301,
    )
    evidence_before = tuple(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )
    outcomes = []

    def invoke(service, value, *, mark_started=False):
        try:
            if mark_started:
                second_started.set()
            outcomes.append(service.execute(value))
        except Exception as exc:  # pragma: no cover - asserted below
            outcomes.append(exc)

    first_worker = threading.Thread(target=invoke, args=(first_service, request))
    second_worker = threading.Thread(
        target=invoke,
        args=(second_service, changed),
        kwargs={"mark_started": True},
    )
    first_worker.start()
    assert first_inside_transaction.wait(timeout=60)
    second_worker.start()
    first_worker.join(timeout=120)
    second_worker.join(timeout=120)

    assert not first_worker.is_alive()
    assert not second_worker.is_alive()
    assert len(outcomes) == 2
    results = [
        value for value in outcomes if type(value) is Phase9ForensicReplayResult
    ]
    conflicts = [
        value for value in outcomes if type(value) is Phase9ForensicReplayConflict
    ]
    assert len(results) == 1
    assert results[0].replayed is False
    assert len(conflicts) == 1
    assert "idempotency key" in str(conflicts[0])
    connection = sqlite3.connect(foundation.database)
    try:
        bindings = connection.execute(
            "SELECT workflow_id, request_sha256, replay_id FROM "
            "authority_production_phase9_replay_idempotency "
            "WHERE idempotency_key=?",
            (request.idempotency_key,),
        ).fetchall()
        assert len(bindings) == 1
        assert bindings[0][0] == request.workflow_id
        assert bindings[0][1] == request.request_sha256
        assert bindings[0][2] == request.replay_id
    finally:
        connection.close()
    assert _counts(foundation.database) == {
        "authority_production_phase9_replays": 1,
        "authority_production_phase9_replay_events": 6,
        "authority_production_phase9_terminal_receipts": 1,
        "authority_production_phase9_replay_idempotency": 1,
        "authority_production_phase9_replay_current": 1,
        "authority_production_phase9_evidence_receipts": 30,
        "authority_production_phase9_gate_consumptions": 1,
    }
    assert evidence_before == tuple(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


@pytest.mark.parametrize("peer_kind", ("exact", "cross_workflow"))
def test_independent_processes_serialize_same_key_forensic_requests(
    tmp_path,
    peer_kind,
):
    """Exercise the real OS flock around one complete forensic commit."""

    context = multiprocessing.get_context("fork")
    foundation, root, request, first_service = _fixture(tmp_path)
    changed = (
        request
        if peer_kind == "exact"
        else replace(request, workflow_id="workflow-process-global-key")
    )
    first_inside_transaction = context.Event()
    peer_started = context.Event()

    def hold_first(checkpoint):
        if checkpoint == "after_live_gate_start":
            first_inside_transaction.set()
            assert peer_started.wait(timeout=30)

    first_service.fault_hook = hold_first
    second_service = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        evidence_root=tmp_path / f"missing-process-{peer_kind}-evidence",
        official_input_root=tmp_path / f"missing-process-{peer_kind}-input",
        execution_context_receipt_path=(
            tmp_path / f"missing-process-{peer_kind}-context.json"
        ),
        clock=lambda: changed.occurred_at + 301,
    )
    evidence_before = tuple(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )
    output = context.Queue()

    def invoke(service, value, *, mark_started=False):
        if mark_started:
            peer_started.set()
        try:
            output.put(("result", service.execute(value).as_dict()))
        except Exception as exc:  # pragma: no cover - asserted in parent
            output.put(("error", type(exc).__name__, str(exc)))

    first_process = context.Process(target=invoke, args=(first_service, request))
    second_process = context.Process(
        target=invoke,
        args=(second_service, changed),
        kwargs={"mark_started": True},
    )
    first_process.start()
    assert first_inside_transaction.wait(timeout=60)
    second_process.start()
    first_process.join(timeout=180)
    second_process.join(timeout=180)
    for process in (first_process, second_process):
        if process.is_alive():  # pragma: no cover - defensive cleanup
            process.terminate()
            process.join(timeout=10)
        assert process.exitcode == 0
    outcomes = [output.get(timeout=10), output.get(timeout=10)]
    results = [item[1] for item in outcomes if item[0] == "result"]
    errors = [item for item in outcomes if item[0] == "error"]

    if peer_kind == "exact":
        assert errors == []
        assert len(results) == 2
        assert results[0] == results[1]
        assert results[0]["replayed"] is False
    else:
        assert len(results) == 1
        assert len(errors) == 1
        assert errors[0][1] == "Phase9ForensicReplayConflict"
        assert "idempotency key" in errors[0][2]
    assert _counts(foundation.database) == {
        "authority_production_phase9_replays": 1,
        "authority_production_phase9_replay_events": 6,
        "authority_production_phase9_terminal_receipts": 1,
        "authority_production_phase9_replay_idempotency": 1,
        "authority_production_phase9_replay_current": 1,
        "authority_production_phase9_evidence_receipts": 30,
        "authority_production_phase9_gate_consumptions": 1,
    }
    connection = sqlite3.connect(foundation.database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM "
            "authority_production_phase9_start_authorization_consumptions"
        ).fetchone()[0] == 1
    finally:
        connection.close()
    assert evidence_before == tuple(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def test_concurrent_same_key_different_replays_never_overwrite(tmp_path):
    """A racing nonmatching payload cannot replace the one valid commit."""

    (
        foundation,
        input_root,
        generation_request,
        candidate,
        state,
        p0_root,
        p0_root_sha,
        p0_receipts,
    ) = _ready_fixture(tmp_path)
    gate = _ready_gate(
        input_root,
        generation_request,
        candidate,
        state,
        p0_root,
        p0_root_sha,
        p0_receipts,
    )
    first_root = tmp_path / "concurrent-first-evidence"
    second_root = tmp_path / "concurrent-second-evidence"
    request = _request_for_evidence(
        first_root,
        generation_request,
        state,
        gate,
        idempotency_key="phase9-racing-shared-key",
        occurred_at=2200,
    )
    request = _attest_fixture_evidence(
        foundation=foundation,
        root=first_root,
        request=request,
        input_root=input_root,
        context_path=tmp_path / "execution-context.json",
    )
    changed = _request_for_evidence(
        second_root,
        generation_request,
        state,
        gate,
        idempotency_key=request.idempotency_key,
        occurred_at=request.occurred_at + 1,
    )
    second_values, _second_inventory = replay_module._read_evidence_set(
        second_root, changed
    )
    second_evaluation = replay_module._evaluate_evidence(
        changed,
        second_values,
        trusted_now=changed.occurred_at,
        require_component_receipts=False,
    )
    replay_evidence_module._write_authority_component_receipts(
        root=second_root,
        request=changed,
        values=second_values,
        evaluation=second_evaluation,
    )
    changed = _refresh_start_authorization(second_root, changed)
    second_preflight = preflight_phase9_forensic_replay(
        changed,
        evidence_root=second_root,
        trusted_now=changed.occurred_at,
    )
    assert second_preflight["blockers"] == []
    assert second_preflight["status"] == "READY"
    first_inside_lease = threading.Event()
    second_entered_service = threading.Event()

    def hold_first_writer(checkpoint):
        if checkpoint == "after_live_gate_start":
            first_inside_lease.set()
            assert second_entered_service.wait(timeout=30)

    first_service = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        evidence_root=first_root,
        official_input_root=input_root,
        execution_context_receipt_path=tmp_path / "execution-context.json",
        clock=lambda: request.occurred_at,
        fault_hook=hold_first_writer,
    )
    second_service = Phase9ForensicReplayService(
        foundation.database,
        expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        evidence_root=second_root,
        official_input_root=input_root,
        execution_context_receipt_path=tmp_path / "execution-context.json",
        clock=lambda: changed.occurred_at,
    )
    assert request.idempotency_key == changed.idempotency_key
    assert request.request_sha256 != changed.request_sha256
    assert request.replay_id != changed.replay_id
    first_files_before = tuple(
        (path.relative_to(first_root).as_posix(), path.read_bytes())
        for path in sorted(first_root.rglob("*"))
        if path.is_file()
    )
    second_files_before = tuple(
        (path.relative_to(second_root).as_posix(), path.read_bytes())
        for path in sorted(second_root.rglob("*"))
        if path.is_file()
    )

    outcomes = []

    def invoke(service, request, entered=None):
        try:
            if entered is not None:
                entered.set()
            outcomes.append(service.execute(request))
        except Exception as exc:  # pragma: no cover - asserted below
            outcomes.append(exc)

    workers = [threading.Thread(target=invoke, args=(first_service, request))]
    workers[0].start()
    assert first_inside_lease.wait(timeout=30)
    workers.append(
        threading.Thread(
            target=invoke,
            args=(second_service, changed, second_entered_service),
        )
    )
    workers[1].start()
    for worker in workers:
        worker.join(timeout=120)

    assert len(outcomes) == 2
    assert sum(type(value) is Phase9ForensicReplayResult for value in outcomes) == 1
    refusals = [value for value in outcomes if type(value) is Phase9ForensicReplayConflict]
    assert len(refusals) == 1
    assert "idempotency key" in str(refusals[0])
    winner = next(
        value for value in outcomes if type(value) is Phase9ForensicReplayResult
    )
    connection = sqlite3.connect(foundation.database)
    connection.row_factory = sqlite3.Row
    try:
        binding = connection.execute(
            "SELECT * FROM authority_production_phase9_replay_idempotency "
            "WHERE workflow_id=? AND idempotency_key=?",
            (request.workflow_id, request.idempotency_key),
        ).fetchone()
        assert binding is not None
        assert binding["request_sha256"] == request.request_sha256
        assert binding["replay_id"] == winner.replay_id == request.replay_id
        assert binding["terminal_receipt_sha256"] == winner.receipt_sha256
        assert connection.execute(
            "SELECT COUNT(*) FROM authority_production_phase9_replays "
            "WHERE replay_id=?",
            (changed.replay_id,),
        ).fetchone()[0] == 0
    finally:
        connection.close()
    assert first_files_before == tuple(
        (path.relative_to(first_root).as_posix(), path.read_bytes())
        for path in sorted(first_root.rglob("*"))
        if path.is_file()
    )
    assert second_files_before == tuple(
        (path.relative_to(second_root).as_posix(), path.read_bytes())
        for path in sorted(second_root.rglob("*"))
        if path.is_file()
    )
    assert _counts(foundation.database) == {
        "authority_production_phase9_replays": 1,
        "authority_production_phase9_replay_events": 6,
        "authority_production_phase9_terminal_receipts": 1,
        "authority_production_phase9_replay_idempotency": 1,
        "authority_production_phase9_replay_current": 1,
        "authority_production_phase9_evidence_receipts": 30,
        "authority_production_phase9_gate_consumptions": 1,
    }


def test_shared_current_terminal_graph_validator_is_query_only(tmp_path):
    foundation, _root, request, service = _fixture(tmp_path)
    result = service.execute(request)
    connection = sqlite3.connect(foundation.database)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        before = connection.total_changes
        graph = validate_current_phase9_completed_replay_in_transaction(
            connection,
            workflow_id=request.workflow_id,
            expected_run_generation=request.run_generation,
            expected_terminal_receipt_sha256=result.receipt_sha256,
        )
        assert connection.total_changes == before
        assert graph == {
            "workflow_id": request.workflow_id,
            "run_generation": request.run_generation,
            "replay_id": request.replay_id,
            "terminal_receipt_sha256": result.receipt_sha256,
            "final_event_sha256": connection.execute(
                "SELECT final_event_sha256 FROM "
                "authority_production_phase9_replay_current "
                "WHERE workflow_id=?",
                (request.workflow_id,),
            ).fetchone()[0],
            "request_sha256": request.request_sha256,
            "typed_receipt_set_sha256": connection.execute(
                "SELECT json_extract(receipt_json, '$.typed_receipt_set_sha256') "
                "FROM authority_production_phase9_terminal_receipts "
                "WHERE receipt_sha256=?",
                (result.receipt_sha256,),
            ).fetchone()[0],
        }
    finally:
        connection.rollback()
        connection.close()


@pytest.mark.parametrize(
    "component_field",
    (
        "packet_sha256",
        "roles_sha256",
        "verdict_sha256",
        "snapshot_sha256",
        "runtime_safety_sha256",
        "acceptance_sha256",
    ),
)
def test_completed_graph_cannot_rehash_a_component_away_from_request_evidence(
    tmp_path,
    component_field,
):
    foundation, _root, request, service = _fixture(tmp_path)
    result = service.execute(request)
    connection = sqlite3.connect(foundation.database)
    connection.row_factory = sqlite3.Row
    try:
        terminal_row = connection.execute(
            "SELECT * FROM authority_production_phase9_terminal_receipts "
            "WHERE receipt_sha256=?",
            (result.receipt_sha256,),
        ).fetchone()
        terminal_body = json.loads(terminal_row["receipt_json"])
        terminal_body[component_field] = "0" * 64
        consumption = connection.execute(
            "SELECT * FROM authority_production_phase9_gate_consumptions "
            "WHERE replay_id=?",
            (request.replay_id,),
        ).fetchone()
        typed_set_sha256 = terminal_body["typed_receipt_set_sha256"]
        event_specs = (
            ("ENTRY_READY", "READY", {
                "entry_gate_result_sha256": request.entry_gate_result_sha256,
                "entry_state_receipt_sha256": consumption[
                    "entry_state_receipt_sha256"
                ],
                "authorization_receipt_sha256": consumption[
                    "start_authorization_receipt_sha256"
                ],
                "gate_consumption_receipt_sha256": consumption[
                    "receipt_sha256"
                ],
            }),
            ("PACKET_REBUILT", "PACKET_REBUILT", {
                "packet_sha256": terminal_body["packet_sha256"],
            }),
            ("ROLES_COLLECTED", "ROLES_COLLECTED", {
                "roles_sha256": terminal_body["roles_sha256"],
                "replay_mode": request.replay_mode,
            }),
            ("VERDICT_COMPUTED", "VERDICT_COMPUTED", {
                "verdict_sha256": terminal_body["verdict_sha256"],
                "effective_verdict": terminal_body["effective_verdict"],
            }),
            ("SNAPSHOT_CAPTURED", "SNAPSHOT_CAPTURED", {
                "snapshot_sha256": terminal_body["snapshot_sha256"],
            }),
            ("TERMINAL", "COMPLETED", {
                "runtime_safety_sha256": terminal_body[
                    "runtime_safety_sha256"
                ],
                "acceptance_sha256": terminal_body["acceptance_sha256"],
                "typed_receipt_set_sha256": typed_set_sha256,
                "terminal_reason": terminal_body["terminal_reason"],
                "delivery_capability": "DISABLED",
            }),
        )
        rebuilt_events = []
        predecessor = None
        for sequence, (kind, state, evidence) in enumerate(event_specs, start=1):
            body, event_sha256 = replay_module._event(
                request, sequence, kind, state, predecessor, evidence
            )
            rebuilt_events.append(
                (sequence, kind, state, predecessor, body, event_sha256)
            )
            predecessor = event_sha256
        terminal_body["final_event_sha256"] = predecessor
        forged_terminal_sha256 = canonical_sha256(terminal_body)
        mutable_tables = (
            "authority_production_phase9_replay_events",
            "authority_production_phase9_terminal_receipts",
            "authority_production_phase9_replay_idempotency",
            "authority_production_phase9_replay_current",
        )
        placeholders = ",".join("?" for _ in mutable_tables)
        triggers = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            f"AND tbl_name IN ({placeholders}) AND sql LIKE '%BEFORE UPDATE%'",
            mutable_tables,
        ).fetchall()
        for trigger in triggers:
            connection.execute(f'DROP TRIGGER "{trigger["name"]}"')
        for sequence, kind, state, prior, body, event_sha256 in rebuilt_events:
            connection.execute(
                "UPDATE authority_production_phase9_replay_events "
                "SET event_kind=?, state=?, predecessor_event_sha256=?, "
                "event_json=?, event_sha256=? WHERE replay_id=? AND sequence=?",
                (
                    kind,
                    state,
                    prior,
                    canonical_bytes(body).decode(),
                    event_sha256,
                    request.replay_id,
                    sequence,
                ),
            )
        connection.execute(
            "UPDATE authority_production_phase9_terminal_receipts "
            "SET receipt_id=?, final_event_sha256=?, receipt_json=?, "
            "receipt_sha256=? WHERE replay_id=?",
            (
                f"phase9-terminal:{forged_terminal_sha256[:32]}",
                predecessor,
                canonical_bytes(terminal_body).decode(),
                forged_terminal_sha256,
                request.replay_id,
            ),
        )
        connection.execute(
            "UPDATE authority_production_phase9_replay_idempotency "
            "SET terminal_receipt_sha256=? WHERE replay_id=?",
            (forged_terminal_sha256, request.replay_id),
        )
        connection.execute(
            "UPDATE authority_production_phase9_replay_current "
            "SET terminal_receipt_sha256=?, final_event_sha256=? "
            "WHERE replay_id=?",
            (forged_terminal_sha256, predecessor, request.replay_id),
        )
        for trigger in triggers:
            connection.execute(trigger["sql"])
        connection.commit()
    finally:
        connection.close()

    connection = sqlite3.connect(foundation.database)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        before = connection.total_changes
        with pytest.raises(
            Phase9ForensicReplayConflict,
            match="terminal binding differs",
        ):
            validate_current_phase9_completed_replay_in_transaction(
                connection,
                workflow_id=request.workflow_id,
                expected_run_generation=request.run_generation,
                expected_terminal_receipt_sha256=forged_terminal_sha256,
            )
        assert connection.total_changes == before
    finally:
        connection.rollback()
        connection.close()

    with pytest.raises(
        Phase9ForensicReplayConflict,
        match="terminal binding differs",
    ):
        collect_phase9_forensic_replay_state(
            foundation.database,
            expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
            workflow_id=request.workflow_id,
        )


def test_replay_coordinate_binds_generation_source_inventory(tmp_path):
    foundation, _root, request, _service = _fixture(tmp_path)
    connection = sqlite3.connect(foundation.database)
    connection.row_factory = sqlite3.Row
    try:
        with pytest.raises(
            Phase9ForensicReplayConflict, match="generation coordinate differs"
        ):
            Phase9ForensicReplayService._verify_coordinate(
                connection,
                replace(request, source_inventory_sha256="0" * 64),
            )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "column, value",
    [
        ("run_mode", "NORMAL"),
        ("modeling_consultation_contract", "INHERITED"),
    ],
)
def test_replay_start_rechecks_fixed_generation_modes(tmp_path, column, value):
    foundation, _root, request, _service = _fixture(tmp_path)
    connection = sqlite3.connect(foundation.database)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            "DROP TRIGGER "
            "authority_production_run_generations_append_only_update"
        )
        connection.execute(
            f"UPDATE authority_production_run_generations SET {column}=? "
            "WHERE run_generation=?",
            (value, request.run_generation),
        )
        with pytest.raises(
            Phase9ForensicReplayConflict,
            match="current generation coordinate differs",
        ):
            Phase9ForensicReplayService._verify_coordinate(connection, request)
    finally:
        connection.rollback()
        connection.close()


def test_completed_collector_rejects_an_extra_hash_consistent_event(tmp_path):
    foundation, _root, request, service = _fixture(tmp_path)
    result = service.execute(request)
    connection = sqlite3.connect(foundation.database)
    try:
        event = {
            "schema": "authority-phase9-forensic-replay-event-v1",
            "replay_id": request.replay_id,
            "workflow_id": request.workflow_id,
            "run_generation": request.run_generation,
            "sequence": 7,
            "event_kind": "INVENTED_EXTRA_EVENT",
            "state": "COMPLETED",
            "predecessor_event_sha256": None,
            "evidence": {},
            "occurred_at": request.occurred_at,
        }
        event_sha = canonical_sha256(event)
        connection.execute(
            "INSERT INTO authority_production_phase9_replay_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request.replay_id, 7, "INVENTED_EXTRA_EVENT", "COMPLETED", None,
                canonical_bytes(event).decode(), event_sha, request.occurred_at,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(Phase9ForensicReplayConflict, match="event inventory differs"):
        collect_phase9_forensic_replay_state(
            foundation.database,
            expected_source_fence_sha256=foundation.preflight.source_fence_sha256,
            workflow_id=request.workflow_id,
        )
    assert result.receipt_sha256


def test_cli_disabled_returns_before_missing_request_or_configured_paths(monkeypatch, capsys):
    monkeypatch.delenv("PHASE9_ENABLED", raising=False)
    monkeypatch.setenv("PHASE9_AUTHORITY_DB_FILE", "relative-invalid")
    code = replay_main(["execute", "--request", "/definitely/missing.json", "--confirm"])
    captured = capsys.readouterr()
    assert code == 2
    assert '"code":"PHASE9_DISABLED"' in captured.out
    assert captured.err == ""
