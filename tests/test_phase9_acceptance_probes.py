"""Non-recursive behavioral probes for the formal Phase9-A runner.

Each canonical acceptance node below exercises an implementation surface that
is independent of the replay evidence producer and finalizer. This keeps the
formal runner finite while making PASS depend on observable state transitions,
rejection behavior, or byte-level validation rather than restated constants.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from factory_core.adapters.infrastructure.pause_policy import (
    PauseAction,
    PauseMode,
    ProcessScopeKind,
    decide_pause_action,
)
from factory_core.audit.acceptance import build_final_acceptance_receipt
from factory_core.audit.domain import AuditSnapshot
from factory_core.audit.service import FinalAuditService
from factory_core.canonical import canonical_bytes, canonical_sha256
from factory_core.contract_pins import CONTRACT_PIN_SET_SCHEMA, ContractPinSetV1
from factory_core.delivery.release import ReleasePublisher
from factory_core.domain import StepContext
from factory_core.durable_operation import (
    InvalidOperationTransition,
    OperationEvent,
    OperationStatus,
    build_worker_launch_identity,
)
from factory_core import phase4_shadow_runtime as shadow_runtime
from factory_core.phase4_shadow_runtime import Phase4ShadowFenceError, Phase4ShadowStore
from factory_core.phase9_delivery_fence import Phase9DeliveryFence, Phase9DeliveryFenceError
from factory_core import phase9_forensic_replay as replay
from factory_core.project_snapshot_v0 import (
    PROJECT_SNAPSHOT_V0_SCHEMA,
    SNAPSHOT_COORDINATE_SCHEMA,
    ProjectSnapshotV0,
    SnapshotAvailabilityV0,
    SnapshotCompletenessV0,
    SnapshotCoordinateV0,
    SnapshotErrorCodeV0,
    SnapshotSectionIdV0,
    SnapshotSectionV0,
    SnapshotV0ValidationError,
    build_project_snapshot_v0,
    validate_project_snapshot_v0,
)


def _packet(*, present: tuple[str, ...] = ("claim-a", "claim-b")) -> bytes:
    return canonical_bytes(
        {
            "schema": replay.PHASE9_PACKET_PAYLOAD_SCHEMA,
            "rebuild_start": replay.RESUME_TARGET,
            "required_claims": ["claim-a", "claim-b"],
            "claims": [
                {
                    "claim_id": claim,
                    "content_sha256": hashlib.sha256(
                        ("content:" + claim).encode("utf-8")
                    ).hexdigest(),
                }
                for claim in present
            ],
        }
    )


def _request(*, run_generation: str = "run-generation:new"):
    descriptors = tuple(
        replay.ReplayEvidenceFileV1(path, 1, "f" * 64)
        for path in sorted(
            {
                "acceptance.json",
                "entry_gate.json",
                "outbox_supervisor.json",
                "packet.json",
                "roles.json",
                "snapshot.json",
                "start_authorization.json",
                "verdict.json",
            }
        )
    )
    return replay.Phase9ForensicReplayRequestV1(
        schema_version=replay.PHASE9_REPLAY_REQUEST_SCHEMA,
        idempotency_key="phase9-probe-request",
        operation_kind=replay.CREATE,
        project_id="probe-project",
        workflow_id="probe-workflow",
        project_revision=7,
        project_generation="project-generation:new",
        run_generation=run_generation,
        run_generation_creation_receipt_sha256="1" * 64,
        predecessor_replay_id=None,
        predecessor_terminal_receipt_sha256=None,
        replay_mode=replay.TECHNICAL,
        requested_resume_target=replay.RESUME_TARGET,
        delivery_capability=replay.DELIVERY_DISABLED,
        source_commit="2" * 40,
        source_tree="3" * 40,
        source_parent="4" * 40,
        source_inventory_sha256="5" * 64,
        entry_gate_result_sha256="6" * 64,
        evidence_files=descriptors,
        occurred_at=2_200,
    )


def _probe_receipt(kind: str, logical_id: str):
    slug = (kind + "-" + logical_id).lower().replace("_", "-")
    body = {
        "receipt_id": "probe-receipt:" + slug,
        "invocation_id": "probe-invocation:" + slug,
        "attempt_id": "probe-attempt:" + slug,
        "process_scope_id": "probe-scope:" + slug,
    }
    raw = canonical_bytes(body)
    return replay.ValidatedEvidenceReceiptV1(
        receipt_kind=kind,
        logical_id=logical_id,
        logical_path=f"probe/{slug}.json",
        byte_length=len(raw),
        raw_bytes_sha256=hashlib.sha256(raw).hexdigest(),
        receipt_json=raw.decode("utf-8"),
        receipt_sha256=canonical_sha256(body),
        occurred_at=2_200,
    )


def _evaluation_values(
    monkeypatch: pytest.MonkeyPatch,
    *,
    request=None,
) -> tuple[replay.Phase9ForensicReplayRequestV1, dict[str, bytes]]:
    """Build controls while stubbing only unrelated typed-receipt I/O."""

    request = _request() if request is None else request
    packet_raw = _packet()
    packet_sha256 = hashlib.sha256(packet_raw).hexdigest()
    values: dict[str, bytes] = {"payload/packet.bin": packet_raw}
    role_values = []
    role_layers = {}
    for role_name in ("execution", "math", "paper"):
        output_path = f"roles/{role_name}.out"
        output = (role_name + " newly generated output\n").encode("utf-8")
        values[output_path] = output
        role_values.append(
            {
                "role": role_name,
                "role_generation": f"role-generation:{role_name}:new",
                "inherited": False,
                "packet_sha256": packet_sha256,
                "output_path": output_path,
                "output_sha256": hashlib.sha256(output).hexdigest(),
                "process_receipt": {},
            }
        )
        role_layers[role_name] = {
            "raw": "PASS",
            "protocol": "PASS",
            "grounding": "PASS",
            "effective": "PASS",
        }
    coordinate = {
        "project_id": request.project_id,
        "project_revision": request.project_revision,
        "run_generation": request.run_generation,
    }
    controls = {
        "entry_gate.json": {"schema": replay.PHASE9_ENTRY_GATE_SCHEMA},
        "packet.json": {
            "schema": "authority-phase9-packet-evidence-v1",
            "required_claims": ["claim-a", "claim-b"],
            "present_claims": ["claim-a", "claim-b"],
            "packet_path": "payload/packet.bin",
            "packet_sha256": packet_sha256,
            "dispatch_count": 0,
        },
        "roles.json": {
            "schema": replay.PHASE9_ROLE_EVIDENCE_SCHEMA,
            "roles": role_values,
        },
        "verdict.json": {
            "schema": "authority-phase9-verdict-evidence-v1",
            "roles": role_layers,
            "effective_verdict": "PASS",
            "exit_code": 0,
        },
        "snapshot.json": {
            "schema": "authority-phase9-snapshot-evidence-v1",
            "coordinate": coordinate,
            "sections": [
                {
                    "section": "authority",
                    "coordinate": coordinate,
                    "read_failed": False,
                    "read_status": "AVAILABLE",
                }
            ],
        },
        "outbox_supervisor.json": {
            "schema": replay.PHASE9_RUNTIME_EVIDENCE_SCHEMA,
            "precommit_external_launch_count": 0,
            "committed_reclaim_count": 0,
            "pending_outbox_count": 0,
            "uncertain_automatic_resend_count": 0,
            "active_descendant_count": 0,
            "process_scope_receipts": {
                "failed": {},
                "kill": {},
                "pause": {},
            },
        },
        "acceptance.json": {
            "schema": replay.PHASE9_ACCEPTANCE_EVIDENCE_SCHEMA,
            "cases": [
                {"case_id": case, "result": "PASS", "receipt": {}}
                for case in replay.PHASE9_ACCEPTANCE_CASES
            ],
            "delivery": {
                "delivery_capability": "DISABLED",
                "release_created": False,
                "final_acceptance_created": False,
                "final_submission_created": False,
                "reusable": False,
                "delivery_override_applied": False,
            },
            "terminal": {
                "terminal_reason": "FORENSIC_REPLAY_COMPLETED",
                "requested_resume_target": replay.RESUME_TARGET,
                "effective_verdict": "PASS",
                "exit_code": 0,
            },
        },
    }
    values.update({name: canonical_bytes(body) for name, body in controls.items()})

    monkeypatch.setattr(
        replay,
        "_verify_entry_gate",
        lambda _body, _request, *, trusted_now: ("e" * 64, trusted_now),
    )

    def role_receipts(_value, *, role: str, **_kwargs):
        return (
            _probe_receipt("ROLE_PROCESS", role),
            _probe_receipt("ROLE_PROVIDER", role),
        )

    monkeypatch.setattr(replay, "_validate_role_process_receipt", role_receipts)
    monkeypatch.setattr(
        replay,
        "_validate_process_scope_receipt",
        lambda _value, *, action_name, **_kwargs: _probe_receipt(
            "PROCESS_SCOPE", action_name
        ),
    )
    monkeypatch.setattr(
        replay,
        "_validate_acceptance_case_receipt",
        lambda _value, *, case_id, **_kwargs: (
            _probe_receipt("ACCEPTANCE_CASE", case_id),
            {"case_id": case_id},
        ),
    )
    return request, values


def _control(values: dict[str, bytes], name: str) -> dict[str, object]:
    return json.loads(values[name].decode("utf-8"))


def _set_control(values: dict[str, bytes], name: str, body: dict[str, object]) -> None:
    values[name] = canonical_bytes(body)


def _evaluate(request, values):
    return replay._evaluate_evidence(
        request,
        values,
        trusted_now=request.occurred_at,
        require_start_authorization=False,
        require_component_receipts=False,
    )


def _delivery_fence(replay_mode: str) -> Phase9DeliveryFence:
    return Phase9DeliveryFence(
        project_id="probe-project",
        workflow_id="probe-workflow",
        run_generation="run-generation:new",
        replay_id="phase9-replay:new",
        replay_mode=replay_mode,
        terminal_receipt_sha256="7" * 64,
        run_mode="FORENSIC_REPLAY",
        modeling_consultation_contract="LEGACY_NOT_APPLICABLE",
        delivery_capability="DISABLED",
    )


def _assert_delivery_side_effects_blocked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    replay_mode: str,
) -> None:
    project = tmp_path / "probe-project"
    project.mkdir()
    papers = tmp_path / "papers"
    collector_calls = 0

    def collect(*_args, **_kwargs):
        nonlocal collector_calls
        collector_calls += 1
        return _delivery_fence(replay_mode)

    monkeypatch.setattr(
        "factory_core.phase9_delivery_fence.collect_phase9_delivery_fence",
        collect,
    )
    monkeypatch.setattr(
        "factory_core.phase9_delivery_fence._current_phase9_generation",
        lambda _project: _delivery_fence(replay_mode),
    )
    snapshot = AuditSnapshot(
        snapshot_id="8" * 64,
        base=project.name,
        profile="final",
        created_at="2026-09-02T00:00:00+00:00",
        identity={"source": "formal"},
    )
    package_calls = 0

    def package_builder(_output: Path) -> bool:
        nonlocal package_calls
        package_calls += 1
        return True

    class NeverCalled:
        def __getattr__(self, name):
            raise AssertionError(f"delivery dependency was reached: {name}")

    attempts = (
        lambda: build_final_acceptance_receipt(
            project,
            snapshot,
            status="PASS",
            workflow_id="probe-workflow",
            run_generation="run-generation:new",
        ),
        lambda: ReleasePublisher(papers).publish(
            project,
            snapshot.snapshot_id,
            status="PASS",
            package_builder=package_builder,
            workflow_id="probe-workflow",
            run_generation="run-generation:new",
        ),
        lambda: FinalAuditService(
            tmp_path,
            NeverCalled(),
            NeverCalled(),
            NeverCalled(),
            fingerprinter=lambda *_args: "8" * 64,
            override_provider=NeverCalled(),
        ).run(
            StepContext(project, project.name, 16, 1, 60, 0),
            analysis_only=False,
            workflow_id="probe-workflow",
            run_generation="run-generation:new",
        ),
    )
    for attempt in attempts:
        with pytest.raises(Phase9DeliveryFenceError, match="delivery DISABLED"):
            attempt()
    assert collector_calls == len(attempts)
    assert package_calls == 0
    assert list(project.iterdir()) == []
    assert not papers.exists()


def _operation_identity(label: str = "probe"):
    return build_worker_launch_identity(
        outbox_command_id=f"command-{label}",
        invocation_id=f"invocation-{label}",
        attempt_id=f"attempt-{label}",
        process_scope_id=f"scope-{label}",
        payload_sha256=hashlib.sha256(label.encode("utf-8")).hexdigest(),
    )


def _reserved_and_claimed(tmp_path: Path, label: str = "probe"):
    store = Phase4ShadowStore(tmp_path / f"{label}.sqlite")
    store.initialize()
    identity = _operation_identity(label)
    store.reserve_operation(identity, occurred_at=1)
    claimed = store.claim_operation(
        identity.identity_sha256,
        request_idempotency_key=f"claim-{label}",
        claim_owner_id=f"worker-{label}",
        claim_owner_epoch=1,
        expected_claim_generation=0,
        occurred_at=10,
        lease_seconds=5,
    )
    return store, identity, claimed


def test_ac_del_001(monkeypatch, tmp_path):
    _assert_delivery_side_effects_blocked(monkeypatch, tmp_path, replay.TECHNICAL)


def test_ac_del_002(monkeypatch, tmp_path):
    _assert_delivery_side_effects_blocked(monkeypatch, tmp_path, replay.ABLATE_NO_JUDGE)


def test_ac_out_001(monkeypatch, tmp_path):
    store = Phase4ShadowStore(tmp_path / "precommit.sqlite")
    store.initialize()
    before = store.table_counts()

    def fail_after_intent(stage: str) -> None:
        if stage == "after_intent":
            raise RuntimeError("synthetic precommit failure")

    monkeypatch.setattr(shadow_runtime, "_phase4_failure_point", fail_after_intent)
    identity = _operation_identity("precommit")
    with pytest.raises(RuntimeError, match="synthetic precommit failure"):
        store.reserve_operation(identity, occurred_at=1)
    assert store.table_counts() == before
    with pytest.raises(Phase4ShadowFenceError, match="unavailable"):
        store.load(identity.identity_sha256)


def test_ac_out_002(tmp_path):
    store, identity, _claimed = _reserved_and_claimed(tmp_path, "reclaim")
    restarted = Phase4ShadowStore(store.path)
    restarted.initialize()
    reclaimed = restarted.claim_operation(
        identity.identity_sha256,
        request_idempotency_key="reclaim-once",
        claim_owner_id="worker-restarted",
        claim_owner_epoch=2,
        expected_claim_generation=1,
        occurred_at=15,
        lease_seconds=5,
    )
    counts = restarted.table_counts()
    replayed = restarted.claim_operation(
        identity.identity_sha256,
        request_idempotency_key="reclaim-once",
        claim_owner_id="worker-restarted",
        claim_owner_epoch=2,
        expected_claim_generation=1,
        occurred_at=15,
        lease_seconds=5,
    )
    assert reclaimed.state.operation.claim_generation == 2
    assert reclaimed.state.retry_count == 1
    assert replayed.replayed is True
    assert replayed.receipt == reclaimed.receipt
    assert restarted.table_counts() == counts
    with pytest.raises(Phase4ShadowFenceError, match="claim generation is stale"):
        restarted.claim_operation(
            identity.identity_sha256,
            request_idempotency_key="reclaim-twice",
            claim_owner_id="worker-third",
            claim_owner_epoch=3,
            expected_claim_generation=1,
            occurred_at=20,
            lease_seconds=5,
        )
    assert restarted.table_counts() == counts


def test_ac_out_004(tmp_path):
    store, identity, claimed = _reserved_and_claimed(tmp_path, "uncertain")
    owner = "worker-uncertain"
    checkpoint = store.transition(
        identity.identity_sha256,
        OperationEvent.CHECKPOINT_DISPATCH,
        request_idempotency_key="checkpoint-once",
        expected_claim_generation=claimed.state.operation.claim_generation,
        claim_owner_id=owner,
        claim_owner_epoch=1,
        dispatch_nonce="dispatch-uncertain",
        reason_code="DISPATCH_INTENT_DURABLE",
        occurred_at=11,
    )
    uncertain = store.transition(
        identity.identity_sha256,
        OperationEvent.MARK_DISPATCH_UNCERTAIN,
        request_idempotency_key="mark-uncertain",
        expected_claim_generation=1,
        claim_owner_id=owner,
        claim_owner_epoch=1,
        dispatch_nonce="dispatch-uncertain",
        reason_code="ACK_LOST",
        occurred_at=12,
    )
    assert uncertain.state.operation.status is OperationStatus.DISPATCH_UNCERTAIN
    old_checkpoint = store.transition(
        identity.identity_sha256,
        OperationEvent.CHECKPOINT_DISPATCH,
        request_idempotency_key="checkpoint-once",
        expected_claim_generation=1,
        claim_owner_id=owner,
        claim_owner_epoch=1,
        dispatch_nonce="dispatch-uncertain",
        reason_code="DISPATCH_INTENT_DURABLE",
        occurred_at=11,
    )
    assert old_checkpoint.replayed is True
    assert old_checkpoint.receipt == checkpoint.receipt
    assert store.load(identity.identity_sha256).operation.status is OperationStatus.DISPATCH_UNCERTAIN
    with pytest.raises(InvalidOperationTransition, match="not allowed"):
        store.transition(
            identity.identity_sha256,
            OperationEvent.CHECKPOINT_DISPATCH,
            request_idempotency_key="forbidden-resend",
            expected_claim_generation=1,
            claim_owner_id=owner,
            claim_owner_epoch=1,
            dispatch_nonce="dispatch-resend",
            reason_code="AUTOMATIC_RESEND",
            occurred_at=13,
        )
    required = store.transition(
        identity.identity_sha256,
        OperationEvent.REQUIRE_RECONCILIATION,
        request_idempotency_key="reconcile-required",
        expected_claim_generation=1,
        claim_owner_id=owner,
        claim_owner_epoch=1,
        dispatch_nonce="dispatch-uncertain",
        reason_code="LOOKUP_REQUIRED",
        occurred_at=13,
    )
    reconciled = store.transition(
        identity.identity_sha256,
        OperationEvent.RECONCILE_ACTIVE,
        request_idempotency_key="reconcile-existing",
        expected_claim_generation=1,
        claim_owner_id=owner,
        claim_owner_epoch=1,
        dispatch_nonce="dispatch-uncertain",
        reason_code="EXISTING_PROCESS_FOUND",
        occurred_at=14,
    )
    assert required.state.operation.status is OperationStatus.RECONCILIATION_REQUIRED
    assert reconciled.state.operation.status is OperationStatus.ACTIVE


def test_ac_packet_001(monkeypatch):
    request, values = _evaluation_values(monkeypatch)
    assert _evaluate(request, values)["blockers"] == []
    values["payload/packet.bin"] += b"\n"
    with pytest.raises(replay.Phase9ForensicReplaySafetyError, match="bytes binding"):
        _evaluate(request, values)


def test_ac_packet_002(monkeypatch):
    request, values = _evaluation_values(monkeypatch)
    raw = _packet(present=("claim-a",))
    values["payload/packet.bin"] = raw
    packet = _control(values, "packet.json")
    packet["present_claims"] = ["claim-a"]
    packet["packet_sha256"] = hashlib.sha256(raw).hexdigest()
    packet["dispatch_count"] = 0
    _set_control(values, "packet.json", packet)
    roles = _control(values, "roles.json")
    for role in roles["roles"]:
        role["packet_sha256"] = packet["packet_sha256"]
    _set_control(values, "roles.json", roles)
    assert _evaluate(request, values)["blockers"] == [
        {"code": "MISSING_PACKET_CLAIMS", "detail": "claim-b"}
    ]
    packet["dispatch_count"] = 1
    _set_control(values, "packet.json", packet)
    assert [item["code"] for item in _evaluate(request, values)["blockers"]] == [
        "DISPATCH_WITH_MISSING_CLAIMS",
        "MISSING_PACKET_CLAIMS",
    ]


def test_ac_packet_003():
    request = _request()
    first_input = hashlib.sha256(b"packet revision one").hexdigest()
    second_input = hashlib.sha256(b"packet revision two").hexdigest()
    first = replay._dependency_fingerprint_sha256(
        request, receipt_kind="PACKET", logical_id="step13", input_sha256=first_input
    )
    second = replay._dependency_fingerprint_sha256(
        request, receipt_kind="PACKET", logical_id="step13", input_sha256=second_input
    )
    assert first != second
    body = {
        "producer": {
            "schema": replay.PHASE9_EVIDENCE_PRODUCER_SCHEMA,
            "execution_domain": "FORMAL_PHASE9_A",
            "component": "packet-rebuilder",
            "component_version": "2",
            "source_commit": request.source_commit,
            "source_tree": request.source_tree,
            "source_parent": request.source_parent,
            "source_inventory_sha256": request.source_inventory_sha256,
        },
        "replay_coordinate_sha256": replay._replay_coordinate_sha256(request),
        "source_run_generation": request.run_generation,
        "dependency_fingerprint_sha256": first,
        "event_id": replay._evidence_event_id(
            receipt_kind="PACKET",
            logical_id="step13",
            dependency_fingerprint_sha256=first,
        ),
        "event_sequence": 1,
        "predecessor_event_id": None,
        "predecessor_receipt_sha256": None,
        "input_sha256": first_input,
    }
    replay._validate_provenance(
        body,
        request=request,
        receipt_kind="PACKET",
        logical_id="step13",
        component="packet-rebuilder",
        input_sha256=first_input,
        event_sequence=1,
        predecessor_event_id=None,
        predecessor_receipt_sha256=None,
        path="packet",
    )
    with pytest.raises(replay.Phase9ForensicReplaySafetyError, match="provenance"):
        replay._validate_provenance(
            body,
            request=request,
            receipt_kind="PACKET",
            logical_id="step13",
            component="packet-rebuilder",
            input_sha256=second_input,
            event_sequence=1,
            predecessor_event_id=None,
            predecessor_receipt_sha256=None,
            path="packet",
        )


def test_ac_run4_001():
    request = _request()
    assert replay.validate_phase9_forensic_replay_request(request) is request
    assert request.replay_id != _request(run_generation="run-generation:old").replay_id
    with pytest.raises(replay.Phase9ForensicReplaySafetyError, match="Step 13"):
        replay.validate_phase9_forensic_replay_request(
            replace(request, requested_resume_target="STEP12_LEGACY_REPLAY")
        )


def test_ac_run4_002(monkeypatch):
    request, values = _evaluation_values(monkeypatch)
    roles = _control(values, "roles.json")
    roles["roles"][0]["inherited"] = True
    _set_control(values, "roles.json", roles)
    with pytest.raises(
        replay.Phase9ForensicReplaySafetyError,
        match="role generations must be new",
    ):
        _evaluate(request, values)


def _snapshot_coordinate(*, revision: int = 7) -> SnapshotCoordinateV0:
    pins = ContractPinSetV1(
        CONTRACT_PIN_SET_SCHEMA,
        *(character * 64 for character in "12345678"),
    )
    return SnapshotCoordinateV0(
        schema_version=SNAPSHOT_COORDINATE_SCHEMA,
        project_id="probe-project",
        workflow_schema_version=2,
        project_revision=revision,
        project_generation="project-generation:new",
        run_generation="run-generation:new",
        runtime_generation="runtime-v1",
        scheduler_generation="scheduler-v1",
        recorded_contract_pin_set_sha256=canonical_sha256(pins),
    )


def test_ac_snap_001():
    coordinate = _snapshot_coordinate()
    pins = ContractPinSetV1(
        CONTRACT_PIN_SET_SCHEMA,
        *(character * 64 for character in "12345678"),
    )
    sections = tuple(
        SnapshotSectionV0(
            section_id=section,
            availability=SnapshotAvailabilityV0.AVAILABLE,
            coordinate=coordinate,
            facts=(),
            error_code=None,
            gap_id=None,
            policy_id=None,
            page_cursor=None,
        )
        for section in SnapshotSectionIdV0
    )
    snapshot = ProjectSnapshotV0(
        schema_version=PROJECT_SNAPSHOT_V0_SCHEMA,
        coordinate=coordinate,
        completeness=SnapshotCompletenessV0.COMPLETE,
        sections=sections,
        contract_pins=pins,
        authoritative=False,
        performed_workflow_side_effects=(),
        application_initiated_write_operations=(),
    )
    assert validate_project_snapshot_v0(snapshot) is snapshot
    changed = replace(
        snapshot,
        sections=(
            replace(sections[0], coordinate=_snapshot_coordinate(revision=8)),
            *sections[1:],
        ),
    )
    with pytest.raises(SnapshotV0ValidationError, match="one coordinate"):
        validate_project_snapshot_v0(changed)


def test_ac_snap_002(tmp_path):
    directory_instead_of_database = tmp_path / "authority.sqlite"
    directory_instead_of_database.mkdir()
    result = build_project_snapshot_v0(directory_instead_of_database)
    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.DB_PATH_NOT_REGULAR
    assert result.snapshot is None
    assert list(directory_instead_of_database.iterdir()) == []


def test_ac_sup_001():
    original = _operation_identity("identity")
    other_attempt = build_worker_launch_identity(
        outbox_command_id=original.outbox_command_id,
        invocation_id=original.invocation_id,
        attempt_id="attempt-other",
        process_scope_id=original.process_scope_id,
        payload_sha256=original.payload_sha256,
    )
    other_scope = build_worker_launch_identity(
        outbox_command_id=original.outbox_command_id,
        invocation_id=original.invocation_id,
        attempt_id=original.attempt_id,
        process_scope_id="scope-other",
        payload_sha256=original.payload_sha256,
    )
    assert len(
        {original.identity_sha256, other_attempt.identity_sha256, other_scope.identity_sha256}
    ) == 3
    assert other_attempt.idempotency_key != original.idempotency_key
    assert other_scope.idempotency_key == original.idempotency_key


def test_ac_sup_002():
    ordinary = decide_pause_action(PauseMode.PAUSE, ProcessScopeKind.DURABLE_SOLVER)
    explicit = decide_pause_action(
        PauseMode.PAUSE_AND_CANCEL_SOLVERS, ProcessScopeKind.DURABLE_SOLVER
    )
    attached = decide_pause_action(PauseMode.PAUSE, ProcessScopeKind.ATTACHED_SOLVER)
    assert ordinary.action is PauseAction.CONTINUE
    assert explicit.action is PauseAction.REQUEST_CANCEL
    assert attached.action is PauseAction.TERMINATE_SCOPE


def test_ac_sup_004(monkeypatch):
    request, values = _evaluation_values(monkeypatch)
    runtime = _control(values, "outbox_supervisor.json")
    runtime["active_descendant_count"] = 1
    _set_control(values, "outbox_supervisor.json", runtime)
    assert _evaluate(request, values)["blockers"] == [
        {"code": "ACTIVE_DESCENDANT_COUNT", "detail": "expected 0"}
    ]


def test_ac_verdict_001(monkeypatch):
    request, values = _evaluation_values(monkeypatch)
    verdict = _control(values, "verdict.json")
    verdict["roles"]["math"] = {
        "raw": "PASS",
        "protocol": "FAIL",
        "grounding": "PASS",
        "effective": "FAIL",
    }
    verdict["effective_verdict"] = "FAIL"
    _set_control(values, "verdict.json", verdict)
    acceptance = _control(values, "acceptance.json")
    acceptance["terminal"]["effective_verdict"] = "FAIL"
    _set_control(values, "acceptance.json", acceptance)
    result = _evaluate(request, values)
    assert result["effective_verdict"] == "FAIL"
    assert result["terminal_reason"] == "FORENSIC_REPLAY_COMPLETED"


def test_ac_verdict_003(monkeypatch):
    request, values = _evaluation_values(monkeypatch)
    verdict = _control(values, "verdict.json")
    verdict["roles"]["math"] = {
        "raw": "PASS",
        "protocol": "FAIL",
        "grounding": "PASS",
        "effective": "FAIL",
    }
    verdict["effective_verdict"] = "PASS"
    _set_control(values, "verdict.json", verdict)
    with pytest.raises(
        replay.Phase9ForensicReplaySafetyError,
        match="contradictory aggregate verdict",
    ):
        _evaluate(request, values)
