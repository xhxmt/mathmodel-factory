"""S1-A: the artifact policy layer must be a pure compatibility façade.

S1-A introduces ``ArtifactPolicy`` / ``artifact_policy()`` and derives a policy
for every existing ownership rule.  It must change **no** behaviour, so the tests
here are mostly invariants relating the new API to the old one rather than
assertions about new features.

The three CI invariants from the plan (S1.4) are covered:
  1  artifact_policy(path).ownership_rule == artifact_ownership(path)
  2  compatibility policy fields are copied verbatim from the legacy rule
  3  a policy-only pattern may not shadow an ownership rule without an allowlist
"""
from __future__ import annotations

import pytest

from factory_core.current_artifact_ownership import (
    ARTIFACT_OWNERSHIP_REGISTRY,
    ADDITIONAL_OWNERSHIP,
    _OWNERSHIP_ORDER,
    artifact_owner_stage,
    artifact_ownership,
)
from factory_core.artifact_policy import (
    NATIVE_POLICY,
    POLICY_ONLY_SHADOW_ALLOWLIST,
    POLICY_ORDER,
    ArtifactPolicy,
    ArtifactRole,
    InvalidationMode,
    artifact_policy,
    artifact_policy_owner_stage,
    compatibility_policy,
    policies_for_role,
    shadowed_ownership_rules,
)

#: The 21 paths that A's history showed falling into the fail-closed fallback
#: (``MATH@8 + RESULT@4``).  S1-B registers them; S1-A must leave them alone.
OBSERVED_GAP_PATHS = (
    "step5_bounded_repair_plan.md",
    "step5_bounded_repair_report.md",
    "step5_readonly_recovery_plan.md",
    "step5_readonly_recovery_report.md",
    "step5_results_gap_report.md",
    "step5_reuse_gap_record.md",
    "step5_scope_alignment_report.md",
    "step4_reuse_gap_record.md",
    "m1_reuse_gap_record.md",
    "m4_reuse_gap_record.md",
    "m1_solver_evidence.json",
    "m4_solver_evidence.json",
    "m1_solver_evidence_failed.json",
    "model_source_map.json",
    "paper/appendix_sources/06_figures.py",
    "paper/appendix_sources/pro01/input_arrays.npz",
    "paper/appendix_sources/pro01/input_audit.json",
    "paper/appendix_sources/pro01/manifest.json",
    "paper/appendix_sources/pro01/prototype.py",
    "tables.tex",
    "results_values.tex",
)

EDGE_CASE_PATHS = (
    "",
    "./results/canonical_results.json",       # leading ./ normalization
    "RESULTS/CANONICAL_RESULTS.JSON",         # case-insensitivity
    "results\\canonical_results.json",        # backslash normalization
    "results/nested/deep/values.json",
    "unknown_authored_contract.json",
    ".factory/solver_receipts/local_x.submitted.json",
    "paper/main_paper.tex",
    "tables/m1_step12_table1.tex",
)


def _witness(pattern: str) -> str:
    return pattern.replace("**", "x").replace("*", "x")


#: A corpus that hits every ownership rule plus the real-world cases.
PATH_CORPUS = tuple(
    dict.fromkeys(
        [*(_witness(rule.pattern) for rule in _OWNERSHIP_ORDER),
         *OBSERVED_GAP_PATHS,
         *EDGE_CASE_PATHS]
    )
)


# --------------------------------------------------------------- invariants 1, 2
@pytest.mark.parametrize("path", PATH_CORPUS)
def test_all_ownership_backed_policies_agree_with_legacy_resolution(path):
    """Invariant 1, strengthened to *identity* rather than equality.

    The policy must carry the very object the legacy API returns, so no two
    registries can drift apart for the same path.
    """

    policy = artifact_policy(path)
    legacy = artifact_ownership(path)
    if policy is None:
        assert legacy is None, path
        return
    if policy.ownership_rule is None:
        # policy-only: it must NOT appear in the legacy registry
        assert legacy is None, path
        return
    assert policy.ownership_rule is legacy, path


def test_every_ownership_rule_has_exactly_one_compatibility_policy():
    backed = [p for p in POLICY_ORDER if p.ownership_rule is not None]
    assert len(backed) == len(_OWNERSHIP_ORDER)
    # order is preserved exactly: native first, then ADDITIONAL, then frozen
    assert [p.ownership_rule for p in backed] == list(_OWNERSHIP_ORDER)
    assert [p.pattern for p in backed] == [r.pattern for r in _OWNERSHIP_ORDER]


def test_compatibility_policy_copies_every_delivery_field_verbatim():
    """Invariant 2: field-by-field equality, no derivation, no defaults."""

    for rule in _OWNERSHIP_ORDER:
        policy = compatibility_policy(rule)
        assert policy.pattern == rule.pattern
        assert policy.role == rule.semantic_domain
        assert policy.final_input == rule.final_input
        assert policy.submission_member == rule.submission_member
        assert policy.ownership_rule is rule
        assert policy.invalidation_mode == InvalidationMode.UPSTREAM_RECOMPUTE.value
        assert policy.blocker is None


def test_compatibility_phase_registers_nothing_new():
    """S1-A is a no-op: today every policy is ownership-backed."""

    assert NATIVE_POLICY == ()
    assert all(p.ownership_rule is not None for p in POLICY_ORDER)
    assert len(POLICY_ORDER) == len(ARTIFACT_OWNERSHIP_REGISTRY)
    assert len(ADDITIONAL_OWNERSHIP) == 5


@pytest.mark.parametrize("path", OBSERVED_GAP_PATHS)
def test_observed_gap_paths_are_still_unregistered_in_s1a(path):
    """S1-A must not quietly register the 21 fallback paths.

    Registering them is S1-B's deliberate, reviewed change; doing it here would
    hide a behaviour change inside a "no behaviour change" commit.
    """

    assert artifact_policy(path) is None, path
    assert artifact_ownership(path) is None, path


# ------------------------------------------------------------------ invariant 3
def test_no_policy_only_entry_shadows_an_ownership_rule():
    """Invariant 3. Vacuous while NATIVE_POLICY is empty, hence the detector test
    below proves the check is not silently doing nothing."""

    assert shadowed_ownership_rules() == []
    assert POLICY_ONLY_SHADOW_ALLOWLIST == frozenset()


def test_shadow_detector_actually_detects_shadowing():
    """Guard against a vacuous invariant: feed it a policy that does shadow."""

    shadowing = ArtifactPolicy(
        pattern="results/**",                      # steals from the Stage-4 rules
        role=ArtifactRole.EXPLORATORY.value,
        invalidation_mode=InvalidationMode.EXPLICIT_ONLY.value,
        final_input=False,
        submission_member=False,
        ownership_rule=None,
    )
    legacy_policies = tuple(compatibility_policy(r) for r in _OWNERSHIP_ORDER)
    hits = shadowed_ownership_rules(policies=(shadowing, *legacy_policies))
    assert hits, "the detector must report a shadowing policy-only entry"
    assert any(rule_pattern.startswith("results/") for _, rule_pattern, _ in hits)


# ------------------------------------------------------- zero behaviour change
@pytest.mark.parametrize("path", PATH_CORPUS)
def test_policy_presence_matches_ownership_presence(path):
    """The policy layer must never become a second, divergent authority."""

    assert (artifact_policy(path) is None) == (artifact_ownership(path) is None), path


@pytest.mark.parametrize("path", PATH_CORPUS)
def test_stage_routing_is_unchanged(path):
    assert artifact_policy_owner_stage(path) == artifact_owner_stage(path)
    assert artifact_policy_owner_stage(path, default=99) == artifact_owner_stage(
        path, default=99
    )


def test_normalization_and_case_folding_match_the_legacy_matcher():
    assert artifact_policy("./results/canonical_results.json") is artifact_policy(
        "results/canonical_results.json"
    )
    assert artifact_policy("RESULTS/CANONICAL_RESULTS.JSON") is artifact_policy(
        "results/canonical_results.json"
    )
    assert artifact_policy("results\\canonical_results.json") is artifact_policy(
        "results/canonical_results.json"
    )


def test_the_module_does_not_change_the_frozen_classifier_identity():
    """S1-A must not disturb the frozen trust root.

    ``artifact_policy`` is deliberately outside ``classifier_contract_sha256()``;
    if importing/using it moved that hash, historical receipts would stop being
    comparable.
    """

    import factory_core.dirty as dirty
    import factory_core.current_dirty as current_dirty

    assert dirty.classifier_contract_sha256() == (
        "c451a9d0be64fabd185c7561b67093956e663db6e845915cd320fea1cbabc515"
    )
    assert current_dirty.classifier_contract_sha256() != dirty.classifier_contract_sha256()


# ------------------------------------------------------- policy semantics (S1.3)
def test_non_blocking_semantics_require_all_three_conditions():
    base = dict(
        pattern="x.md",
        role=ArtifactRole.DIAGNOSTIC.value,
        invalidation_mode=InvalidationMode.EXPLICIT_ONLY.value,
        final_input=False,
        submission_member=False,
    )
    assert ArtifactPolicy(**base).is_non_blocking is True

    # a named blocker disqualifies it
    assert ArtifactPolicy(**base, blocker="paper_audit").is_non_blocking is False
    # either delivery flag true disqualifies it
    assert ArtifactPolicy(**{**base, "final_input": True}).is_non_blocking is False
    assert ArtifactPolicy(**{**base, "submission_member": True}).is_non_blocking is False
    # and a different mode is not the explicit-only declaration at all
    assert ArtifactPolicy(
        **{**base, "invalidation_mode": InvalidationMode.PRESENTATION_ONLY.value}
    ).is_non_blocking is False


def test_declares_blocker_separates_declared_contracts_from_non_blocking():
    non_blocking = ArtifactPolicy(
        pattern="x.md",
        role=ArtifactRole.EXPLORATORY.value,
        invalidation_mode=InvalidationMode.EXPLICIT_ONLY.value,
        final_input=False,
        submission_member=False,
    )
    assert non_blocking.declares_blocker is False

    for mode in (
        InvalidationMode.UPSTREAM_RECOMPUTE,
        InvalidationMode.EVIDENCE_SUFFICIENCY,
        InvalidationMode.FAIL_CLOSED,
    ):
        assert ArtifactPolicy(
            pattern="y.md",
            role=ArtifactRole.EVIDENCE.value,
            invalidation_mode=mode.value,
        ).declares_blocker is True


def test_role_vocabulary_is_closed_and_compatibility_roles_are_frozen_domains():
    frozen_domains = {rule.semantic_domain for rule in _OWNERSHIP_ORDER}
    # compatibility roles are the frozen semantic domains, not invented values
    assert {p.role for p in POLICY_ORDER if p.ownership_rule is not None} == frozen_domains
    # the closed vocabulary is defined for S1-B to assign
    assert {r.value for r in ArtifactRole} >= {
        "DIAGNOSTIC",
        "REPAIR_EVIDENCE",
        "EVIDENCE",
        "EVIDENCE_INDEX",
        "HISTORICAL_EVIDENCE",
        "DERIVED_GENERATOR",
        "DERIVED_PRESENTATION",
        "EXPLORATORY",
    }
    assert policies_for_role("canonical_result")