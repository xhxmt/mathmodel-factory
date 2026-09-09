from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import pwd
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time

import pytest

import factory_core.phase9_run_generation as run_generation
from factory_core.authority_operations import AuthorityOperations
from factory_core.contract_pins import compile_contract_pin_set
from factory_core.canonical import canonical_bytes, canonical_sha256
from factory_core.phase9_run_generation import (
    CREATE,
    DELIVERY_DISABLED,
    EXECUTION_CONTEXT_EVIDENCE_SCHEMA,
    GIT_SOURCE_IDENTITY_SCHEMA,
    OFFICIAL_INPUT_FILE_EVIDENCE_SCHEMA,
    OFFICIAL_INPUT_MANIFEST_EVIDENCE_SCHEMA,
    OPERATOR_AUTHORIZATION_EVIDENCE_SCHEMA,
    ROTATE,
    RUN_GENERATION_REQUEST_SCHEMA,
    ExecutionContextEvidenceV1,
    GitSourceIdentityV1,
    OfficialInputFileEvidenceV1,
    OfficialInputManifestEvidenceV1,
    OperatorAuthorizationEvidenceV1,
    Phase9RunGenerationConflict,
    Phase9RunGenerationSafetyError,
    Phase9RunGenerationService,
    RunGenerationCreationResult,
    RunGenerationRequestV1,
    read_current_git_source_identity,
    read_current_git_source_snapshot,
    read_verified_execution_source_snapshot,
    run_generation_request_from_dict,
)
from factory_core.phase9_p0_evidence import phase9_p0_execution_context_bindings
from factory_core.workflow_contract_v2 import compile_workflow_contract_bundle_v2
from tests.support.authority_production import install_foundation


OFFICIAL_BYTES = b"verified official phase9 bytes\n"
RUN_TABLES = (
    "authority_production_run_generations",
    "authority_production_run_generation_current",
    "authority_production_run_generation_creation_receipts",
    "authority_production_run_generation_idempotency",
    "authority_production_run_generation_successions",
    "authority_production_run_generation_source_inventories",
    "authority_production_run_generation_authorization_consumptions",
)


def _source_repository() -> Path:
    """Share the candidate used by the real entry/P0 predecessor helpers."""

    # A test's explicit source override takes precedence over the shared cache.
    raw = os.environ.get("PHASE9_TEST_SOURCE_REPOSITORY")
    if raw is not None:
        path = Path(raw)
        if not path.is_absolute():
            raise AssertionError("PHASE9_TEST_SOURCE_REPOSITORY must be absolute")
        return path

    from tests.test_phase9_entry_gate import _source_repository as entry_source

    return entry_source()


@pytest.fixture(scope="module", autouse=True)
def _shared_formal_source(tmp_path_factory):
    from tests.test_phase9_entry_gate import _formal_test_source_repository

    _formal_test_source_repository(tmp_path_factory.mktemp("phase9-run-generation"))


def test_regular_file_reader_rejects_final_pathname_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verified descriptor must still name the live pathname on return."""

    target = tmp_path / "execution-context.json"
    target.write_bytes(b"same-length-context\n")
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(target.read_bytes())
    real_fstat = os.fstat
    calls = 0

    def replacing_fstat(descriptor: int):
        nonlocal calls
        result = real_fstat(descriptor)
        calls += 1
        if calls == 2:
            os.replace(replacement, target)
        return result

    monkeypatch.setattr(run_generation.os, "fstat", replacing_fstat)
    with pytest.raises(Phase9RunGenerationSafetyError, match="changed while being read"):
        run_generation._regular_file_bytes(
            target, maximum_bytes=1024, label="execution context receipt"
        )


def _request(
    *,
    operation_kind: str = CREATE,
    key: str = "generation-key-1",
    predecessor: str | None = None,
    predecessor_receipt: str | None = None,
    predecessor_terminal_receipt: str | None = None,
    occurred_at: int = 2000,
    formal_p0_context: bool = False,
) -> RunGenerationRequestV1:
    source_snapshot = read_current_git_source_snapshot(_source_repository())
    source = source_snapshot.source
    p0_context = (
        phase9_p0_execution_context_bindings(
            source_repository=_source_repository(),
            python_executable=Path(os.sys.executable),
        )
        if formal_p0_context
        else None
    )
    authorization = OperatorAuthorizationEvidenceV1(
        OPERATOR_AUTHORIZATION_EVIDENCE_SCHEMA,
        f"authorization-{key}",
        "CONTROLLED_OS_ACCOUNT",
        "6" * 64,
        True,
        os.geteuid(),
        pwd.getpwuid(os.geteuid()).pw_name,
        "product-owner",
        "phase9-operator",
        operation_kind,
        "demo",
        "legacy_current",
        source.source_commit,
        "0" * 64,
        1900,
        3000,
        "0" * 64,
    )
    request = RunGenerationRequestV1(
        RUN_GENERATION_REQUEST_SCHEMA,
        key,
        operation_kind,
        "demo",
        "legacy_current",
        1,
        "project-generation-phase9-1",
        "native_v2",
        "stage_v1",
        predecessor,
        predecessor_receipt,
        predecessor_terminal_receipt,
        "FORENSIC_REPLAY",
        "LEGACY_NOT_APPLICABLE",
        DELIVERY_DISABLED,
        source,
        source_snapshot.source_inventory_sha256,
        compile_contract_pin_set(compile_workflow_contract_bundle_v2()),
        OfficialInputManifestEvidenceV1(
            OFFICIAL_INPUT_MANIFEST_EVIDENCE_SCHEMA,
            "official-input-generation-1",
            (
                OfficialInputFileEvidenceV1(
                    OFFICIAL_INPUT_FILE_EVIDENCE_SCHEMA,
                    "official/problem.pdf",
                    len(OFFICIAL_BYTES),
                    hashlib.sha256(OFFICIAL_BYTES).hexdigest(),
                ),
            ),
        ),
        ExecutionContextEvidenceV1(
            EXECUTION_CONTEXT_EVIDENCE_SCHEMA,
            "phase9-context-1",
            str(p0_context["runtime_environment"]["descriptor_sha256"])
            if p0_context is not None else "3" * 64,
            str(p0_context["dependency_lock_sha256"])
            if p0_context is not None else "4" * 64,
            str(p0_context["launcher"]["descriptor_sha256"])
            if p0_context is not None else "5" * 64,
            1950,
        ),
        authorization,
        occurred_at,
    )
    request = replace(
        request,
        project_generation=request.derived_project_generation,
    )
    return _reauthorize(request)


def _reauthorize(
    request: RunGenerationRequestV1, **authorization_changes
) -> RunGenerationRequestV1:
    authorization = replace(
        request.operator_authorization,
        authorized_request_sha256="0" * 64,
        authorization_statement_sha256="0" * 64,
        **authorization_changes,
    )
    prototype = replace(request, operator_authorization=authorization)
    authorization = replace(
        authorization,
        authorized_request_sha256=prototype.authorization_target_sha256,
    )
    authorization = replace(
        authorization,
        authorization_statement_sha256=authorization.expected_statement_sha256,
    )
    return replace(prototype, operator_authorization=authorization)


def _evidence_paths(fixture, request, *, write=True):
    root = fixture.project_dir.parent / f"{fixture.project_dir.name}-official-inputs"
    official = root / "official" / "problem.pdf"
    context = fixture.project_dir.parent / f"{fixture.project_dir.name}-context.json"
    if write:
        official.parent.mkdir(parents=True, exist_ok=True)
        official.write_bytes(OFFICIAL_BYTES)
        context.write_bytes(canonical_bytes(request.execution_context.as_dict()))
    return root, context


def _service(
    fixture,
    *,
    request=None,
    fault_hook=None,
    prepare_evidence=True,
    clock=None,
) -> Phase9RunGenerationService:
    value = _request() if request is None else request
    official_root, context = _evidence_paths(
        fixture, value, write=prepare_evidence
    )
    return Phase9RunGenerationService(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        official_input_root=official_root,
        execution_context_receipt_path=context,
        fault_hook=fault_hook,
        clock=(lambda: 2000) if clock is None else clock,
    )


def _counts(database: Path) -> dict[str, int]:
    connection = sqlite3.connect(database)
    try:
        return {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in RUN_TABLES
        }
    finally:
        connection.close()


def _database_family_snapshot(database: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in sorted(database.parent.glob(f"{database.name}*"))
        if path.is_file()
    }


def _insert_empty_workflow_clone(database: Path, workflow_id: str) -> None:
    """Create a second independently writable workflow in the same Authority DB."""

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            INSERT INTO authority_workflows(
                workflow_id, project_id, project_generation, run_generation,
                runtime_generation, scheduler_generation, current_revision,
                current_revision_availability, contract_pin_set_sha256,
                contract_pin_availability, authority_state
            )
            SELECT ?, project_id, 'legacy_unknown', 'legacy_unknown',
                   runtime_generation, scheduler_generation, current_revision,
                   current_revision_availability, contract_pin_set_sha256,
                   contract_pin_availability, authority_state
            FROM authority_workflows WHERE workflow_id='legacy_current'
            """,
            (workflow_id,),
        )
        connection.commit()
    finally:
        connection.close()


def _request_for_workflow(
    request: RunGenerationRequestV1, workflow_id: str
) -> RunGenerationRequestV1:
    authorization = replace(
        request.operator_authorization,
        authorization_id=(
            f"{request.operator_authorization.authorization_id}-{workflow_id}"
        ),
        workflow_id=workflow_id,
    )
    changed = replace(
        request,
        workflow_id=workflow_id,
        project_generation="pending-derived-project-generation",
        operator_authorization=authorization,
    )
    changed = replace(
        changed,
        project_generation=changed.derived_project_generation,
    )
    return _reauthorize(changed)


def _authority_operator_run_generation(
    fixture,
    request: RunGenerationRequestV1,
    request_path: Path,
) -> subprocess.CompletedProcess[str]:
    request_path.write_bytes(canonical_bytes(request.as_dict()))
    official_root, context = _evidence_paths(fixture, request, write=False)
    return subprocess.run(
        [
            sys.executable,
            "-B",
            str(_source_repository() / "scripts" / "authority_operator.py"),
            "run-generation",
            "--database",
            str(fixture.database),
            "--expected-source-fence",
            fixture.preflight.source_fence_sha256,
            "--source-repository",
            str(_source_repository()),
            "--official-input-root",
            str(official_root),
            "--execution-context-receipt",
            str(context),
            "--request",
            str(request_path),
            "--confirm",
        ],
        cwd=_source_repository(),
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=False,
    )


def _record_semantically_invalid_predecessor(
    database: Path,
    request: RunGenerationRequestV1,
    run_generation: str,
    creation_receipt_sha256: str,
    *,
    include_evidence: bool = True,
) -> str:
    """Install a hash-correct but semantically invalid completed replay graph."""

    replay_id = f"replay-for-{run_generation[-24:]}"
    gate_sha256 = hashlib.sha256(f"gate:{replay_id}".encode()).hexdigest()
    replay_request_sha256 = hashlib.sha256(
        f"replay-request:{replay_id}".encode()
    ).hexdigest()
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        attestation_guard = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            ("authority_production_phase9_terminal_a2_0019_attestation_guard",),
        ).fetchone()
        assert attestation_guard is not None and attestation_guard[0]
        connection.execute(
            "DROP TRIGGER authority_production_phase9_terminal_a2_0019_attestation_guard"
        )
        connection.execute(
            """
            INSERT INTO authority_production_phase9_replays(
                replay_id, workflow_id, project_id, project_revision,
                project_generation, run_generation,
                run_generation_creation_receipt_sha256, operation_kind,
                predecessor_replay_id, predecessor_terminal_receipt_sha256,
                replay_mode, requested_resume_target, delivery_capability,
                source_commit, source_tree, source_parent,
                entry_gate_result_sha256, evidence_set_sha256,
                request_json, request_sha256, started_at
            ) VALUES (
                :replay_id, :workflow_id, :project_id, :project_revision,
                :project_generation, :run_generation, :creation_receipt,
                'CREATE', NULL, NULL, 'TECHNICAL', 'STEP13_PACKET_REBUILD',
                'DISABLED', :source_commit, :source_tree, :source_parent,
                :gate_sha256, :evidence_set_sha256, :request_json,
                :request_sha256, :occurred_at
            )
            """,
            {
                "replay_id": replay_id,
                "workflow_id": request.workflow_id,
                "project_id": request.project_id,
                "project_revision": request.project_revision,
                "project_generation": request.project_generation,
                "run_generation": run_generation,
                "creation_receipt": creation_receipt_sha256,
                "source_commit": request.source.source_commit,
                "source_tree": request.source.source_tree,
                "source_parent": request.source.source_parent,
                "gate_sha256": gate_sha256,
                "evidence_set_sha256": hashlib.sha256(
                    f"evidence:{replay_id}".encode()
                ).hexdigest(),
                "request_json": canonical_bytes(
                    {"schema": "test-completed-replay", "replay_id": replay_id}
                ).decode(),
                "request_sha256": replay_request_sha256,
                "occurred_at": request.occurred_at,
            },
        )
        gate_body = {
            "schema": "authority-phase9-gate-consumption-v1",
            "gate_result_sha256": gate_sha256,
            "entry_state_receipt_sha256": hashlib.sha256(
                f"entry:{replay_id}".encode()
            ).hexdigest(),
            "start_authorization_id": f"start-auth-{replay_id}",
            "start_authorization_receipt_sha256": hashlib.sha256(
                f"start-auth:{replay_id}".encode()
            ).hexdigest(),
            "workflow_id": request.workflow_id,
            "run_generation": run_generation,
            "replay_id": replay_id,
            "request_sha256": replay_request_sha256,
            "consumed_at": request.occurred_at,
        }
        gate_receipt_sha256 = canonical_sha256(gate_body)
        connection.execute(
            """
            INSERT INTO authority_production_phase9_gate_consumptions(
                gate_result_sha256, entry_state_receipt_sha256,
                start_authorization_id, start_authorization_receipt_sha256,
                workflow_id, run_generation, replay_id, request_sha256,
                consumed_at, receipt_json, receipt_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                gate_sha256,
                gate_body["entry_state_receipt_sha256"],
                gate_body["start_authorization_id"],
                gate_body["start_authorization_receipt_sha256"],
                request.workflow_id,
                run_generation,
                replay_id,
                replay_request_sha256,
                request.occurred_at,
                canonical_bytes(gate_body).decode(),
                gate_receipt_sha256,
            ),
        )
        if include_evidence:
            evidence_ids = (
                *(("PROCESS_SCOPE", f"process-scope-{index}") for index in range(3)),
                *(("ACCEPTANCE_CASE", f"acceptance-case-{index:02d}") for index in range(17)),
                *(("ROLE_PROCESS", f"role-process-{index}") for index in range(3)),
                *(("ROLE_PROVIDER", f"role-provider-{index}") for index in range(3)),
                (("PACKET", "packet")),
                (("OUTBOX", "outbox")),
                (("SNAPSHOT", "snapshot")),
                (("VERDICT", "verdict")),
            )
            for receipt_kind, logical_id in evidence_ids:
                raw_bytes = f"{replay_id}:{receipt_kind}:{logical_id}".encode()
                logical_path = f"evidence/{receipt_kind.lower()}/{logical_id}.json"
                evidence_body = {
                    "schema": "test-phase9-typed-evidence-receipt",
                    "replay_id": replay_id,
                    "workflow_id": request.workflow_id,
                    "run_generation": run_generation,
                    "receipt_kind": receipt_kind,
                    "logical_id": logical_id,
                    "logical_path": logical_path,
                    "byte_length": len(raw_bytes),
                    "raw_bytes_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                    "occurred_at": request.occurred_at,
                }
                evidence_receipt_sha256 = canonical_sha256(evidence_body)
                connection.execute(
                    """
                    INSERT INTO authority_production_phase9_evidence_receipts(
                        replay_id, workflow_id, run_generation, receipt_kind,
                        logical_id, logical_path, byte_length, raw_bytes_sha256,
                        receipt_json, receipt_sha256, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        replay_id,
                        request.workflow_id,
                        run_generation,
                        receipt_kind,
                        logical_id,
                        logical_path,
                        len(raw_bytes),
                        evidence_body["raw_bytes_sha256"],
                        canonical_bytes(evidence_body).decode(),
                        evidence_receipt_sha256,
                        request.occurred_at,
                    ),
                )
        event_body = {
            "schema": "test-completed-event",
            "replay_id": replay_id,
            "sequence": 1,
            "state": "COMPLETED",
        }
        event_sha256 = canonical_sha256(event_body)
        connection.execute(
            """
            INSERT INTO authority_production_phase9_replay_events(
                replay_id, sequence, event_kind, state,
                predecessor_event_sha256, event_json, event_sha256, occurred_at
            ) VALUES (?, 1, 'COMPLETED', 'COMPLETED', NULL, ?, ?, ?)
            """,
            (
                replay_id,
                canonical_bytes(event_body).decode(),
                event_sha256,
                request.occurred_at,
            ),
        )
        terminal_body = {
            "schema": "test-completed-terminal",
            "replay_id": replay_id,
            "run_generation": run_generation,
            "final_event_sha256": event_sha256,
        }
        terminal_sha256 = canonical_sha256(terminal_body)
        connection.execute(
            """
            INSERT INTO authority_production_phase9_terminal_receipts(
                receipt_id, replay_id, workflow_id, run_generation,
                terminal_reason, exit_code, effective_verdict,
                final_event_sha256, receipt_json, receipt_sha256, occurred_at
            ) VALUES (?, ?, ?, ?, 'FORENSIC_REPLAY_COMPLETED', 0, 'PASS',
                      ?, ?, ?, ?)
            """,
            (
                f"terminal-{replay_id}",
                replay_id,
                request.workflow_id,
                run_generation,
                event_sha256,
                canonical_bytes(terminal_body).decode(),
                terminal_sha256,
                request.occurred_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO authority_production_phase9_replay_current(
                workflow_id, replay_id, run_generation,
                terminal_receipt_sha256, final_event_sha256, state, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'COMPLETED', ?)
            """,
            (
                request.workflow_id,
                replay_id,
                run_generation,
                terminal_sha256,
                event_sha256,
                request.occurred_at,
            ),
        )
        # This fixture deliberately seeds a hash-correct, semantic-wrong graph
        # that a read-only collector must reject.  Restore the production guard
        # before committing the otherwise impossible corruption fixture.
        connection.execute(attestation_guard[0])
        connection.commit()
        return terminal_sha256
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _record_completed_predecessor(
    fixture,
    request: RunGenerationRequestV1,
    run_generation: str,
    creation_receipt_sha256: str,
    *,
    predecessor_replay_id: str | None = None,
    predecessor_terminal_receipt_sha256: str | None = None,
) -> str:
    """Create the predecessor through the real entry and forensic services."""

    from factory_core.phase9_entry import (
        CandidateIdentity,
        collect_phase9_entry_state,
        p0_evidence_root_sha256,
    )
    from factory_core.phase9_forensic_replay import Phase9ForensicReplayService
    from tests.test_phase9_entry_gate import _p0_receipts
    from tests.test_phase9_forensic_replay import (
        _attest_fixture_evidence,
        _ready_gate,
        _request_for_evidence,
    )

    candidate = CandidateIdentity(
        request.source.source_commit,
        request.source.source_tree,
        request.source.source_parent,
    )
    state = collect_phase9_entry_state(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        workflow_id=request.workflow_id,
        candidate=candidate,
    )
    assert state.run_generation == run_generation
    assert state.creation_receipt_sha256 == creation_receipt_sha256
    evidence_parent = fixture.project_dir.parent
    p0_root = evidence_parent / f"p0-{run_generation[-16:]}"
    p0_receipts = _p0_receipts(
        candidate,
        p0_root,
        project_id=request.project_id,
        workflow_id=request.workflow_id,
        run_generation=run_generation,
        source_inventory_sha256=state.source_inventory_sha256,
        authority_database=fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    state = collect_phase9_entry_state(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        workflow_id=request.workflow_id,
        candidate=candidate,
    )
    p0_root_sha256 = p0_evidence_root_sha256(p0_root)
    official_root, context_path = _evidence_paths(
        fixture, request, write=False
    )
    gate = _ready_gate(
        official_root,
        request,
        candidate,
        state,
        p0_root,
        p0_root_sha256,
        p0_receipts,
    )
    replay_root = evidence_parent / f"replay-{run_generation[-16:]}"
    replay_request = _request_for_evidence(
        replay_root,
        request,
        state,
        gate,
        idempotency_key=f"phase9-replay-{run_generation}",
        operation_kind=(
            ROTATE if predecessor_replay_id is not None else CREATE
        ),
        predecessor_replay_id=predecessor_replay_id,
        predecessor_terminal_receipt_sha256=(
            predecessor_terminal_receipt_sha256
        ),
        occurred_at=request.occurred_at + 100,
    )
    replay_request = _attest_fixture_evidence(
        foundation=fixture,
        root=replay_root,
        request=replay_request,
        input_root=official_root,
        context_path=context_path,
    )
    result = Phase9ForensicReplayService(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        evidence_root=replay_root,
        official_input_root=official_root,
        execution_context_receipt_path=context_path,
        clock=lambda: replay_request.occurred_at,
    ).execute(replay_request)
    return result.receipt_sha256


def test_database_rejects_terminal_without_exact_typed_evidence_inventory(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    created = _service(fixture, request=request).create_or_rotate(request)

    with pytest.raises(
        sqlite3.DatabaseError,
        match="exact typed evidence",
    ):
        _record_semantically_invalid_predecessor(
            fixture.database,
            request,
            created.run_generation,
            created.receipt_sha256,
            include_evidence=False,
        )

    connection = sqlite3.connect(fixture.database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM authority_production_phase9_terminal_receipts"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM authority_production_phase9_gate_consumptions"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM authority_production_phase9_evidence_receipts"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_atomic_create_binds_candidate_inputs_coordinates_and_exact_replay(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    service = _service(fixture, request=request)

    first = service.create_or_rotate(request)
    replay = service.create_or_rotate(request)

    assert first.run_generation == request.derived_run_generation
    assert first.run_generation == (
        f"run-generation:{canonical_sha256(request.generation_intent)}"
    )
    assert first.replayed is False
    assert replay == first
    assert replay.as_dict() == first.as_dict()
    connection = sqlite3.connect(fixture.database)
    connection.row_factory = sqlite3.Row
    try:
        generation = connection.execute(
            "SELECT * FROM authority_production_run_generations"
        ).fetchone()
        current = connection.execute(
            "SELECT * FROM authority_production_run_generation_current"
        ).fetchone()
        workflow = connection.execute(
            "SELECT * FROM authority_workflows WHERE workflow_id='legacy_current'"
        ).fetchone()
        receipt = connection.execute(
            "SELECT * FROM authority_production_run_generation_creation_receipts"
        ).fetchone()
    finally:
        connection.close()
    assert generation["run_generation"] == first.run_generation
    assert generation["source_commit"] == request.source.source_commit
    assert generation["source_tree"] == request.source.source_tree
    assert generation["source_parent"] == request.source.source_parent
    assert generation["delivery_capability"] == "DISABLED"
    assert generation["official_input_manifest_sha256"] == (
        request.official_inputs.manifest_sha256
    )
    assert generation["official_input_raw_bytes_set_sha256"] == (
        request.official_inputs.raw_bytes_set_sha256
    )
    assert current["run_generation"] == first.run_generation
    assert current["creation_receipt_sha256"] == first.receipt_sha256
    assert workflow["project_generation"] == request.project_generation
    assert workflow["run_generation"] == first.run_generation
    assert workflow["runtime_generation"] == request.runtime_generation
    assert workflow["scheduler_generation"] == request.scheduler_generation
    assert workflow["authority_state"] == "RECORDED_SHADOW"
    assert receipt["receipt_sha256"] == first.receipt_sha256
    assert _counts(fixture.database) == {table: 1 for table in RUN_TABLES}


def test_service_verifies_loaded_execution_source_at_start_and_precommit(
    tmp_path,
    monkeypatch,
):
    fixture = install_foundation(tmp_path)
    request = _request()
    original = run_generation.read_verified_execution_source_snapshot
    calls = 0

    def verify(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        run_generation,
        "read_verified_execution_source_snapshot",
        verify,
    )
    _service(fixture, request=request).create_or_rotate(request)
    # The lease-owned query-only preflight is followed by transaction-start
    # and precommit source revalidation.
    assert calls == 3


@pytest.mark.parametrize(
    "failed_call",
    (2, 3),
    ids=("transaction_start", "precommit"),
)
def test_loaded_execution_source_drift_at_write_boundaries_rolls_back_every_row(
    tmp_path,
    monkeypatch,
    failed_call,
):
    fixture = install_foundation(tmp_path)
    request = _request()
    original = run_generation.read_verified_execution_source_snapshot
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
        run_generation,
        "read_verified_execution_source_snapshot",
        verify,
    )
    with pytest.raises(
        Phase9RunGenerationSafetyError,
        match="execution source differs",
    ):
        _service(fixture, request=request).create_or_rotate(request)
    assert calls == failed_call
    # These faults are deliberately injected after the SQLite write connection
    # has opened.  The atomicity contract here is that every logical row rolls
    # back; byte-for-byte WAL/SHM preservation is separately asserted for all
    # exact-recovery and deterministic pre-write rejection paths.
    assert _counts(fixture.database) == {
        table: 0 for table in RUN_TABLES
    }


def test_typed_request_json_round_trip_is_canonical_identity_stable():
    request = _request()
    decoded = run_generation_request_from_dict(
        request.as_dict(), trusted_now=2000
    )
    assert decoded == request
    assert decoded.request_sha256 == request.request_sha256
    assert decoded.derived_run_generation == request.derived_run_generation


def test_same_key_different_canonical_request_conflicts_without_mutation(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    service = _service(fixture, request=request)
    service.create_or_rotate(request)
    before = _counts(fixture.database)
    before_files = _database_family_snapshot(fixture.database)

    with pytest.raises(Phase9RunGenerationConflict, match="different request bytes"):
        service.create_or_rotate(_reauthorize(replace(request, occurred_at=2001)))

    assert _counts(fixture.database) == before
    assert _database_family_snapshot(fixture.database) == before_files


def test_same_key_cannot_be_reused_by_another_workflow_before_live_gates(
    tmp_path,
):
    fixture = install_foundation(tmp_path)
    other_workflow = "workflow-global-key-conflict"
    _insert_empty_workflow_clone(fixture.database, other_workflow)
    first = _request(key="global-run-generation-key")
    changed = _request_for_workflow(first, other_workflow)
    first_service = _service(fixture, request=first)
    first_service.create_or_rotate(first)
    before = _database_family_snapshot(fixture.database)

    # A global key conflict must precede both the stale-clock gate and reads of
    # the second workflow's deliberately absent external inputs.
    changed_service = _service(
        fixture,
        request=changed,
        prepare_evidence=False,
        clock=lambda: changed.occurred_at + 301,
    )
    with pytest.raises(
        Phase9RunGenerationConflict,
        match="idempotency key.*workflow|workflow.*idempotency key",
    ):
        changed_service.create_or_rotate(changed)

    assert _database_family_snapshot(fixture.database) == before
    connection = sqlite3.connect(fixture.database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM authority_production_run_generation_idempotency "
            "WHERE idempotency_key=?",
            (first.idempotency_key,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM authority_production_run_generations "
            "WHERE workflow_id=?",
            (other_workflow,),
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_same_key_generation_binding_mismatch_conflicts_without_mutation(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    service = _service(fixture, request=request)
    service.create_or_rotate(request)
    before_bytes = fixture.database.read_bytes()
    before = _counts(fixture.database)
    changed = _reauthorize(
        replace(request, project_generation="project-generation:different")
    )

    with pytest.raises(Phase9RunGenerationConflict, match="different request bytes"):
        service.create_or_rotate(changed)

    assert fixture.database.read_bytes() == before_bytes
    assert _counts(fixture.database) == before


@pytest.mark.parametrize(
    "checkpoint",
    (
        "after_contract_pin",
        "after_generation",
        "after_receipt",
        "after_current_pointer",
        "before_commit",
    ),
)
def test_faults_rollback_pin_generation_receipt_pointer_and_workflow(
    tmp_path, checkpoint
):
    fixture = install_foundation(tmp_path)

    def fail(actual: str) -> None:
        if actual == checkpoint:
            raise RuntimeError(f"fault:{checkpoint}")

    with pytest.raises(RuntimeError, match=f"fault:{checkpoint}"):
        _service(fixture, fault_hook=fail).create_or_rotate(_request())

    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}
    connection = sqlite3.connect(fixture.database)
    try:
        pins = connection.execute(
            "SELECT COUNT(*) FROM authority_contract_pin_sets"
        ).fetchone()[0]
        workflow = connection.execute(
            "SELECT project_generation, run_generation FROM authority_workflows "
            "WHERE workflow_id='legacy_current'"
        ).fetchone()
    finally:
        connection.close()
    assert pins == 0
    assert workflow == ("legacy_unknown", "legacy_unknown")


def test_rotate_requires_and_persists_exact_concrete_predecessor_receipt(tmp_path):
    fixture = install_foundation(tmp_path)
    create_request = _request(formal_p0_context=True)
    service = _service(fixture, request=create_request)
    first = service.create_or_rotate(create_request)
    terminal = _record_completed_predecessor(
        fixture, create_request, first.run_generation, first.receipt_sha256
    )
    rotate = _request(
        operation_kind=ROTATE,
        key="generation-key-2",
        predecessor=first.run_generation,
        predecessor_receipt=first.receipt_sha256,
        predecessor_terminal_receipt=terminal,
        occurred_at=2001,
        formal_p0_context=True,
    )

    second = service.create_or_rotate(rotate)

    assert second.operation_kind == ROTATE
    connection = sqlite3.connect(fixture.database)
    connection.row_factory = sqlite3.Row
    try:
        current = connection.execute(
            "SELECT * FROM authority_production_run_generation_current"
        ).fetchone()
        succession = connection.execute(
            "SELECT * FROM authority_production_run_generation_successions "
            "WHERE run_generation=?",
            (second.run_generation,),
        ).fetchone()
    finally:
        connection.close()
    assert current["run_generation"] == second.run_generation
    assert succession["predecessor_run_generation"] == first.run_generation
    assert succession["predecessor_creation_receipt_sha256"] == first.receipt_sha256
    assert succession["predecessor_terminal_receipt_sha256"] == terminal
    assert _counts(fixture.database) == {
        RUN_TABLES[0]: 2,
        RUN_TABLES[1]: 1,
        RUN_TABLES[2]: 2,
        RUN_TABLES[3]: 2,
        RUN_TABLES[4]: 2,
        RUN_TABLES[5]: 1,
        RUN_TABLES[6]: 2,
    }


def test_rotate_rejects_a_completed_terminal_from_the_old_generation(tmp_path):
    fixture = install_foundation(tmp_path)
    first_request = _request(formal_p0_context=True)
    service = _service(fixture, request=first_request)
    first = service.create_or_rotate(first_request)
    first_terminal = _record_completed_predecessor(
        fixture, first_request, first.run_generation, first.receipt_sha256
    )
    connection = sqlite3.connect(fixture.database)
    try:
        first_replay_id = connection.execute(
            "SELECT replay_id FROM authority_production_phase9_replay_current "
            "WHERE workflow_id=?",
            (first_request.workflow_id,),
        ).fetchone()[0]
    finally:
        connection.close()

    second_request = _request(
        operation_kind=ROTATE,
        key="generation-key-old-terminal-second",
        predecessor=first.run_generation,
        predecessor_receipt=first.receipt_sha256,
        predecessor_terminal_receipt=first_terminal,
        occurred_at=2001,
        formal_p0_context=True,
    )
    second = service.create_or_rotate(second_request)
    _record_completed_predecessor(
        fixture,
        second_request,
        second.run_generation,
        second.receipt_sha256,
        predecessor_replay_id=first_replay_id,
        predecessor_terminal_receipt_sha256=first_terminal,
    )
    stale_terminal_rotation = _request(
        operation_kind=ROTATE,
        key="generation-key-old-terminal-third",
        predecessor=second.run_generation,
        predecessor_receipt=second.receipt_sha256,
        predecessor_terminal_receipt=first_terminal,
        occurred_at=2002,
        formal_p0_context=True,
    )
    before_counts = _counts(fixture.database)
    connection = sqlite3.connect(fixture.database)
    try:
        before_run_pointer = connection.execute(
            "SELECT * FROM authority_production_run_generation_current"
        ).fetchone()
        before_workflow_pointer = connection.execute(
            "SELECT project_generation, run_generation FROM authority_workflows "
            "WHERE workflow_id=?",
            (first_request.workflow_id,),
        ).fetchone()
    finally:
        connection.close()

    with pytest.raises(
        Phase9RunGenerationConflict,
        match="strictly valid completed graph",
    ):
        service.create_or_rotate(stale_terminal_rotation)

    assert _counts(fixture.database) == before_counts
    connection = sqlite3.connect(fixture.database)
    try:
        assert connection.execute(
            "SELECT * FROM authority_production_run_generation_current"
        ).fetchone() == before_run_pointer
        assert connection.execute(
            "SELECT project_generation, run_generation FROM authority_workflows "
            "WHERE workflow_id=?",
            (first_request.workflow_id,),
        ).fetchone() == before_workflow_pointer
    finally:
        connection.close()


def test_rotate_rejects_wrong_receipt_and_keeps_current_generation(tmp_path):
    fixture = install_foundation(tmp_path)
    create_request = _request(formal_p0_context=True)
    service = _service(fixture, request=create_request)
    first = service.create_or_rotate(create_request)
    terminal = _record_completed_predecessor(
        fixture, create_request, first.run_generation, first.receipt_sha256
    )
    rotate = _request(
        operation_kind=ROTATE,
        key="generation-key-2",
        predecessor=first.run_generation,
        predecessor_receipt="f" * 64,
        predecessor_terminal_receipt=terminal,
        occurred_at=2001,
        formal_p0_context=True,
    )

    with pytest.raises(Phase9RunGenerationConflict, match="predecessor/current"):
        service.create_or_rotate(rotate)

    assert _counts(fixture.database) == {
        table: 1 for table in RUN_TABLES
    }


def test_rotate_rejects_a_hash_correct_semantically_fake_terminal_graph(tmp_path):
    fixture = install_foundation(tmp_path)
    create_request = _request(formal_p0_context=True)
    service = _service(fixture, request=create_request)
    first = service.create_or_rotate(create_request)
    fake_terminal = _record_semantically_invalid_predecessor(
        fixture.database,
        create_request,
        first.run_generation,
        first.receipt_sha256,
    )
    rotate = _request(
        operation_kind=ROTATE,
        key="generation-key-semantic-fake",
        predecessor=first.run_generation,
        predecessor_receipt=first.receipt_sha256,
        predecessor_terminal_receipt=fake_terminal,
        occurred_at=2001,
        formal_p0_context=True,
    )
    before_counts = _counts(fixture.database)
    connection = sqlite3.connect(fixture.database)
    try:
        before_run_pointer = connection.execute(
            "SELECT * FROM authority_production_run_generation_current"
        ).fetchone()
        before_workflow_pointer = connection.execute(
            "SELECT project_generation, run_generation FROM authority_workflows "
            "WHERE workflow_id=?",
            (create_request.workflow_id,),
        ).fetchone()
    finally:
        connection.close()

    with pytest.raises(
        Phase9RunGenerationConflict,
        match="strictly valid completed graph",
    ):
        service.create_or_rotate(rotate)

    assert _counts(fixture.database) == before_counts
    connection = sqlite3.connect(fixture.database)
    try:
        assert connection.execute(
            "SELECT * FROM authority_production_run_generation_current"
        ).fetchone() == before_run_pointer
        assert connection.execute(
            "SELECT project_generation, run_generation FROM authority_workflows "
            "WHERE workflow_id=?",
            (create_request.workflow_id,),
        ).fetchone() == before_workflow_pointer
    finally:
        connection.close()


def test_two_concurrent_rotations_have_exactly_one_cas_winner(tmp_path):
    fixture = install_foundation(tmp_path)
    create_request = _request(formal_p0_context=True)
    first = _service(fixture, request=create_request).create_or_rotate(
        create_request
    )
    terminal = _record_completed_predecessor(
        fixture, create_request, first.run_generation, first.receipt_sha256
    )
    rotations = tuple(
        _request(
            operation_kind=ROTATE,
            key=f"generation-key-concurrent-{suffix}",
            predecessor=first.run_generation,
            predecessor_receipt=first.receipt_sha256,
            predecessor_terminal_receipt=terminal,
            occurred_at=2001,
            formal_p0_context=True,
        )
        for suffix in ("a", "b")
    )
    barrier = threading.Barrier(3)
    results = []
    errors = []

    def rotate(request):
        service = _service(
            fixture,
            request=request,
            prepare_evidence=False,
            clock=lambda: 2001,
        )
        barrier.wait()
        try:
            results.append(service.create_or_rotate(request))
        except Exception as exc:  # the assertion below checks the exact type
            errors.append(exc)

    threads = [threading.Thread(target=rotate, args=(request,)) for request in rotations]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], Phase9RunGenerationConflict)
    assert "predecessor/current coordinate differs" in str(errors[0])
    winner = results[0]
    assert _counts(fixture.database) == {
        RUN_TABLES[0]: 2,
        RUN_TABLES[1]: 1,
        RUN_TABLES[2]: 2,
        RUN_TABLES[3]: 2,
        RUN_TABLES[4]: 2,
        RUN_TABLES[5]: 1,
        RUN_TABLES[6]: 2,
    }
    connection = sqlite3.connect(fixture.database)
    try:
        assert connection.execute(
            "SELECT run_generation FROM "
            "authority_production_run_generation_current"
        ).fetchone()[0] == winner.run_generation
        assert connection.execute(
            "SELECT run_generation FROM authority_workflows WHERE workflow_id=?",
            (create_request.workflow_id,),
        ).fetchone()[0] == winner.run_generation
    finally:
        connection.close()


def test_database_guards_reject_direct_pointer_or_workflow_generation_updates(
    tmp_path,
):
    fixture = install_foundation(tmp_path)
    result = _service(fixture).create_or_rotate(_request())
    connection = sqlite3.connect(fixture.database)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="succession graph"):
            connection.execute(
                "UPDATE authority_production_run_generation_current "
                "SET updated_at=updated_at+1 WHERE workflow_id='legacy_current'"
            )
        connection.rollback()
        with pytest.raises(sqlite3.DatabaseError, match="companion graph"):
            connection.execute(
                "UPDATE authority_workflows SET run_generation='invented-run' "
                "WHERE workflow_id='legacy_current'"
            )
        connection.rollback()
        current = connection.execute(
            "SELECT run_generation FROM authority_production_run_generation_current"
        ).fetchone()[0]
    finally:
        connection.close()
    assert current == result.run_generation


def test_default_off_fence_rejects_enabled_writer(tmp_path):
    fixture = install_foundation(tmp_path)
    operations = AuthorityOperations(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    operations.configure_writer(
        new_writer_id="writer-a",
        enabled=True,
        expected_writer_epoch=0,
        expected_switch_epoch=0,
        operator_subject="operator-a",
        reason="exercise default-off generation fence",
        occurred_at=1500,
    )

    with pytest.raises(Phase9RunGenerationSafetyError, match="writer and consumer disabled"):
        official_root, context = _evidence_paths(fixture, _request())
        operations.create_or_rotate_run_generation(
            _request(), source_repository=_source_repository(),
            official_input_root=official_root,
            execution_context_receipt_path=context,
            clock=lambda: 2000,
        )

    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_request_rejects_legacy_generation_and_unbound_authorization(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    with pytest.raises(Phase9RunGenerationSafetyError, match="must be concrete"):
        _service(fixture).create_or_rotate(
            replace(request, runtime_generation="legacy_unknown")
        )

    wrong_request = _reauthorize(request, workflow_id="other-workflow")
    with pytest.raises(Phase9RunGenerationSafetyError, match="differs from request"):
        _service(fixture).create_or_rotate(wrong_request)
    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_create_authorization_uses_trusted_clock_not_backfilled_occurrence(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request(occurred_at=1940)
    request = _reauthorize(
        replace(
            request,
            execution_context=replace(request.execution_context, captured_at=1930),
        ),
        issued_at=1900,
        expires_at=1950,
    )
    with pytest.raises(
        Phase9RunGenerationSafetyError,
        match="not valid at trusted current time",
    ):
        _service(fixture, request=request).create_or_rotate(request)
    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_create_rejects_future_authorization_and_request_clock_skew(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    future = _reauthorize(request, issued_at=2001, expires_at=3000)
    with pytest.raises(
        Phase9RunGenerationSafetyError,
        match="not valid",
    ):
        _service(fixture, request=future).create_or_rotate(future)

    skewed = _reauthorize(
        replace(
            request,
            occurred_at=1600,
            execution_context=replace(request.execution_context, captured_at=1500),
        ),
        issued_at=1500,
        expires_at=3000,
    )
    with pytest.raises(
        Phase9RunGenerationSafetyError,
        match="exceeds trusted clock skew",
    ):
        _service(fixture, request=skewed).create_or_rotate(skewed)
    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_authorization_expiring_during_transaction_rolls_back_every_row(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    request = _reauthorize(request, issued_at=1900, expires_at=2001)
    trusted_times = iter((2000, 2002))

    with pytest.raises(
        Phase9RunGenerationSafetyError,
        match="not valid at trusted current time",
    ):
        _service(
            fixture,
            request=request,
            clock=lambda: next(trusted_times),
        ).create_or_rotate(request)

    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_authorization_statement_hash_is_recomputed_and_not_trusted(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    tampered = replace(
        request,
        operator_authorization=replace(
            request.operator_authorization,
            authorization_statement_sha256="f" * 64,
        ),
    )

    with pytest.raises(
        Phase9RunGenerationSafetyError,
        match="statement hash differs from canonical statement",
    ):
        _service(fixture, request=tampered).create_or_rotate(tampered)

    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_create_rejects_invented_project_generation_and_unverified_os_identity(
    tmp_path,
):
    fixture = install_foundation(tmp_path)
    request = _request()
    with pytest.raises(Phase9RunGenerationSafetyError, match="must be derived"):
        _service(fixture).create_or_rotate(
            replace(request, project_generation="invented-project-label")
        )

    wrong_uid = _reauthorize(
        request,
        operator_uid=request.operator_authorization.operator_uid + 1,
    )
    with pytest.raises(Phase9RunGenerationSafetyError, match="executing OS account"):
        _service(fixture).create_or_rotate(
            wrong_uid
        )

    unsupported = _reauthorize(
        request, authorization_mechanism="SIGNED_AUTHORIZATION"
    )
    with pytest.raises(Phase9RunGenerationSafetyError, match="only verified"):
        _service(fixture).create_or_rotate(
            unsupported
        )
    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_request_source_must_equal_live_commit_tree_and_parent(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    wrong = GitSourceIdentityV1(
        GIT_SOURCE_IDENTITY_SCHEMA,
        "f" * 40,
        request.source.source_tree,
        request.source.source_parent,
    )
    wrong_request = replace(request, source=wrong)
    wrong_request = replace(
        wrong_request,
        project_generation=wrong_request.derived_project_generation,
    )
    wrong_request = _reauthorize(
        wrong_request, source_commit=wrong.source_commit
    )
    with pytest.raises(Phase9RunGenerationConflict, match="not current"):
        _service(fixture).create_or_rotate(wrong_request)
    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_create_reads_real_official_bytes_and_rejects_wrong_or_extra_files(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    root, _context = _evidence_paths(fixture, request)
    official = root / "official" / "problem.pdf"
    official.write_bytes(b"X" + OFFICIAL_BYTES[1:])
    with pytest.raises(Phase9RunGenerationSafetyError, match="official input bytes differ"):
        _service(
            fixture, request=request, prepare_evidence=False
        ).create_or_rotate(request)

    official.write_bytes(OFFICIAL_BYTES)
    (root / "unexpected.txt").write_bytes(b"not in manifest")
    with pytest.raises(Phase9RunGenerationSafetyError, match="inventory differs"):
        _service(
            fixture, request=request, prepare_evidence=False
        ).create_or_rotate(request)
    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_create_rejects_symlinked_official_input(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    root, _context = _evidence_paths(fixture, request)
    official = root / "official" / "problem.pdf"
    target = fixture.project_dir.parent / "outside-official.bin"
    target.write_bytes(OFFICIAL_BYTES)
    official.unlink()
    os.symlink(target, official)

    with pytest.raises(Phase9RunGenerationSafetyError, match="symlink"):
        _service(
            fixture, request=request, prepare_evidence=False
        ).create_or_rotate(request)
    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_create_requires_exact_canonical_execution_context_receipt(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    _root, context = _evidence_paths(fixture, request)
    context.write_bytes(canonical_bytes({"schema_version": "invented-context"}))

    with pytest.raises(
        Phase9RunGenerationSafetyError,
        match="execution context receipt canonical bytes/hash differ",
    ):
        _service(
            fixture, request=request, prepare_evidence=False
        ).create_or_rotate(request)
    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_official_input_toctou_before_commit_rolls_back_every_row(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    root, _context = _evidence_paths(fixture, request)
    official = root / "official" / "problem.pdf"

    def mutate(checkpoint: str) -> None:
        if checkpoint == "after_receipt":
            official.write_bytes(b"X" + OFFICIAL_BYTES[1:])

    with pytest.raises(Phase9RunGenerationSafetyError, match="official input bytes differ"):
        _service(
            fixture,
            request=request,
            fault_hook=mutate,
            prepare_evidence=False,
        ).create_or_rotate(request)
    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_execution_context_toctou_before_commit_rolls_back_every_row(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    _root, context = _evidence_paths(fixture, request)

    def mutate(checkpoint: str) -> None:
        if checkpoint == "after_receipt":
            context.write_bytes(b"{}")

    with pytest.raises(
        Phase9RunGenerationSafetyError,
        match="execution context receipt canonical bytes/hash differ",
    ):
        _service(
            fixture,
            request=request,
            fault_hook=mutate,
            prepare_evidence=False,
        ).create_or_rotate(request)
    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_rotate_requires_current_predecessor_terminal_receipt(tmp_path):
    fixture = install_foundation(tmp_path)
    create_request = _request()
    service = _service(fixture, request=create_request)
    first = service.create_or_rotate(create_request)
    rotate = _request(
        operation_kind=ROTATE,
        key="generation-key-no-terminal",
        predecessor=first.run_generation,
        predecessor_receipt=first.receipt_sha256,
        predecessor_terminal_receipt="f" * 64,
        occurred_at=2001,
    )
    connection = sqlite3.connect(fixture.database)
    try:
        before_run_pointer = connection.execute(
            "SELECT * FROM authority_production_run_generation_current"
        ).fetchone()
        before_workflow_pointer = connection.execute(
            "SELECT project_generation, run_generation FROM authority_workflows "
            "WHERE workflow_id=?",
            (create_request.workflow_id,),
        ).fetchone()
    finally:
        connection.close()

    with pytest.raises(
        Phase9RunGenerationConflict, match="current predecessor terminal"
    ):
        service.create_or_rotate(rotate)

    assert _counts(fixture.database) == {table: 1 for table in RUN_TABLES}
    connection = sqlite3.connect(fixture.database)
    try:
        assert connection.execute(
            "SELECT * FROM authority_production_run_generation_current"
        ).fetchone() == before_run_pointer
        assert connection.execute(
            "SELECT project_generation, run_generation FROM authority_workflows "
            "WHERE workflow_id=?",
            (create_request.workflow_id,),
        ).fetchone() == before_workflow_pointer
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("run_mode", "TECHNICAL", "mode must be FORENSIC_REPLAY"),
        (
            "modeling_consultation_contract",
            "REQUIRED",
            "must be LEGACY_NOT_APPLICABLE",
        ),
        ("delivery_capability", "ENABLED", "must remain DISABLED"),
    ),
)
def test_request_pins_exact_phase9_modes(tmp_path, field, value, message):
    fixture = install_foundation(tmp_path)
    request = replace(_request(), **{field: value})

    with pytest.raises(Phase9RunGenerationSafetyError, match=message):
        _service(fixture, request=request).create_or_rotate(request)

    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_database_rejects_direct_sql_with_unpinned_mode(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    _service(fixture, request=request).create_or_rotate(request)
    connection = sqlite3.connect(fixture.database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        original = connection.execute(
            "SELECT * FROM authority_production_run_generations"
        ).fetchone()
        values = dict(original)
        values.update(
            run_generation="run-generation:direct-invalid-mode",
            predecessor_run_generation=None,
            predecessor_creation_receipt_sha256=None,
            predecessor_terminal_receipt_sha256=None,
            operation_kind="CREATE",
            run_mode="TECHNICAL",
            request_sha256="a" * 64,
            authorization_id="direct-invalid-mode-authorization",
            authorization_target_sha256="b" * 64,
        )
        columns = tuple(values)
        with pytest.raises(sqlite3.DatabaseError, match="hardened audit binding"):
            connection.execute(
                f"INSERT INTO authority_production_run_generations("
                f"{','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
        connection.rollback()
    finally:
        connection.close()

    assert _counts(fixture.database) == {table: 1 for table in RUN_TABLES}


def test_authorization_binds_every_request_field(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    altered = replace(request, idempotency_key="authorization-unbound-key")

    with pytest.raises(
        Phase9RunGenerationSafetyError,
        match="authorization coordinate differs from request",
    ):
        _service(fixture, request=altered).create_or_rotate(altered)

    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_authorization_explicitly_binds_one_stable_derived_generation():
    request = _request()
    target = request.authorization_target
    assert target == {
        "schema": "authority-phase9-run-generation-authorization-target-v2",
        "derived_run_generation": request.derived_run_generation,
        "intent": request.generation_intent,
    }
    assert (
        request.operator_authorization.authorized_request_sha256
        == canonical_sha256(target)
    )

    separately_issued = _reauthorize(
        request,
        authorization_id="authorization-generation-key-1-second-issue",
        authorization_evidence_sha256="7" * 64,
    )
    assert separately_issued.request_sha256 != request.request_sha256
    assert separately_issued.authorization_target == target
    assert separately_issued.derived_run_generation == request.derived_run_generation
    assert (
        separately_issued.operator_authorization.authorized_request_sha256
        == canonical_sha256(target)
    )


def test_authorization_is_consumed_once_but_exact_replay_is_read_only(tmp_path):
    fixture = install_foundation(tmp_path)
    create_request = _request(formal_p0_context=True)
    service = _service(fixture, request=create_request)
    first = service.create_or_rotate(create_request)
    assert service.create_or_rotate(create_request) == first
    terminal = _record_completed_predecessor(
        fixture,
        create_request,
        first.run_generation,
        first.receipt_sha256,
    )
    rotate = _request(
        operation_kind=ROTATE,
        key="generation-key-reused-authorization",
        predecessor=first.run_generation,
        predecessor_receipt=first.receipt_sha256,
        predecessor_terminal_receipt=terminal,
        occurred_at=2001,
        formal_p0_context=True,
    )
    rotate = _reauthorize(
        rotate,
        authorization_id=create_request.operator_authorization.authorization_id,
    )

    with pytest.raises(
        Phase9RunGenerationConflict, match="authorization was already consumed"
    ):
        service.create_or_rotate(rotate)

    counts = _counts(fixture.database)
    assert counts["authority_production_run_generations"] == 1
    assert counts[
        "authority_production_run_generation_authorization_consumptions"
    ] == 1


@pytest.mark.parametrize("closed_gate", ("authorization_expired", "request_stale"))
def test_exact_replay_survives_closed_new_write_time_gates(tmp_path, closed_gate):
    """A committed result remains recoverable after new-write time gates close."""

    fixture = install_foundation(tmp_path)
    request = _request()
    if closed_gate == "authorization_expired":
        request = _reauthorize(request, expires_at=request.occurred_at + 50)
    service = _service(fixture, request=request, clock=lambda: request.occurred_at)
    first = service.create_or_rotate(request)
    before_bytes = fixture.database.read_bytes()
    before_counts = _counts(fixture.database)
    connection = sqlite3.connect(fixture.database)
    try:
        stored_receipt_before = connection.execute(
            "SELECT receipt_json, receipt_sha256 FROM "
            "authority_production_run_generation_creation_receipts "
            "WHERE run_generation=?",
            (request.derived_run_generation,),
        ).fetchone()
    finally:
        connection.close()
    before_files = _database_family_snapshot(fixture.database)

    late = _service(
        fixture,
        request=request,
        prepare_evidence=False,
        clock=(
            (lambda: request.operator_authorization.expires_at + 1)
            if closed_gate == "authorization_expired"
            else (lambda: request.occurred_at + 301)
        ),
    )
    replayed = late.create_or_rotate(request)

    assert replayed == first
    assert replayed.as_dict() == first.as_dict()
    assert fixture.database.read_bytes() == before_bytes
    assert _database_family_snapshot(fixture.database) == before_files
    assert _counts(fixture.database) == before_counts
    connection = sqlite3.connect(fixture.database)
    try:
        assert connection.execute(
            "SELECT receipt_json, receipt_sha256 FROM "
            "authority_production_run_generation_creation_receipts "
            "WHERE run_generation=?",
            (request.derived_run_generation,),
        ).fetchone() == stored_receipt_before
        assert connection.execute(
            "SELECT COUNT(*) FROM "
            "authority_production_run_generation_authorization_consumptions"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_exact_replay_does_not_require_the_original_live_os_identity(
    tmp_path, monkeypatch
):
    fixture = install_foundation(tmp_path)
    request = _request()
    service = _service(fixture, request=request)
    first = service.create_or_rotate(request)
    before = fixture.database.read_bytes()

    monkeypatch.setattr(run_generation.os, "geteuid", lambda: 999_999)
    monkeypatch.setattr(
        run_generation.pwd,
        "getpwuid",
        lambda _uid: type("Account", (), {"pw_name": "different-account"})(),
    )

    assert service.create_or_rotate(request) == first
    assert fixture.database.read_bytes() == before


@pytest.mark.parametrize(
    "closed_gate",
    ("authorization_expired", "request_stale"),
)
def test_confirmed_authority_operator_recovers_exact_committed_request_before_live_gates(
    tmp_path,
    closed_gate,
):
    """The supported JSON/CLI ingress must not preempt service recovery."""

    now = int(time.time())
    if closed_gate == "authorization_expired":
        occurred_at = now - 60
        request = _reauthorize(
            _request(occurred_at=occurred_at),
            issued_at=occurred_at - 60,
            expires_at=now - 10,
        )
    else:
        occurred_at = now - 400
        request = _reauthorize(
            _request(occurred_at=occurred_at),
            issued_at=occurred_at - 60,
            expires_at=now + 60,
        )
    fixture = install_foundation(tmp_path)
    first = _service(
        fixture,
        request=request,
        clock=lambda: request.occurred_at,
    ).create_or_rotate(request)
    before_bytes = fixture.database.read_bytes()
    before_files = _database_family_snapshot(fixture.database)
    before_counts = _counts(fixture.database)

    completed = _authority_operator_run_generation(
        fixture,
        request,
        tmp_path / f"{closed_gate}.json",
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload == first.as_dict()
    assert fixture.database.read_bytes() == before_bytes
    assert _database_family_snapshot(fixture.database) == before_files
    assert _counts(fixture.database) == before_counts


def test_confirmed_authority_operator_conflicts_or_rejects_without_mutation(
    tmp_path,
):
    """The binding-only ingress does not weaken conflicts or new-write gates."""

    now = int(time.time())
    occurred_at = now - 60
    request = _reauthorize(
        _request(occurred_at=occurred_at),
        issued_at=occurred_at - 60,
        expires_at=now - 10,
    )
    fixture = install_foundation(tmp_path)
    _service(
        fixture,
        request=request,
        clock=lambda: request.occurred_at,
    ).create_or_rotate(request)
    before_bytes = fixture.database.read_bytes()
    before_counts = _counts(fixture.database)
    different = _reauthorize(
        replace(request, occurred_at=request.occurred_at + 1),
        issued_at=request.occurred_at - 60,
        expires_at=now + 60,
    )

    conflict = _authority_operator_run_generation(
        fixture,
        different,
        tmp_path / "different.json",
    )

    assert conflict.returncode == 2
    assert "idempotency key has different request bytes" in conflict.stderr
    assert fixture.database.read_bytes() == before_bytes
    assert _counts(fixture.database) == before_counts

    invalid = _reauthorize(
        replace(request, delivery_capability="ENABLED")
    )
    invalid_conflict = _authority_operator_run_generation(
        fixture,
        invalid,
        tmp_path / "different-invalid-mode.json",
    )
    assert invalid_conflict.returncode == 2
    assert "idempotency key has different request bytes" in invalid_conflict.stderr
    assert fixture.database.read_bytes() == before_bytes
    assert _counts(fixture.database) == before_counts

    fresh_fixture = install_foundation(tmp_path / "fresh")
    fresh_before = fresh_fixture.database.read_bytes()
    rejected = _authority_operator_run_generation(
        fresh_fixture,
        request,
        tmp_path / "fresh-expired.json",
    )
    assert rejected.returncode == 2
    assert "not valid at trusted current time" in rejected.stderr
    assert fresh_fixture.database.read_bytes() == fresh_before
    assert _counts(fresh_fixture.database) == {
        table: 0 for table in RUN_TABLES
    }


def test_exact_replay_does_not_reauthorize_historical_contract_pins(
    tmp_path,
    monkeypatch,
):
    fixture = install_foundation(tmp_path)
    request = _request()
    service = _service(fixture, request=request)
    first = service.create_or_rotate(request)
    before = fixture.database.read_bytes()

    def current_contract_must_not_be_compiled():
        raise AssertionError("exact recovery consulted the current contract")

    monkeypatch.setattr(
        run_generation,
        "compile_workflow_contract_bundle_v2",
        current_contract_must_not_be_compiled,
    )

    assert service.create_or_rotate(request) == first
    assert fixture.database.read_bytes() == before


def test_exact_replay_accepts_base_compatible_authorization_timing(tmp_path):
    """Recovery must not add a request-time authorization rule retroactively."""

    fixture = install_foundation(tmp_path)
    prototype = _request(occurred_at=1700)
    request = replace(
        prototype,
        execution_context=replace(
            prototype.execution_context,
            captured_at=1600,
        ),
        operator_authorization=replace(
            prototype.operator_authorization,
            issued_at=1999,
            expires_at=3000,
        ),
    )
    request = _reauthorize(request)
    service = _service(fixture, request=request, clock=lambda: 2000)

    first = service.create_or_rotate(request)
    before = _database_family_snapshot(fixture.database)

    assert service.create_or_rotate(request) == first
    assert _database_family_snapshot(fixture.database) == before


@pytest.mark.parametrize(
    ("closed_gate", "message"),
    (
        ("authorization_expired", "not valid at trusted current time"),
        ("request_stale", "request occurrence metadata exceeds trusted clock skew"),
    ),
)
def test_new_generation_still_rejects_closed_time_gates_without_mutation(
    tmp_path,
    closed_gate,
    message,
):
    fixture = install_foundation(tmp_path)
    request = _request()
    if closed_gate == "authorization_expired":
        request = _reauthorize(request, expires_at=request.occurred_at + 50)
        now = request.occurred_at + 51
    else:
        now = request.occurred_at + 301
    service = _service(fixture, request=request, clock=lambda: now)
    before_bytes = fixture.database.read_bytes()
    before_files = _database_family_snapshot(fixture.database)

    with pytest.raises(Phase9RunGenerationSafetyError, match=message):
        service.create_or_rotate(request)

    assert fixture.database.read_bytes() == before_bytes
    assert _database_family_snapshot(fixture.database) == before_files
    assert _counts(fixture.database) == {table: 0 for table in RUN_TABLES}


def test_unsafe_snapshot_state_is_a_domain_conflict_without_mutation(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    service = _service(fixture, request=request)
    journal = Path(f"{fixture.database}-journal")
    journal.write_bytes(b"ambiguous-hot-journal")
    before = _database_family_snapshot(fixture.database)

    with pytest.raises(
        Phase9RunGenerationConflict,
        match="Authority state snapshot cannot be read safely",
    ):
        service.create_or_rotate(request)

    assert _database_family_snapshot(fixture.database) == before


@pytest.mark.parametrize("damage", ("missing", "moved_key", "moved_workflow"))
def test_idempotency_miss_cannot_fall_through_an_existing_commit(
    tmp_path,
    damage,
):
    fixture = install_foundation(tmp_path)
    request = _request()
    service = _service(fixture, request=request)
    service.create_or_rotate(request)
    table = "authority_production_run_generation_idempotency"
    connection = sqlite3.connect(fixture.database)
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
                f"UPDATE {table} SET idempotency_key='generation-key-moved' "
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
    recovery = _service(
        fixture,
        request=request,
        prepare_evidence=False,
        clock=lambda: request.occurred_at + 301,
    )
    recovery.official_input_root = tmp_path / "official-input-no-longer-live"
    recovery.execution_context_receipt_path = (
        tmp_path / "context-no-longer-live.json"
    )
    before = _database_family_snapshot(fixture.database)

    with pytest.raises(
        Phase9RunGenerationConflict,
        match="idempotency",
    ):
        recovery.create_or_rotate(request)

    assert _database_family_snapshot(fixture.database) == before


def test_orphaned_receipt_reserves_global_key_across_workflows(tmp_path):
    fixture = install_foundation(tmp_path)
    other_workflow = "workflow-orphaned-global-key"
    _insert_empty_workflow_clone(fixture.database, other_workflow)
    request = _request(key="orphaned-global-run-generation-key")
    _service(fixture, request=request).create_or_rotate(request)
    table = "authority_production_run_generation_idempotency"
    connection = sqlite3.connect(fixture.database)
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
    changed = _request_for_workflow(request, other_workflow)
    service = _service(
        fixture,
        request=changed,
        prepare_evidence=False,
        clock=lambda: changed.occurred_at + 301,
    )
    before = _database_family_snapshot(fixture.database)

    with pytest.raises(
        Phase9RunGenerationConflict,
        match="trace lacks its exact global idempotency key binding",
    ):
        service.create_or_rotate(changed)

    assert _database_family_snapshot(fixture.database) == before
    assert _counts(fixture.database)["authority_production_run_generations"] == 1


@pytest.mark.parametrize(
    "damage",
    (
        "creation_receipt",
        "generation",
        "succession",
        "source_inventory",
        "source_inventory_recorded_at",
        "current_pointer",
        "missing_business_object",
        "idempotency_alias",
    ),
)
def test_exact_replay_rejects_incomplete_or_corrupt_committed_graph(
    tmp_path,
    damage,
):
    fixture = install_foundation(tmp_path)
    request = _request()
    service = _service(fixture, request=request)
    service.create_or_rotate(request)
    table = {
        "creation_receipt": "authority_production_run_generation_creation_receipts",
        "generation": "authority_production_run_generations",
        "succession": "authority_production_run_generation_successions",
        "source_inventory": (
            "authority_production_run_generation_source_inventories"
        ),
        "source_inventory_recorded_at": (
            "authority_production_run_generation_source_inventories"
        ),
        "current_pointer": "authority_production_run_generation_current",
        "missing_business_object": "authority_production_run_generations",
        "idempotency_alias": (
            "authority_production_run_generation_idempotency"
        ),
    }[damage]
    connection = sqlite3.connect(fixture.database)
    try:
        triggers = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND tbl_name=?",
            (table,),
        ).fetchall()
        for name, _sql in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        if damage == "creation_receipt":
            connection.execute(
                "UPDATE authority_production_run_generation_creation_receipts "
                "SET receipt_json='{}' WHERE run_generation=?",
                (request.derived_run_generation,),
            )
        elif damage == "generation":
            connection.execute(
                "UPDATE authority_production_run_generations "
                "SET project_id='wrong-project' WHERE run_generation=?",
                (request.derived_run_generation,),
            )
        elif damage == "succession":
            connection.execute(
                "UPDATE authority_production_run_generation_successions "
                "SET succession_json='{}' WHERE run_generation=?",
                (request.derived_run_generation,),
            )
        elif damage == "source_inventory":
            connection.execute(
                "UPDATE authority_production_run_generation_source_inventories "
                "SET inventory_json='{}' WHERE inventory_sha256=?",
                (request.source_inventory_sha256,),
            )
        elif damage == "source_inventory_recorded_at":
            connection.execute(
                "UPDATE authority_production_run_generation_source_inventories "
                "SET recorded_at=0 WHERE inventory_sha256=?",
                (request.source_inventory_sha256,),
            )
        elif damage == "current_pointer":
            connection.execute(
                "DELETE FROM authority_production_run_generation_current "
                "WHERE workflow_id=?",
                (request.workflow_id,),
            )
        elif damage == "missing_business_object":
            connection.execute(
                "DELETE FROM authority_production_run_generations "
                "WHERE run_generation=?",
                (request.derived_run_generation,),
            )
        elif damage == "idempotency_alias":
            connection.execute(
                "INSERT INTO authority_production_run_generation_idempotency "
                "SELECT workflow_id, 'generation-key-alias', request_sha256, "
                "run_generation, creation_receipt_sha256 FROM "
                "authority_production_run_generation_idempotency "
                "WHERE workflow_id=? AND idempotency_key=?",
                (request.workflow_id, request.idempotency_key),
            )
        for _name, sql in triggers:
            connection.execute(sql)
        connection.commit()
    finally:
        connection.close()
    damaged_bytes = fixture.database.read_bytes()
    damaged_counts = _counts(fixture.database)

    with pytest.raises(
        Phase9RunGenerationConflict,
        match=(
            "committed replay graph|source inventory|current pointer|"
            "reverse binding"
        ),
    ):
        service.create_or_rotate(request)

    assert fixture.database.read_bytes() == damaged_bytes
    assert _counts(fixture.database) == damaged_counts


def test_shared_inventory_survives_cross_workflow_occurrence_time_regression(
    tmp_path,
):
    """Inventory insertion order is not inferred from request timestamps."""

    fixture = install_foundation(tmp_path)
    connection = sqlite3.connect(fixture.database)
    try:
        connection.execute(
            """
            INSERT INTO authority_workflows(
                workflow_id, project_id, project_generation, run_generation,
                runtime_generation, scheduler_generation, current_revision,
                current_revision_availability, contract_pin_set_sha256,
                contract_pin_availability, authority_state
            )
            SELECT 'workflow-2', project_id, project_generation, run_generation,
                   runtime_generation, scheduler_generation, current_revision,
                   current_revision_availability, contract_pin_set_sha256,
                   contract_pin_availability, authority_state
            FROM authority_workflows WHERE workflow_id='legacy_current'
            """
        )
        connection.commit()
    finally:
        connection.close()
    first_request = _request(occurred_at=2000, key="inventory-first")
    second_request = replace(
        _request(occurred_at=1999, key="inventory-second"),
        workflow_id="workflow-2",
    )
    second_request = replace(
        second_request,
        project_generation=second_request.derived_project_generation,
        operator_authorization=replace(
            second_request.operator_authorization,
            workflow_id="workflow-2",
        ),
    )
    second_request = _reauthorize(second_request)
    first_service = _service(fixture, request=first_request)
    second_service = _service(
        fixture,
        request=second_request,
        clock=lambda: second_request.occurred_at,
    )

    first = first_service.create_or_rotate(first_request)
    second = second_service.create_or_rotate(second_request)
    before = fixture.database.read_bytes()

    assert first_service.create_or_rotate(first_request) == first
    assert second_service.create_or_rotate(second_request) == second
    assert fixture.database.read_bytes() == before
    connection = sqlite3.connect(fixture.database)
    try:
        recorded_at = connection.execute(
            "SELECT recorded_at FROM "
            "authority_production_run_generation_source_inventories "
            "WHERE inventory_sha256=?",
            (first_request.source_inventory_sha256,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert recorded_at == first_request.occurred_at
    assert second_request.occurred_at < recorded_at


def test_exact_replay_rejects_cross_workflow_dangling_successor(
    tmp_path,
):
    fixture = install_foundation(tmp_path)
    request = _request()
    service = _service(fixture, request=request)
    service.create_or_rotate(request)
    connection = sqlite3.connect(fixture.database)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            """
            INSERT INTO authority_workflows(
                workflow_id, project_id, project_generation, run_generation,
                runtime_generation, scheduler_generation, current_revision,
                current_revision_availability, contract_pin_set_sha256,
                contract_pin_availability, authority_state
            )
            SELECT 'workflow-dangling', project_id, project_generation,
                   run_generation, runtime_generation, scheduler_generation,
                   current_revision, current_revision_availability,
                   contract_pin_set_sha256, contract_pin_availability,
                   authority_state
            FROM authority_workflows WHERE workflow_id=?
            """,
            (request.workflow_id,),
        )
        table = "authority_production_run_generations"
        triggers = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND tbl_name=?",
            (table,),
        ).fetchall()
        for name, _sql in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        source = dict(
            connection.execute(
                "SELECT * FROM authority_production_run_generations "
                "WHERE run_generation=?",
                (request.derived_run_generation,),
            ).fetchone()
        )
        source.update(
            run_generation="run-generation:cross-workflow-dangling",
            workflow_id="workflow-dangling",
            predecessor_run_generation=request.derived_run_generation,
            predecessor_creation_receipt_sha256=connection.execute(
                "SELECT receipt_sha256 FROM "
                "authority_production_run_generation_creation_receipts "
                "WHERE run_generation=?",
                (request.derived_run_generation,),
            ).fetchone()[0],
            predecessor_terminal_receipt_sha256=None,
            operation_kind=ROTATE,
            authorization_id="cross-workflow-dangling-authorization",
            authorization_target_sha256="d" * 64,
            request_sha256="e" * 64,
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
    damaged = fixture.database.read_bytes()

    with pytest.raises(Phase9RunGenerationConflict, match="dangling successor"):
        service.create_or_rotate(request)

    assert fixture.database.read_bytes() == damaged


def test_exact_replay_rejects_succession_only_dangling_successor(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    service = _service(fixture, request=request)
    committed = service.create_or_rotate(request)
    table = "authority_production_run_generation_successions"
    connection = sqlite3.connect(fixture.database)
    connection.row_factory = sqlite3.Row
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
                f"SELECT * FROM {table} WHERE run_generation=?",
                (committed.run_generation,),
            ).fetchone()
        )
        dangling.update(
            run_generation="run-generation:succession-only-dangling",
            predecessor_run_generation=committed.run_generation,
            predecessor_creation_receipt_sha256=committed.receipt_sha256,
            succession_json="{}",
            succession_sha256="d" * 64,
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
    damaged = _database_family_snapshot(fixture.database)

    with pytest.raises(Phase9RunGenerationConflict, match="dangling successor"):
        service.create_or_rotate(request)

    assert _database_family_snapshot(fixture.database) == damaged


def test_two_concurrent_exact_creates_commit_once_and_recover_once(tmp_path):
    fixture = install_foundation(tmp_path)
    request = _request()
    first_service = _service(fixture, request=request)
    second_service = _service(
        fixture,
        request=request,
        prepare_evidence=False,
    )
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def invoke(service):
        try:
            barrier.wait(timeout=10)
            results.append(service.create_or_rotate(request))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=invoke, args=(first_service,)),
        threading.Thread(target=invoke, args=(second_service,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(results) == 2
    assert all(result.replayed is False for result in results)
    assert results[0] == results[1]
    assert results[0].as_dict() == results[1].as_dict()
    assert _counts(fixture.database) == {
        "authority_production_run_generations": 1,
        "authority_production_run_generation_current": 1,
        "authority_production_run_generation_creation_receipts": 1,
        "authority_production_run_generation_idempotency": 1,
        "authority_production_run_generation_successions": 1,
        "authority_production_run_generation_source_inventories": 1,
        "authority_production_run_generation_authorization_consumptions": 1,
    }


def test_exact_peer_commit_queued_on_lease_preempts_closed_live_gates(
    tmp_path,
):
    """A peer queued on the lease recovers before evaluating closed live gates."""

    fixture = install_foundation(tmp_path)
    request = _reauthorize(
        _request(), expires_at=_request().occurred_at + 50
    )
    first_inside_transaction = threading.Event()
    second_started = threading.Event()

    def pause_first(checkpoint):
        if checkpoint == "after_contract_pin":
            first_inside_transaction.set()
            assert second_started.wait(timeout=30)

    first_service = _service(
        fixture,
        request=request,
        clock=lambda: request.occurred_at,
        fault_hook=pause_first,
    )
    second_service = _service(
        fixture,
        request=request,
        prepare_evidence=False,
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
            outcomes.append(service.create_or_rotate(request))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first_worker = threading.Thread(target=invoke, args=(first_service,))
    second_worker = threading.Thread(
        target=invoke, args=(second_service,), kwargs={"mark_started": True}
    )
    first_worker.start()
    assert first_inside_transaction.wait(timeout=30)
    second_worker.start()
    assert second_has_lease.wait(timeout=30)
    before = _database_family_snapshot(fixture.database)
    allow_recovery.set()
    first_worker.join(timeout=30)
    second_worker.join(timeout=30)

    assert not first_worker.is_alive()
    assert not second_worker.is_alive()
    assert errors == []
    assert len(outcomes) == 2
    assert all(result.replayed is False for result in outcomes)
    assert outcomes[0] == outcomes[1]
    assert outcomes[0].as_dict() == outcomes[1].as_dict()
    assert _database_family_snapshot(fixture.database) == before


def test_concurrent_same_key_different_requests_never_overwrite(tmp_path):
    fixture = install_foundation(tmp_path)
    first = _request()
    second = _reauthorize(replace(first, occurred_at=first.occurred_at + 1))
    first_service = _service(fixture, request=first)
    second_service = _service(
        fixture,
        request=second,
        prepare_evidence=False,
        clock=lambda: second.occurred_at,
    )
    barrier = threading.Barrier(2)
    outcomes = []

    def invoke(service, request):
        try:
            barrier.wait(timeout=10)
            outcomes.append(service.create_or_rotate(request))
        except Exception as exc:  # pragma: no cover - asserted below
            outcomes.append(exc)

    threads = [
        threading.Thread(target=invoke, args=(first_service, first)),
        threading.Thread(target=invoke, args=(second_service, second)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(outcomes) == 2
    assert sum(type(value) is RunGenerationCreationResult for value in outcomes) == 1
    conflicts = [value for value in outcomes if type(value) is Phase9RunGenerationConflict]
    assert len(conflicts) == 1
    assert "idempotency key" in str(conflicts[0])
    counts = _counts(fixture.database)
    assert counts["authority_production_run_generations"] == 1
    assert counts["authority_production_run_generation_idempotency"] == 1
    assert counts[
        "authority_production_run_generation_authorization_consumptions"
    ] == 1


def test_concurrent_same_key_cross_workflow_requests_commit_only_once(tmp_path):
    fixture = install_foundation(tmp_path)
    other_workflow = "workflow-concurrent-global-key"
    _insert_empty_workflow_clone(fixture.database, other_workflow)
    first = _request(key="concurrent-global-run-generation-key")
    second = _request_for_workflow(first, other_workflow)
    first_service = _service(fixture, request=first)
    second_service = _service(
        fixture,
        request=second,
        clock=lambda: second.occurred_at,
    )
    barrier = threading.Barrier(2)
    outcomes = []

    def invoke(service, request):
        try:
            barrier.wait(timeout=10)
            outcomes.append(service.create_or_rotate(request))
        except Exception as exc:  # pragma: no cover - asserted below
            outcomes.append(exc)

    workers = [
        threading.Thread(target=invoke, args=(first_service, first)),
        threading.Thread(target=invoke, args=(second_service, second)),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)

    assert all(not worker.is_alive() for worker in workers)
    assert len(outcomes) == 2
    results = [
        value for value in outcomes if type(value) is RunGenerationCreationResult
    ]
    conflicts = [
        value for value in outcomes if type(value) is Phase9RunGenerationConflict
    ]
    assert len(results) == 1
    assert results[0].replayed is False
    assert len(conflicts) == 1
    assert "idempotency key" in str(conflicts[0])
    connection = sqlite3.connect(fixture.database)
    try:
        binding = connection.execute(
            "SELECT workflow_id, request_sha256, run_generation FROM "
            "authority_production_run_generation_idempotency "
            "WHERE idempotency_key=?",
            (first.idempotency_key,),
        ).fetchall()
        assert len(binding) == 1
        assert binding[0][0] in {first.workflow_id, second.workflow_id}
        assert connection.execute(
            "SELECT COUNT(*) FROM authority_production_run_generations"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM "
            "authority_production_run_generation_authorization_consumptions"
        ).fetchone()[0] == 1
        advanced = connection.execute(
            "SELECT COUNT(*) FROM authority_workflows "
            "WHERE run_generation!='legacy_unknown'"
        ).fetchone()[0]
        assert advanced == 1
    finally:
        connection.close()


@pytest.mark.parametrize("peer_kind", ("exact", "cross_workflow"))
def test_independent_processes_serialize_same_key_run_generation_requests(
    tmp_path,
    peer_kind,
):
    """Exercise the real OS flock with two independently forked writers."""

    context = multiprocessing.get_context("fork")
    fixture = install_foundation(tmp_path)
    first = _request(key=f"process-global-run-generation-{peer_kind}")
    if peer_kind == "cross_workflow":
        other_workflow = "workflow-process-global-key"
        _insert_empty_workflow_clone(fixture.database, other_workflow)
        second = _request_for_workflow(first, other_workflow)
    else:
        second = first
    first_inside_transaction = context.Event()
    peer_started = context.Event()

    def hold_first(checkpoint):
        if checkpoint == "after_contract_pin":
            first_inside_transaction.set()
            assert peer_started.wait(timeout=30)

    first_service = _service(
        fixture,
        request=first,
        fault_hook=hold_first,
    )
    second_service = _service(
        fixture,
        request=second,
        prepare_evidence=peer_kind != "exact",
        clock=(
            (lambda: second.occurred_at + 301)
            if peer_kind == "exact"
            else (lambda: second.occurred_at)
        ),
    )
    output = context.Queue()

    def invoke(service, value, *, mark_started=False):
        if mark_started:
            peer_started.set()
        try:
            output.put(("result", service.create_or_rotate(value).as_dict()))
        except Exception as exc:  # pragma: no cover - asserted in parent
            output.put(("error", type(exc).__name__, str(exc)))

    first_process = context.Process(target=invoke, args=(first_service, first))
    second_process = context.Process(
        target=invoke,
        args=(second_service, second),
        kwargs={"mark_started": True},
    )
    first_process.start()
    assert first_inside_transaction.wait(timeout=60)
    second_process.start()
    first_process.join(timeout=120)
    second_process.join(timeout=120)
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
        assert errors[0][1] == "Phase9RunGenerationConflict"
        assert "idempotency key" in errors[0][2]
    counts = _counts(fixture.database)
    assert counts["authority_production_run_generations"] == 1
    assert counts["authority_production_run_generation_idempotency"] == 1
    assert counts[
        "authority_production_run_generation_authorization_consumptions"
    ] == 1


def test_predecessor_exact_replay_survives_committed_successor_generation(tmp_path):
    fixture = install_foundation(tmp_path)
    create = _request(formal_p0_context=True)
    create_service = _service(fixture, request=create)
    first = create_service.create_or_rotate(create)
    terminal = _record_completed_predecessor(
        fixture,
        create,
        first.run_generation,
        first.receipt_sha256,
    )
    rotate = _request(
        operation_kind=ROTATE,
        key="generation-successor-key",
        predecessor=first.run_generation,
        predecessor_receipt=first.receipt_sha256,
        predecessor_terminal_receipt=terminal,
        occurred_at=create.occurred_at + 1,
        formal_p0_context=True,
    )
    successor = _service(fixture, request=rotate).create_or_rotate(rotate)
    before_bytes = fixture.database.read_bytes()

    recovered = _service(
        fixture,
        request=create,
        prepare_evidence=False,
        clock=lambda: create.operator_authorization.expires_at + 1,
    ).create_or_rotate(create)

    assert recovered == first
    assert recovered.as_dict() == first.as_dict()
    assert successor.run_generation != recovered.run_generation
    assert fixture.database.read_bytes() == before_bytes

    connection = sqlite3.connect(fixture.database)
    connection.row_factory = sqlite3.Row
    table = "authority_production_run_generation_successions"
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
                f"SELECT * FROM {table} WHERE run_generation=?",
                (successor.run_generation,),
            ).fetchone()
        )
        dangling.update(
            run_generation="run-generation:historical-tip-dangling",
            predecessor_run_generation=successor.run_generation,
            predecessor_creation_receipt_sha256=successor.receipt_sha256,
            succession_json="{}",
            succession_sha256="d" * 64,
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
    damaged = _database_family_snapshot(fixture.database)

    with pytest.raises(Phase9RunGenerationConflict, match="dangling successor"):
        create_service.create_or_rotate(create)

    assert _database_family_snapshot(fixture.database) == damaged


def test_rotate_exact_replay_rejects_missing_predecessor_terminal_graph(tmp_path):
    fixture = install_foundation(tmp_path)
    create = _request(formal_p0_context=True)
    first = _service(fixture, request=create).create_or_rotate(create)
    terminal = _record_completed_predecessor(
        fixture,
        create,
        first.run_generation,
        first.receipt_sha256,
    )
    rotate = _request(
        operation_kind=ROTATE,
        key="generation-predecessor-damage-key",
        predecessor=first.run_generation,
        predecessor_receipt=first.receipt_sha256,
        predecessor_terminal_receipt=terminal,
        occurred_at=create.occurred_at + 1,
        formal_p0_context=True,
    )
    rotate_service = _service(fixture, request=rotate)
    committed = rotate_service.create_or_rotate(rotate)
    assert rotate_service.create_or_rotate(rotate) == committed

    connection = sqlite3.connect(fixture.database)
    try:
        table = "authority_production_phase9_terminal_receipts"
        triggers = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND tbl_name=?",
            (table,),
        ).fetchall()
        for name, _sql in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(
            "DELETE FROM authority_production_phase9_terminal_receipts "
            "WHERE receipt_sha256=?",
            (terminal,),
        )
        for _name, sql in triggers:
            connection.execute(sql)
        connection.commit()
    finally:
        connection.close()
    damaged = fixture.database.read_bytes()

    with pytest.raises(
        Phase9RunGenerationConflict, match="predecessor terminal"
    ):
        rotate_service.create_or_rotate(rotate)

    assert fixture.database.read_bytes() == damaged


def test_official_inventory_rejects_extra_empty_directory(tmp_path):
    request = _request()
    root = tmp_path / "official-root"
    official = root / "official" / "problem.pdf"
    official.parent.mkdir(parents=True)
    official.write_bytes(OFFICIAL_BYTES)
    (root / "unexpected-empty").mkdir()

    with pytest.raises(
        Phase9RunGenerationSafetyError, match="directory inventory differs"
    ):
        run_generation.verify_official_input_snapshot(root, request.official_inputs)


def test_official_inventory_fails_closed_when_enumeration_errors(
    tmp_path, monkeypatch
):
    request = _request()
    root = tmp_path / "official-root"
    official = root / "official" / "problem.pdf"
    official.parent.mkdir(parents=True)
    official.write_bytes(OFFICIAL_BYTES)

    def fail_listdir(_descriptor):
        raise PermissionError("injected enumeration denial")

    monkeypatch.setattr(run_generation.os, "listdir", fail_listdir)
    with pytest.raises(Phase9RunGenerationSafetyError, match="cannot be enumerated"):
        run_generation.verify_official_input_snapshot(root, request.official_inputs)


@pytest.mark.parametrize("special_kind", ("fifo", "socket", "hardlink"))
def test_official_inventory_rejects_special_and_hardlinked_members(
    tmp_path, special_kind
):
    request = _request()
    root = tmp_path.parent / f"oi-{special_kind}"
    official = root / "official" / "problem.pdf"
    official.parent.mkdir(parents=True)
    listener = None
    directory_fd = None
    if special_kind == "fifo":
        os.mkfifo(official)
    elif special_kind == "socket":
        listener = socket.socket(socket.AF_UNIX)
        # Address the already-open directory instead of spelling the pytest
        # basetemp path into sockaddr_un.  This keeps the rejection case real
        # on platforms with a short AF_UNIX path limit; the created directory
        # entry is still the exact member inspected by the validator.
        directory_fd = os.open(official.parent, os.O_RDONLY | os.O_DIRECTORY)
        listener.bind(f"/proc/self/fd/{directory_fd}/{official.name}")
    else:
        original = tmp_path / "outside.bin"
        original.write_bytes(OFFICIAL_BYTES)
        os.link(original, official)
    try:
        with pytest.raises(
            Phase9RunGenerationSafetyError,
            match="symlink, hardlink, or special file",
        ):
            run_generation.verify_official_input_snapshot(
                root, request.official_inputs
            )
    finally:
        if listener is not None:
            listener.close()
        if directory_fd is not None:
            os.close(directory_fd)


def test_official_inventory_rejects_same_bytes_member_replacement(
    tmp_path, monkeypatch
):
    request = _request()
    root = tmp_path / "official-root"
    official = root / "official" / "problem.pdf"
    official.parent.mkdir(parents=True)
    official.write_bytes(OFFICIAL_BYTES)
    original_read = run_generation._StableDirectoryTree.read_regular_file
    replaced = False

    def replace_after_read(self, parent_parts, name, *, maximum_bytes):
        nonlocal replaced
        result = original_read(
            self, parent_parts, name, maximum_bytes=maximum_bytes
        )
        if self.label == "official input root" and not replaced:
            replaced = True
            displaced = official.with_suffix(".old")
            official.rename(displaced)
            official.write_bytes(OFFICIAL_BYTES)
        return result

    monkeypatch.setattr(
        run_generation._StableDirectoryTree,
        "read_regular_file",
        replace_after_read,
    )
    with pytest.raises(Phase9RunGenerationSafetyError, match="changed"):
        run_generation.verify_official_input_snapshot(root, request.official_inputs)


def test_official_inventory_rejects_nested_directory_pathname_replacement(
    tmp_path, monkeypatch
):
    request = _request()
    root = tmp_path / "official-root"
    official_dir = root / "official"
    official = official_dir / "problem.pdf"
    official_dir.mkdir(parents=True)
    official.write_bytes(OFFICIAL_BYTES)
    original_read = run_generation._StableDirectoryTree.read_regular_file
    replaced = False

    def replace_directory_after_read(self, parent_parts, name, *, maximum_bytes):
        nonlocal replaced
        result = original_read(
            self, parent_parts, name, maximum_bytes=maximum_bytes
        )
        if self.label == "official input root" and not replaced:
            replaced = True
            official_dir.rename(tmp_path / "official-displaced")
            official_dir.mkdir()
            (official_dir / "problem.pdf").write_bytes(OFFICIAL_BYTES)
        return result

    monkeypatch.setattr(
        run_generation._StableDirectoryTree,
        "read_regular_file",
        replace_directory_after_read,
    )
    with pytest.raises(Phase9RunGenerationSafetyError, match="changed"):
        run_generation.verify_official_input_snapshot(root, request.official_inputs)


def _two_commit_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "source-repository"
    repository.mkdir()
    commands = (
        ("init", "-q"),
        ("config", "user.name", "Phase9 Test"),
        ("config", "user.email", "phase9@example.invalid"),
    )
    for arguments in commands:
        subprocess.run(
            ("git", *arguments), cwd=repository, check=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    tracked = repository / "tracked.txt"
    tracked.write_bytes(b"first\n")
    subprocess.run(("git", "add", "tracked.txt"), cwd=repository, check=True)
    subprocess.run(
        ("git", "commit", "-q", "-m", "first"), cwd=repository, check=True
    )
    tracked.write_bytes(b"second\n")
    subprocess.run(("git", "add", "tracked.txt"), cwd=repository, check=True)
    subprocess.run(
        ("git", "commit", "-q", "-m", "second"), cwd=repository, check=True
    )
    return repository


def test_source_snapshot_rejects_dirty_tracked_bytes_with_unchanged_head(tmp_path):
    repository = _two_commit_repository(tmp_path)
    clean = read_current_git_source_snapshot(repository)
    (repository / "untracked.txt").write_bytes(b"ignored untracked evidence\n")
    assert read_current_git_source_snapshot(repository) == clean

    (repository / "tracked.txt").write_bytes(b"same OID, changed live bytes\n")
    assert read_current_git_source_identity(repository) == clean.source
    with pytest.raises(
        Phase9RunGenerationSafetyError, match="tracked worktree or index differs"
    ):
        read_current_git_source_snapshot(repository)


def _archive_git_head(repository: Path, destination: Path) -> None:
    result = subprocess.run(
        ("git", "archive", "--format=tar", "HEAD"),
        cwd=repository,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    destination.mkdir()
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        archive.extractall(destination, filter="data")


def _run_execution_source_check(
    repository: Path,
    import_roots: tuple[Path, ...],
    *,
    cwd: Path,
) -> subprocess.CompletedProcess:
    insertions = "\n".join(
        f"sys.path.insert(0, {str(path)!r})" for path in reversed(import_roots)
    )
    script = (
        "import sys\n"
        f"{insertions}\n"
        "from factory_core.phase9_run_generation import "
        "read_verified_execution_source_snapshot\n"
        f"read_verified_execution_source_snapshot({str(repository)!r})\n"
    )
    return subprocess.run(
        (sys.executable, "-I", "-B", "-c", script),
        cwd=cwd,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_execution_source_matches_a_fresh_no_git_archive_and_rejects_pollution(
    tmp_path,
):
    repository = _source_repository()
    clean_archive = tmp_path / "clean-archive"
    _archive_git_head(repository, clean_archive)
    assert not (clean_archive / ".git").exists()

    exact = _run_execution_source_check(
        repository,
        (clean_archive,),
        cwd=tmp_path,
    )
    assert exact.returncode == 0, exact.stderr

    modified_archive = tmp_path / "modified-archive"
    shutil.copytree(clean_archive, modified_archive)
    modified = modified_archive / "factory_core/canonical.py"
    modified.write_bytes(modified.read_bytes() + b"\n# execution-byte-drift\n")
    changed = _run_execution_source_check(
        repository,
        (modified_archive,),
        cwd=tmp_path,
    )
    assert changed.returncode != 0
    assert "execution source differs from candidate Git tree" in changed.stderr

    polluted = _run_execution_source_check(
        repository,
        (modified_archive, clean_archive),
        cwd=tmp_path,
    )
    assert polluted.returncode != 0
    assert "execution source differs from candidate Git tree" in polluted.stderr
