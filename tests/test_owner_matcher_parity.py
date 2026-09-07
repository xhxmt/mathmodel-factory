from __future__ import annotations

from pathlib import Path

import pytest

from factory_core.artifact_ownership import (
    ARTIFACT_OWNERSHIP_REGISTRY,
    artifact_ownership,
    artifact_pattern_matches,
)
from factory_core.owner_compiler import compile_owner_registry, resolve_owner


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    (
        ("models/**/solver.py", "models/solver.py", True),
        ("models/**/solver.py", "models/a/b/solver.py", True),
        ("models/*.py", "models/nested/solver.py", True),
        ("m?_spec.md", "M1_SPEC.MD", True),
        ("models/**", r".\MODELS\nested\solver.py", True),
        ("results/file?.json", "results/file1.json", True),
        ("results/file?.json", "results/file10.json", False),
        ("literal[ab].txt", "literala.txt", True),
        ("literal[ab].txt", "literal[ab].txt", False),
        ("results/a+b.json", "results/a+b.json", True),
        ("results/a+b.json", "results/ab.json", False),
        ("method_fit_suggestions.json", "method_fit_suggestions.json", True),
        ("STEP5_RECEIPT.json", "STEP5_RECEIPT.json", True),
    ),
)
def test_public_owner_matcher_preserves_v1_path_and_fnmatch_semantics(
    pattern: str, path: str, expected: bool
) -> None:
    assert artifact_pattern_matches(pattern, path) is expected


def _materialized_paths(pattern: str) -> tuple[str, ...]:
    variants = {pattern}
    pending = [pattern]
    while pending:
        candidate = pending.pop()
        if "/**/" not in candidate:
            continue
        collapsed = candidate.replace("/**/", "/", 1)
        if collapsed not in variants:
            variants.add(collapsed)
            pending.append(collapsed)

    paths = set()
    for variant in variants:
        candidate = variant.replace("**", "segment/deep")
        candidate = candidate.replace("*", "sample").replace("?", "q")
        paths.add(candidate)
    return tuple(sorted(paths))


def test_compiler_first_match_equals_runtime_for_every_current_registry_pattern() -> None:
    compilation = compile_owner_registry()

    for source_rule in ARTIFACT_OWNERSHIP_REGISTRY:
        for path in _materialized_paths(source_rule.pattern):
            assert artifact_pattern_matches(source_rule.pattern, path), (
                source_rule.pattern,
                path,
            )
            legacy = artifact_ownership(path)
            compiled = resolve_owner(compilation, path)
            assert legacy is not None, (source_rule.pattern, path)
            assert compiled.all_matches, (source_rule.pattern, path)
            assert compiled.all_matches[0].pattern == legacy.pattern
            assert compiled.resolved_owner_stage == legacy.owner_stage


def test_normal_root_evidence_has_explicit_ownership() -> None:
    assert artifact_ownership("method_fit_suggestions.json").owner_stage == 1
    assert artifact_ownership("STEP5_RECEIPT.json").owner_stage == 4


def test_owner_compiler_contains_no_independent_fnmatch_or_globstar_implementation() -> None:
    source = (ROOT / "factory_core" / "owner_compiler.py").read_text(encoding="utf-8")

    assert "from fnmatch import" not in source
    assert "def _artifact_pattern_matches" not in source
    assert "def _globstar_variants" not in source
