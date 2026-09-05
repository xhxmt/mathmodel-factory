from __future__ import annotations

from dataclasses import replace
from functools import lru_cache

import pytest

from factory_core.command_envelope import (
    ActorRefV1,
    ActorType,
    ActualPayloadV1,
    BoundEntityScopeV1,
    BoundSubjectScopeV1,
    COMMAND_ENVELOPE_SCHEMA,
    CURRENT_FACTS_SCHEMA,
    CommandCASRejectionCode,
    CommandEnvelopeV1,
    CommandEnvelopeValidationError,
    CommandType,
    CurrentFactV1,
    CurrentFactsV1,
    FactScopeBindingV1,
    FactType,
    NoEntityScopeV1,
    NoPayloadV1,
    NoSubjectScopeV1,
    PayloadBindingV1,
    PayloadFieldV1,
    ProjectGenerationBindingV1,
    ReadSetEntryV1,
    RunGenerationBindingV1,
    command_cas_decision_bytes,
    command_envelope_bytes,
    command_envelope_sha256,
    compile_command_scope_policy,
    compile_snapshot_fact_projection_v1,
    compile_read_set,
    current_facts_from_snapshot,
    validate_command_cas,
    validate_command_envelope_structure,
    validate_command_scope_policy,
    validate_snapshot_fact_projection_v1,
)
from factory_core.contract_pins import compile_contract_pin_set, contract_pin_set_sha256
from factory_core.project_snapshot_v0 import (
    EVENT_HEAD_FACT_TYPE,
    PROJECT_SNAPSHOT_V0_SCHEMA,
    ProjectSnapshotV0,
    SOLVER_RECEIPT_FACT_TYPE,
    SnapshotAvailabilityV0,
    SnapshotCompletenessV0,
    SnapshotCoordinateV0,
    SnapshotFactV0,
    SnapshotSectionIdV0,
    SnapshotSectionV0,
    build_project_snapshot_v0,
    validate_project_snapshot_v0,
)
from factory_core.storage import SQLiteStateStore
from factory_core.workflow_contract_v2 import compile_workflow_contract_bundle_v2


@lru_cache(maxsize=None)
def _fixture(
    command_type: CommandType = CommandType.SHADOW_ADVANCE,
):
    workflow = compile_workflow_contract_bundle_v2()
    pins = compile_contract_pin_set(workflow)
    policy = compile_command_scope_policy(command_type)
    entity = (
        BoundEntityScopeV1("workflow-step", "step:5", 3)
        if policy.entity_scope == "required:workflow-step"
        else BoundEntityScopeV1("solver-job", "job:1", 2)
        if policy.entity_scope == "required:solver-job"
        else NoEntityScopeV1()
    )
    subject = (
        BoundSubjectScopeV1("pending-action", "request:1", "b" * 64)
        if policy.subject_scope == "required:pending-action"
        else BoundSubjectScopeV1("solver-receipt", "receipt:1", "c" * 64)
        if policy.subject_scope == "required:solver-receipt"
        else NoSubjectScopeV1()
    )
    facts = tuple(
        CurrentFactV1(
            requirement.fact_type,
            requirement.fact_key,
            SnapshotAvailabilityV0.AVAILABLE,
            chr(97 + index) * 64,
            entity.entity_generation
            if isinstance(entity, BoundEntityScopeV1)
            and requirement.scope_binding
            in {FactScopeBindingV1.ENTITY, FactScopeBindingV1.ENTITY_AND_SUBJECT}
            else None,
            subject.subject_sha256
            if isinstance(subject, BoundSubjectScopeV1)
            and requirement.scope_binding
            in {FactScopeBindingV1.SUBJECT, FactScopeBindingV1.ENTITY_AND_SUBJECT}
            else None,
            None,
        )
        for index, requirement in enumerate(policy.required_facts)
    )
    current = CurrentFactsV1(
        CURRENT_FACTS_SCHEMA,
        SnapshotCompletenessV0.COMPLETE,
        ProjectGenerationBindingV1("project:m03", "project-generation:1", 42),
        RunGenerationBindingV1("native_v2", "stage_v1", "run-generation:9"),
        entity,
        subject,
        pins,
        facts,
    )
    read_set = compile_read_set(
        tuple(
            ReadSetEntryV1(
                fact.fact_type,
                fact.fact_key,
                fact.value_sha256 or "",
                fact.entity_generation,
                fact.subject_sha256,
            )
            for fact in facts
        )
    )
    if policy.payload_schema is None:
        actual_payload = NoPayloadV1()
        payload_binding = NoPayloadV1()
    else:
        actual_payload = ActualPayloadV1(
            policy.payload_schema,
            (PayloadFieldV1("reason", "fixture"),),
        )
        from factory_core.command_envelope import actual_payload_sha256

        payload_binding = PayloadBindingV1(
            policy.payload_schema,
            actual_payload_sha256(actual_payload),
        )
    envelope = CommandEnvelopeV1(
        COMMAND_ENVELOPE_SCHEMA,
        "command:fixture",
        command_type,
        current.project_binding,
        current.run_binding,
        entity,
        subject,
        ActorRefV1(ActorType.TEST_FIXTURE, "actor:fixture"),
        payload_binding,
        read_set,
        pins,
    )
    return workflow, policy, current, actual_payload, envelope


@pytest.mark.parametrize(
    "command_type",
    [
        CommandType.SHADOW_ADVANCE,
        CommandType.SHADOW_RETRY,
        CommandType.SHADOW_APPLY_DECISION,
        CommandType.SHADOW_RECORD_SOLVER_FACT,
    ],
)
def test_synthetic_complete_current_facts_can_pass_shadow_cas(command_type) -> None:
    workflow, policy, current, payload, envelope = _fixture(command_type)
    decision = validate_command_cas(envelope, payload, current, policy, workflow)
    assert decision.accepted_for_shadow_validation is True
    assert decision.rejections == ()
    assert decision.authoritative is False
    assert decision.proposed_mutations == ()
    assert decision.performed_side_effects == ()


@pytest.mark.parametrize(
    ("field_name", "code"),
    [
        ("project_id", CommandCASRejectionCode.PROJECT_ID_MISMATCH),
        ("project_generation", CommandCASRejectionCode.PROJECT_GENERATION_MISMATCH),
        ("project_revision", CommandCASRejectionCode.PROJECT_REVISION_MISMATCH),
        ("runtime_generation", CommandCASRejectionCode.RUNTIME_GENERATION_MISMATCH),
        ("scheduler_generation", CommandCASRejectionCode.SCHEDULER_GENERATION_MISMATCH),
        ("run_generation", CommandCASRejectionCode.RUN_GENERATION_MISMATCH),
    ],
)
def test_generation_and_revision_mismatches_return_typed_rejections(field_name, code) -> None:
    workflow, policy, current, payload, envelope = _fixture()
    if field_name in {"project_id", "project_generation", "project_revision"}:
        binding = envelope.project_binding
        value = binding.project_revision + 1 if field_name == "project_revision" else getattr(binding, field_name) + ":stale"
        envelope = replace(envelope, project_binding=replace(binding, **{field_name: value}))
    else:
        binding = envelope.run_binding
        envelope = replace(envelope, run_binding=replace(binding, **{field_name: getattr(binding, field_name) + ":stale"}))
    decision = validate_command_cas(envelope, payload, current, policy, workflow)
    assert code in {item.code for item in decision.rejections}


def test_partial_legacy_snapshot_facts_can_never_be_accepted() -> None:
    workflow, policy, current, payload, envelope = _fixture()
    current = replace(current, snapshot_completeness=SnapshotCompletenessV0.PARTIAL)
    decision = validate_command_cas(envelope, payload, current, policy, workflow)
    assert decision.accepted_for_shadow_validation is False
    assert decision.rejections[0].code is CommandCASRejectionCode.SNAPSHOT_INCOMPLETE


@pytest.mark.parametrize(
    "availability",
    [
        SnapshotAvailabilityV0.ERROR,
        SnapshotAvailabilityV0.UNAVAILABLE_LEGACY_UNBOUND,
        SnapshotAvailabilityV0.REDACTED,
        SnapshotAvailabilityV0.PAGED,
    ],
)
def test_required_unavailable_fact_fails_closed(availability) -> None:
    workflow, policy, current, payload, envelope = _fixture()
    first = current.facts[0]
    unavailable = replace(
        first,
        availability=availability,
        value_sha256=None,
        status_detail=f"{availability.value}:fixture",
    )
    current = replace(current, facts=(unavailable,) + current.facts[1:])
    decision = validate_command_cas(envelope, payload, current, policy, workflow)
    assert CommandCASRejectionCode.REQUIRED_FACT_UNAVAILABLE in {
        item.code for item in decision.rejections
    }
    assert CommandCASRejectionCode.READ_SET_MISMATCH in {
        item.code for item in decision.rejections
    }


def test_read_set_hash_entries_order_duplicates_and_current_mismatch_fail_closed() -> None:
    workflow, policy, current, payload, envelope = _fixture()
    with pytest.raises(CommandEnvelopeValidationError):
        validate_command_envelope_structure(
            replace(envelope, read_set=replace(envelope.read_set, entries_sha256="0" * 64))
        )
    if len(envelope.read_set.entries) > 1:
        with pytest.raises(CommandEnvelopeValidationError):
            compile_read_set(tuple(reversed(envelope.read_set.entries)) + (envelope.read_set.entries[0],))
    changed = replace(current.facts[0], value_sha256="9" * 64)
    current = replace(current, facts=(changed,) + current.facts[1:])
    decision = validate_command_cas(envelope, payload, current, policy, workflow)
    assert CommandCASRejectionCode.READ_SET_MISMATCH in {item.code for item in decision.rejections}


def _envelope_with_current_read_set(
    envelope: CommandEnvelopeV1,
    current: CurrentFactsV1,
) -> CommandEnvelopeV1:
    return replace(
        envelope,
        read_set=compile_read_set(
            tuple(
                ReadSetEntryV1(
                    fact.fact_type,
                    fact.fact_key,
                    fact.value_sha256 or "",
                    fact.entity_generation,
                    fact.subject_sha256,
                )
                for fact in current.facts
                if fact.availability is SnapshotAvailabilityV0.AVAILABLE
            )
        ),
    )


def test_entity_bound_fact_generation_rejects_coherent_read_set_forgery() -> None:
    workflow, policy, current, payload, envelope = _fixture(CommandType.SHADOW_RETRY)
    facts = tuple(
        replace(fact, entity_generation=4)
        if fact.fact_type is FactType.STAGE_CURSOR
        else fact
        for fact in current.facts
    )
    current = replace(current, facts=facts)
    envelope = _envelope_with_current_read_set(envelope, current)
    decision = validate_command_cas(envelope, payload, current, policy, workflow)
    assert decision.accepted_for_shadow_validation is False
    assert CommandCASRejectionCode.ENTITY_GENERATION_MISMATCH in {
        item.code for item in decision.rejections
    }
    assert CommandCASRejectionCode.READ_SET_MISMATCH not in {
        item.code for item in decision.rejections
    }


def test_subject_bound_fact_rejects_coherent_read_set_forgery() -> None:
    workflow, policy, current, payload, envelope = _fixture(
        CommandType.SHADOW_APPLY_DECISION
    )
    facts = tuple(
        replace(fact, subject_sha256="d" * 64)
        if fact.fact_type is FactType.PENDING_ACTION
        else fact
        for fact in current.facts
    )
    current = replace(current, facts=facts)
    envelope = _envelope_with_current_read_set(envelope, current)
    decision = validate_command_cas(envelope, payload, current, policy, workflow)
    assert decision.accepted_for_shadow_validation is False
    assert CommandCASRejectionCode.SUBJECT_FINGERPRINT_MISMATCH in {
        item.code for item in decision.rejections
    }
    assert CommandCASRejectionCode.READ_SET_MISMATCH not in {
        item.code for item in decision.rejections
    }


@pytest.mark.parametrize(
    ("command_type", "fact_type", "field_name", "code"),
    [
        (
            CommandType.SHADOW_RETRY,
            FactType.STAGE_CURSOR,
            "entity_generation",
            CommandCASRejectionCode.ENTITY_GENERATION_MISMATCH,
        ),
        (
            CommandType.SHADOW_APPLY_DECISION,
            FactType.PENDING_ACTION,
            "subject_sha256",
            CommandCASRejectionCode.SUBJECT_FINGERPRINT_MISMATCH,
        ),
    ],
)
def test_bound_fact_none_is_a_typed_scope_rejection(
    command_type, fact_type, field_name, code
) -> None:
    workflow, policy, current, payload, envelope = _fixture(command_type)
    facts = tuple(
        replace(fact, **{field_name: None}) if fact.fact_type is fact_type else fact
        for fact in current.facts
    )
    current = replace(current, facts=facts)
    envelope = _envelope_with_current_read_set(envelope, current)
    decision = validate_command_cas(envelope, payload, current, policy, workflow)
    assert code in {item.code for item in decision.rejections}


def test_partial_bound_fact_mismatch_does_not_hide_other_matching_binding() -> None:
    workflow, policy, current, payload, envelope = _fixture(
        CommandType.SHADOW_RECORD_SOLVER_FACT
    )
    facts = tuple(
        replace(fact, subject_sha256="d" * 64)
        if fact.fact_type is FactType.DIRTY_OWNER
        else fact
        for fact in current.facts
    )
    current = replace(current, facts=facts)
    envelope = _envelope_with_current_read_set(envelope, current)
    decision = validate_command_cas(envelope, payload, current, policy, workflow)
    codes = {item.code for item in decision.rejections}
    assert CommandCASRejectionCode.SUBJECT_FINGERPRINT_MISMATCH in codes
    assert CommandCASRejectionCode.ENTITY_GENERATION_MISMATCH not in codes


def test_scope_binding_policy_is_source_authorized() -> None:
    policy = compile_command_scope_policy(CommandType.SHADOW_RETRY)
    forged = replace(
        policy,
        required_facts=policy.required_facts[:-1]
        + (replace(policy.required_facts[-1], scope_binding=FactScopeBindingV1.NONE),),
    )
    with pytest.raises(CommandEnvelopeValidationError):
        validate_command_scope_policy(forged)


def _synthetic_complete_snapshot(command_type: CommandType):
    workflow = compile_workflow_contract_bundle_v2()
    pins = compile_contract_pin_set(workflow)
    coordinate = SnapshotCoordinateV0(
        "snapshot-coordinate-v0",
        "project:m03",
        9,
        42,
        "project-generation:1",
        "run-generation:9",
        "native_v2",
        "stage_v1",
        contract_pin_set_sha256(pins, workflow),
    )
    facts_by_section = {
        SnapshotSectionIdV0.PROJECT_STATE_SCHEMA: (
            SnapshotFactV0(
                "project_state", "project", "a" * 64, None, None, None, None, None, None, None
            ),
        ),
        SnapshotSectionIdV0.EVENT_HEAD_CHAIN: (
            SnapshotFactV0(
                EVENT_HEAD_FACT_TYPE, "project", "b" * 64, None, None, None, None, None, None, None
            ),
        ),
        SnapshotSectionIdV0.STAGE_CURSOR_CHECKPOINTS: (
            SnapshotFactV0(
                "recorded_stage_cursor",
                "active",
                "c" * 64,
                "workflow-step",
                "step:5",
                3,
                None,
                None,
                None,
                None,
            ),
        ),
        SnapshotSectionIdV0.PENDING_HUMAN: (
            SnapshotFactV0(
                "recorded_pending_action",
                "active",
                "d" * 64,
                None,
                None,
                None,
                "pending-action",
                "request:1",
                "b" * 64,
                None,
            ),
        ),
        SnapshotSectionIdV0.DIRTY_FACTS: (
            SnapshotFactV0(
                "solver_receipt_dirty_owner",
                "solver-receipt",
                "e" * 64,
                None,
                None,
                None,
                "solver-receipt",
                "receipt:1",
                "c" * 64,
                "f" * 64,
            ),
        ),
        SnapshotSectionIdV0.INVOCATIONS_SOLVER: (
            SnapshotFactV0(
                "bound_solver_job",
                "bound",
                "f" * 64,
                "solver-job",
                "job:1",
                2,
                None,
                None,
                None,
                None,
            ),
        ),
    }
    sections = tuple(
        SnapshotSectionV0(
            section_id,
            SnapshotAvailabilityV0.AVAILABLE,
            coordinate,
            facts_by_section.get(section_id, ()),
            None,
            None,
            None,
            None,
        )
        for section_id in SnapshotSectionIdV0
    )
    snapshot = validate_project_snapshot_v0(
        ProjectSnapshotV0(
            PROJECT_SNAPSHOT_V0_SCHEMA,
            coordinate,
            SnapshotCompletenessV0.COMPLETE,
            sections,
            pins,
            False,
            (),
            (),
        )
    )
    return workflow, pins, snapshot


@pytest.mark.parametrize(
    "command_type",
    [
        CommandType.SHADOW_ADVANCE,
        CommandType.SHADOW_RETRY,
        CommandType.SHADOW_APPLY_DECISION,
        CommandType.SHADOW_RECORD_SOLVER_FACT,
    ],
)
def test_complete_snapshot_bridge_can_reach_shadow_cas_acceptance(command_type) -> None:
    workflow, pins, snapshot = _synthetic_complete_snapshot(command_type)
    current = current_facts_from_snapshot(snapshot, command_type, workflow)
    policy = compile_command_scope_policy(command_type)
    read_set = compile_read_set(
        tuple(
            ReadSetEntryV1(
                fact.fact_type,
                fact.fact_key,
                fact.value_sha256 or "",
                fact.entity_generation,
                fact.subject_sha256,
            )
            for fact in current.facts
        )
    )
    if policy.payload_schema is None:
        payload = NoPayloadV1()
        binding = NoPayloadV1()
    else:
        payload = ActualPayloadV1(
            policy.payload_schema,
            (PayloadFieldV1("reason", "snapshot-bridge"),),
        )
        from factory_core.command_envelope import actual_payload_sha256

        binding = PayloadBindingV1(policy.payload_schema, actual_payload_sha256(payload))
    envelope = CommandEnvelopeV1(
        COMMAND_ENVELOPE_SCHEMA,
        "command:snapshot-bridge",
        command_type,
        current.project_binding,
        current.run_binding,
        current.entity_scope,
        current.subject_scope,
        ActorRefV1(ActorType.TEST_FIXTURE, "actor:snapshot-bridge"),
        binding,
        read_set,
        pins,
    )
    decision = validate_command_cas(envelope, payload, current, policy, workflow)
    assert decision.accepted_for_shadow_validation is True
    assert decision.rejections == ()


@pytest.mark.parametrize("status", ["queued", "cancelling"])
def test_recorded_solver_lifecycle_generation_reaches_snapshot_cas_bridge(
    tmp_path, status: str
) -> None:
    store = SQLiteStateStore(tmp_path / status, clock=lambda: 1)
    state = store.initialize(project_id=f"project:m03:{status}", project_type="test")
    state = store.create_solver_job(
        expected_revision=state.revision,
        record={
            "job_id": f"job:{status}",
            "backend": "local",
            "runtime": "python",
            "script": "solve.py",
            "workdir": ".",
            "argv": [],
            "max_time_seconds": 30,
            "status": "submitting",
            "result_refs": {},
        },
    )
    store.update_solver_job(
        f"job:{status}",
        expected_job_revision=1,
        status=status,
    )
    recorded = build_project_snapshot_v0(str(store.path))
    assert recorded.snapshot is not None
    recorded_section = next(
        section
        for section in recorded.snapshot.sections
        if section.section_id is SnapshotSectionIdV0.INVOCATIONS_SOLVER
    )
    recorded_fact = next(
        fact for fact in recorded_section.facts if fact.fact_type == "bound_solver_job"
    )

    command_type = CommandType.SHADOW_RECORD_SOLVER_FACT
    workflow, pins, snapshot = _synthetic_complete_snapshot(command_type)
    section_index = next(
        index
        for index, section in enumerate(snapshot.sections)
        if section.section_id is SnapshotSectionIdV0.INVOCATIONS_SOLVER
    )
    section = snapshot.sections[section_index]
    injected = replace(section, facts=(recorded_fact,))
    snapshot = validate_project_snapshot_v0(
        replace(
            snapshot,
            sections=(
                snapshot.sections[:section_index]
                + (injected,)
                + snapshot.sections[section_index + 1 :]
            ),
        )
    )
    current = current_facts_from_snapshot(snapshot, command_type, workflow)
    assert current.entity_scope == BoundEntityScopeV1(
        "solver-job", f"job:{status}", 2
    )
    solver_fact = next(fact for fact in current.facts if fact.fact_type is FactType.SOLVER_JOB)
    assert solver_fact.entity_generation == 2

    policy = compile_command_scope_policy(command_type)
    read_set = compile_read_set(
        tuple(
            ReadSetEntryV1(
                fact.fact_type,
                fact.fact_key,
                fact.value_sha256 or "",
                fact.entity_generation,
                fact.subject_sha256,
            )
            for fact in current.facts
        )
    )
    payload = ActualPayloadV1(
        policy.payload_schema or "",
        (PayloadFieldV1("reason", f"recorded-{status}"),),
    )
    from factory_core.command_envelope import actual_payload_sha256

    envelope = CommandEnvelopeV1(
        COMMAND_ENVELOPE_SCHEMA,
        f"command:recorded-{status}",
        command_type,
        current.project_binding,
        current.run_binding,
        current.entity_scope,
        current.subject_scope,
        ActorRefV1(ActorType.TEST_FIXTURE, "actor:recorded-solver"),
        PayloadBindingV1(policy.payload_schema or "", actual_payload_sha256(payload)),
        read_set,
        pins,
    )
    decision = validate_command_cas(envelope, payload, current, policy, workflow)
    assert decision.accepted_for_shadow_validation is True
    assert decision.rejections == ()


def test_recorded_solver_receipt_snapshot_reaches_non_authoritative_cas_bridge(
    tmp_path,
) -> None:
    store = SQLiteStateStore(tmp_path / "receipt", clock=lambda: 1)
    state = store.initialize(project_id="project:m03:receipt", project_type="test")
    store.create_solver_job(
        expected_revision=state.revision,
        record={
            "job_id": "job_receipt_bridge",
            "backend": "local",
            "runtime": "python",
            "script": "solve.py",
            "workdir": ".",
            "argv": [],
            "max_time_seconds": 30,
            "status": "submitting",
            "result_refs": {},
        },
    )
    store.record_solver_receipt(
        "job_receipt_bridge",
        stage="submitted",
        receipt_path=".factory/solver_receipts/job_receipt_bridge.submitted.json",
        receipt_sha256="a" * 64,
        content_sha256="b" * 64,
        request_sha256="c" * 64,
    )
    recorded = build_project_snapshot_v0(str(store.path))
    assert recorded.availability is SnapshotAvailabilityV0.AVAILABLE
    assert recorded.snapshot is not None
    recorded_section = next(
        section
        for section in recorded.snapshot.sections
        if section.section_id is SnapshotSectionIdV0.INVOCATIONS_SOLVER
    )
    assert any(
        fact.fact_type == SOLVER_RECEIPT_FACT_TYPE
        for fact in recorded_section.facts
    )

    command_type = CommandType.SHADOW_RECORD_SOLVER_FACT
    workflow, pins, snapshot = _synthetic_complete_snapshot(command_type)
    section_index = next(
        index
        for index, section in enumerate(snapshot.sections)
        if section.section_id is SnapshotSectionIdV0.INVOCATIONS_SOLVER
    )
    snapshot = validate_project_snapshot_v0(
        replace(
            snapshot,
            sections=(
                snapshot.sections[:section_index]
                + (replace(snapshot.sections[section_index], facts=recorded_section.facts),)
                + snapshot.sections[section_index + 1 :]
            ),
        )
    )
    current = current_facts_from_snapshot(snapshot, command_type, workflow)
    assert current.entity_scope == BoundEntityScopeV1(
        "solver-job", "job_receipt_bridge", 1
    )
    policy = compile_command_scope_policy(command_type)
    read_set = compile_read_set(
        tuple(
            ReadSetEntryV1(
                fact.fact_type,
                fact.fact_key,
                fact.value_sha256 or "",
                fact.entity_generation,
                fact.subject_sha256,
            )
            for fact in current.facts
        )
    )
    payload = ActualPayloadV1(
        policy.payload_schema or "",
        (PayloadFieldV1("reason", "recorded-receipt"),),
    )
    from factory_core.command_envelope import actual_payload_sha256

    envelope = CommandEnvelopeV1(
        COMMAND_ENVELOPE_SCHEMA,
        "command:recorded-receipt",
        command_type,
        current.project_binding,
        current.run_binding,
        current.entity_scope,
        current.subject_scope,
        ActorRefV1(ActorType.TEST_FIXTURE, "actor:recorded-receipt"),
        PayloadBindingV1(policy.payload_schema or "", actual_payload_sha256(payload)),
        read_set,
        pins,
    )
    decision = validate_command_cas(envelope, payload, current, policy, workflow)
    assert decision.accepted_for_shadow_validation is True
    assert decision.rejections == ()
    assert decision.authoritative is False
    assert decision.proposed_mutations == ()
    assert decision.performed_side_effects == ()


def test_complete_snapshot_missing_recoverable_pins_fails_bridge() -> None:
    workflow, _pins, snapshot = _synthetic_complete_snapshot(CommandType.SHADOW_ADVANCE)
    forged = replace(
        snapshot,
        coordinate=replace(snapshot.coordinate, recorded_contract_pin_set_sha256=None),
        sections=tuple(
            replace(
                section,
                coordinate=replace(section.coordinate, recorded_contract_pin_set_sha256=None),
            )
            for section in snapshot.sections
        ),
        contract_pins=None,
    )
    with pytest.raises(CommandEnvelopeValidationError):
        current_facts_from_snapshot(forged, CommandType.SHADOW_ADVANCE, workflow)


def test_legacy_sqlite_partial_snapshot_is_a_typed_bridge_rejection(tmp_path) -> None:
    store = SQLiteStateStore(tmp_path / "legacy-project", clock=lambda: 1)
    store.initialize(project_id="project:m03:legacy", project_type="test")
    result = build_project_snapshot_v0(str(store.path))
    assert result.snapshot is not None
    assert result.snapshot.completeness is SnapshotCompletenessV0.PARTIAL
    workflow = compile_workflow_contract_bundle_v2()
    with pytest.raises(
        CommandEnvelopeValidationError,
        match="legacy PARTIAL snapshot has no project/run generation binding",
    ):
        current_facts_from_snapshot(
            result.snapshot,
            CommandType.SHADOW_ADVANCE,
            workflow,
        )


def test_snapshot_projection_duplicate_fails_before_current_facts() -> None:
    workflow, _pins, snapshot = _synthetic_complete_snapshot(CommandType.SHADOW_ADVANCE)
    index = next(
        index
        for index, section in enumerate(snapshot.sections)
        if section.section_id is SnapshotSectionIdV0.PROJECT_STATE_SCHEMA
    )
    section = snapshot.sections[index]
    duplicate = replace(section.facts[0], value_sha256="9" * 64)
    forged_section = replace(section, facts=section.facts + (duplicate,))
    forged = replace(
        snapshot,
        sections=snapshot.sections[:index] + (forged_section,) + snapshot.sections[index + 1 :],
    )
    with pytest.raises(CommandEnvelopeValidationError):
        current_facts_from_snapshot(forged, CommandType.SHADOW_ADVANCE, workflow)


def test_snapshot_projection_mapping_is_source_authorized_and_unique() -> None:
    projection = compile_snapshot_fact_projection_v1()
    assert validate_snapshot_fact_projection_v1(projection) is projection
    with pytest.raises(CommandEnvelopeValidationError, match="source-authorized"):
        validate_snapshot_fact_projection_v1(
            projection[:-1]
            + (replace(projection[-1], source_fact_key="coherent-forgery"),)
        )
    with pytest.raises(CommandEnvelopeValidationError, match="duplicate mapping"):
        validate_snapshot_fact_projection_v1(
            projection
            + (
                replace(
                    projection[-1],
                    target_fact_type=projection[0].target_fact_type,
                    target_fact_key=projection[0].target_fact_key,
                ),
            )
        )


def test_payload_is_recomputed_and_no_payload_is_explicit() -> None:
    workflow, policy, current, payload, envelope = _fixture(CommandType.SHADOW_RETRY)
    changed = replace(payload, fields=(PayloadFieldV1("reason", "changed"),))
    decision = validate_command_cas(envelope, changed, current, policy, workflow)
    assert CommandCASRejectionCode.PAYLOAD_MISMATCH in {item.code for item in decision.rejections}
    workflow, policy, current, payload, envelope = _fixture(CommandType.SHADOW_ADVANCE)
    assert validate_command_cas(envelope, payload, current, policy, workflow).accepted_for_shadow_validation


def test_entity_and_subject_sum_types_cannot_be_omitted_with_none_or_empty_sentinels() -> None:
    workflow, policy, current, payload, envelope = _fixture(CommandType.SHADOW_RETRY)
    with pytest.raises(CommandEnvelopeValidationError):
        validate_command_envelope_structure(replace(envelope, entity_scope=None))
    with pytest.raises(CommandEnvelopeValidationError):
        validate_command_envelope_structure(
            replace(envelope, entity_scope=BoundEntityScopeV1("workflow-step", "", 1))
        )
    workflow, policy, current, payload, envelope = _fixture(CommandType.SHADOW_APPLY_DECISION)
    decision = validate_command_cas(
        replace(envelope, subject_scope=NoSubjectScopeV1()), payload, current, policy, workflow
    )
    assert CommandCASRejectionCode.SUBJECT_SCOPE_MISMATCH in {
        item.code for item in decision.rejections
    }


def test_only_dirty_owner_policy_commands_compare_persisted_owner_pins() -> None:
    workflow, policy, current, payload, envelope = _fixture(CommandType.SHADOW_ADVANCE)
    current = replace(
        current,
        contract_pins=replace(
            current.contract_pins,
            persisted_dirty_owner_policy_semantic_sha256="f" * 64,
            persisted_dirty_owner_policy_implementation_sha256="e" * 64,
        ),
    )
    assert validate_command_cas(envelope, payload, current, policy, workflow).accepted_for_shadow_validation
    workflow, policy, current, payload, envelope = _fixture(CommandType.SHADOW_RECORD_SOLVER_FACT)
    current = replace(
        current,
        contract_pins=replace(
            current.contract_pins,
            persisted_dirty_owner_policy_semantic_sha256="f" * 64,
        ),
    )
    decision = validate_command_cas(envelope, payload, current, policy, workflow)
    assert CommandCASRejectionCode.PERSISTED_OWNER_PIN_MISMATCH in {
        item.code for item in decision.rejections
    }


def test_supplied_pin_self_forgery_is_rejected_before_cas_decision() -> None:
    workflow, policy, current, payload, envelope = _fixture()
    envelope = replace(
        envelope,
        contract_pins=replace(envelope.contract_pins, dirty_classifier_semantic_sha256="f" * 64),
    )
    with pytest.raises(Exception):
        validate_command_cas(envelope, payload, current, policy, workflow)


def test_unsupported_command_returns_typed_rejection() -> None:
    workflow, policy, current, payload, envelope = _fixture(CommandType.UNSUPPORTED)
    decision = validate_command_cas(envelope, payload, current, policy, workflow)
    assert decision.rejections[0].code is CommandCASRejectionCode.UNSUPPORTED_COMMAND


def test_registered_enum_identity_exact_dto_utf8_and_serializer_boundary() -> None:
    _workflow, _policy, _current, _payload, envelope = _fixture()
    forged_enum = str.__new__(CommandType, "PWN")
    forged_enum._name_ = "PWN"
    forged_enum._value_ = "PWN"
    with pytest.raises(CommandEnvelopeValidationError):
        command_envelope_bytes(replace(envelope, command_type=forged_enum))
    with pytest.raises(CommandEnvelopeValidationError):
        command_envelope_bytes(replace(envelope, command_id="bad\ud800"))
    subclass = type("ForgedEnvelope", (CommandEnvelopeV1,), {})
    forged = object.__new__(subclass)
    for field_name in envelope.__dataclass_fields__:
        object.__setattr__(forged, field_name, getattr(envelope, field_name))
    with pytest.raises(CommandEnvelopeValidationError):
        command_envelope_sha256(forged)
    with pytest.raises(CommandEnvelopeValidationError):
        command_envelope_bytes(object.__new__(CommandEnvelopeV1))


def test_envelope_and_decision_golden() -> None:
    workflow, policy, current, payload, envelope = _fixture()
    decision = validate_command_cas(envelope, payload, current, policy, workflow)
    assert command_envelope_sha256(envelope) == (
        "d62d406da4fff43004de0e7b119c8300fc8652f14c908b0b9e37b1efb7e7c11e"
    )
    assert command_cas_decision_bytes(decision)
