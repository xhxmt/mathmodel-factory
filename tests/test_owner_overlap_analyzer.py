from __future__ import annotations

from itertools import combinations_with_replacement, product

import pytest

from factory_core.artifact_ownership import ArtifactOwnership, artifact_pattern_matches
from factory_core.owner_compiler import (
    OwnerDiagnosticCode,
    PatternOverlapStatus,
    analyze_pattern_overlap,
    compile_owner_registry,
)


def test_interleaved_star_languages_produce_a_real_overlap_witness() -> None:
    compilation = compile_owner_registry(
        (
            ArtifactOwnership("a*b*c*", 3, "model", "MODEL_DIRTY"),
            ArtifactOwnership("a*c*b*", 5, "validation", "RESULT_DIRTY"),
        )
    )

    diagnostic = next(
        item
        for item in compilation.diagnostics
        if item.code is OwnerDiagnosticCode.MULTIPLE_MATCH
    )
    assert diagnostic.witness is not None
    assert artifact_pattern_matches("a*b*c*", diagnostic.witness)
    assert artifact_pattern_matches("a*c*b*", diagnostic.witness)


def test_question_and_star_follow_production_cross_slash_semantics() -> None:
    for first, second in (("a?b", "a/b"), ("a*b", "a/b")):
        analysis = analyze_pattern_overlap(first, second)

        assert analysis.status is PatternOverlapStatus.OVERLAP
        assert analysis.witness is not None
        assert artifact_pattern_matches(first, analysis.witness)
        assert artifact_pattern_matches(second, analysis.witness)
        assert analysis.explored_states <= analysis.state_limit


def test_character_classes_remain_explicitly_unanalyzable() -> None:
    analysis = analyze_pattern_overlap("models/[ab]/**", "models/a/file.py")

    assert analysis.status is PatternOverlapStatus.UNANALYZABLE
    assert analysis.witness is None
    assert "character-class" in analysis.explanation


@pytest.mark.parametrize(
    ("first", "second"),
    (
        ("models/[ab]/**", "models/a/file.py"),
        (r"models\**", "models/file.py"),
        ("paper//*.tex", "paper/a.tex"),
        ("./paper/*.tex", "paper/a.tex"),
        ("paper/", "paper"),
        ("", "paper"),
    ),
)
def test_normalization_sensitive_static_inputs_fail_closed(
    first: str, second: str
) -> None:
    analysis = analyze_pattern_overlap(first, second)

    assert analysis.status is PatternOverlapStatus.UNANALYZABLE
    assert analysis.witness is None
    assert analysis.explored_states == 0
    assert analysis.state_limit == 0


def test_product_nfa_exhaustion_is_finite_without_a_heuristic_search_budget() -> None:
    analysis = analyze_pattern_overlap("a*b*c*d*e*f*", "z*y*x*w*v*u*")

    assert analysis.status is PatternOverlapStatus.DISJOINT
    assert 0 < analysis.explored_states <= analysis.state_limit
    assert analysis.state_limit == 5 * (len("a*b*c*d*e*f*") + 1) * (
        len("z*y*x*w*v*u*") + 1
    )


def _values(alphabet: tuple[str, ...], maximum_length: int) -> tuple[str, ...]:
    return tuple(
        "".join(characters)
        for length in range(maximum_length + 1)
        for characters in product(alphabet, repeat=length)
    )


def test_short_glob_exhaustion_has_no_overlap_false_negatives() -> None:
    patterns = tuple(
        pattern
        for pattern in _values(("a", "b", "/", "*", "?"), 3)
        if pattern
        and not pattern.endswith("/")
        and "//" not in pattern
    )
    candidate_paths = _values(("a", "b", "/"), 4)
    matches = {
        pattern: {
            path
            for path in candidate_paths
            if artifact_pattern_matches(pattern, path)
        }
        for pattern in patterns
    }

    for first, second in combinations_with_replacement(patterns, 2):
        analysis = analyze_pattern_overlap(first, second)
        observed_overlap = matches[first] & matches[second]

        assert analysis.status is not PatternOverlapStatus.UNANALYZABLE
        if observed_overlap:
            assert analysis.status is PatternOverlapStatus.OVERLAP, (
                first,
                second,
                min(observed_overlap),
            )
        if analysis.status is PatternOverlapStatus.OVERLAP:
            assert analysis.witness is not None
            assert artifact_pattern_matches(first, analysis.witness)
            assert artifact_pattern_matches(second, analysis.witness)
        else:
            assert analysis.status is PatternOverlapStatus.DISJOINT
            assert not observed_overlap
        assert analysis.explored_states <= analysis.state_limit
        assert analyze_pattern_overlap(first, second) == analysis


def test_every_current_static_overlap_witness_is_accepted_by_shared_matcher() -> None:
    compilation = compile_owner_registry()
    rules = {rule.rule_id: rule for rule in compilation.rules}

    for diagnostic in compilation.diagnostics:
        if diagnostic.code not in {
            OwnerDiagnosticCode.OVERLAP,
            OwnerDiagnosticCode.MULTIPLE_MATCH,
            OwnerDiagnosticCode.INTENTIONAL_PRIORITY,
            OwnerDiagnosticCode.SHADOWED,
        }:
            continue
        assert diagnostic.witness is not None
        assert all(
            artifact_pattern_matches(rules[rule_id].pattern, diagnostic.witness)
            for rule_id in diagnostic.rule_ids
        )
