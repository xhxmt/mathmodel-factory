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
    iter_owned_artifacts,
    ADDITIONAL_OWNERSHIP,
    _OWNERSHIP_ORDER,
    artifact_owner_stage,
    artifact_ownership,
)
from factory_core.artifact_policy import (
    NATIVE_POLICY,
    iter_policy_artifacts,
    policy_ownership_rule,
    reopen_after_step_for_policy_artifact,
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


def policy_owner_stage_is_none(path) -> bool:
    """A policy-only path must resolve to no Stage through the policy layer."""

    from factory_core.artifact_policy import artifact_policy_owner_stage

    return artifact_policy_owner_stage(path) is None


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


def test_s1b_registers_exactly_the_observed_gap_and_nothing_else():
    """The registration is exactly the 21 observed paths, all policy-only."""

    covered = {
        path for path in OBSERVED_GAP_PATHS
        if artifact_policy(path) is not None
    }
    assert covered == set(OBSERVED_GAP_PATHS)
    assert len(NATIVE_POLICY) == 18, "18 entries whose globs cover 21 paths"
    assert all(p.is_policy_only for p in NATIVE_POLICY)
    # every ownership rule still has its compatibility policy, and no ownership
    # rule was displaced
    backed = [p for p in POLICY_ORDER if p.ownership_rule is not None]
    assert len(backed) == len(ARTIFACT_OWNERSHIP_REGISTRY)
    assert len(ADDITIONAL_OWNERSHIP) == 5
    # and nothing shadowed the legacy registry
    assert shadowed_ownership_rules() == []


@pytest.mark.parametrize("path", OBSERVED_GAP_PATHS)
def test_observed_gap_paths_are_registered_by_s1b_without_gaining_an_owner(path):
    """S1-B's whole point.

    Each of the 21 fallback paths is now described by a policy, and **none of
    them gained a Stage owner** - the legacy registry still returns None for
    every one. That is what keeps this a classification fix rather than an
    invented ownership.
    """

    policy = artifact_policy(path)
    assert policy is not None, path
    assert policy.is_policy_only, path
    assert policy.ownership_rule is None, path
    assert artifact_ownership(path) is None, f"must not gain a Stage owner: {path}"
    assert policy_owner_stage_is_none(path), path


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
def test_policy_presence_covers_and_may_extend_ownership_presence(path):
    """S1-B widens coverage deliberately; it must not narrow it or diverge.

    The relationship is now:
      * every path the legacy registry resolves must resolve through the policy
        layer to the *same* rule, and
      * a policy-only entry must NOT be visible to ``artifact_ownership``.
    Equality would be wrong once policy-only entries exist - and equality in the
    other direction (policy missing where ownership exists) is still forbidden.
    """

    policy = artifact_policy(path)
    legacy = artifact_ownership(path)
    if legacy is not None:
        assert policy is not None, f"policy must cover an owned path: {path}"
        assert policy.ownership_rule is legacy, path
    elif policy is not None:
        assert policy.is_policy_only, path
        assert legacy is None, path


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

# ===========================================================================
# S1-C-prep: the migrated consumers must be exact equivalents
# ===========================================================================

def _tree(tmp_path, relatives):
    root = tmp_path / "proj"
    for rel in relatives:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    return root


#: A tree that exercises collector membership: final-input yes/no, submission
#: yes/no, unregistered, archived and scratch paths.
_COLLECTOR_TREE = (
    "results/canonical_results.json",
    "results/problem1/values.json",
    "judge_evidence.json",
    "STEP5_RECEIPT.json",
    "method_fit_suggestions.json",
    ".factory/solver_inputs/snap.json",
    "problem/problem_brief.md",
    "m1_spec.md",
    "solve_log.md",
    "abstract_draft.md",
    "references.bib",
    "tables/t.tex",
    "paper/main_paper.tex",
    "result1.xlsx",
    "style/s.tex",
    "unknown_authored_contract.json",
    "archive/old.json",
    "__pycache__/c.json",
    "data/intermediate/scratch.csv",
)

_ITERATOR_KWARGS = (
    {},
    {"final_input_only": True},
    {"submission_only": True},
    {"final_input_only": True, "include_symlinks": True},
    {"submission_only": True, "include_symlinks": True},
    {"final_input_only": True, "submission_only": True},
)


@pytest.mark.parametrize("kwargs", _ITERATOR_KWARGS)
def test_policy_iterator_is_set_identical_to_the_ownership_iterator(tmp_path, kwargs):
    """The core S1-C-prep guarantee.

    While NATIVE_POLICY is empty the policy-aware collector must yield exactly
    the same files, otherwise the migration itself would change what ships or
    what the final audit consumes.
    """

    root = _tree(tmp_path, _COLLECTOR_TREE)
    owned = {
        p.relative_to(root).as_posix()
        for p in iter_owned_artifacts(root, **kwargs)
    }
    policy = {
        p.relative_to(root).as_posix()
        for p in iter_policy_artifacts(root, **kwargs)
    }
    assert policy == owned


def test_policy_iterator_mirrors_the_walk_and_skip_order(tmp_path):
    """Including the archive/__pycache__ skip, which is not ownership-driven."""

    root = _tree(tmp_path, _COLLECTOR_TREE)
    got = {
        p.relative_to(root).as_posix()
        for p in iter_policy_artifacts(root)
    }
    assert "archive/old.json" not in got
    assert "__pycache__/c.json" not in got
    # data/intermediate/** IS registered, but explicitly final_input=False and
    # submission_member=False, so it shows in the unfiltered walk and in neither
    # delivery set.
    assert "data/intermediate/scratch.csv" in got
    assert "data/intermediate/scratch.csv" not in {
        p.relative_to(root).as_posix()
        for p in iter_policy_artifacts(root, final_input_only=True)
    }
    assert "data/intermediate/scratch.csv" not in {
        p.relative_to(root).as_posix()
        for p in iter_policy_artifacts(root, submission_only=True)
    }
    assert "unknown_authored_contract.json" not in got, "unregistered is skipped"
    assert "results/canonical_results.json" in got


@pytest.mark.parametrize("path", PATH_CORPUS)
def test_policy_ownership_rule_matches_the_legacy_lookup(path):
    """``policy_ownership_rule`` must agree with ``artifact_ownership`` for every
    path that has a rule, and be None exactly where the legacy lookup is None."""

    assert policy_ownership_rule(path) is artifact_ownership(path) if (
        artifact_ownership(path) is not None
    ) else policy_ownership_rule(path) is None


@pytest.mark.parametrize("path", PATH_CORPUS)
def test_policy_reopen_target_matches_the_legacy_one(path):
    from factory_core.current_artifact_ownership import reopen_after_step_for_artifact

    assert reopen_after_step_for_policy_artifact(path) == (
        reopen_after_step_for_artifact(path)
    )
    assert reopen_after_step_for_policy_artifact(path, default_stage=7) == (
        reopen_after_step_for_artifact(path, default_stage=7)
    )


def test_submission_coverage_gate_accepts_exactly_the_same_paths(tmp_path):
    """The two fail-closed gates in submission_bundle are membership tests.

    If the migration had widened them, the submission bundle could silently
    grow; if it had narrowed them, a previously valid bundle would start
    raising.  Both directions are asserted here.
    """

    root = _tree(tmp_path, _COLLECTOR_TREE)
    for rel in _COLLECTOR_TREE:
        legacy_none = artifact_ownership(rel) is None
        policy_none = artifact_policy(rel) is None
        assert policy_none == legacy_none, rel


def test_diagnostics_owner_stage_now_uses_the_current_registry():
    """Regression for a real drift the migration fixes.

    web/backend/diagnostics_service.py imported the FROZEN registry, so it
    reported owner_stage=None for judge_evidence.json (really 10) and 3 for
    scope_review_manifest.json (really 10).
    """

    from factory_core.artifact_ownership import artifact_owner_stage as frozen_lookup

    for path, expected in (
        ("judge_evidence.json", 10),
        ("models/reporting_scope/scope_review_manifest.json", 10),
        ("STEP5_RECEIPT.json", 4),
        ("method_fit_suggestions.json", 1),
    ):
        assert artifact_policy_owner_stage(path) == expected
        assert frozen_lookup(path) != expected, (
            "this path is precisely where the frozen lookup drifted"
        )
        assert artifact_policy_owner_stage(path) == artifact_owner_stage(path)


def test_every_ownership_consumer_is_migrated_except_the_frozen_classifier():
    """A source-level guard: no production module may still call the legacy
    owner lookups, except factory_core/dirty.py (frozen trust root) and the
    provenance mirror that deliberately reproduces the frozen branch order.
    """

    import re
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    allowed = {
        "factory_core/dirty.py",                 # frozen: hashed into the classifier identity
        "factory_core/dirty_classification.py",  # mirrors the frozen classifier on purpose
        "factory_core/artifact_ownership.py",    # the definitions
        "factory_core/current_artifact_ownership.py",
        "factory_core/artifact_policy.py",
    }
    legacy_names = (
        "artifact_ownership",
        "artifact_owner_stage",
        "reopen_after_step_for_artifact",
        "iter_owned_artifacts",
    )
    call = re.compile(r"\b(" + "|".join(legacy_names) + r")\(")
    #: A module is migrated when the name it calls is bound by an import from the
    #: policy layer - including aliases such as
    #: ``from ...artifact_policy import artifact_policy_owner_stage as artifact_owner_stage``.
    policy_import = re.compile(
        r"^\s*from [\w.]*artifact_policy import ([^\n(]+)", re.M
    )
    offenders = []
    for base in ("factory_core", "apps", "scripts", "web"):
        for source in (root / base).rglob("*.py"):
            rel = source.relative_to(root).as_posix()
            if rel in allowed or "__pycache__" in rel:
                continue
            text = source.read_text(encoding="utf-8", errors="replace")
            rebound = set()
            for import_list in policy_import.findall(text):
                for item in import_list.split(","):
                    item = item.strip()
                    if not item:
                        continue
                    if " as " in item:
                        rebound.add(item.split(" as ")[-1].strip())
                    else:
                        rebound.add(item)
            for match in call.finditer(text):
                if match.group(1) in rebound:
                    continue
                line = text[: match.start()].count("\n") + 1
                offenders.append(
                    f"{rel}:{line}  {text.splitlines()[line - 1].strip()[:70]}"
                )
    assert offenders == [], "unmigrated ownership consumers:\n" + "\n".join(offenders)
