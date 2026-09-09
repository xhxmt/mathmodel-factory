from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from factory_core.adapters.infrastructure.pause_policy import (
    PauseAction,
    PauseMode,
    ProcessScopeKind,
    decide_pause_action,
)
from factory_core.authority_envelopes import (
    EVENT_ENVELOPE_SCHEMA,
    OUTBOX_MESSAGE_SCHEMA,
    RECEIPT_ENVELOPE_SCHEMA,
    EnvelopeFieldV1,
    EventEnvelopeV1,
    OutboxMessageV1,
    ReceiptEnvelopeV1,
    event_envelope_sha256,
    receipt_envelope_bytes,
    receipt_envelope_sha256,
)
from factory_core.authority_repository import AuthorityRepository
from factory_core.authority_schema import (
    AUTHORITY_SCHEMA_VERSION,
    authority_schema_status,
    migrate_authority_schema_v2,
)
from factory_core.canonical import canonical_bytes, canonical_sha256
from factory_core.command_envelope import (
    COMMAND_ENVELOPE_SCHEMA,
    ActorRefV1,
    ActorType,
    CommandEnvelopeV1,
    CommandType,
    NoEntityScopeV1,
    NoPayloadV1,
    NoSubjectScopeV1,
    ProjectGenerationBindingV1,
    RunGenerationBindingV1,
    command_envelope_sha256,
    compile_read_set,
)
from factory_core.contract_pins import CONTRACT_PIN_SET_SCHEMA, ContractPinSetV1
from factory_core.data_egress import (
    DATA_EGRESS_APPROVAL_SCHEMA,
    DATA_EGRESS_REQUEST_SCHEMA,
    evaluate_data_egress,
    verify_data_egress_decision,
)
from factory_core.durable_operation import (
    DurableOperation,
    OperationEvent,
    OperationStatus,
    build_worker_launch_identity,
    transition_operation,
)
from factory_core.owner_compiler import compile_owner_registry
from factory_core.reference_evidence import (
    REFERENCE_EVIDENCE_SCHEMA,
    derive_reference_chunk_id,
    validate_canonical_reference_evidence,
)
from scripts.aggregate_judges import DIMENSION_SPECS, aggregate_outputs
from scripts.evidence_grounding import validate_grounding
from shadow_contracts.artifact_registry import (
    register_artifact_owner,
    validate_artifact_registration,
)
from shadow_contracts.phase2_8_integration import (
    bind_registered_artifact,
    build_shadow_chain_coordinate,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ID = "phase2-8-synthetic-model"
WORKFLOW_ID = "legacy_current"
PROJECT_GENERATION = "legacy_unknown"
RUNTIME_GENERATION = "native_v2"
SCHEDULER_GENERATION = "stage_v1"
RUN_GENERATION = "legacy_unknown"
INITIAL_REVISION = 7
COMMITTED_REVISION = 8
ARTIFACT_PATH = "results/canonical_results.json"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_text(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8", newline="\n")
    return path


def _create_ready_authority_database(root: Path) -> Path:
    root.mkdir(parents=True)
    path = root / "synthetic-authority.db"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            f"""
            CREATE TABLE schema_info(
                singleton INTEGER PRIMARY KEY,
                schema_version INTEGER NOT NULL
            );
            INSERT INTO schema_info VALUES (1, 9);
            CREATE TABLE project_state(
                singleton INTEGER PRIMARY KEY,
                project_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                last_completed_step INTEGER NOT NULL,
                active_step INTEGER,
                runtime_generation TEXT NOT NULL,
                scheduler_generation TEXT NOT NULL,
                last_completed_stage INTEGER NOT NULL,
                active_stage INTEGER
            );
            INSERT INTO project_state VALUES(
                1, '{PROJECT_ID}', {INITIAL_REVISION}, 2, NULL,
                '{RUNTIME_GENERATION}', '{SCHEDULER_GENERATION}', 1, 2
            );
            CREATE TABLE stage_checkpoints(
                stage_id INTEGER NOT NULL,
                subtask TEXT NOT NULL,
                completed_revision INTEGER,
                receipt_json TEXT,
                PRIMARY KEY(stage_id, subtask)
            );
            INSERT INTO stage_checkpoints VALUES(
                2, 'phase2-8-synthetic-ready', {INITIAL_REVISION},
                '{{"receipt":"synthetic-recorded"}}'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()
    report = migrate_authority_schema_v2(
        path, owner_token="phase2-8-integration-fixture"
    )
    assert report.state == "READY"
    assert report.authority_schema_version == AUTHORITY_SCHEMA_VERSION
    return path


def _pins() -> ContractPinSetV1:
    return ContractPinSetV1(
        CONTRACT_PIN_SET_SCHEMA,
        *(character * 64 for character in "12345678"),
    )


def _authority_bundle(artifact_sha256: str):
    command_id = "phase2-8-command-1"
    event_id = "phase2-8-event-1"
    receipt_id = "phase2-8-receipt-1"
    message_id = "phase2-8-outbox-1"
    pins = _pins()
    pin_sha256 = canonical_sha256(pins)
    command = CommandEnvelopeV1(
        schema_version=COMMAND_ENVELOPE_SCHEMA,
        command_id=command_id,
        command_type=CommandType.SHADOW_ADVANCE,
        project_binding=ProjectGenerationBindingV1(
            PROJECT_ID, PROJECT_GENERATION, INITIAL_REVISION
        ),
        run_binding=RunGenerationBindingV1(
            RUNTIME_GENERATION, SCHEDULER_GENERATION, RUN_GENERATION
        ),
        entity_scope=NoEntityScopeV1(),
        subject_scope=NoSubjectScopeV1(),
        actor=ActorRefV1(ActorType.TEST_FIXTURE, "phase2-8-integration"),
        payload_binding=NoPayloadV1(),
        read_set=compile_read_set(()),
        contract_pins=pins,
    )
    event = EventEnvelopeV1(
        EVENT_ENVELOPE_SCHEMA,
        event_id,
        PROJECT_ID,
        WORKFLOW_ID,
        COMMITTED_REVISION,
        "SHADOW_INTEGRATION_RECORDED",
        command_id,
        PROJECT_GENERATION,
        RUN_GENERATION,
        RUNTIME_GENERATION,
        SCHEDULER_GENERATION,
        pin_sha256,
        (
            EnvelopeFieldV1("artifact_path", ARTIFACT_PATH),
            EnvelopeFieldV1("artifact_sha256", artifact_sha256),
        ),
    )
    receipt = ReceiptEnvelopeV1(
        RECEIPT_ENVELOPE_SCHEMA,
        receipt_id,
        PROJECT_ID,
        WORKFLOW_ID,
        COMMITTED_REVISION,
        command_id,
        event_id,
        "RECORDED",
        pin_sha256,
        (
            EnvelopeFieldV1("artifact_sha256", artifact_sha256),
            EnvelopeFieldV1("assurance", "shadow-only"),
        ),
    )
    outbox = OutboxMessageV1(
        OUTBOX_MESSAGE_SCHEMA,
        message_id,
        WORKFLOW_ID,
        COMMITTED_REVISION,
        event_id,
        "authority.shadow.integration-recorded",
        (EnvelopeFieldV1("receipt_id", receipt_id),),
    )
    return command, event, receipt, outbox


def _read_authority_identity(path: Path) -> dict[str, object]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        workflow = connection.execute(
            "SELECT * FROM authority_workflows WHERE workflow_id=?",
            (WORKFLOW_ID,),
        ).fetchone()
        event = connection.execute(
            "SELECT envelope_sha256 FROM authority_events WHERE event_id=?",
            ("phase2-8-event-1",),
        ).fetchone()
        receipt = connection.execute(
            "SELECT envelope_sha256 FROM authority_receipts WHERE receipt_id=?",
            ("phase2-8-receipt-1",),
        ).fetchone()
        assert workflow is not None and event is not None and receipt is not None
        return {
            "project_id": workflow["project_id"],
            "workflow_id": workflow["workflow_id"],
            "project_generation": workflow["project_generation"],
            "runtime_generation": workflow["runtime_generation"],
            "scheduler_generation": workflow["scheduler_generation"],
            "run_generation": workflow["run_generation"],
            "revision": workflow["current_revision"],
            "event_sha256": event["envelope_sha256"],
            "receipt_sha256": receipt["envelope_sha256"],
        }
    finally:
        connection.close()


def _transition(operation, event, *, nonce=None, reason):
    return transition_operation(
        operation,
        event,
        expected_claim_generation=operation.claim_generation,
        dispatch_nonce=nonce,
        reason_code=reason,
    )


def _project_ui(snapshot: dict[str, object]) -> dict[str, object]:
    script = r"""
import { buildProjectSnapshotViewModel } from './web/frontend/src/lib/projectSnapshotUi.js'
let source = ''
for await (const chunk of process.stdin) source += chunk
const view = buildProjectSnapshotViewModel(JSON.parse(source))
if (!Object.isFrozen(view) || !Object.isFrozen(view.sections)) {
  throw new Error('Phase 6 view is not frozen')
}
process.stdout.write(JSON.stringify(view))
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=REPO_ROOT,
        input=json.dumps(snapshot, ensure_ascii=False),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    return json.loads(completed.stdout)


def _write_judge_inputs(
    root: Path,
    *,
    subject: str,
    artifact_binding_sha256: str,
    ui_projection_sha256: str,
    authority_receipt_sha256: str,
):
    root.mkdir()
    evidence_path = "synthetic/phase2-8-evidence.txt"
    exact_quote = f"artifact_binding_sha256={artifact_binding_sha256}"
    content = (
        f"subject={subject}\n"
        f"authority_receipt_sha256={authority_receipt_sha256}\n"
        f"{exact_quote}\n"
        f"ui_projection_sha256={ui_projection_sha256}\n"
    )
    context_path = _write_text(
        root / "context.txt",
        f"\n----- FILE: {evidence_path} -----\n{content}\n",
    )
    included = content.encode("utf-8")
    included_sha256 = _sha256_bytes(included)
    context_bytes = context_path.read_bytes()
    manifests: dict[str, Path] = {}
    roles: dict[str, Path] = {}
    quote_sha256 = _sha256_bytes(exact_quote.encode("utf-8"))

    for role in ("math", "execution", "paper"):
        chunk_id = canonical_sha256(
            {
                "schema_version": "phase2-8-judge-chunk-v1",
                "role": role,
                "subject": subject,
                "path": evidence_path,
                "included_sha256": included_sha256,
            }
        )
        manifest = {
            "role": role,
            "files": [
                {
                    "path": evidence_path,
                    "status": "included",
                    "sha256": included_sha256,
                    "included_sha256": included_sha256,
                    "included_bytes": len(included),
                    "chunk_id": chunk_id,
                    "source_line_start": 40,
                    "source_line_end": 43,
                }
            ],
            "context": {
                "sha256": _sha256_bytes(context_bytes),
                "size": len(context_bytes),
            },
            "completeness": {
                "contract_version": "judge-packet-completeness-v1",
                "status": "COMPLETE",
                "eligible": True,
                "requirements": [
                    {
                        "id": "phase2_8_identity_evidence",
                        "description": "shared synthetic chain identity",
                        "required_status": "included",
                        "paths": [evidence_path],
                        "satisfied_paths": [evidence_path],
                        "satisfied": True,
                    }
                ],
                "limitations": [],
            },
        }
        manifest_path = root / f"{role}.manifest.json"
        _write_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, sort_keys=True),
        )
        manifests[role] = manifest_path

        if role in {"math", "execution"}:
            payload = {
                "schema_version": "judge-hard-role-v2",
                "role": role,
                "verdict": "PASS",
                "fatal_flaws": 0,
                "evidence": [
                    {
                        "ref_id": f"{role}-phase2-8-binding",
                        "claim": "the result belongs to the shared shadow chain",
                        "chunk_id": chunk_id,
                        "quote": exact_quote,
                        "quote_sha256": quote_sha256,
                        "finding": "the exact binding is present once",
                        "severity": "support",
                    }
                ],
                "limitations": [],
                "conclusion": "synthetic evidence is grounded",
            }
        else:
            scores = {
                "model_presentation": 18,
                "solution_narrative": 18,
                "innovation": 17,
                "writing_clarity": 14,
                "result_persuasiveness": 14,
                "sensitivity_limitations": 9,
            }
            assert set(scores) == {key for key, _label, _maximum in DIMENSION_SPECS}
            payload = {
                "schema_version": "judge-paper-role-v3",
                "role": "paper",
                "verdict": "PASS",
                "dimensions": {
                    key: {
                        "score": score,
                        "evidence": [
                            {
                                "ref_id": f"paper-{key}-phase2-8-binding",
                                "chunk_id": chunk_id,
                                "quote": exact_quote,
                                "quote_sha256": quote_sha256,
                                "finding": "the presentation binds the shared result",
                            }
                        ],
                    }
                    for key, score in scores.items()
                },
                "overall_score": sum(scores.values()),
                "issues": [],
                "limitations": [],
                "recommendations": ["retain the deterministic identity receipt"],
                "conclusion": "synthetic paper evidence is grounded",
            }
        role_path = root / f"{role}.md"
        _write_text(
            role_path,
            f"VERDICT: PASS\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n",
        )
        roles[role] = role_path
    return roles, manifests, context_path, exact_quote, quote_sha256


def _text_fact(text: str) -> dict[str, object]:
    encoded = text.encode("utf-8")
    return {
        "text": text,
        "sha256": _sha256_bytes(encoded),
        "byte_length": len(encoded),
    }


def _reference_evidence(
    *,
    coordinate,
    artifact_binding_sha256: str,
    aggregate_sha256: str,
) -> dict[str, object]:
    reference_id = f"reference-{coordinate.coordinate_sha256[:24]}"
    raw_pdf_sha256 = canonical_sha256(
        {
            "schema_version": "phase2-8-materialized-pdf-fact-v1",
            "coordinate_sha256": coordinate.coordinate_sha256,
            "aggregate_sha256": aggregate_sha256,
        }
    )
    page_texts = (
        f"Synthetic project {coordinate.project_id} at revision {coordinate.revision}.\n"
        f"Authority receipt {coordinate.authority_receipt_sha256}.\n",
        f"Grounded artifact {artifact_binding_sha256}.\n"
        f"Judge aggregate {aggregate_sha256}.\n",
    )
    pages = []
    chunks = []
    for page_number, text in enumerate(page_texts, start=1):
        text_sha256 = _sha256_bytes(text.encode("utf-8"))
        chunk_text = text.rstrip("\n")
        chunk_sha256 = _sha256_bytes(chunk_text.encode("utf-8"))
        pages.append(
            {
                "page_number": page_number,
                "page_label": str(page_number),
                "render": {
                    "media_type": "image/png",
                    "sha256": canonical_sha256(
                        {
                            "schema_version": "phase2-8-png-render-fact-v1",
                            "raw_pdf_sha256": raw_pdf_sha256,
                            "page_number": page_number,
                            "canonical_text_sha256": text_sha256,
                        }
                    ),
                    "byte_length": 2000 + page_number,
                    "width_px": 1000,
                    "height_px": 1400,
                },
                "canonical_text": _text_fact(text),
            }
        )
        chunks.append(
            {
                "chunk_id": derive_reference_chunk_id(
                    reference_id=reference_id,
                    ordinal=page_number - 1,
                    page_start=page_number,
                    page_end=page_number,
                    text_sha256=chunk_sha256,
                ),
                "ordinal": page_number - 1,
                "page_start": page_number,
                "page_end": page_number,
                "text": chunk_text,
                "text_sha256": chunk_sha256,
                "byte_length": len(chunk_text.encode("utf-8")),
            }
        )
    metadata = {
        "title": "Phase 2-8 Synthetic Modeling Reference",
        "authors": ["Synthetic Acceptance Team"],
        "published_year": 2026,
        "doi": None,
    }
    provenance = {
        field: {
            "source_kind": "synthetic-acceptance-fact",
            "source_ref": (
                coordinate.authority_receipt_sha256
                if field in {"title", "authors"}
                else aggregate_sha256
            ),
            "value_sha256": canonical_sha256(value),
        }
        for field, value in metadata.items()
    }
    return {
        "schema_version": REFERENCE_EVIDENCE_SCHEMA,
        "reference_id": reference_id,
        "raw_pdf": {
            "blob_ref": f"sha256:{raw_pdf_sha256}",
            "sha256": raw_pdf_sha256,
            "byte_length": 8192,
        },
        "pdf_inspection": {
            "status": "valid",
            "pdf_sha256": raw_pdf_sha256,
            "page_count": 2,
            "encrypted": False,
        },
        "pages": pages,
        "chunks": chunks,
        "bibliographic_metadata": metadata,
        "metadata_provenance": provenance,
        "external_share_classification": "internal",
    }


def _run_complete_chain(root: Path) -> dict[str, object]:
    artifact_bytes = canonical_bytes(
        {
            "schema_version": "phase2-8-synthetic-result-v1",
            "project_id": PROJECT_ID,
            "run_generation": RUN_GENERATION,
            "objective_value": 42,
        }
    )
    artifact_sha256 = _sha256_bytes(artifact_bytes)

    database = _create_ready_authority_database(root / "phase2")
    status = authority_schema_status(database)
    assert status is not None
    assert status["authority_schema_version"] == AUTHORITY_SCHEMA_VERSION
    assert status["state"] == "READY"
    repository = AuthorityRepository(database, write_shadow=True)
    command, event, receipt, outbox = _authority_bundle(artifact_sha256)
    commit = repository.persist_command_bundle(
        workflow_id=WORKFLOW_ID,
        idempotency_key="phase2-8-idempotency-1",
        command=command,
        event=event,
        receipt=receipt,
        outbox=outbox,
    )
    replay = repository.persist_command_bundle(
        workflow_id=WORKFLOW_ID,
        idempotency_key="phase2-8-idempotency-1",
        command=command,
        event=event,
        receipt=receipt,
        outbox=outbox,
    )
    assert commit.committed_revision == COMMITTED_REVISION
    assert commit.request_sha256 == command_envelope_sha256(command)
    assert commit.replayed is False and replay.replayed is True
    assert repository.table_count("authority_commands") == 1
    assert repository.table_count("authority_events") == 1
    assert repository.table_count("authority_receipts") == 1
    assert repository.table_count("authority_outbox") == 1
    authority = _read_authority_identity(database)
    assert authority["event_sha256"] == event_envelope_sha256(event)
    assert authority["receipt_sha256"] == receipt_envelope_sha256(receipt)
    assert dict((item.key, item.value) for item in event.fields)[
        "artifact_sha256"
    ] == artifact_sha256
    assert dict((item.key, item.value) for item in receipt.fields)[
        "artifact_sha256"
    ] == artifact_sha256

    coordinate = build_shadow_chain_coordinate(
        project_id=str(authority["project_id"]),
        workflow_id=str(authority["workflow_id"]),
        project_generation=str(authority["project_generation"]),
        runtime_generation=str(authority["runtime_generation"]),
        scheduler_generation=str(authority["scheduler_generation"]),
        run_generation=str(authority["run_generation"]),
        revision=int(authority["revision"]),
        authority_request_sha256=commit.request_sha256,
        authority_event_sha256=str(authority["event_sha256"]),
        authority_receipt_sha256=str(authority["receipt_sha256"]),
    )
    assert coordinate.project_id == PROJECT_ID
    assert coordinate.revision == commit.committed_revision
    assert coordinate.run_generation == command.run_binding.run_generation

    compilation = compile_owner_registry()
    registration = register_artifact_owner(compilation, ARTIFACT_PATH)
    assert validate_artifact_registration(
        registration, expected_compilation=compilation
    ) is registration
    binding = bind_registered_artifact(
        coordinate=coordinate,
        registration=registration,
        artifact_sha256=artifact_sha256,
        artifact_byte_length=len(artifact_bytes),
    )
    assert binding.coordinate_sha256 == coordinate.coordinate_sha256
    assert binding.registration_sha256 == registration.registration_sha256
    assert binding.artifact_sha256 == artifact_sha256

    operation_identity = build_worker_launch_identity(
        outbox_command_id=commit.outbox_message_id,
        invocation_id=f"{PROJECT_ID}:revision:{coordinate.revision}",
        attempt_id=f"{RUN_GENERATION}:attempt:1",
        process_scope_id=f"{PROJECT_ID}:durable-solver:1",
        payload_sha256=binding.binding_sha256,
    )
    operation = DurableOperation(operation_identity)
    operation_receipts = []
    operation, transition_receipt = _transition(
        operation, OperationEvent.CLAIM, reason="LEASE_ACQUIRED"
    )
    operation_receipts.append(transition_receipt)
    operation, transition_receipt = _transition(
        operation,
        OperationEvent.CHECKPOINT_DISPATCH,
        nonce="phase2-8-dispatch-1",
        reason="DISPATCH_INTENT_DURABLE",
    )
    operation_receipts.append(transition_receipt)
    operation, transition_receipt = _transition(
        operation,
        OperationEvent.CONFIRM_ACTIVE,
        nonce="phase2-8-dispatch-1",
        reason="WORKER_READY",
    )
    operation_receipts.append(transition_receipt)

    pause = decide_pause_action(PauseMode.PAUSE, ProcessScopeKind.DURABLE_SOLVER)
    assert pause.action is PauseAction.CONTINUE
    assert pause.reason_code == "DURABLE_SOLVER_CONTINUES_ON_PAUSE"

    operation, transition_receipt = _transition(
        operation,
        OperationEvent.REQUIRE_RECONCILIATION,
        nonce="phase2-8-dispatch-1",
        reason="POST_PAUSE_RECONCILIATION",
    )
    operation_receipts.append(transition_receipt)
    assert operation.status is OperationStatus.RECONCILIATION_REQUIRED
    operation, transition_receipt = _transition(
        operation,
        OperationEvent.RECONCILE_ACTIVE,
        nonce="phase2-8-dispatch-1",
        reason="EXISTING_SCOPE_RECOVERED",
    )
    operation_receipts.append(transition_receipt)
    operation, transition_receipt = _transition(
        operation,
        OperationEvent.CONFIRM_SUCCEEDED,
        nonce="phase2-8-dispatch-1",
        reason="WORKER_EXIT_ZERO",
    )
    operation_receipts.append(transition_receipt)
    assert operation.status is OperationStatus.SUCCEEDED
    assert operation.identity.payload_sha256 == binding.binding_sha256
    assert all(
        item.operation_identity_sha256 == operation_identity.identity_sha256
        for item in operation_receipts
    )

    snapshot_id = canonical_sha256(
        {
            "schema_version": "phase2-8-ui-snapshot-identity-v1",
            "coordinate_sha256": coordinate.coordinate_sha256,
            "artifact_binding_sha256": binding.binding_sha256,
            "operation_identity_sha256": operation_identity.identity_sha256,
            "operation_receipt_sha256": operation_receipts[-1].receipt_sha256,
            "pause": pause.as_dict(),
        }
    )
    snapshot_input = {
        "state": "ready",
        "coordinate": {
            "snapshot_id": snapshot_id,
            "revision": coordinate.revision,
        },
        "sections": [
            {
                "key": "authority",
                "data": {
                    "coordinate": coordinate.as_dict(),
                    "coordinate_sha256": coordinate.coordinate_sha256,
                },
            },
            {
                "key": "artifact",
                "data": binding.as_dict(),
            },
            {
                "key": "operation",
                "data": {
                    "identity": operation_identity.as_dict(),
                    "identity_sha256": operation_identity.identity_sha256,
                    "status": operation.status.value,
                    "receipt_sha256": [
                        item.receipt_sha256 for item in operation_receipts
                    ],
                },
            },
            {"key": "pause", "data": pause.as_dict()},
        ],
        "actions": [
            {
                "id": "inspect-shadow-evidence",
                "severity": "info",
                "artifact_binding_sha256": binding.binding_sha256,
            }
        ],
    }
    view = _project_ui(snapshot_input)
    view = json.loads(json.dumps(view, ensure_ascii=False))
    assert view["state"] == "ready"
    assert view["coordinate"] == {
        "snapshot_id": snapshot_id,
        "revision": coordinate.revision,
    }
    assert all(section["coordinate"] == view["coordinate"] for section in view["sections"])
    assert view["actionCenter"]["coordinate"] == view["coordinate"]
    assert view["sections"][1]["data"]["binding_sha256"] == binding.binding_sha256
    ui_projection_sha256 = canonical_sha256(view)

    roles, manifests, context_path, exact_quote, quote_sha256 = _write_judge_inputs(
        root / "phase7",
        subject=coordinate.subject,
        artifact_binding_sha256=binding.binding_sha256,
        ui_projection_sha256=ui_projection_sha256,
        authority_receipt_sha256=coordinate.authority_receipt_sha256,
    )
    direct_grounding = validate_grounding(
        roles["math"], manifests["math"], context_path, role="math"
    )
    direct_grounding = json.loads(json.dumps(direct_grounding, ensure_ascii=False))
    assert direct_grounding["valid"] is True
    assert direct_grounding["refs"][0]["quote_sha256"] == quote_sha256
    assert exact_quote == f"artifact_binding_sha256={binding.binding_sha256}"
    aggregate = aggregate_outputs(
        math_path=roles["math"],
        execution_path=roles["execution"],
        paper_path=roles["paper"],
        math_manifest=manifests["math"],
        execution_manifest=manifests["execution"],
        paper_manifest=manifests["paper"],
    )
    assert aggregate.status == "PASS"
    assert aggregate.verdict == "PASS"
    assert all(role.status == "PASS" for role in aggregate.roles)
    assert all(
        aggregate.evidence_grounding[role]["valid"] is True
        for role in ("math", "execution", "paper")
    )
    grounding_binding = {
        role: {
            "schema_version": aggregate.evidence_grounding[role]["schema_version"],
            "valid": aggregate.evidence_grounding[role]["valid"],
            "context_sha256": aggregate.evidence_grounding[role]["context"]["sha256"],
            "refs": aggregate.evidence_grounding[role]["refs"],
        }
        for role in ("math", "execution", "paper")
    }
    grounding_sha256 = canonical_sha256(grounding_binding)
    aggregate_binding = {
        "schema_version": "phase2-8-aggregate-binding-v1",
        "subject": coordinate.subject,
        "verdict": aggregate.verdict,
        "status": aggregate.status,
        "role_statuses": {role.role: role.status for role in aggregate.roles},
        "grounding_sha256": grounding_sha256,
    }
    aggregate_sha256 = canonical_sha256(aggregate_binding)

    reference_record = validate_canonical_reference_evidence(
        _reference_evidence(
            coordinate=coordinate,
            artifact_binding_sha256=binding.binding_sha256,
            aggregate_sha256=aggregate_sha256,
        )
    )
    reference_wire = json.loads(
        json.dumps(reference_record.as_dict(), ensure_ascii=False)
    )
    assert reference_record.pdf_inspection.page_count == 2
    assert reference_record.egress_authority_granted is False
    assert reference_wire["record_sha256"] == reference_record.record_sha256
    assert canonical_sha256(
        {
            key: value
            for key, value in reference_wire.items()
            if key != "record_sha256"
        }
    ) == reference_record.record_sha256
    assert binding.binding_sha256 in reference_record.pages[1].canonical_text.text
    assert aggregate_sha256 in reference_record.pages[1].canonical_text.text
    binding_identity_wire = {
        key: value
        for key, value in binding.as_dict().items()
        if key != "binding_sha256"
    }
    reference_identity_wire = {
        key: value
        for key, value in reference_wire.items()
        if key != "record_sha256"
    }

    selected_artifacts = [
        {
            "artifact_id": "artifact-registration-binding",
            "sha256": binding.binding_sha256,
            "byte_length": len(canonical_bytes(binding_identity_wire)),
            "transfer_form": "canonical-text",
            "classification": "internal",
        },
        {
            "artifact_id": "authority-receipt",
            "sha256": coordinate.authority_receipt_sha256,
            "byte_length": len(receipt_envelope_bytes(receipt)),
            "transfer_form": "canonical-text",
            "classification": "internal",
        },
        {
            "artifact_id": "evidence-grounding-receipt",
            "sha256": grounding_sha256,
            "byte_length": len(canonical_bytes(grounding_binding)),
            "transfer_form": "canonical-text",
            "classification": "internal",
        },
        {
            "artifact_id": "reference-document-record",
            "sha256": reference_record.record_sha256,
            "byte_length": len(canonical_bytes(reference_identity_wire)),
            "transfer_form": "canonical-text",
            "classification": "internal",
        },
    ]
    egress_request = {
        "schema_version": DATA_EGRESS_REQUEST_SCHEMA,
        "subject": coordinate.subject,
        "provider": "mock-provider",
        "surface": "phase2-8-shadow-review",
        "account_scope": "synthetic-account-only",
        "retention": "ephemeral-test-run",
        "purpose": "verify-phase2-8-shadow-composition",
        "artifacts": list(reversed(selected_artifacts)),
    }
    missing_approval = evaluate_data_egress(egress_request)
    missing_wire = json.loads(json.dumps(missing_approval.as_dict()))
    assert missing_approval.status == "DENIED"
    assert missing_approval.reason_code == "APPROVAL_MISSING"
    assert missing_approval.staged_manifest.state == "STAGED"
    assert missing_approval.dispatch_performed is False
    assert verify_data_egress_decision(missing_wire)
    staged = missing_approval.staged_manifest
    approval = {
        "schema_version": DATA_EGRESS_APPROVAL_SCHEMA,
        "approval_id": f"approval-{coordinate.coordinate_sha256[:16]}",
        "approved": True,
        "staged_manifest_sha256": staged.staged_manifest_sha256,
        "subject": coordinate.subject,
        "policy_sha256": staged.policy_sha256,
        "purpose": staged.purpose,
        "artifacts": [artifact.approval_binding() for artifact in staged.artifacts],
    }
    authorized = evaluate_data_egress(egress_request, approval)
    authorized_wire = json.loads(json.dumps(authorized.as_dict()))
    assert authorized.status == "AUTHORIZED"
    assert authorized.reason_code == "EXACT_APPROVAL_MATCH"
    assert authorized.dispatch_performed is False
    assert authorized.staged_manifest == missing_approval.staged_manifest
    assert verify_data_egress_decision(authorized_wire)

    summary = {
        "schema_version": "phase2-8-shadow-integration-acceptance-v1",
        "coordinate": coordinate.as_dict(),
        "coordinate_sha256": coordinate.coordinate_sha256,
        "authority": {
            "command_id": commit.command_id,
            "event_id": commit.event_id,
            "receipt_id": commit.receipt_id,
            "outbox_message_id": commit.outbox_message_id,
            "request_sha256": commit.request_sha256,
        },
        "artifact_binding_sha256": binding.binding_sha256,
        "operation_identity_sha256": operation_identity.identity_sha256,
        "operation_receipt_sha256": [
            item.receipt_sha256 for item in operation_receipts
        ],
        "pause": pause.as_dict(),
        "ui_projection_sha256": ui_projection_sha256,
        "grounding_sha256": grounding_sha256,
        "aggregate_sha256": aggregate_sha256,
        "reference_record_sha256": reference_record.record_sha256,
        "egress": {
            "staged_manifest_sha256": staged.staged_manifest_sha256,
            "missing_approval_decision_sha256": missing_approval.decision_sha256,
            "authorized_decision_sha256": authorized.decision_sha256,
            "dispatch_performed": authorized.dispatch_performed,
        },
    }
    return {
        "summary": json.loads(json.dumps(summary, ensure_ascii=False)),
        "acceptance_sha256": canonical_sha256(summary),
    }


def test_complete_phase2_8_shadow_chain_is_identity_bound_and_deterministic(tmp_path):
    first = _run_complete_chain(tmp_path / "first")
    second = _run_complete_chain(tmp_path / "second")

    assert first == second
    assert canonical_sha256(first["summary"]) == first["acceptance_sha256"]
    assert first["summary"]["egress"]["dispatch_performed"] is False


def test_production_cli_import_does_not_load_joint_harness_or_phase8_modules():
    script = """
import sys
import factory_core.cli

assert "shadow_contracts.phase2_8_integration" not in sys.modules
assert "factory_core.reference_evidence" not in sys.modules
assert "factory_core.data_egress" not in sys.modules
print("phase2-8-shadow-integration-not-loaded")
"""
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout == "phase2-8-shadow-integration-not-loaded\n"

    references = []
    module_name = "phase2_8_integration"
    for root in (REPO_ROOT / "factory_core", REPO_ROOT / "web", REPO_ROOT / "scripts"):
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".js", ".vue", ".sh"}:
                if module_name in path.read_text(encoding="utf-8", errors="strict"):
                    references.append(str(path.relative_to(REPO_ROOT)))
    assert references == []
