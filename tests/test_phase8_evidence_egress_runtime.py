from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import stat

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
from factory_core.phase8_evidence_egress_runtime import (
    Phase8CurrentConflict,
    Phase8Disabled,
    Phase8DeadlineExceeded,
    Phase8EvidenceEgressRunner,
    Phase8EvidenceEgressStore,
    Phase8NotFound,
    Phase8ReplayConflict,
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
