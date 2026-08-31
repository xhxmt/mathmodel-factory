from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import threading

import pytest

from factory_core.authority_read_repository import (
    AuthorityPhase3ArtifactState,
    authority_phase3_artifact_state_from_dict,
    validate_authority_phase3_artifact_state,
)
from factory_core.canonical import canonical_sha256
from factory_core.phase3_artifacts import (
    artifact_occurrence_from_dict,
    build_artifact_occurrence,
)
from factory_core.phase6_snapshot_grants import (
    GrantScope,
    Phase6SnapshotGrantStore,
    SectionAvailability,
    VerifiedSection,
    build_authority_source_binding,
)
import factory_core.phase7_grounding_runtime as phase7
from factory_core.phase78_deadline import (
    Phase78CancellationError,
    Phase78CancellationReason,
    Phase78DeadlineError,
    Phase78OutcomeUncertain,
)
from factory_core.phase8_evidence_egress_runtime import (
    build_phase8_publication_identity,
    Phase8CurrentConflict,
    Phase8Disabled,
    Phase8DeadlineExceeded,
    Phase8EvidenceEgressRunner,
    Phase8EvidenceEgressStore,
    Phase8NotFound,
    Phase8ReplayConflict,
    Phase8SchemaIncompatible,
)
from factory_core.reference_materializer import (
    ReferenceMaterializerConfig,
    materialize_reference_pdf,
)
from test_phase8_reference_materializer import make_pdf, phase3_pdf_occurrence


def _h(character: str) -> str:
    return character * 64


def _source_binding(state_sha256: str, head_revision: int = 1):
    coordinate = {
        "schema": "authority-workflow-coordinate-v1",
        "workflow_id": "workflow-phase8",
        "project_id": "project-phase8",
        "project_generation": "project-generation-1",
        "run_generation": "run-generation-1",
        "runtime_generation": "runtime-generation-1",
        "scheduler_generation": "scheduler-generation-1",
        "current_revision": head_revision,
        "contract_pin_set_sha256": _h("a"),
        "authority_state": "active",
        "source_fence_sha256": _h("b"),
        "switch_mode": "shadow",
        "switch_epoch": 1,
    }
    source_coordinate = {
        "schema_version": "snapshot-coordinate-v0",
        "project_id": "project-phase8",
        "workflow_schema_version": 1,
        "project_revision": head_revision,
        "project_generation": "project-generation-1",
        "run_generation": "run-generation-1",
        "runtime_generation": "runtime-generation-1",
        "scheduler_generation": "scheduler-generation-1",
        "recorded_contract_pin_set_sha256": _h("a"),
    }
    return build_authority_source_binding(
        authority_coordinate=coordinate,
        authority_coordinate_sha256=canonical_sha256(coordinate),
        authority_revision_snapshot_sha256=hashlib.sha256(
            f"revision-{head_revision}".encode()
        ).hexdigest(),
        authority_revision_through_revision=head_revision,
        source_snapshot_schema="project-snapshot-v0-source-authorized-v3",
        source_snapshot_semantic_sha256=hashlib.sha256(b"snapshot-1").hexdigest(),
        source_snapshot_completeness="COMPLETE",
        source_snapshot_coordinate=source_coordinate,
        phase3_artifact_state_sha256=state_sha256,
        phase4_operation_state_sha256=_h("d"),
        phase5_supervisor_state_sha256=_h("e"),
    )


def _phase6_proof(tmp_path: Path, state):
    store = Phase6SnapshotGrantStore(tmp_path / "phase6.db")
    store.initialize()
    snapshot = store.append_snapshot(
        source_binding=_source_binding(state.state_sha256, state.through_revision),
        sections=(
            VerifiedSection(
                "reference",
                SectionAvailability.AVAILABLE,
                _h("1"),
                "reference-section-v1",
            ),
        ),
        captured_at=10,
        valid_until=1000,
        expected_previous_snapshot_id=None,
        idempotency_key="phase6-snapshot-1",
    ).snapshot
    grant = store.issue_grant(
        snapshot_id=snapshot.snapshot_id,
        subject_type="user",
        subject_id="alice",
        subject_generation="membership-generation-1",
        scope=GrantScope.SNAPSHOT_VIEW,
        scope_key=None,
        issuer_id="phase6-shadow-issuer",
        issuer_generation="issuer-generation-1",
        issuer_evidence_schema="issuer-receipt-v1",
        issuer_receipt_sha256=_h("2"),
        issued_at=11,
        not_before=12,
        expires_at=900,
        expected_previous_grant_id=None,
        idempotency_key="phase6-grant-1",
    ).grant
    evaluated = store.evaluate_grant(
        grant.grant_id,
        subject_type="user",
        subject_id="alice",
        subject_generation="membership-generation-1",
        requested_scope=GrantScope.SNAPSHOT_VIEW,
        requested_scope_key=None,
        evaluated_at=12,
        idempotency_key="phase6-evaluate-1",
    )
    assert evaluated.access_proof is not None
    return store, evaluated.access_proof


def _packets():
    roles: dict[str, bytes] = {}
    manifests: dict[str, bytes] = {}
    contexts: dict[str, bytes] = {}
    for role in phase7.ROLE_ORDER:
        context = b""
        manifest = {
            "role": role,
            "files": [],
            "context": {"sha256": hashlib.sha256(context).hexdigest(), "size": 0},
        }
        payload = (
            {
                "schema_version": "judge-paper-role-v3",
                "role": role,
                "verdict": "PASS",
                "dimensions": {},
                "issues": [],
            }
            if role == "paper"
            else {
                "schema_version": "judge-hard-role-v2",
                "role": role,
                "verdict": "PASS",
                "evidence": [],
            }
        )
        roles[role] = f"VERDICT: PASS\n{json.dumps(payload, sort_keys=True)}\n".encode()
        manifests[role] = json.dumps(manifest, sort_keys=True).encode()
        contexts[role] = context
    return roles, manifests, contexts


def runtime_fixture(tmp_path: Path, *, head_revision: int = 1):
    raw = make_pdf()
    project = tmp_path / "project"
    source = project / "references" / "source.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(raw)
    occurrence = phase3_pdf_occurrence(raw)
    state = validate_authority_phase3_artifact_state(
        AuthorityPhase3ArtifactState(
            "workflow-phase8", head_revision, (occurrence,)
        )
    )
    p6_store, proof = _phase6_proof(tmp_path, state)
    roles, manifests, contexts = _packets()

    def upstream_current(state_wire, occurrence_wire, proof_wire):
        assert authority_phase3_artifact_state_from_dict(state_wire) == state
        assert artifact_occurrence_from_dict(occurrence_wire) == occurrence
        p6_store.verify_current_access_proof(proof_wire)
        return True

    p7_store = phase7.Phase7GroundingStore(tmp_path / "phase7.db")
    p7_result = p7_store.record_grounding_bundle(
        idempotency_key="phase7-bundle-1",
        phase3_artifact_state=state,
        phase3_artifact_occurrence=occurrence,
        phase6_access_proof=proof,
        role_output_bytes=roles,
        manifest_bytes=manifests,
        context_bytes=contexts,
        current_head_verifier=upstream_current,
    )
    p7_bundle = p7_store.load_bundle("phase7-bundle-1")
    p7_alive = {"value": True}

    def p7_current(scope_key, commit_sha256):
        if not p7_alive["value"]:
            return False
        loaded = p7_store.load_current_bundle(
            scope_key, current_head_verifier=upstream_current
        )
        return loaded["result"]["commit_sha256"] == commit_sha256

    cas = tmp_path / "cas"
    scratch = tmp_path / "scratch"
    cas.mkdir(); scratch.mkdir()
    package = materialize_reference_pdf(
        reference_id="reference-phase8-1",
        project_root=project,
        pdf_path=source,
        phase3_artifact_occurrence=occurrence,
        bibliographic_metadata={
            "title": "Phase Eight Reference",
            "authors": ["Ada Example"],
            "published_year": 2026,
            "doi": None,
        },
        external_share_classification="internal",
        cas_root=cas,
        scratch_root=scratch,
        config=ReferenceMaterializerConfig(render_dpi=72),
    )
    store = Phase8EvidenceEgressStore(
        tmp_path / "phase8.db", cas, enabled=True
    )
    binding = store.record_reference_binding(
        idempotency_key="phase8-binding-1",
        logical_id="reference-binding-1",
        phase3_artifact_state=state,
        phase3_artifact_occurrence=occurrence,
        phase6_access_proof=proof,
        phase7_result=p7_bundle["result"],
        phase7_receipt=p7_bundle["receipt"],
        phase7_effective_verdict=p7_bundle["effective_verdict"],
        reference_package_blob=package.package_blob,
        reference_receipt_blob=package.receipt_blob,
        phase7_current_head_verifier=p7_current,
    )
    return {
        "store": store,
        "state": state,
        "occurrence": occurrence,
        "proof": proof,
        "p7_bundle": p7_bundle,
        "p7_current": p7_current,
        "p7_alive": p7_alive,
        "package": package,
        "binding": binding,
        "cas": cas,
        "database": tmp_path / "phase8.db",
    }


def egress_request(*, subject: str = "alice"):
    return {
        "schema_version": "data-egress-request-v1",
        "subject": subject,
        "provider": "operator-download",
        "surface": "phase8-shadow-review",
        "account_scope": "project-phase8",
        "retention": "operator-controlled",
        "purpose": "reference-review",
        "artifacts": [
            {
                "artifact_id": "reference-record",
                "sha256": _h("9"),
                "byte_length": 123,
                "transfer_form": "canonical-text",
                "classification": "internal",
            }
        ],
    }


def issue_active(fixture, *, key="approval-issue-1", approval_id="approval-1"):
    preflight = fixture["store"].register_trusted_approval_preflight(
        idempotency_key=f"{key}:trusted-preflight",
        preflight_id=f"{approval_id}-trusted-preflight",
        approval_id=approval_id,
        binding_sha256=fixture["binding"].binding_sha256,
        issuer_id="operator-1",
        issuer_generation="operator-generation-1",
        subject_id="alice",
        subject_generation="membership-generation-1",
        logical_issued_at=20,
        not_before=21,
        expires_at=100,
        decision_evaluated_at=25,
        data_egress_request=egress_request(),
        phase7_current_head_verifier=fixture["p7_current"],
    )
    return fixture["store"].issue_approval(
        idempotency_key=key,
        approval_id=approval_id,
        binding_sha256=fixture["binding"].binding_sha256,
        issuer_id="operator-1",
        issuer_generation="operator-generation-1",
        subject_id="alice",
        subject_generation="membership-generation-1",
        logical_issued_at=20,
        not_before=21,
        expires_at=100,
        data_egress_request=egress_request(),
        trusted_preflight_sha256=preflight.preflight["preflight_sha256"],
        phase7_current_head_verifier=fixture["p7_current"],
    )


def _timeout_after_commit(stage: str) -> None:
    if stage == "after_sqlite_commit":
        raise Phase78DeadlineError("synthetic deadline crossing durable commit")


def _publication(fixture, key: str, generation: str):
    binding = fixture["binding"].binding
    return build_phase8_publication_identity(
        publication_kind="work-generation",
        publication_key=key,
        generation={"winning_generation": generation},
        phase7_scope_key=binding["phase7_scope_key"],
        phase7_commit_sha256=binding["phase7_commit_sha256"],
    )


def _revocation_publication(fixture, key: str, generation: str):
    binding = fixture["binding"].binding
    return build_phase8_publication_identity(
        publication_kind="approval-revocation",
        publication_key=key,
        generation={"winning_generation": generation},
        phase7_scope_key=binding["phase7_scope_key"],
        phase7_commit_sha256=binding["phase7_commit_sha256"],
    )


def _publication_verifier(winner: dict[str, str]):
    def verify(publication):
        if publication.get("publication_kind") == "standalone-shadow":
            return publication.get("generation") == {
                "mode": "store-local",
                "publication_key": publication.get("publication_key"),
            }
        return publication.get("generation") == {
            "winning_generation": winner["value"]
        }

    return verify


def _reference_successor_kwargs(fixture, *, key: str, logical_id: str):
    return {
        "idempotency_key": key,
        "logical_id": logical_id,
        "phase3_artifact_state": fixture["state"],
        "phase3_artifact_occurrence": fixture["occurrence"],
        "phase6_access_proof": fixture["proof"],
        "phase7_result": fixture["p7_bundle"]["result"],
        "phase7_receipt": fixture["p7_bundle"]["receipt"],
        "phase7_effective_verdict": fixture["p7_bundle"]["effective_verdict"],
        "reference_package_blob": fixture["package"].package_blob,
        "reference_receipt_blob": fixture["package"].receipt_blob,
        "phase7_current_head_verifier": fixture["p7_current"],
        "expected_current_binding_sha256": fixture["binding"].binding_sha256,
    }


_PHASE8_V1_SCHEMA = (
    """CREATE TABLE phase8_schema_state(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        schema_version TEXT NOT NULL,
        absolute_path_sha256 TEXT NOT NULL
    )""",
    """CREATE TABLE phase8_reference_bindings(
        binding_sha256 TEXT PRIMARY KEY,
        logical_id TEXT NOT NULL UNIQUE,
        idempotency_key TEXT NOT NULL UNIQUE,
        request_sha256 TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        previous_binding_sha256 TEXT,
        binding_json TEXT NOT NULL,
        binding_blob_json TEXT NOT NULL,
        UNIQUE(scope_key,sequence)
    )""",
    """CREATE TABLE phase8_reference_current(
        scope_key TEXT PRIMARY KEY,
        sequence INTEGER NOT NULL,
        binding_sha256 TEXT NOT NULL,
        FOREIGN KEY(binding_sha256)
            REFERENCES phase8_reference_bindings(binding_sha256)
    )""",
    """CREATE TABLE phase8_trusted_approval_preflights(
        preflight_sha256 TEXT PRIMARY KEY,
        preflight_id TEXT NOT NULL UNIQUE,
        idempotency_key TEXT NOT NULL UNIQUE,
        request_sha256 TEXT NOT NULL,
        binding_sha256 TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        preflight_json TEXT NOT NULL,
        preflight_blob_json TEXT NOT NULL,
        FOREIGN KEY(binding_sha256)
            REFERENCES phase8_reference_bindings(binding_sha256)
    )""",
    """CREATE TABLE phase8_approvals(
        approval_id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        request_sha256 TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        binding_sha256 TEXT NOT NULL,
        trusted_preflight_sha256 TEXT NOT NULL,
        successor_of TEXT,
        approval_sha256 TEXT NOT NULL UNIQUE,
        approval_json TEXT NOT NULL,
        approval_blob_json TEXT NOT NULL,
        FOREIGN KEY(binding_sha256)
            REFERENCES phase8_reference_bindings(binding_sha256),
        FOREIGN KEY(trusted_preflight_sha256)
            REFERENCES phase8_trusted_approval_preflights(preflight_sha256)
    )""",
    """CREATE TABLE phase8_approval_events(
        event_sha256 TEXT PRIMARY KEY,
        approval_id TEXT NOT NULL,
        event_sequence INTEGER NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        request_sha256 TEXT NOT NULL,
        event_json TEXT NOT NULL,
        event_blob_json TEXT NOT NULL,
        UNIQUE(approval_id,event_sequence),
        FOREIGN KEY(approval_id) REFERENCES phase8_approvals(approval_id)
    )""",
    """CREATE TABLE phase8_approval_current(
        approval_id TEXT PRIMARY KEY,
        event_sequence INTEGER NOT NULL,
        event_sha256 TEXT NOT NULL,
        state TEXT NOT NULL,
        FOREIGN KEY(approval_id) REFERENCES phase8_approvals(approval_id),
        FOREIGN KEY(event_sha256)
            REFERENCES phase8_approval_events(event_sha256)
    )""",
    """CREATE TABLE phase8_scope_approval_current(
        scope_key TEXT PRIMARY KEY,
        approval_id TEXT NOT NULL,
        FOREIGN KEY(approval_id) REFERENCES phase8_approvals(approval_id)
    )""",
    """CREATE TABLE phase8_decisions(
        decision_sha256 TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        request_sha256 TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        previous_decision_sha256 TEXT,
        decision_json TEXT NOT NULL,
        decision_blob_json TEXT NOT NULL,
        UNIQUE(scope_key,sequence)
    )""",
    """CREATE TABLE phase8_decision_current(
        scope_key TEXT PRIMARY KEY,
        sequence INTEGER NOT NULL,
        decision_sha256 TEXT NOT NULL,
        FOREIGN KEY(decision_sha256)
            REFERENCES phase8_decisions(decision_sha256)
    )""",
)


def _create_phase8_v1_database(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        for statement in _PHASE8_V1_SCHEMA:
            connection.execute(statement)
        for table in (
            "phase8_schema_state",
            "phase8_reference_bindings",
            "phase8_trusted_approval_preflights",
            "phase8_approvals",
            "phase8_approval_events",
            "phase8_decisions",
        ):
            for action in ("UPDATE", "DELETE"):
                connection.execute(
                    f"""CREATE TRIGGER {table}_immutable_{action.lower()}
                    BEFORE {action} ON {table} BEGIN
                        SELECT RAISE(ABORT, '{table} is append-only');
                    END"""
                )
        connection.execute(
            "INSERT INTO phase8_schema_state VALUES(1,?,?)",
            (
                "phase8-evidence-egress-runtime-sqlite-v1",
                canonical_sha256(
                    {
                        "schema_version": "phase8-absolute-database-path-v1",
                        "absolute_path": str(database),
                    }
                ),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    database.chmod(0o600)


def test_v1_reopen_fails_closed_before_v2_columns_and_never_mutates_store(
    tmp_path: Path,
) -> None:
    database = tmp_path / "phase8-v1.db"
    cas = tmp_path / "cas-v1"
    cas.mkdir()
    _create_phase8_v1_database(database)
    before = database.read_bytes()

    for _attempt in range(2):
        reopened = Phase8EvidenceEgressStore(database, cas, enabled=True)
        with pytest.raises(Phase8SchemaIncompatible) as caught:
            reopened.load_reference_binding(_h("1"))
        assert caught.value.code == "PHASE8_SCHEMA_INCOMPATIBLE"
        assert "in-place upgrade is unsupported" in str(caught.value)

    assert database.read_bytes() == before
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()
    assert not Path(f"{database}-journal").exists()
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        assert connection.execute(
            "SELECT schema_version FROM phase8_schema_state"
        ).fetchone()[0] == "phase8-evidence-egress-runtime-sqlite-v1"
        assert "publication_sha256" not in {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(phase8_reference_current)"
            )
        }
        assert connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='phase8_publication_receipts'"
        ).fetchone() is None
    finally:
        connection.close()


def test_default_off_precedes_paths_deadline_and_resources(tmp_path):
    class Poison:
        def check(self, stage):
            raise AssertionError(stage)

        def remaining_seconds(self):
            raise AssertionError

    store = Phase8EvidenceEgressStore(
        tmp_path / "missing" / "phase8.db",
        tmp_path / "missing" / "cas",
        enabled=False,
    )
    with pytest.raises(Phase8Disabled):
        store.load_reference_binding(_h("1"), deadline=Poison())
    assert not (tmp_path / "missing").exists()


def test_enabled_total_deadline_expires_before_sqlite_creation(tmp_path):
    cas = tmp_path / "cas"
    cas.mkdir()

    class Expired:
        def check(self, stage):
            raise TimeoutError(stage)

        def remaining_seconds(self):
            return 0.0

    database = tmp_path / "phase8.db"
    store = Phase8EvidenceEgressStore(database, cas, enabled=True)
    with pytest.raises(Phase8DeadlineExceeded):
        store.load_reference_binding(_h("1"), deadline=Expired())
    assert not database.exists()


def test_reference_binding_replay_restart_and_deep_current_load(tmp_path):
    fixture = runtime_fixture(tmp_path)
    binding = fixture["binding"]
    loaded = Phase8EvidenceEgressStore(
        fixture["database"], fixture["cas"], enabled=True
    ).load_current_reference_binding(
        binding.scope_key,
        phase7_current_head_verifier=fixture["p7_current"],
        expected_binding_sha256=binding.binding_sha256,
    )
    assert loaded.binding == binding.binding
    assert loaded.binding["authoritative"] is False
    assert loaded.binding["phase3_artifact_state_sha256"] == fixture["state"].state_sha256
    assert stat.S_IMODE(fixture["database"].stat().st_mode) == 0o600
    assert not any(
        Path(f"{fixture['database']}{suffix}").exists()
        for suffix in ("-wal", "-shm", "-journal")
    )

    replay = fixture["store"].record_reference_binding(
        idempotency_key="phase8-binding-1",
        logical_id="reference-binding-1",
        phase3_artifact_state=fixture["state"],
        phase3_artifact_occurrence=fixture["occurrence"],
        phase6_access_proof=fixture["proof"],
        phase7_result=fixture["p7_bundle"]["result"],
        phase7_receipt=fixture["p7_bundle"]["receipt"],
        phase7_effective_verdict=fixture["p7_bundle"]["effective_verdict"],
        reference_package_blob=fixture["package"].package_blob,
        reference_receipt_blob=fixture["package"].receipt_blob,
        phase7_current_head_verifier=fixture["p7_current"],
    )
    assert replay.binding_sha256 == binding.binding_sha256
    assert replay.replayed is True


def test_reference_logical_id_rejects_different_valid_package_without_current_drift(tmp_path):
    fixture = runtime_fixture(tmp_path)
    different_package = materialize_reference_pdf(
        reference_id="reference-phase8-1",
        project_root=tmp_path / "project",
        pdf_path=tmp_path / "project" / "references" / "source.pdf",
        phase3_artifact_occurrence=fixture["occurrence"],
        bibliographic_metadata={
            "title": "A Different Valid Bibliographic Identity",
            "authors": ["Ada Example"],
            "published_year": 2026,
            "doi": None,
        },
        external_share_classification="internal",
        cas_root=fixture["cas"],
        scratch_root=tmp_path / "scratch",
        config=ReferenceMaterializerConfig(render_dpi=72),
    )
    assert different_package.package_blob != fixture["package"].package_blob

    with pytest.raises(Phase8ReplayConflict):
        fixture["store"].record_reference_binding(
            idempotency_key="phase8-binding-different-package",
            logical_id="reference-binding-1",
            phase3_artifact_state=fixture["state"],
            phase3_artifact_occurrence=fixture["occurrence"],
            phase6_access_proof=fixture["proof"],
            phase7_result=fixture["p7_bundle"]["result"],
            phase7_receipt=fixture["p7_bundle"]["receipt"],
            phase7_effective_verdict=fixture["p7_bundle"]["effective_verdict"],
            reference_package_blob=different_package.package_blob,
            reference_receipt_blob=different_package.receipt_blob,
            phase7_current_head_verifier=fixture["p7_current"],
            expected_current_binding_sha256=fixture["binding"].binding_sha256,
        )

    current = fixture["store"].load_current_reference_binding(
        fixture["binding"].scope_key,
        phase7_current_head_verifier=fixture["p7_current"],
        expected_binding_sha256=fixture["binding"].binding_sha256,
    )
    assert current.binding_sha256 == fixture["binding"].binding_sha256
    assert current.sequence == 1


def test_later_aggregate_head_accepts_unchanged_older_current_occurrence(tmp_path):
    fixture = runtime_fixture(tmp_path, head_revision=2)

    assert fixture["state"].through_revision == 2
    assert fixture["occurrence"].revision == 1
    assert fixture["binding"].binding[
        "phase3_artifact_occurrence_id"
    ] == fixture["occurrence"].occurrence_id
    assert fixture["binding"].binding["authority_coordinate"]["current_revision"] == 2
    assert fixture["binding"].binding[
        "source_snapshot_coordinate"
    ]["project_revision"] == 2


def test_return_to_same_bytes_rejects_stale_noncurrent_occurrence(tmp_path):
    fixture = runtime_fixture(tmp_path)
    old = fixture["occurrence"]
    replacement = build_artifact_occurrence(
        workflow_id=old.workflow_id,
        revision=3,
        command_id="command-phase8-return-to-a",
        mutation_sha256=hashlib.sha256(b"phase8-a-b-a-return").hexdigest(),
        artifact_record=old.artifact_record,
    )
    state = validate_authority_phase3_artifact_state(
        AuthorityPhase3ArtifactState(old.workflow_id, 3, (replacement,))
    )
    newer_root = tmp_path / "newer-head"
    newer_root.mkdir()
    _store, proof = _phase6_proof(newer_root, state)

    with pytest.raises(Phase8CurrentConflict):
        fixture["store"].record_reference_binding(
            idempotency_key="phase8-stale-a-b-a-binding",
            logical_id="phase8-stale-a-b-a-binding",
            phase3_artifact_state=state,
            phase3_artifact_occurrence=old,
            phase6_access_proof=proof,
            phase7_result=fixture["p7_bundle"]["result"],
            phase7_receipt=fixture["p7_bundle"]["receipt"],
            phase7_effective_verdict=fixture["p7_bundle"]["effective_verdict"],
            reference_package_blob=fixture["package"].package_blob,
            reference_receipt_blob=fixture["package"].receipt_blob,
            phase7_current_head_verifier=fixture["p7_current"],
        )


def test_issue_requires_exact_durable_trusted_local_preflight(tmp_path):
    fixture = runtime_fixture(tmp_path)
    issue_kwargs = {
        "idempotency_key": "approval-trust-gate",
        "approval_id": "approval-trust-gate",
        "binding_sha256": fixture["binding"].binding_sha256,
        "issuer_id": "operator-1",
        "issuer_generation": "operator-generation-1",
        "subject_id": "alice",
        "subject_generation": "membership-generation-1",
        "logical_issued_at": 20,
        "not_before": 21,
        "expires_at": 100,
        "data_egress_request": egress_request(),
        "phase7_current_head_verifier": fixture["p7_current"],
    }
    with pytest.raises(TypeError):
        fixture["store"].issue_approval(**issue_kwargs)
    with pytest.raises(Phase8NotFound):
        fixture["store"].issue_approval(
            **issue_kwargs,
            trusted_preflight_sha256=_h("f"),
        )

    trusted = fixture["store"].register_trusted_approval_preflight(
        idempotency_key="trusted-preflight-gate",
        preflight_id="trusted-preflight-gate",
        approval_id="approval-trust-gate",
        binding_sha256=fixture["binding"].binding_sha256,
        issuer_id="operator-1",
        issuer_generation="operator-generation-1",
        subject_id="alice",
        subject_generation="membership-generation-1",
        logical_issued_at=20,
        not_before=21,
        expires_at=100,
        decision_evaluated_at=25,
        data_egress_request=egress_request(),
        phase7_current_head_verifier=fixture["p7_current"],
    )
    with pytest.raises(Phase8CurrentConflict):
        fixture["store"].issue_approval(
            **{**issue_kwargs, "issuer_generation": "caller-forged-generation"},
            trusted_preflight_sha256=trusted.preflight["preflight_sha256"],
        )
    with pytest.raises(Phase8NotFound):
        fixture["store"].load_current_approval(
            fixture["binding"].scope_key,
            phase7_current_head_verifier=fixture["p7_current"],
        )
    assert not hasattr(
        Phase8EvidenceEgressRunner(fixture["store"]),
        "register_trusted_approval_preflight",
    )


def test_normal_approval_authorized_decision_and_restart(tmp_path):
    fixture = runtime_fixture(tmp_path)
    approval = issue_active(fixture)
    decision = fixture["store"].evaluate_egress(
        idempotency_key="decision-1",
        binding_sha256=fixture["binding"].binding_sha256,
        data_egress_request=egress_request(),
        evaluated_at=25,
        phase7_current_head_verifier=fixture["p7_current"],
    )
    assert approval.approval["approved"] is True
    assert decision.decision["status"] == "AUTHORIZED"
    assert decision.decision["dispatch_performed"] is False
    view = Phase8EvidenceEgressStore(
        fixture["database"], fixture["cas"], enabled=True
    ).load_current_decision(
        fixture["binding"].scope_key,
        evaluated_at=25,
        phase7_current_head_verifier=fixture["p7_current"],
    )
    assert view["status"] == "AUTHORIZED"
    assert view["history_retained"] is True


def test_policy_drift_preserves_history_and_denies_only_effective_current(
    tmp_path, monkeypatch
):
    fixture = runtime_fixture(tmp_path)
    approval = issue_active(fixture)
    decision = fixture["store"].evaluate_egress(
        idempotency_key="decision-before-policy-drift",
        binding_sha256=fixture["binding"].binding_sha256,
        data_egress_request=egress_request(),
        evaluated_at=25,
        phase7_current_head_verifier=fixture["p7_current"],
    )
    changed_policy = _h("f")
    monkeypatch.setattr(
        "factory_core.phase8_evidence_egress_runtime.data_egress_policy_sha256",
        lambda: changed_policy,
    )
    restarted = Phase8EvidenceEgressStore(
        fixture["database"], fixture["cas"], enabled=True
    )

    assert restarted.load_trusted_approval_preflight(
        approval.approval["trusted_preflight_sha256"]
    ).preflight["policy_sha256"] != changed_policy
    assert restarted.load_approval("approval-1").approval["approval_sha256"] \
        == approval.approval["approval_sha256"]
    assert restarted.load_decision(
        decision.decision["decision_sha256"]
    ).decision["status"] == "AUTHORIZED"
    current = restarted.load_current_decision(
        fixture["binding"].scope_key,
        evaluated_at=26,
        phase7_current_head_verifier=fixture["p7_current"],
    )
    assert (current["status"], current["reason_code"]) == (
        "DENIED",
        "POLICY_DRIFT",
    )


def test_revoke_and_expiry_auto_deny_current_but_preserve_authorized_history(tmp_path):
    fixture = runtime_fixture(tmp_path)
    approval = issue_active(fixture)
    decision = fixture["store"].evaluate_egress(
        idempotency_key="decision-1",
        binding_sha256=fixture["binding"].binding_sha256,
        data_egress_request=egress_request(),
        evaluated_at=25,
        phase7_current_head_verifier=fixture["p7_current"],
    )
    fixture["store"].revoke_approval(
        idempotency_key="revoke-1",
        approval_id="approval-1",
        expected_event_sha256=approval.lifecycle_event["event_sha256"],
        revoked_at=30,
    )
    restarted = Phase8EvidenceEgressStore(
        fixture["database"], fixture["cas"], enabled=True
    )
    view = restarted.load_current_decision(
        fixture["binding"].scope_key,
        evaluated_at=31,
        phase7_current_head_verifier=fixture["p7_current"],
    )
    assert view["status"] == "DENIED"
    assert view["reason_code"] == "APPROVAL_REVOKED"
    assert Phase8EvidenceEgressRunner(restarted).current_decision(
        fixture["binding"].scope_key,
        evaluated_at=31,
        phase7_current_head_verifier=fixture["p7_current"],
    )["status"] == "DENIED"
    assert restarted.load_decision(
        decision.decision["decision_sha256"]
    ).decision["status"] == "AUTHORIZED"

    second = runtime_fixture(tmp_path / "second")
    issue_active(second)
    denied = second["store"].evaluate_egress(
        idempotency_key="decision-expired",
        binding_sha256=second["binding"].binding_sha256,
        data_egress_request=egress_request(),
        evaluated_at=101,
        phase7_current_head_verifier=second["p7_current"],
    )
    assert denied.decision["status"] == "DENIED"
    assert denied.decision["reason_code"] == "APPROVAL_EXPIRED"


def test_successor_supersedes_old_and_becomes_current(tmp_path):
    fixture = runtime_fixture(tmp_path)
    first = issue_active(fixture)
    fixture["store"].evaluate_egress(
        idempotency_key="decision-before-successor",
        binding_sha256=fixture["binding"].binding_sha256,
        data_egress_request=egress_request(),
        evaluated_at=25,
        phase7_current_head_verifier=fixture["p7_current"],
    )
    second_preflight = fixture["store"].register_trusted_approval_preflight(
        idempotency_key="approval-issue-2:trusted-preflight",
        preflight_id="approval-2-trusted-preflight",
        approval_id="approval-2",
        binding_sha256=fixture["binding"].binding_sha256,
        issuer_id="operator-1",
        issuer_generation="operator-generation-1",
        subject_id="alice",
        subject_generation="membership-generation-2",
        logical_issued_at=40,
        not_before=41,
        expires_at=200,
        decision_evaluated_at=45,
        data_egress_request=egress_request(),
        phase7_current_head_verifier=fixture["p7_current"],
        successor_of="approval-1",
        expected_predecessor_event_sha256=first.lifecycle_event["event_sha256"],
    )
    second = fixture["store"].issue_approval(
        idempotency_key="approval-issue-2",
        approval_id="approval-2",
        binding_sha256=fixture["binding"].binding_sha256,
        issuer_id="operator-1",
        issuer_generation="operator-generation-1",
        subject_id="alice",
        subject_generation="membership-generation-2",
        logical_issued_at=40,
        not_before=41,
        expires_at=200,
        data_egress_request=egress_request(),
        trusted_preflight_sha256=second_preflight.preflight["preflight_sha256"],
        phase7_current_head_verifier=fixture["p7_current"],
        successor_of="approval-1",
        expected_predecessor_event_sha256=first.lifecycle_event["event_sha256"],
    )
    assert second.approval["successor_of"] == "approval-1"
    assert fixture["store"].load_approval("approval-1").lifecycle_event["state"] == "SUPERSEDED"
    assert fixture["store"].load_current_approval(
        fixture["binding"].scope_key,
        phase7_current_head_verifier=fixture["p7_current"],
    ).approval["approval_id"] == "approval-2"
    view = fixture["store"].load_current_decision(
        fixture["binding"].scope_key,
        evaluated_at=45,
        phase7_current_head_verifier=fixture["p7_current"],
    )
    assert (view["status"], view["reason_code"]) == (
        "DENIED",
        "APPROVAL_SUPERSEDED",
    )


def test_phase7_head_drift_blocks_binding_load_and_auto_denies_decision(tmp_path):
    fixture = runtime_fixture(tmp_path)
    issue_active(fixture)
    fixture["store"].evaluate_egress(
        idempotency_key="decision-1",
        binding_sha256=fixture["binding"].binding_sha256,
        data_egress_request=egress_request(),
        evaluated_at=25,
        phase7_current_head_verifier=fixture["p7_current"],
    )
    fixture["p7_alive"]["value"] = False
    with pytest.raises(Phase8CurrentConflict):
        fixture["store"].load_current_reference_binding(
            fixture["binding"].scope_key,
            phase7_current_head_verifier=fixture["p7_current"],
        )
    view = fixture["store"].load_current_decision(
        fixture["binding"].scope_key,
        evaluated_at=25,
        phase7_current_head_verifier=fixture["p7_current"],
    )
    assert (view["status"], view["reason_code"]) == ("DENIED", "PHASE7_HEAD_DRIFT")


def test_decision_idempotency_conflict_and_concurrent_same_bytes(tmp_path):
    fixture = runtime_fixture(tmp_path)
    issue_active(fixture)

    def run():
        return fixture["store"].evaluate_egress(
            idempotency_key="decision-concurrent",
            binding_sha256=fixture["binding"].binding_sha256,
            data_egress_request=egress_request(),
            evaluated_at=25,
            phase7_current_head_verifier=fixture["p7_current"],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert {item.decision["decision_sha256"] for item in results}.__len__() == 1
    assert sorted(item.replayed for item in results) == [False, True]
    with pytest.raises(Phase8ReplayConflict):
        fixture["store"].evaluate_egress(
            idempotency_key="decision-concurrent",
            binding_sha256=fixture["binding"].binding_sha256,
            data_egress_request={**egress_request(), "purpose": "different-purpose"},
            evaluated_at=25,
            phase7_current_head_verifier=fixture["p7_current"],
        )


def test_late_fence_rolls_back_current_publication(tmp_path):
    fixture = runtime_fixture(tmp_path)
    issue_active(fixture)

    def fence(stage):
        if stage == "before_sqlite_commit":
            raise RuntimeError("adapter generation changed")

    with pytest.raises(RuntimeError, match="generation changed"):
        fixture["store"].evaluate_egress(
            idempotency_key="decision-fenced",
            binding_sha256=fixture["binding"].binding_sha256,
            data_egress_request=egress_request(),
            evaluated_at=25,
            phase7_current_head_verifier=fixture["p7_current"],
            adapter_fence=fence,
        )
    with pytest.raises(Exception) as caught:
        fixture["store"].load_current_decision(
            fixture["binding"].scope_key,
            evaluated_at=25,
            phase7_current_head_verifier=fixture["p7_current"],
        )
    assert getattr(caught.value, "code", None) == "PHASE8_NOT_FOUND"


def test_revoke_and_successor_replays_are_exactly_idempotent(tmp_path):
    fixture = runtime_fixture(tmp_path)
    first = issue_active(fixture)
    revoked = fixture["store"].revoke_approval(
        idempotency_key="revoke-replay",
        approval_id="approval-1",
        expected_event_sha256=first.lifecycle_event["event_sha256"],
        revoked_at=30,
    )
    replay = fixture["store"].revoke_approval(
        idempotency_key="revoke-replay",
        approval_id="approval-1",
        expected_event_sha256=first.lifecycle_event["event_sha256"],
        revoked_at=30,
    )
    assert replay.replayed is True
    assert replay.lifecycle_event == revoked.lifecycle_event

    other = runtime_fixture(tmp_path / "successor")
    predecessor = issue_active(other)
    successor_preflight = other["store"].register_trusted_approval_preflight(
        idempotency_key="approval-successor-preflight",
        preflight_id="approval-2-successor-preflight",
        approval_id="approval-2",
        binding_sha256=other["binding"].binding_sha256,
        issuer_id="operator-1",
        issuer_generation="operator-generation-1",
        subject_id="alice",
        subject_generation="membership-generation-2",
        logical_issued_at=40,
        not_before=41,
        expires_at=200,
        decision_evaluated_at=45,
        data_egress_request=egress_request(),
        phase7_current_head_verifier=other["p7_current"],
        successor_of="approval-1",
        expected_predecessor_event_sha256=predecessor.lifecycle_event["event_sha256"],
    )
    kwargs = {
        "idempotency_key": "approval-successor-replay",
        "approval_id": "approval-2",
        "binding_sha256": other["binding"].binding_sha256,
        "issuer_id": "operator-1",
        "issuer_generation": "operator-generation-1",
        "subject_id": "alice",
        "subject_generation": "membership-generation-2",
        "logical_issued_at": 40,
        "not_before": 41,
        "expires_at": 200,
        "data_egress_request": egress_request(),
        "trusted_preflight_sha256": successor_preflight.preflight[
            "preflight_sha256"
        ],
        "phase7_current_head_verifier": other["p7_current"],
        "successor_of": "approval-1",
        "expected_predecessor_event_sha256": predecessor.lifecycle_event["event_sha256"],
    }
    issued = other["store"].issue_approval(**kwargs)
    replayed = other["store"].issue_approval(**kwargs)
    assert replayed.replayed is True
    assert replayed.approval == issued.approval


def test_decision_replay_returns_frozen_history_after_revocation(tmp_path):
    fixture = runtime_fixture(tmp_path)
    approval = issue_active(fixture)
    kwargs = {
        "idempotency_key": "decision-history-replay",
        "binding_sha256": fixture["binding"].binding_sha256,
        "data_egress_request": egress_request(),
        "evaluated_at": 25,
        "phase7_current_head_verifier": fixture["p7_current"],
    }
    first = fixture["store"].evaluate_egress(**kwargs)
    fixture["store"].revoke_approval(
        idempotency_key="revoke-after-decision",
        approval_id="approval-1",
        expected_event_sha256=approval.lifecycle_event["event_sha256"],
        revoked_at=30,
    )
    replay = fixture["store"].evaluate_egress(**kwargs)
    assert replay.replayed is True
    assert replay.decision == first.decision
    assert replay.decision["status"] == "AUTHORIZED"
    assert fixture["store"].load_current_decision(
        fixture["binding"].scope_key,
        evaluated_at=31,
        phase7_current_head_verifier=fixture["p7_current"],
    )["status"] == "DENIED"


def test_binding_publication_rolls_back_at_late_fence(tmp_path):
    fixture = runtime_fixture(tmp_path)

    def fence(stage):
        if stage == "before_sqlite_commit":
            raise RuntimeError("late generation")

    with pytest.raises(RuntimeError, match="late generation"):
        fixture["store"].record_reference_binding(
            idempotency_key="phase8-binding-late",
            logical_id="reference-binding-late",
            phase3_artifact_state=fixture["state"],
            phase3_artifact_occurrence=fixture["occurrence"],
            phase6_access_proof=fixture["proof"],
            phase7_result=fixture["p7_bundle"]["result"],
            phase7_receipt=fixture["p7_bundle"]["receipt"],
            phase7_effective_verdict=fixture["p7_bundle"]["effective_verdict"],
            reference_package_blob=fixture["package"].package_blob,
            reference_receipt_blob=fixture["package"].receipt_blob,
            phase7_current_head_verifier=fixture["p7_current"],
            expected_current_binding_sha256=fixture["binding"].binding_sha256,
            adapter_fence=fence,
        )
    connection = sqlite3.connect(fixture["database"])
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM phase8_reference_bindings WHERE idempotency_key='phase8-binding-late'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT binding_sha256 FROM phase8_reference_current WHERE scope_key=?",
            (fixture["binding"].scope_key,),
        ).fetchone()[0] == fixture["binding"].binding_sha256
    finally:
        connection.close()


def test_reference_commit_crossing_is_uncertain_historical_and_same_key_replayable(
    tmp_path,
):
    fixture = runtime_fixture(tmp_path)
    kwargs = {
        "idempotency_key": "phase8-binding-post-commit-timeout",
        "logical_id": "reference-binding-post-commit-timeout",
        "phase3_artifact_state": fixture["state"],
        "phase3_artifact_occurrence": fixture["occurrence"],
        "phase6_access_proof": fixture["proof"],
        "phase7_result": fixture["p7_bundle"]["result"],
        "phase7_receipt": fixture["p7_bundle"]["receipt"],
        "phase7_effective_verdict": fixture["p7_bundle"]["effective_verdict"],
        "reference_package_blob": fixture["package"].package_blob,
        "reference_receipt_blob": fixture["package"].receipt_blob,
        "phase7_current_head_verifier": fixture["p7_current"],
        "expected_current_binding_sha256": fixture["binding"].binding_sha256,
    }
    with pytest.raises(Phase78OutcomeUncertain) as captured:
        fixture["store"].record_reference_binding(
            **kwargs, adapter_fence=_timeout_after_commit
        )
    assert captured.value.idempotency_key == kwargs["idempotency_key"]

    connection = sqlite3.connect(fixture["database"])
    try:
        assert connection.execute(
            "SELECT count(*) FROM phase8_reference_bindings "
            "WHERE idempotency_key=?",
            (kwargs["idempotency_key"],),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT binding_sha256 FROM phase8_reference_current WHERE scope_key=?",
            (fixture["binding"].scope_key,),
        ).fetchone()[0] == fixture["binding"].binding_sha256
    finally:
        connection.close()

    replay = fixture["store"].record_reference_binding(**kwargs)
    assert replay.replayed is True
    assert replay.current is True
    assert replay.sequence == 2
    current = fixture["store"].load_current_reference_binding(
        replay.scope_key,
        phase7_current_head_verifier=fixture["p7_current"],
        expected_binding_sha256=replay.binding_sha256,
    )
    assert current.binding_sha256 == replay.binding_sha256
    with pytest.raises(Phase8ReplayConflict):
        fixture["store"].record_reference_binding(
            **{**kwargs, "logical_id": "different-reference-binding-bytes"}
        )


def test_trusted_preflight_commit_crossing_is_uncertain_and_exactly_replayable(
    tmp_path,
):
    fixture = runtime_fixture(tmp_path)
    kwargs = {
        "idempotency_key": "trusted-preflight-post-commit-timeout",
        "preflight_id": "trusted-preflight-post-commit-timeout",
        "approval_id": "approval-post-commit-preflight",
        "binding_sha256": fixture["binding"].binding_sha256,
        "issuer_id": "operator-1",
        "issuer_generation": "operator-generation-1",
        "subject_id": "alice",
        "subject_generation": "membership-generation-1",
        "logical_issued_at": 20,
        "not_before": 21,
        "expires_at": 100,
        "decision_evaluated_at": 25,
        "data_egress_request": egress_request(),
        "phase7_current_head_verifier": fixture["p7_current"],
    }
    with pytest.raises(Phase78OutcomeUncertain) as captured:
        fixture["store"].register_trusted_approval_preflight(
            **kwargs, adapter_fence=_timeout_after_commit
        )
    assert captured.value.idempotency_key == kwargs["idempotency_key"]
    connection = sqlite3.connect(fixture["database"])
    try:
        assert connection.execute(
            "SELECT count(*) FROM phase8_trusted_approval_preflights "
            "WHERE idempotency_key=?",
            (kwargs["idempotency_key"],),
        ).fetchone()[0] == 1
    finally:
        connection.close()
    replay = fixture["store"].register_trusted_approval_preflight(**kwargs)
    assert replay.replayed is True
    loaded = fixture["store"].load_trusted_approval_preflight(
        replay.preflight["preflight_sha256"]
    )
    assert loaded.preflight == replay.preflight
    with pytest.raises(Phase8ReplayConflict):
        fixture["store"].register_trusted_approval_preflight(
            **{**kwargs, "preflight_id": "different-preflight-bytes"}
        )


def test_approval_issue_commit_crossing_keeps_history_without_effective_current(
    tmp_path,
):
    fixture = runtime_fixture(tmp_path)
    preflight = fixture["store"].register_trusted_approval_preflight(
        idempotency_key="approval-post-commit-preflight",
        preflight_id="approval-post-commit-preflight",
        approval_id="approval-post-commit-timeout",
        binding_sha256=fixture["binding"].binding_sha256,
        issuer_id="operator-1",
        issuer_generation="operator-generation-1",
        subject_id="alice",
        subject_generation="membership-generation-1",
        logical_issued_at=20,
        not_before=21,
        expires_at=100,
        decision_evaluated_at=25,
        data_egress_request=egress_request(),
        phase7_current_head_verifier=fixture["p7_current"],
    )
    kwargs = {
        "idempotency_key": "approval-issue-post-commit-timeout",
        "approval_id": "approval-post-commit-timeout",
        "binding_sha256": fixture["binding"].binding_sha256,
        "issuer_id": "operator-1",
        "issuer_generation": "operator-generation-1",
        "subject_id": "alice",
        "subject_generation": "membership-generation-1",
        "logical_issued_at": 20,
        "not_before": 21,
        "expires_at": 100,
        "data_egress_request": egress_request(),
        "trusted_preflight_sha256": preflight.preflight["preflight_sha256"],
        "phase7_current_head_verifier": fixture["p7_current"],
    }
    with pytest.raises(Phase78OutcomeUncertain) as captured:
        fixture["store"].issue_approval(
            **kwargs, adapter_fence=_timeout_after_commit
        )
    assert captured.value.idempotency_key == kwargs["idempotency_key"]
    assert fixture["store"].load_approval(
        kwargs["approval_id"]
    ).approval["approval_id"] == kwargs["approval_id"]
    with pytest.raises(Phase8NotFound):
        fixture["store"].load_current_approval(
            fixture["binding"].scope_key,
            phase7_current_head_verifier=fixture["p7_current"],
        )
    replay = fixture["store"].issue_approval(**kwargs)
    assert replay.replayed is True
    current = fixture["store"].load_current_approval(
        fixture["binding"].scope_key,
        phase7_current_head_verifier=fixture["p7_current"],
    )
    assert current.approval["approval_id"] == kwargs["approval_id"]


def test_approval_lifecycle_commit_crossing_replays_without_cancelled_head(
    tmp_path,
):
    fixture = runtime_fixture(tmp_path)
    active = issue_active(fixture)
    kwargs = {
        "idempotency_key": "approval-revoke-post-commit-timeout",
        "approval_id": active.approval["approval_id"],
        "expected_event_sha256": active.lifecycle_event["event_sha256"],
        "revoked_at": 30,
    }
    with pytest.raises(Phase78OutcomeUncertain) as captured:
        fixture["store"].revoke_approval(
            **kwargs, adapter_fence=_timeout_after_commit
        )
    assert captured.value.idempotency_key == kwargs["idempotency_key"]
    historical = fixture["store"].load_approval(kwargs["approval_id"])
    assert historical.lifecycle_event["state"] == "ACTIVE"
    connection = sqlite3.connect(fixture["database"])
    try:
        assert connection.execute(
            "SELECT count(*) FROM phase8_approval_events WHERE idempotency_key=?",
            (kwargs["idempotency_key"],),
        ).fetchone()[0] == 1
    finally:
        connection.close()
    replay = fixture["store"].revoke_approval(**kwargs)
    assert replay.replayed is True
    assert fixture["store"].load_approval(
        kwargs["approval_id"]
    ).lifecycle_event["state"] == "REVOKED"
    with pytest.raises(Phase8ReplayConflict):
        fixture["store"].revoke_approval(**{**kwargs, "revoked_at": 31})


def test_egress_decision_commit_crossing_keeps_history_and_replays_exact_head(
    tmp_path,
):
    fixture = runtime_fixture(tmp_path)
    issue_active(fixture)
    kwargs = {
        "idempotency_key": "decision-post-commit-timeout",
        "binding_sha256": fixture["binding"].binding_sha256,
        "data_egress_request": egress_request(),
        "evaluated_at": 25,
        "phase7_current_head_verifier": fixture["p7_current"],
    }
    with pytest.raises(Phase78OutcomeUncertain) as captured:
        fixture["store"].evaluate_egress(
            **kwargs, adapter_fence=_timeout_after_commit
        )
    assert captured.value.idempotency_key == kwargs["idempotency_key"]
    connection = sqlite3.connect(fixture["database"])
    try:
        decision_sha = connection.execute(
            "SELECT decision_sha256 FROM phase8_decisions WHERE idempotency_key=?",
            (kwargs["idempotency_key"],),
        ).fetchone()[0]
        assert connection.execute(
            "SELECT count(*) FROM phase8_decision_current WHERE scope_key=?",
            (fixture["binding"].scope_key,),
        ).fetchone()[0] == 0
    finally:
        connection.close()
    assert fixture["store"].load_decision(
        decision_sha
    ).decision["status"] == "AUTHORIZED"
    replay = fixture["store"].evaluate_egress(**kwargs)
    assert replay.replayed is True
    assert fixture["store"].load_current_decision(
        fixture["binding"].scope_key,
        evaluated_at=25,
        phase7_current_head_verifier=fixture["p7_current"],
    )["source_decision_sha256"] == replay.decision["decision_sha256"]
    with pytest.raises(Phase8ReplayConflict):
        fixture["store"].evaluate_egress(
            **{
                **kwargs,
                "data_egress_request": {
                    **egress_request(),
                    "purpose": "different-purpose",
                },
            }
        )


def test_history_commit_crash_and_concurrent_window_never_publish_current(
    tmp_path,
):
    fixture = runtime_fixture(tmp_path)
    old_binding = fixture["binding"].binding_sha256
    concurrent_kwargs = _reference_successor_kwargs(
        fixture,
        key="reference-concurrent-history-window",
        logical_id="reference-concurrent-history-window",
    )
    first_commit = threading.Event()
    release = threading.Event()
    observed = {}
    commits = 0

    def block_after_history(stage):
        nonlocal commits
        if stage == "after_sqlite_commit":
            commits += 1
            if commits == 1:
                first_commit.set()
                assert release.wait(timeout=10)

    def invoke():
        try:
            observed["result"] = fixture["store"].record_reference_binding(
                **concurrent_kwargs,
                adapter_fence=block_after_history,
            )
        except BaseException as exc:
            observed["error"] = exc

    thread = threading.Thread(target=invoke, name="phase8-history-window")
    thread.start()
    assert first_commit.wait(timeout=10)
    reader = sqlite3.connect(fixture["database"])
    try:
        assert reader.execute(
            "SELECT count(*) FROM phase8_reference_bindings "
            "WHERE idempotency_key=?",
            (concurrent_kwargs["idempotency_key"],),
        ).fetchone()[0] == 1
        assert reader.execute(
            "SELECT binding_sha256 FROM phase8_reference_current WHERE scope_key=?",
            (fixture["binding"].scope_key,),
        ).fetchone()[0] == old_binding
    finally:
        reader.close()
    release.set()
    thread.join(timeout=15)
    assert not thread.is_alive()
    assert "error" not in observed
    concurrent = observed["result"]

    crash_kwargs = {
        **_reference_successor_kwargs(
            fixture,
            key="reference-crash-after-history",
            logical_id="reference-crash-after-history",
        ),
        "expected_current_binding_sha256": concurrent.binding_sha256,
    }

    def crash_after_history(stage):
        if stage == "after_sqlite_commit":
            raise SystemExit("synthetic process crash after history commit")

    with pytest.raises(SystemExit):
        fixture["store"].record_reference_binding(
            **crash_kwargs,
            adapter_fence=crash_after_history,
        )
    assert fixture["store"].load_reference_binding_by_idempotency_key(
        crash_kwargs["idempotency_key"]
    ).binding_sha256
    current = fixture["store"].load_current_reference_binding(
        fixture["binding"].scope_key,
        phase7_current_head_verifier=fixture["p7_current"],
    )
    assert current.binding_sha256 == concurrent.binding_sha256
    replay = fixture["store"].record_reference_binding(**crash_kwargs)
    assert replay.replayed is True


@pytest.mark.parametrize(
    "crossing",
    ["user_cancel", "shutdown", "superseded", "head_drift", "process_crash"],
)
def test_reference_activation_crossing_is_read_time_qualified_and_takeoverable(
    tmp_path,
    monkeypatch,
    crossing,
):
    fixture = runtime_fixture(tmp_path)
    key = f"reference-activation-{crossing}"
    kwargs = _reference_successor_kwargs(
        fixture,
        key=key,
        logical_id=key,
    )
    winner = {"value": "generation-1"}
    verifier = _publication_verifier(winner)
    kwargs.update(
        publication_identity=_publication(fixture, key, "generation-1"),
        publication_head_verifier=verifier,
    )
    monkeypatch.setattr(
        Phase8EvidenceEgressStore,
        "_reconcile_post_commit",
        lambda *_args, **_kwargs: None,
    )
    commits = 0

    def lose_during_activation(stage):
        nonlocal commits
        if stage != "after_sqlite_commit":
            return
        commits += 1
        if commits != 2:
            return
        winner["value"] = "generation-2"
        if crossing == "head_drift":
            fixture["p7_alive"]["value"] = False
            raise Phase8CurrentConflict("synthetic Phase-7 head drift")
        if crossing == "process_crash":
            raise SystemExit("synthetic crash after activation commit")
        raise Phase78CancellationError(
            Phase78CancellationReason(crossing)
        )

    expected_error = (
        SystemExit
        if crossing == "process_crash"
        else Phase8CurrentConflict
        if crossing == "head_drift"
        else Phase78CancellationError
    )
    with pytest.raises(expected_error):
        fixture["store"].record_reference_binding(
            **kwargs,
            adapter_fence=lose_during_activation,
        )
    historical = fixture["store"].load_reference_binding_by_idempotency_key(key)
    with pytest.raises(Phase8CurrentConflict):
        fixture["store"].load_current_reference_binding(
            historical.scope_key,
            phase7_current_head_verifier=fixture["p7_current"],
            publication_head_verifier=verifier,
            expected_binding_sha256=historical.binding_sha256,
        )
    fixture["p7_alive"]["value"] = True
    replay = fixture["store"].record_reference_binding(
        **{
            **kwargs,
            "publication_identity": _publication(
                fixture, key, "generation-2"
            ),
        }
    )
    assert replay.replayed is True
    current = fixture["store"].load_current_reference_binding(
        replay.scope_key,
        phase7_current_head_verifier=fixture["p7_current"],
        publication_head_verifier=verifier,
        expected_binding_sha256=replay.binding_sha256,
    )
    assert current.binding_sha256 == historical.binding_sha256
    with pytest.raises(Phase8ReplayConflict):
        fixture["store"].record_reference_binding(
            **{
                **kwargs,
                "logical_id": f"{key}-different",
                "publication_identity": _publication(
                    fixture, key, "generation-2"
                ),
            }
        )


def _cancel_second_commit(winner: dict[str, str]):
    commits = 0

    def fence(stage):
        nonlocal commits
        if stage == "after_sqlite_commit":
            commits += 1
            if commits == 2:
                winner["value"] = "generation-2"
                raise Phase78CancellationError(
                    Phase78CancellationReason.SUPERSEDED
                )

    return fence


def test_approval_issue_activation_cancelled_generation_is_never_current(
    tmp_path,
    monkeypatch,
):
    fixture = runtime_fixture(tmp_path)
    key = "approval-activation-superseded"
    approval_id = "approval-activation-superseded"
    winner = {"value": "generation-1"}
    verifier = _publication_verifier(winner)
    preflight = fixture["store"].register_trusted_approval_preflight(
        idempotency_key=f"{key}:preflight",
        preflight_id=f"{key}:preflight",
        approval_id=approval_id,
        binding_sha256=fixture["binding"].binding_sha256,
        issuer_id="operator-1",
        issuer_generation="operator-generation-1",
        subject_id="alice",
        subject_generation="membership-generation-1",
        logical_issued_at=20,
        not_before=21,
        expires_at=100,
        decision_evaluated_at=25,
        data_egress_request=egress_request(),
        phase7_current_head_verifier=fixture["p7_current"],
        publication_head_verifier=verifier,
    )
    kwargs = {
        "idempotency_key": key,
        "approval_id": approval_id,
        "binding_sha256": fixture["binding"].binding_sha256,
        "issuer_id": "operator-1",
        "issuer_generation": "operator-generation-1",
        "subject_id": "alice",
        "subject_generation": "membership-generation-1",
        "logical_issued_at": 20,
        "not_before": 21,
        "expires_at": 100,
        "data_egress_request": egress_request(),
        "trusted_preflight_sha256": preflight.preflight["preflight_sha256"],
        "phase7_current_head_verifier": fixture["p7_current"],
        "publication_identity": _publication(fixture, key, "generation-1"),
        "publication_head_verifier": verifier,
    }
    monkeypatch.setattr(
        Phase8EvidenceEgressStore,
        "_reconcile_post_commit",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(Phase78CancellationError):
        fixture["store"].issue_approval(
            **kwargs,
            adapter_fence=_cancel_second_commit(winner),
        )
    assert fixture["store"].load_approval(approval_id).approval["approval_id"] == approval_id
    with pytest.raises(Phase8CurrentConflict):
        fixture["store"].load_current_approval(
            fixture["binding"].scope_key,
            phase7_current_head_verifier=fixture["p7_current"],
            publication_head_verifier=verifier,
        )
    replay = fixture["store"].issue_approval(
        **{
            **kwargs,
            "publication_identity": _publication(
                fixture, key, "generation-2"
            ),
        }
    )
    assert replay.replayed is True
    assert fixture["store"].load_current_approval(
        fixture["binding"].scope_key,
        phase7_current_head_verifier=fixture["p7_current"],
        publication_head_verifier=verifier,
    ).approval["approval_id"] == approval_id


def test_lifecycle_activation_cancelled_generation_is_never_current(
    tmp_path,
    monkeypatch,
):
    fixture = runtime_fixture(tmp_path)
    active = issue_active(fixture)
    key = "lifecycle-activation-superseded"
    winner = {"value": "generation-1"}
    verifier = _publication_verifier(winner)
    kwargs = {
        "idempotency_key": key,
        "approval_id": active.approval["approval_id"],
        "expected_event_sha256": active.lifecycle_event["event_sha256"],
        "revoked_at": 30,
        "phase7_current_head_verifier": fixture["p7_current"],
        "publication_identity": _publication(fixture, key, "generation-1"),
        "publication_head_verifier": verifier,
    }
    monkeypatch.setattr(
        Phase8EvidenceEgressStore,
        "_reconcile_post_commit",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(Phase78CancellationError):
        fixture["store"].revoke_approval(
            **kwargs,
            adapter_fence=_cancel_second_commit(winner),
        )
    assert fixture["store"].load_approval(
        active.approval["approval_id"]
    ).lifecycle_event["state"] == "REVOKED"
    with pytest.raises(Phase8CurrentConflict):
        fixture["store"].load_current_approval(
            fixture["binding"].scope_key,
            phase7_current_head_verifier=fixture["p7_current"],
            publication_head_verifier=verifier,
        )
    replay = fixture["store"].revoke_approval(
        **{
            **kwargs,
            "publication_identity": _publication(
                fixture, key, "generation-2"
            ),
        }
    )
    assert replay.replayed is True
    assert fixture["store"].load_current_approval(
        fixture["binding"].scope_key,
        phase7_current_head_verifier=fixture["p7_current"],
        publication_head_verifier=verifier,
    ).lifecycle_event["state"] == "REVOKED"


@pytest.mark.parametrize("crossing", ["user_cancel", "head_drift", "process_crash"])
def test_revocation_activation_requires_terminal_publication_receipt(
    tmp_path,
    monkeypatch,
    crossing,
):
    fixture = runtime_fixture(tmp_path)
    active = issue_active(fixture)
    key = f"revocation-receipt-{crossing}"
    winner = {"value": "generation-1"}
    verifier = _publication_verifier(winner)
    kwargs = {
        "idempotency_key": key,
        "approval_id": active.approval["approval_id"],
        "expected_event_sha256": active.lifecycle_event["event_sha256"],
        "revoked_at": 30,
        "phase7_current_head_verifier": fixture["p7_current"],
        "publication_identity": _revocation_publication(
            fixture, key, "generation-1"
        ),
        "publication_head_verifier": verifier,
    }
    monkeypatch.setattr(
        Phase8EvidenceEgressStore,
        "_reconcile_post_commit",
        lambda *_args, **_kwargs: None,
    )
    commits = 0

    def cross_activation(stage):
        nonlocal commits
        if stage != "after_sqlite_commit":
            return
        commits += 1
        if commits != 2:
            return
        winner["value"] = "generation-2"
        if crossing == "head_drift":
            fixture["p7_alive"]["value"] = False
            raise Phase8CurrentConflict("synthetic revocation head drift")
        if crossing == "process_crash":
            raise SystemExit("synthetic revocation activation crash")
        raise Phase78CancellationError(Phase78CancellationReason.USER_CANCEL)

    expected_error = (
        SystemExit
        if crossing == "process_crash"
        else Phase8CurrentConflict
        if crossing == "head_drift"
        else Phase78CancellationError
    )
    with pytest.raises(expected_error):
        fixture["store"].revoke_approval(
            **kwargs,
            adapter_fence=cross_activation,
        )
    connection = sqlite3.connect(fixture["database"])
    try:
        assert connection.execute(
            "SELECT count(*) FROM phase8_publication_receipts"
        ).fetchone()[0] == 0
    finally:
        connection.close()
    with pytest.raises(Phase8CurrentConflict):
        fixture["store"].load_current_approval(
            fixture["binding"].scope_key,
            phase7_current_head_verifier=fixture["p7_current"],
            publication_head_verifier=verifier,
        )
    fixture["p7_alive"]["value"] = True
    replay = fixture["store"].revoke_approval(
        **{
            **kwargs,
            "publication_identity": _revocation_publication(
                fixture, key, "generation-2"
            ),
        }
    )
    assert replay.replayed is True
    current = fixture["store"].load_current_approval(
        fixture["binding"].scope_key,
        phase7_current_head_verifier=fixture["p7_current"],
        publication_head_verifier=verifier,
    )
    assert current.lifecycle_event["state"] == "REVOKED"
    connection = sqlite3.connect(fixture["database"])
    try:
        assert connection.execute(
            "SELECT count(*) FROM phase8_publication_receipts"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_revocation_receipt_commit_crossing_is_uncertain_and_exactly_replayable(
    tmp_path,
):
    fixture = runtime_fixture(tmp_path)
    active = issue_active(fixture)
    key = "revocation-receipt-post-commit-timeout"
    winner = {"value": "generation-1"}
    verifier = _publication_verifier(winner)
    kwargs = {
        "idempotency_key": key,
        "approval_id": active.approval["approval_id"],
        "expected_event_sha256": active.lifecycle_event["event_sha256"],
        "revoked_at": 30,
        "phase7_current_head_verifier": fixture["p7_current"],
        "publication_identity": _revocation_publication(
            fixture, key, "generation-1"
        ),
        "publication_head_verifier": verifier,
    }
    commits = 0

    def timeout_after_receipt(stage):
        nonlocal commits
        if stage == "after_sqlite_commit":
            commits += 1
            if commits == 3:
                raise Phase78DeadlineError(
                    "synthetic deadline crossing revocation receipt commit"
                )

    with pytest.raises(Phase78OutcomeUncertain) as captured:
        fixture["store"].revoke_approval(
            **kwargs,
            adapter_fence=timeout_after_receipt,
        )
    assert captured.value.idempotency_key == key
    assert fixture["store"].load_current_approval(
        fixture["binding"].scope_key,
        phase7_current_head_verifier=fixture["p7_current"],
        publication_head_verifier=verifier,
    ).lifecycle_event["state"] == "REVOKED"
    replay = fixture["store"].revoke_approval(**kwargs)
    assert replay.replayed is True
    with pytest.raises(Phase8ReplayConflict):
        fixture["store"].revoke_approval(**{**kwargs, "revoked_at": 31})


def test_decision_activation_cancelled_generation_is_never_current(
    tmp_path,
    monkeypatch,
):
    fixture = runtime_fixture(tmp_path)
    issue_active(fixture)
    key = "decision-activation-superseded"
    winner = {"value": "generation-1"}
    verifier = _publication_verifier(winner)
    kwargs = {
        "idempotency_key": key,
        "binding_sha256": fixture["binding"].binding_sha256,
        "data_egress_request": egress_request(),
        "evaluated_at": 25,
        "phase7_current_head_verifier": fixture["p7_current"],
        "publication_identity": _publication(fixture, key, "generation-1"),
        "publication_head_verifier": verifier,
    }
    monkeypatch.setattr(
        Phase8EvidenceEgressStore,
        "_reconcile_post_commit",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(Phase78CancellationError):
        fixture["store"].evaluate_egress(
            **kwargs,
            adapter_fence=_cancel_second_commit(winner),
        )
    historical = fixture["store"].load_decision_by_idempotency_key(key)
    assert historical.decision["status"] == "AUTHORIZED"
    with pytest.raises(Phase8CurrentConflict):
        fixture["store"].load_current_decision(
            fixture["binding"].scope_key,
            evaluated_at=25,
            phase7_current_head_verifier=fixture["p7_current"],
            publication_head_verifier=verifier,
        )
    replay = fixture["store"].evaluate_egress(
        **{
            **kwargs,
            "publication_identity": _publication(
                fixture, key, "generation-2"
            ),
        }
    )
    assert replay.replayed is True
    assert fixture["store"].load_current_decision(
        fixture["binding"].scope_key,
        evaluated_at=25,
        phase7_current_head_verifier=fixture["p7_current"],
        publication_head_verifier=verifier,
    )["source_decision_sha256"] == historical.decision["decision_sha256"]
