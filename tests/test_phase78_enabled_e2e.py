from __future__ import annotations

import base64
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from factory_core.artifact_ownership import ArtifactOwnership
from factory_core.adapters.infrastructure.pause_policy import (
    PauseMode,
    ProcessScopeKind,
)
from factory_core.authority_read_repository import AuthorityReadRepository
from factory_core.canonical import canonical_sha256
from factory_core.owner_compiler import compile_owner_registry
from factory_core.phase3_artifacts import (
    CheckpointState,
    CheckpointTransition,
    build_artifact_manifest,
    build_artifact_record,
    build_checkpoint_entry,
    build_phase3_mutation,
    build_phase3_previous_head_bootstrap,
    build_reopen_plan,
    compute_change_set,
    owner_compilation_semantic_sha256,
    register_artifact_owner,
)
from factory_core.phase6_snapshot_grants import (
    GrantScope,
    Phase6SnapshotGrantStore,
    SectionAvailability,
    VerifiedSection,
    build_authority_source_binding,
)
from factory_core.phase6_source_assembler import Phase6TrustedSourceAssembler
from factory_core.durable_operation import OperationEvent
from factory_core.phase4_shadow_runtime import Phase4ShadowStore
from factory_core.phase5_shadow_supervisor import (
    Phase5SupervisorStore,
    SyntheticEffectObservation,
    SyntheticObservationOutcome,
    run_phase5_full_shadow,
)
from factory_core.phase78_config import Phase78Settings
from factory_core.phase78_current import (
    Phase78CurrentHeadError,
)
from factory_core.phase78_deadline import (
    Phase78CancellationError,
    Phase78CancellationReason,
    Phase78DeadlineError,
    Phase78OutcomeUncertain,
    TotalDeadline,
)
from factory_core.phase78_operator import (
    Phase78OperatorError,
    prepare_phase78_trusted_preflight,
)
from factory_core.phase78_service import (
    Phase78RequestError,
    cancel_phase78_request,
    load_phase78_status,
    parse_phase78_request,
    revoke_phase78_approval,
    run_phase78_worker_once,
    submit_phase78_request,
)
from factory_core.phase78_scheduler import (
    Phase78SchedulerTerminalConflict,
    Phase78ShadowScheduler,
)
from factory_core.phase78_work_ledger import (
    Phase78WorkIdempotencyConflict,
    Phase78WorkLedger,
)
import factory_core.phase78_worker as phase78_worker
from factory_core.phase8_evidence_egress_runtime import (
    Phase8CurrentConflict,
    Phase8NotFound,
)
from factory_core.reference_materializer import ReferenceMaterializationError
from tests.support.authority_production import (
    bundle,
    configure_canary,
    install_foundation,
)
from tests.test_phase8_reference_materializer import make_pdf


def _authorization_headers(runtime_token: str) -> dict[str, str]:
    """Build the HTTP scheme only from a runtime-issued access token."""

    return {"Authorization": f"Bearer {runtime_token}"}


def _authority_pdf(tmp_path: Path, raw_pdf: bytes):
    fixture = install_foundation(tmp_path)
    # Create the concrete generation through the atomic audited API before the
    # canary writer is enabled.  Direct SQL completion and invented generation
    # labels are deliberately forbidden by the production contract.
    from tests.test_phase9_run_generation import _request as generation_request
    from tests.test_phase9_run_generation import _service as generation_service

    generation_input = generation_request(key=f"generation-{tmp_path.name}")
    generation = generation_service(
        fixture,
        request=generation_input,
    ).create_or_rotate(generation_input)
    writer = configure_canary(fixture)
    compilation = compile_owner_registry(
        (
            ArtifactOwnership(
                pattern="references/**",
                owner_stage=4,
                semantic_domain="canonical_reference",
                dirty_flag="REFERENCE_DIRTY",
            ),
        )
    )
    registration = register_artifact_owner(compilation, "references/source.pdf")
    record = build_artifact_record(registration, content=raw_pdf)
    owner_sha = owner_compilation_semantic_sha256(compilation)
    previous = build_artifact_manifest(
        owner_compilation_sha256=owner_sha,
        records=(),
    )
    current = build_artifact_manifest(
        owner_compilation_sha256=owner_sha,
        records=(record,),
    )
    changes = compute_change_set(previous, current)
    checkpoint = build_checkpoint_entry(
        checkpoint_key="phase3:stage4.references",
        owner_stage=4,
        input_manifest_sha256=current.manifest_sha256,
        state=CheckpointState.VALID,
        transition=CheckpointTransition.RECORDED_VALID,
        validation_sha256="9" * 64,
        previous_checkpoint_id=None,
        previous_checkpoint_occurrence_id=None,
        reason_code="INITIAL_ATTESTATION",
    )
    mutation = build_phase3_mutation(
        artifact_records=current.records,
        artifact_blockers=(),
        removals=changes.removals,
        checkpoint_entries=(checkpoint,),
        reopen_plan=build_reopen_plan(
            workflow_id="legacy_current",
            source_revision=1,
            change_set=changes,
            previous_manifest=previous,
            previous_occurrence_ids={},
        ),
        previous_manifest=previous,
        current_manifest=current,
        change_set=changes,
        blocked_disposition=None,
        previous_head=build_phase3_previous_head_bootstrap(
            workflow_id="legacy_current",
            source_revision=1,
            previous_manifest=previous,
        ),
    )
    command, event, receipt, outbox = bundle(
        requested_revision=1, suffix="phase78-e2e"
    )
    command = replace(
        command,
        project_binding=replace(
            command.project_binding,
            project_generation=generation_input.project_generation,
        ),
        run_binding=replace(
            command.run_binding,
            runtime_generation=generation_input.runtime_generation,
            scheduler_generation=generation_input.scheduler_generation,
            run_generation=generation.run_generation,
        ),
        contract_pins=generation_input.contract_pins,
    )
    generation_pin_sha256 = canonical_sha256(generation_input.contract_pins)
    event = replace(
        event,
        project_generation=command.project_binding.project_generation,
        runtime_generation=command.run_binding.runtime_generation,
        scheduler_generation=command.run_binding.scheduler_generation,
        run_generation=command.run_binding.run_generation,
        contract_pin_set_sha256=generation_pin_sha256,
    )
    receipt = replace(
        receipt,
        contract_pin_set_sha256=generation_pin_sha256,
    )
    committed = writer.persist_command_bundle(
        workflow_id="legacy_current",
        idempotency_key="idempotency-phase78-e2e",
        command=command,
        event=event,
        receipt=receipt,
        outbox=outbox,
        occurred_at=2000,
        phase3_mutation=mutation,
    )
    repository = AuthorityReadRepository(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    coordinate = repository.workflow_coordinate("legacy_current")
    state = repository.phase3_artifact_state(
        "legacy_current", through_revision=coordinate.current_revision
    )
    assert committed.revision == coordinate.current_revision
    assert len(state.occurrences) == 1
    source = fixture.project_dir / "references" / "source.pdf"
    source.parent.mkdir(mode=0o700, exist_ok=True)
    source.write_bytes(raw_pdf)
    return fixture, coordinate, state, state.occurrences[0], source


class _Phase5ObservationPort:
    def record_would_apply(self, *, request_id, binding, decision):
        del request_id, binding, decision
        return SyntheticEffectObservation(
            observation_id="phase78-source-observation",
            outcome=SyntheticObservationOutcome.CONFIRMED_APPLIED,
            observed_at=16,
            evidence_sha256="8" * 64,
        )


def _phase6_proof(tmp_path: Path, coordinate, state):
    phase6_path = tmp_path / "phase6.db"
    phase4_path = (tmp_path / "phase4-source.db").resolve()
    phase5_path = (tmp_path / "phase5-source.db").resolve()
    store = Phase6SnapshotGrantStore(phase6_path)
    store.initialize()
    assembler = Phase6TrustedSourceAssembler(
        authority_database=(
            tmp_path / "authority-project" / ".factory" / "state.db"
        ),
        authority_source_fence_sha256=coordinate.source_fence_sha256,
        phase4_database=phase4_path,
        phase5_database=phase5_path,
        phase6_store=store,
    )
    Phase4ShadowStore(phase4_path).initialize()
    Phase5SupervisorStore(phase5_path).initialize()
    operation = assembler.produce_phase4_operation(
        workflow_id=coordinate.workflow_id,
        occurrence_id=state.occurrences[0].occurrence_id,
        invocation_id="phase6-source-invocation",
        attempt_id="phase6-source-attempt",
        process_scope_id="phase6-source-scope",
        occurred_at=10,
    )
    phase4 = Phase4ShadowStore(phase4_path)
    claimed = phase4.claim_operation(
        operation.state.operation.identity.identity_sha256,
        request_idempotency_key="phase6-source-claim",
        claim_owner_id="phase6-source-owner",
        claim_owner_epoch=1,
        expected_claim_generation=0,
        occurred_at=11,
        lease_seconds=30,
    )
    checkpoint = phase4.transition(
        operation.state.operation.identity.identity_sha256,
        OperationEvent.CHECKPOINT_DISPATCH,
        request_idempotency_key="phase6-source-checkpoint",
        expected_claim_generation=claimed.state.operation.claim_generation,
        claim_owner_id="phase6-source-owner",
        claim_owner_epoch=1,
        dispatch_nonce="phase6-source-nonce",
        reason_code="SHADOW_NO_DISPATCH",
        occurred_at=12,
    )
    active = phase4.transition(
        operation.state.operation.identity.identity_sha256,
        OperationEvent.CONFIRM_ACTIVE,
        request_idempotency_key="phase6-source-active",
        expected_claim_generation=checkpoint.state.operation.claim_generation,
        claim_owner_id="phase6-source-owner",
        claim_owner_epoch=1,
        dispatch_nonce="phase6-source-nonce",
        reason_code="SHADOW_ACTIVE",
        occurred_at=13,
    )
    phase5_binding = assembler.phase5_binding_from_current_phase4(
        operation_identity_sha256=(
            operation.state.operation.identity.identity_sha256
        ),
        scope_kind=ProcessScopeKind.DURABLE_SOLVER,
    )
    phase5_run = run_phase5_full_shadow(
        enabled=True,
        database=phase5_path,
        binding=phase5_binding,
        mode=PauseMode.PAUSE,
        request_idempotency_key="phase6-source-supervisor",
        occurred_at=14,
        effect_port=_Phase5ObservationPort(),
    )
    succeeded = phase4.transition(
        operation.state.operation.identity.identity_sha256,
        OperationEvent.CONFIRM_SUCCEEDED,
        request_idempotency_key="phase6-source-success",
        expected_claim_generation=active.state.operation.claim_generation,
        claim_owner_id="phase6-source-owner",
        claim_owner_epoch=1,
        dispatch_nonce="phase6-source-nonce",
        reason_code="SHADOW_SUCCEEDED",
        occurred_at=17,
    )
    assert succeeded.state.operation.status.value == "succeeded"
    receipt = assembler.assemble(
        workflow_id=coordinate.workflow_id,
        occurrence_id=state.occurrences[0].occurrence_id,
        operation_identity_sha256=(
            operation.state.operation.identity.identity_sha256
        ),
        phase5_request_id=phase5_run.request_id,
    )
    source_coordinate = {
        "schema_version": "snapshot-coordinate-v0",
        "project_id": coordinate.project_id,
        "workflow_schema_version": 1,
        "project_revision": coordinate.current_revision,
        "project_generation": coordinate.project_generation,
        "run_generation": coordinate.run_generation,
        "runtime_generation": coordinate.runtime_generation,
        "scheduler_generation": coordinate.scheduler_generation,
        "recorded_contract_pin_set_sha256": coordinate.contract_pin_set_sha256,
    }
    binding = assembler.build_phase6_source_binding(
        receipt,
        source_snapshot_schema="project-snapshot-v0-source-authorized-v3",
        source_snapshot_semantic_sha256=hashlib.sha256(
            b"phase78-e2e-snapshot"
        ).hexdigest(),
        source_snapshot_completeness="COMPLETE",
        source_snapshot_coordinate=source_coordinate,
    )
    snapshot = store.append_snapshot(
        source_binding=binding,
        sections=(
            VerifiedSection(
                "reference",
                SectionAvailability.AVAILABLE,
                "1" * 64,
                "reference-section-v1",
            ),
        ),
        captured_at=10,
        valid_until=1000,
        expected_previous_snapshot_id=None,
        idempotency_key="phase6-snapshot-phase78-e2e",
    ).snapshot
    grant = store.issue_grant(
        snapshot_id=snapshot.snapshot_id,
        subject_type="user",
        subject_id="alice",
        subject_generation="membership-generation-1",
        scope=GrantScope.SNAPSHOT_VIEW,
        scope_key=None,
        issuer_id="phase6-local-shadow-issuer",
        issuer_generation="issuer-generation-1",
        issuer_evidence_schema="local-issuer-receipt-v1",
        issuer_receipt_sha256="2" * 64,
        issued_at=11,
        not_before=12,
        expires_at=900,
        expected_previous_grant_id=None,
        idempotency_key="phase6-grant-phase78-e2e",
    ).grant
    evaluated = store.evaluate_grant(
        grant.grant_id,
        subject_type="user",
        subject_id="alice",
        subject_generation="membership-generation-1",
        requested_scope=GrantScope.SNAPSHOT_VIEW,
        requested_scope_key=None,
        evaluated_at=12,
        idempotency_key="phase6-evaluation-phase78-e2e",
    )
    assert evaluated.access_proof is not None
    return store, evaluated.access_proof


def _packets():
    outputs: dict[str, str] = {}
    manifests: dict[str, str] = {}
    contexts: dict[str, str] = {}
    for role in ("math", "execution", "paper"):
        context = b""
        manifest = {
            "role": role,
            "files": [],
            "context": {
                "sha256": hashlib.sha256(context).hexdigest(),
                "size": 0,
            },
        }
        body = (
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
        output = f"VERDICT: PASS\n{json.dumps(body, sort_keys=True)}\n".encode()
        outputs[role] = base64.b64encode(output).decode("ascii")
        manifests[role] = base64.b64encode(
            json.dumps(manifest, sort_keys=True).encode()
        ).decode("ascii")
        contexts[role] = base64.b64encode(context).decode("ascii")
    return outputs, manifests, contexts


def _settings(fixture, tmp_path: Path, phase6_path: Path) -> Phase78Settings:
    runtime = tmp_path / "phase78-runtime"
    runtime.mkdir(mode=0o700)
    cas = runtime / "cas"
    scratch = runtime / "scratch"
    cas.mkdir(mode=0o700)
    scratch.mkdir(mode=0o700)
    return Phase78Settings(
        enabled=True,
        authority_database=fixture.database,
        authority_source_fence_sha256=fixture.preflight.source_fence_sha256,
        phase4_database=(tmp_path / "phase4-source.db").resolve(),
        phase5_database=(tmp_path / "phase5-source.db").resolve(),
        phase6_database=phase6_path,
        phase7_database=runtime / "phase7.db",
        phase8_database=runtime / "phase8.db",
        work_database=runtime / "work.db",
        work_spool=runtime / "work-spool",
        project_root=fixture.project_dir,
        cas_root=cas,
        scratch_root=scratch,
        # The trusted P1--8 rejoin deliberately revalidates six durable stores
        # at every publish fence; keep the E2E budget explicit and bounded.
        deadline_ms=120_000,
        lease_seconds=30,
    )


def _payload(state, occurrence, proof, raw_pdf: bytes):
    outputs, manifests, contexts = _packets()
    now = int(time.time())
    return {
        "schema_version": "phase78-pipeline-request-v1",
        "idempotency_key": "pipeline-normal-1",
        "workflow_id": "legacy_current",
        "phase3_artifact_state": state.as_dict(),
        "phase3_artifact_occurrence": occurrence.as_dict(),
        "phase6_access_proof": proof.as_dict(),
        "grounding": {
            "role_output_base64": outputs,
            "manifest_base64": manifests,
            "context_base64": contexts,
        },
        "reference": {
            "reference_id": "phase78-reference-1",
            "logical_id": "phase78-reference-binding-1",
            "pdf_path": "references/source.pdf",
            "bibliographic_metadata": {
                "title": "Phase 7+8 Normal Flow",
                "authors": ["Ada Example"],
                "published_year": 2026,
                "doi": None,
            },
            "external_share_classification": "internal",
            "expected_current_binding_sha256": None,
        },
        "approval": {
            "approval_id": "phase78-approval-1",
            "issuer_id": "trusted-operator-1",
            "issuer_generation": "operator-generation-1",
            "subject_id": "alice",
            "subject_generation": "membership-generation-1",
            "logical_issued_at": now - 2,
            "not_before": now - 1,
            "expires_at": now + 3600,
            "data_egress_request": {
                "schema_version": "data-egress-request-v1",
                "subject": "alice",
                "provider": "operator-download",
                "surface": "phase78-shadow-review",
                "account_scope": "demo",
                "retention": "operator-controlled",
                "purpose": "reference-review",
                "artifacts": [
                    {
                        "artifact_id": "reference-source-pdf",
                        "sha256": hashlib.sha256(raw_pdf).hexdigest(),
                        "byte_length": len(raw_pdf),
                        "transfer_form": "raw",
                        "classification": "internal",
                    }
                ],
            },
            "successor_of": None,
            "expected_predecessor_event_sha256": None,
            "trusted_preflight_sha256": "0" * 64,
            "expected_previous_decision_sha256": None,
        },
        "work": {
            "submitted_at": 1,
            "claimed_at": 2,
            "checkpointed_at": 3,
            "completed_at": 4,
        },
    }


def _register_operator_preflight(
    settings: Phase78Settings,
    payload: dict[str, object],
) -> dict[str, object]:
    """Call the real operator-only adapter, then attach only its receipt hash."""

    operator = str(payload["approval"]["issuer_id"])
    operator_generation = str(payload["approval"]["issuer_generation"])
    with patch.dict(
        os.environ,
        {
            "PHASE78_TRUSTED_OPERATOR_ID": operator,
            "PHASE78_TRUSTED_OPERATOR_GENERATION": operator_generation,
        },
        clear=False,
    ):
        result = prepare_phase78_trusted_preflight(
            settings,
            "demo",
            operator,
            payload,
        )
    payload["approval"]["trusted_preflight_sha256"] = result[
        "trusted_preflight_sha256"
    ]
    return result


def _phase78_environment(settings: Phase78Settings) -> dict[str, str]:
    return {
        "PHASE78_ENABLED": "true",
        "PHASE78_AUTHORITY_DB_FILE": str(settings.authority_database),
        "PHASE78_AUTHORITY_SOURCE_FENCE_SHA256": str(
            settings.authority_source_fence_sha256
        ),
        "PHASE78_PHASE4_DB_FILE": str(settings.phase4_database),
        "PHASE78_PHASE5_DB_FILE": str(settings.phase5_database),
        "PHASE78_PHASE6_DB_FILE": str(settings.phase6_database),
        "PHASE78_PHASE7_DB_FILE": str(settings.phase7_database),
        "PHASE78_PHASE8_DB_FILE": str(settings.phase8_database),
        "PHASE78_WORK_DB_FILE": str(settings.work_database),
        "PHASE78_WORK_SPOOL": str(settings.work_spool),
        "PHASE78_PROJECT_ROOT": str(settings.project_root),
        "PHASE78_CAS_ROOT": str(settings.cas_root),
        "PHASE78_SCRATCH_ROOT": str(settings.scratch_root),
        "PHASE78_DEADLINE_MS": str(settings.deadline_ms),
        "PHASE78_LEASE_SECONDS": str(settings.lease_seconds),
        "PHASE78_TRUSTED_OPERATOR_ID": "trusted-operator-1",
        "PHASE78_TRUSTED_OPERATOR_GENERATION": "operator-generation-1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def test_strict_request_parser_rejects_unknown_fields_and_noncanonical_base64(
    tmp_path: Path,
) -> None:
    del tmp_path
    minimal = {
        "schema_version": "phase78-pipeline-request-v1",
        "unexpected": True,
    }
    with pytest.raises(Phase78RequestError, match="fields must be exactly"):
        parse_phase78_request(minimal)

    packet = {role: "YQ==" for role in ("math", "execution", "paper")}
    complete = {
        "schema_version": "phase78-pipeline-request-v1",
        "idempotency_key": "parser-request-1",
        "workflow_id": "workflow-1",
        "phase3_artifact_state": {
            "schema": "authority-read-phase3-artifact-state-v1",
            "workflow_id": "workflow-1",
            "through_revision": 1,
            "occurrences": [],
            "state_sha256": "a" * 64,
        },
        "phase3_artifact_occurrence": {
            "schema_version": "authority-phase3-artifact-occurrence-v1",
            "occurrence_id": "occurrence-1",
            "workflow_id": "workflow-1",
            "revision": 1,
            "command_id": "command-1",
            "mutation_sha256": "b" * 64,
            "kind": "RECORD",
            "normalized_path": "references/source.pdf",
            "semantic_sha256": "c" * 64,
            "artifact_record": {},
            "blocker": None,
            "removal": None,
        },
        "phase6_access_proof": {},
        "grounding": {
            "role_output_base64": packet,
            "manifest_base64": packet,
            "context_base64": packet,
        },
        "reference": {
            "reference_id": "reference-1",
            "logical_id": "binding-1",
            "pdf_path": "references/source.pdf",
            "bibliographic_metadata": {},
            "external_share_classification": "internal",
            "expected_current_binding_sha256": None,
        },
        "approval": {
            "approval_id": "approval-1",
            "issuer_id": "alice",
            "issuer_generation": "issuer-generation-1",
            "subject_id": "alice",
            "subject_generation": "subject-generation-1",
            "logical_issued_at": 1,
            "not_before": 2,
            "expires_at": 4,
            "data_egress_request": {},
            "successor_of": None,
            "expected_predecessor_event_sha256": None,
            "trusted_preflight_sha256": "0" * 64,
            "expected_previous_decision_sha256": None,
        },
        "work": {
            "submitted_at": 1,
            "claimed_at": 2,
            "checkpointed_at": 3,
            "completed_at": 4,
        },
    }
    assert parse_phase78_request(complete).role_outputs["math"] == b"a"
    malformed = json.loads(json.dumps(complete))
    malformed["grounding"]["role_output_base64"]["math"] = "YQ"
    with pytest.raises(Phase78RequestError, match="strict base64"):
        parse_phase78_request(malformed)
    missing_preflight = json.loads(json.dumps(complete))
    missing_preflight["approval"].pop("trusted_preflight_sha256")
    with pytest.raises(Phase78RequestError, match="fields must be exactly"):
        parse_phase78_request(missing_preflight)
    for field in ("idempotency_key", "workflow_id"):
        slash = json.loads(json.dumps(complete))
        slash[field] = "not/a-work-identifier"
        if field == "workflow_id":
            slash["phase3_artifact_state"]["workflow_id"] = slash[field]
            slash["phase3_artifact_occurrence"]["workflow_id"] = slash[field]
        with pytest.raises(Phase78RequestError, match="canonical identifier"):
            parse_phase78_request(slash)


def test_operator_module_is_resource_free_and_fail_closed_while_disabled(
    tmp_path: Path,
) -> None:
    poison = tmp_path / "must-not-exist"
    environment = os.environ.copy()
    environment.update(
        {
            "PHASE78_ENABLED": "false",
            "PHASE78_PROJECT_ROOT": str(poison / "project"),
            "PHASE78_PHASE7_DB_FILE": str(poison / "phase7.db"),
            "PHASE78_PHASE8_DB_FILE": str(poison / "phase8.db"),
            "PHASE78_CAS_ROOT": str(poison / "cas"),
            "PHASE78_SCRATCH_ROOT": str(poison / "scratch"),
            "PHASE78_TRUSTED_OPERATOR_ID": "trusted-operator-1",
            "PHASE78_TRUSTED_OPERATOR_GENERATION": "operator-generation-1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    script = """
import sys
import factory_core.phase78_operator as operator
for name in (
    'factory_core.phase78_current',
    'factory_core.phase78_service',
    'factory_core.phase7_grounding_runtime',
    'factory_core.phase8_evidence_egress_runtime',
    'factory_core.reference_materializer',
):
    assert name not in sys.modules, name
assert operator.main([
    '--operator', 'trusted-operator-1', 'prepare', 'demo',
    '/definitely/not/read/while/disabled.json',
]) == 1
for name in (
    'factory_core.phase78_current',
    'factory_core.phase78_service',
    'factory_core.phase7_grounding_runtime',
    'factory_core.phase8_evidence_egress_runtime',
    'factory_core.reference_materializer',
):
    assert name not in sys.modules, name
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stderr)["code"] == "PHASE78_SHADOW_DISABLED"
    assert not poison.exists()


def test_cli_errors_expose_only_stable_cancellation_reason_allowlist(
    monkeypatch,
    capsys,
) -> None:
    import factory_core.phase78_cli as cli_module

    state = {"reason": "shutdown"}

    class Failure(RuntimeError):
        code = "PHASE78_REQUEST_CANCELLED"

        @property
        def reason(self):
            return state["reason"]

    def load_status(**_kwargs):
        raise Failure("private cancellation detail")

    config = SimpleNamespace(
        load_phase78_settings=lambda: SimpleNamespace(enabled=True)
    )
    service = SimpleNamespace(load_phase78_status=load_status)
    real_import = cli_module.importlib.import_module

    def imported(name: str):
        if name == "factory_core.phase78_config":
            return config
        if name == "factory_core.phase78_service":
            return service
        return real_import(name)

    monkeypatch.setattr(cli_module.importlib, "import_module", imported)
    arguments = ["--actor", "alice", "status", "demo", "pipeline-1"]
    assert cli_module.main(arguments) == 1
    safe = json.loads(capsys.readouterr().err)
    assert safe == {
        "code": "PHASE78_REQUEST_CANCELLED",
        "reason": "shutdown",
    }
    state["reason"] = "private_internal_detail"
    assert cli_module.main(arguments) == 1
    unsafe = json.loads(capsys.readouterr().err)
    assert unsafe == {"code": "PHASE78_REQUEST_CANCELLED"}


def test_operator_rejects_expired_interval_before_phase7_or_phase8_resources(
    tmp_path: Path,
) -> None:
    raw_pdf = make_pdf("Phase 7+8 expired operator preflight")
    fixture, coordinate, state, occurrence, _source = _authority_pdf(tmp_path, raw_pdf)
    phase6_store, proof = _phase6_proof(tmp_path, coordinate, state)
    settings = _settings(fixture, tmp_path, phase6_store.path)
    payload = _payload(state, occurrence, proof, raw_pdf)
    payload["approval"]["logical_issued_at"] = 1
    payload["approval"]["not_before"] = 2
    payload["approval"]["expires_at"] = 3
    with patch.dict(
        os.environ,
        {
            "PHASE78_TRUSTED_OPERATOR_ID": "trusted-operator-1",
            "PHASE78_TRUSTED_OPERATOR_GENERATION": "operator-generation-1",
        },
        clear=False,
    ):
        with pytest.raises(Phase78OperatorError, match="not currently active"):
            prepare_phase78_trusted_preflight(
                settings, "demo", "trusted-operator-1", payload
            )
    assert not settings.required_path("phase7_database").exists()
    assert not settings.required_path("phase8_database").exists()


def test_operator_cli_identity_cannot_override_protected_local_identity() -> None:
    settings = Phase78Settings(enabled=True)
    with patch.dict(
        os.environ,
        {
            "PHASE78_TRUSTED_OPERATOR_ID": "configured-operator",
            "PHASE78_TRUSTED_OPERATOR_GENERATION": "configured-generation",
        },
        clear=False,
    ):
        with pytest.raises(
            Phase78OperatorError,
            match="protected local configuration",
        ):
            prepare_phase78_trusted_preflight(
                settings,
                "demo",
                "self-reported-operator",
                {},
            )


def test_operator_rejects_self_approval_before_persisting_phase7_or_phase8(
    tmp_path: Path,
) -> None:
    raw_pdf = make_pdf("Phase 7+8 self approval rejection")
    fixture, coordinate, state, occurrence, _source = _authority_pdf(tmp_path, raw_pdf)
    phase6_store, proof = _phase6_proof(tmp_path, coordinate, state)
    settings = _settings(fixture, tmp_path, phase6_store.path)
    payload = _payload(state, occurrence, proof, raw_pdf)
    payload["approval"]["issuer_id"] = "alice"
    with patch.dict(
        os.environ,
        {
            "PHASE78_TRUSTED_OPERATOR_ID": "alice",
            "PHASE78_TRUSTED_OPERATOR_GENERATION": "operator-generation-1",
        },
        clear=False,
    ):
        with pytest.raises(Phase78OperatorError, match="own subject"):
            prepare_phase78_trusted_preflight(
                settings, "demo", "alice", payload
            )
    assert not settings.required_path("phase7_database").exists()
    assert not settings.required_path("phase8_database").exists()


def test_real_enabled_pipeline_restart_replay_conflict_and_revoke(tmp_path: Path):
    raw_pdf = make_pdf("Phase 7+8 production adapter flow")
    fixture, coordinate, state, occurrence, _source = _authority_pdf(tmp_path, raw_pdf)
    phase6_store, proof = _phase6_proof(tmp_path, coordinate, state)
    settings = _settings(fixture, tmp_path, phase6_store.path)
    payload = _payload(state, occurrence, proof, raw_pdf)
    _register_operator_preflight(settings, payload)

    pending = submit_phase78_request(settings, "demo", "alice", payload)
    assert pending["outcome"] == "pending"

    def run_once():
        return run_phase78_worker_once(settings, "demo", "alice", payload)

    # The second call is a fresh service/worker entry on the same durable key;
    # it must replay the exact bytes produced by the first call.
    results = [run_once(), run_once()]

    assert {result["outcome"] for result in results} == {"shadow_authorized"}
    assert {result["decision"]["status"] for result in results} == {"AUTHORIZED"}
    assert all(result["work"]["status"] == "succeeded" for result in results)
    assert any(result["replayed"] is True for result in results)
    for result in results:
        encoded = json.dumps(result, sort_keys=True)
        assert "provider_call_performed\": true" not in encoded
        assert "outbox_dispatch_performed\": true" not in encoded
        assert result["authoritative"] is False
        assert result["authority_transferred"] is False
        assert result["dispatch_performed"] is False

    restarted = replace(settings)
    status = load_phase78_status(
        restarted, "demo", "alice", payload["idempotency_key"]
    )
    assert status["outcome"] == "shadow_authorized"
    assert status["decision"]["status"] == "AUTHORIZED"
    assert status["work"]["status"] == "succeeded"

    with pytest.raises(Phase78SchedulerTerminalConflict):
        cancel_phase78_request(
            settings,
            "demo",
            "alice",
            {
                "schema_version": "phase78-work-cancel-request-v1",
                "idempotency_key": payload["idempotency_key"],
                "cancelled_at": int(time.time()),
                "reason": "user_cancel",
            },
        )

    changed = json.loads(json.dumps(payload))
    changed["reference"]["bibliographic_metadata"]["title"] = "Different bytes"
    with pytest.raises(Phase78WorkIdempotencyConflict):
        submit_phase78_request(settings, "demo", "alice", changed)

    approval = next(result["approval"] for result in results if result["approval"])
    revoke_payload = {
        "schema_version": "phase78-approval-revoke-request-v1",
        "idempotency_key": "phase78-revoke-1",
        "expected_event_sha256": approval["event_sha256"],
        "revoked_at": int(time.time()) + 1,
        "reason_code": "OPERATOR_REVOKED",
    }
    with pytest.raises(Phase8CurrentConflict):
        revoke_phase78_approval(
            settings,
            "demo",
            "alice",
            approval["approval_id"],
            revoke_payload,
        )
    revoked = revoke_phase78_approval(
        settings,
        "demo",
        "trusted-operator-1",
        approval["approval_id"],
        revoke_payload,
    )
    assert revoked["approval"]["state"] == "REVOKED"
    after_revoke = load_phase78_status(
        settings, "demo", "alice", payload["idempotency_key"]
    )
    assert after_revoke["outcome"] == "denied"
    assert after_revoke["decision"]["status"] == "DENIED"
    assert after_revoke["decision"]["reason_code"] == "APPROVAL_REVOKED"

    phase6_store.revoke_grant(
        proof.grant.grant_id,
        actor_id="phase6-local-shadow-issuer",
        actor_generation="issuer-generation-1",
        reason_code="PROJECT_ACCESS_REVOKED",
        effective_at=int(time.time()) + 2,
        idempotency_key="phase6-revoke-after-phase78-success",
    )
    after_source_drift = load_phase78_status(
        settings, "demo", "alice", payload["idempotency_key"]
    )
    assert after_source_drift["outcome"] == "denied"
    assert after_source_drift["work"]["status"] == "succeeded"
    assert after_source_drift["blocker"]["reason_code"] == "SOURCE_HEAD_DRIFT"


def test_completed_work_is_a_valid_publication_source_but_not_an_activation_fence(
    tmp_path: Path,
) -> None:
    """History qualification and mutable activation have distinct fences."""

    del tmp_path
    operation = SimpleNamespace(claim_generation=7, dispatch_nonce="nonce-7")
    state = SimpleNamespace(
        operation=operation,
        claim_owner_id="worker-7",
        claim_owner_epoch=3,
    )
    completed = SimpleNamespace(
        operation_identity_sha256="a" * 64,
        state=state,
        status=phase78_worker.OperationStatus.SUCCEEDED,
        cancellation=None,
    )
    scheduler = SimpleNamespace(
        ledger=SimpleNamespace(load=lambda _key, deadline: completed)
    )
    request = SimpleNamespace(idempotency_key="completed-publication-source")
    token = phase78_worker._WorkLeaseToken(
        operation_identity_sha256="a" * 64,
        claim_generation=7,
        claim_owner_id="worker-7",
        claim_owner_epoch=3,
        local_worker_nonce="nonce-7",
    )
    publication = {
        "publication_kind": "work-generation",
        "generation": {
            "request_idempotency_key": request.idempotency_key,
            "operation_identity_sha256": token.operation_identity_sha256,
            "claim_generation": token.claim_generation,
            "claim_owner_id": token.claim_owner_id,
            "claim_owner_epoch": token.claim_owner_epoch,
            "local_worker_nonce": token.local_worker_nonce,
        },
    }
    source_checks: list[str] = []
    deadline = TotalDeadline(30_000)
    source_verifier = phase78_worker._worker_publication_source_verifier(
        scheduler=scheduler,
        request=request,
        token=token,
        deadline=deadline,
        current_fence=source_checks.append,
        trusted_preflight={},
    )

    assert source_verifier(publication) is True
    assert source_checks == ["phase8_publication_source_upstream"]
    with pytest.raises(phase78_worker._WorkAlreadyCompleted):
        phase78_worker._work_fence(
            scheduler, request, token, deadline
        )("mutable_activation")


def test_operator_missing_pdf_fails_closed_before_work_is_enqueued(tmp_path: Path):
    raw_pdf = make_pdf("Phase 7+8 missing input")
    fixture, coordinate, state, occurrence, source = _authority_pdf(tmp_path, raw_pdf)
    phase6_store, proof = _phase6_proof(tmp_path, coordinate, state)
    settings = _settings(fixture, tmp_path, phase6_store.path)
    payload = _payload(state, occurrence, proof, raw_pdf)
    source.unlink()

    with patch.dict(
        os.environ,
        {
            "PHASE78_TRUSTED_OPERATOR_ID": "trusted-operator-1",
            "PHASE78_TRUSTED_OPERATOR_GENERATION": "operator-generation-1",
        },
        clear=False,
    ):
        with pytest.raises(ReferenceMaterializationError) as raised:
            prepare_phase78_trusted_preflight(
                settings, "demo", "trusted-operator-1", payload
            )
    assert getattr(raised.value, "code", None) == "INPUT_NOT_REGULAR"
    assert str(source) not in str(raised.value)
    assert not settings.required_path("work_database").exists()
    assert not settings.required_path("phase8_database").exists()


@pytest.mark.parametrize("variant", ("forged", "mismatch"))
def test_untrusted_or_mismatched_preflight_never_authorizes(
    tmp_path: Path,
    variant: str,
) -> None:
    raw_pdf = make_pdf(f"Phase 7+8 rejected preflight {variant}")
    fixture, coordinate, state, occurrence, _source = _authority_pdf(tmp_path, raw_pdf)
    phase6_store, proof = _phase6_proof(tmp_path, coordinate, state)
    settings = _settings(fixture, tmp_path, phase6_store.path)
    payload = _payload(state, occurrence, proof, raw_pdf)
    payload["idempotency_key"] = f"pipeline-preflight-{variant}"
    payload["reference"]["reference_id"] = f"reference-preflight-{variant}"
    payload["reference"]["logical_id"] = f"binding-preflight-{variant}"
    payload["approval"]["approval_id"] = f"approval-preflight-{variant}"
    _register_operator_preflight(settings, payload)
    if variant == "forged":
        payload["approval"]["trusted_preflight_sha256"] = "f" * 64
        expected_error = Phase8NotFound
    else:
        payload["approval"]["issuer_generation"] = "operator-generation-mismatch"
        expected_error = Phase8CurrentConflict

    with pytest.raises(expected_error):
        run_phase78_worker_once(settings, "demo", "alice", payload)

    status = load_phase78_status(
        settings, "demo", "alice", payload["idempotency_key"]
    )
    assert status["work"]["status"] == "failed"
    assert status["outcome"] == "failed"
    assert status["decision"] is None
    connection = sqlite3.connect(settings.required_path("phase8_database"))
    assert connection.execute("SELECT count(*) FROM phase8_approvals").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM phase8_decisions").fetchone()[0] == 0
    connection.close()


def test_first_worker_run_after_preflight_expiry_is_immediately_denied(
    tmp_path: Path,
    monkeypatch,
) -> None:
    raw_pdf = make_pdf("Phase 7+8 queued across approval expiry")
    fixture, coordinate, state, occurrence, _source = _authority_pdf(tmp_path, raw_pdf)
    phase6_store, proof = _phase6_proof(tmp_path, coordinate, state)
    settings = _settings(fixture, tmp_path, phase6_store.path)
    payload = _payload(state, occurrence, proof, raw_pdf)
    payload["idempotency_key"] = "pipeline-expired-before-first-run"
    payload["reference"]["reference_id"] = "reference-expired-first-run"
    payload["reference"]["logical_id"] = "binding-expired-first-run"
    payload["approval"]["approval_id"] = "approval-expired-first-run"
    _register_operator_preflight(settings, payload)

    import factory_core.phase78_service as service_module

    trusted_after_expiry = int(payload["approval"]["expires_at"]) + 1
    monkeypatch.setattr(
        service_module, "_trusted_logical_time", lambda: trusted_after_expiry
    )
    result = run_phase78_worker_once(settings, "demo", "alice", payload)
    assert result["work"]["status"] == "succeeded"
    assert result["outcome"] == "denied"
    assert result["decision"]["status"] == "DENIED"
    assert result["decision"]["reason_code"] == "APPROVAL_EXPIRED"
    restarted = load_phase78_status(
        replace(settings), "demo", "alice", payload["idempotency_key"]
    )
    assert restarted["outcome"] == "denied"
    assert restarted["decision"]["reason_code"] == "APPROVAL_EXPIRED"


def test_cancelled_generation_cannot_publish_after_late_persisted_adapter(
    tmp_path: Path,
    monkeypatch,
) -> None:
    raw_pdf = make_pdf("Phase 7+8 late parser generation fence")
    fixture, coordinate, state, occurrence, _source = _authority_pdf(tmp_path, raw_pdf)
    phase6_store, proof = _phase6_proof(tmp_path, coordinate, state)
    settings = _settings(fixture, tmp_path, phase6_store.path)
    payload = _payload(state, occurrence, proof, raw_pdf)
    payload["idempotency_key"] = "pipeline-cancelled-late-parser"
    payload["reference"]["reference_id"] = "reference-cancelled-late-adapter"
    payload["reference"]["logical_id"] = "binding-cancelled-late-adapter"
    payload["approval"]["approval_id"] = "approval-cancelled-late-adapter"
    _register_operator_preflight(settings, payload)

    import factory_core.phase78_worker as worker_module

    original_load = (
        worker_module.Phase8EvidenceEgressStore.load_trusted_approval_preflight
    )
    entered = threading.Event()
    release = threading.Event()

    def blocked_load(store, *args, **kwargs):
        entered.set()
        assert release.wait(timeout=10), "test did not release the persisted adapter"
        return original_load(store, *args, **kwargs)

    monkeypatch.setattr(
        worker_module.Phase8EvidenceEgressStore,
        "load_trusted_approval_preflight",
        blocked_load,
    )
    observed: dict[str, BaseException | dict[str, object]] = {}

    def invoke() -> None:
        try:
            observed["result"] = run_phase78_worker_once(
                settings, "demo", "alice", payload
            )
        except BaseException as exc:  # the assertion inspects the exact reason
            observed["error"] = exc

    thread = threading.Thread(target=invoke, name="phase78-late-parser-test")
    thread.start()
    assert entered.wait(timeout=10), "worker did not reach the persisted adapter"
    cancelled = cancel_phase78_request(
        settings,
        "demo",
        "alice",
        {
            "schema_version": "phase78-work-cancel-request-v1",
            "idempotency_key": payload["idempotency_key"],
            "cancelled_at": payload["work"]["completed_at"],
            "reason": "user_cancel",
        },
    )
    assert cancelled["work"]["status"] == "cancelled"
    assert cancelled["cancellation_reason"] == "user_cancel"
    release.set()
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert "result" not in observed
    assert isinstance(observed.get("error"), Phase78CancellationError)
    assert observed["error"].reason == "user_cancel"
    ledger = Phase78WorkLedger(
        settings.required_path("work_database"),
        settings.required_path("work_spool"),
    )
    ledger.initialize()
    assert ledger.load(payload["idempotency_key"]).status.value == "cancelled"
    assert settings.required_path("phase7_database").is_file()
    connection = sqlite3.connect(settings.required_path("phase8_database"))
    assert connection.execute("SELECT count(*) FROM phase8_approvals").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM phase8_decisions").fetchone()[0] == 0
    connection.close()


def test_cancel_after_authorized_decision_keeps_history_but_status_is_cancelled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A P8 history commit cannot override the durable work terminal state."""

    raw_pdf = make_pdf("Phase 7+8 cancel after immutable decision")
    fixture, coordinate, state, occurrence, _source = _authority_pdf(tmp_path, raw_pdf)
    phase6_store, proof = _phase6_proof(tmp_path, coordinate, state)
    settings = _settings(fixture, tmp_path, phase6_store.path)
    payload = _payload(state, occurrence, proof, raw_pdf)
    payload["idempotency_key"] = "pipeline-cancel-after-decision"
    payload["reference"]["reference_id"] = "reference-cancel-after-decision"
    payload["reference"]["logical_id"] = "binding-cancel-after-decision"
    payload["approval"]["approval_id"] = "approval-cancel-after-decision"
    _register_operator_preflight(settings, payload)
    submit_phase78_request(settings, "demo", "alice", payload)

    original_complete = Phase78WorkLedger.complete
    entered = threading.Event()
    release = threading.Event()

    def block_after_decision(self, *args, **kwargs):
        entered.set()
        assert release.wait(timeout=30), "test did not release work completion"
        return original_complete(self, *args, **kwargs)

    monkeypatch.setattr(Phase78WorkLedger, "complete", block_after_decision)
    observed: dict[str, BaseException | dict[str, object]] = {}

    def invoke() -> None:
        try:
            observed["result"] = run_phase78_worker_once(
                settings, "demo", "alice", payload
            )
        except BaseException as exc:
            observed["error"] = exc

    thread = threading.Thread(
        target=invoke, name="phase78-cancel-after-decision"
    )
    thread.start()
    assert entered.wait(timeout=90), "worker did not reach work completion"
    cancelled = cancel_phase78_request(
        settings,
        "demo",
        "alice",
        {
            "schema_version": "phase78-work-cancel-request-v1",
            "idempotency_key": payload["idempotency_key"],
            "cancelled_at": payload["work"]["completed_at"],
            "reason": "user_cancel",
        },
    )
    assert cancelled["work"]["status"] == "cancelled"
    assert cancelled["cancellation_reason"] == "user_cancel"
    public_wire = json.dumps(cancelled, sort_keys=True)
    assert "local_worker_nonce" not in public_wire
    cancellation_summary = cancelled["work"]["cancellation"]
    for private_field in (
        "claim_owner_id",
        "claim_owner_epoch",
        "claim_generation",
        "operation_identity_sha256",
        "request_idempotency_key",
    ):
        assert private_field not in cancellation_summary
    release.set()
    thread.join(timeout=60)
    assert not thread.is_alive()
    assert "result" not in observed
    assert isinstance(observed.get("error"), Phase78CancellationError)
    assert observed["error"].reason == "user_cancel"

    restarted = load_phase78_status(
        replace(settings), "demo", "alice", payload["idempotency_key"]
    )
    assert restarted["work"]["status"] == "cancelled"
    assert restarted["work"]["cancellation_reason"] == "user_cancel"
    assert restarted["outcome"] == "cancelled"
    # The immutable P8 record remains available as history, but it no longer
    # determines the pipeline's effective status.
    assert restarted["decision"]["status"] == "AUTHORIZED"

    for name, value in _phase78_environment(settings).items():
        monkeypatch.setenv(name, value)
    from fastapi import FastAPI
    from web.backend.auth import create_access_token
    from web.backend.auth_store import AuthStore
    from web.backend.config import Settings as WebSettings
    from web.backend.phase78_api import create_phase78_router

    web_settings = WebSettings(
        jwt_secret="c" * 32,
        admin_password="phase78 cancellation e2e password",
        factory_root=fixture.project_dir,
        auth_db_file=tmp_path / "phase78-cancel-web-auth.db",
        phase78_shadow_enabled=True,
    )
    auth = AuthStore(web_settings.resolved_auth_db_file)
    auth.initialize()
    auth.register_user("alice", "phase78 cancellation user password")
    auth.approve_user("alice", actor="admin")
    auth.grant_project_owner("demo", "alice", actor="admin")
    runtime_token = create_access_token(web_settings, "alice", "user", "active")
    app = FastAPI()
    app.include_router(create_phase78_router(web_settings))

    async def read_status() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://phase78.cancelled"
        ) as client:
            return await client.get(
                "/api/projects/demo/phase78-shadow/pipeline-cancel-after-decision",
                headers=_authorization_headers(runtime_token),
            )

    response = asyncio.run(read_status())
    assert response.status_code == 200, response.text
    web_wire = response.json()
    assert web_wire["outcome"] == "cancelled"
    assert web_wire["work"]["cancellation_reason"] == "user_cancel"
    assert web_wire["decision"]["status"] == "AUTHORIZED"
    encoded = json.dumps(web_wire, sort_keys=True)
    assert "local_worker_nonce" not in encoded
    cancellation_summary = web_wire["work"]["cancellation"]
    for private_field in (
        "claim_owner_id",
        "claim_owner_epoch",
        "claim_generation",
        "operation_identity_sha256",
        "request_idempotency_key",
    ):
        assert private_field not in cancellation_summary


def test_public_cancel_claims_and_settles_pending_work_without_a_token(
    tmp_path: Path,
) -> None:
    raw_pdf = make_pdf("Phase 7+8 pending public cancellation")
    fixture, coordinate, state, occurrence, _source = _authority_pdf(tmp_path, raw_pdf)
    phase6_store, proof = _phase6_proof(tmp_path, coordinate, state)
    settings = _settings(fixture, tmp_path, phase6_store.path)
    payload = _payload(state, occurrence, proof, raw_pdf)
    payload["idempotency_key"] = "pipeline-public-cancel-pending"
    pending = submit_phase78_request(settings, "demo", "alice", payload)
    assert pending["work"]["status"] == "pending"
    cancel_payload = {
        "schema_version": "phase78-work-cancel-request-v1",
        "idempotency_key": payload["idempotency_key"],
        "cancelled_at": payload["work"]["completed_at"],
        "reason": "user_cancel",
    }
    cancel_path = tmp_path / "phase78-cancel.json"
    cancel_path.write_text(
        json.dumps(cancel_payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(_phase78_environment(settings))
    command = subprocess.run(
        [
            sys.executable,
            "-m",
            "factory_core.cli",
            "phase78",
            "--actor",
            "alice",
            "cancel",
            "demo",
            str(cancel_path),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert command.returncode == 0, command.stderr
    cancelled = json.loads(command.stdout)
    assert cancelled["work"]["status"] == "cancelled"
    assert cancelled["cancellation_reason"] == "user_cancel"
    assert cancelled["authoritative"] is False
    replayed = cancel_phase78_request(
        settings, "demo", "alice", cancel_payload
    )
    assert replayed["work"]["status"] == "cancelled"
    assert replayed["replayed"] is True


def test_scheduler_lifecycle_preserves_shutdown_and_superseded_reasons(
    tmp_path: Path,
) -> None:
    for reason in (
        Phase78CancellationReason.SHUTDOWN,
        Phase78CancellationReason.SUPERSEDED,
    ):
        case = tmp_path / reason.value
        case.mkdir()
        raw_pdf = make_pdf(f"Phase 7+8 scheduler cancellation {reason.value}")
        fixture, coordinate, state, occurrence, _source = _authority_pdf(case, raw_pdf)
        phase6_store, proof = _phase6_proof(case, coordinate, state)
        settings = _settings(fixture, case, phase6_store.path)
        payload = _payload(state, occurrence, proof, raw_pdf)
        payload["idempotency_key"] = f"pipeline-{reason.value}"
        submit_phase78_request(settings, "demo", "alice", payload)
        total = TotalDeadline(settings.deadline_ms)
        scheduler = Phase78ShadowScheduler(settings, deadline=total)
        cancelled = scheduler.cancel(
            idempotency_key=payload["idempotency_key"],
            occurred_at=payload["work"]["completed_at"],
            reason=reason,
            deadline=total,
        )
        assert cancelled.view.status.value == "cancelled"
        assert cancelled.cancellation_reason == reason.value
        assert cancelled.view.cancellation is not None
        receipt_sha256 = cancelled.view.cancellation.receipt_sha256

        restarted_total = TotalDeadline(settings.deadline_ms)
        restarted = Phase78ShadowScheduler(
            settings, deadline=restarted_total
        )
        exact_replay = restarted.cancel(
            idempotency_key=payload["idempotency_key"],
            occurred_at=payload["work"]["completed_at"],
            reason=reason,
            deadline=restarted_total,
        )
        assert exact_replay.replayed is True
        assert exact_replay.cancellation_reason == reason.value
        assert exact_replay.view.cancellation.receipt_sha256 == receipt_sha256

        status = load_phase78_status(
            settings,
            "demo",
            "alice",
            payload["idempotency_key"],
        )
        assert status["outcome"] == "cancelled"
        assert status["work"]["cancellation_reason"] == reason.value
        assert (
            status["work"]["cancellation"]["receipt_sha256"]
            == receipt_sha256
        )

        other = (
            Phase78CancellationReason.SUPERSEDED
            if reason is Phase78CancellationReason.SHUTDOWN
            else Phase78CancellationReason.SHUTDOWN
        )
        with pytest.raises(
            Phase78WorkIdempotencyConflict, match="different bytes"
        ):
            restarted.cancel(
                idempotency_key=payload["idempotency_key"],
                occurred_at=payload["work"]["completed_at"],
                reason=other,
                deadline=TotalDeadline(settings.deadline_ms),
            )
        with pytest.raises(
            Phase78WorkIdempotencyConflict, match="different bytes"
        ):
            restarted.cancel(
                idempotency_key=payload["idempotency_key"],
                occurred_at=payload["work"]["completed_at"] + 1,
                reason=reason,
                deadline=TotalDeadline(settings.deadline_ms),
            )


def test_deadline_after_durable_adapter_commit_is_uncertain_then_same_key_replays(
    tmp_path: Path,
    monkeypatch,
) -> None:
    raw_pdf = make_pdf("Phase 7+8 outcome uncertain replay")
    fixture, coordinate, state, occurrence, _source = _authority_pdf(tmp_path, raw_pdf)
    phase6_store, proof = _phase6_proof(tmp_path, coordinate, state)
    settings = _settings(fixture, tmp_path, phase6_store.path)
    payload = _payload(state, occurrence, proof, raw_pdf)
    payload["idempotency_key"] = "pipeline-outcome-uncertain"
    payload["reference"]["reference_id"] = "reference-outcome-uncertain"
    payload["reference"]["logical_id"] = "binding-outcome-uncertain"
    payload["approval"]["approval_id"] = "approval-outcome-uncertain"
    _register_operator_preflight(settings, payload)

    original_complete = Phase78WorkLedger.complete
    interrupted = False

    def timeout_after_decision(self, *args, **kwargs):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise Phase78DeadlineError(
                "synthetic total deadline after durable Phase-8 decision"
            )
        return original_complete(self, *args, **kwargs)

    monkeypatch.setattr(
        Phase78WorkLedger, "complete", timeout_after_decision
    )
    with pytest.raises(Phase78OutcomeUncertain) as raised:
        run_phase78_worker_once(settings, "demo", "alice", payload)
    assert raised.value.idempotency_key == payload["idempotency_key"]
    ledger = Phase78WorkLedger(
        settings.required_path("work_database"),
        settings.required_path("work_spool"),
    )
    ledger.initialize()
    assert ledger.load(payload["idempotency_key"]).status.value == (
        "dispatch-checkpointed"
    )
    uncertain_status = load_phase78_status(
        replace(settings), "demo", "alice", payload["idempotency_key"]
    )
    assert uncertain_status["work"]["status"] == "dispatch-checkpointed"
    assert uncertain_status["outcome"] == "dispatch-checkpointed"
    assert uncertain_status["decision"]["status"] == "AUTHORIZED"

    monkeypatch.setattr(Phase78WorkLedger, "complete", original_complete)
    replay = run_phase78_worker_once(settings, "demo", "alice", payload)
    assert replay["outcome"] == "shadow_authorized"
    assert replay["work"]["status"] == "succeeded"


def test_phase6_head_change_after_enqueue_blocks_worker_before_phase7_current(
    tmp_path: Path,
) -> None:
    raw_pdf = make_pdf("Phase 7+8 fenced request")
    fixture, coordinate, state, occurrence, _source = _authority_pdf(tmp_path, raw_pdf)
    phase6_store, proof = _phase6_proof(tmp_path, coordinate, state)
    settings = _settings(fixture, tmp_path, phase6_store.path)
    payload = _payload(state, occurrence, proof, raw_pdf)
    pending = submit_phase78_request(settings, "demo", "alice", payload)
    assert pending["work"]["status"] == "pending"

    phase6_store.revoke_grant(
        proof.grant.grant_id,
        actor_id="phase6-local-shadow-issuer",
        actor_generation="issuer-generation-1",
        reason_code="PROJECT_ACCESS_REVOKED",
        effective_at=30,
        idempotency_key="phase6-revoke-before-phase78-worker",
    )
    with pytest.raises(Phase78CurrentHeadError):
        run_phase78_worker_once(settings, "demo", "alice", payload)

    assert not settings.required_path("phase7_database").exists()
    assert not settings.required_path("phase8_database").exists()
    status = pending["work"]
    assert status["status"] == "pending"


def test_actual_cli_service_worker_pipeline_then_authenticated_web_read(
    tmp_path: Path, monkeypatch
) -> None:
    """One enabled path crosses both real public adapters without injection."""

    raw_pdf = make_pdf("Phase 7+8 CLI through Web read")
    fixture, coordinate, state, occurrence, _source = _authority_pdf(tmp_path, raw_pdf)
    phase6_store, proof = _phase6_proof(tmp_path, coordinate, state)
    settings = _settings(fixture, tmp_path, phase6_store.path)
    materialization_root = tmp_path / "operator-materialization-root"
    materialization_source = materialization_root / "references" / "source.pdf"
    materialization_source.parent.mkdir(mode=0o700, parents=True)
    materialization_source.write_bytes(raw_pdf)
    settings = replace(settings, project_root=materialization_root)
    payload = _payload(state, occurrence, proof, raw_pdf)
    payload["idempotency_key"] = "pipeline-cli-web-1"
    payload["reference"]["reference_id"] = "phase78-reference-cli-web-1"
    payload["reference"]["logical_id"] = "phase78-binding-cli-web-1"
    payload["approval"]["approval_id"] = "phase78-approval-cli-web-1"
    request_path = tmp_path / "phase78-cli-request.json"
    request_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(_phase78_environment(settings))
    root = Path(__file__).resolve().parents[1]

    prepared = subprocess.run(
        [
            sys.executable,
            "-m",
            "factory_core.phase78_operator",
            "--operator",
            "trusted-operator-1",
            "prepare",
            "demo",
            str(request_path),
        ],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=45,
    )
    assert prepared.returncode == 0, prepared.stderr
    prepared_wire = json.loads(prepared.stdout)
    assert prepared_wire["dispatch_performed"] is False
    payload["approval"]["trusted_preflight_sha256"] = prepared_wire[
        "trusted_preflight_sha256"
    ]
    request_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    moved_materialization_root = tmp_path / "moved-operator-materialization-root"
    materialization_root.rename(moved_materialization_root)
    assert not settings.required_path("project_root").exists()

    submitted = subprocess.run(
        [
            sys.executable,
            "-m",
            "factory_core.cli",
            "phase78",
            "--actor",
            "alice",
            "submit",
            "demo",
            str(request_path),
        ],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=45,
    )
    assert submitted.returncode == 0, submitted.stderr
    assert json.loads(submitted.stdout)["outcome"] == "pending"

    executed = subprocess.run(
        [
            sys.executable,
            "-m",
            "factory_core.cli",
            "phase78",
            "--actor",
            "alice",
            "run-one",
            "demo",
            str(request_path),
        ],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=45,
    )
    assert executed.returncode == 0, executed.stderr
    executed_wire = json.loads(executed.stdout)
    assert executed_wire["outcome"] == "shadow_authorized"
    assert executed_wire["decision"]["status"] == "AUTHORIZED"
    assert executed_wire["work"]["status"] == "succeeded"
    assert executed_wire["phase7"]["replayed"] is True
    assert executed_wire["reference_binding"]["replayed"] is True
    assert executed_wire["reference_binding"]["binding_sha256"] == prepared_wire[
        "binding_sha256"
    ]

    for name, value in _phase78_environment(settings).items():
        monkeypatch.setenv(name, value)
    from fastapi import FastAPI
    from web.backend.auth import create_access_token
    from web.backend.auth_store import AuthStore
    from web.backend.config import Settings as WebSettings
    from web.backend.phase78_api import create_phase78_router

    web_settings = WebSettings(
        jwt_secret="v" * 32,
        admin_password="phase78 enabled e2e password",
        factory_root=fixture.project_dir,
        auth_db_file=tmp_path / "phase78-web-auth.db",
        phase78_shadow_enabled=True,
    )
    auth = AuthStore(web_settings.resolved_auth_db_file)
    auth.initialize()
    auth.register_user("alice", "phase78 enabled e2e user password")
    auth.approve_user("alice", actor="admin")
    auth.grant_project_owner("demo", "alice", actor="admin")
    runtime_token = create_access_token(web_settings, "alice", "user", "active")
    app = FastAPI()
    app.include_router(create_phase78_router(web_settings))

    async def read_status(target: FastAPI = app) -> httpx.Response:
        transport = httpx.ASGITransport(app=target)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://phase78.e2e"
        ) as client:
            return await client.get(
                "/api/projects/demo/phase78-shadow/pipeline-cli-web-1",
                headers=_authorization_headers(runtime_token),
            )

    response = asyncio.run(read_status())
    assert response.status_code == 200, response.text
    web_wire = response.json()
    assert web_wire["outcome"] == "shadow_authorized"
    assert web_wire["decision"]["status"] == "AUTHORIZED"
    assert web_wire["work"]["status"] == "succeeded"
    assert all(
        web_wire[field] is False
        for field in (
            "authoritative",
            "authority_transferred",
            "dispatch_performed",
            "provider_call_performed",
            "outbox_dispatch_performed",
        )
    )

    import factory_core.phase78_service as service_module

    monkeypatch.setattr(
        service_module,
        "_trusted_logical_time",
        lambda: int(payload["approval"]["expires_at"]) + 1,
    )
    restarted_app = FastAPI()
    restarted_app.include_router(create_phase78_router(web_settings))
    expired_response = asyncio.run(read_status(restarted_app))
    assert expired_response.status_code == 200, expired_response.text
    expired_wire = expired_response.json()
    assert expired_wire["outcome"] == "denied"
    assert expired_wire["decision"]["status"] == "DENIED"
    assert expired_wire["decision"]["reason_code"] == "APPROVAL_EXPIRED"
    assert expired_wire["work"]["status"] == "succeeded"


def test_status_boundaries_preserve_deadline_and_cancellation_control_flow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """M1: no status boundary may synthesize a denied success response."""

    from factory_core.phase7_grounding_runtime import Phase7GroundingStore
    from factory_core.phase78_current import Phase78CurrentHeadVerifier
    from factory_core.phase8_evidence_egress_runtime import (
        Phase8CurrentConflict,
        Phase8EvidenceEgressStore,
        Phase8NotFound,
    )

    raw_pdf = make_pdf("Phase 7+8 status control-flow propagation")
    fixture, coordinate, state, occurrence, _source = _authority_pdf(
        tmp_path, raw_pdf
    )
    phase6_store, proof = _phase6_proof(tmp_path, coordinate, state)
    settings = _settings(fixture, tmp_path, phase6_store.path)
    payload = _payload(state, occurrence, proof, raw_pdf)
    payload["idempotency_key"] = "pipeline-status-control-flow"
    payload["reference"]["reference_id"] = "reference-status-control-flow"
    payload["reference"]["logical_id"] = "binding-status-control-flow"
    payload["approval"]["approval_id"] = "approval-status-control-flow"
    _register_operator_preflight(settings, payload)
    completed = run_phase78_worker_once(settings, "demo", "alice", payload)
    assert completed["outcome"] == "shadow_authorized"

    boundaries = (
        (Phase7GroundingStore, "load_bundle", "phase7_history_load"),
        (Phase78CurrentHeadVerifier, "verify", "live_current_verification"),
        (
            Phase8EvidenceEgressStore,
            "load_reference_binding_by_idempotency_key",
            "phase8_binding_load",
        ),
        (
            Phase8EvidenceEgressStore,
            "load_decision_by_idempotency_key",
            "phase8_decision_load",
        ),
    )
    controls = (
        (
            "timeout",
            lambda: Phase78DeadlineError("synthetic status deadline"),
            "PHASE78_DEADLINE_EXCEEDED",
            "timeout",
        ),
        *(
            (
                reason.value,
                lambda reason=reason: Phase78CancellationError(reason),
                "PHASE78_REQUEST_CANCELLED",
                reason.value,
            )
            for reason in (
                Phase78CancellationReason.USER_CANCEL,
                Phase78CancellationReason.SHUTDOWN,
                Phase78CancellationReason.SUPERSEDED,
            )
        ),
    )
    for owner, method_name, boundary in boundaries:
        for control, factory, code, reason in controls:
            def explode(*_args, factory=factory, **_kwargs):
                raise factory()

            with monkeypatch.context() as scoped:
                scoped.setattr(owner, method_name, explode)
                with pytest.raises(
                    (Phase78DeadlineError, Phase78CancellationError)
                ) as captured:
                    load_phase78_status(
                        replace(settings),
                        "demo",
                        "alice",
                        payload["idempotency_key"],
                    )
            assert captured.value.code == code, (boundary, control)
            assert getattr(captured.value, "reason", None) == reason, (
                boundary,
                control,
            )
            assert "CURRENT_HEAD_UNAVAILABLE" not in str(captured.value)

    # History is audit evidence, never proof of effective currentness.  If the
    # qualified current loader has no publication or rejects its generation,
    # status must retain the historical binding with current=false.
    for error_type, reason_code in (
        (Phase8NotFound, None),
        (Phase8CurrentConflict, "PUBLICATION_GENERATION_DRIFT"),
    ):
        def reject_current(*_args, error_type=error_type, **_kwargs):
            raise error_type("synthetic current binding unavailable")

        with monkeypatch.context() as scoped:
            scoped.setattr(
                Phase8EvidenceEgressStore,
                "load_current_reference_binding",
                reject_current,
            )
            scoped.setattr(
                Phase8EvidenceEgressStore,
                "load_current_decision",
                reject_current,
            )
            historical_status = load_phase78_status(
                replace(settings),
                "demo",
                "alice",
                payload["idempotency_key"],
            )
        assert historical_status["reference_binding"]["current"] is False
        if reason_code is None:
            assert historical_status["blocker"] is None
        else:
            assert historical_status["outcome"] == "denied"
            assert historical_status["blocker"]["reason_code"] == reason_code


class _CrossingDeadline:
    def __init__(self, crossing_stage: str):
        self._base = TotalDeadline(300_000)
        self._crossing_stage = crossing_stage
        self._crossed = False

    def check(self, stage=None):
        self._base.check(stage)
        if stage == self._crossing_stage and not self._crossed:
            self._crossed = True
            raise Phase78DeadlineError(f"synthetic crossing at {stage}")

    def remaining_seconds(self):
        return self._base.remaining_seconds()

    def remaining_milliseconds(self):
        return self._base.remaining_milliseconds()


def test_terminal_operator_preflight_deadline_is_root_uncertain_and_replayable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import factory_core.phase78_operator as operator_module

    raw_pdf = make_pdf("Phase 7+8 terminal operator preflight crossing")
    fixture, coordinate, state, occurrence, _source = _authority_pdf(
        tmp_path, raw_pdf
    )
    phase6_store, proof = _phase6_proof(tmp_path, coordinate, state)
    settings = _settings(fixture, tmp_path, phase6_store.path)
    payload = _payload(state, occurrence, proof, raw_pdf)
    payload["idempotency_key"] = "operator-terminal-uncertain"
    payload["reference"]["reference_id"] = "operator-terminal-reference"
    payload["reference"]["logical_id"] = "operator-terminal-binding"
    payload["approval"]["approval_id"] = "operator-terminal-approval"
    trusted_now = int(payload["approval"]["not_before"])
    monkeypatch.setattr(operator_module.time, "time", lambda: trusted_now)
    environment = {
        "PHASE78_TRUSTED_OPERATOR_ID": payload["approval"]["issuer_id"],
        "PHASE78_TRUSTED_OPERATOR_GENERATION": payload["approval"][
            "issuer_generation"
        ],
    }
    with patch.dict(os.environ, environment, clear=False):
        with pytest.raises(Phase78OutcomeUncertain) as captured:
            prepare_phase78_trusted_preflight(
                settings,
                "demo",
                str(payload["approval"]["issuer_id"]),
                payload,
                deadline=_CrossingDeadline("operator preflight response"),
            )
    assert captured.value.idempotency_key == payload["idempotency_key"]
    connection = sqlite3.connect(settings.required_path("phase8_database"))
    try:
        assert connection.execute(
            "SELECT count(*) FROM phase8_trusted_approval_preflights "
            "WHERE idempotency_key=?",
            (f"{payload['idempotency_key']}:trusted-preflight",),
        ).fetchone()[0] == 1
    finally:
        connection.close()
    with patch.dict(os.environ, environment, clear=False):
        replay = prepare_phase78_trusted_preflight(
            settings,
            "demo",
            str(payload["approval"]["issuer_id"]),
            payload,
        )
    assert replay["idempotency_key"] == payload["idempotency_key"]
    assert replay["binding_replayed"] is True
    assert replay["preflight_replayed"] is True


def test_terminal_revocation_deadline_is_root_uncertain_and_read_time_safe(
    tmp_path: Path,
) -> None:
    raw_pdf = make_pdf("Phase 7+8 terminal revocation crossing")
    fixture, coordinate, state, occurrence, _source = _authority_pdf(
        tmp_path, raw_pdf
    )
    phase6_store, proof = _phase6_proof(tmp_path, coordinate, state)
    settings = _settings(fixture, tmp_path, phase6_store.path)
    payload = _payload(state, occurrence, proof, raw_pdf)
    payload["idempotency_key"] = "revocation-terminal-pipeline"
    payload["reference"]["reference_id"] = "revocation-terminal-reference"
    payload["reference"]["logical_id"] = "revocation-terminal-binding"
    payload["approval"]["approval_id"] = "revocation-terminal-approval"
    _register_operator_preflight(settings, payload)
    completed = run_phase78_worker_once(settings, "demo", "alice", payload)
    approval = completed["approval"]
    revoke_key = "revocation-terminal-uncertain"
    revoke_payload = {
        "schema_version": "phase78-approval-revoke-request-v1",
        "idempotency_key": revoke_key,
        "expected_event_sha256": approval["event_sha256"],
        "revoked_at": int(time.time()) + 1,
        "reason_code": "OPERATOR_REVOKED",
    }
    with pytest.raises(Phase78OutcomeUncertain) as captured:
        revoke_phase78_approval(
            settings,
            "demo",
            str(payload["approval"]["issuer_id"]),
            approval["approval_id"],
            revoke_payload,
            deadline=_CrossingDeadline("approval revocation response"),
        )
    assert captured.value.idempotency_key == revoke_key
    status = load_phase78_status(
        replace(settings), "demo", "alice", payload["idempotency_key"]
    )
    assert status["outcome"] == "denied"
    assert status["decision"]["reason_code"] == "APPROVAL_REVOKED"
    replay = revoke_phase78_approval(
        settings,
        "demo",
        str(payload["approval"]["issuer_id"]),
        approval["approval_id"],
        revoke_payload,
    )
    assert replay["approval"]["state"] == "REVOKED"
