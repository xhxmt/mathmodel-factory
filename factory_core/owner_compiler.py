from __future__ import annotations

import heapq
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from .artifact_ownership import (
    ARTIFACT_OWNERSHIP_REGISTRY,
    ARTIFACT_OWNERSHIP_SCHEMA,
    ArtifactOwnership,
    artifact_pattern_matches,
    artifact_pattern_variants,
    normalize_artifact_path,
)
from .canonical import canonical_sha256


OWNER_COMPILER_SCHEMA = "compatibility-owner-compiler-v1"
OWNER_COMPILER_MODE = "ordered-first-match-v1"


class OwnerDiagnosticCode(str, Enum):
    NO_OWNER = "NO_OWNER"
    OVERLAP = "OVERLAP"
    MULTIPLE_MATCH = "MULTIPLE_MATCH"
    SHADOWED = "SHADOWED"
    UNREACHABLE = "UNREACHABLE"
    INTENTIONAL_PRIORITY = "INTENTIONAL_PRIORITY"
    UNANALYZABLE = "UNANALYZABLE"


class PatternOverlapStatus(str, Enum):
    OVERLAP = "OVERLAP"
    DISJOINT = "DISJOINT"
    UNANALYZABLE = "UNANALYZABLE"


@dataclass(frozen=True)
class PatternOverlapAnalysis:
    status: PatternOverlapStatus
    witness: str | None
    explanation: str
    explored_states: int
    state_limit: int


@dataclass(frozen=True)
class OwnerPriorityAuthorization:
    winner_pattern: str
    winner_owner_stage: int
    loser_pattern: str
    loser_owner_stage: int
    issue_id: str
    rationale: str


_RUN4_PRIORITY_RATIONALE = (
    "Run4 established that this later-stage narrow artifact family is the final "
    "semantic owner and must precede the named broad compatibility fallback."
)
_RESULT_PRIORITY_RATIONALE = (
    "Canonical numerical truth or solver evidence must precede the named broad "
    "result-projection fallback."
)


def _priority(
    winner_pattern: str,
    winner_owner_stage: int,
    loser_pattern: str,
    loser_owner_stage: int,
    issue_id: str,
    rationale: str,
) -> OwnerPriorityAuthorization:
    return OwnerPriorityAuthorization(
        winner_pattern=winner_pattern,
        winner_owner_stage=winner_owner_stage,
        loser_pattern=loser_pattern,
        loser_owner_stage=loser_owner_stage,
        issue_id=issue_id,
        rationale=rationale,
    )


OWNER_PRIORITY_AUTHORIZATIONS: tuple[OwnerPriorityAuthorization, ...] = (
    *(
        _priority(pattern, owner_stage, "models/**", 3, "RUN4-OWNER-001", _RUN4_PRIORITY_RATIONALE)
        for pattern, owner_stage in (
            ("models/**/07_bootstrap_convergence.py", 8),
            ("models/**/07_bootstrap_convergence.log", 8),
            ("models/**/08_block_length_extended.py", 8),
            ("models/**/*.log", 5),
            ("models/**/05_sensitivity.py", 5),
            ("models/**/05_peak_rule_sensitivity.py", 5),
            ("models/**/06_figures.py", 6),
            ("models/generate_derived.py", 9),
            ("models/**/*.stub", 4),
        )
    ),
    _priority(
        "models/**/07_bootstrap_convergence.log",
        8,
        "models/**/*.log",
        5,
        "RUN4-OWNER-001",
        _RUN4_PRIORITY_RATIONALE,
    ),
    _priority(
        "scripts/step5/**",
        4,
        "scripts/**",
        3,
        "RUN4-OWNER-001",
        _RUN4_PRIORITY_RATIONALE,
    ),
    *(
        _priority(pattern, owner_stage, "results/**", 9, issue_id, rationale)
        for pattern, owner_stage, issue_id, rationale in (
            ("results/sensitivity/**", 5, "RUN4-OWNER-001", _RUN4_PRIORITY_RATIONALE),
            ("results/**/*sensitivity*", 5, "RUN4-OWNER-001", _RUN4_PRIORITY_RATIONALE),
            ("results/**/*robustness*", 5, "RUN4-OWNER-001", _RUN4_PRIORITY_RATIONALE),
            ("results/**/bootstrap_convergence*.json", 8, "RUN4-OWNER-001", _RUN4_PRIORITY_RATIONALE),
            ("results/**/block_length_extended*.json", 8, "RUN4-OWNER-001", _RUN4_PRIORITY_RATIONALE),
            ("results/canonical_results.json", 4, "OWNER-RESULT-001", _RESULT_PRIORITY_RATIONALE),
            ("results/**/values.json", 4, "OWNER-RESULT-001", _RESULT_PRIORITY_RATIONALE),
            ("results/invariants.json", 4, "OWNER-RESULT-001", _RESULT_PRIORITY_RATIONALE),
            ("results/**/bound.json", 4, "OWNER-RESULT-001", _RESULT_PRIORITY_RATIONALE),
            ("results/**/convergence.json", 4, "OWNER-RESULT-001", _RESULT_PRIORITY_RATIONALE),
            ("results/**/cross_check.json", 4, "OWNER-RESULT-001", _RESULT_PRIORITY_RATIONALE),
            ("results/**/solver.log", 4, "OWNER-RESULT-001", _RESULT_PRIORITY_RATIONALE),
            ("results/**/plots.pdf", 4, "OWNER-RESULT-001", _RESULT_PRIORITY_RATIONALE),
            ("results/**/*provenance*", 4, "OWNER-RESULT-001", _RESULT_PRIORITY_RATIONALE),
            ("results/**/*source_mapping*", 4, "OWNER-RESULT-001", _RESULT_PRIORITY_RATIONALE),
            ("results/**/*adopted_objective*", 4, "OWNER-RESULT-001", _RESULT_PRIORITY_RATIONALE),
            ("results/**/*decision_variable*", 4, "OWNER-RESULT-001", _RESULT_PRIORITY_RATIONALE),
            ("results/**/*solver_evidence*", 4, "OWNER-RESULT-001", _RESULT_PRIORITY_RATIONALE),
        )
    ),
)


@dataclass(frozen=True)
class OwnerRuleContract:
    rule_id: str
    priority_index: int
    pattern: str
    owner_id: str
    owner_stage: int
    semantic_domain: str
    dirty_flag: str
    final_input: bool
    submission_member: bool
    priority_authorizations: tuple[OwnerPriorityAuthorization, ...]


@dataclass(frozen=True)
class OwnerDiagnostic:
    code: OwnerDiagnosticCode
    rule_ids: tuple[str, ...] = ()
    path: str | None = None
    witness: str | None = None
    issue_id: str | None = None
    rationale: str | None = None
    explanation: str = ""


@dataclass(frozen=True)
class OwnerCompilation:
    schema_version: str
    mode: str
    ownership_schema_version: str
    rules: tuple[OwnerRuleContract, ...]
    diagnostics: tuple[OwnerDiagnostic, ...]


@dataclass(frozen=True)
class OwnerResolution:
    path: str
    normalized_path: str
    all_matches: tuple[OwnerRuleContract, ...]
    resolved_rule_id: str | None
    resolved_owner_id: str | None
    resolved_owner_stage: int | None
    diagnostics: tuple[OwnerDiagnostic, ...]


class OwnerContractValidationError(ValueError):
    """Raised when strict owner validation encounters an unsafe contract."""


def _rule_id(
    source: ArtifactOwnership,
    authorizations: tuple[OwnerPriorityAuthorization, ...],
) -> str:
    identity = {
        "pattern": source.pattern,
        "owner_id": f"owner:stage:{int(source.owner_stage)}",
        "semantic_domain": source.semantic_domain,
        "dirty_flag": source.dirty_flag,
        "final_input": bool(source.final_input),
        "submission_member": bool(source.submission_member),
        "priority_authorizations": authorizations,
    }
    return f"owner-rule:sha256:{canonical_sha256(identity)}"


def _compiled_rule(
    source: ArtifactOwnership,
    priority_index: int,
    authorizations: tuple[OwnerPriorityAuthorization, ...],
) -> OwnerRuleContract:
    return OwnerRuleContract(
        rule_id=_rule_id(source, authorizations),
        priority_index=int(priority_index),
        pattern=str(source.pattern),
        owner_id=f"owner:stage:{int(source.owner_stage)}",
        owner_stage=int(source.owner_stage),
        semantic_domain=str(source.semantic_domain),
        dirty_flag=str(source.dirty_flag),
        final_input=bool(source.final_input),
        submission_member=bool(source.submission_member),
        priority_authorizations=authorizations,
    )


def _unsupported_static_reason(pattern: str) -> str | None:
    if "[" in pattern or "]" in pattern:
        return (
            "character-class glob syntax is supported by runtime fnmatch but "
            "outside the literal/*/? static intersection analyzer"
        )
    if "\\" in pattern:
        return "backslash pattern literals conflict with v1 path normalization"
    components = pattern.split("/")
    if (
        not pattern
        or pattern.endswith("/")
        or "//" in pattern
        or "." in components
    ):
        return (
            "pattern uses a path-normalization-sensitive empty, slash, or dot "
            "component outside the static intersection analyzer"
        )
    return None


def _token_transition(token: str, index: int) -> tuple[str | None, int]:
    if token == "*":
        return None, index
    if token == "?":
        return None, index + 1
    return token, index + 1


def _plain_glob_intersection_witness(
    first: str, second: str
) -> tuple[str | None, int, int]:
    """Intersect two literal/*/? glob NFAs with a finite product search.

    A state is ``(first_index, second_index, path_shape)``. The five path-shape
    states distinguish start, one/two leading slashes, body, and a body trailing
    slash. Each component NFA has ``len(pattern) + 1`` positions, so at most
    ``5 * (len(first)+1) * (len(second)+1)`` states can be explored. Every state
    is finalized once by the deterministic
    shortest-then-lexicographic heap order. ``*`` consumes across ``/`` exactly
    as production ``fnmatchcase`` does.
    """

    start, leading_one, leading_two, body, body_slash = range(5)
    accepting_shapes = {start, leading_one, leading_two, body}

    def advance_path_shape(shape: int, character: str) -> int | None:
        if shape == start:
            return leading_one if character == "/" else body
        if shape == leading_one:
            return leading_two if character == "/" else body
        if shape == leading_two:
            return None if character == "/" else body
        if shape == body:
            return body_slash if character == "/" else body
        return None if character == "/" else body

    state_limit = 5 * (len(first) + 1) * (len(second) + 1)
    pending: list[tuple[int, str, int, int, int]] = [(0, "", 0, 0, start)]
    visited: set[tuple[int, int, int]] = set()
    while pending:
        _length, witness, first_index, second_index, path_shape = heapq.heappop(
            pending
        )
        state = (first_index, second_index, path_shape)
        if state in visited:
            continue
        visited.add(state)
        if first_index == len(first) and second_index == len(second):
            if path_shape in accepting_shapes:
                return witness, len(visited), state_limit
            continue

        first_token = first[first_index] if first_index < len(first) else None
        second_token = second[second_index] if second_index < len(second) else None
        if first_token == "*":
            heapq.heappush(
                pending,
                (len(witness), witness, first_index + 1, second_index, path_shape),
            )
        if second_token == "*":
            heapq.heappush(
                pending,
                (len(witness), witness, first_index, second_index + 1, path_shape),
            )
        if first_token is None or second_token is None:
            continue

        first_literal, next_first = _token_transition(first_token, first_index)
        second_literal, next_second = _token_transition(second_token, second_index)
        if (
            first_literal is not None
            and second_literal is not None
            and first_literal != second_literal
        ):
            continue
        characters = (
            (first_literal or second_literal,)
            if first_literal is not None or second_literal is not None
            else ("/", "a")
        )
        for character in characters:
            assert character is not None
            next_path_shape = advance_path_shape(path_shape, character)
            if next_path_shape is None:
                continue
            next_state = (next_first, next_second, next_path_shape)
            if next_state == state:
                continue
            next_witness = witness + character
            heapq.heappush(
                pending,
                (
                    len(next_witness),
                    next_witness,
                    next_first,
                    next_second,
                    next_path_shape,
                ),
            )
    return None, len(visited), state_limit


def analyze_pattern_overlap(first: str, second: str) -> PatternOverlapAnalysis:
    """Return exact overlap evidence or an explicit conservative outcome."""

    for pattern in (str(first), str(second)):
        if reason := _unsupported_static_reason(pattern):
            return PatternOverlapAnalysis(
                status=PatternOverlapStatus.UNANALYZABLE,
                witness=None,
                explanation=reason,
                explored_states=0,
                state_limit=0,
            )

    witnesses: list[str] = []
    explored_states = 0
    state_limit = 0
    normalization_uncertain = False
    for first_variant in artifact_pattern_variants(first):
        for second_variant in artifact_pattern_variants(second):
            witness, explored, limit = _plain_glob_intersection_witness(
                first_variant, second_variant
            )
            explored_states += explored
            state_limit += limit
            if witness is None:
                continue
            if artifact_pattern_matches(first, witness) and artifact_pattern_matches(
                second, witness
            ):
                witnesses.append(witness)
            else:
                normalization_uncertain = True

    if witnesses:
        return PatternOverlapAnalysis(
            status=PatternOverlapStatus.OVERLAP,
            witness=min(witnesses, key=lambda item: (len(item), item)),
            explanation="deterministic product-NFA witness accepted by v1 matcher",
            explored_states=explored_states,
            state_limit=state_limit,
        )
    if normalization_uncertain:
        return PatternOverlapAnalysis(
            status=PatternOverlapStatus.UNANALYZABLE,
            witness=None,
            explanation=(
                "raw glob languages overlap but path normalization prevented a "
                "verified production-matcher witness"
            ),
            explored_states=explored_states,
            state_limit=state_limit,
        )
    return PatternOverlapAnalysis(
        status=PatternOverlapStatus.DISJOINT,
        witness=None,
        explanation="finite product-NFA intersection is empty",
        explored_states=explored_states,
        state_limit=state_limit,
    )


def _overlap_witness(first: str, second: str) -> str | None:
    analysis = analyze_pattern_overlap(first, second)
    return analysis.witness if analysis.status is PatternOverlapStatus.OVERLAP else None


def _has_glob(pattern: str) -> bool:
    return any(character in pattern for character in "*?[")


def _is_subset(later: str, earlier: str) -> bool:
    if later == earlier:
        return True
    if not _has_glob(later):
        return artifact_pattern_matches(earlier, later)
    if later.lower() in artifact_pattern_variants(earlier):
        return True
    if earlier in {"*", "**"}:
        return True
    if earlier.endswith("/**"):
        prefix = earlier[:-3]
        return later.startswith(prefix + "/")
    return False


def _priority_for_pair(
    winner: OwnerRuleContract, loser: OwnerRuleContract
) -> OwnerPriorityAuthorization | None:
    return next(
        (
            authorization
            for authorization in winner.priority_authorizations
            if authorization.winner_pattern == winner.pattern
            and authorization.winner_owner_stage == winner.owner_stage
            and authorization.loser_pattern == loser.pattern
            and authorization.loser_owner_stage == loser.owner_stage
            and bool(authorization.issue_id)
            and bool(authorization.rationale)
        ),
        None,
    )


def _pair_diagnostics(
    winner: OwnerRuleContract, later: OwnerRuleContract
) -> tuple[OwnerDiagnostic, ...]:
    overlap = analyze_pattern_overlap(winner.pattern, later.pattern)
    if overlap.status is PatternOverlapStatus.UNANALYZABLE:
        return (
            OwnerDiagnostic(
                code=OwnerDiagnosticCode.UNANALYZABLE,
                rule_ids=(winner.rule_id, later.rule_id),
                explanation=overlap.explanation,
            ),
        )
    if overlap.status is PatternOverlapStatus.DISJOINT:
        return ()
    witness = overlap.witness
    assert witness is not None

    diagnostics: list[OwnerDiagnostic] = []
    if winner.owner_id == later.owner_id:
        diagnostics.append(
            OwnerDiagnostic(
                code=OwnerDiagnosticCode.OVERLAP,
                rule_ids=(winner.rule_id, later.rule_id),
                witness=witness,
                explanation="two ordered rules match the same path and resolve to one owner",
            )
        )
    elif (authorization := _priority_for_pair(winner, later)) is not None:
        diagnostics.append(
            OwnerDiagnostic(
                code=OwnerDiagnosticCode.INTENTIONAL_PRIORITY,
                rule_ids=(winner.rule_id, later.rule_id),
                witness=witness,
                issue_id=authorization.issue_id,
                rationale=authorization.rationale,
                explanation="first-match compatibility is explicitly authorized by rule metadata",
            )
        )
    else:
        diagnostics.append(
            OwnerDiagnostic(
                code=OwnerDiagnosticCode.MULTIPLE_MATCH,
                rule_ids=(winner.rule_id, later.rule_id),
                witness=witness,
                explanation="multiple owner IDs match and no machine-readable priority exists",
            )
        )

    if _is_subset(later.pattern, winner.pattern):
        diagnostics.extend(
            (
                OwnerDiagnostic(
                    code=OwnerDiagnosticCode.SHADOWED,
                    rule_ids=(winner.rule_id, later.rule_id),
                    witness=witness,
                    explanation="an earlier rule covers the complete later rule language",
                ),
                OwnerDiagnostic(
                    code=OwnerDiagnosticCode.UNREACHABLE,
                    rule_ids=(later.rule_id,),
                    witness=witness,
                    explanation="ordered first-match can never select this later rule",
                ),
            )
        )
    return tuple(diagnostics)


def compile_owner_registry(
    registry: Iterable[ArtifactOwnership] = ARTIFACT_OWNERSHIP_REGISTRY,
    *,
    priority_authorizations: Iterable[OwnerPriorityAuthorization] | None = None,
) -> OwnerCompilation:
    """Compile owner truth without reading the filesystem or runtime state."""

    use_current_authorizations = (
        priority_authorizations is None and registry is ARTIFACT_OWNERSHIP_REGISTRY
    )
    authorizations = tuple(
        OWNER_PRIORITY_AUTHORIZATIONS
        if use_current_authorizations
        else (priority_authorizations or ())
    )
    authorizations = tuple(
        sorted(
            authorizations,
            key=lambda item: (
                item.winner_pattern,
                item.winner_owner_stage,
                item.loser_pattern,
                item.loser_owner_stage,
                item.issue_id,
                item.rationale,
            ),
        )
    )
    rules = tuple(
        _compiled_rule(
            source,
            index,
            tuple(
                authorization
                for authorization in authorizations
                if authorization.winner_pattern == source.pattern
                and authorization.winner_owner_stage == source.owner_stage
            ),
        )
        for index, source in enumerate(tuple(registry))
    )
    diagnostics: list[OwnerDiagnostic] = []
    for authorization in authorizations:
        winner_indexes = [
            rule.priority_index
            for rule in rules
            if rule.pattern == authorization.winner_pattern
            and rule.owner_stage == authorization.winner_owner_stage
        ]
        loser_indexes = [
            rule.priority_index
            for rule in rules
            if rule.pattern == authorization.loser_pattern
            and rule.owner_stage == authorization.loser_owner_stage
        ]
        if (
            len(winner_indexes) != 1
            or len(loser_indexes) != 1
            or winner_indexes[0] >= loser_indexes[0]
            or not authorization.issue_id
            or not authorization.rationale
        ):
            diagnostics.append(
                OwnerDiagnostic(
                    code=OwnerDiagnosticCode.UNANALYZABLE,
                    issue_id=authorization.issue_id or None,
                    rationale=authorization.rationale or None,
                    explanation=(
                        "priority authorization must bind one earlier winner and "
                        "one later loser using exact pattern and owner identities"
                    ),
                )
            )
    for index, winner in enumerate(rules):
        for later in rules[index + 1 :]:
            diagnostics.extend(_pair_diagnostics(winner, later))
    return OwnerCompilation(
        schema_version=OWNER_COMPILER_SCHEMA,
        mode=OWNER_COMPILER_MODE,
        ownership_schema_version=ARTIFACT_OWNERSHIP_SCHEMA,
        rules=rules,
        diagnostics=tuple(diagnostics),
    )


def resolve_owner(compilation: OwnerCompilation, path: str) -> OwnerResolution:
    normalized = normalize_artifact_path(path)
    matches = tuple(
        rule
        for rule in compilation.rules
        if artifact_pattern_matches(rule.pattern, normalized)
    )
    diagnostics: list[OwnerDiagnostic] = []
    if not matches:
        diagnostics.append(
            OwnerDiagnostic(
                code=OwnerDiagnosticCode.NO_OWNER,
                path=normalized,
                explanation="no compiled ownership rule matches this artifact path",
            )
        )
    elif len(matches) > 1:
        winner = matches[0]
        for loser in matches[1:]:
            authorization = _priority_for_pair(winner, loser)
            if winner.owner_id == loser.owner_id:
                code = OwnerDiagnosticCode.OVERLAP
            elif authorization is not None:
                code = OwnerDiagnosticCode.INTENTIONAL_PRIORITY
            else:
                code = OwnerDiagnosticCode.MULTIPLE_MATCH
            diagnostics.append(
                OwnerDiagnostic(
                    code=code,
                    rule_ids=(winner.rule_id, loser.rule_id),
                    path=normalized,
                    witness=normalized,
                    issue_id=(authorization.issue_id if authorization else None),
                    rationale=(authorization.rationale if authorization else None),
                    explanation=(
                        "pair-scoped diagnostic; resolved owner preserves v1 first-match"
                    ),
                )
            )
    winner = matches[0] if matches else None
    return OwnerResolution(
        path=str(path),
        normalized_path=normalized,
        all_matches=matches,
        resolved_rule_id=winner.rule_id if winner else None,
        resolved_owner_id=winner.owner_id if winner else None,
        resolved_owner_stage=winner.owner_stage if winner else None,
        diagnostics=tuple(diagnostics),
    )


def _raise_for_codes(
    diagnostics: Iterable[OwnerDiagnostic], forbidden: set[OwnerDiagnosticCode]
) -> None:
    present = tuple(
        sorted(
            {diagnostic.code.value for diagnostic in diagnostics if diagnostic.code in forbidden}
        )
    )
    if present:
        raise OwnerContractValidationError(
            "strict owner validation rejected diagnostics: " + ", ".join(present)
        )


def validate_owner_resolution(
    resolution: OwnerResolution, *, strict: bool = True
) -> OwnerResolution:
    if strict:
        _raise_for_codes(
            resolution.diagnostics,
            {
                OwnerDiagnosticCode.NO_OWNER,
                OwnerDiagnosticCode.MULTIPLE_MATCH,
                OwnerDiagnosticCode.SHADOWED,
                OwnerDiagnosticCode.UNREACHABLE,
                OwnerDiagnosticCode.UNANALYZABLE,
            },
        )
    return resolution


def validate_owner_compilation(
    compilation: OwnerCompilation,
    *,
    required_paths: Iterable[str] = (),
    strict: bool = True,
) -> tuple[OwnerResolution, ...]:
    if strict:
        _raise_for_codes(
            compilation.diagnostics,
            {
                OwnerDiagnosticCode.MULTIPLE_MATCH,
                OwnerDiagnosticCode.SHADOWED,
                OwnerDiagnosticCode.UNREACHABLE,
                OwnerDiagnosticCode.UNANALYZABLE,
            },
        )
    resolutions = tuple(resolve_owner(compilation, path) for path in required_paths)
    for resolution in resolutions:
        validate_owner_resolution(resolution, strict=strict)
    return resolutions
