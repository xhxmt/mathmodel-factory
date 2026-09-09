from __future__ import annotations

from dataclasses import replace
import copy
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import socket
import subprocess

import pytest

import factory_core.phase9_entry as phase9_entry
import factory_core.phase9_p0_evidence as phase9_p0_evidence
from factory_core.canonical import canonical_bytes, canonical_sha256
from factory_core.contract_pins import compile_contract_pin_set
from factory_core.phase9_entry import (
    PHASE9_EXECUTION_CONTEXT_SCHEMA,
    PHASE9_OFFICIAL_INPUT_FILE_SCHEMA,
    PHASE9_OFFICIAL_INPUT_MANIFEST_SCHEMA,
    PHASE9_OPERATOR_AUTHORIZATION_SCHEMA,
    PHASE9_P0_COMMAND_RECORD_SCHEMA,
    PHASE9_P0_RECEIPT_SCHEMA,
    P0_REQUIREMENTS,
    CandidateIdentity,
    Phase9EntryError,
    _p0_runner_attestation,
    blocked_phase9_entry_result,
    collect_phase9_entry_state,
    p0_evidence_root_sha256,
    verify_candidate_source,
    verify_official_input_manifest,
    verify_phase9_entry_gate,
)
from factory_core.phase9_p0_evidence import (
    Phase9P0EvidenceError,
    phase9_p0_execution_context_bindings,
    phase9_p0_test_nodes,
    produce_formal_phase9_p0_evidence,
)
from factory_core.phase9_run_generation import (
    CREATE,
    DELIVERY_DISABLED,
    EXECUTION_CONTEXT_EVIDENCE_SCHEMA,
    GIT_SOURCE_IDENTITY_SCHEMA,
    OFFICIAL_INPUT_FILE_EVIDENCE_SCHEMA,
    OFFICIAL_INPUT_MANIFEST_EVIDENCE_SCHEMA,
    OPERATOR_AUTHORIZATION_EVIDENCE_SCHEMA,
    RUN_GENERATION_REQUEST_SCHEMA,
    ExecutionContextEvidenceV1,
    GitSourceIdentityV1,
    OfficialInputFileEvidenceV1,
    OfficialInputManifestEvidenceV1,
    OperatorAuthorizationEvidenceV1,
    Phase9RunGenerationService,
    RunGenerationRequestV1,
    read_current_git_source_identity,
    read_current_git_source_snapshot,
    read_verified_execution_source_snapshot,
)
from factory_core.workflow_contract_v2 import compile_workflow_contract_bundle_v2
from tests.support.authority_production import install_foundation


EXECUTION_ROOT = Path(__file__).resolve().parents[1]
_TEST_SOURCE_REPOSITORY: Path | None = None
_FORMAL_P0_BUNDLES: dict[tuple[str, ...], object] = {}


def test_candidate_file_reader_rejects_final_pathname_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "candidate-manifest.json"
    target.write_bytes(b"same-length-manifest\n")
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

    monkeypatch.setattr(phase9_entry.os, "fstat", replacing_fstat)
    with pytest.raises(Phase9EntryError, match="changed while being read"):
        phase9_entry._regular_file_bytes(
            target, maximum_bytes=1024, label="candidate manifest"
        )


def _source_repository() -> Path:
    """Use the exact Git identity root for tests run from a no-.git extraction."""

    if _TEST_SOURCE_REPOSITORY is not None:
        return _TEST_SOURCE_REPOSITORY
    raw = os.environ.get("PHASE9_TEST_SOURCE_REPOSITORY")
    if raw is None:
        return EXECUTION_ROOT
    path = Path(raw)
    if not path.is_absolute():
        raise AssertionError("PHASE9_TEST_SOURCE_REPOSITORY must be absolute")
    return path


def _formal_test_source_repository(tmp_path: Path) -> Path:
    """Create one clean, single-parent Git snapshot of the live test sources."""

    global _TEST_SOURCE_REPOSITORY
    if _TEST_SOURCE_REPOSITORY is not None:
        return _TEST_SOURCE_REPOSITORY
    # The formal source/fresh runner provides an immutable repository identity
    # separately from the executed tree.  Reuse it only after independently
    # checking that every executing byte and mode matches that Git tree.  This
    # is the only valid path for a no-.git clean-room export.
    if os.environ.get("PHASE9_TEST_SOURCE_REPOSITORY") is not None:
        repository = _source_repository()
        snapshot = read_verified_execution_source_snapshot(
            repository,
            execution_root=EXECUTION_ROOT,
        )
        if not snapshot.tracked_inventory.entries:
            raise AssertionError("formal test source snapshot is empty")
        _TEST_SOURCE_REPOSITORY = repository
        return repository
    root = tmp_path.parent / "phase9-formal-test-source"
    # Pytest can collect this file as ``test_phase9_entry_gate`` while the
    # replay tests import it as ``tests.test_phase9_entry_gate``.  Those are
    # distinct module objects, so their module-local caches do not meet.  The
    # first copy has nevertheless already created the one immutable test Git
    # snapshot for this basetemp.  Reuse that clean snapshot instead of
    # treating normal multi-module collection as a conflicting second run.
    if root.exists():
        if not root.is_dir() or root.is_symlink():
            raise AssertionError("formal test source path is not a directory")
        snapshot = read_current_git_source_snapshot(root)
        if not snapshot.tracked_inventory.entries:
            raise AssertionError("formal test source snapshot is empty")
        _TEST_SOURCE_REPOSITORY = root
        return root
    root.mkdir()
    paths = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=EXECUTION_ROOT,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout.split(b"\0")
    for raw in paths:
        if not raw:
            continue
        relative = raw.decode("utf-8", errors="strict")
        if relative == "autoresearch-results.tsv" or relative.startswith("audit_artifacts/"):
            continue
        source = EXECUTION_ROOT / relative
        if source.is_symlink() or not source.is_file():
            continue
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "phase9-test@example.invalid")
    _git(root, "config", "user.name", "Phase9 Formal Test")
    _git(root, "commit", "--allow-empty", "-qm", "formal-test-parent")
    # Some candidate-tracked historical fixtures also match current ignore
    # rules.  Force-add the byte-exact copied inventory so the synthetic Git
    # object has no untracked or ignored namespace outside its bound tree.
    _git(root, "add", "-f", "-A")
    _git(root, "commit", "-qm", "formal-test-candidate")
    _TEST_SOURCE_REPOSITORY = root
    return root


def _creation_request(input_root: Path) -> RunGenerationRequestV1:
    raw = b"official-input-bytes\n"
    path = input_root / "official"
    path.mkdir(parents=True)
    (path / "problem.pdf").write_bytes(raw)
    source_snapshot = read_current_git_source_snapshot(_source_repository())
    p0_context = phase9_p0_execution_context_bindings(
        source_repository=_source_repository(),
        python_executable=Path(os.sys.executable),
    )
    source = source_snapshot.source
    authorization = OperatorAuthorizationEvidenceV1(
        schema_version=OPERATOR_AUTHORIZATION_EVIDENCE_SCHEMA,
        authorization_id="phase9-generation-authorization",
        authorization_mechanism="CONTROLLED_OS_ACCOUNT",
        authorization_evidence_sha256="7" * 64,
        authorized=True,
        operator_uid=os.geteuid(),
        operator_account=pwd.getpwuid(os.geteuid()).pw_name,
        authorizer_subject="controlled-authorizer",
        operator_subject="controlled-operator",
        operation_kind=CREATE,
        project_id="demo",
        workflow_id="legacy_current",
        source_commit=source.source_commit,
        authorized_request_sha256="0" * 64,
        issued_at=1900,
        expires_at=3000,
        authorization_statement_sha256="0" * 64,
    )
    request = RunGenerationRequestV1(
        schema_version=RUN_GENERATION_REQUEST_SCHEMA,
        idempotency_key="phase9-entry-generation-key",
        operation_kind=CREATE,
        project_id="demo",
        workflow_id="legacy_current",
        project_revision=1,
        project_generation="project-generation-phase9-entry",
        runtime_generation="native_v2",
        scheduler_generation="stage_v1",
        predecessor_run_generation=None,
        predecessor_creation_receipt_sha256=None,
        predecessor_terminal_receipt_sha256=None,
        run_mode="FORENSIC_REPLAY",
        modeling_consultation_contract="LEGACY_NOT_APPLICABLE",
        delivery_capability=DELIVERY_DISABLED,
        source=source,
        source_inventory_sha256=source_snapshot.source_inventory_sha256,
        contract_pins=compile_contract_pin_set(compile_workflow_contract_bundle_v2()),
        official_inputs=OfficialInputManifestEvidenceV1(
            OFFICIAL_INPUT_MANIFEST_EVIDENCE_SCHEMA,
            "official-input-generation-entry",
            (
                OfficialInputFileEvidenceV1(
                    OFFICIAL_INPUT_FILE_EVIDENCE_SCHEMA,
                    "official/problem.pdf",
                    len(raw),
                    hashlib.sha256(raw).hexdigest(),
                ),
            ),
        ),
        execution_context=ExecutionContextEvidenceV1(
            EXECUTION_CONTEXT_EVIDENCE_SCHEMA,
            "phase9-entry-context",
            str(p0_context["runtime_environment"]["descriptor_sha256"]),
            str(p0_context["dependency_lock_sha256"]),
            str(p0_context["launcher"]["descriptor_sha256"]),
            1950,
        ),
        operator_authorization=authorization,
        occurred_at=2000,
    )
    request = replace(
        request,
        project_generation=request.derived_project_generation,
    )
    authorization = replace(
        authorization,
        authorized_request_sha256=request.authorization_target_sha256,
    )
    authorization = replace(
        authorization,
        authorization_statement_sha256=authorization.expected_statement_sha256,
    )
    return replace(request, operator_authorization=authorization)


def _candidate(source: GitSourceIdentityV1) -> CandidateIdentity:
    return CandidateIdentity(source.source_commit, source.source_tree, source.source_parent)


def _p0_receipts(
    candidate: CandidateIdentity,
    evidence_root: Path,
    *,
    authority_database: Path,
    expected_source_fence_sha256: str,
    project_id: str,
    workflow_id: str,
    run_generation: str,
    source_inventory_sha256: str,
) -> dict[str, object]:
    bundle = produce_formal_phase9_p0_evidence(
        authority_database=authority_database,
        expected_source_fence_sha256=expected_source_fence_sha256,
        source_repository=_source_repository(),
        evidence_root=evidence_root,
        python_executable=Path(os.sys.executable),
        workflow_id=workflow_id,
    )
    assert bundle.candidate == candidate.as_dict()
    assert bundle.coordinate == {
        "project_id": project_id,
        "workflow_id": workflow_id,
        "run_generation": run_generation,
    }
    assert bundle.source_inventory_sha256 == source_inventory_sha256
    return copy.deepcopy(bundle.receipts)


def _entry_authorization(
    candidate: CandidateIdentity,
    request: RunGenerationRequestV1,
    p0_root_sha256: str,
) -> dict[str, object]:
    body = {
        "schema": PHASE9_OPERATOR_AUTHORIZATION_SCHEMA,
        "candidate": candidate.as_dict(),
        "project_id": request.project_id,
        "workflow_id": request.workflow_id,
        "run_generation": request.derived_run_generation,
        "authorized_operation": "PHASE9_ENTRY",
        "authorization_mechanism": "CONTROLLED_OS_ACCOUNT",
        "authorization_evidence_sha256": "f" * 64,
        "p0_evidence_root_sha256": p0_root_sha256,
        "operator_account": pwd.getpwuid(os.geteuid()).pw_name,
        "operator_uid": os.geteuid(),
        "authorized": True,
        "issued_at": 2000,
        "expires_at": 2500,
    }
    body["receipt_sha256"] = canonical_sha256(body)
    return body


def _rehash_formal_p0_tree(root: Path) -> dict[str, object]:
    """Rebuild every dependent digest after adversarial byte changes."""

    def reference(relative: str, *, kind: str | None = None) -> dict[str, object]:
        raw = (root / relative).read_bytes()
        value: dict[str, object] = {
            "path": relative,
            "byte_length": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        if kind is not None:
            value["kind"] = kind
        return value

    outcome_path = root / "test_outcomes/p0_suite.json"
    outcome = json.loads(outcome_path.read_bytes())
    outcome["raw_log"] = reference("test_results/p0_suite.log")
    outcome["junit_report"] = reference("test_reports/p0_suite.xml")
    outcome["trusted_events"] = reference("test_events/p0_suite.jsonl")
    outcome.pop("outcome_sha256", None)
    outcome["outcome_sha256"] = canonical_sha256(outcome)
    outcome_path.write_bytes(canonical_bytes(outcome))

    command_path = root / "command_records/p0_suite.json"
    command = json.loads(command_path.read_bytes())
    environment = json.loads(
        (root / "attestations/environment.json").read_bytes()
    )
    command["cwd"] = environment["cwd"]
    command["python"] = environment["python"]
    command["python_runtime"] = environment["python_runtime"]
    command["environment"] = environment["environment"]
    command["environment_sha256"] = environment["environment_sha256"]
    command["excluded_present_environment_names"] = environment[
        "excluded_present_names"
    ]
    command["source_attestation_sha256"] = hashlib.sha256(
        (root / "attestations/source_inventory.json").read_bytes()
    ).hexdigest()
    command["raw_test_log"] = reference("test_results/p0_suite.log")
    command["junit_report"] = reference("test_reports/p0_suite.xml")
    command["trusted_events"] = reference("test_events/p0_suite.jsonl")
    command["test_outcome"] = reference("test_outcomes/p0_suite.json")
    command.pop("command_record_sha256", None)
    command["command_record_sha256"] = canonical_sha256(command)
    command_path.write_bytes(canonical_bytes(command))

    receipts: dict[str, object] = {}
    for requirement in P0_REQUIREMENTS:
        path = root / f"receipts/{requirement}.json"
        receipt = json.loads(path.read_bytes())
        receipt["command_record"] = reference("command_records/p0_suite.json")
        receipt["raw_test_log"] = reference("test_results/p0_suite.log")
        receipt["test_outcome"] = reference("test_outcomes/p0_suite.json")
        receipt["trusted_events"] = reference("test_events/p0_suite.jsonl")
        for evidence in receipt["evidence"]:
            evidence_path = evidence["path"]
            evidence_kind = evidence["kind"]
            evidence.clear()
            evidence.update(reference(evidence_path, kind=evidence_kind))
        receipt["evidence_sha256"] = canonical_sha256(receipt["evidence"])
        receipt.pop("receipt_sha256", None)
        receipt["receipt_sha256"] = canonical_sha256(receipt)
        path.write_bytes(canonical_bytes(receipt))
        receipts[requirement] = receipt
    return receipts


def _ready_fixture(tmp_path: Path):
    source_repository = _formal_test_source_repository(tmp_path)
    fixture = install_foundation(tmp_path)
    input_root = tmp_path / "official-inputs"
    request = _creation_request(input_root)
    context_receipt = tmp_path / "execution-context.json"
    context_receipt.write_bytes(canonical_bytes(request.execution_context.as_dict()))
    result = Phase9RunGenerationService(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        source_repository=source_repository,
        official_input_root=input_root,
        execution_context_receipt_path=context_receipt,
        clock=lambda: 2000,
    ).create_or_rotate(request)
    candidate = _candidate(request.source)
    p0_root = tmp_path / "p0-evidence"
    p0_receipts = _p0_receipts(
        candidate,
        p0_root,
        authority_database=fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        project_id=request.project_id,
        workflow_id=request.workflow_id,
        run_generation=request.derived_run_generation,
        source_inventory_sha256=request.source_inventory_sha256,
    )
    p0_root_sha = p0_evidence_root_sha256(p0_root)
    state = collect_phase9_entry_state(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        workflow_id=request.workflow_id,
        candidate=candidate,
    )
    assert state.creation_receipt_sha256 == result.receipt_sha256
    return (
        fixture,
        input_root,
        request,
        candidate,
        state,
        p0_root,
        p0_root_sha,
        p0_receipts,
    )


def test_read_only_collector_and_candidate_bound_gate_are_ready(tmp_path):
    (
        fixture, input_root, request, candidate, state,
        p0_root, p0_root_sha, p0_receipts,
    ) = _ready_fixture(tmp_path)
    before = hashlib.sha256(fixture.database.read_bytes()).hexdigest()
    source = {
        "mode": "GIT",
        "candidate": candidate.as_dict(),
        "verified_tree": candidate.tree,
        "source_inventory_sha256": request.source_inventory_sha256,
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
    assert result["blockers"] == []
    assert set(result["p0_receipt_sha256s"]) == set(P0_REQUIREMENTS)
    assert result["p0_evidence_root_sha256"] == p0_root_sha
    assert result["authorization_scope"] == {
        "phase9_a_forensic_replay": False,
        "provider_or_network": False,
        "production_outbox_or_delivery": False,
        "release": False,
        "deployment": False,
        "migration": False,
        "cutover": False,
    }
    assert hashlib.sha256(fixture.database.read_bytes()).hexdigest() == before


def test_semantically_valid_file_only_p0_closure_cannot_be_formal_ready(tmp_path):
    (
        _, input_root, request, candidate, state,
        p0_root, p0_root_sha, receipts,
    ) = _ready_fixture(tmp_path)
    result = verify_phase9_entry_gate(
        state=replace(state, p0_runner_attestation=None),
        source_verification={
            "mode": "GIT",
            "candidate": candidate.as_dict(),
            "verified_tree": candidate.tree,
            "source_inventory_sha256": request.source_inventory_sha256,
        },
        p0_receipts=receipts,
        p0_evidence_root=p0_root,
        p0_evidence_root_sha256=p0_root_sha,
        operator_authorization=_entry_authorization(candidate, request, p0_root_sha),
        official_input_manifest=request.official_inputs.as_dict(),
        official_input_root=input_root,
        execution_context=request.execution_context.as_dict(),
        evaluated_at=2100,
        trusted_now=2100,
    )
    assert result["status"] == "BLOCKED"
    assert next(
        item["detail"] for item in result["blockers"]
        if item["code"] == "P0_RECEIPTS_INVALID"
    ) == "P0 evidence has no Authority-backed runner attestation"


def test_query_only_join_rejects_hash_correct_semantically_wrong_attestation_row(
    tmp_path,
):
    fixture, _, _, _, state, _, _, _ = _ready_fixture(tmp_path)
    connection = sqlite3.connect(fixture.database)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            "DROP TRIGGER "
            "authority_production_phase9_p0_runner_attestations_append_only_update"
        )
        row = connection.execute(
            "SELECT * FROM authority_production_phase9_p0_runner_attestations"
        ).fetchone()
        attestation = json.loads(row["attestation_json"])
        attestation["spec_sha256"] = "0" * 64
        attestation.pop("attestation_sha256")
        attestation["attestation_sha256"] = canonical_sha256(attestation)
        connection.execute(
            "UPDATE authority_production_phase9_p0_runner_attestations "
            "SET spec_sha256=?, attestation_json=?, attestation_sha256=?",
            (
                attestation["spec_sha256"],
                canonical_bytes(attestation).decode("utf-8"),
                attestation["attestation_sha256"],
            ),
        )
        connection.commit()
        with pytest.raises(Phase9EntryError, match="provenance differs"):
            _p0_runner_attestation(connection, replace(state, p0_runner_attestation=None))
    finally:
        connection.close()


def test_formal_runner_cannot_reuse_generation_or_issue_second_nonce(tmp_path):
    fixture, _, request, _, _, _, _, _ = _ready_fixture(tmp_path)
    with pytest.raises(Phase9P0EvidenceError, match="already has a successful"):
        produce_formal_phase9_p0_evidence(
            authority_database=fixture.database,
            expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
            source_repository=_source_repository(),
            evidence_root=tmp_path / "second-p0-evidence",
            python_executable=Path(os.sys.executable),
            workflow_id=request.workflow_id,
        )
    connection = sqlite3.connect(fixture.database)
    try:
        assert tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "authority_production_phase9_p0_runner_authorizations",
                "authority_production_phase9_p0_runner_consumptions",
                "authority_production_phase9_p0_runner_attestations",
            )
        ) == (1, 1, 1)
    finally:
        connection.close()


def test_formal_runner_expiry_before_attestation_writes_no_attestation(
    tmp_path, monkeypatch
):
    source_repository = _formal_test_source_repository(tmp_path)
    fixture = install_foundation(tmp_path)
    input_root = tmp_path / "official-inputs"
    request = _creation_request(input_root)
    context_receipt = tmp_path / "execution-context.json"
    context_receipt.write_bytes(canonical_bytes(request.execution_context.as_dict()))
    Phase9RunGenerationService(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        source_repository=source_repository,
        official_input_root=input_root,
        execution_context_receipt_path=context_receipt,
        clock=lambda: 2000,
    ).create_or_rotate(request)

    real_time = phase9_p0_evidence.time

    class ExpiringClock:
        calls = 0

        def time_ns(self):
            self.calls += 1
            seconds = 2_000_000_000 + (301 if self.calls >= 5 else 0)
            return seconds * 1_000_000_000

        def monotonic_ns(self):
            return real_time.monotonic_ns()

    monkeypatch.setattr(phase9_p0_evidence, "time", ExpiringClock())
    with pytest.raises(
        Phase9P0EvidenceError,
        match="changed before P0 attestation",
    ):
        produce_formal_phase9_p0_evidence(
            authority_database=fixture.database,
            expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
            source_repository=source_repository,
            evidence_root=tmp_path / "expired-p0-evidence",
            python_executable=Path(os.sys.executable),
            workflow_id=request.workflow_id,
        )
    connection = sqlite3.connect(fixture.database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM authority_production_phase9_p0_runner_attestations"
        ).fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.parametrize(
    "forgery",
    (
        "arbitrary-pass-log",
        "arbitrary-argv",
        "nonexistent-cwd",
        "nonexistent-python",
        "nonexistent-test-node",
        "wrong-source-inventory",
        "wrong-producer",
        "wrong-junit-node",
        "parseable-failed-log",
        "setup-error-log",
        "truncated-log",
        "command-log-mismatch",
        "command-duration-mismatch",
        "command-outside-authorization-window",
        "cross-coordinate",
    ),
)
def test_gate_rejects_fully_rehashed_nonformal_p0_semantics(tmp_path, forgery):
    (
        _, input_root, request, candidate, state,
        p0_root, _, receipts,
    ) = _ready_fixture(tmp_path)
    receipts = copy.deepcopy(receipts)
    if forgery == "arbitrary-pass-log":
        (p0_root / "test_results/p0_suite.log").write_bytes(
            b"PASS AR_007_DELIVERY_BYPASS\n"
        )
    elif forgery == "arbitrary-argv":
        path = p0_root / "command_records/p0_suite.json"
        record = json.loads(path.read_bytes())
        record["command_argv"] = ["python3", "-c", "print('PASS')"]
        record.pop("command_record_sha256")
        record["command_record_sha256"] = canonical_sha256(record)
        path.write_bytes(canonical_bytes(record))
    elif forgery == "nonexistent-cwd":
        missing = str(tmp_path / "source-that-never-existed")
        environment_path = p0_root / "attestations/environment.json"
        environment = json.loads(environment_path.read_bytes())
        environment["cwd"] = missing
        environment.pop("attestation_sha256")
        environment["attestation_sha256"] = canonical_sha256(environment)
        environment_path.write_bytes(canonical_bytes(environment))
        command_path = p0_root / "command_records/p0_suite.json"
        command = json.loads(command_path.read_bytes())
        command["cwd"] = missing
        command.pop("command_record_sha256")
        command["command_record_sha256"] = canonical_sha256(command)
        command_path.write_bytes(canonical_bytes(command))
    elif forgery == "nonexistent-python":
        missing = str(tmp_path / "python-that-never-existed")
        environment_path = p0_root / "attestations/environment.json"
        environment = json.loads(environment_path.read_bytes())
        environment["python"] = dict(environment["python"])
        environment["python"]["requested_path"] = missing
        environment["python"]["resolved_path"] = missing
        environment.pop("attestation_sha256")
        environment["attestation_sha256"] = canonical_sha256(environment)
        environment_path.write_bytes(canonical_bytes(environment))
        command_path = p0_root / "command_records/p0_suite.json"
        command = json.loads(command_path.read_bytes())
        command["command_argv"][0] = missing
        command.pop("command_record_sha256")
        command["command_record_sha256"] = canonical_sha256(command)
        command_path.write_bytes(canonical_bytes(command))
    elif forgery == "nonexistent-test-node":
        command_path = p0_root / "command_records/p0_suite.json"
        command = json.loads(command_path.read_bytes())
        command["command_argv"][-1] = (
            "tests/test_phase9_run_generation.py::test_node_that_does_not_exist"
        )
        command.pop("command_record_sha256")
        command["command_record_sha256"] = canonical_sha256(command)
        command_path.write_bytes(canonical_bytes(command))
    elif forgery == "wrong-source-inventory":
        source_path = p0_root / "attestations/source_inventory.json"
        source = json.loads(source_path.read_bytes())
        inventory = source["tracked_inventory"]
        blob_entry = next(
            entry
            for entry in inventory["entries"]
            if entry["git_object_type"] == "blob"
        )
        replacement = "0" * 64
        if blob_entry["raw_bytes_sha256"] == replacement:
            replacement = "1" * 64
        blob_entry["raw_bytes_sha256"] = replacement
        wrong_inventory_sha256 = canonical_sha256(inventory)
        source["source_inventory_sha256"] = wrong_inventory_sha256
        source.pop("attestation_sha256")
        source["attestation_sha256"] = canonical_sha256(source)
        source_path.write_bytes(canonical_bytes(source))

        outcome_path = p0_root / "test_outcomes/p0_suite.json"
        outcome = json.loads(outcome_path.read_bytes())
        outcome["source_inventory_sha256"] = wrong_inventory_sha256
        outcome.pop("outcome_sha256")
        outcome["outcome_sha256"] = canonical_sha256(outcome)
        outcome_path.write_bytes(canonical_bytes(outcome))

        command_path = p0_root / "command_records/p0_suite.json"
        command = json.loads(command_path.read_bytes())
        command["source_inventory_sha256"] = wrong_inventory_sha256
        command.pop("command_record_sha256")
        command["command_record_sha256"] = canonical_sha256(command)
        command_path.write_bytes(canonical_bytes(command))
        for requirement in P0_REQUIREMENTS:
            path = p0_root / f"receipts/{requirement}.json"
            receipt = json.loads(path.read_bytes())
            receipt["source_inventory_sha256"] = wrong_inventory_sha256
            receipt.pop("receipt_sha256")
            receipt["receipt_sha256"] = canonical_sha256(receipt)
            path.write_bytes(canonical_bytes(receipt))
    elif forgery == "wrong-producer":
        name = "AR_007_DELIVERY_BYPASS"
        path = p0_root / f"receipts/{name}.json"
        receipt = json.loads(path.read_bytes())
        receipt["producer"] = dict(receipt["producer"])
        receipt["producer"]["producer_version"] = "forged"
        receipt.pop("receipt_sha256")
        receipt["receipt_sha256"] = canonical_sha256(receipt)
        path.write_bytes(canonical_bytes(receipt))
    elif forgery == "wrong-junit-node":
        path = p0_root / "test_reports/p0_suite.xml"
        path.write_bytes(
            path.read_bytes().replace(
                b'name="test_no_judge_ablation_cannot_replace_current_release"',
                b'name="test_forged_same_count"',
                1,
            )
        )
    elif forgery in {"parseable-failed-log", "setup-error-log"}:
        nodes = phase9_p0_test_nodes()
        status = "FAILED" if forgery == "parseable-failed-log" else "ERROR"
        label = "failed" if status == "FAILED" else "error"
        lines = [f"collecting ... collected {len(nodes)} items"]
        lines.extend(
            f"{node} {status if index == 0 else 'PASSED'} [ 50%]"
            for index, node in enumerate(nodes)
        )
        lines.append(
            f"================ {len(nodes) - 1} passed, 1 {label} in 0.01s ================"
        )
        (p0_root / "test_results/p0_suite.log").write_bytes(
            ("\n".join(lines) + "\n").encode()
        )
    elif forgery == "truncated-log":
        log_path = p0_root / "test_results/p0_suite.log"
        log_path.write_bytes(log_path.read_bytes()[:-1])
    elif forgery == "command-log-mismatch":
        command_path = p0_root / "command_records/p0_suite.json"
        command = json.loads(command_path.read_bytes())
        command["observed_outcomes"] = dict(command["observed_outcomes"])
        command["observed_outcomes"]["passed"] -= 1
        command.pop("command_record_sha256")
        command["command_record_sha256"] = canonical_sha256(command)
        command_path.write_bytes(canonical_bytes(command))
    elif forgery == "command-duration-mismatch":
        command_path = p0_root / "command_records/p0_suite.json"
        command = json.loads(command_path.read_bytes())
        command["duration_ns"] = 1
        command.pop("command_record_sha256")
        command["command_record_sha256"] = canonical_sha256(command)
        command_path.write_bytes(canonical_bytes(command))
    elif forgery == "command-outside-authorization-window":
        authority = json.loads(
            (p0_root / "attestations/authority_runner.json").read_bytes()
        )
        command_path = p0_root / "command_records/p0_suite.json"
        command = json.loads(command_path.read_bytes())
        command["started_at"] = authority["expires_at"] + 1
        command["finished_at"] = authority["expires_at"] + 2
        command.pop("command_record_sha256")
        command["command_record_sha256"] = canonical_sha256(command)
        command_path.write_bytes(canonical_bytes(command))
    else:
        name = "AR_007_DELIVERY_BYPASS"
        receipt = receipts[name]
        receipt["coordinate"] = dict(receipt["coordinate"])
        receipt["coordinate"]["workflow_id"] = "other-workflow"
        receipt.pop("receipt_sha256")
        receipt["receipt_sha256"] = canonical_sha256(receipt)
        (p0_root / f"receipts/{name}.json").write_bytes(canonical_bytes(receipt))

    receipts = _rehash_formal_p0_tree(p0_root)
    recomputed_root_sha = p0_evidence_root_sha256(p0_root)
    result = verify_phase9_entry_gate(
        state=state,
        source_verification={
            "mode": "GIT",
            "candidate": candidate.as_dict(),
            "verified_tree": candidate.tree,
            "source_inventory_sha256": request.source_inventory_sha256,
        },
        p0_receipts=receipts,
        p0_evidence_root=p0_root,
        p0_evidence_root_sha256=recomputed_root_sha,
        operator_authorization=_entry_authorization(
            candidate, request, recomputed_root_sha
        ),
        official_input_manifest=request.official_inputs.as_dict(),
        official_input_root=input_root,
        execution_context=request.execution_context.as_dict(),
        evaluated_at=2100,
        trusted_now=2100,
    )
    assert result["status"] == "BLOCKED"
    assert {item["code"] for item in result["blockers"]} == {
        "P0_RECEIPTS_INVALID"
    }


@pytest.mark.parametrize(
    ("field", "value", "code"),
    (
        ("active_process_count", 1, "ACTIVE_PROCESS"),
        ("pending_outbox_count", 1, "PENDING_OUTBOX"),
        ("unresolved_migration_count", 1, "UNRESOLVED_MIGRATION"),
        (
            "old_generation_post_boundary_event_count",
            1,
            "OLD_GENERATION_NOT_READ_ONLY",
        ),
        ("run_mode", "NORMAL_DELIVERY_RUN", "RUN_MODE_INVALID"),
        (
            "modeling_consultation_contract",
            "ACTIVE",
            "MODELING_CONTRACT_INVALID",
        ),
        ("delivery_capability", "ENABLED", "DELIVERY_CAPABILITY_ENABLED"),
        ("writer_enabled", True, "DELIVERY_CONTROL_NOT_QUIET"),
    ),
)
def test_gate_blocks_each_quiescence_and_delivery_fence(tmp_path, field, value, code):
    (
        _, input_root, request, candidate, state,
        p0_root, p0_root_sha, p0_receipts,
    ) = _ready_fixture(tmp_path)
    result = verify_phase9_entry_gate(
        state=replace(state, **{field: value}),
        source_verification={
            "mode": "GIT",
            "candidate": candidate.as_dict(),
            "verified_tree": candidate.tree,
            "source_inventory_sha256": request.source_inventory_sha256,
        },
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
    assert result["status"] == "BLOCKED"
    assert code in {item["code"] for item in result["blockers"]}


def test_gate_requires_ar007_and_all_other_candidate_bound_p0_receipts(tmp_path):
    (
        _, input_root, request, candidate, state,
        p0_root, p0_root_sha, receipts,
    ) = _ready_fixture(tmp_path)
    receipts = dict(receipts)
    receipts.pop("AR_007_DELIVERY_BYPASS")

    result = verify_phase9_entry_gate(
        state=state,
        source_verification={
            "mode": "GIT",
            "candidate": candidate.as_dict(),
            "verified_tree": candidate.tree,
            "source_inventory_sha256": request.source_inventory_sha256,
        },
        p0_receipts=receipts,
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

    assert result["status"] == "BLOCKED"
    assert {item["code"] for item in result["blockers"]} == {"P0_RECEIPTS_INVALID"}


def test_each_of_nine_p0_receipts_is_required_and_hash_verified(tmp_path):
    (
        _, input_root, request, candidate, state,
        p0_root, p0_root_sha, receipts,
    ) = _ready_fixture(tmp_path)
    source = {
        "mode": "GIT",
        "candidate": candidate.as_dict(),
        "verified_tree": candidate.tree,
        "source_inventory_sha256": request.source_inventory_sha256,
    }
    authorization = _entry_authorization(candidate, request, p0_root_sha)
    for requirement in P0_REQUIREMENTS:
        missing = dict(receipts)
        missing.pop(requirement)
        missing_result = verify_phase9_entry_gate(
            state=state,
            source_verification=source,
            p0_receipts=missing,
            p0_evidence_root=p0_root,
            p0_evidence_root_sha256=p0_root_sha,
            operator_authorization=authorization,
            official_input_manifest=request.official_inputs.as_dict(),
            official_input_root=input_root,
            execution_context=request.execution_context.as_dict(),
            evaluated_at=2100,
            trusted_now=2100,
        )
        assert missing_result["status"] == "BLOCKED", requirement
        tampered = copy.deepcopy(receipts)
        tampered[requirement]["evidence_sha256"] = "0" * 64
        tampered_result = verify_phase9_entry_gate(
            state=state,
            source_verification=source,
            p0_receipts=tampered,
            p0_evidence_root=p0_root,
            p0_evidence_root_sha256=p0_root_sha,
            operator_authorization=authorization,
            official_input_manifest=request.official_inputs.as_dict(),
            official_input_root=input_root,
            execution_context=request.execution_context.as_dict(),
            evaluated_at=2100,
            trusted_now=2100,
        )
        assert tampered_result["status"] == "BLOCKED", requirement
        assert {item["code"] for item in tampered_result["blockers"]} == {
            "P0_RECEIPTS_INVALID"
        }


@pytest.mark.parametrize(
    "relative_path",
    (
        "command_records/p0_suite.json",
        "test_results/p0_suite.log",
        "attestations/source_inventory.json",
    ),
)
def test_gate_rejects_recomputed_root_when_external_p0_artifact_is_missing(
    tmp_path, relative_path
):
    (
        _, input_root, request, candidate, state,
        p0_root, _, receipts,
    ) = _ready_fixture(tmp_path)
    removed = p0_root / relative_path
    removed.unlink()
    if not any(removed.parent.iterdir()):
        removed.parent.rmdir()
    recomputed_root_sha = p0_evidence_root_sha256(p0_root)
    result = verify_phase9_entry_gate(
        state=state,
        source_verification={
            "mode": "GIT",
            "candidate": candidate.as_dict(),
            "verified_tree": candidate.tree,
            "source_inventory_sha256": request.source_inventory_sha256,
        },
        p0_receipts=receipts,
        p0_evidence_root=p0_root,
        p0_evidence_root_sha256=recomputed_root_sha,
        operator_authorization=_entry_authorization(
            candidate, request, recomputed_root_sha
        ),
        official_input_manifest=request.official_inputs.as_dict(),
        official_input_root=input_root,
        execution_context=request.execution_context.as_dict(),
        evaluated_at=2100,
        trusted_now=2100,
    )
    assert result["status"] == "BLOCKED"
    assert "P0_RECEIPTS_INVALID" in {
        item["code"] for item in result["blockers"]
    }


def test_gate_rejects_recomputed_receipt_hashes_without_any_p0_evidence_root(
    tmp_path,
):
    (
        _, input_root, request, candidate, state,
        _, _, receipts,
    ) = _ready_fixture(tmp_path)
    forged = copy.deepcopy(receipts)
    for requirement, receipt in forged.items():
        receipt["evidence_domain"] = "TEST_FIXTURE"
        receipt.pop("receipt_sha256")
        receipt["receipt_sha256"] = canonical_sha256(receipt)
    missing_root = tmp_path / "nonexistent-p0-evidence"
    forged_root_sha = "a" * 64
    result = verify_phase9_entry_gate(
        state=state,
        source_verification={
            "mode": "GIT",
            "candidate": candidate.as_dict(),
            "verified_tree": candidate.tree,
            "source_inventory_sha256": request.source_inventory_sha256,
        },
        p0_receipts=forged,
        p0_evidence_root=missing_root,
        p0_evidence_root_sha256=forged_root_sha,
        operator_authorization=_entry_authorization(
            candidate, request, forged_root_sha
        ),
        official_input_manifest=request.official_inputs.as_dict(),
        official_input_root=input_root,
        execution_context=request.execution_context.as_dict(),
        evaluated_at=2100,
        trusted_now=2100,
    )
    assert result["status"] == "BLOCKED"
    assert "P0 evidence root is unavailable" in next(
        item["detail"]
        for item in result["blockers"]
        if item["code"] == "P0_RECEIPTS_INVALID"
    )


def test_gate_rejects_cross_candidate_receipt_expired_auth_and_changed_input(tmp_path):
    (
        _, input_root, request, candidate, state,
        p0_root, p0_root_sha, receipts,
    ) = _ready_fixture(tmp_path)
    receipts = dict(receipts)
    receipts["AR_007_DELIVERY_BYPASS"] = copy.deepcopy(
        receipts["AR_007_DELIVERY_BYPASS"]
    )
    receipts["AR_007_DELIVERY_BYPASS"]["candidate"] = {
        "commit": "a" * 40,
        "tree": "b" * 40,
        "parent": "c" * 40,
    }
    receipts["AR_007_DELIVERY_BYPASS"].pop("receipt_sha256")
    receipts["AR_007_DELIVERY_BYPASS"]["receipt_sha256"] = canonical_sha256(
        receipts["AR_007_DELIVERY_BYPASS"]
    )
    (input_root / "official" / "problem.pdf").write_bytes(b"changed")

    result = verify_phase9_entry_gate(
        state=state,
        source_verification={
            "mode": "GIT",
            "candidate": candidate.as_dict(),
            "verified_tree": candidate.tree,
            "source_inventory_sha256": request.source_inventory_sha256,
        },
        p0_receipts=receipts,
        p0_evidence_root=p0_root,
        p0_evidence_root_sha256=p0_root_sha,
        operator_authorization=_entry_authorization(
            candidate, request, p0_root_sha
        ),
        official_input_manifest=request.official_inputs.as_dict(),
        official_input_root=input_root,
        execution_context=request.execution_context.as_dict(),
        evaluated_at=2600,
        trusted_now=2600,
    )

    codes = {item["code"] for item in result["blockers"]}
    assert codes == {
        "P0_RECEIPTS_INVALID",
        "OPERATOR_AUTHORIZATION_INVALID",
        "OFFICIAL_INPUT_INVALID",
    }


@pytest.mark.parametrize(
    ("authorization_changes", "requested_time", "trusted_now", "expected_code"),
    (
        ({"issued_at": 1900, "expires_at": 2000}, 1950, 2100,
         "OPERATOR_AUTHORIZATION_INVALID"),
        ({"issued_at": 2200, "expires_at": 2500}, 2100, 2100,
         "OPERATOR_AUTHORIZATION_INVALID"),
        ({}, 1700, 2100, "REQUEST_TIME_INVALID"),
    ),
)
def test_gate_authorization_uses_trusted_clock_not_request_time(
    tmp_path,
    authorization_changes,
    requested_time,
    trusted_now,
    expected_code,
):
    (
        _, input_root, request, candidate, state,
        p0_root, p0_root_sha, p0_receipts,
    ) = _ready_fixture(tmp_path)
    authorization = _entry_authorization(candidate, request, p0_root_sha)
    authorization.update(authorization_changes)
    authorization.pop("receipt_sha256")
    authorization["receipt_sha256"] = canonical_sha256(authorization)
    result = verify_phase9_entry_gate(
        state=state,
        source_verification={
            "mode": "GIT",
            "candidate": candidate.as_dict(),
            "verified_tree": candidate.tree,
            "source_inventory_sha256": request.source_inventory_sha256,
        },
        p0_receipts=p0_receipts,
        p0_evidence_root=p0_root,
        p0_evidence_root_sha256=p0_root_sha,
        operator_authorization=authorization,
        official_input_manifest=request.official_inputs.as_dict(),
        official_input_root=input_root,
        execution_context=request.execution_context.as_dict(),
        evaluated_at=requested_time,
        trusted_now=trusted_now,
    )
    assert result["status"] == "BLOCKED"
    assert expected_code in {item["code"] for item in result["blockers"]}
    assert result["evaluated_at"] == trusted_now
    assert result["request_evaluated_at"] == requested_time


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repository, check=True, stdout=subprocess.PIPE, text=True
    ).stdout.strip()


def _write_candidate_metadata(
    fresh: Path,
    *,
    candidate: CandidateIdentity,
    payload: dict[str, tuple[bytes, int]],
) -> None:
    archive_root = "candidate"
    files = [
        {
            "archive_path": f"{archive_root}/{path}",
            "mode": mode,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
            "source_path": path,
        }
        for path, (raw, mode) in sorted(payload.items())
    ]
    paths_raw = "".join(f"{path}\n" for path in sorted(payload)).encode()
    manifest = {
        "archive_root": archive_root,
        "builder": "paper-factory-deterministic-zip-v1",
        "closure": {
            "checksums": f"{archive_root}/checksums/SHA256SUMS",
            "checksums_cover": "every payload member plus MANIFEST.json",
            "checksums_exclude": "checksums/SHA256SUMS (self-reference is forbidden)",
            "manifest": f"{archive_root}/MANIFEST.json",
        },
        "deterministic_timestamp": "1980-01-01T00:00:00Z",
        "files": files,
        "inventory_sha256": hashlib.sha256(paths_raw).hexdigest(),
        "metadata": {
            "authorization_scope": {
                "cutover": False,
                "delivery": False,
                "deployment": False,
                "migration": False,
                "phase9_a_forensic_replay": False,
                "production_outbox": False,
                "provider_or_network": False,
                "release": False,
            },
            "candidate_commit": candidate.commit,
            "candidate_parent": candidate.parent,
            "candidate_tree": candidate.tree,
            "freeze_utc": "2026-01-01T00:00:00Z",
            "purpose": "candidate binding test",
            "schema": "phase1-8-phase9-candidate-build-metadata-v1",
            "shadow_only": True,
        },
        "schema": "paper-factory-full-shadow-candidate-manifest-v2",
    }
    manifest_raw = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    (fresh / "MANIFEST.json").write_bytes(manifest_raw)
    (fresh / "checksums").mkdir()
    checksum_values = {
        row["archive_path"]: row["sha256"]
        for row in files
    }
    checksum_values[f"{archive_root}/MANIFEST.json"] = hashlib.sha256(
        manifest_raw
    ).hexdigest()
    (fresh / "checksums" / "SHA256SUMS").write_text(
        "".join(
            f"{digest}  {path}\n"
            for path, digest in sorted(checksum_values.items())
        ),
        encoding="utf-8",
    )


def test_source_verifier_proves_git_and_no_git_fresh_tree(tmp_path):
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "entry@example.invalid")
    _git(repository, "config", "user.name", "Phase9 Entry")
    (repository / "a.txt").write_bytes(b"first\n")
    _git(repository, "add", "a.txt")
    _git(repository, "commit", "-qm", "parent")
    (repository / "a.txt").write_bytes(b"second\n")
    _git(repository, "commit", "-qam", "candidate")
    candidate = CandidateIdentity(
        _git(repository, "rev-parse", "HEAD^{commit}"),
        _git(repository, "rev-parse", "HEAD^{tree}"),
        _git(repository, "rev-parse", "HEAD^"),
    )

    live = verify_candidate_source(repository, candidate=candidate)
    assert live["mode"] == "GIT"

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    raw = b"second\n"
    (fresh / "a.txt").write_bytes(raw)
    inventory = tmp_path / "inventory.tsv"
    inventory.write_text(
        "path\tsize\tmode\tsha256\n"
        f"a.txt\t{len(raw)}\t0644\t{hashlib.sha256(raw).hexdigest()}\n",
        encoding="utf-8",
    )
    no_git = verify_candidate_source(
        fresh, candidate=candidate, inventory=inventory
    )
    assert no_git["mode"] == "FRESH_INVENTORY"
    assert no_git["verified_tree"] == candidate.tree
    assert no_git["candidate_metadata_present"] is False
    assert no_git["source_inventory_sha256"] == live["source_inventory_sha256"]

    _write_candidate_metadata(
        fresh,
        candidate=candidate,
        payload={"a.txt": (raw, 0o644)},
    )
    extracted = verify_candidate_source(
        fresh, candidate=candidate, inventory=inventory
    )
    assert extracted["candidate_metadata_present"] is True
    assert extracted["candidate_metadata"]["binding"] == (
        "CANONICAL_MANIFEST_AND_EXACT_PAYLOAD_CHECKSUMS"
    )

    (fresh / "extra.txt").write_bytes(b"not frozen")
    with pytest.raises(Phase9EntryError, match="files differ"):
        verify_candidate_source(fresh, candidate=candidate, inventory=inventory)


def test_source_verifier_binds_filtered_payload_through_candidate_metadata(tmp_path):
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "entry@example.invalid")
    _git(repository, "config", "user.name", "Phase9 Entry")
    (repository / "a.txt").write_bytes(b"first\n")
    (repository / "excluded.lock").write_bytes(b"runtime state\n")
    _git(repository, "add", "a.txt", "excluded.lock")
    _git(repository, "commit", "-qm", "parent")
    raw = b"second\n"
    (repository / "a.txt").write_bytes(raw)
    _git(repository, "commit", "-qam", "candidate")
    candidate = CandidateIdentity(
        _git(repository, "rev-parse", "HEAD^{commit}"),
        _git(repository, "rev-parse", "HEAD^{tree}"),
        _git(repository, "rev-parse", "HEAD^"),
    )
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    (fresh / "a.txt").write_bytes(raw)
    inventory = tmp_path / "inventory.tsv"
    inventory.write_text(
        "path\tsize\tmode\tsha256\n"
        f"a.txt\t{len(raw)}\t0644\t{hashlib.sha256(raw).hexdigest()}\n",
        encoding="utf-8",
    )

    with pytest.raises(Phase9EntryError, match="Git tree differs"):
        verify_candidate_source(fresh, candidate=candidate, inventory=inventory)

    _write_candidate_metadata(
        fresh,
        candidate=candidate,
        payload={"a.txt": (raw, 0o644)},
    )
    verified = verify_candidate_source(
        fresh, candidate=candidate, inventory=inventory
    )
    assert verified["verified_tree"] == candidate.tree
    assert verified["payload_tree"] != candidate.tree
    assert verified["candidate_metadata_present"] is True


def test_receipt_files_must_be_canonical_json(tmp_path):
    value = {"b": 2, "a": 1}
    path = tmp_path / "noncanonical.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    from factory_core.phase9_entry import read_canonical_json_file

    with pytest.raises(Phase9EntryError, match="not canonical"):
        read_canonical_json_file(path)
    path.write_bytes(canonical_bytes(value))
    assert read_canonical_json_file(path) == {"a": 1, "b": 2}


@pytest.mark.parametrize("kind", ("fifo", "socket"))
def test_gate_official_input_snapshot_rejects_special_members(tmp_path, kind):
    root = tmp_path / "official-input"
    request = _creation_request(root)
    special = root / f"unexpected-{kind}"
    if kind == "fifo":
        os.mkfifo(special)
        bound = None
    else:
        bound = socket.socket(socket.AF_UNIX)
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        bound.bind(f"/proc/self/fd/{directory_fd}/{special.name}")
    try:
        with pytest.raises(Phase9EntryError, match="special file"):
            verify_official_input_manifest(
                request.official_inputs.as_dict(), input_root=root
            )
    finally:
        if bound is not None:
            bound.close()
            os.close(directory_fd)


def test_gate_official_input_snapshot_rejects_symlink_root_and_directory(tmp_path):
    real_root = tmp_path / "real-official-input"
    request = _creation_request(real_root)
    linked_root = tmp_path / "linked-official-input"
    linked_root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(Phase9EntryError, match="non-symlink directory"):
        verify_official_input_manifest(
            request.official_inputs.as_dict(), input_root=linked_root
        )

    outside = tmp_path / "outside-directory"
    outside.mkdir()
    (outside / "problem.pdf").write_bytes(b"official-input-bytes\n")
    (real_root / "official" / "problem.pdf").unlink()
    (real_root / "official").rmdir()
    (real_root / "official").symlink_to(outside, target_is_directory=True)
    with pytest.raises(Phase9EntryError, match="symlink.*special"):
        verify_official_input_manifest(
            request.official_inputs.as_dict(), input_root=real_root
        )


def test_gate_official_input_snapshot_rejects_hardlink(tmp_path):
    root = tmp_path / "official-input"
    request = _creation_request(root)
    official = root / "official" / "problem.pdf"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(official.read_bytes())
    official.unlink()
    os.link(outside, official)
    with pytest.raises(Phase9EntryError, match="hardlink"):
        verify_official_input_manifest(
            request.official_inputs.as_dict(), input_root=root
        )


def test_gate_official_input_snapshot_rejects_root_replacement(tmp_path, monkeypatch):
    import factory_core.phase9_run_generation as run_generation

    root = tmp_path / "official-input"
    request = _creation_request(root)
    original = run_generation._StableDirectoryTree.read_regular_file
    replaced = False

    def replace_after_read(tree, directory_parts, filename, *, maximum_bytes):
        nonlocal replaced
        result = original(
            tree, directory_parts, filename, maximum_bytes=maximum_bytes
        )
        if not replaced and tree.label.startswith("official input"):
            replaced = True
            old = tmp_path / "official-input-original"
            root.rename(old)
            (root / "official").mkdir(parents=True)
            (root / "official" / "problem.pdf").write_bytes(result[0])
        return result

    monkeypatch.setattr(
        run_generation._StableDirectoryTree,
        "read_regular_file",
        replace_after_read,
    )
    with pytest.raises(Phase9EntryError, match="root.*changed"):
        verify_official_input_manifest(
            request.official_inputs.as_dict(), input_root=root
        )


def test_p0_inventory_rejects_root_link_hardlink_and_special_member(tmp_path):
    real = tmp_path / "real-p0"
    real.mkdir()
    (real / "receipt.json").write_bytes(b"{}")
    linked = tmp_path / "linked-p0"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(Phase9EntryError, match="non-symlink directory"):
        p0_evidence_root_sha256(linked)

    outside = tmp_path / "outside"
    outside.write_bytes(b"same")
    hardlink_root = tmp_path / "hardlink-p0"
    hardlink_root.mkdir()
    os.link(outside, hardlink_root / "receipt.json")
    with pytest.raises(Phase9EntryError, match="hardlink"):
        p0_evidence_root_sha256(hardlink_root)

    special_root = tmp_path / "special-p0"
    special_root.mkdir()
    os.mkfifo(special_root / "receipt.fifo")
    with pytest.raises(Phase9EntryError, match="special"):
        p0_evidence_root_sha256(special_root)


def test_p0_inventory_rejects_extra_empty_directory(tmp_path):
    root = tmp_path / "p0"
    (root / "receipts").mkdir(parents=True)
    (root / "receipts" / "one.json").write_bytes(b"{}")
    (root / "unexpected-empty").mkdir()

    with pytest.raises(Phase9EntryError, match="unexpected empty directory"):
        p0_evidence_root_sha256(root)


def test_p0_inventory_fails_closed_on_enumeration_error(tmp_path, monkeypatch):
    import factory_core.phase9_run_generation as run_generation

    root = tmp_path / "p0"
    root.mkdir()
    (root / "one.json").write_bytes(b"{}")

    def deny_enumeration(_descriptor):
        raise PermissionError("injected enumeration denial")

    monkeypatch.setattr(run_generation.os, "listdir", deny_enumeration)
    with pytest.raises(Phase9EntryError, match="cannot be enumerated"):
        p0_evidence_root_sha256(root)


def test_gate_rejects_fully_rehashed_extra_p0_file(tmp_path):
    (
        _, input_root, request, candidate, state,
        p0_root, _, receipts,
    ) = _ready_fixture(tmp_path)
    (p0_root / "unexpected.json").write_bytes(b"{}")
    root_sha = p0_evidence_root_sha256(p0_root)

    result = verify_phase9_entry_gate(
        state=state,
        source_verification={
            "mode": "GIT",
            "candidate": candidate.as_dict(),
            "verified_tree": candidate.tree,
            "source_inventory_sha256": request.source_inventory_sha256,
        },
        p0_receipts=receipts,
        p0_evidence_root=p0_root,
        p0_evidence_root_sha256=root_sha,
        operator_authorization=_entry_authorization(candidate, request, root_sha),
        official_input_manifest=request.official_inputs.as_dict(),
        official_input_root=input_root,
        execution_context=request.execution_context.as_dict(),
        evaluated_at=2100,
        trusted_now=2100,
    )

    assert result["status"] == "BLOCKED"
    assert {item["code"] for item in result["blockers"]} == {
        "P0_RECEIPTS_INVALID"
    }


def test_p0_inventory_rejects_root_replacement_while_reading(tmp_path, monkeypatch):
    import factory_core.phase9_run_generation as run_generation

    root = tmp_path / "p0"
    root.mkdir()
    (root / "receipt.json").write_bytes(b"{}")
    original = run_generation._StableDirectoryTree.read_regular_file
    replaced = False

    def replace_after_read(tree, parent_parts, name, *, maximum_bytes):
        nonlocal replaced
        result = original(
            tree, parent_parts, name, maximum_bytes=maximum_bytes
        )
        if not replaced and tree.label == "P0 evidence root":
            replaced = True
            root.rename(tmp_path / "p0-original")
            root.mkdir()
            (root / "receipt.json").write_bytes(result[0])
        return result

    monkeypatch.setattr(
        run_generation._StableDirectoryTree,
        "read_regular_file",
        replace_after_read,
    )
    with pytest.raises(Phase9EntryError, match="root.*changed"):
        p0_evidence_root_sha256(root)


def test_p0_inventory_rejects_same_name_member_replacement(tmp_path, monkeypatch):
    import factory_core.phase9_run_generation as run_generation

    root = tmp_path / "p0"
    root.mkdir()
    member = root / "receipt.json"
    member.write_bytes(b"{}")
    original = run_generation._StableDirectoryTree.read_regular_file
    replaced = False

    def replace_after_read(tree, parent_parts, name, *, maximum_bytes):
        nonlocal replaced
        result = original(
            tree, parent_parts, name, maximum_bytes=maximum_bytes
        )
        if not replaced and tree.label == "P0 evidence root":
            replaced = True
            member.rename(root / "receipt.old")
            member.write_bytes(result[0])
        return result

    monkeypatch.setattr(
        run_generation._StableDirectoryTree,
        "read_regular_file",
        replace_after_read,
    )
    with pytest.raises(Phase9EntryError, match="changed"):
        p0_evidence_root_sha256(root)


def test_entry_cli_is_default_off_and_does_not_create_missing_database(tmp_path):
    source = read_current_git_source_identity(_source_repository())
    database = tmp_path / "must-not-be-created.db"
    request = {
        "schema": "phase9-entry-state-collection-request-v1",
        "candidate": _candidate(source).as_dict(),
        "authority_database": str(database),
        "workflow_id": "legacy_current",
    }
    request_path = tmp_path / "request.json"
    request_path.write_bytes(canonical_bytes(request))

    completed = subprocess.run(
        [
            "python3",
            "-m",
            "scripts.phase9_entry_gate",
            "collect",
            "--request",
            str(request_path),
        ],
        cwd=EXECUTION_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 2
    result = json.loads(completed.stdout)
    assert result["status"] == "BLOCKED"
    assert result["blockers"][0]["code"] == "STATE_COLLECTION_FAILED"
    assert not database.exists()


def test_blocked_result_preserves_only_prevalidated_source_and_nine_p0_hashes():
    source = read_current_git_source_identity(_source_repository())
    candidate = _candidate(source)
    p0_hashes = {
        name: hashlib.sha256(name.encode("ascii")).hexdigest()
        for name in P0_REQUIREMENTS
    }
    result = blocked_phase9_entry_result(
        candidate=candidate,
        evaluated_at=2200,
        error=RuntimeError("formal runtime inputs absent"),
        source_verification_sha256="a" * 64,
        p0_receipt_sha256s=p0_hashes,
    )
    assert result["status"] == "BLOCKED"
    assert result["source_inventory_sha256"] is None
    assert result["source_verification_sha256"] == "a" * 64
    assert result["p0_receipt_sha256s"] == p0_hashes
    assert all(value is False for value in result["authorization_scope"].values())
