from __future__ import annotations

from enum import Enum
from pathlib import PurePosixPath


class ArtifactLayer(str, Enum):
    BUSINESS_TRUTH = "business_truth"
    MACHINE_EVIDENCE = "machine_evidence"
    REBUILDABLE_PROJECTION = "rebuildable_projection"


_BUSINESS_NAMES = {
    "problem_contract.json",
    "model_contract.json",
    "canonical_results.json",
    "paper.tex",
    "issue_ledger.json",
}
_PROJECTION_NAMES = {
    "checkpoint.md",
    "chosen_method.md",
    "method_decision.md",
    "solve_log.md",
    "verification_summary.md",
    "status.json",
}


def classify_artifact(path: str) -> ArtifactLayer:
    """Classify contract artifacts; unknown authored files remain business truth."""

    normalized = str(PurePosixPath(path.replace("\\", "/"))).lower()
    name = PurePosixPath(normalized).name
    if name in _PROJECTION_NAMES:
        return ArtifactLayer.REBUILDABLE_PROJECTION
    if (
        "receipt" in name
        or "receipts/" in normalized
        or "snapshot" in name
        or "fingerprint" in name
    ):
        return ArtifactLayer.MACHINE_EVIDENCE
    if name in _BUSINESS_NAMES or normalized.startswith("selection/decisions/"):
        return ArtifactLayer.BUSINESS_TRUTH
    return ArtifactLayer.BUSINESS_TRUTH


ARTIFACT_LAYER_CONTRACT = {
    ArtifactLayer.BUSINESS_TRUTH.value: (
        "problem contract",
        "structured selection decisions",
        "model contract",
        "canonical results",
        "paper.tex",
        "issue ledger",
    ),
    ArtifactLayer.MACHINE_EVIDENCE.value: (
        "solver receipts",
        "audit receipts",
        "snapshot hashes",
        "final acceptance receipt",
    ),
    ArtifactLayer.REBUILDABLE_PROJECTION.value: (
        "checkpoint.md",
        "chosen_method.md",
        "solve_log.md",
        "verification summaries",
        "Web runtime status",
    ),
}
