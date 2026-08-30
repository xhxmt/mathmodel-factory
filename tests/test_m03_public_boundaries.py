from __future__ import annotations

from dataclasses import fields, replace

import pytest

from factory_core.classifier_identity import (
    DirtyClassifierIdentityBundleV1,
    DirtyClassifierIdentityValidationError,
    compile_dirty_classifier_identity_bundle,
    dirty_classifier_analysis_sha256,
)
from factory_core.command_envelope import (
    ActorRefV1,
    ActorType,
    COMMAND_ENVELOPE_SCHEMA,
    CommandEnvelopeV1,
    CommandEnvelopeValidationError,
    CommandType,
    NoEntityScopeV1,
    NoPayloadV1,
    NoSubjectScopeV1,
    ProjectGenerationBindingV1,
    RunGenerationBindingV1,
    command_envelope_bytes,
    compile_read_set,
)
from factory_core.contract_pins import compile_contract_pin_set
from factory_core.persisted_dirty_owner_policy import (
    PersistedDirtyOwnerIdentityV1,
    PersistedDirtyOwnerPolicyValidationError,
    compile_persisted_dirty_owner_identity,
    validate_persisted_dirty_owner_identity,
)
from factory_core.project_snapshot_v0 import (
    PROJECT_SNAPSHOT_V0_SCHEMA,
    ProjectSnapshotV0,
    SnapshotCompletenessV0,
    SnapshotCoordinateV0,
    SnapshotV0ValidationError,
    project_snapshot_v0_semantic_sha256,
)
from factory_core.workflow_contract_v2 import (
    WorkflowContractBundleV2,
    WorkflowContractV2ValidationError,
    compile_workflow_contract_bundle_v2,
    workflow_contract_v2_analysis_sha256,
)


def _subclass_clone(value):
    subclass = type(f"Forged{type(value).__name__}", (type(value),), {})
    forged = object.__new__(subclass)
    for item in fields(value):
        object.__setattr__(forged, item.name, getattr(value, item.name))
    object.__setattr__(forged, "mutable_extra", [])
    return forged


@pytest.mark.parametrize(
    ("builder", "serializer", "error"),
    [
        (
            compile_dirty_classifier_identity_bundle,
            dirty_classifier_analysis_sha256,
            DirtyClassifierIdentityValidationError,
        ),
        (
            compile_persisted_dirty_owner_identity,
            validate_persisted_dirty_owner_identity,
            PersistedDirtyOwnerPolicyValidationError,
        ),
        (
            compile_workflow_contract_bundle_v2,
            workflow_contract_v2_analysis_sha256,
            WorkflowContractV2ValidationError,
        ),
    ],
)
def test_identity_public_boundaries_reject_uninitialized_and_subclass_values(
    builder, serializer, error
) -> None:
    value = builder()
    with pytest.raises(error):
        serializer(object.__new__(type(value)))
    with pytest.raises(error):
        serializer(_subclass_clone(value))


def test_analysis_only_fields_are_still_structurally_validated() -> None:
    classifier = compile_dirty_classifier_identity_bundle()
    with pytest.raises(DirtyClassifierIdentityValidationError):
        dirty_classifier_analysis_sha256(
            replace(classifier, analysis_evidence_refs=("bad\ud800",))
        )
    owner = compile_persisted_dirty_owner_identity()
    with pytest.raises(PersistedDirtyOwnerPolicyValidationError):
        validate_persisted_dirty_owner_identity(
            replace(owner, analysis_evidence_refs=("bad\ud800",))
        )


def test_tuple_subclasses_are_rejected_across_identity_boundaries() -> None:
    class ForgedTuple(tuple):
        pass

    classifier = compile_dirty_classifier_identity_bundle()
    with pytest.raises(DirtyClassifierIdentityValidationError):
        dirty_classifier_analysis_sha256(
            replace(classifier, analysis_evidence_refs=ForgedTuple(("evidence",)))
        )


def test_snapshot_coordinate_requires_plain_int_not_bool_and_strict_utf8() -> None:
    coordinate = SnapshotCoordinateV0(
        "snapshot-coordinate-v0",
        "p",
        9,
        1,
        "pg",
        "rg",
        "native_v2",
        "stage_v1",
        "a" * 64,
    )
    empty_sections = ()
    snapshot = ProjectSnapshotV0(
        PROJECT_SNAPSHOT_V0_SCHEMA,
        coordinate,
        SnapshotCompletenessV0.COMPLETE,
        empty_sections,
        None,
        False,
        (),
        (),
    )
    with pytest.raises(SnapshotV0ValidationError):
        project_snapshot_v0_semantic_sha256(snapshot)
    with pytest.raises(SnapshotV0ValidationError):
        project_snapshot_v0_semantic_sha256(
            replace(snapshot, coordinate=replace(coordinate, project_revision=True))
        )
    with pytest.raises(SnapshotV0ValidationError):
        project_snapshot_v0_semantic_sha256(
            replace(snapshot, coordinate=replace(coordinate, project_id="bad\ud800"))
        )


def test_command_nested_dto_and_registered_enum_boundaries_are_closed() -> None:
    workflow = compile_workflow_contract_bundle_v2()
    pins = compile_contract_pin_set(workflow)
    envelope = CommandEnvelopeV1(
        COMMAND_ENVELOPE_SCHEMA,
        "command",
        CommandType.UNSUPPORTED,
        ProjectGenerationBindingV1("p", "pg", 1),
        RunGenerationBindingV1("r", "s", "run"),
        NoEntityScopeV1(),
        NoSubjectScopeV1(),
        ActorRefV1(ActorType.TEST_FIXTURE, "actor"),
        NoPayloadV1(),
        compile_read_set(()),
        pins,
    )
    with pytest.raises(CommandEnvelopeValidationError):
        command_envelope_bytes(
            replace(envelope, actor=_subclass_clone(envelope.actor))
        )
    forged_enum = str.__new__(ActorType, "PWN")
    forged_enum._value_ = "PWN"
    forged_enum._name_ = "PWN"
    with pytest.raises(CommandEnvelopeValidationError):
        command_envelope_bytes(
            replace(envelope, actor=replace(envelope.actor, actor_type=forged_enum))
        )
