from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess

import pytest

from factory_core.canonical import canonical_bytes, canonical_sha256
from factory_core.contract_pins import compile_contract_pin_set
from factory_core.phase9_entry import (
    PHASE9_EXECUTION_CONTEXT_SCHEMA,
    PHASE9_OFFICIAL_INPUT_FILE_SCHEMA,
    PHASE9_OFFICIAL_INPUT_MANIFEST_SCHEMA,
    PHASE9_OPERATOR_AUTHORIZATION_SCHEMA,
    PHASE9_P0_RECEIPT_SCHEMA,
    P0_REQUIREMENTS,
    CandidateIdentity,
    Phase9EntryError,
    blocked_phase9_entry_result,
    collect_phase9_entry_state,
    verify_candidate_source,
    verify_phase9_entry_gate,
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
)
from factory_core.workflow_contract_v2 import compile_workflow_contract_bundle_v2
from tests.support.authority_production import install_foundation


EXECUTION_ROOT = Path(__file__).resolve().parents[1]


def _source_repository() -> Path:
    """Use the exact Git identity root for tests run from a no-.git extraction."""

    raw = os.environ.get("PHASE9_TEST_SOURCE_REPOSITORY")
    if raw is None:
        return EXECUTION_ROOT
    path = Path(raw)
    if not path.is_absolute():
        raise AssertionError("PHASE9_TEST_SOURCE_REPOSITORY must be absolute")
    return path


def _creation_request(input_root: Path) -> RunGenerationRequestV1:
    raw = b"official-input-bytes\n"
    path = input_root / "official"
    path.mkdir(parents=True)
    (path / "problem.pdf").write_bytes(raw)
    source = read_current_git_source_identity(_source_repository())
    request = RunGenerationRequestV1(
        RUN_GENERATION_REQUEST_SCHEMA,
        "phase9-entry-generation-key",
        CREATE,
        "demo",
        "legacy_current",
        1,
        "project-generation-phase9-entry",
        "native_v2",
        "stage_v1",
        None,
        None,
        "FORENSIC_REPLAY",
        "LEGACY_NOT_APPLICABLE",
        DELIVERY_DISABLED,
        source,
        compile_contract_pin_set(compile_workflow_contract_bundle_v2()),
        OfficialInputManifestEvidenceV1(
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
        ExecutionContextEvidenceV1(
            EXECUTION_CONTEXT_EVIDENCE_SCHEMA,
            "phase9-entry-context",
            "3" * 64,
            "4" * 64,
            "5" * 64,
            1950,
        ),
        OperatorAuthorizationEvidenceV1(
            OPERATOR_AUTHORIZATION_EVIDENCE_SCHEMA,
            "phase9-generation-authorization",
            "CONTROLLED_OS_ACCOUNT",
            "7" * 64,
            True,
            os.geteuid(),
            pwd.getpwuid(os.geteuid()).pw_name,
            "controlled-authorizer",
            "controlled-operator",
            CREATE,
            "demo",
            "legacy_current",
            source.source_commit,
            1900,
            3000,
            "6" * 64,
        ),
        2000,
    )
    return replace(
        request,
        project_generation=request.derived_project_generation,
    )


def _candidate(source: GitSourceIdentityV1) -> CandidateIdentity:
    return CandidateIdentity(source.source_commit, source.source_tree, source.source_parent)


def _p0_receipts(candidate: CandidateIdentity) -> dict[str, object]:
    receipts: dict[str, object] = {}
    for index, requirement in enumerate(P0_REQUIREMENTS, start=1):
        evidence = [
            {
                "path": f"evidence/{requirement}.json",
                "sha256": hashlib.sha256(
                    f"evidence:{requirement}".encode("ascii")
                ).hexdigest(),
            }
        ]
        body = {
            "schema": PHASE9_P0_RECEIPT_SCHEMA,
            "requirement": requirement,
            "candidate": candidate.as_dict(),
            "status": "PASS",
            "test_result_sha256": hashlib.sha256(
                f"test:{requirement}".encode("ascii")
            ).hexdigest(),
            "command_record_sha256": hashlib.sha256(
                f"command:{requirement}".encode("ascii")
            ).hexdigest(),
            "command_exit_code": 0,
            "evidence": evidence,
            "evidence_sha256": canonical_sha256(evidence),
            "capabilities": {
                "network_access": False,
                "provider_call": False,
                "outbox_dispatch": False,
                "delivery": False,
                "release": False,
                "migration": False,
                "deployment": False,
                "cutover": False,
            },
        }
        body["receipt_sha256"] = canonical_sha256(body)
        receipts[requirement] = body
    return receipts


def _entry_authorization(
    candidate: CandidateIdentity, request: RunGenerationRequestV1
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
        "operator_account": pwd.getpwuid(os.geteuid()).pw_name,
        "operator_uid": os.geteuid(),
        "authorized": True,
        "expires_at": 2500,
    }
    body["receipt_sha256"] = canonical_sha256(body)
    return body


def _ready_fixture(tmp_path: Path):
    fixture = install_foundation(tmp_path)
    input_root = tmp_path / "official-inputs"
    request = _creation_request(input_root)
    context_receipt = tmp_path / "execution-context.json"
    context_receipt.write_bytes(canonical_bytes(request.execution_context.as_dict()))
    result = Phase9RunGenerationService(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        source_repository=_source_repository(),
        official_input_root=input_root,
        execution_context_receipt_path=context_receipt,
    ).create_or_rotate(request)
    candidate = _candidate(request.source)
    state = collect_phase9_entry_state(
        fixture.database, workflow_id=request.workflow_id, candidate=candidate
    )
    assert state.creation_receipt_sha256 == result.receipt_sha256
    return fixture, input_root, request, candidate, state


def test_read_only_collector_and_candidate_bound_gate_are_ready(tmp_path):
    fixture, input_root, request, candidate, state = _ready_fixture(tmp_path)
    before = hashlib.sha256(fixture.database.read_bytes()).hexdigest()
    source = {
        "mode": "GIT",
        "candidate": candidate.as_dict(),
        "verified_tree": candidate.tree,
        "worktree_clean": True,
    }

    result = verify_phase9_entry_gate(
        state=state,
        source_verification=source,
        p0_receipts=_p0_receipts(candidate),
        operator_authorization=_entry_authorization(candidate, request),
        official_input_manifest=request.official_inputs.as_dict(),
        official_input_root=input_root,
        execution_context=request.execution_context.as_dict(),
        evaluated_at=2100,
    )

    assert result["status"] == "READY"
    assert result["blockers"] == []
    assert set(result["p0_receipt_sha256s"]) == set(P0_REQUIREMENTS)
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
        ("delivery_capability", "ENABLED", "DELIVERY_CAPABILITY_ENABLED"),
        ("writer_enabled", True, "DELIVERY_CONTROL_NOT_QUIET"),
    ),
)
def test_gate_blocks_each_quiescence_and_delivery_fence(tmp_path, field, value, code):
    _, input_root, request, candidate, state = _ready_fixture(tmp_path)
    result = verify_phase9_entry_gate(
        state=replace(state, **{field: value}),
        source_verification={
            "mode": "GIT",
            "candidate": candidate.as_dict(),
            "verified_tree": candidate.tree,
        },
        p0_receipts=_p0_receipts(candidate),
        operator_authorization=_entry_authorization(candidate, request),
        official_input_manifest=request.official_inputs.as_dict(),
        official_input_root=input_root,
        execution_context=request.execution_context.as_dict(),
        evaluated_at=2100,
    )
    assert result["status"] == "BLOCKED"
    assert code in {item["code"] for item in result["blockers"]}


def test_gate_requires_ar007_and_all_other_candidate_bound_p0_receipts(tmp_path):
    _, input_root, request, candidate, state = _ready_fixture(tmp_path)
    receipts = _p0_receipts(candidate)
    receipts.pop("AR_007_DELIVERY_BYPASS")

    result = verify_phase9_entry_gate(
        state=state,
        source_verification={
            "mode": "GIT",
            "candidate": candidate.as_dict(),
            "verified_tree": candidate.tree,
        },
        p0_receipts=receipts,
        operator_authorization=_entry_authorization(candidate, request),
        official_input_manifest=request.official_inputs.as_dict(),
        official_input_root=input_root,
        execution_context=request.execution_context.as_dict(),
        evaluated_at=2100,
    )

    assert result["status"] == "BLOCKED"
    assert {item["code"] for item in result["blockers"]} == {"P0_RECEIPTS_INVALID"}


def test_each_of_nine_p0_receipts_is_required_and_hash_verified(tmp_path):
    _, input_root, request, candidate, state = _ready_fixture(tmp_path)
    source = {
        "mode": "GIT",
        "candidate": candidate.as_dict(),
        "verified_tree": candidate.tree,
    }
    authorization = _entry_authorization(candidate, request)
    for requirement in P0_REQUIREMENTS:
        missing = _p0_receipts(candidate)
        missing.pop(requirement)
        missing_result = verify_phase9_entry_gate(
            state=state,
            source_verification=source,
            p0_receipts=missing,
            operator_authorization=authorization,
            official_input_manifest=request.official_inputs.as_dict(),
            official_input_root=input_root,
            execution_context=request.execution_context.as_dict(),
            evaluated_at=2100,
        )
        assert missing_result["status"] == "BLOCKED", requirement
        tampered = _p0_receipts(candidate)
        tampered[requirement]["evidence_sha256"] = "0" * 64
        tampered_result = verify_phase9_entry_gate(
            state=state,
            source_verification=source,
            p0_receipts=tampered,
            operator_authorization=authorization,
            official_input_manifest=request.official_inputs.as_dict(),
            official_input_root=input_root,
            execution_context=request.execution_context.as_dict(),
            evaluated_at=2100,
        )
        assert tampered_result["status"] == "BLOCKED", requirement
        assert {item["code"] for item in tampered_result["blockers"]} == {
            "P0_RECEIPTS_INVALID"
        }


def test_gate_rejects_cross_candidate_receipt_expired_auth_and_changed_input(tmp_path):
    _, input_root, request, candidate, state = _ready_fixture(tmp_path)
    receipts = _p0_receipts(candidate)
    wrong = CandidateIdentity("a" * 40, "b" * 40, "c" * 40)
    receipts["AR_007_DELIVERY_BYPASS"] = _p0_receipts(wrong)[
        "AR_007_DELIVERY_BYPASS"
    ]
    (input_root / "official" / "problem.pdf").write_bytes(b"changed")

    result = verify_phase9_entry_gate(
        state=state,
        source_verification={
            "mode": "GIT",
            "candidate": candidate.as_dict(),
            "verified_tree": candidate.tree,
        },
        p0_receipts=receipts,
        operator_authorization=_entry_authorization(candidate, request),
        official_input_manifest=request.official_inputs.as_dict(),
        official_input_root=input_root,
        execution_context=request.execution_context.as_dict(),
        evaluated_at=2600,
    )

    codes = {item["code"] for item in result["blockers"]}
    assert codes == {
        "P0_RECEIPTS_INVALID",
        "OPERATOR_AUTHORIZATION_INVALID",
        "OFFICIAL_INPUT_INVALID",
    }


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
    receipts = _p0_receipts(candidate)
    p0_hashes = {
        name: receipt["receipt_sha256"] for name, receipt in receipts.items()
    }
    result = blocked_phase9_entry_result(
        candidate=candidate,
        evaluated_at=2200,
        error=RuntimeError("formal runtime inputs absent"),
        source_verification_sha256="a" * 64,
        p0_receipt_sha256s=p0_hashes,
    )
    assert result["status"] == "BLOCKED"
    assert result["source_verification_sha256"] == "a" * 64
    assert result["p0_receipt_sha256s"] == p0_hashes
    assert all(value is False for value in result["authorization_scope"].values())
