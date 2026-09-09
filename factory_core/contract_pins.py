"""Source-authorized M0.3 contract pins and independent runtime identity."""

from __future__ import annotations

from dataclasses import dataclass, fields
import fnmatch
import hashlib
import json
from pathlib import PurePath
import re
import sys

from .artifact_ownership import ARTIFACT_OWNERSHIP_REGISTRY, ARTIFACT_OWNERSHIP_SCHEMA
from .canonical import CANONICAL_JSON_SCHEMA, CanonicalizationError, canonical_bytes, canonical_sha256
from .classifier_identity import validate_dirty_classifier_identity_bundle
from .persisted_dirty_owner_policy import validate_persisted_dirty_owner_identity
from .shadow_scheduler import (
    PARITY_VALUE_SCHEMA,
    READINESS_INPUT_SCHEMA,
    READINESS_RESULT_SCHEMA,
    SHADOW_SCHEDULER_ENABLED_BY_DEFAULT,
    TRANSITION_PLAN_SCHEMA,
    WORKFLOW_STATUS_PLAN_DISPOSITIONS,
    ParityStatus,
    ReadinessState,
    TransitionAction,
)
from .workflow_contract_v2 import (
    WorkflowContractBundleV2,
    validate_workflow_contract_bundle_v2,
    workflow_contract_v2_sha256,
)


CONTRACT_PIN_SET_SCHEMA = "contract-pin-set-v1"
RUNTIME_CONTRACT_PIN_SCHEMA = "python-runtime-contract-pin-v1"
CONTRACT_PIN_ANALYSIS_SCHEMA = "contract-pin-analysis-v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class ContractPinValidationError(ValueError):
    """Raised when a pin set is malformed or not source-authorized."""


@dataclass(frozen=True)
class RuntimeContractPinV1:
    schema_version: str
    python_implementation: str
    python_version: str
    canonicalization_schema_version: str
    regex_runtime: str
    fnmatch_runtime: str
    pathlib_runtime: str
    json_runtime: str
    hashlib_runtime: str


@dataclass(frozen=True)
class ContractPinSetV1:
    schema_version: str
    workflow_contract_semantic_sha256: str
    scheduler_contract_semantic_sha256: str
    artifact_ownership_recording_semantic_sha256: str
    dirty_classifier_semantic_sha256: str
    dirty_classifier_operational_implementation_sha256: str
    persisted_dirty_owner_policy_semantic_sha256: str
    persisted_dirty_owner_policy_implementation_sha256: str
    runtime_contract_sha256: str


@dataclass(frozen=True)
class ContractPinAnalysisV1:
    schema_version: str
    pin_set: ContractPinSetV1
    workflow_contract_analysis_sha256: str
    contract_compiler_implementation_sha256: str
    source_locators: tuple[str, ...]
    conformance_corpus: tuple[str, ...]
    evidence_refs: tuple[str, ...]


def _field(value: object, name: str, path: str) -> object:
    try:
        return object.__getattribute__(value, name)
    except AttributeError as exc:
        raise ContractPinValidationError(f"{path}.{name} is missing") from exc


def _text(value: object, path: str, *, sha: bool = False) -> str:
    if type(value) is not str or not value:
        raise ContractPinValidationError(f"{path} must be a non-empty plain string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ContractPinValidationError(f"{path} must contain valid UTF-8 scalar values") from exc
    if sha and _SHA256_RE.fullmatch(value) is None:
        raise ContractPinValidationError(f"{path} must be lowercase SHA-256")
    return value


def _tuple_text(value: object, path: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise ContractPinValidationError(f"{path} must be an immutable tuple")
    return tuple(_text(item, f"{path}[{index}]") for index, item in enumerate(value))


def compile_runtime_contract_pin() -> RuntimeContractPinV1:
    """Describe the language-library runtime without pretending it is repo source."""

    return RuntimeContractPinV1(
        schema_version=RUNTIME_CONTRACT_PIN_SCHEMA,
        python_implementation=sys.implementation.name,
        python_version=".".join(str(item) for item in sys.version_info[:3]),
        canonicalization_schema_version=CANONICAL_JSON_SCHEMA,
        regex_runtime=f"{re.__name__}:python-stdlib",
        fnmatch_runtime=f"{fnmatch.__name__}:python-stdlib",
        pathlib_runtime=f"{PurePath.__module__.split('.')[0]}:python-stdlib",
        json_runtime=f"{json.__name__}:python-stdlib",
        hashlib_runtime=f"{hashlib.__name__}:python-stdlib",
    )


def validate_runtime_contract_pin(value: RuntimeContractPinV1) -> RuntimeContractPinV1:
    if type(value) is not RuntimeContractPinV1:
        raise ContractPinValidationError("runtime pin has an unsupported runtime type")
    for item in fields(RuntimeContractPinV1):
        _text(_field(value, item.name, "runtime_pin"), f"runtime_pin.{item.name}")
    if value != compile_runtime_contract_pin():
        raise ContractPinValidationError("runtime pin differs from the executing runtime")
    return value


def runtime_contract_sha256(value: RuntimeContractPinV1 | None = None) -> str:
    runtime = validate_runtime_contract_pin(
        value if value is not None else compile_runtime_contract_pin()
    )
    try:
        return canonical_sha256(runtime)
    except CanonicalizationError as exc:  # pragma: no cover - structural boundary
        raise ContractPinValidationError("runtime pin cannot be canonicalized") from exc


def _scheduler_semantic_sha256() -> str:
    projection = (
        "shadow-scheduler-semantic-contract-v1",
        READINESS_INPUT_SCHEMA,
        READINESS_RESULT_SCHEMA,
        TRANSITION_PLAN_SCHEMA,
        PARITY_VALUE_SCHEMA,
        SHADOW_SCHEDULER_ENABLED_BY_DEFAULT,
        tuple(member.value for member in TransitionAction),
        tuple(member.value for member in ReadinessState),
        tuple(member.value for member in ParityStatus),
        tuple(sorted(WORKFLOW_STATUS_PLAN_DISPOSITIONS.items())),
        "authoritative=false",
        "proposed_mutations=empty",
        "performed_side_effects=empty",
    )
    return canonical_sha256(projection)


def _artifact_ownership_recording_semantic_sha256() -> str:
    projection = (
        "artifact-ownership-recording-contract-v1",
        ARTIFACT_OWNERSHIP_SCHEMA,
        tuple(
            (
                index,
                rule.pattern,
                rule.owner_stage,
                rule.semantic_domain,
                rule.dirty_flag,
                rule.final_input,
                rule.submission_member,
            )
            for index, rule in enumerate(ARTIFACT_OWNERSHIP_REGISTRY)
        ),
        "dirty-row-records-classifier-contract-in-legacy_classifier_contract_sha256-only",
        "recorded-owner-stage-is-static-classifier-output-with-persisted-solver-owner-postprocessing",
    )
    return canonical_sha256(projection)


def compile_contract_pin_set(workflow: WorkflowContractBundleV2) -> ContractPinSetV1:
    bundle = validate_workflow_contract_bundle_v2(workflow)
    classifier = validate_dirty_classifier_identity_bundle(bundle.classifier_identity)
    policy = validate_persisted_dirty_owner_identity(bundle.persisted_dirty_owner_identity)
    return ContractPinSetV1(
        schema_version=CONTRACT_PIN_SET_SCHEMA,
        workflow_contract_semantic_sha256=workflow_contract_v2_sha256(bundle),
        scheduler_contract_semantic_sha256=_scheduler_semantic_sha256(),
        artifact_ownership_recording_semantic_sha256=(
            _artifact_ownership_recording_semantic_sha256()
        ),
        dirty_classifier_semantic_sha256=classifier.dirty_classifier_semantic_sha256,
        dirty_classifier_operational_implementation_sha256=(
            classifier.dirty_classifier_operational_implementation_sha256
        ),
        persisted_dirty_owner_policy_semantic_sha256=(
            policy.persisted_dirty_owner_policy_semantic_sha256
        ),
        persisted_dirty_owner_policy_implementation_sha256=(
            policy.persisted_dirty_owner_policy_implementation_sha256
        ),
        runtime_contract_sha256=runtime_contract_sha256(),
    )


def validate_contract_pin_set(
    value: ContractPinSetV1,
    workflow: WorkflowContractBundleV2,
) -> ContractPinSetV1:
    if type(value) is not ContractPinSetV1:
        raise ContractPinValidationError("contract pin set has an unsupported runtime type")
    for item in fields(ContractPinSetV1):
        _field(value, item.name, "pin_set")
    if _text(value.schema_version, "pin_set.schema_version") != CONTRACT_PIN_SET_SCHEMA:
        raise ContractPinValidationError("contract pin set schema is unsupported")
    for item in fields(ContractPinSetV1):
        if item.name == "schema_version":
            continue
        _text(getattr(value, item.name), f"pin_set.{item.name}", sha=True)
    expected = compile_contract_pin_set(workflow)
    if value != expected:
        for item in fields(ContractPinSetV1):
            if getattr(value, item.name) != getattr(expected, item.name):
                raise ContractPinValidationError(
                    f"contract pin set source authorization failed field {item.name}"
                )
        raise ContractPinValidationError("contract pin set source authorization failed")
    return value


def contract_pin_set_bytes(value: ContractPinSetV1, workflow: WorkflowContractBundleV2) -> bytes:
    pins = validate_contract_pin_set(value, workflow)
    try:
        return canonical_bytes(pins)
    except CanonicalizationError as exc:
        raise ContractPinValidationError("contract pin set cannot be canonicalized") from exc


def contract_pin_set_sha256(value: ContractPinSetV1, workflow: WorkflowContractBundleV2) -> str:
    pins = validate_contract_pin_set(value, workflow)
    try:
        return canonical_sha256(pins)
    except CanonicalizationError as exc:
        raise ContractPinValidationError("contract pin set cannot be canonicalized") from exc


def compile_contract_pin_analysis(
    *,
    pin_set: ContractPinSetV1,
    workflow: WorkflowContractBundleV2,
    workflow_contract_analysis_sha256: str,
    contract_compiler_implementation_sha256: str,
    evidence_refs: tuple[str, ...] = (),
) -> ContractPinAnalysisV1:
    """Bind build-only implementation evidence without making it a CAS behavior root."""

    validate_contract_pin_set(pin_set, workflow)
    _text(workflow_contract_analysis_sha256, "workflow_contract_analysis_sha256", sha=True)
    _text(contract_compiler_implementation_sha256, "contract_compiler_implementation_sha256", sha=True)
    _tuple_text(evidence_refs, "evidence_refs")
    return ContractPinAnalysisV1(
        schema_version=CONTRACT_PIN_ANALYSIS_SCHEMA,
        pin_set=pin_set,
        workflow_contract_analysis_sha256=workflow_contract_analysis_sha256,
        contract_compiler_implementation_sha256=contract_compiler_implementation_sha256,
        source_locators=(
            "factory_core/canonical.py",
            "factory_core/owner_compiler.py",
            "factory_core/workflow_contract.py",
            "factory_core/workflow_contract_v2.py",
            "factory_core/classifier_identity.py",
            "factory_core/contract_pins.py",
            "factory_core/classifier_implementation_manifest.py",
            "factory_core/persisted_dirty_owner_implementation_manifest.py",
        ),
        conformance_corpus=(
            "tests/test_workflow_contract_bundle.py",
            "tests/test_m02_shadow_scheduler.py",
            "tests/test_m03_classifier_identity.py",
            "tests/test_m03_contract_pins.py",
        ),
        evidence_refs=evidence_refs,
    )


def contract_pin_analysis_bytes(value: ContractPinAnalysisV1, workflow: WorkflowContractBundleV2) -> bytes:
    if type(value) is not ContractPinAnalysisV1:
        raise ContractPinValidationError("contract pin analysis has an unsupported runtime type")
    for item in fields(ContractPinAnalysisV1):
        _field(value, item.name, "analysis")
    if value.schema_version != CONTRACT_PIN_ANALYSIS_SCHEMA:
        raise ContractPinValidationError("contract pin analysis schema is unsupported")
    validate_contract_pin_set(value.pin_set, workflow)
    _text(value.workflow_contract_analysis_sha256, "analysis.workflow_contract_analysis_sha256", sha=True)
    _text(value.contract_compiler_implementation_sha256, "analysis.contract_compiler_implementation_sha256", sha=True)
    _tuple_text(value.source_locators, "analysis.source_locators")
    _tuple_text(value.conformance_corpus, "analysis.conformance_corpus")
    _tuple_text(value.evidence_refs, "analysis.evidence_refs")
    try:
        return canonical_bytes(value)
    except CanonicalizationError as exc:
        raise ContractPinValidationError("contract pin analysis cannot be canonicalized") from exc


def contract_pin_analysis_sha256(
    value: ContractPinAnalysisV1,
    workflow: WorkflowContractBundleV2,
) -> str:
    return hashlib.sha256(contract_pin_analysis_bytes(value, workflow)).hexdigest()
