"""Artifact policy: a semantic layer above the ownership registry (S1-A).

Why this exists
---------------
``artifact_ownership()`` answers one question - which Stage owns this path - and
is then reused for five different jobs: dirty routing, finalization recovery,
Judge missing-evidence routing, diagnostics, and final/submission input
collection.  Conflating "who owns it" with "what is it", "what does changing it
invalidate", "does it ship" and "does it enter the final audit" is what produced
the failures this project is fixing: a purely observational file changing caused
a rewind to the solve stage, because its only representation was an ownership
rule (or, when unregistered, the fail-closed fallback).

``ArtifactPolicy`` separates those four concerns:

    pattern             which paths this covers
    role                what kind of artifact it is
    invalidation_mode   what a change to it invalidates
    final_input         does it enter the final audit input
    submission_member   does it ship in the submission bundle
    ownership_rule      the legacy routing rule, when there is one

S1-A is a compatibility façade: it introduces the structure and generates a
policy for every existing ownership rule, without changing any behaviour.

    - ``artifact_ownership()`` is untouched (byte-for-byte behaviour preserved).
    - ``artifact_policy(path).ownership_rule`` is, for every path an ownership
      rule matches, exactly the rule ``artifact_ownership(path)`` returns.
    - A policy with ``ownership_rule=None`` is "policy-only": it is described and
      classified, but carries no Stage routing.  Such entries deliberately do NOT
      appear in ``artifact_ownership()``.

That last point is the whole reason the layer exists: S1-B registers the 21
currently-unregistered paths that fall into the fail-closed fallback
(``MATH@8 + RESULT@4``) as policy-only entries, so that their changes stop
implying a solve-stage rewind - without pretending they have a Stage owner, and
without a second ownership registry.

Relationship to the classifier identity
---------------------------------------
This module is deliberately NOT part of ``classifier_contract_sha256()``
(``factory_core/dirty.py`` + ``artifact_ownership.py``), whose bytes are a frozen
compatibility trust root and are hashed into historical receipts.  The provenance
contract that explains how a change was *attributed* is
``dirty_classification.classification_contract_sha256()``; the contract that
explains what a change *produces* stays with the classifier identity.  Keeping
the two apart means a provenance-only change cannot force a dirty rebase (Q14).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path

from .artifact_ownership import ArtifactOwnership
from .current_artifact_ownership import (
    ADDITIONAL_OWNERSHIP,
    _OWNERSHIP_ORDER,
    _pattern_variants,
    normalize_artifact_path,
)

ARTIFACT_POLICY_SCHEMA = "factory-artifact-policy-v1"


class ArtifactRole(str, Enum):
    """What kind of artifact this is.

    Assigning these to the registered paths is S1-B work.  During the
    compatibility phase a policy carries the frozen rule's ``semantic_domain``
    verbatim instead, so that no mapping is invented before it is reviewed.
    """

    PRODUCTION = "PRODUCTION"
    DERIVED = "DERIVED"
    EVIDENCE = "EVIDENCE"
    EVIDENCE_INDEX = "EVIDENCE_INDEX"
    HISTORICAL_EVIDENCE = "HISTORICAL_EVIDENCE"
    OBSERVATION = "OBSERVATION"
    DIAGNOSTIC = "DIAGNOSTIC"
    REPAIR_EVIDENCE = "REPAIR_EVIDENCE"
    DERIVED_GENERATOR = "DERIVED_GENERATOR"
    DERIVED_PRESENTATION = "DERIVED_PRESENTATION"
    EXPLORATORY = "EXPLORATORY"
    DELIVERY = "DELIVERY"


class InvalidationMode(str, Enum):
    """What a change to the artifact invalidates.

    ``UPSTREAM_RECOMPUTE`` is the only mode a compatibility policy can have: an
    ownership rule exists precisely to route a change back to its owning Stage.
    """

    UPSTREAM_RECOMPUTE = "UPSTREAM_RECOMPUTE"
    EVIDENCE_SUFFICIENCY = "EVIDENCE_SUFFICIENCY"
    PRESENTATION_ONLY = "PRESENTATION_ONLY"
    EXPLICIT_ONLY = "EXPLICIT_ONLY"
    FAIL_CLOSED = "FAIL_CLOSED"


#: A policy-only artifact whose own changes are declared to need no blocking:
#: ``EXPLICIT_ONLY`` + both delivery flags false + no named blocker.  It is a
#: positive declaration, not a missing consumer, and finalization must NOT fail
#: closed for it.  See the S1.3 closure table.
NON_BLOCKING_BY_POLICY = "NON_BLOCKING_BY_POLICY"


@dataclass(frozen=True)
class ArtifactPolicy:
    pattern: str
    role: str
    invalidation_mode: str
    final_input: bool = True
    submission_member: bool = True
    ownership_rule: ArtifactOwnership | None = None
    #: Name of the existing validator / finding / repair contract that blocks a
    #: change to this artifact.  Required by ``EXPLICIT_ONLY`` unless the artifact
    #: is both non-final-input and non-submission (S1.3 closure table).
    blocker: str | None = None
    #: Routing for a policy-only entry.  CI invariant 1 forbids giving it an
    #: ``ownership_rule`` (that would make the policy disagree with
    #: ``artifact_ownership``), so a policy-only entry that still has to route a
    #: change names its consequence explicitly here.
    route_stage: int | None = None
    route_flag: str | None = None

    @property
    def is_policy_only(self) -> bool:
        """True when this entry intentionally carries no Stage routing."""

        return self.ownership_rule is None

    @property
    def is_explicit_only(self) -> bool:
        return self.invalidation_mode == InvalidationMode.EXPLICIT_ONLY.value

    @property
    def is_non_blocking(self) -> bool:
        """``EXPLICIT_ONLY`` + both delivery flags false + no named blocker.

        A positive policy declaration ("this artifact's own changes need no
        blocking"), **not** a missing consumer.  finalization must not fail closed
        for it - that distinction is the S1.3 closure.
        """

        return (
            self.is_explicit_only
            and not self.final_input
            and not self.submission_member
            and self.blocker is None
        )

    @property
    def declares_blocker(self) -> bool:
        """The policy claims something must block a change to this artifact."""

        if self.blocker is not None:
            return True
        return self.invalidation_mode in {
            InvalidationMode.UPSTREAM_RECOMPUTE.value,
            InvalidationMode.EVIDENCE_SUFFICIENCY.value,
            InvalidationMode.FAIL_CLOSED.value,
        }


def compatibility_policy(rule: ArtifactOwnership) -> ArtifactPolicy:
    """Derive the policy for one existing ownership rule.

    Every field is copied verbatim from the rule, so the policy cannot disagree
    with the legacy routing it is describing (CI invariant 2).
    """

    return ArtifactPolicy(
        pattern=rule.pattern,
        role=rule.semantic_domain,
        invalidation_mode=InvalidationMode.UPSTREAM_RECOMPUTE.value,
        final_input=rule.final_input,
        submission_member=rule.submission_member,
        ownership_rule=rule,
    )


#: Explicit policies for the 21 paths that A's real history showed falling into
#: the fail-closed fallback (MATH@8 + RESULT@4).  S1-B.
#
#: Every one of them is policy-only: none has a Stage owner, and none gains one.
#: The change they make is that a modification stops implying a *solve* rewind
#: unless it genuinely is result evidence.
#:
#: Producer/consumer reading behind the roles (Q2 forbids guessing):
#:   - the templates below have NO producer in Python; they are agent-authored
#:   - results/canonical_results.json lists m1/m4_solver_evidence.json in its
#:     evidence closure, so those two keep RESULT@4 - dropping it would let a
#:     genuine result-evidence change pass unnoticed
#:   - derived_artifacts_verification.latest.json binds results_values.tex by
#:     byte comparison with "matches_regenerated", which is why the two .tex
#:     artifacts are PRESENTATION_ONLY and rely on that chain
#:   - tables.tex/results_values.tex and the appendix generators are rebuildable
#:     presentation, so their own change yields FORMAT@9 and never a rewind
#:   - the diagnostic/repair reports and the exploratory appendix sources declare
#:     EXPLICIT_ONLY: no obligation is created, and blocking - if any - must come
#:     from a named finding/repair contract
#:
#: All 21 are final_input=False and submission_member=False.  Promoting any of
#: them into a delivery closure would change what ships or what the final audit
#: consumes, and that is a separate, explicitly reviewed decision - not a
#: side effect of removing a misclassification.
NATIVE_POLICY: tuple[ArtifactPolicy, ...] = (
    # -- Step 5 diagnostics and repair evidence: reports about work, not work.
    ArtifactPolicy("step5_results_gap_report.md", ArtifactRole.DIAGNOSTIC.value,
                   InvalidationMode.EXPLICIT_ONLY.value, False, False),
    ArtifactPolicy("step5_scope_alignment_report.md", ArtifactRole.DIAGNOSTIC.value,
                   InvalidationMode.EXPLICIT_ONLY.value, False, False),
    ArtifactPolicy("step5_reuse_gap_record.md", ArtifactRole.DIAGNOSTIC.value,
                   InvalidationMode.EXPLICIT_ONLY.value, False, False),
    ArtifactPolicy("step5_bounded_repair_plan.md", ArtifactRole.REPAIR_EVIDENCE.value,
                   InvalidationMode.EXPLICIT_ONLY.value, False, False),
    ArtifactPolicy("step5_bounded_repair_report.md", ArtifactRole.REPAIR_EVIDENCE.value,
                   InvalidationMode.EXPLICIT_ONLY.value, False, False),
    ArtifactPolicy("step5_readonly_recovery_plan.md", ArtifactRole.REPAIR_EVIDENCE.value,
                   InvalidationMode.EXPLICIT_ONLY.value, False, False),
    ArtifactPolicy("step5_readonly_recovery_report.md", ArtifactRole.REPAIR_EVIDENCE.value,
                   InvalidationMode.EXPLICIT_ONLY.value, False, False),
    ArtifactPolicy("step4_reuse_gap_record.md", ArtifactRole.DIAGNOSTIC.value,
                   InvalidationMode.EXPLICIT_ONLY.value, False, False),
    ArtifactPolicy("m1_reuse_gap_record.md", ArtifactRole.DIAGNOSTIC.value,
                   InvalidationMode.EXPLICIT_ONLY.value, False, False),
    ArtifactPolicy("m4_reuse_gap_record.md", ArtifactRole.DIAGNOSTIC.value,
                   InvalidationMode.EXPLICIT_ONLY.value, False, False),
    ArtifactPolicy("m1_solver_evidence_failed.json", ArtifactRole.HISTORICAL_EVIDENCE.value,
                   InvalidationMode.EXPLICIT_ONLY.value, False, False),
    # -- Result-evidence closure: keep the rewind, drop the spurious MATH@8.
    ArtifactPolicy("m1_solver_evidence.json", ArtifactRole.EVIDENCE.value,
                   InvalidationMode.EVIDENCE_SUFFICIENCY.value, False, False,
                   blocker="canonical_results_evidence_closure",
                   route_stage=4, route_flag="RESULT_DIRTY"),
    ArtifactPolicy("m4_solver_evidence.json", ArtifactRole.EVIDENCE.value,
                   InvalidationMode.EVIDENCE_SUFFICIENCY.value, False, False,
                   blocker="canonical_results_evidence_closure",
                   route_stage=4, route_flag="RESULT_DIRTY"),
    ArtifactPolicy("model_source_map.json", ArtifactRole.EVIDENCE_INDEX.value,
                   InvalidationMode.EVIDENCE_SUFFICIENCY.value, False, False,
                   blocker="canonical_results_evidence_closure",
                   route_stage=4, route_flag="RESULT_DIRTY"),
    # -- Rebuildable presentation: format obligation only, verified by the
    #    derived-artifact chain rather than by a rewind.
    ArtifactPolicy("tables.tex", ArtifactRole.DERIVED_PRESENTATION.value,
                   InvalidationMode.PRESENTATION_ONLY.value, False, False,
                   blocker="derived_artifacts_verification",
                   route_stage=9, route_flag="FORMAT_DIRTY"),
    ArtifactPolicy("results_values.tex", ArtifactRole.DERIVED_PRESENTATION.value,
                   InvalidationMode.PRESENTATION_ONLY.value, False, False,
                   blocker="derived_artifacts_verification",
                   route_stage=9, route_flag="FORMAT_DIRTY"),
    ArtifactPolicy("paper/appendix_sources/06_figures.py", ArtifactRole.DERIVED_GENERATOR.value,
                   InvalidationMode.PRESENTATION_ONLY.value, False, False,
                   blocker="derived_artifacts_verification",
                   route_stage=9, route_flag="FORMAT_DIRTY"),
    # -- Exploratory appendix sources: declared non-blocking, and both delivery
    #    flags false, so NON_BLOCKING_BY_POLICY applies and finalization must not
    #    fail closed for them.
    ArtifactPolicy("paper/appendix_sources/pro01/**", ArtifactRole.EXPLORATORY.value,
                   InvalidationMode.EXPLICIT_ONLY.value, False, False),
)

#: A policy-only pattern may only shadow an ownership rule if it is listed here
#: with a witness test.  Empty means "no shadowing is permitted" (CI invariant 3).
POLICY_ONLY_SHADOW_ALLOWLIST: frozenset[str] = frozenset()

#: Native entries win over the ownership order, mirroring
#: ``_OWNERSHIP_ORDER = ADDITIONAL_OWNERSHIP + FROZEN_REGISTRY``.
POLICY_ORDER: tuple[ArtifactPolicy, ...] = NATIVE_POLICY + tuple(
    compatibility_policy(rule) for rule in _OWNERSHIP_ORDER
)


@lru_cache(maxsize=65536)
def artifact_policy(path) -> ArtifactPolicy | None:
    """Return the policy for one artifact path, or None if unregistered.

    Matching semantics are identical to ``artifact_ownership``:
    case-insensitive, normalized, globstar, first match in policy order.
    """

    normalized = normalize_artifact_path(path).lower()
    for policy in POLICY_ORDER:
        if any(
            fnmatchcase(normalized, candidate)
            for candidate in _pattern_variants(policy.pattern)
        ):
            return policy
    return None


def artifact_policy_owner_stage(path, *, default=None):
    """Stage routing as the policy layer sees it (legacy rule or ``default``)."""

    policy = artifact_policy(path)
    if policy is None or policy.ownership_rule is None:
        return default
    return policy.ownership_rule.owner_stage


def policies_for_role(role: str) -> tuple[ArtifactPolicy, ...]:
    return tuple(p for p in POLICY_ORDER if p.role == role)



# --------------------------------------------------------------------------
# Policy-aware equivalents of the legacy consumers
#
# S1-C migrates every collection/routing consumer onto these.  While
# NATIVE_POLICY is empty they are exact equivalents of their legacy
# counterparts - that is what makes the migration reviewable - but they see
# policy-only entries, which the ownership-based versions silently skip.
# --------------------------------------------------------------------------

def policy_ownership_rule(path) -> ArtifactOwnership | None:
    """The legacy routing rule behind a path, or None.

    Unlike ``artifact_ownership()`` this also returns None for a policy-only
    entry, so callers reasoning about *routing* keep their old semantics while
    the policy layer gains the ability to describe the path.
    """

    policy = artifact_policy(path)
    return policy.ownership_rule if policy is not None else None


def reopen_after_step_for_policy_artifact(path, *, default_stage: int = 3) -> int:
    """Policy-aware reopen target.  Equivalent while only compatibility
    policies exist; a policy-only entry falls back to ``default_stage`` exactly
    as an unregistered path does today."""

    from .stages import resume_after_step_for_stage

    policy = artifact_policy(path)
    owner_stage = (
        policy.ownership_rule.owner_stage
        if policy is not None and policy.ownership_rule is not None
        else default_stage
    )
    return resume_after_step_for_stage(owner_stage)


def iter_policy_artifacts(project_dir, *, final_input_only=False,
                          submission_only=False, include_symlinks=False):
    """Policy-aware twin of ``iter_owned_artifacts``.

    Deliberately mirrors that function's walk and skip order exactly; the only
    difference is that membership is decided by ``artifact_policy`` instead of
    ``artifact_ownership``, so a policy-only entry is visible.
    """

    project = Path(project_dir).resolve()
    for path in sorted(project.rglob("*")):
        relative = path.relative_to(project)
        policy = artifact_policy(relative.as_posix())
        if policy is None or (final_input_only and not policy.final_input):
            continue
        if submission_only and not policy.submission_member:
            continue
        if any(part in {"archive", "__pycache__"} for part in relative.parts):
            continue
        if path.is_symlink():
            if include_symlinks:
                yield path
        elif path.is_file():
            yield path

def _witness_path(pattern: str) -> str:
    """A concrete path that the pattern matches, for shadowing analysis.

    ``**`` and ``*`` are replaced by a single component name; the frozen matcher
    treats them the same way for witness purposes.
    """

    return pattern.replace("**", "x").replace("*", "x")


def shadowed_ownership_rules(
    policies: tuple[ArtifactPolicy, ...] | None = None,
) -> list[tuple[str, str, str]]:
    """Policy-only entries that steal a path from an ownership rule.

    Returns ``(policy_pattern, ownership_pattern, witness_path)`` triples.  Any
    hit must be justified by ``POLICY_ONLY_SHADOW_ALLOWLIST`` plus a dedicated
    overlap test - silently turning "has a Stage owner" into "has none" is the
    failure mode this guards (CI invariant 3).
    """

    entries = POLICY_ORDER if policies is None else policies
    policy_only = [p for p in entries if p.ownership_rule is None]
    if not policy_only:
        return []

    found: list[tuple[str, str, str]] = []
    for policy in policy_only:
        witness = _witness_path(policy.pattern).lower()
        for rule in _OWNERSHIP_ORDER:
            if any(
                fnmatchcase(witness, candidate)
                for candidate in _pattern_variants(rule.pattern)
            ):
                found.append((policy.pattern, rule.pattern, witness))
    return found


__all__ = [
    "ARTIFACT_POLICY_SCHEMA",
    "ArtifactPolicy",
    "ArtifactRole",
    "InvalidationMode",
    "NATIVE_POLICY",
    "NON_BLOCKING_BY_POLICY",
    "POLICY_ONLY_SHADOW_ALLOWLIST",
    "POLICY_ORDER",
    "ADDITIONAL_OWNERSHIP",
    "artifact_policy",
    "artifact_policy_owner_stage",
    "compatibility_policy",
    "iter_policy_artifacts",
    "policies_for_role",
    "policy_ownership_rule",
    "reopen_after_step_for_policy_artifact",
    "shadowed_ownership_rules",
]