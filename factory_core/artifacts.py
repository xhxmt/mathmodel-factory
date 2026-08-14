from __future__ import annotations

import hashlib
import os
import tempfile
from enum import Enum
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any


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
        "method_decision.md",
        "solve_log.md",
        "verification summaries",
        "Web runtime status",
    ),
}


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> Path:
    """Commit a file with rename semantics before linking it from SQLite."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def artifact_ref(project: Path, path: Path) -> dict[str, Any]:
    resolved_project = project.resolve()
    resolved = path.resolve(strict=True)
    relative = resolved.relative_to(resolved_project).as_posix()
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "path": relative,
        "sha256": digest.hexdigest(),
        "size": resolved.stat().st_size,
    }
