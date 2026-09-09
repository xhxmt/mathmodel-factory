#!/usr/bin/env python3
"""Emit the deterministic M0.3 identity/golden projection."""

from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from factory_core.classifier_identity import (  # noqa: E402
    compile_dirty_classifier_identity_bundle,
    dirty_classifier_analysis_sha256,
)
from factory_core.command_envelope import (  # noqa: E402
    ActorRefV1,
    ActorType,
    ActualPayloadV1,
    BoundEntityScopeV1,
    COMMAND_CAS_DECISION_SCHEMA,
    COMMAND_ENVELOPE_SCHEMA,
    CURRENT_FACTS_SCHEMA,
    CommandEnvelopeV1,
    CommandType,
    CurrentFactV1,
    CurrentFactsV1,
    FactScopeBindingV1,
    NoEntityScopeV1,
    NoPayloadV1,
    NoSubjectScopeV1,
    PayloadBindingV1,
    PayloadFieldV1,
    ProjectGenerationBindingV1,
    ReadSetEntryV1,
    RunGenerationBindingV1,
    command_cas_decision_sha256,
    command_envelope_sha256,
    compile_command_scope_policy,
    compile_read_set,
    current_facts_sha256,
    read_set_sha256,
    validate_command_cas,
    actual_payload_sha256,
)
from factory_core.contract_pins import (  # noqa: E402
    compile_contract_pin_analysis,
    compile_contract_pin_set,
    contract_pin_analysis_sha256,
    contract_pin_set_sha256,
)
from factory_core.persisted_dirty_owner_policy import (  # noqa: E402
    compile_persisted_dirty_owner_identity,
    persisted_dirty_owner_analysis_sha256,
)
from factory_core.project_snapshot_v0 import (  # noqa: E402
    PROJECT_SNAPSHOT_V0_SCHEMA,
    SNAPSHOT_EVENT_HEAD_CONTRACT_SCHEMA,
    SNAPSHOT_IMMUTABLE_REF_CONTRACT_SCHEMA,
    SNAPSHOT_POLICY_SCHEMA,
    SNAPSHOT_SOLVER_RECEIPT_CONTRACT_SCHEMA,
    ProjectSnapshotV0,
    SnapshotAvailabilityV0,
    SnapshotCompletenessV0,
    SnapshotCoordinateV0,
    SnapshotErrorCodeV0,
    SnapshotSectionIdV0,
    SnapshotSectionV0,
    project_snapshot_v0_analysis_sha256,
    project_snapshot_v0_semantic_sha256,
    validate_project_snapshot_v0,
)
from factory_core.workflow_contract_v2 import (  # noqa: E402
    compile_workflow_contract_bundle_v2,
    workflow_contract_v2_analysis_sha256,
    workflow_contract_v2_sha256,
)
from tests.support.m03_source_manifest import (  # noqa: E402
    contract_compiler_implementation_sha256,
)


def snapshot(*, complete: bool, pin_sha256: str, pins) -> ProjectSnapshotV0:
    coordinate = SnapshotCoordinateV0(
        schema_version="snapshot-coordinate-v0",
        project_id="fixture:m03:replay",
        workflow_schema_version=9,
        project_revision=42,
        project_generation="project-generation:1" if complete else None,
        run_generation="run-generation:1" if complete else None,
        runtime_generation="native_v2",
        scheduler_generation="stage_v1",
        recorded_contract_pin_set_sha256=pin_sha256 if complete else None,
    )
    gap_ids = {
        SnapshotSectionIdV0.DELIVERY_AUTHORIZATION: (
            SnapshotErrorCodeV0.LEGACY_DELIVERY_AUTHORIZATION_UNBOUND,
            "legacy-delivery-authorization-unbound",
        ),
        SnapshotSectionIdV0.GENERATION_BINDING: (
            SnapshotErrorCodeV0.LEGACY_PROJECT_GENERATION_UNBOUND,
            "legacy-project-run-generation-unbound",
        ),
        SnapshotSectionIdV0.CONTRACT_PINS: (
            SnapshotErrorCodeV0.LEGACY_CONTRACT_PINS_UNBOUND,
            "legacy-contract-pins-unbound",
        ),
    }
    sections = []
    for section_id in SnapshotSectionIdV0:
        if not complete and section_id in gap_ids:
            code, gap = gap_ids[section_id]
            sections.append(
                SnapshotSectionV0(
                    section_id,
                    SnapshotAvailabilityV0.UNAVAILABLE_LEGACY_UNBOUND,
                    coordinate,
                    (),
                    code,
                    gap,
                    None,
                    None,
                )
            )
        else:
            sections.append(
                SnapshotSectionV0(
                    section_id,
                    SnapshotAvailabilityV0.AVAILABLE,
                    coordinate,
                    (),
                    None,
                    None,
                    None,
                    None,
                )
            )
    return validate_project_snapshot_v0(
        ProjectSnapshotV0(
            PROJECT_SNAPSHOT_V0_SCHEMA,
            coordinate,
            SnapshotCompletenessV0.COMPLETE if complete else SnapshotCompletenessV0.PARTIAL,
            tuple(sections),
            pins if complete else None,
            False,
            (),
            (),
        )
    )


def projection() -> dict[str, object]:
    workflow = compile_workflow_contract_bundle_v2(
        analysis_evidence_refs=("m03-identity-replay",)
    )
    classifier = workflow.classifier_identity
    owner = workflow.persisted_dirty_owner_identity
    pins = compile_contract_pin_set(workflow)
    pin_sha = contract_pin_set_sha256(pins, workflow)
    compiler_sha = contract_compiler_implementation_sha256(ROOT)
    pin_analysis = compile_contract_pin_analysis(
        pin_set=pins,
        workflow=workflow,
        workflow_contract_analysis_sha256=workflow_contract_v2_analysis_sha256(workflow),
        contract_compiler_implementation_sha256=compiler_sha,
        evidence_refs=("m03-identity-replay",),
    )
    complete_snapshot = snapshot(complete=True, pin_sha256=pin_sha, pins=pins)
    partial_snapshot = snapshot(complete=False, pin_sha256=pin_sha, pins=pins)
    policy = compile_command_scope_policy(CommandType.SHADOW_ADVANCE)
    current_facts_values = tuple(
        CurrentFactV1(
            requirement.fact_type,
            requirement.fact_key,
            SnapshotAvailabilityV0.AVAILABLE,
            chr(ord("a") + index) * 64,
            None,
            None,
            None,
        )
        for index, requirement in enumerate(policy.required_facts)
    )
    current = CurrentFactsV1(
        CURRENT_FACTS_SCHEMA,
        SnapshotCompletenessV0.COMPLETE,
        ProjectGenerationBindingV1("fixture:m03:replay", "project-generation:1", 42),
        RunGenerationBindingV1("native_v2", "stage_v1", "run-generation:1"),
        NoEntityScopeV1(),
        NoSubjectScopeV1(),
        pins,
        current_facts_values,
    )
    read_set = compile_read_set(
        tuple(
            ReadSetEntryV1(
                fact.fact_type,
                fact.fact_key,
                fact.value_sha256 or "",
                None,
                None,
            )
            for fact in current_facts_values
        )
    )
    envelope = CommandEnvelopeV1(
        COMMAND_ENVELOPE_SCHEMA,
        "command:m03:replay",
        CommandType.SHADOW_ADVANCE,
        current.project_binding,
        current.run_binding,
        current.entity_scope,
        current.subject_scope,
        ActorRefV1(ActorType.TEST_FIXTURE, "actor:m03:replay"),
        NoPayloadV1(),
        read_set,
        pins,
    )
    decision = validate_command_cas(envelope, NoPayloadV1(), current, policy, workflow)
    retry_policy = compile_command_scope_policy(CommandType.SHADOW_RETRY)
    retry_scope = BoundEntityScopeV1("workflow-step", "step:5", 3)
    retry_facts = tuple(
        CurrentFactV1(
            requirement.fact_type,
            requirement.fact_key,
            SnapshotAvailabilityV0.AVAILABLE,
            chr(ord("d") + index) * 64,
            4 if requirement.scope_binding is FactScopeBindingV1.ENTITY else None,
            None,
            None,
        )
        for index, requirement in enumerate(retry_policy.required_facts)
    )
    retry_current = CurrentFactsV1(
        CURRENT_FACTS_SCHEMA,
        SnapshotCompletenessV0.COMPLETE,
        current.project_binding,
        current.run_binding,
        retry_scope,
        NoSubjectScopeV1(),
        pins,
        retry_facts,
    )
    retry_read_set = compile_read_set(
        tuple(
            ReadSetEntryV1(
                fact.fact_type,
                fact.fact_key,
                fact.value_sha256 or "",
                fact.entity_generation,
                fact.subject_sha256,
            )
            for fact in retry_facts
        )
    )
    retry_payload = ActualPayloadV1(
        retry_policy.payload_schema or "",
        (PayloadFieldV1("reason", "scope-rejection-replay"),),
    )
    retry_envelope = CommandEnvelopeV1(
        COMMAND_ENVELOPE_SCHEMA,
        "command:m03:scope-rejection-replay",
        CommandType.SHADOW_RETRY,
        retry_current.project_binding,
        retry_current.run_binding,
        retry_scope,
        NoSubjectScopeV1(),
        ActorRefV1(ActorType.TEST_FIXTURE, "actor:m03:replay"),
        PayloadBindingV1(
            retry_payload.payload_schema,
            actual_payload_sha256(retry_payload),
        ),
        retry_read_set,
        pins,
    )
    rejected = validate_command_cas(
        retry_envelope,
        retry_payload,
        retry_current,
        retry_policy,
        workflow,
    )
    return {
        "schema_version": "run4-m03-identity-replay-v1",
        "classifier": {
            "semantic_sha256": classifier.dirty_classifier_semantic_sha256,
            "operational_implementation_sha256": classifier.dirty_classifier_operational_implementation_sha256,
            "analysis_sha256": dirty_classifier_analysis_sha256(classifier),
        },
        "persisted_dirty_owner": {
            "semantic_sha256": owner.persisted_dirty_owner_policy_semantic_sha256,
            "implementation_sha256": owner.persisted_dirty_owner_policy_implementation_sha256,
            "analysis_sha256": persisted_dirty_owner_analysis_sha256(owner),
        },
        "contract_compiler_implementation_sha256": compiler_sha,
        "workflow_v2": {
            "semantic_sha256": workflow_contract_v2_sha256(workflow),
            "analysis_sha256": workflow_contract_v2_analysis_sha256(workflow),
        },
        "contract_pins": {
            "semantic_sha256": pin_sha,
            "analysis_sha256": contract_pin_analysis_sha256(pin_analysis, workflow),
        },
        "read_set_sha256": read_set_sha256(read_set),
        "current_facts_sha256": current_facts_sha256(current),
        "snapshot": {
            "schema_version": PROJECT_SNAPSHOT_V0_SCHEMA,
            "policy_schema": SNAPSHOT_POLICY_SCHEMA,
            "event_head_contract_schema": SNAPSHOT_EVENT_HEAD_CONTRACT_SCHEMA,
            "immutable_ref_contract_schema": SNAPSHOT_IMMUTABLE_REF_CONTRACT_SCHEMA,
            "solver_receipt_contract_schema": SNAPSHOT_SOLVER_RECEIPT_CONTRACT_SCHEMA,
            "complete_semantic_sha256": project_snapshot_v0_semantic_sha256(complete_snapshot),
            "complete_analysis_sha256": project_snapshot_v0_analysis_sha256(complete_snapshot),
            "partial_semantic_sha256": project_snapshot_v0_semantic_sha256(partial_snapshot),
            "partial_analysis_sha256": project_snapshot_v0_analysis_sha256(partial_snapshot),
        },
        "command_envelope_sha256": command_envelope_sha256(envelope),
        "current_facts_schema": CURRENT_FACTS_SCHEMA,
        "command_cas_decision_schema": COMMAND_CAS_DECISION_SCHEMA,
        "accepted_cas_decision_sha256": command_cas_decision_sha256(decision),
        "scope_rejected_cas_decision_sha256": command_cas_decision_sha256(rejected),
        "scope_rejection_codes": [item.code.value for item in rejected.rejections],
        "accepted_for_shadow_validation": decision.accepted_for_shadow_validation,
        "authoritative": decision.authoritative,
        "proposed_mutations": list(decision.proposed_mutations),
        "performed_side_effects": list(decision.performed_side_effects),
    }


def main() -> int:
    sys.stdout.write(
        json.dumps(projection(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
