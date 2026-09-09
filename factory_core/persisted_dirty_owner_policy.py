"""M0.3 identity and pure model for persisted dirty-owner postprocessing.

The production implementation remains frozen in ``engine.py`` and
``storage.py``. This module describes and validates its behavior, binds only
the relevant source symbols, and provides a side-effect-free conformance model.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
import re

from .canonical import CANONICAL_JSON_SCHEMA, CanonicalizationError, canonical_bytes, canonical_sha256
from .dirty import DirtyChange, DirtyFlag, _SOLVER_RECEIPT_RE
from .persisted_dirty_owner_implementation_manifest import (
    PERSISTED_DIRTY_OWNER_IMPLEMENTATION_MANIFEST_SCHEMA,
    persisted_dirty_owner_policy_implementation_sha256,
)


PERSISTED_DIRTY_OWNER_POLICY_SCHEMA = "persisted-dirty-owner-policy-v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class PersistedDirtyOwnerPolicyValidationError(ValueError):
    """Raised for malformed or non-source-authorized policy values."""


@dataclass(frozen=True)
class PersistedDirtyOwnerPolicyV1:
    schema_version: str
    canonicalization_schema_version: str
    receipt_path_pattern: str
    receipt_path_regex_flags: int
    receipt_job_id_group: str
    receipt_terminal_suffixes: tuple[str, ...]
    lookup_entity: str
    lookup_key: str
    lookup_missing_behavior: str
    null_owner_behavior: str
    owner_conversion: str
    application_order: str
    overridden_fields: tuple[str, ...]
    preserved_fields: tuple[str, ...]
    registry_precedence: str


@dataclass(frozen=True)
class PersistedDirtyOwnerFactV1:
    job_id: str
    owner_stage: int | None


@dataclass(frozen=True)
class PersistedDirtyOwnerIdentityV1:
    schema_version: str
    policy: PersistedDirtyOwnerPolicyV1
    persisted_dirty_owner_policy_semantic_sha256: str
    implementation_manifest_schema_version: str
    persisted_dirty_owner_policy_implementation_sha256: str
    analysis_source_locators: tuple[str, ...]
    analysis_conformance_corpus: tuple[str, ...]
    analysis_evidence_refs: tuple[str, ...]


def _field(value: object, name: str, path: str) -> object:
    try:
        return object.__getattribute__(value, name)
    except AttributeError as exc:
        raise PersistedDirtyOwnerPolicyValidationError(f"{path}.{name} is missing") from exc


def _text(value: object, path: str) -> str:
    if type(value) is not str or not value:
        raise PersistedDirtyOwnerPolicyValidationError(f"{path} must be a non-empty plain string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PersistedDirtyOwnerPolicyValidationError(
            f"{path} must contain valid UTF-8 scalar values"
        ) from exc
    return value


def _tuple_text(value: object, path: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise PersistedDirtyOwnerPolicyValidationError(f"{path} must be an immutable tuple")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(_text(item, f"{path}[{index}]"))
    return tuple(result)


def compile_persisted_dirty_owner_policy() -> PersistedDirtyOwnerPolicyV1:
    """Compile the source-authorized behavior of the frozen engine hook."""

    return PersistedDirtyOwnerPolicyV1(
        schema_version=PERSISTED_DIRTY_OWNER_POLICY_SCHEMA,
        canonicalization_schema_version=CANONICAL_JSON_SCHEMA,
        receipt_path_pattern=_SOLVER_RECEIPT_RE.pattern,
        receipt_path_regex_flags=int(_SOLVER_RECEIPT_RE.flags),
        receipt_job_id_group="job_id",
        receipt_terminal_suffixes=("submitted", "completed"),
        lookup_entity="SQLiteStateStore.solver_job",
        lookup_key="job_id",
        lookup_missing_behavior="KeyError-or-missing-row-preserves-classified-owner",
        null_owner_behavior="owner_stage-None-preserves-classified-owner",
        owner_conversion="plain-int-conversion-of-recorded-owner-stage",
        application_order="after-classify_manifest_changes-before-dirty-row-recording",
        overridden_fields=("owner_stage",),
        preserved_fields=(
            "flag",
            "cause_artifact",
            "baseline_fingerprint",
            "current_fingerprint",
        ),
        registry_precedence="persisted-solver-job-owner-overrides-static-registry-owner",
    )


def validate_persisted_dirty_owner_policy(
    value: PersistedDirtyOwnerPolicyV1,
) -> PersistedDirtyOwnerPolicyV1:
    if type(value) is not PersistedDirtyOwnerPolicyV1:
        raise PersistedDirtyOwnerPolicyValidationError(
            "persisted dirty-owner policy has an unsupported runtime type"
        )
    for item in fields(PersistedDirtyOwnerPolicyV1):
        _field(value, item.name, "policy")
    for name in (
        "schema_version",
        "canonicalization_schema_version",
        "receipt_path_pattern",
        "receipt_job_id_group",
        "lookup_entity",
        "lookup_key",
        "lookup_missing_behavior",
        "null_owner_behavior",
        "owner_conversion",
        "application_order",
        "registry_precedence",
    ):
        _text(getattr(value, name), f"policy.{name}")
    if type(value.receipt_path_regex_flags) is not int or value.receipt_path_regex_flags < 0:
        raise PersistedDirtyOwnerPolicyValidationError(
            "policy.receipt_path_regex_flags must be a non-negative plain integer"
        )
    _tuple_text(value.receipt_terminal_suffixes, "policy.receipt_terminal_suffixes")
    _tuple_text(value.overridden_fields, "policy.overridden_fields")
    _tuple_text(value.preserved_fields, "policy.preserved_fields")
    expected = compile_persisted_dirty_owner_policy()
    if value != expected:
        raise PersistedDirtyOwnerPolicyValidationError(
            "persisted dirty-owner behavior differs from source-authorized projection"
        )
    return value


def persisted_dirty_owner_policy_semantic_bytes(
    value: PersistedDirtyOwnerPolicyV1 | None = None,
) -> bytes:
    policy = validate_persisted_dirty_owner_policy(
        value if value is not None else compile_persisted_dirty_owner_policy()
    )
    try:
        return canonical_bytes(policy)
    except CanonicalizationError as exc:  # pragma: no cover - structural validation
        raise PersistedDirtyOwnerPolicyValidationError("policy cannot be canonicalized") from exc


def persisted_dirty_owner_policy_semantic_sha256(
    value: PersistedDirtyOwnerPolicyV1 | None = None,
) -> str:
    policy = validate_persisted_dirty_owner_policy(
        value if value is not None else compile_persisted_dirty_owner_policy()
    )
    try:
        return canonical_sha256(policy)
    except CanonicalizationError as exc:  # pragma: no cover - structural validation
        raise PersistedDirtyOwnerPolicyValidationError("policy cannot be canonicalized") from exc


def compile_persisted_dirty_owner_identity(
    *, analysis_evidence_refs: tuple[str, ...] = ()
) -> PersistedDirtyOwnerIdentityV1:
    policy = compile_persisted_dirty_owner_policy()
    return PersistedDirtyOwnerIdentityV1(
        schema_version="persisted-dirty-owner-identity-v1",
        policy=policy,
        persisted_dirty_owner_policy_semantic_sha256=(
            persisted_dirty_owner_policy_semantic_sha256(policy)
        ),
        implementation_manifest_schema_version=(
            PERSISTED_DIRTY_OWNER_IMPLEMENTATION_MANIFEST_SCHEMA
        ),
        persisted_dirty_owner_policy_implementation_sha256=(
            persisted_dirty_owner_policy_implementation_sha256()
        ),
        analysis_source_locators=(
            "factory_core/dirty.py:_SOLVER_RECEIPT_RE",
            "factory_core/dirty.py:solver_receipt_job_id",
            "factory_core/engine.py:FactoryEngine._stage_manifest_delta",
            "factory_core/engine.py:FactoryEngine._solver_receipt_owner_stage",
            "factory_core/storage.py:SQLiteStateStore.solver_job",
            "factory_core/storage.py:SQLiteStateStore._solver_job_from_row",
        ),
        analysis_conformance_corpus=(
            "tests/test_dirty.py",
            "tests/test_m03_persisted_dirty_owner_policy.py",
        ),
        analysis_evidence_refs=analysis_evidence_refs,
    )


def validate_persisted_dirty_owner_identity(
    value: PersistedDirtyOwnerIdentityV1,
) -> PersistedDirtyOwnerIdentityV1:
    if type(value) is not PersistedDirtyOwnerIdentityV1:
        raise PersistedDirtyOwnerPolicyValidationError(
            "persisted dirty-owner identity has an unsupported runtime type"
        )
    for item in fields(PersistedDirtyOwnerIdentityV1):
        _field(value, item.name, "identity")
    _text(value.schema_version, "identity.schema_version")
    validate_persisted_dirty_owner_policy(value.policy)
    for name in (
        "analysis_source_locators",
        "analysis_conformance_corpus",
        "analysis_evidence_refs",
    ):
        _tuple_text(getattr(value, name), f"identity.{name}")
    for name in (
        "persisted_dirty_owner_policy_semantic_sha256",
        "persisted_dirty_owner_policy_implementation_sha256",
    ):
        digest = _text(getattr(value, name), f"identity.{name}")
        if _SHA256_RE.fullmatch(digest) is None:
            raise PersistedDirtyOwnerPolicyValidationError(
                f"identity.{name} must be lowercase SHA-256"
            )
    expected = compile_persisted_dirty_owner_identity(
        analysis_evidence_refs=value.analysis_evidence_refs
    )
    behavior_fields = (
        "schema_version",
        "policy",
        "persisted_dirty_owner_policy_semantic_sha256",
        "implementation_manifest_schema_version",
        "persisted_dirty_owner_policy_implementation_sha256",
    )
    for name in behavior_fields:
        if getattr(value, name) != getattr(expected, name):
            raise PersistedDirtyOwnerPolicyValidationError(
                f"persisted dirty-owner identity behavior drift field {name}"
            )
    return value


def persisted_dirty_owner_analysis_bytes(
    value: PersistedDirtyOwnerIdentityV1,
) -> bytes:
    identity = validate_persisted_dirty_owner_identity(value)
    try:
        return canonical_bytes(identity)
    except CanonicalizationError as exc:
        raise PersistedDirtyOwnerPolicyValidationError(
            "persisted dirty-owner analysis identity cannot be canonicalized"
        ) from exc


def persisted_dirty_owner_analysis_sha256(
    value: PersistedDirtyOwnerIdentityV1,
) -> str:
    identity = validate_persisted_dirty_owner_identity(value)
    try:
        return canonical_sha256(identity)
    except CanonicalizationError as exc:
        raise PersistedDirtyOwnerPolicyValidationError(
            "persisted dirty-owner analysis identity cannot be canonicalized"
        ) from exc


def apply_persisted_dirty_owner_policy(
    change: DirtyChange,
    owner_facts: tuple[PersistedDirtyOwnerFactV1, ...],
    *,
    policy: PersistedDirtyOwnerPolicyV1 | None = None,
) -> DirtyChange:
    """Apply the frozen persisted-owner rule without storage or other I/O."""

    active_policy = validate_persisted_dirty_owner_policy(
        policy if policy is not None else compile_persisted_dirty_owner_policy()
    )
    if type(change) is not DirtyChange:
        raise PersistedDirtyOwnerPolicyValidationError(
            "change has an unsupported runtime type"
        )
    for item in fields(DirtyChange):
        _field(change, item.name, "change")
    if type(change.flag) is not DirtyFlag or not any(
        change.flag is member for member in DirtyFlag
    ):
        raise PersistedDirtyOwnerPolicyValidationError(
            "change.flag is not a registered DirtyFlag member"
        )
    if type(change.owner_stage) is not int:
        raise PersistedDirtyOwnerPolicyValidationError(
            "change.owner_stage must be a plain integer"
        )
    for name in ("cause_artifact", "baseline_fingerprint", "current_fingerprint"):
        _text(getattr(change, name), f"change.{name}")
    if type(owner_facts) is not tuple:
        raise PersistedDirtyOwnerPolicyValidationError("owner_facts must be an immutable tuple")
    facts: dict[str, int | None] = {}
    for index, fact in enumerate(owner_facts):
        if type(fact) is not PersistedDirtyOwnerFactV1:
            raise PersistedDirtyOwnerPolicyValidationError(
                f"owner_facts[{index}] has an unsupported runtime type"
            )
        job_id = _text(_field(fact, "job_id", f"owner_facts[{index}]"), f"owner_facts[{index}].job_id")
        owner_stage = _field(fact, "owner_stage", f"owner_facts[{index}]")
        if owner_stage is not None and type(owner_stage) is not int:
            raise PersistedDirtyOwnerPolicyValidationError(
                f"owner_facts[{index}].owner_stage must be None or a plain integer"
            )
        if job_id in facts:
            raise PersistedDirtyOwnerPolicyValidationError("owner_facts contains duplicate job_id")
        facts[job_id] = owner_stage
    match = re.compile(
        active_policy.receipt_path_pattern,
        active_policy.receipt_path_regex_flags,
    ).fullmatch(change.cause_artifact.replace("\\", "/"))
    if match is None:
        return change
    job_id = match.group(active_policy.receipt_job_id_group)
    if job_id not in facts or facts[job_id] is None:
        return change
    owner_stage = facts[job_id]
    assert owner_stage is not None
    return replace(change, owner_stage=int(owner_stage))
