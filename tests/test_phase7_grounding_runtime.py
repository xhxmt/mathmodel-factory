from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from factory_core.artifact_ownership import ArtifactOwnership
from factory_core.authority_read_repository import (
    AuthorityPhase3ArtifactState,
    authority_phase3_artifact_state_from_dict,
    validate_authority_phase3_artifact_state,
)
from factory_core.canonical import canonical_sha256
from factory_core.owner_compiler import compile_owner_registry
from factory_core.phase3_artifacts import (
    ArtifactBlocker,
    ArtifactBlockerCode,
    artifact_occurrence_from_dict,
    build_artifact_occurrence,
    build_artifact_record,
    register_artifact_owner,
)
from factory_core.phase6_snapshot_grants import (
    GrantScope,
    Phase6SnapshotGrantStore,
    SectionAvailability,
    VerifiedSection,
    build_authority_source_binding,
)
import factory_core.phase7_grounding_runtime as phase7


def _h(character: str) -> str:
    return character * 64


def _phase3_source(*, revision: int = 1, kind: str = "RECORD"):
    compilation = compile_owner_registry(
        (
            ArtifactOwnership(
                pattern="results/**",
                owner_stage=4,
                semantic_domain="canonical_result",
                dirty_flag="RESULT_DIRTY",
            ),
        )
    )
    registration = register_artifact_owner(compilation, "results/result.json")
    if kind == "RECORD":
        record = build_artifact_record(registration, content=b'{"value":1}\n')
        occurrence = build_artifact_occurrence(
            workflow_id="workflow-1",
            revision=revision,
            command_id=f"command-{revision}",
            mutation_sha256=hashlib.sha256(f"mutation:{revision}".encode()).hexdigest(),
            artifact_record=record,
        )
    else:
        blocker = ArtifactBlocker(
            ArtifactBlockerCode.MISSING,
            registration.normalized_path,
            "artifact_missing",
            registration,
            None,
        )
        occurrence = build_artifact_occurrence(
            workflow_id="workflow-1",
            revision=revision,
            command_id=f"command-{revision}",
            mutation_sha256=hashlib.sha256(f"mutation:{revision}".encode()).hexdigest(),
            blocker=blocker,
        )
    state = validate_authority_phase3_artifact_state(
        AuthorityPhase3ArtifactState("workflow-1", revision, (occurrence,))
    )
    return state, occurrence


def _binding(state_sha256: str, revision: int = 1):
    coordinate = {
        "schema": "authority-workflow-coordinate-v1",
        "workflow_id": "workflow-1",
        "project_id": "project-1",
        "project_generation": "project-generation-1",
        "run_generation": "run-generation-1",
        "runtime_generation": "runtime-generation-1",
        "scheduler_generation": "scheduler-generation-1",
        "current_revision": revision,
        "contract_pin_set_sha256": _h("a"),
        "authority_state": "active",
        "source_fence_sha256": _h("b"),
        "switch_mode": "shadow",
        "switch_epoch": 3,
    }
    source_coordinate = {
        "schema_version": "snapshot-coordinate-v0",
        "project_id": "project-1",
        "workflow_schema_version": 1,
        "project_revision": revision,
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
            f"revision:{revision}".encode()
        ).hexdigest(),
        authority_revision_through_revision=revision,
        source_snapshot_schema="project-snapshot-v0-source-authorized-v3",
        source_snapshot_semantic_sha256=hashlib.sha256(
            f"snapshot:{revision}".encode()
        ).hexdigest(),
        source_snapshot_completeness="COMPLETE",
        source_snapshot_coordinate=source_coordinate,
        phase3_artifact_state_sha256=state_sha256,
        phase4_operation_state_sha256=_h("d"),
        phase5_supervisor_state_sha256=_h("e"),
    )


def _phase6_proof(tmp_path: Path, state, revision: int = 1):
    store = Phase6SnapshotGrantStore(tmp_path / "phase6.db")
    store.initialize()
    snapshot = store.append_snapshot(
        source_binding=_binding(state.state_sha256, revision),
        sections=(
            VerifiedSection(
                "overview",
                SectionAvailability.AVAILABLE,
                _h("1"),
                "overview-section-v1",
            ),
        ),
        captured_at=10,
        valid_until=100,
        expected_previous_snapshot_id=None,
        idempotency_key="snapshot-request-1",
    ).snapshot
    grant = store.issue_grant(
        snapshot_id=snapshot.snapshot_id,
        subject_type="user",
        subject_id="alice",
        subject_generation="membership-generation-1",
        scope=GrantScope.SECTION_VIEW,
        scope_key="overview",
        issuer_id="shadow-issuer",
        issuer_generation="issuer-generation-1",
        issuer_evidence_schema="synthetic-issuer-receipt-v1",
        issuer_receipt_sha256=_h("2"),
        issued_at=11,
        not_before=12,
        expires_at=50,
        expected_previous_grant_id=None,
        idempotency_key="grant-request-1",
    ).grant
    evaluation = store.evaluate_grant(
        grant.grant_id,
        subject_type="user",
        subject_id="alice",
        subject_generation="membership-generation-1",
        requested_scope=GrantScope.SECTION_VIEW,
        requested_scope_key="overview",
        evaluated_at=12,
        idempotency_key="evaluation-request-1",
    )
    assert evaluation.access_proof is not None
    return store, evaluation.access_proof


def _packets(
    *,
    math_verdict: str = "PASS",
    execution_verdict: str = "PASS",
    paper_verdict: str = "PASS",
):
    roles: dict[str, bytes] = {}
    manifests: dict[str, bytes] = {}
    contexts: dict[str, bytes] = {}
    for role in phase7.ROLE_ORDER:
        context = b""
        manifest = {
            "role": role,
            "files": [],
            "context": {
                "sha256": hashlib.sha256(context).hexdigest(),
                "size": len(context),
            },
        }
        verdict = {
            "math": math_verdict,
            "execution": execution_verdict,
            "paper": paper_verdict,
        }[role]
        payload = (
            {
                "schema_version": "judge-paper-role-v3",
                "role": role,
                "verdict": verdict,
                "dimensions": {},
                "issues": [],
            }
            if role == "paper"
            else {
                "schema_version": "judge-hard-role-v2",
                "role": role,
                "verdict": verdict,
                "evidence": [],
            }
        )
        roles[role] = (
            f"VERDICT: {verdict}\n{json.dumps(payload, sort_keys=True)}\n"
        ).encode()
        manifests[role] = json.dumps(manifest, sort_keys=True).encode()
        contexts[role] = context
    return roles, manifests, contexts


def _fixture(tmp_path: Path, *, kind: str = "RECORD"):
    state, occurrence = _phase3_source(kind=kind)
    phase6_store, proof = _phase6_proof(tmp_path, state)
    roles, manifests, contexts = _packets()

    def current_head(state_wire, occurrence_wire, proof_wire):
        assert authority_phase3_artifact_state_from_dict(state_wire) == state
        assert artifact_occurrence_from_dict(occurrence_wire) == occurrence
        phase6_store.verify_current_access_proof(proof_wire)
        return True

    return state, occurrence, proof, current_head, roles, manifests, contexts


def test_default_off_returns_before_paths_sources_deadline_or_sqlite(tmp_path):
    class Poison:
        def check(self):
            raise AssertionError("disabled deadline was called")

    run = phase7.run_phase7_grounding_shadow(
        enabled=False,
        database=tmp_path / "missing" / "phase7.db",
        phase3_artifact_state=object(),
        deadline=Poison(),
    )

    assert run.enabled is False
    assert run.store_verified is False
    assert not tmp_path.joinpath("missing").exists()


def test_enabled_public_runner_commits_normal_shadow_bundle(tmp_path):
    state, occurrence, proof, head, roles, manifests, contexts = _fixture(tmp_path)
    run = phase7.run_phase7_grounding_shadow(
        enabled=True,
        database=(tmp_path / "phase7.db").resolve(),
        idempotency_key="grounding-runner",
        phase3_artifact_state=state.as_dict(),
        phase3_artifact_occurrence=occurrence.as_dict(),
        phase6_access_proof=proof.as_dict(),
        role_output_bytes=roles,
        manifest_bytes=manifests,
        context_bytes=contexts,
        current_head_verifier=head,
    )

    assert run.enabled is True and run.store_verified is True
    assert run.result is not None
    assert run.result["aggregate_verdict"] == "PASS"
    assert run.authoritative is False
    assert run.authority_transferred is False
    assert run.dispatch_performed is False


def test_three_role_bundle_binds_exact_bytes_policy_and_upstream_state(tmp_path):
    state, occurrence, proof, _, roles, manifests, contexts = _fixture(tmp_path)

    prepared = phase7.prepare_grounding_bundle(
        idempotency_key="grounding-1",
        phase3_artifact_state=state.as_dict(),
        phase3_artifact_occurrence=occurrence.as_dict(),
        phase6_access_proof=proof.as_dict(),
        role_output_bytes=roles,
        manifest_bytes=manifests,
        context_bytes=contexts,
    )

    assert prepared.aggregate_verdict == "PASS"
    assert prepared.grounding_valid is True
    assert set(prepared.receipt["role_receipts"]) == set(phase7.ROLE_ORDER)
    assert prepared.receipt["input_identity"]["phase3_artifact_state_sha256"] == (
        state.state_sha256
    )
    for role in phase7.ROLE_ORDER:
        stored = prepared.receipt["role_receipts"][role]["role_output_bytes"]
        assert stored["base64"]
        assert stored["byte_length"] == len(roles[role])
    policy = prepared.effective_verdict["aggregate_policy"]
    assert policy["upstream_aggregate_schema_version"] == "judge-aggregate-v3"
    assert policy["role_schema_versions"] == phase7._ROLE_SCHEMAS


@pytest.mark.parametrize(
    ("packet_options", "expected_verdict", "expected_action"),
    (
        ({"math_verdict": "FAIL"}, "FAIL", "REOPEN_REVISION_MODEL"),
        ({"execution_verdict": "FAIL"}, "FAIL", "REOPEN_REVISION_MODEL"),
        ({"paper_verdict": "REVISE"}, "REVISE", "REOPEN_REVISION_TEXT"),
        ({"math_verdict": "INDETERMINATE"}, "INDETERMINATE", "INDETERMINATE_REVIEW"),
    ),
)
def test_aggregate_policy_covers_all_three_role_outcomes(
    tmp_path, packet_options, expected_verdict, expected_action
):
    state, occurrence, proof, _, _, _, _ = _fixture(tmp_path)
    roles, manifests, contexts = _packets(**packet_options)
    prepared = phase7.prepare_grounding_bundle(
        idempotency_key="grounding-aggregate",
        phase3_artifact_state=state,
        phase3_artifact_occurrence=occurrence,
        phase6_access_proof=proof,
        role_output_bytes=roles,
        manifest_bytes=manifests,
        context_bytes=contexts,
    )
    assert prepared.aggregate_verdict == expected_verdict
    assert prepared.aggregate_action == expected_action


def test_normal_commit_replay_restart_and_current_load(tmp_path):
    state, occurrence, proof, head, roles, manifests, contexts = _fixture(tmp_path)
    path = (tmp_path / "phase7.db").resolve()
    store = phase7.Phase7GroundingStore(path)
    kwargs = dict(
        idempotency_key="grounding-1",
        phase3_artifact_state=state,
        phase3_artifact_occurrence=occurrence,
        phase6_access_proof=proof,
        role_output_bytes=roles,
        manifest_bytes=manifests,
        context_bytes=contexts,
        current_head_verifier=head,
    )

    first = store.record_grounding_bundle(**kwargs)
    replay = store.record_grounding_bundle(**kwargs)
    reopened = phase7.Phase7GroundingStore(path)
    current = reopened.load_current(first.scope_key, current_head_verifier=head)
    bundle = reopened.load_bundle("grounding-1")

    assert first.aggregate_verdict == "PASS"
    assert first.current is True and first.replayed is False
    assert replay.commit_sha256 == first.commit_sha256 and replay.replayed is True
    assert current.commit_sha256 == first.commit_sha256
    assert bundle["receipt"]["receipt_sha256"] == first.receipt_sha256
    assert bundle["result"]["current"] is True
    assert stat_mode(path) == 0o600
    assert not any(Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal"))


def test_replay_identity_is_independent_of_absolute_packet_root(tmp_path):
    state, occurrence, proof, head, roles, manifests, contexts = _fixture(tmp_path)

    def persist_packets(root: Path) -> tuple[dict[str, bytes], ...]:
        root.mkdir()
        mappings: list[dict[str, bytes]] = []
        for label, values in (
            ("role", roles),
            ("manifest", manifests),
            ("context", contexts),
        ):
            directory = root / label
            directory.mkdir()
            for role, value in values.items():
                (directory / f"{role}.bin").write_bytes(value)
            mappings.append(
                {
                    role: (directory / f"{role}.bin").read_bytes()
                    for role in phase7.ROLE_ORDER
                }
            )
        return tuple(mappings)

    first_root = tmp_path / "absolute-root-a"
    second_root = tmp_path / "absolute-root-b"
    first_roles, first_manifests, first_contexts = persist_packets(first_root)
    second_roles, second_manifests, second_contexts = persist_packets(second_root)
    store = phase7.Phase7GroundingStore((tmp_path / "phase7.db").resolve())
    common = dict(
        idempotency_key="path-free-grounding",
        phase3_artifact_state=state,
        phase3_artifact_occurrence=occurrence,
        phase6_access_proof=proof,
        current_head_verifier=head,
    )

    committed = store.record_grounding_bundle(
        role_output_bytes=first_roles,
        manifest_bytes=first_manifests,
        context_bytes=first_contexts,
        **common,
    )
    first_root.rename(tmp_path / "moved-original-root")
    replayed = phase7.Phase7GroundingStore(store.path).record_grounding_bundle(
        role_output_bytes=second_roles,
        manifest_bytes=second_manifests,
        context_bytes=second_contexts,
        **common,
    )

    assert replayed.replayed is True
    assert replayed.commit_sha256 == committed.commit_sha256


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def test_invalid_and_missing_outputs_replace_prior_pass_without_deleting_history(tmp_path):
    state, occurrence, proof, head, roles, manifests, contexts = _fixture(tmp_path)
    store = phase7.Phase7GroundingStore((tmp_path / "phase7.db").resolve())
    common = dict(
        phase3_artifact_state=state,
        phase3_artifact_occurrence=occurrence,
        phase6_access_proof=proof,
        manifest_bytes=manifests,
        context_bytes=contexts,
        current_head_verifier=head,
    )
    passed = store.record_grounding_bundle(
        idempotency_key="grounding-pass", role_output_bytes=roles, **common
    )
    missing = dict(roles)
    missing["execution"] = None
    invalid = store.record_grounding_bundle(
        idempotency_key="grounding-invalid", role_output_bytes=missing, **common
    )

    assert passed.aggregate_verdict == "PASS"
    assert invalid.sequence == 2
    assert invalid.aggregate_verdict == "INDETERMINATE"
    assert invalid.current is True
    assert store.load("grounding-pass").current is False
    assert store.load_current(invalid.scope_key, current_head_verifier=head).commit_sha256 == (
        invalid.commit_sha256
    )


def test_non_record_occurrence_forces_indeterminate(tmp_path):
    state, occurrence, proof, head, roles, manifests, contexts = _fixture(
        tmp_path, kind="BLOCKER"
    )
    result = phase7.Phase7GroundingStore(
        (tmp_path / "phase7.db").resolve()
    ).record_grounding_bundle(
        idempotency_key="grounding-blocked",
        phase3_artifact_state=state,
        phase3_artifact_occurrence=occurrence,
        phase6_access_proof=proof,
        role_output_bytes=roles,
        manifest_bytes=manifests,
        context_bytes=contexts,
        current_head_verifier=head,
    )
    assert result.aggregate_verdict == "INDETERMINATE"
    assert result.grounding_valid is False


def test_exact_whitespace_change_conflicts_under_same_idempotency_key(tmp_path):
    state, occurrence, proof, head, roles, manifests, contexts = _fixture(tmp_path)
    store = phase7.Phase7GroundingStore((tmp_path / "phase7.db").resolve())
    kwargs = dict(
        idempotency_key="grounding-1",
        phase3_artifact_state=state,
        phase3_artifact_occurrence=occurrence,
        phase6_access_proof=proof,
        manifest_bytes=manifests,
        context_bytes=contexts,
        current_head_verifier=head,
    )
    store.record_grounding_bundle(role_output_bytes=roles, **kwargs)
    changed = dict(roles)
    changed["math"] += b"\n"
    with pytest.raises(phase7.Phase7GroundingReplayConflict):
        store.record_grounding_bundle(role_output_bytes=changed, **kwargs)


def test_current_head_failure_rolls_back_and_stale_current_load_fails_closed(tmp_path):
    state, occurrence, proof, head, roles, manifests, contexts = _fixture(tmp_path)
    store = phase7.Phase7GroundingStore((tmp_path / "phase7.db").resolve())
    kwargs = dict(
        idempotency_key="grounding-1",
        phase3_artifact_state=state,
        phase3_artifact_occurrence=occurrence,
        phase6_access_proof=proof,
        role_output_bytes=roles,
        manifest_bytes=manifests,
        context_bytes=contexts,
    )
    with pytest.raises(phase7.Phase7GroundingCurrentConflict):
        store.record_grounding_bundle(
            current_head_verifier=lambda *_: False, **kwargs
        )
    result = store.record_grounding_bundle(current_head_verifier=head, **kwargs)
    with pytest.raises(phase7.Phase7GroundingCurrentConflict):
        store.load_current(
            result.scope_key, current_head_verifier=lambda *_: False
        )


@pytest.mark.parametrize(
    "code",
    ["PHASE78_DEADLINE_EXCEEDED", "PHASE78_REQUEST_CANCELLED"],
)
def test_current_head_deadline_and_cancellation_keep_public_classification(
    tmp_path, code
):
    state, occurrence, proof, _head, roles, manifests, contexts = _fixture(tmp_path)
    store = phase7.Phase7GroundingStore((tmp_path / "phase7.db").resolve())
    error_type = type("ClassifiedCurrentError", (RuntimeError,), {"code": code})
    failure = error_type("classified current-head interruption")

    def interrupted(*_args):
        raise failure

    with pytest.raises(error_type) as captured:
        store.record_grounding_bundle(
            idempotency_key=f"grounding-{code.lower()}",
            phase3_artifact_state=state,
            phase3_artifact_occurrence=occurrence,
            phase6_access_proof=proof,
            role_output_bytes=roles,
            manifest_bytes=manifests,
            context_bytes=contexts,
            current_head_verifier=interrupted,
        )
    assert captured.value is failure
    with pytest.raises(phase7.Phase7GroundingNotFound):
        store.load(f"grounding-{code.lower()}")


def test_post_commit_timeout_is_replayable_and_old_fence_cannot_undo_commit(tmp_path):
    state, occurrence, proof, head, roles, manifests, contexts = _fixture(tmp_path)
    store = phase7.Phase7GroundingStore((tmp_path / "phase7.db").resolve())
    kwargs = dict(
        idempotency_key="grounding-1",
        phase3_artifact_state=state,
        phase3_artifact_occurrence=occurrence,
        phase6_access_proof=proof,
        role_output_bytes=roles,
        manifest_bytes=manifests,
        context_bytes=contexts,
        current_head_verifier=head,
    )

    def timeout_after_commit(stage):
        if stage == "phase7_commit_after":
            raise TimeoutError("client deadline elapsed after commit")

    with pytest.raises(TimeoutError, match="after commit"):
        store.record_grounding_bundle(fence_hook=timeout_after_commit, **kwargs)
    replay = store.record_grounding_bundle(**kwargs)
    assert replay.replayed is True
    assert replay.sequence == 1


def test_total_deadline_is_shared_across_precompute_lock_begin_and_commit(tmp_path):
    state, occurrence, proof, head, roles, manifests, contexts = _fixture(tmp_path)

    class Deadline:
        def __init__(self):
            self.check_calls = 0
            self.remaining_calls = 0

        def check(self):
            self.check_calls += 1

        def remaining_seconds(self):
            self.remaining_calls += 1
            return 2.0

    deadline = Deadline()
    result = phase7.Phase7GroundingStore(
        (tmp_path / "phase7.db").resolve()
    ).record_grounding_bundle(
        idempotency_key="grounding-deadline",
        phase3_artifact_state=state,
        phase3_artifact_occurrence=occurrence,
        phase6_access_proof=proof,
        role_output_bytes=roles,
        manifest_bytes=manifests,
        context_bytes=contexts,
        current_head_verifier=head,
        deadline=deadline,
    )

    assert result.aggregate_verdict == "PASS"
    assert deadline.remaining_calls >= 3
    assert deadline.check_calls >= 10


def test_precommit_deadline_and_transaction_failure_roll_back_atomically(
    tmp_path, monkeypatch
):
    state, occurrence, proof, head, roles, manifests, contexts = _fixture(tmp_path)
    store = phase7.Phase7GroundingStore((tmp_path / "phase7.db").resolve())
    kwargs = dict(
        idempotency_key="grounding-rollback",
        phase3_artifact_state=state,
        phase3_artifact_occurrence=occurrence,
        phase6_access_proof=proof,
        role_output_bytes=roles,
        manifest_bytes=manifests,
        context_bytes=contexts,
        current_head_verifier=head,
    )

    class Deadline:
        def __init__(self):
            self.calls = 0

        def check(self):
            self.calls += 1
            if self.calls == 9:
                raise TimeoutError("deadline before commit")

        def remaining_seconds(self):
            return 2.0

    store.initialize()
    with pytest.raises(TimeoutError, match="before commit"):
        store.record_grounding_bundle(deadline=Deadline(), **kwargs)
    with pytest.raises(phase7.Phase7GroundingNotFound):
        store.load("grounding-rollback")

    def fail_before_commit(stage):
        if stage == "before_commit":
            raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(phase7, "_phase7_failure_point", fail_before_commit)
    with pytest.raises(RuntimeError, match="transaction failure"):
        store.record_grounding_bundle(**kwargs)
    monkeypatch.setattr(phase7, "_phase7_failure_point", lambda stage: None)
    recovered = store.record_grounding_bundle(**kwargs)
    assert recovered.sequence == 1


def test_revision_chain_rejects_stale_a_b_a_reuse(tmp_path):
    state_a, occurrence_a = _phase3_source(revision=1)
    state_b, occurrence_b = _phase3_source(revision=2)
    proof_a_dir = tmp_path / "proof-a"
    proof_b_dir = tmp_path / "proof-b"
    proof_a_dir.mkdir()
    proof_b_dir.mkdir()
    _, proof_a = _phase6_proof(proof_a_dir, state_a, revision=1)
    _, proof_b = _phase6_proof(proof_b_dir, state_b, revision=2)
    roles, manifests, contexts = _packets()
    store = phase7.Phase7GroundingStore((tmp_path / "phase7.db").resolve())

    def write(key, state, occurrence, proof):
        return store.record_grounding_bundle(
            idempotency_key=key,
            phase3_artifact_state=state,
            phase3_artifact_occurrence=occurrence,
            phase6_access_proof=proof,
            role_output_bytes=roles,
            manifest_bytes=manifests,
            context_bytes=contexts,
            current_head_verifier=lambda *_: True,
        )

    first = write("grounding-a", state_a, occurrence_a, proof_a)
    second = write("grounding-b", state_b, occurrence_b, proof_b)
    with pytest.raises(phase7.Phase7GroundingCurrentConflict, match="regress"):
        write("grounding-a-again", state_a, occurrence_a, proof_a)
    assert first.sequence == 1 and second.sequence == 2
    assert store.load_current(
        second.scope_key, current_head_verifier=lambda *_: True
    ).commit_sha256 == second.commit_sha256


def test_unchanged_occurrence_can_be_selected_from_a_later_aggregate_head(tmp_path):
    _, occurrence = _phase3_source(revision=1)
    later_state = validate_authority_phase3_artifact_state(
        AuthorityPhase3ArtifactState("workflow-1", 2, (occurrence,))
    )
    proof_dir = tmp_path / "proof-later"
    proof_dir.mkdir()
    _, proof = _phase6_proof(proof_dir, later_state, revision=2)
    roles, manifests, contexts = _packets()

    prepared = phase7.prepare_grounding_bundle(
        idempotency_key="grounding-later-head",
        phase3_artifact_state=later_state,
        phase3_artifact_occurrence=occurrence,
        phase6_access_proof=proof,
        role_output_bytes=roles,
        manifest_bytes=manifests,
        context_bytes=contexts,
    )

    assert occurrence.revision == 1
    assert prepared.authority_revision == 2
    assert prepared.aggregate_verdict == "PASS"


def test_concurrent_normal_writers_serialize_without_sidecars(tmp_path):
    state, occurrence, proof, head, roles, manifests, contexts = _fixture(tmp_path)
    path = (tmp_path / "phase7.db").resolve()
    store = phase7.Phase7GroundingStore(path)

    def write(index):
        return store.record_grounding_bundle(
            idempotency_key=f"grounding-{index}",
            phase3_artifact_state=state,
            phase3_artifact_occurrence=occurrence,
            phase6_access_proof=proof,
            role_output_bytes=roles,
            manifest_bytes=manifests,
            context_bytes=contexts,
            current_head_verifier=head,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, (1, 2)))
    assert sorted(item.sequence for item in results) == [1, 2]
    assert sum(store.load(f"grounding-{index}").current for index in (1, 2)) == 1
    assert not any(Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal"))


def test_schema_tamper_and_sidecar_fail_closed(tmp_path):
    path = (tmp_path / "phase7.db").resolve()
    store = phase7.Phase7GroundingStore(path)
    store.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unexpected(value TEXT)")
    with pytest.raises(phase7.Phase7GroundingStoreError, match="schema profile"):
        phase7.Phase7GroundingStore(path).initialize()

    other = (tmp_path / "phase7-sidecar.db").resolve()
    phase7.Phase7GroundingStore(other).initialize()
    Path(f"{other}-wal").write_bytes(b"unexpected")
    with pytest.raises(phase7.Phase7GroundingStoreError, match="-wal"):
        phase7.Phase7GroundingStore(other).initialize()
