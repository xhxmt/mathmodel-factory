"""Current native ownership, additive to the frozen M0.2/M0.3 v1 table.

The v1 module is a persisted compatibility trust root: do not edit its bytes
or indices when registering newly supported native artifacts. Current input
manifests explicitly carry this module's new schema, not the frozen identity.
"""
from pathlib import Path
from .artifact_ownership import (
    ARTIFACT_OWNERSHIP_REGISTRY as FROZEN_REGISTRY,
    ArtifactOwnership,
    artifact_pattern_matches,
    normalize_artifact_path,
)

ARTIFACT_OWNERSHIP_SCHEMA = "factory-native-artifact-ownership-v2"
ADDITIONAL_OWNERSHIP = (
    ArtifactOwnership("method_fit_suggestions.json", 1, "method_fit_reference", "MODEL_DIRTY"),
    ArtifactOwnership("STEP5_RECEIPT.json", 4, "solver_evidence_projection", "RESULT_DIRTY"),
)
ARTIFACT_OWNERSHIP_REGISTRY = FROZEN_REGISTRY + ADDITIONAL_OWNERSHIP


def artifact_ownership(path):
    return next((rule for rule in ARTIFACT_OWNERSHIP_REGISTRY
                 if artifact_pattern_matches(rule.pattern, path)), None)


def artifact_owner_stage(path, *, default=None):
    owner = artifact_ownership(path)
    return owner.owner_stage if owner is not None else default


def reopen_after_step_for_artifact(path, *, default_stage=3):
    from .stages import resume_after_step_for_stage
    return resume_after_step_for_stage(artifact_owner_stage(path, default=default_stage))


def iter_owned_artifacts(project_dir, *, final_input_only=False,
                         submission_only=False, include_symlinks=False):
    project = Path(project_dir).resolve()
    for path in sorted(project.rglob("*")):
        relative = path.relative_to(project)
        owner = artifact_ownership(relative.as_posix())
        if owner is None or (final_input_only and not owner.final_input):
            continue
        if submission_only and not owner.submission_member:
            continue
        if any(part in {"archive", "__pycache__"} for part in relative.parts):
            continue
        if path.is_symlink():
            if include_symlinks:
                yield path
        elif path.is_file():
            yield path
