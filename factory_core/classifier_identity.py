"""M0.3 split identities for the pure dirty classifier.

The semantic identity is compiled from behavior-bearing source constants and
the frozen M0.2 workflow trust root. The operational identity is supplied by a
checked-in source-byte manifest. Neither public validator opens repository
files or calls the historical ``classifier_contract_sha256`` function.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
from functools import lru_cache
import re

from .artifact_ownership import (
    ARTIFACT_OWNERSHIP_REGISTRY,
    ARTIFACT_OWNERSHIP_SCHEMA,
    artifact_pattern_variants,
)
from .canonical import CANONICAL_JSON_SCHEMA, CanonicalizationError, canonical_bytes, canonical_sha256
from .classifier_implementation_manifest import (
    DIRTY_CLASSIFIER_IMPLEMENTATION_MANIFEST_SCHEMA,
    dirty_classifier_operational_implementation_sha256,
)
from .dirty import (
    DIRTY_CLASSIFIER_SCHEMA,
    DirtyChange,
    DirtyFlag,
    _CITATION_RE,
    _DEF_STYLE_RE,
    _EXCLUDED_PARTS,
    _IGNORED_NAMES,
    _LATEX_COMMAND_RE,
    _MATH_CONTROL_RE,
    _MATH_DEFINITION_RE,
    _MATH_RE,
    _TRACKED_ROOTS,
    _TRACKED_TOP_SUFFIXES,
)
from .paper_sources import (
    LATEX_DEPENDENCY_SCHEMA,
    _BIBLIOGRAPHY_COMMANDS,
    _BIBLIOGRAPHY_STYLE_COMMANDS,
    _DEPENDENCY_RE,
    _GENERATED_INPUT_SUFFIXES,
    _INACTIVE_ENVIRONMENTS,
    _OPTIONAL_EXTERNAL_COMMANDS,
    _RESOURCE_SUFFIXES,
    _SOURCE_COMMANDS,
)
from .workflow_contract import (
    WorkflowContractBundle,
    compile_workflow_contract_bundle,
    validate_workflow_contract_bundle,
)


DIRTY_CLASSIFIER_SEMANTIC_CONTRACT_SCHEMA = "dirty-classifier-semantic-contract-v1"
DIRTY_CLASSIFIER_IDENTITY_BUNDLE_SCHEMA = "dirty-classifier-identity-bundle-v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class DirtyClassifierIdentityValidationError(ValueError):
    """Raised when an identity DTO is malformed or not source-authorized."""


@dataclass(frozen=True)
class RegexBehaviorV1:
    rule_id: str
    pattern: str
    flags: int


@dataclass(frozen=True)
class OwnershipRuleBehaviorV1:
    rule_index: int
    pattern: str
    owner_stage: int
    semantic_domain: str
    dirty_flag: str
    final_input: bool
    submission_member: bool
    globstar_variants: tuple[str, ...]


@dataclass(frozen=True)
class DependencySuffixBehaviorV1:
    command: str
    suffixes: tuple[str, ...]


@dataclass(frozen=True)
class DirtyClassifierSemanticContractV1:
    schema_version: str
    canonicalization_schema_version: str
    classifier_schema_version: str
    dirty_flags: tuple[str, ...]
    semantic_dirty_flags: tuple[str, ...]
    dirty_change_fields: tuple[str, ...]
    tracked_roots: tuple[str, ...]
    tracked_top_suffixes: tuple[str, ...]
    excluded_parts: tuple[str, ...]
    ignored_names: tuple[str, ...]
    tracking_rules: tuple[str, ...]
    file_read_rules: tuple[str, ...]
    regex_rules: tuple[RegexBehaviorV1, ...]
    fingerprint_rules: tuple[str, ...]
    latex_dependency_schema_version: str
    latex_source_commands: tuple[str, ...]
    latex_bibliography_commands: tuple[str, ...]
    latex_bibliography_style_commands: tuple[str, ...]
    latex_optional_external_commands: tuple[str, ...]
    latex_resource_suffixes: tuple[DependencySuffixBehaviorV1, ...]
    latex_generated_input_suffixes: tuple[str, ...]
    latex_inactive_environments: tuple[str, ...]
    latex_dependency_rules: tuple[str, ...]
    ownership_schema_version: str
    ownership_rules: tuple[OwnershipRuleBehaviorV1, ...]
    ownership_matcher_rules: tuple[str, ...]
    classification_rules: tuple[str, ...]
    output_rules: tuple[str, ...]
    step13_condition_operands: tuple[str, ...]


@dataclass(frozen=True)
class DirtyClassifierIdentityBundleV1:
    schema_version: str
    semantic_contract: DirtyClassifierSemanticContractV1
    dirty_classifier_semantic_sha256: str
    operational_manifest_schema_version: str
    dirty_classifier_operational_implementation_sha256: str
    analysis_source_locators: tuple[str, ...]
    analysis_conformance_corpus: tuple[str, ...]
    analysis_evidence_refs: tuple[str, ...]


_CLOSED_TYPES = {
    RegexBehaviorV1,
    OwnershipRuleBehaviorV1,
    DependencySuffixBehaviorV1,
    DirtyClassifierSemanticContractV1,
    DirtyClassifierIdentityBundleV1,
}


def _validate_scalar(value: object, path: str, *, allow_empty: bool = True) -> None:
    if type(value) is str:
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise DirtyClassifierIdentityValidationError(
                f"{path} must contain valid UTF-8 scalar values"
            ) from exc
        if not allow_empty and value == "":
            raise DirtyClassifierIdentityValidationError(f"{path} must not be empty")
        return
    if type(value) in {int, bool}:
        return
    raise DirtyClassifierIdentityValidationError(f"{path} has an unsupported scalar type")


def _validate_closed(value: object, expected: type, path: str) -> None:
    if type(value) is not expected:
        raise DirtyClassifierIdentityValidationError(
            f"{path} has an unsupported runtime type"
        )
    if issubclass(expected, Enum):
        if not any(value is member for member in expected):
            raise DirtyClassifierIdentityValidationError(
                f"{path} is not a registered enum member"
            )
        return
    for item in fields(expected):
        try:
            child = object.__getattribute__(value, item.name)
        except AttributeError as exc:
            raise DirtyClassifierIdentityValidationError(
                f"{path}.{item.name} is missing"
            ) from exc
        _validate_value(child, f"{path}.{item.name}")


def _validate_value(value: object, path: str) -> None:
    if type(value) in _CLOSED_TYPES:
        _validate_closed(value, type(value), path)
        return
    if type(value) is tuple:
        for index, child in enumerate(value):
            _validate_value(child, f"{path}[{index}]")
        return
    _validate_scalar(value, path)


def _regex(rule_id: str, value: re.Pattern[str]) -> RegexBehaviorV1:
    return RegexBehaviorV1(rule_id=rule_id, pattern=value.pattern, flags=int(value.flags))


@lru_cache(maxsize=1)
def _validated_source_workflow_bundle() -> WorkflowContractBundle:
    return validate_workflow_contract_bundle(compile_workflow_contract_bundle())


def compile_dirty_classifier_semantic_contract(
    workflow_bundle: WorkflowContractBundle | None = None,
) -> DirtyClassifierSemanticContractV1:
    """Compile the complete pure-classifier behavior projection from source."""

    workflow = (
        validate_workflow_contract_bundle(workflow_bundle)
        if workflow_bundle is not None
        else _validated_source_workflow_bundle()
    )
    step13 = tuple(
        subtask.condition.operands
        for stage in workflow.stages
        for subtask in stage.subtasks
        if subtask.source_step_id == 13
    )
    if len(step13) != 1 or step13[0] != workflow.classifier.semantic_dirty_flags:
        raise DirtyClassifierIdentityValidationError(
            "source Step 13 condition does not match the validated workflow classifier"
        )
    owner_rules = tuple(
        OwnershipRuleBehaviorV1(
            rule_index=index,
            pattern=rule.pattern,
            owner_stage=rule.owner_stage,
            semantic_domain=rule.semantic_domain,
            dirty_flag=rule.dirty_flag,
            final_input=rule.final_input,
            submission_member=rule.submission_member,
            globstar_variants=artifact_pattern_variants(rule.pattern),
        )
        for index, rule in enumerate(ARTIFACT_OWNERSHIP_REGISTRY)
    )
    return DirtyClassifierSemanticContractV1(
        schema_version=DIRTY_CLASSIFIER_SEMANTIC_CONTRACT_SCHEMA,
        canonicalization_schema_version=CANONICAL_JSON_SCHEMA,
        classifier_schema_version=DIRTY_CLASSIFIER_SCHEMA,
        dirty_flags=tuple(flag.value for flag in DirtyFlag),
        semantic_dirty_flags=workflow.classifier.semantic_dirty_flags,
        dirty_change_fields=tuple(item.name for item in fields(DirtyChange)),
        tracked_roots=tuple(sorted(_TRACKED_ROOTS)),
        tracked_top_suffixes=tuple(sorted(_TRACKED_TOP_SUFFIXES)),
        excluded_parts=tuple(sorted(_EXCLUDED_PARTS)),
        ignored_names=tuple(sorted(_IGNORED_NAMES)),
        tracking_rules=(
            "exclude-any-matching-path-component",
            "ignore-exact-names-and-latest-txt-json-suffixes",
            "top-level-files-tracked-by-lowercase-suffix",
            "nested-files-tracked-by-exact-root-or-root-prefix",
            "only-ordinary-non-symlink-files",
            "paths-sorted-by-platform-path-order-before-posix-relative-projection",
        ),
        file_read_rules=(
            "ordinary-file-read-OSError-skips-artifact",
            "paper-text-decode-utf8-errors-replace",
            "protected-ledger-text-decode-utf8-errors-replace",
            "inactive-project-paper-dependencies-are-not-manifest-members",
        ),
        regex_rules=(
            _regex("citation", _CITATION_RE),
            _regex("definition-style", _DEF_STYLE_RE),
            _regex("latex-command", _LATEX_COMMAND_RE),
            _regex("math-control", _MATH_CONTROL_RE),
            _regex("math-definition", _MATH_DEFINITION_RE),
            _regex("math-expression", _MATH_RE),
            _regex("latex-dependency", _DEPENDENCY_RE),
        ),
        fingerprint_rules=(
            "math=canonical-hash(formulas,definitions,controls)",
            "citation=canonical-hash(ordered-citation-matches)",
            "prose=sha256(utf8-replacement-after-math-citation-command-and-punctuation-removal)",
            "format=sha256(utf8-replacement-after-math-removal-and-whitespace-collapse)",
            "protected-row=sha256(cleaned-first-cell-issue-id)",
            "raw-artifact=sha256(file-bytes)",
        ),
        latex_dependency_schema_version=LATEX_DEPENDENCY_SCHEMA,
        latex_source_commands=tuple(sorted(_SOURCE_COMMANDS)),
        latex_bibliography_commands=tuple(sorted(_BIBLIOGRAPHY_COMMANDS)),
        latex_bibliography_style_commands=tuple(sorted(_BIBLIOGRAPHY_STYLE_COMMANDS)),
        latex_optional_external_commands=tuple(sorted(_OPTIONAL_EXTERNAL_COMMANDS)),
        latex_resource_suffixes=tuple(
            DependencySuffixBehaviorV1(command=command, suffixes=tuple(suffixes))
            for command, suffixes in sorted(_RESOURCE_SUFFIXES.items())
        ),
        latex_generated_input_suffixes=tuple(sorted(_GENERATED_INPUT_SUFFIXES)),
        latex_inactive_environments=tuple(_INACTIVE_ENVIRONMENTS),
        latex_dependency_rules=(
            "comments-and-inactive-environments-are-masked-before-dependency-and-math-analysis",
            "paper-root-search-order=root-source-directory-then-project",
            "source-recursion-preserves-root-search-order",
            "optional-system-class-package-style-missing-is-external",
            "missing-dynamic-cycle-and-mixed-backend-diagnostics-fail-safe",
            "dependency-files-sources-bibliographies-styles-resources-deduplicate-first-seen",
        ),
        ownership_schema_version=ARTIFACT_OWNERSHIP_SCHEMA,
        ownership_rules=owner_rules,
        ownership_matcher_rules=(
            "normalize-backslash-to-posix-and-remove-leading-dot-slash",
            "lowercase-pattern-and-path-before-fnmatchcase",
            "globstar-slash-may-match-zero-components",
            "registry-first-match-wins",
            "protected-paper-semantic-and-raw-paper-routing-precede-registry",
            "unknown-authored-change-fails-closed-to-MATH_DIRTY-stage8-and-RESULT_DIRTY-stage4",
        ),
        classification_rules=(
            "changed-paths=sorted-union-where-before-value-differs-from-after",
            "protected-pseudo-paths-route-MATH_DIRTY-stage8",
            "paper-math-citation-prose-format-route-to-stages8-9-9-9",
            "raw-active-tex-change-defers-to-semantic-pseudo-paths",
            "raw-active-tex-without-semantic-delta-routes-FORMAT_DIRTY-stage9",
            "registry-owner-controls-flag-and-stage",
            "duplicate-(flag,cause)-uses-first-change",
            "semantic-subset-is-MODEL_DIRTY-MATH_DIRTY-RESULT_DIRTY",
        ),
        output_rules=(
            "manifest-keys-and-classification-input-paths-are-deterministically-sorted",
            "manifest-fingerprint-is-canonical-json-sha256",
            "classification-output-preserves-first-insertion-over-sorted-changed-paths",
            "DirtyChange-fields-are-flag-owner-stage-cause-before-after",
        ),
        step13_condition_operands=step13[0],
    )


def validate_dirty_classifier_semantic_contract(
    value: DirtyClassifierSemanticContractV1,
) -> DirtyClassifierSemanticContractV1:
    _validate_closed(value, DirtyClassifierSemanticContractV1, "semantic_contract")
    expected = compile_dirty_classifier_semantic_contract()
    if value != expected:
        raise DirtyClassifierIdentityValidationError(
            "dirty classifier semantic behavior differs from source-authorized projection"
        )
    return value


def dirty_classifier_semantic_bytes(
    value: DirtyClassifierSemanticContractV1 | None = None,
) -> bytes:
    contract = validate_dirty_classifier_semantic_contract(
        value if value is not None else compile_dirty_classifier_semantic_contract()
    )
    try:
        return canonical_bytes(contract)
    except CanonicalizationError as exc:  # pragma: no cover - structural boundary
        raise DirtyClassifierIdentityValidationError(
            "dirty classifier semantic contract cannot be canonicalized"
        ) from exc


def dirty_classifier_semantic_sha256(
    value: DirtyClassifierSemanticContractV1 | None = None,
) -> str:
    contract = validate_dirty_classifier_semantic_contract(
        value if value is not None else compile_dirty_classifier_semantic_contract()
    )
    try:
        return canonical_sha256(contract)
    except CanonicalizationError as exc:  # pragma: no cover - structural boundary
        raise DirtyClassifierIdentityValidationError(
            "dirty classifier semantic contract cannot be canonicalized"
        ) from exc


def compile_dirty_classifier_identity_bundle(
    *,
    workflow_bundle: WorkflowContractBundle | None = None,
    analysis_evidence_refs: tuple[str, ...] = (),
) -> DirtyClassifierIdentityBundleV1:
    semantic = compile_dirty_classifier_semantic_contract(workflow_bundle)
    return DirtyClassifierIdentityBundleV1(
        schema_version=DIRTY_CLASSIFIER_IDENTITY_BUNDLE_SCHEMA,
        semantic_contract=semantic,
        dirty_classifier_semantic_sha256=dirty_classifier_semantic_sha256(semantic),
        operational_manifest_schema_version=DIRTY_CLASSIFIER_IMPLEMENTATION_MANIFEST_SCHEMA,
        dirty_classifier_operational_implementation_sha256=(
            dirty_classifier_operational_implementation_sha256()
        ),
        analysis_source_locators=(
            "factory_core/dirty.py",
            "factory_core/artifact_ownership.py",
            "factory_core/paper_sources.py",
            "factory_core/workflow_contract.py:validated-step13-condition",
        ),
        analysis_conformance_corpus=(
            "tests/test_dirty.py",
            "tests/test_artifact_ownership_overlap.py",
            "tests/test_m02_shadow_scheduler.py",
            "tests/test_m03_classifier_identity.py",
        ),
        analysis_evidence_refs=analysis_evidence_refs,
    )


def validate_dirty_classifier_identity_bundle(
    value: DirtyClassifierIdentityBundleV1,
) -> DirtyClassifierIdentityBundleV1:
    _validate_closed(value, DirtyClassifierIdentityBundleV1, "identity_bundle")
    if value.schema_version != DIRTY_CLASSIFIER_IDENTITY_BUNDLE_SCHEMA:
        raise DirtyClassifierIdentityValidationError("dirty classifier identity schema is unsupported")
    validate_dirty_classifier_semantic_contract(value.semantic_contract)
    if value.dirty_classifier_semantic_sha256 != dirty_classifier_semantic_sha256(
        value.semantic_contract
    ):
        raise DirtyClassifierIdentityValidationError("dirty classifier semantic hash mismatch")
    if value.operational_manifest_schema_version != DIRTY_CLASSIFIER_IMPLEMENTATION_MANIFEST_SCHEMA:
        raise DirtyClassifierIdentityValidationError("dirty classifier operational manifest schema drift")
    if value.dirty_classifier_operational_implementation_sha256 != (
        dirty_classifier_operational_implementation_sha256()
    ):
        raise DirtyClassifierIdentityValidationError(
            "dirty classifier operational implementation differs from trusted manifest"
        )
    for field_name in (
        "dirty_classifier_semantic_sha256",
        "dirty_classifier_operational_implementation_sha256",
    ):
        if _SHA256_RE.fullmatch(getattr(value, field_name)) is None:
            raise DirtyClassifierIdentityValidationError(f"identity_bundle.{field_name} must be lowercase SHA-256")
    expected = compile_dirty_classifier_identity_bundle(
        workflow_bundle=_validated_source_workflow_bundle(),
        analysis_evidence_refs=value.analysis_evidence_refs
    )
    behavior_fields = (
        "schema_version",
        "semantic_contract",
        "dirty_classifier_semantic_sha256",
        "operational_manifest_schema_version",
        "dirty_classifier_operational_implementation_sha256",
    )
    for field_name in behavior_fields:
        if getattr(value, field_name) != getattr(expected, field_name):
            raise DirtyClassifierIdentityValidationError(
                f"dirty classifier identity behavior drift field {field_name}"
            )
    return value


def dirty_classifier_analysis_bytes(value: DirtyClassifierIdentityBundleV1) -> bytes:
    bundle = validate_dirty_classifier_identity_bundle(value)
    try:
        return canonical_bytes(bundle)
    except CanonicalizationError as exc:
        raise DirtyClassifierIdentityValidationError(
            "dirty classifier identity bundle cannot be canonicalized"
        ) from exc


def dirty_classifier_analysis_sha256(value: DirtyClassifierIdentityBundleV1) -> str:
    bundle = validate_dirty_classifier_identity_bundle(value)
    try:
        return canonical_sha256(bundle)
    except CanonicalizationError as exc:
        raise DirtyClassifierIdentityValidationError(
            "dirty classifier identity bundle cannot be canonicalized"
        ) from exc
