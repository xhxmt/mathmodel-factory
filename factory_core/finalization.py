from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifact_ownership import (
    ARTIFACT_OWNERSHIP_SCHEMA,
    iter_owned_artifacts,
    reopen_after_step_for_artifact,
)


FINAL_INPUT_MANIFEST_SCHEMA = "factory-final-input-manifest-v4"


class FinalizationSnapshotChanged(RuntimeError):
    def __init__(self, changed_paths: list[str]):
        self.changed_paths = changed_paths
        super().__init__(
            "finalization input snapshot changed: " + ", ".join(changed_paths[:8])
        )


@dataclass(frozen=True)
class FinalInputSnapshot:
    fingerprint: str
    manifest_path: Path
    manifest: dict[str, Any]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _input_paths(project: Path) -> list[Path]:
    from .submission_bundle import submission_bundle_paths

    paths = set(
        submission_bundle_paths(project, project.name, require_pdf=False)
    )
    paths.update(iter_owned_artifacts(project, final_input_only=True))
    return sorted(paths, key=lambda path: path.relative_to(project).as_posix())


def build_final_input_manifest(project_dir: str | Path) -> FinalInputSnapshot:
    project = Path(project_dir).resolve()
    from .decision_receipts import verified_approval_receipts
    from .submission_bundle import submission_bundle_manifest

    planned_bundle = submission_bundle_manifest(
        project, project.name, require_pdf=False
    )
    files = [
        {
            "path": path.relative_to(project).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in _input_paths(project)
    ]
    identity = {
        "schema_version": FINAL_INPUT_MANIFEST_SCHEMA,
        "artifact_ownership_schema": ARTIFACT_OWNERSHIP_SCHEMA,
        "base": project.name,
        "files": files,
        "planned_submission_bundle": planned_bundle,
        "approval_receipts": verified_approval_receipts(project),
    }
    fingerprint = _canonical_hash(identity)
    manifest = {**identity, "fingerprint": fingerprint}
    path = project / ".factory" / "finalization" / "input_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return FinalInputSnapshot(fingerprint, path, manifest)


def verify_final_input_snapshot(
    project_dir: str | Path, snapshot: FinalInputSnapshot
) -> None:
    project = Path(project_dir).resolve()
    current_records = {
        path.relative_to(project).as_posix(): {
            "path": path.relative_to(project).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in _input_paths(project)
    }
    expected_records = {
        str(item["path"]): item for item in snapshot.manifest.get("files", [])
    }
    changed = sorted(
        path
        for path in set(current_records) | set(expected_records)
        if current_records.get(path) != expected_records.get(path)
    )
    from .submission_bundle import submission_bundle_manifest
    from .decision_receipts import verified_approval_receipts

    current_bundle = submission_bundle_manifest(
        project, project.name, require_pdf=False
    )
    if (
        current_bundle != snapshot.manifest.get("planned_submission_bundle")
        and not changed
    ):
        changed.append("<submission-bundle-manifest>")
    try:
        current_approvals = verified_approval_receipts(project)
    except ValueError as exc:
        raise FinalizationSnapshotChanged(["<approval-receipts>"]) from exc
    if (
        current_approvals != snapshot.manifest.get("approval_receipts")
        and not changed
    ):
        changed.append("<approval-receipts>")
    identity = {
        "schema_version": FINAL_INPUT_MANIFEST_SCHEMA,
        "artifact_ownership_schema": ARTIFACT_OWNERSHIP_SCHEMA,
        "base": project.name,
        "files": [current_records[path] for path in sorted(current_records)],
        "planned_submission_bundle": current_bundle,
        "approval_receipts": current_approvals,
    }
    if _canonical_hash(identity) != snapshot.fingerprint and not changed:
        changed = ["<manifest-identity>"]
    if changed:
        raise FinalizationSnapshotChanged(changed)


def reopen_after_for_changed_paths(paths: list[str]) -> int:
    target = 13
    for relative in paths:
        target = min(
            target,
            reopen_after_step_for_artifact(relative, default_stage=3),
        )
    return target
