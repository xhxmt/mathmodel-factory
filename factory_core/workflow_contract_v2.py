"""Additive M0.3 workflow identity envelope over the frozen v1 bundle."""

from __future__ import annotations

from dataclasses import dataclass, fields
from functools import lru_cache
import re

from .canonical import CanonicalizationError, canonical_bytes, canonical_sha256
from .classifier_identity import (
    DirtyClassifierIdentityBundleV1,
    compile_dirty_classifier_identity_bundle,
    dirty_classifier_analysis_sha256,
    validate_dirty_classifier_identity_bundle,
)
from .persisted_dirty_owner_policy import (
    PersistedDirtyOwnerIdentityV1,
    compile_persisted_dirty_owner_identity,
    validate_persisted_dirty_owner_identity,
)
from .workflow_contract import (
    WorkflowContractBundle,
    compile_workflow_contract_bundle,
    validate_workflow_contract_bundle,
    workflow_contract_analysis_sha256,
    workflow_contract_sha256,
)


WORKFLOW_CONTRACT_BUNDLE_V2_SCHEMA = "workflow-contract-bundle-v2"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class WorkflowContractV2ValidationError(ValueError):
    """Raised when an additive workflow identity is malformed or forged."""


@dataclass(frozen=True)
class WorkflowContractBundleV2:
    schema_version: str
    v1_bundle: WorkflowContractBundle
    workflow_v1_semantic_sha256: str
    workflow_v1_analysis_sha256: str
    classifier_identity: DirtyClassifierIdentityBundleV1
    persisted_dirty_owner_identity: PersistedDirtyOwnerIdentityV1
    analysis_source_locators: tuple[str, ...]
    analysis_evidence_refs: tuple[str, ...]


def _field(value: object, name: str, path: str) -> object:
    try:
        return object.__getattribute__(value, name)
    except AttributeError as exc:
        raise WorkflowContractV2ValidationError(f"{path}.{name} is missing") from exc


def _text(value: object, path: str, *, sha: bool = False) -> str:
    if type(value) is not str or not value:
        raise WorkflowContractV2ValidationError(f"{path} must be a non-empty plain string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise WorkflowContractV2ValidationError(
            f"{path} must contain valid UTF-8 scalar values"
        ) from exc
    if sha and _SHA256_RE.fullmatch(value) is None:
        raise WorkflowContractV2ValidationError(f"{path} must be lowercase SHA-256")
    return value


def _texts(value: object, path: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise WorkflowContractV2ValidationError(f"{path} must be an immutable tuple")
    return tuple(_text(item, f"{path}[{index}]") for index, item in enumerate(value))


@lru_cache(maxsize=1)
def _validated_source_v1_bundle() -> WorkflowContractBundle:
    return validate_workflow_contract_bundle(compile_workflow_contract_bundle())


def compile_workflow_contract_bundle_v2(
    *,
    v1_bundle: WorkflowContractBundle | None = None,
    analysis_evidence_refs: tuple[str, ...] = (),
) -> WorkflowContractBundleV2:
    """Validate the frozen v1 contract before adding parallel M0.3 identities."""

    v1 = (
        validate_workflow_contract_bundle(v1_bundle)
        if v1_bundle is not None
        else _validated_source_v1_bundle()
    )
    classifier = compile_dirty_classifier_identity_bundle(
        workflow_bundle=v1,
        analysis_evidence_refs=analysis_evidence_refs
    )
    owner_policy = compile_persisted_dirty_owner_identity(
        analysis_evidence_refs=analysis_evidence_refs
    )
    return WorkflowContractBundleV2(
        schema_version=WORKFLOW_CONTRACT_BUNDLE_V2_SCHEMA,
        v1_bundle=v1,
        workflow_v1_semantic_sha256=workflow_contract_sha256(v1),
        workflow_v1_analysis_sha256=workflow_contract_analysis_sha256(v1),
        classifier_identity=classifier,
        persisted_dirty_owner_identity=owner_policy,
        analysis_source_locators=(
            "factory_core/workflow_contract.py:frozen-v1",
            "factory_core/workflow_contract_v2.py:additive-identity-envelope",
            "factory_core/classifier_identity.py:pure-classifier-contract",
            "factory_core/persisted_dirty_owner_policy.py:postprocessing-policy",
        ),
        analysis_evidence_refs=analysis_evidence_refs,
    )


def validate_workflow_contract_bundle_v2(
    value: WorkflowContractBundleV2,
) -> WorkflowContractBundleV2:
    if type(value) is not WorkflowContractBundleV2:
        raise WorkflowContractV2ValidationError(
            "workflow v2 bundle has an unsupported runtime type"
        )
    for item in fields(WorkflowContractBundleV2):
        _field(value, item.name, "bundle")
    if _text(value.schema_version, "bundle.schema_version") != WORKFLOW_CONTRACT_BUNDLE_V2_SCHEMA:
        raise WorkflowContractV2ValidationError("workflow v2 schema is unsupported")
    try:
        v1 = validate_workflow_contract_bundle(value.v1_bundle)
    except Exception as exc:
        from .workflow_contract import WorkflowContractValidationError

        if isinstance(exc, WorkflowContractValidationError):
            raise WorkflowContractV2ValidationError(
                f"workflow v1 trust root rejected: {exc}"
            ) from exc
        raise
    _text(value.workflow_v1_semantic_sha256, "bundle.workflow_v1_semantic_sha256", sha=True)
    _text(value.workflow_v1_analysis_sha256, "bundle.workflow_v1_analysis_sha256", sha=True)
    validate_dirty_classifier_identity_bundle(value.classifier_identity)
    validate_persisted_dirty_owner_identity(value.persisted_dirty_owner_identity)
    _texts(value.analysis_source_locators, "bundle.analysis_source_locators")
    _texts(value.analysis_evidence_refs, "bundle.analysis_evidence_refs")
    if value.workflow_v1_semantic_sha256 != workflow_contract_sha256(v1):
        raise WorkflowContractV2ValidationError("workflow v1 semantic identity mismatch")
    if value.workflow_v1_analysis_sha256 != workflow_contract_analysis_sha256(v1):
        raise WorkflowContractV2ValidationError("workflow v1 analysis identity mismatch")
    expected = compile_workflow_contract_bundle_v2(
        v1_bundle=v1,
        analysis_evidence_refs=value.analysis_evidence_refs,
    )
    behavior_fields = (
        "schema_version",
        "v1_bundle",
        "workflow_v1_semantic_sha256",
        "classifier_identity",
        "persisted_dirty_owner_identity",
    )
    for name in behavior_fields:
        if getattr(value, name) != getattr(expected, name):
            raise WorkflowContractV2ValidationError(f"workflow v2 behavior drift field {name}")
    return value


def _semantic_projection(value: WorkflowContractBundleV2) -> tuple[object, ...]:
    bundle = validate_workflow_contract_bundle_v2(value)
    return (
        bundle.schema_version,
        bundle.workflow_v1_semantic_sha256,
        bundle.classifier_identity.dirty_classifier_semantic_sha256,
        bundle.persisted_dirty_owner_identity.persisted_dirty_owner_policy_semantic_sha256,
    )


def workflow_contract_v2_bytes(value: WorkflowContractBundleV2) -> bytes:
    try:
        return canonical_bytes(_semantic_projection(value))
    except CanonicalizationError as exc:
        raise WorkflowContractV2ValidationError("workflow v2 semantic projection is not canonical") from exc


def workflow_contract_v2_sha256(value: WorkflowContractBundleV2) -> str:
    try:
        return canonical_sha256(_semantic_projection(value))
    except CanonicalizationError as exc:
        raise WorkflowContractV2ValidationError("workflow v2 semantic projection is not canonical") from exc


def workflow_contract_v2_analysis_bytes(value: WorkflowContractBundleV2) -> bytes:
    bundle = validate_workflow_contract_bundle_v2(value)
    projection = (
        bundle,
        dirty_classifier_analysis_sha256(bundle.classifier_identity),
    )
    try:
        return canonical_bytes(projection)
    except CanonicalizationError as exc:
        raise WorkflowContractV2ValidationError("workflow v2 analysis projection is not canonical") from exc


def workflow_contract_v2_analysis_sha256(value: WorkflowContractBundleV2) -> str:
    bundle = validate_workflow_contract_bundle_v2(value)
    projection = (
        bundle,
        dirty_classifier_analysis_sha256(bundle.classifier_identity),
    )
    try:
        return canonical_sha256(projection)
    except CanonicalizationError as exc:
        raise WorkflowContractV2ValidationError("workflow v2 analysis projection is not canonical") from exc
