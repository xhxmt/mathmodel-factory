from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Iterable

ARTIFACT_OWNERSHIP_SCHEMA = "factory-artifact-ownership-v1"


@dataclass(frozen=True)
class ArtifactOwnership:
    pattern: str
    owner_stage: int
    semantic_domain: str
    dirty_flag: str
    final_input: bool = True
    submission_member: bool = True


# Order is part of the contract: narrow business artifacts precede their broad
# directory fallbacks.  This is the single ownership table used for dirty
# routing, finalization recovery, Judge missing-evidence routing, diagnostics,
# and final/submission input collection.
ARTIFACT_OWNERSHIP_REGISTRY: tuple[ArtifactOwnership, ...] = (
    # Stage 1: problem understanding and viability.
    ArtifactOwnership("problem/**", 1, "problem_contract", "MODEL_DIRTY"),
    ArtifactOwnership("data/raw/**", 1, "problem_input", "MODEL_DIRTY"),
    ArtifactOwnership("research_brief.md", 1, "problem_research", "MODEL_DIRTY"),
    ArtifactOwnership("viable_streams.md", 1, "model_candidates", "MODEL_DIRTY"),
    ArtifactOwnership("viability_gate.md", 1, "model_candidates", "MODEL_DIRTY"),
    ArtifactOwnership("kill_memo.md", 1, "model_candidates", "MODEL_DIRTY"),
    # Stage 2: proposal tournament and the SQLite-backed selection projection.
    ArtifactOwnership("m*_spec.md", 2, "model_candidate", "MODEL_DIRTY"),
    ArtifactOwnership("m*_critique.md", 2, "model_candidate", "MODEL_DIRTY"),
    ArtifactOwnership("m*_demo_result.*", 2, "model_candidate", "MODEL_DIRTY"),
    ArtifactOwnership("method_decision.md", 2, "method_selection_projection", "MODEL_DIRTY"),
    ArtifactOwnership("chosen_method.md", 2, "method_selection_projection", "MODEL_DIRTY"),
    # Stage 3: promoted model contract and executable implementation.
    ArtifactOwnership("models/**", 3, "model_contract", "MODEL_DIRTY"),
    ArtifactOwnership("scripts/**", 3, "model_implementation", "MODEL_DIRTY"),
    ArtifactOwnership("model.md", 3, "model_contract", "MODEL_DIRTY"),
    ArtifactOwnership("modeling_guide.md", 3, "modeling_guidance", "MODEL_DIRTY"),
    ArtifactOwnership("modeling_scope_gate.md", 3, "model_contract", "MODEL_DIRTY"),
    ArtifactOwnership("quality_contract.json", 3, "model_contract", "MODEL_DIRTY"),
    ArtifactOwnership("claim_registry.json", 3, "model_contract", "MODEL_DIRTY"),
    ArtifactOwnership("symbol_table.md", 3, "model_contract", "MODEL_DIRTY"),
    ArtifactOwnership("assumption_ledger.md", 3, "model_contract", "MODEL_DIRTY"),
    # Stage 5-specific result validation must precede the generic result tree.
    ArtifactOwnership("sensitivity_report.md", 5, "model_validation", "RESULT_DIRTY"),
    ArtifactOwnership("evaluation.md", 5, "model_validation", "RESULT_DIRTY"),
    ArtifactOwnership("figures/sensitivity_*", 5, "model_validation", "RESULT_DIRTY"),
    ArtifactOwnership("results/sensitivity/**", 5, "model_validation", "RESULT_DIRTY"),
    ArtifactOwnership("results/**/*sensitivity*", 5, "model_validation", "RESULT_DIRTY"),
    ArtifactOwnership("results/**/*robustness*", 5, "model_validation", "RESULT_DIRTY"),
    # Stage 4: adopted numerical truth and solver evidence.
    ArtifactOwnership("results/canonical_results.json", 4, "canonical_result", "RESULT_DIRTY"),
    ArtifactOwnership("results/**/values.json", 4, "canonical_result", "RESULT_DIRTY"),
    ArtifactOwnership("results/invariants.json", 4, "canonical_result", "RESULT_DIRTY"),
    ArtifactOwnership("results/derived_artifacts.json", 4, "canonical_result", "RESULT_DIRTY"),
    ArtifactOwnership("results/**/bound.json", 4, "solver_evidence", "RESULT_DIRTY"),
    ArtifactOwnership("results/**/convergence.json", 4, "solver_evidence", "RESULT_DIRTY"),
    ArtifactOwnership("results/**/cross_check.json", 4, "solver_evidence", "RESULT_DIRTY"),
    ArtifactOwnership("results/**/solver.log", 4, "solver_evidence", "RESULT_DIRTY"),
    ArtifactOwnership("results/**/plots.pdf", 4, "canonical_result", "RESULT_DIRTY"),
    ArtifactOwnership("results/**/*provenance*", 4, "canonical_result", "RESULT_DIRTY"),
    ArtifactOwnership("results/**/*source_mapping*", 4, "canonical_result", "RESULT_DIRTY"),
    ArtifactOwnership("results/**/*adopted_objective*", 4, "canonical_result", "RESULT_DIRTY"),
    ArtifactOwnership("results/**/*decision_variable*", 4, "canonical_result", "RESULT_DIRTY"),
    ArtifactOwnership("results/**/*solver_evidence*", 4, "canonical_result", "RESULT_DIRTY"),
    ArtifactOwnership("data/final/**", 4, "canonical_result", "RESULT_DIRTY"),
    ArtifactOwnership(
        "run_state/solver_jobs/**",
        4,
        "solver_evidence",
        "RESULT_DIRTY",
        submission_member=False,
    ),
    ArtifactOwnership(
        ".factory/solver_receipts/**",
        4,
        "solver_evidence",
        "RESULT_DIRTY",
        submission_member=False,
    ),
    ArtifactOwnership("solve_log.md", 4, "solver_evidence_projection", "RESULT_DIRTY"),
    ArtifactOwnership("result*.xlsx", 4, "declared_result", "RESULT_DIRTY"),
    # Non-canonical result products are rebuildable presentation artifacts.
    ArtifactOwnership("results/**", 9, "result_projection", "FORMAT_DIRTY"),
    # Stage 6: figure narrative and reviewer-entry gate.
    ArtifactOwnership("reviewer_entry_map.md", 6, "reviewer_entry", "VISUAL_DIRTY"),
    ArtifactOwnership("anchor_figure_plan.md", 6, "reviewer_entry", "VISUAL_DIRTY"),
    ArtifactOwnership("entry_gate.md", 6, "reviewer_entry", "VISUAL_DIRTY"),
    ArtifactOwnership("visualization_log.md", 6, "visualization", "VISUAL_DIRTY"),
    ArtifactOwnership("figures/**", 6, "visualization", "VISUAL_DIRTY"),
    # Stage 7/8 authored review boundaries.
    ArtifactOwnership("code_review.md", 7, "paper_audit", "PROSE_DIRTY"),
    ArtifactOwnership("review_comments.md", 8, "paper_review", "PROSE_DIRTY"),
    ArtifactOwnership("revision_summary.md", 8, "paper_review", "PROSE_DIRTY"),
    ArtifactOwnership(
        "judge_evaluation.md",
        8,
        "math_preflight_projection",
        "MATH_DIRTY",
        final_input=False,
        submission_member=False,
    ),
    ArtifactOwnership("audit_issue_ledger.md", 8, "issue_ledger", "MATH_DIRTY"),
    # Active paper sources default to Stage 8 for fail-closed finalization
    # routing.  Dirty classification refines their prose/citation/format domains
    # to Stage 9 when it can prove that the mathematical fingerprint is stable.
    ArtifactOwnership(
        "*_paper.tex",
        8,
        "paper_source",
        "MATH_DIRTY",
        final_input=False,
        submission_member=False,
    ),
    ArtifactOwnership(
        "paper/**/*.tex",
        8,
        "paper_source",
        "MATH_DIRTY",
        final_input=False,
        submission_member=False,
    ),
    ArtifactOwnership(
        "paper/*.tex",
        8,
        "paper_source",
        "MATH_DIRTY",
        final_input=False,
        submission_member=False,
    ),
    # Stage 9: final prose, citations, and presentation.
    ArtifactOwnership("abstract_draft.md", 9, "final_prose", "PROSE_DIRTY"),
    ArtifactOwnership("citation_audit.md", 9, "citation", "CITATION_DIRTY"),
    ArtifactOwnership("derobotification.md", 9, "final_prose", "PROSE_DIRTY"),
    ArtifactOwnership(
        "references.bib",
        9,
        "citation",
        "CITATION_DIRTY",
        final_input=False,
        submission_member=False,
    ),
    ArtifactOwnership(
        "*.bib",
        9,
        "citation",
        "CITATION_DIRTY",
        final_input=False,
        submission_member=False,
    ),
    ArtifactOwnership("tables/**", 9, "format", "FORMAT_DIRTY"),
    ArtifactOwnership("style/**", 9, "format", "FORMAT_DIRTY"),
)


def normalize_artifact_path(path: str) -> str:
    normalized = str(PurePosixPath(str(path).replace("\\", "/")))
    return normalized.removeprefix("./")


def artifact_ownership(path: str) -> ArtifactOwnership | None:
    normalized = normalize_artifact_path(path).lower()
    for ownership in ARTIFACT_OWNERSHIP_REGISTRY:
        pattern = ownership.pattern.lower()
        patterns = {pattern}
        # ``fnmatch`` requires one component for ``**/``.  Artifact rules use
        # globstar semantics, where that component may also be absent.
        pending = [pattern]
        while pending:
            candidate = pending.pop()
            if "/**/" not in candidate:
                continue
            collapsed = candidate.replace("/**/", "/", 1)
            if collapsed not in patterns:
                patterns.add(collapsed)
                pending.append(collapsed)
        if any(fnmatchcase(normalized, candidate) for candidate in patterns):
            return ownership
    return None


def artifact_owner_stage(path: str, *, default: int | None = None) -> int | None:
    ownership = artifact_ownership(path)
    return ownership.owner_stage if ownership is not None else default


def reopen_after_step_for_artifact(path: str, *, default_stage: int = 3) -> int:
    from .stages import resume_after_step_for_stage

    owner_stage = artifact_owner_stage(path, default=default_stage)
    assert owner_stage is not None
    return resume_after_step_for_stage(owner_stage)


def iter_owned_artifacts(
    project_dir: str | Path,
    *,
    final_input_only: bool = False,
    submission_only: bool = False,
    include_symlinks: bool = False,
) -> Iterable[Path]:
    project = Path(project_dir).resolve()
    for path in sorted(project.rglob("*")):
        try:
            relative = path.relative_to(project).as_posix()
        except ValueError:  # pragma: no cover - rglob is rooted in project
            continue
        ownership = artifact_ownership(relative)
        if ownership is None or (final_input_only and not ownership.final_input):
            continue
        if submission_only and not ownership.submission_member:
            continue
        if any(part in {"archive", "__pycache__"} for part in Path(relative).parts):
            continue
        if path.is_symlink():
            if include_symlinks:
                yield path
            continue
        if not path.is_file():
            continue
        yield path
